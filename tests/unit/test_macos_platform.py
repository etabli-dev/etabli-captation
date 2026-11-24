"""macOS platform services with fakes: OBS locator, OBS PIDs, FileVault, LaunchAgent.

Pure logic: runs on every OS (ADR-015). Tests marked ``macos`` run only on the Mac.
"""

from __future__ import annotations

import plistlib
import subprocess
import sys
from pathlib import Path

import pytest

from captation.models import ObsInstallation
from captation.obs import launcher
from captation.platform import macos
from captation.platform.macos import (
    MacAutostart,
    MacDiskEncryption,
    MacObsLocator,
    MacPlatform,
    app_path_of,
    crash_upload_settings,
    launch_agent_plist,
    launch_path,
    obs_host_pids,
    parse_fdesetup,
    parse_ps,
)

Calls = list[list[str]]

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="macOS bundle paths and process trees are POSIX"
)


def runner_for(calls: Calls, replies: dict[str, tuple[int, str]] | None = None) -> macos.Runner:
    """A fake subprocess runner: the reply is chosen by the first matching argv word."""

    def run(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        for key, (code, out) in (replies or {}).items():
            if key in argv:
                return subprocess.CompletedProcess(argv, code, out, "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    return run


def make_obs_app(base: Path, version: str | None = "33.0.3") -> Path:
    app = base / "OBS.app"
    exe = app / "Contents" / "MacOS" / "OBS"
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/sh\n", encoding="utf-8")
    exe.chmod(0o755)
    if version is not None:
        info = {"CFBundleIdentifier": "com.obsproject.obs-studio"}
        info["CFBundleShortVersionString"] = version
        (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps(info))
    return app


# ---- OBS locator ---------------------------------------------------------------------------


def test_locator_finds_user_applications_and_reads_info_plist(tmp_path: Path) -> None:
    app = make_obs_app(tmp_path / "Applications")
    calls: Calls = []
    loc = MacObsLocator(
        home=tmp_path, runner=runner_for(calls), app_dirs=("/nonexistent", "~/Applications")
    )
    inst = loc.find()
    assert inst is not None and inst.kind == "native" and inst.version == "33.0.3"
    assert inst.command == ("/usr/bin/open", "-g", "-a", str(app), "--args")
    assert inst.config_dir == str(tmp_path / "Library/Application Support/obs-studio")
    assert calls == []  # Info.plist was enough: the GUI binary was not executed
    argv = [*inst.command, *launcher.launch_args("Captation-Audio", "Captation", [])]
    assert argv[:5] == list(inst.command) and "--disable-updater" in argv[5:]
    assert argv[argv.index("--profile") + 1] == "Captation-Audio"
    assert app_path_of(inst) == str(app)


def test_locator_version_fallback_and_explicit_paths(tmp_path: Path) -> None:
    app = make_obs_app(tmp_path / "Custom", version=None)
    calls: Calls = []
    run = runner_for(calls, {"--version": (0, "OBS Studio - 33.1.0 (mac)\n")})
    inst = MacObsLocator(str(app / "Contents/MacOS/OBS"), home=tmp_path, runner=run).find()
    assert inst is not None and inst.version == "33.1.0" and str(app) in inst.command
    assert calls == [[str(app / "Contents/MacOS/OBS"), "--version"]]
    by_bundle = MacObsLocator(str(app), home=tmp_path, runner=run).find()
    assert by_bundle is not None and by_bundle.command == inst.command
    bare = tmp_path / "bin" / "obs-dev"
    bare.parent.mkdir()
    bare.write_text("#!/bin/sh\n", encoding="utf-8")
    bare.chmod(0o755)
    dev = MacObsLocator(str(bare), home=tmp_path, runner=run).find()
    assert dev is not None and dev.command == (str(bare),) and app_path_of(dev) is None


def test_locator_not_found(tmp_path: Path) -> None:
    loc = MacObsLocator(home=tmp_path, app_dirs=("~/Applications",))
    assert loc.find() is None
    assert MacObsLocator(str(tmp_path / "missing.app"), home=tmp_path).find() is None


def test_obs_pids_cover_the_bundle_and_helpers() -> None:
    ps = (
        "    1 /sbin/launchd\n"
        " 4001 /Applications/OBS.app/Contents/MacOS/OBS\n"
        " 4002 /Applications/OBS.app/Contents/Frameworks/OBS Helper.app/Contents/MacOS/OBS Helper\n"
        " 4003 /Applications/OBS.app/Contents/MacOS/obs-ffmpeg-mux\n"
        " 5000 /Applications/OBS.app.backup/Contents/MacOS/OBS\n"
        " junk line\n"
    )
    assert parse_ps(ps)[1] == (4001, "/Applications/OBS.app/Contents/MacOS/OBS")
    calls: Calls = []
    run = runner_for(calls, {"-axo": (0, ps), "-x": (0, "4001\n")})
    inst = ObsInstallation(
        "native", ("/usr/bin/open", "-g", "-a", "/Applications/OBS.app", "--args"), "/c"
    )
    assert obs_host_pids(inst, run) == frozenset({4001, 4002, 4003})
    assert calls == [["/bin/ps", "-axo", "pid=,comm="], ["/usr/bin/pgrep", "-x", "OBS"]]


def test_crash_upload_settings_scan(tmp_path: Path) -> None:
    (tmp_path / "user.ini").write_text(
        "[General]\nFirstRun=false\nCrashReportUpload=true\n[Other]\nX=1\n", encoding="utf-8"
    )
    (tmp_path / "global.ini").write_text("[General]\nLastCrashLog=none\n", encoding="utf-8")
    assert crash_upload_settings(tmp_path) == {
        "user.ini:[General] CrashReportUpload": "true",
        "global.ini:[General] LastCrashLog": "none",
    }
    assert crash_upload_settings(tmp_path / "missing") == {}


# ---- FileVault -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "status"),
    [
        ("FileVault is On.\n", "encrypted"),
        ("FileVault is Off.\n", "unencrypted"),
        ("FileVault is On.\nEncryption in progress: Percent completed = 12.5\n", "unknown"),
        ("FileVault is Off.\nDecryption in progress: Percent completed = 80\n", "unknown"),
        ("", "unknown"),
    ],
)
def test_parse_fdesetup(text: str, status: str) -> None:
    assert parse_fdesetup(text) == status


def test_disk_encryption_boot_volume_and_other_volumes(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    calls: Calls = []
    run = runner_for(calls, {"status": (0, "FileVault is On.\n")})
    same = MacDiskEncryption(run, data_volume=tmp_path, device_of=lambda p: 1)
    assert same.status(data / "CaptationData" / "missing") == "encrypted"
    assert calls == [["/usr/bin/fdesetup", "status"]]

    def other(info: dict[str, object] | None, code: int = 0) -> str:
        out = plistlib.dumps(info).decode() if info is not None else "garbage"
        enc = MacDiskEncryption(
            runner_for([], {"info": (code, out)}),
            data_volume=tmp_path,
            device_of=lambda p: 2 if p in (data, Path("/Volumes/X")) else 1,
            mount_point_of=lambda p: Path("/Volumes/X"),
        )
        return enc.status(data)

    # diskutil gets the volume's mount point, never the folder itself (PF-121)
    calls = []
    MacDiskEncryption(
        runner_for(calls, {"info": (0, plistlib.dumps({"FileVault": True}).decode())}),
        data_volume=tmp_path,
        device_of=lambda p: 2 if p in (data, Path("/Volumes/X")) else 1,
        mount_point_of=lambda p: Path("/Volumes/X"),
    ).status(data)
    assert calls == [["/usr/sbin/diskutil", "info", "-plist", "/Volumes/X"]]

    assert other({"FileVault": True, "MountPoint": "/Volumes/X"}) == "encrypted"
    assert other({"FileVault": False}) == "unencrypted"
    assert other({"MountPoint": "/Volumes/X"}) == "unknown"
    assert other(None) == "unknown"
    assert other({"FileVault": True}, code=1) == "unknown"


# ---- LaunchAgent ---------------------------------------------------------------------------


def test_launch_agent_definition() -> None:
    d = launch_agent_plist(
        "/Applications/Établi Captation.app/Contents/MacOS/captation",
        Path("/Users/u/Library/Logs/captation"),
        "/usr/bin:/bin",
    )
    assert d["Label"] == "de.etabli.captation"
    assert d["ProgramArguments"] == [
        "/Applications/Établi Captation.app/Contents/MacOS/captation",
        "run",
    ]
    assert d["RunAtLoad"] is True and d["KeepAlive"] == {"SuccessfulExit": False}
    assert d["LimitLoadToSessionType"] == "Aqua" and d["ProcessType"] == "Interactive"
    # time to stop and finalise a recording on logout; no 10 s restart loop on a broken install
    assert d["ExitTimeOut"] == 60 and d["ThrottleInterval"] == 60
    assert d["StandardOutPath"].endswith("captation/launchd.out.log")
    assert d["StandardErrorPath"].endswith("captation/launchd.err.log")
    assert d["EnvironmentVariables"] == {"PATH": "/usr/bin:/bin"}
    assert "arm" not in " ".join(d["ProgramArguments"]).split()  # autostart never arms
    assert launch_path("/x/bin:/usr/bin") == (
        "/x/bin:/usr/bin:/opt/homebrew/bin:/usr/local/bin:/bin:/usr/sbin:/sbin"
    )
    assert launch_path(None).startswith("/opt/homebrew/bin:")


class FakeLaunchd:
    def __init__(self) -> None:
        self.calls: Calls = []
        self.loaded = False
        self.fail_bootstrap = 0

    def __call__(self, argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        verb = argv[1]
        code = 0
        if verb == "print":
            code = 0 if self.loaded else 113
        elif verb == "bootstrap":
            if self.fail_bootstrap:
                self.fail_bootstrap -= 1
                code = 5
            else:
                self.loaded = True
        elif verb == "bootout":
            self.loaded = False
        return subprocess.CompletedProcess(argv, code, "", "Bootstrap failed: 5" if code else "")


def make_autostart(tmp_path: Path, launchd: FakeLaunchd) -> MacAutostart:
    logs = tmp_path / "Library" / "Logs" / "captation"
    return MacAutostart(
        home=tmp_path,
        runner=launchd,
        uid=501,
        log_dir=lambda: logs,
        command=lambda: "/opt/captation/captation",
        environ_path="/usr/bin",
        sleep=lambda s: None,
    )


@pytest.mark.req("R-49")
def test_launch_agent_install_is_idempotent_and_uninstall_leaves_no_residue(
    tmp_path: Path,
) -> None:
    launchd = FakeLaunchd()
    auto = make_autostart(tmp_path, launchd)
    plist = tmp_path / "Library/LaunchAgents/de.etabli.captation.plist"
    assert auto.status().installed is False and auto.status().method == "none"
    assert auto.install() == [plist]
    service = "gui/501/de.etabli.captation"
    assert ["/bin/launchctl", "bootstrap", "gui/501", str(plist)] in launchd.calls
    data = plistlib.loads(plist.read_bytes())
    assert data["ProgramArguments"] == ["/opt/captation/captation", "run"]
    assert auto.status().installed and auto.status().method == "launchagent"

    launchd.calls.clear()
    assert auto.install() == [plist]  # unchanged and loaded: nothing to do
    assert launchd.calls == [["/bin/launchctl", "print", service]]

    logs = tmp_path / "Library/Logs/captation"
    logs.mkdir(parents=True)
    (logs / "launchd.out.log").write_text("x", encoding="utf-8")
    (logs / "launchd.err.log").write_text("x", encoding="utf-8")
    (logs / "captation.log").write_text("x", encoding="utf-8")  # the agent's own log stays
    launchd.calls.clear()
    removed = auto.uninstall()
    assert removed == [plist, logs / "launchd.out.log", logs / "launchd.err.log"]
    assert ["/bin/launchctl", "bootout", service] in launchd.calls and not launchd.loaded
    assert not plist.exists() and (logs / "captation.log").exists()
    assert auto.uninstall() == [] and auto.status().installed is False


def test_launch_agent_reload_on_change_and_bootstrap_retry(tmp_path: Path) -> None:
    launchd = FakeLaunchd()
    auto = make_autostart(tmp_path, launchd)
    auto.install()
    changed = MacAutostart(
        home=tmp_path,
        runner=launchd,
        uid=501,
        log_dir=lambda: tmp_path / "logs",
        command=lambda: "/new/captation",
        environ_path="/usr/bin",
        sleep=lambda s: None,
    )
    launchd.calls.clear()
    launchd.fail_bootstrap = 1  # launchd still tearing the old job down
    changed.install()
    verbs = [c[1] for c in launchd.calls]
    assert verbs.count("bootout") == 1 and verbs.count("bootstrap") == 2 and launchd.loaded

    launchd.loaded = False
    launchd.fail_bootstrap = 10
    with pytest.raises(RuntimeError, match="bootstrap failed"):
        make_autostart(tmp_path, launchd).install()


def test_platform_services() -> None:
    assert macos.SERVICES.name == "macos" and isinstance(macos.SERVICES, MacPlatform)
    probe = macos.SERVICES.probe(exclude_pids=frozenset({1}), store_titles=False)
    assert probe.mode == "watch"  # nothing is spawned before the first snapshot
    probe.close()
    assert isinstance(macos.SERVICES.autostart(), MacAutostart)
    assert isinstance(macos.SERVICES.disk_encryption(), MacDiskEncryption)
    assert isinstance(macos.SERVICES.obs_locator(None), MacObsLocator)


@pytest.mark.macos
def test_real_fdesetup_and_locator() -> None:
    """On the Mac: fdesetup answers without root (PF-120); OBS is found if installed."""
    assert parse_fdesetup(
        subprocess.run(
            ["/usr/bin/fdesetup", "status"], capture_output=True, text=True, timeout=10
        ).stdout
    ) in ("encrypted", "unencrypted")
    inst = MacObsLocator().find()
    if inst is None:
        pytest.skip("OBS is not installed in /Applications")
    assert inst.version is not None


def test_mount_point(tmp_path: Path) -> None:
    assert macos.mount_point(Path("/")) == Path("/")
    found = macos.mount_point(tmp_path / "missing" / "deeper")
    assert found == Path("/") or found.is_mount()


@pytest.mark.macos
def test_real_diskutil_on_mount_point() -> None:
    """On the Mac: diskutil answers for a mount point with a boolean FileVault key (PF-121)."""
    out = subprocess.run(
        ["/usr/sbin/diskutil", "info", "-plist", str(macos.mount_point(Path.home()))],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert out.returncode == 0
    assert isinstance(plistlib.loads(out.stdout.encode()).get("FileVault"), bool)


# ---- crash-report egress (PF-106) ------------------------------------------------------------


def test_crash_upload_check_from_platform_facts(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from captation.doctor import crash_upload_check

    clean = crash_upload_check(macos, tmp_path)
    assert clean.level == "ok"
    (tmp_path / "global.ini").write_text(
        "[CrashHandler]\nlast_crash_log_file=/x.ips\nlast_crash_log_url=https://example.invalid/c\n",
        encoding="utf-8",
    )
    uploaded = crash_upload_check(macos, tmp_path)
    assert uploaded.level == "warn" and "uploaded a crash report" in uploaded.detail
    assert macos.CRASH_UPLOAD_CONSENT_LABEL in uploaded.fix
    assert "example.invalid" not in uploaded.detail  # the report URL is not echoed
    # a platform without a verified upload key keeps listing every crash key
    pending = crash_upload_check(
        SimpleNamespace(crash_upload_settings=macos.crash_upload_settings), tmp_path
    )
    assert pending.level == "warn" and "pending verification" in pending.detail


def test_disk_encryption_resolves_symlinks_and_never_guesses(tmp_path: Path) -> None:
    """A data root behind a symlink is judged on its real volume (audit 2026-11-24)."""
    real = tmp_path / "external" / "CaptationData"
    real.mkdir(parents=True)
    link = tmp_path / "CaptationData"
    link.symlink_to(real)
    seen: list[Path] = []

    def mount_point_of(p: Path) -> Path:
        seen.append(p)
        return Path("/Volumes/Ext")

    info = plistlib.dumps({"FileVault": False}).decode()
    enc = MacDiskEncryption(
        runner_for([], {"info": (0, info)}),
        data_volume=tmp_path / "boot",
        device_of=lambda p: 7 if p in (real, Path("/Volumes/Ext")) else 1,
        mount_point_of=mount_point_of,
    )
    assert enc.status(link) == "unencrypted"
    assert seen == [real]
    # the walk ended on another device than the data: never guess "encrypted"
    lost = MacDiskEncryption(
        runner_for([], {"info": (0, plistlib.dumps({"FileVault": True}).decode())}),
        data_volume=tmp_path / "boot",
        device_of=lambda p: 7 if p == real else 1,
        mount_point_of=lambda p: Path("/"),
    )
    assert lost.status(link) == "unknown"


@pytest.mark.req("R-49")
def test_start_now_kickstarts_only_a_loaded_launch_agent(tmp_path: Path) -> None:
    launchd = FakeLaunchd()
    auto = make_autostart(tmp_path, launchd)
    assert auto.start_now() is False  # not installed: the opened app starts the agent itself
    assert not any(c[1] == "kickstart" for c in launchd.calls)
    auto.install()
    assert auto.start_now() is True
    assert launchd.calls[-1] == ["/bin/launchctl", "kickstart", "gui/501/de.etabli.captation"]


def test_start_now_only_for_this_build(tmp_path: Path) -> None:
    launchd = FakeLaunchd()
    auto = make_autostart(tmp_path, launchd)
    auto.install()
    other = MacAutostart(
        home=tmp_path,
        runner=launchd,
        uid=501,
        log_dir=lambda: tmp_path,
        command=lambda: "/Applications/Older.app/Contents/MacOS/captation",
        environ_path="/usr/bin",
        sleep=lambda s: None,
    )
    assert other.start_now() is False  # the LaunchAgent points to another build
    assert not any(c[1] == "kickstart" for c in launchd.calls)
