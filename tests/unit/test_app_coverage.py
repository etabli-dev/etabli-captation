"""App coverage of the default rules (docs/app-coverage.md, ADR-018).

Identifiers come from the platform facts (verified on Linux) and from vendor documentation for
the other OSes (``verify: true`` in the rules until confirmed on the real OS).
"""

from __future__ import annotations

import pytest
import yaml

from captation.config import DetectionConfig, data_text, parse_config
from captation.models import AppRef, ProbeSnapshot
from captation.rules import classify, normalise_title
from captation.rulesfile import BUILTIN_DEFAULT, load_rules
from tests.unit.session_support import detection

RULES = load_rules(BUILTIN_DEFAULT)
CFG = detection(record_unknown_apps=True)


def verdict(
    ref: AppRef, titles: tuple[str, ...] = (), cfg: DetectionConfig | None = None
) -> tuple[str, str, str]:
    snap = ProbeSnapshot(
        ts_monotonic=0.0,
        mic_users=(ref,),
        camera_active=False,
        window_titles={ref.pid: titles} if titles else None,
    )
    ctx = classify(snap, RULES, cfg or CFG, None)
    assert ctx is not None
    return ctx.app_id, ctx.category, ctx.mode


NATIVE = [
    # (process name, bundle ID, package / Flatpak ID) -> app id
    (AppRef(1, "zoom"), "zoom"),
    (AppRef(1, "zoom.us", bundle_id="us.zoom.xos"), "zoom"),
    (AppRef(1, "Zoom.exe"), "zoom"),
    (AppRef(1, "zoom", package_id="us.zoom.Zoom"), "zoom"),
    (AppRef(1, "ms-teams.exe", package_id="MSTeams_8wekyb3d8bbwe"), "teams"),
    (AppRef(1, "MSTeams", bundle_id="com.microsoft.teams2"), "teams"),
    (AppRef(1, "Microsoft Teams WebView Helper", bundle_id="com.microsoft.teams2.helper"), "teams"),
    (AppRef(1, "teams-for-linux"), "teams"),
    (AppRef(1, "electron", package_id="com.github.IsmaelMartinez.teams_for_linux"), "teams"),
    (AppRef(1, "CiscoCollabHost.exe"), "webex"),
    (AppRef(1, "webexhost.exe"), "webex"),
    (AppRef(1, "atmgr.exe"), "webex"),
    (AppRef(1, "Webex", bundle_id="Cisco-Systems.Spark"), "webex"),
    (AppRef(1, "Meeting Center", bundle_id="com.webex.meetingmanager"), "webex"),
    (AppRef(1, "jitsi-meet"), "jitsi"),
    (AppRef(1, "Jitsi Meet", bundle_id="org.jitsi.jitsi-meet"), "jitsi"),
    (AppRef(1, "GoTo.exe"), "goto"),
    (AppRef(1, "avconferenced", bundle_id="com.apple.avconferenced"), "apple_call"),
    (AppRef(1, "FaceTime", bundle_id="com.apple.FaceTime"), "apple_call"),
    (AppRef(1, "Phone", bundle_id="com.apple.mobilephone"), "apple_call"),
    (AppRef(1, "CiscoJabber.exe"), "cisco_jabber"),
    (AppRef(1, "Cisco Jabber", bundle_id="com.cisco.Jabber"), "cisco_jabber"),
    (
        AppRef(1, "PhoneExperienceHost.exe", package_id="Microsoft.YourPhone_8wekyb3d8bbwe"),
        "phone_link",
    ),
    (AppRef(1, "zoiper"), "softphone"),
    (AppRef(1, "linphone"), "softphone"),
    (AppRef(1, "microsip.exe"), "softphone"),
    (AppRef(1, "gnome-calls", package_id="org.gnome.Calls"), "softphone"),
    (AppRef(1, "WhatsApp", bundle_id="net.whatsapp.WhatsApp"), "whatsapp"),
    (
        AppRef(1, "WhatsApp.Root.exe", package_id="5319275A.WhatsAppDesktop_cv1g1gvanyjgm"),
        "whatsapp",
    ),
    (AppRef(1, "python3.13", package_id="com.rtosta.zapzap"), "whatsapp"),
    (AppRef(1, "QtWebEngineProcess", package_id="com.ktechpit.whatsie"), "whatsapp"),
    (AppRef(1, "signal-desktop"), "signal"),
    (AppRef(1, "signal-desktop", package_id="org.signal.Signal"), "signal"),
    (AppRef(1, "Signal.exe"), "signal"),
    (AppRef(1, "Signal Helper", bundle_id="org.whispersystems.signal-desktop.helper"), "signal"),
    (AppRef(1, "Telegram", package_id="org.telegram.desktop"), "telegram"),
    (AppRef(1, "Discord"), "discord"),
    (AppRef(1, "slack"), "slack"),
    (AppRef(1, "element-desktop"), "element"),
]


@pytest.mark.req("R-60")
@pytest.mark.parametrize(
    ("ref", "app_id"), NATIVE, ids=[f"{r.process_name}-{a}" for r, a in NATIVE]
)
def test_native_apps_are_recognised(ref: AppRef, app_id: str) -> None:
    assert verdict(ref)[0] == app_id


BROWSERS = [
    AppRef(1, "chrome"),  # PF-018
    AppRef(1, "chromium"),  # PF-024
    AppRef(1, "vivaldi-bin"),  # PF-025
    AppRef(1, "firefox"),  # PF-026 (Snap)
    AppRef(1, "firefox-bin", package_id="org.mozilla.firefox"),
    AppRef(1, "firefox-esr"),
    AppRef(1, "librewolf"),
    AppRef(1, "zen-bin"),
    AppRef(1, "msedge.exe"),
    AppRef(1, "brave.exe"),
    AppRef(1, "opera.exe"),
    AppRef(1, "Google Chrome Helper", bundle_id="com.google.Chrome.helper"),
    AppRef(1, "Microsoft Edge Helper", bundle_id="com.microsoft.edgemac.helper"),
    AppRef(1, "Vivaldi Helper", bundle_id="com.vivaldi.Vivaldi.helper"),
    AppRef(1, "Arc Helper", bundle_id="company.thebrowser.Browser.helper"),
    AppRef(1, "Safari Graphics and Media", bundle_id="com.apple.WebKit.GPU"),
]


@pytest.mark.req("R-60")
@pytest.mark.parametrize("ref", BROWSERS, ids=[r.bundle_id or r.process_name for r in BROWSERS])
def test_browsers_are_recognised_and_recorded_as_video(ref: AppRef) -> None:
    # Without an identifying title a browser call is taken as a meeting (ADR-018).
    assert verdict(ref) == ("browser", "browser", "video")


TITLES = [
    ("Cisco Meeting Server web app", "cisco_meeting_web", "meeting"),
    ("Cisco Meeting Server Web App - Google Chrome", "cisco_meeting_web", "meeting"),
    ("Projektbesprechung | Microsoft Teams", "teams_web", "meeting"),
    ("Microsoft Teams meeting | Microsoft Teams - Work - Microsoft​ Edge", "teams_web", "meeting"),
    ("Meet – abc-defg-hij", "google_meet", "meeting"),
    ("Google Meet – abc-defg-hij - Google Chrome", "google_meet", "meeting"),
    ("abc-defg-hij", "google_meet", "meeting"),
    ("BigBlueButton - Seminar 3 - Präsentation", "bbb", "meeting"),
    ("Arbeitsgruppe | Jitsi Meet — Mozilla Firefox", "jitsi_web", "meeting"),
    ("Webex", "webex_web", "meeting"),
    ("Zoom", "zoom_web", "meeting"),
    ("FaceTime", "facetime_web", "call"),
    ("(3) WhatsApp", "whatsapp_web", "messenger"),
    ("Discord | #allgemein | Server", "discord_web", "messenger"),
    ("Allgemein - Firma - Slack", "slack_web", "messenger"),
    ("Element [2] | Raum", "element_web", "messenger"),
]


@pytest.mark.req("R-60")
@pytest.mark.parametrize(("title", "app_id", "category"), TITLES)
def test_meeting_page_titles(title: str, app_id: str, category: str) -> None:
    assert verdict(AppRef(1, "chrome"), (title,))[:2] == (app_id, category)


NOT_MEETINGS = [
    "Zoom lens buying guide - Google Chrome",
    "How to zoom in on a PDF",
    "Download Microsoft Teams for desktop",
    "Microsoft Teams – Wikipedia",
    "WhatsApp privacy policy explained",
    "Meet the team - Company",
    "jitsi-meet/README.md at master · jitsi/jitsi-meet · GitHub",
    "BigBlueButton documentation",
]


@pytest.mark.req("R-60")
@pytest.mark.parametrize("title", NOT_MEETINGS)
def test_ordinary_pages_do_not_upgrade_the_browser(title: str) -> None:
    assert verdict(AppRef(1, "chrome"), (title,))[:2] == ("browser", "browser")


def test_title_normalisation() -> None:
    assert normalise_title("Meeting | Microsoft Teams - Work - Microsoft​ Edge") == (
        "Meeting | Microsoft Teams"
    )
    assert normalise_title("Room | Jitsi Meet — Mozilla Firefox") == "Room | Jitsi Meet"
    assert normalise_title("Plain") == "Plain"


@pytest.mark.req("R-60")
def test_extra_meeting_titles_for_own_instances() -> None:
    cfg = CFG.model_copy(update={"extra_meeting_titles": [r"^Uni-Konferenz - "]})
    title = ("Uni-Konferenz - Lehrstuhl - Folien - Google Chrome",)
    assert verdict(AppRef(1, "chrome"), title, cfg)[:2] == ("web_meeting", "meeting")
    assert verdict(AppRef(1, "chrome"), title)[:2] == ("browser", "browser")


@pytest.mark.req("R-06")
def test_unknown_apps_are_recorded_by_default() -> None:
    default = parse_config(yaml.safe_load(data_text("config.default.yaml")))
    assert default.detection.record_unknown_apps is True
    assert verdict(AppRef(1, "some-new-voip-app")) == ("unknown", "unknown", "audio")
