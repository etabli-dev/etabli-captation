"""OBS locator and OBS PID self-exclusion on Windows, with fake paths, registry and runner."""

from __future__ import annotations

import pytest

from captation.models import ObsInstallation
from captation.platform import windows as win
from captation.platform.windows import (
    ProcInfo,
    WindowsObsLocator,
    obs_host_pids,
    obs_install_root,
)
from tests.unit.test_windows_probe import FakeRegistry, FakeRunner, decoded_script

PF = "C:\\Program Files"
DEFAULT_EXE = "C:\\Program Files\\obs-studio\\bin\\64bit\\obs64.exe"
ENV = {"ProgramFiles": PF, "APPDATA": "C:\\Users\\alice\\AppData\\Roaming"}


def locator(
    existing: set[str],
    reg: FakeRegistry | None = None,
    runner: FakeRunner | None = None,
    executable: str | None = None,
    env: dict[str, str] | None = None,
) -> WindowsObsLocator:
    return WindowsObsLocator(
        executable,
        registry=reg or FakeRegistry(),  # type: ignore[arg-type]
        env=ENV if env is None else env,
        exists=lambda p: p in existing,
        runner=runner or FakeRunner(),
    )


def test_default_install_path_and_config_dir() -> None:
    runner = FakeRunner()
    runner.on(lambda argv: argv[0].endswith("powershell.exe"), out="33.0.3.0\r\n")
    inst = locator({DEFAULT_EXE}, runner=runner).find()
    assert inst == ObsInstallation(
        kind="native",
        command=(DEFAULT_EXE,),
        config_dir="C:\\Users\\alice\\AppData\\Roaming\\obs-studio",
        version="33.0.3",
    )
    script = decoded_script(runner.calls[0])
    assert f"$exePath = '{DEFAULT_EXE}'" in script and "VersionInfo.ProductVersion" in script


def test_registry_install_dir_is_used_when_program_files_has_no_obs() -> None:
    reg = FakeRegistry()
    reg.add("HKLM", "SOFTWARE\\OBS Studio", **{"": "D:\\Apps\\OBS"})
    exe = "D:\\Apps\\OBS\\bin\\64bit\\obs64.exe"
    loc = locator({exe}, reg=reg)
    assert loc.candidates() == [DEFAULT_EXE, exe]
    inst = loc.find()
    assert inst is not None and inst.command == (exe,)


def test_uninstall_key_location_and_display_version() -> None:
    reg = FakeRegistry()
    reg.add(
        "HKLM",
        "SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\OBS Studio",
        InstallLocation='"E:\\OBS Studio"',
        DisplayVersion="33.0.3",
    )
    exe = "E:\\OBS Studio\\bin\\64bit\\obs64.exe"
    runner = FakeRunner()
    runner.on(lambda argv: argv[0].endswith("powershell.exe"), rc=1)
    inst = locator({exe}, reg=reg, runner=runner).find()
    assert inst is not None and inst.command == (exe,) and inst.version == "33.0.3"
    assert all(not argv[0].endswith("obs64.exe") for argv in runner.calls)


def test_version_flag_is_the_last_fallback() -> None:
    runner = FakeRunner()
    runner.on(lambda argv: argv[0].endswith("powershell.exe"), rc=1)
    runner.on(lambda argv: argv[1:] == ["--version"], out="OBS Studio - 33.0.3\n")
    inst = locator({DEFAULT_EXE}, runner=runner).find()
    assert inst is not None and inst.version == "33.0.3"
    assert runner.calls[-1] == [DEFAULT_EXE, "--version"]


def test_unknown_version_and_explicit_executable() -> None:
    runner = FakeRunner()
    inst = locator(set(), runner=runner, executable="C:\\portable\\obs64.exe").find()
    assert inst is not None and inst.command == ("C:\\portable\\obs64.exe",)
    assert inst.version is None


def test_not_installed() -> None:
    assert locator(set()).find() is None
    assert locator(set(), env={}).candidates() == []


def test_duplicate_candidates_are_dropped() -> None:
    reg = FakeRegistry()
    reg.add("HKLM", "SOFTWARE\\OBS Studio", **{"": "c:\\program files\\OBS-Studio"})
    assert locator(set(), reg=reg).candidates() == [DEFAULT_EXE]


@pytest.mark.parametrize(
    ("exe", "root"),
    [
        (DEFAULT_EXE, "C:\\Program Files\\obs-studio"),
        ("D:/OBS/bin/64bit/OBS64.EXE", "D:\\OBS"),
        ("C:\\obs64.exe", None),
        ("C:\\tools\\obs\\obs64.exe", None),
    ],
)
def test_obs_install_root(exe: str, root: str | None) -> None:
    assert obs_install_root(exe) == root


@pytest.mark.req("R-35")
def test_obs_host_pids_include_helpers_below_the_install_root() -> None:
    inst = ObsInstallation("native", (DEFAULT_EXE,), "C:\\cfg", "33.0.3")
    procs = [
        ProcInfo(10, 1, "obs64.exe", DEFAULT_EXE),
        ProcInfo(11, 10, "obs-ffmpeg-mux.exe", f"{PF}\\obs-studio\\bin\\64bit\\obs-ffmpeg-mux.exe"),
        ProcInfo(12, 10, "obs-browser-page.exe", f"{PF}\\OBS-Studio\\obs-plugins\\64bit\\x.exe"),
        ProcInfo(13, 1, "obs64.exe", None),  # another user's OBS: matched by name
        ProcInfo(20, 1, "Zoom.exe", "C:\\Users\\alice\\AppData\\Roaming\\Zoom\\bin\\Zoom.exe"),
        ProcInfo(21, 1, "evil.exe", f"{PF}\\obs-studio-fake\\evil.exe"),
    ]
    assert obs_host_pids(inst, lambda: procs) == frozenset({10, 11, 12, 13})
    odd = ObsInstallation("native", ("C:\\obs64.exe",), "C:\\cfg", None)
    assert obs_host_pids(odd, lambda: procs) == frozenset({10, 13})


def test_locator_on_the_services_bundle() -> None:
    assert isinstance(win.SERVICES.obs_locator("C:\\x\\obs64.exe"), WindowsObsLocator)
