"""Data contracts shared by probes, classifier, session state machine and OBS layer.

See ``specs/architecture.md`` §4. All types are immutable value objects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Mode = Literal["audio", "video"]
Category = Literal["meeting", "call", "messenger", "browser", "unknown"]
Confidence = Literal["high", "low"]
EncryptionStatus = Literal["encrypted", "unencrypted", "unknown"]
OsName = Literal["linux", "macos", "windows"]
CaptureKind = Literal["window", "display", "none"]
TrackRole = Literal["mix", "remote", "local", "external"]

CATEGORY_PRIORITY: dict[Category, int] = {
    "meeting": 4,
    "call": 3,
    "messenger": 2,
    "browser": 1,
    "unknown": 0,
}


@dataclass(frozen=True)
class AppRef:
    """A process that uses the microphone or the camera."""

    pid: int
    process_name: str
    exe_path: str | None = None
    bundle_id: str | None = None
    package_id: str | None = None
    parent_app: str | None = None


@dataclass(frozen=True)
class ProbeSnapshot:
    """One poll of the OS probe. Window titles live in memory only."""

    ts_monotonic: float
    mic_users: tuple[AppRef, ...]
    camera_active: bool
    camera_users: tuple[AppRef, ...] = ()
    window_titles: dict[int, tuple[str, ...]] | None = None
    confidence: Confidence = "high"


@dataclass(frozen=True)
class CallContext:
    """The classifier's verdict for one snapshot."""

    app_id: str
    category: Category
    pid: int
    mode: Mode
    reason: str
    capture_target: AppRef
    window_title: str | None = None


@dataclass(frozen=True)
class ObsInstallation:
    """A located OBS Studio installation."""

    kind: Literal["native", "flatpak"]
    command: tuple[str, ...]
    config_dir: str
    version: str | None = None


@dataclass(frozen=True)
class AutostartStatus:
    installed: bool
    method: Literal["xdg", "systemd", "launchagent", "task", "none"]
    artefacts: tuple[str, ...] = field(default_factory=tuple)
