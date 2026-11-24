# Principles

These rules are binding for the code in this repository. §2–§6 are hard rules: they are never
weakened, bypassed or "temporarily disabled" to make a check pass. Other documents refer to them
as `docs/principles.md §n` (in `docs/requirements.md` as C§n).

## 1. Purpose

Établi Captation is a background agent for **research recordings with prior written consent**.
It detects calls and online meetings on Linux, macOS and Windows and drives **OBS Studio** via
obs-websocket v5:

- **Video conference** (meeting app, or camera in use during a call) → video + multitrack audio
- **Any other call** → audio only (multitrack)
- **Not armed** → nothing is recorded

It also imports recordings from mobile hardware (Plaud Note Pro, viaim RecDot, RecorderGear
PR200, …) into the same verified session structure.

Code, comments and docs are in English. User-facing UI strings are German by default and
i18n-ready (`de`, `en`). The target architecture is `specs/architecture.md`; if it disagrees with
this document, this document wins and the conflict is recorded in `docs/decisions.md`.

**Use of the software requires the prior written approval of the copyright holder and the
acceptance of `DISCLAIMER.md` (enforced by the acceptance gate, §2.10).**

## 2. Consent guard (non-negotiable)

1. **Never record unless armed.** Arming is an explicit user action (tray or `captation arm`).
   `consent.require_armed` is `const: true` in the schema.
2. The agent **always starts disarmed**. Arming is never persisted across agent restarts,
   reboots or crashes.
3. Arming **expires** after `consent.auto_disarm_after_minutes`. If the owner sets it to `null`
   (ADR-021), arming stays on until it is ended, the agent stops or the user logs out, and it is
   never unnoticed: a reminder with *end study mode* appears every
   `consent.armed_reminder_minutes` (mandatory then) between calls.
4. Before each recording, a **countdown notification** (`consent.countdown_seconds`) with
   *Cancel* is shown. A cancelled start produces metadata only (`status: cancelled`) and **no media**.
5. While recording, a **visible indicator** is always shown (tray icon state and tooltip). There
   is no stealth mode, no option to hide the indicator and no silent mode.
6. After recording starts, the configurable **start reminder** is shown: state consent verbally,
   for **all** participants, so it is on the recording.
7. The tool stores a **consent reference** (`consent_ref`) entered at arming time. It never
   stores consent documents. **Imports require a `consent_ref`.**
8. Tray and notifications are **mandatory**. No configuration option may disable the indicator,
   the countdown or the reminder. If the OS blocks notifications, the fallback is a tray state
   change plus a sound and a logged warning. Never start silently.
9. No pre-roll and no replay buffer. Nothing from before the countdown may ever be kept.
10. **Acceptance gate.** The agent does not arm and nothing is imported until the current text of
    `DISCLAIMER.md` has been accepted by the user with an explicit checkbox; a changed text must
    be accepted again.

## 3. Privacy and data minimisation

1. No window titles, contact names, phone numbers or meeting IDs are stored unless
   `detection.store_window_titles: true`. Titles may be *read* in memory for classification.
2. Session IDs, folder names and file names never contain personal data
   (`S-YYYYMMDD-HHMMSS-xxxx`, `segNN_<role>.<ext>`). **Original file names of imported recordings
   are never stored**, because devices often name files after phone numbers. Only their SHA-256
   is kept.
3. Storage is **local only**. The agent's only network traffic is the obs-websocket connection
   to loopback, plus hook commands the user configured explicitly. No telemetry, no update
   checks, no crash upload.
4. **OBS's own egress:** OBS is launched with its updater disabled where a verified flag exists,
   and its crash-log upload must be off (`doctor` checks this).
5. Logs contain no personal data (no titles, no names, no file contents). Apps are logged by `app.id`.
6. Video mode prefers **window capture** of the meeting window over display capture. If display
   capture is used, `session.json` gets a warning.
7. Raw recordings are **never deleted automatically** unless `storage.retention_days` is set.

## 4. OBS boundary, exposure and licensing

1. OBS is controlled **only** through obs-websocket v5, as a separate process. OBS (GPL-2.0) is
   never linked, forked, patched or bundled. Installers and `doctor` point to the official
   OBS download.
2. Only OBS profiles and scene collections whose names start with `Captation` are created or
   modified. The only exception is the obs-websocket config (backed up first; auth is never
   disabled).
3. **Exposure:** obs-websocket offers no loopback-only bind option. With
   `obs.exposure_policy: require_firewall` (the default), a host firewall rule that blocks
   inbound access to the websocket port from non-loopback addresses must be in place before
   arming is allowed. Otherwise `doctor` errors and arming is refused.
4. No code from AGPL projects may be copied into this repository.
5. **Licence gate:** no GPL, AGPL or unknown-licence runtime dependency (CI checks it). Every
   dependency decision records its licence.
6. The project licence is proprietary (`LICENSE`, decision ADR-016).

## 5. Self-trigger and correctness

1. OBS, its helper processes and the agent itself never count as call apps. They are excluded by
   the ignore list in the rules **and** by PID.
2. OBS must **not hold the microphone while idle**. Audio inputs are created at recording start
   and removed at stop.
3. Never two concurrent recordings. Never `RECORDING` while `DISARMED`. A cancelled start never
   leaves media. Property-based tests prove all three.
4. Profile or scene-collection switches happen only while OBS is not recording.
5. Track QC is mandatory. A remote or local track that is silent beyond `qc.silence_warn_s`
   raises a warning in `session.json` and a notification after the segment.

## 6. Verify, don't assume (machine-checked)

OBS input-kind IDs, property names, profile parameter keys, CLI flags, obs-websocket request
availability, OS APIs and process or bundle names differ between versions.

- They are queried at runtime and recorded in `docs/platform-facts.md` as **PF-ID** entries.
- Every hard-coded identifier in the platform code carries a `# PF-xxx` comment.
  `tools/check_facts.py` fails if a referenced PF-ID is missing or not verified on the OS it
  belongs to (ADR-015), or if a platform file contains a string constant without one.
- Detection rules marked `verify: true` must be confirmed or corrected on the real OS before
  that OS is released.

## 7. Verification discipline

- Every change is backed by evidence: tests, `tools/check_facts.py`, `tools/sync_specs.py
  --check`, ruff, `mypy --strict`, `tools/licence_gate.py`, and `tools/traceability.py`
  (requirement → test matrix).
- Sign-off on platform facts, end-to-end runs and releases is given by the maintainer and
  recorded in `docs/decisions.md`.

## 8. Code conventions

- Python 3.12, `uv`, src layout `src/captation/`, full type hints, `ruff` (lint and format),
  `mypy --strict` on `src/`, `pytest` (+ `hypothesis`).
- Platform code sits behind `typing.Protocol` interfaces in `src/captation/platform/base.py`.
  Shared modules do not import OS-specific modules at top level.
- **Package data:** `specs/*.schema.json`, `specs/rules.default.yaml` and `DISCLAIMER.md` are
  mirrored to `src/captation/data/` by `tools/sync_specs.py`; CI fails on drift. Runtime code
  loads only package data.
- Configuration: pydantic v2 models **and** JSON-Schema validation against the packaged
  `config.schema.json`. Unknown keys are errors.
- Logging: structured JSON lines, rotating files in the platform log dir, with a redaction filter.
- Secrets: the obs-websocket password is generated at bootstrap and stored in the OS keyring,
  with an environment variable as fallback. It is never in the repo, logs or process arguments,
  and OBS and hooks never inherit it.
- Subprocesses: always argv lists, never `shell=True`, always with timeouts.
- Tests: markers `obs`, `e2e`, `linux|macos|windows`, plus `req("R-xx")` for traceability.
  CI runs unit tests only.
