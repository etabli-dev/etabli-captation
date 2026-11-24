"""macOS inbound block rule for the obs-websocket port: a ``pf`` anchor (ADR-012, ADR-015).

The stock ``/etc/pf.conf`` evaluates every sub-anchor of ``com.apple`` (``anchor "com.apple/*"``),
so the rules live in their own anchor ``com.apple/250.EtabliCaptation`` and ``/etc/pf.conf`` is
never edited (PF-123). The macOS Application Firewall is per app, not per port, so it cannot
express "loopback only".

Artefacts created by :func:`apply` (``sudo``), removed exactly by :func:`remove`:

- ``/etc/pf.anchors/de.etabli.captation``: the two block rules (IPv4 and IPv6);
- ``/Library/LaunchDaemons/de.etabli.captation.pf.plist``: a root LaunchDaemon that at boot
  (and right after ``apply``) loads the stock main ruleset if it is missing, enables pf with a
  reference (``pfctl -E``; the token goes to ``/var/run/de.etabli.captation.pf.token``) and loads
  the anchor;
- the anchor's rules and the pf enable reference (runtime state).

:func:`check` runs without root (``pfctl -sr`` needs root): the daemon plist and the rule file
must be present for this port, and a TCP connection to the port from every local non-loopback
address must fail. Every identifier is a pending fact (PF-123 … PF-125).
"""

from __future__ import annotations

import ipaddress
import logging
import plistlib
import re
import subprocess
import tempfile
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from captation.obs.firewall import FirewallStatus, connect_from

log = logging.getLogger(__name__)

METHOD = "pf"  # not-a-fact: status label
ANCHOR = "com.apple/250.EtabliCaptation"  # PF-123
RULE_FILE = Path("/etc/pf.anchors/de.etabli.captation")  # PF-123
DAEMON_LABEL = "de.etabli.captation.pf"  # not-a-fact: our own launchd label
DAEMON_PLIST = Path("/Library/LaunchDaemons/de.etabli.captation.pf.plist")  # PF-124
TOKEN_FILE = Path("/var/run/de.etabli.captation.pf.token")  # PF-123
PF_CONF = "/etc/pf.conf"  # PF-123
# Root-run commands: absolute paths only, never a binary from the user's PATH.
PFCTL = "/sbin/pfctl"  # PF-123
LAUNCHCTL = "/bin/launchctl"  # PF-124
INSTALL = "/usr/bin/install"  # PF-124
RM = "/bin/rm"  # PF-124
SUDO = "sudo"  # not-a-fact: the user's interactive sudo (as on Linux)
SH = "/bin/sh"  # PF-124
GREP = "/usr/bin/grep"  # PF-124
SED = "/usr/bin/sed"  # PF-124
CAT = "/bin/cat"  # PF-124
SYSTEM_DOMAIN = "system"  # PF-124
LSOF = "/usr/sbin/lsof"  # PF-105
IFCONFIG = "/sbin/ifconfig"  # PF-125
PROBE_TIMEOUT_S = 0.5
APPLY_WAIT_S = 5.0
ENCODING = "utf-8"  # not-a-fact: text encoding
SPACE = " "  # not-a-fact: separator
COMMA = ", "  # not-a-fact: separator
MSG_SKIPPED = "websocket not listening publicly: connection test skipped"  # not-a-fact: text
MSG_BLOCKED = "connection from every non-loopback local address was blocked"  # not-a-fact: text
MSG_NO_IFCONFIG = "local addresses unknown (ifconfig failed): not verified"  # not-a-fact: text
MSG_NO_ADDRESS = (
    "no non-loopback address: the port is not reachable from a network"  # not-a-fact: text
)
LOOPBACK_V4 = "127.0.0.1"  # not-a-fact: IPv4 loopback address


def pf_rules(port: int) -> str:
    """Rule file text: drop TCP to the port from every source except loopback (PF-123)."""
    return (
        "# Établi Captation: block non-loopback access to the obs-websocket port.\n"
        "# Created by `captation obs firewall --apply`; removed by `--remove`.\n"
        f"# Loaded into the pf anchor {ANCHOR} by {DAEMON_PLIST}.\n"
        f"block drop in quick proto tcp from ! 127.0.0.1 to any port {port}\n"  # PF-123
        f"block drop in quick inet6 proto tcp from ! ::1 to any port {port}\n"  # PF-123
    )


def boot_script() -> str:
    """What the LaunchDaemon runs (as root): main ruleset present, pf enabled with our own
    reference (the previous one released), anchor loaded (PF-123)."""
    # Load the stock /etc/pf.conf only if no main ruleset is loaded at all: another tool's
    # ruleset (VPN kill switch …) is never replaced. `-s Anchors` would also list our own
    # anchor and so could not tell whether the main ruleset references it (audit 2026-11-24).
    main_empty = f'[ -z "$({PFCTL} -sr 2>/dev/null)" ]'  # PF-123
    return (
        f"{main_empty} && {PFCTL} -q -f {PF_CONF}\n"  # PF-123
        f"t={TOKEN_FILE}\n"
        f'[ -s "$t" ] && {PFCTL} -X "$({CAT} "$t")" >/dev/null 2>&1\n'
        f"{PFCTL} -E 2>&1 | {SED} -n 's/^Token : //p' > \"$t\"\n"  # PF-123
        f"{PFCTL} -q -a {ANCHOR} -f {RULE_FILE}\n"  # PF-123
    )


def daemon_definition() -> dict[str, Any]:
    """The LaunchDaemon plist (PF-124): runs :func:`boot_script` once at load and at boot."""
    return {
        "Label": DAEMON_LABEL,  # PF-124
        "ProgramArguments": [SH, "-c", boot_script()],  # PF-124
        "RunAtLoad": True,  # PF-124
    }


Runner = Callable[[list[str]], subprocess.CompletedProcess[str]]


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=120, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(argv, 127, "", type(exc).__name__)


def _run_interactive(argv: list[str]) -> subprocess.CompletedProcess[str]:
    """sudo may ask for the password on the terminal: do not capture stdin/stderr."""
    try:
        res = subprocess.run(argv, stdout=subprocess.PIPE, text=True, timeout=300, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(argv, 127, "", type(exc).__name__)
    return subprocess.CompletedProcess(argv, res.returncode, res.stdout or "", "")


# ---- check (no root) --------------------------------------------------------------------------

_IFACE_RE = re.compile(r"^(?P<name>[A-Za-z0-9_.-]+): flags=")  # PF-125
_INET_RE = re.compile(r"^\s+inet6?\s+(?P<addr>[0-9A-Fa-f:.]+)(?:%\S+)?\s")  # PF-125
_LOOPBACK_IFACE = "lo"  # PF-125


def parse_ifconfig(text: str) -> list[str]:
    """Local addresses a LAN client could target: not loopback, not link-local (PF-125)."""
    addresses: list[str] = []
    iface = ""
    for line in text.splitlines():
        head = _IFACE_RE.match(line)
        if head:
            iface = head.group("name")  # not-a-fact: regex group name
            continue
        m = _INET_RE.match(line + SPACE)
        if not m or iface.startswith(_LOOPBACK_IFACE):
            continue
        try:
            ip = ipaddress.ip_address(m.group("addr"))  # not-a-fact: regex group name
        except ValueError:
            continue
        if ip.is_loopback or ip.is_link_local or ip.is_unspecified:
            continue
        if str(ip) not in addresses:
            addresses.append(str(ip))
    return addresses


def parse_lsof_listen(text: str) -> list[str]:
    """Hosts from ``lsof -nP -iTCP:<port> -sTCP:LISTEN`` NAME columns (``*``, ``127.0.0.1`` …)."""
    hosts: list[str] = []
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 2 or parts[-1] != "(LISTEN)":  # PF-105
            continue
        host = parts[-2].rsplit(":", 1)[0].strip("[]")  # not-a-fact: host:port syntax
        hosts.append(host)
    return hosts


LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})  # not-a-fact: loopback names


def listening_publicly(port: int, runner: Runner = _run) -> bool:
    out = runner([LSOF, "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"])  # PF-105
    return any(h not in LOOPBACK_HOSTS for h in parse_lsof_listen(out.stdout))


def local_addresses(runner: Runner = _run) -> list[str] | None:
    """Non-loopback local addresses; ``None`` if ``ifconfig`` failed (never "none found")."""
    out = runner([IFCONFIG])
    if out.returncode != 0 or not out.stdout.strip():
        return None
    return parse_ifconfig(out.stdout)


def artefacts_ok(port: int, *, rule_file: Path = RULE_FILE, plist: Path = DAEMON_PLIST) -> bool:
    """The rule file holds this port's rules and the daemon loads our anchor."""
    try:
        rules = rule_file.read_text(encoding=ENCODING)
        definition = plistlib.loads(plist.read_bytes())
    except (OSError, plistlib.InvalidFileException, ValueError):
        return False
    script = SPACE.join(str(a) for a in definition.get("ProgramArguments", []))  # PF-124
    wanted = f"to any port {port}\n"
    return rules.count(wanted) == 2 and ANCHOR in script and str(rule_file) in script


def check(
    port: int,
    *,
    runner: Runner = _run,
    connect: Callable[[str, int], bool] = connect_from,
    rule_file: Path = RULE_FILE,
    plist: Path = DAEMON_PLIST,
) -> FirewallStatus:
    details: list[str] = []
    present = artefacts_ok(port, rule_file=rule_file, plist=plist)
    details.append(f"LaunchDaemon {plist.name}: {'present' if plist.exists() else 'missing'}")
    details.append(f"pf rules for port {port}: {'present' if present else 'missing'}")
    if not present:
        return FirewallStatus(False, METHOD, details)
    # "Listening" is decided by a loopback connection (allowed by our rule), not by lsof:
    # a failed or slow lsof must never turn into "not listening, nothing to test".
    if not connect(LOOPBACK_V4, port):
        details.append(MSG_SKIPPED)
        return FirewallStatus(True, METHOD, details)
    addresses = local_addresses(runner)
    if addresses is None:
        details.append(MSG_NO_IFCONFIG)
        return FirewallStatus(False, METHOD, details)
    if not addresses:
        details.append(MSG_NO_ADDRESS)
        return FirewallStatus(True, METHOD, details)
    with ThreadPoolExecutor(max_workers=max(1, min(32, len(addresses)))) as pool:
        results = list(pool.map(lambda a: connect(a, port), addresses))
    reachable = [a for a, ok in zip(addresses, results, strict=True) if ok]
    if reachable:
        reach = COMMA.join(reachable)
        details.append(f"reachable from non-loopback addresses: {reach}")
        return FirewallStatus(False, METHOD, details)
    details.append(MSG_BLOCKED)
    return FirewallStatus(True, METHOD, details)


# ---- apply / remove (sudo) ----------------------------------------------------------------------


def apply(
    port: int,
    *,
    runner: Runner = _run_interactive,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> list[str]:
    """Install the rule file and the LaunchDaemon, load it and wait until the anchor holds the
    rules. Returns the commands that ran."""
    ran: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        rules = Path(tmp) / "rules"  # not-a-fact: temp file name
        daemon = Path(tmp) / "daemon.plist"  # not-a-fact: temp file name
        rules.write_text(pf_rules(port), encoding=ENCODING)
        daemon.write_bytes(plistlib.dumps(daemon_definition(), fmt=plistlib.FMT_XML))
        owner = ["-m", "0644", "-o", "root", "-g", "wheel"]  # PF-124
        steps: list[tuple[list[str], bool]] = [
            ([SUDO, INSTALL, *owner, str(rules), str(RULE_FILE)], True),
            ([SUDO, INSTALL, *owner, str(daemon), str(DAEMON_PLIST)], True),
            # Re-apply: unload the old job first (fails harmlessly if it is not loaded).
            ([SUDO, LAUNCHCTL, "bootout", f"{SYSTEM_DOMAIN}/{DAEMON_LABEL}"], False),  # PF-124
            ([SUDO, LAUNCHCTL, "bootstrap", SYSTEM_DOMAIN, str(DAEMON_PLIST)], True),  # PF-124
        ]
        for argv, required in steps:
            res = runner(argv)
            ran.append(SPACE.join(argv))
            if required and res.returncode != 0:
                raise RuntimeError(f"command failed ({res.returncode}): {SPACE.join(argv)}")
    # RunAtLoad runs the boot script asynchronously: wait until the anchor shows the rules.
    show = [SUDO, PFCTL, "-a", ANCHOR, "-sr"]  # PF-123
    deadline = clock() + APPLY_WAIT_S
    while True:
        res = runner(show)
        if res.returncode == 0 and str(port) in res.stdout:
            break
        if clock() > deadline:
            raise RuntimeError(f"pf anchor {ANCHOR} has no rules for port {port} after apply")
        sleep(0.25)
    ran.append(SPACE.join(show))
    log.info("firewall rule applied for port %d", port)
    return ran


def remove(*, runner: Runner = _run_interactive, token_file: Path = TOKEN_FILE) -> list[str]:
    """Revert exactly what :func:`apply` created: the job, the anchor's rules, our pf enable
    reference, the token, the rule file and the plist."""
    steps: list[list[str]] = [
        [SUDO, LAUNCHCTL, "bootout", f"{SYSTEM_DOMAIN}/{DAEMON_LABEL}"],  # PF-124
        [SUDO, PFCTL, "-a", ANCHOR, "-F", "all"],  # PF-123
    ]
    try:
        token = token_file.read_text(encoding=ENCODING).strip()
    except OSError:
        token = ""
    if token.isdigit():
        steps.append([SUDO, PFCTL, "-X", token])  # PF-123
    steps.append([SUDO, RM, "-f", str(token_file), str(RULE_FILE), str(DAEMON_PLIST)])  # PF-124
    ran = []
    for argv in steps:
        res = runner(argv)
        ran.append(SPACE.join(argv) + ("" if res.returncode == 0 else f"  (exit {res.returncode})"))
    log.info("firewall rule removed")
    return ran
