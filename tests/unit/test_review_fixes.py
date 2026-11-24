"""Regression tests for the release review findings (2026-11-23)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from captation import storage
from captation.config import Config, ConfigError, parse_config
from captation.models import AppRef
from captation.session import State
from tests.unit.test_agent import CALL, Rig, arm
from tests.unit.test_obs_controller import cfg_with, make


@pytest.mark.req("R-22", "R-14")
@pytest.mark.parametrize(
    "flag",
    [
        "--startrecording",
        "--startreplaybuffer",
        "--startstreaming",
        "--safe-mode",
        "--websocket_port=1",
    ],
)
def test_launch_args_cannot_bypass_the_consent_guard(tmp_path: Path, flag: str) -> None:
    raw = cfg_with(tmp_path).model_dump(by_alias=True, exclude_none=True)
    raw["obs"]["launch_args"] = [flag]
    with pytest.raises(ConfigError, match="not allowed"):
        parse_config(raw)
    raw["obs"]["launch_args"] = ["--verbose"]
    assert parse_config(raw).obs.launch_args == ["--verbose"]


@pytest.mark.req("R-20", "R-43")
def test_overlong_or_unprintable_consent_ref_is_refused(tmp_path: Path) -> None:
    r = Rig(tmp_path)
    try:
        assert "consent_ref_invalid" in arm(r, "P" * 65)["refused"]
        assert "consent_ref_invalid" in arm(r, "P-1\x07")["refused"]
        assert r.state is State.DISARMED
    finally:
        r.close()


@pytest.mark.req("R-21", "R-18")
def test_no_new_recording_after_indicator_loss(tmp_path: Path) -> None:
    r = Rig(tmp_path)
    try:
        arm(r)
        r.probe.mic = (CALL,)
        r.tick(n=10)
        assert r.state is State.RECORDING
        r.backend.tray = False
        r.agent.tray_changed(False)
        r.flush()
        assert r.state is State.RECORDING  # the consented recording continues
        r.probe.mic = ()
        r.tick(n=12)
        assert r.state is State.DISARMED  # ... but no further recording without a tray
        assert "ui:indicator_lost" in r.sessions()[0]["warnings"]
    finally:
        r.close()


def test_countdown_aborted_when_tray_disappears(tmp_path: Path) -> None:
    r = Rig(tmp_path)
    try:
        arm(r)
        r.backend.tray = False  # vanished without a NameOwnerChanged
        r.probe.mic = (CALL,)
        r.tick(n=4)
        assert r.state is State.DISARMED
        assert "StartRecord" not in r.fake_obs.names()
    finally:
        r.close()


@pytest.mark.req("R-53")
def test_obs_lost_while_idle_starts_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    r = Rig(tmp_path)
    started: list[bool] = []
    monkeypatch.setattr(r.agent, "_recover_obs", lambda: started.append(True))
    try:
        arm(r)
        r.agent.obs_lost()
        r.flush()
        assert started == [True]
        # arming again proves OBS is reachable and clears the loss: calls record again
        arm(r)
        r.probe.mic = (CALL,)
        r.tick(n=10)
        assert r.state is State.RECORDING
    finally:
        r.close()


def test_failed_session_creation_cancels(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    r = Rig(tmp_path)

    def boom(*a: object, **k: object) -> None:
        raise PermissionError("data_root not writable")

    monkeypatch.setattr(storage.SessionRecord, "create", boom)
    try:
        arm(r)
        r.probe.mic = (CALL,)
        r.tick(n=10)
        assert r.state is not State.RECORDING and "StartRecord" not in r.fake_obs.names()
        assert any("Sitzung konnte nicht" in n["body"] for n in r.backend.notes)
    finally:
        r.close()


@pytest.mark.req("R-31")
def test_controller_switches_to_the_captation_collection(tmp_path: Path) -> None:
    ctl, fake = make(tmp_path)
    fake.current_collection = "Untitled"
    ctl.prepare_segment("audio", AppRef(1, "x"))
    assert fake.current_collection == "Captation"


def test_reset_leaves_a_user_recording_alone(tmp_path: Path) -> None:
    ctl, fake = make(tmp_path)
    fake.current_profile = "Untitled"
    fake.recording = True
    ctl.reset()
    assert fake.recording is True


def test_approve_screen_refuses_while_recording(tmp_path: Path) -> None:
    from captation.obs.client import ObsError
    from tests.unit.test_obs_controller import MemTokens

    ctl, fake = make(tmp_path, token_store=MemTokens(None))
    ctl.prepare_segment("audio", AppRef(1, "x"))
    ctl.start_segment(tmp_path / "rec", "seg01_container")
    with pytest.raises(ObsError, match="recording"):
        ctl.approve_screen(timeout_s=1)
    assert fake.recording


def test_stale_recording_sessions_are_recovered(tmp_path: Path) -> None:
    cfg: Config = cfg_with(tmp_path)
    host = storage.host_info(None, None)
    rec = storage.SessionRecord.create(
        cfg,
        session_id="S-20261123-100000-abcd",
        app_id="zoom",
        category="meeting",
        consent_ref="P-1",
        started_at=datetime(2026, 11, 23, 10, tzinfo=UTC),
        host=host,
    )
    rec.begin_segment(1, "audio", "rule:default", datetime(2026, 11, 23, 10, tzinfo=UTC), "none")
    rec.write()
    assert storage.recover_stale_sessions(cfg, datetime(2026, 11, 23, 11, tzinfo=UTC)) == [
        rec.session_id
    ]
    doc = json.loads((rec.directory / "session.json").read_text())
    assert doc["status"] == "interrupted" and doc["segments"][0]["interrupted"] is True
    assert "agent:recovered_after_crash" in doc["warnings"]
    assert storage.recover_stale_sessions(cfg, datetime(2026, 11, 23, 12, tzinfo=UTC)) == []


def test_single_instance_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from captation import ipc, service

    monkeypatch.setattr(ipc, "runtime_dir", lambda: tmp_path)
    first = service.single_instance_lock()
    try:
        with pytest.raises(service.AlreadyRunning):
            service.single_instance_lock()
    finally:
        first.close()
    service.single_instance_lock().close()


def test_ipc_rejects_deeply_nested_json() -> None:
    from captation import ipc

    with pytest.raises((ValueError, RecursionError)):
        ipc.validate_message(json.loads("[" * 50 + "]" * 50))


def test_recovered_session_registers_and_seals_the_obs_container(tmp_path: Path) -> None:
    """Audit 2026-11-24: a container left by a crashed agent is registered with SHA-256, listed
    in SHA256SUMS and made read-only; an odd session.json never stops the agent."""
    import stat

    from captation.integrity import verify_sha256sums

    cfg: Config = cfg_with(tmp_path)
    start = datetime(2026, 11, 23, 10, tzinfo=UTC)
    rec = storage.SessionRecord.create(
        cfg,
        session_id="S-20261123-100000-abce",
        app_id="zoom",
        category="meeting",
        consent_ref="P-1",
        started_at=start,
        host=storage.host_info(None, None),
    )
    rec.begin_segment(1, "audio", "rule:default", start, "none")
    rec.write()
    (rec.directory / "seg01_container.mkv").write_bytes(b"\x1a\x45\xdf\xa3partial")
    odd = rec.directory.parent / "S-20261123-100001-ffff"
    odd.mkdir()
    (odd / "session.json").write_text("[]", encoding="utf-8")
    assert storage.recover_stale_sessions(cfg, datetime(2026, 11, 23, 11, tzinfo=UTC)) == [
        rec.session_id
    ]
    doc = json.loads((rec.directory / "session.json").read_text())
    [entry] = doc["segments"][0]["files"]
    assert entry["role"] == "container" and entry["bytes"] == 11 and len(entry["sha256"]) == 64
    assert verify_sha256sums(rec.directory)
    mode = stat.S_IMODE((rec.directory / "seg01_container.mkv").stat().st_mode)
    assert not mode & stat.S_IWUSR
