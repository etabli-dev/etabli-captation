"""macOS probe client (MacProbe) against the helper's sample output and a fake helper process.

Pure logic: runs on every OS. Only ``test_real_helper_once`` needs macOS and a built helper.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from captation.models import AppRef, ProbeSnapshot
from captation.platform import macos
from captation.platform.macos import (
    EXIT_USAGE,
    MacProbe,
    app_ref_from,
    find_helper,
    helper_parent_bundle,
    helper_parent_name,
    parse_probe_line,
)
from captation.rules import classify
from captation.rulesfile import load_rules
from tests.unit.test_obs_controller import cfg_with

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "probes" / "macos" / "CaptationProbe"
SAMPLE = PACKAGE / "Samples" / "watch-sample.jsonl"


def sample_lines() -> list[str]:
    return SAMPLE.read_text(encoding="utf-8").splitlines()


def wait_for(pred: Callable[[], bool], timeout: float = 3.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class Feed:
    """A blocking line stream (the helper's stdout pipe)."""

    _END = object()

    def __init__(self) -> None:
        self._q: queue.Queue[object] = queue.Queue()

    def push(self, line: str) -> None:
        self._q.put(line if line.endswith("\n") else line + "\n")

    def close(self) -> None:
        self._q.put(self._END)

    def __iter__(self) -> Iterator[str]:
        while True:
            item = self._q.get()
            if item is self._END:
                return
            assert isinstance(item, str)
            yield item


class FakeHelper:
    def __init__(self, pid: int) -> None:
        self.stdout = Feed()
        self.pid = pid
        self.returncode: int | None = None
        self.terminated = False

    def poll(self) -> int | None:
        return self.returncode

    def exit(self, code: int) -> None:
        self.returncode = code
        self.stdout.close()

    def terminate(self) -> None:
        self.terminated = True
        if self.returncode is None:
            self.exit(-15)

    def kill(self) -> None:
        self.terminate()

    def wait(self, timeout: float | None = None) -> int:
        assert self.returncode is not None
        return self.returncode


class Spawner:
    def __init__(self) -> None:
        self.argvs: list[list[str]] = []
        self.helpers: list[FakeHelper] = []

    def __call__(self, argv: list[str]) -> FakeHelper:
        self.argvs.append(argv)
        helper = FakeHelper(90000 + len(self.helpers))
        self.helpers.append(helper)
        return helper

    @property
    def current(self) -> FakeHelper:
        return self.helpers[-1]


def make_probe(tmp_path: Path, **kw: object) -> tuple[MacProbe, Spawner, Clock]:
    helper = tmp_path / "captation-probe"
    helper.write_text("#!/bin/sh\n", encoding="utf-8")
    helper.chmod(0o755)
    spawner = Spawner()
    clock = Clock()
    probe = MacProbe(
        helper=helper,
        interval_ms=500,
        spawn=spawner,  # type: ignore[arg-type]
        clock=clock,
        first_line_timeout_s=2.0,
        **kw,  # type: ignore[arg-type]
    )
    return probe, spawner, clock


# ---- parsing and normalisation -------------------------------------------------------------


@pytest.mark.req("R-07")
def test_helper_name_and_bundle_normalisation() -> None:
    assert helper_parent_bundle("com.google.Chrome.helper") == "com.google.Chrome"
    assert helper_parent_bundle("com.google.Chrome.helper.Renderer") == "com.google.Chrome"
    assert helper_parent_bundle("com.microsoft.edgemac.HELPER.GPU") == "com.microsoft.edgemac"
    assert helper_parent_bundle("com.example.helperapp") is None
    assert helper_parent_bundle("us.zoom.xos") is None and helper_parent_bundle(None) is None
    assert helper_parent_name("Google Chrome Helper (Renderer)") == "Google Chrome"
    assert helper_parent_name("Signal Helper") == "Signal"
    assert helper_parent_name("zoom.us") is None and helper_parent_name("Helper Tool") is None


@pytest.mark.req("R-07")
def test_sample_lines_parse_and_normalise() -> None:
    lines = sample_lines()
    assert len(lines) >= 6
    idle, zoom, chrome, obs_signal, low, no_titles = (parse_probe_line(x) for x in lines[:6])

    assert idle is not None and idle.mic_users == () and idle.confidence == "high"
    assert idle.camera_active is False and idle.window_titles is None

    assert zoom is not None and zoom.camera_active and zoom.confidence == "high"
    assert zoom.mic_users == (
        AppRef(
            pid=2101,
            process_name="zoom.us",
            exe_path="/Applications/zoom.us.app/Contents/MacOS/zoom.us",
            bundle_id="us.zoom.xos",
        ),
    )

    assert chrome is not None
    [helper] = chrome.mic_users
    assert helper.pid == 3044 and helper.process_name == "Google Chrome Helper"
    assert helper.bundle_id == "com.google.Chrome" and helper.parent_app == "Google Chrome"
    assert chrome.window_titles is not None
    # the helper holds the mic, the browser owns the window: titles are shared
    assert chrome.window_titles[3044] == chrome.window_titles[3001]
    assert chrome.titles_status == "ok"

    assert obs_signal is not None
    obs, signal = obs_signal.mic_users
    assert obs.bundle_id == "com.obsproject.obs-studio" and obs.process_name == "OBS"
    assert signal.bundle_id == "org.whispersystems.signal-desktop"
    assert signal.parent_app == "Signal" and signal.process_name == "Signal Helper (Renderer)"

    assert low is not None and low.confidence == "low" and low.api == "device_running"
    assert [u.bundle_id for u in low.mic_users] == ["net.whatsapp.WhatsApp"]
    assert low.mic_device_running is True

    assert no_titles is not None and no_titles.titles_status == "permission_missing"


def test_sample_classifies_with_default_rules(tmp_path: Path) -> None:
    """Zoom → meeting, Meet in Chrome (title) → meeting, OBS ignored + Signal → messenger."""
    rules = load_rules()
    detection = cfg_with(tmp_path).detection
    contexts = []
    for line in sample_lines()[1:4]:
        reading = parse_probe_line(line)
        assert reading is not None
        snap = ProbeSnapshot(
            ts_monotonic=0.0,
            mic_users=reading.mic_users,
            camera_active=reading.camera_active,
            window_titles=reading.window_titles,
            confidence=reading.confidence,
        )
        contexts.append(classify(snap, rules, detection, None))
    zoom, meet, signal = contexts
    assert zoom is not None and zoom.app_id == "zoom" and zoom.mode == "video"
    assert zoom.capture_target.bundle_id == "us.zoom.xos"
    assert meet is not None and meet.app_id == "google_meet" and meet.category == "meeting"
    assert meet.capture_target.bundle_id == "com.google.Chrome"  # OBS targets the browser
    assert signal is not None and signal.app_id == "signal" and signal.mode == "audio"


@pytest.mark.parametrize(
    "line",
    [
        "",
        "not json",
        "[]",
        json.dumps({"schema": 2, "mic_users": []}),
        json.dumps({"schema": 1}),
        json.dumps({"schema": 1, "mic_users": "x"}),
    ],
)
def test_malformed_lines_are_rejected(line: str) -> None:
    assert parse_probe_line(line) is None


def test_bad_user_entries_are_skipped() -> None:
    doc = {
        "schema": 1,
        "api": "process_objects",
        "confidence": "high",
        "camera_active": "yes",
        "mic_users": [{"pid": -1}, {"pid": "x"}, "junk", {"pid": True}, {"pid": 7, "name": "a"}],
    }
    reading = parse_probe_line(json.dumps(doc))
    assert reading is not None and reading.camera_active is False
    assert [u.pid for u in reading.mic_users] == [7]
    assert app_ref_from({"pid": 8, "path": "/x/y/Thing"}) == AppRef(
        pid=8, process_name="Thing", exe_path="/x/y/Thing"
    )
    # a non-helper without a bundle ID takes the parent bundle the probe resolved
    ref = app_ref_from({"pid": 9, "name": "tool", "parent_bundle_id": "com.example.App"})
    assert ref is not None and ref.bundle_id == "com.example.App"


# ---- MacProbe with a fake helper -------------------------------------------------------------


def test_watch_mode_streams_latest_and_excludes(tmp_path: Path) -> None:
    probe, spawner, _ = make_probe(tmp_path, exclude_pids=frozenset({4001}))
    lines = sample_lines()
    try:
        threading.Timer(0.05, lambda: spawner.current.stdout.push(lines[3])).start()
        snap = probe.snapshot()  # waits for the first line
        argv = spawner.argvs[0]
        assert argv[1:] == ["--watch", "--interval-ms", "500", "--titles"]
        assert [u.pid for u in snap.mic_users] == [4120]  # OBS (4001) excluded by PID
        assert snap.confidence == "high" and snap.camera_users == ()
        spawner.current.stdout.push(lines[1])
        assert wait_for(lambda: [u.pid for u in probe.snapshot().mic_users] == [2101])
        assert probe.snapshot().camera_active is True
        probe.set_exclude_pids(frozenset({2101}))
        assert probe.snapshot().mic_users == ()
        assert len(spawner.helpers) == 1
    finally:
        probe.close()
    assert spawner.current.terminated


def test_helper_pid_and_own_pid_are_excluded(tmp_path: Path) -> None:
    probe, spawner, _ = make_probe(tmp_path)
    own = {"pid": os.getpid(), "name": "python"}
    me = {"pid": 90000, "name": "captation-probe"}  # the first fake helper's PID
    doc = {"schema": 1, "api": "process_objects", "confidence": "high", "mic_users": [own, me]}
    try:
        threading.Timer(0.05, lambda: spawner.current.stdout.push(json.dumps(doc))).start()
        snap = probe.snapshot()
        assert spawner.current.pid == 90000 and snap.confidence == "high"
        assert snap.mic_users == ()
    finally:
        probe.close()


def test_crash_restarts_with_low_confidence_and_budget(tmp_path: Path) -> None:
    probe, spawner, clock = make_probe(tmp_path, max_restarts=2, restart_window_s=600)
    zoom = sample_lines()[1]
    try:
        threading.Timer(0.05, lambda: spawner.current.stdout.push(zoom)).start()
        assert probe.snapshot().confidence == "high"
        spawner.current.exit(1)  # crash
        snap = probe.snapshot()
        # last users kept for continuity, but marked low
        assert snap.confidence == "low" and [u.pid for u in snap.mic_users] == [2101]
        assert len(spawner.helpers) == 2 and probe.mode == "watch"
        spawner.current.exit(1)
        probe.snapshot()
        assert len(spawner.helpers) == 3
        spawner.current.exit(1)
        snap = probe.snapshot()  # budget (2 per window) used up
        assert len(spawner.helpers) == 3 and probe.mode == "failed"
        assert probe.snapshot().mic_users == () and probe.snapshot().confidence == "low"
        clock.t += 601  # the window frees a restart
        threading.Timer(0.05, lambda: spawner.current.stdout.push(zoom)).start()
        snap = probe.snapshot()
        assert len(spawner.helpers) == 4 and probe.mode == "watch"
        assert snap.confidence == "high"
    finally:
        probe.close()


def test_hung_helper_is_restarted(tmp_path: Path) -> None:
    probe, spawner, clock = make_probe(tmp_path)
    try:
        threading.Timer(0.05, lambda: spawner.current.stdout.push(sample_lines()[1])).start()
        assert probe.snapshot().confidence == "high"
        first = spawner.current
        clock.t += 10  # no line for longer than 3 intervals + 2 s
        snap = probe.snapshot()
        assert snap.confidence == "low" and first.terminated and len(spawner.helpers) == 2
    finally:
        probe.close()


def test_usage_exit_falls_back_to_once(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def run_once(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, sample_lines()[1] + "\n", "")

    probe, spawner, _ = make_probe(tmp_path, run_once=run_once)
    try:
        threading.Timer(0.05, lambda: spawner.current.exit(EXIT_USAGE)).start()
        snap = probe.snapshot()  # wakes at the end of output, sees exit 64
        assert probe.mode == "once" and len(spawner.helpers) == 1
        assert [u.pid for u in snap.mic_users] == [2101]
        assert calls[-1][1:] == ["--once", "--titles"]
        assert [u.pid for u in probe.snapshot().mic_users] == [2101]
    finally:
        probe.close()


def test_once_failure_reports_low(tmp_path: Path) -> None:
    def run_once(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 2, "", "")

    probe, spawner, _ = make_probe(tmp_path, run_once=run_once)
    probe._mode = "once"
    snap = probe.snapshot()
    assert snap.mic_users == () and snap.confidence == "low" and spawner.helpers == []


def test_missing_helper_reports_low(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(macos, "find_helper", lambda: None)
    probe = MacProbe(spawn=lambda argv: pytest.fail("must not spawn"))  # type: ignore[arg-type,return-value]
    snap = probe.snapshot()
    assert snap.mic_users == () and snap.confidence == "low" and probe.mode == "failed"
    probe.close()


def test_low_confidence_users_stick_while_obs_captures(tmp_path: Path) -> None:
    capturing = {"on": False}
    probe, spawner, _ = make_probe(tmp_path, obs_capturing=lambda: capturing["on"])
    low = json.loads(sample_lines()[4])
    try:
        threading.Timer(0.05, lambda: spawner.current.stdout.push(json.dumps(low))).start()
        assert [u.pid for u in probe.snapshot().mic_users] == [5005]
        capturing["on"] = True  # OBS now holds the device too; the user switches apps
        other = dict(low, mic_users=[{"pid": 6006, "name": "Finder"}], ts=low["ts"] + 1)
        spawner.current.stdout.push(json.dumps(other))
        time.sleep(0.1)
        assert [u.pid for u in probe.snapshot().mic_users] == [5005]
        capturing["on"] = False
        assert wait_for(lambda: [u.pid for u in probe.snapshot().mic_users] == [6006])
    finally:
        probe.close()


def test_find_helper_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    exe = tmp_path / "App.app" / "Contents" / "MacOS" / "captation"
    exe.parent.mkdir(parents=True)
    probe = exe.parent / "captation-probe"
    probe.write_text("#!/bin/sh\n", encoding="utf-8")
    probe.chmod(0o755)
    monkeypatch.delenv("CAPTATION_PROBE", raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    assert find_helper() == probe
    override = tmp_path / "custom-probe"
    override.write_text("#!/bin/sh\n", encoding="utf-8")
    override.chmod(0o755)
    monkeypatch.setenv("CAPTATION_PROBE", str(override))
    assert find_helper() == override


@pytest.mark.macos
def test_real_helper_once() -> None:
    """On the Mac: the built helper prints one valid object ."""
    helper = PACKAGE / ".build" / "release" / "captation-probe"
    if not helper.exists():
        pytest.skip("build the helper first: swift build -c release")
    out = subprocess.run([str(helper), "--once"], capture_output=True, text=True, timeout=10)
    assert out.returncode == 0, out.stderr
    reading = parse_probe_line(out.stdout)
    assert reading is not None and reading.confidence in ("high", "low")
