# Établi Captation — Target Architecture (stack v2)

Status: specification, updated to the implementation. Everything marked *(verify)* must be
confirmed on the real OS and recorded in `docs/platform-facts.md` before code relies on it
(docs/principles.md §6).

## 1. Overview

```
┌──────────────────────────── captation agent (Python, user session) ───────────────────────────┐
│                                                                                                │
│  CallProbe (per OS) ──snapshot every poll_interval_ms──▶ Classifier (rules.py)                  │
│   • mic users per process         ProbeSnapshot           • app match (process/package/bundle) │
│   • camera in use                                         • category: meeting|call|messenger|  │
│   • window titles (optional)                                          browser|unknown          │
│                                                           • mode: audio|video                  │
│                                                                   │ CallContext | None         │
│                                                                   ▼                            │
│  Consent guard ◀──arm/disarm── Tray / CLI / IPC        Session state machine (session.py)      │
│   (armed? countdown, cancel)  ───────────────────────▶  debounce, segments, mode upgrade       │
│                                                                   │ commands                   │
│                                                                   ▼                            │
│                                                        ObsController (obs/controller.py)       │
│                                                         profile/scene switch, inputs, tracks,  │
│                                                         start/stop, crash recovery             │
│                                                                   │ obs-websocket v5 (127.0.0.1)│
└───────────────────────────────────────────────────────────────────┼────────────────────────────┘
                                                                    ▼
                                                     OBS Studio (separate process, unmodified)
                                                     profiles Captation-Audio / Captation-Video
                                                     scene collection Captation (scenes Audio, Video)
                                                                    │ segNN.mkv
                                                                    ▼
                                         Post-processing (ffmpeg/ffprobe): track extraction,
                                         remux, checksums, read-only, session.json, hooks
```

## 2. Components

| Component | Module | Responsibility |
|---|---|---|
| CLI | `cli.py` (Typer) | `run`, `arm`, `disarm`, `status`, `stop`, `cancel`, `accept`, `doctor`, `import`, `check-config`, `install-autostart`, `uninstall-autostart`, `version`; `obs bootstrap`, `obs facts`, `obs approve-screen`, `obs set-password`, `obs firewall` |
| Acceptance gate | `terms.py`, `ui/terms_dialog.py` | `captation accept`: `DISCLAIMER.md` with a checkbox; the record binds to the text's SHA-256 (R-59) |
| IPC | `ipc.py` | CLI ↔ running agent. `multiprocessing.connection` (AF_UNIX on POSIX, named pipe on Windows) with a random authkey stored in a user-only (0600) file |
| Agent loop | `agent.py` | Wires probe → classifier → session. Owns timers |
| CallProbe | `platform/*.py` | Returns `ProbeSnapshot` (§4) |
| Classifier | `rules.py` | Pure function `classify(snapshot, rules, config) → CallContext | None` |
| Session | `session.py` | Pure state machine (§6), emits commands to ObsController and UI |
| ObsController | `obs/controller.py` | All OBS control: profile/scene switches, Captation inputs, track routing, start/stop, crash recovery |
| obs-websocket client | `obs/client.py` | Thin v5 client (auth handshake, request/response matching, event reader thread; ADR-002) |
| OBS bootstrap | `obs/bootstrap.py` | Creates `Captation*` profiles and scene collection, websocket config, facts dump |
| OBS launcher | `obs/launcher.py` | Locates and starts OBS with verified CLI flags, waits for the websocket |
| Post-processing | `postprocess.py` | ffprobe inventory, track extraction, remux, verification |
| Track QC | `qc.py` | Per-file RMS, peak and silent ratio (ffmpeg `astats`/`silencedetect`); warnings above `qc` thresholds |
| Importer | `importer.py` | `captation import`: mobile recorder files → verified session (§11) |
| Firewall | `obs/firewall.py` | Checks, and with consent applies, the inbound block rule for the websocket port per OS (§9) |
| Storage / integrity | `storage.py`, `integrity.py` | Folder layout, `session.json`, SHA-256, read-only, disk-encryption check, free-space check, optional age encryption |
| Hooks | `hooks.py` | `on_session_complete` argv with placeholders, timeout, logging |
| UI | `ui/base.py`, `ui/tray.py`, `ui/notify.py`; back ends `ui/linux.py` (D-Bus), `ui/pystray_ui.py` (macOS/Windows) | Tray states, countdown with *Cancel*, reminders (ADR-003) |

## 3. Platform matrix (Linux verified; macOS and Windows *(verify)*, pending facts in `docs/platform-facts.md`)

| Capability | Linux | macOS | Windows |
|---|---|---|---|
| Mic in use, per process | `pw-dump`: nodes with `media.class = Stream/Input/Audio`, running, with `application.process.id/binary/name` | Swift probe: CoreAudio process objects (`kAudioHardwarePropertyProcessObjectList`, `kAudioProcessPropertyPID` (macOS 14+), `…BundleID`, `…IsRunningInput`). **Poll**: property listeners for `IsRunningInput` are reported not to fire. Fallback: `kAudioDevicePropertyDeviceIsRunningSomewhere` plus frontmost app (`confidence: low`) | Registry `…\CapabilityAccessManager\ConsentStore\microphone` in HKCU (also check HKLM, keep the hive labelled). Packaged apps appear by package family name, `NonPackaged\` by `#`-separated exe paths. In use ⇔ `LastUsedTimeStop == 0` |
| Camera in use | PipeWire `Stream/Input/Video` nodes, plus `/proc/*/fd` → `/dev/video*` | CoreMediaIO `kCMIODevicePropertyDeviceIsRunningSomewhere` | Same ConsentStore under `webcam` |
| Window titles (optional) | Not read: unavailable on Wayland, and no X11 path is implemented (PF-019, L-15) | `CGWindowListCopyWindowInfo` (needs Screen Recording permission) | `EnumWindows` + `GetWindowThreadProcessId` |
| Per-app audio in OBS | Plugin `obs-pipewire-audio-capture`. Fallback: desktop audio (warn) | "macOS Audio Capture" (ScreenCaptureKit), application mode | "Application Audio Capture" (WASAPI process loopback) |
| Video capture in OBS | PipeWire screen capture via the portal with a restore token; no window targeting on Wayland, so the approved screen is captured (`capture:display_fallback`) | "macOS Screen Capture" (ScreenCaptureKit), window or display | Window Capture (WGC) or Display Capture |
| Autostart | XDG autostart `.desktop` (default) or `systemd --user` | LaunchAgent `~/Library/LaunchAgents/de.etabli.captation.plist` | Task Scheduler task "At log on", least privilege |
| Disk-encryption check | dm-crypt layer below `data_root` (`findmnt --target` + `lsblk -s`, PF-020); other layers → `unknown` | `fdesetup status` | `Get-BitLockerVolume` (may need elevation, otherwise report `unknown`) |
| OBS distribution | Flatpak `com.obsproject.Studio` (default, ADR-006: different config path, sandboxed plugins) or a native `obs` on PATH | `/Applications/OBS.app` | `%ProgramFiles%\obs-studio` |
| Websocket firewall rule | Dedicated `nftables` table `inet etabli_captation` (drop non-loopback → port), loaded by a oneshot systemd unit; independent of ufw/iptables front ends (ADR-012) | `pf` anchor (application firewall is per app, not per port) | `New-NetFirewallRule` inbound block, remote ≠ loopback (needs elevation once) |

Browser helper processes (e.g. `Google Chrome Helper`, `com.google.Chrome.helper…`) are
normalised to their parent app before classification.

## 4. Data contracts (Python, conceptual)

```python
@dataclass(frozen=True)
class AppRef:
    pid: int
    process_name: str            # basename, as reported by the OS
    exe_path: str | None
    bundle_id: str | None        # macOS
    package_id: str | None       # Windows package family name
    parent_app: str | None       # normalised parent (browser helpers)

@dataclass(frozen=True)
class ProbeSnapshot:
    ts_monotonic: float
    mic_users: tuple[AppRef, ...]
    camera_active: bool
    camera_users: tuple[AppRef, ...]          # may be empty when the OS cannot attribute
    window_titles: dict[int, tuple[str, ...]] | None   # pid → titles, in memory only
    confidence: Literal["high", "low"]

@dataclass(frozen=True)
class CallContext:
    app_id: str                  # from rules, e.g. "zoom"
    category: Literal["meeting", "call", "messenger", "browser", "unknown"]
    pid: int
    mode: Literal["audio", "video"]
    reason: str                  # e.g. "meeting_app", "camera_active", "default"
    capture_target: AppRef       # for OBS per-app audio / window capture
```

## 5. Classification and mode rules

1. Drop all mic users matching `ignore` (by name or bundle) and the PIDs of OBS and the agent.
2. Match the remaining users against `apps` in the packaged default rules (merged with user
   overrides). Priority: `meeting` > `call` > `messenger` > `browser`
   (upgraded to `meeting` or `messenger` by `title_rules`) > `unknown`.
3. `unknown` apps are only recorded if `detection.record_unknown_apps: true` (default false).
4. **Deterministic tie-break** among equal-priority candidates. The classifier stays a pure
   function of `(snapshot, rules, config, current_app_id)`:
   (a) the app of the running session (`current_app_id`), if it is still a candidate;
   (b) an app whose PID also uses the camera;
   (c) rule order in the rules file;
   (d) the lowest PID.
5. Mode: the first matching entry in `modes`:
   `category == meeting → video`; `camera_active → video`; otherwise `audio`.

## 6. Session state machine

States: `DISARMED`, `ARMED_IDLE`, `PENDING_START`, `COUNTDOWN`, `RECORDING`,
`PENDING_STOP`, `STOPPING`, `POSTPROCESSING`, `ERROR`.

| From | Event | To | Action |
|---|---|---|---|
| DISARMED | arm(consent_ref) | ARMED_IDLE | start auto-disarm timer; pre-warm OBS (`obs.launch_on: arm`) |
| ARMED_IDLE | context present | PENDING_START | start `start_debounce_s` |
| PENDING_START | context stable ≥ debounce | COUNTDOWN | notify with *Cancel*; check free space and encryption policy |
| PENDING_START | context lost | ARMED_IDLE | – |
| COUNTDOWN | elapsed | RECORDING | prepare(mode) → start segment 1; start reminder |
| COUNTDOWN | cancel | ARMED_IDLE | write `session.json` with `status: cancelled`, no media |
| RECORDING | camera on while mode=audio | RECORDING | if `segments_on_mode_change`: stop segment, start a video segment (target gap ≤ 2 s) |
| RECORDING / PENDING_STOP | context app changes (different `app_id`) | STOPPING → PENDING_START | finalise the current session, then treat the new app as a new call (new countdown) |
| RECORDING | context lost | PENDING_STOP | start `stop_debounce_s` |
| PENDING_STOP | context back | RECORDING | – |
| PENDING_STOP | elapsed | STOPPING | stop record, tear down inputs |
| RECORDING / PENDING_STOP | manual stop, disarm, auto-disarm, low disk | STOPPING | – |
| RECORDING | OBS lost | ERROR → RECORDING | mark segment `interrupted`, relaunch OBS, new segment if the context is still present |
| STOPPING | stopped | POSTPROCESSING | post-process all segments |
| POSTPROCESSING | done | ARMED_IDLE or DISARMED | `status: complete` or `interrupted`; run hooks |
| any | disarm | DISARMED (via STOPPING if recording) | – |

Mode is never downgraded from video to audio within a session.

Invariants (property tests): never `RECORDING` while disarmed; at most one active recording;
every started segment ends in exactly one finalised `session.json` entry; a cancelled start
leaves no media file; arming is refused while the exposure policy is unmet.

## 7. OBS layout

- **Profiles**: `Captation-Audio`, `Captation-Video` (advanced output mode, MKV, tracks 1–4,
  48 kHz). The video profile uses canvas and output ≤ `recording.video.max_height` at
  `recording.video.fps`.
- **Scene collection** `Captation` with scenes `Audio` (no capture sources) and `Video`
  (window or display capture).
- **Audio inputs**, created at segment start and removed at stop:

| Input | Source | Tracks |
|---|---|---|
| `Captation Remote` | per-app capture of `capture_target` (fallback: desktop audio, warning) | 1 + `tracks.remote` |
| `Captation Local` | `recording.audio.local_input`: `follow_default`, or a device name (it must match the device the call app uses; QC catches a mismatch) | 1 + `tracks.local` |
| `Captation External` | optional `external_input_device` (e.g. a USB audio interface) | 1 + `tracks.external` |

Track 1 is always the mix.

- **Audio-only strategy**, chosen empirically (ADR-009):
  (A) the profile records audio only (e.g. custom FFmpeg output), or
  (B) minimal canvas, then the video stream is dropped in post-processing.
  A still writes an H.264 stream on Linux, so B is used: 128×72 at 30 fps (PF-008).
- **Audio codec**: first available from `recording.audio.codec_preference`
  (lossless PCM/FLAC in MKV preferred) *(verify per OBS version)*.

## 8. Storage layout

```
<data_root>/<project_id or "unassigned">/<session_id>/
├── session.json              # specs/session.schema.json
├── SHA256SUMS
├── seg01_container.mkv       # original OBS output (kept if keep_original_mkv)
├── seg01_audio.mka           # audio mode with strategy B: all audio tracks, no dummy video
├── seg01_video.mp4           # video mode only (remux, all audio tracks kept)
├── seg01_mix.flac
├── seg01_remote.flac
├── seg01_local.flac
└── seg01_external.flac       # only if configured
```

Finalised files are made read-only. `session.json` is written atomically (temp file + rename)
and updated at every state change.

### Warnings in `session.json`

`warnings` lists codes, never personal data. A warning never blocks finalisation.

| Code | Meaning |
|---|---|
| `qc:remote_silent`, `qc:local_silent` | The remote or local track is silent for longer than `qc.silence_warn_s` |
| `qc:clipping` | A track peaks above −0.5 dBFS |
| `qc:failed:<role>:<segNN>` | QC of one file could not run |
| `remote:desktop_fallback` | The call app could not be captured per application; the remote track holds desktop audio |
| `local:device_not_found` | `recording.audio.local_input` names a device OBS does not offer; the local track follows the default input |
| `external:device_not_found` | `recording.audio.external_input_device` was not found; no external track was recorded |
| `capture:display_fallback` | Video mode with `window_preferred`, but the screen (not the meeting window) was captured |
| `capture:not_approved` | No screen capture available (Wayland: not yet approved with `captation obs approve-screen`); the video is black |
| `storage:disk_unencrypted` | `data_root` is on unencrypted storage (only with `storage.require_disk_encryption: warn`; `block` refuses arming) |
| `storage:disk_encryption_unknown` | Whether `data_root` is encrypted could not be determined (for example BitLocker without elevation); arming was allowed |
| `obs:websocket_exposed` | `obs.exposure_policy: warn` and the firewall rule for the websocket port was not confirmed when the session started |
| `ui:indicator_lost` | The tray indicator disappeared during the recording (ADR-014) |
| `agent:recovered_after_crash` | The agent stopped during the recording; at its next start the session was finalised as `interrupted`, media as written by OBS |
| `postprocess:<problem>:<segNN>` | Post-processing of a segment failed (for example `container_missing`, `probe_failed`, `remux_failed`, `extract_failed:<role>`, `verify_failed:<role>`); the OBS original is kept if it exists |
| `integrity:file_missing:<file>` | A listed file was missing when the checksums were written; it was removed from the list |
| `integrity:encryption_failed` | `age` encryption failed; the plaintext file was kept |
| `hook:not_found`, `hook:error`, `hook:timeout`, `hook:failed:exit_<code>` | The `on_session_complete` hook failed (`docs/hooks.md`) |
| `import:time_from_mtime`, `import:metadata_not_stripped` | Import: start time from the file time; tags could not be removed losslessly (`docs/import-devices.md`) |

## 9. Security model

- obs-websocket: auth always on, a random ≥ 32-char password in the keyring, and the client
  connects to loopback only. The server offers no loopback-only bind (only `--websocket_ipv4_only`),
  so assume LAN reachability. With `exposure_policy: require_firewall`, arming is refused until
  `obs/firewall.py` confirms an inbound block rule for non-loopback sources on the websocket
  port. Applying the rule is an explicit, elevated, one-time command (`captation obs firewall --apply`).
- OBS egress: updater disabled via a verified launch flag; crash-log upload must be off
  (`doctor` check).
- IPC: user-only socket or pipe plus authkey file (0600 / user ACL).
- Hooks: argv lists, placeholders substituted as whole arguments, no shell, timeout.
- OBS itself fetches streaming-service lists at every start (`rtmp-services`, PF-023, L-19);
  no recording data is sent.
- No network use by the agent beyond the above (R-26, checked by end-to-end scenario E10).

## 10. Non-goals (v1)

- Recording calls taken **on the iPhone itself** in software: iOS gives apps no access to call
  audio. Those calls are recorded on mobile hardware and imported (§11).
- Cloud upload, transcription or analysis inside the agent. These are only available through hooks.
- Per-participant tracks in group meetings (use Zoom local recording if needed).
- Pre-roll or replay buffer (it would keep audio from before the countdown; docs/principles.md §2.9).
- Any stealth or covert recording capability.

## 11. Mobile recorder import

`captation import <path> --device <profile> --consent-ref <ref> [--started-at <ISO>] [--app-id <id>]`

| Device profile | Typical export | Notes |
|---|---|---|
| `plaud_note_pro` | Audio export from the Plaud app or device storage *(verify format)* | Disable Plaud cloud sync and AI for study data. Import local exports only |
| `viaim_recdot` | Audio export from the viaim app *(verify format)* | Check data location and DPA before study use |
| `recordergear_pr200` | MP3 on USB mass storage | WhatsApp/Signal support unconfirmed |
| `magmo_pro` | App export *(verify)* | The device may name files after phone numbers, so never keep them |
| `generic` | WAV, FLAC, MP3, M4A, Opus | – |

Rules:
- `consent_ref` is required.
- The original bitstream is copied **unchanged** (no transcoding) to `seg01_mix.<ext>`.
- The SHA-256 of the source is recorded, but **never** its file name or the device's metadata
  text fields (they can hold numbers or names).
- `started_at` comes from container metadata if present (`time_source: file_metadata`),
  otherwise `--started-at` (`user_input`), otherwise the file mtime (`file_mtime`, with a warning).
- QC runs as for OBS sessions. `origin.type = import`. The same integrity steps apply
  (checksums, read-only, schema validation).
