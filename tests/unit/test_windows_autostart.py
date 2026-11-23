"""Task Scheduler autostart, AppUserModelID registration and the BitLocker check,
tested on Linux with a fake runner and a fake registry."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from captation.platform import windows as win
from captation.platform.windows import (
    AgentLaunch,
    WindowsAutostart,
    WindowsDiskEncryption,
    agent_launch,
    current_user_id,
    drive_of,
    parse_bitlocker,
    task_xml,
)
from tests.unit.test_windows_probe import FakeRegistry, FakeRunner, decoded_script

NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
LAUNCH = AgentLaunch(
    "C:\\Users\\alice\\AppData\\Local\\Programs\\Etabli Captation\\captationw.exe",
    "run",
    "C:\\Users\\alice\\AppData\\Local\\Programs\\Etabli Captation",
)


def _text(root: ET.Element, path: str) -> str:
    node = root.find(path, NS)
    assert node is not None, path
    return node.text or ""


# ---- task XML --------------------------------------------------------------------------------


@pytest.mark.req("R-49")
def test_task_xml_settings() -> None:
    root = ET.fromstring(task_xml(LAUNCH, "DESKTOP-1\\alice").split("\n", 1)[1])
    assert root.tag == f"{{{NS['t']}}}Task" and root.get("version") == "1.2"
    assert _text(root, "t:RegistrationInfo/t:URI") == "\\Etabli Captation"
    assert _text(root, "t:Triggers/t:LogonTrigger/t:UserId") == "DESKTOP-1\\alice"
    assert _text(root, "t:Triggers/t:LogonTrigger/t:Enabled") == "true"
    assert _text(root, "t:Principals/t:Principal/t:UserId") == "DESKTOP-1\\alice"
    assert _text(root, "t:Principals/t:Principal/t:LogonType") == "InteractiveToken"
    assert _text(root, "t:Principals/t:Principal/t:RunLevel") == "LeastPrivilege"
    settings = {
        "DisallowStartIfOnBatteries": "false",
        "StopIfGoingOnBatteries": "false",
        "ExecutionTimeLimit": "PT0S",
        "MultipleInstancesPolicy": "IgnoreNew",
        "RestartOnFailure/t:Interval": "PT1M",
        "RestartOnFailure/t:Count": "3",
    }
    for name, value in settings.items():
        assert _text(root, f"t:Settings/t:{name}") == value, name
    assert _text(root, "t:Actions/t:Exec/t:Command") == LAUNCH.command
    # autostart never arms: the only argument is the run command
    assert _text(root, "t:Actions/t:Exec/t:Arguments") == "run"
    assert _text(root, "t:Actions/t:Exec/t:WorkingDirectory") == LAUNCH.workdir


def test_task_xml_escapes_values() -> None:
    launch = AgentLaunch("C:\\A&B\\<x>\\captationw.exe", "run", "C:\\A&B")
    root = ET.fromstring(task_xml(launch, "DOM\\a&b").split("\n", 1)[1])
    assert _text(root, "t:Actions/t:Exec/t:Command") == launch.command
    assert _text(root, "t:Triggers/t:LogonTrigger/t:UserId") == "DOM\\a&b"


def test_task_xml_declares_utf16() -> None:
    assert task_xml(LAUNCH, "u").startswith('<?xml version="1.0" encoding="UTF-16"?>')


# ---- launch command and user ------------------------------------------------------------------


def test_agent_launch_frozen_prefers_the_windowed_exe(tmp_path: Path) -> None:
    exe = tmp_path / "captation.exe"
    exe.write_text("")
    assert agent_launch(str(exe), frozen=True) == AgentLaunch(str(exe), "run", str(tmp_path))
    (tmp_path / "captationw.exe").write_text("")
    launch = agent_launch(str(exe), frozen=True)
    assert launch.command == str(tmp_path / "captationw.exe") and launch.arguments == "run"


def test_agent_launch_dev_uses_pythonw(tmp_path: Path) -> None:
    exe = tmp_path / "python.exe"
    exe.write_text("")
    assert agent_launch(str(exe), frozen=False).command == str(exe)
    (tmp_path / "pythonw.exe").write_text("")
    launch = agent_launch(str(exe), frozen=False)
    assert launch.command == str(tmp_path / "pythonw.exe")
    assert launch.arguments == "-m captation run"


def test_current_user_id() -> None:
    assert current_user_id({"USERDOMAIN": "DESKTOP-1", "USERNAME": "alice"}) == "DESKTOP-1\\alice"
    assert current_user_id({"USERNAME": "alice"}) == "alice"


# ---- install / uninstall / status ---------------------------------------------------------------


def _autostart() -> tuple[WindowsAutostart, FakeRunner, FakeRegistry, dict[str, str]]:
    runner = FakeRunner()
    reg = FakeRegistry()
    state: dict[str, str] = {}

    def is_cmd(word: str) -> object:
        return lambda argv: argv[0].endswith("schtasks.exe") and argv[1] == word

    def create(argv: list[str]) -> bool:
        if not (argv[0].endswith("schtasks.exe") and argv[1] == "/Create"):
            return False
        state["xml"] = Path(argv[3]).read_text(encoding="utf-16")
        state["task"] = "yes"
        return True

    def delete(argv: list[str]) -> bool:
        if argv[0].endswith("schtasks.exe") and argv[1] == "/Delete":
            state.pop("task", None)
            return True
        return False

    runner.on(create)
    runner.on(delete)
    runner.on(lambda argv: is_cmd("/Query")(argv) and "task" not in state, rc=1)  # type: ignore[operator]
    auto = WindowsAutostart(runner=runner, registry=reg, launch=LAUNCH, user_id="DOM\\alice")
    return auto, runner, reg, state


@pytest.mark.req("R-49")
def test_install_is_idempotent_and_uninstall_leaves_no_residue() -> None:
    auto, runner, reg, state = _autostart()
    assert auto.status().installed is False and auto.status().method == "none"
    first = auto.install()
    second = auto.install()
    assert first == second == [Path("\\Etabli Captation"), win.AUMID_ARTEFACT]
    assert "LogonTrigger" in state["xml"] and "<Arguments>run</Arguments>" in state["xml"]
    assert reg.values("HKCU", win.AUMID_KEY) == {"DisplayName": "Établi Captation"}
    status = auto.status()
    assert status.installed and status.method == "task"
    assert status.artefacts == (str(Path("\\Etabli Captation")),)
    create = next(c for c in runner.calls if c[1] == "/Create")
    assert create[0] == win.schtasks_exe() and create[0].endswith("\\System32\\schtasks.exe")
    assert create[1:3] == ["/Create", "/XML"] and create[4:] == ["/TN", "Etabli Captation", "/F"]
    assert not Path(create[3]).exists()  # the temporary XML is gone
    assert auto.uninstall() == first
    assert auto.uninstall() == []
    assert reg.keys == {} and auto.status().installed is False
    assert [c[1:] for c in runner.calls if c[1] == "/Delete"] == [
        ["/Delete", "/TN", "Etabli Captation", "/F"]
    ]


def test_install_failure_raises_and_registers_nothing() -> None:
    runner = FakeRunner()
    runner.on(lambda argv: argv[1] == "/Create", rc=1)
    reg = FakeRegistry()
    auto = WindowsAutostart(runner=runner, registry=reg, launch=LAUNCH, user_id="u")
    with pytest.raises(RuntimeError, match="schtasks /Create failed"):
        auto.install()
    assert reg.keys == {}


def test_systemd_flag_is_ignored_with_a_warning(caplog: pytest.LogCaptureFixture) -> None:
    auto, _runner, _reg, _state = _autostart()
    assert auto.install(systemd=True)[0] == Path("\\Etabli Captation")
    assert "Linux only" in caplog.text


def test_aumid_registry_values() -> None:
    reg = FakeRegistry()
    win.register_aumid(reg)  # type: ignore[arg-type]
    assert win.AUMID_KEY == "Software\\Classes\\AppUserModelId\\de.etabli.captation"
    assert reg.values("HKCU", win.AUMID_KEY) == {"DisplayName": "Établi Captation"}
    assert win.unregister_aumid(reg) is True  # type: ignore[arg-type]
    assert win.unregister_aumid(reg) is False  # type: ignore[arg-type]


def test_services_bundle() -> None:
    services = win.SERVICES
    assert services.name == "windows"
    assert isinstance(services.autostart(), WindowsAutostart)
    assert isinstance(services.disk_encryption(), WindowsDiskEncryption)
    assert isinstance(
        services.probe(exclude_pids=frozenset(), store_titles=False), win.WindowsProbe
    )
    assert isinstance(services.obs_locator(None), win.WindowsObsLocator)


# ---- BitLocker -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "drive"),
    [
        ("C:\\Users\\alice\\Recordings", "C:"),
        ("d:/data", "D:"),
        ("\\\\server\\share\\x", None),
        ("relative\\x", None),
    ],
)
def test_drive_of(path: str, drive: str | None) -> None:
    assert drive_of(path) == drive


@pytest.mark.req("R-46")
@pytest.mark.parametrize(
    ("output", "status"),
    [
        ('{"source":"cmdlet","protection":"On","volume":"FullyEncrypted","shell":""}', "encrypted"),
        (
            '{"source":"cmdlet","protection":"Off","volume":"FullyDecrypted","shell":""}',
            "unencrypted",
        ),
        ('{"source":"cmdlet","protection":"Off","volume":"FullyEncrypted","shell":""}', "unknown"),
        ('{"source":"shell","protection":"","volume":"","shell":"1"}', "encrypted"),
        ('{"source":"shell","protection":"","volume":"","shell":"2"}', "unencrypted"),
        ('{"source":"shell","protection":"","volume":"","shell":"6"}', "unknown"),
        ('{"source":"none","protection":"","volume":"","shell":""}', "unknown"),
        ('\ufeff{"source":"cmdlet","protection":"On","volume":"x","shell":""}\r\n', "encrypted"),
        ("not json", "unknown"),
        ("[]", "unknown"),
        ("", "unknown"),
    ],
)
def test_parse_bitlocker(output: str, status: str) -> None:
    assert parse_bitlocker(output) == status


def test_disk_encryption_runs_one_unelevated_powershell() -> None:
    runner = FakeRunner()
    runner.on(lambda argv: True, out='{"source":"shell","shell":"1"}')
    enc = WindowsDiskEncryption(runner)
    assert enc.status(Path("D:\\Recordings")) == "encrypted"  # type: ignore[arg-type]
    [argv] = runner.calls
    assert argv[0].endswith("powershell.exe") and "-NoProfile" in argv
    script = decoded_script(argv)
    assert "$drive = 'D:'" in script and "Get-BitLockerVolume -MountPoint $drive" in script
    assert "System.Volume.BitLockerProtection" in script
    assert "RunAs" not in script  # never elevated


def test_disk_encryption_unknown_paths() -> None:
    runner = FakeRunner()
    enc = WindowsDiskEncryption(runner)
    assert enc.status(Path("relative")) == "unknown" and runner.calls == []
    runner.on(lambda argv: True, rc=1, out='{"source":"cmdlet","protection":"On"}')
    assert enc.status("C:\\x") == "unknown"  # type: ignore[arg-type]

    def broken(argv: list[str], timeout: float) -> object:
        raise OSError("no powershell")

    assert WindowsDiskEncryption(broken).status("C:\\x") == "unknown"  # type: ignore[arg-type]
