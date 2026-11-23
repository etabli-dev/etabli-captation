from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from captation import doctor
from captation.config import Config, data_text, parse_config
from captation.models import ObsInstallation
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
    checks = run_doctor(monkeypatch, cfg_with(tmp_path))
    assert checks["obs"].level == "error" and "obs.profiles" not in checks
    assert checks["exposure"].level == "error" and checks["obs.websocket"].level == "warn"
    assert checks["data_root"].level == "ok"
    assert doctor._version_tuple("33.0.3") == (33, 0, 3) and doctor._version_tuple(None) == ()


class FakeWs:
    def __init__(self, kinds: list[str]) -> None:
        self.kinds = kinds
        self.closed = False

    def request(self, request_type: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        if request_type == "GetVersion":
            return {"obsWebSocketVersion": "5.7.6"}
        return {"inputKinds": self.kinds}

    def close(self) -> None:
        self.closed = True


def config(tmp_path: Path, **changes: Any) -> Config:
    raw = yaml.safe_load(data_text("config.default.yaml"))
    raw["data_root"] = str(tmp_path / "data")
    for dotted, value in changes.items():
        node = raw
        *parents, key = dotted.split("__")
        for name in parents:
            node = node[name]
        node[key] = value
    return parse_config(raw)


def run_doctor(
    monkeypatch: pytest.MonkeyPatch,
    cfg: Config,
    *,
    inst: ObsInstallation | None = None,
    running: bool = False,
    ws: FakeWs | Exception | None = None,
    password: bool = True,
    firewall_ok: bool = False,
    missing_tools: frozenset[str] = frozenset(),
) -> dict[str, doctor.Check]:
    """``doctor.run_checks`` with OBS, keyring, firewall and tools replaced by fakes."""
    from captation.obs import launcher, manage

    def locate(cfg: object) -> ObsInstallation:
        if inst is None:
            raise launcher.ObsNotFound("OBS Studio not found: https://obsproject.com/download")
        return inst

    def connect(cfg: object) -> FakeWs:
        if isinstance(ws, Exception):
            raise ws
        assert ws is not None
        return ws

    def get_password(ws_cfg: object) -> str:
        if not password:
            raise doctor.credentials.PasswordMissing("run `captation obs set-password`")
        return "x" * 40

    real_which = shutil.which

    def which(name: str, *args: Any, **kw: Any) -> str | None:
        return None if name in missing_tools else real_which(name, *args, **kw)

    monkeypatch.setattr(manage, "locate", locate)
    monkeypatch.setattr(manage, "obs_running", lambda cfg: running)
    monkeypatch.setattr(manage, "connect", connect)
    monkeypatch.setattr(doctor.credentials, "get_password", get_password)
    monkeypatch.setattr(
        doctor.firewall,
        "check",
        lambda port: doctor.firewall.FirewallStatus(firewall_ok, "nftables", ["checked"]),
    )
    monkeypatch.setattr(doctor.shutil, "which", which)
    checks = doctor.run_checks(cfg, ui_probe=False)
    names = [c.name for c in checks]
    assert len(names) == len(set(names)), names  # one entry per check
    assert {c.level for c in checks} <= {"ok", "warn", "error"}
    return {c.name: c for c in checks}


def obs_install(tmp_path: Path, version: str = "33.0.3") -> ObsInstallation:
    return ObsInstallation(
        kind="flatpak",
        command=("flatpak", "run", "com.obsproject.Studio"),
        config_dir=str(tmp_path / "obs-studio"),
        version=version,
    )


@pytest.mark.req("R-27")
def test_doctor_reports_obs_crash_upload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = cfg_with(tmp_path)
    inst = obs_install(tmp_path)
    # Linux: OBS has neither an updater nor an automatic crash upload (PF-012)
    checks = run_doctor(monkeypatch, cfg, inst=inst)
    assert checks["obs.updater"].level == "ok" and "crash" in checks["obs.updater"].detail
    assert "obs.crash_upload" not in checks
    # macOS: the OBS ini files are read and whatever crash setting they hold is reported
    config_dir = Path(inst.config_dir)
    config_dir.mkdir(parents=True)
    (config_dir / "user.ini").write_text("[General]\nCrashUploadEnabled=true\nOther=1\n")
    monkeypatch.setattr(doctor, "current_os", lambda: "macos")
    checks = run_doctor(monkeypatch, cfg, inst=inst)
    crash = checks["obs.crash_upload"]
    assert crash.level == "warn" and "obs.updater" not in checks
    assert "user.ini:[General] CrashUploadEnabled=true" in crash.detail
    assert "Other" not in crash.detail and "crash-log upload" in crash.fix
    (config_dir / "user.ini").unlink()
    assert "not found" in run_doctor(monkeypatch, cfg, inst=inst)["obs.crash_upload"].detail
    # Windows: no reader for the setting yet, still a warning with the fix
    monkeypatch.setattr(doctor, "current_os", lambda: "windows")
    crash = run_doctor(monkeypatch, cfg, inst=inst)["obs.crash_upload"]
    assert crash.level == "warn" and "not found" in crash.detail and crash.fix


@pytest.mark.req("R-50")
def test_every_non_ok_doctor_check_carries_a_fix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from captation.obs.client import ObsError

    failing = config(
        tmp_path,
        recording__audio__local_input="Headset Mic",
        storage__min_free_gb=10**9,
        storage__require_disk_encryption="block",
    )
    runs = [
        run_doctor(
            monkeypatch,
            failing,
            password=False,
            missing_tools=frozenset({"ffmpeg", "ffprobe"}),
        ),
        run_doctor(monkeypatch, failing, inst=obs_install(tmp_path, "31.0.0")),
        run_doctor(
            monkeypatch,
            failing,
            inst=obs_install(tmp_path),
            running=True,
            ws=ObsError("auth failed"),
        ),
        run_doctor(
            monkeypatch,
            failing,
            inst=obs_install(tmp_path),
            running=True,
            ws=FakeWs(["pulse_input_capture"]),
        ),
    ]
    seen = {(c.name, c.level) for checks in runs for c in checks.values()}
    # the scenarios reach the non-ok branches of doctor.run_checks
    for name, level in [
        ("obs", "error"),
        ("obs.password", "error"),
        ("obs.profiles", "error"),
        ("obs.websocket", "error"),
        ("obs.websocket", "warn"),
        ("obs.app_audio", "warn"),
        ("exposure", "error"),
        ("ffmpeg", "error"),
        ("ffprobe", "error"),
        ("local_input", "warn"),
        ("free_space", "error"),
    ]:
        assert (name, level) in seen, (name, level)
    missing = sorted(
        {
            f"{c.name} ({c.level}: {c.detail})"
            for checks in runs
            for c in checks.values()
            if c.level != "ok" and not c.fix.strip()
        }
    )
    assert missing == []
