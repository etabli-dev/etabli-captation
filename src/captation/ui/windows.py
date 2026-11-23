"""Windows tray and notification back end (ADR-003): pystray + desktop-notifier
(``captation.ui.pystray_ui``) plus a native sound, the consent-reference dialog and the
AppUserModelID that Windows needs for toast buttons (stage 09 task 7, stage 11).

Prepared on Linux, verified on Windows (ADR-015, ``docs/runbook-windows.md``).

AppUserModelID: desktop-notifier 6.2 uses its ``app_name`` argument as the AUMID and writes
``HKCU\\Software\\Classes\\AppUserModelId\\<app_name>`` with ``DisplayName = app_name`` on every
start (PF-223). The notifier therefore gets the AUMID ``de.etabli.captation`` as its name, and
the display name is written back right after it. The process gets the same explicit AUMID, and
the Start-menu shortcut of the installer carries it too.
"""

from __future__ import annotations

import logging
import subprocess
import sys

from captation.platform.windows import (
    AUMID,
    Runner,
    default_runner,
    powershell_argv,
    ps_quote,
    register_aumid,
)
from captation.ui.pystray_ui import PystrayUi

log = logging.getLogger(__name__)

TK_TOPMOST = "-topmost"  # PF-252
INPUT_BOX_SCRIPT = """
Add-Type -AssemblyName Microsoft.VisualBasic
[Microsoft.VisualBasic.Interaction]::InputBox($prompt, $title, '')
"""  # PF-252
CAPABILITY_BUTTONS = "buttons"  # PF-223
CAPABILITY_BODY = "body"  # not-a-fact: internal capability name
CAPABILITY_ACTIONS = "actions"  # not-a-fact: internal capability name
NAME_ATTR = "name"  # not-a-fact: enum attribute name


def set_process_aumid(aumid: str = AUMID) -> bool:
    """``SetCurrentProcessExplicitAppUserModelID`` for this process (PF-223)."""
    if sys.platform != "win32":  # not-a-fact: sys.platform value
        return False
    import ctypes

    try:
        result = ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(  # PF-223
            ctypes.c_wchar_p(aumid)
        )
    except (AttributeError, OSError):
        return False
    return int(result) == 0


def ask_text_tk(title: str, prompt: str) -> str | None:
    """A minimal Tk input dialog in the calling thread. None if cancelled (PF-252)."""
    import tkinter
    from tkinter import simpledialog

    root = tkinter.Tk()
    try:
        root.withdraw()
        root.attributes(TK_TOPMOST, True)
        answer = simpledialog.askstring(title, prompt, parent=root)
    finally:
        root.destroy()
    return answer.strip() if answer is not None else None


def ask_text_powershell(title: str, prompt: str, runner: Runner = default_runner) -> str | None:
    """Fallback dialog (VisualBasic ``InputBox``). It cannot tell *Cancel* from an empty
    answer, so both return None (PF-252)."""
    script = f"$title = {ps_quote(title)}\n$prompt = {ps_quote(prompt)}\n" + INPUT_BOX_SCRIPT
    try:
        out = runner(powershell_argv(script), 600)
    except (OSError, subprocess.TimeoutExpired):
        return None
    answer = (out.stdout or "").strip() if out.returncode == 0 else ""
    return answer or None


class WindowsUi(PystrayUi):
    """pystray tray, desktop-notifier toasts under the AUMID, beep and input dialog."""

    async def _setup_notifier(self) -> None:
        from desktop_notifier import DesktopNotifier

        self._notifier = DesktopNotifier(app_name=AUMID, app_icon=None)
        try:
            register_aumid()  # desktop-notifier just wrote DisplayName = AUMID (PF-223)
        except OSError:
            log.warning("notify: could not set the notification display name")
        caps = await self._notifier.get_capabilities()
        names = {str(getattr(c, NAME_ATTR, c)).lower() for c in caps}
        actions = {CAPABILITY_ACTIONS} if CAPABILITY_BUTTONS in names else set()
        self._capabilities = {CAPABILITY_BODY} | actions

    def play_sound(self) -> None:
        if sys.platform == "win32":  # not-a-fact: sys.platform value
            import winsound

            try:
                winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)  # PF-251
            except RuntimeError:
                log.warning("ui: system sound unavailable")

    def ask_text(self, title: str, prompt: str) -> str | None:
        try:
            return ask_text_tk(title, prompt)
        except Exception:
            log.warning("ui: Tk dialog unavailable, using the PowerShell input box")
        return ask_text_powershell(title, prompt)


def backend() -> WindowsUi:
    """The Windows UiBackend. Sets and registers the AUMID before any toast is sent."""
    if not set_process_aumid():
        log.warning("ui: could not set the AppUserModelID of this process")
    try:
        register_aumid()
    except OSError:
        log.warning("ui: could not register the AppUserModelID")
    return WindowsUi()
