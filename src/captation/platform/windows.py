"""Windows platform services: OBS locator (stage 03), ConsentStore probe (stage 07),
Task Scheduler autostart with the AppUserModelID and the BitLocker check (stage 11).

Prepared on Linux (ADR-015). Every Windows identifier carries a ``# PF-2xx`` reference to a
``pending`` entry in ``docs/platform-facts.md``; the Windows PC verifies them
(``docs/runbook-windows.md``). Windows-only APIs (``winreg``, ``ctypes.windll``, ``psutil``)
sit behind ``sys.platform == "win32"`` guards and small injectable interfaces (registry access,
process lister, command runner), so the logic is unit-tested on Linux with fakes.

Diagnostics for the hive check (hive labels against Settings → Privacy → Microphone):
``python -m captation.platform.windows [--watch SECONDS]``.
"""

from __future__ import annotations

import base64
import getpass
import json
import logging
import ntpath
import os
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol
from xml.sax.saxutils import escape

from captation.models import (
    AppRef,
    AutostartStatus,
    EncryptionStatus,
    ObsInstallation,
    ProbeSnapshot,
)

log = logging.getLogger(__name__)

Hive = Literal["HKCU", "HKLM"]  # PF-210
Capability = Literal["microphone", "webcam"]  # PF-210
Runner = Callable[[list[str], float], subprocess.CompletedProcess[str]]

HKCU: Hive = "HKCU"  # PF-210
HKLM: Hive = "HKLM"  # PF-210
HIVES: tuple[Hive, ...] = (HKCU, HKLM)  # PF-212: both hives are read and labelled
MICROPHONE: Capability = "microphone"  # PF-210
WEBCAM: Capability = "webcam"  # PF-210
ENCODING = "utf-8"  # not-a-fact: text encoding of PowerShell output (forced by PS_PREAMBLE)


# ---- subprocess and PowerShell helpers ------------------------------------------------------


def _system_root() -> str:
    return os.environ.get("SYSTEMROOT") or "C:\\Windows"  # PF-209


def powershell_exe() -> str:
    """Windows PowerShell 5.1 by absolute path (no PATH lookup)."""
    return ntpath.join(
        _system_root(),
        "System32",  # PF-209
        "WindowsPowerShell",  # PF-209
        "v1.0",  # PF-209
        "powershell.exe",  # PF-209
    )


def schtasks_exe() -> str:
    return ntpath.join(_system_root(), "System32", "schtasks.exe")  # PF-220


PS_PREAMBLE = (
    "$ErrorActionPreference = 'Stop'\n"  # PF-209
    "$ProgressPreference = 'SilentlyContinue'\n"  # PF-209
    "[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false\n"  # PF-209
)


def encode_command(script: str) -> str:
    """``-EncodedCommand`` payload: base64 of the UTF-16LE script (no quoting problems)."""
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")  # PF-209


def powershell_argv(script: str) -> list[str]:
    """Run a script in a non-interactive Windows PowerShell without the user profile."""
    return [
        powershell_exe(),
        "-NoProfile",  # PF-209
        "-NonInteractive",  # PF-209
        "-EncodedCommand",  # PF-209
        encode_command(PS_PREAMBLE + script),
    ]


def ps_quote(text: str) -> str:
    """A PowerShell single-quoted string literal."""
    return "'" + text.replace("'", "''") + "'"  # PF-209


def _run(argv: list[str], timeout: float = 20) -> subprocess.CompletedProcess[str]:
    kwargs: dict[str, Any] = {}
    if sys.platform == "win32":  # not-a-fact: sys.platform value
        # The windowed agent must not flash console windows for its helper commands.
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW  # not-a-fact: Popen keyword
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        encoding=ENCODING,
        errors="replace",  # not-a-fact: codec error handler
        timeout=timeout,
        check=False,
        **kwargs,
    )


def default_runner(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    return _run(argv, timeout)


def parse_json_output(text: str) -> Any:
    """JSON from PowerShell stdout (tolerates a UTF-8 BOM and surrounding blank lines)."""
    return json.loads(text.lstrip("\ufeff").strip() or "null")  # not-a-fact: BOM character


def _win_basename(path: str) -> str:
    return path.replace("/", "\\").rsplit("\\", 1)[-1]  # PF-215


def _norm(path: str) -> str:
    """Windows paths compare case-insensitively and accept both separators (PF-215)."""
    return path.replace("/", "\\").lower()  # PF-215


# ---- registry access --------------------------------------------------------------------------


class RegistryAccess(Protocol):
    """The registry operations this module needs; tests inject a dict-backed fake."""

    def subkeys(self, hive: Hive, path: str) -> list[str]: ...

    def values(self, hive: Hive, path: str) -> dict[str, object]: ...

    def set_string(self, hive: Hive, path: str, name: str, value: str) -> None: ...

    def delete_key(self, hive: Hive, path: str) -> bool: ...


class WinRegistry:
    """``winreg`` implementation. HKLM is read in the 64-bit view (PF-210)."""

    def subkeys(self, hive: Hive, path: str) -> list[str]:
        if sys.platform != "win32":  # not-a-fact: sys.platform value
            return []
        import winreg

        try:
            with winreg.OpenKey(self._root(hive), path, 0, self._access(hive)) as key:
                count = winreg.QueryInfoKey(key)[0]
                return [winreg.EnumKey(key, i) for i in range(count)]
        except OSError:
            return []

    def values(self, hive: Hive, path: str) -> dict[str, object]:
        if sys.platform != "win32":  # not-a-fact: sys.platform value
            return {}
        import winreg

        out: dict[str, object] = {}
        try:
            with winreg.OpenKey(self._root(hive), path, 0, self._access(hive)) as key:
                count = winreg.QueryInfoKey(key)[1]
                for i in range(count):
                    name, data, _kind = winreg.EnumValue(key, i)
                    out[name] = data
        except OSError:
            return {}
        return out

    def set_string(self, hive: Hive, path: str, name: str, value: str) -> None:
        if sys.platform != "win32":  # not-a-fact: sys.platform value
            raise OSError("the Windows registry is not available on this OS")
        import winreg

        with winreg.CreateKeyEx(self._root(hive), path, 0, winreg.KEY_WRITE) as key:
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)

    def delete_key(self, hive: Hive, path: str) -> bool:
        if sys.platform != "win32":  # not-a-fact: sys.platform value
            return False
        import winreg

        try:
            winreg.DeleteKey(self._root(hive), path)
        except FileNotFoundError:
            return False
        return True

    @staticmethod
    def _root(hive: Hive) -> int:
        if sys.platform != "win32":  # not-a-fact: sys.platform value
            raise OSError("the Windows registry is not available on this OS")
        import winreg

        return winreg.HKEY_CURRENT_USER if hive == HKCU else winreg.HKEY_LOCAL_MACHINE

    @staticmethod
    def _access(hive: Hive) -> int:
        if sys.platform != "win32":  # not-a-fact: sys.platform value
            return 0
        import winreg

        return winreg.KEY_READ | (winreg.KEY_WOW64_64KEY if hive == HKLM else 0)


# ---- processes ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class ProcInfo:
    """One running process as psutil reports it (``exe`` is None when access is denied)."""

    pid: int
    ppid: int
    name: str
    exe: str | None


ProcessLister = Callable[[], list[ProcInfo]]

PSUTIL_ATTRS = ["pid", "ppid", "name", "exe"]  # PF-215


def psutil_processes() -> list[ProcInfo]:
    """All processes via psutil. Other users' processes come back with ``exe=None`` (PF-215)."""
    import psutil  # type: ignore[import-untyped]

    out: list[ProcInfo] = []
    for proc in psutil.process_iter(PSUTIL_ATTRS):
        info: dict[str, Any] = proc.info
        out.append(
            ProcInfo(
                pid=int(info.get("pid") or 0),  # PF-215
                ppid=int(info.get("ppid") or 0),  # PF-215
                name=str(info.get("name") or ""),  # PF-215
                exe=str(info["exe"]) if info.get("exe") else None,  # PF-215
            )
        )
    return out


# ---- OBS locator (stage 03) ----------------------------------------------------------------------

PROGRAM_FILES_VAR = "ProgramFiles"  # PF-201
APPDATA_VAR = "APPDATA"  # PF-204
OBS_DIR = "obs-studio"  # PF-201
OBS_BIN_PARTS = ("bin", "64bit")  # PF-201
OBS_EXE = "obs64.exe"  # PF-201
OBS_CONFIG_SUBDIR = "obs-studio"  # PF-204
OBS_REG_KEY = "SOFTWARE\\OBS Studio"  # PF-202
OBS_UNINSTALL_KEY = "SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\OBS Studio"  # PF-202
REG_DEFAULT_VALUE = ""  # PF-202
REG_INSTALL_LOCATION = "InstallLocation"  # PF-202
REG_DISPLAY_VERSION = "DisplayVersion"  # PF-202
VERSION_RE = re.compile(r"(\d+\.\d+\.\d+)")  # PF-203
FILE_VERSION_SCRIPT = "(Get-Item -LiteralPath $exePath).VersionInfo.ProductVersion\n"  # PF-203
OBS_VERSION_FLAG = "--version"  # PF-203


def obs_exe_in(install_dir: str) -> str:
    return ntpath.join(install_dir, *OBS_BIN_PARTS, OBS_EXE)


class WindowsObsLocator:
    """``%ProgramFiles%\\obs-studio\\bin\\64bit\\obs64.exe``, else the OBS registry keys.

    The version comes from the exe's file version, else the uninstall key, else ``--version``.
    """

    def __init__(
        self,
        executable: str | None = None,
        *,
        registry: RegistryAccess | None = None,
        env: Mapping[str, str] | None = None,
        exists: Callable[[str], bool] = os.path.isfile,
        runner: Runner = default_runner,
    ) -> None:
        self._executable = executable
        self._reg: RegistryAccess = registry or WinRegistry()
        self._env = os.environ if env is None else env
        self._exists = exists
        self._runner = runner

    def candidates(self) -> list[str]:
        found: list[str] = []
        program_files = self._env.get(PROGRAM_FILES_VAR)
        if program_files:
            found.append(obs_exe_in(ntpath.join(program_files, OBS_DIR)))
        install = self._reg.values(HKLM, OBS_REG_KEY).get(REG_DEFAULT_VALUE)
        if isinstance(install, str) and install:
            found.append(obs_exe_in(install))
        location = self._reg.values(HKLM, OBS_UNINSTALL_KEY).get(REG_INSTALL_LOCATION)
        if isinstance(location, str) and location:
            found.append(obs_exe_in(location.strip('"')))  # PF-202
        unique: list[str] = []
        for path in found:
            if _norm(path) not in {_norm(p) for p in unique}:
                unique.append(path)
        return unique

    def _version(self, exe: str) -> str | None:
        script = f"$exePath = {ps_quote(exe)}\n" + FILE_VERSION_SCRIPT
        try:
            out = self._runner(powershell_argv(script), 20)
            m = VERSION_RE.search(out.stdout or "")
            if out.returncode == 0 and m:
                return m.group(1)
        except (OSError, subprocess.TimeoutExpired):
            log.debug("obs locator: file version not readable")
        shown = self._reg.values(HKLM, OBS_UNINSTALL_KEY).get(REG_DISPLAY_VERSION)
        m = VERSION_RE.search(shown) if isinstance(shown, str) else None
        if m:
            return m.group(1)
        try:
            out = self._runner([exe, OBS_VERSION_FLAG], 20)
        except (OSError, subprocess.TimeoutExpired):
            return None
        m = VERSION_RE.search((out.stdout or "") + (out.stderr or ""))
        return m.group(1) if m else None

    def find(self) -> ObsInstallation | None:
        exe = self._executable or next((p for p in self.candidates() if self._exists(p)), None)
        if exe is None:
            return None
        appdata = self._env.get(APPDATA_VAR) or ntpath.join(
            str(Path.home()),
            "AppData",  # PF-204
            "Roaming",  # PF-204
        )
        return ObsInstallation(
            kind="native",  # not-a-fact: internal enum value
            command=(exe,),
            config_dir=ntpath.join(appdata, OBS_CONFIG_SUBDIR),
            version=self._version(exe),
        )


def obs_install_root(exe: str) -> str | None:
    """``<root>`` for ``<root>\\bin\\64bit\\obs64.exe``; None for any other layout."""
    parts = exe.replace("/", "\\").split("\\")  # PF-215
    tail = [p.lower() for p in parts[-3:]]
    if len(parts) < 4 or tail != [*OBS_BIN_PARTS, OBS_EXE]:
        return None
    return "\\".join(parts[:-3])  # PF-205, PF-215


def obs_host_pids(
    installation: ObsInstallation, processes: ProcessLister = psutil_processes
) -> frozenset[int]:
    """PIDs of OBS and its helpers (``obs-ffmpeg-mux.exe``, ``obs-browser-page.exe``, …):
    ``obs64.exe`` by name, plus every process whose exe lies below the OBS install root
    (PF-205), so a browser source that opens the mic never counts as a call app."""
    root = obs_install_root(installation.command[0]) if installation.command else None
    prefix = _norm(root) + "\\" if root else None  # PF-215
    pids: set[int] = set()
    for proc in processes():
        below_root = bool(prefix and proc.exe and _norm(proc.exe).startswith(prefix))
        if proc.name.lower() == OBS_EXE or below_root:
            pids.add(proc.pid)
    return frozenset(pids)


# ---- ConsentStore probe (stage 07) ---------------------------------------------------------------

CONSENT_STORE = (
    "Software\\Microsoft\\Windows\\CurrentVersion\\CapabilityAccessManager\\ConsentStore"  # PF-210
)
NONPACKAGED = "NonPackaged"  # PF-210
PATH_SEPARATOR_IN_KEY = "#"  # PF-210
LAST_START = "LastUsedTimeStart"  # PF-211
LAST_STOP = "LastUsedTimeStop"  # PF-211
WINDOWS_APPS = "windowsapps"  # PF-213
# Helper processes that act for a host app; attributed to the first non-helper ancestor.
HELPER_EXES = frozenset({"msedgewebview2.exe"})  # PF-214
MAX_PARENT_HOPS = 5
FILETIME_EPOCH_OFFSET_S = 11_644_473_600  # PF-211


@dataclass(frozen=True)
class ConsentEntry:
    """One ConsentStore app entry, labelled with its hive (hives are never merged)."""

    hive: Hive
    capability: Capability
    packaged: bool
    name: str  # package family name, or the decoded exe path of a desktop app
    last_start: int
    last_stop: int

    @property
    def in_use(self) -> bool:
        """In use ⇔ ``LastUsedTimeStop == 0`` and ``LastUsedTimeStart != 0`` (PF-211)."""
        return self.last_start != 0 and self.last_stop == 0

    @property
    def app(self) -> str:
        """Package family name, or the exe basename (no user path: logs and reports)."""
        return self.name if self.packaged else _win_basename(self.name)

    @property
    def label(self) -> str:
        return f"{self.hive}:{self.capability}:{self.app}"


def decode_nonpackaged(key: str) -> str:
    """``C:#Program Files#Zoom#bin#Zoom.exe`` → ``C:\\Program Files\\Zoom\\bin\\Zoom.exe``."""
    return key.replace(PATH_SEPARATOR_IN_KEY, "\\")  # PF-210


def _qword(value: object) -> int:
    if isinstance(value, int):
        return value
    try:
        return int(str(value))
    except ValueError:
        return 0


def read_consent_store(
    reg: RegistryAccess,
    hives: Iterable[Hive] = HIVES,
    capabilities: Iterable[Capability] = (MICROPHONE, WEBCAM),
) -> list[ConsentEntry]:
    """Every app entry of both hives and capabilities, each labelled with its hive."""
    entries: list[ConsentEntry] = []
    for hive in hives:
        for cap in capabilities:
            base = f"{CONSENT_STORE}\\{cap}"
            for sub in reg.subkeys(hive, base):
                if sub == NONPACKAGED:
                    root = f"{base}\\{NONPACKAGED}"
                    for exe_key in reg.subkeys(hive, root):
                        vals = reg.values(hive, f"{root}\\{exe_key}")
                        entries.append(
                            ConsentEntry(
                                hive,
                                cap,
                                False,
                                decode_nonpackaged(exe_key),
                                _qword(vals.get(LAST_START, 0)),
                                _qword(vals.get(LAST_STOP, 0)),
                            )
                        )
                    continue
                vals = reg.values(hive, f"{base}\\{sub}")
                entries.append(
                    ConsentEntry(
                        hive,
                        cap,
                        True,
                        sub,
                        _qword(vals.get(LAST_START, 0)),
                        _qword(vals.get(LAST_STOP, 0)),
                    )
                )
    return entries


def package_dir_matches(family: str, exe: str) -> bool:
    """True if ``exe`` runs from the install folder of package family ``family``.

    Family name ``<Name>_<PublisherId>``; install folder
    ``…\\WindowsApps\\<Name>_<Version>_<Arch>_<ResourceId>_<PublisherId>\\…`` (PF-213).
    """
    name, sep, publisher = family.rpartition("_")  # PF-213
    if not sep or not name or not publisher:
        return False
    parts = _norm(exe).split("\\")  # PF-215
    if WINDOWS_APPS not in parts:
        return False
    i = parts.index(WINDOWS_APPS)
    if i + 1 >= len(parts) - 1:
        return False
    folder = parts[i + 1]
    return folder.startswith(f"{name.lower()}_") and folder.endswith(f"_{publisher.lower()}")


def _roots(matched: list[ProcInfo]) -> list[ProcInfo]:
    """The processes of a group whose parent is not in the group: Chrome/Edge/Firefox child
    processes (same exe) collapse to the browser's main process (R-07, PF-214)."""
    pids = {p.pid for p in matched}
    return [p for p in matched if p.ppid not in pids]


def _host_of_helper(proc: ProcInfo, by_pid: Mapping[int, ProcInfo]) -> ProcInfo:
    """``msedgewebview2.exe`` → the first ancestor that is not a WebView2 helper (PF-214)."""
    current = proc
    for _ in range(MAX_PARENT_HOPS):
        if _win_basename(current.exe or current.name).lower() not in HELPER_EXES:
            return current
        parent = by_pid.get(current.ppid)
        if parent is None or parent.pid == current.pid:
            return current
        current = parent
    return current


def resolve_entry(
    entry: ConsentEntry, procs: list[ProcInfo], by_pid: Mapping[int, ProcInfo]
) -> list[AppRef]:
    """Running processes for one ConsentStore entry (exe path or package family)."""
    if entry.packaged:
        matched = [p for p in procs if p.exe and package_dir_matches(entry.name, p.exe)]
    else:
        target = _norm(entry.name)
        matched = [p for p in procs if p.exe and _norm(p.exe) == target]
    refs: list[AppRef] = []
    for root in _roots(matched):
        host = _host_of_helper(root, by_pid)
        refs.append(
            AppRef(
                pid=host.pid,
                process_name=host.name or _win_basename(host.exe or ""),
                exe_path=host.exe,
                package_id=entry.name if entry.packaged else None,
            )
        )
    return refs


def _unique(refs: Iterable[AppRef]) -> list[AppRef]:
    seen: dict[tuple[int, str], AppRef] = {}
    for ref in refs:
        seen.setdefault((ref.pid, ref.process_name.lower()), ref)
    return sorted(seen.values(), key=lambda r: (r.pid, r.process_name))


TitleReader = Callable[[set[int]], dict[int, tuple[str, ...]]]


def enum_window_titles(pids: set[int]) -> dict[int, tuple[str, ...]]:
    """Titles of visible top-level windows of ``pids`` (in memory only, docs/principles.md §3.1)."""
    if sys.platform != "win32":  # not-a-fact: sys.platform value
        return {}
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)  # PF-216
    enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [enum_proc, wintypes.LPARAM]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    found: dict[int, list[str]] = {}

    def callback(hwnd: int | None, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):  # PF-216
            return True
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))  # PF-216
        if owner.value in pids:
            length = user32.GetWindowTextLengthW(hwnd)  # PF-216
            if length > 0:
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)  # PF-216
                if buf.value:
                    found.setdefault(owner.value, []).append(buf.value)
        return True

    user32.EnumWindows(enum_proc(callback), 0)  # PF-216
    return {pid: tuple(titles) for pid, titles in found.items()}


class WindowsProbe:
    """CallProbe on the Capability Access Manager ConsentStore (HKCU and HKLM, labelled).

    An entry is in use while ``LastUsedTimeStop == 0`` (PF-211). Desktop apps map to running
    PIDs by exe path, packaged apps by their install folder (PF-213). An in-use entry without
    a running process is stale (e.g. after a crash) and ignored. OBS (``obs64.exe``), the
    excluded PIDs and the agent itself never count.
    """

    def __init__(
        self,
        *,
        exclude_pids: frozenset[int] = frozenset(),
        registry: RegistryAccess | None = None,
        processes: ProcessLister = psutil_processes,
        titles: TitleReader | None = enum_window_titles,
        hives: tuple[Hive, ...] = HIVES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._exclude = exclude_pids | {os.getpid()}
        self._reg: RegistryAccess = registry or WinRegistry()
        self._processes = processes
        self._titles = titles
        self._hives = hives
        self._clock = clock
        self.last_entries: tuple[ConsentEntry, ...] = ()
        self.unresolved: tuple[ConsentEntry, ...] = ()
        self._live_labels: frozenset[str] = frozenset()
        self._missing_logged = False

    def set_exclude_pids(self, pids: frozenset[int]) -> None:
        self._exclude = pids | {os.getpid()}

    def _store_present(self) -> bool:
        """The HKCU microphone key exists on every Windows 10/11 user profile (PF-210)."""
        base = f"{CONSENT_STORE}\\{MICROPHONE}"
        try:
            return bool(self._reg.values(HKCU, base) or self._reg.subkeys(HKCU, base))
        except OSError:
            return False

    def _keep(self, ref: AppRef) -> bool:
        return ref.pid not in self._exclude and ref.process_name.lower() != OBS_EXE

    def snapshot(self) -> ProbeSnapshot:
        ts = self._clock()
        confidence: Any = "high"  # not-a-fact: internal status value
        try:
            entries = read_consent_store(self._reg, self._hives)
        except OSError:
            log.warning("probe: ConsentStore not readable, microphone detection is degraded")
            entries = []
            confidence = "low"  # not-a-fact: internal status value
        if HKCU in self._hives and not self._store_present():
            if not self._missing_logged:
                log.warning("probe: no ConsentStore microphone key, detection is degraded")
                self._missing_logged = True
            confidence = "low"  # not-a-fact: internal status value
        self.last_entries = tuple(entries)
        live = [e for e in entries if e.in_use]
        labels = frozenset(e.label for e in live)
        if labels != self._live_labels:
            log.debug("probe: in use %s", sorted(labels))
            self._live_labels = labels
        mic: list[AppRef] = []
        cam: list[AppRef] = []
        unresolved: list[ConsentEntry] = []
        if live:
            try:
                procs = self._processes()
            except Exception:
                log.warning("probe: process list unavailable, microphone detection is degraded")
                procs = []
                confidence = "low"  # not-a-fact: internal status value
            by_pid = {p.pid: p for p in procs}
            for entry in live:
                refs = resolve_entry(entry, procs, by_pid)
                if not refs:
                    unresolved.append(entry)
                    continue
                target = mic if entry.capability == MICROPHONE else cam
                target.extend(r for r in refs if self._keep(r))
        self.unresolved = tuple(unresolved)
        mic_users = _unique(mic)
        titles = None
        if mic_users and self._titles is not None:
            try:
                titles = self._titles({r.pid for r in mic_users})
            except Exception:
                log.debug("probe: window titles unavailable")
        cam_users = _unique(cam)
        return ProbeSnapshot(
            ts_monotonic=ts,
            mic_users=tuple(mic_users),
            camera_active=bool(cam_users),
            camera_users=tuple(cam_users),
            window_titles=titles,
            confidence=confidence,
        )

    def close(self) -> None:
        return None


# ---- disk encryption (stage 10/11) ---------------------------------------------------------------

DRIVE_RE = re.compile(r"^[A-Za-z]:$")  # PF-225
BITLOCKER_SCRIPT = """
$r = [ordered]@{ source = 'none'; protection = ''; volume = ''; shell = '' }
try {
    $v = Get-BitLockerVolume -MountPoint $drive
    $r.source = 'cmdlet'
    $r.protection = [string]$v.ProtectionStatus
    $r.volume = [string]$v.VolumeStatus
} catch {
    try {
        $item = (New-Object -ComObject Shell.Application).NameSpace($drive + '\\').Self
        $s = $item.ExtendedProperty('System.Volume.BitLockerProtection')
        if ($null -ne $s) { $r.source = 'shell'; $r.shell = [string]$s }
    } catch { }
}
ConvertTo-Json -Compress -InputObject ([pscustomobject]$r)
"""  # PF-225, PF-226
BL_PROTECTION_ON = "On"  # PF-225
BL_PROTECTION_OFF = "Off"  # PF-225
BL_FULLY_DECRYPTED = "FullyDecrypted"  # PF-225
BL_SHELL_ON = frozenset({"1"})  # PF-226
BL_SHELL_OFF = frozenset({"2"})  # PF-226


def drive_of(path: Path | str) -> str | None:
    """Drive letter (``C:``) of an absolute Windows path; None for UNC or relative paths."""
    drive = ntpath.splitdrive(str(path))[0]
    return drive.upper() if DRIVE_RE.match(drive) else None


def parse_bitlocker(text: str) -> EncryptionStatus:
    """``Get-BitLockerVolume`` (elevated) or the shell property (unelevated) → status."""
    try:
        data = parse_json_output(text)
    except ValueError:
        return "unknown"  # not-a-fact: internal status value
    if not isinstance(data, dict):
        return "unknown"  # not-a-fact: internal status value
    source = data.get("source")  # not-a-fact: key of our own script's output
    if source == "cmdlet":  # not-a-fact: our own script's value
        protection = str(data.get("protection", ""))  # not-a-fact: our own key
        volume = str(data.get("volume", ""))  # not-a-fact: our own key
        if protection == BL_PROTECTION_ON:
            return "encrypted"  # not-a-fact: internal status value
        if protection == BL_PROTECTION_OFF and volume == BL_FULLY_DECRYPTED:
            return "unencrypted"  # not-a-fact: internal status value
        return "unknown"  # not-a-fact: internal status value
    if source == "shell":  # not-a-fact: our own script's value
        value = str(data.get("shell", "")).strip()  # not-a-fact: our own key
        if value in BL_SHELL_ON:
            return "encrypted"  # not-a-fact: internal status value
        if value in BL_SHELL_OFF:
            return "unencrypted"  # not-a-fact: internal status value
    return "unknown"  # not-a-fact: internal status value


class WindowsDiskEncryption:
    """BitLocker state of the drive below the data root.

    ``Get-BitLockerVolume`` needs elevation (PF-225); the agent never runs elevated, so the
    unelevated shell property ``System.Volume.BitLockerProtection`` is the fallback (PF-226).
    Anything else (UNC path, no answer) is ``unknown``.
    """

    def __init__(self, runner: Runner = default_runner) -> None:
        self._runner = runner

    def status(self, path: Path) -> EncryptionStatus:
        drive = drive_of(path)
        if drive is None:
            return "unknown"  # not-a-fact: internal status value
        script = f"$drive = {ps_quote(drive)}\n" + BITLOCKER_SCRIPT
        try:
            out = self._runner(powershell_argv(script), 30)
        except (OSError, subprocess.TimeoutExpired):
            return "unknown"  # not-a-fact: internal status value
        if out.returncode != 0:
            return "unknown"  # not-a-fact: internal status value
        return parse_bitlocker(out.stdout or "")


# ---- autostart (stage 11) ------------------------------------------------------------------------

TASK_NAME = "Etabli Captation"  # PF-220
TASK_URI = "\\" + TASK_NAME  # PF-220
AUMID = "de.etabli.captation"  # PF-223
AUMID_KEY = "Software\\Classes\\AppUserModelId\\" + AUMID  # PF-223
AUMID_DISPLAY_NAME_VALUE = "DisplayName"  # PF-223
APP_DISPLAY_NAME = "Établi Captation"  # not-a-fact: display name
WINDOWED_EXE = "captationw.exe"  # PF-224
PYTHONW_EXE = "pythonw.exe"  # PF-224
USERDOMAIN_VAR = "USERDOMAIN"  # PF-221
USERNAME_VAR = "USERNAME"  # PF-221
TASK_XML_FILE = "etabli-captation-task.xml"  # not-a-fact: temporary file name
TASK_XML_ENCODING = "utf-16"  # PF-221
# Artefacts as cli.py prints them: the task's scheduler path and the AUMID registry key.
TASK_ARTEFACT = Path(TASK_URI)
AUMID_ARTEFACT = Path("HKCU\\" + AUMID_KEY)  # PF-223

TASK_XML_TEMPLATE = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Starts the Etabli Captation agent at logon (always disarmed).</Description>
    <URI>{uri}</URI>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{user}</UserId>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{user}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>false</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>5</Priority>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>3</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{command}</Command>
      <Arguments>{arguments}</Arguments>
      <WorkingDirectory>{workdir}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""  # PF-221, PF-222


@dataclass(frozen=True)
class AgentLaunch:
    """How the task starts the agent: a windowed exe, so no console window appears."""

    command: str
    arguments: str
    workdir: str


def agent_launch(executable: str | None = None, frozen: bool | None = None) -> AgentLaunch:
    """Frozen build: ``captationw.exe run`` next to ``captation.exe``. Dev venv:
    ``pythonw.exe -m captation run`` (PF-224)."""
    exe = Path(executable or sys.executable)
    is_frozen = getattr(sys, "frozen", False) if frozen is None else frozen  # not-a-fact: attr
    if is_frozen:
        windowed = exe.with_name(WINDOWED_EXE)
        command = windowed if windowed.exists() else exe
        return AgentLaunch(str(command), "run", str(exe.parent))  # not-a-fact: CLI command
    pythonw = exe.with_name(PYTHONW_EXE)
    command = pythonw if pythonw.exists() else exe
    return AgentLaunch(str(command), "-m captation run", str(exe.parent))  # not-a-fact: argv


def current_user_id(env: Mapping[str, str] | None = None) -> str:
    """``DOMAIN\\user`` of the logged-on user for the task's trigger and principal."""
    env = os.environ if env is None else env
    user = env.get(USERNAME_VAR) or getpass.getuser()
    domain = env.get(USERDOMAIN_VAR)
    return f"{domain}\\{user}" if domain else user


def task_xml(launch: AgentLaunch, user_id: str) -> str:
    """Task Scheduler XML: logon trigger for this user, least privilege, restart on failure
    3× every minute, battery-independent, no execution time limit (stage 11)."""
    return TASK_XML_TEMPLATE.format(
        uri=escape(TASK_URI),
        user=escape(user_id),
        command=escape(launch.command),
        arguments=escape(launch.arguments),
        workdir=escape(launch.workdir),
    )


def schtasks_create_argv(xml_path: Path | str) -> list[str]:
    return [schtasks_exe(), "/Create", "/XML", str(xml_path), "/TN", TASK_NAME, "/F"]  # PF-220


def schtasks_query_argv() -> list[str]:
    return [schtasks_exe(), "/Query", "/TN", TASK_NAME]  # PF-220


def schtasks_delete_argv() -> list[str]:
    return [schtasks_exe(), "/Delete", "/TN", TASK_NAME, "/F"]  # PF-220


def register_aumid(reg: RegistryAccess | None = None) -> None:
    """Register the AppUserModelID so toasts (with buttons) work for this unpackaged app."""
    (reg or WinRegistry()).set_string(HKCU, AUMID_KEY, AUMID_DISPLAY_NAME_VALUE, APP_DISPLAY_NAME)


def unregister_aumid(reg: RegistryAccess | None = None) -> bool:
    return (reg or WinRegistry()).delete_key(HKCU, AUMID_KEY)


class WindowsAutostart:
    """Task Scheduler task ``Etabli Captation`` plus the AUMID key. Autostart never arms."""

    def __init__(
        self,
        *,
        runner: Runner = default_runner,
        registry: RegistryAccess | None = None,
        launch: AgentLaunch | None = None,
        user_id: str | None = None,
    ) -> None:
        self._runner = runner
        self._reg: RegistryAccess = registry or WinRegistry()
        self._launch = launch
        self._user = user_id

    def _task_exists(self) -> bool:
        try:
            return self._runner(schtasks_query_argv(), 30).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    def install(self, *, systemd: bool = False) -> list[Path]:
        if systemd:
            log.warning("autostart: --systemd is Linux only; using the Task Scheduler")
        xml = task_xml(self._launch or agent_launch(), self._user or current_user_id())
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / TASK_XML_FILE
            path.write_text(xml, encoding=TASK_XML_ENCODING)
            res = self._runner(schtasks_create_argv(path), 60)
        if res.returncode != 0:
            raise RuntimeError(
                f"schtasks /Create failed (exit {res.returncode}): {(res.stderr or '').strip()}"
            )
        register_aumid(self._reg)
        return [TASK_ARTEFACT, AUMID_ARTEFACT]

    def uninstall(self) -> list[Path]:
        removed: list[Path] = []
        if self._task_exists():
            res = self._runner(schtasks_delete_argv(), 60)
            if res.returncode == 0:
                removed.append(TASK_ARTEFACT)
            else:
                log.warning("autostart: schtasks /Delete failed (exit %d)", res.returncode)
        if unregister_aumid(self._reg):
            removed.append(AUMID_ARTEFACT)
        return removed

    def status(self) -> AutostartStatus:
        if self._task_exists():
            return AutostartStatus(True, "task", (str(TASK_ARTEFACT),))  # not-a-fact: enum
        return AutostartStatus(False, "none")  # not-a-fact: internal enum value


class WindowsPlatform:
    name = "windows"  # not-a-fact: internal OS name

    def obs_locator(self, executable: str | None) -> WindowsObsLocator:
        return WindowsObsLocator(executable)

    def probe(self, *, exclude_pids: frozenset[int], store_titles: bool) -> WindowsProbe:
        # Titles are read in memory for classification; store_titles only affects storage.
        return WindowsProbe(exclude_pids=exclude_pids)

    def disk_encryption(self) -> WindowsDiskEncryption:
        return WindowsDiskEncryption()

    def autostart(self) -> WindowsAutostart:
        return WindowsAutostart()


SERVICES = WindowsPlatform()


# ---- diagnostics: python -m captation.platform.windows [--watch SECONDS] -------------------------


def filetime_to_unix(value: int) -> float | None:
    """FILETIME (100 ns since 1601-01-01) → Unix seconds; None for 0 (PF-211)."""
    return value / 10_000_000 - FILETIME_EPOCH_OFFSET_S if value else None


def describe(probe: WindowsProbe) -> list[dict[str, Any]]:
    """Every ConsentStore entry with its hive label, in-use flag and resolved PIDs.

    Desktop apps are shown by exe basename only (no user name from the path)."""
    snap = probe.snapshot()
    users = {r.pid: r.process_name for r in (*snap.mic_users, *snap.camera_users)}
    unresolved = set(probe.unresolved)
    rows = []
    for e in probe.last_entries:
        if not (e.last_start or e.last_stop):
            continue
        rows.append(
            {
                "hive": e.hive,  # not-a-fact: report key
                "capability": e.capability,  # not-a-fact: report key
                "app": e.app,  # not-a-fact: report key
                "packaged": e.packaged,  # not-a-fact: report key
                "in_use": e.in_use,  # not-a-fact: report key
                "stale": e in unresolved,  # not-a-fact: report key
                "start_unix": filetime_to_unix(e.last_start),  # not-a-fact: report key
                "stop_unix": filetime_to_unix(e.last_stop),  # not-a-fact: report key
            }
        )
    rows.sort(key=lambda r: (not r["in_use"], str(r["hive"]), str(r["app"])))  # not-a-fact: keys
    if users:
        rows.insert(0, {"probe_users": users, "confidence": snap.confidence})  # not-a-fact: keys
    return rows


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="ConsentStore hive labels")  # not-a-fact: help
    parser.add_argument("--watch", type=float, default=0.0)  # not-a-fact: option of this tool
    args = parser.parse_args(argv)
    probe = WindowsProbe(titles=None)
    while True:
        for row in describe(probe):
            print(json.dumps(row, ensure_ascii=False))
        if args.watch <= 0:
            return 0
        print("--")  # not-a-fact: separator line
        time.sleep(args.watch)


if __name__ == "__main__":  # not-a-fact: module guard
    sys.exit(main())
