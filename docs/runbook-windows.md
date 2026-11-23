# Windows runbook: the remaining steps on the Windows PC

Windows support was prepared on Linux (ADR-015): the probe, autostart, firewall rule, BitLocker
check, OBS locator, tray back end, IPC pipe and packaging exist and are unit-tested with fakes.
Every Windows identifier the code relies on is a **`pending`** entry PF-201…PF-270 in
`docs/platform-facts.md`. On Windows, `tools/check_facts.py` treats each pending Windows fact as
an error, so Windows cannot be released until every fact is verified (or refuted and replaced)
with evidence from this PC.

Run the steps in order. Every step says how to evidence it.
Never mark a fact `verified` without the command and an excerpt of its output (docs/principles.md §6, §7).

| Prepared file | Purpose |
|---|---|
| `src/captation/platform/windows.py` | OBS locator, ConsentStore probe (`WindowsProbe`), Task Scheduler autostart + AUMID, BitLocker check, OBS PID exclusion, diagnostics (`python -m captation.platform.windows`) |
| `src/captation/obs/kinds_windows.py` | Capture profile: WASAPI app/desktop/mic audio, window capture (WGC), display fallback |
| `src/captation/obs/firewall_windows.py` | `captation obs firewall --check/--apply/--remove` on Windows |
| `src/captation/ui/windows.py` | pystray tray, desktop-notifier toasts under the AUMID, beep, consent-reference dialog |
| `src/captation/ipc.py` (Windows branch) | Per-user named pipe |
| `packaging/windows/` | `build.ps1`, PyInstaller spec, Inno Setup script |

## 0. Shared code prepared on Linux

The shared code already contains the Windows adaptations (verify the behaviour on Windows):

1. `obs/launcher.py` starts OBS with `cwd` = the folder of `obs64.exe` (PF-205).
2. `service.build_agent` excludes OBS and its helpers via `platform.windows.obs_host_pids`.
3. The facts dump reads `kinds_windows.FACT_LIST_PROPERTIES`; `priority`/`method` values are printed.
4. `obs facts` uses `firewall_windows.listening_publicly` through `firewall.listening_publicly`;
   the `--apply` confirmation mentions the UAC prompt; `install-autostart` reports errors.
5. `doctor`: no `screen_capture` check on Windows, crash-upload check via the platform module,
   Windows fix texts (tray, app audio, OBS installer).
6. `tests/e2e/rules.e2e.yaml` matches `fakecall-e2e(.exe)`.
7. `mypy --strict src --platform win32` is clean (POSIX paths guarded with `sys.platform`).
8. New: launch flags (`kinds_windows.LAUNCH_FLAGS`, PF-206) and hardware encoders
   (`kinds_windows.VIDEO_ENCODERS_HARDWARE`, PF-245 pending) are read per OS.

Still open on Windows: the tray-hidden case (L-23) and the second-device firewall test (L-24).

## 1. Prerequisites

| Item | How | Check |
|---|---|---|
| Windows 10 22H2 or 11 (record the build) | – | `[Environment]::OSVersion.Version`; `winver` |
| A standard user account, plus admin credentials for the one UAC prompt (firewall) | – | `whoami /groups` shows no elevated token in a normal shell |
| OBS Studio 33 from the official installer, default path | `winget install --id OBSProject.OBSStudio -e` or <https://obsproject.com/download> | `Test-Path "$env:ProgramFiles\obs-studio\bin\64bit\obs64.exe"` |
| uv | `winget install --id astral-sh.uv -e` | `uv --version` |
| Python 3.12 via uv | `uv python install 3.12` | `uv python find 3.12` |
| Git | `winget install --id Git.Git -e` | `git --version` |
| ffmpeg and ffprobe on PATH | `winget install --id Gyan.FFmpeg -e` (open a new shell afterwards) | `ffmpeg -version`; `ffprobe -version` |
| Inno Setup 6.3+ (only for the installer) | `winget install --id JRSoftware.InnoSetup -e` | `Test-Path "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe"` |
| A second device on the same LAN | – | needed once for the firewall test (PF-232) |
| Headset, webcam, an external audio interface if one is used, call partners for Zoom, Teams, Meet/Jitsi, Signal, WhatsApp | – | real-app checklist (§7) |

Start OBS once from the Start menu, cancel the auto-configuration wizard and close OBS again.
If Windows Defender Firewall asks whether OBS may communicate on networks, choose **Cancel**
(no allow rule is needed: the agent connects over loopback, which the firewall never filters).

## 2. Code and the test suite

```powershell
git clone https://github.com/etabli-dev/etabli-captation.git C:\src\etabli-captation
cd C:\src\etabli-captation
```

Use a path outside OneDrive.

```powershell
uv sync
uv run pytest -m "not obs and not e2e"          # includes the windows-marked tests
uv run ruff check src tests tools
uv run mypy --strict src                        # see §0 item 7
uv run python tools/check_facts.py              # lists every pending Windows fact as an error
```

`check_facts.py` on Windows is the to-do list for section 8. The windows-marked unit tests
already exercise real APIs: registry round trip under HKCU, ConsentStore read, psutil, AUMID,
primary monitor name and a named-pipe round trip (PF-210, PF-215, PF-223, PF-244, PF-260).

**Disclaimer and configuration (before the OBS setup).** Accept the disclaimer first (the gate
itself is checked in section 5), then create the user configuration. On Windows the config file
is `%LOCALAPPDATA%\etabli\captation\config.yaml` (platformdirs `user_config_dir`):

```powershell
uv run captation accept                         # dialog with checkbox; details in section 5
New-Item -ItemType Directory -Force "$env:LOCALAPPDATA\etabli\captation" | Out-Null
Copy-Item config\config.example.yaml "$env:LOCALAPPDATA\etabli\captation\config.yaml"
# edit: data_root on a BitLocker-encrypted drive (PF-225/226), project_id
uv run captation check-config                   # ok <digest>
```

Then work through sections 3 to 7 in order; section 8 lists every fact to flip.

## 3. OBS setup and platform facts: bootstrap, facts, firewall

1. **Locate OBS** (PF-201…PF-204): `uv run captation doctor` → `obs ✅ OBS 33.0.x (native)`.
   Verify the registry and version facts with the PF-202/PF-203 commands.
2. **Bootstrap** (OBS closed): `uv run captation obs bootstrap`. Prove idempotency:
   ```powershell
   $cfg = "$env:APPDATA\obs-studio"
   Get-ChildItem -Recurse -File $cfg -Exclude *.txt | Get-FileHash | Select Hash, Path | Export-Csv before.csv
   uv run captation obs bootstrap
   Get-ChildItem -Recurse -File $cfg -Exclude *.txt | Get-FileHash | Select Hash, Path | Export-Csv after.csv
   Compare-Object (Import-Csv before.csv) (Import-Csv after.csv) -Property Hash, Path   # empty
   ```
   List profiles and scene collections before and after (`Get-ChildItem $cfg\basic\profiles`,
   `…\basic\scenes`): non-Captation entries unchanged. Delete the CSV files afterwards.
3. **Launch flags** (PF-205, PF-206): with OBS started by the agent,
   `Get-CimInstance Win32_Process -Filter "Name='obs64.exe'" | Select CommandLine` → verified flags
   only, `--disable-updater` present, **no password**.
4. **Facts dump**: `uv run captation obs facts --out docs/platform-facts.md` writes the
   `facts:windows` section (needs §0 items 3 and 4). Check it lists the five kinds of PF-240 and
   the defaults/items of PF-241…PF-244, and contains no device names or window titles.
5. **Firewall** (PF-208, PF-230…PF-232):
   ```powershell
   uv run captation obs firewall --check     # NOT MET (rule missing)
   uv run captation obs firewall --apply     # confirm, then a UAC prompt appears: click Yes
   uv run captation obs firewall --check     # ok; the note says why no self-connect test is done
   Test-NetConnection 127.0.0.1 -Port 4455   # TcpTestSucceeded : True (loopback still works)
   ```
   From the second device: `nc -vz <pc-lan-ip> 4455` (or `Test-NetConnection <ip> -Port 4455`)
   → refused or timed out. Optionally `--remove` (UAC again) → the remote test connects → `--apply`
   again. Declining the UAC prompt once must end with "UAC prompt declined" (PF-230).
6. **Test recordings** (ADR-009 on Windows): in OBS select profile `Captation-Audio`,
   record 10 s, then `Captation-Video`, 10 s. `ffprobe -hide_banner <file>.mkv` → audio profile:
   `h264 128x72 30 fps` + 3 × `pcm_s24le 48000 Hz`; CPU while recording:
   `Get-Counter '\Process(obs64)\% Processor Time' -SampleInterval 2 -MaxSamples 4` (divide by the
   core count; target < 5 % of one core). If strategy B fails here, re-test A and update ADR-009.
7. **Mic release** (ADR-010): OBS running idle with the Captation collection →
   Settings → Privacy & security → Microphone shows OBS **not** "Currently in use", and
   `uv run python -m captation.platform.windows` shows no `obs64.exe` row with `"in_use": true`.
   Creating `Captation Local` (start a segment) → in use; stop → released.
8. **Egress** (PF-207): OBS Help menu offers only a manual crash-log upload; `--disable-updater`
   in the command line (step 3).
9. **Secrets**: `git grep -n "<password prefix>"` → nothing; search the agent log
   (`Select-String -Path "$env:LOCALAPPDATA\etabli\captation\Logs\*" -Pattern <prefix>`, the
   platformdirs log dir unless `logging.dir` is set) and the OBS logs → nothing; process list
   from step 3 → nothing.

If a fact deviates from Linux in a way that changes code paths (for example strategy B fails,
or OBS binds loopback-only), add it to the open questions (section 10) before continuing.

## 4. Detection on Windows

1. **Hive check (PF-210…PF-212)**: run
   `uv run python -m captation.platform.windows --watch 1` in one window while you start and end
   calls. Each row carries `hive`, `capability`, `app` (exe basename or package family), `in_use`
   and `stale`. Compare every in-use row with Settings → Privacy & security → Microphone / Camera
   ("Currently in use"). Record per app type which hive shows the live state.
2. **Live probe**: `uv run captation doctor --probe` during a real call prints one JSON line per
   poll (`mic`, `camera_active`, `camera`). A Signal or WhatsApp call **and** a Zoom or
   Teams meeting each appear within 2 polls after the call starts and disappear within 2 polls
   after it ends. Keep the timestamped excerpts (app IDs only, no titles).
3. **OBS idle**: OBS running, no call, 60 s of `doctor --probe` → OBS never listed.
4. **Per-app audio isolation** (PF-241): during a Zoom call, play a test tone from another
   program (for example `ffplay -nodisp -f lavfi -i "sine=frequency=1000"`). After the segment:
   ```powershell
   ffmpeg -hide_banner -i seg01_remote.flac -af "bandpass=f=1000:width_type=h:w=50,volumedetect" -f null - 2>&1 | Select-String max_volume
   ```
   The remote track shows no 1 kHz peak (max_volume far below the tone's level in a direct
   recording); the call partner is audible.
5. **Browser and WebView2 attribution** (PF-214, R-07): Meet in Chrome and in Edge, Teams call:
   `probe_users` shows the browser's main process or `ms-teams.exe`, never a helper.
6. **Window titles** (PF-216): the Meet-in-browser classification (`google_meet`) needs titles;
   check that the session's `app_id` is `google_meet` while titles stay out of `session.json`
   unless `detection.store_window_titles` is true.
7. **Fixtures**: export the ConsentStore keys during a call
   (`reg export "HKCU\Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore" cs-hkcu.reg`,
   same for HKLM), replace the user name in every path with `alice`, convert them to JSON under
   `tests/fixtures/windows/` and add tests that load them into `FakeRegistry`
   (`tests/unit/test_windows_probe.py`). Delete the raw `.reg` exports.
8. **Rules**: confirm or correct the `verify: true` entries for Windows (Teams package
   `MSTeams_…`, WhatsApp `5319275A.WhatsAppDesktop_…`, Signal, Webex, Jitsi), each with a new
   PF-2xx row (Windows build + app version + evidence).

## 5. Tray smoke test on Windows (tray, toasts, dialog)

**Disclaimer gate first (R-59, ADR-017):** withdraw the acceptance from section 2
(`uv run captation accept --revoke`); with the agent running, arming must be refused with
`terms_not_accepted`. `uv run captation accept` opens the Tk dialog: the checkbox unlocks only
after scrolling to the end, *Akzeptieren* only while it is ticked, *Ablehnen* accepts nothing.
After accepting, `captation accept --status` exits 0 and `doctor` reports `terms` as ok. The
installer's last page offers the same dialog (checked by default); confirm it opens from there.

`uv run captation run` in a normal shell (or the packaged `captationw.exe run`):

- tray icon visible (pin it to the taskbar corner), grey when disarmed (PF-250);
- tray → "Studienmodus aktivieren…" → the consent-reference dialog opens in front; Cancel
  leaves the agent disarmed; OK with a reference arms it, the icon turns yellow (PF-252);
- simulated or real call → countdown toast from "Établi Captation" with *Abbrechen*; pressing it
  cancels (`status: cancelled`, no media) (PF-223); with notifications turned off for the app
  (Settings → System → Notifications), the fallback beeps (PF-251) and the tray menu offers
  "Start abbrechen";
- second attempt records: icon red, tooltip with mode, app ID and elapsed time; the start
  reminder toast appears; "Studienmodus beenden" stops and finalises.

## 6. Autostart, doctor, packaging

1. **Autostart** (PF-220…PF-224):
   ```powershell
   uv run captation install-autostart; uv run captation install-autostart   # idempotent
   schtasks /Query /TN "Etabli Captation" /XML
   Get-ItemProperty 'HKCU:\Software\Classes\AppUserModelId\de.etabli.captation'
   uv run captation uninstall-autostart; uv run captation uninstall-autostart   # 2nd: nothing to remove
   schtasks /Query /TN "Etabli Captation"; $LASTEXITCODE                      # 1
   Test-Path 'HKCU:\Software\Classes\AppUserModelId\de.etabli.captation'      # False
   ```
   The running agent re-creates the AUMID key at its next start; the installer's uninstaller
   removes it for good.
2. **Logon**: install autostart, sign out and in (or reboot) → no console window,
   tray icon present, `uv run captation status` → `DISARMED`.
3. **Restart on failure** (PF-222): `taskkill /F /IM pythonw.exe` (dev) or `captationw.exe`
   (packaged) → note whether the task restarts the agent within about a minute.
4. **BitLocker** (PF-225, PF-226): `captation doctor` → `disk_encryption`; cross-check with an
   elevated `Get-BitLockerVolume -MountPoint C:`.
5. **doctor on a clean account**: create a local standard user, sign in, install the
   packaged build there, run `captation doctor`. Expected explained warnings: `autostart` (until
   installed), `obs.websocket` (OBS not running), `rules` (`verify: true` until the real-app
   checklist in section 7 is done).
6. **Packaged build** (PF-270):
   ```powershell
   powershell -ExecutionPolicy Bypass -File packaging\windows\build.ps1 -Installer
   ```
   Install `dist\etabli-captation-<version>-windows-x86_64-setup.exe` as a standard user (no UAC
   prompt; SmartScreen, see section 9), optionally with "Start at login". Start it from the
   Start menu → tray icon; `& "$env:LOCALAPPDATA\Programs\Etabli Captation\captation.exe" status`
   answers; `Get-StartApps | Where-Object Name -like 'Etabli*'` → `AppID de.etabli.captation`.
   Uninstall via Settings → Apps: the task and the AUMID key are gone; the firewall rule stays
   (remove it with `captation obs firewall --remove` before uninstalling, if wanted).
   Quit the agent from the tray before uninstalling or upgrading.

## 7. End-to-end checks and real apps

The automated harness in `tests/e2e/` is Linux-only (PipeWire, D-Bus stubs, v4l2loopback).
On Windows run the scenarios manually or port the harness; E4, E5, E9, E10, E12 and E13 can
never be skipped.

**Simulated call app.** A copy of the base interpreter named `fakecall-e2e.exe` (the probe sees
the exe name, so it must be a real copy, not the venv launcher) that opens the default
microphone through MCI, plays a 1 kHz tone and shows a window (Application Audio Capture
targets a window of the exe):

```powershell
$py = uv python find 3.12
Copy-Item $py (Join-Path (Split-Path $py) 'fakecall-e2e.exe')
Copy-Item $py (Join-Path (Split-Path $py) 'fakemeet-e2e.exe')
```

```python
# fakecall.py: run with  & <python dir>\fakecall-e2e.exe fakecall.py 20
import ctypes, math, struct, sys, tempfile, tkinter, wave, winsound
secs = int(sys.argv[1]) if len(sys.argv) > 1 else 20
tone = tempfile.NamedTemporaryFile(suffix=".wav", delete=False).name
with wave.open(tone, "wb") as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(48000)
    w.writeframes(b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * 1000 * i / 48000)))
                           for i in range(48000)))
mci = ctypes.windll.winmm.mciSendStringW
mci("open new type waveaudio alias cap", None, 0, None)
mci("record cap", None, 0, None)                      # holds the default microphone
winsound.PlaySound(tone, winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_LOOP)
root = tkinter.Tk(); root.title("fakecall-e2e"); root.after(secs * 1000, root.destroy)
root.mainloop()
winsound.PlaySound(None, 0); mci("close cap", None, 0, None)
```

Use `tests/e2e/rules.e2e.yaml` as `detection.user_rules_file` (with the `(\.exe)?` fix, §0 item 6).
For E3 start a camera user during the call (Windows Camera app, or
`ffmpeg -f dshow -i video="<camera name>" -f null -`).

| # | Scenario on Windows | Evidence |
|---|---|---|
| E1 | Armed, `fakecall-e2e` 20 s | 1 audio segment; start ≤ debounce + countdown + 2 s; `seg01_remote` shows 1 kHz |
| E2 | Armed, `fakemeet-e2e` (category meeting) | 1 video segment, MP4 + audio files, window capture of the fake window |
| E3 | `fakecall-e2e`, camera on after 10 s | 2 segments audio → video, gap ≤ 2 s |
| E4 | Disarmed, fake call | no session folder |
| E5 | Armed, OBS running, no call, 60 s | no session; OBS not "Currently in use" in Settings |
| E6 | Countdown cancelled via the toast or the tray | `status: cancelled`, no media |
| E7 | `taskkill /F /IM obs64.exe` mid-call | segment 1 `interrupted`, segment 2 after relaunch (ADR-013: confirm "Run Normally") |
| E8 | `auto_disarm_after_minutes: 1` during a call | recording stops at expiry, disarmed |
| E9 | Kill and restart the agent while armed | comes back `DISARMED` |
| E10 | During E1/E2: `Get-NetTCPConnection -OwningProcess <pid>` for the agent and `obs64.exe` | agent: only 127.0.0.1:4455; OBS: no outbound connections (or documented ones) |
| E11 | `local_input` set to an unused device | warning `qc:local_silent` + toast |
| E12 | `--remove` the firewall rule | `captation arm` refused (exposure); after `--apply` it arms; LAN connect from the second device fails |
| E13 | Tray unavailable: start the agent with `$env:PYSTRAY_BACKEND = 'none'` (pystray then fails to load its back end) | arming refused (`no_tray`), `doctor` → tray ❌ |

Metrics for the release notes: start latency, mode-switch gap, CPU and RAM of the agent and of
`obs64.exe` while idle-armed and while recording audio and video
(`Get-Process -Id <pid> | Select CPU, WorkingSet64` sampled twice, or `Get-Counter`).

**Real-app checklist** (record the results in the release notes). For each row record the detected app ID, mode,
start latency, remote-track isolation (no system sounds on the remote track), echo on the local
track (headset vs speakers) and issues:

| App / path | Windows |
|---|---|
| Zoom desktop meeting (video) | ☐ |
| Teams meeting (video), new Teams (MSIX): ConsentStore key `MSTeams_…` or `msedgewebview2.exe`? | ☐ |
| Webex App meeting: `CiscoCollabHost.exe` or `webexhost.exe`? | ☐ |
| Meet, Teams web, BigBlueButton, Cisco Meeting Server web app in Chrome and in Edge (title → app ID) | ☐ |
| FaceTime link in Chrome or Edge (`facetime_web`) | ☐ |
| Signal Desktop voice → video | ☐ |
| WhatsApp (Store, WebView2): package key or `msedgewebview2.exe`? | ☐ |
| Phone Link call (iPhone or Android over Bluetooth): which process and ConsentStore entry? | ☐ |
| Cisco Jabber call, a softphone if used (MicroSIP, Zoiper, 3CX) | ☐ |
| Discord / Slack huddle | ☐ |
| An app without a rule (recorded as `unknown`) | ☐ |
| External audio interface (if used) as audio device / external input | ☐ |

The expected identifiers per app are in `docs/app-coverage.md`.

Then: no `verify: true` rule remains for Windows; `uv run pytest` (unit, integration with OBS,
windows tests) is green; `uv run python tools/traceability.py` shows every requirement covered.

## 8. Verify and flip every pending fact

For each row: run the command, compare with the expected value, then edit the row in
`docs/platform-facts.md`: Status `verified`, OS version (e.g. `11 24H2 (26100.xxxx)`), OBS / ws
version, app version, Evidence = the command actually run → an output excerpt, Date. If the
fact is wrong: set `refuted`, add a new PF-2xx row with the correct value, change the code to
reference the new ID, and add a test. At the end, on Windows:
`uv run python tools/check_facts.py` → `facts ok`, and `--release --os windows` → `facts ok`.

| PF | Fact (short) | Command (full text in the PF row) | Section |
|---|---|---|---|
| PF-201 | OBS exe path | `Test-Path "$env:ProgramFiles\obs-studio\bin\64bit\obs64.exe"` | 3.1 |
| PF-202 | OBS registry keys | `Get-ItemProperty 'HKLM:\SOFTWARE\OBS Studio'`, uninstall key | 3.1 |
| PF-203 | OBS version source | `(Get-Item …obs64.exe).VersionInfo.ProductVersion`; `.\obs64.exe --version` | 3.1 |
| PF-204 | Config dir layout | `Get-ChildItem "$env:APPDATA\obs-studio"` | 3.2 |
| PF-205 | cwd = `bin\64bit`, helper processes | start from another cwd; `Get-Process obs*` | 3.3 |
| PF-206 | CLI flags | `.\obs64.exe --help \| Out-String`; OBS command line | 3.3 |
| PF-207 | No automatic crash upload | Help menu; `Select-String … crash, upload` | 3.8 |
| PF-208 | Websocket binds all interfaces | `Get-NetTCPConnection -State Listen -LocalPort 4455` | 3.5 |
| PF-209 | PowerShell invocation, UTF-8, exit codes | `_run(powershell_argv(...))` one-liner | 2 |
| PF-210 | ConsentStore layout, HKLM readable | `Get-ChildItem 'HKCU:\…\ConsentStore\microphone'` (+ HKLM) | 4.1 |
| PF-211 | In-use rule, latency, stale entries | `Get-ItemProperty … LastUsedTimeStart/Stop`; `--watch 1`; `taskkill` | 4.1 |
| PF-212 | Hive with live state | `python -m captation.platform.windows` vs Settings | 4.1 |
| PF-213 | Packaged install folders, family names | `Get-AppxPackage`; `Get-Process … Path` | 4.5 |
| PF-214 | Browser / WebView2 attribution | `Win32_Process` parent chain during calls | 4.5 |
| PF-215 | psutil name/exe/ppid | one-liner; windows-marked tests | 2 |
| PF-216 | EnumWindows titles | `enum_window_titles` one-liner | 4.6 |
| PF-220 | schtasks create/query/delete unelevated | section 6.1 commands | 6.1 |
| PF-221 | Task XML elements accepted | `schtasks /Query /TN … /XML`; Task Scheduler GUI | 6.1 |
| PF-222 | Restart on failure | `taskkill /F` the agent | 6.3 |
| PF-223 | AUMID key, toasts with buttons | `Get-ItemProperty …AppUserModelId\de.etabli.captation`; toast | 5, 6.1 |
| PF-224 | `pythonw.exe` / `captationw.exe` launch | `Test-Path`; logon without console | 6.2 |
| PF-225 | `Get-BitLockerVolume` needs elevation | unelevated vs elevated | 6.4 |
| PF-226 | Shell property `BitLockerProtection` | `Shell.Application` one-liner | 6.4 |
| PF-230 | Rule create/remove elevated via UAC | `firewall --apply/--remove`; decline once | 3.5 |
| PF-231 | Unelevated rule/profile read | `firewall --check` | 3.5 |
| PF-232 | Loopback exempt, LAN blocked, block wins | `Test-NetConnection` local + second device | 3.5 |
| PF-240 | Input kinds | facts dump | 3.4 |
| PF-241 | Process capture `window`/`priority`, isolation | facts dump; tone test | 3.4, 4.4 |
| PF-242 | `device_id` | facts dump; external-interface row | 3.4, 7 |
| PF-243 | Window capture `method`/`priority`, window pick | facts dump; Zoom/Teams video | 3.4, 7 |
| PF-244 | Monitor capture `monitor_id` | facts dump; `primary_monitor_device()` | 3.4 |
| PF-250 | pystray tray | smoke test | 5 |
| PF-251 | `MessageBeep` | one-liner; notification fallback | 5 |
| PF-252 | Tk dialog / InputBox fallback | tray → arm | 5 |
| PF-260 | Pipe DACL, authkey ACL | `GetAccessControl` / `accesschk`; `icacls` | 2 |
| PF-270 | Packaging, per-user install, SmartScreen | `build.ps1 -Installer`; install | 6.6 |

## 9. SmartScreen and unsigned builds

The zip and the setup are not code-signed. Windows marks downloaded files (Mark of the Web), and
SmartScreen shows "Windows protected your PC" on the first start of `setup.exe`,
`captation.exe` or `captationw.exe` because a new, unsigned build has no reputation: choose
**More info → Run anyway**. For the zip, unblock it before extracting
(Properties → Unblock, or `Unblock-File <zip>`); otherwise every extracted exe carries the mark.
With **Smart App Control** on (Windows 11, Settings → Privacy & security → Windows Security →
App & browser control), unsigned apps are blocked outright and there is no "Run anyway".
Code signing (an OV/EV certificate in the release workflow) removes the hard block and builds
reputation over time; it is optional. The short form of this section is L-22 in
`docs/known-limitations.md`.

## 10. Open questions for the maintainer (after the end-to-end checks, earlier if a fact deviates)

1. **Hive result:** which hive carries the live state on this PC? Keep reading both hives
   (labelled), or restrict the probe to HKCU?
2. **Window pick:** for apps with several windows (Zoom main window plus meeting window) OBS
   may capture the wrong one when matching by executable only. Accept, or pass the meeting
   window's title to OBS? The title would then sit in OBS's scene-collection file while the
   segment runs (docs/principles.md §3.1 trade-off).
3. **UWP or packaged apps** whose windows OBS cannot target: accept display capture with the
   warning `capture:display_fallback`?
4. **Restart after a crash:** if the task does not restart a killed agent (PF-222), accept it as
   a known limitation or add a small watchdog?
5. **BitLocker without elevation:** if the shell property is unavailable, `data_root` is
   `unknown` (warning). Accept, or require a one-time elevated check?
6. **IPC pipe ACL:** accept the default DACL and the residual squatting risk (PF-260), or
   implement an owner-only pipe via `CreateNamedPipeW`?
7. **Toast buttons:** if the countdown toast's *Abbrechen* button does not reach the agent, is the
   tray-menu cancel (fallback step 2) acceptable on Windows?
8. **Code signing:** buy a certificate for releases, or ship unsigned with the SmartScreen note?
9. **ADR-003:** confirm pystray + desktop-notifier as the Windows back end once the smoke test
   (section 5) passes. Full Windows support in v1 (ADR-007) is already accepted.
10. **Deviations from Linux:** audio-only strategy B, encoder choice (`obs_x264` vs NVENC/AMF/QSV
    from the facts dump), mic-release latency: any change to ADR-009/ADR-010 for Windows?
