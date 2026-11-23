# App coverage

Which calls and online meetings Établi Captation recognises, on which system, native and in the
browser (ADR-018). Recognition decides the **app ID** and the **mode**; it never bypasses the
consent guard: every recording still needs study mode, a countdown with *Cancel* and the visible
indicator.

**Anything else is recorded too.** An app that matches no rule but opens the microphone while
study mode is on is recorded as `unknown` (audio, video if the camera is on;
`detection.record_unknown_apps`, default `true`). A browser holding the microphone is recorded as
**video** even when the page cannot be identified, because most browser calls are meetings whose
page title carries no service name.

Legend: ✅ verified on that OS · ◐ rule from vendor documentation, confirm on the OS (`verify:
true` in `specs/rules.default.yaml`) · 🌐 browser only · – not available on that OS.

## Meetings → video

| Service | Linux | macOS | Windows | Browser (page title) |
|---|---|---|---|---|
| Zoom | ◐ `zoom` (deb, Flatpak `us.zoom.Zoom`) | ◐ `us.zoom.xos` | ◐ `Zoom.exe` | ◐ `zoom_web` (only titles naming Zoom; the in-call page shows the topic → `browser`, video) |
| Microsoft Teams | ◐ `teams-for-linux` (also Flatpak); no official client | ◐ `com.microsoft.teams2` (+ helper) | ◐ `ms-teams.exe`, package `MSTeams_8wekyb3d8bbwe` | ◐ `teams_web` (`… \| Microsoft Teams`) |
| Cisco Webex (App and Meetings) | ◐ `CiscoCollabHost` | ◐ `Cisco-Systems.Spark`, `com.webex.meetingmanager` | ◐ `CiscoCollabHost.exe`, `webexhost.exe`, `atmgr.exe` | ◐ `webex_web` |
| Cisco Meeting Server (CMS) | 🌐 | 🌐 | 🌐 | ◐ `cisco_meeting_web` (`Cisco Meeting Server web app`; a branded title via `extra_meeting_titles`) |
| BigBlueButton | 🌐 | 🌐 | 🌐 | ◐ `bbb` (`BigBlueButton - …`; a renamed instance via `extra_meeting_titles`) |
| Jitsi Meet | ◐ `jitsi-meet` (Flatpak `org.jitsi.jitsi-meet`) | ◐ `org.jitsi.jitsi-meet` | ◐ `Jitsi Meet.exe` | ◐ `jitsi_web` (`… \| Jitsi Meet`) |
| Google Meet | 🌐 | 🌐 | 🌐 | ◐ `google_meet` (meeting code or `Google Meet`; an in-call page showing the event name → `browser`, video) |
| GoTo Meeting | 🌐 | ◐ | ◐ `GoTo.exe` | ◐ `goto_web` |

## Calls → audio (video when the camera is on)

| Service | Linux | macOS | Windows | Browser |
|---|---|---|---|---|
| FaceTime | – | ◐ `apple_call` (`com.apple.avconferenced`, FaceTime) | – | ◐ `facetime_web` (join link in Chrome/Edge) |
| iPhone calls | – | ◐ `apple_call` (Continuity; Phone app from macOS 26) | ◐ `phone_link` (Phone Link, Bluetooth) | – |
| Cisco Jabber | – | ◐ `com.cisco.Jabber` | ◐ `CiscoJabber.exe` | – |
| Softphones (Zoiper, Linphone, MicroSIP, Bria, 3CX, GNOME Calls, baresip, Twinkle, Jami, Telephone, sipgate) | ◐ `softphone` | ◐ `softphone` | ◐ `softphone` | 3CX web app: `browser` |

## Messengers → audio (video when the camera is on)

| Service | Linux | macOS | Windows | Browser |
|---|---|---|---|---|
| WhatsApp | ◐ ZapZap / Karere / Whatsie by Flatpak ID; no official app | ◐ `net.whatsapp.WhatsApp` | ◐ package `5319275A.WhatsAppDesktop` | ◐ `whatsapp_web` (main window title; web calls are rolling out) |
| Signal | ◐ `signal-desktop` (deb, Flatpak `org.signal.Signal`) | ◐ `org.whispersystems.signal-desktop` | ◐ `Signal.exe` | – |
| Telegram | ◐ `telegram-desktop`, Flatpak | ◐ `ru.keepcoder.Telegram`, `com.tdesktop.Telegram` | ◐ `Telegram.exe` | – |
| Discord, Slack huddles, Element | ◐ binaries and Flatpak IDs | ◐ bundle IDs | ◐ exe / package | ◐ `discord_web`, `slack_web`, `element_web` |

## Browsers

| Browser | Linux | macOS | Windows | Page titles |
|---|---|---|---|---|
| Chrome | ✅ `chrome` (PF-018) | ◐ `com.google.Chrome.helper` | ◐ `chrome.exe` | Windows, macOS; Linux: none (PF-019) |
| Chromium | ✅ `chromium` (PF-024) | ◐ | – | as Chrome |
| Vivaldi | ✅ `vivaldi-bin` (PF-025) | ◐ | ◐ | as Chrome |
| Firefox (incl. ESR, LibreWolf, Zen, Floorp, Waterfox) | ✅ `firefox` (Snap, PF-026) | ◐ `org.mozilla.firefox` | ◐ `firefox.exe` | all OSes; **Linux: tab title from PipeWire (PF-026)** |
| Edge, Brave, Opera, Arc | ◐ | ◐ | ◐ | as Chrome |
| Safari | – | ◐ `com.apple.WebKit.GPU` | – | not attributed (L-27) |

## iPhone and iPad

The software runs on Linux, macOS and Windows only (L-29). For calls on an iPhone:

1. **Take the call on the Mac** (FaceTime, or the iPhone call relayed to the Mac via Continuity /
   the Phone app on macOS 26): recorded as `apple_call`.
2. **Join FaceTime from a computer browser** (FaceTime link): `facetime_web`.
3. **Mobile hardware** (Plaud Note Pro, viaim RecDot, …): `captation import`.
4. iOS 18.1+ call recording is not available in the EU (and not for WhatsApp, Signal or FaceTime
   video). Where it is available, an exported `.m4a` can be imported with
   `captation import --device generic`.

## Your own meeting pages

An institution's BigBlueButton, Jitsi or Cisco Meeting Server often has its own name in the page
title. Add a regex per instance to the configuration; a match records as `web_meeting`:

```yaml
detection:
  extra_meeting_titles:
    - '^Uni-Konferenz - '            # BigBlueButton clientTitle of the instance
    - '\| Videokonferenz Muster$'    # renamed Jitsi
```

Titles are normalised before matching (invisible characters removed, the browser's own suffix
such as " - Google Chrome" cut off). They are read in memory only and stored only with
`detection.store_window_titles: true`.

## Confirming the ◐ entries

On each OS, for each app in use: start a test call while study mode is armed and check
`captation status` (app ID and mode) and `captation doctor --probe --count 5` (process name,
bundle ID or package). Record the result in `docs/platform-facts.md` and remove `verify: true`
from the rule once every OS it names is confirmed. The macOS and Windows runbooks list the
checks per app.
