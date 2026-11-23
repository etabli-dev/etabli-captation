"""IPC on Windows: the per-user pipe name, and a real named-pipe round trip on Windows."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest

from captation import ipc


def test_pipe_address_is_per_user_and_hides_the_name() -> None:
    a = ipc.pipe_address("DESKTOP-1", "Alice")
    assert a == ipc.pipe_address("desktop-1", "alice")  # Windows accounts: case-insensitive
    assert a != ipc.pipe_address("DESKTOP-1", "bob")
    assert a != ipc.pipe_address("OTHER", "alice")
    assert a.startswith("\\\\.\\pipe\\etabli-captation-") and len(a.rsplit("-", 1)[1]) == 16
    assert "alice" not in a.lower()


@pytest.mark.req("R-52")
@pytest.mark.windows
@pytest.mark.timeout(60)
def test_named_pipe_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ipc, "state_dir", lambda: ipc.ensure_private_dir(tmp_path / "state"))
    addr = ipc.pipe_address("TEST", f"captation-{threading.get_ident()}")
    calls: list[dict[str, Any]] = []

    def handler(msg: dict[str, Any]) -> dict[str, Any]:
        calls.append(msg)
        return {"ok": True, "state": "DISARMED"}

    server = ipc.IpcServer(handler, addr)
    server.start()
    try:
        assert ipc.request({"type": "status"}, addr=addr, timeout_s=5) == {
            "ok": True,
            "state": "DISARMED",
        }
        with pytest.raises(ipc.IpcError):
            ipc.IpcServer(handler, addr)  # the name is taken: no second agent
    finally:
        server.close()
    assert calls == [{"type": "status"}]
    with pytest.raises(ipc.AgentNotRunning):
        ipc.request({"type": "status"}, addr=addr, timeout_s=2)
