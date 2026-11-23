"""WindowsProbe logic on Linux: a dict-backed registry and fake process lists."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable
from typing import Any

import pytest

from captation.platform import windows as win
from captation.platform.windows import (
    CONSENT_STORE,
    ConsentEntry,
    ProcInfo,
    WindowsProbe,
    decode_nonpackaged,
    package_dir_matches,
    read_consent_store,
)

# ---- shared fakes (imported by the other test_windows_* modules) -----------------------------


class FakeRegistry:
    """Registry keys as {(hive, path): values}; subkeys derive from the stored paths."""

    def __init__(self) -> None:
        self.keys: dict[tuple[str, str], dict[str, object]] = {}
        self.fail = False

    def add(self, hive: str, path: str, **values: object) -> None:
        self.keys.setdefault((hive, path), {}).update(values)

    def subkeys(self, hive: str, path: str) -> list[str]:
        if self.fail:
            raise OSError("registry unavailable")
        prefix = path.lower() + "\\"
        out: list[str] = []
        for h, key in self.keys:
            if h == hive and key.lower().startswith(prefix):
                child = key[len(prefix) :].split("\\", 1)[0]
                if child not in out:
                    out.append(child)
        return out

    def values(self, hive: str, path: str) -> dict[str, object]:
        for (h, key), vals in self.keys.items():
            if h == hive and key.lower() == path.lower():
                return dict(vals)
        return {}

    def set_string(self, hive: str, path: str, name: str, value: str) -> None:
        self.add(hive, path, **{name: value})

    def delete_key(self, hive: str, path: str) -> bool:
        found = [k for k in self.keys if k[0] == hive and k[1].lower() == path.lower()]
        for k in found:
            del self.keys[k]
        return bool(found)


class FakeRunner:
    """Records argv lists and answers with scripted results (first matching predicate)."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.rules: list[tuple[Callable[[list[str]], bool], int, str]] = []

    def on(self, predicate: Callable[[list[str]], bool], rc: int = 0, out: str = "") -> None:
        self.rules.append((predicate, rc, out))

    def __call__(self, argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        for predicate, rc, out in self.rules:
            if predicate(argv):
                return subprocess.CompletedProcess(argv, rc, out, "")
        return subprocess.CompletedProcess(argv, 0, "", "")


def decoded_script(argv: list[str]) -> str:
    """The PowerShell script of a ``powershell_argv`` call."""
    import base64

    i = argv.index("-EncodedCommand")
    return base64.b64decode(argv[i + 1]).decode("utf-16-le")


# ---- fixtures ------------------------------------------------------------------------------------

USER = "C:\\Users\\alice"
ZOOM = f"{USER}\\AppData\\Roaming\\Zoom\\bin\\Zoom.exe"
CHROME = "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe"
OBS = "C:\\Program Files\\obs-studio\\bin\\64bit\\obs64.exe"
WEBVIEW = (
    "C:\\Program Files (x86)\\Microsoft\\EdgeWebView\\Application\\131.0.2903.70\\"
    "msedgewebview2.exe"
)
WA_FAMILY = "5319275A.WhatsAppDesktop_cv1g1gvanyjgm"
WA_EXE = (
    "C:\\Program Files\\WindowsApps\\5319275A.WhatsAppDesktop_2.2450.6.0_x64__cv1g1gvanyjgm\\"
    "WhatsApp.exe"
)
TEAMS_FAMILY = "MSTeams_8wekyb3d8bbwe"
TEAMS_EXE = (
    "C:\\Program Files\\WindowsApps\\MSTeams_24295.605.3225.8804_x64__8wekyb3d8bbwe\\ms-teams.exe"
)
T0 = 133_700_000_000_000_000  # a FILETIME in 2024


def key(cap: str, *rest: str) -> str:
    return "\\".join([CONSENT_STORE, cap, *rest])


def nonpackaged(path: str) -> str:
    return path.replace("\\", "#")


def registry(*entries: tuple[str, str, str | None, str, int, int]) -> FakeRegistry:
    """entries: (hive, capability, package family or None, exe path or '', start, stop)."""
    reg = FakeRegistry()
    for hive, cap, family, exe, start, stop in entries:
        reg.add(hive, key(cap), Value="Allow")
        path = key(cap, family) if family else key(cap, "NonPackaged", nonpackaged(exe))
        reg.add(hive, path, LastUsedTimeStart=start, LastUsedTimeStop=stop)
    return reg


def probe(
    reg: FakeRegistry,
    procs: list[ProcInfo],
    *,
    exclude: frozenset[int] = frozenset(),
    titles: dict[int, tuple[str, ...]] | None = None,
) -> WindowsProbe:
    title_calls: list[set[int]] = []

    def read_titles(pids: set[int]) -> dict[int, tuple[str, ...]]:
        title_calls.append(pids)
        return {p: t for p, t in (titles or {}).items() if p in pids}

    p = WindowsProbe(
        exclude_pids=exclude,
        registry=reg,
        processes=lambda: procs,
        titles=read_titles,
        clock=lambda: 42.0,
    )
    p.title_calls = title_calls  # type: ignore[attr-defined]
    return p


# ---- ConsentStore parsing ------------------------------------------------------------------------


def test_decode_nonpackaged_key() -> None:
    assert decode_nonpackaged("C:#Program Files#Zoom#bin#Zoom.exe") == (
        "C:\\Program Files\\Zoom\\bin\\Zoom.exe"
    )


@pytest.mark.req("R-35")
def test_consent_store_reads_both_hives_and_labels_them() -> None:
    reg = registry(
        ("HKCU", "microphone", None, ZOOM, T0, 0),
        ("HKLM", "microphone", None, ZOOM, T0, 0),
        ("HKCU", "webcam", WA_FAMILY, "", T0, T0 + 5),
    )
    entries = read_consent_store(reg)  # type: ignore[arg-type]
    assert {(e.hive, e.capability, e.packaged) for e in entries} == {
        ("HKCU", "microphone", False),
        ("HKLM", "microphone", False),
        ("HKCU", "webcam", True),
    }
    # the same app in both hives stays two labelled entries, never merged
    zoom = [e for e in entries if e.capability == "microphone"]
    assert len(zoom) == 2 and {e.label for e in zoom} == {
        "HKCU:microphone:Zoom.exe",
        "HKLM:microphone:Zoom.exe",
    }
    assert all(e.name == ZOOM for e in zoom)
    assert [e.in_use for e in entries if e.packaged] == [False]


def test_label_never_contains_the_user_path() -> None:
    entry = ConsentEntry("HKCU", "microphone", False, ZOOM, T0, 0)
    assert "alice" not in entry.label and entry.app == "Zoom.exe"


@pytest.mark.parametrize(
    ("start", "stop", "in_use"),
    [(T0, 0, True), (T0, T0 + 1, False), (0, 0, False), (0, T0, False)],
)
def test_in_use_rule(start: int, stop: int, in_use: bool) -> None:
    assert ConsentEntry("HKCU", "microphone", False, ZOOM, start, stop).in_use is in_use


def test_non_integer_timestamps_count_as_zero() -> None:
    reg = FakeRegistry()
    reg.add("HKCU", key("microphone", "NonPackaged", nonpackaged(ZOOM)), LastUsedTimeStart="x")
    [entry] = read_consent_store(reg, hives=("HKCU",))  # type: ignore[arg-type]
    assert entry.last_start == 0 and not entry.in_use


@pytest.mark.parametrize(
    ("family", "exe", "ok"),
    [
        (WA_FAMILY, WA_EXE, True),
        (WA_FAMILY, WA_EXE.upper(), True),
        (WA_FAMILY, WA_EXE.replace("cv1g1gvanyjgm", "otherpublisher"), False),
        (TEAMS_FAMILY, TEAMS_EXE, True),
        (TEAMS_FAMILY, WA_EXE, False),
        (WA_FAMILY, ZOOM, False),
        ("nounderscore", WA_EXE, False),
        (WA_FAMILY, "C:\\Program Files\\WindowsApps\\WhatsApp.exe", False),
    ],
)
def test_package_dir_matching(family: str, exe: str, ok: bool) -> None:
    assert package_dir_matches(family, exe) is ok


# ---- PID mapping ---------------------------------------------------------------------------------


@pytest.mark.req("R-03")
def test_desktop_app_maps_to_pid_case_insensitively() -> None:
    reg = registry(("HKCU", "microphone", None, ZOOM, T0, 0))
    procs = [
        ProcInfo(500, 4, "explorer.exe", "C:\\Windows\\explorer.exe"),
        ProcInfo(1200, 500, "Zoom.exe", ZOOM.lower()),
    ]
    snap = probe(reg, procs).snapshot()
    [ref] = snap.mic_users
    assert (ref.pid, ref.process_name, ref.package_id) == (1200, "Zoom.exe", None)
    assert snap.camera_active is False and snap.confidence == "high" and snap.ts_monotonic == 42.0


@pytest.mark.req("R-07")
def test_browser_children_collapse_to_the_main_process() -> None:
    reg = registry(("HKCU", "microphone", None, CHROME, T0, 0))
    procs = [
        ProcInfo(500, 4, "explorer.exe", "C:\\Windows\\explorer.exe"),
        ProcInfo(2000, 500, "chrome.exe", CHROME),
        ProcInfo(2001, 2000, "chrome.exe", CHROME),  # GPU process
        ProcInfo(2002, 2000, "chrome.exe", CHROME),  # audio service (holds the mic)
        ProcInfo(2003, 2001, "chrome.exe", CHROME),
    ]
    [ref] = probe(reg, procs).snapshot().mic_users
    assert ref.pid == 2000 and ref.process_name == "chrome.exe"


@pytest.mark.req("R-07")
def test_webview2_helper_is_attributed_to_its_host_app() -> None:
    reg = registry(("HKCU", "microphone", None, WEBVIEW, T0, 0))
    procs = [
        ProcInfo(700, 500, "ms-teams.exe", TEAMS_EXE),
        ProcInfo(701, 700, "msedgewebview2.exe", WEBVIEW),
        ProcInfo(702, 701, "msedgewebview2.exe", WEBVIEW),
    ]
    [ref] = probe(reg, procs).snapshot().mic_users
    assert (ref.pid, ref.process_name, ref.exe_path) == (700, "ms-teams.exe", TEAMS_EXE)


def test_webview2_without_a_known_parent_stays_itself() -> None:
    reg = registry(("HKCU", "microphone", None, WEBVIEW, T0, 0))
    procs = [ProcInfo(701, 9999, "msedgewebview2.exe", WEBVIEW)]
    [ref] = probe(reg, procs).snapshot().mic_users
    assert ref.pid == 701


def test_packaged_app_maps_by_install_folder_and_sets_package_id() -> None:
    reg = registry(("HKCU", "microphone", WA_FAMILY, "", T0, 0))
    procs = [
        ProcInfo(900, 500, "WhatsApp.exe", WA_EXE),
        ProcInfo(901, 900, "WhatsApp.exe", WA_EXE),
        ProcInfo(950, 500, "Zoom.exe", ZOOM),
    ]
    [ref] = probe(reg, procs).snapshot().mic_users
    assert (ref.pid, ref.package_id, ref.process_name) == (900, WA_FAMILY, "WhatsApp.exe")


@pytest.mark.req("R-35")
def test_obs_excluded_pids_and_the_agent_never_count() -> None:
    reg = registry(
        ("HKCU", "microphone", None, OBS, T0, 0),
        ("HKCU", "microphone", None, ZOOM, T0, 0),
        ("HKCU", "microphone", None, CHROME, T0, 0),
        ("HKCU", "microphone", None, sys.executable, T0, 0),
        ("HKCU", "webcam", None, OBS, T0, 0),
    )
    procs = [
        ProcInfo(300, 500, "obs64.exe", OBS),
        ProcInfo(1200, 500, "Zoom.exe", ZOOM),
        ProcInfo(2000, 500, "chrome.exe", CHROME),
        ProcInfo(os.getpid(), 500, "python.exe", sys.executable),
    ]
    p = probe(reg, procs, exclude=frozenset({2000}))
    snap = p.snapshot()
    assert [r.pid for r in snap.mic_users] == [1200]
    assert snap.camera_active is False  # OBS's own camera use never counts
    p.set_exclude_pids(frozenset())
    assert [r.pid for r in p.snapshot().mic_users] == [1200, 2000]


def test_stale_entry_without_a_process_is_ignored_and_reported() -> None:
    reg = registry(
        ("HKCU", "microphone", None, ZOOM, T0, 0),  # Zoom crashed, stop never written
        ("HKCU", "webcam", WA_FAMILY, "", T0, 0),  # WhatsApp not running
    )
    p = probe(reg, [ProcInfo(500, 4, "explorer.exe", "C:\\Windows\\explorer.exe")])
    snap = p.snapshot()
    assert snap.mic_users == () and snap.camera_active is False
    assert {e.app for e in p.unresolved} == {"Zoom.exe", WA_FAMILY}


@pytest.mark.req("R-02")
def test_camera_users_and_camera_active() -> None:
    reg = registry(
        ("HKCU", "microphone", None, ZOOM, T0, 0),
        ("HKCU", "webcam", None, ZOOM, T0, 0),
        ("HKLM", "webcam", None, ZOOM, T0, 0),
    )
    snap = probe(reg, [ProcInfo(1200, 500, "Zoom.exe", ZOOM)]).snapshot()
    assert snap.camera_active is True and [r.pid for r in snap.camera_users] == [1200]
    assert [r.pid for r in snap.mic_users] == [1200]


def test_hklm_only_entry_counts_when_it_maps_to_a_visible_process() -> None:
    reg = registry(("HKLM", "microphone", None, ZOOM, T0, 0))
    procs = [ProcInfo(1200, 500, "Zoom.exe", ZOOM), ProcInfo(1300, 500, "other.exe", None)]
    p = probe(reg, procs)
    assert [r.pid for r in p.snapshot().mic_users] == [1200]
    assert [e.label for e in p.last_entries] == ["HKLM:microphone:Zoom.exe"]


def test_hives_can_be_restricted() -> None:
    reg = registry(("HKLM", "microphone", None, ZOOM, T0, 0))
    p = WindowsProbe(
        registry=reg, processes=lambda: [ProcInfo(1200, 1, "Zoom.exe", ZOOM)], hives=("HKCU",)
    )
    assert p.snapshot().mic_users == ()


def test_no_process_scan_without_an_in_use_entry() -> None:
    reg = registry(("HKCU", "microphone", None, ZOOM, T0, T0 + 1))

    def boom() -> list[ProcInfo]:
        raise AssertionError("process list must not be read while nothing is in use")

    p = WindowsProbe(registry=reg, processes=boom, titles=None)
    assert p.snapshot().mic_users == ()


def test_titles_are_read_only_for_mic_users() -> None:
    reg = registry(("HKCU", "microphone", None, CHROME, T0, 0))
    procs = [ProcInfo(2000, 500, "chrome.exe", CHROME), ProcInfo(2001, 2000, "chrome.exe", CHROME)]
    p = probe(reg, procs, titles={2000: ("Meet - abc-defg-hij - Google Chrome",), 9: ("x",)})
    snap = p.snapshot()
    assert snap.window_titles == {2000: ("Meet - abc-defg-hij - Google Chrome",)}
    assert p.title_calls == [{2000}]  # type: ignore[attr-defined]
    idle = probe(registry(), procs)
    assert idle.snapshot().window_titles is None and idle.title_calls == []  # type: ignore[attr-defined]


def test_registry_and_process_failures_degrade_confidence() -> None:
    reg = registry(("HKCU", "microphone", None, ZOOM, T0, 0))
    reg.fail = True
    assert probe(reg, []).snapshot().confidence == "low"

    def broken() -> list[ProcInfo]:
        raise RuntimeError("psutil unavailable")

    ok_reg = registry(("HKCU", "microphone", None, ZOOM, T0, 0))
    p = WindowsProbe(registry=ok_reg, processes=broken, titles=None)
    snap = p.snapshot()
    assert snap.confidence == "low" and snap.mic_users == ()


def test_missing_consent_store_lowers_confidence(caplog: pytest.LogCaptureFixture) -> None:
    empty = probe(FakeRegistry(), [])
    assert empty.snapshot().confidence == "low" and empty.snapshot().mic_users == ()
    assert caplog.text.count("no ConsentStore microphone key") == 1  # warned once
    present = registry(("HKCU", "microphone", None, ZOOM, T0, T0 + 1))
    assert probe(present, []).snapshot().confidence == "high"


def test_describe_lists_labelled_entries_without_user_paths() -> None:
    reg = registry(
        ("HKCU", "microphone", None, ZOOM, T0, 0),
        ("HKLM", "microphone", None, ZOOM, T0, T0 + 10_000_000),
        ("HKCU", "webcam", WA_FAMILY, "", 0, 0),  # never used: not listed
    )
    p = probe(reg, [ProcInfo(1200, 500, "Zoom.exe", ZOOM)])
    rows = win.describe(p)
    assert rows[0] == {"probe_users": {1200: "Zoom.exe"}, "confidence": "high"}
    assert [(r["hive"], r["app"], r["in_use"]) for r in rows[1:]] == [
        ("HKCU", "Zoom.exe", True),
        ("HKLM", "Zoom.exe", False),
    ]
    assert rows[2]["stop_unix"] - rows[2]["start_unix"] == pytest.approx(1.0)
    assert "alice" not in repr(rows)


def test_filetime_conversion() -> None:
    assert win.filetime_to_unix(0) is None
    assert win.filetime_to_unix(116_444_736_000_000_000) == 0.0


def test_main_prints_json_lines(capsys: pytest.CaptureFixture[str], monkeypatch: Any) -> None:
    reg = registry(("HKCU", "microphone", None, ZOOM, T0, 0))
    monkeypatch.setattr(win, "WinRegistry", lambda: reg)
    assert win.main([]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out and all(line.startswith("{") for line in out)


# ---- the real Windows APIs (run on the Windows PC and the Windows CI runner) ---------------------


@pytest.mark.windows
def test_real_registry_roundtrip_under_hkcu() -> None:
    reg = win.WinRegistry()
    path = "Software\\etabli-captation-test"
    try:
        reg.set_string("HKCU", path, "DisplayName", "test")
        assert reg.values("HKCU", path)["DisplayName"] == "test"
        assert "etabli-captation-test" in reg.subkeys("HKCU", "Software")
    finally:
        assert reg.delete_key("HKCU", path) is True
    assert reg.delete_key("HKCU", path) is False


@pytest.mark.windows
def test_real_consent_store_and_processes() -> None:
    entries = read_consent_store(win.WinRegistry())
    assert all(e.hive in ("HKCU", "HKLM") for e in entries)
    procs = win.psutil_processes()
    me = next(p for p in procs if p.pid == os.getpid())
    assert me.name.lower().endswith(".exe") and me.exe and "\\" in me.exe
    snap = WindowsProbe().snapshot()
    assert os.getpid() not in {r.pid for r in snap.mic_users}
