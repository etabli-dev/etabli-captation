"""Linux UI back end (ADR-003): StatusNotifierItem + com.canonical.dbusmenu tray and
org.freedesktop.Notifications, on dbus-fast in a private asyncio thread.

No GTK/PyGObject. Tray availability = a registered StatusNotifierWatcher with a host
(GNOME: the AppIndicator extension; KDE: built in).
"""

import sys

# Linux-only module (dbus-fast is a Linux-only dependency). The assert also makes mypy skip the
# rest of the module when it checks for macOS or Windows.
assert sys.platform == "linux"

import asyncio  # noqa: E402
import contextlib  # noqa: E402
import functools  # noqa: E402
import logging  # noqa: E402
import os  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
import threading  # noqa: E402
from collections.abc import Callable, Coroutine  # noqa: E402
from typing import Annotated, Any, TypeVar  # noqa: E402

from dbus_fast import BusType, Message, MessageType, PropertyAccess, Variant  # noqa: E402
from dbus_fast.aio import MessageBus  # noqa: E402
from dbus_fast.annotations import DBusSignature  # noqa: E402
from dbus_fast.service import ServiceInterface, dbus_property, method, signal  # noqa: E402

from captation.ui.base import MenuItem, TrayState  # noqa: E402

log = logging.getLogger(__name__)

T = TypeVar("T")
Pixmaps = Annotated[list[tuple[int, int, bytes]], DBusSignature("a(iiay)")]
ToolTip = Annotated[
    tuple[str, list[tuple[int, int, bytes]], str, str], DBusSignature("(sa(iiay)ss)")
]
Str = Annotated[str, DBusSignature("s")]
Int = Annotated[int, DBusSignature("i")]
UInt = Annotated[int, DBusSignature("u")]
Bool = Annotated[bool, DBusSignature("b")]
ObjPath = Annotated[str, DBusSignature("o")]
StrList = Annotated[list[str], DBusSignature("as")]
IntList = Annotated[list[int], DBusSignature("ai")]

SNI_IFACE = "org.kde.StatusNotifierItem"
SNI_PATH = "/StatusNotifierItem"
MENU_IFACE = "com.canonical.dbusmenu"
MENU_PATH = "/MenuBar"
WATCHER_NAME = "org.kde.StatusNotifierWatcher"
WATCHER_PATH = "/StatusNotifierWatcher"
NOTIFY_NAME = "org.freedesktop.Notifications"
NOTIFY_PATH = "/org/freedesktop/Notifications"
SOUND_FILE = "/usr/share/sounds/freedesktop/stereo/message-new-instant.oga"

COLOURS = {
    "disarmed": (0x80, 0x80, 0x80),
    "armed": (0xE0, 0xA8, 0x00),
    "recording": (0xD0, 0x10, 0x10),
    "attention": (0xFF, 0xFF, 0xFF),
}


@functools.lru_cache(maxsize=32)
def circle_pixmap(size: int, rgb: tuple[int, int, int]) -> tuple[int, int, bytes]:
    """ARGB32 (network byte order) filled circle with a 1 px dark rim."""
    r, g, b = rgb
    data = bytearray()
    c = (size - 1) / 2
    radius = size / 2 - 1
    for y in range(size):
        for x in range(size):
            d = ((x - c) ** 2 + (y - c) ** 2) ** 0.5
            if d <= radius - 1:
                data += bytes((0xFF, r, g, b))
            elif d <= radius:
                data += bytes((0xFF, 0x20, 0x20, 0x20))
            else:
                data += bytes((0, 0, 0, 0))
    return (size, size, bytes(data))


class StatusNotifierItem(ServiceInterface):
    def __init__(self, owner: "LinuxUi") -> None:
        super().__init__(SNI_IFACE)
        self._owner = owner

    @dbus_property(access=PropertyAccess.READ)
    def Category(self) -> Str:
        return "ApplicationStatus"

    @dbus_property(access=PropertyAccess.READ)
    def Id(self) -> Str:
        return "etabli-captation"

    @dbus_property(access=PropertyAccess.READ)
    def Title(self) -> Str:
        return self._owner.title

    @dbus_property(access=PropertyAccess.READ)
    def Status(self) -> Str:
        return "NeedsAttention" if self._owner.state == "recording" else "Active"

    @dbus_property(access=PropertyAccess.READ)
    def WindowId(self) -> Int:
        return 0

    @dbus_property(access=PropertyAccess.READ)
    def IconName(self) -> Str:
        return ""

    @dbus_property(access=PropertyAccess.READ)
    def IconPixmap(self) -> Pixmaps:
        return self._owner.pixmaps()

    @dbus_property(access=PropertyAccess.READ)
    def AttentionIconName(self) -> Str:
        return ""

    @dbus_property(access=PropertyAccess.READ)
    def AttentionIconPixmap(self) -> Pixmaps:
        return self._owner.pixmaps()

    @dbus_property(access=PropertyAccess.READ)
    def ToolTip(self) -> ToolTip:
        return ("", [], self._owner.tooltip_title, self._owner.tooltip_body)

    @dbus_property(access=PropertyAccess.READ)
    def ItemIsMenu(self) -> Bool:
        return True

    @dbus_property(access=PropertyAccess.READ)
    def Menu(self) -> ObjPath:
        return MENU_PATH

    @method()
    def ContextMenu(self, x: Int, y: Int) -> None:
        return None

    @method()
    def Activate(self, x: Int, y: Int) -> None:
        return None

    @method()
    def SecondaryActivate(self, x: Int, y: Int) -> None:
        return None

    @method()
    def Scroll(self, delta: Int, orientation: Str) -> None:
        return None

    @signal()
    def NewIcon(self) -> None:
        return None

    @signal()
    def NewAttentionIcon(self) -> None:
        return None

    @signal()
    def NewToolTip(self) -> None:
        return None

    @signal()
    def NewTitle(self) -> None:
        return None

    @signal()
    def NewStatus(self) -> Str:
        return "NeedsAttention" if self._owner.state == "recording" else "Active"


class DbusMenu(ServiceInterface):
    def __init__(self, owner: "LinuxUi") -> None:
        super().__init__(MENU_IFACE)
        self._owner = owner
        self.revision = 1

    def _item(self, idx: int, item: MenuItem) -> list[Any]:
        props: dict[str, Variant] = {
            "label": Variant("s", item.label),
            "enabled": Variant("b", item.enabled),
            "visible": Variant("b", True),
        }
        if item.separator:
            props = {"type": Variant("s", "separator")}
        return [idx, props, []]

    def layout(self) -> list[Any]:
        children = [
            Variant("(ia{sv}av)", self._item(i + 1, it)) for i, it in enumerate(self._owner.menu)
        ]
        root = [0, {"children-display": Variant("s", "submenu")}, children]
        return [self.revision, root]

    @method()
    def GetLayout(
        self, parent_id: Int, recursion_depth: Int, property_names: StrList
    ) -> Annotated[list[Any], DBusSignature("u(ia{sv}av)")]:
        return self.layout()

    @method()
    def GetGroupProperties(
        self, ids: IntList, property_names: StrList
    ) -> Annotated[list[Any], DBusSignature("a(ia{sv})")]:
        out = []
        for i in ids:
            if 1 <= i <= len(self._owner.menu):
                idx, props, _ = self._item(i, self._owner.menu[i - 1])
                out.append([idx, props])
        return out

    @method()
    def GetProperty(self, item_id: Int, name: Str) -> Annotated[Variant, DBusSignature("v")]:
        if 1 <= item_id <= len(self._owner.menu):
            props = self._item(item_id, self._owner.menu[item_id - 1])[1]
            value = props.get(name)
            if isinstance(value, Variant):
                return value
        return Variant("s", "")

    @method()
    def Event(
        self,
        item_id: Int,
        event_id: Str,
        data: Annotated[Variant, DBusSignature("v")],
        timestamp: UInt,
    ) -> None:
        if event_id == "clicked":
            self._owner.menu_clicked(item_id)

    @method()
    def EventGroup(self, events: Annotated[list[Any], DBusSignature("a(isvu)")]) -> IntList:
        for item_id, event_id, _data, _ts in events:
            if event_id == "clicked":
                self._owner.menu_clicked(item_id)
        return []

    @method()
    def AboutToShow(self, item_id: Int) -> Bool:
        return False

    @method()
    def AboutToShowGroup(self, ids: IntList) -> Annotated[list[Any], DBusSignature("aiai")]:
        return [[], []]

    @dbus_property(access=PropertyAccess.READ)
    def Version(self) -> UInt:
        return 3

    @dbus_property(access=PropertyAccess.READ)
    def TextDirection(self) -> Str:
        return "ltr"

    @dbus_property(access=PropertyAccess.READ)
    def Status(self) -> Str:
        return "normal"

    @dbus_property(access=PropertyAccess.READ)
    def IconThemePath(self) -> StrList:
        return []

    @signal()
    def LayoutUpdated(self) -> Annotated[list[Any], DBusSignature("ui")]:
        return [self.revision, 0]

    @signal()
    def ItemsPropertiesUpdated(self) -> Annotated[list[Any], DBusSignature("a(ia{sv})a(ias)")]:
        return [[], []]


class LinuxUi:
    """UiBackend for Linux desktops (see captation.ui.base.UiBackend)."""

    def __init__(self, bus_address: str | None = None) -> None:
        self._bus_address = bus_address
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, name="captation-dbus", daemon=True
        )
        self._bus: MessageBus | None = None
        self._sni: StatusNotifierItem | None = None
        self._menu_iface: DbusMenu | None = None
        self._name = f"org.kde.StatusNotifierItem-{os.getpid()}-1"
        self.state: TrayState = "disarmed"
        self.title = "Établi Captation"
        self.tooltip_title = self.title
        self.tooltip_body = ""
        self.menu: list[MenuItem] = []
        self._blink = False
        self._blink_phase = False
        self._blink_task: asyncio.Task[None] | None = None
        self._notify_iface: Any = None
        self._action_handlers: dict[int, Callable[[str], None]] = {}
        self._closed_handlers: dict[int, Callable[[], None]] = {}
        self._registered = False
        self._on_tray_change: Callable[[bool], None] | None = None

    # ---- loop plumbing -----------------------------------------------------------------

    def _run(self, coro: Coroutine[Any, Any, T], timeout: float = 5) -> T:
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout)

    def start(self, on_tray_change: Callable[[bool], None] | None = None) -> None:
        self._on_tray_change = on_tray_change
        self._thread.start()
        self._run(self._start())

    async def _start(self) -> None:
        if self._bus_address:
            bus = MessageBus(bus_address=self._bus_address)
        else:
            bus = MessageBus(bus_type=BusType.SESSION)
        self._bus = await bus.connect()
        self._sni = StatusNotifierItem(self)
        self._menu_iface = DbusMenu(self)
        self._bus.export(SNI_PATH, self._sni)
        self._bus.export(MENU_PATH, self._menu_iface)
        await self._bus.request_name(self._name)
        self._bus.add_message_handler(self._on_message)
        await self._add_match(
            "type='signal',interface='org.freedesktop.DBus',member='NameOwnerChanged',"
            f"arg0='{WATCHER_NAME}'"
        )
        await self._register()
        await self._setup_notifications()

    async def _add_match(self, rule: str) -> None:
        assert self._bus is not None
        await self._bus.call(
            Message(
                destination="org.freedesktop.DBus",
                path="/org/freedesktop/DBus",
                interface="org.freedesktop.DBus",
                member="AddMatch",
                signature="s",
                body=[rule],
            )
        )

    def _on_message(self, msg: Message) -> bool:
        if msg.message_type != MessageType.SIGNAL:
            return False
        if msg.member == "NameOwnerChanged" and msg.body and msg.body[0] == WATCHER_NAME:
            self._loop.create_task(self._register())
        elif msg.interface == NOTIFY_NAME and msg.member == "ActionInvoked":
            nid, key = int(msg.body[0]), str(msg.body[1])
            handler = self._action_handlers.get(nid)
            if handler is not None:
                handler(key)
        elif msg.interface == NOTIFY_NAME and msg.member == "NotificationClosed":
            nid = int(msg.body[0])
            self._action_handlers.pop(nid, None)
            closed = self._closed_handlers.pop(nid, None)
            if closed is not None:
                closed()
        return False

    async def _register(self) -> None:
        assert self._bus is not None
        before = self._registered
        try:
            reply = await self._bus.call(
                Message(
                    destination=WATCHER_NAME,
                    path=WATCHER_PATH,
                    interface=WATCHER_NAME,
                    member="RegisterStatusNotifierItem",
                    signature="s",
                    body=[self._name],
                )
            )
            self._registered = reply is not None and reply.message_type == MessageType.METHOD_RETURN
        except Exception:
            self._registered = False
        if self._registered and not await self._host_registered():
            self._registered = False
        if before != self._registered:
            log.info("tray: %s", "registered" if self._registered else "no tray host")
            if self._on_tray_change is not None:
                self._on_tray_change(self._registered)

    async def _host_registered(self) -> bool:
        assert self._bus is not None
        reply = await self._bus.call(
            Message(
                destination=WATCHER_NAME,
                path=WATCHER_PATH,
                interface="org.freedesktop.DBus.Properties",
                member="Get",
                signature="ss",
                body=[WATCHER_NAME, "IsStatusNotifierHostRegistered"],
            )
        )
        if reply is None or reply.message_type != MessageType.METHOD_RETURN:
            return False
        return bool(reply.body[0].value)

    async def _setup_notifications(self) -> None:
        assert self._bus is not None
        await self._add_match(f"type='signal',interface='{NOTIFY_NAME}'")
        reply = await self._bus.call(
            Message(
                destination=NOTIFY_NAME,
                path=NOTIFY_PATH,
                interface=NOTIFY_NAME,
                member="GetCapabilities",
            )
        )
        if reply is not None and reply.message_type == MessageType.METHOD_RETURN:
            self._capabilities = set(reply.body[0])
        else:
            self._capabilities = set()

    # ---- UiBackend -----------------------------------------------------------------------

    def tray_available(self) -> bool:
        try:
            self._run(self._register())
        except Exception:
            return False
        return self._registered

    def notification_capabilities(self) -> set[str]:
        return set(getattr(self, "_capabilities", set()))

    def pixmaps(self) -> list[tuple[int, int, bytes]]:
        key: str = self.state
        if self._blink and self._blink_phase:
            key = "attention"
        rgb = COLOURS[key]
        return [circle_pixmap(s, rgb) for s in (16, 22, 32)]

    def set_state(self, state: TrayState, tooltip_title: str, tooltip_body: str) -> None:
        self.state = state
        self.tooltip_title = tooltip_title
        self.tooltip_body = tooltip_body
        self._loop.call_soon_threadsafe(self._emit_all)

    def _emit_all(self) -> None:
        if self._sni is None:
            return
        self._sni.NewIcon()
        self._sni.NewAttentionIcon()
        self._sni.NewToolTip()
        self._sni.NewStatus()

    def set_menu(self, items: list[MenuItem]) -> None:
        self.menu = list(items)

        def emit() -> None:
            if self._menu_iface is not None:
                self._menu_iface.revision += 1
                self._menu_iface.LayoutUpdated()

        self._loop.call_soon_threadsafe(emit)

    def menu_clicked(self, item_id: int) -> None:
        if 1 <= item_id <= len(self.menu):
            item = self.menu[item_id - 1]
            if item.enabled and item.callback is not None:
                threading.Thread(target=item.callback, daemon=True).start()

    def set_blink(self, on: bool) -> None:
        def apply() -> None:
            self._blink = on
            if on and self._blink_task is None:
                self._blink_task = self._loop.create_task(self._blink_loop())
            elif not on and self._blink_task is not None:
                self._blink_task.cancel()
                self._blink_task = None
                self._blink_phase = False
                self._emit_all()

        self._loop.call_soon_threadsafe(apply)

    async def _blink_loop(self) -> None:
        while True:
            self._blink_phase = not self._blink_phase
            self._emit_all()
            await asyncio.sleep(0.5)

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
        flat: list[str] = []
        for key, label in actions or []:
            flat += [key, label]
        hints = {"urgency": Variant("y", 2 if critical else 1)}
        if critical:
            hints["resident"] = Variant("b", True)

        async def call() -> int | None:
            assert self._bus is not None
            reply = await self._bus.call(
                Message(
                    destination=NOTIFY_NAME,
                    path=NOTIFY_PATH,
                    interface=NOTIFY_NAME,
                    member="Notify",
                    signature="susssasa{sv}i",
                    body=["Établi Captation", replaces, "", summary, body, flat, hints, -1],
                )
            )
            if reply is None or reply.message_type != MessageType.METHOD_RETURN:
                return None
            return int(reply.body[0])

        try:
            nid = self._run(call())
        except Exception:
            log.warning("notify: notification service unavailable")
            return None
        if nid is not None:
            if on_action is not None:
                self._action_handlers[nid] = on_action
            if on_closed is not None:
                self._closed_handlers[nid] = on_closed
        return nid

    def close_notification(self, nid: int) -> None:
        async def call() -> None:
            assert self._bus is not None
            await self._bus.call(
                Message(
                    destination=NOTIFY_NAME,
                    path=NOTIFY_PATH,
                    interface=NOTIFY_NAME,
                    member="CloseNotification",
                    signature="u",
                    body=[nid],
                )
            )

        try:
            self._run(call())
        except Exception:
            log.warning("notify: could not close a notification")
        self._action_handlers.pop(nid, None)
        self._closed_handlers.pop(nid, None)

    def play_sound(self) -> None:
        player = shutil.which("pw-play")
        if player and os.path.exists(SOUND_FILE):
            subprocess.Popen(
                [player, SOUND_FILE],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )

    def ask_text(self, title: str, prompt: str) -> str | None:
        zenity = shutil.which("zenity")
        if not zenity:
            return None
        try:
            out = subprocess.run(
                [zenity, "--entry", f"--title={title}", f"--text={prompt}"],
                capture_output=True,
                text=True,
                timeout=300,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return None
        return out.stdout.strip() if out.returncode == 0 else None

    def run_main_loop(self, stop: threading.Event) -> None:
        stop.wait()

    def stop(self) -> None:
        async def shutdown() -> None:
            if self._bus is not None:
                self._bus.disconnect()

        with contextlib.suppress(Exception):
            self._run(shutdown(), timeout=2)
        self._loop.call_soon_threadsafe(self._loop.stop)
