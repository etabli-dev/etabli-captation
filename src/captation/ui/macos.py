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


def backend() -> MacUi:
    return MacUi()
