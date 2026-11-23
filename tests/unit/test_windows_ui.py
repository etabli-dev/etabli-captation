"""Windows UI back end on Linux: AUMID handling, input-dialog fallback, import safety."""

from __future__ import annotations

import asyncio
import sys
import types
from typing import Any

import pytest

from captation.platform import windows as win
from captation.ui import windows as wui
from tests.unit.test_windows_probe import FakeRunner, decoded_script


def test_module_imports_and_aumid_is_shared() -> None:
    assert wui.AUMID == win.AUMID == "de.etabli.captation"
    assert issubclass(wui.WindowsUi, wui.PystrayUi)


@pytest.mark.skipif(sys.platform == "win32", reason="checks the non-Windows guard")
def test_process_aumid_is_a_no_op_off_windows() -> None:
    assert wui.set_process_aumid() is False


def test_notifier_uses_the_aumid_and_restores_the_display_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: dict[str, Any] = {}
    order: list[str] = []

    class Capability:
        def __init__(self, name: str) -> None:
            self.name = name

    class FakeNotifier:
        def __init__(self, app_name: str, app_icon: Any = "default") -> None:
            created.update(app_name=app_name, app_icon=app_icon)
            order.append("notifier")

        async def get_capabilities(self) -> set[Any]:
            return {Capability("BUTTONS"), Capability("TITLE")}

    fake = types.ModuleType("desktop_notifier")
    fake.DesktopNotifier = FakeNotifier  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "desktop_notifier", fake)
    monkeypatch.setattr(wui, "register_aumid", lambda: order.append("register"))
    ui = wui.WindowsUi()
    asyncio.run(ui._setup_notifier())
    assert created == {"app_name": "de.etabli.captation", "app_icon": None}
    assert order == ["notifier", "register"]  # display name written after desktop-notifier's
    assert ui.notification_capabilities() == {"body", "actions"}


def test_notifier_without_buttons(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeNotifier:
        def __init__(self, app_name: str, app_icon: Any = None) -> None:
            pass

        async def get_capabilities(self) -> set[Any]:
            return set()

    fake = types.ModuleType("desktop_notifier")
    fake.DesktopNotifier = FakeNotifier  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "desktop_notifier", fake)

    def denied() -> None:
        raise OSError("access denied")

    monkeypatch.setattr(wui, "register_aumid", denied)
    ui = wui.WindowsUi()
    asyncio.run(ui._setup_notifier())
    assert ui.notification_capabilities() == {"body"}


@pytest.mark.req("R-20")
def test_ask_text_falls_back_to_powershell(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_tk(title: str, prompt: str) -> str | None:
        raise ImportError("no tkinter")

    monkeypatch.setattr(wui, "ask_text_tk", no_tk)
    monkeypatch.setattr(wui, "ask_text_powershell", lambda title, prompt: "STUDY-042")
    assert wui.WindowsUi().ask_text("Studienmodus", "Referenz") == "STUDY-042"


def test_ask_text_uses_tk_first(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wui, "ask_text_tk", lambda title, prompt: None)  # cancelled
    monkeypatch.setattr(wui, "ask_text_powershell", lambda title, prompt: "never")
    assert wui.WindowsUi().ask_text("t", "p") is None


@pytest.mark.parametrize(
    ("rc", "out", "answer"), [(0, "REF-1\r\n", "REF-1"), (0, "", None), (1, "x", None)]
)
def test_powershell_input_box(rc: int, out: str, answer: str | None) -> None:
    runner = FakeRunner()
    runner.on(lambda argv: True, rc=rc, out=out)
    assert wui.ask_text_powershell("Zustimmung", "Referenz l'ID", runner) == answer
    script = decoded_script(runner.calls[0])
    assert "$title = 'Zustimmung'" in script and "$prompt = 'Referenz l''ID'" in script
    assert "[Microsoft.VisualBasic.Interaction]::InputBox($prompt, $title, '')" in script


def test_backend_survives_registry_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def denied() -> None:
        raise OSError("denied")

    monkeypatch.setattr(wui, "register_aumid", denied)
    monkeypatch.setattr(wui, "set_process_aumid", lambda: False)
    assert isinstance(wui.backend(), wui.WindowsUi)


def test_play_sound_is_safe_everywhere() -> None:
    wui.WindowsUi().play_sound()


@pytest.mark.windows
def test_process_aumid_on_windows() -> None:
    assert wui.set_process_aumid() is True
