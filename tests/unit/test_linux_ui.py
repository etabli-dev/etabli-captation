"""Linux tray + notifications against a stub desktop on a private D-Bus (stage 09)."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

import pytest

pytest.importorskip("dbus_fast", reason="Linux-only dependency")

from captation.ui.i18n import STRINGS, Strings
from captation.ui.linux import LinuxUi, circle_pixmap
from captation.ui.notify import ConsentUi, UiHandlers
from tests.dbus_stub import StubDesktop, stub_desktop

pytestmark = pytest.mark.linux


def wait_for(pred: Callable[[], bool], timeout: float = 3) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


def make_ui(desk: StubDesktop, calls: list[str]) -> ConsentUi:
    handlers = UiHandlers(
        arm=lambda: calls.append("arm"),
        disarm=lambda: calls.append("disarm"),
        stop=lambda: calls.append("stop"),
        cancel=lambda: calls.append("cancel"),
    )
    ui = ConsentUi(LinuxUi(bus_address=desk.address), Strings("de"), handlers)
    ui.start()
    return ui


@pytest.mark.req("R-18", "R-21")
def test_tray_registers_and_shows_state() -> None:
    with stub_desktop() as desk:
        calls: list[str] = []
        ui = make_ui(desk, calls)
        try:
            assert ui.tray_available()
            name = desk.watcher.items[0]
            assert name.startswith("org.kde.StatusNotifierItem-")
            sni = ("/StatusNotifierItem", "org.kde.StatusNotifierItem")
            assert desk.get_prop(name, *sni, "Status") == "Active"
            ui.show_state(
                "recording", machine_state="RECORDING", mode="video", app_id="zoom", elapsed_s=65
            )
            tooltip = desk.get_prop(name, *sni, "ToolTip")
            assert (
                "AUFNAHME" in tooltip[2] + tooltip[3]
                and "zoom" in tooltip[3]
                and "0:01:05" in tooltip[3]
            )
            assert desk.get_prop(name, *sni, "Status") == "NeedsAttention"
            pix = desk.get_prop(name, *sni, "IconPixmap")
            assert [p[0] for p in pix] == [16, 22, 32]
            assert pix[2][2][4 * (16 * 32 + 16) : 4 * (16 * 32 + 16) + 4] == bytes(
                (0xFF, 0xD0, 0x10, 0x10)
            )
        finally:
            ui.stop()


@pytest.mark.req("R-18", "R-21")
def test_menu_layout_and_clicks() -> None:
    with stub_desktop() as desk:
        calls: list[str] = []
        ui = make_ui(desk, calls)
        try:
            name = desk.watcher.items[0]
            ui.show_state("armed", machine_state="COUNTDOWN", app_id="zoom", countdown_s=5)
            _rev, root = desk.call(
                name, "/MenuBar", "com.canonical.dbusmenu", "GetLayout", "iias", [0, -1, []]
            )
            labels = [
                c.value[1].get("label").value if "label" in c.value[1] else "-" for c in root[2]
            ]
            assert labels[2:] == [
                "Studienmodus aktivieren…",
                "Start abbrechen",
                "Aufnahme beenden",
                "Studienmodus beenden",
            ]
            enabled = {
                c.value[1]["label"].value: c.value[1]["enabled"].value
                for c in root[2]
                if "label" in c.value[1]
            }
            assert enabled["Start abbrechen"] and not enabled["Studienmodus aktivieren…"]
            from dbus_fast import Variant

            desk.call(
                name,
                "/MenuBar",
                "com.canonical.dbusmenu",
                "Event",
                "isvu",
                [4, "clicked", Variant("s", ""), 0],
            )
            assert wait_for(lambda: calls == ["cancel"])
            desk.call(
                name,
                "/MenuBar",
                "com.canonical.dbusmenu",
                "Event",
                "isvu",
                [3, "clicked", Variant("s", ""), 0],
            )
            time.sleep(0.2)
            assert calls == ["cancel"]  # disabled item: ignored
        finally:
            ui.stop()


@pytest.mark.req("R-17", "R-21")
def test_countdown_level_1_cancel_via_notification_action() -> None:
    with stub_desktop(caps=["actions", "body"]) as desk:
        calls: list[str] = []
        ui = make_ui(desk, calls)
        try:
            assert ui.countdown("signal", 5) == 1
            sent = desk.notify.sent[-1]
            assert sent["actions"] == ["cancel", "Abbrechen"] and "signal" in sent["body"]
            desk.invoke_action(len(desk.notify.sent), "cancel")
            assert wait_for(lambda: calls == ["cancel"])
            ui.close_countdown()
            assert desk.notify.closed == [len(desk.notify.sent)]
        finally:
            ui.stop()


@pytest.mark.req("R-21")
def test_countdown_level_2_without_actions(caplog: pytest.LogCaptureFixture) -> None:
    with stub_desktop(caps=["body"]) as desk:
        ui = make_ui(desk, [])
        try:
            assert ui.countdown("signal", 5) == 2
            assert desk.notify.sent[-1]["actions"] == []
            assert "fallback level 2" in caplog.text
        finally:
            ui.stop()


@pytest.mark.req("R-21")
def test_countdown_level_3_without_notification_service(caplog: pytest.LogCaptureFixture) -> None:
    with stub_desktop(notifications=False) as desk:
        ui = make_ui(desk, [])
        played: list[bool] = []
        ui.backend.play_sound = lambda: played.append(True)  # type: ignore[method-assign]
        try:
            assert ui.countdown("signal", 5) == 3
            assert played == [True]
            assert "tray blink and sound" in caplog.text
            ui.close_countdown()
        finally:
            ui.stop()


@pytest.mark.req("R-21")
def test_no_tray_host_means_unavailable() -> None:
    with stub_desktop(tray=False) as desk:
        ui = make_ui(desk, [])
        try:
            assert ui.tray_available() is False
        finally:
            ui.stop()
    with stub_desktop() as desk:
        desk.watcher.host = False
        ui = make_ui(desk, [])
        try:
            assert ui.tray_available() is False
        finally:
            ui.stop()


@pytest.mark.req("R-19")
def test_reminder_and_sound() -> None:
    with stub_desktop() as desk:
        ui = make_ui(desk, [])
        played: list[bool] = []
        ui.backend.play_sound = lambda: played.append(True)  # type: ignore[method-assign]
        try:
            ui.recording_started()
            assert played == [True]
            assert desk.notify.sent[-1]["body"].startswith(
                "Aufnahme läuft – bitte die Einwilligung"
            )
            ui.reminder_text = "Eigener Text"
            ui.recording_started()
            assert desk.notify.sent[-1]["body"] == "Eigener Text"
        finally:
            ui.stop()


def test_string_tables_are_complete() -> None:
    assert set(STRINGS["de"]) == set(STRINGS["en"])
    t = Strings("en")
    assert t("countdown.title", seconds=5) == "Recording starts in 5 s"


def test_pixmap_geometry() -> None:
    w, h, data = circle_pixmap(22, (1, 2, 3))
    assert (w, h, len(data)) == (22, 22, 22 * 22 * 4)
    assert data[:4] == bytes(4)  # transparent corner


def test_blink_toggles() -> None:
    with stub_desktop() as desk:
        ui = make_ui(desk, [])
        try:
            ui.backend.set_blink(True)
            time.sleep(0.7)
            ui.backend.set_blink(False)
            assert threading.active_count() >= 1
        finally:
            ui.stop()
