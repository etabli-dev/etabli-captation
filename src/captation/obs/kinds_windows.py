"""Windows capture profile (stage 07): WASAPI per-application, desktop and microphone audio,
Windows Graphics Capture of the call window, display capture as the fallback.

Prepared on Linux from the OBS 33.0.3 sources (``plugins/win-wasapi``, ``plugins/win-capture``,
``libobs/util/windows/window-helpers.c``). Every identifier refers to a ``pending`` PF entry
(ADR-015) that ``captation obs facts`` and the runbook verify on Windows.

Privacy (docs/principles.md §3.1): the ``window`` target string carries only the executable
name, never the window title, because OBS persists input settings in its scene-collection file.
Matching by executable (``priority`` = exe) works with an empty title and class (PF-241).
"""

from __future__ import annotations

import sys

from captation.obs.kinds import CaptureProfile

WINDOWS = "windows"  # not-a-fact: internal OS name

# ---- input kinds (PF-240) -------------------------------------------------------------------
APP_AUDIO_KIND = "wasapi_process_output_capture"  # PF-240
DESKTOP_AUDIO_KIND = "wasapi_output_capture"  # PF-240
MIC_KIND = "wasapi_input_capture"  # PF-240
WINDOW_KIND = "window_capture"  # PF-240
SCREEN_KIND = "monitor_capture"  # PF-240

# ---- window target string and match priority (PF-241) -----------------------------------------
S_WINDOW = "window"  # PF-241
S_PRIORITY = "priority"  # PF-241
WINDOW_FIELD_SEPARATOR = ":"  # PF-241
PRIORITY_CLASS = 0  # PF-241
PRIORITY_TITLE = 1  # PF-241
PRIORITY_EXE = 2  # PF-241
# OBS escapes '#' first, then ':' inside each field (encode_dstr in window-helpers.c).
WINDOW_ESCAPES = (("#", "#22"), (":", "#3A"))  # PF-241

# ---- audio devices (PF-242) --------------------------------------------------------------------
DEVICE_ID = "device_id"  # PF-242
DEVICE_DEFAULT = "default"  # PF-242

# ---- window capture (PF-243) and display capture (PF-244) --------------------------------------
S_METHOD = "method"  # PF-243
WINDOW_METHOD_WGC = 2  # PF-243
S_MONITOR = "monitor_id"  # PF-244
# GDI device name of a monitor; OBS matches it in its fallback search (PF-244). The default
# "DUMMY" selects no monitor, so the display fallback must always name one.
MONITOR_FALLBACK = "\\\\.\\DISPLAY1"  # PF-244
MONITOR_DEFAULTTOPRIMARY = 1  # PF-244

# ---- list properties for the facts dump (wired by obs/facts.py) --------------------------------
# ---- CLI flags on Windows (PF-206): the same flags as on Linux (PF-003) -------------------------
LAUNCH_FLAGS = (
    "--minimize-to-tray",  # PF-206
    "--disable-missing-files-check",  # PF-206
    "--disable-updater",  # PF-206
    "--profile",  # PF-206
    "--collection",  # PF-206
)

# ---- Hardware H.264 encoders on Windows (PF-245), first available wins ------------------------
VIDEO_ENCODERS_HARDWARE = (
    "obs_nvenc_h264_tex",  # PF-245
    "obs_qsv11_v2",  # PF-245
    "h264_texture_amf",  # PF-245
)

FACT_LIST_PROPERTIES = {
    APP_AUDIO_KIND: (S_WINDOW, S_PRIORITY),
    MIC_KIND: (DEVICE_ID,),
    DESKTOP_AUDIO_KIND: (DEVICE_ID,),
    WINDOW_KIND: (S_METHOD, S_PRIORITY),
    SCREEN_KIND: (S_METHOD, S_MONITOR),
}


def encode_window_field(text: str) -> str:
    for raw, escaped in WINDOW_ESCAPES:
        text = text.replace(raw, escaped)
    return text


def window_target(exe: str, title: str = "", window_class: str = "") -> str:
    """OBS ``window`` setting ``title:class:exe`` with OBS's field escaping (PF-241)."""
    fields = (title, window_class, exe)
    return WINDOW_FIELD_SEPARATOR.join(encode_window_field(f) for f in fields)


def exe_name(process_name: str, exe_path: str | None) -> str:
    """The executable file name OBS compares against (case-insensitive, with ``.exe``)."""
    source = exe_path or process_name
    return source.replace("/", "\\").rsplit("\\", 1)[-1]  # PF-215


def primary_monitor_device() -> str:
    """GDI device name (``\\\\.\\DISPLAYn``) of the primary monitor (PF-244)."""
    if sys.platform != "win32":  # not-a-fact: sys.platform value
        return MONITOR_FALLBACK
    import ctypes
    from ctypes import wintypes

    class MonitorInfoEx(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),  # PF-244
            ("rcMonitor", wintypes.RECT),  # PF-244
            ("rcWork", wintypes.RECT),  # PF-244
            ("dwFlags", wintypes.DWORD),  # PF-244
            ("szDevice", wintypes.WCHAR * 32),  # PF-244
        ]

    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)  # PF-244
        user32.MonitorFromPoint.restype = wintypes.HMONITOR
        user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
        user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(MonitorInfoEx)]
        handle = user32.MonitorFromPoint(wintypes.POINT(0, 0), MONITOR_DEFAULTTOPRIMARY)
        info = MonitorInfoEx()
        info.cbSize = ctypes.sizeof(MonitorInfoEx)
        if handle and user32.GetMonitorInfoW(handle, ctypes.byref(info)):  # PF-244
            return str(info.szDevice) or MONITOR_FALLBACK
    except (AttributeError, OSError):
        pass
    return MONITOR_FALLBACK


class WindowsCaptureProfile(CaptureProfile):
    """Input kinds and settings builders for Windows (pending facts, ADR-015)."""

    os_name = WINDOWS
    app_audio_kinds: tuple[str, ...] = (APP_AUDIO_KIND,)
    desktop_audio_kinds: tuple[str, ...] = (DESKTOP_AUDIO_KIND,)
    mic_kinds: tuple[str, ...] = (MIC_KIND,)
    screen_kinds: tuple[str, ...] = (SCREEN_KIND,)
    window_kinds: tuple[str, ...] = (WINDOW_KIND,)
    mic_device_property = DEVICE_ID
    mic_default = DEVICE_DEFAULT
    screen_token_key: str | None = None
    needs_screen_token = False

    def app_audio_settings(
        self, process_name: str, bundle_id: str | None, exe_path: str | None
    ) -> dict[str, object]:
        """Application Audio Capture of the call app's process tree, matched by executable."""
        return {
            S_WINDOW: window_target(exe_name(process_name, exe_path)),
            S_PRIORITY: PRIORITY_EXE,
        }

    def window_settings(
        self, process_name: str, bundle_id: str | None, exe_path: str | None
    ) -> dict[str, object] | None:
        """Window capture (WGC) of a top-level window of the call app's executable.

        OBS picks the best-rated window of that executable; with several windows (e.g. a
        meeting window next to the main window) the pick is not guaranteed (PF-243). UWP apps
        whose frame belongs to ``ApplicationFrameHost.exe`` do not match by executable. The
        stage 07 real-app checks record both cases.
        """
        exe = exe_name(process_name, exe_path)
        if not exe:
            return None
        return {
            S_WINDOW: window_target(exe),
            S_PRIORITY: PRIORITY_EXE,
            S_METHOD: WINDOW_METHOD_WGC,
        }

    def screen_settings(self, token: str | None) -> dict[str, object] | None:
        """Display capture of the primary monitor (no approval token on Windows)."""
        return {S_MONITOR: primary_monitor_device()}


PROFILE = WindowsCaptureProfile()

# Properties whose item values are enums (safe to print in the facts dump).
FACT_VALUE_PROPERTIES = frozenset({S_PRIORITY, S_METHOD})
