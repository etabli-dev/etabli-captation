"""The thin obs-websocket v5 client against a local fake server (no OBS needed)."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from typing import Any

import pytest
from websockets.sync.server import ServerConnection, serve

from captation.obs.client import (
    ObsConnectionError,
    ObsError,
    ObsLost,
    ObsRequestError,
    ObsWebSocket,
    auth_string,
)

PASSWORD = "p" * 40
SALT, CHALLENGE = "c2FsdA==", "Y2hhbGxlbmdl"


class Server:
    def __init__(self, *, auth: bool = True, mode: str = "normal") -> None:
        self.auth = auth
        self.mode = mode
        self.conns: list[ServerConnection] = []
        self.srv = serve(self.handler, "127.0.0.1", 0, subprotocols=["obswebsocket.json"])  # type: ignore[list-item]
        self.port = self.srv.socket.getsockname()[1]
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()

    def handler(self, ws: ServerConnection) -> None:
        self.conns.append(ws)
        if self.mode == "garbage":
            ws.send(json.dumps({"op": 9}))
            return
        hello: dict[str, Any] = {"obsWebSocketVersion": "5.7.6", "rpcVersion": 1}
        if self.auth:
            hello["authentication"] = {"salt": SALT, "challenge": CHALLENGE}
        ws.send(json.dumps({"op": 0, "d": hello}))
        identify = json.loads(ws.recv())
        if self.auth and identify["d"].get("authentication") != auth_string(
            PASSWORD, SALT, CHALLENGE
        ):
            ws.close(4009, "Authentication failed.")
            return
        ws.send(json.dumps({"op": 2, "d": {"negotiatedRpcVersion": 1}}))
        for raw in ws:
            msg = json.loads(raw)["d"]
            rtype, rid = msg["requestType"], msg["requestId"]
            if rtype == "Silent":
                continue
            if rtype == "Close":
                ws.close()
                return
            ok = rtype != "Fail"
            ws.send(json.dumps({"op": 5, "d": {"eventType": "Echo", "eventData": {"for": rtype}}}))
            ws.send(
                json.dumps(
                    {
                        "op": 7,
                        "d": {
                            "requestType": rtype,
                            "requestId": rid,
                            "requestStatus": {
                                "result": ok,
                                "code": 100 if ok else 600,
                                "comment": None if ok else "bad",
                            },
                            "responseData": {"echo": msg.get("requestData")} if ok else None,
                        },
                    }
                )
            )

    def stop(self) -> None:
        self.srv.shutdown()


@pytest.fixture
def server() -> Iterator[Server]:
    s = Server()
    yield s
    s.stop()


def test_handshake_requests_and_events(server: Server) -> None:
    client = ObsWebSocket("127.0.0.1", server.port, PASSWORD)
    events: list[tuple[str, dict[str, Any]]] = []
    client.on_event(lambda t, d: events.append((t, d)))
    client.on_event(
        lambda t, d: (_ for _ in ()).throw(RuntimeError("callback errors are contained"))
    )
    client.connect()
    assert client.connected and client.ws_version == "5.7.6" and client.negotiated_rpc == 1
    assert client.request("GetVersion", {"x": 1}) == {"echo": {"x": 1}}
    with pytest.raises(ObsRequestError) as info:
        client.request("Fail")
    assert info.value.code == 600
    time.sleep(0.1)
    assert ("Echo", {"for": "GetVersion"}) in events
    with pytest.raises(ObsError, match="no response"):
        client.request("Silent", timeout_s=0.2)
    client.close()
    assert not client.connected
    with pytest.raises(ObsLost):
        client.request("GetVersion")


def test_wrong_password_and_missing_password(server: Server) -> None:
    with pytest.raises(ObsConnectionError, match="authentication failed"):
        ObsWebSocket("127.0.0.1", server.port, "wrong-password").connect()
    with pytest.raises(ObsConnectionError, match="requires a password"):
        ObsWebSocket("127.0.0.1", server.port, None).connect()


def test_connection_lost_is_reported(server: Server) -> None:
    client = ObsWebSocket("127.0.0.1", server.port, PASSWORD)
    lost = threading.Event()
    client.on_event(lambda t, d: lost.set() if t == "ConnectionLost" else None)
    client.connect()
    with pytest.raises(ObsLost):
        client.request("Close", timeout_s=2)
    assert lost.wait(2)
    assert not client.connected


def test_unreachable_and_unexpected_server() -> None:
    with pytest.raises(ObsConnectionError, match="cannot connect"):
        ObsWebSocket("127.0.0.1", 1, PASSWORD).connect(timeout_s=0.5)
    bad = Server(mode="garbage")
    try:
        with pytest.raises(ObsConnectionError):
            ObsWebSocket("127.0.0.1", bad.port, PASSWORD).connect(timeout_s=1)
    finally:
        bad.stop()
    noauth = Server(auth=False)
    try:
        c = ObsWebSocket("::1" if False else "127.0.0.1", noauth.port, None)
        c.connect()
        assert c.request("GetVersion") == {"echo": None}
        c.close()
    finally:
        noauth.stop()
