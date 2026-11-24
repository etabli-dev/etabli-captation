# Decisions (ADRs)

Format: context, options, licence (for dependencies), decision, consequences, verify-by.
Package facts (version, release date, licence) are from PyPI and Flathub on 2026-11-23.

Status values: **accepted** (settled; the code and the release rely on it), **provisional**
(default chosen, not yet confirmed by the maintainer), **open**. An accepted decision that still
has to be checked on another operating system says what is pending there.

---

## ADR-001 — Agent runtime

- **Context:** the agent is a long-running user-session process with OS probes, a tray, IPC,
  ffmpeg post-processing and an obs-websocket client.
- **Options:** Python 3.12 (default); Go; Rust.
- **Decision (accepted):** Python 3.12, `uv`, src layout. No hard blocker appeared: all OS APIs
  needed are reachable through subprocess tools (`pw-dump`), D-Bus or a small native helper
  (macOS probe, ADR-004).
- **Consequences:** packaging needs a freezer (ADR-005). Startup and memory are higher than Go/Rust.
- **Verify-by:** end-to-end metrics (CPU and RAM while idle-armed and recording).

## ADR-002 — obs-websocket client

- **Context:** the client must speak obs-websocket v5 with auth, events and timeouts. The licence
  gate (docs/principles.md §4.5) forbids GPL clients.
- **Options and licences:**

  | Option | Licence | Last release | Notes |
  |---|---|---|---|
  | In-house thin v5 client on `websockets` | `websockets`: BSD-3-Clause | 17.4, 2026-10-30 | Only the requests we use, typed |
  | `obs-websocket-py` | MIT | 1.0, 2023-02-20 | No release for almost 3 years |
  | `simpleobsws` | MIT (classifier) | 1.4.3, 2025-06-20 | asyncio only |
  | `obsws-python` | **GPL-3.0-only** | 1.8.1, 2026-10-31 | Fails the licence gate |
- **Decision (accepted):** in-house thin client (`captation/obs/client.py`) on the synchronous
  `websockets.sync.client` API, with a reader thread for events.
- **Consequences:** we implement the v5 handshake (auth = base64(sha256(base64(sha256(password +
  salt)) + challenge))) and request/response matching ourselves. Small and fully typed.
- **Verify-by:** integration tests (`pytest -m obs`) against OBS 33.

## ADR-003 — Tray and notifications

- **Context:** tray and notifications are mandatory (docs/principles.md §2.8). On GNOME (Wayland), only
  StatusNotifierItem (AppIndicator) trays work; XEmbed trays do not.
- **Options and licences:**

  | Option | Licence | Last release | Notes |
  |---|---|---|---|
  | `pystray` | LGPL-3.0 | 0.19.5, 2023-09-17 | On Linux needs PyGObject + AyatanaAppIndicator (system build deps); unmaintained since 2023 |
  | `desktop-notifier` | MIT | 6.2.0, 2025-08-08 | Notifications with actions on all three OSes |
  | PySide6 (Qt) | LGPL-3.0 / GPL | 6.12.0, 2026-10-08 | ~200 MB, LGPL relinking duties |
  | In-house D-Bus (StatusNotifierItem + com.canonical.dbusmenu + org.freedesktop.Notifications) on `dbus-fast` | MIT | 5.2.0, 2026-10-02 | Linux only, pure Python, no system build deps |
- **Decision (accepted for Linux; implemented for macOS and Windows, to be verified there):**
  - **Linux:** in-house D-Bus tray and notifications on `dbus-fast` (MIT). Tray availability is
    detected from `org.kde.StatusNotifierWatcher` (`IsStatusNotifierHostRegistered`).
  - **macOS and Windows:** `pystray` (LGPL-3.0) for the tray and `desktop-notifier` (MIT) for
    notifications, installed only on those platforms. To be verified on macOS and Windows with
    the tray smoke test of each runbook (PF-126, PF-250…PF-252). LGPL relinking is possible
    because `pystray` ships as a replaceable Python module (documented in `docs/licensing.md`).
- **Consequences:** Linux avoids GTK/PyGObject entirely. Two tray back ends behind one interface
  (`UiBackend` in `captation/ui/base.py`, selected through `captation/ui/tray.py`).
- **Verify-by:** tray smoke test per OS; end-to-end scenario E13.

## ADR-004 — macOS probe

- **Context:** per-process mic detection needs CoreAudio process objects (macOS 14+).
- **Options:** Swift CLI helper `captation-probe` (default); `pyobjc` (MIT).
- **Decision (accepted; verification on macOS pending):** Swift CLI helper. The macOS platform
  code (`captation/platform/macos.py`) relies on it; its identifiers are pending facts
  PF-113…PF-119.
- **Consequences:** a SwiftPM package in `probes/macos/`, codesigned with the app.
- **Verify-by:** detection checks on macOS (`docs/runbook-macos.md` §3, §7).

## ADR-005 — Packaging

- **Options and licences:**

  | Option | Licence | Last release | Notes |
  |---|---|---|---|
  | PyInstaller | GPL-2.0-or-later **with bootloader exception** | 6.22.3, 2026-09-12 | Build tool only; the exception allows any licence for built apps |
  | Briefcase | BSD-3-Clause | 0.4.7, 2026-10-27 | Native installers, heavier template system |
  | Nuitka | **AGPL-3.0-or-later** (4.x) | 4.3.5, 2026-11-08 | Rejected: fails the licence gate |
- **Decision (accepted):** PyInstaller one-folder builds. PyInstaller is a dev/build
  dependency only and is not a runtime dependency (the licence gate checks runtime
  dependencies). Linux ships a tarball with an install script. AppImage and Flatpak are deferred.
  The macOS `.app` and the Windows installer use the same approach and are still to be built and
  checked on those systems.
- **Verify-by:** packaging check per OS (packaged build starts, tray shows, `captation status`
  works): done on Linux, pending on macOS and Windows.

## ADR-006 — Minimum versions and Linux OBS distribution

- **Context:** OBS 33.0 dropped Ubuntu 24.04. The Linux reference system runs Ubuntu 26.04 LTS.
  Ubuntu's archive ships `obs-studio 32.1.0-0ubuntu3` (built by Ubuntu). Flathub ships the
  official OBS Project build `com.obsproject.Studio 33.0.3`, plus the extension
  `com.obsproject.Studio.Plugin.PipeWireAudioCapture 1.2.2`.
- **Options (Linux):** Ubuntu 26.04 `.deb` (32.1, distro build) vs Flatpak (33.0.3, upstream build).
- **Decision (accepted, 2026-11-23):** Linux uses the **Flatpak** (installed per user:
  `flatpak install --user flathub com.obsproject.Studio com.obsproject.Studio.Plugin.PipeWireAudioCapture`).
  A native `obs` on PATH is also supported. Minimum versions are set from the OBS facts
  (see ADR-009 and the Linux rows of `docs/platform-facts.md`): OBS ≥ 32.0 (opt-in crash
  upload setting exists; obs-websocket ≥ 5.3.0 for `SetRecordDirectory`). macOS ≥ 14
  *(to be verified on macOS)*. Windows 10 22H2 / 11 *(to be verified on Windows)*.
- **Consequences:** Flatpak changes the config path (`~/.var/app/com.obsproject.Studio/config/obs-studio`)
  and PIDs inside the sandbox are namespaced (the Linux probe resolves host PIDs, PF-001, PF-017).
- **Verify-by:** OBS setup and detection checks on Linux (done).

## ADR-007 — Windows in v1

- **Options:** full support vs best effort.
- **Decision (accepted, 2026-11-23):** full support. The Windows-specific checks run on a
  Windows PC (`docs/runbook-windows.md`).
- **Verify-by:** detection and end-to-end checks on Windows (`docs/runbook-windows.md` §4, §7).

## ADR-008 — Video capture default

- **Options:** `window_preferred` (privacy: only the meeting window) vs `display` (robust, but
  captures everything on screen).
- **Decision (accepted, 2026-11-23):** `window_preferred`. Where the window cannot be targeted, display
  capture is used and `session.json` gets a warning (docs/principles.md §3.6). On Linux Wayland, window
  targeting goes through the screencast portal and is not dynamic, so Linux uses the portal
  source with the warning `capture:display_fallback`. On macOS ScreenCaptureKit targets the
  meeting **application** (all its windows; for a browser meeting all windows of that browser);
  the owner accepted this instead of single-window capture (ADR-021).
- **Verify-by:** integration and detection checks on Linux (done), macOS and Windows (pending).

## ADR-012 — Websocket exposure

- **Context:** obs-websocket has no loopback-only bind option (only `--websocket_ipv4_only`).
- **Options:** firewall rule per OS (`require_firewall`) vs warn only; random port vs 4455.
- **Decision (accepted):** `require_firewall` is the default. Port 4455 stays the default
  (configurable): a fixed port keeps the firewall rule simple and auditable; the protection comes
  from auth plus the rule, not from obscurity.
  - Linux: a dedicated nftables table (`inet etabli_captation`) that drops TCP to the websocket
    port from every source address that is not loopback. It is loaded at boot by a oneshot
    systemd unit, so the rule is independent of ufw/iptables front ends. `--remove` deletes
    exactly the unit, the rule file and the table. Details: `docs/security.md`.
  - macOS: `pf` anchor; Windows: `New-NetFirewallRule` (prepared; pending verification,
    PF-123…PF-125 and PF-230…PF-232).
- **Verify-by:** `obs firewall --check` during the OBS setup on each OS; end-to-end scenario E12.

## ADR-009 — Audio-only recording strategy

- **Context:** audio calls must not produce a full-resolution video file (architecture §7).
- **Options:** (A) the profile records audio only (custom FFmpeg output); (B) minimal canvas,
  dummy video dropped in post-processing.
- **Measurements (Linux, OBS 33.0.3 Flatpak, PF-008):**
  - A: `RecType=FFmpeg`, `FFVEncoderId=0`, `FFAEncoder=pcm_s24le`, 3 mixes → the file still has
    an H.264 stream at canvas size (1280×720, 9.9 MB for 11 s). **Fails.**
  - B: `RecType=Standard`, MKV, `obs_x264`, `ffmpeg_pcm_s24le`, canvas 128×72 at 10 fps →
    `h264 128x72` + 3 × `pcm_s24le 48 kHz stereo`, plays, OBS 3.8 % of one core. **Passes.**
    1 fps also works, but the x264 lookahead delays the first packet and the stop by ~12 s.
  - Follow-up measurement: at 10 fps the stop takes 1.76 s because of the x264 lookahead flush,
    so the audio → video switch gap was 2.24 s. At 30 fps the stop takes 0.6 s, the gap is
    1.05–1.09 s, and OBS uses 4.6 % of one core.
- **Decision (accepted for Linux):** strategy **B**, 128×72 at 30 fps, audio `pcm_s24le`
  (first available entry of `codec_preference`). Post-processing writes `segNN_audio.mka`
  without the dummy video. Re-check A on macOS/Windows only if B fails there.
- **Measurements (macOS 27.0.1, Apple M1, OBS 33.0.3, PF-112, 2026-11-23):** B as bootstrapped
  (128×72 at 30 fps, `obs_x264`, 3 × `ffmpeg_pcm_s24le`, MKV) → `h264 128x72 30/1` + 3 ×
  `pcm_s24le 48000`; input setup 0.82 s, stop 0.66 s. OBS CPU while recording 13.5 % of one core
  (idle 2.5–3.3 %; Linux 3.8–4.6 %), with `sck_audio_capture` in desktop mode as remote source,
  so the < 5 % criterion **fails on macOS**. The extra load is most likely ScreenCaptureKit, not
  the 128×72 encode; `replayd` (PF-131) is not included. Video profile (1920×1080, VideoToolbox):
  14.6–24.2 %.
- **Decision (accepted for macOS by the owner, 2026-11-24):** strategy **B** as on Linux; the
  OBS CPU load of 13–15 % of one core while recording audio (video 15–24 %) is accepted on
  macOS. The < 5 % target stays for Linux and Windows. A and per-app SCK audio are not measured.
- **Verify-by:** integration tests, FFT track-mapping unit tests.

## ADR-010 — Mic release

- **Context:** OBS must not hold the microphone while idle (docs/principles.md §5.2).
- **Measurement (PF-013):** the Captation collection has no global audio devices; without
  Captation inputs OBS has no capture stream; creating `Captation Local` takes 5 ms; removing it
  releases the mic.
- **Decision (accepted for Linux):** create the Captation inputs at segment start and remove
  them at stop. The alternative (keep the input with no device) is unnecessary at 5 ms.
- **Measurement (macOS 27.0.1, OBS 33.0.3, PF-112/PF-113, 2026-11-23):** with OBS running and no
  Captation inputs `captation-probe --once` lists no mic user; after `prepare_segment` (creates
  `Captation Local` and the SCK remote input, 0.41–1.11 s) → `OBS`, `replayd`; after
  `teardown_segment` → none. The decision holds on macOS.
- **Verify-by:** end-to-end scenario E5 (60 s idle, armed, OBS running → no session).

## ADR-013 — OBS 33 has no `--disable-shutdown-check`

- **Context:** after a crash or kill, OBS 33 asks on the next start whether to run in Safe Mode,
  and Safe Mode disables WebSockets. The flag that used to skip this check does not exist in
  33.0.3 (PF-003).
- **Options:** (a) wait for the websocket after a relaunch and tell the user to choose
  *Run Normally*; (b) delete OBS's crash sentinel before relaunching (changes OBS-internal state
  outside the Captation profiles/collection, docs/principles.md §4.2).
- **Decision (accepted):** (a). Recovery relaunches OBS and waits up to 30 s for the
  websocket; if it does not come up, the segment stays `interrupted`, the session is finalised
  and a notification asks the user to confirm the OBS dialog. Measured in end-to-end scenario E7
  on Linux; to be re-checked on macOS and Windows.
- **Verify-by:** end-to-end scenario E7.

## ADR-014 — Tray indicator lost while armed

- **Context:** GNOME disables user extensions while the screen is locked, so the AppIndicator
  tray host disappears (observed on GNOME 50: `org.gnome.ScreenSaver.GetActive` → true,
  extension state INACTIVE). docs/principles.md §2.5/§2.8: never record without a visible indicator.
- **Decision (accepted, 2026-11-23):** arming is refused while no tray host exists. If the host
  disappears while armed but not recording, the agent disarms and notifies. If it disappears
  during a recording, the recording continues (participants consented and the call is live),
  every affected session gets the warning `ui:indicator_lost`, and the indicator is restored as
  soon as the host is back (the SNI item re-registers on `NameOwnerChanged`).
- **Verify-by:** unit test `test_indicator_loss`; end-to-end scenario E13.

## ADR-003 update (2026-11-23)

The macOS/Windows back end is implemented in `captation/ui/pystray_ui.py` (`pystray` tray,
`desktop-notifier` notifications) and used by `captation/ui/macos.py` and
`captation/ui/windows.py`. `pystray`, `Pillow` and `desktop-notifier` are runtime dependencies on
macOS and Windows only (platform markers in `pyproject.toml`). `captation/ui/base.py`
(`default_backend()`) loads the module of the current OS; if it cannot be loaded, the agent falls
back to `UnavailableUi`: `tray_available()` is false, so arming is refused (fail-safe). The back
end is still to be verified on macOS and Windows (PF-126, PF-250…PF-252).

## ADR-015 — macOS/Windows prepared on Linux, verified on their machines

- **Context:** macOS and Windows support is prepared on Linux; the last steps can only be done on
  a Mac and a Windows PC. docs/principles.md §6 requires every OS-specific identifier to be
  verified before code relies on it.
- **Decision (accepted, 2026-11-23):** macOS and Windows code (probe, autostart, firewall,
  disk encryption, OBS locator, tray, packaging) is written and unit-tested with fakes on Linux.
  Its identifiers get PF entries with status **`pending`** (macOS PF-101…199, Windows
  PF-201…299). `tools/check_facts.py` tolerates a pending fact only when it runs on a different
  OS; on the target OS (and with `--release`) every pending fact is an error. The runbooks for
  the Mac and the Windows PC (`docs/runbook-macos.md`, `docs/runbook-windows.md`) verify each
  fact and set it to `verified` (or refute and replace it); only then can that OS be released.
- **Consequences:** the hard rule keeps its effect where it matters (nothing ships on an OS with
  an unverified fact); CI on Linux stays green meanwhile. The release workflow runs
  `check_facts.py --release --os <os>` per platform: Linux is built on every release tag, macOS
  and Windows only once their own facts are verified (otherwise their build is skipped with a
  notice).
- **Verify-by:** OBS setup, detection, packaging and end-to-end checks in each runbook;
  `check_facts.py --release --os <os>` on each OS.

## ADR-016 — Decisions before the Linux release (2026-11-23)

Decided by the maintainer once the Linux build was complete.

| Topic | Decision |
|---|---|
| Project licence | **Proprietary, all rights reserved**; no licence headers; may be opened later |
| Storage | `data_root` on an encrypted volume (for example a folder on a LUKS-encrypted data partition); **`storage.require_disk_encryption: block`** is the product default, so arming is refused while `data_root` resolves to unencrypted storage (for example while the encrypted volume is not yet unlocked and mounted) |
| OBS start-up egress (L-19, PF-023) | **Accepted and documented**; no network block for OBS |
| Windows in v1 (ADR-007) | **Full support** |
| Video capture default (ADR-008) | **`window_preferred`** (meeting window; screen fallback with a warning) |
| Linux OBS distribution (ADR-006) | **Flatpak** (official OBS build) |
| Release plan | superseded by ADR-021 (macOS first); was: **v0.1.0 = Linux**, tagged after the Linux real-call checklist of the release checklist; macOS and Windows follow as v0.2.0 once their facts are verified |
| Transcription hook | **None preconfigured**; examples in docs/hooks.md |
| Tray lost during a recording (ADR-014) | **Continue with a warning**; no new recording until the indicator is back |
| Auto-disarm | **240 min** (default unchanged); superseded for the owner's use by ADR-021 (no limit, reminder every 4 h) |
| Project ID of the first deployment | **unassigned** until the study is registered |
| macOS target | superseded by ADR-021: **macOS 27** (the build Mac); was macOS 26 (per-process detection needs ≥ 14) |

## ADR-017 — Public, source-available repository; disclaimer and acceptance gate (2026-11-23)

- **Context:** the repository is made public, with a strict notice that any use needs prior
  approval and a thorough legal disclaimer that every user confirms with a checkbox before use.
  "Approval" covers *both*: use of the software needs the copyright holder's prior written
  approval, and every recording needs the prior, documented consent of all persons recorded
  (plus ethics and data-protection approval for research).
- **Decision (accepted, 2026-11-23):**
  - The repository is public; the licence stays proprietary ("source available": `LICENSE` grants
    no rights, use requires prior written approval).
  - `DISCLAIMER.md` (English and German, the German version prevails) holds the strict notice and
    the disclaimer: intended and prohibited use, the user's responsibility as controller under the
    GDPR, legal framework examples, what consent must cover, technical safeguards are not legal
    compliance, no legal advice, warranty and liability within German law, third-party software,
    acceptance, applicable law. It is not legal advice and is to be reviewed legally before
    anyone relies on it.
  - **Acceptance gate (R-59, principles §2.10):** `captation accept` shows the packaged text in a
    dialog whose checkbox unlocks only after scrolling to the end (or, without a display, as a
    terminal checkbox ticked with `x`). There is no flag and no non-interactive path. The record
    (text version, SHA-256, time, OS user name, method, app version) is stored in the user's
    state directory and bound to the text's hash, so a changed text needs a new acceptance. Without it the agent
    refuses to arm (`terms_not_accepted`), imports are refused and `doctor` reports an error.
    The record is the user's declaration, not copy protection.
  - The Linux install script and the Windows installer run `captation accept` after installation;
    the macOS runbook verifies the dialog.
  - The repository was published with a fresh history that starts with a single initial commit.
- **Consequences:** unit and integration tests run with the gate patched open (marker
  `terms_gate` keeps the real check); the end-to-end suite points `XDG_STATE_HOME` at its own
  directory, writes a simulated terminal-checkbox record (user `e2e-test`) there and never touches
  the user's acceptance. The product accepts only the two interactive methods.

## ADR-018 — App coverage: all call apps, native and in the browser (2026-11-23)

- **Context:** the owner requires recordings to work with all apps and with the browser versions
  of online meetings: FaceTime, phone calls, WhatsApp, Signal, Teams, BigBlueButton, Cisco
  Meeting Server, Cisco Webex, Zoom and others, also for calls that start on an iPhone. The rules
  covered a few apps; Vivaldi (`vivaldi-bin`, PF-025), Flatpak messengers and the macOS call
  daemon were missed, and the browser title rules were both too broad (`zoom` in any title) and
  blind to real in-call titles.
- **Decision (accepted):**
  - Default rules cover the apps in `docs/app-coverage.md` per OS by process name, macOS bundle
    ID, Windows package and (new) Linux Flatpak app ID (`package` also matches
    `pipewire.access.portal.app_id`). FaceTime and iPhone calls on the Mac are `apple_call`
    (microphone in `com.apple.avconferenced`); Windows Phone Link is `phone_link`; SIP/VoIP
    softphones are `softphone`.
  - Browser page titles are normalised (invisible characters, no-break spaces, the browser's own
    suffix) and matched by anchored rules per service, including Cisco Meeting Server's fixed
    title. On Linux, Firefox-family browsers supply the capturing tab's title through PipeWire
    (PF-026). `detection.extra_meeting_titles` adds an institution's own meeting pages.
  - **A browser holding the microphone is recorded as video** (`browser_meeting`) when the page
    cannot be identified: most browser calls are meetings whose in-call title names no service
    (Google Meet shows the event, Zoom the topic). On Windows/macOS the browser window is
    captured; on Linux the approved screen (L-25).
  - **`detection.record_unknown_apps` defaults to `true`**: any other app opening the microphone
    while study mode is on is recorded as `unknown` after the usual countdown (L-30).
  - iPhone/iPad cannot run the software; calls are taken on the Mac, FaceTime is joined from a
    browser, or mobile hardware is used with `captation import` (L-29).
  - Identifiers from documentation keep `verify: true` until confirmed on the real OS.
- **Consequences:** R-03, R-06 and R-60; `docs/app-coverage.md`; L-25…L-30; PF-024…PF-026.

## ADR-019 — Fail-closed recording control (2026-11-23)

- **Context:** a correctness audit found paths where the consent guard or the indicator could be
  bypassed: a countdown whose display failed still turned into a recording at its deadline; OBS
  kept recording without indicator when the agent died; an unconfirmed stop was treated as done;
  timers stopped when a poll failed; a browser tab switch ended the session; a refused start
  retried endlessly; post-processing blocked the event loop.
- **Decision (accepted):**
  - A countdown turns into a recording only after the UI confirmed it was shown
    (`CountdownShown`); without confirmation it is cancelled, the app is not offered again and
    study mode ends (`countdown_failed`).
  - A watchdog process (started by the agent, connected by a pipe) stops a Captation recording
    and removes the Captation inputs if the agent ends without shutting down. At start-up a
    leftover recording is stopped before stale sessions are finalised.
  - An unconfirmed stop is checked and retried; while OBS still records, the session stays in
    STOPPING (indicator on) and the user is told (`stop_failed`).
  - A failed poll counts as "no call", so auto-disarm and the stop debounce keep running.
  - A different app ID is debounced like a lost call; a different app ID of the same process (a
    tab or title change) continues the session.
  - OBS lost or stopping on its own: recovery at most three times per session, background
    reconnection while armed; a refused start finalises the session once as `failed`.
  - Post-processing and finalising run on worker threads; one lock serialises OBS access.
  - Tray arming without an answer in the consent-reference dialog does not arm; the acceptance
    is re-checked right before each countdown; the macOS/Windows tray counts as available only
    once the icon is shown.
- **Consequences:** R-09, R-17, R-53 and R-61; tests in `tests/unit/test_session_hardening.py`
  and `tests/unit/test_hardening_paths.py`.

## ADR-020 — The local track follows the call app's microphone (2026-11-24)

- **Context:** the owner uses different headsets (wired, USB, Bluetooth/AirPods) and switches
  them during calls. The local track recorded the system default input, so a call app set to
  another microphone, or a headset connected mid-call, gave a wrong or silent local track (L-08).
  The remote track is not affected: it is captured from the app (ScreenCaptureKit), before any
  output device.
- **Decision (accepted):** new `recording.audio.local_input` value `follow_call`, the default.
  On macOS `captation-probe` reports the input device each mic user records from (PF-132); OBS's
  `Captation Local` is created on that device and switched with `SetInputSettings` when the call
  app changes devices during the recording (same segment, no restart). Where no device is
  reported (Linux, Windows, macOS < 14) the default input is used, as before. A device OBS does
  not list falls back to the default input with `local:call_device_not_found`. The device UID
  stays in memory: it is not logged and not written to `session.json` (a Bluetooth UID can
  contain the device's address, principles §3).
- **Consequences:** R-13, L-04, L-08, PF-132; `follow_default` and device names keep working.
- **Verify-by:** unit tests (controller, agent, probe parser), probe `swift test`, live check on
  macOS against OBS 33.0.3.

## ADR-021 — Owner decisions for the macOS release (2026-11-24)

Decided by the owner after the macOS audit, for daily use on the build Mac.

| Topic | Decision |
|---|---|
| Study mode duration (§2.3) | **No time limit** (`consent.auto_disarm_after_minutes: null`, now the default): study mode stays on until it is ended, the agent stops or the user logs out. Never unnoticed: a reminder with *Studienmodus beenden* every `consent.armed_reminder_minutes` (240), only between calls; the reminder is mandatory without a limit (schema and model). A time limit (5–720 min) can still be set. Every recording keeps the countdown with *Abbrechen*, the red icon and the start reminder |
| App coverage | **All call apps and browser calls** (ADR-018), including FaceTime and iPhone calls on the Mac; no built-in browser |
| Browser tab titles on macOS | The agent gets **Screen & System Audio Recording** so the probe reads window titles (in memory only, §3.1): WhatsApp Web voice calls are recorded as audio, meeting pages as video, sessions name the service |
| Video scope on macOS | **Whole meeting app** (ScreenCaptureKit application capture), not single-window capture |
| Firewall (ADR-012) | **pf anchor** via a root LaunchDaemon (`captation obs firewall --apply`); no pf-based VPN on the Mac |
| Remote track of FaceTime/iPhone calls on the Mac | **Desktop-audio fallback accepted** (`remote:desktop_fallback`): the call audio runs in a system service that cannot be captured per app |
| Consent reference at arming | **Kept** (`ask_consent_ref_on_arm: true`) |
| Retention | **Never delete automatically** (`retention_days: null`) |
| Project ID | **`unassigned`** until the study is registered |
| OBS CPU on macOS (ADR-009) | **13–15 % accepted** |
| Headsets | Local track follows the call app's microphone (ADR-020) |
| Signing | **Ad-hoc** while only the owner uses the app; Developer ID and notarisation when it is handed to others |
| Release order | **macOS first:** v0.1.0 = macOS after the owner's real-call checks on the Mac; Linux follows after its real-call checklist |
| Reference system | **macOS 27** (the build Mac), minimum macOS 14; no fallback below 14 (P8 dropped) |

