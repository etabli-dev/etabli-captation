"""Session state machine, pure and deterministic (architecture §6, stage 08).

The machine consumes events and returns commands. It performs no I/O: time comes from an
injected ``Clock``, session IDs from an injected ``IdFactory``, and every side effect (OBS,
UI, storage) is a command that the agent's executor carries out.

Timing: the agent sends a ``Tick`` with the classifier's verdict on every poll. On *every*
event the machine first checks the auto-disarm expiry, then handles the event, then advances
the time-based transitions (start debounce, countdown, stop debounce) from ``Clock.now()``.
The explicit ``AutoDisarmDue`` and ``CountdownElapsed`` events are accepted as well.

Consent guard (docs/principles.md §2, §5.3), enforced here and proven by property tests:

- the machine starts ``DISARMED`` and never persists arming;
- no ``StartSegment`` without a preceding countdown of the same session in the same armed
  period; a cancelled countdown never yields a ``StartSegment``;
- never ``RECORDING`` while disarmed, at most one active segment, every started segment is
  stopped (or marked interrupted) and its session finalised exactly once.

Decisions beyond the architecture table (§6):

- After a user ``Cancel``, a ``ManualStop`` or ``LowDisk``, the call's ``app_id`` is
  suppressed: it does not trigger a new countdown until its context has been absent for
  ``stop_debounce_s`` or a different app becomes the context. Arming again clears it.
- A countdown is aborted (``status: cancelled``) when the context is absent for
  ``stop_debounce_s``, absent when the countdown elapses, replaced by a different app, or
  when OBS is lost. While OBS is lost, no new countdown starts.
- ``Arm`` while armed refreshes the consent reference (for later sessions) and the expiry.
- ``SetTrayState`` is emitted only when the tray state changes (the agent shows ``disarmed``
  at start). The ``recording`` state goes up before ``StartSegment`` and stays until OBS has
  confirmed the stop (``SegmentStopped``), also during ``ERROR``.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol, assert_never

from captation.models import AppRef, CallContext, Mode

if TYPE_CHECKING:
    from datetime import datetime

    from captation.config import Config

TrayState = Literal["disarmed", "armed", "recording"]
SessionStatus = Literal["recording", "complete", "interrupted", "cancelled", "failed"]
DisarmReason = Literal["user", "auto"]

MAX_SEGMENTS = 99  # session.schema.json: segment index 1..99


class State(enum.Enum):
    DISARMED = "disarmed"
    ARMED_IDLE = "armed_idle"
    PENDING_START = "pending_start"
    COUNTDOWN = "countdown"
    RECORDING = "recording"
    PENDING_STOP = "pending_stop"
    STOPPING = "stopping"
    POSTPROCESSING = "postprocessing"
    ERROR = "error"


class Clock(Protocol):
    def now(self) -> float:
        """Monotonic seconds."""
        ...

    def wall(self) -> datetime:
        """Timezone-aware local wall-clock time."""
        ...


class IdFactory(Protocol):
    def new_session_id(self, wall: datetime) -> str:
        """``S-YYYYMMDD-HHMMSS-xxxx`` for the given local time."""
        ...


@dataclass(frozen=True)
class SessionParams:
    start_debounce_s: float
    stop_debounce_s: float
    countdown_seconds: int
    auto_disarm_after_minutes: int
    segments_on_mode_change: bool

    @classmethod
    def from_config(cls, cfg: Config) -> SessionParams:
        return cls(
            start_debounce_s=cfg.detection.start_debounce_s,
            stop_debounce_s=cfg.detection.stop_debounce_s,
            countdown_seconds=cfg.consent.countdown_seconds,
            auto_disarm_after_minutes=cfg.consent.auto_disarm_after_minutes,
            segments_on_mode_change=cfg.recording.segments_on_mode_change,
        )


# ---- events -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Tick:
    """One poll: the classifier's verdict (``None`` = no call)."""

    context: CallContext | None


@dataclass(frozen=True)
class Arm:
    """Arm request. Non-empty ``blockers`` (computed by the executor) refuse it."""

    consent_ref: str | None
    blockers: tuple[str, ...] = ()


@dataclass(frozen=True)
class Disarm:
    pass


@dataclass(frozen=True)
class AutoDisarmDue:
    pass


@dataclass(frozen=True)
class Cancel:
    """The user cancelled the countdown (notification action or tray menu)."""


@dataclass(frozen=True)
class ManualStop:
    """The user stopped the recording (tray menu, CLI ``stop``). Arming stays."""


@dataclass(frozen=True)
class ObsLost:
    pass


@dataclass(frozen=True)
class ObsBack:
    pass


@dataclass(frozen=True)
class LowDisk:
    """Free space is below ``storage.min_free_gb``."""


@dataclass(frozen=True)
class CountdownElapsed:
    pass


@dataclass(frozen=True)
class SegmentStopped:
    """OBS confirmed the end of the segment requested by ``StopSegment``."""

    ok: bool = True


@dataclass(frozen=True)
class PostprocessDone:
    ok: bool = True


Event = (
    Tick
    | Arm
    | Disarm
    | AutoDisarmDue
    | Cancel
    | ManualStop
    | ObsLost
    | ObsBack
    | LowDisk
    | CountdownElapsed
    | SegmentStopped
    | PostprocessDone
)


# ---- commands ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class SetTrayState:
    state: TrayState
    mode: Mode | None = None
    app_id: str | None = None


@dataclass(frozen=True)
class ArmRefused:
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class Armed:
    """Arming accepted. ``expires_at`` is on the ``Clock.now()`` (monotonic) scale."""

    consent_ref: str | None
    expires_at: float


@dataclass(frozen=True)
class Disarmed:
    reason: DisarmReason


@dataclass(frozen=True)
class PrewarmObs:
    pass


@dataclass(frozen=True)
class NotifyCountdown:
    session_id: str
    app_id: str
    seconds: int


@dataclass(frozen=True)
class CloseCountdown:
    session_id: str


@dataclass(frozen=True)
class BeginSession:
    session_id: str
    context: CallContext
    consent_ref: str | None
    started_at: datetime


@dataclass(frozen=True)
class StartSegment:
    session_id: str
    index: int
    mode: Mode
    target: AppRef
    trigger: str


@dataclass(frozen=True)
class StopSegment:
    session_id: str
    index: int


@dataclass(frozen=True)
class MarkSegmentInterrupted:
    """Record ``interrupted: true`` (and the segment end) for the segment in session.json."""

    session_id: str
    index: int


@dataclass(frozen=True)
class Teardown:
    """Remove or deactivate the Captation inputs (OBS must not hold the mic while idle)."""


@dataclass(frozen=True)
class RunPostprocess:
    session_id: str


@dataclass(frozen=True)
class WriteSessionJson:
    session_id: str
    status: SessionStatus


@dataclass(frozen=True)
class ShowReminder:
    session_id: str


@dataclass(frozen=True)
class Notify:
    """One-off notification. Kinds: auto_disarmed, low_disk, obs_lost, start_cancelled."""

    kind: str


Command = (
    SetTrayState
    | ArmRefused
    | Armed
    | Disarmed
    | PrewarmObs
    | NotifyCountdown
    | CloseCountdown
    | BeginSession
    | StartSegment
    | StopSegment
    | MarkSegmentInterrupted
    | Teardown
    | RunPostprocess
    | WriteSessionJson
    | ShowReminder
    | Notify
)

_LIVE_STATES = frozenset({State.RECORDING, State.PENDING_STOP, State.STOPPING, State.ERROR})


@dataclass
class _Session:
    session_id: str
    app_id: str
    segment: int = 0  # index of the latest started segment; 0 = none yet
    segment_mode: Mode | None = None
    interrupted: bool = False
    recording_since: float | None = None


# ---- the machine ------------------------------------------------------------------------------


class SessionMachine:
    """The session state machine (architecture §6). Always created ``DISARMED``."""

    def __init__(self, params: SessionParams, clock: Clock, ids: IdFactory) -> None:
        self._p = params
        self._clock = clock
        self._ids = ids
        self._state = State.DISARMED
        self._armed = False
        self._consent_ref: str | None = None
        self._expires_at: float | None = None
        self._ctx: CallContext | None = None
        self._pending_app: str | None = None
        self._pending_since = 0.0
        self._countdown_deadline = 0.0
        self._countdown_elapsed = False
        self._lost_since: float | None = None
        self._session: _Session | None = None
        self._suppressed: str | None = None
        self._suppressed_since: float | None = None
        self._obs_up = True
        self._tray: tuple[TrayState, Mode | None, str | None] = ("disarmed", None, None)

    # ---- public API ---------------------------------------------------------------------------

    @property
    def state(self) -> State:
        return self._state

    @property
    def armed(self) -> bool:
        return self._armed

    @property
    def current_app_id(self) -> str | None:
        """App of the open session (countdown to post-processing): ``classify``'s tie-break (a)."""
        return self._session.app_id if self._session is not None else None

    def handle(self, event: Event) -> list[Command]:
        """Process one event and return the commands to execute, in order."""
        out: list[Command] = []
        now = self._clock.now()
        if isinstance(event, Tick):
            self._ctx = event.context
        self._update_suppression(now)
        if self._armed and self._expires_at is not None and now >= self._expires_at:
            self._disarm("auto", out)
        self._dispatch(event, now, out)
        self._advance(now, out)
        self._sync_tray(out)
        return out

    def status(self) -> dict[str, object]:
        """Snapshot for ``captation status`` (no personal data)."""
        now = self._clock.now()
        s = self._session
        auto_disarm_in: float | None = None
        if self._armed and self._expires_at is not None:
            auto_disarm_in = max(0.0, self._expires_at - now)
        app_id = self.current_app_id
        if app_id is None and self._state is State.PENDING_START:
            app_id = self._pending_app
        elapsed: float | None = None
        if s is not None and s.recording_since is not None:
            elapsed = now - s.recording_since
        return {
            "state": self._state.name,
            "armed": self._armed,
            "consent_ref": self._consent_ref,
            "auto_disarm_in_s": auto_disarm_in,
            "session_id": s.session_id if s is not None else None,
            "app_id": app_id,
            "mode": s.segment_mode if s is not None else None,
            "segment": s.segment if s is not None and s.segment else None,
            "recording_elapsed_s": elapsed,
        }

    # ---- event dispatch -----------------------------------------------------------------------

    def _dispatch(self, event: Event, now: float, out: list[Command]) -> None:
        match event:
            case Arm():
                self._on_arm(event, now, out)
            case Disarm():
                if self._armed:
                    self._disarm("user", out)
            case AutoDisarmDue():
                if self._armed:
                    self._disarm("auto", out)
            case Cancel():
                if self._state is State.COUNTDOWN:
                    self._cancel_countdown_by_user(out)
            case ManualStop():
                self._on_manual_stop(out)
            case ObsLost():
                self._on_obs_lost(out)
            case ObsBack():
                self._on_obs_back(out)
            case LowDisk():
                self._on_low_disk(out)
            case CountdownElapsed():
                if self._state is State.COUNTDOWN:
                    self._countdown_elapsed = True
            case SegmentStopped():
                if self._state is State.STOPPING:
                    self._on_segment_stopped(event.ok, out)
            case PostprocessDone():
                if self._state is State.POSTPROCESSING:
                    self._on_postprocess_done(event.ok, out)
            case Tick():
                pass  # the context was stored in handle(); _advance() acts on it
            case _:
                assert_never(event)

    def _on_arm(self, event: Arm, now: float, out: list[Command]) -> None:
        if event.blockers:
            out.append(ArmRefused(tuple(event.blockers)))
            return
        was_armed = self._armed
        self._armed = True
        self._consent_ref = event.consent_ref
        self._expires_at = now + self._p.auto_disarm_after_minutes * 60.0
        self._suppressed = None
        out.append(Armed(event.consent_ref, self._expires_at))
        if self._state is State.DISARMED:
            self._state = State.ARMED_IDLE
        self._sync_tray(out)
        if not was_armed:
            out.append(PrewarmObs())

    def _disarm(self, reason: DisarmReason, out: list[Command]) -> None:
        self._armed = False
        self._consent_ref = None
        self._expires_at = None
        st = self._state
        if st in (State.ARMED_IDLE, State.PENDING_START):
            self._state = State.DISARMED
        elif st is State.COUNTDOWN:
            self._abort_countdown(out)
            self._state = State.DISARMED
        elif st in (State.RECORDING, State.PENDING_STOP):
            self._begin_stop(out)
        elif st is State.ERROR:
            self._begin_postprocess(out)
        # STOPPING / POSTPROCESSING: the running finalisation ends in DISARMED.
        out.append(Disarmed(reason))
        if reason == "auto":
            out.append(Notify("auto_disarmed"))

    def _on_manual_stop(self, out: list[Command]) -> None:
        st = self._state
        if st is State.COUNTDOWN:
            self._cancel_countdown_by_user(out)
        elif st in (State.RECORDING, State.PENDING_STOP):
            self._suppress(self._live().app_id)
            self._begin_stop(out)
        elif st is State.ERROR:
            self._suppress(self._live().app_id)
            self._begin_postprocess(out)

    def _on_obs_lost(self, out: list[Command]) -> None:
        self._obs_up = False
        st = self._state
        if st in (State.RECORDING, State.PENDING_STOP):
            self._mark_interrupted(out)
            out.append(Notify("obs_lost"))
            self._state = State.ERROR
            self._lost_since = None
        elif st is State.STOPPING:
            self._mark_interrupted(out)
            out.append(Notify("obs_lost"))
            self._begin_postprocess(out)
        elif st is State.COUNTDOWN:
            self._abort_countdown(out)
            out.append(Notify("obs_lost"))
            self._state = State.ARMED_IDLE

    def _on_obs_back(self, out: list[Command]) -> None:
        self._obs_up = True
        if self._state is not State.ERROR:
            return
        s = self._live()
        ctx = self._ctx
        if ctx is not None and ctx.app_id == s.app_id and s.segment < MAX_SEGMENTS:
            self._state = State.RECORDING
            mode = self._wanted_mode(s, ctx)
            self._start_segment(mode, ctx.capture_target, "recovery:obs_restart", out)
        else:
            out.append(Teardown())
            self._begin_postprocess(out)

    def _on_low_disk(self, out: list[Command]) -> None:
        st = self._state
        if st is State.PENDING_START:
            self._suppress(self._pending_app)
            self._state = State.ARMED_IDLE
        elif st is State.COUNTDOWN:
            self._suppress(self._live().app_id)
            self._abort_countdown(out)
            self._state = State.ARMED_IDLE
        elif st in (State.RECORDING, State.PENDING_STOP):
            self._suppress(self._live().app_id)
            self._begin_stop(out)
        elif st is State.ERROR:
            self._suppress(self._live().app_id)
            self._begin_postprocess(out)
        elif st is not State.ARMED_IDLE:
            return  # DISARMED, STOPPING, POSTPROCESSING: nothing to refuse or stop
        out.append(Notify("low_disk"))

    def _on_segment_stopped(self, ok: bool, out: list[Command]) -> None:
        if not ok:
            self._mark_interrupted(out)
        self._begin_postprocess(out)

    def _on_postprocess_done(self, ok: bool, out: list[Command]) -> None:
        s = self._live()
        status: SessionStatus
        if not ok:
            status = "failed"
        elif s.interrupted:
            status = "interrupted"
        else:
            status = "complete"
        out.append(WriteSessionJson(s.session_id, status))
        self._session = None
        self._state = State.ARMED_IDLE if self._armed else State.DISARMED

    # ---- time- and context-driven transitions -------------------------------------------------

    def _advance(self, now: float, out: list[Command]) -> None:
        while True:
            before = self._state
            self._step(now, out)
            if self._state is before:
                return

    def _step(self, now: float, out: list[Command]) -> None:
        st = self._state
        ctx = self._ctx
        if st is State.ARMED_IDLE:
            if ctx is not None and ctx.app_id != self._suppressed:
                self._state = State.PENDING_START
                self._pending_app = ctx.app_id
                self._pending_since = now
        elif st is State.PENDING_START:
            if ctx is None or ctx.app_id == self._suppressed:
                self._state = State.ARMED_IDLE
            elif ctx.app_id != self._pending_app:
                self._pending_app = ctx.app_id
                self._pending_since = now
            elif self._obs_up and now - self._pending_since >= self._p.start_debounce_s:
                self._begin_countdown(ctx, now, out)
        elif st is State.COUNTDOWN:
            self._step_countdown(ctx, now, out)
        elif st is State.RECORDING:
            s = self._live()
            if ctx is None:
                self._state = State.PENDING_STOP
                self._lost_since = now
            elif ctx.app_id != s.app_id:
                self._begin_stop(out)
            else:
                self._maybe_upgrade(s, ctx, out)
        elif st is State.PENDING_STOP:
            s = self._live()
            if ctx is None:
                lost_since = now if self._lost_since is None else self._lost_since
                if now - lost_since >= self._p.stop_debounce_s:
                    self._begin_stop(out)
            elif ctx.app_id != s.app_id:
                self._begin_stop(out)
            else:
                self._state = State.RECORDING
                self._lost_since = None

    def _step_countdown(self, ctx: CallContext | None, now: float, out: list[Command]) -> None:
        s = self._live()
        if ctx is not None and ctx.app_id != s.app_id:
            # A different app became the context: this countdown named the wrong app.
            self._abort_countdown(out)
            self._state = State.ARMED_IDLE
            return
        if ctx is None:
            if self._lost_since is None:
                self._lost_since = now
            lost_too_long = now - self._lost_since >= self._p.stop_debounce_s
        else:
            self._lost_since = None
            lost_too_long = False
        elapsed = self._countdown_elapsed or now >= self._countdown_deadline
        if ctx is None:
            if lost_too_long or elapsed:
                self._abort_countdown(out)
                self._state = State.ARMED_IDLE
        elif elapsed:
            self._start_recording(ctx, now, out)

    def _update_suppression(self, now: float) -> None:
        if self._suppressed is None:
            return
        ctx = self._ctx
        if ctx is not None:
            if ctx.app_id != self._suppressed:
                self._suppressed = None
            else:
                self._suppressed_since = None
            return
        if self._suppressed_since is None:
            self._suppressed_since = now
        if now - self._suppressed_since >= self._p.stop_debounce_s:
            self._suppressed = None

    # ---- building blocks ----------------------------------------------------------------------

    def _live(self) -> _Session:
        s = self._session
        assert s is not None, f"no session in state {self._state.name}"
        return s

    def _suppress(self, app_id: str | None) -> None:
        self._suppressed = app_id
        self._suppressed_since = None

    def _wanted_mode(self, s: _Session, ctx: CallContext) -> Mode:
        """Mode for the next segment. Never downgrades; upgrades only if configured."""
        if s.segment_mode == "video":
            return "video"
        if ctx.mode == "video" and self._p.segments_on_mode_change:
            return "video"
        return "audio"

    def _begin_countdown(self, ctx: CallContext, now: float, out: list[Command]) -> None:
        wall = self._clock.wall()
        sid = self._ids.new_session_id(wall)
        self._session = _Session(session_id=sid, app_id=ctx.app_id)
        out.append(BeginSession(sid, ctx, self._consent_ref, wall))
        out.append(NotifyCountdown(sid, ctx.app_id, self._p.countdown_seconds))
        self._countdown_deadline = now + self._p.countdown_seconds
        self._countdown_elapsed = False
        self._lost_since = None
        self._state = State.COUNTDOWN

    def _abort_countdown(self, out: list[Command]) -> None:
        """End a countdown without media: ``status: cancelled``. The caller sets the state."""
        s = self._live()
        out.append(CloseCountdown(s.session_id))
        out.append(WriteSessionJson(s.session_id, "cancelled"))
        self._session = None
        self._lost_since = None

    def _cancel_countdown_by_user(self, out: list[Command]) -> None:
        self._suppress(self._live().app_id)
        self._abort_countdown(out)
        out.append(Notify("start_cancelled"))
        self._state = State.ARMED_IDLE

    def _start_recording(self, ctx: CallContext, now: float, out: list[Command]) -> None:
        s = self._live()
        out.append(CloseCountdown(s.session_id))
        self._state = State.RECORDING
        s.recording_since = now
        self._start_segment(ctx.mode, ctx.capture_target, f"rule:{ctx.reason}", out)
        out.append(ShowReminder(s.session_id))

    def _start_segment(self, mode: Mode, target: AppRef, trigger: str, out: list[Command]) -> None:
        s = self._live()
        s.segment += 1
        s.segment_mode = mode
        self._sync_tray(out)  # the indicator goes up before the media starts
        out.append(StartSegment(s.session_id, s.segment, mode, target, trigger))
        out.append(WriteSessionJson(s.session_id, "recording"))

    def _maybe_upgrade(self, s: _Session, ctx: CallContext, out: list[Command]) -> None:
        if s.segment_mode == "audio" and self._wanted_mode(s, ctx) == "video":
            if s.segment >= MAX_SEGMENTS:
                return
            out.append(StopSegment(s.session_id, s.segment))
            out.append(Teardown())
            self._start_segment("video", ctx.capture_target, "upgrade:camera_on", out)

    def _begin_stop(self, out: list[Command]) -> None:
        s = self._live()
        out.append(StopSegment(s.session_id, s.segment))
        out.append(Teardown())
        self._state = State.STOPPING
        self._lost_since = None

    def _begin_postprocess(self, out: list[Command]) -> None:
        out.append(RunPostprocess(self._live().session_id))
        self._state = State.POSTPROCESSING

    def _mark_interrupted(self, out: list[Command]) -> None:
        s = self._live()
        s.interrupted = True
        out.append(MarkSegmentInterrupted(s.session_id, s.segment))

    def _sync_tray(self, out: list[Command]) -> None:
        s = self._session
        want: tuple[TrayState, Mode | None, str | None]
        if self._state in _LIVE_STATES and s is not None:
            want = ("recording", s.segment_mode, s.app_id)
        elif self._armed:
            want = ("armed", None, None)
        else:
            want = ("disarmed", None, None)
        if want != self._tray:
            self._tray = want
            out.append(SetTrayState(*want))
