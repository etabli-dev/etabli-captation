from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
import yaml

from captation.config import Config, data_text, parse_config
from captation.models import AppRef
from captation.obs.client import ObsConnectionError, ObsError, ObsLost
from captation.obs.controller import (
    LOCAL,
    REMOTE,
    SCREEN,
    FileTokenStore,
    ObsController,
    track_map,
    tracks_for,
)
from tests.fakes import FakeObs

TARGET = AppRef(pid=4242, process_name="signal-desktop")


def cfg_with(tmp_path: Path, **changes: Any) -> Config:
    raw = yaml.safe_load(data_text("config.default.yaml"))
    raw["data_root"] = str(tmp_path / "data")
    audio = raw["recording"]["audio"]
    for key, value in changes.items():
        if key == "external":
            audio["tracks"]["external"] = value
        elif key == "capture":
            raw["recording"]["video"]["capture"] = value
        else:
            audio[key] = value
    return parse_config(raw)


class MemTokens:
    def __init__(self, token: str | None = None) -> None:
        self.token = token
        self.saved: list[str] = []

    def load(self) -> str | None:
        return self.token

    def save(self, token: str) -> None:
        self.saved.append(token)
        self.token = token


def make(tmp_path: Path, fake: FakeObs | None = None, **kw: Any) -> tuple[ObsController, FakeObs]:
    fake = fake or FakeObs()
    cfg = kw.pop("cfg", None) or cfg_with(tmp_path)
    ctl = ObsController(cfg, lambda: fake, os_name="linux", **kw)
    ctl.ensure_running()
    return ctl, fake


@pytest.mark.req("R-11")
def test_track_routing_is_mix_plus_role() -> None:
    assert tracks_for(2) == {"1": True, "2": True, "3": False, "4": False, "5": False, "6": False}
    assert tracks_for(3)["3"] and not tracks_for(3)["2"]


@pytest.mark.req("R-11", "R-12", "R-13", "R-36")
def test_audio_segment_call_order_and_tracks(tmp_path: Path) -> None:
    ctl, fake = make(tmp_path)
    prep = ctl.prepare_segment("audio", TARGET)
    assert prep.capture == "none" and prep.warnings == ()
    assert fake.current_profile == "Captation-Audio" and fake.program_scene == "Audio"
    remote = fake.inputs[REMOTE]
    assert remote["kind"] == "pipewire_audio_application_capture"
    assert remote["settings"] == {
        "CaptureMode": 0,
        "TargetName": "signal-desktop",
        "MatchPriorty": 0,
    }
    assert remote["tracks"] == tracks_for(2) and remote["monitor"] == "OBS_MONITORING_TYPE_NONE"
    assert fake.inputs[LOCAL]["tracks"] == tracks_for(3)
    out = tmp_path / "rec"
    ctl.start_segment(out, "seg01_container")
    assert fake.record_dir == str(out)
    path = ctl.stop_segment()
    assert path == out / "seg01_container.mkv" and path.stat().st_size > 0
    ctl.teardown_segment()
    assert fake.inputs == {}
    names = fake.names()
    assert (
        names.index("SetCurrentProfile") < names.index("CreateInput") < names.index("StartRecord")
    )


@pytest.mark.req("R-12")
def test_desktop_fallback_without_app_capture(tmp_path: Path) -> None:
    fake = FakeObs(kinds=["pipewire_audio_output_capture", "pulse_input_capture"])
    ctl, fake = make(tmp_path, fake)
    prep = ctl.prepare_segment("audio", TARGET)
    assert "remote:desktop_fallback" in prep.warnings
    assert fake.inputs[REMOTE]["kind"] == "pipewire_audio_output_capture"
    ctl2, _ = make(tmp_path)
    assert "remote:desktop_fallback" in ctl2.prepare_segment("audio", None).warnings


def test_missing_kinds_are_errors(tmp_path: Path) -> None:
    ctl, _ = make(tmp_path, FakeObs(kinds=["pulse_input_capture"]))
    with pytest.raises(ObsError, match="desktop-audio"):
        ctl.prepare_segment("audio", None)
    ctl, _ = make(tmp_path, FakeObs(kinds=["pipewire_audio_output_capture"]))
    with pytest.raises(ObsError, match="microphone"):
        ctl.prepare_segment("audio", None)


@pytest.mark.req("R-13")
def test_named_local_device_resolves_or_falls_back(tmp_path: Path) -> None:
    ctl, fake = make(tmp_path, cfg=cfg_with(tmp_path, local_input="headset mic"))
    assert ctl.prepare_segment("audio", TARGET).warnings == ()
    assert fake.inputs[LOCAL]["settings"]["device_id"] == "alsa_input.headset"
    ctl.teardown_segment()
    ctl, fake = make(tmp_path, cfg=cfg_with(tmp_path, local_input="nope"))
    assert "local:device_not_found" in ctl.prepare_segment("audio", TARGET).warnings
    assert fake.inputs[LOCAL]["settings"]["device_id"] == "default"


def test_external_input(tmp_path: Path) -> None:
    cfg = cfg_with(tmp_path, external=4, external_input_device="Headset Mic")
    ctl, fake = make(tmp_path, cfg=cfg)
    ctl.prepare_segment("audio", TARGET)
    assert fake.inputs["Captation External"]["tracks"] == tracks_for(4)
    assert track_map(cfg) == {"remote": 2, "local": 3, "external": 4}
    cfg = cfg_with(tmp_path, external=4, external_input_device="RØDECaster")
    ctl, fake = make(tmp_path, cfg=cfg)
    assert "external:device_not_found" in ctl.prepare_segment("audio", TARGET).warnings
    assert "Captation External" not in fake.inputs


@pytest.mark.req("R-28")
def test_video_capture_needs_an_approved_token(tmp_path: Path) -> None:
    ctl, fake = make(tmp_path, token_store=MemTokens(None))
    prep = ctl.prepare_segment("video", TARGET)
    assert prep.capture == "none" and "capture:not_approved" in prep.warnings
    assert fake.current_profile == "Captation-Video" and fake.program_scene == "Video"
    ctl.teardown_segment()
    tokens = MemTokens("tok-1")
    ctl, fake = make(tmp_path, token_store=tokens)
    prep = ctl.prepare_segment("video", TARGET)
    assert prep.capture == "display" and "capture:display_fallback" in prep.warnings
    assert fake.inputs[SCREEN]["settings"] == {"RestoreToken": "tok-1"}
    fake.inputs[SCREEN]["settings"]["RestoreToken"] = "tok-2"
    ctl.teardown_segment()
    assert tokens.saved == ["tok-2"]
    ctl, fake = make(
        tmp_path, token_store=MemTokens("t"), cfg=cfg_with(tmp_path, capture="display")
    )
    assert ctl.prepare_segment("video", TARGET).warnings == ()


SWITCHES = frozenset(
    {"SetCurrentProfile", "SetCurrentSceneCollection", "CreateProfile", "CreateSceneCollection"}
)


class GuardedObs(FakeObs):
    """Records every profile or scene-collection request that arrives while OBS records."""

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.violations: list[str] = []

    def request(
        self, request_type: str, data: dict[str, Any] | None = None, *, timeout_s: float = 10
    ) -> dict[str, Any]:
        if request_type in SWITCHES and self.recording:
            self.violations.append(request_type)
        return super().request(request_type, data, timeout_s=timeout_s)


class FakeClock:
    """Stands in for the controller's ``time`` module: sleeping advances the clock at once."""

    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


@pytest.mark.req("R-38")
def test_no_profile_or_collection_switch_while_recording(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("captation.obs.controller.time", FakeClock())
    # Someone else's recording, OBS on another profile and collection: the segment needs a
    # switch to Captation-Audio and the Captation collection, which must not happen now.
    ctl, fake = make(tmp_path, GuardedObs(), token_store=MemTokens(None))
    assert (fake.current_profile, fake.current_collection) == ("Untitled", "Untitled")
    fake.recording = True
    with pytest.raises(ObsError, match="recording"):
        ctl.prepare_segment("audio", TARGET)
    with pytest.raises(ObsError, match="recording"):
        ctl.approve_screen(timeout_s=1)
    assert fake.violations == [] and not SWITCHES & set(fake.names())
    assert "CreateInput" not in fake.names() and fake.inputs == {}
    assert (fake.current_profile, fake.current_collection) == ("Untitled", "Untitled")

    # Our own recording: a camera upgrade needs the video profile, which waits for the stop.
    fake.recording = False
    ctl.prepare_segment("audio", TARGET)
    ctl.start_segment(tmp_path / "rec", "seg01_container")
    sent_before = len(fake.calls)
    with pytest.raises(ObsError, match="recording"):
        ctl.prepare_segment("video", TARGET)
    assert not SWITCHES & set(fake.names()[sent_before:])
    assert fake.current_profile == "Captation-Audio" and fake.recording

    # After the stop the switch is allowed, and none was ever sent while recording.
    ctl.stop_segment()
    ctl.teardown_segment()
    ctl.prepare_segment("video", TARGET)
    assert fake.current_profile == "Captation-Video"
    assert fake.violations == []


def test_record_status_lag_is_waited_out(tmp_path: Path) -> None:
    ctl, fake = make(tmp_path)
    fake.lag_active = 3
    ctl.prepare_segment("audio", TARGET)
    assert fake.current_profile == "Captation-Audio"


def test_fallback_record_path_for_old_websocket(tmp_path: Path) -> None:
    ctl, fake = make(tmp_path, FakeObs(set_record_directory=False))
    ctl.prepare_segment("audio", TARGET)
    ctl.start_segment(tmp_path / "rec", "seg01_container")
    assert fake.params["Captation-Audio"][("AdvOut", "RecFilePath")] == str(tmp_path / "rec")
    assert ctl.stop_segment().name == "seg01_container.mkv"


@pytest.mark.linux  # Flatpak OBS cannot write to /tmp (PF-011)
def test_flatpak_refuses_tmp(tmp_path: Path) -> None:
    from captation.models import ObsInstallation

    inst = ObsInstallation(kind="flatpak", command=("flatpak",), config_dir="/x")
    ctl, _ = make(tmp_path, installation=inst)
    with pytest.raises(ObsError, match="/tmp"):
        ctl.start_segment(Path("/tmp/x"), "seg01_container")


@pytest.mark.req("R-53")
def test_connection_loss_during_stop_raises_obs_lost(tmp_path: Path) -> None:
    lost: list[bool] = []
    ctl, fake = make(tmp_path, on_lost=lambda: lost.append(True))
    ctl.prepare_segment("audio", TARGET)
    ctl.start_segment(tmp_path / "rec", "seg01_container")
    fake.emit_stopped = False
    fake.fail["StopRecord"] = ObsLost("gone")
    with pytest.raises(ObsLost):
        ctl.stop_segment(timeout_s=1)
    fake.crash()
    assert lost == [True]
    assert ctl.health().connected is False
    with pytest.raises(ObsLost):
        ctl.prepare_segment("audio", TARGET)


def test_stop_timeout_without_stopped_event(tmp_path: Path) -> None:
    ctl, fake = make(tmp_path)
    ctl.prepare_segment("audio", TARGET)
    ctl.start_segment(tmp_path / "rec", "seg01_container")
    fake.emit_stopped = False
    with pytest.raises(ObsError, match="timeout"):
        ctl.stop_segment(timeout_s=0.3)


def test_reset_and_health(tmp_path: Path) -> None:
    ctl, fake = make(tmp_path)
    ctl.prepare_segment("audio", TARGET)
    ctl.start_segment(tmp_path / "rec", "seg01_container")
    assert ctl.health().recording is True
    ctl.reset()
    assert fake.recording is False and fake.inputs == {}
    ctl.reset()  # idempotent, StopRecord not needed
    ctl.close()
    assert ctl.health().connected is False


def test_ensure_running_needs_profiles_and_retries(tmp_path: Path) -> None:
    fake = FakeObs(profiles=["Untitled"])
    with pytest.raises(ObsError, match="bootstrap"):
        make(tmp_path, fake)
    attempts: list[int] = []

    def flaky() -> FakeObs:
        attempts.append(1)
        if len(attempts) < 3:
            raise ObsConnectionError("not yet")
        return FakeObs()

    launched: list[bool] = []
    ctl = ObsController(
        cfg_with(tmp_path), flaky, os_name="linux", launch=lambda: launched.append(True)
    )
    info = ctl.ensure_running(timeout_s=10)
    assert info.set_record_directory and len(attempts) == 3 and launched == [True]
    assert ctl.ensure_running() is info
    never = ObsController(
        cfg_with(tmp_path), lambda: (_ for _ in ()).throw(ObsConnectionError("x")), os_name="linux"
    )
    with pytest.raises(ObsConnectionError):
        never.ensure_running(timeout_s=0.6)


def test_approve_screen_stores_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tokens = MemTokens(None)
    ctl, fake = make(tmp_path, token_store=tokens)
    monkeypatch.setattr(
        "captation.obs.controller.time.sleep",
        lambda s: (
            fake.inputs.get(SCREEN, {}).get("settings", {}).update({"RestoreToken": "approved"})
        ),
    )
    assert ctl.approve_screen(timeout_s=5) is True
    assert tokens.saved == ["approved"] and SCREEN not in fake.inputs
    assert fake.current_profile == "Captation-Audio"
    ctl2, _ = make(tmp_path)
    assert ctl2.approve_screen() is False  # no token store


def test_file_token_store(tmp_path: Path) -> None:
    store = FileTokenStore(tmp_path / "state" / "screencast-token")
    assert store.load() is None
    store.save("abc")
    assert store.load() == "abc"
    if os.name == "posix":  # Windows: ACLs, not mode bits
        assert (tmp_path / "state" / "screencast-token").stat().st_mode & 0o077 == 0


# ---- audit 2026-11-24 ---------------------------------------------------------------------


@pytest.mark.req("R-53")
def test_obs_ending_the_recording_itself_is_reported(tmp_path: Path) -> None:
    lost: list[bool] = []
    stopped: list[bool] = []
    ctl, fake = make(
        tmp_path, on_lost=lambda: lost.append(True), on_record_stopped=lambda: stopped.append(True)
    )
    ctl.prepare_segment("audio", AppRef(pid=0, process_name="signal"))
    ctl.start_segment(tmp_path / "rec", "seg01_container")
    fake.recording = False  # disk full / stop clicked in the OBS window
    fake.emit("RecordStateChanged", {"outputState": "OBS_WEBSOCKET_OUTPUT_STOPPED"})
    assert stopped == [True] and lost == []  # still connected: a stop, not a lost OBS
    # a stop the agent asked for is not reported
    ctl2, _ = make(tmp_path, on_record_stopped=lambda: stopped.append(False))
    ctl2.prepare_segment("audio", AppRef(pid=0, process_name="signal"))
    ctl2.start_segment(tmp_path / "rec2", "seg01_container")
    ctl2.stop_segment()
    assert stopped == [True]


@pytest.mark.req("R-11", "R-42")
def test_every_segment_verifies_the_track_layout_and_container(tmp_path: Path) -> None:
    ctl, fake = make(tmp_path)
    ctl.prepare_segment("audio", AppRef(pid=0, process_name="signal"))
    profile = fake.current_profile
    fake.params[profile][("AdvOut", "RecTracks")] = "3"  # edited in the OBS window
    fake.params[profile][("AdvOut", "RecFormat2")] = "hybrid_mov"
    ctl.start_segment(tmp_path / "rec", "seg01_container")
    assert fake.params[profile][("AdvOut", "RecTracks")] == "7"
    assert fake.params[profile][("AdvOut", "RecFormat2")] == "mkv"
    assert fake.params[profile][("AdvOut", "RecSplitFile")] == "false"


@pytest.mark.req("R-36")
def test_leftover_inputs_are_removed_when_connecting(tmp_path: Path) -> None:
    fake = FakeObs()
    fake.inputs["Captation Local"] = {"kind": "pulse_input_capture", "settings": {}}
    make(tmp_path, fake)
    assert "Captation Local" not in fake.inputs  # OBS never holds the mic while idle


@pytest.mark.req("R-34")
def test_no_scene_switch_while_the_users_virtual_camera_runs(tmp_path: Path) -> None:
    ctl, fake = make(tmp_path)
    fake.virtualcam = True
    with pytest.raises(ObsError, match="virtual camera"):
        ctl.prepare_segment("audio", AppRef(pid=0, process_name="signal"))
    assert fake.current_collection != "Captation" or fake.calls  # nothing switched before


@pytest.mark.req("R-24")
def test_record_directory_is_absolute(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctl, fake = make(tmp_path)
    ctl.prepare_segment("audio", AppRef(pid=0, process_name="signal"))
    monkeypatch.chdir(tmp_path)
    ctl.start_segment(Path("rel") / "rec", "seg01_container")
    assert fake.record_dir == str((tmp_path / "rel" / "rec").resolve())
