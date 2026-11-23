"""ObsController against a real OBS. Needs a bootstrapped OBS on Linux."""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from captation.models import AppRef
from captation.obs import manage
from captation.obs.client import ObsError
from captation.obs.controller import ObsController
from captation.obs.credentials import PasswordMissing
from captation.obs.launcher import ObsNotFound

from .conftest import (
    FAKE_APP,
    TEST_MIC_INPUT,
    TEST_SINK,
    dominant_frequency,
    make_config,
    obs_mic_streams,
    play,
    probe_streams,
)

pytestmark = [pytest.mark.obs, pytest.mark.linux]
TARGET = AppRef(pid=0, process_name=FAKE_APP)


@pytest.fixture
def controller(work_dir: Path, virtual_audio: None) -> Iterator[ObsController]:
    cfg = make_config(work_dir / "data")
    try:
        ctl = manage.make_controller(cfg)
        ctl.ensure_running()
    except (ObsNotFound, PasswordMissing, ObsError) as exc:
        pytest.skip(f"OBS not available: {exc}")
    yield ctl
    try:
        ctl.reset()
    finally:
        ctl.close()


def _record(ctl: ObsController, work_dir: Path, mode: str, name: str, seconds: float) -> Path:
    remote = play(work_dir, 880, seconds + 3, TEST_SINK, app=FAKE_APP)
    local = play(work_dir, 1320, seconds + 3, TEST_MIC_INPUT)
    try:
        time.sleep(0.5)
        prepared = ctl.prepare_segment(mode, TARGET)  # type: ignore[arg-type]
        assert prepared.mode == mode
        out_dir = work_dir / "rec"
        out_dir.mkdir(exist_ok=True)
        ctl.start_segment(out_dir, name)
        time.sleep(seconds)
        path = ctl.stop_segment()
    finally:
        remote.terminate()
        local.terminate()
    ctl.teardown_segment()
    return path


@pytest.mark.req("R-03", "R-11", "R-12", "R-13", "R-42")
def test_audio_segment_has_three_tracks_with_the_right_sources(
    controller: ObsController, work_dir: Path
) -> None:
    path = _record(controller, work_dir, "audio", "seg01_container", 5)
    assert path.name == "seg01_container.mkv"
    streams = probe_streams(path)
    video = [s for s in streams if s["codec_type"] == "video"]
    audio = [s for s in streams if s["codec_type"] == "audio"]
    assert len(video) == 1 and (video[0]["width"], video[0]["height"]) == (128, 72)
    assert len(audio) == 3 and {s["codec_name"] for s in audio} == {"pcm_s24le"}
    remote = dominant_frequency(path, "0:a:1")
    local = dominant_frequency(path, "0:a:2")
    print(f"ffprobe: {[(s['codec_type'], s['codec_name']) for s in streams]}")
    print(f"dominant: remote {remote:.0f} Hz, local {local:.0f} Hz")
    assert abs(remote - 880) < 10
    assert abs(local - 1320) < 10


@pytest.mark.req("R-01", "R-11", "R-28")
def test_video_segment_has_video_and_three_audio_tracks(
    controller: ObsController, work_dir: Path
) -> None:
    path = _record(controller, work_dir, "video", "seg02_container", 5)
    streams = probe_streams(path)
    video = [s for s in streams if s["codec_type"] == "video"]
    audio = [s for s in streams if s["codec_type"] == "audio"]
    print(f"ffprobe: {[(s['codec_type'], s['codec_name'], s.get('width')) for s in streams]}")
    assert len(video) == 1 and video[0]["height"] <= 1080
    assert len(audio) == 3


@pytest.mark.req("R-08", "R-38")
def test_audio_to_video_switch_gap(controller: ObsController, work_dir: Path) -> None:
    remote = play(work_dir, 880, 12, TEST_SINK, app=FAKE_APP)
    try:
        time.sleep(0.5)
        out_dir = work_dir / "switch"
        out_dir.mkdir(exist_ok=True)
        controller.prepare_segment("audio", TARGET)
        controller.start_segment(out_dir, "seg01_container")
        time.sleep(3)
        t0 = time.monotonic()
        first = controller.stop_segment()
        controller.teardown_segment()
        controller.prepare_segment("video", TARGET)
        controller.start_segment(out_dir, "seg02_container")
        gap = time.monotonic() - t0
        time.sleep(2)
        second = controller.stop_segment()
        controller.teardown_segment()
    finally:
        remote.terminate()
    print(f"mode-switch gap: {gap:.3f} s")
    assert first.exists() and second.exists()
    assert gap <= 2.0


@pytest.mark.req("R-36")
def test_teardown_removes_inputs_and_releases_the_mic(
    controller: ObsController, work_dir: Path
) -> None:
    controller.prepare_segment("audio", TARGET)
    time.sleep(0.5)
    assert obs_mic_streams() >= 1
    controller.teardown_segment()
    time.sleep(0.5)
    assert obs_mic_streams() == 0
    names = [i["inputName"] for i in controller._req("GetInputList")["inputs"]]
    assert not [n for n in names if n.startswith("Captation ")]


@pytest.mark.req("R-13")
def test_unresolvable_local_input_falls_back_with_warning(
    work_dir: Path, virtual_audio: None
) -> None:
    cfg = make_config(work_dir / "data", local_input="No Such Microphone 9000")
    try:
        ctl = manage.make_controller(cfg)
        ctl.ensure_running()
    except (ObsNotFound, PasswordMissing, ObsError) as exc:
        pytest.skip(f"OBS not available: {exc}")
    try:
        prepared = ctl.prepare_segment("audio", TARGET)
        assert "local:device_not_found" in prepared.warnings
        settings = ctl._req("GetInputSettings", {"inputName": "Captation Local"})["inputSettings"]
        assert settings.get("device_id") == "default"
    finally:
        ctl.teardown_segment()
        ctl.close()


@pytest.mark.req("R-12")
def test_no_target_uses_desktop_audio_with_warning(controller: ObsController) -> None:
    prepared = controller.prepare_segment("audio", None)
    assert "remote:desktop_fallback" in prepared.warnings
    controller.teardown_segment()
