"""macOS platform services: OBS locator, Swift-probe client, FileVault
check and LaunchAgent autostart.

Prepared on Linux (ADR-015): every macOS identifier carries a ``# PF-1xx`` reference to a
**pending** fact in ``docs/platform-facts.md``; the Mac run verifies them
(``docs/runbook-macos.md``). The module imports only the standard library, so it loads on
every OS (tests run on Linux with fakes).
"""

from __future__ import annotations

import collections
import json
import logging
import os
import plistlib
import re
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, Literal, Protocol

from captation.models import (
    AppRef,
    AutostartStatus,
    Confidence,
    EncryptionStatus,
    ObsInstallation,
    ProbeSnapshot,
)

log = logging.getLogger(__name__)

ENCODING = "utf-8"  # not-a-fact: text encoding
DECODE_ERRORS = "replace"  # not-a-fact: decode policy

Runner = Callable[[list[str], float], subprocess.CompletedProcess[str]]


def _run(argv: list[str], timeout: float = 10) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(argv, 127, "", f"{type(exc).__name__}")


def _default_runner(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    return _run(argv, timeout)


# ---- OBS locator ----------------------------------------------------------------

OBS_APP_NAME = "OBS.app"  # PF-101
OBS_APP_DIRS = ("/Applications", "~/Applications")  # PF-101
OBS_EXECUTABLE_REL = "Contents/MacOS/OBS"  # PF-101
OBS_INFO_PLIST_REL = "Contents/Info.plist"  # PF-101
OBS_VERSION_KEY = "CFBundleShortVersionString"  # PF-101
OBS_PROCESS = "OBS"  # PF-101
OBS_CONFIG_REL = "Library/Application Support/obs-studio"  # PF-102
APP_SUFFIX = ".app"  # PF-101
CONTENTS_MARKER = "/Contents/"  # PF-101
# LaunchServices start: OBS becomes its own TCC-responsible process (PF-103).
OPEN = "/usr/bin/open"  # PF-103
OPEN_BACKGROUND = "-g"  # PF-103
OPEN_APP = "-a"  # PF-103
OPEN_ARGS = "--args"  # PF-103
VERSION_FLAG = "--version"  # PF-104
VERSION_RE = re.compile(r"(\d+\.\d+(?:\.\d+)?)")  # not-a-fact: version-number pattern


def _app_bundle_of(path: Path) -> Path | None:
    """``…/X.app`` for a path to the bundle or to a file inside it."""
    text = str(path)
    if text.endswith(APP_SUFFIX):
        return path
    if CONTENTS_MARKER in text:
        head = text.split(CONTENTS_MARKER, 1)[0]
        return Path(head) if head.endswith(APP_SUFFIX) else None
    return None


def bundle_version(app: Path) -> str | None:
    """``CFBundleShortVersionString`` from the bundle's Info.plist (PF-101)."""
    try:
        with (app / OBS_INFO_PLIST_REL).open("rb") as fh:  # not-a-fact: file mode
            info = plistlib.load(fh)
    except (OSError, plistlib.InvalidFileException, ValueError):
        return None
    value = info.get(OBS_VERSION_KEY) if isinstance(info, dict) else None
    return str(value) if value else None


class MacObsLocator:
    """``/Applications/OBS.app`` (or ``~/Applications``), or an explicit path.

    OBS is started through LaunchServices (``open -g -a <app> --args …``), never by executing
    the binary as a child of the agent: TCC attributes microphone and Screen Recording access
    to the *responsible* process, and only a LaunchServices start makes that OBS itself (PF-103).
    """

    def __init__(
        self,
        executable: str | None = None,
        *,
        home: Path | None = None,
        runner: Runner = _default_runner,
        app_dirs: tuple[str, ...] = OBS_APP_DIRS,
    ) -> None:
        self._executable = executable
        self._home = home or Path.home()
        self._runner = runner
        self._app_dirs = app_dirs

    def _candidates(self) -> list[Path]:
        if self._executable:
            return [Path(os.path.expanduser(self._executable))]
        out = []
        for directory in self._app_dirs:
            home_rel = directory.startswith("~/")  # not-a-fact: tilde prefix
            base = self._home / directory[2:] if home_rel else Path(directory)
            out.append(base / OBS_APP_NAME)
        return out

    def _installation(self, app: Path) -> ObsInstallation:
        version = bundle_version(app)
        if version is None:
            res = self._runner([str(app / OBS_EXECUTABLE_REL), VERSION_FLAG], 10)
            m = VERSION_RE.search(res.stdout + res.stderr)
            version = m.group(1) if m else None
        return ObsInstallation(
            kind="native",  # not-a-fact: internal enum value
            command=(OPEN, OPEN_BACKGROUND, OPEN_APP, str(app), OPEN_ARGS),
            config_dir=str(self._home / OBS_CONFIG_REL),
            version=version,
        )

    def find(self) -> ObsInstallation | None:
        for candidate in self._candidates():
            app = _app_bundle_of(candidate)
            if app is not None and (app / OBS_EXECUTABLE_REL).exists():
                return self._installation(app)
            if app is None and candidate.is_file() and os.access(candidate, os.X_OK):
                # A bare binary outside a bundle (developer builds): started directly.
                res = self._runner([str(candidate), VERSION_FLAG], 10)
                m = VERSION_RE.search(res.stdout + res.stderr)
                return ObsInstallation(
                    kind="native",  # not-a-fact: internal enum value
                    command=(str(candidate),),
                    config_dir=str(self._home / OBS_CONFIG_REL),
                    version=m.group(1) if m else None,
                )
        return None


def app_path_of(installation: ObsInstallation) -> str | None:
    """The OBS.app path inside an installation's command (``open … -a <app> --args``)."""
    for part in installation.command:
        app = _app_bundle_of(Path(part))
        if app is not None:
            return str(app)
    return None


PS = "/bin/ps"  # PF-101
PS_ARGS = ("-axo", "pid=,comm=")  # PF-101
PGREP = "/usr/bin/pgrep"  # PF-101


def parse_ps(text: str) -> list[tuple[int, str]]:
    """``ps -axo pid=,comm=`` → (pid, executable path) pairs (PF-101)."""
    out = []
    for line in text.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and parts[0].isdigit():
            out.append((int(parts[0]), parts[1].strip()))
    return out


def obs_host_pids(
    installation: ObsInstallation, runner: Runner = _default_runner
) -> frozenset[int]:
    """PIDs of OBS and its helpers (everything executing from inside OBS.app) for exclusion."""
    pids: set[int] = set()
    app = app_path_of(installation)
    if app:
        prefix = app.rstrip("/") + CONTENTS_MARKER  # not-a-fact: path separator
        out = runner([PS, *PS_ARGS], 5)
        pids.update(pid for pid, comm in parse_ps(out.stdout) if comm.startswith(prefix))
    out = runner([PGREP, "-x", OBS_PROCESS], 5)  # PF-101
    pids.update(int(p) for p in out.stdout.split() if p.isdigit())
    return frozenset(pids)


CRASH_KEY_RE = re.compile(r"crash", re.IGNORECASE)  # PF-106
OBS_INI_FILES = ("user.ini", "global.ini")  # PF-106
# OBS 33 has no automatic crash upload: after a crash its dialog offers an opt-in checkbox, and
# an upload leaves this key in global.ini [CrashHandler] (PF-106).
CRASH_UPLOAD_URL_KEY = "last_crash_log_url"  # PF-106
CRASH_UPLOAD_CONSENT_LABEL = (  # PF-106: OBS's en-US label of the opt-in checkbox
    "I have read the privacy policy and consent to the upload."  # PF-106
)


def crash_upload_settings(config_dir: Path) -> dict[str, str]:
    """``file:[section] key = value`` for every OBS ini key that mentions crashes (PF-106).

    The exact key of the opt-in crash-log upload (OBS ≥ 32 on macOS) is pending; ``doctor``
    can report what this returns until the key is verified.
    """
    found: dict[str, str] = {}
    for name in OBS_INI_FILES:
        try:
            text = (config_dir / name).read_text(encoding=ENCODING, errors=DECODE_ERRORS)
        except OSError:
            continue
        section = ""
        for raw in text.splitlines():
            line = raw.strip()
            if line.startswith("[") and line.endswith("]"):  # not-a-fact: ini syntax
                section = line[1:-1]
            elif "=" in line:  # not-a-fact: ini syntax
                key, value = (p.strip() for p in line.split("=", 1))  # not-a-fact: ini syntax
                if CRASH_KEY_RE.search(key):
                    found[f"{name}:[{section}] {key}"] = value
    return found


# ---- Swift probe client ---------------------------------------------------------

HELPER_NAME = "captation-probe"  # PF-119
HELPER_REPO_REL = "probes/macos/CaptationProbe/.build/release"  # PF-119
HELPER_PKG_REL = "_bin/macos"  # not-a-fact: build-time copy inside the package
HELPER_ENV = "CAPTATION_PROBE"  # not-a-fact: developer override of the helper path
ARG_ONCE = "--once"  # not-a-fact: our helper's CLI (probes/macos/CaptationProbe/README.md)
ARG_WATCH = "--watch"  # not-a-fact: our helper's CLI
ARG_INTERVAL = "--interval-ms"  # not-a-fact: our helper's CLI
ARG_TITLES = "--titles"  # not-a-fact: our helper's CLI
EXIT_API_UNAVAILABLE = 2  # helper exit code (README)
EXIT_PERMISSION = 3  # helper exit code (README)
EXIT_USAGE = 64  # helper exit code (README, EX_USAGE)
SCHEMA_VERSION = 1

# JSON keys of the helper output (our own schema, README "Output schema").
K_SCHEMA = "schema"  # not-a-fact: probe JSON schema
K_CONFIDENCE = "confidence"  # not-a-fact: probe JSON schema
K_API = "api"  # not-a-fact: probe JSON schema
K_MIC_USERS = "mic_users"  # not-a-fact: probe JSON schema
K_MIC_RUNNING = "mic_device_running"  # not-a-fact: probe JSON schema
K_CAMERA = "camera_active"  # not-a-fact: probe JSON schema
K_TITLES = "window_titles"  # not-a-fact: probe JSON schema
K_TITLES_STATUS = "titles_status"  # not-a-fact: probe JSON schema
K_PID = "pid"  # not-a-fact: probe JSON schema
K_BUNDLE = "bundle_id"  # not-a-fact: probe JSON schema
K_NAME = "name"  # not-a-fact: probe JSON schema
K_LOCALIZED = "localized_name"  # not-a-fact: probe JSON schema
K_PATH = "path"  # not-a-fact: probe JSON schema
K_PARENT_PID = "parent_pid"  # not-a-fact: probe JSON schema
K_PARENT_BUNDLE = "parent_bundle_id"  # not-a-fact: probe JSON schema
K_PARENT_NAME = "parent_name"  # not-a-fact: probe JSON schema
K_INPUT_DEVICE = "input_device"  # not-a-fact: probe JSON schema (PF-132)
HIGH: Confidence = "high"  # not-a-fact: internal status value
LOW: Confidence = "low"  # not-a-fact: internal status value
TITLES_MISSING = "permission_missing"  # not-a-fact: probe JSON schema

# Chromium/Electron helpers: bundle ``<app>.helper[.<kind>]``, name ``<App> Helper (<kind>)``.
HELPER_BUNDLE_RE = re.compile(r"^(?P<parent>.+?)\.helper(?:\..*)?$", re.IGNORECASE)  # PF-117
HELPER_NAME_RE = re.compile(r"^(?P<parent>.+?) Helper(?: \(.*\))?$")  # PF-117


def find_helper() -> Path | None:
    """The ``captation-probe`` binary: frozen app, package copy, repo build, then PATH."""
    candidates: list[Path] = []
    override = os.environ.get(HELPER_ENV)
    if override:
        candidates.append(Path(override))
    if getattr(sys, "frozen", False):  # not-a-fact: PyInstaller attribute
        candidates.append(Path(sys.executable).parent / HELPER_NAME)  # Contents/MacOS (PF-119)
        meipass = getattr(sys, "_MEIPASS", None)  # not-a-fact: PyInstaller attribute
        if meipass:
            candidates.append(Path(meipass) / HELPER_NAME)
    package = Path(__file__).resolve().parent.parent
    candidates.append(package / HELPER_PKG_REL / HELPER_NAME)
    candidates.append(package.parent.parent / HELPER_REPO_REL / HELPER_NAME)
    for path in candidates:
        if path.is_file() and os.access(path, os.X_OK):
            return path
    found = shutil.which(HELPER_NAME)
    return Path(found) if found else None


def helper_parent_bundle(bundle_id: str | None) -> str | None:
    """``com.google.Chrome.helper.Renderer`` → ``com.google.Chrome`` (PF-117)."""
    if not bundle_id:
        return None
    m = HELPER_BUNDLE_RE.match(bundle_id)
    return m.group("parent") if m else None  # not-a-fact: regex group name


def helper_parent_name(name: str | None) -> str | None:
    """``Google Chrome Helper (Renderer)`` → ``Google Chrome`` (PF-117)."""
    if not name:
        return None
    m = HELPER_NAME_RE.match(name)
    return m.group("parent") if m else None  # not-a-fact: regex group name


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.lstrip("-").isdigit():  # not-a-fact: sign
        return int(value)
    return None


def app_ref_from(raw: Any) -> AppRef | None:
    """One ``mic_users`` entry → AppRef with browser/Electron helpers normalised.

    The helper's PID stays (it holds the device); ``bundle_id`` becomes the parent app's bundle
    (so rules and the OBS app-audio target see the app), ``parent_app`` its name.
    """
    if not isinstance(raw, dict):
        return None
    pid = _int(raw.get(K_PID))
    if pid is None or pid <= 0:
        return None
    path = _str(raw.get(K_PATH))
    bundle = _str(raw.get(K_BUNDLE))
    name = (
        _str(raw.get(K_NAME))
        or (os.path.basename(path) if path else None)
        or _str(raw.get(K_LOCALIZED))
        or bundle
        or str(pid)
    )
    parent_bundle = _str(raw.get(K_PARENT_BUNDLE))
    parent_name = _str(raw.get(K_PARENT_NAME))
    helper_bundle = helper_parent_bundle(bundle)
    if helper_bundle is not None:
        bundle = parent_bundle or helper_bundle
    elif bundle is None and parent_bundle:
        bundle = parent_bundle
    if parent_name is None:
        parent_name = helper_parent_name(name) or helper_parent_name(_str(raw.get(K_LOCALIZED)))
    if parent_name == name:
        parent_name = None
    return AppRef(
        pid=pid,
        process_name=name,
        exe_path=path,
        bundle_id=bundle,
        parent_app=parent_name,
        input_device=_str(raw.get(K_INPUT_DEVICE)),
    )


@dataclass(frozen=True)
class ProbeReading:
    """One parsed helper line."""

    mic_users: tuple[AppRef, ...]
    camera_active: bool
    confidence: Confidence
    api: str
    mic_device_running: bool | None
    window_titles: dict[int, tuple[str, ...]] | None
    titles_status: str | None


def parse_probe_line(line: str) -> ProbeReading | None:
    """Parse one JSON line of ``captation-probe``; ``None`` for anything malformed."""
    line = line.strip()
    if not line:
        return None
    try:
        doc = json.loads(line)
    except ValueError:
        return None
    if not isinstance(doc, dict) or doc.get(K_SCHEMA) != SCHEMA_VERSION:
        return None
    raw_users = doc.get(K_MIC_USERS)
    if not isinstance(raw_users, list):
        return None
    users: dict[int, AppRef] = {}
    parents: dict[int, int] = {}
    for raw in raw_users:
        ref = app_ref_from(raw)
        if ref is not None:
            users.setdefault(ref.pid, ref)
            parent_pid = _int(raw.get(K_PARENT_PID))
            if parent_pid:
                parents[ref.pid] = parent_pid
    titles: dict[int, tuple[str, ...]] | None = None
    raw_titles = doc.get(K_TITLES)
    if isinstance(raw_titles, dict):
        titles = {}
        for key, value in raw_titles.items():
            pid = _int(key)
            if pid is not None and isinstance(value, list):
                titles[pid] = tuple(str(t) for t in value if isinstance(t, str) and t)
        # A helper holds the mic, the parent app owns the windows: share the titles.
        for helper_pid, parent_pid in parents.items():
            if helper_pid not in titles and parent_pid in titles:
                titles[helper_pid] = titles[parent_pid]
    confidence: Confidence = HIGH if doc.get(K_CONFIDENCE) == HIGH else LOW
    running = doc.get(K_MIC_RUNNING)
    return ProbeReading(
        mic_users=tuple(sorted(users.values(), key=lambda r: r.pid)),
        camera_active=doc.get(K_CAMERA) is True,
        confidence=confidence,
        api=str(doc.get(K_API) or ""),
        mic_device_running=running if isinstance(running, bool) else None,
        window_titles=titles,
        titles_status=_str(doc.get(K_TITLES_STATUS)),
    )


class HelperProcess(Protocol):
    """The part of ``subprocess.Popen`` that MacProbe uses (fakes implement it in tests)."""

    @property
    def stdout(self) -> IO[str] | None: ...

    @property
    def pid(self) -> int: ...

    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    def wait(self, timeout: float | None = None) -> int: ...


Spawner = Callable[[list[str]], HelperProcess]


def _spawn(argv: list[str]) -> HelperProcess:
    return subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
    )


ProbeMode = Literal["watch", "once", "failed"]  # not-a-fact: internal states
MODE_WATCH: ProbeMode = "watch"  # not-a-fact: internal state
MODE_ONCE: ProbeMode = "once"  # not-a-fact: internal state
MODE_FAILED: ProbeMode = "failed"  # not-a-fact: internal state
API_PROCESS_OBJECTS = "process_objects"  # not-a-fact: probe JSON schema (README)


class MacProbe:
    """CallProbe on the Swift helper ``captation-probe`` (ADR-004).

    - Runs ``captation-probe --watch --interval-ms N`` as one long-lived subprocess and keeps
      the latest JSON line; falls back to ``--once`` per poll if watch mode is unsupported.
    - A crashed or hung helper is restarted (at most ``max_restarts`` per ``restart_window_s``);
      the snapshot in between repeats the last users with confidence ``low``. With the budget
      used up the probe reports nothing with confidence ``low`` (and logs an error once) until
      the window frees a restart.
    - Excludes the agent, the helper and ``exclude_pids`` (OBS) by PID; browser/Electron helper
      processes are normalised to their parent app (:func:`app_ref_from`).
    - Low-confidence readings (device running + frontmost app, macOS < 14 or API failure) are
      ambiguous while OBS itself captures: then the users seen before OBS started are kept
      (``obs_capturing``), so OBS can neither start nor end a call on its own
      (docs/principles.md §5.2).
    """

    def __init__(
        self,
        *,
        exclude_pids: frozenset[int] = frozenset(),
        helper: Path | None = None,
        interval_ms: int = 500,
        titles: bool = True,
        spawn: Spawner = _spawn,
        run_once: Runner = _default_runner,
        clock: Callable[[], float] = time.monotonic,
        obs_capturing: Callable[[], bool] | None = None,
        max_restarts: int = 5,
        restart_window_s: float = 600.0,
        first_line_timeout_s: float | None = None,
    ) -> None:
        self._exclude = exclude_pids | {os.getpid()}
        self._helper = helper if helper is not None else find_helper()
        self._interval_ms = max(100, interval_ms)
        self._titles = titles
        self._spawn = spawn
        self._run_once = run_once
        self._clock = clock
        self._obs_capturing = obs_capturing or (lambda: False)
        self._max_restarts = max_restarts
        self._window_s = restart_window_s
        interval_s = self._interval_ms / 1000
        self._first_timeout = (
            first_line_timeout_s if first_line_timeout_s is not None else 2 * interval_s + 2.0
        )
        self._stale_after = 3 * interval_s + 2.0
        self._lock = threading.Lock()
        self._fresh = threading.Event()
        self._proc: HelperProcess | None = None
        self._reader: threading.Thread | None = None
        self._latest: ProbeReading | None = None
        self._latest_at = 0.0
        self._restarts: collections.deque[float] = collections.deque()
        self._mode: ProbeMode = MODE_WATCH
        self._failed_logged = False
        self._sticky_users: tuple[AppRef, ...] = ()
        self._titles_logged = False

    # ---- helper process -------------------------------------------------------------------

    def set_exclude_pids(self, pids: frozenset[int]) -> None:
        self._exclude = pids | {os.getpid()}

    @property
    def mode(self) -> ProbeMode:
        return self._mode

    def _argv(self, *mode: str) -> list[str]:
        assert self._helper is not None
        return [str(self._helper), *mode, *([ARG_TITLES] if self._titles else [])]

    def _start_watch(self) -> bool:
        argv = self._argv(ARG_WATCH, ARG_INTERVAL, str(self._interval_ms))
        try:
            proc = self._spawn(argv)
        except OSError:
            log.error("probe: cannot start the macOS probe helper")
            return False
        self._fresh.clear()
        with self._lock:
            self._proc = proc
            self._latest = None
        reader = threading.Thread(target=self._pump, args=(proc,), daemon=True)
        self._reader = reader
        reader.start()
        return True

    def _pump(self, proc: HelperProcess) -> None:
        stream = proc.stdout
        if stream is None:
            return
        try:
            for line in stream:
                reading = parse_probe_line(line)
                if reading is None:
                    continue
                with self._lock:
                    if proc is not self._proc:
                        return
                    self._latest = reading
                    self._latest_at = self._clock()
                self._fresh.set()
        except (OSError, ValueError):
            pass
        # End of output (helper exited or crashed): wake a snapshot waiting for a line.
        with self._lock:
            current = proc is self._proc
        if current:
            self._fresh.set()

    def _stop_proc(self) -> None:
        with self._lock:
            proc, self._proc = self._proc, None
        if proc is None:
            return
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    log.warning("probe: helper did not exit after SIGKILL")
        if self._reader is not None and self._reader is not threading.current_thread():
            self._reader.join(timeout=2)
        self._reader = None

    def _restart_allowed(self) -> bool:
        now = self._clock()
        while self._restarts and now - self._restarts[0] > self._window_s:
            self._restarts.popleft()
        return len(self._restarts) < self._max_restarts

    def _restart(self, why: str) -> bool:
        self._stop_proc()
        if not self._restart_allowed():
            if not self._failed_logged:
                log.error("probe: helper restart budget used up (%s)", why)
                self._failed_logged = True
            self._mode = MODE_FAILED
            return False
        self._restarts.append(self._clock())
        log.warning("probe: restarting the macOS probe helper (%s)", why)
        if self._start_watch():
            self._mode = MODE_WATCH
            self._failed_logged = False
            return True
        self._mode = MODE_FAILED
        return False

    def _degraded(self) -> ProbeReading | None:
        """The last reading, marked low confidence (crash or restart in between)."""
        with self._lock:
            last = self._latest
        if last is None:
            return None
        return ProbeReading(
            mic_users=last.mic_users,
            camera_active=last.camera_active,
            confidence=LOW,
            api=last.api,
            mic_device_running=last.mic_device_running,
            window_titles=last.window_titles,
            titles_status=last.titles_status,
        )

    def _read_watch(self) -> ProbeReading | None:
        if self._proc is None and not self._start_watch():
            self._mode = MODE_FAILED
            return None
        proc = self._proc
        assert proc is not None
        with self._lock:
            have = self._latest is not None
        if not have and proc.poll() is None:
            self._fresh.wait(self._first_timeout)
        code = proc.poll()
        if code is None:
            with self._lock:
                have = self._latest is not None
            if not have and self._fresh.is_set():
                # Woken by the end of the output: give the exit status a moment to appear.
                try:
                    code = proc.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    code = None
        # code None: running (a hung or silent helper is caught by the staleness check)
        if code is not None:
            if code == EXIT_USAGE:
                log.warning("probe: helper has no watch mode, polling with --once")
                self._stop_proc()
                self._mode = MODE_ONCE
                return self._read_once()
            if code in (EXIT_API_UNAVAILABLE, EXIT_PERMISSION):
                log.error("probe: helper reports exit code %d (API or permission)", code)
            last = self._degraded()
            self._restart(f"exit code {code}")
            return last
        with self._lock:
            latest, latest_at = self._latest, self._latest_at
        if latest is None or self._clock() - latest_at > self._stale_after:
            last = self._degraded()
            self._restart("no output")  # not-a-fact: log detail
            return last
        return latest

    def _read_once(self) -> ProbeReading | None:
        res = self._run_once(self._argv(ARG_ONCE), 5.0)
        if res.returncode != 0:
            log.warning("probe: helper --once failed with exit code %d", res.returncode)
            return None
        for line in reversed(res.stdout.splitlines()):
            reading = parse_probe_line(line)
            if reading is not None:
                with self._lock:
                    self._latest = reading
                    self._latest_at = self._clock()
                return reading
        return None

    def _read(self) -> ProbeReading | None:
        if self._helper is None:
            if not self._failed_logged:
                log.error("probe: captation-probe helper not found (build it, see runbook)")
                self._failed_logged = True
            self._mode = MODE_FAILED
            return None
        if self._mode == MODE_FAILED:
            if not self._restart_allowed():
                return None
            if not self._restart("retry after failure"):  # not-a-fact: log detail
                return None
        if self._mode == MODE_ONCE:
            return self._read_once()
        return self._read_watch()

    # ---- CallProbe ------------------------------------------------------------------------

    def snapshot(self) -> ProbeSnapshot:
        reading = self._read()
        ts = self._clock()
        if reading is None:
            return ProbeSnapshot(ts_monotonic=ts, mic_users=(), camera_active=False, confidence=LOW)
        exclude = set(self._exclude)
        if self._proc is not None:
            exclude.add(self._proc.pid)
        users = tuple(u for u in reading.mic_users if u.pid not in exclude)
        if reading.confidence == LOW and reading.api != API_PROCESS_OBJECTS:
            if self._obs_capturing():
                users = self._sticky_users
            else:
                self._sticky_users = users
        if reading.titles_status == TITLES_MISSING and not self._titles_logged:
            log.info("probe: window titles need the Screen Recording permission (optional)")
            self._titles_logged = True
        return ProbeSnapshot(
            ts_monotonic=ts,
            mic_users=users,
            camera_active=reading.camera_active,
            camera_users=(),  # CoreMediaIO has no attribution
            window_titles=reading.window_titles,
            confidence=reading.confidence,
        )

    def close(self) -> None:
        """Stop the helper. A later snapshot starts it again and never sees a reading from
        before the close (the agent closes the probe whenever it is disarmed)."""
        self._stop_proc()
        with self._lock:
            self._latest = None
        self._fresh.clear()


# ---- disk encryption -----------------------------------------------------------

FDESETUP = "/usr/bin/fdesetup"  # PF-120
FDE_ON = "FileVault is On."  # PF-120
FDE_OFF = "FileVault is Off."  # PF-120
FDE_IN_PROGRESS = "in progress"  # PF-120
DATA_VOLUME = "/System/Volumes/Data"  # PF-121
DISKUTIL = "/usr/sbin/diskutil"  # PF-121
DISKUTIL_FILEVAULT = "FileVault"  # PF-121


def parse_fdesetup(text: str) -> EncryptionStatus:
    """``fdesetup status`` output → status (PF-120). Conversion in progress → unknown."""
    if FDE_IN_PROGRESS in text:
        return "unknown"  # not-a-fact: internal status value
    if FDE_ON in text:
        return "encrypted"  # not-a-fact: internal status value
    if FDE_OFF in text:
        return "unencrypted"  # not-a-fact: internal status value
    return "unknown"  # not-a-fact: internal status value


def mount_point(path: Path) -> Path:
    """The mount point of the volume holding ``path`` (``path`` itself or its nearest ancestor)."""
    target = path.absolute()
    while not os.path.ismount(target) and target != target.parent:
        target = target.parent
    return target


class MacDiskEncryption:
    """FileVault for the boot data volume (``fdesetup status``), ``diskutil info`` otherwise."""

    def __init__(
        self,
        runner: Runner = _default_runner,
        data_volume: Path = Path(DATA_VOLUME),
        device_of: Callable[[Path], int] = lambda p: p.stat().st_dev,
        mount_point_of: Callable[[Path], Path] | None = None,
    ) -> None:
        self._runner = runner
        self._data_volume = data_volume
        self._device_of = device_of
        self._mount_point_of = mount_point_of or mount_point

    def status(self, path: Path) -> EncryptionStatus:
        # Symlinks are resolved first: an unresolved link would lead the mount-point walk
        # to "/" (an encrypted system volume) while the data lives elsewhere.
        target = Path(os.path.realpath(path))
        while not target.exists() and target != target.parent:
            target = target.parent
        try:
            same = self._device_of(target) == self._device_of(self._data_volume)
        except OSError:
            same = False
        if same:
            return parse_fdesetup(self._runner([FDESETUP, "status"], 10).stdout)  # PF-120
        # diskutil takes a mount point or disk, not any folder ("Could not find disk", PF-121)
        volume = self._mount_point_of(target)
        try:
            if self._device_of(volume) != self._device_of(target):
                return "unknown"  # not-a-fact: the walk left the volume (never guess)
        except OSError:
            return "unknown"  # not-a-fact: internal status value
        out = self._runner([DISKUTIL, "info", "-plist", str(volume)], 10)  # PF-121
        if out.returncode != 0:
            return "unknown"  # not-a-fact: internal status value
        try:
            info = plistlib.loads(out.stdout.encode(ENCODING))
        except (plistlib.InvalidFileException, ValueError):
            return "unknown"  # not-a-fact: internal status value
        value = info.get(DISKUTIL_FILEVAULT) if isinstance(info, dict) else None
        if isinstance(value, bool):
            return "encrypted" if value else "unencrypted"  # not-a-fact: internal status value
        return "unknown"  # not-a-fact: internal status value


# ---- autostart: LaunchAgent -------------------------------------------------------

LABEL = "de.etabli.captation"  # not-a-fact: our own launchd label (= bundle ID)
LAUNCH_AGENTS_REL = "Library/LaunchAgents"  # PF-122
PLIST_SUFFIX = ".plist"  # PF-122
LAUNCHCTL = "/bin/launchctl"  # PF-122
GUI_DOMAIN = "gui"  # PF-122
LD_LABEL = "Label"  # PF-122
LD_PROGRAM_ARGUMENTS = "ProgramArguments"  # PF-122
LD_RUN_AT_LOAD = "RunAtLoad"  # PF-122
LD_KEEP_ALIVE = "KeepAlive"  # PF-122
LD_SUCCESSFUL_EXIT = "SuccessfulExit"  # PF-122
LD_SESSION_TYPE = "LimitLoadToSessionType"  # PF-122
LD_AQUA = "Aqua"  # PF-122
LD_PROCESS_TYPE = "ProcessType"  # PF-122
LD_INTERACTIVE = "Interactive"  # PF-122
LD_STDOUT = "StandardOutPath"  # PF-122
LD_STDERR = "StandardErrorPath"  # PF-122
LD_ENV = "EnvironmentVariables"  # PF-122
LD_EXIT_TIMEOUT = "ExitTimeOut"  # PF-122: seconds between SIGTERM and SIGKILL (default 20)
LD_THROTTLE = "ThrottleInterval"  # PF-122: minimum seconds between (re)starts (default 10)
EXIT_TIMEOUT_S = 60  # stop the recording and finalise the session before launchd kills us
THROTTLE_S = 60  # a broken install (no OBS, bad config) restarts at most once a minute
STDOUT_LOG = "launchd.out.log"  # not-a-fact: our own file name
STDERR_LOG = "launchd.err.log"  # not-a-fact: our own file name
# launchd starts agents with a minimal PATH; ffmpeg usually comes from Homebrew (PF-122).
BASE_PATH = (
    "/opt/homebrew/bin",  # PF-122
    "/usr/local/bin",  # PF-122
    "/usr/bin",  # PF-122
    "/bin",  # PF-122
    "/usr/sbin",  # PF-122
    "/sbin",  # PF-122
)
PATH_VAR = "PATH"  # not-a-fact: environment variable name
RUN_COMMAND = "run"  # not-a-fact: our CLI sub-command (never "arm": autostart never arms)
BOOTSTRAP_RETRIES = 3


def agent_command() -> str:
    """The command that starts the agent: the frozen executable or the venv script."""
    if getattr(sys, "frozen", False):  # not-a-fact: PyInstaller attribute
        return sys.executable
    name = "captation"  # not-a-fact: entry-point name
    script = Path(sys.executable).parent / name
    return str(script if script.exists() else shutil.which(name) or script)


def launch_path(current: str | None) -> str:
    """PATH for the agent: the installing shell's PATH plus the Homebrew/system defaults."""
    parts: list[str] = []
    for part in [*(current or "").split(os.pathsep), *BASE_PATH]:
        if part and part not in parts:
            parts.append(part)
    return os.pathsep.join(parts)


def launch_agent_plist(command: str, log_dir: Path, path_env: str) -> dict[str, Any]:
    """The LaunchAgent definition (PF-122). ``KeepAlive.SuccessfulExit=false``: launchd
    restarts the agent after a crash, not after a clean exit."""
    return {
        LD_LABEL: LABEL,
        LD_PROGRAM_ARGUMENTS: [command, RUN_COMMAND],
        LD_RUN_AT_LOAD: True,
        LD_KEEP_ALIVE: {LD_SUCCESSFUL_EXIT: False},
        LD_EXIT_TIMEOUT: EXIT_TIMEOUT_S,
        LD_THROTTLE: THROTTLE_S,
        LD_SESSION_TYPE: LD_AQUA,
        LD_PROCESS_TYPE: LD_INTERACTIVE,
        LD_STDOUT: str(log_dir / STDOUT_LOG),
        LD_STDERR: str(log_dir / STDERR_LOG),
        LD_ENV: {PATH_VAR: path_env},
    }


def _default_log_dir() -> Path:
    from captation.logs import log_dir

    return log_dir()


class MacAutostart:
    """``~/Library/LaunchAgents/de.etabli.captation.plist``, loaded with ``launchctl bootstrap``.

    Install and uninstall are idempotent; uninstall removes the plist and the two launchd log
    files it caused. Autostart never arms (the agent always starts disarmed).
    """

    def __init__(
        self,
        home: Path | None = None,
        runner: Runner = _default_runner,
        uid: int | None = None,
        log_dir: Callable[[], Path] = _default_log_dir,
        command: Callable[[], str] = agent_command,
        environ_path: str | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._sleep = sleep
        self._home = home or Path.home()
        self._runner = runner
        self._uid = uid if uid is not None else _current_uid()
        self._log_dir = log_dir
        self._command = command
        self._environ_path = environ_path

    @property
    def plist_path(self) -> Path:
        return self._home / LAUNCH_AGENTS_REL / f"{LABEL}{PLIST_SUFFIX}"

    @property
    def domain(self) -> str:
        return f"{GUI_DOMAIN}/{self._uid}"

    @property
    def service(self) -> str:
        return f"{self.domain}/{LABEL}"

    def loaded(self) -> bool:
        return self._runner([LAUNCHCTL, "print", self.service], 10).returncode == 0  # PF-122

    def _bootout(self) -> None:
        self._runner([LAUNCHCTL, "bootout", self.service], 30)  # PF-122

    def start_now(self) -> bool:
        """Start the loaded LaunchAgent now (the app was opened while it was not running, e.g.
        after macOS's "Quit & Reopen"): the agent stays under launchd, which restarts it after a
        crash. False if the LaunchAgent is not loaded."""
        if not self.loaded():
            return False
        return self._runner([LAUNCHCTL, "kickstart", self.service], 30).returncode == 0  # PF-122

    def install(self, *, systemd: bool = False) -> list[Path]:
        """Write the plist and (re)load it. ``systemd`` has no meaning on macOS."""
        logs = self._log_dir()
        path_env = self._environ_path
        if path_env is None:
            path_env = os.environ.get(PATH_VAR)
        definition = launch_agent_plist(self._command(), logs, launch_path(path_env))
        data = plistlib.dumps(definition, fmt=plistlib.FMT_XML, sort_keys=True)
        path = self.plist_path
        unchanged = path.exists() and path.read_bytes() == data
        if unchanged and self.loaded():
            return [path]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        if self.loaded():
            self._bootout()
        # Right after a bootout launchd may still be tearing the old job down: retry briefly.
        res = self._runner([LAUNCHCTL, "bootstrap", self.domain, str(path)], 30)  # PF-122
        for _ in range(BOOTSTRAP_RETRIES):
            if res.returncode == 0 or self.loaded():
                return [path]
            self._sleep(0.5)
            res = self._runner([LAUNCHCTL, "bootstrap", self.domain, str(path)], 30)  # PF-122
        if res.returncode != 0 and not self.loaded():
            raise RuntimeError(
                f"launchctl bootstrap failed ({res.returncode}): {res.stderr.strip()[:200]}"
            )
        return [path]

    def uninstall(self) -> list[Path]:
        removed: list[Path] = []
        if self.loaded():
            self._bootout()
        path = self.plist_path
        logs: Path | None = None
        if path.exists():
            try:
                definition = plistlib.loads(path.read_bytes())
                if isinstance(definition, dict):
                    out = definition.get(LD_STDOUT)
                    logs = Path(out).parent if isinstance(out, str) else None
            except (plistlib.InvalidFileException, ValueError):
                logs = None
            path.unlink()
            removed.append(path)
        for name in (STDOUT_LOG, STDERR_LOG):
            log_file = (logs or self._log_dir()) / name
            if log_file.exists():
                log_file.unlink()
                removed.append(log_file)
        return removed

    def status(self) -> AutostartStatus:
        if self.plist_path.exists():
            entry = (str(self.plist_path),)
            return AutostartStatus(True, "launchagent", entry)  # not-a-fact: internal enum value
        return AutostartStatus(False, "none")  # not-a-fact: internal enum value


# ---- services ------------------------------------------------------------------------------


class MacPlatform:
    name = "macos"  # not-a-fact: internal OS name

    def obs_locator(self, executable: str | None) -> MacObsLocator:
        return MacObsLocator(executable)

    def probe(
        self,
        *,
        exclude_pids: frozenset[int],
        store_titles: bool,
        obs_capturing: Callable[[], bool] | None = None,
    ) -> MacProbe:
        # Titles are read in memory where the Screen Recording permission allows it (the
        # helper never prompts); store_titles only decides what is stored (principles §3.1).
        # obs_capturing keeps OBS's own microphone use from starting or ending a call in the
        # low-confidence fallback (principles §5.2).
        return MacProbe(exclude_pids=exclude_pids, titles=True, obs_capturing=obs_capturing)

    def disk_encryption(self) -> MacDiskEncryption:
        return MacDiskEncryption()

    def autostart(self) -> MacAutostart:
        return MacAutostart()


SERVICES = MacPlatform()


def _current_uid() -> int:
    if sys.platform == "win32":  # not-a-fact: interpreter platform name (mypy narrowing)
        return -1
    return os.getuid()
