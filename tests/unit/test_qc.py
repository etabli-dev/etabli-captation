from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from captation import qc
from captation.config import Config, schema_errors
from captation.integrity import sha256_file
from captation.postprocess import finalize_session, process_segment
from captation.storage import HostInfo, SessionRecord, new_session_id
from tests.fixtures.media import make_config, make_container, needs_ffmpeg

HOST = HostInfo("linux", "test-os", "0.1.0", "33.0.3", "5.6.3")
T0 = datetime(2026, 11, 23, 14, 15, 2).astimezone()


def _session(cfg: Config) -> SessionRecord:
    rec = SessionRecord.create(
        cfg,
        session_id=new_session_id(T0, "b001"),
        app_id="signal",
        category="messenger",
        consent_ref="P-017",
        started_at=T0,
        host=HOST,
    )
    rec.begin_segment(1, "audio", "rule:default", T0, "none")
    rec.end_segment(1, T0 + timedelta(seconds=12))
    return rec


SAMPLE_STDERR = """\
[silencedetect @ 0x1] silence_start: 5
[Parsed_astats_0 @ 0x2] Channel: 1
[Parsed_astats_0 @ 0x2] Peak level dB: -12.000000
[Parsed_astats_0 @ 0x2] RMS level dB: -15.000000
[Parsed_astats_0 @ 0x2] Overall
[Parsed_astats_0 @ 0x2] Peak level dB: -12.041200
[Parsed_astats_0 @ 0x2] RMS level dB: -18.875918
[silencedetect @ 0x1] silence_end: 12 | silence_duration: 7
"""


@pytest.mark.req("R-39")
def test_parse_analysis() -> None:
    result = qc.parse_analysis(SAMPLE_STDERR, 12.0)
    assert result.peak_dbfs == -12.04
    assert result.rms_dbfs == -18.88
    assert result.longest_silence_s == 7.0
    assert result.silent_ratio == pytest.approx(7 / 12, abs=1e-3)
    # silence running to EOF without a silence_end line, and digital silence (-inf)
    eof = qc.parse_analysis(
        "[x] Overall\n[x] Peak level dB: -inf\n[x] RMS level dB: -inf\n"
        "[silencedetect @ 0x1] silence_start: 0\n",
        10.0,
    )
    assert eof.peak_dbfs is None and eof.rms_dbfs is None
    assert eof.longest_silence_s == 10.0 and eof.silent_ratio == 1.0


@pytest.mark.req("R-39")
def test_qc_warning_rules() -> None:
    silent = qc.QcResult(-30.0, -10.0, 0.6, 7.0, 12.0)
    assert qc.qc_warnings("remote", silent, 5) == ["qc:remote_silent"]
    assert qc.qc_warnings("local", silent, 5) == ["qc:local_silent"]
    assert qc.qc_warnings("mix", silent, 5) == []
    assert qc.qc_warnings("remote", silent, 30) == []
    clipped = qc.QcResult(-3.0, -0.1, 0.0, 0.0, 12.0)
    assert qc.qc_warnings("mix", clipped, 30) == ["qc:clipping"]
    assert qc.qc_warnings("mix", qc.QcResult(-3.0, -0.5, 0.0, 0.0, 1.0), 30) == []


@needs_ffmpeg
@pytest.mark.req("R-39")
def test_remote_silent_after_5_s_raises_warning(tmp_path: Path) -> None:
    cfg = make_config(tmp_path, {"qc.silence_warn_s": 5})
    rec = _session(cfg)
    # stream 1 = remote: tone for 5 s, then 7 s of digital silence
    make_container(rec.directory / "seg01_container.mkv", duration=12.0, silent_after={1: 5.0})
    process_segment(rec, 1, "audio", cfg)
    result = finalize_session(rec, cfg, status="complete")
    assert result.qc_warnings == ["qc:remote_silent"]
    doc = json.loads(rec.session_file.read_text(encoding="utf-8"))
    assert schema_errors(doc, "session.schema.json") == []
    assert "qc:remote_silent" in doc["warnings"]
    blocks = {f["role"]: f["qc"] for f in doc["segments"][0]["files"]}
    print("QC blocks:", json.dumps(blocks, indent=1))
    assert blocks["remote"]["longest_silence_s"] == pytest.approx(7.0, abs=0.1)
    assert blocks["remote"]["silent_ratio"] == pytest.approx(7 / 12, abs=0.02)
    assert blocks["mix"]["silent_ratio"] == 0.0 and blocks["local"]["silent_ratio"] == 0.0
    assert blocks["container"] is None


@needs_ffmpeg
@pytest.mark.req("R-39")
def test_local_silent_raises_warning(tmp_path: Path) -> None:
    cfg = make_config(tmp_path, {"qc.silence_warn_s": 5})
    rec = _session(cfg)
    make_container(rec.directory / "seg01_container.mkv", duration=12.0, silent_after={2: 2.0})
    process_segment(rec, 1, "audio", cfg)
    assert finalize_session(rec, cfg, status="complete").qc_warnings == ["qc:local_silent"]


@needs_ffmpeg
@pytest.mark.req("R-39")
def test_normal_file_raises_nothing(tmp_path: Path) -> None:
    cfg = make_config(tmp_path, {"qc.silence_warn_s": 5})
    rec = _session(cfg)
    make_container(rec.directory / "seg01_container.mkv", duration=12.0)
    process_segment(rec, 1, "audio", cfg)
    result = finalize_session(rec, cfg, status="complete")
    assert result.qc_warnings == [] and result.warnings == []
    doc = json.loads(rec.session_file.read_text(encoding="utf-8"))
    mix = next(f for f in doc["segments"][0]["files"] if f["role"] == "mix")
    # 0.25 * sine → peak −12.04 dBFS, RMS −15.05 dBFS
    assert mix["qc"]["peak_dbfs"] == pytest.approx(-12.04, abs=0.1)
    assert mix["qc"]["rms_dbfs"] == pytest.approx(-15.05, abs=0.1)


@needs_ffmpeg
@pytest.mark.req("R-39")
def test_clipped_sine_raises_clipping(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    rec = _session(cfg)
    make_container(rec.directory / "seg01_container.mkv", duration=4.0, amplitude=1.0)
    process_segment(rec, 1, "audio", cfg)
    result = finalize_session(rec, cfg, status="complete")
    assert result.qc_warnings == ["qc:clipping"]


@needs_ffmpeg
def test_qc_never_modifies_media(tmp_path: Path) -> None:
    path = make_container(tmp_path / "x.mkv", freqs=[440.0], duration=3.0)
    before = sha256_file(path)
    mtime = path.stat().st_mtime_ns
    result = qc.analyse(path, -60)
    assert result.duration_s == pytest.approx(3.0, abs=0.05)
    assert sha256_file(path) == before and path.stat().st_mtime_ns == mtime
    assert sorted(p.name for p in tmp_path.iterdir()) == ["x.mkv"]
