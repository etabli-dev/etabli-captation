"""macOS tray and notification back end (ADR-003): :class:`PystrayUi` plus a native sound and
a native text-input dialog. Prepared on Linux, verified on the Mac (ADR-015, PF-126).

Threading: Cocoa UI (the pystray menu-bar item) must run on the **main thread**.
``captation run`` therefore runs the agent loop in worker threads and calls
:meth:`run_main_loop` on the main thread, which hands it to ``pystray.Icon.run()`` (the
NSApplication run loop) until the stop event is set.

Notifications: desktop-notifier uses ``UNUserNotificationCenter``, which only works when the
process runs from a **signed app bundle** (``Établi Captation.app``, ``packaging/macos``). Run
from a virtualenv (``uv run captation run``) the notifier is unavailable, so
``notification_capabilities()`` is empty and the mandatory fallback chain applies (tray blink,
sound, tray-menu cancel; docs/principles.md §2.8). Never start silently.
"""

from __future__ import annotations

import logging
import subprocess
import threading
from collections.abc import Callable
from typing import Any

from captation.ui.pystray_ui import PystrayUi

log = logging.getLogger(__name__)

AFPLAY = "/usr/bin/afplay"  # PF-126
SOUND_FILE = "/System/Library/Sounds/Glass.aiff"  # PF-126
OSASCRIPT = "/usr/bin/osascript"  # PF-126
DIALOG_TIMEOUT_S = 300
# Title and prompt are passed as arguments (``item n of argv``), never spliced into the
# script, so no text can inject AppleScript. Cancel raises error -128 → exit status 1.
DIALOG_SCRIPT = (
    "on run argv",  # PF-126
    "activate",  # PF-126
    'set r to display dialog (item 2 of argv) default answer ""'  # PF-126
    " with title (item 1 of argv)",  # PF-126
    "return text returned of r",  # PF-126
    "end run",  # PF-126
)
SCRIPT_FLAG = "-e"  # PF-126

Popen = Callable[..., Any]
Run = Callable[..., subprocess.CompletedProcess[str]]


def dialog_argv(title: str, prompt: str) -> list[str]:
    argv = [OSASCRIPT]
    for line in DIALOG_SCRIPT:
        argv += [SCRIPT_FLAG, line]
    return [*argv, title, prompt]


class MacUi(PystrayUi):
    """PystrayUi with ``afplay`` and an ``osascript`` input dialog."""

    def __init__(self, *, popen: Popen = subprocess.Popen, run: Run = subprocess.run) -> None:
        super().__init__()
        self._popen = popen
        self._run_cmd = run
        self._loop_running = threading.Event()
        self._terminate_observer: Any = None

    def play_sound(self) -> None:
        try:
            self._popen(
                [AFPLAY, SOUND_FILE],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError:
            log.warning("ui: cannot play the attention sound")

    def ask_text(self, title: str, prompt: str) -> str | None:
        """A modal dialog with a text field; ``None`` on cancel, timeout or error."""
        try:
            out = self._run_cmd(
                dialog_argv(title, prompt),
                capture_output=True,
                text=True,
                timeout=DIALOG_TIMEOUT_S,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        return out.stdout.strip() if out.returncode == 0 else None

    # ---- AppKit threading, real visibility, run-loop signals (audit 2026-11-24) ------------

    def _ui(self, fn: Callable[[], None]) -> None:
        """AppKit is main-thread only: tray changes from the agent threads are queued onto the
        Cocoa run loop. A failing change is logged; the next refresh repeats it."""

        def guarded() -> None:
            try:
                fn()
            except Exception:
                log.exception("tray: update failed")

        if _on_main_thread():
            guarded()
        else:
            from PyObjCTools import AppHelper

            AppHelper.callAfter(guarded)

    def _icon_visible(self) -> bool:
        """The menu-bar item really exists and is shown (not only the pystray object): the
        NSStatusItem is visible and its button has a visible window. Read on the main thread;
        no answer within ``VISIBILITY_TIMEOUT_S`` counts as not visible (fail closed, §2.5)."""
        item = getattr(self._icon, "_status_item", None)  # not-a-fact: pystray darwin internals
        if item is None:
            return True  # not pystray's AppKit back end (tests)

        def check() -> bool:
            button = item.button()
            window = button.window() if button is not None else None
            return bool(item.isVisible()) and window is not None and bool(window.isVisible())

        if _on_main_thread():
            return check()
        if not self._loop_running.is_set():
            return False
        answer: list[bool] = []
        done = threading.Event()

        def ask() -> None:
            try:
                answer.append(check())
            finally:
                done.set()

        from PyObjCTools import AppHelper

        AppHelper.callAfter(ask)
        return done.wait(VISIBILITY_TIMEOUT_S) and bool(answer and answer[0])

    def _on_loop_running(
        self, stop: threading.Event, on_terminate: Callable[[], None] | None
    ) -> None:
        """Called once NSApplication runs. Python signal handlers cannot run while the main
        thread sits in ``NSApp.run()``, and pystray turns SIGINT into ``NSApp terminate:``
        (process exit without cleanup). Mach-port handlers run on the run loop instead and
        only set ``stop``; an ``NSApplicationWillTerminate`` observer runs ``on_terminate``
        when AppKit ends the process itself (logout)."""
        from PyObjCTools import AppHelper

        def install() -> None:
            import signal

            from AppKit import NSApplicationWillTerminateNotification
            from Foundation import NSNotificationCenter
            from PyObjCTools import MachSignals

            def on_signal(signum: int) -> None:
                log.info("signal %d: stopping", signum)
                stop.set()

            for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
                MachSignals.signal(signum, on_signal)

            def will_terminate(note: Any) -> None:
                log.info("AppKit terminates the process: shutting down first")
                stop.set()
                if on_terminate is not None:
                    on_terminate()

            center = NSNotificationCenter.defaultCenter()
            self._terminate_observer = center.addObserverForName_object_queue_usingBlock_(
                NSApplicationWillTerminateNotification, None, None, will_terminate
            )
            self._loop_running.set()

        AppHelper.callAfter(install)


VISIBILITY_TIMEOUT_S = 2.0


def _on_main_thread() -> bool:
    try:
        from Foundation import NSThread
    except ImportError:  # not on macOS (unit tests on other OSes)
        return True
    return bool(NSThread.isMainThread())


def backend() -> MacUi:
    return MacUi()
