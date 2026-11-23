"""Tray + notification back end for macOS and Windows (ADR-003): pystray + desktop-notifier.

Prepared on Linux, verified on macOS/Windows (stages 06/07, runbooks in docs/). The
platform modules ``captation.ui.macos`` / ``captation.ui.windows`` subclass it.

Threading: pystray's ``Icon.run()`` must own the main thread on macOS (Cocoa); the agent's
event loop runs in a worker thread and :meth:`run_main_loop` blocks the main thread until the
stop event is set. desktop-notifier is asyncio-based and gets its own loop thread.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from collections.abc import Callable, Coroutine
from typing import Any, TypeVar

from captation.ui.base import MenuItem, TrayState

log = logging.getLogger(__name__)
T = TypeVar("T")

COLOURS = {
    "disarmed": (0x80, 0x80, 0x80),
    "armed": (0xE0, 0xA8, 0x00),
    "recording": (0xD0, 0x10, 0x10),
    "attention": (0xFF, 0xFF, 0xFF),
}


def make_icon_image(rgb: tuple[int, int, int], size: int = 64) -> Any:
    """A filled circle as a PIL image (pystray needs PIL images)."""
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((2, 2, size - 3, size - 3), fill=(*rgb, 255), outline=(32, 32, 32, 255), width=2)
    return img


class PystrayUi:
    """UiBackend on pystray (tray) and desktop-notifier (notifications)."""

    app_name = "Établi Captation"

    def __init__(self) -> None:
        self._icon: Any = None
        self._menu: list[MenuItem] = []
        self.state: TrayState = "disarmed"
        self._tooltip = self.app_name
        self._blink = False
        self._blink_stop = threading.Event()
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._notifier: Any = None
        self._capabilities: set[str] = set()
        self._handles: dict[int, Any] = {}
        self._next_id = 0
        self._tray_ok = False
        self._on_tray_change: Callable[[bool], None] | None = None

    # ---- helpers ----------------------------------------------------------------------

    def _run(self, coro: Coroutine[Any, Any, T], timeout: float = 5) -> T:
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout)

    def _pystray_menu(self) -> Any:
        import pystray

        items = []
        for item in self._menu:
            if item.separator:
                items.append(pystray.Menu.SEPARATOR)
                continue
            cb = item.callback

            def action(icon: Any, _item: Any, cb: Callable[[], None] | None = cb) -> None:
                if cb is not None:
                    threading.Thread(target=cb, daemon=True).start()

            items.append(pystray.MenuItem(item.label, action, enabled=item.enabled))
        return pystray.Menu(*items)

    # ---- UiBackend -----------------------------------------------------------------------

    def start(self, on_tray_change: Callable[[bool], None] | None = None) -> None:
        self._on_tray_change = on_tray_change
        try:
            import pystray

            self._icon = pystray.Icon(
                "etabli-captation",
                make_icon_image(COLOURS["disarmed"]),
                self._tooltip,
                menu=self._pystray_menu(),
            )
            self._tray_ok = True
        except Exception:
            log.warning("tray: pystray unavailable")
            self._tray_ok = False
        self._loop_thread.start()
        try:
            self._run(self._setup_notifier())
        except Exception:
            log.warning("notify: desktop-notifier unavailable")

    async def _setup_notifier(self) -> None:
        from desktop_notifier import DesktopNotifier

        self._notifier = DesktopNotifier(app_name=self.app_name)
        caps = await self._notifier.get_capabilities()
        names = {str(getattr(c, "name", c)).lower() for c in caps}
        self._capabilities = {"body"} | ({"actions"} if "buttons" in names else set())

    def tray_available(self) -> bool:
        return self._tray_ok and self._icon is not None

    def notification_capabilities(self) -> set[str]:
        return set(self._capabilities)

    def set_state(self, state: TrayState, tooltip_title: str, tooltip_body: str) -> None:
        self.state = state
        self._tooltip = f"{tooltip_title}\n{tooltip_body}"
        if self._icon is not None:
            self._icon.icon = make_icon_image(COLOURS[state])
            self._icon.title = self._tooltip[:127]  # Windows tooltip limit

    def set_menu(self, items: list[MenuItem]) -> None:
        self._menu = list(items)
        if self._icon is not None:
            self._icon.menu = self._pystray_menu()
            self._icon.update_menu()

    def set_blink(self, on: bool) -> None:
        if on and not self._blink:
            self._blink = True
            self._blink_stop.clear()
            threading.Thread(target=self._blink_loop, daemon=True).start()
        elif not on and self._blink:
            self._blink = False
            self._blink_stop.set()
            self.set_state(self.state, self._tooltip.split("\n")[0], self._tooltip.split("\n")[-1])

    def _blink_loop(self) -> None:
        phase = False
        while not self._blink_stop.wait(0.5):
            phase = not phase
            if self._icon is not None:
                key = "attention" if phase else self.state
                self._icon.icon = make_icon_image(COLOURS[key])

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
        if self._notifier is None:
            return None
        from desktop_notifier import Button, Urgency

        def pressed(key: str) -> Callable[[], None] | None:
            if on_action is None:
                return None
            handler = on_action
            return lambda: handler(key)

        buttons = [Button(title=label, on_pressed=pressed(key)) for key, label in (actions or [])]

        async def send() -> Any:
            return await self._notifier.send(
                title=summary,
                message=body,
                buttons=buttons,
                urgency=Urgency.Critical if critical else Urgency.Normal,
                on_dismissed=on_closed,
            )

        try:
            handle = self._run(send())
        except Exception:
            log.warning("notify: sending failed")
            return None
        self._next_id += 1
        self._handles[self._next_id] = handle
        return self._next_id

    def close_notification(self, nid: int) -> None:
        handle = self._handles.pop(nid, None)
        if handle is None or self._notifier is None:
            return
        try:
            self._run(self._notifier.clear(handle))
        except Exception:
            log.warning("notify: could not close a notification")

    def play_sound(self) -> None:
        """Platform modules override this with a native sound."""
        return None

    def ask_text(self, title: str, prompt: str) -> str | None:
        """Platform modules override this with a native input dialog."""
        return None

    def run_main_loop(self, stop: threading.Event) -> None:
        if self._icon is None:
            stop.wait()
            return

        def watcher(icon: Any) -> None:
            icon.visible = True
            stop.wait()
            icon.stop()

        self._icon.run(
            setup=lambda icon: threading.Thread(target=watcher, args=(icon,), daemon=True).start()
        )

    def stop(self) -> None:
        if self._icon is not None:
            with contextlib.suppress(Exception):
                self._icon.stop()
        self._loop.call_soon_threadsafe(self._loop.stop)
