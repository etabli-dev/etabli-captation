"""Linux platform services: OBS locator (stage 03), PipeWire probe (stage 05),
XDG/systemd autostart and dm-crypt check (stage 11).

Every OS-specific string constant carries a ``# PF-xxx`` reference (tools/check_facts.py).
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from captation.models import (
    AppRef,
    AutostartStatus,
    EncryptionStatus,
    ObsInstallation,
    ProbeSnapshot,
)

log = logging.getLogger(__name__)

FLATPAK_APP_ID = "com.obsproject.Studio"  # PF-001
OBS_CONFIG_SUBDIR = "obs-studio"  # PF-002
FLATPAK_CONFIG_ROOT = ".var/app"  # PF-002
FLATPAK_CONFIG_LEAF = "config"  # PF-002


def _run(argv: list[str], timeout: float = 10) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)


class LinuxObsLocator:
    """Native ``obs`` (explicit path or PATH) or the Flatpak (preferred, ADR-006)."""

    def __init__(self, executable: str | None = None) -> None:
        self._executable = executable

    def _native(self, path: str) -> ObsInstallation:
        xdg = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")  # PF-002
        version = None
        out = _run([path, "--version"])  # PF-003
        m = re.search(r"(\d+\.\d+\.\d+)", out.stdout + out.stderr)  # PF-003
        if m:
            version = m.group(1)
        return ObsInstallation(
            kind="native",  # not-a-fact: internal enum value
            command=(path,),
            config_dir=str(Path(xdg) / OBS_CONFIG_SUBDIR),
            version=version,
        )

    def _flatpak(self) -> ObsInstallation | None:
        if not shutil.which("flatpak"):  # PF-001
            return None
        out = _run(["flatpak", "info", FLATPAK_APP_ID])  # PF-001
        if out.returncode != 0:
            return None
        m = re.search(r"Version:\s*(\S+)", out.stdout)  # PF-001
        config = Path.home() / FLATPAK_CONFIG_ROOT / FLATPAK_APP_ID / FLATPAK_CONFIG_LEAF
        return ObsInstallation(
            kind="flatpak",  # not-a-fact: internal enum value
            command=("flatpak", "run", FLATPAK_APP_ID),  # PF-001
            config_dir=str(config / OBS_CONFIG_SUBDIR),
            version=m.group(1) if m else None,
        )

    def find(self) -> ObsInstallation | None:
        if self._executable:
            return self._native(self._executable)
        flatpak = self._flatpak()
        if flatpak:
            return flatpak
        native = shutil.which("obs")  # PF-001
        return self._native(native) if native else None


def obs_host_pids(installation: ObsInstallation) -> frozenset[int]:
    """Host PIDs of running OBS processes (for self-exclusion)."""
    pids: set[int] = set()
    if installation.kind == "flatpak":  # not-a-fact: internal enum value
        out = _run(["flatpak", "ps", "--columns=application,pid,child-pid"])  # PF-001
        for line in out.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 3 and parts[0] == FLATPAK_APP_ID:
                pids.update(int(p) for p in parts[1:3] if p.isdigit())
    out = _run(["pgrep", "-x", "obs"])  # PF-001
    pids.update(int(p) for p in out.stdout.split() if p.isdigit())
    return frozenset(pids)


# ---- PipeWire probe (stage 05) -----------------------------------------------------------------

PW_DUMP = "pw-dump"  # PF-016
PW_TIMEOUT_S = 2.0
CAMERA_SCAN_S = 2.0
NEVER = float("-inf")  # not-a-fact: sentinel timestamp
NODE_TYPE = "PipeWire:Interface:Node"  # PF-016
CLIENT_TYPE = "PipeWire:Interface:Client"  # PF-016
MIC_CLASS = "Stream/Input/Audio"  # PF-016
CAMERA_CLASS = "Stream/Input/Video"  # PF-016
RUNNING = "running"  # PF-016
K_CLASS = "media.class"  # PF-016
K_CLIENT = "client.id"  # PF-016
K_BINARY = "application.process.binary"  # PF-016
K_PID = "application.process.id"  # PF-016
K_HOST_PID = "pipewire.sec.pid"  # PF-016
K_APP_NAME = "application.name"  # PF-016
K_PORTAL_APP = "pipewire.access.portal.app_id"  # PF-013
OBS_PROCESS = "obs"  # PF-013
VIDEO_DEVICE_PREFIX = "/dev/video"  # PF-017
# Defensive heuristic for browser helper processes; Chrome itself needs none (PF-018).
_HELPER_PATTERN = r"(?i)(helper|renderer|utility|zygote|webcontent)"  # not-a-fact: heuristic
HELPER_RE = re.compile(_HELPER_PATTERN)
SESSION_TYPE_VAR = "XDG_SESSION_TYPE"  # PF-019

Runner = Callable[[list[str], float], subprocess.CompletedProcess[str]]


def _default_runner(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    return _run(argv, timeout)


def _props(obj: dict[str, Any]) -> dict[str, Any]:
    info = obj.get("info") or {}  # PF-016
    props: dict[str, Any] = info.get("props") or {}  # PF-016
    return props


def _proc_exe(pid: int, proc: Path) -> str | None:
    try:
        return os.readlink(proc / str(pid) / "exe")  # PF-017
    except OSError:
        return None


def _proc_ppid_comm(pid: int, proc: Path) -> tuple[int, str] | None:
    try:
        stat = (proc / str(pid) / "stat").read_text(encoding="utf-8", errors="replace")  # PF-017
    except OSError:
        return None
    # pid (comm) state ppid ...: comm may contain spaces and parentheses
    comm = stat[stat.index("(") + 1 : stat.rindex(")")]  # PF-017
    fields = stat[stat.rindex(")") + 2 :].split()  # PF-017
    return (int(fields[1]), comm) if len(fields) > 1 else None


def _parent_app(pid: int, name: str, proc: Path) -> str | None:
    """Browser helpers: walk up to 3 parents to the first non-helper process."""
    if not HELPER_RE.search(name):
        return None
    current = pid
    for _ in range(3):
        info = _proc_ppid_comm(current, proc)
        if info is None or info[0] <= 1:
            return None
        current = info[0]
        exe = _proc_exe(current, proc)
        parent = os.path.basename(exe) if exe else (_proc_ppid_comm(current, proc) or (0, ""))[1]
        if parent and not HELPER_RE.search(parent):
            return parent
    return None


def _pid_matches(pid: int, binary: str, proc: Path) -> bool:
    exe = _proc_exe(pid, proc)
    if exe is not None:
        return os.path.basename(exe) == binary
    comm = (_proc_ppid_comm(pid, proc) or (0, ""))[1]
    return bool(binary) and comm == binary[:15]


def _host_pid_from_namespace(ns_pid: int, binary: str, proc: Path) -> int | None:
    """Flatpak apps report a sandbox PID: find the host PID via ``NSpid`` in /proc."""
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            status = (entry / "status").read_text(encoding="utf-8", errors="replace")  # PF-017
        except OSError:
            continue
        for line in status.splitlines():
            if line.startswith("NSpid:"):  # PF-017
                ids = line.split()[1:]
                if len(ids) > 1 and ids[-1] == str(ns_pid):
                    host = int(entry.name)
                    if _pid_matches(host, binary, proc):
                        return host
                break
    return None


def _int(value: Any) -> int | None:
    try:
        return int(str(value))
    except ValueError:
        return None


def app_ref(node: dict[str, Any], client: dict[str, Any] | None, proc: Path) -> AppRef | None:
    """Build an AppRef from a PipeWire stream node and its client.

    PID choice (PF-016): ``application.process.id`` is the app's own PID (sandbox PID for
    Flatpak apps); ``pipewire.sec.pid`` is the peer of the PipeWire socket, which is
    pipewire-pulse for PulseAudio-API clients. The candidate whose /proc exe matches the
    binary wins; a Flatpak PID is mapped to the host PID via NSpid.
    """
    props = _props(node)
    cprops = _props(client) if client else {}
    app_pid = _int(props.get(K_PID) or cprops.get(K_PID))
    sec_pid = _int(cprops.get(K_HOST_PID) or props.get(K_HOST_PID))
    portal = props.get(K_PORTAL_APP) or cprops.get(K_PORTAL_APP)
    binary = os.path.basename(str(props.get(K_BINARY) or cprops.get(K_BINARY) or ""))
    pid: int | None = None
    for candidate in (app_pid, sec_pid):
        if candidate is not None and binary and _pid_matches(candidate, binary, proc):
            pid = candidate
            break
    if pid is None and app_pid is not None and portal:
        pid = _host_pid_from_namespace(app_pid, binary, proc)
    if pid is None:
        pid = app_pid if app_pid is not None else sec_pid
    if pid is None:
        return None
    exe = _proc_exe(pid, proc)
    if not binary and exe:
        binary = os.path.basename(exe)
    if portal == FLATPAK_APP_ID:
        binary = OBS_PROCESS
    app_name = props.get(K_APP_NAME) or cprops.get(K_APP_NAME)
    parent = _parent_app(pid, binary, proc)
    if parent is None and app_name and str(app_name) != binary:
        parent = str(app_name)
    return AppRef(pid=pid, process_name=binary, exe_path=exe, parent_app=parent)


def parse_pw_dump(
    objects: Iterable[dict[str, Any]],
    proc: Path = Path("/proc"),  # PF-017
) -> tuple[list[AppRef], list[AppRef]]:
    """(mic users, camera users) from a pw-dump document. Only running stream nodes count."""
    objs = list(objects)
    clients = {o.get("id"): o for o in objs if o.get("type") == CLIENT_TYPE}  # PF-016
    mic: list[AppRef] = []
    cam: list[AppRef] = []
    for obj in objs:
        if obj.get("type") != NODE_TYPE:  # PF-016
            continue
        info = obj.get("info") or {}  # PF-016
        props = info.get("props") or {}  # PF-016
        cls = props.get(K_CLASS)
        if cls not in (MIC_CLASS, CAMERA_CLASS) or info.get("state") != RUNNING:  # PF-016
            continue
        ref = app_ref(obj, clients.get(props.get(K_CLIENT)), proc)
        if ref is not None:
            (mic if cls == MIC_CLASS else cam).append(ref)
    return _unique(mic), _unique(cam)


def _unique(refs: list[AppRef]) -> list[AppRef]:
    seen: dict[tuple[int, str], AppRef] = {}
    for ref in refs:
        seen.setdefault((ref.pid, ref.process_name), ref)
    return sorted(seen.values(), key=lambda r: (r.pid, r.process_name))


def video_device_users(
    proc: Path = Path("/proc"),  # PF-017
    uid: int | None = None,
) -> list[AppRef]:
    """Processes of this user with an open /dev/video* (V4L2 cameras)."""
    if uid is None:
        if sys.platform == "win32":  # not-a-fact: interpreter platform name (mypy narrowing)
            return []
        uid = os.getuid()
    users: list[AppRef] = []
    with os.scandir(proc) as entries:
        for entry in entries:
            if not entry.name.isdigit():
                continue
            try:
                if entry.stat(follow_symlinks=False).st_uid != uid:
                    continue
                fd_dir = os.path.join(entry.path, "fd")  # PF-017
                fds = os.listdir(fd_dir)
            except OSError:
                continue
            for fd in fds:
                try:
                    target = os.readlink(os.path.join(fd_dir, fd))
                except OSError:
                    continue
                if target.startswith(VIDEO_DEVICE_PREFIX):
                    pid = int(entry.name)
                    exe = _proc_exe(pid, proc)
                    comm = (_proc_ppid_comm(pid, proc) or (0, ""))[1]
                    name = os.path.basename(exe) if exe else comm
                    users.append(AppRef(pid=pid, process_name=name, exe_path=exe))
                    break
    return users


class LinuxProbe:
    """CallProbe on PipeWire (``pw-dump``) plus a /proc scan for V4L2 cameras."""

    def __init__(
        self,
        *,
        exclude_pids: frozenset[int] = frozenset(),
        runner: Runner = _default_runner,
        proc: Path = Path("/proc"),  # PF-017
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._exclude = exclude_pids | {os.getpid()}
        self._runner = runner
        self._proc = proc
        self._clock = clock
        self._wayland_logged = False
        self._cam_cache: list[AppRef] = []
        self._cam_at = NEVER

    def set_exclude_pids(self, pids: frozenset[int]) -> None:
        self._exclude = pids | {os.getpid()}

    def _titles(self) -> dict[int, tuple[str, ...]] | None:
        """Window titles: unavailable on Wayland (PF-019). An X11 path (wmctrl) is not
        implemented until it can be verified on an X11 session (docs/known-limitations.md)."""
        if not self._wayland_logged:
            session = os.environ.get(SESSION_TYPE_VAR, "unknown")  # not-a-fact: log label
            log.info("probe: window titles unavailable (session type %s)", session)
            self._wayland_logged = True
        return None

    def snapshot(self) -> ProbeSnapshot:
        ts = self._clock()
        confidence: Any = "high"  # not-a-fact: internal status value
        mic: list[AppRef] = []
        cam: list[AppRef] = []
        try:
            out = self._runner([PW_DUMP], PW_TIMEOUT_S)
            if out.returncode != 0:
                raise ValueError("pw-dump failed")
            mic, cam = parse_pw_dump(json.loads(out.stdout or "[]"), self._proc)  # PF-020
        except (ValueError, OSError, subprocess.TimeoutExpired):
            log.warning("probe: pw-dump unavailable, microphone detection is degraded")
            confidence = "low"  # not-a-fact: internal status value
        mic = [r for r in mic if r.pid not in self._exclude]
        if mic:
            # The /proc scan costs ~35 ms on a busy desktop; the camera only matters during
            # a call (mode selection and upgrade), so it runs only while someone holds the mic,
            # and at most every CAMERA_SCAN_S seconds (PF-017).
            if self._clock() - self._cam_at >= CAMERA_SCAN_S:
                self._cam_cache = video_device_users(self._proc)
                self._cam_at = self._clock()
            cam_pids = {r.pid for r in cam}
            cam += [r for r in self._cam_cache if r.pid not in cam_pids]
        else:
            self._cam_at = NEVER
        cam = [r for r in cam if r.pid not in self._exclude and r.process_name != OBS_PROCESS]
        return ProbeSnapshot(
            ts_monotonic=ts,
            mic_users=tuple(mic),
            camera_active=bool(cam),
            camera_users=tuple(cam),
            window_titles=self._titles(),
            confidence=confidence,
        )

    def close(self) -> None:
        return None


# ---- disk encryption (stage 10/11) ---------------------------------------------------------------

LUKS_TYPE = "crypt"  # PF-020
PLAIN_FS = frozenset({"ext4", "ext3", "xfs", "btrfs", "vfat", "exfat", "f2fs"})  # PF-020


class LinuxDiskEncryption:
    """dm-crypt below the data root (``findmnt`` + ``lsblk -s``); other layers → unknown."""

    def __init__(self, runner: Runner = _default_runner) -> None:
        self._runner = runner

    def status(self, path: Path) -> EncryptionStatus:
        target = path
        while not target.exists() and target != target.parent:
            target = target.parent
        out = self._runner(
            ["findmnt", "-no", "SOURCE,FSTYPE", "--target", str(target)],  # PF-020
            5,  # PF-020
        )  # PF-020
        parts = out.stdout.split()
        if out.returncode != 0 or len(parts) < 2:
            return "unknown"  # not-a-fact: internal status value
        source, fstype = parts[0], parts[1]
        if not source.startswith("/dev/"):  # PF-020
            return "unknown"  # not-a-fact: internal status value
        tree = self._runner(["lsblk", "-J", "-s", "-o", "NAME,TYPE", source], 5)  # PF-020
        try:
            devices = json.loads(tree.stdout or "{}").get("blockdevices", [])  # PF-020
        except ValueError:
            return "unknown"  # not-a-fact: internal status value

        def types(nodes: list[dict[str, Any]]) -> Iterable[str]:
            for node in nodes:
                yield str(node.get("type"))  # PF-020
                yield from types(node.get("children", []))  # PF-020

        if LUKS_TYPE in set(types(devices)):
            return "encrypted"  # not-a-fact: internal status value
        return (
            "unencrypted" if fstype in PLAIN_FS else "unknown"  # not-a-fact: internal status value
        )  # not-a-fact: internal status value


# ---- autostart (stage 11) ------------------------------------------------------------------

ENCODING = "utf-8"  # not-a-fact: text encoding
AUTOSTART_DIR = ".config/autostart"  # PF-021
DESKTOP_FILE = "de.etabli.captation.desktop"  # PF-021
SYSTEMD_USER_DIR = ".config/systemd/user"  # PF-021
UNIT_FILE = "etabli-captation.service"  # PF-021


def agent_command() -> str:
    """The command that starts the agent: the frozen executable or the venv script."""
    import sys

    if getattr(sys, "frozen", False):  # not-a-fact: PyInstaller attribute
        return sys.executable
    name = "captation"  # not-a-fact: entry-point name
    script = Path(sys.executable).parent / name
    return str(script if script.exists() else shutil.which(name) or script)


def desktop_entry(command: str) -> str:
    return (
        "[Desktop Entry]\n"  # PF-021
        "Type=Application\n"  # PF-021
        "Name=Établi Captation\n"  # not-a-fact: display name
        "Comment=Records calls only while study mode is armed\n"  # not-a-fact: display text
        f"Exec={command} run\n"  # PF-021
        "Terminal=false\n"  # PF-021
        "X-GNOME-Autostart-enabled=true\n"  # PF-021
    )


def systemd_unit(command: str) -> str:
    return (
        "[Unit]\n"  # PF-021
        "Description=Etabli Captation agent (starts disarmed)\n"  # not-a-fact: description
        "After=graphical-session.target\n"  # PF-021
        "PartOf=graphical-session.target\n\n"  # PF-021
        "[Service]\n"  # PF-021
        f"ExecStart={command} run\n"  # PF-021
        "Restart=on-failure\n\n"  # PF-021
        "[Install]\n"  # PF-021
        "WantedBy=graphical-session.target\n"  # PF-021
    )


class LinuxAutostart:
    """XDG autostart entry (default) or a systemd user unit. Autostart never arms."""

    def __init__(self, home: Path | None = None, runner: Runner = _default_runner) -> None:
        self._home = home or Path.home()
        self._runner = runner

    @property
    def desktop_path(self) -> Path:
        return self._home / AUTOSTART_DIR / DESKTOP_FILE

    @property
    def unit_path(self) -> Path:
        return self._home / SYSTEMD_USER_DIR / UNIT_FILE

    def install(self, *, systemd: bool = False) -> list[Path]:
        """Install one autostart method; the other one is removed (never two agents)."""
        command = agent_command()
        if systemd:
            self.desktop_path.unlink(missing_ok=True)
            self.unit_path.parent.mkdir(parents=True, exist_ok=True)
            text = systemd_unit(command)
            self.unit_path.write_text(text, encoding=ENCODING)
            self._runner(["systemctl", "--user", "daemon-reload"], 30)  # PF-021
            self._runner(["systemctl", "--user", "enable", "--now", UNIT_FILE], 30)  # PF-021
            return [self.unit_path]
        if self.unit_path.exists():
            self._runner(["systemctl", "--user", "disable", "--now", UNIT_FILE], 30)  # PF-021
            self.unit_path.unlink()
            self._runner(["systemctl", "--user", "daemon-reload"], 30)  # PF-021
        self.desktop_path.parent.mkdir(parents=True, exist_ok=True)
        self.desktop_path.write_text(desktop_entry(command), encoding=ENCODING)
        return [self.desktop_path]

    def uninstall(self) -> list[Path]:
        removed: list[Path] = []
        if self.unit_path.exists():
            self._runner(["systemctl", "--user", "disable", "--now", UNIT_FILE], 30)  # PF-021
            self.unit_path.unlink()
            self._runner(["systemctl", "--user", "daemon-reload"], 30)  # PF-021
            removed.append(self.unit_path)
        if self.desktop_path.exists():
            self.desktop_path.unlink()
            removed.append(self.desktop_path)
        return removed

    def status(self) -> AutostartStatus:
        if self.unit_path.exists():
            unit = (str(self.unit_path),)
            return AutostartStatus(True, "systemd", unit)  # not-a-fact: internal enum value
        if self.desktop_path.exists():
            entry = (str(self.desktop_path),)
            return AutostartStatus(True, "xdg", entry)  # not-a-fact: internal enum value
        return AutostartStatus(False, "none")  # not-a-fact: internal enum value


class LinuxPlatform:
    name = "linux"  # not-a-fact: internal OS name

    def obs_locator(self, executable: str | None) -> LinuxObsLocator:
        return LinuxObsLocator(executable)

    def probe(self, *, exclude_pids: frozenset[int], store_titles: bool) -> LinuxProbe:
        # Titles are read in memory on X11 regardless; store_titles only affects storage.
        return LinuxProbe(exclude_pids=exclude_pids)

    def disk_encryption(self) -> LinuxDiskEncryption:
        return LinuxDiskEncryption()

    def autostart(self) -> LinuxAutostart:
        return LinuxAutostart()


SERVICES = LinuxPlatform()
