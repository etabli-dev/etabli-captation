# captation-probe (macOS)

A small Swift command-line helper (ADR-004, stage 06) that reports which processes use the
microphone and whether a camera is in use. The agent runs it as a long-lived subprocess
(`MacProbe` in `src/captation/platform/macos.py`) and parses its JSON lines.

Public APIs only. Written and reviewed on Linux (ADR-015); every identifier is a **pending**
fact (PF-113 … PF-119) until verified on the Mac (`docs/runbook-macos.md`).

| What | API | PF |
|---|---|---|
| Mic per process (macOS ≥ 14) | `kAudioHardwarePropertyProcessObjectList`; per process object `kAudioProcessPropertyPID`, `kAudioProcessPropertyBundleID`, `kAudioProcessPropertyIsRunningInput` | PF-113 |
| Mic fallback (`confidence: low`) | `kAudioHardwarePropertyDefaultInputDevice` → `kAudioDevicePropertyDeviceIsRunningSomewhere`, plus `NSWorkspace.frontmostApplication` | PF-114 |
| Camera | `kCMIOHardwarePropertyDevices` → `kCMIODevicePropertyDeviceIsRunningSomewhere` (no attribution) | PF-115 |
| Process identity | `proc_pidpath`, `proc_pidinfo(PROC_PIDTBSDINFO).pbi_ppid`, `NSRunningApplication` | PF-116 |
| Browser/Electron helpers | parent-PID chain to the first non-helper ancestor (no private `responsibility_*` API) | PF-117 |
| Window titles (`--titles`) | `CGPreflightScreenCaptureAccess()` (never prompts), `CGWindowListCopyWindowInfo` | PF-118 |

The probe **polls**. Property listeners for `IsRunningInput` are reported not to fire, so none
are used. It never opens the microphone or the camera and needs no TCC permission for the mic
and camera state (to be confirmed, PF-113/PF-115). Window titles need the Screen Recording
permission of the process that runs the probe; without it the output says
`"titles_status": "permission_missing"`.

## Build

```sh
cd probes/macos/CaptationProbe
swift build -c release                     # → .build/release/captation-probe (PF-119)
codesign --force --sign - .build/release/captation-probe   # ad-hoc, for development
swift test                                 # needs Xcode (XCTest is not in the Command Line Tools)
```

Universal binary (if the toolchain allows):
`swift build -c release --arch arm64 --arch x86_64` → `.build/apple/Products/Release/captation-probe`.
`packaging/macos/build.sh` builds it and copies it to `Établi Captation.app/Contents/MacOS/`.
The agent finds the helper next to the frozen executable, in `src/captation/_bin/macos/`, in
`.build/release/` of this package, via `$CAPTATION_PROBE`, or on `PATH`.

## Usage

```
captation-probe --once [--titles] [--force-fallback]
captation-probe --watch [--interval-ms N] [--titles] [--force-fallback]
captation-probe --version | --help
```

- `--once` prints one JSON object and exits.
- `--watch` prints one JSON line every `--interval-ms` (100…60000, default 1000) until stdout
  closes or the parent process exits (then it is re-parented to launchd and stops).
- `--force-fallback` uses the low-confidence path even on macOS ≥ 14 (for verification).

### Exit codes

| Code | Meaning |
|---|---|
| 0 | ok |
| 2 | API unavailable: neither process objects nor the default input device could be read (`--once`; `--watch` keeps reporting with `errors`) |
| 3 | reserved: permission missing (window titles report `titles_status` instead) |
| 64 | bad command line |

## Output schema

One JSON object per line (keys sorted, `null` fields omitted). JSON Schema (2020-12):

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "captation-probe/output/1",
  "type": "object",
  "required": ["schema", "ts", "macos", "api", "confidence", "mic_users", "camera_active", "camera_users", "errors"],
  "properties": {
    "schema": { "const": 1 },
    "ts": { "type": "number", "description": "wall-clock seconds since 1970" },
    "macos": { "type": "string", "pattern": "^\\d+\\.\\d+\\.\\d+$" },
    "api": { "enum": ["process_objects", "device_running"] },
    "confidence": { "enum": ["high", "low"] },
    "reason": { "enum": ["macos_lt_14", "process_objects_failed", "forced"] },
    "mic_users": { "type": "array", "items": { "$ref": "#/$defs/app" } },
    "mic_device_running": { "type": "boolean", "description": "default input device running somewhere (any process, OBS included)" },
    "camera_active": { "type": "boolean" },
    "camera_users": { "type": "array", "maxItems": 0, "description": "CoreMediaIO has no attribution" },
    "frontmost": { "$ref": "#/$defs/app", "description": "low-confidence path only" },
    "titles_status": { "enum": ["ok", "permission_missing", "unavailable"] },
    "window_titles": {
      "type": "object",
      "description": "PID (decimal string) → window titles; only with --titles and permission",
      "additionalProperties": { "type": "array", "items": { "type": "string" } }
    },
    "errors": { "type": "array", "items": { "type": "string" } }
  },
  "$defs": {
    "app": {
      "type": "object",
      "required": ["pid"],
      "properties": {
        "pid": { "type": "integer", "minimum": 1 },
        "bundle_id": { "type": "string" },
        "name": { "type": "string", "description": "executable basename" },
        "localized_name": { "type": "string" },
        "path": { "type": "string" },
        "ppid": { "type": "integer" },
        "parent_pid": { "type": "integer", "description": "helpers: first non-helper ancestor" },
        "parent_bundle_id": { "type": "string" },
        "parent_name": { "type": "string" }
      }
    }
  }
}
```

`api: "process_objects"` always comes with `confidence: "high"`; `device_running` with `low`.
In the low path `mic_users` holds the frontmost app while the default input device runs. OBS
itself can hold that device, so the agent keeps the users seen before OBS started capturing
(docs/principles.md §5.2, `MacProbe(obs_capturing=…)`).

## Sample

`Samples/watch-sample.jsonl` is a sanitised sample (idle; a Zoom call with the camera on; Meet
in Chrome through `Google Chrome Helper`; OBS plus a Signal helper; the low-confidence path on
macOS 13; titles without permission). The Python tests (`tests/unit/test_macos_probe.py`) and
`swift test` (`testSampleDecodes`) both read it, so the two sides agree on the schema. Replace it
with a recorded and sanitised sample from the Mac (no titles, names or numbers of people) once
the probe runs there.
