"""Property-based tests (hypothesis) for the consent and correctness invariants (stage 08).

1. never ``RECORDING`` while disarmed;
2. at most one active segment (and one open session);
3. every started segment is stopped and its session finalised exactly once;
4. no ``StartSegment`` without a preceding countdown in the same armed period;
5. agent start ⇒ ``DISARMED`` (arming is never persisted);
6. a cancelled countdown never produces a ``StartSegment``;
7. ``classify`` is deterministic and independent of the order of ``mic_users``.
"""

from __future__ import annotations

from dataclasses import replace

import hypothesis
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from captation.models import AppRef, CallContext, ProbeSnapshot
from captation.rules import classify
from captation.rulesfile import load_rules
from captation.session import (
    Arm,
    AutoDisarmDue,
    BeginSession,
    Cancel,
    CountdownElapsed,
    Disarm,
    Disarmed,
    Event,
    LowDisk,
    ManualStop,
    MarkSegmentInterrupted,
    NotifyCountdown,
    ObsBack,
    ObsLost,
    PostprocessDone,
    RunPostprocess,
    SegmentStopped,
    SessionMachine,
    SessionParams,
    StartSegment,
    State,
    StopSegment,
    Tick,
    WriteSessionJson,
)
from tests.unit.session_support import (
    FINAL_STATUSES,
    Driver,
    FakeClock,
    FakeIds,
    check_invariants,
    ctx,
    detection,
    log_steps,
)

EXAMPLES = settings(
    max_examples=2000,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)

# ---- strategies -----------------------------------------------------------------------------

CONTEXTS = [
    None,
    ctx("signal", pid=300),
    ctx("signal", pid=300),
    ctx("signal", pid=300, mode="video"),
    ctx("whatsapp", pid=200),
    ctx("zoom", category="meeting", pid=4242, mode="video", reason="meeting_app"),
]

ticks = st.sampled_from(CONTEXTS).map(Tick)
arms = st.builds(
    Arm,
    st.sampled_from([None, "CR-001", "CR-002"]),
    st.sampled_from([(), (), ("exposure_policy_unmet",), ("disk_encryption_block",)]),
)
others = st.sampled_from(
    [
        Disarm(),
        AutoDisarmDue(),
        Cancel(),
        ManualStop(),
        ObsLost(),
        ObsBack(),
        LowDisk(),
        CountdownElapsed(),
        SegmentStopped(),
        SegmentStopped(ok=False),
        PostprocessDone(),
        PostprocessDone(ok=False),
    ]
)
dts = st.sampled_from([0.0, 0.5, 1.0, 2.0, 3.0, 5.0, 10.0, 30.0, 400.0])
Step = tuple[float, Event]


def _phase(context: CallContext | None, n: int, dt: float) -> list[Step]:
    """A stretch of polls with the same verdict, like a call (or silence) in real life."""
    return [(dt, Tick(context))] * n


phases = st.builds(
    _phase, st.sampled_from(CONTEXTS), st.integers(1, 12), st.sampled_from([0.5, 1.0, 2.0])
)
singles = st.tuples(dts, st.one_of(arms, others, ticks)).map(lambda step: [step])
# OBS goes away and (usually) comes back a little later.
obs_blips = st.builds(
    lambda gap, back: [(0.5, ObsLost()), (gap, ObsBack())] if back else [(0.5, ObsLost())],
    st.sampled_from([0.5, 2.0, 15.0]),
    st.booleans() | st.just(True),
)
# The executor confirms a stop and finishes post-processing (with random outcomes).
lifecycle = st.builds(
    lambda stop_ok, pp_ok: [(1.0, SegmentStopped(stop_ok)), (2.0, PostprocessDone(pp_ok))],
    st.booleans() | st.just(True),
    st.booleans() | st.just(True),
)
# A call long enough to get through debounce and countdown, often turning the camera on.
calls = st.builds(
    lambda context, n, camera_after: (
        _phase(context, n, 1.0)
        + _phase(None if context is None else replace(context, mode="video"), camera_after, 1.0)
    ),
    st.sampled_from(CONTEXTS[1:]),
    st.integers(6, 14),
    st.integers(0, 4),
)
blocks = st.lists(
    st.one_of(phases, calls, singles, obs_blips, lifecycle),
    max_size=30,
)


def _timeline(armed_first: bool, parts: list[list[Step]]) -> list[Step]:
    flat = [step for part in parts for step in part]
    return [(0.0, Arm("CR-001")), *flat] if armed_first else flat


steps = st.builds(_timeline, st.booleans(), blocks)
params = st.builds(
    SessionParams,
    start_debounce_s=st.sampled_from([0.0, 1.0, 3.0]),
    stop_debounce_s=st.sampled_from([2.0, 10.0]),
    countdown_seconds=st.sampled_from([3, 5]),
    auto_disarm_after_minutes=st.sampled_from([5, 240]),
    segments_on_mode_change=st.booleans(),
)


def run(p: SessionParams, seq: list[Step]) -> Driver:
    d = Driver(params=p)
    for dt, event in seq:
        d.send(event, dt)
    _label(d)
    return d


def _label(d: Driver) -> None:
    """Hypothesis statistics: how often the deep paths were exercised."""
    cmds = d.commands()
    triggers = {c.trigger for c in cmds if isinstance(c, StartSegment)}
    statuses = {c.status for c in cmds if isinstance(c, WriteSessionJson)}
    sessions = sum(isinstance(c, BeginSession) for c in cmds)
    hypothesis.event(f"recorded: {any(t.startswith('rule:') for t in triggers)}")
    for label in ("upgrade:camera_on", "recovery:obs_restart"):
        hypothesis.event(f"{label}: {label in triggers}")
    for status in ("cancelled", "interrupted", "failed"):
        hypothesis.event(f"status {status}: {status in statuses}")
    hypothesis.event(f"sessions >= 2: {sessions >= 2}")
    hypothesis.event(
        f"auto-disarmed: {any(isinstance(c, Disarmed) and c.reason == 'auto' for c in cmds)}"
    )


# ---- 1. never RECORDING while disarmed --------------------------------------------------------


@pytest.mark.req("R-14", "R-37")
@EXAMPLES
@given(params, steps)
def test_never_recording_while_disarmed(p: SessionParams, seq: list[Step]) -> None:
    d = run(p, seq)
    for _, _, cmds, state, armed in d.log:
        if state in (State.RECORDING, State.PENDING_STOP, State.COUNTDOWN, State.PENDING_START):
            assert armed, f"{state.name} while disarmed"
        if any(isinstance(c, StartSegment) for c in cmds):
            assert armed and state is State.RECORDING


# ---- 2. at most one active segment ------------------------------------------------------------


@pytest.mark.req("R-37")
@EXAMPLES
@given(params, steps)
def test_at_most_one_active_segment(p: SessionParams, seq: list[Step]) -> None:
    d = run(p, seq)
    active: set[tuple[str, int]] = set()
    open_sessions: set[str] = set()
    for c in d.commands():
        if isinstance(c, BeginSession):
            open_sessions.add(c.session_id)
            assert len(open_sessions) <= 1
        elif isinstance(c, StartSegment):
            active.add((c.session_id, c.index))
            assert len(active) <= 1, f"concurrent segments {active}"
        elif isinstance(c, StopSegment | MarkSegmentInterrupted):
            active.discard((c.session_id, c.index))
        elif isinstance(c, WriteSessionJson) and c.status in FINAL_STATUSES:
            open_sessions.discard(c.session_id)


# ---- 3. every started segment is stopped and finalised ----------------------------------------


@pytest.mark.req("R-16", "R-37")
@EXAMPLES
@given(params, steps)
def test_every_segment_is_stopped_and_finalised(p: SessionParams, seq: list[Step]) -> None:
    d = run(p, seq)
    d.drain()  # disarm, confirm the stop, finish post-processing
    assert d.state is State.DISARMED
    assert d.m.status()["session_id"] is None
    cmds = d.commands()
    for i, c in enumerate(cmds):
        if isinstance(c, StartSegment):
            assert any(
                isinstance(x, StopSegment | MarkSegmentInterrupted)
                and (x.session_id, x.index) == (c.session_id, c.index)
                for x in cmds[i + 1 :]
            ), f"segment {c.index} of {c.session_id} never stopped"
    for sid in {c.session_id for c in cmds if isinstance(c, BeginSession)}:
        finals = [
            i
            for i, c in enumerate(cmds)
            if isinstance(c, WriteSessionJson)
            and c.session_id == sid
            and c.status in FINAL_STATUSES
        ]
        assert len(finals) == 1, f"{sid}: {len(finals)} final session.json writes"
        last = finals[0]
        assert all(getattr(c, "session_id", None) != sid for c in cmds[last + 1 :])
        final = cmds[last]
        assert isinstance(final, WriteSessionJson)
        recorded = any(isinstance(c, StartSegment) and c.session_id == sid for c in cmds)
        postprocessed = any(isinstance(c, RunPostprocess) and c.session_id == sid for c in cmds)
        if recorded:
            assert final.status in ("complete", "interrupted", "failed") and postprocessed
        else:
            assert final.status == "cancelled" and not postprocessed
    check_invariants(log_steps(d))


# ---- 4. no StartSegment without a countdown in the same armed period ---------------------------


@pytest.mark.req("R-17", "R-37")
@EXAMPLES
@given(params, steps)
def test_no_start_without_countdown(p: SessionParams, seq: list[Step]) -> None:
    d = run(p, seq)
    counted_down: set[str] = set()
    for c in d.commands():
        if isinstance(c, Disarmed):
            counted_down.clear()  # a new armed period needs a new countdown
        elif isinstance(c, NotifyCountdown):
            counted_down.add(c.session_id)
        elif isinstance(c, StartSegment):
            assert c.session_id in counted_down


# ---- 5. agent start ⇒ DISARMED ---------------------------------------------------------------


@pytest.mark.req("R-15")
@EXAMPLES
@given(params, steps, st.lists(st.tuples(dts, st.one_of(ticks, others)), max_size=30))
def test_agent_start_is_disarmed(p: SessionParams, history: list[Step], after: list[Step]) -> None:
    before = run(p, history)  # whatever happened before the restart
    clock, ids = FakeClock(before.clock.t), FakeIds()
    restarted = SessionMachine(p, clock, ids)
    assert restarted.state is State.DISARMED and not restarted.armed
    for dt, event in after:  # no Arm: nothing may ever happen
        clock.t += dt
        assert restarted.handle(event) == []
        assert restarted.state is State.DISARMED


# ---- 6. a cancelled countdown never produces a StartSegment ------------------------------------


@pytest.mark.req("R-17", "R-37")
@EXAMPLES
@given(params, steps)
def test_cancelled_countdown_never_starts(p: SessionParams, seq: list[Step]) -> None:
    d = run(p, seq)
    cmds = d.commands()
    cancelled = {
        c.session_id for c in cmds if isinstance(c, WriteSessionJson) and c.status == "cancelled"
    }
    started = {c.session_id for c in cmds if isinstance(c, StartSegment)}
    assert not cancelled & started


# ---- 7. classify is deterministic -------------------------------------------------------------

RULES = load_rules()
POOL = [
    AppRef(pid=4242, process_name="zoom"),
    AppRef(pid=4300, process_name="Microsoft Teams", bundle_id="com.microsoft.teams2"),
    AppRef(pid=300, process_name="signal-desktop"),
    AppRef(pid=305, process_name="Signal.exe", exe_path="C:\\Signal\\Signal.exe"),
    AppRef(pid=200, process_name="WhatsApp"),
    AppRef(pid=250, process_name="telegram-desktop"),
    AppRef(pid=90, process_name="avconferenced"),
    AppRef(pid=500, process_name="chrome"),
    AppRef(pid=510, process_name="Google Chrome Helper (Renderer)", parent_app="Google Chrome"),
    AppRef(pid=600, process_name="firefox"),
    AppRef(pid=999, process_name="obs"),
    AppRef(pid=998, process_name="obs-browser-page"),
    AppRef(pid=997, process_name="captation-probe"),
    AppRef(pid=777, process_name="foo-voip"),
    AppRef(pid=778, process_name="bar-voip"),
    AppRef(pid=300, process_name="signal-desktop", exe_path="/opt/Signal/signal-desktop"),
]
TITLES = ["Google Meet", "WhatsApp", "Zoom Meeting", "Inbox", "Jitsi Meet", "Microsoft Teams"]
IGNORED_PIDS = {999, 998, 997}


@pytest.mark.req("R-05", "R-35")
@EXAMPLES
@given(
    mic=st.lists(st.sampled_from(POOL), max_size=8),
    camera=st.booleans(),
    camera_users=st.lists(st.sampled_from(POOL), max_size=3),
    titles=st.none()
    | st.dictionaries(
        st.sampled_from([500, 510, 600, 300, 4242]),
        st.lists(st.sampled_from(TITLES), max_size=3).map(tuple),
        max_size=3,
    ),
    current=st.sampled_from(
        [None, "zoom", "signal", "whatsapp", "browser", "google_meet", "unknown", "telegram"]
    ),
    unknown=st.booleans(),
    store_titles=st.booleans(),
    exclude=st.frozensets(st.sampled_from([300, 200, 999, 4242, 500]), max_size=2),
    data=st.data(),
)
def test_classify_is_deterministic(
    mic: list[AppRef],
    camera: bool,
    camera_users: list[AppRef],
    titles: dict[int, tuple[str, ...]] | None,
    current: str | None,
    unknown: bool,
    store_titles: bool,
    exclude: frozenset[int],
    data: st.DataObject,
) -> None:
    cfg = detection(record_unknown_apps=unknown, store_window_titles=store_titles)
    snap = ProbeSnapshot(1.0, tuple(mic), camera, tuple(camera_users), titles)
    first = classify(snap, RULES, cfg, current, exclude_pids=exclude)
    assert classify(snap, RULES, cfg, current, exclude_pids=exclude) == first

    shuffled_titles = None
    if titles is not None:
        items = list(reversed(titles.items()))
        shuffled_titles = {pid: tuple(data.draw(st.permutations(t))) for pid, t in items}
    shuffled = ProbeSnapshot(
        1.0,
        tuple(data.draw(st.permutations(mic))),
        camera,
        tuple(data.draw(st.permutations(camera_users))),
        shuffled_titles,
    )
    assert classify(shuffled, RULES, cfg, current, exclude_pids=exclude) == first

    if first is not None:
        assert first.capture_target in mic
        assert first.pid not in exclude and first.pid not in IGNORED_PIDS
        assert (first.window_title is None) or store_titles
