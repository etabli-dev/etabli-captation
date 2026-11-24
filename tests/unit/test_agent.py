"""Agent wiring (stages 08/09): consent guard, countdown cancel, recording flow, recovery."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from captation.agent import Agent, AgentDeps, _Query
from captation.config import Config
from captation.models import AppRef, ProbeSnapshot
from captation.obs.client import ObsError
from captation.obs.controller import ObsController
from captation.rulesfile import load_rules
from captation.session import Disarm, State
from captation.ui.base import MenuItem, TrayState
from captation.ui.i18n import Strings
from captation.ui.notify import ConsentUi, UiHandlers
from tests.fakes import FakeObs
from tests.unit.session_support import FakeClock, FakeIds
from tests.unit.test_obs_controller import cfg_with

CALL = AppRef(pid=4242, process_name="signal-desktop")


class FakeBackend:
    def __init__(self, *, tray: bool = True, caps: set[str] | None = None) -> None:
        self.tray = tray
        self.caps = caps if caps is not None else {"actions", "body"}
        self.states: list[tuple[TrayState, str]] = []
        self.notes: list[dict[str, Any]] = []
        self.closed: list[int] = []
        self.menu: list[MenuItem] = []
        self.blink = False
        self.sounds = 0
        self.ask_answer: str | None = "P-017"
        self.menu_opens = 0
        self.can_open_menu = True

    def start(self, on_tray_change: Callable[[bool], None] | None = None) -> None:
        self.on_tray_change = on_tray_change

    def tray_available(self) -> bool:
        return self.tray

    def notification_capabilities(self) -> set[str]:
        return set(self.caps)

    def set_state(self, state: TrayState, tooltip_title: str, tooltip_body: str) -> None:
        self.states.append((state, tooltip_body))

    def set_menu(self, items: list[MenuItem]) -> None:
        self.menu = items

    def set_blink(self, on: bool) -> None:
        self.blink = on

    def notify(self, summary: str, body: str, **kw: Any) -> int | None:
        self.notes.append({"summary": summary, "body": body, **kw})
        return len(self.notes)

    def close_notification(self, nid: int) -> None:
        self.closed.append(nid)

    def play_sound(self) -> None:
        self.sounds += 1

    def open_menu(self) -> bool:
        if self.can_open_menu:
            self.menu_opens += 1
        return self.can_open_menu

    def ask_text(self, title: str, prompt: str) -> str | None:
        return self.ask_answer

    def stop(self) -> None:
        return None

    def press(self, label: str) -> None:
        item = next(i for i in self.menu if i.label == label)
        assert item.enabled and item.callback is not None
        item.callback()


class ScriptedProbe:
    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.mic: tuple[AppRef, ...] = ()
        self.camera = False
        self.calls = 0
        self.closes = 0

    def snapshot(self) -> ProbeSnapshot:
        self.calls += 1
        return ProbeSnapshot(
            ts_monotonic=self.clock.now(), mic_users=self.mic, camera_active=self.camera
        )

    def close(self) -> None:
        self.closes += 1


def classic_cfg(tmp_path: Path, *, live: bool = False) -> Config:
    """The rig's default: recording starts after the countdown (the tests below describe that
    sequence); ``live=True`` is the product default of ADR-022 (record during the countdown)."""
    raw = cfg_with(tmp_path).model_dump(by_alias=True)
    raw["consent"]["record_during_countdown"] = live
    return Config.model_validate(raw)


class Rig:
    def __init__(self, tmp_path: Path, *, cfg: Config | None = None, **backend_kw: Any) -> None:
        self.cfg = cfg or classic_cfg(tmp_path)
        self.clock = FakeClock()
        self.fake_obs = FakeObs()
        self.lost: list[bool] = []
        self.obs = ObsController(
            self.cfg, lambda: self.fake_obs, os_name="linux", on_lost=lambda: self.agent.obs_lost()
        )
        self.backend = FakeBackend(**backend_kw)
        self.probe = ScriptedProbe(self.clock)
        self.exposure = True
        self.encryption = "encrypted"
        handlers = UiHandlers(
            arm=lambda: self.agent.arm_from_tray(),
            disarm=lambda: self.agent.post(Disarm()),
            stop=lambda: None,
            cancel=lambda: self.agent.post(
                __import__("captation.session", fromlist=["Cancel"]).Cancel()
            ),
        )
        self.ui = ConsentUi(self.backend, Strings("de"), handlers)
        self.agent = Agent(
            AgentDeps(
                cfg=self.cfg,
                rules=load_rules(),
                probe=self.probe,
                obs=self.obs,
                ui=self.ui,
                exposure_ok=lambda: self.exposure,
                encryption=lambda: self.encryption,  # type: ignore[arg-type,return-value]
                clock=self.clock,
                ids=FakeIds(),
            )
        )
        self.thread = threading.Thread(target=self.agent._loop, daemon=True)
        self.thread.start()
        self.agent.d.ui.on_tray_change = self.agent.tray_changed
        self.ui.start()

    def flush(self) -> None:
        for _ in range(6):
            self.agent.submit(_Query(lambda: None))
            self.agent.wait_for_workers(timeout_s=60)  # post-processing runs off the loop

    def tick(self, seconds: float = 1.0, n: int = 1) -> None:
        for _ in range(n):
            self.clock.t += seconds
            self.agent.poll_once()
            self.flush()

    @property
    def state(self) -> State:
        return self.agent.machine.state

    def sessions(self) -> list[dict[str, Any]]:
        root = self.cfg.data_root_path / "unassigned"
        return [
            json.loads((d / "session.json").read_text())
            for d in sorted(root.glob("S-*"))
            if (d / "session.json").exists()
        ]

    def close(self) -> None:
        from captation.agent import _Stop

        self.agent.submit(_Stop())


@pytest.fixture
def rig(tmp_path: Path) -> Iterator[Rig]:
    r = Rig(tmp_path)
    yield r
    r.close()


def arm(rig: Rig, ref: str | None = "P-017") -> dict[str, Any]:
    result = rig.agent.arm(ref)
    rig.flush()
    return result


@pytest.mark.req("R-14", "R-15")
def test_starts_disarmed_and_reads_nothing(rig: Rig) -> None:
    assert rig.state is State.DISARMED
    rig.probe.mic = (CALL,)
    rig.tick(n=5)
    assert rig.probe.calls == 0 and rig.sessions() == []
    assert rig.backend.states[-1][0] == "disarmed"
    restarted = Rig(rig.cfg.data_root_path.parent.parent / "second")
    try:
        assert restarted.state is State.DISARMED  # a fresh agent never inherits arming
    finally:
        restarted.close()


@pytest.mark.req("R-21", "R-32", "R-20", "R-46")
@pytest.mark.parametrize(
    ("setup", "ref", "reason"),
    [
        (lambda r: setattr(r.backend, "tray", False), "P-017", "no_tray"),
        (lambda r: setattr(r, "exposure", False), "P-017", "exposure_policy_unmet"),
        (lambda r: None, "bad ref!", "consent_ref_invalid"),
        (lambda r: None, None, "consent_ref_required"),
    ],
)
def test_arming_is_refused(
    tmp_path: Path, setup: Callable[[Rig], None], ref: str | None, reason: str
) -> None:
    cfg = cfg_with(tmp_path)
    raw = cfg.model_dump(by_alias=True)
    raw["consent"]["consent_ref_pattern"] = "^P-[0-9]{3}$"
    r = Rig(tmp_path, cfg=Config.model_validate(raw))
    try:
        setup(r)
        result = arm(r, ref)
        assert result["ok"] is False and reason in result["refused"]
        assert r.state is State.DISARMED
        assert any("nicht aktiviert" in n["summary"] for n in r.backend.notes)
    finally:
        r.close()


def test_encryption_block_refuses(tmp_path: Path) -> None:
    cfg = cfg_with(tmp_path)
    raw = cfg.model_dump(by_alias=True)
    raw["storage"]["require_disk_encryption"] = "block"
    r = Rig(tmp_path, cfg=Config.model_validate(raw))
    try:
        r.encryption = "unencrypted"
        assert "disk_encryption_required" in arm(r)["refused"]
    finally:
        r.close()


@pytest.mark.terms_gate
@pytest.mark.req("R-59")
def test_arming_refused_until_disclaimer_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from captation import terms

    state = {"accepted": False}
    monkeypatch.setattr(terms, "accepted", lambda path=None: state["accepted"])
    r = Rig(tmp_path)
    try:
        assert arm(r)["refused"] == ["terms_not_accepted"]
        assert r.agent.machine.state.name == "DISARMED"
        state["accepted"] = True
        assert arm(r)["ok"]
    finally:
        r.close()


def _start_call(rig: Rig) -> None:
    rig.probe.mic = (CALL,)
    rig.tick(n=4)  # start debounce (3 s)


@pytest.mark.req("R-03", "R-10", "R-17", "R-18", "R-19", "R-43", "R-44")
def test_full_recording_flow(rig: Rig) -> None:
    assert arm(rig)["ok"] is True
    assert rig.state is State.ARMED_IDLE and rig.backend.states[-1][0] == "armed"
    _start_call(rig)
    assert rig.state is State.COUNTDOWN
    countdown = rig.backend.notes[-1]
    assert countdown["actions"] == [("cancel", "Abbrechen")] and "signal" in countdown["body"]
    assert "StartRecord" not in rig.fake_obs.names()  # no start before the countdown ends
    rig.tick(n=6)  # countdown 5 s
    assert rig.state is State.RECORDING and rig.fake_obs.recording
    assert rig.backend.states[-1][0] == "recording" and "AUFNAHME" in rig.backend.states[-1][1]
    # countdown sound (always, §2.8) + start sound
    assert rig.backend.sounds == 2 and "Einwilligung" in rig.backend.notes[-1]["body"]
    session = rig.sessions()[0]
    assert session["status"] == "recording" and session["consent_ref"] == "P-017"
    rig.probe.mic = ()
    rig.tick(n=12)  # stop debounce (10 s)
    assert rig.state is State.ARMED_IDLE
    session = rig.sessions()[0]
    assert session["status"] == "complete"
    seg = session["segments"][0]
    assert seg["mode"] == "audio" and seg["trigger"] == "rule:default" and seg["ended_at"]
    folder = next((rig.cfg.data_root_path / "unassigned").glob("S-*"))
    assert (folder / "SHA256SUMS").exists()
    assert not rig.fake_obs.inputs  # teardown released every input


@pytest.mark.req("R-17", "R-37")
def test_countdown_cancel_leaves_no_media(rig: Rig) -> None:
    arm(rig)
    _start_call(rig)
    nid = len(rig.backend.notes)
    rig.backend.notes[-1]["on_action"]("cancel")  # the user clicks "Abbrechen"
    rig.flush()
    assert rig.state is State.ARMED_IDLE
    assert rig.backend.closed == [nid]
    session = rig.sessions()[0]
    assert session["status"] == "cancelled" and session["segments"] == []
    folder = next((rig.cfg.data_root_path / "unassigned").glob("S-*"))
    assert sorted(p.name for p in folder.iterdir()) == ["session.json"]
    assert "StartRecord" not in rig.fake_obs.names()
    rig.tick(n=20)  # the same call does not restart a countdown
    assert rig.state is State.ARMED_IDLE


@pytest.mark.req("R-21")
def test_tray_menu_cancel_and_arm(rig: Rig) -> None:
    rig.backend.press("Studienmodus aktivieren…")
    rig.flush()
    assert rig.state is State.ARMED_IDLE
    _start_call(rig)
    rig.backend.press("Start abbrechen")
    rig.flush()
    assert rig.sessions()[0]["status"] == "cancelled"


@pytest.mark.req("R-16")
def test_disarm_stops_and_finalises(rig: Rig) -> None:
    arm(rig)
    _start_call(rig)
    rig.tick(n=6)
    assert rig.state is State.RECORDING
    rig.agent.handle_ipc({"type": "disarm"})
    rig.flush()
    assert rig.state is State.DISARMED and not rig.fake_obs.recording
    assert rig.sessions()[0]["status"] == "complete"


@pytest.mark.req("R-16")
def test_auto_disarm(tmp_path: Path) -> None:
    raw = cfg_with(tmp_path).model_dump(by_alias=True)
    raw["consent"]["auto_disarm_after_minutes"] = 240  # a time limit (default since ADR-021: none)
    rig = Rig(tmp_path, cfg=Config.model_validate(raw))
    try:
        arm(rig)
        rig.tick(seconds=60, n=241)
        assert rig.state is State.DISARMED
        assert any(n["body"].startswith("Die Zeit ist abgelaufen") for n in rig.backend.notes)
    finally:
        rig.close()


def test_indicator_loss(rig: Rig) -> None:
    arm(rig)
    rig.agent.tray_changed(False)
    rig.flush()
    assert rig.state is State.DISARMED  # armed but idle → disarm
    arm(rig)
    _start_call(rig)
    rig.tick(n=6)
    rig.agent.tray_changed(False)
    rig.flush()
    assert rig.state is State.RECORDING  # never cut a consented recording
    rig.agent.handle_ipc({"type": "stop"})
    rig.flush()
    assert "ui:indicator_lost" in rig.sessions()[0]["warnings"]


@pytest.mark.req("R-53")
def test_obs_crash_and_recovery(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rig.agent, "_recover_obs", lambda *a: None)
    arm(rig)
    _start_call(rig)
    rig.tick(n=6)
    rig.fake_obs.crash()
    rig.flush()
    assert rig.state is State.ERROR
    rig.fake_obs.alive = True
    rig.fake_obs.recording = False
    rig.obs._t = rig.fake_obs
    from captation.session import ObsBack

    rig.agent.post(ObsBack())
    rig.flush()
    assert rig.state is State.RECORDING
    rig.agent.handle_ipc({"type": "stop"})
    rig.flush()
    session = rig.sessions()[0]
    assert [s["trigger"] for s in session["segments"]] == ["rule:default", "recovery:obs_restart"]
    assert session["segments"][0]["interrupted"] is True and session["status"] == "interrupted"


def test_ipc_status_and_unknown_flows(rig: Rig) -> None:
    status = rig.agent.handle_ipc({"type": "status"})
    assert status["ok"] and status["state"] == "DISARMED" and status["tray"] is True
    assert rig.agent.handle_ipc({"type": "arm", "consent_ref": "P-017"})["ok"] is True
    assert rig.agent.handle_ipc({"type": "cancel"})["state"] == "ARMED_IDLE"


# ---- audit 2026-11-24: fail closed around the countdown and the reminder ---------------------


@pytest.mark.req("R-16", "R-17")
def test_countdown_that_cannot_be_shown_never_records(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(app_id: str, seconds: int) -> int:
        raise RuntimeError("notification centre gone")

    monkeypatch.setattr(rig.ui, "countdown", broken)
    arm(rig)
    _start_call(rig)
    rig.tick(n=30)
    assert "StartRecord" not in rig.fake_obs.names()
    assert rig.state is State.DISARMED  # fail closed: a broken countdown also disarms
    assert all(s["status"] == "cancelled" for s in rig.sessions())


@pytest.mark.req("R-16")
def test_preflight_error_cancels_the_start(rig: Rig) -> None:
    arm(rig)

    def exposure() -> bool:
        raise OSError("lsof vanished")

    rig.agent.d.exposure_ok = exposure
    _start_call(rig)
    rig.tick(n=30)
    assert "StartRecord" not in rig.fake_obs.names()


@pytest.mark.req("R-59")
def test_revoked_acceptance_stops_the_next_countdown(rig: Rig) -> None:
    arm(rig)
    rig.agent.d.terms_accepted = lambda: False  # `captation accept --revoke` while armed
    _start_call(rig)
    rig.tick(n=30)
    assert "StartRecord" not in rig.fake_obs.names()
    assert not rig.agent.machine.armed


@pytest.mark.req("R-15")
def test_cancelled_consent_dialog_never_arms(rig: Rig) -> None:
    rig.backend.ask_answer = None  # Cancel or timeout in the dialog
    rig.agent.arm_from_tray()
    rig.flush()
    assert rig.state is State.DISARMED


@pytest.mark.req("R-20")
def test_reminder_failure_falls_back_to_sound(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken() -> None:
        raise RuntimeError("reminder failed")

    monkeypatch.setattr(rig.ui, "recording_started", broken)
    arm(rig)
    _start_call(rig)
    before = rig.backend.sounds
    rig.tick(n=7)
    assert rig.state is State.RECORDING
    assert rig.backend.sounds > before


@pytest.mark.req("R-24")
def test_session_json_exists_before_any_media(rig: Rig) -> None:
    arm(rig)
    _start_call(rig)
    assert rig.state is State.COUNTDOWN
    [session] = rig.sessions()
    assert session["consent_ref"] == "P-017"
    assert "StartRecord" not in rig.fake_obs.names()


@pytest.mark.req("R-53")
def test_unconfirmed_stop_keeps_the_indicator_until_obs_is_reset(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    arm(rig)
    _start_call(rig)
    rig.tick(n=7)
    assert rig.state is State.RECORDING

    def failing_stop(timeout_s: float = 20) -> Any:
        raise ObsError("StopRecord timed out")

    monkeypatch.setattr(rig.obs, "stop_segment", failing_stop)
    monkeypatch.setattr(rig.agent, "_recover_obs", lambda *a: None)  # recovery tested elsewhere
    rig.probe.mic = ()
    rig.tick(n=12)
    assert rig.state is State.ERROR
    assert rig.backend.states[-1][0] == "recording"  # the indicator stays on


@pytest.mark.req("R-23")
def test_probe_helper_stops_while_disarmed(rig: Rig) -> None:
    """Disarmed means nothing is read: the helper (window titles) is stopped (§3.1)."""
    arm(rig)
    rig.tick(n=2)
    calls = rig.probe.calls
    rig.agent.post(Disarm())
    rig.flush()
    rig.tick(n=3)
    assert rig.probe.closes == 1 and rig.probe.calls == calls
    arm(rig)
    rig.tick(n=1)
    assert rig.probe.calls == calls + 1  # started again with the next armed poll


@pytest.mark.req("R-13")
def test_local_track_follows_the_headset_during_the_call(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    from dataclasses import replace

    arm(rig)
    rig.probe.mic = (replace(CALL, input_device="alsa_input.headset"),)
    rig.tick(n=4)
    rig.tick(n=6)
    assert rig.state is State.RECORDING
    local = rig.fake_obs.inputs["Captation Local"]["settings"]
    assert local["device_id"] == "alsa_input.headset"
    # the headset is taken off: the call app falls back to the built-in microphone
    rig.probe.mic = (replace(CALL, input_device="default"),)
    rig.tick()
    assert rig.fake_obs.inputs["Captation Local"]["settings"]["device_id"] == "default"
    assert rig.state is State.RECORDING and rig.fake_obs.names().count("StartRecord") == 1
    assert "alsa_input.headset" not in caplog.text  # device UIDs are never logged


@pytest.mark.req("R-49")
def test_opening_the_app_opens_the_menu_bar_menu(rig: Rig) -> None:
    """The agent runs in the background; opening the app again shows its menu."""
    reply = rig.agent.handle_ipc({"type": "show"})
    assert reply["ok"] is True and rig.backend.menu_opens == 1
    rig.backend.can_open_menu = False  # back ends without it: a notice says where the icon is
    rig.agent.handle_ipc({"type": "show"})
    assert "Menüleiste" in rig.backend.notes[-1]["body"]


def test_status_reports_notification_capabilities(rig: Rig) -> None:
    reply = rig.agent.handle_ipc({"type": "status"})
    assert reply["tray"] is True and reply["notifications"] == ["actions", "body"]


@pytest.mark.req("R-16")
def test_still_armed_reminder_can_end_study_mode(tmp_path: Path) -> None:
    """ADR-021: without a time limit a reminder with 'Studienmodus beenden' appears."""
    raw = cfg_with(tmp_path).model_dump(by_alias=True)
    raw["consent"]["auto_disarm_after_minutes"] = None
    raw["consent"]["armed_reminder_minutes"] = 15
    r = Rig(tmp_path, cfg=Config.model_validate(raw))
    try:
        assert arm(r)["ok"] is True
        assert "bis Sie den Studienmodus beenden" in r.backend.notes[-1]["body"]
        assert "bis Sie ihn beenden" in r.backend.states[-1][1]
        r.tick(seconds=60, n=15)
        note = r.backend.notes[-1]
        assert note["summary"] == "Studienmodus ist noch aktiv"
        assert note["actions"] == [("disarm", "Studienmodus beenden")]
        note["on_action"]("disarm")
        r.flush()
        deadline = time.monotonic() + 5
        while r.state is not State.DISARMED and time.monotonic() < deadline:
            time.sleep(0.05)
            r.flush()
        assert r.state is State.DISARMED
    finally:
        r.close()


# ---- ADR-022: recording during the countdown (the product default) ---------------------------


@pytest.fixture
def live_rig(tmp_path: Path) -> Iterator[Rig]:
    r = Rig(tmp_path, cfg=classic_cfg(tmp_path, live=True))
    yield r
    r.close()


@pytest.mark.req("R-17", "R-18")
def test_recording_runs_during_the_countdown_and_is_kept(live_rig: Rig) -> None:
    rig = live_rig
    assert rig.cfg.detection.start_debounce_s >= 0
    arm(rig)
    _start_call(rig)
    assert rig.state is State.COUNTDOWN
    note = rig.backend.notes[-1]
    assert note["summary"].startswith("Aufnahme läuft") and "löscht" in note["body"]
    assert "StartRecord" in rig.fake_obs.names()  # recording from the shown countdown on
    assert rig.backend.states[-1][0] == "recording"  # the indicator is red at once (§2.5)
    rig.tick(n=6)
    assert rig.state is State.RECORDING
    assert rig.fake_obs.names().count("StartRecord") == 1  # no gap, no second start
    assert "Einwilligung" in rig.backend.notes[-1]["body"]  # start reminder after the countdown
    rig.probe.mic = ()
    rig.tick(n=12)
    rig.agent.wait_for_workers()
    rig.flush()
    assert rig.sessions()[0]["status"] == "complete"


@pytest.mark.req("R-17", "R-19")
def test_cancel_during_the_countdown_deletes_the_recording(live_rig: Rig) -> None:
    rig = live_rig
    arm(rig)
    _start_call(rig)
    assert "StartRecord" in rig.fake_obs.names()
    rig.backend.notes[-1]["on_action"]("cancel")  # the user clicks "Abbrechen"
    rig.flush()
    deadline = time.monotonic() + 5
    while rig.state is not State.ARMED_IDLE and time.monotonic() < deadline:
        time.sleep(0.05)
        rig.flush()
    assert rig.state is State.ARMED_IDLE
    assert "StopRecord" in rig.fake_obs.names() and not rig.fake_obs.recording
    session = rig.sessions()[0]
    assert session["status"] == "cancelled"
    folder = next((rig.cfg.data_root_path / "unassigned").glob("S-*"))
    assert sorted(p.name for p in folder.iterdir()) == ["session.json"]  # no media kept
    assert not any(n.startswith("Captation") for n in rig.fake_obs.inputs)  # mic released
    rig.tick(n=20)  # the same call does not restart a countdown
    assert rig.state is State.ARMED_IDLE


@pytest.mark.req("R-17")
def test_a_countdown_that_cannot_be_shown_records_nothing_even_live(
    live_rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = live_rig

    def broken(app_id: str, seconds: int, *, live: bool = False) -> int:
        raise RuntimeError("notification centre gone")

    monkeypatch.setattr(rig.ui, "countdown", broken)
    arm(rig)
    _start_call(rig)
    rig.tick(n=10)
    assert "StartRecord" not in rig.fake_obs.names()  # recording waits for a shown countdown
    assert all(s["status"] == "cancelled" for s in rig.sessions())
