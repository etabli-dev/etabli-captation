"""Synthetic media and config helpers for the stage 10/13 tests.

Everything is generated at test time with ffmpeg ``lavfi`` sources into ``tmp_path``; no media
fixtures are committed. Decoding for the FFT check happens here (tests only).
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pytest
import yaml

from captation.config import Config, data_text, parse_config

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
needs_ffmpeg = pytest.mark.skipif(
    FFMPEG is None or FFPROBE is None, reason="ffmpeg/ffprobe not on PATH"
)

POSIX = os.name == "posix"
# root ignores permission bits, so write-protection checks are skipped for it
WRITE_PROTECTION_ENFORCED = not (hasattr(os, "geteuid") and os.geteuid() == 0)
SAMPLE_RATE = 48000
ROLE_FREQS = {"mix": 440.0, "remote": 880.0, "local": 1320.0, "external": 1760.0}


def default_doc() -> dict[str, Any]:
    raw: dict[str, Any] = yaml.safe_load(data_text("config.default.yaml"))
    return raw


def config_doc(tmp_path: Path, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """The packaged default config with ``data_root``/``logging.dir`` in ``tmp_path``.

    ``overrides`` uses dotted keys, e.g. ``{"postprocess.keep_original_mkv": False}``.
    """
    doc = copy.deepcopy(default_doc())
    doc["data_root"] = str(tmp_path / "data")
    doc["logging"]["dir"] = str(tmp_path / "logs")
    for dotted, value in (overrides or {}).items():
        node = doc
        keys = dotted.split(".")
        for key in keys[:-1]:
            node = node[key]
        node[keys[-1]] = value
    return doc


def make_config(tmp_path: Path, overrides: dict[str, Any] | None = None) -> Config:
    return parse_config(config_doc(tmp_path, overrides), source="test")


def write_config_file(tmp_path: Path, overrides: dict[str, Any] | None = None) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config_doc(tmp_path, overrides)), encoding="utf-8")
    return path


def run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, check=True, timeout=120)


def _tone(freq: float, duration: float, silent_after: float | None, amplitude: float) -> str:
    """lavfi ``aevalsrc`` expression: a sine that may fall silent after ``silent_after`` s."""
    gate = f"*lt(t\\,{silent_after})" if silent_after is not None else ""
    return f"aevalsrc={amplitude}*sin(2*PI*{freq}*t){gate}:s={SAMPLE_RATE}:d={duration}:c=mono"


def make_container(
    path: Path,
    *,
    video: Literal["none", "dummy", "testsrc", "ffv1"] = "none",
    freqs: list[float] | None = None,
    duration: float = 4.0,
    silent_after: dict[int, float] | None = None,
    amplitude: float = 0.25,
    audio_codec: str = "pcm_s24le",
) -> Path:
    """MKV like OBS writes it: audio streams in track order (+ optional video stream first).

    ``video``: none (strategy A), dummy (128x72 black, strategy B), testsrc (video mode),
    ffv1 (a video codec MP4 cannot hold, to provoke a remux failure).
    """
    freqs = freqs if freqs is not None else [440.0, 880.0, 1320.0]
    silent_after = silent_after or {}
    argv = [str(FFMPEG), "-nostdin", "-hide_banner", "-loglevel", "error", "-y"]
    maps: list[str] = []
    n = 0
    if video != "none":
        src = (
            f"color=c=black:s=128x72:r=5:d={duration}"
            if video == "dummy"
            else f"testsrc=s=160x120:r=15:d={duration}"
        )
        argv += ["-f", "lavfi", "-i", src]
        maps += ["-map", f"{n}:v"]
        n += 1
    for idx, freq in enumerate(freqs):
        argv += ["-f", "lavfi", "-i", _tone(freq, duration, silent_after.get(idx), amplitude)]
        maps += ["-map", f"{n}:a"]
        n += 1
    argv += maps
    if video != "none":
        vcodec = "ffv1" if video == "ffv1" else "libx264"
        argv += ["-c:v", vcodec]
        if vcodec == "libx264":
            argv += ["-pix_fmt", "yuv420p", "-preset", "ultrafast"]
    argv += ["-c:a", audio_codec, str(path)]
    subprocess.run(argv, check=True, timeout=120, capture_output=True)
    return path


def probe(path: Path) -> dict[str, Any]:
    proc = run(
        [str(FFPROBE), "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)]
    )
    data: dict[str, Any] = json.loads(proc.stdout)
    return data


def stream_types(path: Path) -> list[str]:
    return [str(s["codec_type"]) for s in probe(path)["streams"]]


def dominant_frequency(path: Path, audio_index: int = 0) -> float:
    """Decode one audio stream (tests only) and return the FFT peak frequency in Hz."""
    info = probe(path)
    audio = [s for s in info["streams"] if s["codec_type"] == "audio"]
    rate = int(audio[audio_index]["sample_rate"])
    proc = subprocess.run(
        [
            str(FFMPEG),
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(path),
            "-map",
            f"0:a:{audio_index}",
            "-f",
            "f32le",
            "-",
        ],
        capture_output=True,
        check=True,
        timeout=120,
    )
    samples = np.frombuffer(proc.stdout, dtype="<f4").astype(np.float64)
    channels = int(audio[audio_index].get("channels", 1))
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    spectrum = np.abs(np.fft.rfft(samples * np.hanning(len(samples))))
    freqs = np.fft.rfftfreq(len(samples), d=1.0 / rate)
    return float(freqs[int(np.argmax(spectrum))])


def make_tagged_audio(path: Path, ext: str, *, title: str, duration: float = 3.0) -> Path:
    """Mobile-recorder-like file with planted text metadata (title/artist/comment)."""
    codec = {
        "mp3": ["-c:a", "libmp3lame", "-b:a", "64k"],
        "m4a": ["-c:a", "aac", "-b:a", "64k"],
        "wav": ["-c:a", "pcm_s16le"],
        "flac": ["-c:a", "flac"],
        "opus": ["-c:a", "libopus", "-b:a", "32k"],
        "ogg": ["-c:a", "libvorbis"],
    }[ext]
    argv = [
        str(FFMPEG),
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"sine=f=440:d={duration}:sample_rate={SAMPLE_RATE}",
        *codec,
        "-metadata",
        f"title={title}",
        "-metadata",
        f"artist={title}",
        "-metadata",
        f"comment={title}",
        "-metadata:s:a:0",
        f"title={title}",
        str(path),
    ]
    subprocess.run(argv, check=True, timeout=120, capture_output=True)
    return path


def make_plain_audio(
    path: Path, *, duration: float = 3.0, creation_time: str | None = None
) -> Path:
    argv = [
        str(FFMPEG),
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"sine=f=440:d={duration}:sample_rate={SAMPLE_RATE}",
    ]
    if path.suffix == ".m4a":
        argv += ["-c:a", "aac"]
    if creation_time is not None:
        argv += ["-metadata", f"creation_time={creation_time}"]
    argv.append(str(path))
    subprocess.run(argv, check=True, timeout=120, capture_output=True)
    return path


def tags_of(path: Path) -> list[str]:
    """All format and stream tag values of a media file."""
    info = probe(path)
    values = [str(v) for v in info.get("format", {}).get("tags", {}).values()]
    for stream in info.get("streams", []):
        values += [str(v) for v in stream.get("tags", {}).values()]
    return values


def stream_hash(path: Path) -> str:
    proc = run(
        [
            str(FFMPEG),
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(path),
            "-map",
            "0:a",
            "-c",
            "copy",
            "-f",
            "streamhash",
            "-hash",
            "sha256",
            "-",
        ]
    )
    return proc.stdout.strip()


def grep_tree(root: Path, needles: list[bytes]) -> list[str]:
    """Paths (relative) whose name or bytes contain any needle."""
    hits: list[str] = []
    for path in sorted(root.rglob("*")):
        rel = str(path.relative_to(root))
        if any(n.decode("utf-8", "replace") in rel for n in needles):
            hits.append(rel)
            continue
        if path.is_file():
            data = path.read_bytes()
            if any(n in data for n in needles):
                hits.append(rel)
    return hits
