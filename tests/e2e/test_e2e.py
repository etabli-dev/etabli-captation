"""End-to-end scenarios E1–E13 on Linux. Marker ``e2e``; needs a bootstrapped OBS,
PipeWire, dbus-daemon and (for E3) v4l2loopback /dev/video42. See tests/e2e/harness.py."""

from __future__ import annotations

import json
import os
import subprocess
import time
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import psutil
import pytest

from tests.dbus_stub import StubDesktop
from tests.e2e.harness import (
    CAMERA,
    Rig,
    Secret,
    documented_obs_peer,
    ensure_camera,
    is_loopback,
    obs_password,
    restore_obs_after_crash,
    rig,
    tcp_peers,
    wait_for,
)
from tests.integration.conftest import dominant_frequency

pytestmark = [pytest.mark.e2e, pytest.mark.linux, pytest.mark.timeout(900)]
METRICS: dict[str, Any] = {}


@pytest.fixture(scope="module")
def desk() -> Iterator[StubDesktop]:
    d = StubDesktop()
    d.start()
    yield d
    d.stop()


@pytest.fixture(scope="module")
def password() -> Secret:
    return Secret(obs_password())


def ts(value: str) -> float:
    return datetime.fromisoformat(value).timestamp()


def obs_pid() -> int | None:
    out = subprocess.run(["pgrep", "-x", "obs"], capture_output=True, text=True).stdout.split()
    return int(out[0]) if out else None


def final(r: Rig, n: int = 1, timeout: float = 60) -> list[dict[str, Any]]:
    assert wait_for(lambda: len(r.final_sessions()) >= n, timeout), r.sessions()
    return r.final_sessions()


def files_of(session: dict[str, Any], index: int = 0) -> dict[str, str]:
    return {f["role"]: f["path"] for f in session["segments"][index]["files"]}


@pytest.mark.req("R-03", "R-10", "R-11", "R-12", "R-26", "R-40", "R-42", "R-43", "R-44")
def test_e1_messenger_call_and_e10_network(
    desk: StubDesktop, password: Secret, virtual_audio: None
) -> None:
    with rig(desk, password) as r:
        assert r.arm()["ok"]
        agent = psutil.Process(r.proc.pid)  # type: ignore[union-attr]
        time.sleep(3)
        idle_cpu = agent.cpu_percent(interval=5)
        t0 = time.time()
        call = r.call(seconds=20)
        assert wait_for(lambda: r.sessions() and r.sessions()[0]["segments"], 20)
        started = ts(r.sessions()[0]["segments"][0]["started_at"])
        rec_cpu = agent.cpu_percent(interval=5)
        rss_mb = agent.memory_info().rss / 2**20
        opid = obs_pid()
        from captation.platform.linux import LinuxObsLocator, obs_host_pids

        obs_pids = obs_host_pids(LinuxObsLocator().find())  # type: ignore[arg-type]
        own = [agent, *(c for c in agent.children(recursive=True) if c.pid not in obs_pids)]
        peers_agent = [p for c in own for p in tcp_peers(c.pid)]
        peers_obs = tcp_peers(opid) if opid else []
        obs_cpu = psutil.Process(opid).cpu_percent(interval=3) if opid else None
        time.sleep(8)
        r.hang_up(call)
        t_hang = time.time()
        session = final(r)[0]
        t_final = max(Path(r.session_dirs()[0] / "session.json").stat().st_mtime, t_hang)
    start_latency = started - t0
    METRICS.update(
        e1_start_latency_s=round(start_latency, 2),
        e1_stop_latency_s=round(t_final - t_hang, 2),
        agent_cpu_idle_armed_pct=idle_cpu,
        agent_cpu_recording_pct=rec_cpu,
        agent_rss_mb=round(rss_mb, 1),
        obs_cpu_recording_audio_pct=obs_cpu,
    )
    print("E1/E10 metrics:", json.dumps(METRICS))
    print(
        "E10 agent sockets:",
        peers_agent,
        "OBS non-loopback sockets:",
        [p for p in peers_obs if not is_loopback(p[2])],
    )
    assert session["status"] == "complete" and len(session["segments"]) == 1
    seg = session["segments"][0]
    assert seg["mode"] == "audio" and session["app"]["id"] == "fakecall_e2e"
    assert start_latency <= 1 + 3 + 2 + 1.5  # debounce + countdown + 2 s (+ poll slack)
    assert t_final - t_hang <= 3 + 5 + 15  # stop debounce + 5 s (+ post-processing)
    roles = files_of(session)
    assert {"container", "audio", "mix", "remote", "local"} <= set(roles)
    folder = r.data / "unassigned" / session["session_id"]
    assert not folder.exists()  # the rig cleaned up; frequency checked below via kept copy
    # E10: the agent talks only to loopback; OBS has no live non-loopback connection while
    # recording (CLOSE-WAIT leftovers of its documented start-up fetch may linger, PF-023).
    assert peers_agent and all(
        is_loopback(peer) and peer.endswith(":4455") for _, _, peer in peers_agent
    )
    undocumented = [
        (state, peer)
        for state, _, peer in peers_obs
        if not is_loopback(peer) and not documented_obs_peer(peer)
    ]
    assert undocumented == [], undocumented


@pytest.mark.req("R-12", "R-40")
def test_e1_remote_track_carries_the_call_tone(
    desk: StubDesktop, password: Secret, virtual_audio: None
) -> None:
    with rig(desk, password) as r:
        assert r.arm()["ok"]
        call = r.call(seconds=14)
        assert wait_for(lambda: r.sessions() and r.sessions()[0]["segments"], 20)
        time.sleep(6)
        r.hang_up(call)
        session = final(r)[0]
        folder = r.data / "unassigned" / session["session_id"]
        roles = files_of(session)
        remote = dominant_frequency(folder / roles["remote"], "0:a:0")
        local = dominant_frequency(folder / roles["local"], "0:a:0")
    print(f"E1 FFT: remote {remote:.0f} Hz, local {local:.0f} Hz")
    assert abs(remote - 1000) < 15 and abs(local - 1320) < 15


@pytest.mark.req("R-01", "R-28", "R-41")
def test_e2_meeting_records_video(desk: StubDesktop, password: Secret, virtual_audio: None) -> None:
    with rig(desk, password) as r:
        assert r.arm()["ok"]
        call = r.call("fakemeet-e2e", seconds=15)
        assert wait_for(lambda: r.sessions() and r.sessions()[0]["segments"], 20)
        opid = obs_pid()
        METRICS["obs_cpu_recording_video_pct"] = (
            psutil.Process(opid).cpu_percent(interval=3) if opid else None
        )
        time.sleep(5)
        r.hang_up(call)
        session = final(r)[0]
    seg = session["segments"][0]
    print("E2:", seg["mode"], sorted(files_of(session)), session["warnings"])
    assert seg["mode"] == "video" and seg["trigger"] == "rule:meeting_app"
    roles = files_of(session)
    assert roles["video"].endswith(".mp4") and {"mix", "remote", "local"} <= set(roles)


@pytest.mark.req("R-02", "R-08")
def test_e3_camera_upgrade(desk: StubDesktop, password: Secret, virtual_audio: None) -> None:
    if not ensure_camera():
        pytest.skip(f"needs v4l2loopback at {CAMERA}")
    with rig(desk, password) as r:
        assert r.arm()["ok"]
        call = r.call(seconds=40)
        assert wait_for(lambda: r.sessions() and r.sessions()[0]["segments"], 20)
        time.sleep(10)
        producer = r.spawn(
            [
                "ffmpeg",
                "-loglevel",
                "error",
                "-re",
                "-f",
                "lavfi",
                "-i",
                "testsrc=size=640x360:rate=15",
                "-pix_fmt",
                "yuyv422",
                "-f",
                "v4l2",
                CAMERA,
            ]
        )
        time.sleep(1)
        consumer = r.spawn(
            ["ffmpeg", "-loglevel", "error", "-f", "v4l2", "-i", CAMERA, "-f", "null", "-"]
        )
        assert wait_for(lambda: len(r.sessions()[0]["segments"]) >= 2, 20)
        time.sleep(5)
        r.hang_up([*call, consumer, producer])
        session = final(r, timeout=90)[0]
    s1, s2 = session["segments"][:2]
    gap = ts(s2["started_at"]) - ts(s1["ended_at"])
    METRICS["e3_mode_switch_gap_s"] = round(gap, 2)
    print("E3:", [(s["mode"], s["trigger"]) for s in session["segments"]], f"gap {gap:.2f} s")
    assert (s1["mode"], s2["mode"]) == ("audio", "video") and s2["trigger"] == "upgrade:camera_on"
    assert gap <= 2.0


@pytest.mark.req("R-14")
def test_e4_disarmed_records_nothing(
    desk: StubDesktop, password: Secret, virtual_audio: None
) -> None:
    with rig(desk, password) as r:
        call = r.call(seconds=10)
        time.sleep(9)
        r.hang_up(call)
        assert r.status()["state"] == "DISARMED"
        assert r.session_dirs() == []


@pytest.mark.req("R-35", "R-36")
def test_e5_idle_armed_with_obs_running_records_nothing(
    desk: StubDesktop, password: Secret, virtual_audio: None
) -> None:
    with rig(desk, password) as r:
        assert r.arm()["ok"] and obs_pid() is not None
        time.sleep(60)
        assert r.status()["state"] == "ARMED_IDLE"
        assert r.session_dirs() == []


@pytest.mark.req("R-17", "R-37")
def test_e6_countdown_cancel_and_smoke(
    desk: StubDesktop, password: Secret, virtual_audio: None
) -> None:
    """Also the tray smoke test: cancel → second attempt records → red → disarm stops."""
    desk.notify.sent.clear()
    with rig(desk, password) as r:
        assert r.arm()["ok"] and r.tray_status() == "Active"
        call = r.call(seconds=40)
        nid = wait_for(r.countdown_notification, 10)
        assert nid, desk.notify.sent
        desk.invoke_action(nid, "cancel")
        cancelled = final(r)[0]
        assert cancelled["status"] == "cancelled" and cancelled["segments"] == []
        assert sorted(p.name for p in r.session_dirs()[0].iterdir()) == ["session.json"]
        r.hang_up(call)
        time.sleep(4)  # the call is gone for stop_debounce: a new call may start again
        call = r.call(seconds=30)
        assert wait_for(lambda: r.status().get("state") == "RECORDING", 20)
        assert r.tray_status() == "NeedsAttention"  # red recording indicator
        assert any("Einwilligung" in n["body"] for n in desk.notify.sent)  # start reminder
        out = r.cli("disarm")
        assert out.returncode == 0
        sessions = final(r, 2)
        r.hang_up(call)
    assert sorted(s["status"] for s in sessions) == ["cancelled", "complete"]


@pytest.mark.req("R-53")
def test_e7_obs_crash_mid_call(desk: StubDesktop, password: Secret, virtual_audio: None) -> None:
    """OBS crashes (SIGKILL) during a recording. OBS 33 then waits in its Safe Mode dialog
    (PF-022), so the agent cannot reconnect: segment 1 is interrupted and, after the 30 s
    recovery window, the session is finalised as interrupted and the user is told (ADR-013).
    The graceful case (OBS back → segment 2 with recovery:obs_restart) is covered by
    tests/unit/test_agent.py::test_obs_crash_and_recovery."""
    desk.notify.sent.clear()
    try:
        with rig(desk, password) as r:
            assert r.arm()["ok"]
            call = r.call(seconds=120)
            assert wait_for(lambda: r.sessions() and r.sessions()[0]["segments"], 20)
            time.sleep(5)
            pid = obs_pid()
            assert pid
            os.kill(pid, 9)
            session = final(r, timeout=120)[0]
            r.hang_up(call)
            assert r.status()["armed"] is True  # the agent stays armed for the next call
    finally:
        restore_obs_after_crash()
    print(
        "E7:",
        [(s["trigger"], s.get("interrupted")) for s in session["segments"]],
        session["status"],
    )
    assert session["status"] == "interrupted"
    assert session["segments"][0]["interrupted"] is True
    assert any("abgestürzt" in n["body"] for n in desk.notify.sent)


@pytest.mark.req("R-16")
def test_e8_auto_disarm_during_call(
    desk: StubDesktop, password: Secret, virtual_audio: None
) -> None:
    with rig(desk, password) as r:  # auto_disarm_after_minutes: 5 (schema minimum)
        assert r.arm()["ok"]
        armed_at = time.time()
        call = r.call(seconds=400)
        assert wait_for(lambda: r.status().get("state") == "RECORDING", 20)
        assert wait_for(lambda: r.status().get("state") == "DISARMED", 5 * 60 + 30, step=2)
        disarmed_after = time.time() - armed_at
        session = final(r, timeout=90)[0]
        r.hang_up(call)
    print(f"E8: disarmed after {disarmed_after:.0f} s, status {session['status']}")
    assert 295 <= disarmed_after <= 5 * 60 + 15 and session["status"] == "complete"


@pytest.mark.req("R-15")
def test_e9_restart_comes_back_disarmed(
    desk: StubDesktop, password: Secret, virtual_audio: None
) -> None:
    with rig(desk, password) as r:
        assert r.arm()["ok"] and r.status()["state"] == "ARMED_IDLE"
        r.stop()
        r.start()
        assert r.status()["state"] == "DISARMED"


@pytest.mark.req("R-39")
def test_e11_silent_local_track(desk: StubDesktop, password: Secret, virtual_audio: None) -> None:
    desk.notify.sent.clear()
    with rig(desk, password) as r:
        assert r.arm()["ok"]
        call = r.call(seconds=20, mic_feed=False)  # the virtual mic stays silent
        assert wait_for(lambda: r.sessions() and r.sessions()[0]["segments"], 20)
        time.sleep(12)
        r.hang_up(call)
        session = final(r)[0]
    print("E11 warnings:", session["warnings"])
    assert "qc:local_silent" in session["warnings"] and session["status"] == "complete"
    assert any("Qualitätsprüfung" in n["body"] for n in desk.notify.sent)


@pytest.mark.req("R-32")
def test_e12_exposure_policy(desk: StubDesktop, password: Secret, virtual_audio: None) -> None:
    from captation.obs import firewall

    with rig(desk, password) as r:
        try:
            assert r.cli("obs", "firewall", "--remove", "--yes").returncode in (0, 2)
            refused = r.arm()
            assert refused.get("ok") is False and "exposure_policy_unmet" in refused["refused"]
            assert r.cli("obs", "firewall", "--apply", "--yes").returncode == 0
            assert r.arm()["ok"] is True
            status = firewall.check(4455)
            print("E12:", status.details)
            assert status.ok and any("blocked" in d for d in status.details)
        finally:
            if not firewall.check(4455).ok:
                r.cli("obs", "firewall", "--apply", "--yes")


@pytest.mark.req("R-21")
def test_e13_no_tray_no_recording(password: Secret, virtual_audio: None) -> None:
    no_tray = StubDesktop(tray=False)
    no_tray.start()
    try:
        with rig(no_tray, password) as r:
            call = r.call(seconds=8)
            refused = r.arm()
            time.sleep(6)
            r.hang_up(call)
            assert refused.get("ok") is False and "no_tray" in refused["refused"]
            assert r.session_dirs() == []
    finally:
        no_tray.stop()


def test_zz_metrics_summary() -> None:
    print("E2E metrics:", json.dumps(METRICS, indent=1))
