# Phone numbers in these tests are synthetic test values.
from __future__ import annotations

import json
import logging
import os
import stat
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from captation import importer
from captation.cli import app
from captation.config import schema_errors
from captation.integrity import sha256_file, verify_sha256sums
from captation.storage import GIB
from tests.fixtures.media import (
    POSIX,
    grep_tree,
    make_config,
    make_plain_audio,
    make_tagged_audio,
    needs_ffmpeg,
    stream_hash,
    tags_of,
    write_config_file,
)

pytestmark = needs_ffmpeg

NUMBER = "+49 170 1234567"
SOURCE_STEM = f"Anruf {NUMBER} Max Mustermann 2026-11-20"
NEEDLES = [b"1234567", b"Anruf", b"Mustermann", NUMBER.encode()]


@pytest.fixture(autouse=True)
def restore_logging() -> Iterator[None]:
    """The CLI configures the ``captation`` logger; undo that after each test."""
    logger = logging.getLogger("captation")
    handlers, level, propagate = list(logger.handlers), logger.level, logger.propagate
    yield
    for handler in list(logger.handlers):
        if handler not in handlers:
            logger.removeHandler(handler)
            handler.close()
    logger.setLevel(level)
    logger.propagate = propagate


def _cli(tmp_path: Path, *args: str, overrides: dict[str, Any] | None = None) -> Any:
    cfg_file = write_config_file(tmp_path, overrides)
    return CliRunner().invoke(app, ["import", *args, "--config", str(cfg_file)])


def _sessions(tmp_path: Path) -> list[Path]:
    root = tmp_path / "data" / "unassigned"
    return sorted(root.iterdir()) if root.exists() else []


def _doc(session: Path) -> dict[str, Any]:
    doc: dict[str, Any] = json.loads((session / "session.json").read_text(encoding="utf-8"))
    return doc


@pytest.mark.req("R-25", "R-55", "R-56", "R-43", "R-44")
@pytest.mark.parametrize("ext", ["mp3", "m4a", "wav"])
def test_planted_metadata_never_leaks(tmp_path: Path, ext: str) -> None:
    phone = tmp_path / "phone export"
    phone.mkdir()
    src = make_tagged_audio(phone / f"{SOURCE_STEM}.{ext}", ext, title=NUMBER)
    assert any(NUMBER in v for v in tags_of(src))  # the metadata really is planted
    hash_before = stream_hash(src)

    result = _cli(tmp_path, str(src), "--device", "magmo_pro", "--consent-ref", "P-018")
    assert result.exit_code == 0, result.output
    sessions = _sessions(tmp_path)
    assert len(sessions) == 1
    session = sessions[0]
    out = session / f"seg01_mix.{ext}"
    hash_after = stream_hash(out)
    print(f"{ext} stream hash before: {hash_before}")
    print(f"{ext} stream hash after:  {hash_after}")
    assert hash_before == hash_after

    # no number or name in the session folder, the index, the logs, stdout or the tags
    data_hits = grep_tree(tmp_path / "data", NEEDLES)
    log_hits = grep_tree(tmp_path / "logs", NEEDLES)
    print(f"grep data root: {data_hits}  grep logs: {log_hits}")
    assert data_hits == [] and log_hits == []
    assert not any(n.decode() in result.output for n in NEEDLES)
    assert SOURCE_STEM not in result.output
    assert not any(n.decode() in v for v in tags_of(out) for n in NEEDLES)
    log_text = (tmp_path / "logs" / "agent.jsonl").read_text(encoding="utf-8")
    assert session.name in log_text and "import" in log_text

    doc = _doc(session)
    assert schema_errors(doc, "session.schema.json") == []
    assert doc["origin"]["type"] == "import" and doc["origin"]["device"] == "magmo_pro"
    assert doc["origin"]["source_sha256"] == sha256_file(src)
    assert doc["app"] == {"id": "mobile_magmo", "category": "call", "window_title": None}
    assert doc["host"]["obs_version"] is None and doc["consent_ref"] == "P-018"
    seg = doc["segments"][0]
    assert seg["trigger"] == "import:magmo_pro" and seg["mode"] == "audio"
    assert seg["capture"] == "none" and seg["index"] == 1
    assert [f["path"] for f in seg["files"]] == [f"seg01_mix.{ext}"]
    assert seg["files"][0]["qc"] is not None
    assert "import:metadata_not_stripped" not in doc["warnings"]
    assert stat.S_IMODE(out.stat().st_mode) == 0o444
    assert verify_sha256sums(session)
    assert sorted(p.name for p in session.iterdir()) == [
        "SHA256SUMS",
        f"seg01_mix.{ext}",
        "session.json",
    ]
    assert not list((tmp_path / "data").glob(".import-staging-*"))
    assert src.exists() and sha256_file(src) == doc["origin"]["source_sha256"]


@pytest.mark.req("R-56")
def test_unverifiable_strip_copies_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = make_tagged_audio(tmp_path / "rec.mp3", "mp3", title=NUMBER)
    calls = iter(["0,a,SHA256=aaaa", "0,a,SHA256=bbbb"])
    monkeypatch.setattr(importer, "stream_hash", lambda path, duration=None: next(calls))
    cfg = make_config(tmp_path)
    [res] = importer.import_recordings(
        cfg, src, device="generic", consent_ref="P-018", started_at=None, app_id=None
    )
    assert res.status == "imported"
    assert "import:metadata_not_stripped" in res.warnings
    session = _sessions(tmp_path)[0]
    assert sha256_file(session / "seg01_mix.mp3") == sha256_file(src)  # unchanged copy
    assert _doc(session)["app"]["id"] == "mobile_generic"


@pytest.mark.req("R-57")
def test_duplicate_refused_unless_forced(tmp_path: Path) -> None:
    src = make_plain_audio(tmp_path / "a.wav")
    first = _cli(tmp_path, str(src), "--device", "generic", "--consent-ref", "P-018")
    assert first.exit_code == 0, first.output
    second = _cli(tmp_path, str(src), "--device", "generic", "--consent-ref", "P-018")
    assert second.exit_code == 2
    assert "duplicate" in second.output
    assert len(_sessions(tmp_path)) == 1
    log_text = (tmp_path / "logs" / "agent.jsonl").read_text(encoding="utf-8")
    assert "already imported, refused" in log_text
    forced = _cli(tmp_path, str(src), "--device", "generic", "--consent-ref", "P-018", "--force")
    assert forced.exit_code == 0, forced.output
    assert len(_sessions(tmp_path)) == 2
    index = tmp_path / "data" / importer.INDEX_NAME
    if POSIX:
        assert stat.S_IMODE(index.stat().st_mode) == 0o600
    assert sha256_file(src) in json.loads(index.read_text(encoding="utf-8"))["sources"]


@pytest.mark.req("R-54")
def test_missing_or_invalid_consent_ref_refused(tmp_path: Path) -> None:
    src = make_plain_audio(tmp_path / "a.wav")
    missing = _cli(tmp_path, str(src), "--device", "generic")
    assert missing.exit_code == 2
    assert "--consent-ref is required" in missing.output
    bad = _cli(tmp_path, str(src), "--device", "generic", "--consent-ref", "max mustermann")
    assert bad.exit_code == 2 and "consent_ref_pattern" in bad.output
    assert _sessions(tmp_path) == []
    cfg = make_config(tmp_path)
    for ref in (None, "", "   ", "X" * 65):
        with pytest.raises(importer.ImportRefused):
            importer.validate_consent_ref(cfg, ref)
    assert importer.validate_consent_ref(cfg, " P-018 ") == "P-018"


def test_unknown_device_and_bad_app_id_refused(tmp_path: Path) -> None:
    src = make_plain_audio(tmp_path / "a.wav")
    res = _cli(tmp_path, str(src), "--device", "iphone", "--consent-ref", "P-018")
    assert res.exit_code == 2 and "unknown device" in res.output
    res = _cli(
        tmp_path, str(src), "--device", "generic", "--consent-ref", "P-018", "--app-id", "Bad Id"
    )
    assert res.exit_code == 2 and "--app-id" in res.output
    assert _sessions(tmp_path) == []


def test_extension_and_size_limits(tmp_path: Path) -> None:
    txt = tmp_path / f"{SOURCE_STEM}.txt"
    txt.write_text("hello", encoding="utf-8")
    res = _cli(tmp_path, str(txt), "--device", "generic", "--consent-ref", "P-018")
    assert res.exit_code == 2 and "extension_not_allowed" in res.output
    assert SOURCE_STEM not in res.output

    aac = make_plain_audio(tmp_path / "b.m4a")
    res = _cli(
        tmp_path,
        str(aac),
        "--device",
        "generic",
        "--consent-ref",
        "P-018",
        overrides={"import.allowed_extensions": ["wav", "mp3"]},
    )
    assert res.exit_code == 2 and "extension_not_allowed" in res.output

    big = tmp_path / f"{SOURCE_STEM}.wav"
    with big.open("wb") as fh:  # sparse file just over 0.1 GiB
        fh.truncate(int(0.1 * GIB) + 1)
    res = _cli(
        tmp_path,
        str(big),
        "--device",
        "generic",
        "--consent-ref",
        "P-018",
        overrides={"import.max_file_gb": 0.1},
    )
    assert res.exit_code == 2 and "too_large" in res.output
    assert _sessions(tmp_path) == []

    res = _cli(tmp_path, str(tmp_path / "nope.wav"), "--device", "generic", "--consent-ref", "P-1X")
    assert res.exit_code == 2 and "does not exist" in res.output and "nope" not in res.output


def test_dry_run_prints_no_file_names(tmp_path: Path) -> None:
    phone = tmp_path / "phone"
    phone.mkdir()
    a = make_tagged_audio(phone / f"{SOURCE_STEM}.mp3", "mp3", title=NUMBER)
    make_plain_audio(phone / f"Anruf {NUMBER}.wav")
    (phone / f"{SOURCE_STEM}.txt").write_text("x", encoding="utf-8")
    res = _cli(
        tmp_path, str(phone), "--device", "plaud_note_pro", "--consent-ref", "P-018", "--dry-run"
    )
    print(res.output)
    assert not any(n.decode() in res.output for n in NEEDLES)
    assert "phone" not in res.output
    assert res.output.count("would_import") == 2
    assert sha256_file(a)[: importer.SHA_PREFIX_LEN] in res.output
    assert "extension_not_allowed" in res.output
    assert _sessions(tmp_path) == []
    assert not (tmp_path / "data" / importer.INDEX_NAME).exists()


def test_directory_import_recursive_opt_in(tmp_path: Path) -> None:
    phone = tmp_path / "phone"
    (phone / "sub").mkdir(parents=True)
    make_plain_audio(phone / "a.wav", duration=2.0)
    make_plain_audio(phone / "sub" / "b.wav", duration=2.5)
    cfg = make_config(tmp_path)
    flat = importer.import_recordings(
        cfg, phone, device="viaim_recdot", consent_ref="P-018", started_at=None, app_id=None
    )
    assert [r.status for r in flat] == ["imported"]
    deep = importer.import_recordings(
        cfg,
        phone,
        device="viaim_recdot",
        consent_ref="P-018",
        started_at=None,
        app_id="mobile_viaim_desk",
        recursive=True,
    )
    assert [r.status for r in deep] == ["duplicate", "imported"]
    ids = {_doc(s)["app"]["id"] for s in _sessions(tmp_path)}
    assert ids == {"mobile_viaim", "mobile_viaim_desk"}


@pytest.mark.req("R-58")
def test_time_source_order(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    user_start = datetime.fromisoformat("2026-11-21T10:00:00+01:00")

    # 1. container metadata wins, even over --started-at
    tagged = make_plain_audio(tmp_path / "m.m4a", creation_time="2026-11-20T08:30:00Z")
    [res] = importer.import_recordings(
        cfg,
        tagged,
        device="plaud_note_pro",
        consent_ref="P-018",
        started_at=user_start,
        app_id=None,
    )
    doc = _doc(next(s for s in _sessions(tmp_path) if s.name == res.session_id))
    expected = datetime.fromisoformat("2026-11-20T08:30:00+00:00")
    assert doc["origin"]["time_source"] == "file_metadata"
    assert datetime.fromisoformat(doc["started_at"]) == expected
    assert res.session_id == f"S-{expected.astimezone():%Y%m%d-%H%M%S}-{res.session_id[-4:]}"
    assert doc["app"]["id"] == "mobile_plaud"

    # 2. no metadata → --started-at
    plain = make_plain_audio(tmp_path / "u.wav", duration=3.0)
    [res] = importer.import_recordings(
        cfg, plain, device="generic", consent_ref="P-018", started_at=user_start, app_id=None
    )
    doc = _doc(next(s for s in _sessions(tmp_path) if s.name == res.session_id))
    assert doc["origin"]["time_source"] == "user_input"
    assert datetime.fromisoformat(doc["started_at"]) == user_start
    assert datetime.fromisoformat(doc["ended_at"]) == user_start + timedelta(seconds=3)
    assert "import:time_from_mtime" not in doc["warnings"]

    # 3. neither → file mtime with a warning
    old = make_plain_audio(tmp_path / "t.wav", duration=2.0)
    mtime = datetime(2026, 11, 19, 7, 45, 10).astimezone()
    os.utime(old, (mtime.timestamp(), mtime.timestamp()))
    [res] = importer.import_recordings(
        cfg, old, device="recordergear_pr200", consent_ref="P-018", started_at=None, app_id=None
    )
    doc = _doc(next(s for s in _sessions(tmp_path) if s.name == res.session_id))
    assert doc["origin"]["time_source"] == "file_mtime"
    assert "import:time_from_mtime" in doc["warnings"]
    assert datetime.fromisoformat(doc["started_at"]) == mtime
    assert doc["app"]["id"] == "mobile_pr200"
    assert schema_errors(doc, "session.schema.json") == []


def test_metadata_start_parsing() -> None:
    bwf = {"format": {"tags": {"date": "2026-11-24", "creation_time": "09:12:00"}}}
    got = importer.metadata_start(bwf)
    assert got == datetime(2026, 11, 24, 9, 12, 0).astimezone()
    assert (
        importer.metadata_start({"format": {"tags": {"creation_time": "1970-01-01T00:00:00Z"}}})
        is None
    )
    assert importer.metadata_start({"format": {"tags": {"creation_time": "garbage"}}}) is None
    assert importer.metadata_start({}) is None


def test_failed_import_leaves_no_session(tmp_path: Path) -> None:
    broken = tmp_path / "broken.mp3"
    broken.write_bytes(b"\x00" * 4096)
    cfg = make_config(tmp_path)
    [res] = importer.import_recordings(
        cfg, broken, device="generic", consent_ref="P-018", started_at=None, app_id=None
    )
    assert res.status == "rejected" and res.reason == "unreadable_media"
    assert _sessions(tmp_path) == []
    assert not list((tmp_path / "data").glob(".import-staging-*"))
    assert not (tmp_path / "data" / importer.INDEX_NAME).exists()
