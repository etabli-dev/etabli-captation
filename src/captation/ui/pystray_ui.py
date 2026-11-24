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
import time
from collections.abc import Callable, Coroutine
from typing import Any, TypeVar

from captation.ui.base import MenuItem, TrayState

log = logging.getLogger(__name__)
T = TypeVar("T")
AUTH_CHECK_S = 30.0  # re-check of the notification permission (users can revoke it)

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


def _menu_action(cb: Callable[[], None] | None) -> Callable[[Any, Any], None]:
    """A pystray action ``(icon, item)``. pystray rejects callables with more than two
    positional parameters, so the callback is bound by closure, not by a default argument."""

    def action(icon: Any, item: Any) -> None:
        if cb is not None:
            threading.Thread(target=cb, daemon=True).start()

    return action


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
        self._auth_request: Any = None  # pending permission request (kept referenced)
        self._capabilities: set[str] = set()
        self._authorised = False
        self._authorised_at = 0.0
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
            items.append(
                pystray.MenuItem(item.label, _menu_action(item.callback), enabled=item.enabled)
            )
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
            # Not yet visible: only the main loop shows the icon (run_main_loop).
            self._tray_ok = False
        except Exception:
            log.warning("tray: pystray unavailable")
            self._tray_ok = False
        self._loop_thread.start()
        try:
            self._run(self._setup_notifier())
        except Exception as exc:
            # Type and message only: OS/library errors here carry no personal data.
            log.warning(
                "notify: desktop-notifier unavailable (%s: %s)", type(exc).__name__, str(exc)[:200]
            )

    async def _setup_notifier(self) -> None:
        from desktop_notifier import DesktopNotifier

        self._notifier = DesktopNotifier(app_name=self.app_name)
        caps = await self._notifier.get_capabilities()
        names = {str(getattr(c, "name", c)).lower() for c in caps}
        self._capabilities = {"body"} | ({"actions"} if "buttons" in names else set())
        # Ask for the permission now (the OS shows its prompt once), not at the first countdown:
        # an unanswered prompt would turn that countdown into the fallback (§2.8). Not awaited.
        self._auth_request = asyncio.ensure_future(self._request_authorisation())

    async def _request_authorisation(self) -> None:
        try:
            if self._notifier is not None:
                await self._notifier.request_authorisation()
        except Exception:
            log.warning("notify: permission request failed")

    def tray_available(self) -> bool:
        return self._tray_ok and self._icon is not None and self._icon_visible()

    def _icon_visible(self) -> bool:
        """Platform back ends override this with a check of the real menu-bar item."""
        return True

    def _ui(self, fn: Callable[[], None]) -> None:
        """Run a change of the tray item; macOS marshals it to the main thread (AppKit)."""
        fn()

    def notification_capabilities(self) -> set[str]:
        """Empty while the OS denies notifications for this app (checked at most every
        ``AUTH_CHECK_S``): the countdown then uses the full fallback (§2.8)."""
        if not self._capabilities:
            return set()
        check = getattr(self._notifier, "has_authorisation", None)
        if check is None:
            return set(self._capabilities)  # back end without a permission model
        if self._authorised_at == 0.0 or time.monotonic() - self._authorised_at > AUTH_CHECK_S:
            try:
                self._authorised = bool(self._run(check()))
            except Exception:
                self._authorised = False
            self._authorised_at = time.monotonic()
        return set(self._capabilities) if self._authorised else set()

    def set_state(self, state: TrayState, tooltip_title: str, tooltip_body: str) -> None:
        self.state = state
        self._tooltip = f"{tooltip_title}\n{tooltip_body}"
        icon, image, title = self._icon, make_icon_image(COLOURS[state]), self._tooltip[:127]
        if icon is not None:

            def apply() -> None:
                icon.icon = image
                icon.title = title  # Windows tooltip limit

            self._ui(apply)

    def set_menu(self, items: list[MenuItem]) -> None:
        self._menu = list(items)
        icon, menu = self._icon, self._pystray_menu()
        if icon is not None:

            def apply() -> None:
                icon.menu = menu
                icon.update_menu()

            self._ui(apply)

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
            icon = self._icon
            if icon is not None:
                key = "attention" if phase else self.state
                image = make_icon_image(COLOURS[key])

                def apply(icon: Any = icon, image: Any = image) -> None:
                    icon.icon = image

                self._ui(apply)

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

    def run_main_loop(
        self, stop: threading.Event, on_terminate: Callable[[], None] | None = None
    ) -> None:
        if self._icon is None:
            stop.wait()
            return

        def watcher(icon: Any) -> None:
            def show() -> None:
                icon.visible = True
                self._set_tray(bool(getattr(icon, "visible", True)))

            self._ui(show)
            self._on_loop_running(stop, on_terminate)
            stop.wait()
            self._set_tray(False)
            icon.stop()

        self._icon.run(
            setup=lambda icon: threading.Thread(target=watcher, args=(icon,), daemon=True).start()
        )

    def _set_tray(self, ok: bool) -> None:
        changed = ok != self._tray_ok
        self._tray_ok = ok
        if changed and self._on_tray_change is not None:
            with contextlib.suppress(Exception):
                self._on_tray_change(ok)

    def _on_loop_running(
        self, stop: threading.Event, on_terminate: Callable[[], None] | None
    ) -> None:
        """Hook for platform back ends once the tray run loop runs (macOS: signals)."""
        return None

    def open_menu(self) -> bool:
        """Generic pystray cannot open its menu programmatically (macOS overrides this)."""
        return False

    def stop(self) -> None:
        if self._icon is not None:
            with contextlib.suppress(Exception):
                self._icon.stop()
        self._loop.call_soon_threadsafe(self._loop.stop)
