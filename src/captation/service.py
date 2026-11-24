"""Production wiring for ``captation run``: real OBS, probe, tray, IPC and storage."""

from __future__ import annotations

import importlib
import logging
import signal
import sys
import threading
from datetime import datetime
from types import FrameType
from typing import Any

from captation import ipc, storage, watchdog
from captation.agent import Agent, AgentDeps
from captation.config import Config
from captation.logs import setup_logging
from captation.obs import firewall, manage
from captation.platform import current_os, get_platform
from captation.rulesfile import load_rules
from captation.session import Cancel, Disarm, ManualStop
from captation.ui.base import default_backend
from captation.ui.i18n import Strings
from captation.ui.notify import ConsentUi, UiHandlers

log = logging.getLogger(__name__)


class AlreadyRunning(Exception):
    pass


def agent_running() -> bool:
    try:
        reply = ipc.request({"type": "status"}, timeout_s=3)
    except ipc.IpcError:
        return False
    return isinstance(reply, dict) and bool(reply.get("ok"))


def build_agent(cfg: Config) -> Agent:
    platform = get_platform()
    rules = load_rules(cfg.detection.rules_file, cfg.detection.user_rules_file)
    holder: dict[str, Agent] = {}
    controller = manage.make_controller(
        cfg,
        on_lost=lambda: holder["agent"].obs_lost(),
        on_record_stopped=lambda: holder["agent"].obs_record_stopped(),
    )
    installation = manage.locate(cfg)
    probe = platform.probe(
        exclude_pids=frozenset(),
        store_titles=cfg.detection.store_window_titles,
        obs_capturing=lambda: controller.recording_expected,
    )
    encryption = platform.disk_encryption()
    handlers = UiHandlers(
        arm=lambda: holder["agent"].arm_from_tray(),
        disarm=lambda: holder["agent"].post(Disarm()),
        stop=lambda: holder["agent"].post(ManualStop()),
        cancel=lambda: holder["agent"].post(Cancel()),
    )
    ui = ConsentUi(
        default_backend(),
        Strings(cfg.language),
        handlers,
        sound_on_start=cfg.ui.sound_on_start,
        reminder_text=cfg.consent.start_reminder_text,
    )

    def obs_pids() -> frozenset[int]:
        module = importlib.import_module(f"captation.platform.{current_os()}")
        finder = getattr(module, "obs_host_pids", None)
        return frozenset(finder(installation)) if finder is not None else frozenset()

    agent = Agent(
        AgentDeps(
            cfg=cfg,
            rules=rules,
            probe=probe,
            obs=controller,
            ui=ui,
            exposure_ok=lambda: firewall.check(cfg.obs.websocket.port).ok,
            encryption=lambda: encryption.status(cfg.data_root_path),
            obs_pids=obs_pids,
            free_space_ok=lambda: storage.has_free_space(
                cfg.data_root_path, cfg.storage.min_free_gb
            ),
        )
    )
    holder["agent"] = agent
    return agent


def single_instance_lock() -> Any:
    """Hold an exclusive lock for the agent's lifetime (review finding 12)."""
    path = ipc.runtime_dir() / "agent.lock"
    fh = open(path, "a+")  # noqa: SIM115 - kept open while the agent runs
    if sys.platform == "win32":  # pragma: no cover - Windows
        import msvcrt

        try:
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            fh.close()
            raise AlreadyRunning("an agent is already running for this user") from exc
    else:
        import fcntl

        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            fh.close()
            raise AlreadyRunning("an agent is already running for this user") from exc
    return fh


def recover_after_crash(cfg: Config, agent: Agent) -> None:
    """A previous agent may have died mid-recording: stop a Captation recording still running
    in OBS, remove the Captation inputs and finalise stale sessions (review finding 3)."""
    # First stop a recording that may still run, then finalise: hashing a file that OBS is
    # still writing would make SHA256SUMS wrong.
    if manage.obs_running(cfg):
        try:
            agent.d.obs.ensure_running(timeout_s=10)
            agent.d.obs.reset()
        except Exception:
            log.warning("startup: could not check OBS for a leftover recording")
    now = datetime.now().astimezone()
    recovered = storage.recover_stale_sessions(cfg, now)
    if recovered:
        log.warning("recovered %d stale session(s)", len(recovered))


def run(cfg: Config, config_path: str | None = None) -> None:
    """Run until SIGTERM/SIGINT. Starts disarmed; a stop finalises a running session."""
    lock = single_instance_lock()
    if agent_running():
        raise AlreadyRunning("an agent is already running for this user")
    setup_logging(cfg.logging.level, cfg.logging.dir, cfg.data_root_path)
    removed = storage.apply_retention(cfg, datetime.now().astimezone())
    if removed:
        log.info("retention removed %d session(s)", len(removed))
    from captation.importer import sweep_stale_staging

    if sweep_stale_staging(cfg.data_root_path):
        log.warning("removed staging folders of interrupted imports")
    agent = build_agent(cfg)
    recover_after_crash(cfg, agent)
    server = ipc.IpcServer(agent.handle_ipc)
    stop = threading.Event()

    def on_signal(signum: int, frame: FrameType | None) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, on_signal)
    agent.start()
    server.start()
    guard = watchdog.start(config_path)  # stops a recording if this process dies
    cleaned = threading.Lock()

    def cleanup() -> None:
        """Runs once: from ``finally`` or, on macOS, from AppKit's terminate (logout)."""
        if not cleaned.acquire(blocking=False):
            return
        stop.set()
        try:
            server.close()
            agent.shutdown()
            if guard is not None:
                guard.goodbye()
        finally:
            lock.close()

    try:
        # macOS needs the tray run loop on the main thread; elsewhere this just waits.
        agent.d.ui.backend.run_main_loop(stop, on_terminate=cleanup)
    finally:
        cleanup()
