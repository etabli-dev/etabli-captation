# Known limitations and risks

| ID | Limitation or risk | Consequence | Mitigation |
|---|---|---|---|
| L-01 | **iPhone-native calls** cannot be recorded in software. iOS gives apps no access to call audio, and native call recording is unavailable in the EU | Calls taken on the iPhone itself are out of scope for the agent | Record them on mobile hardware (Plaud Note Pro, viaim RecDot, …) and import them (`captation import`) |
| L-02 | **Window titles are unavailable on Wayland** | Browser meetings cannot be told apart from other browser mic use by title | Browsers holding the mic stay category `browser` (audio, or video if the camera is on) |
| L-03 | **macOS may periodically re-confirm screen-recording permission** (ScreenCaptureKit) | Video capture can silently fail until the user confirms | `doctor` checks the permission; QC and warnings flag empty video (stage 06) |
| L-04 | **Bluetooth or speaker echo** | With speakers, remote voices bleed into the local-mic track; Bluetooth headsets switch to low-quality profiles while the mic is open | Use a wired headset. Documented in the README |
| L-05 | **obs-websocket has no loopback-only bind** | The websocket server is reachable from the LAN unless blocked | Mandatory firewall rule (`exposure_policy: require_firewall`, ADR-012), auth always on |
| L-06 | **Browser helper-process attribution** | The mic may be held by a helper or renderer process whose name differs from the browser | Normalisation to the parent app (`application.name`, parent PID chain) |
| L-07 | **The first seconds of a call are lost** (start debounce + countdown) | The very beginning of a call is not on the recording | Verbal consent must be stated **after** the indicator turns red. The start reminder says so |
| L-08 | **The call app may use a different microphone than `local_input`** | Wrong or silent local track | Configure `recording.audio.local_input` to the device the call apps use; QC raises `qc:local_silent` |
| L-09 | **OBS's own network egress** (updater, opt-in crash-log upload since 32.0) | OBS could contact the internet | Launch with the verified updater-disable flag; `doctor` checks that crash upload is off; stage 14 attributes connections per process |
| L-10 | **Linux distribution support of current OBS**: OBS 33 dropped Ubuntu 24.04 | Native OBS 33 packages may be missing on older distros | Use Ubuntu 26.04 or the Flatpak (ADR-006) |
| L-11 | **Mobile devices**: vendor cloud sync and AI features (Plaud, viaim) can upload study data | Data leaves the controlled environment | Keep vendor cloud and AI off for study data; import local exports only (`docs/import-devices.md`) |
| L-12 | **Late joiners in group meetings** are not covered by prior consent, and the tool cannot detect them | Recording people without consent | Consent workflow (stage 14): late joiners need their own consent or must be excluded; the start reminder names all participants |
| L-13 | **OBS 33 asks for Safe Mode after a crash** (`--disable-shutdown-check` no longer exists, PF-003) and Safe Mode disables WebSockets | After an OBS crash the agent cannot reconnect until someone answers the dialog | Recovery waits 30 s, then finalises the session as `interrupted` and notifies the user (ADR-013) |
| L-14 | **Wayland screen capture needs a one-time portal approval** | Video segments record a black canvas (`capture:not_approved`) until approved | Run `captation obs approve-screen` once (steps below). The restore token is stored and refreshed after every segment. Window targeting is not dynamic on Wayland, so the approved screen is captured (`capture:display_fallback`) |
| L-15 | **Window titles are not read on Linux** (Wayland has none; the X11 path could not be verified on the build machine, PF-019) | Browser meetings stay category `browser` | As L-02. An X11 path can be added once verified on an X11 session |
| L-16 | **Flatpak OBS cannot write to the host's `/tmp`** (PF-011) | Recording fails immediately | `data_root` must be outside `/tmp`; the controller refuses `/tmp` with a clear error |
| L-17 | **Camera attribution scans `/proc`** (~35 ms CPU per poll on a busy desktop, PF-017) | Small CPU cost | The scan runs only while an app holds the microphone; camera-only use never starts a recording anyway |
| L-18 | **`~/CaptationData` may sit on an unencrypted disk.** On the build machine `/` (incl. `/home`) is plain ext4; only `/data` is LUKS (PF-020) | `storage:disk_unencrypted` warning on every armed period | Set `data_root` to a folder on an encrypted volume (e.g. `/data/CaptationData`) or set `storage.require_disk_encryption: block` |
| L-19 | **OBS contacts obsproject.com, Twitch and Amazon IVS at every start** (bundled `rtmp-services` plugin fetches the streaming-service and ingest lists, PF-023) | The machine's IP address is visible to those services when OBS starts; no recording data is sent | Documented in `docs/security.md` and `docs/datenschutz.md`. Optional: block outbound traffic of the OBS sandbox at the network level (owner decision) |
| L-20 | **OBS ignores SIGTERM while recording** (it asks for confirmation) and **waits in a Safe Mode dialog after a crash** (PF-022) | After a crash the agent cannot reconnect until someone answers the OBS dialog | The agent finalises the session as `interrupted` after 30 s and shows a notification saying to choose *Run Normally* (ADR-013) |
| L-21 | **macOS: OBS needs Microphone and Screen & System Audio Recording permission** (TCC); macOS may ask again periodically (L-03). Pending PF-127 | Without them the local track or the remote/video tracks stay silent/black | First-run steps below; QC flags silent tracks |
| L-22 | **Windows: unsigned builds trigger SmartScreen** ("Windows protected your PC") on the first start of the installer | One extra click (*More info → Run anyway*) | Code signing (optional, release workflow); details in `docs/runbook-windows.md` §9 |
| L-23 | **Windows: the tray counts as available whenever pystray loads**; a hidden notification-area icon is not detected | The indicator may sit in the overflow area | Pin the icon (Settings → Personalisation → Taskbar → other icons); checked in the Windows smoke test |
| L-24 | **Windows Firewall does not filter loopback, including connections to the PC's own LAN address** (PF-232) | `obs firewall --check` cannot prove the block locally | Test once from a second device (`Test-NetConnection <pc> -Port 4455` must fail) |

## macOS first-run permissions (L-21, pending verification)

1. **OBS → Microphone**: asked when the first `Captation Local` input is created → *Allow*
   (System Settings → Privacy & Security → Microphone → OBS on).
2. **OBS → Screen & System Audio Recording**: asked when the first app-audio or screen input is
   created → enable OBS there; OBS may need a restart (quit it, the agent starts it again).
3. **Établi Captation → Screen Recording** (optional): only for window titles.
4. The agent and `captation-probe` must never cause a Microphone or Camera prompt; OBS is
   started through LaunchServices so macOS attributes the prompts to OBS (PF-103).

## Wayland screen-capture approval (L-14)

`captation obs approve-screen` creates the OBS screen source once; the desktop portal then asks
which screen to share.

- **GNOME:** in the dialog *"Screen Share"*, select the monitor, tick *"Remember this selection"*
  if shown, click *Share*. The screen-sharing indicator appears in the top bar while a video
  segment records.
- **KDE Plasma:** in *"Screen Sharing"*, select the screen, tick *"Allow restoring on future
  sessions"*, click *Share*.

If the dialog is cancelled, video segments keep recording a black canvas with the warning
`capture:not_approved` until the command is run again.
