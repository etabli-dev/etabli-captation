"""Fail-closed and recovery paths outside the session machine: Linux stream identity, the
watchdog, the agent's countdown/start/stop handling and the IPC limits."""

from __future__ import annotations

import io
import os
from pathlib import Path
from typing import Any

import pytest

from captation import watchdog
from captation.platform.linux import parse_pw_dump


def _node(nid: int, client: int, cls: str, **props: Any) -> dict[str, Any]:
    return {
        "id": nid,
        "type": "PipeWire:Interface:Node",
        "info": {"state": "running", "props": {"media.class": cls, "client.id": client, **props}},
    }


def _client(cid: int, **props: Any) -> dict[str, Any]:
    return {"id": cid, "type": "PipeWire:Interface:Client", "info": {"props": props}}


@pytest.mark.req("R-60")
def test_flatpak_app_id_becomes_the_package_and_firefox_reports_the_tab_title(
    tmp_path: Path,
) -> None:
    doc = [
        _client(
            10,
            **{
                "application.process.id": 4001,
                "pipewire.access.portal.app_id": "com.rtosta.zapzap",
            },
        ),
        _node(11, 10, "Stream/Input/Audio", **{"application.process.binary": "python3.13"}),
        _client(20, **{"application.process.id": 5001}),
        _node(
            21,
            20,
            "Stream/Input/Audio",
            **{"application.process.binary": "firefox", "media.name": "Seminar | Jitsi Meet"},
        ),
        _client(30, **{"application.process.id": 6001}),
        _node(
            31,
            30,
            "Stream/Input/Audio",
            **{"application.process.binary": "chrome", "media.name": "RecordStream"},
        ),
    ]
    titles: dict[int, list[str]] = {}
    mic, _ = parse_pw_dump(doc, tmp_path, titles)
    by_pid = {r.pid: r for r in mic}
    assert by_pid[4001].package_id == "com.rtosta.zapzap"
    assert by_pid[5001].package_id is None
    assert titles == {5001: ["Seminar | Jitsi Meet"]}  # Chromium's fixed stream name is no title


@pytest.mark.req("R-61")
def test_watchdog_stops_the_recording_only_when_the_agent_vanished() -> None:
    calls: list[str] = []
    assert watchdog.watch(io.StringIO("bye\n"), lambda: calls.append("stop")) is False
    assert calls == []
    assert watchdog.watch(io.StringIO(""), lambda: calls.append("stop")) is True
    assert calls == ["stop"]

    def broken() -> None:
        raise OSError("OBS unreachable")

    assert watchdog.watch(io.StringIO(""), broken) is True  # logged, never raises


def test_watchdog_command_runs_this_installation() -> None:
    argv = watchdog.command()
    assert argv[-1] == "watchdog" and argv[0]


@pytest.mark.req("R-61")
@pytest.mark.skipif(os.name != "posix", reason="pipe semantics checked on POSIX")
def test_watchdog_handle_says_goodbye(tmp_path: Path) -> None:
    import subprocess
    import sys

    marker = tmp_path / "said"
    script = (
        "import sys, pathlib; from captation import watchdog; "
        f"pathlib.Path({str(marker)!r}).write_text("
        "'clean' if watchdog.wait_for_parent(sys.stdin) else 'vanished')"
    )
    proc = subprocess.Popen([sys.executable, "-c", script], stdin=subprocess.PIPE, text=True)
    watchdog.Handle(proc).goodbye()
    assert marker.read_text() == "clean"


# ---- agent: countdown that cannot be shown, start refused, OBS stopping on its own --------


@pytest.mark.req("R-17")
def test_countdown_that_cannot_be_shown_never_records(tmp_path: Path) -> None:
    from captation.session import State
    from tests.unit.test_agent import CALL, Rig, arm

    rig = Rig(tmp_path)
    try:
        assert arm(rig)["ok"]

        def broken(*_a: Any, **_k: Any) -> int:
            raise RuntimeError("notification service gone")

        rig.ui.countdown = broken  # type: ignore[method-assign]
        rig.probe.mic = (CALL,)
        rig.tick(n=12)
        assert "StartRecord" not in rig.fake_obs.names()
        assert rig.state is State.DISARMED
        assert [s["status"] for s in rig.sessions()] == ["cancelled"]
    finally:
        rig.close()


@pytest.mark.req("R-53")
def test_refused_start_finalises_once_instead_of_retrying(tmp_path: Path) -> None:
    from captation.obs.client import ObsRequestError
    from captation.session import State
    from tests.unit.test_agent import CALL, Rig, arm

    rig = Rig(tmp_path)
    try:
        assert arm(rig)["ok"]

        def refuse(*_a: Any, **_k: Any) -> None:
            raise ObsRequestError("StartRecord", 500, "output already active")

        rig.obs.start_segment = refuse  # type: ignore[method-assign]
        rig.probe.mic = (CALL,)
        rig.tick(n=12)
        rig.tick(n=12)
        assert [s["status"] for s in rig.sessions()] == ["failed"]
        assert rig.state in (State.ARMED_IDLE, State.PENDING_START)
    finally:
        rig.close()


@pytest.mark.req("R-53")
def test_obs_stopping_on_its_own_starts_a_new_segment(tmp_path: Path) -> None:
    from captation.session import State
    from tests.unit.test_agent import CALL, Rig, arm

    rig = Rig(tmp_path)
    try:
        assert arm(rig)["ok"]
        rig.probe.mic = (CALL,)
        rig.tick(n=10)
        assert rig.state is State.RECORDING
        rig.agent.obs_record_stopped()
        import time

        deadline = time.monotonic() + 5  # recovery runs on its own thread
        while time.monotonic() < deadline and rig.agent.machine.status()["segment"] != 2:
            rig.tick()
            time.sleep(0.02)
        assert rig.state is State.RECORDING
        assert rig.agent.machine.status()["segment"] == 2
    finally:
        rig.close()


def test_tray_arming_cancelled_means_disarmed(tmp_path: Path) -> None:
    from captation.session import State
    from tests.unit.test_agent import Rig

    rig = Rig(tmp_path)
    try:
        rig.ui.ask_consent_ref = lambda: None  # type: ignore[method-assign]
        rig.agent.arm_from_tray()
        rig.flush()
        assert rig.state is State.DISARMED
    finally:
        rig.close()
