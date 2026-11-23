"""Agent configuration: YAML → JSON-Schema validation (packaged schema) → pydantic v2 models.

Unknown keys are errors at both levels. Runtime code loads only package data
(``importlib.resources``), never repo paths.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from functools import cache
from importlib import resources
from pathlib import Path
from typing import Any, Literal

import jsonschema
import yaml
from platformdirs import user_config_dir
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from captation.errors import ConfigError

__all__ = ["Config", "ConfigError", "load_config", "parse_config"]

APP_NAME = "captation"
APP_AUTHOR = "etabli"
DEFAULT_CONFIG_RESOURCE = "config.default.yaml"


Encoder = Literal["hardware", "x264"]
ImportExt = Literal["wav", "flac", "mp3", "m4a", "opus", "ogg"]
DEFAULT_ENCODERS: tuple[Encoder, ...] = ("hardware", "x264")
DEFAULT_IMPORT_EXTS: tuple[ImportExt, ...] = ("wav", "flac", "mp3", "m4a", "opus", "ogg")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ConsentConfig(_Strict):
    require_armed: Literal[True]
    auto_disarm_after_minutes: int
    countdown_seconds: int
    ask_consent_ref_on_arm: bool = True
    consent_ref_pattern: str | None = None
    start_reminder_text: str | None = None


class DetectionConfig(_Strict):
    poll_interval_ms: int
    start_debounce_s: float
    stop_debounce_s: float
    rules_file: str
    user_rules_file: str | None = None
    record_unknown_apps: bool = True
    store_window_titles: bool = False
    extra_meeting_titles: list[str] = Field(default_factory=list)


class TracksConfig(_Strict):
    mix: Literal[1]
    remote: int
    local: int
    external: int | None = None


class AudioConfig(_Strict):
    sample_rate: Literal[44100, 48000]
    codec_preference: list[Literal["pcm_s24le", "pcm_s16le", "flac", "alac", "aac"]]
    tracks: TracksConfig
    external_input_device: str | None = None
    local_input: str = "follow_default"


class VideoConfig(_Strict):
    capture: Literal["window_preferred", "display"]
    fps: Literal[15, 24, 25, 30]
    max_height: Literal[720, 1080, 1440]
    encoder_preference: list[Encoder] = Field(default_factory=lambda: list(DEFAULT_ENCODERS))


class RecordingConfig(_Strict):
    audio: AudioConfig
    video: VideoConfig
    segments_on_mode_change: bool = True


class WebsocketConfig(_Strict):
    host: Literal["127.0.0.1", "::1", "localhost"]
    port: int
    password_source: Literal["keyring", "env"] = "keyring"
    password_env: str = "CAPTATION_OBS_PASSWORD"


SAFE_OBS_LAUNCH_ARGS = frozenset(
    {"--verbose", "--unfiltered_log", "--only-bundled-plugins", "--always-on-top"}
)


class ObsConfig(_Strict):
    executable: str | None = None
    websocket: WebsocketConfig
    profile_audio: str
    profile_video: str
    scene_collection: str
    launch_args: list[str] = Field(default_factory=list)
    launch_on: Literal["arm", "agent_start"] = "arm"
    exposure_policy: Literal["require_firewall", "warn"] = "require_firewall"

    @model_validator(mode="after")
    def _safe_launch_args(self) -> ObsConfig:
        # --startrecording/--startreplaybuffer/--startstreaming/--safe-mode/--websocket_* would
        # bypass the consent guard or the exposure policy (release review).
        bad = [a for a in self.launch_args if a not in SAFE_OBS_LAUNCH_ARGS]
        if bad:
            raise ValueError(
                f"obs.launch_args: not allowed: {bad} (allowed: {sorted(SAFE_OBS_LAUNCH_ARGS)})"
            )
        return self


class PostprocessConfig(_Strict):
    extract_tracks: bool
    audio_export_format: Literal["flac", "wav"]
    remux_video_to: Literal["mp4", "mkv"]
    keep_original_mkv: bool
    checksums: Literal["sha256"]
    make_read_only: bool = True


class StorageConfig(_Strict):
    require_disk_encryption: Literal["off", "warn", "block"]
    min_free_gb: float
    age_recipients: list[str] = Field(default_factory=list)
    retention_days: int | None = None


class HooksConfig(_Strict):
    on_session_complete: list[str] | None = None
    timeout_s: int = 3600


class UiConfig(_Strict):
    sound_on_start: bool = True


class LoggingConfig(_Strict):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    dir: str | None = None


class QcConfig(_Strict):
    silence_dbfs: float
    silence_warn_s: float


class ImportConfig(_Strict):
    require_consent_ref: Literal[True]
    allowed_extensions: list[ImportExt] = Field(default_factory=lambda: list(DEFAULT_IMPORT_EXTS))
    max_file_gb: float = 4


class Config(_Strict):
    schema_version: Literal[1]
    project_id: str | None = None
    data_root: str
    language: Literal["de", "en"] = "de"
    consent: ConsentConfig
    detection: DetectionConfig
    recording: RecordingConfig
    obs: ObsConfig
    postprocess: PostprocessConfig
    storage: StorageConfig
    hooks: HooksConfig = Field(default_factory=HooksConfig)
    ui: UiConfig = Field(default_factory=UiConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    qc: QcConfig
    import_: ImportConfig = Field(alias="import")

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    @model_validator(mode="after")
    def _extra_checks(self) -> Config:
        tracks = self.recording.audio.tracks
        numbers = [tracks.mix, tracks.remote, tracks.local]
        if tracks.external is not None:
            numbers.append(tracks.external)
        if len(set(numbers)) != len(numbers):
            raise ValueError(f"recording.audio.tracks: track numbers must be unique, got {numbers}")
        pattern = self.consent.consent_ref_pattern
        if pattern is not None:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"consent.consent_ref_pattern does not compile: {exc}") from exc
        return self

    # ---- derived values -------------------------------------------------------------------

    @property
    def data_root_path(self) -> Path:
        return Path(os.path.expanduser(self.data_root))

    def digest(self) -> str:
        """``sha256:<hex>`` of the canonical JSON form (session.json ``config_digest``)."""
        canonical = json.dumps(
            self.model_dump(mode="json", by_alias=True), sort_keys=True, separators=(",", ":")
        )
        return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---- loading --------------------------------------------------------------------------------


def data_text(name: str) -> str:
    """Read a packaged data file (``captation/data/<name>``)."""
    return resources.files("captation.data").joinpath(name).read_text(encoding="utf-8")


@cache
def packaged_schema(name: str) -> dict[str, Any]:
    schema: dict[str, Any] = json.loads(data_text(name))
    return schema


def schema_errors(document: Any, schema_name: str) -> list[str]:
    """Validate ``document`` against a packaged schema; return readable error lines."""
    validator = jsonschema.Draft202012Validator(
        packaged_schema(schema_name), format_checker=jsonschema.FormatChecker()
    )
    errors = sorted(validator.iter_errors(document), key=lambda e: list(e.absolute_path))
    lines = []
    for err in errors:
        where = "/".join(str(p) for p in err.absolute_path) or "<root>"
        lines.append(f"{where}: {err.message}")
    return lines


def default_config_path() -> Path:
    return Path(user_config_dir(APP_NAME, APP_AUTHOR)) / "config.yaml"


def parse_config(raw: Any, *, source: str = "<config>") -> Config:
    """Validate a parsed YAML document and build the pydantic model."""
    if not isinstance(raw, dict):
        raise ConfigError(f"{source}: the configuration must be a mapping")
    problems = schema_errors(raw, "config.schema.json")
    if problems:
        raise ConfigError(f"{source}: invalid configuration:\n  " + "\n  ".join(problems))
    try:
        return Config.model_validate(raw)
    except ValidationError as exc:
        lines = [
            f"{'/'.join(str(p) for p in e['loc']) or '<root>'}: {e['msg']}" for e in exc.errors()
        ]
        raise ConfigError(f"{source}: invalid configuration:\n  " + "\n  ".join(lines)) from exc


def load_config(path: Path | None = None, *, create_data_root: bool = True) -> Config:
    """Load the user config, or the packaged default if no user config exists.

    ``path`` given explicitly must exist. The data root is expanded and created with
    user-only permissions.
    """
    if path is None:
        candidate = default_config_path()
        if candidate.exists():
            path = candidate
    if path is None:
        text, source = data_text(DEFAULT_CONFIG_RESOURCE), "builtin:default"
    else:
        if not path.exists():
            raise ConfigError(f"config file not found: {path}")
        text, source = path.read_text(encoding="utf-8"), str(path)
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{source}: YAML error: {exc}") from exc
    cfg = parse_config(raw, source=source)
    if create_data_root:
        ensure_private_dir(cfg.data_root_path)
    return cfg


def ensure_private_dir(path: Path) -> Path:
    """Create ``path`` (and parents) with mode 0700 for the leaf; tighten if it exists."""
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix":
        os.chmod(path, 0o700)
    return path
