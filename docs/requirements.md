# Requirements

Numbered, testable requirements for Établi Captation v1. Sources: `specs/architecture.md` (§n)
and the principles in `docs/principles.md` (C§n). Tests reference requirements with
`@pytest.mark.req("R-xx")`. `tools/traceability.py` builds the matrix.

Test levels: **unit** (no OBS, no OS state), **integration** (`obs` marker, real OBS),
**e2e** (`e2e` marker, real agent, OBS and OS), **manual** (checklist in the stage report).

## Detection and mode

| ID | Requirement | Source | Test |
|---|---|---|---|
| R-01 | A mic user matching a `meeting` app is recorded in **video** mode | §5.5 | unit, e2e (E2) |
| R-02 | A call in any category while the camera is active is recorded in **video** mode | §5.5 | unit, e2e (E3) |
| R-03 | Any other recognised call is recorded in **audio** mode. A browser holding the microphone without an identifying page title is taken as a meeting and recorded in **video** (ADR-018) | §5.5 | unit, e2e (E1) |
| R-04 | Category priority is meeting > call > messenger > browser > unknown. Browser title rules may upgrade the category and set the app ID | §5.2 | unit |
| R-05 | `classify()` is a pure function. Ties break by (a) the running session's app, (b) the camera user, (c) rule order, (d) the lowest PID. Shuffling `mic_users` does not change the result | §5.4 | unit (property) |
| R-06 | Apps that match no rule are recorded only if `detection.record_unknown_apps` is true (the default since ADR-018; countdown, *Cancel* and indicator apply as always) | §5.3 | unit |
| R-07 | Browser helper and renderer processes are attributed to their parent browser | §3 | unit |
| R-08 | Camera on during an audio segment starts a new video segment (`upgrade:camera_on`, target gap ≤ 2 s). Mode is never downgraded within a session | §6 | unit, integration, e2e (E3) |
| R-09 | When a different `app_id` stays the call context for `stop_debounce_s`, the current session is finalised and the new app starts a new session with a new countdown. A different app ID of the same process (a browser tab or title change) continues the session (ADR-019) | §6 | unit |
| R-10 | Recording starts only after the context was stable for `start_debounce_s`, and stops only after it was absent for `stop_debounce_s` | §6 | unit, e2e (E1) |

## Multitrack audio

| ID | Requirement | Source | Test |
|---|---|---|---|
| R-11 | Track 1 is the mix. Remote, local and external use their configured, unique track numbers. Each Captation input is routed to track 1 plus its role track only | §7 | unit, integration |
| R-12 | `Captation Remote` captures the call app per application. If that is impossible, desktop audio is used and a warning is recorded | §7 | unit, integration |
| R-13 | `Captation Local` follows the input device the call app records from (`follow_call`, default; where the OS reports it, macOS PF-132), also when the call app switches devices during the call, else the default input; or the default input (`follow_default`) or a named device. An unresolvable device falls back to the default device with a warning | §7 | unit, integration |

## Consent guard (docs/principles.md §2)

| ID | Requirement | Source | Test |
|---|---|---|---|
| R-14 | Nothing is recorded unless the agent is armed. `consent.require_armed` cannot be set to false | C§2.1 | unit (schema, property), e2e (E4) |
| R-15 | The agent always starts disarmed. Arming is never persisted across restarts, reboots or crashes | C§2.2 | unit, e2e (E9) |
| R-16 | Arming expires after `consent.auto_disarm_after_minutes`; with `null` (ADR-021) it stays on until it is ended, the agent stops or the user logs out, and a reminder with *end study mode* appears every `consent.armed_reminder_minutes` between calls. Disarming or expiry while recording stops and finalises the session immediately | C§2.3 | unit, e2e (E8) |
| R-17 | Every recording is preceded by a countdown notification with *Cancel*. A cancelled start writes `status: cancelled` and no media. Fail-closed: a countdown turns into a recording only after the UI confirmed it was shown; otherwise it is cancelled and study mode ends (ADR-019). With `consent.record_during_countdown` (ADR-022, default) the recording starts when the countdown is shown; a cancel or any other end within the countdown stops it and deletes its media (only `session.json`, `cancelled`) | C§2.4 | unit, e2e (E6) |
| R-18 | While recording, a visible indicator (tray state and tooltip) is always shown. No setting can hide it | C§2.5 | unit (schema), manual |
| R-19 | Right after recording starts, the start reminder (`consent.start_reminder_text`) is shown | C§2.6 | unit, manual |
| R-20 | A consent reference is entered at arming time and validated against `consent_ref_pattern`. Consent documents are never stored | C§2.7 | unit |
| R-21 | Tray and notifications are mandatory with the fallback chain notification-with-action → notification + tray cancel → tray blink + sound + tray cancel. Without a tray, arming is refused | C§2.8 | unit, e2e (E13) |
| R-22 | No pre-roll and no replay buffer. Nothing from before the countdown is kept. The config schema rejects `preroll_seconds` | C§2.9 | unit (schema), grep |
| R-59 | Before the first use, the current `DISCLAIMER.md` is accepted with an explicit checkbox (`captation accept`: dialog that unlocks the checkbox only after scrolling to the end, or a terminal checkbox; never by a flag or without a TTY). Without a record matching the text's SHA-256, arming (`terms_not_accepted`) and import are refused, and `doctor` reports an error. A changed text must be accepted again | C§2.10, ADR-017 | unit |
| R-60 | The default rules recognise the meeting, call and messenger apps listed in `docs/app-coverage.md` by process name, macOS bundle ID, Windows package or Linux Flatpak app ID, and the web versions in a browser by normalised page title (Linux: Firefox-family tab title from PipeWire). Ordinary pages that merely mention a service do not count as a meeting. `detection.extra_meeting_titles` adds the user's own meeting pages | ADR-018 | unit |
| R-61 | If the agent ends without shutting down (crash, kill), a watchdog process stops a recording that runs in a Captation profile and removes the Captation inputs; a recording of the user's own profile is left alone (ADR-019) | C§2.3, C§2.5 | unit |

## Privacy and data minimisation (docs/principles.md §3)

| ID | Requirement | Source | Test |
|---|---|---|---|
| R-23 | Window titles, contact names, phone numbers and meeting IDs are not stored unless `detection.store_window_titles` is true | C§3.1 | unit |
| R-24 | Session IDs, folder names and file names contain no personal data (`S-YYYYMMDD-HHMMSS-xxxx`, `segNN_<role>.<ext>`) | C§3.2 | unit (schema) |
| R-25 | Logs contain no personal data: no titles, names, file contents or source file names. Apps are logged by `app.id` | C§3.5 | unit (redaction), grep (stage 14) |
| R-26 | The agent's only network traffic is the obs-websocket connection to loopback plus user-configured hooks. No telemetry, update checks or crash upload | C§3.3 | e2e (E10) |
| R-27 | OBS is launched with its updater disabled where a verified flag exists. `doctor` reports whether OBS crash-log upload is off | C§3.4 | integration, e2e (E10) |
| R-28 | Video mode prefers window capture. If display capture is used, `session.json` gets a warning | C§3.6 | unit, integration |
| R-29 | Raw recordings are never deleted automatically unless `storage.retention_days` is set. Retention never touches `status: recording` | C§3.7 | unit |

## OBS boundary, exposure and licensing (docs/principles.md §4)

| ID | Requirement | Source | Test |
|---|---|---|---|
| R-30 | OBS is controlled only through obs-websocket v5 as a separate process. OBS is never linked, patched or bundled | C§4.1 | grep, packaging check |
| R-31 | Only profiles and scene collections whose names start with `Captation` are created or changed. The obs-websocket config is backed up before change and auth is never disabled | C§4.2 | integration (diff) |
| R-32 | With `exposure_policy: require_firewall`, arming is refused until a host firewall rule that blocks non-loopback access to the websocket port is confirmed | C§4.3 | unit, e2e (E12) |
| R-33 | No GPL, AGPL or unknown-licence runtime dependency (licence gate in CI) | C§4.5 | CI step |
| R-34 | The websocket password has ≥ 32 characters, lives in the OS keyring (env var fallback) and never appears in the repo, logs, reports or process arguments | C§8 | integration, grep |

## Self-trigger and correctness (docs/principles.md §5)

| ID | Requirement | Source | Test |
|---|---|---|---|
| R-35 | OBS, its helpers and the agent never count as call apps (ignore rules and PIDs) | C§5.1 | unit, e2e (E5) |
| R-36 | OBS does not hold the microphone while idle. Captation inputs exist only during a segment | C§5.2 | integration, e2e (E5) |
| R-37 | Never two concurrent recordings, never `RECORDING` while disarmed, and a cancelled start never leaves media | C§5.3 | unit (property) |
| R-38 | Profile and scene-collection switches happen only while OBS is not recording | C§5.4 | unit (fake OBS) |
| R-39 | Track QC: a remote or local track silent beyond `qc.silence_warn_s` raises `qc:remote_silent` / `qc:local_silent`; a peak above −0.5 dBFS raises `qc:clipping`; any QC warning triggers a tray notification | C§5.5 | unit, e2e (E11) |

## Post-processing, storage and integrity

| ID | Requirement | Source | Test |
|---|---|---|---|
| R-40 | Each audio role is extracted to `segNN_<role>.flac` (or `.wav`) without resampling, normalisation or filters | §8 | unit (FFT mapping) |
| R-41 | Video segments are remuxed to `segNN_video.mp4` with all audio tracks. If remuxing fails, the MKV is kept and a warning is recorded | §8 | unit |
| R-42 | Audio segments recorded with strategy B also produce `segNN_audio.mka` without the dummy video stream | §7, §8 | unit |
| R-43 | `session.json` validates against the session schema before every write and is written atomically. Invalid documents are never written | §8 | unit |
| R-44 | Every file has a SHA-256 in `SHA256SUMS` and `session.json`. Finalised files are read-only when `make_read_only` is true | §8 | unit |
| R-45 | Before each segment start, free space ≥ `storage.min_free_gb` is checked. Otherwise the start is refused, or a running recording stops gracefully | §6, §8 | unit |
| R-46 | Disk-encryption policy: `warn` (and `unknown`) adds a warning, `block` refuses arming, `off` does nothing | §3, §8 | unit |
| R-47 | With `age_recipients` set, every finalised file is encrypted, verified, and only then the plaintext is removed | §8 | unit |
| R-48 | Hooks run as argv lists with whole-argument placeholders, no shell and a timeout. A hook failure adds a warning and never changes media | §9 | unit |

## Agent, CLI and installation

| ID | Requirement | Source | Test |
|---|---|---|---|
| R-49 | The agent autostarts at login (Linux XDG autostart or systemd user unit, macOS LaunchAgent, Windows Task Scheduler), never arms itself, and install/uninstall are idempotent and leave no residue | §3, stage 11 | integration, manual |
| R-50 | `captation doctor` reports every check with ok/warn/error and a fix; exit code 0/1/2; `--json` | stage 11 | unit, integration |
| R-51 | The configuration is validated against the packaged schema and pydantic models; unknown keys are errors; runtime code loads only package data | C§8 | unit |
| R-52 | IPC uses a user-only socket and authkey file. Malformed messages are rejected and logged and never crash the agent | §2 | unit (fuzz) |
| R-53 | If OBS is lost or stops recording on its own, the segment is marked interrupted, OBS is reconnected or relaunched, and a new segment starts if the call is still present (`recovery:obs_restart`), at most three times per session. A start that OBS refuses for another reason finalises the session once as `failed` without retries. A stop that OBS does not confirm is retried; while OBS still records, the session is not finalised and the indicator stays on (ADR-019) | §6 | unit, e2e (E7) |

## Mobile import

| ID | Requirement | Source | Test |
|---|---|---|---|
| R-54 | `captation import` requires a valid `consent_ref` | §11, C§2.7 | unit |
| R-55 | Original file names and device text metadata are never stored in `session.json`, logs, reports or output tags. Only `origin.source_sha256` is kept | §11, C§3.2 | unit (planted metadata) |
| R-56 | The audio bitstream is unchanged (stream hash before = after). Container tags are stripped losslessly, or the file is copied unchanged with `import:metadata_not_stripped` | §11 | unit |
| R-57 | A second import of the same source is refused unless `--force` | stage 13 | unit |
| R-58 | `started_at` comes from file metadata, `--started-at`, or the file mtime (with `import:time_from_mtime`), in that order | §11 | unit |
