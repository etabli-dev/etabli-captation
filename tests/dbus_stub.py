"""A private D-Bus session with a stub tray host and notification server (tests only)."""

import asyncio
import os
import shutil
import subprocess
import threading
from collections.abc import Coroutine, Iterator
from contextlib import contextmanager
from typing import Annotated, Any, TypeVar

from dbus_fast import Message, MessageType, PropertyAccess, Variant
from dbus_fast.aio import MessageBus
from dbus_fast.annotations import DBusSignature
from dbus_fast.service import ServiceInterface, dbus_property, method, signal

T = TypeVar("T")

# No <servicedir>: the private bus must never D-Bus-activate real desktop services (document
# portal, gvfs, …). Activating xdg-document-portal on a throwaway bus unmounts the user's
# /run/user/<uid>/doc when that bus goes away, which breaks every Flatpak app.
BUS_CONFIG = """<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <type>session</type>
  <listen>unix:tmpdir=/tmp</listen>
  <auth>EXTERNAL</auth>
  <policy context="default">
    <allow send_destination="*" eavesdrop="true"/>
    <allow eavesdrop="true"/>
    <allow own="*"/>
  </policy>
</busconfig>
"""
Str = Annotated[str, DBusSignature("s")]
UInt = Annotated[int, DBusSignature("u")]
Bool = Annotated[bool, DBusSignature("b")]
StrList = Annotated[list[str], DBusSignature("as")]


class Watcher(ServiceInterface):
    def __init__(self) -> None:
        super().__init__("org.kde.StatusNotifierWatcher")
        self.items: list[str] = []
        self.host = True

    @method()
    def RegisterStatusNotifierItem(self, service: Str) -> None:
        if service not in self.items:
            self.items.append(service)

    @dbus_property(access=PropertyAccess.READ)
    def IsStatusNotifierHostRegistered(self) -> Bool:
        return self.host


class Notifications(ServiceInterface):
    def __init__(self, caps: list[str]) -> None:
        super().__init__("org.freedesktop.Notifications")
        self.caps = caps
        self.sent: list[dict[str, Any]] = []
        self.closed: list[int] = []

    @method()
    def GetCapabilities(self) -> StrList:
        return self.caps

    @method()
    def Notify(
        self,
        app: Str,
        replaces: UInt,
        icon: Str,
        summary: Str,
        body: Str,
        actions: StrList,
        hints: Annotated[dict[str, Variant], DBusSignature("a{sv}")],
        timeout: Annotated[int, DBusSignature("i")],
    ) -> UInt:
        self.sent.append({"summary": summary, "body": body, "actions": list(actions)})
        return len(self.sent)

    @method()
    def CloseNotification(self, nid: UInt) -> None:
        self.closed.append(nid)

    @signal()
    def ActionInvoked(self) -> Annotated[list[Any], DBusSignature("us")]:
        return [self._pending[0], self._pending[1]]

    def invoke(self, nid: int, key: str) -> None:
        self._pending = (nid, key)
        self.ActionInvoked()


class StubDesktop:
    def __init__(
        self, *, tray: bool = True, caps: list[str] | None = None, notifications: bool = True
    ) -> None:
        self.tray = tray
        self.notifications = notifications
        self.watcher = Watcher()
        self.notify = Notifications(caps if caps is not None else ["actions", "body"])
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.bus: MessageBus | None = None
        self.address = ""
        self.daemon: subprocess.Popen[str] | None = None

    def run(self, coro: Coroutine[Any, Any, T]) -> T:
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(5)

    def start(self) -> None:
        import tempfile

        self._conf = tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False)  # noqa: SIM115
        self._conf.write(BUS_CONFIG)
        self._conf.close()
        self.daemon = subprocess.Popen(
            ["dbus-daemon", f"--config-file={self._conf.name}", "--nofork", "--print-address=1"],
            stdout=subprocess.PIPE,
            text=True,
        )
        assert self.daemon.stdout is not None
        self.address = self.daemon.stdout.readline().strip()
        self.thread.start()
        self.run(self._start())

    async def _start(self) -> None:
        self.bus = await MessageBus(bus_address=self.address).connect()
        if self.tray:
            self.bus.export("/StatusNotifierWatcher", self.watcher)
            await self.bus.request_name("org.kde.StatusNotifierWatcher")
        if self.notifications:
            self.bus.export("/org/freedesktop/Notifications", self.notify)
            await self.bus.request_name("org.freedesktop.Notifications")

    def call(
        self,
        dest: str,
        path: str,
        iface: str,
        member: str,
        sig: str = "",
        body: list[Any] | None = None,
    ) -> Any:
        async def go() -> Any:
            assert self.bus is not None
            reply = await self.bus.call(
                Message(
                    destination=dest,
                    path=path,
                    interface=iface,
                    member=member,
                    signature=sig,
                    body=body or [],
                )
            )
            assert reply is not None and reply.message_type == MessageType.METHOD_RETURN, reply
            return reply.body

        return self.run(go())

    def get_prop(self, dest: str, path: str, iface: str, name: str) -> Any:
        return self.call(dest, path, "org.freedesktop.DBus.Properties", "Get", "ss", [iface, name])[
            0
        ].value

    def invoke_action(self, nid: int, key: str) -> None:
        self.loop.call_soon_threadsafe(self.notify.invoke, nid, key)

    def stop(self) -> None:
        if self.bus is not None:
            self.loop.call_soon_threadsafe(self.bus.disconnect)
        self.loop.call_soon_threadsafe(self.loop.stop)
        if self.daemon is not None:
            self.daemon.terminate()
            self.daemon.wait(5)
        if getattr(self, "_conf", None) is not None:
            os.unlink(self._conf.name)


@contextmanager
def stub_desktop(**kw: Any) -> Iterator[StubDesktop]:
    if not shutil.which("dbus-daemon") or os.name != "posix":
        import pytest

        pytest.skip("needs dbus-daemon")
    desk = StubDesktop(**kw)
    desk.start()
    try:
        yield desk
    finally:
        desk.stop()
