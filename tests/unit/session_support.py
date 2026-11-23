"""Shared fakes and helpers for the classifier and session state machine tests."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from captation.config import DetectionConfig
from captation.models import AppRef, CallContext, Category, Mode
from captation.session import (
    Arm,
    ArmRefused,
    BeginSession,
    Command,
    CountdownShown,
    Disarm,
    Disarmed,
    Event,
    MarkSegmentInterrupted,
    Notify,
    NotifyCountdown,
    ObsLost,
    PostprocessDone,
    SegmentStopped,
    SessionMachine,
    SessionParams,
    SetTrayState,
    StartSegment,
    State,
    StopSegment,
    Tick,
    WriteSessionJson,
)

BASE_WALL = datetime(2026, 11, 23, 9, 30, 0, tzinfo=UTC)
FINAL_STATUSES = frozenset({"complete", "interrupted", "cancelled", "failed"})

PARAMS = SessionParams(
    start_debounce_s=3.0,
    stop_debounce_s=10.0,
    countdown_seconds=5,
    auto_disarm_after_minutes=240,
    segments_on_mode_change=True,
)


class FakeClock:
    """Monotonic seconds ``t``; wall time is ``BASE_WALL + t``."""

    def __init__(self, t: float = 0.0) -> None:
        self.t = t

    def now(self) -> float:
        return self.t

    def wall(self) -> datetime:
        return BASE_WALL + timedelta(seconds=self.t)


class FakeIds:
    def __init__(self) -> None:
        self.issued = 0

    def new_session_id(self, wall: datetime) -> str:
        self.issued += 1
        return f"S-{wall:%Y%m%d-%H%M%S}-{self.issued:04x}"


def detection(
    *,
    start_debounce_s: float = 3.0,
    stop_debounce_s: float = 10.0,
    record_unknown_apps: bool = False,
    store_window_titles: bool = False,
) -> DetectionConfig:
    return DetectionConfig(
        poll_interval_ms=1000,
        start_debounce_s=start_debounce_s,
        stop_debounce_s=stop_debounce_s,
        rules_file="builtin:default",
        record_unknown_apps=record_unknown_apps,
        store_window_titles=store_window_titles,
    )


def ref(pid: int, name: str, **kw: str | None) -> AppRef:
    return AppRef(pid=pid, process_name=name, **kw)  # type: ignore[arg-type]


def ctx(
    app_id: str = "signal",
    *,
    category: Category = "messenger",
    pid: int = 300,
    mode: Mode = "audio",
    reason: str | None = None,
) -> CallContext:
    if reason is None:
        reason = "default" if mode == "audio" else "camera_active"
    return CallContext(
        app_id=app_id,
        category=category,
        pid=pid,
        mode=mode,
        reason=reason,
        capture_target=ref(pid, app_id),
    )


def summarize(cmd: Command) -> str:
    """Compact, readable form used by the transition table and the scenario fixtures."""
    match cmd:
        case SetTrayState(state=state, mode=None, app_id=None):
            return f"SetTrayState({state})"
        case SetTrayState(state=state, mode=mode, app_id=app_id):
            return f"SetTrayState({state},{mode},{app_id})"
        case ArmRefused(reasons=reasons):
            return f"ArmRefused({','.join(reasons)})"
        case Disarmed(reason=reason):
            return f"Disarmed({reason})"
        case BeginSession(context=context):
            return f"BeginSession({context.app_id})"
        case NotifyCountdown(app_id=app_id, seconds=seconds):
            return f"NotifyCountdown({app_id},{seconds})"
        case StartSegment(index=index, mode=mode, trigger=trigger):
            return f"StartSegment({index},{mode},{trigger})"
        case StopSegment(index=index):
            return f"StopSegment({index})"
        case MarkSegmentInterrupted(index=index):
            return f"MarkSegmentInterrupted({index})"
        case WriteSessionJson(status=status):
            return f"WriteSessionJson({status})"
        case Notify(kind=kind):
            return f"Notify({kind})"
        case _:
            return type(cmd).__name__


@dataclass
class Driver:
    """A machine plus fake clock and a log of every (time, event, commands, state) step."""

    params: SessionParams = PARAMS
    clock: FakeClock = field(default_factory=FakeClock)
    ids: FakeIds = field(default_factory=FakeIds)
    log: list[tuple[float, Event, list[Command], State, bool]] = field(default_factory=list)
    # Like the real executor: a countdown that was shown is confirmed right away. Tests of the
    # fail-closed path set this to False.
    ack_countdown: bool = True

    def __post_init__(self) -> None:
        self.m = SessionMachine(self.params, self.clock, self.ids)

    @property
    def state(self) -> State:
        return self.m.state

    def send(self, event: Event, dt: float = 0.0) -> list[Command]:
        self.clock.t += dt
        cmds = self.m.handle(event)
        self.log.append((self.clock.t, event, cmds, self.m.state, self.m.armed))
        if self.ack_countdown:
            for cmd in cmds:
                if isinstance(cmd, NotifyCountdown):
                    shown = CountdownShown(cmd.session_id)
                    extra = self.m.handle(shown)
                    self.log.append((self.clock.t, shown, extra, self.m.state, self.m.armed))
                    cmds = [*cmds, *extra]
        return cmds

    def send_s(self, event: Event, dt: float = 0.0) -> list[str]:
        return [summarize(c) for c in self.send(event, dt)]

    def commands(self) -> list[Command]:
        return [c for _, _, cmds, _, _ in self.log for c in cmds]

    def drain(self) -> None:
        """Disarm and let any running stop/post-processing finish (property 3)."""
        self.send(Disarm(), 1.0)
        self.send(SegmentStopped(), 1.0)
        self.send(PostprocessDone(), 1.0)


# ---- reaching every state of the machine ----------------------------------------------------


def goto(state: State, *, app: CallContext | None = None) -> Driver:
    """A driver whose machine is in ``state`` (built from the real transitions only)."""
    c = app or ctx()
    d = Driver()
    if state is State.DISARMED:
        return d
    d.send(Arm("CR-001"))
    if state is State.ARMED_IDLE:
        return d
    d.send(Tick(c), 1.0)
    if state is State.PENDING_START:
        return d
    d.send(Tick(c), PARAMS.start_debounce_s)
    if state is State.COUNTDOWN:
        return d
    d.send(Tick(c), PARAMS.countdown_seconds)
    if state is State.RECORDING:
        return d
    if state is State.ERROR:
        d.send(ObsLost(), 1.0)
        return d
    d.send(Tick(None), 1.0)
    if state is State.PENDING_STOP:
        return d
    d.send(Tick(None), PARAMS.stop_debounce_s)
    if state is State.STOPPING:
        return d
    d.send(SegmentStopped(), 1.0)
    assert state is State.POSTPROCESSING
    return d


# ---- invariant checks over a command log (shared by scenarios and properties) ----------------


def check_invariants(steps: Iterable[tuple[list[Command], State, bool]]) -> None:
    """Principles §2 / §5.3 invariants over (commands, state after, armed after) steps."""
    countdowns: set[str] = set()  # sessions with a countdown in the current armed period
    cancelled: set[str] = set()
    started: set[str] = set()
    open_session: str | None = None
    active: set[tuple[str, int]] = set()
    for cmds, state, armed in steps:
        for c in cmds:
            match c:
                case Disarmed():
                    countdowns.clear()
                case BeginSession(session_id=sid):
                    assert open_session is None, "two sessions at once"
                    open_session = sid
                case NotifyCountdown(session_id=sid):
                    assert sid == open_session
                    countdowns.add(sid)
                case StartSegment(session_id=sid, index=index):
                    assert sid in countdowns, "StartSegment without a countdown in this period"
                    assert sid not in cancelled, "StartSegment of a cancelled session"
                    assert sid == open_session
                    active.add((sid, index))
                    assert len(active) <= 1, f"concurrent segments: {active}"
                    started.add(sid)
                case (
                    StopSegment(session_id=sid, index=index)
                    | MarkSegmentInterrupted(session_id=sid, index=index)
                ):
                    active.discard((sid, index))
                case WriteSessionJson(session_id=sid, status=status) if status in FINAL_STATUSES:
                    assert sid == open_session, "final write for a session that is not open"
                    assert not active, "session finalised with an active segment"
                    if status == "cancelled":
                        assert sid not in started, "cancelled session has media"
                        cancelled.add(sid)
                    else:
                        assert sid in started
                    open_session = None
        if state in (State.RECORDING, State.PENDING_STOP, State.COUNTDOWN, State.PENDING_START):
            assert armed, f"{state.name} while disarmed"
        if state is State.RECORDING:
            assert len(active) == 1


def log_steps(d: Driver) -> list[tuple[list[Command], State, bool]]:
    return [(cmds, state, armed) for _, _, cmds, state, armed in d.log]
