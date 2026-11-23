"""Windows capture profile: kinds and settings the controller sends to OBS."""

from __future__ import annotations

import sys

import pytest

from captation.obs import kinds, kinds_windows
from captation.obs.kinds_windows import PROFILE, exe_name, window_target

ZOOM = "C:\\Users\\alice\\AppData\\Roaming\\Zoom\\bin\\Zoom.exe"


def test_profile_is_found_by_os_name() -> None:
    profile = kinds.capture_profile("windows")
    assert profile is PROFILE and isinstance(profile, kinds.CaptureProfile)
    assert profile.os_name == "windows"
    assert profile.app_audio_kinds == ("wasapi_process_output_capture",)
    assert profile.desktop_audio_kinds == ("wasapi_output_capture",)
    assert profile.mic_kinds == ("wasapi_input_capture",)
    assert profile.window_kinds == ("window_capture",)
    assert profile.screen_kinds == ("monitor_capture",)
    assert (profile.mic_device_property, profile.mic_default) == ("device_id", "default")
    assert profile.needs_screen_token is False and profile.screen_token_key is None


@pytest.mark.parametrize(
    ("exe", "title", "cls", "expected"),
    [
        ("Zoom.exe", "", "", "::Zoom.exe"),
        (
            "Zoom.exe",
            "Zoom Meeting",
            "ZPContentViewWndClass",
            "Zoom Meeting:ZPContentViewWndClass:Zoom.exe",
        ),
        ("a:b#c.exe", "x:y", "#", "x#3Ay:#22:a#3Ab#22c.exe"),
    ],
)
def test_window_target_matches_obs_encoding(exe: str, title: str, cls: str, expected: str) -> None:
    assert window_target(exe, title, cls) == expected


def test_window_target_round_trips_through_obs_decoding() -> None:
    """OBS splits on ':' and then replaces '#3A' → ':' and '#22' → '#' (window-helpers.c)."""
    fields = ("a#3Ab:c", "#22", "x.exe")
    encoded = window_target(fields[2], fields[0], fields[1])
    parts = encoded.split(":")
    assert len(parts) == 3
    decoded = [p.replace("#3A", ":").replace("#22", "#") for p in parts]
    assert tuple(decoded) == (fields[0], fields[1], fields[2])


@pytest.mark.req("R-12")
def test_app_audio_targets_the_executable_only() -> None:
    settings = PROFILE.app_audio_settings("Zoom.exe", None, ZOOM)
    assert settings == {"window": "::Zoom.exe", "priority": 2}
    # no window title in settings that OBS persists (docs/principles.md §3.1)
    assert PROFILE.app_audio_settings("ms-teams.exe", None, None) == {
        "window": "::ms-teams.exe",
        "priority": 2,
    }


@pytest.mark.req("R-28")
def test_window_capture_uses_wgc_and_exe_priority() -> None:
    assert PROFILE.window_settings("Zoom.exe", None, ZOOM) == {
        "window": "::Zoom.exe",
        "priority": 2,
        "method": 2,
    }
    assert PROFILE.window_settings("", None, None) is None


def test_display_fallback_names_a_monitor() -> None:
    settings = PROFILE.screen_settings(None)
    assert settings is not None
    monitor = settings["monitor_id"]
    assert isinstance(monitor, str) and monitor.startswith("\\\\.\\DISPLAY")
    if sys.platform != "win32":
        assert monitor == kinds_windows.MONITOR_FALLBACK


def test_exe_name() -> None:
    assert exe_name("zoom", ZOOM) == "Zoom.exe"
    assert exe_name("chrome.exe", None) == "chrome.exe"
    assert exe_name("x", "D:/apps/x.exe") == "x.exe"


def test_fact_list_properties_cover_profile_kinds() -> None:
    props = kinds_windows.FACT_LIST_PROPERTIES
    assert set(props) == {
        "wasapi_process_output_capture",
        "wasapi_input_capture",
        "wasapi_output_capture",
        "window_capture",
        "monitor_capture",
    }
    assert props["wasapi_process_output_capture"] == ("window", "priority")


@pytest.mark.windows
def test_primary_monitor_device_on_windows() -> None:
    device = kinds_windows.primary_monitor_device()
    assert device.startswith("\\\\.\\DISPLAY")
