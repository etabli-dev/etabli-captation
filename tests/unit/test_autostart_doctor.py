from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from captation import doctor
from captation.platform.linux import LinuxAutostart, desktop_entry, systemd_unit
from tests.unit.test_obs_controller import cfg_with

pytestmark = pytest.mark.linux


@pytest.mark.req("R-49")
def test_xdg_autostart_install_is_idempotent_and_leaves_no_residue(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    auto = LinuxAutostart(
        home=tmp_path,
        runner=lambda a, t: calls.append(a) or subprocess.CompletedProcess(a, 0, "", ""),
    )  # type: ignore[func-returns-value]
    before = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*"))
    assert auto.status().installed is False
    first = auto.install()
    second = auto.install()
    assert first == second == [tmp_path / ".config/autostart/de.etabli.captation.desktop"]
    text = first[0].read_text()
    assert (
        "Exec=" in text
        and text.strip().endswith("X-GNOME-Autostart-enabled=true")
        and " run" in text
    )
    exec_line = next(line for line in text.splitlines() if line.startswith("Exec="))
    assert exec_line.endswith(" run") and " arm" not in exec_line  # autostart never arms
    assert auto.status().method == "xdg"
    assert auto.uninstall() == first and auto.uninstall() == []
    after = [
        p
        for p in sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*"))
        if p.name and not (tmp_path / p).is_dir()
    ]
    assert [p for p in before if not (tmp_path / p).is_dir()] == after == []
    assert calls == []


@pytest.mark.req("R-49")
def test_systemd_user_unit(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    auto = LinuxAutostart(
        home=tmp_path,
        runner=lambda a, t: calls.append(a) or subprocess.CompletedProcess(a, 0, "", ""),
    )  # type: ignore[func-returns-value]
    [unit] = auto.install(systemd=True)
    assert unit.name == "etabli-captation.service" and auto.status().method == "systemd"
    assert ["systemctl", "--user", "enable", "--now", "etabli-captation.service"] in calls
    assert auto.uninstall() == [unit] and not unit.exists()
    assert ["systemctl", "--user", "disable", "--now", "etabli-captation.service"] in calls


def test_unit_and_entry_text() -> None:
    unit = systemd_unit("/opt/captation/captation")
    assert (
        "ExecStart=/opt/captation/captation run" in unit
        and "PartOf=graphical-session.target" in unit
    )
    assert "Restart=on-failure" in unit and "WantedBy=graphical-session.target" in unit
    assert desktop_entry("/x/captation").startswith("[Desktop Entry]\nType=Application\n")


@pytest.mark.req("R-50")
def test_doctor_exit_codes_and_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    checks = [doctor.Check("a", "ok", "fine"), doctor.Check("b", "warn", "hmm", "do x")]
    assert doctor.exit_code(checks) == 1
    assert doctor.exit_code([*checks, doctor.Check("c", "error", "bad", "fix")]) == 2
    assert doctor.exit_code(checks[:1]) == 0
    doc = json.loads(doctor.render(checks, True))
    assert doc["exit_code"] == 1 and doc["checks"][1]["fix"] == "do x"
    text = doctor.render(checks, False)
    assert "→ do x" in text and "fine" in text


def test_doctor_run_checks_without_obs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from captation.obs import launcher, manage

    def missing(cfg: object) -> None:
        raise launcher.ObsNotFound("OBS Studio not found")

    monkeypatch.setattr(manage, "locate", missing)
    monkeypatch.setattr(manage, "obs_running", lambda cfg: False)
    monkeypatch.setattr(doctor.credentials, "get_password", lambda ws: "x" * 40)
    monkeypatch.setattr(
        doctor.firewall,
        "check",
        lambda port: doctor.firewall.FirewallStatus(False, "nftables", ["missing"]),
    )
    checks = {c.name: c for c in doctor.run_checks(cfg_with(tmp_path), ui_probe=False)}
    assert checks["obs"].level == "error" and "obs.profiles" not in checks
    assert checks["exposure"].level == "error" and checks["obs.websocket"].level == "warn"
    assert checks["data_root"].level == "ok"
    assert doctor._version_tuple("33.0.3") == (33, 0, 3) and doctor._version_tuple(None) == ()
