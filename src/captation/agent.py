"""Agent: probe → classifier → session state machine → command executor.

One event loop thread owns the state machine and executes every command; the poll thread,
IPC threads, UI callbacks and the OBS reader only enqueue events (docs/principles.md §7: a single,
thread-safe event queue). The agent always starts disarmed and never persists arming.
"""

from __future__ import annotations

import contextlib
import logging
import queue
import re
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from captation import postprocess, session, storage, terms
from captation.config import Config
from captation.models import AppRef, EncryptionStatus, Mode
from captation.obs.client import ObsError
from captation.obs.controller import ObsInfo, PreparedSegment
from captation.platform.base import CallProbe
from captation.rules import Rules, classify
from captation.session import (
    Arm,
    Armed,
    ArmRefused,
    BeginSession,
    Cancel,
    CloseCountdown,
    Disarm,
    Disarmed,
    LowDisk,
    ManualStop,
    MarkSegmentInterrupted,
    Notify,
    NotifyCountdown,
    ObsBack,
    ObsLost,
    PostprocessDone,
    PrewarmObs,
    RunPostprocess,
    SegmentStopped,
    SessionMachine,
    SessionParams,
    SetTrayState,
    ShowReminder,
    StartSegment,
    State,
    StopSegment,
    Teardown,
    Tick,
    WriteSessionJson,
)
from captation.ui.notify import ConsentUi

log = logging.getLogger(__name__)

FREE_SPACE_CHECK_S = 10.0
TRAY_CHECK_S = 10.0
MAX_CONSENT_REF = 64  # session.schema.json consent_ref maxLength
OBS_PID_REFRESH_S = 15.0
RECOVERY_TIMEOUT_S = 30.0
NOTICE_KINDS = {
    "auto_disarmed": ("disarmed.title", "auto_disarmed.body"),
    "low_disk": ("refused.title", "notify.low_disk"),
    "obs_lost": ("app.title", "notify.obs_lost"),
    "start_cancelled": ("app.title", "notify.start_cancelled"),
}


class ObsDriver(Protocol):
    """The parts of ObsController the agent uses (a fake in tests)."""

    def ensure_running(self, *, timeout_s: float = 30) -> ObsInfo: ...

    def prepare_segment(self, mode: Mode, target: AppRef | None) -> PreparedSegment: ...

    def start_segment(self, record_dir: Any, basename: str) -> None: ...

    def stop_segment(self, timeout_s: float = 20) -> Any: ...

    def teardown_segment(self) -> None: ...

    def reset(self) -> None: ...

    def close(self) -> None: ...


class RealClock:
    def now(self) -> float:
        return time.monotonic()

    def wall(self) -> datetime:
        return datetime.now().astimezone()


class StorageIds:
    def new_session_id(self, wall: datetime) -> str:
        return storage.new_session_id(wall)


@dataclass
class _Query:
    """Internal event: run a function on the loop thread (status, tray changes)."""

    fn: Callable[[], Any]


@dataclass
class _Stop:
    pass


@dataclass
class AgentDeps:
    cfg: Config
    rules: Rules
    probe: CallProbe
    obs: ObsDriver
    ui: ConsentUi
    exposure_ok: Callable[[], bool]
    encryption: Callable[[], EncryptionStatus]
    obs_pids: Callable[[], frozenset[int]] = lambda: frozenset()
    terms_accepted: Callable[[], bool] = lambda: terms.accepted()
    free_space_ok: Callable[[], bool] | None = None
    clock: session.Clock = field(default_factory=RealClock)
    ids: session.IdFactory = field(default_factory=StorageIds)


class Agent:
    def __init__(self, deps: AgentDeps) -> None:
        self.d = deps
        self.cfg = deps.cfg
        self.machine = SessionMachine(SessionParams.from_config(deps.cfg), deps.clock, deps.ids)
        self._queue: queue.Queue[tuple[Any, Future[Any] | None]] = queue.Queue()
        self._records: dict[str, storage.SessionRecord] = {}
        self._modes: dict[tuple[str, int], Mode] = {}
        self._obs_info: ObsInfo | None = None
        self._stopping = threading.Event()
        self._recovering = threading.Lock()
        self._exclude: frozenset[int] = frozenset()
        self._exclude_at = 0.0
        self._space_at = 0.0
        self._encryption_warning: str | None = None
        self._indicator_lost = False
        self._tray_at = 0.0
        self._tray_seen = True
        self._loop_thread: threading.Thread | None = None
        self._poll_thread: threading.Thread | None = None

    # ---- event plumbing --------------------------------------------------------------------

    def post(self, event: Any) -> None:
        self._queue.put((event, None))

    def submit(self, event: Any, timeout_s: float = 60) -> Any:
        fut: Future[Any] = Future()
        self._queue.put((event, fut))
        return fut.result(timeout_s)

    def _dispatch(self, event: Any) -> Any:
        if isinstance(event, _Query):
            return event.fn()
        commands = self.machine.handle(event)
        for cmd in commands:
            try:
                self._execute(cmd)
            except Exception:
                log.exception("executor: %s failed", type(cmd).__name__)
                self._on_command_failed(cmd)
        idle = self.machine.state in (State.ARMED_IDLE, State.PENDING_START)
        if self._indicator_lost and idle and not self._tray_ok():
            # ADR-014: no new recording without a visible indicator.
            log.warning("tray indicator still lost after the session: disarming")
            extra = self.machine.handle(Disarm())
            for cmd in extra:
                try:
                    self._execute(cmd)
                except Exception:
                    log.exception("executor: %s failed", type(cmd).__name__)
            commands = [*commands, *extra]
        self._refresh_tray()
        return commands

    def _on_command_failed(self, cmd: Any) -> None:
        """Feed executor failures back into the state machine (no false indicator)."""
        if isinstance(cmd, BeginSession):
            self.d.ui.notice("app.title", "notify.session_failed")
            self.post(Cancel())
        elif isinstance(cmd, StartSegment):
            self.post(ObsLost())
        elif isinstance(cmd, StopSegment):
            self.post(SegmentStopped(ok=False))
        elif isinstance(cmd, RunPostprocess):
            self.post(PostprocessDone(ok=False))
        elif isinstance(cmd, WriteSessionJson):
            self.d.ui.notice("app.title", "notify.session_failed")
            self._records.pop(cmd.session_id, None)

    def _loop(self) -> None:
        while True:
            event, fut = self._queue.get()
            if isinstance(event, _Stop):
                if fut is not None:
                    fut.set_result(None)
                return
            try:
                result = self._dispatch(event)
            except Exception as exc:
                log.exception("agent: event %s failed", type(event).__name__)
                if fut is not None:
                    fut.set_exception(exc)
                continue
            if fut is not None:
                fut.set_result(result)

    # ---- polling ---------------------------------------------------------------------------

    def _exclude_pids(self) -> frozenset[int]:
        now = time.monotonic()
        if now - self._exclude_at > OBS_PID_REFRESH_S:
            self._exclude = self.d.obs_pids()
            self._exclude_at = now
        return self._exclude

    def poll_once(self) -> None:
        """One probe + classify step (only while armed: disarmed means nothing is read)."""
        if self.machine.state is State.DISARMED:
            return
        snap = self.d.probe.snapshot()
        ctx = classify(
            snap,
            self.d.rules,
            self.cfg.detection,
            self.machine.current_app_id,
            exclude_pids=self._exclude_pids(),
        )
        self.post(Tick(ctx))
        now = time.monotonic()
        if now - self._tray_at > TRAY_CHECK_S:
            # Catches a host that vanished without a NameOwnerChanged (review finding 16).
            self._tray_at = now
            ok = self.d.ui.tray_available()
            if ok != self._tray_seen:
                self._tray_seen = ok
                self.tray_changed(ok)
        if self.d.free_space_ok is not None and now - self._space_at > FREE_SPACE_CHECK_S:
            self._space_at = now
            if not self.d.free_space_ok():
                self.post(LowDisk())

    def _poll(self) -> None:
        interval = self.cfg.detection.poll_interval_ms / 1000
        while not self._stopping.wait(interval):
            try:
                self.poll_once()
            except Exception:
                log.exception("poll: probe failed")

    # ---- arming ----------------------------------------------------------------------------

    def arm_blockers(self, consent_ref: str | None) -> tuple[str, ...]:
        c = self.cfg.consent
        blockers: list[str] = []
        if not self.d.terms_accepted():
            blockers.append("terms_not_accepted")  # DISCLAIMER.md acceptance gate (R-59)
        ref = (consent_ref or "").strip()
        if len(ref) > MAX_CONSENT_REF or (ref and not ref.isprintable()):
            blockers.append("consent_ref_invalid")
        elif c.consent_ref_pattern is not None:
            if not ref and c.ask_consent_ref_on_arm:
                blockers.append("consent_ref_required")
            elif ref and not re.fullmatch(c.consent_ref_pattern, ref):
                blockers.append("consent_ref_invalid")
        if not self._tray_ok():
            blockers.append("no_tray")
        if self.cfg.obs.exposure_policy == "require_firewall" and not self.d.exposure_ok():
            blockers.append("exposure_policy_unmet")
        verdict = storage.encryption_verdict(
            self.d.encryption(), self.cfg.storage.require_disk_encryption
        )
        if verdict == "block":
            blockers.append("disk_encryption_required")
        if not blockers and self.cfg.obs.launch_on == "arm":
            try:
                self._obs_info = self.d.obs.ensure_running()
            except Exception:
                log.warning("arming: OBS unavailable")
                blockers.append("obs_unavailable")
            else:
                self.post(ObsBack())  # OBS confirmed reachable (clears an earlier loss)
                # Re-check the exposure rule now that the websocket really listens.
                if self.cfg.obs.exposure_policy == "require_firewall" and not self.d.exposure_ok():
                    blockers.append("exposure_policy_unmet")
        return tuple(blockers)

    def arm(self, consent_ref: str | None) -> dict[str, Any]:
        ref = (consent_ref or "").strip() or None
        blockers = self.arm_blockers(ref)
        commands = self.submit(Arm(ref, blockers))
        refused = [c for c in commands if isinstance(c, ArmRefused)]
        if refused:
            return {"ok": False, "refused": list(refused[0].reasons)}
        return {"ok": True, "state": self.machine.state.name}

    def arm_from_tray(self) -> None:
        ref = None
        if self.cfg.consent.ask_consent_ref_on_arm:
            ref = self.d.ui.ask_consent_ref()
            if ref is None and self.cfg.consent.consent_ref_pattern is not None:
                return  # dialog cancelled
        self.arm(ref)

    # ---- executor --------------------------------------------------------------------------

    def _now(self) -> datetime:
        return self.d.clock.wall()

    def _execute(self, cmd: Any) -> None:
        ui = self.d.ui
        if isinstance(cmd, SetTrayState):
            return  # the tray is refreshed after every event (_refresh_tray)
        if isinstance(cmd, ArmRefused):
            body = " ".join(ui.t(f"refused.{r}") for r in cmd.reasons)
            ui.backend.notify(ui.t("refused.title"), body)
            log.warning("arming refused: %s", ", ".join(cmd.reasons))
        elif isinstance(cmd, Armed):
            minutes = self.cfg.consent.auto_disarm_after_minutes
            ui.notice("armed.title", "armed.body", minutes=minutes, ref=cmd.consent_ref or "–")
            status = self.d.encryption()
            verdict = storage.encryption_verdict(status, self.cfg.storage.require_disk_encryption)
            self._encryption_warning = (
                storage.encryption_warning(status) if verdict == "warn" else None
            )
            if self._encryption_warning:
                ui.notice("app.title", "notify.disk_unencrypted")
            log.info("armed")
        elif isinstance(cmd, Disarmed):
            if cmd.reason == "user":
                ui.notice("disarmed.title", "tray.disarmed")
            log.info("disarmed (%s)", cmd.reason)
        elif isinstance(cmd, PrewarmObs):
            if self._obs_info is None:
                threading.Thread(target=self._prewarm, daemon=True).start()
        elif isinstance(cmd, NotifyCountdown):
            problem = self._preflight()
            if problem is not None:
                ui.notice("refused.title", f"refused.{problem}")
                log.warning("countdown aborted: %s", problem)
                self.post(Cancel())
                self.post(Disarm())
                return
            level = ui.countdown(cmd.app_id, cmd.seconds)
            log.info("countdown for %s (fallback level %d)", cmd.app_id, level)
        elif isinstance(cmd, CloseCountdown):
            ui.close_countdown()
        elif isinstance(cmd, BeginSession):
            self._begin_session(cmd)
        elif isinstance(cmd, StartSegment):
            self._start_segment(cmd)
        elif isinstance(cmd, StopSegment):
            self._stop_segment(cmd)
        elif isinstance(cmd, MarkSegmentInterrupted):
            record = self._records.get(cmd.session_id)
            if record is not None:
                seg = record.segment(cmd.index)
                if seg.ended_at is None or not seg.interrupted:
                    record.end_segment(cmd.index, self._now(), interrupted=True)
        elif isinstance(cmd, Teardown):
            try:
                self.d.obs.teardown_segment()
            except (ObsError, OSError):
                log.warning("teardown failed (OBS unreachable)")
        elif isinstance(cmd, RunPostprocess):
            self._postprocess(cmd.session_id)
        elif isinstance(cmd, WriteSessionJson):
            self._write_session(cmd)
        elif isinstance(cmd, ShowReminder):
            ui.recording_started()
        elif isinstance(cmd, Notify):
            title, body = NOTICE_KINDS.get(cmd.kind, ("app.title", "app.title"))
            ui.notice(title, body)
            if cmd.kind == "obs_lost":
                threading.Thread(target=self._recover_obs, daemon=True).start()

    def _tray_ok(self) -> bool:
        """Fresh tray check; a visible tray clears an earlier loss (ADR-014)."""
        ok = self.d.ui.tray_available()
        if ok:
            self._indicator_lost = False
        return ok

    def _preflight(self) -> str | None:
        """Checks right before a countdown: visible indicator and exposure rule."""
        if not self._tray_ok():
            return "no_tray"
        if self.cfg.obs.exposure_policy == "require_firewall" and not self.d.exposure_ok():
            return "exposure_policy_unmet"
        return None

    def _prewarm(self) -> None:
        try:
            self._obs_info = self.d.obs.ensure_running()
        except Exception:
            log.warning("OBS could not be started")

    def _begin_session(self, cmd: BeginSession) -> None:
        info = self._obs_info
        host = storage.host_info(
            info.obs_version if info else None, info.ws_version if info else None
        )
        ctx = cmd.context
        record = storage.SessionRecord.create(
            self.cfg,
            session_id=cmd.session_id,
            app_id=ctx.app_id,
            category=ctx.category,
            consent_ref=cmd.consent_ref,
            started_at=cmd.started_at,
            host=host,
            window_title=ctx.window_title,
        )
        if self._encryption_warning:
            record.add_warning(self._encryption_warning)
        if self.cfg.obs.exposure_policy == "warn" and not self.d.exposure_ok():
            record.add_warning("obs:websocket_exposed")
        if self._indicator_lost:
            record.add_warning("ui:indicator_lost")
        self._records[cmd.session_id] = record
        log.info("session %s begins (app %s)", cmd.session_id, ctx.app_id)

    def _start_segment(self, cmd: StartSegment) -> None:
        record = self._records.get(cmd.session_id)
        if record is None:
            log.warning("segment %d has no session record", cmd.index)
            self.post(ObsLost())
            return
        self._modes[(cmd.session_id, cmd.index)] = cmd.mode
        try:
            prepared = self.d.obs.prepare_segment(cmd.mode, cmd.target)
            record.begin_segment(cmd.index, cmd.mode, cmd.trigger, self._now(), prepared.capture)
            for warning in prepared.warnings:
                record.add_warning(warning)
            self.d.obs.start_segment(record.directory, f"seg{cmd.index:02d}_container")
            log.info("segment %d started (%s, %s)", cmd.index, cmd.mode, cmd.trigger)
        except (ObsError, OSError) as exc:
            log.warning("segment %d could not start: %s", cmd.index, type(exc).__name__)
            if cmd.index not in {s.index for s in record.segments}:
                record.begin_segment(cmd.index, cmd.mode, cmd.trigger, self._now(), "none")
            self.post(ObsLost())

    def _stop_segment(self, cmd: StopSegment) -> None:
        record = self._records.get(cmd.session_id)
        ok = True
        try:
            self.d.obs.stop_segment()
        except (ObsError, OSError) as exc:
            log.warning("segment %d did not stop cleanly: %s", cmd.index, type(exc).__name__)
            ok = False
        if record is not None and cmd.index in {s.index for s in record.segments}:
            record.end_segment(cmd.index, self._now(), interrupted=not ok)
        self.post(SegmentStopped(ok=ok))

    def _postprocess(self, session_id: str) -> None:
        record = self._records.get(session_id)
        ok = True
        if record is not None:
            for seg in record.segments:
                try:
                    postprocess.process_segment(record, seg.index, seg.mode, self.cfg)
                except Exception:
                    log.exception("post-processing of segment %d failed", seg.index)
                    ok = False
        self.post(PostprocessDone(ok=ok))

    def _write_session(self, cmd: WriteSessionJson) -> None:
        record = self._records.get(cmd.session_id)
        if record is None:
            return
        if cmd.status == "recording":
            record.write()
            return
        if cmd.status == "cancelled":
            record.set_status("cancelled")
            record.set_ended(self._now())
            record.write()
        else:
            result = postprocess.finalize_session(
                record, self.cfg, status=cmd.status, run_hooks=False
            )
            if postprocess.hook_due(record, self.cfg):
                # Hooks may run for hours: never on the event loop (stage 14 review, finding 8).
                threading.Thread(
                    target=postprocess.run_session_hook, args=(record, self.cfg), daemon=True
                ).start()
            if result.qc_warnings:
                self.d.ui.notice(
                    "app.title", "notify.qc_warning", warnings=", ".join(result.qc_warnings)
                )
        log.info("session %s finalised: %s", cmd.session_id, cmd.status)
        del self._records[cmd.session_id]

    def _recover_obs(self) -> None:
        if not self._recovering.acquire(blocking=False):
            return
        try:
            deadline = time.monotonic() + RECOVERY_TIMEOUT_S
            while time.monotonic() < deadline and not self._stopping.is_set():
                try:
                    self.d.obs.close()
                    self._obs_info = self.d.obs.ensure_running(timeout_s=RECOVERY_TIMEOUT_S)
                    self.d.obs.reset()
                except Exception:
                    log.warning("OBS recovery attempt failed")
                    time.sleep(1)
                    continue
                self.post(ObsBack())
                return
            log.warning("OBS did not come back within %.0f s", RECOVERY_TIMEOUT_S)
            if not self._stopping.is_set():
                # ADR-013: finalise the session as interrupted instead of waiting in ERROR.
                self.post(ManualStop())
        finally:
            self._recovering.release()

    def obs_lost(self) -> None:
        """Callback from the OBS connection (reader thread)."""
        if self._stopping.is_set():
            return
        self.post(ObsLost())
        if self.machine.state is not State.DISARMED:
            threading.Thread(target=self._recover_obs, daemon=True).start()

    # ---- tray ------------------------------------------------------------------------------

    def _refresh_tray(self) -> None:
        st = self.machine.status()
        state_name = str(st["state"])
        if state_name in ("RECORDING", "PENDING_STOP", "STOPPING", "ERROR"):
            tray = "recording"
        elif state_name == "DISARMED":
            tray = "disarmed"
        else:
            tray = "armed"
        remaining = st.get("auto_disarm_in_s")
        elapsed = st.get("recording_elapsed_s")
        self.d.ui.show_state(
            tray,  # type: ignore[arg-type]
            machine_state=state_name,
            mode=st.get("mode"),  # type: ignore[arg-type]
            app_id=st.get("app_id"),  # type: ignore[arg-type]
            remaining_s=float(remaining) if isinstance(remaining, (int, float)) else None,
            elapsed_s=float(elapsed) if isinstance(elapsed, (int, float)) else None,
            countdown_s=self.cfg.consent.countdown_seconds,
        )

    def tray_changed(self, available: bool) -> None:
        """Tray host appeared/disappeared (e.g. screen lock). ADR-014."""

        def apply() -> None:
            if available:
                self._indicator_lost = False
                self._refresh_tray()
                return
            state = self.machine.state
            self._indicator_lost = True
            if state is State.DISARMED:
                return
            self.d.ui.notice("app.title", "notify.indicator_lost")
            if state in (State.ARMED_IDLE, State.PENDING_START, State.COUNTDOWN):
                log.warning("tray indicator lost while armed: disarming")
                self._dispatch(Disarm())
            else:
                log.warning("tray indicator lost during a recording: continuing with a warning")
                for record in self._records.values():
                    record.add_warning("ui:indicator_lost")

        self.post(_Query(apply))

    # ---- IPC -------------------------------------------------------------------------------

    def handle_ipc(self, msg: dict[str, Any]) -> dict[str, Any]:
        kind = msg["type"]
        if kind == "arm":
            return self.arm(msg.get("consent_ref"))
        if kind == "status":
            st: dict[str, Any] = self.submit(_Query(self.machine.status))
            st["tray"] = self.d.ui.tray_available()
            return {"ok": True, **st}
        event = {"disarm": Disarm(), "stop": ManualStop(), "cancel": Cancel()}[kind]
        self.submit(event)
        return {"ok": True, "state": self.machine.state.name}

    # ---- lifecycle -------------------------------------------------------------------------

    def start(self) -> None:
        self._loop_thread = threading.Thread(target=self._loop, name="captation-loop", daemon=True)
        self._loop_thread.start()
        self.d.ui.on_tray_change = self.tray_changed
        self.d.ui.start()
        self._refresh_tray()
        if self.cfg.obs.launch_on == "agent_start":
            threading.Thread(target=self._prewarm, daemon=True).start()
        self._poll_thread = threading.Thread(target=self._poll, name="captation-poll", daemon=True)
        self._poll_thread.start()
        log.info("agent started (disarmed)")

    def shutdown(self) -> None:
        """Stop and finalise a running session, then stop all threads."""
        if self.machine.state is not State.DISARMED:
            try:
                self.submit(Disarm(), timeout_s=120)
                deadline = time.monotonic() + 120
                while self._records and time.monotonic() < deadline:
                    time.sleep(0.2)
            except Exception:
                log.exception("shutdown: could not finalise the session")
        self._stopping.set()
        self.submit(_Stop())
        self.d.ui.stop()
        with contextlib.suppress(Exception):
            self.d.obs.close()
        self.d.probe.close()
        log.info("agent stopped")
