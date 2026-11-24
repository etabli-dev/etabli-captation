# Phone numbers in these tests are synthetic test values.
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import stat
from collections import namedtuple
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from captation import storage
from captation.config import schema_errors
from captation.storage import (
    HostInfo,
    SessionDocumentError,
    SessionRecord,
    apply_retention,
    encryption_verdict,
    has_free_space,
    host_info,
    new_session_id,
)
from tests.fixtures.media import POSIX, make_config

HOST = HostInfo("linux", "test-os", "0.1.0", "33.0.3", "5.6.3")
T0 = datetime(2026, 11, 23, 14, 15, 2).astimezone()


def _record(
    tmp_path: Path, *, overrides: dict[str, object] | None = None, **kw: object
) -> SessionRecord:
    cfg = make_config(tmp_path, overrides)
    args: dict[str, object] = {
        "session_id": new_session_id(T0, "a3f9"),
        "app_id": "signal",
        "category": "messenger",
        "consent_ref": "P-017",
        "started_at": T0,
        "host": HOST,
    }
    args.update(kw)
    return SessionRecord.create(cfg, **args)  # type: ignore[arg-type]


@pytest.mark.req("R-24")
def test_session_id_format_and_local_time() -> None:
    sid = new_session_id(datetime(2026, 11, 23, 14, 15, 2), "a3f9")
    assert sid == "S-20261123-141502-a3f9"
    random_ids = {new_session_id(T0) for _ in range(50)}
    assert all(re.fullmatch(r"S-\d{8}-\d{6}-[0-9a-f]{4}", s) for s in random_ids)
    assert len(random_ids) > 1
    utc = datetime(2026, 11, 23, 13, 15, 2, tzinfo=UTC)
    local = utc.astimezone()
    assert new_session_id(utc, "0000") == f"S-{local:%Y%m%d-%H%M%S}-0000"
    with pytest.raises(ValueError):
        new_session_id(T0, "XYZ1")


@pytest.mark.req("R-24", "R-43")
def test_create_makes_private_folder_and_writes_valid_json(tmp_path: Path) -> None:
    rec = _record(tmp_path)
    assert rec.directory == tmp_path / "data" / "unassigned" / "S-20261123-141502-a3f9"
    if POSIX:
        assert stat.S_IMODE(rec.directory.stat().st_mode) == 0o700
    assert not rec.session_file.exists()  # nothing is written before write()
    rec.begin_segment(1, "audio", "rule:default", T0, "none")
    path = rec.write()
    if POSIX:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert schema_errors(doc, "session.schema.json") == []
    assert doc["status"] == "recording"
    assert doc["config_digest"] == make_config(tmp_path).digest()
    assert doc["origin"] == {
        "type": "obs",
        "device": None,
        "time_source": "clock",
        "source_sha256": None,
    }
    assert not list(rec.directory.glob(".*.tmp"))


def test_project_folder_and_duplicate_session(tmp_path: Path) -> None:
    rec = _record(tmp_path, overrides={"project_id": "PRJ-2026-001"})
    assert rec.directory.parent.name == "PRJ-2026-001"
    with pytest.raises(FileExistsError):
        _record(tmp_path, overrides={"project_id": "PRJ-2026-001"})


@pytest.mark.req("R-23")
def test_window_title_only_kept_when_enabled(tmp_path: Path) -> None:
    rec = _record(tmp_path, window_title="Call with +49 170 1234567")
    assert rec.to_document()["app"]["window_title"] is None
    rec2 = _record(
        tmp_path / "b",
        overrides={"detection.store_window_titles": True},
        window_title="Weekly sync",
    )
    assert rec2.to_document()["app"]["window_title"] == "Weekly sync"


def test_segments_warnings_and_status(tmp_path: Path) -> None:
    rec = _record(tmp_path)
    rec.begin_segment(1, "audio", "rule:default", T0, "none")
    rec.begin_segment(2, "video", "upgrade:camera_on", T0 + timedelta(seconds=60), "window")
    rec.end_segment(1, T0 + timedelta(seconds=59))
    rec.end_segment(2, T0 + timedelta(seconds=120), interrupted=True)
    rec.add_warning("capture:display_fallback")
    rec.add_warning("capture:display_fallback")
    rec.set_status("interrupted")
    rec.set_ended(T0 + timedelta(seconds=120))
    rec.write()
    doc = json.loads(rec.session_file.read_text(encoding="utf-8"))
    assert doc["warnings"] == ["capture:display_fallback"]
    assert [s["index"] for s in doc["segments"]] == [1, 2]
    assert doc["segments"][1]["interrupted"] is True
    assert doc["ended_at"] is not None
    with pytest.raises(ValueError):
        rec.begin_segment(2, "video", "x", T0, "none")
    with pytest.raises(ValueError):
        rec.begin_segment(100, "video", "x", T0, "none")


@pytest.mark.req("R-43", "R-24")
def test_invalid_documents_are_rejected_and_not_written(tmp_path: Path) -> None:
    rec = _record(tmp_path)
    rec.begin_segment(1, "audio", "rule:default", T0, "none")
    rec.write()
    before = rec.session_file.read_bytes()

    # 1. a file name with personal data does not match segNN_<role>.<ext>
    bad = rec.directory / "Anruf +49 170 1234567.flac"
    bad.write_bytes(b"x")
    rec.register_file(1, "mix", bad)
    with pytest.raises(SessionDocumentError) as info:
        rec.write()
    assert any("path" in p for p in info.value.problems)
    assert rec.session_file.read_bytes() == before
    rec.remove_file(1, bad.name)

    # 2. an over-long consent reference
    rec.consent_ref = "X" * 65
    with pytest.raises(SessionDocumentError):
        rec.write()
    assert rec.session_file.read_bytes() == before
    rec.consent_ref = "P-017"

    # 3. a status outside the enum
    rec.set_status("paused")  # type: ignore[arg-type]
    with pytest.raises(SessionDocumentError):
        rec.write()
    assert rec.session_file.read_bytes() == before
    assert not list(rec.directory.glob(".*.tmp"))

    # 4. a never-written invalid session leaves no session.json at all
    rec2 = _record(tmp_path / "other", app_id="Not Valid")
    with pytest.raises(SessionDocumentError):
        rec2.write()
    assert not rec2.session_file.exists()


def test_register_file_requires_session_folder(tmp_path: Path) -> None:
    rec = _record(tmp_path)
    rec.begin_segment(1, "audio", "rule:default", T0, "none")
    outside = tmp_path / "seg01_mix.flac"
    outside.write_bytes(b"x")
    with pytest.raises(ValueError):
        rec.register_file(1, "mix", outside)


def test_host_info_contains_no_host_name() -> None:
    info = host_info("33.0.3", "5.6.3")
    assert info.os in {"linux", "macos", "windows"}
    assert info.obs_version == "33.0.3"
    hostname = socket.gethostname()
    if len(hostname) >= 4:
        assert hostname not in info.os_version


Usage = namedtuple("Usage", "total used free")


@pytest.mark.req("R-45")
def test_low_disk_with_mocked_disk_usage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Path] = []

    def fake(path: Path) -> Usage:
        seen.append(Path(path))
        return Usage(100 * storage.GIB, 95 * storage.GIB, 5 * storage.GIB)

    monkeypatch.setattr(shutil, "disk_usage", fake)
    assert has_free_space(tmp_path, 5) is True
    assert has_free_space(tmp_path, 10) is False
    # a not-yet-existing data root is measured on its nearest existing parent
    assert has_free_space(tmp_path / "missing" / "deeper", 1) is True
    assert seen[-1] == tmp_path


@pytest.mark.req("R-46")
@pytest.mark.parametrize(
    ("status", "policy", "expected"),
    [
        ("encrypted", "off", "ok"),
        ("unencrypted", "off", "ok"),
        ("unknown", "off", "ok"),
        ("encrypted", "warn", "ok"),
        ("unencrypted", "warn", "warn"),
        ("unknown", "warn", "warn"),
        ("encrypted", "block", "ok"),
        ("unencrypted", "block", "block"),
        ("unknown", "block", "warn"),
    ],
)
def test_encryption_verdict(status: str, policy: str, expected: str) -> None:
    assert encryption_verdict(status, policy) == expected  # type: ignore[arg-type]


def test_encryption_warning_texts() -> None:
    assert storage.encryption_warning("encrypted") is None
    assert storage.encryption_warning("unencrypted") == storage.WARN_DISK_UNENCRYPTED
    assert storage.encryption_warning("unknown") == storage.WARN_DISK_ENCRYPTION_UNKNOWN


def _session_at(tmp_path: Path, when: datetime, status: str, rand: str) -> SessionRecord:
    rec = _record(
        tmp_path,
        overrides={"storage.retention_days": 30},
        session_id=new_session_id(when, rand),
        started_at=when,
    )
    rec.begin_segment(1, "audio", "rule:default", when, "none")
    media = rec.directory / "seg01_mix.flac"
    media.write_bytes(b"audio")
    rec.register_file(1, "mix", media)
    media.chmod(0o444)
    if status != "recording":
        rec.end_segment(1, when + timedelta(minutes=5))
        rec.set_ended(when + timedelta(minutes=5))
    rec.set_status(status)  # type: ignore[arg-type]
    rec.write()
    return rec


@pytest.mark.req("R-29")
def test_retention_deletes_only_old_finished_sessions(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    now = datetime(2026, 11, 23, 12, 0).astimezone()
    old = _session_at(tmp_path, now - timedelta(days=40), "complete", "0001")
    old_recording = _session_at(tmp_path, now - timedelta(days=40), "recording", "0002")
    recent = _session_at(tmp_path, now - timedelta(days=3), "complete", "0003")
    broken = tmp_path / "data" / "unassigned" / "S-20200101-000000-0004"
    broken.mkdir()
    (tmp_path / "data" / "not-a-session").mkdir()
    # session.json was written when the session ended (retention also counts from the write)
    for rec, at in ((old, now - timedelta(days=40)), (recent, now - timedelta(days=3))):
        os.utime(rec.session_file, (at.timestamp(), at.timestamp()))

    cfg_off = make_config(tmp_path)
    assert cfg_off.storage.retention_days is None
    assert apply_retention(cfg_off, now) == []
    assert old.directory.exists()

    cfg = make_config(tmp_path, {"storage.retention_days": 30})
    with caplog.at_level("INFO", logger="captation.storage"):
        deleted = apply_retention(cfg, now)
    assert deleted == [old.session_id]
    assert not old.directory.exists()
    assert old_recording.directory.exists()
    assert recent.directory.exists()
    assert broken.exists()
    assert (tmp_path / "data" / "not-a-session").exists()
    assert old.session_id in caplog.text
    assert str(tmp_path) not in caplog.text


@pytest.mark.req("R-47")
def test_retention_keeps_a_fresh_import_with_an_old_device_date(tmp_path: Path) -> None:
    """A recorder with a wrong clock must not get a just-imported session deleted."""
    now = datetime.now().astimezone()
    imported = _session_at(tmp_path, now - timedelta(days=4000), "complete", "0005")
    cfg = make_config(tmp_path, {"storage.retention_days": 365})
    assert apply_retention(cfg, now) == []
    assert imported.directory.exists()
