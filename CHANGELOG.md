# Changelog

All notable changes are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses semantic versioning.

## [Unreleased]

### Changed
- The repository is public as source-available software; no licence is granted (ADR-017).
- Unit tests and type checks run on macOS and Windows CI as well (Linux-only modules are skipped).

### Added
- `DISCLAIMER.md` (English/German): strict notice that any use needs the copyright holder's prior
  written approval and every recording the prior consent of everyone recorded; full disclaimer
  and conditions of use. Acceptance gate (R-59): `captation accept` shows it with a checkbox;
  without acceptance the agent does not arm, imports are refused and `doctor` reports an error.
  The Linux install script and the Windows installer run it after installation.
- Linux agent (stages 01–05, 08–11, 13): call and camera detection on PipeWire, OBS control over
  obs-websocket v5 (Flatpak OBS 33), consent guard with tray indicator, countdown and reminder,
  multitrack post-processing with QC, SHA-256 integrity and read-only files, hooks, mobile
  recorder import, `doctor`, XDG/systemd autostart, firewall rule for the websocket port.
- Specifications, requirements (R-01…R-58), decisions (ADR-001…014) and platform facts.

- macOS and Windows prepared on Linux (ADR-015): Swift probe, LaunchAgent, pf firewall rule,
  FileVault check, `.app` packaging; ConsentStore probe, Task Scheduler, Windows Firewall rule,
  BitLocker check, Inno Setup installer. Their platform facts are `pending` until verified on
  those machines (runbooks in `docs/`).
- Release review (stage 14) findings fixed: no recording after a lost tray indicator, crash
  recovery at start-up, launch-argument whitelist, OBS-loss recovery while idle, hooks off the
  event loop and without secrets, single-instance lock, OBS never inherits our secrets.

### Not yet
- `v0.1.0` is tagged only after the release-audit checkpoint (stage 14) and verified facts on
  every platform that ships.
