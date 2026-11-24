"""Session state machine: one test per transition of architecture §6, plus the contract details.

``goto(state)`` reaches each state through real transitions only. Each table row then sends one
or more events and checks the commands of the last event (compact ``summarize`` form) and the
resulting state.
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import timedelta

import pytest
import yaml

from captation.config import data_text, parse_config
from captation.session import (
    MAX_RECOVERIES,
    MAX_SEGMENTS,
    Arm,
    Armed,
    AutoDisarmDue,
    BeginSession,
    Cancel,
    CountdownElapsed,
    CountdownShown,
    Disarm,
    Event,
    LowDisk,
    ManualStop,
    NotifyCountdown,
    ObsBack,
    ObsLost,
    PostprocessDone,
    SegmentStopped,
    SessionMachine,
    SessionParams,
    SetTrayState,
    ShowReminder,
    StartFailed,
    StartSegment,
    State,
    Tick,
)
from tests.unit.session_support import (
    BASE_WALL,
    PARAMS,
    Driver,
    check_invariants,
    ctx,
    goto,
    log_steps,
    summarize,
)

SIGNAL = ctx()
SIGNAL_VIDEO = ctx(mode="video")
WHATSAPP = ctx("whatsapp", pid=200)

START_AUDIO = [
    "CloseCountdown",
    "SetTrayState(recording,audio,signal)",
    "StartSegment(1,audio,rule:default)",
    "WriteSessionJson(recording)",
    "ShowReminder",
]
UPGRADE = [
    "StopSegment(1)",
    "Teardown",
    "SetTrayState(recording,video,signal)",
    "StartSegment(2,video,upgrade:camera_on)",
    "WriteSessionJson(recording)",
]
STOP = ["StopSegment(1)", "Teardown"]
ABORT = ["CloseCountdown", "WriteSessionJson(cancelled)"]
AUTO_DISARM = 240 * 60.0

Row = tuple[State, list[tuple[float, Event]], list[str], State]

# id → (reqs, from state, [(dt, event), ...], commands of the last event, to state)
TABLE: dict[str, tuple[tuple[str, ...], Row]] = {
    # DISARMED
    "disarmed--arm": (
        ("R-14", "R-15"),
        (
            State.DISARMED,
            [(0, Arm("CR-001"))],
            ["Armed", "SetTrayState(armed)", "PrewarmObs"],
            State.ARMED_IDLE,
        ),
    ),
    "disarmed--arm-refused": (
        ("R-21", "R-32", "R-46"),
        (
            State.DISARMED,
            [(0, Arm("CR-001", ("exposure_policy_unmet", "no_tray")))],
            ["ArmRefused(exposure_policy_unmet,no_tray)"],
            State.DISARMED,
        ),
    ),
    "disarmed--context-ignored": (
        ("R-14",),
        (State.DISARMED, [(1, Tick(SIGNAL)), (60, Tick(SIGNAL))], [], State.DISARMED),
    ),
    # ARMED_IDLE
    "idle--context-present": (
        ("R-10",),
        (State.ARMED_IDLE, [(1, Tick(SIGNAL))], [], State.PENDING_START),
    ),
    "idle--low-disk": (
        ("R-45",),
        (State.ARMED_IDLE, [(1, LowDisk())], ["Notify(low_disk)"], State.ARMED_IDLE),
    ),
    # PENDING_START
    "pending-start--stable": (
        ("R-10", "R-17"),
        (
            State.PENDING_START,
            [(3, Tick(SIGNAL))],
            ["BeginSession(signal)", "NotifyCountdown(signal,5)"],
            State.COUNTDOWN,
        ),
    ),
    "pending-start--not-yet-stable": (
        ("R-10",),
        (State.PENDING_START, [(2.9, Tick(SIGNAL))], [], State.PENDING_START),
    ),
    "pending-start--app-switch-restarts-debounce": (
        ("R-10",),
        (State.PENDING_START, [(2, Tick(WHATSAPP)), (2, Tick(WHATSAPP))], [], State.PENDING_START),
    ),
    "pending-start--lost": (
        ("R-10",),
        (State.PENDING_START, [(1, Tick(None))], [], State.ARMED_IDLE),
    ),
    "pending-start--low-disk": (
        ("R-45",),
        (State.PENDING_START, [(1, LowDisk())], ["Notify(low_disk)"], State.ARMED_IDLE),
    ),
    "pending-start--low-disk-no-restart": (
        ("R-45",),
        (State.PENDING_START, [(1, LowDisk()), (5, Tick(SIGNAL))], [], State.ARMED_IDLE),
    ),
    # COUNTDOWN
    "countdown--elapsed": (
        ("R-03", "R-17", "R-19"),
        (State.COUNTDOWN, [(5, Tick(SIGNAL))], START_AUDIO, State.RECORDING),
    ),
    "countdown--elapsed-event": (
        ("R-17",),
        (State.COUNTDOWN, [(0, CountdownElapsed())], START_AUDIO, State.RECORDING),
    ),
    "countdown--not-elapsed": (
        ("R-17",),
        (State.COUNTDOWN, [(4.9, Tick(SIGNAL))], [], State.COUNTDOWN),
    ),
    "countdown--elapsed-with-camera": (
        ("R-02",),
        (
            State.COUNTDOWN,
            [(5, Tick(SIGNAL_VIDEO))],
            [
                "CloseCountdown",
                "SetTrayState(recording,video,signal)",
                "StartSegment(1,video,rule:camera_active)",
                "WriteSessionJson(recording)",
                "ShowReminder",
            ],
            State.RECORDING,
        ),
    ),
    "countdown--cancel": (
        ("R-17", "R-37"),
        (State.COUNTDOWN, [(1, Cancel())], [*ABORT, "Notify(start_cancelled)"], State.ARMED_IDLE),
    ),
    "countdown--manual-stop-cancels": (
        ("R-17", "R-37"),
        (
            State.COUNTDOWN,
            [(1, ManualStop())],
            [*ABORT, "Notify(start_cancelled)"],
            State.ARMED_IDLE,
        ),
    ),
    "countdown--lost-at-elapse": (
        ("R-17",),
        (State.COUNTDOWN, [(1, Tick(None)), (4, Tick(None))], ABORT, State.ARMED_IDLE),
    ),
    "countdown--lost-briefly": (
        ("R-17",),
        (State.COUNTDOWN, [(1, Tick(None)), (4, Tick(SIGNAL))], START_AUDIO, State.RECORDING),
    ),
    "countdown--other-app": (
        ("R-09",),
        (State.COUNTDOWN, [(1, Tick(WHATSAPP))], ABORT, State.PENDING_START),
    ),
    "countdown--low-disk": (
        ("R-45",),
        (State.COUNTDOWN, [(1, LowDisk())], [*ABORT, "Notify(low_disk)"], State.ARMED_IDLE),
    ),
    "countdown--obs-lost": (
        ("R-37",),
        # The call is still there, so it waits in PENDING_START until OBS is back.
        (State.COUNTDOWN, [(1, ObsLost())], [*ABORT, "Notify(obs_lost)"], State.PENDING_START),
    ),
    # RECORDING
    "recording--camera-on-upgrade": (
        ("R-08",),
        (State.RECORDING, [(1, Tick(SIGNAL_VIDEO))], UPGRADE, State.RECORDING),
    ),
    "recording--app-change": (
        ("R-09",),
        # Another app is debounced like a lost call (tab switch, a second app opening the mic).
        (State.RECORDING, [(1, Tick(WHATSAPP))], [], State.PENDING_STOP),
    ),
    "recording--lost": (
        ("R-10",),
        (State.RECORDING, [(1, Tick(None))], [], State.PENDING_STOP),
    ),
    "recording--manual-stop": (
        ("R-37",),
        (State.RECORDING, [(1, ManualStop())], STOP, State.STOPPING),
    ),
    "recording--disarm": (
        ("R-16",),
        (State.RECORDING, [(1, Disarm())], [*STOP, "Disarmed(user)"], State.STOPPING),
    ),
    "recording--auto-disarm-event": (
        ("R-16",),
        (
            State.RECORDING,
            [(1, AutoDisarmDue())],
            [*STOP, "Disarmed(auto)", "Notify(auto_disarmed)"],
            State.STOPPING,
        ),
    ),
    "recording--auto-disarm-expiry": (
        ("R-16",),
        (
            State.RECORDING,
            [(AUTO_DISARM, Tick(SIGNAL))],
            [*STOP, "Disarmed(auto)", "Notify(auto_disarmed)"],
            State.STOPPING,
        ),
    ),
    "recording--low-disk": (
        ("R-45",),
        (State.RECORDING, [(1, LowDisk())], [*STOP, "Notify(low_disk)"], State.STOPPING),
    ),
    "recording--obs-lost": (
        ("R-53",),
        (
            State.RECORDING,
            [(1, ObsLost())],
            ["MarkSegmentInterrupted(1)", "Notify(obs_lost)"],
            State.ERROR,
        ),
    ),
    # PENDING_STOP
    "pending-stop--back": (
        ("R-10",),
        (State.PENDING_STOP, [(1, Tick(SIGNAL))], [], State.RECORDING),
    ),
    "pending-stop--back-with-camera": (
        ("R-08",),
        (State.PENDING_STOP, [(1, Tick(SIGNAL_VIDEO))], UPGRADE, State.RECORDING),
    ),
    "pending-stop--not-elapsed": (
        ("R-10",),
        (State.PENDING_STOP, [(9.9, Tick(None))], [], State.PENDING_STOP),
    ),
    "pending-stop--elapsed": (
        ("R-10",),
        (State.PENDING_STOP, [(10, Tick(None))], STOP, State.STOPPING),
    ),
    "pending-stop--app-change": (
        ("R-09",),
        (State.PENDING_STOP, [(1, Tick(WHATSAPP))], [], State.PENDING_STOP),
    ),
    "pending-stop--app-change-elapsed": (
        ("R-09",),
        (State.PENDING_STOP, [(10, Tick(WHATSAPP))], STOP, State.STOPPING),
    ),
    "pending-stop--manual-stop": (
        ("R-37",),
        (State.PENDING_STOP, [(1, ManualStop())], STOP, State.STOPPING),
    ),
    "pending-stop--disarm": (
        ("R-16",),
        (State.PENDING_STOP, [(1, Disarm())], [*STOP, "Disarmed(user)"], State.STOPPING),
    ),
    "pending-stop--auto-disarm": (
        ("R-16",),
        (
            State.PENDING_STOP,
            [(1, AutoDisarmDue())],
            [*STOP, "Disarmed(auto)", "Notify(auto_disarmed)"],
            State.STOPPING,
        ),
    ),
    "pending-stop--low-disk": (
        ("R-45",),
        (State.PENDING_STOP, [(1, LowDisk())], [*STOP, "Notify(low_disk)"], State.STOPPING),
    ),
    "pending-stop--obs-lost": (
        ("R-53",),
        (
            State.PENDING_STOP,
            [(1, ObsLost())],
            ["MarkSegmentInterrupted(1)", "Notify(obs_lost)"],
            State.ERROR,
        ),
    ),
    # ERROR
    "error--obs-back-context-present": (
        ("R-53",),
        (
            State.ERROR,
            [(5, ObsBack())],
            ["StartSegment(2,audio,recovery:obs_restart)", "WriteSessionJson(recording)"],
            State.RECORDING,
        ),
    ),
    "error--obs-back-context-gone": (
        ("R-53",),
        (
            State.ERROR,
            [(1, Tick(None)), (1, ObsBack())],
            ["Teardown", "RunPostprocess", "SetTrayState(armed)"],
            State.POSTPROCESSING,
        ),
    ),
    "error--obs-back-other-app": (
        ("R-53", "R-09"),
        (
            State.ERROR,
            [(1, Tick(WHATSAPP)), (1, ObsBack())],
            ["Teardown", "RunPostprocess", "SetTrayState(armed)"],
            State.POSTPROCESSING,
        ),
    ),
    "error--manual-stop": (
        ("R-53",),
        (
            State.ERROR,
            [(1, ManualStop())],
            ["RunPostprocess", "SetTrayState(armed)"],
            State.POSTPROCESSING,
        ),
    ),
    "error--low-disk": (
        ("R-45",),
        (
            State.ERROR,
            [(1, LowDisk())],
            ["RunPostprocess", "Notify(low_disk)", "SetTrayState(armed)"],
            State.POSTPROCESSING,
        ),
    ),
    # STOPPING
    "stopping--stopped": (
        ("R-37",),
        (
            State.STOPPING,
            [(1, SegmentStopped())],
            ["RunPostprocess", "SetTrayState(armed)"],
            State.POSTPROCESSING,
        ),
    ),
    "stopping--stop-failed": (
        ("R-53",),
        (
            State.STOPPING,
            [(1, SegmentStopped(ok=False))],
            # unconfirmed stop: OBS may still record → ERROR (indicator on), recovery resets it
            ["MarkSegmentInterrupted(1)", "Notify(obs_lost)"],
            State.ERROR,
        ),
    ),
    "stopping--obs-lost": (
        ("R-53",),
        (
            State.STOPPING,
            [(1, ObsLost())],
            ["MarkSegmentInterrupted(1)", "Notify(obs_lost)"],
            State.ERROR,
        ),
    ),
    # POSTPROCESSING
    "postprocessing--done": (
        ("R-37",),
        (
            State.POSTPROCESSING,
            [(1, PostprocessDone())],
            ["WriteSessionJson(complete)"],
            State.ARMED_IDLE,
        ),
    ),
    "postprocessing--failed": (
        ("R-37",),
        (
            State.POSTPROCESSING,
            [(1, PostprocessDone(ok=False))],
            ["WriteSessionJson(failed)"],
            State.ARMED_IDLE,
        ),
    ),
    "postprocessing--done-disarm-pending": (
        ("R-16",),
        (
            State.POSTPROCESSING,
            [(1, Disarm()), (1, PostprocessDone())],
            ["WriteSessionJson(complete)"],
            State.DISARMED,
        ),
    ),
    "postprocessing--done-new-call-present": (
        ("R-09",),
        (
            State.POSTPROCESSING,
            [(1, Tick(WHATSAPP)), (1, PostprocessDone())],
            ["WriteSessionJson(complete)"],
            State.PENDING_START,
        ),
    ),
    "postprocessing--done-interrupted": (
        ("R-53",),
        (
            State.ERROR,
            [(1, Tick(None)), (1, ObsBack()), (1, PostprocessDone())],
            ["WriteSessionJson(interrupted)"],
            State.ARMED_IDLE,
        ),
    ),
}


def _table_params() -> list[object]:
    return [
        pytest.param(*row, id=row_id, marks=pytest.mark.req(*reqs))
        for row_id, (reqs, row) in TABLE.items()
    ]


@pytest.mark.parametrize(("start", "events", "expected", "end"), _table_params())
def test_transition_table(
    start: State, events: list[tuple[float, Event]], expected: list[str], end: State
) -> None:
    d = goto(start)
    assert d.state is start
    last: list[str] = []
    for dt, event in events:
        last = d.send_s(event, dt)
    assert last == expected
    assert d.state is end
    d.drain()
    assert d.state is State.DISARMED
    check_invariants(log_steps(d))


def test_table_covers_every_state() -> None:
    assert {row[0] for _, row in TABLE.values()} == set(State)


# ---- "any | disarm | DISARMED (via STOPPING if recording)" -----------------------------------

DISARM_FROM: dict[State, list[str]] = {
    State.DISARMED: [],
    State.ARMED_IDLE: ["Disarmed(user)", "SetTrayState(disarmed)"],
    State.PENDING_START: ["Disarmed(user)", "SetTrayState(disarmed)"],
    State.COUNTDOWN: [*ABORT, "Disarmed(user)", "SetTrayState(disarmed)"],
    State.RECORDING: [*STOP, "Disarmed(user)"],
    State.PENDING_STOP: [*STOP, "Disarmed(user)"],
    State.STOPPING: ["Disarmed(user)"],
    State.POSTPROCESSING: ["Disarmed(user)", "SetTrayState(disarmed)"],
    State.ERROR: ["RunPostprocess", "Disarmed(user)", "SetTrayState(disarmed)"],
}


@pytest.mark.req("R-14", "R-16", "R-37")
@pytest.mark.parametrize("start", list(State), ids=lambda s: s.name)
def test_disarm_from_any_state(start: State) -> None:
    d = goto(start)
    assert d.send_s(Disarm(), 1) == DISARM_FROM[start]
    assert not d.m.armed
    assert d.state is not State.RECORDING
    d.send(SegmentStopped(), 1)
    d.send(PostprocessDone(), 1)
    assert d.state is State.DISARMED
    assert d.send_s(Tick(SIGNAL), 100) == []
    assert d.state is State.DISARMED
    check_invariants(log_steps(d))


@pytest.mark.req("R-16")
@pytest.mark.parametrize("start", list(State), ids=lambda s: s.name)
def test_auto_disarm_by_clock_from_any_state(start: State) -> None:
    d = goto(start)
    d.send(Tick(None), AUTO_DISARM)
    d.send(SegmentStopped(), 1)
    d.send(PostprocessDone(), 1)
    assert d.state is State.DISARMED
    sent = [summarize(c) for c in d.commands()]
    if start is not State.DISARMED:
        assert sent.count("Disarmed(auto)") == 1
        assert sent.count("Notify(auto_disarmed)") == 1
    check_invariants(log_steps(d))


# ---- events that must not do anything in a state ---------------------------------------------

IGNORED: list[tuple[State, Event]] = [
    *[
        (State.DISARMED, e)
        for e in (
            Disarm(),
            AutoDisarmDue(),
            Cancel(),
            ManualStop(),
            LowDisk(),
            ObsLost(),
            ObsBack(),
            CountdownElapsed(),
            SegmentStopped(),
            PostprocessDone(),
        )
    ],
    *[
        (State.ARMED_IDLE, e)
        for e in (
            Cancel(),
            ManualStop(),
            ObsBack(),
            CountdownElapsed(),
            SegmentStopped(),
            PostprocessDone(),
        )
    ],
    *[(State.PENDING_START, e) for e in (Cancel(), ManualStop(), SegmentStopped())],
    *[
        (State.RECORDING, e)
        for e in (
            Cancel(),
            CountdownElapsed(),
            ObsBack(),
            SegmentStopped(),
            PostprocessDone(),
            Tick(SIGNAL),
        )
    ],
    *[(State.STOPPING, e) for e in (Cancel(), ManualStop(), LowDisk(), Tick(SIGNAL))],
    *[
        (State.POSTPROCESSING, e)
        for e in (Cancel(), ManualStop(), LowDisk(), ObsLost(), SegmentStopped(), Tick(None))
    ],
    *[(State.ERROR, e) for e in (Cancel(), ObsLost(), SegmentStopped(), Tick(SIGNAL))],
]


@pytest.mark.req("R-37")
@pytest.mark.parametrize(
    ("start", "event"), IGNORED, ids=[f"{s.name}-{type(e).__name__}" for s, e in IGNORED]
)
def test_event_without_effect(start: State, event: Event) -> None:
    d = goto(start)
    assert d.send_s(event, 0.5) == []
    assert d.state is start


# ---- details of the contract -----------------------------------------------------------------


@pytest.mark.req("R-15")
def test_machine_starts_disarmed_and_arming_is_not_persisted() -> None:
    d = goto(State.RECORDING)
    assert d.m.armed
    restarted = SessionMachine(PARAMS, d.clock, d.ids)
    assert restarted.state is State.DISARMED
    assert not restarted.armed
    assert restarted.status()["armed"] is False
    assert restarted.handle(Tick(SIGNAL)) == []
    assert restarted.state is State.DISARMED


@pytest.mark.req("R-24", "R-20")
def test_session_ids_and_begin_session() -> None:
    d = goto(State.RECORDING)
    cmds = d.commands()
    begin = next(c for c in cmds if isinstance(c, BeginSession))
    assert re.fullmatch(r"S-\d{8}-\d{6}-[0-9a-f]{4}", begin.session_id)
    assert begin.consent_ref == "CR-001"
    assert begin.started_at == BASE_WALL + timedelta(seconds=4)  # when the countdown began
    assert begin.context == SIGNAL
    sids = {c.session_id for c in cmds if hasattr(c, "session_id")}
    assert sids == {begin.session_id}


@pytest.mark.req("R-18")
def test_indicator_goes_up_before_the_media_starts() -> None:
    cmds = goto(State.COUNTDOWN).send(Tick(SIGNAL), 5)
    tray = next(i for i, c in enumerate(cmds) if isinstance(c, SetTrayState))
    start = next(i for i, c in enumerate(cmds) if isinstance(c, StartSegment))
    assert tray < start
    assert cmds[tray] == SetTrayState("recording", "audio", "signal")


@pytest.mark.req("R-08")
def test_mode_is_never_downgraded() -> None:
    d = goto(State.RECORDING)
    d.send(Tick(SIGNAL_VIDEO), 1)
    assert d.m.status()["mode"] == "video"
    assert d.send_s(Tick(SIGNAL), 1) == []
    assert d.send_s(Tick(None), 1) == []
    assert d.send_s(Tick(SIGNAL), 1) == []
    assert d.m.status()["mode"] == "video"
    # OBS recovery keeps video, too.
    d.send(ObsLost(), 1)
    assert d.send_s(ObsBack(), 1) == [
        "StartSegment(3,video,recovery:obs_restart)",
        "WriteSessionJson(recording)",
    ]


@pytest.mark.req("R-08")
def test_meeting_starts_in_video_and_stays_there() -> None:
    zoom = ctx("zoom", category="meeting", pid=42, mode="video", reason="meeting_app")
    d = goto(State.RECORDING, app=zoom)
    assert d.m.status()["mode"] == "video"
    assert d.send_s(Tick(zoom), 1) == []


@pytest.mark.req("R-08")
def test_no_upgrade_without_segments_on_mode_change() -> None:
    d = Driver(params=replace(PARAMS, segments_on_mode_change=False))
    d.send(Arm(None))
    d.send(Tick(SIGNAL), 1)
    d.send(Tick(SIGNAL), 3)
    d.send(Tick(SIGNAL), 5)
    assert d.state is State.RECORDING
    assert d.send_s(Tick(SIGNAL_VIDEO), 1) == []
    d.send(ObsLost(), 1)
    assert d.send_s(ObsBack(), 1) == [
        "StartSegment(2,audio,recovery:obs_restart)",
        "WriteSessionJson(recording)",
    ]


@pytest.mark.req("R-09")
def test_app_change_finalises_and_starts_a_new_session_with_countdown() -> None:
    d = goto(State.RECORDING)
    assert d.send_s(Tick(WHATSAPP), 1) == []
    assert d.state is State.PENDING_STOP
    assert d.send_s(Tick(WHATSAPP), 10) == STOP
    d.send(Tick(WHATSAPP), 1)
    d.send(SegmentStopped(), 1)
    assert d.send_s(PostprocessDone(), 1) == ["WriteSessionJson(complete)"]
    assert d.state is State.PENDING_START
    assert d.send_s(Tick(WHATSAPP), 3) == ["BeginSession(whatsapp)", "NotifyCountdown(whatsapp,5)"]
    assert d.send_s(Tick(WHATSAPP), 5)[2] == "StartSegment(1,audio,rule:default)"
    begins = [c.session_id for c in d.commands() if isinstance(c, BeginSession)]
    assert len(begins) == 2 and begins[0] != begins[1]
    check_invariants(log_steps(d))


@pytest.mark.req("R-17")
def test_cancelled_call_is_not_offered_again_until_it_ends() -> None:
    d = goto(State.COUNTDOWN)
    d.send(Cancel(), 1)
    for _ in range(30):
        assert d.send_s(Tick(SIGNAL), 1) == []
        assert d.state is State.ARMED_IDLE
    d.send(Tick(None), 1)
    assert d.send_s(Tick(None), 9.9) == []
    d.send(Tick(SIGNAL), 0.1)
    assert d.state is State.ARMED_IDLE  # absent for 9.9 s only: still the same call
    d.send(Tick(None), 1)
    d.send(Tick(None), 10)
    d.send(Tick(SIGNAL), 1)
    assert d.state is State.PENDING_START  # absent ≥ stop_debounce: a new call


@pytest.mark.req("R-17")
def test_suppression_ends_with_a_different_app_or_rearming() -> None:
    d = goto(State.COUNTDOWN)
    d.send(Cancel(), 1)
    d.send(Tick(WHATSAPP), 1)
    assert d.state is State.PENDING_START
    d.send(Tick(SIGNAL), 1)
    assert d.state is State.PENDING_START  # signal is no longer suppressed

    d = goto(State.COUNTDOWN)
    d.send(Cancel(), 1)
    d.send(Tick(SIGNAL), 1)
    assert d.state is State.ARMED_IDLE
    assert d.send_s(Arm("CR-002"), 1)[0] == "Armed"
    assert d.state is State.PENDING_START


@pytest.mark.req("R-37")
def test_manual_stop_does_not_restart_the_same_call() -> None:
    d = goto(State.RECORDING)
    d.send(ManualStop(), 1)
    d.send(SegmentStopped(), 1)
    d.send(PostprocessDone(), 1)
    assert d.state is State.ARMED_IDLE and d.m.armed
    for _ in range(20):
        d.send(Tick(SIGNAL), 1)
    assert d.state is State.ARMED_IDLE

    d = goto(State.ERROR)
    d.send(ManualStop(), 1)
    d.send(ObsBack(), 1)
    d.send(PostprocessDone(), 1)
    d.send(Tick(SIGNAL), 5)
    assert d.state is State.ARMED_IDLE


@pytest.mark.req("R-45")
def test_low_disk_while_recording_stops_gracefully_and_does_not_restart() -> None:
    d = goto(State.RECORDING)
    assert d.send_s(LowDisk(), 1) == [*STOP, "Notify(low_disk)"]
    d.send(SegmentStopped(), 1)
    assert d.send_s(PostprocessDone(), 1) == ["WriteSessionJson(complete)"]
    d.send(Tick(SIGNAL), 10)
    assert d.state is State.ARMED_IDLE


def test_countdown_aborts_when_the_call_is_gone_for_stop_debounce() -> None:
    d = Driver(params=replace(PARAMS, stop_debounce_s=2.0, countdown_seconds=10))
    d.send(Arm(None))
    d.send(Tick(SIGNAL), 1)
    d.send(Tick(SIGNAL), 3)
    assert d.state is State.COUNTDOWN
    assert d.send_s(Tick(None), 1) == []
    assert d.send_s(Tick(None), 1.9) == []
    assert d.send_s(Tick(None), 0.1) == ABORT  # absent for 2 s, countdown not yet elapsed
    assert d.state is State.ARMED_IDLE


@pytest.mark.req("R-53")
def test_no_countdown_while_obs_is_lost() -> None:
    d = goto(State.ARMED_IDLE)
    assert d.send_s(ObsLost(), 1) == []
    d.send(Tick(SIGNAL), 1)
    assert d.send_s(Tick(SIGNAL), 30) == []
    assert d.state is State.PENDING_START
    assert d.send_s(ObsBack(), 1) == ["BeginSession(signal)", "NotifyCountdown(signal,5)"]


@pytest.mark.req("R-53")
@pytest.mark.req("R-53")
def test_obs_recovery_is_bounded_per_session() -> None:
    d = goto(State.RECORDING)
    for _ in range(MAX_RECOVERIES):
        d.send(ObsLost(), 1)
        assert "StartSegment" in " ".join(d.send_s(ObsBack(), 1))
    assert d.m.status()["segment"] == 1 + MAX_RECOVERIES
    d.send(ObsLost(), 1)
    assert d.send_s(ObsBack(), 1) == [
        "Notify(start_failed)",
        "Teardown",
        "RunPostprocess",
        "SetTrayState(armed)",
    ]
    assert d.send_s(PostprocessDone(), 1) == ["WriteSessionJson(interrupted)"]
    assert d.send_s(Tick(SIGNAL), 1) == []  # the failing call is not retried
    assert MAX_RECOVERIES + 2 <= MAX_SEGMENTS
    check_invariants(log_steps(d))


@pytest.mark.req("R-16", "R-20")
def test_rearm_while_armed_refreshes_expiry_and_reference() -> None:
    d = goto(State.RECORDING)
    cmds = d.send(Arm("CR-002"), 100)
    assert cmds == [Armed("CR-002", d.clock.t + AUTO_DISARM)]
    assert d.m.status()["consent_ref"] == "CR-002"
    assert d.m.status()["auto_disarm_in_s"] == AUTO_DISARM
    assert d.send_s(Arm("CR-003", ("exposure_policy_unmet",)), 1) == [
        "ArmRefused(exposure_policy_unmet)"
    ]
    assert d.m.armed and d.state is State.RECORDING
    assert d.m.status()["consent_ref"] == "CR-002"


@pytest.mark.req("R-15", "R-16")
def test_rearm_during_finalisation_keeps_the_agent_armed() -> None:
    d = goto(State.STOPPING)
    d.send(Disarm(), 1)
    assert d.send_s(Arm("CR-009"), 1) == ["Armed", "PrewarmObs"]
    d.send(SegmentStopped(), 1)
    d.send(PostprocessDone(), 1)
    assert d.state is State.ARMED_IDLE and d.m.armed


def test_status_reports_the_running_session() -> None:
    d = goto(State.RECORDING)
    d.send(Tick(SIGNAL), 30)
    st = d.m.status()
    assert st == {
        "state": "RECORDING",
        "armed": True,
        "consent_ref": "CR-001",
        "auto_disarm_in_s": AUTO_DISARM - d.clock.t,
        "media_active": True,
        "session_id": st["session_id"],
        "app_id": "signal",
        "mode": "audio",
        "segment": 1,
        "recording_elapsed_s": 30.0,
    }
    assert isinstance(st["session_id"], str)
    idle = Driver().m.status()
    assert idle["state"] == "DISARMED" and idle["armed"] is False
    assert idle["auto_disarm_in_s"] is None and idle["session_id"] is None
    assert idle["segment"] is None and idle["recording_elapsed_s"] is None
    pending = goto(State.PENDING_START)
    assert pending.m.status()["app_id"] == "signal"
    assert pending.m.current_app_id is None  # tie-break (a) is about an open session only
    assert goto(State.COUNTDOWN).m.current_app_id == "signal"
    assert goto(State.ARMED_IDLE).m.current_app_id is None
    countdown = goto(State.COUNTDOWN).m.status()
    assert countdown["app_id"] == "signal" and countdown["segment"] is None


def test_params_from_config() -> None:
    cfg = parse_config(yaml.safe_load(data_text("config.default.yaml")))
    params = SessionParams.from_config(cfg)
    assert params == SessionParams(
        start_debounce_s=cfg.detection.start_debounce_s,
        stop_debounce_s=cfg.detection.stop_debounce_s,
        countdown_seconds=cfg.consent.countdown_seconds,
        auto_disarm_after_minutes=cfg.consent.auto_disarm_after_minutes,
        segments_on_mode_change=cfg.recording.segments_on_mode_change,
        armed_reminder_minutes=cfg.consent.armed_reminder_minutes,
        record_during_countdown=cfg.consent.record_during_countdown,
    )
    assert params.auto_disarm_after_minutes is None and params.armed_reminder_minutes == 240
    assert params.record_during_countdown is True and params.start_debounce_s == 1


def test_zero_start_debounce_reaches_countdown_in_one_tick() -> None:
    d = Driver(params=replace(PARAMS, start_debounce_s=0.0))
    d.send(Arm(None))
    assert d.send_s(Tick(SIGNAL), 1) == ["BeginSession(signal)", "NotifyCountdown(signal,5)"]


# ---- audit 2026-11-24: fail-closed countdown and unconfirmed stops ---------------------------


@pytest.mark.req("R-16", "R-17")
def test_full_countdown_runs_from_the_confirmation() -> None:
    """Slow pre-flight checks must not eat into the time to cancel (§2.4, audit 2026-11-24)."""
    d = Driver(ack_countdown=False)
    d.send(Arm("CR-001"))
    d.send(Tick(ctx()), 1.0)
    cmds = d.send(Tick(ctx()), PARAMS.start_debounce_s)
    assert d.state is State.COUNTDOWN
    sid = next(c.session_id for c in cmds if isinstance(c, NotifyCountdown))
    d.send(Tick(ctx()), PARAMS.countdown_seconds - 1.0)  # shown late, but before the deadline
    d.send(CountdownShown(sid), 0.0)
    d.send(Tick(ctx()), PARAMS.countdown_seconds - 0.5)
    assert d.state is State.COUNTDOWN  # the full countdown runs from CountdownShown
    assert not any(isinstance(c, StartSegment) for c in d.commands())
    d.send(Tick(ctx()), 0.5)
    assert d.state is State.RECORDING


@pytest.mark.req("R-53", "R-02")
def test_unconfirmed_stop_after_disarm_never_resumes_recording() -> None:
    d = goto(State.RECORDING)
    d.send(Disarm(), 1.0)
    assert d.state is State.STOPPING
    d.send(SegmentStopped(ok=False), 1.0)
    assert d.state is State.ERROR and not d.m.armed
    cmds = d.send(ObsBack(), 1.0)  # the call is still going on
    assert not any(isinstance(c, StartSegment) for c in cmds)
    assert d.state is State.POSTPROCESSING


@pytest.mark.req("R-53", "R-16")
def test_ended_session_never_resumes_after_rearm() -> None:
    """Found by the property test: disarm → OBS lost while stopping → re-arm → ObsBack must not
    resume the old session without a new countdown."""
    d = goto(State.RECORDING)
    d.send(Disarm(), 1.0)
    d.send(ObsLost(), 0.5)
    assert d.state is State.ERROR
    d.send(Arm("CR-002"), 1.0)
    cmds = d.send(ObsBack(), 0.5)
    assert not any(isinstance(c, StartSegment) for c in cmds)
    assert d.state is State.POSTPROCESSING


# ---- ADR-021: study mode without a time limit, with a reminder ------------------------------


@pytest.mark.req("R-16")
def test_without_a_time_limit_study_mode_stays_on_and_reminds() -> None:
    params = replace(PARAMS, auto_disarm_after_minutes=None, armed_reminder_minutes=240)
    d = Driver(params=params)
    d.send(Arm("CR-001"))
    assert d.m.status()["auto_disarm_in_s"] is None
    assert d.send_s(Tick(None), 239 * 60) == []
    assert d.send_s(Tick(None), 60) == ["Notify(still_armed)"]  # after 4 h
    assert d.send_s(Tick(None), 60) == []  # once per interval
    assert d.send_s(Tick(None), 240 * 60) == ["Notify(still_armed)"]
    for _ in range(3):  # days later: still on, never disarmed by time
        d.send(Tick(None), 24 * 3600)
    assert d.m.armed and d.state is State.ARMED_IDLE
    d.send(Disarm())
    assert d.send_s(Tick(None), 240 * 60) == []  # no reminder once ended


@pytest.mark.req("R-16")
def test_reminder_never_interrupts_a_call() -> None:
    params = replace(PARAMS, auto_disarm_after_minutes=None, armed_reminder_minutes=240)
    d = Driver(params=params)
    d.send(Arm("CR-001"))
    d.send(Tick(ctx()), 239 * 60)
    d.send(Tick(ctx()), PARAMS.start_debounce_s)
    d.send(Tick(ctx()), PARAMS.countdown_seconds + 1)
    assert d.state is State.RECORDING
    for _ in range(5):  # the reminder falls due during the call
        assert "Notify(still_armed)" not in d.send_s(Tick(ctx()), 60)
    d.send(Tick(None), 1)
    d.send(Tick(None), PARAMS.stop_debounce_s + 1)
    assert d.state is State.STOPPING
    assert "Notify(still_armed)" not in d.send_s(SegmentStopped(ok=True))
    after = d.send_s(PostprocessDone(ok=True))
    assert d.state is State.ARMED_IDLE
    assert "Notify(still_armed)" in after  # as soon as the call is finalised
    assert "Notify(still_armed)" not in d.send_s(Tick(None), 1)  # and only once


def test_time_limit_still_works_without_a_reminder() -> None:
    d = Driver(params=replace(PARAMS, auto_disarm_after_minutes=5, armed_reminder_minutes=None))
    d.send(Arm("CR-001"))
    d.send(Tick(None), 5 * 60)
    assert not d.m.armed


# ---- ADR-022: recording during the countdown ---------------------------------------------------

LIVE = replace(PARAMS, record_during_countdown=True)


def _live_countdown(d: Driver) -> list[str]:
    d.send(Arm("CR-001"))
    d.send(Tick(ctx()), 1.0)
    out = d.send_s(Tick(ctx()), PARAMS.start_debounce_s)
    assert d.state is State.COUNTDOWN
    return out


@pytest.mark.req("R-17", "R-18")
def test_live_countdown_records_from_the_shown_countdown() -> None:
    d = Driver(params=LIVE)
    out = _live_countdown(d)
    assert any(c.startswith("StartSegment") for c in out)  # the driver confirms "shown" at once
    assert d.m.status()["media_active"] is True
    d.send(Tick(ctx()), PARAMS.countdown_seconds)
    assert d.state is State.RECORDING
    starts = [c for c in d.commands() if isinstance(c, StartSegment)]
    assert len(starts) == 1 and starts[0].index == 1  # kept, no second start
    assert any(isinstance(c, ShowReminder) for c in d.commands())
    check_invariants(log_steps(d))


@pytest.mark.req("R-17", "R-19")
@pytest.mark.parametrize(
    "abort",
    ["cancel", "disarm", "hang_up", "other_app", "low_disk", "manual_stop"],
)
def test_live_countdown_aborts_delete_the_media(abort: str) -> None:
    d = Driver(params=LIVE)
    _live_countdown(d)
    event = {
        "cancel": Cancel(),
        "disarm": Disarm(),
        "low_disk": LowDisk(),
        "manual_stop": ManualStop(),
        "hang_up": Tick(None),
        "other_app": Tick(WHATSAPP),
    }[abort]
    d.send(event, 1.0)
    if abort == "hang_up":
        d.send(Tick(None), PARAMS.stop_debounce_s)
    assert d.state is State.STOPPING  # OBS must confirm the stop before anything is deleted
    out = d.send_s(SegmentStopped(ok=True), 1.0)
    assert any(c.startswith("DiscardSession") for c in out)
    assert not any(c.startswith("RunPostprocess") for c in out)
    out = d.send_s(PostprocessDone(ok=True), 1.0)
    assert "WriteSessionJson(cancelled)" in out
    if abort == "disarm":
        assert d.state is State.DISARMED
    elif abort == "other_app":
        assert d.state is State.PENDING_START  # the new app's call is offered at once
    else:
        assert d.state is State.ARMED_IDLE
    check_invariants(log_steps(d))


@pytest.mark.req("R-17", "R-53")
def test_live_countdown_obs_loss_waits_for_recovery_then_deletes() -> None:
    d = Driver(params=LIVE)
    _live_countdown(d)
    d.send(ObsLost(), 1.0)
    assert d.state is State.ERROR and d.m.status()["media_active"] is True
    out = d.send_s(ObsBack(), 2.0)
    assert any(c.startswith("DiscardSession") for c in out)
    assert not any(c.startswith("StartSegment") for c in out)  # never resumed
    out = d.send_s(PostprocessDone(ok=True), 1.0)
    assert "WriteSessionJson(cancelled)" in out
    assert d.state is State.PENDING_START  # the call still runs: it gets a new countdown
    check_invariants(log_steps(d))


@pytest.mark.req("R-17")
def test_live_countdown_never_records_without_a_shown_countdown() -> None:
    d = Driver(params=LIVE, ack_countdown=False)
    _live_countdown(d)
    out = d.send_s(Tick(ctx()), PARAMS.countdown_seconds)
    assert "Notify(countdown_failed)" in out
    assert not any(isinstance(c, StartSegment) for c in d.commands())
    check_invariants(log_steps(d))


@pytest.mark.req("R-17")
def test_live_countdown_start_failure_ends_once_without_media() -> None:
    d = Driver(params=LIVE)
    _live_countdown(d)
    out = d.send_s(StartFailed(), 0.5)
    assert "Notify(start_failed)" in out and any(c.startswith("DiscardSession") for c in out)
    out = d.send_s(PostprocessDone(ok=True), 1.0)
    assert "WriteSessionJson(failed)" in out
    check_invariants(log_steps(d))
