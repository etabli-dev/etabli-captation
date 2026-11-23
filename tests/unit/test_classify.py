"""``classify()``: matching, priority, title rules, tie-breaks, modes, ignores (architecture §5)."""

from __future__ import annotations

from typing import Any

import pytest

from captation.models import AppRef, ProbeSnapshot
from captation.rules import Rules, classify, compile_rules
from captation.rulesfile import load_rules
from tests.unit.session_support import detection, ref

RULES = load_rules()
CFG = detection()

ZOOM = ref(4242, "zoom")
SIGNAL = ref(300, "signal-desktop")
WHATSAPP = ref(200, "WhatsApp")
TELEGRAM = ref(250, "telegram-desktop")
PHONE = ref(90, "avconferenced")
CHROME = ref(500, "chrome")
FOO = ref(777, "foo-voip")


def snap(
    *mic: AppRef,
    camera: bool = False,
    camera_users: tuple[AppRef, ...] = (),
    titles: dict[int, tuple[str, ...]] | None = None,
) -> ProbeSnapshot:
    return ProbeSnapshot(
        ts_monotonic=1.0,
        mic_users=tuple(mic),
        camera_active=camera,
        camera_users=camera_users,
        window_titles=titles,
    )


def run(s: ProbeSnapshot, current: str | None = None, **kw: Any) -> tuple[str, str, int, str, str]:
    cfg = detection(**kw.pop("cfg", {}))
    result = classify(s, kw.pop("rules", RULES), cfg, current, **kw)
    assert result is not None
    return result.app_id, result.category, result.pid, result.mode, result.reason


# ---- modes (§5.5) ---------------------------------------------------------------------------


@pytest.mark.req("R-01")
def test_meeting_app_is_video() -> None:
    assert run(snap(ZOOM)) == ("zoom", "meeting", 4242, "video", "meeting_app")
    teams_win = AppRef(pid=7, process_name="ms-teams.exe", package_id="MSTeams_8wekyb3d8bbwe")
    assert run(snap(teams_win))[:2] == ("teams", "meeting")
    teams_mac = AppRef(pid=8, process_name="Microsoft Teams", bundle_id="com.microsoft.teams2")
    assert run(snap(teams_mac))[:2] == ("teams", "meeting")


@pytest.mark.req("R-01")
def test_windows_package_family_name_matches() -> None:
    whatsapp_store = AppRef(
        pid=9, process_name="ApplicationFrameHost.exe", package_id="5319275A.WhatsAppDesktop_cv1g"
    )
    assert run(snap(whatsapp_store))[:2] == ("whatsapp", "messenger")


@pytest.mark.req("R-02")
def test_any_call_with_camera_is_video() -> None:
    assert run(snap(SIGNAL, camera=True)) == ("signal", "messenger", 300, "video", "camera_active")
    assert run(snap(PHONE, camera=True))[3:] == ("video", "camera_active")
    assert run(snap(CHROME, camera=True)) == ("browser", "browser", 500, "video", "camera_active")


@pytest.mark.req("R-03")
def test_other_calls_are_audio() -> None:
    assert run(snap(SIGNAL)) == ("signal", "messenger", 300, "audio", "default")
    assert run(snap(PHONE)) == ("phone_relay", "call", 90, "audio", "default")
    assert run(snap(CHROME)) == ("browser", "browser", 500, "audio", "default")


def test_first_matching_mode_wins_and_fallback_is_audio() -> None:
    raw = dict(RULES.raw)
    raw["modes"] = [
        {"when": {"category": "messenger", "camera_active": True}, "mode": "video", "reason": "x"},
        {"when": {"category": "messenger"}, "mode": "audio", "reason": "y"},
        {"when": {"camera_active": True}, "mode": "video", "reason": "z"},
    ]
    rules = compile_rules(raw)
    assert run(snap(SIGNAL, camera=True), rules=rules)[3:] == ("video", "x")
    assert run(snap(SIGNAL), rules=rules)[3:] == ("audio", "y")
    assert run(snap(PHONE, camera=True), rules=rules)[3:] == ("video", "z")
    assert run(snap(PHONE), rules=rules)[3:] == ("audio", "default")  # no entry matches


# ---- priority and title rules (§5.2) --------------------------------------------------------


@pytest.mark.req("R-04")
@pytest.mark.parametrize(
    ("mic", "winner"),
    [
        ((SIGNAL, ZOOM), "zoom"),  # meeting > messenger
        ((WHATSAPP, PHONE), "phone_relay"),  # call > messenger
        ((CHROME, SIGNAL), "signal"),  # messenger > browser
        ((PHONE, ZOOM, CHROME, SIGNAL), "zoom"),
    ],
)
def test_category_priority(mic: tuple[AppRef, ...], winner: str) -> None:
    assert run(snap(*mic))[0] == winner
    assert run(snap(*reversed(mic)))[0] == winner


@pytest.mark.req("R-04")
def test_browser_title_rules_upgrade_category_and_set_app_id() -> None:
    titles = {500: ("Inbox - Mail", "Meet – abc-defg-hij")}
    assert run(snap(CHROME, SIGNAL, titles=titles)) == (
        "google_meet",
        "meeting",
        500,
        "video",
        "meeting_app",
    )
    wa = {500: ("WhatsApp",)}
    assert run(snap(CHROME, titles=wa))[:2] == ("whatsapp_web", "messenger")
    # Same category: rule order of the *browser* rule decides, and it comes after signal.
    assert run(snap(CHROME, SIGNAL, titles=wa))[0] == "signal"


@pytest.mark.req("R-04")
def test_title_rules_need_titles_for_that_pid() -> None:
    assert run(snap(CHROME, titles=None))[:2] == ("browser", "browser")
    assert run(snap(CHROME, titles={}))[:2] == ("browser", "browser")
    assert run(snap(CHROME, titles={999: ("Google Meet",)}))[:2] == ("browser", "browser")
    assert run(snap(CHROME, titles={500: ("Some article",)}))[:2] == ("browser", "browser")


@pytest.mark.req("R-04", "R-05")
def test_best_title_match_is_independent_of_window_order() -> None:
    a = run(snap(CHROME, titles={500: ("WhatsApp", "Zoom Meeting", "Google Meet")}))
    b = run(snap(CHROME, titles={500: ("Google Meet", "WhatsApp", "Zoom Meeting")}))
    assert a == b
    assert a[:2] == ("google_meet", "meeting")  # meeting beats messenger, then rule order


def _custom_rules() -> Rules:
    return compile_rules(
        {
            "apps": [
                {
                    "id": "confapp",
                    "category": "meeting",
                    "match": {"process": ["^confapp$"]},
                    "title_rules": [{"pattern": "chat", "category": "messenger"}],
                },
                {
                    "id": "webapp",
                    "category": "browser",
                    "match": {"process": ["^webapp$"]},
                    "title_rules": [{"pattern": "call", "category": "call"}],
                },
            ],
            "modes": [{"when": {}, "mode": "audio", "reason": "default"}],
        }
    )


@pytest.mark.req("R-04")
def test_title_rule_never_lowers_category_and_keeps_rule_id_without_app_id() -> None:
    rules = _custom_rules()
    conf = ref(10, "confapp")
    web = ref(11, "webapp")
    assert run(snap(conf, titles={10: ("team chat",)}), rules=rules)[:2] == ("confapp", "meeting")
    assert run(snap(web, titles={11: ("video call",)}), rules=rules)[:2] == ("webapp", "call")


# ---- tie-breaks (§5.4) ----------------------------------------------------------------------


@pytest.mark.req("R-05")
def test_tie_break_a_current_app_wins() -> None:
    both = snap(WHATSAPP, SIGNAL, camera=True, camera_users=(SIGNAL,))
    assert run(both, current="whatsapp")[0] == "whatsapp"  # (a) beats (b)
    assert run(snap(WHATSAPP, SIGNAL), current="signal")[0] == "signal"  # (a) beats (c)
    assert run(snap(WHATSAPP, SIGNAL), current="zoom")[0] == "whatsapp"  # not a candidate


@pytest.mark.req("R-05")
def test_tie_break_a_never_beats_priority() -> None:
    assert run(snap(SIGNAL, ZOOM), current="signal")[0] == "zoom"


@pytest.mark.req("R-05")
def test_tie_break_b_camera_user_wins() -> None:
    s = snap(WHATSAPP, SIGNAL, camera=True, camera_users=(SIGNAL,))
    assert run(s)[0] == "signal"  # (b) beats (c)


@pytest.mark.req("R-05")
def test_tie_break_c_rule_order() -> None:
    assert run(snap(TELEGRAM, SIGNAL, WHATSAPP))[0] == "whatsapp"
    assert run(snap(ref(1, "telegram"), ref(999, "signal")))[0] == "signal"  # (c) beats (d)


@pytest.mark.req("R-05")
def test_tie_break_d_lowest_pid() -> None:
    a, b = ref(310, "signal"), ref(305, "Signal.exe")
    assert run(snap(a, b))[2] == 305
    assert run(snap(b, a))[2] == 305


@pytest.mark.req("R-05")
def test_same_pid_reported_twice_is_order_independent() -> None:
    a = AppRef(pid=5, process_name="signal", exe_path="/usr/bin/signal")
    b = AppRef(pid=5, process_name="signal", exe_path=None)
    ra = classify(snap(a, b), RULES, CFG, None)
    rb = classify(snap(b, a), RULES, CFG, None)
    assert ra == rb
    assert ra is not None and ra.capture_target == a


# ---- unknown apps (§5.3) --------------------------------------------------------------------


@pytest.mark.req("R-06")
def test_unknown_apps_only_when_enabled() -> None:
    assert classify(snap(FOO), RULES, CFG, None) is None
    on = {"record_unknown_apps": True}
    assert run(snap(FOO), cfg=on) == ("unknown", "unknown", 777, "audio", "default")
    assert run(snap(FOO, camera=True), cfg=on)[3:] == ("video", "camera_active")
    assert run(snap(FOO, CHROME), cfg=on)[0] == "browser"  # every known category ranks higher


# ---- browser helpers (§3) -------------------------------------------------------------------


@pytest.mark.req("R-07")
@pytest.mark.parametrize(
    "helper",
    [
        AppRef(pid=501, process_name="Google Chrome Helper (Renderer)", parent_app="Google Chrome"),
        AppRef(pid=502, process_name="xyz", bundle_id="com.google.Chrome.helper.renderer"),
        AppRef(pid=503, process_name="Isolated Web Content", parent_app="Safari"),
        AppRef(
            pid=504,
            process_name="chrome.exe",
            exe_path="C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
        ),
        AppRef(pid=505, process_name="/usr/lib/firefox/firefox-bin"),
    ],
    ids=["chrome-helper-parent", "chrome-helper-bundle", "safari-webcontent", "windows", "path"],
)
def test_browser_helpers_count_as_their_browser(helper: AppRef) -> None:
    assert run(snap(helper))[:2] == ("browser", "browser")


# ---- privacy (C§3.1) ------------------------------------------------------------------------


@pytest.mark.req("R-23")
def test_window_title_is_returned_only_if_storing_is_enabled() -> None:
    s = snap(CHROME, titles={500: ("Google Meet – Jane Doe",)})
    off = classify(s, RULES, CFG, None)
    on = classify(s, RULES, detection(store_window_titles=True), None)
    assert off is not None and off.window_title is None and off.app_id == "google_meet"
    assert on is not None and on.window_title == "Google Meet – Jane Doe"


# ---- self-trigger (C§5.1) -------------------------------------------------------------------


@pytest.mark.req("R-35")
@pytest.mark.parametrize(
    "own",
    [
        ref(1, "obs"),
        ref(2, "obs64.exe"),
        ref(3, "obs-ffmpeg-mux"),
        ref(4, "obs-browser-page"),
        ref(5, "captation"),
        ref(6, "captation-probe"),
        AppRef(pid=7, process_name="OBS", bundle_id="com.obsproject.obs-studio"),
        AppRef(pid=8, process_name="whatever", bundle_id="de.etabli.captation.agent"),
        AppRef(pid=9, process_name="chrome", parent_app="obs"),  # OBS's embedded browser
    ],
    ids=lambda a: f"{a.process_name}-{a.pid}",
)
def test_obs_and_the_agent_never_count(own: AppRef) -> None:
    assert classify(snap(own), RULES, detection(record_unknown_apps=True), None) is None
    assert run(snap(own, SIGNAL), cfg={"record_unknown_apps": True})[0] == "signal"


@pytest.mark.req("R-35")
def test_excluded_pids_never_count() -> None:
    cfg = detection(record_unknown_apps=True)
    assert classify(snap(SIGNAL), RULES, cfg, None, exclude_pids=frozenset({300})) is None
    r = classify(snap(SIGNAL, WHATSAPP), RULES, cfg, None, exclude_pids=frozenset({200}))
    assert r is not None and r.app_id == "signal"


def test_no_mic_users_means_no_call() -> None:
    assert classify(snap(), RULES, CFG, None) is None
    assert classify(snap(camera=True, camera_users=(ZOOM,)), RULES, CFG, None) is None


@pytest.mark.req("R-05")
def test_capture_target_and_pid_are_the_chosen_process() -> None:
    r = classify(snap(WHATSAPP, ZOOM), RULES, CFG, None)
    assert r is not None
    assert r.capture_target == ZOOM and r.pid == ZOOM.pid
