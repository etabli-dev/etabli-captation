"""Fixtures for tests against a real OBS (marker ``obs``) on Linux.

Audio never touches real devices: tones play into a PipeWire null sink, and the "microphone"
is a virtual source (``Captation Test Mic``) fed with its own tone. The fake call app is
``fakecall-e2e``, a symlink to ``pw-cat`` so PipeWire reports that binary name.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import yaml

from captation.config import Config, data_text, parse_config

TEST_SINK = "captation-test-sink"
TEST_MIC = "captation-test-mic"
TEST_MIC_INPUT = "captation-test-mic-in"
TEST_MIC_DESCRIPTION = "Captation Test Mic"
FAKE_APP = "fakecall-e2e"
CACHE = Path.home() / ".cache" / "captation-test"  # Flatpak OBS cannot write to /tmp (PF-011)


def _pw_nodes() -> dict[str, int]:
    out = subprocess.run(["pw-dump"], capture_output=True, text=True, timeout=10).stdout
    nodes = {}
    for obj in json.loads(out):
        props = (obj.get("info") or {}).get("props") or {}
        name = props.get("node.name")
        if obj.get("type") == "PipeWire:Interface:Node" and name:
            nodes[str(name)] = int(obj["id"])
    return nodes


def _create_node(name: str, description: str, media_class: str) -> None:
    props = (
        "{ factory.name=support.null-audio-sink "
        f'node.name={name} node.description="{description}" media.class={media_class} '
        "object.linger=true audio.position=[FL FR] }"
    )
    subprocess.run(["pw-cli", "create-node", "adapter", props], capture_output=True, timeout=10)


@pytest.fixture(scope="session")
def virtual_audio() -> Iterator[None]:
    """A null sink for the fake call app and a virtual mic (pw-loopback) fed by tones."""
    if not sys.platform.startswith("linux") or not shutil.which("pw-loopback"):
        pytest.skip("needs PipeWire")
    created = []
    if TEST_SINK not in _pw_nodes():
        _create_node(TEST_SINK, "Captation Test Sink", "Audio/Sink")
        created.append(TEST_SINK)
    loopback = subprocess.Popen(
        [
            "pw-loopback",
            f"--capture-props=media.class=Audio/Sink node.name={TEST_MIC_INPUT} "
            'node.description="Captation Test Mic Input"',
            f"--playback-props=media.class=Audio/Source node.name={TEST_MIC} "
            f'node.description="{TEST_MIC_DESCRIPTION}"',
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(1.0)
    yield
    loopback.terminate()
    loopback.wait(5)
    nodes = _pw_nodes()
    for name in created:
        if name in nodes:
            subprocess.run(["pw-cli", "destroy", str(nodes[name])], capture_output=True, timeout=10)


@pytest.fixture(scope="session")
def work_dir() -> Iterator[Path]:
    CACHE.mkdir(parents=True, exist_ok=True)
    path = Path(tempfile.mkdtemp(prefix="run-", dir=CACHE))
    yield path
    shutil.rmtree(path, ignore_errors=True)


def make_config(data_root: Path, **audio: Any) -> Config:
    raw = yaml.safe_load(data_text("config.default.yaml"))
    raw["data_root"] = str(data_root)
    raw["recording"]["audio"]["local_input"] = audio.get("local_input", TEST_MIC_DESCRIPTION)
    return parse_config(raw)


def tone_file(directory: Path, freq: int, seconds: float) -> Path:
    path = directory / f"tone-{freq}-{seconds}.wav"
    if not path.exists():
        subprocess.run(
            [
                "ffmpeg",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency={freq}:duration={seconds}",
                "-ac",
                "2",
                str(path),
            ],
            check=True,
            timeout=30,
        )
    return path


def fake_app(directory: Path, name: str = FAKE_APP) -> Path:
    link = directory / name
    if not link.exists():
        pw_cat = shutil.which("pw-cat")
        assert pw_cat, "pw-cat not found"
        link.symlink_to(pw_cat)
    return link


def play(
    directory: Path, freq: int, seconds: float, target: str, app: str | None = None
) -> subprocess.Popen[bytes]:
    wav = tone_file(directory, freq, seconds)
    exe = str(fake_app(directory, app)) if app else "pw-play"
    argv = (
        [exe, "--playback", "--target", target, str(wav)]
        if app
        else [exe, "--target", target, str(wav)]
    )
    return subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def dominant_frequency(
    path: Path, stream: str, *, skip_s: float = 1.0, seconds: float = 2.0
) -> float:
    raw = subprocess.run(
        [
            "ffmpeg",
            "-loglevel",
            "error",
            "-ss",
            str(skip_s),
            "-t",
            str(seconds),
            "-i",
            str(path),
            "-map",
            stream,
            "-ac",
            "1",
            "-f",
            "f32le",
            "-",
        ],
        capture_output=True,
        check=True,
        timeout=60,
    ).stdout
    samples = np.frombuffer(raw, dtype=np.float32)
    if samples.size == 0 or float(np.max(np.abs(samples))) < 1e-4:
        return 0.0
    spectrum = np.abs(np.fft.rfft(samples * np.hanning(samples.size)))
    freqs = np.fft.rfftfreq(samples.size, 1 / 48000)
    return float(freqs[int(np.argmax(spectrum))])


def probe_streams(path: Path) -> list[dict[str, Any]]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(path)],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    ).stdout
    streams: list[dict[str, Any]] = json.loads(out)["streams"]
    return streams


def obs_mic_streams() -> int:
    out = subprocess.run(["pw-dump"], capture_output=True, text=True, timeout=10).stdout
    count = 0
    for obj in json.loads(out):
        props = (obj.get("info") or {}).get("props") or {}
        if (
            obj.get("type") == "PipeWire:Interface:Node"
            and props.get("media.class") == "Stream/Input/Audio"
            and props.get("application.process.binary") == "obs"
        ):
            count += 1
    return count
