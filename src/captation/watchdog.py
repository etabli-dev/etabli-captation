"""Watchdog: stop a Captation recording when the agent dies (principles §2.3, §2.5, §5.3).

The agent starts this helper as a separate process with a pipe on its stdin. On a clean
shutdown the agent writes ``bye`` and closes the pipe; the helper exits without touching OBS.
If the pipe closes without ``bye`` (crash, kill, out of memory), nobody shows the recording
indicator any more, so the helper connects to OBS (never launching it) and stops a recording
that runs in a Captation profile, then removes the Captation inputs. A recording in another
profile belongs to the user and is left alone (the controller's ``reset``).
"""

from __future__ import annotations

import contextlib
import logging
import os
import subprocess
import sys
from collections.abc import Callable
from typing import IO, Any

log = logging.getLogger(__name__)

BYE = "bye"


def wait_for_parent(stream: IO[str]) -> bool:
    """Block until the pipe closes. True if the agent said goodbye (clean shutdown)."""
    clean = False
    for line in stream:
        if line.strip() == BYE:
            clean = True
    return clean


def watch(stream: IO[str], stop_recording: Callable[[], None]) -> bool:
    """Wait for the agent; stop the recording if it vanished. Returns True if it acted."""
    if wait_for_parent(stream):
        return False
    log.warning("watchdog: the agent ended without shutting down; stopping a Captation recording")
    try:
        stop_recording()
    except Exception:
        log.exception("watchdog: could not reach OBS")
    return True


def stop_captation_recording(cfg: Any) -> None:
    from captation.obs import manage

    controller = manage.make_controller(cfg, launch=False)
    try:
        controller.ensure_running(timeout_s=10)
        controller.reset()
    finally:
        controller.close()


def command() -> list[str]:
    """argv that starts the watchdog with this installation (frozen bundle or Python)."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "watchdog"]
    return [sys.executable, "-m", "captation", "watchdog"]


class Handle:
    """The agent's end of the pipe."""

    def __init__(self, proc: subprocess.Popen[str]) -> None:
        self._proc = proc

    def goodbye(self) -> None:
        stdin = self._proc.stdin
        if stdin is None:
            return
        with contextlib.suppress(OSError, ValueError):
            stdin.write(BYE + "\n")
            stdin.flush()
            stdin.close()
        with contextlib.suppress(subprocess.TimeoutExpired):
            self._proc.wait(timeout=5)


def start(config_path: str | None = None) -> Handle | None:
    """Start the watchdog for the running agent. Best effort: a failure is logged only."""
    argv = command()
    if config_path:
        argv += ["--config", config_path]
    kwargs: dict[str, Any] = {}
    if os.name == "posix":
        kwargs["start_new_session"] = True  # survives a Ctrl+C/SIGHUP aimed at the agent
    else:  # pragma: no cover - Windows
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
            subprocess, "CREATE_NO_WINDOW", 0
        )
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            **kwargs,
        )
    except OSError:
        log.warning("watchdog could not be started")
        return None
    return Handle(proc)
