"""Windows firewall rule: PowerShell argv (elevation, rule parameters) and output parsing."""

from __future__ import annotations

import base64
import json
import re
from typing import Any

import pytest

from captation.obs import firewall_windows as fw
from tests.unit.test_windows_probe import FakeRunner, decoded_script


def inner_script(argv: list[str]) -> str:
    """The script that the outer PowerShell starts elevated."""
    outer = decoded_script(argv)
    assert "Start-Process" in outer and "-Verb RunAs" in outer and "-Wait" in outer
    assert "-PassThru" in outer and "exit $p.ExitCode" in outer
    encoded = re.search(r"'-EncodedCommand','([A-Za-z0-9+/=]+)'", outer)
    assert encoded is not None
    return base64.b64decode(encoded.group(1)).decode("utf-16-le")


def check_json(rules: Any, profiles: Any = None) -> str:
    if profiles is None:
        profiles = [
            {"Name": n, "Enabled": "True", "AllowLocalFirewallRules": "NotConfigured"}
            for n in ("Domain", "Private", "Public")
        ]
    return json.dumps({"rules": rules, "profiles": profiles})


GOOD_RULE = {
    "Name": "EtabliCaptation-ObsWebsocket",
    "Enabled": "True",
    "Direction": "Inbound",
    "Action": "Block",
    "Profile": "Any",
    "Protocol": "TCP",
    "LocalPort": ["4455"],
}


# ---- apply / remove ----------------------------------------------------------------------------


@pytest.mark.req("R-32")
def test_apply_runs_one_elevated_new_netfirewallrule() -> None:
    runner = FakeRunner()
    ran = fw.apply(4455, runner)
    [argv] = runner.calls
    assert argv[0].endswith("\\System32\\WindowsPowerShell\\v1.0\\powershell.exe")
    script = inner_script(argv)
    assert "$ruleName = 'EtabliCaptation-ObsWebsocket'" in script
    assert "$displayName = 'Etabli Captation obs-websocket'" in script
    assert "$port = 4455" in script
    assert "-Direction Inbound -Action Block -Protocol TCP -LocalPort $port -Profile Any" in script
    assert "New-NetFirewallRule -Name $ruleName -DisplayName $displayName" in script
    # idempotent: an existing rule of ours is replaced, never duplicated
    assert script.index("Remove-NetFirewallRule") < script.index("New-NetFirewallRule")
    assert "$ErrorActionPreference = 'Stop'" in script
    assert ran and "UAC" in ran[0] and "-LocalPort 4455" in ran[0]


def test_remove_deletes_exactly_our_rule_by_name() -> None:
    runner = FakeRunner()
    ran = fw.remove(runner)
    script = inner_script(runner.calls[0])
    assert "Get-NetFirewallRule -Name $ruleName" in script and "Remove-NetFirewallRule" in script
    assert "New-NetFirewallRule" not in script and "DisplayName $displayName" not in script
    assert ran == [
        "elevated PowerShell (UAC): Remove-NetFirewallRule -Name EtabliCaptation-ObsWebsocket"
    ]


def test_declined_uac_or_failure_raises() -> None:
    runner = FakeRunner()
    runner.on(lambda argv: True, rc=1)
    with pytest.raises(RuntimeError, match="UAC prompt declined"):
        fw.apply(4455, runner)
    with pytest.raises(RuntimeError, match="Remove-NetFirewallRule failed"):
        fw.remove(runner)

    def missing(argv: list[str], timeout: float) -> Any:
        raise OSError("powershell.exe not found")

    with pytest.raises(RuntimeError, match="did not run"):
        fw.apply(4455, missing)


@pytest.mark.parametrize("port", [0, 65536, -1])
def test_invalid_port_is_rejected(port: int) -> None:
    with pytest.raises(ValueError):
        fw.apply_script(port)


def test_quotes_in_names_are_escaped() -> None:
    assert fw.ps_quote("it's") == "'it''s'"


# ---- check ---------------------------------------------------------------------------------------


@pytest.mark.req("R-32")
def test_check_ok_and_runs_unelevated() -> None:
    runner = FakeRunner()
    runner.on(lambda argv: True, out=check_json([GOOD_RULE]))
    status = fw.check(4455, runner)
    assert status.ok and status.method == "windows-firewall"
    [argv] = runner.calls
    script = decoded_script(argv)
    assert "RunAs" not in script
    assert "Get-NetFirewallRule -DisplayName $displayName" in script
    assert "Get-NetFirewallPortFilter" in script and "Get-NetFirewallProfile" in script
    assert any("not meaningful" in d for d in status.details)  # loopback note, always
    assert status.details[-1] == "inbound TCP 4455 blocked for non-loopback sources"


@pytest.mark.parametrize(
    ("rules", "profiles", "expect"),
    [
        ([], None, "missing"),
        (None, None, "missing"),
        ([{**GOOD_RULE, "Enabled": "False"}], None, "does not block"),
        ([{**GOOD_RULE, "Action": "Allow"}], None, "does not block"),
        ([{**GOOD_RULE, "Direction": "Outbound"}], None, "does not block"),
        ([{**GOOD_RULE, "LocalPort": ["4456"]}], None, "does not block"),
        ([{**GOOD_RULE, "Protocol": "UDP"}], None, "does not block"),
        ([{**GOOD_RULE, "Profile": "Private"}], None, "does not block"),
        (
            [GOOD_RULE],
            [{"Name": "Public", "Enabled": "False", "AllowLocalFirewallRules": "True"}],
            "firewall off for profile(s): Public",
        ),
        (
            [GOOD_RULE],
            [{"Name": "Domain", "Enabled": "True", "AllowLocalFirewallRules": "False"}],
            "disabled by policy for: Domain",
        ),
        ([GOOD_RULE], [], "not readable"),
    ],
)
def test_check_not_met(rules: Any, profiles: Any, expect: str) -> None:
    status = fw.parse_check(check_json(rules, profiles), 4455)
    assert status.ok is False
    assert any(expect in d for d in status.details), status.details


@pytest.mark.parametrize(
    "rule",
    [
        {**GOOD_RULE, "LocalPort": "4455"},  # a single port serialised as a string
        {**GOOD_RULE, "Profile": "Domain, Private, Public"},
        {**GOOD_RULE, "Protocol": "tcp"},
    ],
)
def test_check_accepts_equivalent_serialisations(rule: dict[str, Any]) -> None:
    assert fw.parse_check(check_json(rule), 4455).ok  # one rule as an object, not a list


def test_one_good_rule_among_others_is_enough() -> None:
    other = {**GOOD_RULE, "Name": "copy", "Enabled": "False"}
    status = fw.parse_check("﻿" + check_json([other, GOOD_RULE]), 4455)
    assert status.ok
    assert "rule 'copy': does not block TCP 4455 on all profiles" in status.details


@pytest.mark.parametrize("text", ["", "garbage", "[1, 2]", "null"])
def test_check_unreadable_output(text: str) -> None:
    status = fw.parse_check(text, 4455)
    assert not status.ok and status.details[0] == "could not read the firewall state"


def test_check_command_failure() -> None:
    runner = FakeRunner()
    runner.on(lambda argv: True, rc=1)
    assert not fw.check(4455, runner).ok

    def missing(argv: list[str], timeout: float) -> Any:
        raise OSError("no powershell")

    status = fw.check(4455, missing)
    assert not status.ok and status.details[0] == "PowerShell not available"


# ---- listening addresses -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("out", "public"),
    [
        ('["0.0.0.0","::"]', True),
        ('"127.0.0.1"', False),
        ('["127.0.0.1","::1"]', False),
        ("", False),
        ("[]", False),
    ],
)
def test_listening_publicly(out: str, public: bool) -> None:
    runner = FakeRunner()
    runner.on(lambda argv: True, out=out)
    assert fw.listening_publicly(4455, runner) is public
    script = decoded_script(runner.calls[0])
    assert "$port = 4455" in script and "Get-NetTCPConnection -State Listen" in script


def test_dispatch_from_the_shared_module(monkeypatch: pytest.MonkeyPatch) -> None:
    from captation.obs import firewall

    monkeypatch.setattr(firewall, "current_os", lambda: "windows")
    calls: list[int] = []

    def fake_check(port: int) -> firewall.FirewallStatus:
        calls.append(port)
        return firewall.FirewallStatus(True, fw.METHOD, [])

    monkeypatch.setattr(fw, "check", fake_check)
    assert firewall.check(4455).ok and calls == [4455]
