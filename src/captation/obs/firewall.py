"""Inbound block rule for the obs-websocket port (docs/principles.md §4.3, ADR-012).

obs-websocket binds to all interfaces and has no loopback-only option (PF-005). With
``exposure_policy: require_firewall`` arming is refused until :func:`check` passes.

Linux: a dedicated nftables table ``inet etabli_captation`` that drops TCP to the port from
every non-loopback source address, loaded at boot by a oneshot systemd unit. ``--apply`` and
``--remove`` need root (``sudo``); ``--check`` runs as the user: it verifies that the unit is
active and then tries to connect from every local non-loopback address, which must fail.
"""

from __future__ import annotations

import importlib
import json
import logging
import socket
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from captation.platform import current_os

log = logging.getLogger(__name__)

NFT_TABLE = "etabli_captation"
RULE_DIR = Path("/etc/etabli-captation")
RULE_FILE = RULE_DIR / "obs-websocket-guard.nft"
UNIT_NAME = "etabli-captation-obs-guard.service"
UNIT_FILE = Path("/etc/systemd/system") / UNIT_NAME
PROBE_TIMEOUT_S = 0.5


@dataclass
class FirewallStatus:
    ok: bool
    method: str
    details: list[str] = field(default_factory=list)


def nft_ruleset(port: int) -> str:
    return (
        "# Établi Captation: block non-loopback access to the obs-websocket port.\n"
        "# Created by `captation obs firewall --apply`; removed by `--remove`.\n"
        f"table inet {NFT_TABLE}\n"
        f"delete table inet {NFT_TABLE}\n"
        f"table inet {NFT_TABLE} {{\n"
        "  chain input {\n"
        "    type filter hook input priority filter - 10; policy accept;\n"
        f"    tcp dport {port} ip saddr != 127.0.0.0/8 drop\n"
        f"    tcp dport {port} ip6 saddr != ::1 drop\n"
        "  }\n"
        "}\n"
    )


def systemd_unit(nft: str) -> str:
    return (
        "[Unit]\n"
        "Description=Etabli Captation: block non-loopback access to the obs-websocket port\n"
        "Before=network-pre.target\n"
        "Wants=network-pre.target\n\n"
        "[Service]\n"
        "Type=oneshot\n"
        "RemainAfterExit=yes\n"
        f"ExecStart={nft} -f {RULE_FILE}\n"
        f"ExecStop=-{nft} delete table inet {NFT_TABLE}\n\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )


NFT = "/usr/sbin/nft"  # root-run unit: never a binary from the user's PATH (review finding 17)


def _nft_path() -> str:
    return NFT


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, timeout=60, check=False)


def local_addresses() -> list[str]:
    """Global-scope local addresses (``ip -j addr``), the sources a LAN client would hit."""
    out = _run(["ip", "-j", "addr"])
    if out.returncode != 0:
        return []
    addrs = []
    for iface in json.loads(out.stdout or "[]"):
        if iface.get("ifname") == "lo":
            continue
        for info in iface.get("addr_info", []):
            if info.get("scope") == "global" and info.get("local"):
                addrs.append(str(info["local"]))
    return addrs


def connect_from(address: str, port: int) -> bool:
    """True if a TCP connection from ``address`` to ``address:port`` succeeds."""
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as s:
        s.settimeout(PROBE_TIMEOUT_S)
        try:
            s.bind((address, 0))
            s.connect((address, port))
        except OSError:
            return False
    return True


def listening_publicly(port: int) -> bool:
    """True if something listens on the port on a non-loopback address (``ss``)."""
    if current_os() != "linux":
        try:
            return bool(_os_module().listening_publicly(port))
        except (ImportError, AttributeError):
            return True  # unknown: assume exposed
    out = _run(["ss", "-ltnH", f"sport = :{port}"])
    for line in out.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 4:
            local = parts[3].rsplit(":", 1)[0].strip("[]")
            if local not in {"127.0.0.1", "::1"}:
                return True
    return False


def _os_module() -> Any:
    """macOS/Windows implementations live in firewall_macos.py / firewall_windows.py."""
    return importlib.import_module(f"captation.obs.firewall_{current_os()}")


def check(port: int) -> FirewallStatus:
    if current_os() != "linux":
        try:
            status: FirewallStatus = _os_module().check(port)
        except ImportError:
            return FirewallStatus(False, "none", ["no firewall support on this OS yet"])
        return status
    details: list[str] = []
    unit_active = _run(["systemctl", "is-active", UNIT_NAME]).stdout.strip() == "active"
    rule_ok = RULE_FILE.exists() and f"tcp dport {port} " in RULE_FILE.read_text(encoding="utf-8")
    details.append(f"unit {UNIT_NAME}: {'active' if unit_active else 'not active'}")
    details.append(f"rule file for port {port}: {'present' if rule_ok else 'missing'}")
    if not (unit_active and rule_ok):
        return FirewallStatus(False, "nftables", details)
    if not listening_publicly(port):
        details.append("websocket not listening on a public address: connection test skipped")
        return FirewallStatus(True, "nftables", details)
    addresses = local_addresses()
    with ThreadPoolExecutor(max_workers=max(1, min(32, len(addresses)))) as pool:
        results = list(pool.map(lambda a: connect_from(a, port), addresses))
    reachable = [a for a, ok in zip(addresses, results, strict=True) if ok]
    if reachable:
        details.append("reachable from non-loopback addresses: " + ", ".join(reachable))
        return FirewallStatus(False, "nftables", details)
    details.append("connection from every non-loopback local address was blocked")
    return FirewallStatus(True, "nftables", details)


def apply(port: int) -> list[str]:
    """Install and start the rule (needs sudo). Returns the commands that ran."""
    if current_os() != "linux":
        ran: list[str] = _os_module().apply(port)
        return ran
    nft = _nft_path()
    with tempfile.TemporaryDirectory() as tmp:
        rules = Path(tmp) / "guard.nft"
        unit = Path(tmp) / UNIT_NAME
        rules.write_text(nft_ruleset(port), encoding="utf-8")
        unit.write_text(systemd_unit(nft), encoding="utf-8")
        steps = [
            ["sudo", "install", "-D", "-m", "0644", str(rules), str(RULE_FILE)],
            ["sudo", "install", "-m", "0644", str(unit), str(UNIT_FILE)],
            ["sudo", "systemctl", "daemon-reload"],
            ["sudo", "systemctl", "enable", UNIT_NAME],
            ["sudo", "systemctl", "restart", UNIT_NAME],
        ]
        for argv in steps:
            res = subprocess.run(argv, timeout=120, check=False)
            if res.returncode != 0:
                raise RuntimeError(f"command failed ({res.returncode}): {' '.join(argv)}")
    log.info("firewall rule applied for port %d", port)
    return [" ".join(a) for a in steps]


def remove() -> list[str]:
    """Revert exactly what :func:`apply` created (needs sudo)."""
    if current_os() != "linux":
        undone: list[str] = _os_module().remove()
        return undone
    steps = [
        ["sudo", "systemctl", "disable", "--now", UNIT_NAME],
        ["sudo", "rm", "-f", str(UNIT_FILE), str(RULE_FILE)],
        ["sudo", "rmdir", "--ignore-fail-on-non-empty", str(RULE_DIR)],
        ["sudo", "systemctl", "daemon-reload"],
        ["sudo", _nft_path(), "delete", "table", "inet", NFT_TABLE],
    ]
    ran = []
    for argv in steps:
        res = subprocess.run(argv, timeout=120, check=False, capture_output=True)
        ran.append(" ".join(argv) + ("" if res.returncode == 0 else f"  (exit {res.returncode})"))
    log.info("firewall rule removed")
    return ran
