"""Track QC: RMS, peak and silence per audio file.

QC never modifies media: the analysis filters (``astats``, ``silencedetect``) write to the
``null`` muxer only. This module also holds the small ffmpeg/ffprobe runner shared by
``postprocess`` and ``importer`` (argv lists, no shell, timeouts, no personal data in errors).
"""

from __future__ import annotations

import json
import logging
import math
import re
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

QC_ROLES = frozenset({"mix", "remote", "local", "external"})
CLIPPING_PEAK_DBFS = -0.5
SILENCE_MIN_DURATION_S = 1.0
WARN_REMOTE_SILENT = "qc:remote_silent"
WARN_LOCAL_SILENT = "qc:local_silent"
WARN_CLIPPING = "qc:clipping"

_TOOL_BASE_TIMEOUT_S = 300.0
_TOOL_MAX_MEDIA_S = 24 * 3600.0
_ERR_TAIL = 400


class ToolError(Exception):
    """ffmpeg/ffprobe failed. The message is a scrubbed, truncated stderr tail."""


def tool_timeout(duration_s: float | None) -> float:
    """Timeout budget for one ffmpeg pass over media of ``duration_s`` seconds."""
    # Clamp: a crafted container can declare an absurd duration (release review).
    return _TOOL_BASE_TIMEOUT_S + min(max(0.0, duration_s or 0.0), _TOOL_MAX_MEDIA_S)


def media_arg(path: Path) -> str:
    """ffmpeg URL for a local path (``file:`` protocol, absolute), never parsed as an option."""
    return "file:" + str(path.resolve())


def _scrub(text: str, scrub: Sequence[Path]) -> str:
    for path in scrub:
        for variant in {str(path), str(path.resolve())}:
            text = text.replace(variant, "<dir>")
    return text[-_ERR_TAIL:].strip()


def run_tool(
    argv: list[str], *, timeout: float, scrub: Sequence[Path] = ()
) -> subprocess.CompletedProcess[str]:
    """Run ffmpeg/ffprobe (argv[0] is resolved on PATH). Raises :class:`ToolError`.

    Paths listed in ``scrub`` are replaced by ``<dir>`` in the error message, so callers can
    keep folder paths out of the logs.
    """
    exe = shutil.which(argv[0])
    if exe is None:
        raise ToolError(f"{argv[0]} not found on PATH")
    try:
        proc = subprocess.run(
            [exe, *argv[1:]],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as exc:
        raise ToolError(f"{argv[0]} timed out after {timeout:.0f} s") from exc
    if proc.returncode != 0:
        raise ToolError(f"{argv[0]} exit {proc.returncode}: {_scrub(proc.stderr, scrub)}")
    return proc


def ffprobe(path: Path, *, timeout: float = 120.0) -> dict[str, Any]:
    """``ffprobe -show_streams -show_format -of json``."""
    proc = run_tool(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            media_arg(path),
        ],
        timeout=timeout,
        scrub=[path.parent],
    )
    data: dict[str, Any] = json.loads(proc.stdout or "{}")
    return data


def probe_duration(info: dict[str, Any]) -> float | None:
    """Container duration in seconds from ffprobe JSON (None if unknown)."""
    raw = info.get("format", {}).get("duration")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and value >= 0 else None


def audio_streams(info: dict[str, Any]) -> list[dict[str, Any]]:
    return [s for s in info.get("streams", []) if s.get("codec_type") == "audio"]


def video_streams(info: dict[str, Any]) -> list[dict[str, Any]]:
    """Real video streams (cover art / attached pictures excluded)."""
    return [
        s
        for s in info.get("streams", [])
        if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")
    ]


# ---- QC -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class QcResult:
    """QC numbers for one audio file. dBFS values are None for digital silence (-inf)."""

    rms_dbfs: float | None
    peak_dbfs: float | None
    silent_ratio: float
    longest_silence_s: float
    duration_s: float | None

    def to_block(self) -> dict[str, float | None]:
        """The ``qc`` block of a file entry in ``session.json``."""
        return {
            "rms_dbfs": self.rms_dbfs,
            "peak_dbfs": self.peak_dbfs,
            "silent_ratio": self.silent_ratio,
            "longest_silence_s": self.longest_silence_s,
        }


_OVERALL_RE = re.compile(r"\]\s*Overall\s*$")
_PEAK_RE = re.compile(r"\]\s*Peak level dB:\s*(\S+)")
_RMS_RE = re.compile(r"\]\s*RMS level dB:\s*(\S+)")
_SIL_START_RE = re.compile(r"silence_start:\s*(-?[0-9.]+)")
_SIL_END_RE = re.compile(r"silence_end:\s*(-?[0-9.]+)")


def _db(text: str) -> float | None:
    try:
        value = float(text)
    except ValueError:
        return None
    return round(value, 2) if math.isfinite(value) else None


def parse_analysis(stderr: str, duration_s: float | None) -> QcResult:
    """Parse ``astats`` (overall section) and ``silencedetect`` output from ffmpeg's log."""
    peak: float | None = None
    rms: float | None = None
    in_overall = False
    starts: list[float] = []
    intervals: list[tuple[float, float]] = []
    for line in stderr.splitlines():
        if _OVERALL_RE.search(line):
            in_overall = True
            continue
        if in_overall:
            if m := _PEAK_RE.search(line):
                peak = _db(m.group(1))
            elif m := _RMS_RE.search(line):
                rms = _db(m.group(1))
        if m := _SIL_START_RE.search(line):
            starts.append(max(0.0, float(m.group(1))))
        elif (m := _SIL_END_RE.search(line)) and starts:
            intervals.append((starts.pop(0), float(m.group(1))))
    if duration_s is not None:  # silence that runs to EOF without a silence_end line
        intervals.extend((start, duration_s) for start in starts)
    silent = sum(max(0.0, end - start) for start, end in intervals)
    longest = max((end - start for start, end in intervals), default=0.0)
    ratio = min(1.0, max(0.0, silent / duration_s)) if duration_s else 0.0
    return QcResult(
        rms_dbfs=rms,
        peak_dbfs=peak,
        silent_ratio=round(ratio, 4),
        longest_silence_s=round(max(0.0, longest), 3),
        duration_s=duration_s,
    )


def analyse(path: Path, silence_dbfs: float) -> QcResult:
    """RMS/peak (``astats``) and silence (``silencedetect``, min 1 s) of the first audio stream.

    Output goes to the ``null`` muxer; the file is only read.
    """
    duration = probe_duration(ffprobe(path))
    analysis_filter = (
        f"astats,silencedetect=noise={silence_dbfs:g}dB:duration={SILENCE_MIN_DURATION_S:g}"
    )
    proc = run_tool(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-nostats",
            "-loglevel",
            "info",
            "-i",
            media_arg(path),
            "-map",
            "0:a:0",
            "-af",
            analysis_filter,
            "-f",
            "null",
            "-",
        ],
        timeout=tool_timeout(duration),
        scrub=[path.parent],
    )
    return parse_analysis(proc.stderr, duration)


def qc_warnings(role: str, result: QcResult, silence_warn_s: float) -> list[str]:
    """``qc:remote_silent`` / ``qc:local_silent`` (silence > ``silence_warn_s``) and
    ``qc:clipping`` (peak > -0.5 dBFS)."""
    warnings: list[str] = []
    if result.longest_silence_s > silence_warn_s:
        if role == "remote":
            warnings.append(WARN_REMOTE_SILENT)
        elif role == "local":
            warnings.append(WARN_LOCAL_SILENT)
    if result.peak_dbfs is not None and result.peak_dbfs > CLIPPING_PEAK_DBFS:
        warnings.append(WARN_CLIPPING)
    return warnings
