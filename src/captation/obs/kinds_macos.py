"""macOS capture profile: OBS input kinds and settings for OBS 33 on macOS (ADR-015: pending).

Prepared on Linux from the OBS ``mac-capture`` plugin sources; every identifier carries a
pending ``# PF-1xx`` fact that the Mac run verifies with ``captation obs facts`` and
``GetInputPropertiesListPropertyItems`` (``docs/runbook-macos.md``).

- Remote track: ScreenCaptureKit audio (``sck_audio_capture``) in *application* mode, targeted
  by the call app's bundle ID. Desktop mode of the same kind is the fallback.
- Local track: ``coreaudio_input_capture`` with ``device_id`` = ``default`` or a device UID.
- Video: ScreenCaptureKit (``screen_capture``) in *application* mode targeting the call app's
  bundle ID (all of that app's windows, nothing else: ADR-008), display mode as the fallback.
  Window mode would need a CGWindowID, which the agent does not have.
- ScreenCaptureKit needs no restore token (unlike the Wayland portal): the Screen Recording
  permission of OBS is a one-time TCC grant.
"""

from __future__ import annotations

import logging

from captation.obs.kinds import CaptureProfile

log = logging.getLogger(__name__)

MACOS = "macos"  # not-a-fact: internal OS name

# ---- Input kinds (PF-107) -----------------------------------------------------------------
SCK_AUDIO = "sck_audio_capture"  # PF-107
COREAUDIO_INPUT = "coreaudio_input_capture"  # PF-107
COREAUDIO_OUTPUT = "coreaudio_output_capture"  # PF-107
SCREEN_CAPTURE = "screen_capture"  # PF-107

# ---- sck_audio_capture settings (PF-108) ----------------------------------------------------
SCK_AUDIO_TYPE = "type"  # PF-108
SCK_AUDIO_DESKTOP = 0  # PF-108
SCK_AUDIO_APPLICATION = 1  # PF-108
SCK_AUDIO_APPLICATION_KEY = "application"  # PF-108

# ---- coreaudio_input_capture settings (PF-109) ----------------------------------------------
MIC_DEVICE = "device_id"  # PF-109
MIC_DEFAULT = "default"  # PF-109

# ---- screen_capture settings (PF-110) -------------------------------------------------------
SCREEN_TYPE = "type"  # PF-110
SCREEN_DISPLAY = 0  # PF-110
SCREEN_WINDOW = 1  # PF-110 (needs a CGWindowID; not used)
SCREEN_APPLICATION = 2  # PF-110
SCREEN_APPLICATION_KEY = "application"  # PF-110
SCREEN_SHOW_CURSOR = "show_cursor"  # PF-110
# screen_capture defaults to an empty display_uuid, which records black frames; the default of
# display_capture names the main display in the same UUID format (PF-130).
SCREEN_DISPLAY_UUID = "display_uuid"  # PF-130
DISPLAY_CAPTURE = "display_capture"  # PF-130

# ---- Encoders (PF-111): for kinds.VIDEO_ENCODERS_HARDWARE["macos"] ---------------------------
VIDEO_ENCODERS_HARDWARE = ("com.apple.videotoolbox.videoencoder.ave.avc",)  # PF-111

# ---- CLI flags on macOS (PF-104): the same flags as on Linux (PF-003) -------------------------
LAUNCH_FLAGS = (
    "--minimize-to-tray",  # PF-104
    "--disable-missing-files-check",  # PF-104
    "--disable-updater",  # PF-104
    "--profile",  # PF-104
    "--collection",  # PF-104
)

# ---- List properties for the macOS facts dump (PF-108, PF-109, PF-110) ------------------------
FACT_LIST_PROPERTIES = {
    SCK_AUDIO: (SCK_AUDIO_TYPE, SCK_AUDIO_APPLICATION_KEY),
    # Not `application`: listing it crashes OBS 33.0.3 / obs-websocket 5.7.6 (PF-129).
    SCREEN_CAPTURE: (SCREEN_TYPE,),
    COREAUDIO_INPUT: (MIC_DEVICE,),
}
# Properties whose item *values* are enums (safe to print); other items are app/device names.
FACT_VALUE_PROPERTIES = frozenset({SCK_AUDIO_TYPE, SCREEN_TYPE})


class MacCaptureProfile(CaptureProfile):
    """OBS input kinds and settings builders for macOS (ScreenCaptureKit + CoreAudio)."""

    os_name = MACOS
    app_audio_kinds: tuple[str, ...] = (SCK_AUDIO,)
    # sck_audio_capture defaults to desktop mode (type 0), so ``{}`` settings capture desktop
    # audio; coreaudio_output_capture only works with a loopback device (BlackHole etc.).
    desktop_audio_kinds: tuple[str, ...] = (SCK_AUDIO, COREAUDIO_OUTPUT)
    mic_kinds: tuple[str, ...] = (COREAUDIO_INPUT,)
    screen_kinds: tuple[str, ...] = (SCREEN_CAPTURE,)
    window_kinds: tuple[str, ...] = (SCREEN_CAPTURE,)
    mic_device_property = MIC_DEVICE
    mic_default = MIC_DEFAULT
    screen_token_key: str | None = None
    needs_screen_token = False
    main_display: tuple[str, str] | None = (DISPLAY_CAPTURE, SCREEN_DISPLAY_UUID)

    def can_target_app(self, bundle_id: str | None) -> bool:
        """ScreenCaptureKit targets applications by bundle ID only."""
        return bool(bundle_id)

    def app_audio_settings(
        self, process_name: str, bundle_id: str | None, exe_path: str | None
    ) -> dict[str, object]:
        if not self.can_target_app(bundle_id):
            # A process without a bundle ID cannot be targeted. The base contract has no
            # "cannot target" return yet (see docs/runbook-macos.md, interface notes), so
            # desktop mode is used and logged; the controller should add the warning.
            log.warning("app audio: no bundle ID, using desktop audio for the remote track")
            return {SCK_AUDIO_TYPE: SCK_AUDIO_DESKTOP}
        return {SCK_AUDIO_TYPE: SCK_AUDIO_APPLICATION, SCK_AUDIO_APPLICATION_KEY: bundle_id}

    def window_settings(
        self, process_name: str, bundle_id: str | None, exe_path: str | None
    ) -> dict[str, object] | None:
        if not self.can_target_app(bundle_id):
            return None  # controller falls back to display capture with a warning
        return {
            SCREEN_TYPE: SCREEN_APPLICATION,
            SCREEN_APPLICATION_KEY: bundle_id,
            SCREEN_SHOW_CURSOR: True,
        }

    def screen_settings(self, token: str | None) -> dict[str, object] | None:
        # Display mode on the default (main) display; no token is involved on macOS.
        return {SCREEN_TYPE: SCREEN_DISPLAY, SCREEN_SHOW_CURSOR: True}


PROFILE = MacCaptureProfile()
