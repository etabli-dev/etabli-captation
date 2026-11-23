"""Windows Defender Firewall rule for the obs-websocket port (docs/principles.md §4.3, ADR-012).

``--apply`` creates one inbound **block** rule for TCP on the websocket port, all profiles.
Windows Firewall does not filter loopback traffic, so local clients (the agent on 127.0.0.1)
still connect while every remote host is blocked (PF-232). A block rule also wins over an
"allow" rule that the first-start prompt may have created for ``obs64.exe`` (PF-232).

``--apply`` and ``--remove`` need administrator rights: they run an elevated PowerShell via
``Start-Process -Verb RunAs``, so Windows shows a **UAC prompt** (PF-230). ``--check`` runs
unelevated and reads the rule, its port filter and the firewall profiles (PF-231).

Unlike Linux, ``--check`` cannot prove the block with a connection test: a connection from
this host to its own LAN address is routed over loopback and is exempt from the firewall
(PF-232). Verify from a second device once (runbook).

Prepared on Linux (ADR-015): every identifier refers to a ``pending`` PF entry.
"""

from __future__ import annotations

import logging
import subprocess
from typing import Any

from captation.obs.firewall import FirewallStatus
from captation.platform.windows import (
    Runner,
    default_runner,
    encode_command,
    parse_json_output,
    powershell_argv,
    powershell_exe,
    ps_quote,
)

log = logging.getLogger(__name__)

METHOD = "windows-firewall"  # not-a-fact: status label
RULE_NAME = "EtabliCaptation-ObsWebsocket"  # PF-230
RULE_DISPLAY_NAME = "Etabli Captation obs-websocket"  # PF-230
RULE_DESCRIPTION = (
    "Blocks non-loopback access to the obs-websocket port. "  # not-a-fact: display text
    "Created by captation obs firewall --apply."  # not-a-fact: display text
)
LOOPBACK = frozenset({"127.0.0.1", "::1"})  # PF-208
ALL_PROFILES = frozenset({"Domain", "Private", "Public"})  # PF-231
PROFILE_ANY = "Any"  # PF-231
TRUE = "True"  # PF-231
FALSE = "False"  # PF-231
INBOUND = "Inbound"  # PF-231
BLOCK = "Block"  # PF-231
TCP = "TCP"  # PF-231
LOOPBACK_NOTE = (
    "note: a connection test from this host's own LAN address is not meaningful on "  # PF-232
    "Windows (loopback-routed traffic is exempt from the firewall); "  # PF-232
    "test once from a second device"  # not-a-fact: user-facing advice
)
UNREADABLE = "could not read the firewall state"  # not-a-fact: user-facing detail

CHECK_SCRIPT = """
$rules = @(Get-NetFirewallRule -DisplayName $displayName -ErrorAction SilentlyContinue |
    ForEach-Object {
        $pf = $_ | Get-NetFirewallPortFilter
        [pscustomobject]@{
            Name = [string]$_.Name
            Enabled = [string]$_.Enabled
            Direction = [string]$_.Direction
            Action = [string]$_.Action
            Profile = [string]$_.Profile
            Protocol = [string]$pf.Protocol
            LocalPort = @($pf.LocalPort | ForEach-Object { [string]$_ })
        }
    })
try { $fp = @(Get-NetFirewallProfile -PolicyStore ActiveStore) }
catch { $fp = @(Get-NetFirewallProfile) }
$profiles = @($fp | ForEach-Object {
    [pscustomobject]@{
        Name = [string]$_.Name
        Enabled = [string]$_.Enabled
        AllowLocalFirewallRules = [string]$_.AllowLocalFirewallRules
    }
})
$out = [pscustomobject]@{ rules = $rules; profiles = $profiles }
ConvertTo-Json -Compress -Depth 5 -InputObject $out
"""  # PF-231

LISTEN_SCRIPT = """
$a = @(Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue |
    ForEach-Object { [string]$_.LocalAddress })
ConvertTo-Json -Compress -InputObject $a
"""  # PF-208

REMOVE_SCRIPT = """
Get-NetFirewallRule -Name $ruleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
"""  # PF-230

APPLY_SCRIPT = """
Get-NetFirewallRule -Name $ruleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule -Name $ruleName -DisplayName $displayName -Description $description `
    -Direction Inbound -Action Block -Protocol TCP -LocalPort $port -Profile Any | Out-Null
"""  # PF-230


def _valid_port(port: int) -> int:
    if not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError(f"invalid TCP port: {port!r}")
    return port


def _variables(port: int | None = None) -> str:
    lines = [
        f"$ruleName = {ps_quote(RULE_NAME)}",
        f"$displayName = {ps_quote(RULE_DISPLAY_NAME)}",
        f"$description = {ps_quote(RULE_DESCRIPTION)}",
    ]
    if port is not None:
        lines.append(f"$port = {_valid_port(port)}")
    return "\n".join(lines) + "\n"  # not-a-fact: line separator


def apply_script(port: int) -> str:
    return _variables(port) + APPLY_SCRIPT


def remove_script() -> str:
    return _variables() + REMOVE_SCRIPT


def check_script() -> str:
    return _variables() + CHECK_SCRIPT


def elevated_argv(script: str) -> list[str]:
    """Run ``script`` in an elevated PowerShell and return its exit code (UAC prompt, PF-230).

    The outer PowerShell runs unelevated, starts the inner one with ``-Verb RunAs`` and waits.
    A cancelled UAC prompt makes ``Start-Process`` fail, so the outer exit code is non-zero.
    """
    inner = encode_command(
        "$ErrorActionPreference = 'Stop'\n" + script  # PF-209
    )
    exe = powershell_exe()
    outer = (
        f"$p = Start-Process -FilePath {ps_quote(exe)} -Verb RunAs -Wait -PassThru "
        "-WindowStyle Hidden "
        f"-ArgumentList '-NoProfile','-NonInteractive','-EncodedCommand','{inner}'\n"
        "exit $p.ExitCode\n"
    )
    return powershell_argv(outer)


def _rule_ok(rule: dict[str, Any], port: int) -> bool:
    ports = rule.get("LocalPort")  # PF-231
    port_list = [str(p) for p in ports] if isinstance(ports, list) else [str(ports)]
    profile = str(rule.get("Profile", ""))  # PF-231
    profiles = {p.strip() for p in profile.split(",")}  # PF-231
    return (
        str(rule.get("Enabled")) == TRUE  # PF-231
        and str(rule.get("Direction")) == INBOUND  # PF-231
        and str(rule.get("Action")) == BLOCK  # PF-231
        and str(rule.get("Protocol", "")).upper() == TCP  # PF-231
        and str(port) in port_list
        and (profile == PROFILE_ANY or profiles >= ALL_PROFILES)
    )


def _as_list(value: Any) -> list[dict[str, Any]]:
    """ConvertTo-Json writes one element as an object and none as null; normalise."""
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    return [i for i in items if isinstance(i, dict)]


def parse_check(text: str, port: int) -> FirewallStatus:
    """Evaluate the check script's JSON: an enabled inbound block rule for TCP ``port`` on
    all profiles, every firewall profile on, and local rules not disabled by policy."""
    try:
        data = parse_json_output(text)
    except ValueError:
        return FirewallStatus(False, METHOD, [UNREADABLE, LOOPBACK_NOTE])
    if not isinstance(data, dict):
        return FirewallStatus(False, METHOD, [UNREADABLE, LOOPBACK_NOTE])
    details: list[str] = []
    rules = _as_list(data.get("rules"))  # not-a-fact: key of our own script's output
    good = [r for r in rules if _rule_ok(r, port)]
    if not rules:
        details.append(f"rule '{RULE_DISPLAY_NAME}': missing")
    for rule in rules:
        bad = f"does not block TCP {port} on all profiles"
        state = "ok" if rule in good else bad  # not-a-fact: detail text
        details.append(f"rule '{rule.get('Name')}': {state}")  # not-a-fact: our key
    profiles = _as_list(data.get("profiles"))  # not-a-fact: key of our own script's output
    off = [str(p.get("Name")) for p in profiles if str(p.get("Enabled")) != TRUE]  # PF-231
    no_local = [
        str(p.get("Name"))  # PF-231
        for p in profiles
        if str(p.get("AllowLocalFirewallRules")) == FALSE  # PF-231
    ]
    if not profiles:
        details.append("firewall profiles: not readable")  # not-a-fact: detail text
    elif off:
        details.append(f"firewall off for profile(s): {', '.join(off)}")
    else:
        details.append("firewall on for all profiles")  # not-a-fact: detail text
    if no_local:
        details.append(f"local firewall rules disabled by policy for: {', '.join(no_local)}")
    ok = bool(good) and bool(profiles) and not off and not no_local
    details.append(LOOPBACK_NOTE)
    if ok:
        details.append(f"inbound TCP {port} blocked for non-loopback sources")
    return FirewallStatus(ok, METHOD, details)


def check(port: int, runner: Runner = default_runner) -> FirewallStatus:
    """Unelevated check of the rule and the firewall profiles (no connection test)."""
    try:
        out = runner(powershell_argv(check_script()), 60)
    except (OSError, subprocess.TimeoutExpired):
        missing = "PowerShell not available"  # not-a-fact: detail text
        return FirewallStatus(False, METHOD, [missing, LOOPBACK_NOTE])
    if out.returncode != 0:
        return FirewallStatus(
            False, METHOD, [f"firewall query failed (exit {out.returncode})", LOOPBACK_NOTE]
        )
    return parse_check(out.stdout or "", port)


def listening_addresses(port: int, runner: Runner = default_runner) -> list[str]:
    """Local addresses with a TCP listener on ``port`` (PF-208)."""
    script = f"$port = {_valid_port(port)}\n" + LISTEN_SCRIPT
    try:
        out = runner(powershell_argv(script), 60)
        data = parse_json_output(out.stdout or "") if out.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return []
    items = data if isinstance(data, list) else [data] if data else []
    return [str(a) for a in items]


def listening_publicly(port: int, runner: Runner = default_runner) -> bool:
    """True if the websocket listens on a non-loopback address (the ``ss`` check on Linux)."""
    return any(a not in LOOPBACK for a in listening_addresses(port, runner))


def _elevated(script: str, what: str, runner: Runner) -> None:
    log.info("firewall: %s needs administrator rights, Windows shows a UAC prompt", what)
    try:
        res = runner(elevated_argv(script), 300)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"{what}: elevated PowerShell did not run ({exc})") from exc
    if res.returncode != 0:
        raise RuntimeError(
            f"{what} failed (exit {res.returncode}): UAC prompt declined or the "
            "firewall cmdlet failed"
        )


def apply(port: int, runner: Runner = default_runner) -> list[str]:
    """Create (or replace) the block rule. Elevated: UAC prompt. Returns what ran."""
    _elevated(apply_script(port), "New-NetFirewallRule", runner)  # PF-230
    log.info("firewall rule applied for port %d", port)
    return [
        "elevated PowerShell (UAC): New-NetFirewallRule "
        f"-Name {RULE_NAME} -DisplayName '{RULE_DISPLAY_NAME}' -Direction Inbound "
        f"-Action Block -Protocol TCP -LocalPort {port} -Profile Any"
    ]


def remove(runner: Runner = default_runner) -> list[str]:
    """Delete exactly the rule that :func:`apply` created (by its unique name). UAC prompt."""
    _elevated(remove_script(), "Remove-NetFirewallRule", runner)  # PF-230
    log.info("firewall rule removed")
    return [f"elevated PowerShell (UAC): Remove-NetFirewallRule -Name {RULE_NAME}"]
