"""Fail-closed countdown, debounced app changes and bounded failure handling (session machine)."""

from __future__ import annotations

import pytest

from captation.session import (
    Arm,
    LowDisk,
    ObsLost,
    PostprocessDone,
    SegmentStopped,
    StartFailed,
    State,
    Tick,
)
from tests.unit.session_support import Driver, check_invariants, ctx, goto, log_steps

SIGNAL = ctx()
SIGNAL_VIDEO = ctx(mode="video")
MEET = ctx("google_meet", category="meeting", pid=500, mode="video", reason="meeting_app")
BROWSER_SAME_PID = ctx("browser", category="browser", pid=500)
OTHER = ctx("whatsapp", pid=200)


def _to_countdown(d: Driver, app=SIGNAL) -> None:  # type: ignore[no-untyped-def]
    d.send(Arm("CR-1"), 0)
    d.send(Tick(app), 1)
    d.send(Tick(app), 3)
    assert d.state is State.COUNTDOWN


@pytest.mark.req("R-17")
def test_countdown_never_shown_means_no_recording() -> None:
    d = Driver(ack_countdown=False)
    _to_countdown(d)
    out = d.send_s(Tick(SIGNAL), 5)
    assert "Notify(countdown_failed)" in out
    assert "WriteSessionJson(cancelled)" in out
    assert not any(c.startswith("StartSegment") for c in out)
    assert d.state is State.ARMED_IDLE
    # the same call is not offered again while it lasts
    for _ in range(5):
        assert d.send_s(Tick(SIGNAL), 1) == []
    check_invariants(log_steps(d))


@pytest.mark.req("R-17")
def test_confirmation_for_another_session_does_not_count() -> None:
    from captation.session import CountdownShown

    d = Driver(ack_countdown=False)
    _to_countdown(d)
    d.send(CountdownShown("S-other"), 1)
    out = d.send_s(Tick(SIGNAL), 4)
    assert "Notify(countdown_failed)" in out and d.state is State.ARMED_IDLE


@pytest.mark.req("R-09")
def test_title_change_of_the_same_process_keeps_the_session() -> None:
    d = goto(State.RECORDING, app=MEET)
    for _ in range(20):  # e.g. switching from the meeting tab to a mail tab and back
        assert d.send_s(Tick(BROWSER_SAME_PID), 1) == []
        assert d.state is State.RECORDING
    assert d.m.current_app_id == "google_meet"
    check_invariants(log_steps(d))


@pytest.mark.req("R-09")
def test_brief_second_app_does_not_cut_the_recording() -> None:
    d = goto(State.RECORDING)
    assert d.send_s(Tick(OTHER), 1) == []
    assert d.state is State.PENDING_STOP
    assert d.send_s(Tick(SIGNAL), 2) == []
    assert d.state is State.RECORDING


@pytest.mark.req("R-53")
def test_deterministic_start_failure_finalises_once_as_failed() -> None:
    d = goto(State.RECORDING)
    out = d.send_s(StartFailed(), 1)
    assert out[:4] == [
        "MarkSegmentInterrupted(1)",
        "Notify(start_failed)",
        "Teardown",
        "RunPostprocess",
    ]
    assert d.send_s(PostprocessDone(), 1) == ["WriteSessionJson(failed)"]
    for _ in range(5):  # no retry storm for the same call
        assert d.send_s(Tick(SIGNAL), 1) == []
    check_invariants(log_steps(d))


@pytest.mark.req("R-53")
def test_obs_stopping_on_its_own_is_an_interruption() -> None:
    d = goto(State.RECORDING)
    out = d.send_s(ObsLost(reason="stopped"), 1)
    assert "Notify(recording_stopped)" in out and d.state is State.ERROR


@pytest.mark.req("R-45")
def test_low_disk_while_idle_notifies_once_per_episode() -> None:
    d = Driver()
    d.send(Arm("CR-1"), 0)
    assert d.send_s(LowDisk(), 1) == ["Notify(low_disk)"]
    assert d.send_s(LowDisk(quiet=True), 10) == []
    # a quiet report still refuses a pending start
    d.send(Tick(SIGNAL), 1)
    assert d.state is State.PENDING_START
    d.send(LowDisk(quiet=True), 1)
    assert d.state is State.ARMED_IDLE


@pytest.mark.req("R-08")
def test_failed_stop_during_upgrade_marks_the_session_interrupted() -> None:
    d = goto(State.RECORDING)
    assert any(c.startswith("StartSegment(2,video") for c in d.send_s(Tick(SIGNAL_VIDEO), 1))
    d.send(SegmentStopped(ok=False), 1)  # the audio segment did not stop cleanly
    d.send(Tick(None), 1)
    d.send(Tick(None), 10)
    d.send(SegmentStopped(), 1)
    assert d.send_s(PostprocessDone(), 1) == ["WriteSessionJson(interrupted)"]
