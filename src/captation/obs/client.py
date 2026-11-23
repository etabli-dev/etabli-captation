"""Thin obs-websocket v5 client (ADR-002) and the ObsController facade.

Only the requests Captation uses are wrapped. The low-level :class:`ObsWebSocket` speaks the
v5 JSON protocol over ``websockets.sync``: Hello → Identify (with auth) → Identified, requests
matched by ``requestId``, events dispatched from a reader thread.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import logging
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from websockets.exceptions import ConnectionClosed, WebSocketException
from websockets.sync.client import ClientConnection, connect
from websockets.typing import Subprotocol

log = logging.getLogger(__name__)

RPC_VERSION = 1
SUBPROTOCOL = "obswebsocket.json"

# obs-websocket v5 opcodes
OP_HELLO, OP_IDENTIFY, OP_IDENTIFIED = 0, 1, 2
OP_EVENT, OP_REQUEST, OP_REQUEST_RESPONSE = 5, 6, 7

# EventSubscription bits: General, Config, Scenes, Inputs, Outputs
SUBSCRIPTIONS = 1 | 2 | 4 | 8 | 64

EventCallback = Callable[[str, dict[str, Any]], None]


class ObsError(Exception):
    """Base class for OBS communication errors."""


class ObsConnectionError(ObsError):
    """Could not connect or authenticate."""


class ObsLost(ObsError):
    """The connection to OBS was lost (crash, exit or network)."""


class ObsRequestError(ObsError):
    def __init__(self, request_type: str, code: int, comment: str | None) -> None:
        super().__init__(f"{request_type} failed: {code} {comment or ''}".strip())
        self.request_type = request_type
        self.code = code
        self.comment = comment


def auth_string(password: str, salt: str, challenge: str) -> str:
    """obs-websocket v5 authentication string."""
    secret = base64.b64encode(hashlib.sha256((password + salt).encode()).digest()).decode()
    return base64.b64encode(hashlib.sha256((secret + challenge).encode()).digest()).decode()


@dataclass
class _Pending:
    event: threading.Event
    response: dict[str, Any] | None = None


class ObsWebSocket:
    """One authenticated connection. Thread-safe ``request()``; events go to callbacks."""

    def __init__(self, host: str, port: int, password: str | None) -> None:
        self._url = f"ws://{'[::1]' if host == '::1' else host}:{port}"
        self._password = password
        self._ws: ClientConnection | None = None
        self._pending: dict[str, _Pending] = {}
        self._lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._callbacks: list[EventCallback] = []
        self._closed = threading.Event()
        self._reader: threading.Thread | None = None
        self.negotiated_rpc: int | None = None
        self.ws_version: str | None = None

    # ---- lifecycle -------------------------------------------------------------------------

    def connect(self, timeout_s: float = 5) -> None:
        try:
            ws = connect(
                self._url,
                subprotocols=[Subprotocol(SUBPROTOCOL)],
                open_timeout=timeout_s,
                close_timeout=2,
                max_size=16 * 1024 * 1024,
                legacy=True,  # long-lived connection outside a with-block (websockets >= 17.1)
            )
        except (OSError, WebSocketException, TimeoutError) as exc:
            raise ObsConnectionError(f"cannot connect to {self._url}: {exc}") from exc
        try:
            hello = json.loads(ws.recv(timeout=timeout_s))
            if hello.get("op") != OP_HELLO:
                raise ObsConnectionError("unexpected first message from obs-websocket")
            data = hello["d"]
            self.ws_version = data.get("obsWebSocketVersion")
            identify: dict[str, Any] = {
                "rpcVersion": RPC_VERSION,
                "eventSubscriptions": SUBSCRIPTIONS,
            }
            auth = data.get("authentication")
            if auth:
                if not self._password:
                    raise ObsConnectionError(
                        "obs-websocket requires a password, none is configured"
                    )
                identify["authentication"] = auth_string(
                    self._password, auth["salt"], auth["challenge"]
                )
            ws.send(json.dumps({"op": OP_IDENTIFY, "d": identify}))
            reply = json.loads(ws.recv(timeout=timeout_s))
            if reply.get("op") != OP_IDENTIFIED:
                raise ObsConnectionError("obs-websocket did not accept the identification")
            self.negotiated_rpc = reply["d"].get("negotiatedRpcVersion")
        except ConnectionClosed as exc:
            # 4009 = authentication failed
            code = exc.rcvd.code if exc.rcvd else None
            reason = "authentication failed" if code == 4009 else f"closed ({code})"
            raise ObsConnectionError(f"obs-websocket {reason}") from exc
        except (TimeoutError, OSError, ValueError, KeyError) as exc:
            ws.close()
            raise ObsConnectionError(f"obs-websocket handshake failed: {exc}") from exc
        self._ws = ws
        self._closed.clear()
        self._reader = threading.Thread(target=self._read_loop, name="obs-ws-reader", daemon=True)
        self._reader.start()

    def close(self) -> None:
        self._closed.set()
        if self._ws is not None:
            with contextlib.suppress(OSError, WebSocketException):
                self._ws.close()
        self._fail_pending()

    @property
    def connected(self) -> bool:
        return self._ws is not None and not self._closed.is_set()

    def on_event(self, callback: EventCallback) -> None:
        self._callbacks.append(callback)

    # ---- I/O -------------------------------------------------------------------------------

    def _read_loop(self) -> None:
        assert self._ws is not None
        try:
            for raw in self._ws:
                msg = json.loads(raw)
                op = msg.get("op")
                if op == OP_REQUEST_RESPONSE:
                    d = msg["d"]
                    with self._lock:
                        pending = self._pending.pop(d.get("requestId", ""), None)
                    if pending is not None:
                        pending.response = d
                        pending.event.set()
                elif op == OP_EVENT:
                    d = msg["d"]
                    for cb in list(self._callbacks):
                        try:
                            cb(d.get("eventType", ""), d.get("eventData") or {})
                        except Exception:
                            log.exception("obs event callback failed")
        except (ConnectionClosed, OSError, ValueError):
            pass
        finally:
            was_open = not self._closed.is_set()
            self._closed.set()
            self._fail_pending()
            if was_open:
                for cb in list(self._callbacks):
                    try:
                        cb("ConnectionLost", {})
                    except Exception:
                        log.exception("obs event callback failed")

    def _fail_pending(self) -> None:
        with self._lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for p in pending:
            p.event.set()

    def request(
        self, request_type: str, data: dict[str, Any] | None = None, *, timeout_s: float = 10
    ) -> dict[str, Any]:
        """Send a request and return its ``responseData`` (``{}`` if none)."""
        if not self.connected or self._ws is None:
            raise ObsLost("not connected to OBS")
        rid = uuid.uuid4().hex
        pending = _Pending(threading.Event())
        with self._lock:
            self._pending[rid] = pending
        body: dict[str, Any] = {"requestType": request_type, "requestId": rid}
        if data is not None:
            body["requestData"] = data
        try:
            with self._send_lock:
                self._ws.send(json.dumps({"op": OP_REQUEST, "d": body}))
        except (ConnectionClosed, OSError) as exc:
            with self._lock:
                self._pending.pop(rid, None)
            raise ObsLost(f"connection lost while sending {request_type}") from exc
        if not pending.event.wait(timeout_s):
            with self._lock:
                self._pending.pop(rid, None)
            raise ObsError(f"{request_type}: no response within {timeout_s} s")
        response = pending.response
        if response is None:
            raise ObsLost(f"connection lost while waiting for {request_type}")
        status = response.get("requestStatus", {})
        if not status.get("result", False):
            raise ObsRequestError(request_type, int(status.get("code", 0)), status.get("comment"))
        result: dict[str, Any] = response.get("responseData") or {}
        return result
