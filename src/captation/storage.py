"""Session folders, session.json, free-space and disk-encryption policy.

Layout (architecture §8)::

    <data_root>/<project_id or "unassigned">/<session_id>/
        session.json   SHA256SUMS   segNN_<role>.<ext>

Session IDs, folder names and file names never contain personal data (docs/principles.md §3.2).
``session.json`` is validated against the packaged session schema before every write and is
replaced atomically; an invalid document is a bug and is never written.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import re
import secrets
import shutil
import stat
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from captation import integrity
from captation.config import Config, ensure_private_dir, schema_errors
from captation.integrity import sha256_file
from captation.models import CaptureKind, Category, EncryptionStatus, Mode, OsName
from captation.version import __version__

log = logging.getLogger(__name__)

SESSION_SCHEMA = "session.schema.json"
SESSION_FILE = "session.json"
PROVISIONAL_FILE = ".countdown-pending"
UNASSIGNED = "unassigned"
SCHEMA_VERSION = 1
GIB = 1024**3

SESSION_ID_RE = re.compile(r"^S-[0-9]{8}-[0-9]{6}-[0-9a-f]{4}$")
_RAND4_RE = re.compile(r"^[0-9a-f]{4}$")

FileRole = Literal["container", "audio", "video", "mix", "remote", "local", "external"]
SessionStatus = Literal["recording", "complete", "interrupted", "cancelled", "failed"]
OriginType = Literal["obs", "import"]
Device = Literal["plaud_note_pro", "viaim_recdot", "recordergear_pr200", "magmo_pro", "generic"]
TimeSource = Literal["clock", "file_metadata", "user_input", "file_mtime"]
EncryptionVerdict = Literal["ok", "warn", "block"]
EncryptionPolicy = Literal["off", "warn", "block"]

WARN_DISK_UNENCRYPTED = "storage:disk_unencrypted"
WARN_DISK_ENCRYPTION_UNKNOWN = "storage:disk_encryption_unknown"


class SessionDocumentError(ValueError):
    """session.json failed schema validation. Nothing was written."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("invalid session document:\n  " + "\n  ".join(problems))
        self.problems = problems


# ---- small helpers ----------------------------------------------------------------------------


def iso_timestamp(value: datetime) -> str:
    """RFC 3339 timestamp with offset, second precision. Naive values are local time."""
    aware = value if value.tzinfo is not None else value.astimezone()
    return aware.isoformat(timespec="seconds")


def new_session_id(now: datetime, rand4: str | None = None) -> str:
    """``S-YYYYMMDD-HHMMSS-xxxx`` in local time with 4 random lowercase hex characters."""
    local = now.astimezone() if now.tzinfo is not None else now
    suffix = secrets.token_hex(2) if rand4 is None else rand4
    if not _RAND4_RE.match(suffix):
        raise ValueError("rand4 must be 4 lowercase hex characters")
    return f"S-{local:%Y%m%d-%H%M%S}-{suffix}"


@dataclass(frozen=True)
class HostInfo:
    os: OsName
    os_version: str
    agent_version: str
    obs_version: str | None
    obs_websocket_version: str | None

    def to_document(self) -> dict[str, Any]:
        return {
            "os": self.os,
            "os_version": self.os_version,
            "agent_version": self.agent_version,
            "obs_version": self.obs_version,
            "obs_websocket_version": self.obs_websocket_version,
        }


def _os_name() -> OsName:
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform == "darwin":
        return "macos"
    if sys.platform in {"win32", "cygwin"}:
        return "windows"
    raise RuntimeError(f"unsupported platform: {sys.platform}")


def _os_version(os_name: OsName) -> str:
    """Distribution/OS version plus kernel or build. Never the host name."""
    if os_name == "linux":
        try:
            info = platform.freedesktop_os_release()
            distro = f"{info.get('ID', 'linux')}-{info.get('VERSION_ID', '')}".rstrip("-")
        except OSError:
            distro = "linux"
        return f"{distro} ({platform.release()})"
    if os_name == "macos":
        return platform.mac_ver()[0] or platform.release()
    return platform.version()


def host_info(obs_version: str | None, ws_version: str | None) -> HostInfo:
    """Host facts for ``session.json``. Contains no host or user names."""
    os_name = _os_name()
    return HostInfo(
        os=os_name,
        os_version=_os_version(os_name),
        agent_version=__version__,
        obs_version=obs_version,
        obs_websocket_version=ws_version,
    )


# ---- free space and disk encryption -----------------------------------------------------------


def _existing_ancestor(path: Path) -> Path:
    current = path.expanduser()
    while not current.exists() and current.parent != current:
        current = current.parent
    return current


def free_space_gb(path: Path) -> float:
    """Free space in GiB on the file system that holds ``path`` (or its nearest parent)."""
    return shutil.disk_usage(_existing_ancestor(path)).free / GIB


def has_free_space(path: Path, min_free_gb: float) -> bool:
    """True if at least ``min_free_gb`` GiB are free (``shutil.disk_usage``, mockable)."""
    return free_space_gb(path) >= min_free_gb


def encryption_verdict(status: EncryptionStatus, policy: EncryptionPolicy) -> EncryptionVerdict:
    """Disk-encryption policy decision.

    ``off`` → always ok. ``encrypted`` → ok. ``unencrypted`` → ``warn``/``block`` per policy.
    ``unknown`` → ``warn`` under both ``warn`` and ``block`` (we cannot prove it is
    unencrypted, so arming is not refused, but the session gets a warning).
    """
    if policy == "off" or status == "encrypted":
        return "ok"
    if status == "unknown":
        return "warn"
    return "block" if policy == "block" else "warn"


def encryption_warning(status: EncryptionStatus) -> str | None:
    """The ``session.json`` warning for a non-encrypted data root, or None."""
    if status == "unencrypted":
        return WARN_DISK_UNENCRYPTED
    if status == "unknown":
        return WARN_DISK_ENCRYPTION_UNKNOWN
    return None


# ---- session record ---------------------------------------------------------------------------


@dataclass
class FileEntry:
    role: FileRole
    path: str
    sha256: str
    bytes: int
    duration_s: float | None = None
    codec: str | None = None
    qc: dict[str, float | None] | None = None

    def to_document(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "path": self.path,
            "sha256": self.sha256,
            "bytes": self.bytes,
            "duration_s": self.duration_s,
            "codec": self.codec,
            "qc": self.qc,
        }


@dataclass
class SegmentEntry:
    index: int
    mode: Mode
    trigger: str
    started_at: datetime
    capture: CaptureKind
    ended_at: datetime | None = None
    interrupted: bool = False
    files: list[FileEntry] = field(default_factory=list)

    @property
    def prefix(self) -> str:
        return f"seg{self.index:02d}"

    def file(self, name: str) -> FileEntry | None:
        return next((f for f in self.files if f.path == name), None)

    def to_document(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "mode": self.mode,
            "trigger": self.trigger,
            "started_at": iso_timestamp(self.started_at),
            "ended_at": iso_timestamp(self.ended_at) if self.ended_at else None,
            "interrupted": self.interrupted,
            "capture": self.capture,
            "files": [f.to_document() for f in self.files],
        }


class SessionRecord:
    """Mutable builder for one session folder and its ``session.json``.

    :meth:`create` makes the folder; nothing is written until :meth:`write` is called.
    """

    def __init__(
        self,
        *,
        directory: Path,
        session_id: str,
        project_id: str | None,
        consent_ref: str | None,
        app_id: str,
        category: Category,
        window_title: str | None,
        started_at: datetime,
        host: HostInfo,
        origin_type: OriginType,
        device: Device | None,
        time_source: TimeSource,
        source_sha256: str | None,
        config_digest: str,
    ) -> None:
        self._directory = directory
        self._session_id = session_id
        self.project_id = project_id
        self.consent_ref = consent_ref
        self.app_id = app_id
        self.category: Category = category
        self.window_title = window_title
        self.started_at = started_at
        self.ended_at: datetime | None = None
        self.host = host
        self.origin_type: OriginType = origin_type
        self.device: Device | None = device
        self.time_source: TimeSource = time_source
        self.source_sha256 = source_sha256
        self.config_digest = config_digest
        self._status: SessionStatus = "recording"
        self._segments: list[SegmentEntry] = []
        self._warnings: list[str] = []

    @classmethod
    def create(
        cls,
        cfg: Config,
        *,
        session_id: str,
        app_id: str,
        category: Category,
        consent_ref: str | None,
        started_at: datetime,
        host: HostInfo,
        origin_type: OriginType = "obs",
        device: Device | None = None,
        time_source: TimeSource = "clock",
        source_sha256: str | None = None,
        window_title: str | None = None,
    ) -> SessionRecord:
        """Create ``<data_root>/<project_id|unassigned>/<session_id>/`` (mode 0700).

        ``window_title`` is kept only if ``detection.store_window_titles`` is true.
        Raises ``FileExistsError`` if the session folder already exists.
        """
        if not SESSION_ID_RE.match(session_id):
            raise ValueError("session_id must match S-YYYYMMDD-HHMMSS-xxxx")
        project_dir = ensure_private_dir(cfg.data_root_path / (cfg.project_id or UNASSIGNED))
        directory = project_dir / session_id
        directory.mkdir(mode=0o700)
        if os.name == "posix":
            os.chmod(directory, 0o700)
        title = window_title if cfg.detection.store_window_titles else None
        log.info("session %s created (origin %s, app %s)", session_id, origin_type, app_id)
        return cls(
            directory=directory,
            session_id=session_id,
            project_id=cfg.project_id,
            consent_ref=consent_ref,
            app_id=app_id,
            category=category,
            window_title=title,
            started_at=started_at,
            host=host,
            origin_type=origin_type,
            device=device,
            time_source=time_source,
            source_sha256=source_sha256,
            config_digest=cfg.digest(),
        )

    # ---- properties --------------------------------------------------------------------------

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def directory(self) -> Path:
        return self._directory

    @property
    def status(self) -> SessionStatus:
        return self._status

    @property
    def warnings(self) -> tuple[str, ...]:
        return tuple(self._warnings)

    @property
    def segments(self) -> tuple[SegmentEntry, ...]:
        return tuple(self._segments)

    @property
    def session_file(self) -> Path:
        return self._directory / SESSION_FILE

    # ---- mutation ----------------------------------------------------------------------------

    def segment(self, index: int) -> SegmentEntry:
        for seg in self._segments:
            if seg.index == index:
                return seg
        raise KeyError(f"segment {index} not found")

    def begin_segment(
        self,
        index: int,
        mode: Mode,
        trigger: str,
        started_at: datetime,
        capture: CaptureKind,
    ) -> SegmentEntry:
        if not 1 <= index <= 99:
            raise ValueError("segment index must be within 1..99")
        if any(seg.index == index for seg in self._segments):
            raise ValueError(f"segment {index} already exists")
        seg = SegmentEntry(
            index=index, mode=mode, trigger=trigger, started_at=started_at, capture=capture
        )
        self._segments.append(seg)
        self._segments.sort(key=lambda s: s.index)
        return seg

    def end_segment(self, index: int, ended_at: datetime, *, interrupted: bool = False) -> None:
        seg = self.segment(index)
        seg.ended_at = ended_at
        seg.interrupted = seg.interrupted or interrupted

    def add_warning(self, text: str) -> None:
        if text not in self._warnings:
            self._warnings.append(text)

    def set_status(self, status: SessionStatus) -> None:
        self._status = status

    def set_ended(self, ended_at: datetime) -> None:
        self.ended_at = ended_at

    def register_file(
        self,
        index: int,
        role: FileRole,
        path: Path,
        *,
        duration_s: float | None = None,
        codec: str | None = None,
        sha256: str | None = None,
    ) -> FileEntry:
        """Register a produced file (inside the session folder) on segment ``index``.

        Size and SHA-256 are taken from disk (unless ``sha256`` is given). An existing entry
        with the same file name is replaced.
        """
        if path.parent.resolve() != self._directory.resolve():
            raise ValueError("files must live directly in the session folder")
        seg = self.segment(index)
        entry = FileEntry(
            role=role,
            path=path.name,
            sha256=sha256 or sha256_file(path),
            bytes=path.stat().st_size,
            duration_s=None if duration_s is None else round(float(duration_s), 3),
            codec=codec,
        )
        for pos, existing in enumerate(seg.files):
            if existing.path == entry.path:
                entry.qc = existing.qc
                seg.files[pos] = entry
                break
        else:
            seg.files.append(entry)
        return entry

    def remove_file(self, index: int, name: str) -> None:
        seg = self.segment(index)
        seg.files = [f for f in seg.files if f.path != name]

    def iter_files(self) -> Iterator[tuple[SegmentEntry, FileEntry]]:
        for seg in self._segments:
            for entry in seg.files:
                yield seg, entry

    # ---- serialisation -----------------------------------------------------------------------

    def to_document(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "session_id": self._session_id,
            "project_id": self.project_id,
            "consent_ref": self.consent_ref,
            "status": self._status,
            "origin": {
                "type": self.origin_type,
                "device": self.device,
                "time_source": self.time_source,
                "source_sha256": self.source_sha256,
            },
            "app": {
                "id": self.app_id,
                "category": self.category,
                "window_title": self.window_title,
            },
            "started_at": iso_timestamp(self.started_at),
            "ended_at": iso_timestamp(self.ended_at) if self.ended_at else None,
            "host": self.host.to_document(),
            "config_digest": self.config_digest,
            "segments": [seg.to_document() for seg in self._segments],
            "warnings": list(self._warnings),
        }

    def validate(self) -> list[str]:
        return schema_errors(self.to_document(), SESSION_SCHEMA)

    def write(self) -> Path:
        """Validate and atomically replace ``session.json`` (mode 0600).

        Raises :class:`SessionDocumentError` for an invalid document; nothing is written then.
        """
        doc = self.to_document()
        problems = schema_errors(doc, SESSION_SCHEMA)
        if problems:
            log.error("session %s: refusing to write an invalid session.json", self._session_id)
            raise SessionDocumentError(problems)
        text = json.dumps(doc, ensure_ascii=False, indent=2) + "\n"
        target = self.session_file
        tmp = self._directory / f".{SESSION_FILE}.{secrets.token_hex(4)}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, target)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        return target


# ---- retention --------------------------------------------------------------------------------


def iter_session_dirs(data_root: Path) -> Iterator[Path]:
    """All ``<data_root>/<project>/<session_id>/`` folders (session-ID names only)."""
    if not data_root.is_dir():
        return
    for project_dir in sorted(p for p in data_root.iterdir() if p.is_dir()):
        if project_dir.is_symlink():
            continue
        for session_dir in sorted(project_dir.iterdir()):
            if (
                session_dir.is_dir()
                and not session_dir.is_symlink()
                and SESSION_ID_RE.match(session_dir.name)
            ):
                yield session_dir


def _parse_ts(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.astimezone()


def _remove_tree(path: Path) -> None:
    def _onexc(func: Any, target: str, _exc: BaseException) -> None:
        os.chmod(target, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
        func(target)

    shutil.rmtree(path, onexc=_onexc)


def apply_retention(cfg: Config, now: datetime) -> list[str]:
    """Delete whole sessions older than ``storage.retention_days``. Returns the session IDs.

    No-op when ``retention_days`` is None. The age is measured from ``ended_at`` (or
    ``started_at``). Sessions with ``status: recording`` and folders whose ``session.json`` is
    missing or unreadable are never touched. Only session IDs are logged.
    """
    days = cfg.storage.retention_days
    if days is None:
        return []
    current = now if now.tzinfo is not None else now.astimezone()
    cutoff = current - timedelta(days=days)
    deleted: list[str] = []
    for session_dir in iter_session_dirs(cfg.data_root_path):
        sid = session_dir.name
        try:
            doc = json.loads((session_dir / SESSION_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.warning("retention: session %s skipped (no readable session.json)", sid)
            continue
        if not isinstance(doc, dict) or doc.get("status") == "recording":
            continue
        stamp = _parse_ts(doc.get("ended_at")) or _parse_ts(doc.get("started_at"))
        try:
            # An imported recording carries the device's date (often a wrong clock): its
            # age counts from when the session was written here, never from earlier.
            written = datetime.fromtimestamp((session_dir / SESSION_FILE).stat().st_mtime)
            written = written.astimezone()
            stamp = written if stamp is None else max(stamp, written)
        except OSError:
            pass
        if stamp is None or stamp >= cutoff:
            continue
        try:
            _remove_tree(session_dir)
        except OSError as exc:
            log.error("retention: could not delete session %s (%s)", sid, type(exc).__name__)
            continue
        log.info("retention: deleted session %s (older than %d days)", sid, days)
        deleted.append(sid)
    return deleted


def mark_provisional(directory: Path) -> None:
    """Persist cancellation intent before media starts or a delayed cancellation stops it."""
    fd = os.open(directory / PROVISIONAL_FILE, os.O_WRONLY | os.O_CREAT, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write("pending\n")
        fh.flush()
        os.fsync(fh.fileno())


def discard_session_media(directory: Path) -> bool:
    """Delete only within the session, preserving the crash-recovery marker until committed."""
    ok = True
    for path in directory.iterdir():
        if path.name in (SESSION_FILE, PROVISIONAL_FILE):
            continue
        try:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()
        except OSError:
            ok = False
    return ok


def recover_stale_sessions(cfg: Config, now: datetime) -> list[str]:
    """Finalise sessions left in ``status: recording`` by a crashed agent (review finding 3).

    Their media stays as written by OBS (no extraction); the session is marked
    ``interrupted`` with the warning ``agent:recovered_after_crash`` and validated before the
    atomic write. Returns the recovered session IDs.
    """
    recovered: list[str] = []
    for session_dir in iter_session_dirs(cfg.data_root_path):
        path = session_dir / SESSION_FILE
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(doc, dict):
            continue
        provisional = (session_dir / PROVISIONAL_FILE).exists()
        if doc.get("status") != "recording" and not provisional:
            continue
        if provisional and not discard_session_media(session_dir):
            log.warning("cancelled session cleanup pending")
            continue
        stamp = iso_timestamp(now)
        doc["status"] = "cancelled" if provisional else "interrupted"
        if provisional:
            doc["segments"] = []
        doc["ended_at"] = doc.get("ended_at") or stamp
        for seg in doc.get("segments", []):
            if not seg.get("ended_at"):
                seg["ended_at"] = stamp
                seg["interrupted"] = True
        warnings = doc.setdefault("warnings", [])
        if "agent:recovered_after_crash" not in warnings:
            warnings.append("agent:recovered_after_crash")
        names = _register_recovered_media(session_dir, doc)
        problems = schema_errors(doc, SESSION_SCHEMA)
        if problems:
            log.error("session %s could not be recovered: invalid document", session_dir.name)
            continue
        tmp = path.with_suffix(".recover.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
        if names:
            from captation.integrity import make_read_only, write_sha256sums

            write_sha256sums(session_dir, names)
            if cfg.postprocess.make_read_only:
                for name in names:
                    make_read_only(session_dir / name)
        if provisional:
            (session_dir / PROVISIONAL_FILE).unlink()
        recovered.append(session_dir.name)
        log.warning("session %s recovered after an agent crash (interrupted)", session_dir.name)
    return recovered


def _register_recovered_media(session_dir: Path, doc: dict[str, Any]) -> list[str]:
    """OBS containers a crashed agent never registered are added to their segment (with
    SHA-256 and size), so they are covered by ``SHA256SUMS``. Returns every registered name."""
    names: list[str] = []
    for seg in doc.get("segments", []):
        if not isinstance(seg, dict) or not isinstance(seg.get("index"), int):
            continue
        files = seg.setdefault("files", [])
        known = {f.get("path") for f in files if isinstance(f, dict)}
        container = session_dir / f"seg{seg['index']:02d}_container.mkv"
        if container.name not in known and container.is_file() and not container.is_symlink():
            files.append(
                {
                    "role": "container",
                    "path": container.name,
                    "sha256": sha256_file(container),
                    "bytes": container.stat().st_size,
                }
            )
        names += [str(f["path"]) for f in files if isinstance(f, dict) and f.get("path")]
    return [n for n in names if (session_dir / n).is_file()]


MEDIA_DELETED = "media_deleted.json"


def _media_deletion_done(processed: Path) -> bool:
    try:
        record = json.loads((processed / MEDIA_DELETED).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(record, dict) and record.get("complete") is True


def apply_media_retention(cfg: Config, now: datetime) -> list[str]:
    """ADR-023: delete the audio/video of sessions older than ``storage.media_retention_days``
    whose transcript is complete and belongs to that session.

    Only the regular files that ``session.json`` lists as the session's media are deleted
    (never symlinks, never other files in the folder); ``session.json``, ``SHA256SUMS`` (the
    hashes stay as evidence of what was recorded) and ``processed/`` stay, and
    ``processed/media_deleted.json`` lists the deleted files with their hashes. The age counts
    from the later of the session's end and the time ``session.json`` was written (a wrong
    clock at recording time cannot shorten it). An incomplete deletion is retried. Sessions
    without a complete transcript or still recording are never touched. Returns the IDs.
    """
    from captation import transcribe

    days = cfg.storage.media_retention_days
    if days is None:
        return []
    current = now if now.tzinfo is not None else now.astimezone()
    cutoff = current - timedelta(days=days)
    done: list[str] = []
    for session_dir in iter_session_dirs(cfg.data_root_path):
        sid = session_dir.name
        processed = session_dir / transcribe.PROCESSED_DIR
        if processed.is_symlink() or _media_deletion_done(processed):
            continue
        if not transcribe.processed_ok(session_dir):
            continue
        try:
            doc = json.loads((session_dir / SESSION_FILE).read_text(encoding="utf-8"))
            meta = json.loads((processed / transcribe.TRANSCRIPT_JSON).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(doc, dict) or doc.get("status") not in ("complete", "interrupted"):
            continue
        if doc.get("session_id") != sid or (meta.get("session") or {}).get("session_id") != sid:
            continue  # a transcript of another session must never release this media
        stamp = _parse_ts(doc.get("ended_at")) or _parse_ts(doc.get("started_at"))
        try:
            written = datetime.fromtimestamp((session_dir / SESSION_FILE).stat().st_mtime)
            written = written.astimezone()
            stamp = written if stamp is None else max(stamp, written)
        except OSError:
            pass
        if stamp is None or stamp >= cutoff:
            continue
        listed = {
            str(f.get("path")): f.get("sha256")
            for seg in doc.get("segments") or []
            for f in seg.get("files") or []
            if isinstance(f.get("path"), str)
        }
        deleted: list[dict[str, object]] = []
        failed = False
        for name, digest in sorted(listed.items()):
            path = session_dir / name
            if "/" in name or "\\" in name or name in {SESSION_FILE, integrity.SUMS_NAME}:
                continue
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue  # already gone (an earlier, interrupted run)
            except OSError:
                failed = True
                continue
            if not stat.S_ISREG(info.st_mode):
                continue  # symlinks and anything unusual are left alone
            try:
                os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
                path.unlink()
            except OSError as exc:
                log.error(
                    "media retention: %s: could not delete a file (%s)", sid, type(exc).__name__
                )
                failed = True
                continue
            deleted.append({"path": name, "sha256": digest})
        if not deleted and not failed:
            continue
        previous: list[dict[str, object]] = []
        try:
            old = json.loads((processed / MEDIA_DELETED).read_text(encoding="utf-8"))
            previous = [f for f in old.get("files") or [] if isinstance(f, dict)]
        except (OSError, ValueError, AttributeError):
            pass
        record = {
            "deleted_at": current.isoformat(timespec="seconds"),
            "media_retention_days": days,
            "complete": not failed,
            "files": previous + deleted,
        }
        integrity._atomic_write_text(processed / MEDIA_DELETED, json.dumps(record, indent=1) + "\n")
        log.info(
            "media retention: %s: %d media file(s) deleted after %d days", sid, len(deleted), days
        )
        done.append(sid)
    return done
