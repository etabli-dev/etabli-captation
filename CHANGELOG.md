# Changelog

All notable changes are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses semantic versioning.

## [Unreleased]

### Fixed
- macOS: FaceTime and iPhone calls recorded a silent remote track; they use desktop audio now
  (PF-135).
- macOS app: notifications never worked in the bundle because `desktop_notifier.resources` was
  not packaged; the agent now also asks for the notification permission at start (PF-126).

### Changed
- Fail-closed recording control (ADR-019): a countdown must be confirmed as shown before it can
  turn into a recording; unconfirmed stops are retried and never finalised while OBS records;
  OBS stopping on its own is recovered (at most three times per session); refused starts end
  once as `failed`; a browser tab switch no longer ends a session; post-processing runs off the
  event loop; failed polls keep auto-disarm running.
- A browser call that cannot be identified is recorded as video, and apps without a rule are
  recorded by default (ADR-018).
- The repository is public as source-available software; no licence is granted (ADR-017).
- Unit tests and type checks run on macOS and Windows CI as well (Linux-only modules are skipped).

### Added
- Local transcript and summary after every session (ADR-023): `processed/transcript.json`
  (`specs/transcript.schema.json`) and `transcript.md` with metadata, transcript (Ich/Gegenseite),
  summary, agreements and to-dos (owner, urgency, deadline; only when discussed, each with a
  quote); whisper.cpp + llama.cpp, no network; `captation transcribe`, `captation models download`.
- Audio/video deleted 90 days after the call when the transcript is complete
  (`storage.media_retention_days`, ADR-023).
- Recording starts when the countdown is shown, not after it (ADR-022, default): *Abbrechen*
  within the countdown stops the recording and deletes it; start debounce 1 s.
- Study mode without a time limit (ADR-021, now the default): it stays on until it is ended,
  with a reminder and an *end study mode* button every 4 hours between calls
  (`consent.armed_reminder_minutes`); a time limit can still be set.
- Background operation on macOS: opening the app while it runs opens its menu-bar menu (reopen
  Apple Event, PF-133); without a running agent, opening the app starts it in the background.
  The LaunchAgent (`install-autostart`) restarts it after a crash (PF-122).
- Any headset (ADR-020): on macOS the local track records the microphone the call app uses and
  follows a headset that connects or disconnects during the call (`local_input: follow_call`,
  default; PF-132).
- App coverage (ADR-018, R-60, `docs/app-coverage.md`): FaceTime and iPhone calls on the Mac,
  Windows Phone Link, Cisco Jabber, GoTo, Discord, Slack, Element and SIP softphones; WhatsApp
  clients for Linux by Flatpak app ID; browser pages of Teams, Google Meet, Zoom, Webex,
  BigBlueButton, Cisco Meeting Server, Jitsi, FaceTime, WhatsApp and others by normalised title;
  Firefox tab titles on Linux (PF-026); Vivaldi, Chromium and other browsers (PF-024, PF-025);
  `detection.extra_meeting_titles` for an institution's own meeting pages.
- Watchdog (R-61): a recording stops if the agent ends without shutting down.
- `DISCLAIMER.md` (English/German): strict notice that any use needs the copyright holder's prior
  written approval and every recording the prior consent of everyone recorded; full disclaimer
  and conditions of use. Acceptance gate (R-59): `captation accept` shows it with a checkbox;
  without acceptance the agent does not arm, imports are refused and `doctor` reports an error.
  The Linux install script and the Windows installer run it after installation.
- Linux agent: call and camera detection on PipeWire, OBS control over
  obs-websocket v5 (Flatpak OBS 33), consent guard with tray indicator, countdown and reminder,
  multitrack post-processing with QC, SHA-256 integrity and read-only files, hooks, mobile
  recorder import, `doctor`, XDG/systemd autostart, firewall rule for the websocket port.
- Specifications, requirements (R-01…R-61), decisions (ADR-001…019) and platform facts.

- macOS and Windows prepared on Linux (ADR-015): Swift probe, LaunchAgent, pf firewall rule,
  FileVault check, `.app` packaging; ConsentStore probe, Task Scheduler, Windows Firewall rule,
  BitLocker check, Inno Setup installer. Their platform facts are `pending` until verified on
  those machines (runbooks in `docs/`).
- Release review findings fixed: no recording after a lost tray indicator, crash
  recovery at start-up, launch-argument whitelist, OBS-loss recovery while idle, hooks off the
  event loop and without secrets, single-instance lock, OBS never inherits our secrets.

### Not yet
- `v0.1.0` (Linux) is tagged only after the release review and the Linux real-call checklist;
  macOS and Windows follow once their platform facts are verified (ADR-016).
