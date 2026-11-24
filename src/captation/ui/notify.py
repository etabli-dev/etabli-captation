"""Consent UI (docs/principles.md §2.4–§2.8): countdown with cancel, recording indicator, reminder.

The countdown uses the mandatory fallback chain, which has no config switch:
1. notification with a *Cancel* action;
2. notification without action + tray menu "Start abbrechen";
3. tray blink + sound + tray menu cancel.
Every fallback step is logged as a warning. The tray menu cancel is enabled during every
countdown in addition. Without a tray, arming is refused by the agent (not here).
"""

from __future__ import annotations

import contextlib
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass

from captation.models import Mode
from captation.ui.base import MenuItem, TrayState, UiBackend
from captation.ui.i18n import GLYPHS, Strings

log = logging.getLogger(__name__)

CANCEL_ACTION = "cancel"
DISARM_ACTION = "disarm"


@dataclass
class UiHandlers:
    arm: Callable[[], None]
    disarm: Callable[[], None]
    stop: Callable[[], None]
    cancel: Callable[[], None]


def _fmt_elapsed(seconds: float) -> str:
    s = int(max(0, seconds))
    return f"{s // 3600:d}:{s % 3600 // 60:02d}:{s % 60:02d}"


class ConsentUi:
    def __init__(
        self,
        backend: UiBackend,
        strings: Strings,
        handlers: UiHandlers,
        *,
        sound_on_start: bool = True,
        reminder_text: str | None = None,
    ) -> None:
        self.backend = backend
        self.t = strings
        self.handlers = handlers
        self.sound_on_start = sound_on_start
        self.reminder_text = reminder_text
        self.tray_state: TrayState = "disarmed"
        self.machine_state = "DISARMED"
        self.countdown_level: int | None = None
        self._countdown_nid: int | None = None
        self._lock = threading.Lock()
        self.on_tray_change: Callable[[bool], None] | None = None

    # ---- lifecycle -------------------------------------------------------------------------

    def start(self) -> None:
        self.backend.start(self._tray_changed)
        self.show_state("disarmed", machine_state="DISARMED")
        self.backend.set_menu(self._menu())

    def _tray_changed(self, available: bool) -> None:
        if self.on_tray_change is not None:
            self.on_tray_change(available)

    def tray_available(self) -> bool:
        return self.backend.tray_available()

    def stop(self) -> None:
        self.backend.stop()

    # ---- indicator ------------------------------------------------------------------------

    def _menu(self) -> list[MenuItem]:
        st = self.machine_state
        armed = st != "DISARMED"
        recording = st in ("RECORDING", "PENDING_STOP")
        return [
            MenuItem(self.t("menu.status", state=self.t(f"state.{st}")), enabled=False),
            MenuItem("", separator=True),
            MenuItem(self.t("menu.arm"), self.handlers.arm, enabled=not armed),
            MenuItem(self.t("menu.cancel"), self.handlers.cancel, enabled=st == "COUNTDOWN"),
            MenuItem(self.t("menu.stop"), self.handlers.stop, enabled=recording),
            MenuItem(self.t("menu.disarm"), self.handlers.disarm, enabled=armed),
        ]

    def show_state(
        self,
        state: TrayState,
        *,
        machine_state: str,
        mode: Mode | None = None,
        app_id: str | None = None,
        remaining_s: float | None = None,
        elapsed_s: float | None = None,
        countdown_s: int | None = None,
    ) -> None:
        """Update icon, tooltip and menu. The indicator cannot be hidden (§2.5)."""
        with self._lock:
            self.tray_state = state
            changed_menu = machine_state != self.machine_state
            self.machine_state = machine_state
        if state == "recording":
            body = self.t(
                "tray.recording",
                glyph=GLYPHS.get(mode or "audio", ""),
                app=app_id or "?",
                elapsed=_fmt_elapsed(elapsed_s or 0),
            )
        elif machine_state == "COUNTDOWN":
            body = self.t("tray.countdown", seconds=countdown_s or 0, app=app_id or "?")
        elif state == "armed" and remaining_s is None:
            body = self.t("tray.armed_open")  # no time limit (ADR-021)
        elif state == "armed":
            body = self.t("tray.armed", minutes=int((remaining_s or 0) // 60))
        else:
            body = self.t("tray.disarmed")
        self.backend.set_state(state, self.t("app.title"), body)
        if changed_menu:
            self.backend.set_menu(self._menu())

    # ---- countdown ------------------------------------------------------------------------

    def countdown(
        self,
        app_id: str,
        seconds: int,
        *,
        live: bool = False,
        on_cancel: Callable[[], None] | None = None,
    ) -> int:
        """Show the countdown; returns the fallback level that was used (1–3). ``live``
        (ADR-022): the recording already runs, *Cancel* deletes it."""
        self.close_countdown()
        caps = self.backend.notification_capabilities()
        suffix = "_live" if live else ""
        title = self.t(f"countdown.title{suffix}", seconds=seconds)
        body = self.t(f"countdown.body{suffix}", app=app_id)
        nid: int | None = None
        if "actions" in caps:
            nid = self.backend.notify(
                title,
                body,
                actions=[(CANCEL_ACTION, self.t("countdown.cancel"))],
                on_action=lambda key: (
                    (on_cancel or self.handlers.cancel)() if key == CANCEL_ACTION else None
                ),
                critical=True,
            )
            level = 1 if nid is not None else 3
        else:
            nid = self.backend.notify(title, body, critical=True)
            level = 2 if nid is not None else 3
        if level >= 2:
            log.warning("countdown: fallback level %d (notification actions unavailable)", level)
        if level == 3:
            log.warning("countdown: notifications unavailable, tray blink and sound")
        # Delivery cannot be verified (the OS may drop or hide a notification it accepted, e.g.
        # in Focus mode), so every countdown also blinks the tray and plays the sound (§2.8).
        self.backend.set_blink(True)
        self.backend.play_sound()
        self._countdown_nid = nid
        self.countdown_level = level
        self.backend.set_menu(self._menu())
        return level

    def _on_action(self, key: str) -> None:
        if key == CANCEL_ACTION:
            self.handlers.cancel()
        elif key == DISARM_ACTION:
            self.handlers.disarm()

    def still_armed(self) -> None:
        """ADR-021: study mode without a time limit reminds the user regularly, with a button
        that ends it (the tray menu does the same)."""
        title, body = self.t("still_armed.title"), self.t("still_armed.body")
        if "actions" in self.backend.notification_capabilities():
            nid = self.backend.notify(
                title,
                body,
                actions=[(DISARM_ACTION, self.t("still_armed.end"))],
                on_action=self._on_action,
            )
        else:
            nid = self.backend.notify(title, body)
        if nid is None:
            log.warning("still-armed reminder: notification unavailable, sound instead")
            self.backend.play_sound()

    def close_countdown(self) -> None:
        if self._countdown_nid is not None:
            self.backend.close_notification(self._countdown_nid)
            self._countdown_nid = None
        if self.countdown_level is not None:
            self.backend.set_blink(False)
        self.countdown_level = None

    # ---- notices ---------------------------------------------------------------------------

    def recording_started(self) -> None:
        text = self.reminder_text or self.t("reminder.default")
        shown = "body" in self.backend.notification_capabilities() and (
            self.backend.notify(self.t("reminder.title"), text, critical=True) is not None
        )
        if not shown:
            log.warning("reminder: notification unavailable, tray state and sound instead")
        if self.sound_on_start or not shown:
            self.backend.play_sound()  # `sound_on_start: false` never silences the fallback

    def show(self) -> None:
        """The user opened the app: show the menu, or say where it is."""
        opened = False
        with contextlib.suppress(Exception):
            opened = self.backend.open_menu()
        if not opened:
            self.notice("app.title", "notify.running_in_background")

    def notice(self, title_key: str, body_key: str, **values: object) -> None:
        if self.backend.notify(self.t(title_key), self.t(body_key, **values)) is None:
            log.warning("notice %s could not be shown", body_key)

    def ask_consent_ref(self) -> str | None:
        return self.backend.ask_text(self.t("ask_ref.title"), self.t("ask_ref.prompt"))
