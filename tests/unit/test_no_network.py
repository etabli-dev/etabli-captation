"""R-26 at unit level: the agent's only network client is the obs-websocket connection to loopback.

The end-to-end evidence (check of the running agent's sockets, E10) stays in the e2e suite. This
guard keeps network client libraries out of ``src/captation`` and checks that the websocket
host can only be a loopback address.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from captation.config import (
    ConfigError,
    WebsocketConfig,
    data_text,
    packaged_schema,
    parse_config,
)

pytestmark = pytest.mark.req("R-26")

SRC = Path(__file__).resolve().parents[2] / "src" / "captation"

# Modules that open network connections (HTTP, mail, file transfer, RPC, telemetry, TLS).
NETWORK_MODULES = {
    "aiohttp",
    "asyncssh",
    "ftplib",
    "grpc",
    "http",
    "httplib2",
    "httpx",
    "imaplib",
    "nntplib",
    "paramiko",
    "poplib",
    "pycurl",
    "requests",
    "sentry_sdk",
    "smtplib",
    "socketserver",
    "ssl",
    "telnetlib",
    "urllib",
    "urllib3",
    "websocket",
    "xmlrpc",
    "zeroconf",
}
# Allowed low-level network modules, each limited to the files that need it.
ALLOWED = {
    # the obs-websocket v5 client (ADR-002), loopback only
    "websockets": {"obs/client.py"},
    # ipc.py: AF_UNIX socket probe of the agent's own IPC endpoint; launcher.py: port probe of
    # the loopback websocket; firewall.py: self-test from the host's own addresses to its port
    "socket": {"ipc.py", "obs/launcher.py", "obs/firewall.py"},
    # IPC between the CLI and the agent: AF_UNIX socket or named pipe (ipc.py)
    "multiprocessing": {"ipc.py"},
}
LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def _imports(path: Path) -> set[str]:
    """Imported module names (dotted, absolute imports only) of one source file."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "import_module"
            and node.args
            and isinstance(node.args[0], ast.Constant)
        ):
            names.add(str(node.args[0].value))
    return names


def _sources() -> dict[str, set[str]]:
    files = {p.relative_to(SRC).as_posix(): _imports(p) for p in sorted(SRC.rglob("*.py"))}
    assert "obs/client.py" in files and len(files) > 20
    return files


def test_no_network_client_libraries_in_src() -> None:
    offenders = {
        rel: sorted(n for n in names if n.split(".")[0] in NETWORK_MODULES)
        for rel, names in _sources().items()
    }
    assert {rel: mods for rel, mods in offenders.items() if mods} == {}


def test_low_level_network_modules_only_where_allowed() -> None:
    used: dict[str, set[str]] = {module: set() for module in ALLOWED}
    for rel, names in _sources().items():
        for name in names:
            top = name.split(".")[0]
            if top in ALLOWED:
                used[top].add(rel)
    for module, files in used.items():
        assert files <= ALLOWED[module], (module, sorted(files - ALLOWED[module]))
    assert used["websockets"] == {"obs/client.py"}  # the one network client


def test_multiprocessing_ipc_uses_local_transports_only() -> None:
    tree = ast.parse((SRC / "ipc.py").read_text(encoding="utf-8"))
    families = {
        kw.value.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        for kw in node.keywords
        if kw.arg == "family" and isinstance(kw.value, ast.Constant)
    }
    assert families and families <= {"AF_UNIX", "AF_PIPE"}, families
    socket_families = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr.startswith("AF_")
    }
    assert socket_families <= {"AF_UNIX", "AF_PIPE"}, socket_families


def test_every_websocket_connection_uses_the_configured_host() -> None:
    """ObsWebSocket is only created with ``<websocket config>.host``, never a literal host."""
    calls = []
    for path in sorted(SRC.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "ObsWebSocket":
                calls.append((path.relative_to(SRC).as_posix(), node))
    assert calls, "no ObsWebSocket construction found"
    for rel, call in calls:
        host = call.args[0] if call.args else None
        assert isinstance(host, ast.Attribute) and host.attr == "host", (rel, call.lineno)


def _raw_config(host: Any) -> dict[str, Any]:
    raw: dict[str, Any] = yaml.safe_load(data_text("config.default.yaml"))
    raw["data_root"] = "/srv/captation-test"
    raw["obs"]["websocket"]["host"] = host
    return raw


@pytest.mark.parametrize("host", sorted(LOOPBACK))
def test_loopback_hosts_are_accepted(host: str) -> None:
    assert parse_config(_raw_config(host)).obs.websocket.host == host


@pytest.mark.parametrize(
    "host", ["0.0.0.0", "192.168.1.10", "10.0.0.2", "::", "fe80::1", "obs.example.org", ""]
)
def test_non_loopback_hosts_are_rejected(host: str) -> None:
    with pytest.raises(ValidationError):
        WebsocketConfig(host=host, port=4455)  # type: ignore[arg-type]
    with pytest.raises(ConfigError, match="host"):
        parse_config(_raw_config(host))
    schema = packaged_schema("config.schema.json")
    ws_schema = schema["properties"]["obs"]["properties"]["websocket"]["properties"]["host"]
    assert set(ws_schema["enum"]) == LOOPBACK
