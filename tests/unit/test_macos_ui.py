"""macOS UI back end (PystrayUi subclass) with fake subprocess calls: runs on every OS."""

from __future__ import annotations

import subprocess
from typing import Any

import pytest

from captation.ui import macos as mac_ui
from captation.ui.macos import MacUi, backend, dialog_argv
from captation.ui.pystray_ui import PystrayUi


class Recorder:
    def __init__(self, result: Any = None, exc: BaseException | None = None) -> None:
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self.result = result
        self.exc = exc

    def __call__(self, argv: list[str], **kw: Any) -> Any:
        self.calls.append((argv, kw))
        if self.exc is not None:
            raise self.exc
        return self.result


def test_backend_is_a_pystray_ui() -> None:
    ui = backend()
    assert isinstance(ui, MacUi) and isinstance(ui, PystrayUi)
    assert ui.tray_available() is False  # nothing started yet
    assert ui.notification_capabilities() == set()


def test_play_sound_uses_afplay_detached() -> None:
    popen = Recorder()
    MacUi(popen=popen).play_sound()
    [(argv, kw)] = popen.calls
    assert argv == ["/usr/bin/afplay", "/System/Library/Sounds/Glass.aiff"]
    assert kw["start_new_session"] is True and kw["stdout"] == subprocess.DEVNULL
    MacUi(popen=Recorder(exc=OSError("no afplay"))).play_sound()  # never raises


def test_dialog_passes_text_as_arguments_not_script() -> None:
    argv = dialog_argv('Titel "x"', "Referenz eingeben; end run")
    assert argv[0] == "/usr/bin/osascript" and argv[-2:] == [
        'Titel "x"',
        "Referenz eingeben; end run",
    ]
    script = [argv[i + 1] for i, a in enumerate(argv[:-2]) if a == "-e"]
    assert script[0] == "on run argv" and script[-1] == "end run"
    assert not any("Titel" in line or "Referenz" in line for line in script)


@pytest.mark.parametrize(
    ("result", "exc", "expected"),
    [
        (subprocess.CompletedProcess([], 0, "STUDY-042\n", ""), None, "STUDY-042"),
        (subprocess.CompletedProcess([], 0, "\n", ""), None, ""),
        (
            subprocess.CompletedProcess([], 1, "", "execution error: User canceled. (-128)"),
            None,
            None,
        ),
        (None, subprocess.TimeoutExpired(["osascript"], 300), None),
        (None, OSError("missing"), None),
    ],
)
def test_ask_text(result: Any, exc: BaseException | None, expected: str | None) -> None:
    run = Recorder(result, exc)
    assert MacUi(run=run).ask_text("Studienmodus", "Einwilligungsreferenz") == expected
    [(argv, kw)] = run.calls
    assert argv[-2:] == ["Studienmodus", "Einwilligungsreferenz"]
    assert kw["timeout"] == mac_ui.DIALOG_TIMEOUT_S and kw["check"] is False


def test_menu_builds_with_the_real_pystray() -> None:
    """pystray accepts actions with at most two parameters (crashed the agent on macOS)."""
    import threading

    pystray = pytest.importorskip("pystray")
    from captation.ui.base import MenuItem

    fired = threading.Event()
    ui = PystrayUi()
    ui._menu = [MenuItem("Arm", fired.set), MenuItem("", separator=True), MenuItem("Off", None)]
    menu = ui._pystray_menu()
    assert isinstance(menu, pystray.Menu)
    arm = next(i for i in menu.items if i.text == "Arm")
    arm(None)  # pystray calls the action with (icon, item)
    assert fired.wait(2)


SIGNAL_SCRIPT = """
import sys, threading, time
from captation.ui.macos import MacUi
ui = MacUi()
ui.start()
stop = threading.Event()
def report():
    deadline = time.monotonic() + 10
    while not ui._loop_running.is_set() and time.monotonic() < deadline:
        time.sleep(0.05)
    print("visible", ui.tray_available(), flush=True)
    print("ready", flush=True)
threading.Thread(target=report, daemon=True).start()
try:
    ui.run_main_loop(stop, on_terminate=lambda: print("terminate-hook", flush=True))
finally:
    print("finally", flush=True)
"""


@pytest.mark.macos
@pytest.mark.parametrize("signame", ["SIGTERM", "SIGINT"])
def test_signals_stop_the_cocoa_run_loop_cleanly(signame: str) -> None:
    """SIGTERM (launchd, logout) and SIGINT must reach the agent while NSApp.run() owns the
    main thread, and the caller's cleanup (``finally``) must run (audit 2026-11-24)."""
    import signal
    import sys
    import time

    proc = subprocess.Popen(
        [sys.executable, "-c", SIGNAL_SCRIPT], stdout=subprocess.PIPE, text=True
    )
    try:
        lines = []
        assert proc.stdout is not None
        for line in proc.stdout:
            lines.append(line.strip())
            if line.strip() == "ready":
                break
        assert "visible True" in lines  # the real NSStatusItem exists and is shown
        sent = time.monotonic()
        proc.send_signal(getattr(signal, signame))
        out, _ = proc.communicate(timeout=10)
        assert time.monotonic() - sent < 5
        assert "finally" in out.split()
        assert proc.returncode == 0
    finally:
        proc.kill()


@pytest.mark.macos
@pytest.mark.req("R-49")
def test_reopen_event_opens_the_menu() -> None:
    """Opening the running app sends the reopen Apple Event (PF-133): it opens the menu."""
    opened: list[bool] = []
    handler = mac_ui.install_reopen_handler(lambda: opened.append(True))
    handler.handleReopen_withReplyEvent_(None, None)
    assert opened == [True]
    assert (mac_ui.AE_CORE_EVENT_CLASS, mac_ui.AE_REOPEN_APPLICATION) == (0x61657674, 0x72617070)
    assert mac_ui.AE_CORE_EVENT_CLASS.to_bytes(4, "big") == b"aevt"


def test_open_menu_without_a_running_menu_bar_item() -> None:
    ui = MacUi()
    assert ui.open_menu() is False  # no icon yet: the agent then shows a notice instead
