from __future__ import annotations

import contextlib
import json
import os
import random
import shutil
import socket
import stat
import tempfile
import threading
import time
from collections.abc import Iterator
from multiprocessing.connection import Client
from pathlib import Path
from typing import Any

import pytest

from captation import ipc

pytestmark = pytest.mark.skipif(os.name != "posix", reason="AF_UNIX IPC (Windows: stage 07)")


@pytest.fixture
def isolated_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    # AF_UNIX paths are limited to ~104 bytes; macOS's per-test tmp_path is longer than that.
    run = Path(tempfile.mkdtemp(prefix="cpt-", dir="/tmp"))
    state = tmp_path / "state"
    monkeypatch.setattr(ipc, "runtime_dir", lambda: ipc.ensure_private_dir(run))
    monkeypatch.setattr(ipc, "state_dir", lambda: ipc.ensure_private_dir(state))
    try:
        yield tmp_path
    finally:
        shutil.rmtree(run, ignore_errors=True)


@pytest.fixture
def server(isolated_dirs: Path) -> Iterator[ipc.IpcServer]:
    calls: list[dict[str, Any]] = []
    lock = threading.Lock()

    def handler(msg: dict[str, Any]) -> dict[str, Any]:
        with lock:
            calls.append(msg)
        if msg["type"] == "status":
            return {"ok": True, "state": "DISARMED"}
        return {"ok": True, "echo": msg}

    srv = ipc.IpcServer(handler)
    srv.start()
    yield srv
    srv.close()


@pytest.mark.req("R-52")
def test_round_trip(server: ipc.IpcServer) -> None:
    assert ipc.request({"type": "status"}) == {"ok": True, "state": "DISARMED"}
    reply = ipc.request({"type": "arm", "consent_ref": "P-017"})
    assert reply["echo"] == {"type": "arm", "consent_ref": "P-017"}


@pytest.mark.req("R-52")
def test_files_are_user_only(server: ipc.IpcServer) -> None:
    sock = Path(server.addr)
    key = ipc.authkey_path()
    for path in (sock, key):
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode & 0o077 == 0, f"{path} has mode {oct(mode)}"
    assert stat.S_IMODE(sock.parent.stat().st_mode) == 0o700
    sock_mode, key_mode = stat.S_IMODE(sock.stat().st_mode), stat.S_IMODE(key.stat().st_mode)
    print(f"socket {oct(sock_mode)} key {oct(key_mode)}")


@pytest.mark.req("R-52")
@pytest.mark.parametrize(
    "payload",
    [
        b"not json",
        b"\xff\xfe",
        json.dumps([1, 2]).encode(),
        json.dumps({"type": "format-disk"}).encode(),
        json.dumps({"type": "arm", "consent_ref": 5}).encode(),
        json.dumps({"type": "arm", "consent_ref": "x" * 65}).encode(),
        json.dumps({"type": "status", "extra": 1}).encode(),
    ],
)
def test_malformed_messages_are_rejected(server: ipc.IpcServer, payload: bytes) -> None:
    key = ipc.authkey_path().read_bytes()
    with Client(server.addr, family="AF_UNIX") as conn:
        ipc._client_handshake(conn, key)
        conn.send_bytes(payload)
        assert conn.poll(5)
        reply = json.loads(conn.recv_bytes())
    assert reply == {"ok": False, "error": "malformed message"}
    # the server keeps working
    assert ipc.request({"type": "status"})["ok"] is True


@pytest.mark.req("R-52")
def test_wrong_authkey_and_random_bytes_do_not_kill_the_server(server: ipc.IpcServer) -> None:
    with (
        Client(server.addr, family="AF_UNIX") as conn,
        pytest.raises(ipc.IpcError, match="authentication failed"),
    ):
        ipc._client_handshake(conn, b"wrong-key-wrong-key-wrong-key-32")
    # silent clients (never answer the challenge) must not block others
    silent = []
    for _ in range(5):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(server.addr)
        silent.append(s)
    assert ipc.request({"type": "status"})["ok"] is True
    for s in silent:
        s.close()
    rng = random.Random(7)
    for _ in range(20):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(1)
            for _attempt in range(50):  # the listen backlog may be full for a moment
                try:
                    s.connect(server.addr)
                    break
                except (BlockingIOError, ConnectionRefusedError):
                    time.sleep(0.05)
            with contextlib.suppress(OSError):
                s.sendall(rng.randbytes(rng.randint(1, 300)))
    assert ipc.request({"type": "status"})["ok"] is True


def test_agent_not_running(isolated_dirs: Path) -> None:
    with pytest.raises(ipc.AgentNotRunning):
        ipc.request({"type": "status"})


def test_invalid_message_is_refused_client_side() -> None:
    with pytest.raises(ValueError):
        ipc.validate_message({"type": "nope"})
