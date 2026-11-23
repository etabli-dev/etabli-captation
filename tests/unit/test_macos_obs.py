"""macOS OBS side with fakes: capture profile through ObsController, pf firewall (ADR-015)."""

from __future__ import annotations

import plistlib
import subprocess
from pathlib import Path

import pytest

from captation.models import AppRef
from captation.obs import firewall, firewall_macos, kinds, kinds_macos
from captation.obs.controller import LOCAL, REMOTE, SCREEN, ObsController
from captation.obs.firewall_macos import (
    ANCHOR,
    DAEMON_PLIST,
    RULE_FILE,
    artefacts_ok,
    boot_script,
    daemon_definition,
    parse_ifconfig,
    parse_lsof_listen,
    pf_rules,
)
from tests.fakes import FakeObs
from tests.unit.test_obs_controller import cfg_with

MAC_KINDS = [
    "sck_audio_capture",
    "coreaudio_input_capture",
    "coreaudio_output_capture",
    "screen_capture",
]
ZOOM = AppRef(pid=2101, process_name="zoom.us", bundle_id="us.zoom.xos")
CLI_TOOL = AppRef(pid=7, process_name="fakecall-e2e")  # no bundle ID: cannot be targeted


def mac_controller(tmp_path: Path, kinds_list: list[str] | None = None, **cfg: object):
    fake = FakeObs(kinds=MAC_KINDS if kinds_list is None else kinds_list)
    ctl = ObsController(cfg_with(tmp_path, **cfg), lambda: fake, os_name="macos")
    ctl.ensure_running()
    return ctl, fake


# ---- capture profile -------------------------------------------------------------------------


def test_profile_is_selected_and_shaped() -> None:
    prof = kinds.capture_profile("macos")
    assert prof is kinds_macos.PROFILE and prof.os_name == "macos"
    assert prof.needs_screen_token is False and prof.screen_token_key is None
    assert prof.app_audio_kinds == ("sck_audio_capture",)
    assert prof.mic_kinds == ("coreaudio_input_capture",)
    assert prof.window_kinds == prof.screen_kinds == ("screen_capture",)
    assert prof.app_audio_settings("zoom.us", "us.zoom.xos", None) == {
        "type": 1,
        "application": "us.zoom.xos",
    }
    assert prof.app_audio_settings("tool", None, None) == {"type": 0}
    assert prof.window_settings("zoom.us", "us.zoom.xos", None) == {
        "type": 2,
        "application": "us.zoom.xos",
        "show_cursor": True,
    }
    assert prof.window_settings("tool", None, None) is None
    assert prof.screen_settings(None) == {"type": 0, "show_cursor": True}
    assert set(kinds_macos.FACT_LIST_PROPERTIES) <= set(MAC_KINDS)
    assert "--disable-updater" in kinds_macos.LAUNCH_FLAGS


@pytest.mark.req("R-12", "R-13")
def test_audio_segment_targets_the_app_bundle(tmp_path: Path) -> None:
    ctl, fake = mac_controller(tmp_path)
    prep = ctl.prepare_segment("audio", ZOOM)
    assert prep.capture == "none" and prep.warnings == ()
    assert fake.inputs[REMOTE]["kind"] == "sck_audio_capture"
    assert fake.inputs[REMOTE]["settings"] == {"type": 1, "application": "us.zoom.xos"}
    assert fake.inputs[LOCAL]["kind"] == "coreaudio_input_capture"
    assert fake.inputs[LOCAL]["settings"] == {"device_id": "default"}
    ctl.teardown_segment()
    assert fake.inputs == {}  # OBS releases the mic between segments (ADR-010)


@pytest.mark.req("R-12")
def test_desktop_audio_fallback_without_app_kind(tmp_path: Path) -> None:
    ctl, fake = mac_controller(tmp_path, ["coreaudio_input_capture", "coreaudio_output_capture"])
    prep = ctl.prepare_segment("audio", ZOOM)
    assert "remote:desktop_fallback" in prep.warnings
    assert fake.inputs[REMOTE]["kind"] == "coreaudio_output_capture"


@pytest.mark.req("R-28")
def test_video_segment_captures_the_app_not_the_display(tmp_path: Path) -> None:
    ctl, fake = mac_controller(tmp_path)
    prep = ctl.prepare_segment("video", ZOOM)
    assert prep.capture == "window" and prep.warnings == ()
    screen = fake.inputs[SCREEN]
    assert screen["kind"] == "screen_capture"
    assert screen["settings"] == {"type": 2, "application": "us.zoom.xos", "show_cursor": True}


@pytest.mark.req("R-28")
def test_video_without_bundle_falls_back_to_display_with_warning(tmp_path: Path) -> None:
    ctl, fake = mac_controller(tmp_path)
    prep = ctl.prepare_segment("video", CLI_TOOL)
    assert prep.capture == "display" and "capture:display_fallback" in prep.warnings
    assert fake.inputs[SCREEN]["settings"] == {"type": 0, "show_cursor": True}
    ctl.teardown_segment()
    ctl.prepare_segment("video", ZOOM)
    ctl.teardown_segment()


def test_display_capture_config_needs_no_token(tmp_path: Path) -> None:
    ctl, fake = mac_controller(tmp_path, capture="display")
    prep = ctl.prepare_segment("video", ZOOM)
    assert prep.capture == "display" and "capture:not_approved" not in prep.warnings
    assert fake.inputs[SCREEN]["settings"]["type"] == 0
    assert ctl.approve_screen() is False  # no portal approval step on macOS


# ---- pf firewall ------------------------------------------------------------------------------


def test_pf_rule_and_daemon_text() -> None:
    rules = pf_rules(4455)
    assert "block drop in quick proto tcp from ! 127.0.0.1 to any port 4455\n" in rules
    assert "block drop in quick inet6 proto tcp from ! ::1 to any port 4455\n" in rules
    script = boot_script()
    assert f"/sbin/pfctl -q -a {ANCHOR} -f {RULE_FILE}" in script
    assert "/sbin/pfctl -E" in script and "/sbin/pfctl -X" in script
    assert "|| /sbin/pfctl -q -f /etc/pf.conf" in script  # stock main ruleset, never edited
    d = daemon_definition()
    assert d["Label"] == "de.etabli.captation.pf" and d["RunAtLoad"] is True
    assert d["ProgramArguments"][:2] == ["/bin/sh", "-c"]
    # root runs absolute paths only (never a binary from the user's PATH)
    commands = {w.strip('"$(') for w in script.split() if "bin/" in w}
    assert commands == {"/sbin/pfctl", "/usr/bin/grep", "/usr/bin/sed", "/bin/cat"}


def write_artefacts(tmp_path: Path, port: int) -> tuple[Path, Path]:
    rule_file = tmp_path / "rules"
    plist = tmp_path / "daemon.plist"
    rule_file.write_text(pf_rules(port), encoding="utf-8")
    definition = daemon_definition()
    definition["ProgramArguments"][2] = definition["ProgramArguments"][2].replace(
        str(RULE_FILE), str(rule_file)
    )
    plist.write_bytes(plistlib.dumps(definition))
    return rule_file, plist


IFCONFIG = """lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384
\tinet 127.0.0.1 netmask 0xff000000
\tinet6 ::1 prefixlen 128
\tinet6 fe80::1%lo0 prefixlen 64 scopeid 0x1
en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tether 00:00:00:00:00:00
\tinet6 fe80::1c2b:3a4d:5e6f:7081%en0 prefixlen 64 secured scopeid 0xe
\tinet 192.0.2.23 netmask 0xffffff00 broadcast 192.0.2.255
\tinet6 2001:db8::5 prefixlen 64 autoconf secured
\tinet 169.254.10.20 netmask 0xffff0000
utun3: flags=8051<UP,POINTOPOINT,RUNNING,MULTICAST> mtu 1280
\tinet 100.64.0.23 --> 100.64.0.23 netmask 0xffffffff
"""
LSOF_PUBLIC = (
    "COMMAND  PID USER   FD   TYPE DEVICE SIZE/OFF NODE NAME\n"
    "OBS     4001 u   45u  IPv6 0x1      0t0  TCP *:4455 (LISTEN)\n"
)
LSOF_LOOPBACK = (
    "COMMAND  PID USER   FD   TYPE DEVICE SIZE/OFF NODE NAME\n"
    "OBS     4001 u   45u  IPv4 0x1      0t0  TCP 127.0.0.1:4455 (LISTEN)\n"
    "OBS     4001 u   46u  IPv6 0x2      0t0  TCP [::1]:4455 (LISTEN)\n"
)


def test_parse_ifconfig_and_lsof() -> None:
    assert parse_ifconfig(IFCONFIG) == ["192.0.2.23", "2001:db8::5", "100.64.0.23"]
    assert parse_lsof_listen(LSOF_PUBLIC) == ["*"]
    assert parse_lsof_listen(LSOF_LOOPBACK) == ["127.0.0.1", "::1"]


def fw_runner(lsof: str) -> firewall_macos.Runner:
    def run(argv: list[str]) -> subprocess.CompletedProcess[str]:
        out = lsof if argv[0].endswith("lsof") else IFCONFIG
        return subprocess.CompletedProcess(argv, 0, out, "")

    return run


@pytest.mark.req("R-32")
def test_check_needs_artefacts_and_blocked_connections(tmp_path: Path) -> None:
    rule_file, plist = write_artefacts(tmp_path, 4455)
    assert artefacts_ok(4455, rule_file=rule_file, plist=plist)
    assert not artefacts_ok(4456, rule_file=rule_file, plist=plist)

    def check(lsof: str, reachable: set[str], port: int = 4455) -> firewall.FirewallStatus:
        return firewall_macos.check(
            port,
            runner=fw_runner(lsof),
            connect=lambda a, p: a in reachable,
            rule_file=rule_file,
            plist=plist,
        )

    ok = check(LSOF_PUBLIC, set())
    assert ok.ok and ok.method == "pf" and "blocked" in ok.details[-1]
    bad = check(LSOF_PUBLIC, {"192.0.2.23"})
    assert not bad.ok and "192.0.2.23" in bad.details[-1]
    skipped = check(LSOF_LOOPBACK, {"192.0.2.23"})
    assert skipped.ok and "skipped" in skipped.details[-1]
    missing = firewall_macos.check(
        4455, runner=fw_runner(LSOF_PUBLIC), rule_file=tmp_path / "x", plist=tmp_path / "y"
    )
    assert not missing.ok and "missing" in missing.details[0]


def test_apply_and_remove_commands(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    shown = {"n": 0}

    def run(argv: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if argv[-1] == "-sr":
            shown["n"] += 1
            out = "" if shown["n"] < 2 else "block drop in quick ... port = 4455\n"
            return subprocess.CompletedProcess(argv, 0, out, "")
        code = 3 if "bootout" in argv else 0  # not loaded yet: tolerated
        return subprocess.CompletedProcess(argv, code, "", "")

    ran = firewall_macos.apply(4455, runner=run, sleep=lambda s: None)
    assert calls[0][:2] == ["sudo", "/usr/bin/install"] and calls[0][-1] == str(RULE_FILE)
    assert calls[1][-1] == str(DAEMON_PLIST)
    assert ["sudo", "/bin/launchctl", "bootout", "system/de.etabli.captation.pf"] in calls
    assert ["sudo", "/bin/launchctl", "bootstrap", "system", str(DAEMON_PLIST)] in calls
    assert calls[-1] == ["sudo", "/sbin/pfctl", "-a", ANCHOR, "-sr"] and len(ran) == 5

    def never(argv: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 0, "", "")

    ticks = iter(range(100))
    with pytest.raises(RuntimeError, match="has no rules"):
        firewall_macos.apply(4455, runner=never, sleep=lambda s: None, clock=lambda: next(ticks))

    token = tmp_path / "token"
    token.write_text("123456789\n", encoding="utf-8")
    calls.clear()
    firewall_macos.remove(runner=run, token_file=token)
    assert calls == [
        ["sudo", "/bin/launchctl", "bootout", "system/de.etabli.captation.pf"],
        ["sudo", "/sbin/pfctl", "-a", ANCHOR, "-F", "all"],
        ["sudo", "/sbin/pfctl", "-X", "123456789"],
        ["sudo", "/bin/rm", "-f", str(token), str(RULE_FILE), str(DAEMON_PLIST)],
    ]
    calls.clear()
    firewall_macos.remove(runner=run, token_file=tmp_path / "none")
    assert not any("-X" in c for c in calls)


def test_dispatcher_uses_the_macos_module(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(firewall, "current_os", lambda: "macos")
    status = firewall.check(4455)  # no artefacts on this machine: refused, nothing executed
    assert status.method == "pf" and not status.ok
