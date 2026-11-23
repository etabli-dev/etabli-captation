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
