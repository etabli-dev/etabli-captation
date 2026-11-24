"""Mobile recorder import (architecture §11).

Privacy rules (docs/principles.md §3.2, R-55): the original file name and the device's text metadata
(title, artist, comment, …) never reach ``session.json``, the logs, stdout or the output file's
tags. Only the source SHA-256 is kept. Every ffmpeg/ffprobe call runs on a staged copy with a
neutral name, so even tool error messages cannot contain the original name.

The bitstream is never transcoded: container tags are stripped with a lossless re-mux
(``-map 0:a -c copy -map_metadata -1``) that is verified by comparing an audio stream hash
before and after. If that is impossible, the file is copied unchanged and the session gets
``import:metadata_not_stripped``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal, get_args

from captation import terms
from captation.config import Config, ensure_private_dir
from captation.integrity import CHUNK
from captation.models import EncryptionStatus
from captation.postprocess import finalize_session
from captation.qc import (
    ToolError,
    audio_streams,
    ffprobe,
    media_arg,
    probe_duration,
    run_tool,
    tool_timeout,
)
from captation.storage import (
    GIB,
    Device,
    SessionRecord,
    TimeSource,
    encryption_verdict,
    encryption_warning,
    has_free_space,
    host_info,
    new_session_id,
)

log = logging.getLogger(__name__)

DEVICES: tuple[Device, ...] = get_args(Device)
DEVICE_SHORT: dict[Device, str] = {
    "plaud_note_pro": "plaud",
    "viaim_recdot": "viaim",
    "recordergear_pr200": "pr200",
    "magmo_pro": "magmo",
    "generic": "generic",
}
INDEX_NAME = ".import-index.json"
STAGING_PREFIX = ".import-staging-"
STALE_STAGING_S = 6 * 3600  # an interrupted import leaves plaintext copies behind
MUXERS = {"wav": "wav", "flac": "flac", "mp3": "mp3", "m4a": "ipod", "opus": "opus", "ogg": "ogg"}
APP_ID_RE = re.compile(r"^[a-z0-9_-]+$")
CONSENT_REF_MAX = 64
SHA_PREFIX_LEN = 12
WARN_TIME_FROM_MTIME = "import:time_from_mtime"
WARN_NOT_STRIPPED = "import:metadata_not_stripped"
# Sanity window for container timestamps (devices with an unset clock write 1904/1970 dates).
_EARLIEST_PLAUSIBLE = datetime(2000, 1, 1).astimezone()

ItemStatus = Literal["imported", "would_import", "duplicate", "rejected", "failed"]


class ImportRefused(Exception):
    """The whole import is refused (consent ref, device, source). Message has no file names."""


@dataclass
class ImportResult:
    """One input file. Identified by its position and SHA-256 prefix, never by its name."""

    item: int
    status: ItemStatus
    sha256_prefix: str | None = None
    session_id: str | None = None
    reason: str | None = None
    extension: str | None = None
    bytes: int | None = None
    warnings: list[str] = field(default_factory=list)

    def describe(self) -> str:
        """One stdout line without file names."""
        parts = [f"[{self.item}]"]
        parts.append(f"sha256:{self.sha256_prefix}" if self.sha256_prefix else "sha256:-")
        if self.extension:
            parts.append(self.extension)
        if self.bytes is not None:
            parts.append(f"{self.bytes / 1_000_000:.1f} MB")
        parts.append(self.status)
        if self.session_id:
            parts.append(self.session_id)
        if self.reason:
            parts.append(f"({self.reason})")
        if self.warnings:
            parts.append("warnings: " + ",".join(self.warnings))
        return " ".join(parts)


# ---- validation -------------------------------------------------------------------------------


def validate_consent_ref(cfg: Config, consent_ref: str | None) -> str:
    """Mandatory for imports; must match ``consent.consent_ref_pattern`` (full match)."""
    if consent_ref is None or not consent_ref.strip():
        raise ImportRefused("import refused: --consent-ref is required")
    ref = consent_ref.strip()
    if len(ref) > CONSENT_REF_MAX or any(not ch.isprintable() for ch in ref):
        raise ImportRefused("import refused: the consent reference is not valid")
    pattern = cfg.consent.consent_ref_pattern
    if pattern is not None and re.fullmatch(pattern, ref) is None:
        raise ImportRefused(
            "import refused: the consent reference does not match consent.consent_ref_pattern"
        )
    return ref


def validate_device(device: str) -> Device:
    if device not in DEVICES:
        raise ImportRefused(f"import refused: unknown device (one of: {', '.join(DEVICES)})")
    return device


def default_app_id(device: Device) -> str:
    return f"mobile_{DEVICE_SHORT[device]}"


def _extension(path: Path) -> str:
    return path.suffix.lower().lstrip(".")


def _collect(source: Path, recursive: bool) -> list[Path]:
    if source.is_file():
        return [source]
    if source.is_dir():
        walker = source.rglob("*") if recursive else source.iterdir()
        return sorted(
            p
            for p in walker
            # A deleted recording in .Trashes may have been deleted because consent was
            # withdrawn; a symlink may lead outside the source (audit 2026-11-24).
            if not any(part.startswith(".") for part in p.relative_to(source).parts)
            and not p.is_symlink()
            and p.is_file()
        )
    raise ImportRefused("import refused: the source does not exist or is not a file or folder")


# ---- duplicate index --------------------------------------------------------------------------


class ImportIndex:
    """``<data_root>/.import-index.json`` (mode 0600): source SHA-256 → session ID."""

    def __init__(self, data_root: Path) -> None:
        self.path = data_root / INDEX_NAME
        self._entries: dict[str, dict[str, str]] = {}
        if self.path.is_file():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                log.error("import index unreadable; refusing to continue without it")
                raise ImportRefused("import refused: the import index is unreadable") from None
            if isinstance(raw, dict):
                self._entries = {
                    k: v for k, v in raw.get("sources", {}).items() if isinstance(v, dict)
                }

    def __contains__(self, sha256: str) -> bool:
        return sha256 in self._entries

    def add(self, sha256: str, session_id: str, imported_at: datetime) -> None:
        self._entries[sha256] = {
            "session_id": session_id,
            "imported_at": imported_at.astimezone().isoformat(timespec="seconds"),
        }
        self._save()

    def _save(self) -> None:
        text = json.dumps({"version": 1, "sources": self._entries}, indent=1, sort_keys=True)
        tmp = self.path.with_name(f"{INDEX_NAME}.{secrets.token_hex(4)}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise


# ---- media helpers ----------------------------------------------------------------------------


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_hashing(src: Path, dst: Path) -> str:
    """Copy ``src`` to a new private file ``dst`` and return the SHA-256 of the copied bytes."""
    digest = hashlib.sha256()
    fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with src.open("rb") as fin, os.fdopen(fd, "wb") as fout:
        for chunk in iter(lambda: fin.read(CHUNK), b""):
            digest.update(chunk)
            fout.write(chunk)
        fout.flush()
        os.fsync(fout.fileno())
    return digest.hexdigest()


def stream_hash(path: Path, duration: float | None = None) -> str:
    """SHA-256 per audio stream of the encoded packets (``-c copy -f streamhash``)."""
    proc = run_tool(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            media_arg(path),
            "-map",
            "0:a",
            "-c",
            "copy",
            "-f",
            "streamhash",
            "-hash",
            "sha256",
            "-",
        ],
        timeout=tool_timeout(duration),
        scrub=[path.parent],
    )
    return proc.stdout.strip()


# Tags the muxers write themselves on a tag-free re-mux; they carry no device text.
_TECHNICAL_TAGS = frozenset(
    {"major_brand", "minor_version", "compatible_brands", "vendor_id", "language", "encoder"}
)
_TECHNICAL_VALUES = frozenset({"SoundHandler"})


def _text_tag_values(info: dict[str, Any], *, skip_technical: bool = False) -> set[str]:
    values: set[str] = set()
    for tags in [info.get("format", {}).get("tags", {})] + [
        s.get("tags", {}) for s in info.get("streams", [])
    ]:
        for key, value in tags.items():
            text = str(value).strip()
            if skip_technical and (
                str(key).lower() in _TECHNICAL_TAGS or text in _TECHNICAL_VALUES
            ):
                continue
            if len(text) >= 3:
                values.add(text)
    return values


def _plausible(value: datetime) -> datetime | None:
    aware = value if value.tzinfo is not None else value.astimezone()
    now = datetime.now().astimezone()
    if aware < _EARLIEST_PLAUSIBLE or aware > now + timedelta(days=1):
        return None
    return aware


def metadata_start(info: dict[str, Any]) -> datetime | None:
    """Recording start from container metadata: ``creation_time`` (ISO 8601), or BWF
    ``date`` + time-only ``creation_time``. Naive values are local time."""
    tag_sets = [info.get("format", {}).get("tags", {})] + [
        s.get("tags", {}) for s in audio_streams(info)
    ]
    for tags in tag_sets:
        lowered = {str(k).lower(): str(v).strip() for k, v in tags.items()}
        created = lowered.get("creation_time")
        if not created:
            continue
        try:
            return _plausible(datetime.fromisoformat(created))
        except ValueError:
            pass
        date = lowered.get("date", "")
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date) and re.fullmatch(r"\d{2}:\d{2}:\d{2}", created):
            try:
                return _plausible(datetime.fromisoformat(f"{date}T{created}"))
            except ValueError:
                continue
    return None


def _strip_tags(staged: Path, target: Path, ext: str, duration: float | None) -> bool:
    """Lossless re-mux without tags. True if the audio stream hash is unchanged and no source
    text tag value survived; otherwise ``target`` is removed and False is returned."""
    try:
        run_tool(
            [
                "ffmpeg",
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                media_arg(staged),
                "-map",
                "0:a",
                "-c",
                "copy",
                "-map_metadata",
                "-1",
                "-map_chapters",
                "-1",
                "-fflags",
                "+bitexact",
                "-f",
                MUXERS[ext],
                media_arg(target),
            ],
            timeout=tool_timeout(duration),
            scrub=[staged.parent],
        )
        before = stream_hash(staged, duration)
        after = stream_hash(target, duration)
        leftover = _text_tag_values(ffprobe(staged)) & _text_tag_values(
            ffprobe(target), skip_technical=True
        )
    except ToolError as exc:
        log.warning("import: lossless re-mux failed: %s", exc)
        target.unlink(missing_ok=True)
        return False
    if not before or before != after or leftover:
        log.warning("import: re-mux not verified (stream hash or tags differ)")
        target.unlink(missing_ok=True)
        return False
    log.info("import: tags stripped, audio stream hash unchanged (%s)", after.split("=")[-1][:16])
    return True


# ---- import -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Options:
    device: Device
    consent_ref: str
    started_at: datetime | None
    app_id: str
    dry_run: bool
    force: bool
    storage_warning: str | None = None


def _encryption_status(path: Path) -> EncryptionStatus:
    """Disk encryption of the data folder (platform service; tests replace this)."""
    from captation.platform import get_platform

    return get_platform().disk_encryption().status(path)


@dataclass
class _Candidate:
    item: int
    path: Path
    ext: str
    size: int
    mtime: float


def _candidates(cfg: Config, files: list[Path]) -> Iterator[_Candidate | ImportResult]:
    allowed = set(cfg.import_.allowed_extensions)
    limit = cfg.import_.max_file_gb * GIB
    for item, path in enumerate(files, start=1):
        ext = _extension(path)
        if ext not in allowed:
            yield ImportResult(item=item, status="rejected", reason="extension_not_allowed")
            continue
        try:
            st = path.stat()
        except OSError:
            yield ImportResult(item=item, status="failed", reason="unreadable", extension=ext)
            continue
        if st.st_size > limit:
            yield ImportResult(
                item=item, status="rejected", reason="too_large", extension=ext, bytes=st.st_size
            )
            continue
        if st.st_size == 0:
            yield ImportResult(item=item, status="rejected", reason="empty", extension=ext)
            continue
        yield _Candidate(item=item, path=path, ext=ext, size=st.st_size, mtime=st.st_mtime)


def import_recordings(
    cfg: Config,
    source: Path,
    *,
    device: str,
    consent_ref: str | None,
    started_at: datetime | None,
    app_id: str | None,
    recursive: bool = False,
    dry_run: bool = False,
    force: bool = False,
) -> list[ImportResult]:
    """Import one file or the files of a folder into verified sessions (one per file).

    Raises :class:`ImportRefused` for a missing/invalid consent reference, an unknown device,
    an invalid app ID, a missing source, or a single file with a disallowed extension or size.
    """
    if not terms.accepted():
        raise ImportRefused(
            "import refused: the disclaimer has not been accepted (run `captation accept`)"
        )
    ref = validate_consent_ref(cfg, consent_ref)
    dev = validate_device(device)
    chosen_app = app_id or default_app_id(dev)
    if not APP_ID_RE.match(chosen_app):
        raise ImportRefused("import refused: --app-id must match ^[a-z0-9_-]+$")
    files = _collect(source, recursive)
    single = source.is_file()
    opts = _Options(dev, ref, started_at, chosen_app, dry_run, force)

    data_root = ensure_private_dir(cfg.data_root_path)
    status = _encryption_status(data_root)
    verdict = encryption_verdict(status, cfg.storage.require_disk_encryption)
    if verdict == "block":
        raise ImportRefused(
            "import refused: the data folder is not on an encrypted volume "
            "(storage.require_disk_encryption: block)"
        )
    opts = replace(opts, storage_warning=encryption_warning(status) if verdict == "warn" else None)
    index = ImportIndex(data_root)
    results: list[ImportResult] = []
    for cand in _candidates(cfg, files):
        if isinstance(cand, ImportResult):
            if single:
                raise ImportRefused(f"import refused: {cand.reason}")
            log.info("import: item %d rejected (%s)", cand.item, cand.reason)
            results.append(cand)
            continue
        try:
            results.append(_import_one(cfg, cand, opts, index, data_root))
        except OSError as exc:
            log.error("import: item %d could not be read (%s)", cand.item, type(exc).__name__)
            results.append(
                ImportResult(
                    item=cand.item, status="failed", reason="unreadable", extension=cand.ext
                )
            )
    return results


def _import_one(
    cfg: Config, cand: _Candidate, opts: _Options, index: ImportIndex, data_root: Path
) -> ImportResult:
    source_sha = _hash_file(cand.path)
    prefix = source_sha[:SHA_PREFIX_LEN]
    result = ImportResult(
        item=cand.item, status="failed", sha256_prefix=prefix, extension=cand.ext, bytes=cand.size
    )
    if source_sha in index:
        if not opts.force:
            log.warning("import: sha256 %s already imported, refused (use --force)", prefix)
            result.status, result.reason = "duplicate", "already_imported"
            return result
        log.warning("import: sha256 %s already imported, re-import forced", prefix)
    if opts.dry_run:
        result.status = "would_import"
        return result
    if not has_free_space(data_root, cfg.storage.min_free_gb):
        log.error("import: sha256 %s refused, low disk space", prefix)
        result.status, result.reason = "rejected", "low_disk"
        return result

    staging = Path(tempfile.mkdtemp(prefix=STAGING_PREFIX, dir=data_root))
    record: SessionRecord | None = None
    try:
        staged = staging / f"source.{cand.ext}"
        if _copy_hashing(cand.path, staged) != source_sha:
            log.error("import: sha256 %s changed while reading, refused", prefix)
            result.reason = "source_changed"
            return result
        try:
            info = ffprobe(staged)
        except ToolError as exc:
            log.error("import: sha256 %s is not readable media: %s", prefix, exc)
            result.status, result.reason = "rejected", "unreadable_media"
            return result
        streams = audio_streams(info)
        if not streams:
            result.status, result.reason = "rejected", "no_audio_stream"
            return result
        duration = probe_duration(info)
        if duration is not None and not 0 <= duration <= 7 * 24 * 3600:
            result.status, result.reason = "rejected", "implausible_duration"
            return result

        warnings: list[str] = [opts.storage_warning] if opts.storage_warning else []
        time_source: TimeSource
        meta_start = metadata_start(info)
        if meta_start is not None:
            start, time_source = meta_start, "file_metadata"
        elif opts.started_at is not None:
            start, time_source = opts.started_at, "user_input"
        else:
            start = datetime.fromtimestamp(cand.mtime).astimezone()
            time_source = "file_mtime"
            warnings.append(WARN_TIME_FROM_MTIME)
        start = start if start.tzinfo is not None else start.astimezone()
        end = start + timedelta(seconds=duration or 0.0)

        output = staging / f"seg01_mix.{cand.ext}"
        if not _strip_tags(staged, output, cand.ext, duration):
            shutil.copyfile(staged, output)
            warnings.append(WARN_NOT_STRIPPED)
        out_info = ffprobe(output)

        record = _create_record(cfg, opts, start, time_source, source_sha)
        record.begin_segment(1, "audio", f"import:{opts.device}", start, "none")
        final = record.directory / output.name
        os.replace(output, final)
        out_streams = audio_streams(out_info)
        codec = out_streams[0].get("codec_name") if out_streams else None
        record.register_file(
            1,
            "mix",
            final,
            duration_s=probe_duration(out_info),
            codec=str(codec) if codec else None,
        )
        record.end_segment(1, end)
        record.set_ended(end)
        for code in warnings:
            record.add_warning(code)
        finalize_session(record, cfg, status="complete")
        index.add(source_sha, record.session_id, datetime.now())
        log.info(
            "import: sha256 %s → session %s (device %s, time source %s)",
            prefix,
            record.session_id,
            opts.device,
            time_source,
        )
        result.status = "imported"
        result.session_id = record.session_id
        result.warnings = list(record.warnings)
        record = None  # finalised: keep it
        return result
    except (ToolError, OSError, OverflowError, ValueError) as exc:
        log.error("import: sha256 %s failed: %s", prefix, type(exc).__name__)
        result.status, result.reason = "failed", type(exc).__name__
        return result
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        if record is not None:
            _discard(record)


def _create_record(
    cfg: Config, opts: _Options, start: datetime, time_source: TimeSource, source_sha: str
) -> SessionRecord:
    for _ in range(8):
        try:
            return SessionRecord.create(
                cfg,
                session_id=new_session_id(start),
                app_id=opts.app_id,
                category="call",
                consent_ref=opts.consent_ref,
                started_at=start,
                host=host_info(None, None),
                origin_type="import",
                device=opts.device,
                time_source=time_source,
                source_sha256=source_sha,
            )
        except FileExistsError:
            continue
    raise FileExistsError("could not allocate a unique session ID")


def _discard(record: SessionRecord) -> None:
    """Remove a session folder whose import did not complete (no partial sessions)."""
    log.warning("import: discarding incomplete session %s", record.session_id)

    def _onexc(func: Any, target: str, _exc: BaseException) -> None:
        os.chmod(target, 0o700)
        func(target)

    shutil.rmtree(record.directory, onexc=_onexc)


def sweep_stale_staging(data_root: Path, now: float | None = None) -> int:
    """Remove staging folders of interrupted imports (older than ``STALE_STAGING_S``).

    They hold plain copies of recordings outside any session; a running import is younger.
    """
    import time

    limit = (time.time() if now is None else now) - STALE_STAGING_S
    removed = 0
    if not data_root.is_dir():
        return 0
    for path in data_root.glob(f"{STAGING_PREFIX}*"):
        try:
            if path.is_dir() and not path.is_symlink() and path.stat().st_mtime < limit:
                shutil.rmtree(path)
                removed += 1
        except OSError:
            log.warning("import: could not remove a stale staging folder")
    return removed
