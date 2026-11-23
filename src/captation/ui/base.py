"""UI back-end contract shared by the tray/notification implementations."""

from __future__ import annotations

import importlib
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol

TrayState = Literal["disarmed", "armed", "recording"]


@dataclass(frozen=True)
class MenuItem:
    label: str
    callback: Callable[[], None] | None = None
    enabled: bool = True
    separator: bool = False


class UiBackend(Protocol):
    """Primitive tray and notification operations of one desktop platform."""

    def start(self, on_tray_change: Callable[[bool], None] | None = None) -> None: ...

    def tray_available(self) -> bool: ...

    def notification_capabilities(self) -> set[str]: ...

    def set_state(self, state: TrayState, tooltip_title: str, tooltip_body: str) -> None: ...

    def set_menu(self, items: list[MenuItem]) -> None: ...

    def set_blink(self, on: bool) -> None: ...

    def notify(
        self,
        summary: str,
        body: str,
        *,
        actions: list[tuple[str, str]] | None = None,
        on_action: Callable[[str], None] | None = None,
        on_closed: Callable[[], None] | None = None,
        critical: bool = False,
        replaces: int = 0,
    ) -> int | None: ...

    def close_notification(self, nid: int) -> None: ...

    def play_sound(self) -> None: ...

    def ask_text(self, title: str, prompt: str) -> str | None: ...

    def run_main_loop(self, stop: threading.Event) -> None:
        """Block the main thread until ``stop`` is set (macOS: the Cocoa run loop)."""
        ...

    def stop(self) -> None: ...


class UnavailableUi:
    """Platforms without a verified tray back end yet (macOS/Windows until stages 06/07).

    ``tray_available()`` is False, so arming is always refused (docs/principles.md §2.8).
    """

    def start(self, on_tray_change: Callable[[bool], None] | None = None) -> None:
        return None

    def tray_available(self) -> bool:
        return False

    def notification_capabilities(self) -> set[str]:
        return set()

    def set_state(self, state: TrayState, tooltip_title: str, tooltip_body: str) -> None:
        return None

    def set_menu(self, items: list[MenuItem]) -> None:
        return None

    def set_blink(self, on: bool) -> None:
        return None

    def notify(
        self,
        summary: str,
        body: str,
        *,
        actions: list[tuple[str, str]] | None = None,
        on_action: Callable[[str], None] | None = None,
        on_closed: Callable[[], None] | None = None,
        critical: bool = False,
        replaces: int = 0,
    ) -> int | None:
        return None

    def close_notification(self, nid: int) -> None:
        return None

    def play_sound(self) -> None:
        return None

    def ask_text(self, title: str, prompt: str) -> str | None:
        return None

    def run_main_loop(self, stop: threading.Event) -> None:
        stop.wait()

    def stop(self) -> None:
        return None


def default_backend() -> UiBackend:
    """The tray back end of this OS. macOS/Windows: prepared on Linux, verified in 06/07."""
    from captation.platform import current_os

    name = current_os()
    if name == "linux" and sys.platform == "linux":  # the second test is for mypy
        from captation.ui.linux import LinuxUi

        return LinuxUi()
    try:
        module = importlib.import_module(f"captation.ui.{name}")
        backend: UiBackend = module.backend()
        return backend
    except Exception:
        return UnavailableUi()
