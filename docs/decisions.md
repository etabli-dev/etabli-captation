# Decisions (ADRs)

Format: context, options, licence (for dependencies), decision, consequences, verify-by.
Package facts (version, release date, licence) are from PyPI and Flathub on 2026-11-23.

Status values: **accepted** (settled), **provisional** (default chosen during the Linux build
run of 2026-11-23, waiting for the owner's confirmation), **open**.

---

## ADR-001 — Agent runtime

- **Context:** the agent is a long-running user-session process with OS probes, a tray, IPC,
  ffmpeg post-processing and an obs-websocket client.
- **Options:** Python 3.12 (default); Go; Rust.
- **Decision (accepted):** Python 3.12, `uv`, src layout. No hard blocker appeared: all OS APIs
  needed are reachable through subprocess tools (`pw-dump`), D-Bus or a small native helper
  (macOS probe, ADR-004).
- **Consequences:** packaging needs a freezer (ADR-005). Startup and memory are higher than Go/Rust.
- **Verify-by:** stage 12 metrics (CPU and RAM while idle-armed and recording).

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
- **Verify-by:** stage 04 integration tests against OBS 33.

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
- **Decision (provisional):**
  - **Linux:** in-house D-Bus tray and notifications on `dbus-fast` (MIT). Tray availability is
    detected from `org.kde.StatusNotifierWatcher` (`IsStatusNotifierHostRegistered`).
  - **macOS and Windows:** `pystray` (LGPL-3.0) for the tray and `desktop-notifier` (MIT) for
    notifications, installed only on those platforms. To be confirmed in stages 06/07 and the
    stage 09 smoke test on those OSes. LGPL relinking is possible because `pystray` ships as a
    replaceable Python module (documented in `docs/licensing.md`).
- **Consequences:** Linux avoids GTK/PyGObject entirely. Two tray back ends behind one interface
  (`captation/ui/tray.py`).
- **Verify-by:** stage 09 smoke test per OS, stage 12 E13.

## ADR-004 — macOS probe

- **Context:** per-process mic detection needs CoreAudio process objects (macOS 14+).
- **Options:** Swift CLI helper `captation-probe` (default); `pyobjc` (MIT).
- **Decision (provisional):** Swift CLI, as specified in stage 06.
- **Consequences:** a SwiftPM package in `probes/macos/`, codesigned with the app.
- **Verify-by:** stage 06 (needs macOS).

## ADR-005 — Packaging

- **Options and licences:**

  | Option | Licence | Last release | Notes |
  |---|---|---|---|
  | PyInstaller | GPL-2.0-or-later **with bootloader exception** | 6.22.3, 2026-09-12 | Build tool only; the exception allows any licence for built apps |
  | Briefcase | BSD-3-Clause | 0.4.7, 2026-10-27 | Native installers, heavier template system |
  | Nuitka | **AGPL-3.0-or-later** (4.x) | 4.3.5, 2026-11-08 | Rejected: fails the licence gate |
- **Decision (provisional):** PyInstaller one-folder builds. PyInstaller is a dev/build
  dependency only and is not a runtime dependency (the licence gate checks runtime
  dependencies). Linux ships a tarball with an install script. AppImage and Flatpak are deferred.
- **Verify-by:** stage 11 (packaged build starts, tray shows, `captation status` works).

## ADR-006 — Minimum versions and Linux OBS distribution

- **Context:** OBS 33.0 dropped Ubuntu 24.04. The build machine runs Ubuntu 26.04 LTS.
  Ubuntu's archive ships `obs-studio 32.1.0-0ubuntu3` (built by Ubuntu). Flathub ships the
  official OBS Project build `com.obsproject.Studio 33.0.3`, plus the extension
  `com.obsproject.Studio.Plugin.PipeWireAudioCapture 1.2.2`.
- **Options (Linux):** Ubuntu 26.04 `.deb` (32.1, distro build) vs Flatpak (33.0.3, upstream build).
- **Decision (accepted, owner 2026-11-23):** Linux uses the **Flatpak** (installed per user:
  `flatpak install --user flathub com.obsproject.Studio com.obsproject.Studio.Plugin.PipeWireAudioCapture`).
  A native `obs` on PATH is also supported. Minimum versions are set from the stage 03 facts
  (see ADR-009 and the stage 03 section of `docs/platform-facts.md`): OBS ≥ 32.0 (opt-in crash
  upload setting exists; obs-websocket ≥ 5.3.0 for `SetRecordDirectory`). macOS ≥ 14
  *(verify, stage 06)*. Windows 10 22H2 / 11 *(verify, stage 07)*.
- **Consequences:** Flatpak changes the config path (`~/.var/app/com.obsproject.Studio/config/obs-studio`)
  and PIDs inside the sandbox are namespaced (stage 05 resolves host PIDs).
- **Verify-by:** stages 03 and 05 on Linux.

## ADR-007 — Windows in v1

- **Options:** full support vs best effort.
- **Decision (accepted, owner 2026-11-23):** full support, as the default. Stage 07 runs on Windows.
- **Verify-by:** stage 07, stage 12 on Windows.

## ADR-008 — Video capture default

- **Options:** `window_preferred` (privacy: only the meeting window) vs `display` (robust, but
  captures everything on screen).
- **Decision (accepted, owner 2026-11-23):** `window_preferred`. Where the window cannot be targeted, display
  capture is used and `session.json` gets a warning (docs/principles.md §3.6). On Linux Wayland, window
  targeting goes through the screencast portal and is not dynamic, so Linux uses the portal
  source with the warning `capture:display_fallback` (stage 05).
- **Verify-by:** stages 04/05 (Linux), 06, 07.

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
  - macOS: `pf` anchor; Windows: `New-NetFirewallRule` (stages 06/07).
- **Verify-by:** stage 03 (`obs firewall --check`), stage 12 E12.

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
  - Stage 04 follow-up: at 10 fps the stop takes 1.76 s because of the x264 lookahead flush,
    so the audio → video switch gap was 2.24 s. At 30 fps the stop takes 0.6 s, the gap is
    1.05–1.09 s, and OBS uses 4.6 % of one core.
- **Decision (accepted for Linux):** strategy **B**, 128×72 at 30 fps, audio `pcm_s24le`
  (first available entry of `codec_preference`). Post-processing writes `segNN_audio.mka`
  without the dummy video. Re-check A on macOS/Windows only if B fails there.
- **Verify-by:** stage 04 integration tests, stage 10 FFT mapping tests.

## ADR-010 — Mic release

- **Context:** OBS must not hold the microphone while idle (docs/principles.md §5.2).
- **Measurement (PF-013):** the Captation collection has no global audio devices; without
  Captation inputs OBS has no capture stream; creating `Captation Local` takes 5 ms; removing it
  releases the mic.
- **Decision (accepted for Linux):** create the Captation inputs at segment start and remove
  them at stop. The alternative (keep the input with no device) is unnecessary at 5 ms.
- **Verify-by:** stage 12 E5 (60 s idle, armed, OBS running → no session).

## ADR-013 — OBS 33 has no `--disable-shutdown-check`

- **Context:** after a crash or kill, OBS 33 asks on the next start whether to run in Safe Mode,
  and Safe Mode disables WebSockets. The flag that used to skip this check does not exist in
  33.0.3 (PF-003).
- **Options:** (a) wait for the websocket after a relaunch and tell the user to choose
  *Run Normally*; (b) delete OBS's crash sentinel before relaunching (changes OBS-internal state
  outside the Captation profiles/collection, docs/principles.md §4.2).
- **Decision (provisional):** (a). Recovery relaunches OBS and waits up to 30 s for the
  websocket; if it does not come up, the segment stays `interrupted`, the session is finalised
  and a notification asks the user to confirm the OBS dialog. Measured in stage 12 (E7).
- **Verify-by:** stage 12 E7.

## ADR-014 — Tray indicator lost while armed

- **Context:** GNOME disables user extensions while the screen is locked, so the AppIndicator
  tray host disappears (observed on the build machine: `org.gnome.ScreenSaver.GetActive` → true,
  extension state INACTIVE). docs/principles.md §2.5/§2.8: never record without a visible indicator.
- **Decision (accepted, owner 2026-11-23):** arming is refused while no tray host exists. If the host
  disappears while armed but not recording, the agent disarms and notifies. If it disappears
  during a recording, the recording continues (participants consented and the call is live),
  every affected session gets the warning `ui:indicator_lost`, and the indicator is restored as
  soon as the host is back (the SNI item re-registers on `NameOwnerChanged`).
- **Verify-by:** unit test `test_indicator_loss`; stage 12 E13.

## ADR-003 update (2026-11-23)

The provisional macOS/Windows back ends (`pystray`, `desktop-notifier`) were removed from the
dependencies until stages 06/07 verify them on those systems. Until then, macOS and Windows use
`UnavailableUi`: `tray_available()` is false, so arming is always refused there (fail-safe).

## ADR-015 — macOS/Windows prepared on Linux, verified on their machines

- **Context:** owner instruction 2026-11-23: "Make all steps ready on Linux while Mac and win need
  to do the last steps on their own machines." docs/principles.md §6 requires every OS-specific
  identifier to be verified before code relies on it.
- **Decision (accepted, owner instruction):** macOS and Windows code (probe, autostart, firewall,
  disk encryption, OBS locator, tray, packaging) is written and unit-tested with fakes on Linux.
  Its identifiers get PF entries with status **`pending`** (macOS PF-101…199, Windows
  PF-201…299). `tools/check_facts.py` tolerates a pending fact only when it runs on a different
  OS; on the target OS (and with `--release`) every pending fact is an error. The stage runs on
  the Mac and the Windows PC (runbooks `docs/runbook-macos.md`, `docs/runbook-windows.md`) verify
  each fact, set it to `verified` (or refute and replace it), and only then pass.
- **Consequences:** the hard rule keeps its effect where it matters (nothing ships and no stage
  on that OS passes with an unverified fact); CI on Linux stays green meanwhile. The release
  workflow runs `check_facts.py --release`.
- **Verify-by:** stages 03/06/11/12 on macOS, 03/07/11/12 on Windows.

## ADR-016 — Owner decisions at the Linux checkpoint (2026-11-23)

Asked interactively after the Linux build; recorded verbatim as chosen.

| Topic | Decision |
|---|---|
| Project licence (stage 14) | **Proprietary, all rights reserved** (as the other private Établi tools); no licence headers; can be opened later |
| Storage | `data_root` on an encrypted volume (`/data/CaptationData` on the Linux workstation); **`storage.require_disk_encryption: block`** is the product default, so arming is refused on unencrypted storage (and while `/data` is not unlocked) |
| OBS start-up egress (L-19, PF-023) | **Accepted and documented**; no network block for OBS |
| Windows in v1 (ADR-007) | **Full support** |
| Video capture default (ADR-008) | **`window_preferred`** (meeting window; screen fallback with a warning) |
| Linux OBS distribution (ADR-006) | **Flatpak** (official OBS build) |
| Release plan | **v0.1.0 = Linux**, tagged after the Linux real-call checklist (reports/E2E-linux.md); macOS and Windows follow as v0.2.0 once their facts are verified |
| Transcription hook | **None preconfigured**; examples in docs/hooks.md |
| Tray lost during a recording (ADR-014) | **Continue with a warning**; no new recording until the indicator is back |
| Auto-disarm | **240 min** (default unchanged) |
| Project ID on the workstation | **unassigned** until the study is registered |
| macOS target | **macOS 26** on the owner's Mac (per-process detection available, ≥ 14) |

## ADR-017 — Public, source-available repository; disclaimer and acceptance gate (2026-11-23)

- **Context:** the owner decided to make the repository public, with a strict notice that any use
  needs prior approval and a thorough legal disclaimer that every user confirms with a checkbox
  before use. Asked what "approval" covers, the owner chose *both*: use of the software needs the
  owner's prior written approval, and every recording needs the prior, documented consent of all
  persons recorded (plus ethics and data-protection approval for research).
- **Decision (accepted, owner 2026-11-23):**
  - The repository is public; the licence stays proprietary ("source available": `LICENSE` grants
    no rights, use requires prior written approval).
  - `DISCLAIMER.md` (English and German, the German version prevails) holds the strict notice and
    the disclaimer: intended and prohibited use, the user's responsibility as controller under the
    GDPR, legal framework examples, what consent must cover, technical safeguards are not legal
    compliance, no legal advice, warranty and liability within German law, third-party software,
    acceptance, applicable law. It is not legal advice; the owner has it reviewed before relying
    on it.
  - **Acceptance gate (R-59, principles §2.10):** `captation accept` shows the packaged text in a
    dialog whose checkbox unlocks only after scrolling to the end (or, without a display, as a
    terminal checkbox ticked with `x`). There is no flag and no non-interactive path. The record
    (text version, SHA-256, time, OS user name, method) is stored in the user's state directory
    and bound to the text's hash, so a changed text needs a new acceptance. Without it the agent
    refuses to arm (`terms_not_accepted`), imports are refused and `doctor` reports an error.
    The record is the user's declaration, not copy protection.
  - The Linux install script and the Windows installer run `captation accept` after installation;
    the macOS runbook verifies the dialog.
  - The git history was replaced by a single commit before publication; the previous history is
    kept as a local bundle only.
- **Consequences:** unit and integration tests run with the gate patched open (marker
  `terms_gate` keeps the real check); the end-to-end suite uses its own state directory with a
  `test-environment` record and never touches the user's acceptance.
