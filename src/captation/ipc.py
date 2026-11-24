"""CLI ↔ running agent IPC.

``multiprocessing.connection`` with an AF_UNIX socket (POSIX) or a named pipe (Windows) and a
random authkey stored in a user-only file. Payloads are JSON (``send_bytes``/``recv_bytes``),
never pickle, and limited in size. Malformed messages are rejected and logged.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import logging
import os
import secrets
import socket
import sys
import tempfile
import threading
from collections.abc import Callable
from multiprocessing.connection import Client, Connection, Listener
from pathlib import Path
from typing import Any, Protocol

from platformdirs import user_runtime_dir, user_state_dir

from captation.config import APP_AUTHOR, APP_NAME, ensure_private_dir

log = logging.getLogger(__name__)

MAX_MESSAGE_BYTES = 64 * 1024
MAX_CONNECTIONS = 16  # concurrent clients; more are closed at once (only this user can connect)
READ_DEADLINE_S = 5.0  # a client that stalls mid-message is cut off
AUTH_TIMEOUT_S = 2.0
NONCE_BYTES = 32
MESSAGE_TYPES = frozenset({"arm", "disarm", "status", "stop", "cancel", "show"})
Handler = Callable[[dict[str, Any]], dict[str, Any]]


class IpcError(Exception):
    pass


class AgentNotRunning(IpcError):
    pass


def runtime_dir() -> Path:
    return ensure_private_dir(Path(user_runtime_dir(APP_NAME, APP_AUTHOR)))


def state_dir() -> Path:
    return ensure_private_dir(Path(user_state_dir(APP_NAME, APP_AUTHOR)))


def pipe_address(domain: str, user: str) -> str:
    r"""Per-user named pipe of the agent on Windows: ``\\.\pipe\etabli-captation-<hash>``.

    The suffix is a hash of ``DOMAIN\user`` (lower case, as Windows account names are
    case-insensitive), because pipe names are visible to every user of the machine.

    Access control (PF-260): ``multiprocessing.connection`` creates the pipe with NULL security
    attributes and has no parameter for a DACL or ``PIPE_REJECT_REMOTE_CLIENTS``. The default
    DACL grants full control to LocalSystem, Administrators and the creator owner, and *read*
    access to Everyone and Anonymous. A client needs read and write access, so other users
    cannot talk to the agent; any connection must also pass the HMAC handshake with the authkey
    file, which lies in ``%LOCALAPPDATA%`` and inherits the profile's user-only ACL. Remote (SMB)
    clients are not rejected by flag but face the same DACL and handshake. The first pipe
    instance uses ``FILE_FLAG_FIRST_PIPE_INSTANCE``, so a process that takes the name before the
    agent starts makes the agent fail to start (``IpcError``) rather than be impersonated.
    Residual risk: such a squatter blocks the agent and can answer CLI requests with a forged
    reply (it never learns the authkey, only an HMAC of its own nonce). If the Windows checks show
    this matters, replace the listener with ``CreateNamedPipeW`` + an owner-only DACL via ctypes.
    """
    account = f"{domain}\\{user}".lower()
    digest = hashlib.sha256(account.encode("utf-8")).hexdigest()[:16]
    return rf"\\.\pipe\etabli-captation-{digest}"


# sockaddr_un.sun_path holds 104 bytes on macOS/BSD and 108 on Linux, including the final NUL.
SUN_PATH_MAX = 103 if sys.platform == "darwin" else 107


def socket_path(preferred: Path, fallback_root: Path, uid: int) -> Path:
    """``preferred`` if it fits into ``sun_path``, else a private per-user directory below
    ``fallback_root`` (macOS: the per-user ``/var/folders/…/T``). A long home folder would
    otherwise make the agent fail with ``AF_UNIX path too long``."""
    if len(os.fsencode(preferred)) <= SUN_PATH_MAX:
        return preferred
    return ensure_private_dir(fallback_root / f"etabli-captation-{uid}") / preferred.name


def address() -> str:
    if sys.platform == "win32":  # mypy narrows sys.platform, not os.name
        return pipe_address(os.environ.get("USERDOMAIN", ""), os.environ.get("USERNAME", "user"))
    return str(socket_path(runtime_dir() / "agent.sock", Path(tempfile.gettempdir()), os.getuid()))


def authkey_path() -> Path:
    return state_dir() / "ipc.key"


def _write_authkey() -> bytes:
    key = secrets.token_bytes(32)
    path = authkey_path()
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(key)
    os.replace(tmp, path)
    return key


def _read_authkey() -> bytes:
    try:
        return authkey_path().read_bytes()
    except FileNotFoundError as exc:
        raise AgentNotRunning("agent is not running (no IPC key)") from exc


class _Conn(Protocol):
    """What the handshake needs (Connection on POSIX, PipeConnection on Windows)."""

    def send_bytes(self, buf: bytes) -> None: ...

    def recv_bytes(self, maxlength: int | None = None) -> bytes: ...

    def poll(self, timeout: float | None = 0.0) -> bool: ...


def _server_handshake(conn: _Conn, key: bytes) -> bool:
    """HMAC challenge-response with timeouts (runs in the per-connection thread)."""
    nonce = secrets.token_bytes(NONCE_BYTES)
    conn.send_bytes(nonce)
    if not conn.poll(AUTH_TIMEOUT_S):
        return False
    answer = conn.recv_bytes(64)
    ok = hmac.compare_digest(answer, hmac.new(key, nonce, hashlib.sha256).digest())
    conn.send_bytes(b"OK" if ok else b"NO")
    return ok


def _client_handshake(conn: _Conn, key: bytes) -> None:
    if not conn.poll(AUTH_TIMEOUT_S):
        raise IpcError("agent did not start the handshake")
    nonce = conn.recv_bytes(NONCE_BYTES)
    conn.send_bytes(hmac.new(key, nonce, hashlib.sha256).digest())
    if not conn.poll(AUTH_TIMEOUT_S) or conn.recv_bytes(8) != b"OK":
        raise IpcError("IPC authentication failed")


def validate_message(msg: Any) -> dict[str, Any]:
    """Return the message if it is well formed, else raise ``ValueError``."""
    if not isinstance(msg, dict):
        raise ValueError("message must be a JSON object")
    kind = msg.get("type")
    if kind not in MESSAGE_TYPES:
        raise ValueError(f"unknown message type: {kind!r}")
    allowed = {"type", "consent_ref"} if kind == "arm" else {"type"}
    extra = set(msg) - allowed
    if extra:
        raise ValueError(f"unexpected fields: {sorted(extra)}")
    ref = msg.get("consent_ref")
    if kind == "arm" and ref is not None and (not isinstance(ref, str) or len(ref) > 64):
        raise ValueError("consent_ref must be a string of at most 64 characters")
    return msg


class _Deadline:
    """Shut the socket down if the exchange takes longer than ``seconds``: a partial frame
    would otherwise block the reading thread forever. POSIX sockets only."""

    def __init__(self, conn: Connection, seconds: float) -> None:
        self._sock: socket.socket | None = None
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None
        if os.name != "posix":  # pragma: no cover - Windows named pipes
            return
        try:
            self._sock = socket.socket(fileno=os.dup(conn.fileno()))
        except OSError:
            return
        self._timer = threading.Timer(seconds, self._cut)
        self._timer.daemon = True
        self._timer.start()

    def _cut(self) -> None:
        with self._lock:
            if self._sock is not None:
                with contextlib.suppress(OSError):
                    self._sock.shutdown(socket.SHUT_RDWR)
                self._sock.close()
                self._sock = None

    def cancel(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
        with self._lock:
            if self._sock is not None:
                self._sock.close()
                self._sock = None


class IpcServer:
    """Accepts one request per connection and answers it with ``handler``'s result."""

    def __init__(self, handler: Handler, addr: str | None = None) -> None:
        self._handler = handler
        self._addr = addr or address()
        self._authkey = _write_authkey()
        if os.name == "posix":
            Path(self._addr).unlink(missing_ok=True)
            old = os.umask(0o177)
            try:
                self._listener = Listener(self._addr, family="AF_UNIX", backlog=64)
            finally:
                os.umask(old)
            os.chmod(self._addr, 0o600)
        else:  # pragma: no cover - Windows named pipe; access control: see pipe_address()
            try:
                self._listener = Listener(self._addr, family="AF_PIPE")  # PF-260
            except PermissionError as exc:
                raise IpcError(
                    "the agent's named pipe already exists (another agent of this user is "
                    "running, or another process holds the name)"
                ) from exc
        self._stop = threading.Event()
        self._slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        self._thread = threading.Thread(target=self._serve, name="captation-ipc", daemon=True)

    @property
    def addr(self) -> str:
        return self._addr

    def start(self) -> None:
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn = self._listener.accept()
            except OSError:
                if self._stop.is_set():
                    return
                log.warning("ipc: accept failed")
                continue
            if not self._slots.acquire(blocking=False):
                log.warning("ipc: too many connections, closing one")
                conn.close()
                continue
            threading.Thread(target=self._handle_slot, args=(conn,), daemon=True).start()

    def _handle_slot(self, conn: Connection) -> None:
        guard = _Deadline(conn, READ_DEADLINE_S)
        try:
            self._handle(conn)
        finally:
            guard.cancel()
            self._slots.release()

    def _handle(self, conn: Connection) -> None:
        with conn:
            try:
                if not _server_handshake(conn, self._authkey):
                    log.warning("ipc: rejected a connection (authentication failed)")
                    return
                if not conn.poll(AUTH_TIMEOUT_S):
                    log.warning("ipc: client sent no request")
                    return
                data = conn.recv_bytes(MAX_MESSAGE_BYTES)
                msg = validate_message(json.loads(data.decode("utf-8")))
            except (OSError, EOFError):
                log.warning("ipc: connection dropped or message too large")
                return
            except (ValueError, UnicodeDecodeError, RecursionError) as exc:
                log.warning("ipc: malformed message rejected: %s", type(exc).__name__)
                self._reply(conn, {"ok": False, "error": "malformed message"})
                return
            try:
                reply = self._handler(msg)
            except Exception:
                log.exception("ipc: handler failed for %s", msg["type"])
                reply = {"ok": False, "error": "internal error"}
            self._reply(conn, reply)

    @staticmethod
    def _reply(conn: Connection, reply: dict[str, Any]) -> None:
        try:
            conn.send_bytes(json.dumps(reply).encode("utf-8"))
        except OSError:
            log.warning("ipc: could not send the reply")

    def close(self) -> None:
        self._stop.set()
        try:
            # Wake up accept() with a throwaway connection attempt.
            if sys.platform != "win32":
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                    s.settimeout(0.5)
                    s.connect(self._addr)
            else:  # pragma: no cover - Windows: wake accept() with a throwaway pipe client
                Client(self._addr, family="AF_PIPE").close()
        except OSError:
            pass
        self._listener.close()
        if os.name == "posix":
            Path(self._addr).unlink(missing_ok=True)


def request(msg: dict[str, Any], *, addr: str | None = None, timeout_s: float = 10) -> Any:
    """Send one message to the running agent and return its JSON reply."""
    validate_message(msg)
    target = addr or address()
    if os.name == "posix" and not Path(target).exists():
        raise AgentNotRunning("agent is not running")
    key = _read_authkey()
    try:
        conn = Client(target, family="AF_UNIX" if os.name == "posix" else "AF_PIPE")
    except (FileNotFoundError, ConnectionRefusedError) as exc:
        raise AgentNotRunning("agent is not running") from exc
    with conn:
        try:
            _client_handshake(conn, key)
        except (OSError, EOFError) as exc:
            raise IpcError("IPC authentication failed") from exc
        conn.send_bytes(json.dumps(msg).encode("utf-8"))
        if not conn.poll(timeout_s):
            raise IpcError("agent did not answer in time")
        return json.loads(conn.recv_bytes(MAX_MESSAGE_BYTES).decode("utf-8"))
