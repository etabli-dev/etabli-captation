"""Regression coverage for countdown cancellation and confirmed-stop recovery."""

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from captation import storage, transcribe
from captation.session import (
    Arm,
    Cancel,
    Disarm,
    DiscardSession,
    LowDisk,
    ManualStop,
    ObsBack,
    ObsLost,
    PostprocessDone,
    SegmentStopped,
    StartFailed,
    StartSegment,
    State,
    Tick,
)
from tests.unit.session_support import PARAMS, Driver, ctx
from tests.unit.test_obs_controller import cfg_with

pytestmark = pytest.mark.req("R-17", "R-53")


def live() -> Driver:
    d = Driver(params=replace(PARAMS, record_during_countdown=True))
    d.send(Arm("synthetic"))
    d.send(Tick(ctx()))
    d.send(Tick(ctx()), PARAMS.start_debounce_s)
    assert d.state is State.COUNTDOWN
    return d


@pytest.mark.parametrize("end", [Disarm(), ManualStop(), LowDisk()])
def test_lost_countdown_waits_for_confirmed_stop(end: object) -> None:
    d = live()
    d.send(ObsLost())
    commands = d.send(end)
    assert d.state is State.STOPPING
    assert d.m.status()["media_active"]
    assert not any(isinstance(c, DiscardSession) for c in commands)
    d.send(SegmentStopped(ok=False))
    assert d.state is State.ERROR
    commands = d.send(ObsBack())
    assert any(isinstance(c, DiscardSession) for c in commands)


def test_failed_start_must_confirm_stop_before_discard() -> None:
    d = live()
    commands = d.send(StartFailed())
    assert d.state is State.STOPPING
    assert not any(isinstance(c, DiscardSession) for c in commands)
    assert any(isinstance(c, DiscardSession) for c in d.send(SegmentStopped()))


def test_timely_cancel_is_honoured_after_deadline_processing() -> None:
    d = live()
    click = Cancel(d.clock.now() + 1, str(d.m.status()["session_id"]))
    d.send(Tick(ctx()), PARAMS.countdown_seconds + 2)
    assert d.state is State.RECORDING
    d.send(click)
    assert d.state is State.STOPPING
    assert any(isinstance(c, DiscardSession) for c in d.send(SegmentStopped()))


def test_old_notification_cannot_cancel_another_session() -> None:
    d = live()
    d.send(Cancel(d.clock.now(), "S-20261123-100000-dead"))
    assert d.state is State.COUNTDOWN


def test_detection_gap_at_deadline_is_debounced() -> None:
    d = live()
    commands = d.send(Tick(None), PARAMS.countdown_seconds)
    assert d.state is State.COUNTDOWN
    assert not any(isinstance(c, DiscardSession) for c in commands)
    d.send(Tick(ctx()), 1)
    assert d.state is State.RECORDING


def test_discard_failure_stays_pending_and_retries() -> None:
    d = live()
    d.send(Cancel())
    d.send(SegmentStopped())
    commands = d.send(PostprocessDone(False))
    assert d.state is State.POSTPROCESSING
    assert any(getattr(c, "kind", None) == "discard_failed" for c in commands)
    commands = d.send(Tick(None), 5)
    assert any(isinstance(c, DiscardSession) for c in commands)
    d.send(PostprocessDone(True))
    assert d.m.status()["session_id"] is None


def test_camera_change_during_countdown_is_initial_video_even_without_later_upgrades() -> None:
    d = Driver(params=replace(PARAMS, record_during_countdown=True, segments_on_mode_change=False))
    d.send(Arm("synthetic"))
    d.send(Tick(ctx()))
    d.send(Tick(ctx()), PARAMS.start_debounce_s)
    commands = d.send(Tick(ctx(mode="video")), PARAMS.countdown_seconds)
    assert any(isinstance(c, StartSegment) and c.mode == "video" for c in commands)


def test_provisional_crash_recovery_deletes_without_transcribing(tmp_path: Path) -> None:
    cfg = cfg_with(tmp_path)
    now = datetime(2026, 11, 24, tzinfo=UTC)
    rec = storage.SessionRecord.create(
        cfg,
        session_id="S-20261124-000000-abcd",
        app_id="signal",
        category="messenger",
        consent_ref="synthetic",
        started_at=now,
        host=storage.host_info(None, None),
    )
    rec.begin_segment(1, "audio", "rule:default", now, "none")
    rec.write()
    (rec.directory / storage.PROVISIONAL_FILE).write_text("pending\n")
    (rec.directory / "seg01_container.mkv").write_bytes(b"synthetic")
    assert not transcribe.needs_processing(rec.directory)
    assert storage.recover_stale_sessions(cfg, now) == [rec.session_id]
    assert {p.name for p in rec.directory.iterdir()} == {storage.SESSION_FILE}
    assert json.loads(rec.session_file.read_text())["status"] == "cancelled"


def test_idle_reminder_waits_for_suppressed_call_to_end() -> None:
    d = Driver(params=replace(PARAMS, armed_reminder_minutes=1))
    d.send(Arm("synthetic"))
    d.send(Tick(ctx()))
    d.send(Tick(ctx()), PARAMS.start_debounce_s)
    d.send(Cancel())
    commands = d.send(Tick(ctx()), 61)
    assert not any(getattr(c, "kind", None) == "still_armed" for c in commands)
    commands = d.send(Tick(None))
    assert any(getattr(c, "kind", None) == "still_armed" for c in commands)


def test_late_prewarm_cannot_confirm_a_stop() -> None:
    d = live()
    d.send(Cancel())
    commands = d.send(ObsBack(stop_confirmed=False))
    assert d.state is State.STOPPING
    assert not any(isinstance(c, DiscardSession) for c in commands)


def test_startup_recovery_never_touches_media_without_stop_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from captation import service

    def unavailable() -> None:
        raise OSError("synthetic disconnected OBS")

    def forbidden(*args: object) -> None:
        pytest.fail("recovery touched media before confirming stop")

    monkeypatch.setattr(service.manage, "obs_running", lambda cfg: True)
    monkeypatch.setattr(service.storage, "recover_stale_sessions", forbidden)
    fake = SimpleNamespace(
        d=SimpleNamespace(
            obs=SimpleNamespace(
                ensure_running=lambda **kwargs: None,
                reset=unavailable,
            )
        )
    )
    with pytest.raises(RuntimeError, match="stop confirmation"):
        service.recover_after_crash(cfg_with(tmp_path), fake)
