# Runbook: macOS stage runs (03, 06, 11, 12)

macOS support was written and unit-tested on Linux (ADR-015). Nothing macOS-specific has run
yet. This runbook lists, in order, what remains on the Mac: build, run, verify every pending
fact (PF-101 … PF-128), collect the Definition-of-Done evidence and stop at the checkpoints.

Rules that still apply on the Mac: docs/principles.md §2–§6 (consent guard, privacy, OBS boundary,
verify-don't-assume). A fact is set to `verified` only with the command and an output excerpt
in its Evidence column. `tools/check_facts.py` fails on macOS until every referenced PF-1xx is
verified (that is intended).

## 0. What was prepared on Linux

| Area | File | Notes |
|---|---|---|
| OBS locator, OBS PIDs, crash-setting scan, Swift-probe client, FileVault, LaunchAgent | `src/captation/platform/macos.py` | `SERVICES = MacPlatform()` |
| Capture profile (ScreenCaptureKit audio/video, CoreAudio mic) | `src/captation/obs/kinds_macos.py` | `PROFILE`, `FACT_LIST_PROPERTIES`, `LAUNCH_FLAGS`, `VIDEO_ENCODERS_HARDWARE` |
| pf firewall | `src/captation/obs/firewall_macos.py` | anchor `com.apple/250.EtabliCaptation` + LaunchDaemon |
| Tray, sound, input dialog | `src/captation/ui/macos.py` | `PystrayUi` + `afplay` + `osascript` |
| Swift probe | `probes/macos/CaptationProbe/` | README has the JSON schema; `Samples/watch-sample.jsonl` |
| Packaging | `packaging/macos/{build.sh,captation.spec,entitlements.plist}`; `macos` job in `.github/workflows/release.yml` | ad-hoc by default; Developer ID + notarisation with secrets |
| Tests (run on Linux with fakes) | `tests/unit/test_macos_{probe,platform,obs,ui}.py` | `@pytest.mark.macos` tests run only on the Mac |
| Facts | `docs/platform-facts.md` rows PF-101 … PF-128 | all `pending` |

## 1. Prerequisites

Target: **macOS 26** (owner's Mac, ADR-016); per-process mic detection (macOS ≥ 14) applies.


1. Record the machine: `sw_vers` (macOS ≥ 14 expected, ADR-006), `uname -m` (arm64/x86_64).
2. Xcode Command Line Tools: `xcode-select --install`, then `swift --version` (Swift ≥ 5.9).
   `swift test` needs the full Xcode (XCTest is not part of the CLT); without Xcode, skip
   `swift test` and rely on the Python replay tests.
3. Homebrew, then `brew install uv ffmpeg git gh` (`which ffmpeg ffprobe` → `/opt/homebrew/bin/…`
   on Apple silicon).
4. **OBS Studio 33** from the official download page (https://obsproject.com/download), `.dmg`
   → drag *OBS* into `/Applications`. Start it once by hand, finish or skip the auto-configuration
   wizard, then **quit** it (the bootstrap writes the websocket config only while OBS is closed).
5. Hardware for stage 12: a headset, the built-in or a USB camera, the RØDECaster Duo, an iPhone
   signed in with the same Apple ID (for *Calls from iPhone*), a second device on the same LAN
   (for the firewall test).

## 2. Repository and local notes

```sh
mkdir -p ~/src && cd ~/src
gh repo clone etabli-dev/etabli-captation && cd etabli-captation
```

If you keep local, untracked build notes (build state, stage reports) on the Linux machine,
copy them into the repository root over a private channel; they are git-ignored.

## 3. Build the probe

```sh
cd probes/macos/CaptationProbe
swift build -c release 2>&1 | tail -5
codesign --force --sign - .build/release/captation-probe
.build/release/captation-probe --version
.build/release/captation-probe --once | python3 -m json.tool
.build/release/captation-probe --watch --interval-ms 500     # Ctrl-C to stop
swift test            # only with Xcode installed
cd -
```

If the build fails on an identifier (e.g. a CoreAudio/CoreMediaIO constant name or type), fix it
in `Sources/CaptationProbe/main.swift`, note the corrected identifier in the matching PF row
(PF-113 … PF-118) and in the README table. Expected first output while idle:
`"api":"process_objects","confidence":"high","mic_users":[]`.

## 4. Python environment and tests

```sh
uv sync
uv run pytest -m "not obs and not e2e" -q          # includes the @macos tests
uv run mypy --strict src && uv run ruff check src tests tools
uv run python tools/check_facts.py                 # fails until PF-1xx are verified: expected
```

`test_real_helper_once` needs step 3 done; `test_real_fdesetup_and_locator` needs OBS installed.

## 5. Shared code: state after the Linux merge (2026-11-23)

Done on Linux (verify behaviour on the Mac, the code paths are generic):

1. OBS exclusion by PID: `service.build_agent` calls `captation.platform.<os>.obs_host_pids`.
2. Facts dump: `kinds.fact_list_properties/fact_value_properties` read `kinds_macos`.
3. Hardware encoder: `kinds.video_encoders_hardware("macos")` → `kinds_macos.VIDEO_ENCODERS_HARDWARE` (PF-111).
4. Launch flags: `launcher.launch_args` uses only flags in `kinds_macos.LAUNCH_FLAGS` (PF-104), so
   the release gate covers them.
5. Untargetable apps (no bundle ID): `CaptureProfile.can_target_app` → the controller falls back
   to desktop audio **with** `remote:desktop_fallback`.
6. `doctor`: crash-upload check via `platform.macos.crash_upload_settings` (PF-106), no
   approve-screen check (`needs_screen_token` false), macOS fix texts, probe closed after use.
7. `captation run` exits 0 when another agent already runs (no launchd restart loop).
8. `obs facts` uses `firewall_macos.listening_publicly` (`lsof`) through `firewall.listening_publicly`.
9. `docs/security.md` (pf artefacts) and `docs/known-limitations.md` (L-21, first-run permissions)
   contain the macOS sections; replace them with what you observe.
10. OBS install hint and app-audio fix text are macOS-specific.

Still open on the Mac: wire `MacProbe(obs_capturing=…)` only if the low-confidence path is used
(macOS < 14, PF-114); `doctor` check for the probe helper's path and signature.

## 6. Stage 03 on macOS: bootstrap, facts, firewall ⛳ (checkpoint only if something deviates)

```sh
ls ~/Library/Application\ Support/obs-studio > /tmp/obs-before.txt          # PF-102
cp -R ~/Library/Application\ Support/obs-studio /tmp/obs-config-before
uv run captation obs bootstrap
uv run captation obs bootstrap                                               # idempotent
diff -r /tmp/obs-config-before ~/Library/Application\ Support/obs-studio | head -40
uv run captation obs facts --out docs/platform-facts.md
```

- The first bootstrap starts OBS through `open -g -a /Applications/OBS.app --args …` (PF-103).
  The second run must change nothing: rerun `diff -r` between a copy taken after run 1 and the
  state after run 2 (logs and window geometry may differ; profiles, scenes and websocket config
  must not).
- Show non-Captation profiles and collections unchanged (listing before and after).
- Password hygiene (DoD 6), without putting the password on a command line:
  `security find-generic-password -s etabli-captation -a obs-websocket -w > /tmp/pw && git grep -n -F -f /tmp/pw; grep -rF -f /tmp/pw ~/Library/Logs/captation reports; rm -f /tmp/pw`
  → no match; `ps -axww -o args | grep -i password` → nothing.
- 10 s test recordings in `Captation-Audio` and `Captation-Video` (`ffprobe` excerpt; PF-112).
- Mic release (ADR-010, PF-112): `captation-probe --once` with OBS idle → OBS absent; create
  `Captation Local` (start a segment or use the bootstrap test) → OBS present; after teardown →
  absent; the orange menu-bar mic indicator follows.

### Firewall (PF-105, PF-123 … PF-125)

```sh
lsof -nP -iTCP:4455 -sTCP:LISTEN                    # expect *:4455 (PF-105)
grep anchor /etc/pf.conf                            # expect anchor "com.apple/*" (PF-123)
uv run captation obs firewall --check               # expect: rules missing → not ok
uv run captation obs firewall --apply               # sudo; asks once
sudo pfctl -s info | head -1                        # Status: Enabled
sudo pfctl -a com.apple/250.EtabliCaptation -sr     # both rules (port = 4455)
cat /var/run/de.etabli.captation.pf.token           # a number
sudo launchctl print system/de.etabli.captation.pf | grep 'last exit code'   # 0
uv run captation obs firewall --check               # blocked from every non-loopback address
nc -vz -w 3 <mac-lan-ip> 4455                       # from a second LAN device: timeout
uv run captation obs firewall --apply               # re-apply is idempotent
sudo reboot                                         # then: -sr shows the rules again, --check ok
uv run captation obs firewall --remove              # files, token, rules gone
uv run captation obs firewall --check               # not ok again → re-apply for the next stages
```

Also check whether `launchctl print system/de.etabli.captation.pf` works **without** sudo; if it
does, `--check` can additionally require the job (record it in `docs/decisions.md`).

## 7. Stage 06 on macOS: detection

1. Probe DoD 1: `captation-probe --once` → valid JSON with `confidence:"high"` (or `low` with the
   reason documented).
2. `uv run captation doctor --probe` streams snapshots. Run a Signal or WhatsApp call and a
   *Calls from iPhone* call: each appears within 2 polls and disappears within 2 polls after
   hang-up (DoD 2). OBS idle and running is never a mic user (DoD 3).
3. Per-app audio isolation (DoD 4, PF-108): during a recorded call, play a test tone from another
   app (`afplay /System/Library/Sounds/Ping.aiff` in a loop, or `ffplay -f lavfi sine=440`);
   extract the remote track and show it does not contain the tone:
   `ffmpeg -i segNN_container.mkv -map 0:a:1 -af astats -f null - 2>&1 | grep -E 'RMS level|Peak level'`
   compared with a desktop-audio run.
4. Empirical checks: iPhone relay process (PF-128, `phone_relay` rule), Zoom / Teams / Webex /
   Signal / WhatsApp / Meet in Safari and Chrome (PF-117, PF-128), TCC (PF-127). Correct the
   `verify: true` rules in `specs/rules.default.yaml`, run `uv run python tools/sync_specs.py`.
5. Replace `probes/macos/CaptationProbe/Samples/watch-sample.jsonl` with a recorded, sanitised
   sample (no titles, names or numbers of people) and re-run the Python tests; add a
   `tests/fixtures/macos/` replay if more scenarios are useful.

## 8. Verify and flip the facts

For each row: run the command, compare with the expected value, then edit the row in
`docs/platform-facts.md`: `pending` → `verified`, fill *OS version* (`sw_vers -productVersion`),
*OBS / ws version*, *App + version*, replace the Evidence text with the command and an output
excerpt, set the date. If the value differs: set the row to `refuted` with a pointer, add a new
PF-1xx row with the correct value, change the code (and the `# PF-1xx` comment) and re-run the
tests. Finally `uv run python tools/check_facts.py` (on the Mac) and
`uv run python tools/check_facts.py --release` must both print `facts ok`.

| PF | What | Command on the Mac | Expected |
|---|---|---|---|
| PF-101 | OBS bundle, executable, bundle ID, version, process name, ps paths | `defaults read /Applications/OBS.app/Contents/Info CFBundleIdentifier CFBundleShortVersionString`; `pgrep -x OBS`; `ps -axo pid=,comm= \| grep OBS.app/` | `com.obsproject.obs-studio`, `33.x`; one PID; full paths |
| PF-102 | config dir | `ls ~/Library/Application\ Support/obs-studio` | `basic global.ini logs plugin_config user.ini` |
| PF-103 | LaunchServices start, TCC responsibility | `ps -o args= -p $(pgrep -x OBS)` after `captation obs bootstrap`; System Settings → Microphone list; `sudo launchctl procinfo $(pgrep -x OBS) \| grep -i responsible` | flags present; *OBS* listed; responsible = OBS |
| PF-104 | CLI flags | `/Applications/OBS.app/Contents/MacOS/OBS --help` | the five flags; no `--disable-shutdown-check` |
| PF-105 | websocket bind | `lsof -nP -iTCP:4455 -sTCP:LISTEN` | `*:4455 (LISTEN)` |
| PF-106 | crash-upload key | copy ini files, toggle Settings → General option, `diff` | the key name and values |
| PF-107 | input kinds | `captation obs facts` | four kinds present |
| PF-108 | `sck_audio_capture` settings | facts dump list items; isolation test (§7.3) | `type` 0/1, `application` = bundle IDs; no tone on remote |
| PF-109 | mic device property | facts dump | `device_id` default `default`, UIDs |
| PF-110 | `screen_capture` settings | facts dump; a video segment of a Zoom call | `type` 0/1/2; only Zoom windows in the video |
| PF-111 | encoders | facts dump | `ffmpeg_pcm_s24le`, `obs_x264`, `com.apple.videotoolbox.videoencoder.ave.avc` |
| PF-112 | strategy B, mic release | §6 recordings, `ffprobe`, `top -l 5 -pid $(pgrep -x OBS)` | 3 PCM tracks + 128×72; CPU < 5 %; mic released |
| PF-113 | CoreAudio process objects | `captation-probe --once` idle and in a call | high confidence, app listed, no TCC prompt |
| PF-114 | fallback path | `captation-probe --once --force-fallback` in a call | `device_running`, `low`, frontmost app |
| PF-115 | camera | Photo Booth open/closed + `--once` | `camera_active` true/false |
| PF-116 | process identity | probe output in a Zoom call | name `zoom.us`, path, bundle ID |
| PF-117 | helpers | Meet in Chrome and in Safari, Signal call | Chrome helper → `parent_bundle_id` `com.google.Chrome`; record Safari/Signal |
| PF-118 | window titles | `captation-probe --once --titles` before/after granting Screen Recording | `permission_missing` → `ok` |
| PF-119 | probe locations | `ls .build/release/captation-probe`; packaged `doctor` | binary present; probe ok from the app |
| PF-120 | `fdesetup` | `fdesetup status` | `FileVault is On.` |
| PF-121 | data volume, `diskutil` key | `stat -f %d ~ /System/Volumes/Data`; `diskutil info -plist ~ \| plutil -extract FileVault raw -` | equal; `true` |
| PF-122 | LaunchAgent | §11 | loaded, running, disarmed after login |
| PF-123 | pf anchor, `pfctl` behaviour | §6 firewall block | rules active, LAN blocked |
| PF-124 | LaunchDaemon, reboot | §6 firewall block | exit code 0; rules after reboot |
| PF-125 | `ifconfig` format, lo0 filtering | `ifconfig`, `firewall --check` before/after remove | blocked / reachable |
| PF-126 | sound, dialog, tray, notifications | `afplay …`; tray arm dialog; packaged vs venv `doctor` | sound; text returned/cancel; notifications only from the app |
| PF-127 | TCC first run | §10 | permissions as listed |
| PF-128 | call-app identities | `doctor --probe` in each call | names/bundle IDs recorded, rules corrected |

## 9. Firewall artefacts (for `docs/security.md`)

| Artefact | Content |
|---|---|
| `/etc/pf.anchors/de.etabli.captation` | `block drop in quick proto tcp from ! 127.0.0.1 to any port <port>` and `block drop in quick inet6 proto tcp from ! ::1 to any port <port>` |
| `/Library/LaunchDaemons/de.etabli.captation.pf.plist` | root job, `RunAtLoad`: loads the stock `/etc/pf.conf` only if its `com.apple` anchor is missing, releases its previous pf reference, `pfctl -E` (token → `/var/run/de.etabli.captation.pf.token`), loads the anchor `com.apple/250.EtabliCaptation` |
| runtime | the anchor's rules and one pf enable reference |

`/etc/pf.conf` is never edited. `--remove` unloads the job, flushes the anchor, releases the
reference, deletes the token, the rule file and the plist.

## 10. TCC permissions (first-run flow, for `docs/known-limitations.md`)

Record every prompt that actually appears (app named, permission) and turn this into the
click-by-click section of `docs/known-limitations.md`:

1. **OBS → Microphone**: appears when the first `Captation Local` input is created. Click
   *Allow*. Check: System Settings → Privacy & Security → Microphone → *OBS* on.
2. **OBS → Screen & System Audio Recording**: appears when the first `sck_audio_capture` or
   `screen_capture` input is created. System Settings → Privacy & Security → Screen & System
   Audio Recording → *OBS* on; OBS may need a restart (quit it; the agent starts it again).
3. **Établi Captation → Screen Recording** (optional): only for window titles (browser meeting
   detection by title). Without it, browsers holding the mic stay category `browser`.
4. The probe and the agent must **not** trigger Microphone or Camera prompts. If a prompt names
   *Établi Captation* or *Terminal* for the microphone, OBS was not started through
   LaunchServices (PF-103): stop and report.
5. macOS 15: note whether a periodic re-confirmation of Screen Recording for OBS appears (L-03)
   and after how long.

## 11. Stage 11 on macOS: autostart, doctor, package

**Disclaimer gate first (R-59, ADR-017).** `uv run captation arm` must be refused with
`terms_not_accepted` before acceptance. Then `uv run captation accept`: the Tk dialog opens in
front, the checkbox stays disabled until the text is scrolled to the end, *Akzeptieren* is
disabled until the box is ticked; *Ablehnen* leaves `captation accept --status` at exit 2. After
accepting, `--status` shows the version and `doctor` reports `terms` as ok. Repeat once with the
packaged app (`dist/…/captation accept`) to confirm Tk is bundled.

```sh
uv run captation install-autostart
uv run captation install-autostart                     # idempotent: no change, no reload
plutil -lint ~/Library/LaunchAgents/de.etabli.captation.plist
launchctl print gui/$(id -u)/de.etabli.captation | grep -E 'state|pid'
# log out and in (or reboot), then:
uv run captation status                                # running, DISARMED (DoD 1)
ls ~/Library/LaunchAgents ~/Library/Logs/captation > /tmp/before.txt
uv run captation uninstall-autostart
ls ~/Library/LaunchAgents ~/Library/Logs/captation > /tmp/after.txt; diff /tmp/before.txt /tmp/after.txt
uv run captation doctor                                # DoD 3: ok or explained warnings
packaging/macos/build.sh                               # ad-hoc signed .app, zip, dmg
```

Packaged build (DoD 4): open the dmg, drag *Établi Captation* to `/Applications`, first start
via right-click → *Open* (ad-hoc build), then:

```sh
APP="/Applications/Établi Captation.app/Contents/MacOS/captation"
codesign --verify --strict --verbose=2 "/Applications/Établi Captation.app"
"$APP" doctor            # tray ok; notifications ok with actions (signed bundle, PF-126)
"$APP" install-autostart # the LaunchAgent now points into the app
"$APP" status
```

Check the menu-bar icon (grey → yellow when armed → red while recording), the countdown
notification with *Abbrechen*, and the start reminder. With a Developer ID certificate, set
`MACOS_SIGN_IDENTITY` (and notary credentials) and rebuild; verify the notarised build starts
**without** `com.apple.security.cs.allow-unsigned-executable-memory` in
`packaging/macos/entitlements.plist`; remove the key if it is not needed.

## 12. Stage 12 on macOS: E2E and real apps ⛳ (checkpoint)

The e2e harness (`tests/e2e/`) is Linux-only today (`pw-cat`, `pw-loopback`, v4l2loopback, a
D-Bus stub). A macOS variant needs, per scenario:

- fake call app: a copy of `ffmpeg` named `fakecall-e2e` / `fakemeet-e2e` that reads the default
  input (`-f avfoundation -i ":0" -f null -`) while a second process plays a 1 kHz tone to the
  default output; the copy has no bundle ID, so the remote track uses desktop audio (warning,
  see §5 item 5) and isolation is checked in the real-app rows instead. The first run asks for
  the microphone permission of the terminal app: allow it;
- test microphone: BlackHole 2ch (`brew install blackhole-2ch`) as a virtual input fed with a
  1320 Hz tone, or the RØDECaster;
- camera (E3): the real camera (open Photo Booth after 10 s), else skip with the reason;
- network per process (E10): `nettop -P -L 1 -p <pid>` or `lsof -nP -i -a -p <pid>` for the
  agent, the probe and OBS; expected: the agent only `127.0.0.1:4455`, the probe none, OBS none
  (updater disabled) except the documented `rtmp-services` downloads at OBS start (PF-023 on
  Linux; record the macOS equivalent);
- E12: `firewall --remove` → `captation arm` refused; `--apply` → arm works; LAN connect fails;
- E13: simulate no tray (start the agent without the UI backend) → arming refused.

E4, E5, E9, E10, E12 and E13 can never be skipped. Record start latency, the audio → video gap,
CPU and RAM (idle-armed, recording audio, recording video) with `top -l` or Activity Monitor.

### Real-app checklist (macOS) → `reports/E2E-macos.md`

| App / path | Detected app ID | Mode | Start latency | Remote isolation (no system sounds) | Echo on local track (headset / speakers) | Issues |
|---|---|---|---|---|---|---|
| Zoom desktop meeting (video) | | | | | | |
| Teams meeting (video) | | | | | | |
| Meet or Jitsi in a browser (Chrome and Safari) | | | | | | |
| Signal Desktop voice → video | | | | | | |
| WhatsApp Desktop voice call | | | | | | |
| iPhone call via *Calls from iPhone* | | | | | | |
| RØDECaster Duo as audio device / external input | | | | | | |

For each row also note the process name and bundle ID from `doctor --probe` (PF-128) and the
OBS app-audio target that was used. All `verify: true` rules relevant for macOS must be
confirmed or corrected before the checkpoint.

## 13. Definition of Done items that remain on the Mac

| Stage | DoD item | Evidence |
|---|---|---|
| 03 | 1 bootstrap succeeds and is idempotent | §6 command output + `diff -r` excerpt |
| 03 | 2 macOS facts section written | `git diff docs/platform-facts.md` (facts block `facts:macos`) |
| 03 | 3 10 s test recordings per profile | `ffprobe` excerpts |
| 03 | 4 ADR-009/ADR-010 measured on macOS | PF-112 row + ADR notes |
| 03 | 5 non-Captation profiles untouched | listings before/after |
| 03 | 6 password not in repo, logs, process list | `git grep`, log grep, `ps` (empty) |
| 03 | 7 `firewall --check` passes; crash upload off | `--check` output; PF-106 key value |
| 06 | 1 probe JSON high (or low with reason) | `--once` output |
| 06 | 2 live calls appear/disappear within 2 polls | `doctor --probe` excerpt with timestamps |
| 06 | 3 OBS never a mic user | `doctor --probe` with OBS idle |
| 06 | 4 per-app audio isolation | `astats` excerpt |
| 06 | 5 rules corrected, TCC flow documented, tests green | rules diff, known-limitations diff, `pytest`/`swift test` |
| 11 | 1 disarmed after login | `captation status` after re-login |
| 11 | 2 autostart idempotent, no residue | listings before/after |
| 11 | 3 `doctor` ok or explained warnings | `doctor` output |
| 11 | 4 packaged build: tray, `status` | screenshots optional, `status` output |
| 12 | 1–5 E1–E13, real-app checklist, no `verify: true`, full suite, traceability | `reports/E2E-macos.md`, `uv run python tools/traceability.py` |

Each stage ends with `reports/STAGE-NN.md` (macOS run), an updated `STATE.md` and one commit
(`stage NN (macOS): <title>`), per docs/principles.md §7.

## 14. Checkpoint questions for the owner

1. Which macOS version and Mac model is the reference (ADR-006 minimum stays macOS 14)?
2. Is the TCC flow acceptable (OBS: Microphone + Screen & System Audio Recording; agent: no
   permission, optional Screen Recording for browser titles)? Grant Screen Recording to the
   agent for title-based Meet/Jitsi detection, or keep browsers as category `browser`?
3. Application capture (all windows of the meeting app) instead of single-window capture: OK
   for ADR-008, or require single-window capture (needs CGWindowIDs from the probe, more work)?
4. *Calls from iPhone*: if the call audio only comes through a system daemon, is the
   desktop-audio fallback for the remote track acceptable (warning in `session.json`)?
5. Firewall: is a root LaunchDaemon plus pf anchor acceptable, and is pf being enabled
   system-wide (with our own reference) acceptable on this Mac (VPN or other pf users)?
6. Distribution: ad-hoc signed builds only, or set up a Developer ID certificate and notarisation
   secrets (`MACOS_CERT_P12_BASE64`, `MACOS_CERT_PASSWORD`, `MACOS_SIGN_IDENTITY`,
   `MACOS_NOTARY_APPLE_ID`, `MACOS_NOTARY_TEAM_ID`, `MACOS_NOTARY_PASSWORD`)? `SMAppService`
   (ADR-011) instead of a LaunchAgent plist once the app is signed?
7. Any fact refuted on the Mac that changes a decision (e.g. ScreenCaptureKit audio of browser
   helpers not captured by bundle ID → desktop fallback for browser meetings)?
