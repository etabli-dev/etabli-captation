"""Verified OBS identifiers: CLI flags, config paths, profile keys and input kinds.

Every constant carries a ``# PF-xxx`` reference to ``docs/platform-facts.md``
(``tools/check_facts.py`` enforces it). Values were verified on Linux with the OBS 33.0.3
Flatpak; macOS and Windows entries are added in stages 06/07.
"""

from __future__ import annotations

from typing import Any

# ---- CLI flags (PF-003) -------------------------------------------------------------------
FLAG_PROFILE = "--profile"  # PF-003
FLAG_COLLECTION = "--collection"  # PF-003
FLAG_MINIMIZE_TO_TRAY = "--minimize-to-tray"  # PF-003
FLAG_DISABLE_MISSING_FILES_CHECK = "--disable-missing-files-check"  # PF-003
FLAG_DISABLE_UPDATER = "--disable-updater"  # PF-003

# ---- obs-websocket config (PF-004) ----------------------------------------------------------
WEBSOCKET_CONFIG_RELPATH = "plugin_config/obs-websocket/config.json"  # PF-004
WS_KEY_ENABLED = "server_enabled"  # PF-004
WS_KEY_PORT = "server_port"  # PF-004
WS_KEY_AUTH = "auth_required"  # PF-004
WS_KEY_PASSWORD = "server_password"  # PF-004
WS_KEY_ALERTS = "alerts_enabled"  # PF-004
WS_KEY_FIRST_LOAD = "first_load"  # PF-004

# ---- Profile parameters (category, name) (PF-006) ---------------------------------------
P_OUTPUT_MODE = ("Output", "Mode")  # PF-006
P_FILENAME_FORMAT = ("Output", "FilenameFormatting")  # PF-006
P_REC_TYPE = ("AdvOut", "RecType")  # PF-006
P_REC_FORMAT = ("AdvOut", "RecFormat2")  # PF-006
P_REC_TRACKS = ("AdvOut", "RecTracks")  # PF-006
P_REC_ENCODER = ("AdvOut", "RecEncoder")  # PF-006
P_REC_AUDIO_ENCODER = ("AdvOut", "RecAudioEncoder")  # PF-006
P_REC_PATH = ("AdvOut", "RecFilePath")  # PF-006
P_REC_RB_ADV = ("AdvOut", "RecRB")  # PF-006
P_REC_RB_SIMPLE = ("SimpleOutput", "RecRB")  # PF-006
P_REC_SPLIT = ("AdvOut", "RecSplitFile")  # PF-006
P_SAMPLE_RATE = ("Audio", "SampleRate")  # PF-006
V_OUTPUT_ADVANCED = "Advanced"  # PF-006
V_REC_STANDARD = "Standard"  # PF-006
V_FORMAT_MKV = "mkv"  # PF-006
V_FALSE = "false"  # PF-006

# ---- Encoders (PF-007): config codec name → OBS encoder id ---------------------------------
AUDIO_ENCODERS = {
    "pcm_s24le": "ffmpeg_pcm_s24le",  # PF-007
    "pcm_s16le": "ffmpeg_pcm_s16le",  # PF-007
    "flac": "ffmpeg_flac",  # PF-007
    "alac": "ffmpeg_alac",  # PF-007
    "aac": "ffmpeg_aac",  # PF-007
}
VIDEO_ENCODER_X264 = "obs_x264"  # PF-007
VIDEO_ENCODERS_HARDWARE = {
    "linux": ("obs_nvenc_h264_tex",),  # PF-007
}

# ---- Audio-only strategy B canvas (PF-008, ADR-009) ------------------------------------------
AUDIO_CANVAS = (128, 72)  # PF-008
AUDIO_FPS = 30  # PF-008

# ---- Input kinds per OS, ordered by preference (PF-009) -------------------------------------
APP_AUDIO_KINDS = {"linux": ("pipewire_audio_application_capture",)}  # PF-009
DESKTOP_AUDIO_KINDS = {"linux": ("pipewire_audio_output_capture", "pulse_output_capture")}  # PF-009
MIC_KINDS = {"linux": ("pulse_input_capture",)}  # PF-009
SCREEN_KINDS = {"linux": ("pipewire-screen-capture-source",)}  # PF-009

# ---- Input settings and property names (PF-010) ---------------------------------------------
APP_CAPTURE_MODE = "CaptureMode"  # PF-010
APP_CAPTURE_SINGLE = 0  # PF-010
APP_TARGET = "TargetName"  # PF-010
APP_MATCH = "MatchPriorty"  # PF-010
APP_MATCH_BINARY = 0  # PF-010
MIC_DEVICE = "device_id"  # PF-010
MIC_DEFAULT = "default"  # PF-010
MONITOR_OFF = "OBS_MONITORING_TYPE_NONE"  # PF-010

# ---- OBS log markers (PF-007) -----------------------------------------------------------------
LOG_ENCODERS_HEADER = "Available Encoders:"  # PF-007
LOG_AUDIO_ENCODERS = "Audio Encoders:"  # PF-007
LOG_VIDEO_ENCODERS = "Video Encoders:"  # PF-007
SCREEN_RESTORE_TOKEN = "RestoreToken"  # PF-009

# ---- List properties queried by the facts dump (PF-010) --------------------------------------
FACT_LIST_PROPERTIES = {
    "pipewire_audio_application_capture": (APP_CAPTURE_MODE, APP_MATCH),  # PF-010
    "pulse_input_capture": (MIC_DEVICE,),  # PF-010
    "pulse_output_capture": (MIC_DEVICE,),  # PF-010
}


# ---- Capture profile per OS ---------------------------------------------------------------------
# The controller asks the profile of the current OS for kinds and settings. Linux lives here;
# macOS and Windows live in kinds_macos.py / kinds_windows.py (ADR-015: pending facts).


LINUX = "linux"  # not-a-fact: internal OS name


class CaptureProfile:
    """Input kinds and settings builders of one OS (Linux implementation)."""

    os_name = LINUX
    app_audio_kinds: tuple[str, ...] = APP_AUDIO_KINDS[LINUX]
    desktop_audio_kinds: tuple[str, ...] = DESKTOP_AUDIO_KINDS[LINUX]
    mic_kinds: tuple[str, ...] = MIC_KINDS[LINUX]
    screen_kinds: tuple[str, ...] = SCREEN_KINDS[LINUX]
    window_kinds: tuple[str, ...] = ()  # Wayland: no dynamic window targeting (PF-019)
    mic_device_property = MIC_DEVICE
    mic_default = MIC_DEFAULT
    screen_token_key: str | None = SCREEN_RESTORE_TOKEN
    needs_screen_token = True

    def can_target_app(self, bundle_id: str | None) -> bool:
        """Whether per-app capture can target the app (Linux: by executable name, always)."""
        return True

    def app_audio_settings(
        self, process_name: str, bundle_id: str | None, exe_path: str | None
    ) -> dict[str, object]:
        return {
            APP_CAPTURE_MODE: APP_CAPTURE_SINGLE,
            APP_TARGET: process_name,
            APP_MATCH: APP_MATCH_BINARY,
        }

    def window_settings(
        self, process_name: str, bundle_id: str | None, exe_path: str | None
    ) -> dict[str, object] | None:
        return None

    def screen_settings(self, token: str | None) -> dict[str, object] | None:
        return {SCREEN_RESTORE_TOKEN: token} if token else None


def os_module(os_name: str) -> Any:
    """``captation.obs.kinds_<os>`` for macOS/Windows; None for Linux (this module)."""
    if os_name == LINUX:
        return None
    import importlib

    return importlib.import_module(f"captation.obs.kinds_{os_name}")


def launch_flags(os_name: str) -> frozenset[str]:
    """CLI flags verified for this OS (Linux PF-003; others via their own PF entries)."""
    module = os_module(os_name)
    if module is None:
        return frozenset(
            {
                FLAG_MINIMIZE_TO_TRAY,
                FLAG_DISABLE_MISSING_FILES_CHECK,
                FLAG_DISABLE_UPDATER,
                FLAG_PROFILE,
                FLAG_COLLECTION,
            }
        )
    return frozenset(getattr(module, "LAUNCH_FLAGS", ()))  # not-a-fact: attribute name


def video_encoders_hardware(os_name: str) -> tuple[str, ...]:
    module = os_module(os_name)
    if module is None:
        return VIDEO_ENCODERS_HARDWARE.get(os_name, ())
    return tuple(getattr(module, "VIDEO_ENCODERS_HARDWARE", ()))  # not-a-fact: attribute name


def fact_list_properties(os_name: str) -> dict[str, tuple[str, ...]]:
    module = os_module(os_name)
    if module is None:
        return dict(FACT_LIST_PROPERTIES)
    return dict(getattr(module, "FACT_LIST_PROPERTIES", {}))  # not-a-fact: attribute name


_VALUE_ATTR = "FACT_VALUE_PROPERTIES"  # not-a-fact: attribute name


def fact_value_properties(os_name: str) -> frozenset[str]:
    module = os_module(os_name)
    if module is None:
        return frozenset({APP_CAPTURE_MODE, APP_MATCH})
    values: frozenset[str] = getattr(module, _VALUE_ATTR, frozenset())
    return frozenset(values)


def capture_profile(os_name: str) -> CaptureProfile:
    if os_name == LINUX:
        return CaptureProfile()
    import importlib

    module = importlib.import_module(f"captation.obs.kinds_{os_name}")
    profile: CaptureProfile = module.PROFILE
    return profile
