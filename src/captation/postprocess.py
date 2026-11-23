"""Post-processing of OBS segments: inventory, track extraction, remux.

Media-producing ffmpeg commands only copy streams (``-c copy``) or re-encode losslessly to FLAC
or 24-bit PCM. They never resample, normalise or filter. All outputs are written to a hidden
``.partial`` file first and renamed into place, so a crash never leaves a half-written
``segNN_<role>`` file.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from captation import qc
from captation.config import Config
from captation.hooks import run_hook
from captation.integrity import (
    SUMS_NAME,
    IntegrityError,
    age_encrypt,
    make_read_only,
    sha256_file,
    write_sha256sums,
)
from captation.models import Mode, TrackRole
from captation.qc import (
    ToolError,
    audio_streams,
    ffprobe,
    media_arg,
    probe_duration,
    run_tool,
    tool_timeout,
    video_streams,
)
from captation.storage import FileRole, SegmentEntry, SessionRecord, SessionStatus

log = logging.getLogger(__name__)

DURATION_TOLERANCE_S = 0.5
MUXERS = {"flac": "flac", "wav": "wav", "mp4": "mp4", "mkv": "matroska", "mka": "matroska"}
EXPORT_CODECS = {"flac": "flac", "wav": "pcm_s24le"}
ROLE_ORDER: tuple[FileRole, ...] = (
    "container",
    "audio",
    "video",
    "mix",
    "remote",
    "local",
    "external",
)
HOOK_STATUSES = frozenset({"complete", "interrupted"})
WARN_ENCRYPTION_FAILED = "integrity:encryption_failed"


@dataclass(frozen=True)
class FinalizeResult:
    """``warnings``: all session warnings after finalising; ``qc_warnings``: the ``qc:`` subset
    (non-empty → show a tray notification)."""

    warnings: list[str] = field(default_factory=list)
    qc_warnings: list[str] = field(default_factory=list)


def track_roles(cfg: Config) -> list[TrackRole]:
    """Configured roles in the order OBS writes them: ascending track number (mix = 1 first)."""
    tracks = cfg.recording.audio.tracks
    pairs: list[tuple[int, TrackRole]] = [
        (tracks.mix, "mix"),
        (tracks.remote, "remote"),
        (tracks.local, "local"),
    ]
    if tracks.external is not None:
        pairs.append((tracks.external, "external"))
    return [role for _, role in sorted(pairs)]


def container_path(record: SessionRecord, index: int) -> Path:
    return record.directory / f"seg{index:02d}_container.mkv"


def _ffmpeg_to(
    src: Path, target: Path, args: list[str], *, muxer: str, duration: float | None
) -> None:
    """``ffmpeg -i src <args> -f <muxer> <target>`` via a hidden partial file + rename."""
    partial = target.with_name(f".{target.name}.partial")
    argv = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        media_arg(src),
        *args,
        "-f",
        muxer,
        media_arg(partial),
    ]
    try:
        run_tool(argv, timeout=tool_timeout(duration), scrub=[src.parent])
        os.replace(partial, target)
    finally:
        partial.unlink(missing_ok=True)


def _first_codec(streams: list[dict[str, Any]]) -> str | None:
    if not streams:
        return None
    name = streams[0].get("codec_name")
    return str(name) if name else None


def _verified(info: dict[str, Any], expected_duration: float | None, audio_count: int) -> bool:
    """Track count matches and duration is within ±0.5 s of the container."""
    if len(audio_streams(info)) != audio_count:
        return False
    duration = probe_duration(info)
    if duration is None or expected_duration is None:
        return False
    return abs(duration - expected_duration) <= DURATION_TOLERANCE_S


def _sort_files(seg: SegmentEntry) -> None:
    seg.files.sort(key=lambda f: ROLE_ORDER.index(f.role))


def process_segment(record: SessionRecord, index: int, mode: Mode, cfg: Config) -> list[str]:
    """Inventory, extract and remux ``segNN_container.mkv`` of segment ``index``.

    Returns the warnings raised for this segment (they are also added to ``record``).
    The container is deleted only if ``keep_original_mkv`` is false and every output was
    verified (durations ±0.5 s, track count) and together they hold all audio tracks.
    """
    seg = record.segment(index)
    prefix = seg.prefix
    sid = record.session_id
    container = container_path(record, index)
    warnings: list[str] = []

    def warn(code: str) -> None:
        if code not in warnings:
            warnings.append(code)
        record.add_warning(code)

    if not container.is_file():
        log.warning("session %s %s: no container file", sid, prefix)
        warn(f"postprocess:container_missing:{prefix}")
        return warnings
    try:
        info = ffprobe(container)
    except ToolError as exc:
        log.error("session %s %s: ffprobe failed: %s", sid, prefix, exc)
        warn(f"postprocess:probe_failed:{prefix}")
        record.register_file(index, "container", container)
        _sort_files(seg)
        return warnings

    duration = probe_duration(info)
    audio = audio_streams(info)
    video = video_streams(info)
    roles = track_roles(cfg)
    container_codec = (
        _first_codec(video) if mode == "video" and video else _first_codec(audio)
    ) or _first_codec(video)

    if len(audio) != len(roles):
        log.error(
            "session %s %s: %d audio streams, %d configured tracks",
            sid,
            prefix,
            len(audio),
            len(roles),
        )
        warn(f"postprocess:track_count_mismatch:{prefix}")
        record.register_file(
            index, "container", container, duration_s=duration, codec=container_codec
        )
        _sort_files(seg)
        return warnings

    all_ok = True
    all_tracks_elsewhere = False

    # -- video remux, or the audio-only copy for strategy B (dummy video stream) ---------------
    combined: tuple[FileRole, str, list[str]] | None = None
    if mode == "video":
        if not video:
            log.warning("session %s %s: video mode but no video stream", sid, prefix)
            warn(f"postprocess:no_video_stream:{prefix}")
            all_ok = False
        else:
            video_ext = cfg.postprocess.remux_video_to
            video_args = ["-map", "0", "-c", "copy"]
            if video_ext == "mp4":
                video_args += ["-movflags", "+faststart"]
            combined = ("video", video_ext, video_args)
    elif video:
        combined = ("audio", "mka", ["-map", "0:a", "-c", "copy"])

    if combined is not None:
        role, ext, args = combined
        target = record.directory / f"{prefix}_{role}.{ext}"
        try:
            _ffmpeg_to(container, target, args, muxer=MUXERS[ext], duration=duration)
            out_info = ffprobe(target)
        except ToolError as exc:
            log.warning("session %s %s: %s copy failed, MKV kept: %s", sid, prefix, role, exc)
            target.unlink(missing_ok=True)
            failed = "remux_failed" if role == "video" else "audio_copy_failed"
            warn(f"postprocess:{failed}:{prefix}")
            all_ok = False
        else:
            codec = _first_codec(video_streams(out_info)) if role == "video" else None
            record.register_file(
                index,
                role,
                target,
                duration_s=probe_duration(out_info),
                codec=codec or _first_codec(audio_streams(out_info)),
            )
            if _verified(out_info, duration, len(roles)):
                all_tracks_elsewhere = True
            else:
                warn(f"postprocess:verify_failed:{role}:{prefix}")
                all_ok = False

    # -- per-role extraction --------------------------------------------------------------------
    extracted_ok = 0
    if cfg.postprocess.extract_tracks:
        fmt = cfg.postprocess.audio_export_format
        for idx, role_name in enumerate(roles):
            target = record.directory / f"{prefix}_{role_name}.{fmt}"
            args = ["-map", f"0:a:{idx}", "-c:a", EXPORT_CODECS[fmt]]
            if fmt == "wav":
                args += ["-rf64", "auto"]
            try:
                _ffmpeg_to(container, target, args, muxer=MUXERS[fmt], duration=duration)
                out_info = ffprobe(target)
            except ToolError as exc:
                log.warning("session %s %s: extracting %s failed: %s", sid, prefix, role_name, exc)
                target.unlink(missing_ok=True)
                warn(f"postprocess:extract_failed:{role_name}:{prefix}")
                all_ok = False
                continue
            record.register_file(
                index,
                role_name,
                target,
                duration_s=probe_duration(out_info),
                codec=_first_codec(audio_streams(out_info)),
            )
            if _verified(out_info, duration, 1):
                extracted_ok += 1
            else:
                warn(f"postprocess:verify_failed:{role_name}:{prefix}")
                all_ok = False
        if mode == "audio" and not video and extracted_ok == len(roles):
            all_tracks_elsewhere = True  # strategy A: the role files hold every track

    # -- container: delete only when allowed and verified ---------------------------------------
    if not cfg.postprocess.keep_original_mkv and all_ok and all_tracks_elsewhere:
        container.unlink()
        record.remove_file(index, container.name)
        log.info("session %s %s: container removed after verification", sid, prefix)
    else:
        record.register_file(
            index, "container", container, duration_s=duration, codec=container_codec
        )
    _sort_files(seg)
    log.info(
        "session %s %s: post-processed (%s mode, %d files, %d warnings)",
        sid,
        prefix,
        mode,
        len(seg.files),
        len(warnings),
    )
    return warnings


# ---- QC, integrity, finalisation --------------------------------------------------------------


def run_qc(record: SessionRecord, cfg: Config) -> list[str]:
    """QC every mix/remote/local/external file that has no ``qc`` block yet.

    Returns the QC warnings raised by this call (also added to ``record``).
    """
    raised: list[str] = []
    for seg, entry in record.iter_files():
        if entry.role not in qc.QC_ROLES or entry.qc is not None:
            continue
        if entry.path.endswith(".age"):
            continue
        try:
            result = qc.analyse(record.directory / entry.path, cfg.qc.silence_dbfs)
        except ToolError as exc:
            log.warning(
                "session %s %s: QC of %s failed: %s", record.session_id, seg.prefix, entry.role, exc
            )
            record.add_warning(f"qc:failed:{entry.role}:{seg.prefix}")
            continue
        entry.qc = result.to_block()
        for code in qc.qc_warnings(entry.role, result, cfg.qc.silence_warn_s):
            record.add_warning(code)
            if code not in raised:
                raised.append(code)
    if raised:
        log.warning("session %s: QC warnings %s", record.session_id, ",".join(raised))
    return raised


def _encrypt_files(record: SessionRecord, recipients: list[str]) -> None:
    for seg, entry in list(record.iter_files()):
        if entry.path.endswith(".age"):
            continue
        try:
            encrypted = age_encrypt(record.directory / entry.path, recipients)
        except (IntegrityError, OSError) as exc:
            log.error(
                "session %s %s: encryption of %s failed, plaintext kept: %s",
                record.session_id,
                seg.prefix,
                entry.role,
                exc,
            )
            record.add_warning(WARN_ENCRYPTION_FAILED)
            continue
        entry.path = encrypted.name


def _checksum_files(record: SessionRecord) -> dict[str, str]:
    digests: dict[str, str] = {}
    for seg, entry in list(record.iter_files()):
        path = record.directory / entry.path
        if not path.is_file():
            log.error("session %s %s: %s file missing", record.session_id, seg.prefix, entry.role)
            record.remove_file(seg.index, entry.path)
            record.add_warning(f"integrity:file_missing:{entry.path}")
            continue
        entry.sha256 = sha256_file(path)
        entry.bytes = path.stat().st_size
        digests[entry.path] = entry.sha256
    return digests


def _ended_at(record: SessionRecord) -> datetime:
    ends = [seg.ended_at for seg in record.segments if seg.ended_at is not None]
    return max(ends) if ends else datetime.now().astimezone()


def hook_due(record: SessionRecord, cfg: Config) -> bool:
    return record.status in HOOK_STATUSES and bool(cfg.hooks.on_session_complete)


def run_session_hook(record: SessionRecord, cfg: Config) -> None:
    """Run ``on_session_complete`` for a finalised session (the agent calls it off-loop)."""
    if not hook_due(record, cfg):
        return
    warning = run_hook(
        list(cfg.hooks.on_session_complete or []),
        session_dir=record.directory,
        session_id=record.session_id,
        project_id=record.project_id,
        timeout_s=cfg.hooks.timeout_s,
        password_env=cfg.obs.websocket.password_env,
    )
    if warning:
        record.add_warning(warning)
        record.write()


def finalize_session(
    record: SessionRecord, cfg: Config, *, status: SessionStatus, run_hooks: bool = True
) -> FinalizeResult:
    """QC → optional age encryption → SHA-256 (``SHA256SUMS`` + session.json) → read-only →
    final validated ``write()`` with ``status`` and ``ended_at`` → ``on_session_complete`` hook
    (only for ``complete``/``interrupted``)."""
    run_qc(record, cfg)
    if cfg.storage.age_recipients:
        _encrypt_files(record, list(cfg.storage.age_recipients))
    digests = _checksum_files(record)
    if digests:
        sums = write_sha256sums(record.directory, list(digests), digests=digests)
        if cfg.postprocess.make_read_only:
            for name in digests:
                make_read_only(record.directory / name)
            make_read_only(sums)
    elif (record.directory / SUMS_NAME).exists():
        (record.directory / SUMS_NAME).unlink()
    record.set_status(status)
    if record.ended_at is None:
        record.set_ended(_ended_at(record))
    record.write()
    log.info("session %s finalised (%s, %d files)", record.session_id, status, len(digests))

    if run_hooks:
        run_session_hook(record, cfg)
    warnings = list(record.warnings)
    return FinalizeResult(
        warnings=warnings, qc_warnings=[w for w in warnings if w.startswith("qc:")]
    )
