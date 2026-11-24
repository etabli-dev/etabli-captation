from __future__ import annotations

import json
import shutil
import stat
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from captation.config import Config, schema_errors
from captation.integrity import verify_sha256sums
from captation.models import Mode
from captation.postprocess import finalize_session, process_segment, track_roles
from captation.storage import HostInfo, SessionRecord, new_session_id
from tests.fixtures.media import (
    POSIX,
    ROLE_FREQS,
    WRITE_PROTECTION_ENFORCED,
    dominant_frequency,
    make_config,
    make_container,
    needs_ffmpeg,
    probe,
    stream_types,
)

pytestmark = needs_ffmpeg

HOST = HostInfo("linux", "test-os", "0.1.0", "33.0.3", "5.6.3")
T0 = datetime(2026, 11, 23, 14, 15, 2).astimezone()
FREQ_TOLERANCE_HZ = 5.0


def _session(cfg: Config, mode: Mode, rand: str = "a3f9") -> SessionRecord:
    rec = SessionRecord.create(
        cfg,
        session_id=new_session_id(T0, rand),
        app_id="zoom" if mode == "video" else "signal",
        category="meeting" if mode == "video" else "messenger",
        consent_ref="P-017",
        started_at=T0,
        host=HOST,
    )
    rec.begin_segment(1, mode, "rule:default", T0, "window" if mode == "video" else "none")
    rec.end_segment(1, T0 + timedelta(seconds=4))
    return rec


def _files(rec: SessionRecord) -> dict[str, dict[str, Any]]:
    return {f["role"]: f for f in rec.to_document()["segments"][0]["files"]}


def _assert_freq(path: Path, expected: float, audio_index: int = 0) -> float:
    found = dominant_frequency(path, audio_index)
    print(f"FFT {path.name} a:{audio_index}: {found:.1f} Hz (expected {expected:.0f})")
    assert abs(found - expected) <= FREQ_TOLERANCE_HZ
    return found


@pytest.mark.req("R-11", "R-40")
def test_track_roles_follow_track_numbers(tmp_path: Path) -> None:
    assert track_roles(make_config(tmp_path)) == ["mix", "remote", "local"]
    cfg = make_config(
        tmp_path,
        {"recording.audio.tracks": {"mix": 1, "remote": 4, "local": 2, "external": 3}},
    )
    assert track_roles(cfg) == ["mix", "local", "external", "remote"]


@pytest.mark.req("R-40", "R-43", "R-44")
def test_audio_strategy_a_extracts_each_role(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    rec = _session(cfg, "audio")
    make_container(rec.directory / "seg01_container.mkv", video="none")
    warnings = process_segment(rec, 1, "audio", cfg)
    assert warnings == []
    files = _files(rec)
    assert set(files) == {"container", "mix", "remote", "local"}
    for role in ("mix", "remote", "local"):
        entry = files[role]
        assert entry["path"] == f"seg01_{role}.flac"
        assert entry["codec"] == "flac"
        assert abs(entry["duration_s"] - 4.0) < 0.1
        _assert_freq(rec.directory / entry["path"], ROLE_FREQS[role])
    # FLAC keeps the 24-bit samples and the sample rate (no resampling)
    info = probe(rec.directory / "seg01_mix.flac")["streams"][0]
    assert info["sample_rate"] == "48000" and info["bits_per_raw_sample"] == "24"
    assert not list(rec.directory.glob(".*partial"))


@pytest.mark.req("R-40", "R-42")
def test_audio_strategy_b_drops_dummy_video(tmp_path: Path) -> None:
    cfg = make_config(tmp_path, {"postprocess.keep_original_mkv": False})
    rec = _session(cfg, "audio")
    make_container(rec.directory / "seg01_container.mkv", video="dummy")
    assert process_segment(rec, 1, "audio", cfg) == []
    files = _files(rec)
    assert set(files) == {"audio", "mix", "remote", "local"}  # container verified and removed
    assert not (rec.directory / "seg01_container.mkv").exists()
    mka = rec.directory / "seg01_audio.mka"
    assert files["audio"]["path"] == "seg01_audio.mka"
    assert stream_types(mka) == ["audio", "audio", "audio"]
    for idx, role in enumerate(["mix", "remote", "local"]):
        _assert_freq(mka, ROLE_FREQS[role], idx)
        _assert_freq(rec.directory / f"seg01_{role}.flac", ROLE_FREQS[role])


@pytest.mark.req("R-40", "R-41")
def test_video_mode_remuxes_to_mp4_with_all_tracks(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    rec = _session(cfg, "video")
    make_container(rec.directory / "seg01_container.mkv", video="testsrc")
    assert process_segment(rec, 1, "video", cfg) == []
    files = _files(rec)
    assert set(files) == {"container", "video", "mix", "remote", "local"}
    assert files["video"]["codec"] == "h264" and files["container"]["codec"] == "h264"
    mp4 = rec.directory / "seg01_video.mp4"
    assert stream_types(mp4) == ["video", "audio", "audio", "audio"]
    for idx, role in enumerate(["mix", "remote", "local"]):
        _assert_freq(mp4, ROLE_FREQS[role], idx)
        _assert_freq(rec.directory / f"seg01_{role}.flac", ROLE_FREQS[role])
    order = [f["role"] for f in rec.to_document()["segments"][0]["files"]]
    assert order == ["container", "video", "mix", "remote", "local"]


@pytest.mark.req("R-40", "R-41")
def test_video_mode_mkv_target_and_wav_export(tmp_path: Path) -> None:
    cfg = make_config(
        tmp_path,
        {
            "postprocess.remux_video_to": "mkv",
            "postprocess.audio_export_format": "wav",
            "postprocess.keep_original_mkv": False,
        },
    )
    rec = _session(cfg, "video")
    make_container(rec.directory / "seg01_container.mkv", video="testsrc")
    assert process_segment(rec, 1, "video", cfg) == []
    files = _files(rec)
    assert set(files) == {"video", "mix", "remote", "local"}
    assert files["video"]["path"] == "seg01_video.mkv"
    for role in ("mix", "remote", "local"):
        assert files[role]["codec"] == "pcm_s24le"
        _assert_freq(rec.directory / f"seg01_{role}.wav", ROLE_FREQS[role])


@pytest.mark.req("R-11", "R-40")
def test_mapping_uses_ascending_track_numbers_with_external(tmp_path: Path) -> None:
    # OBS writes tracks in ascending order: 1 mix, 2 local, 3 external, 4 remote
    cfg = make_config(
        tmp_path,
        {"recording.audio.tracks": {"mix": 1, "remote": 4, "local": 2, "external": 3}},
    )
    rec = _session(cfg, "audio")
    order = ["mix", "local", "external", "remote"]
    make_container(rec.directory / "seg01_container.mkv", freqs=[ROLE_FREQS[r] for r in order])
    assert process_segment(rec, 1, "audio", cfg) == []
    for role in order:
        _assert_freq(rec.directory / f"seg01_{role}.flac", ROLE_FREQS[role])


@pytest.mark.req("R-41")
def test_remux_failure_keeps_mkv_with_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import captation.postprocess as pp

    real = pp._ffmpeg_to

    def failing_mp4(src: Path, target: Path, args: list[str], **kw: Any) -> None:
        # Whether a codec fits into MP4 depends on the FFmpeg version, so the failure is forced.
        if target.suffix == ".mp4":
            raise pp.ToolError("ffmpeg: could not write header (forced in test)")
        real(src, target, args, **kw)

    monkeypatch.setattr(pp, "_ffmpeg_to", failing_mp4)
    cfg = make_config(tmp_path, {"postprocess.keep_original_mkv": False})
    rec = _session(cfg, "video")
    make_container(rec.directory / "seg01_container.mkv", video="testsrc")
    warnings = process_segment(rec, 1, "video", cfg)
    assert warnings == ["postprocess:remux_failed:seg01"]
    files = _files(rec)
    assert "video" not in files and "container" in files  # kept despite keep_original_mkv false
    assert (rec.directory / "seg01_container.mkv").exists()
    assert not (rec.directory / "seg01_video.mp4").exists()
    assert not list(rec.directory.glob(".*partial"))
    assert "postprocess:remux_failed:seg01" in rec.warnings


def test_track_count_mismatch_is_an_error(tmp_path: Path) -> None:
    cfg = make_config(tmp_path, {"postprocess.keep_original_mkv": False})
    rec = _session(cfg, "audio")
    make_container(rec.directory / "seg01_container.mkv", freqs=[440.0, 880.0])
    assert process_segment(rec, 1, "audio", cfg) == ["postprocess:track_count_mismatch:seg01"]
    assert set(_files(rec)) == {"container"}
    assert (rec.directory / "seg01_container.mkv").exists()


def test_missing_container_is_a_warning(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    rec = _session(cfg, "audio")
    rec.end_segment(1, T0 + timedelta(seconds=1), interrupted=True)
    assert process_segment(rec, 1, "audio", cfg) == ["postprocess:container_missing:seg01"]
    result = finalize_session(rec, cfg, status="interrupted")
    assert "postprocess:container_missing:seg01" in result.warnings
    doc = json.loads(rec.session_file.read_text(encoding="utf-8"))
    assert schema_errors(doc, "session.schema.json") == []
    assert doc["status"] == "interrupted" and doc["segments"][0]["files"] == []


@pytest.mark.req("R-39", "R-43", "R-44")
def test_finalize_checksums_read_only_and_schema(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    rec = _session(cfg, "audio")
    make_container(rec.directory / "seg01_container.mkv", video="dummy")
    process_segment(rec, 1, "audio", cfg)
    result = finalize_session(rec, cfg, status="complete")
    assert result.qc_warnings == []

    doc = json.loads(rec.session_file.read_text(encoding="utf-8"))
    assert schema_errors(doc, "session.schema.json") == []
    assert doc["status"] == "complete" and doc["ended_at"] is not None
    names = sorted(f["path"] for f in doc["segments"][0]["files"])
    assert names == sorted(
        [
            "seg01_container.mkv",
            "seg01_audio.mka",
            "seg01_mix.flac",
            "seg01_remote.flac",
            "seg01_local.flac",
        ]
    )
    for entry in doc["segments"][0]["files"]:
        path = rec.directory / entry["path"]
        assert entry["bytes"] == path.stat().st_size
        assert stat.S_IMODE(path.stat().st_mode) == 0o444
        if entry["role"] in {"mix", "remote", "local"}:
            assert set(entry["qc"]) == {
                "rms_dbfs",
                "peak_dbfs",
                "silent_ratio",
                "longest_silence_s",
            }
            assert entry["qc"]["silent_ratio"] == 0.0
        else:
            assert entry["qc"] is None
    sums = (rec.directory / "SHA256SUMS").read_text(encoding="utf-8")
    assert sorted(line.split("  ")[1] for line in sums.splitlines()) == names
    if POSIX:
        assert stat.S_IMODE(rec.session_file.stat().st_mode) == 0o600
    assert verify_sha256sums(rec.directory)

    sha256sum = shutil.which("sha256sum")
    if sha256sum is not None and POSIX:  # Windows: verify_sha256sums above
        proc = subprocess.run(
            [sha256sum, "-c", "SHA256SUMS"],
            cwd=rec.directory,
            capture_output=True,
            text=True,
            check=False,
        )
        print(proc.stdout)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert proc.stdout.count(": OK") == len(names)
    if WRITE_PROTECTION_ENFORCED:
        with pytest.raises(PermissionError):
            (rec.directory / "seg01_mix.flac").open("ab")


def test_make_read_only_off(tmp_path: Path) -> None:
    cfg = make_config(tmp_path, {"postprocess.make_read_only": False})
    rec = _session(cfg, "audio")
    make_container(rec.directory / "seg01_container.mkv")
    process_segment(rec, 1, "audio", cfg)
    finalize_session(rec, cfg, status="complete")
    mode = stat.S_IMODE((rec.directory / "seg01_mix.flac").stat().st_mode)
    assert mode & stat.S_IWUSR


def test_two_segments_audio_then_video(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    rec = _session(cfg, "audio")
    rec.begin_segment(2, "video", "upgrade:camera_on", T0 + timedelta(seconds=5), "window")
    rec.end_segment(2, T0 + timedelta(seconds=9))
    make_container(rec.directory / "seg01_container.mkv", video="dummy")
    make_container(rec.directory / "seg02_container.mkv", video="testsrc")
    assert process_segment(rec, 1, "audio", cfg) == []
    assert process_segment(rec, 2, "video", cfg) == []
    finalize_session(rec, cfg, status="complete")
    doc = json.loads(rec.session_file.read_text(encoding="utf-8"))
    assert schema_errors(doc, "session.schema.json") == []
    assert [s["mode"] for s in doc["segments"]] == ["audio", "video"]
    assert {f["path"] for f in doc["segments"][1]["files"]} >= {
        "seg02_video.mp4",
        "seg02_remote.flac",
    }
    assert verify_sha256sums(rec.directory)


@pytest.mark.req("R-41")
def test_qc_runs_on_the_container_when_tracks_are_not_extracted(tmp_path: Path) -> None:
    """Track QC is mandatory (§5.5): `extract_tracks: false` must not switch it off."""
    cfg = make_config(tmp_path, {"postprocess.extract_tracks": False, "qc.silence_warn_s": 5})
    rec = _session(cfg, "audio")
    # remote (stream 1) falls silent after 1 s of 9 s
    make_container(
        rec.directory / "seg01_container.mkv", video="dummy", duration=9.0, silent_after={1: 1.0}
    )
    process_segment(rec, 1, "audio", cfg)
    result = finalize_session(rec, cfg, status="complete")
    assert "qc:remote_silent" in result.qc_warnings
    doc = json.loads(rec.session_file.read_text(encoding="utf-8"))
    assert schema_errors(doc, "session.schema.json") == []
    assert "qc:remote_silent" in doc["warnings"]
