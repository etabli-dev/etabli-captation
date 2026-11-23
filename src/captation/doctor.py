"""``captation doctor``: installation and health checks.

Each check yields ok / warn / error with a one-line fix. Exit code: 0 all ok, 1 warnings,
2 errors.
"""

from __future__ import annotations

import importlib
import json
import logging
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from platformdirs import user_state_dir

from captation import storage
from captation.config import APP_AUTHOR, APP_NAME, Config
from captation.obs import credentials, firewall, kinds, launcher, manage
from captation.obs.client import ObsError
from captation.platform import current_os, get_platform

log = logging.getLogger(__name__)

Level = Literal["ok", "warn", "error"]
MIN_OBS = (32, 0)
APP_AUDIO_FIX = {
    "linux": "flatpak install --user flathub com.obsproject.Studio.Plugin.PipeWireAudioCapture",
    "macos": "update OBS (macOS Audio Capture needs OBS ≥ 30 and macOS ≥ 13)",
    "windows": "update OBS (Application Audio Capture needs OBS ≥ 28 and Windows 10 2004+)",
}
TRAY_FIX = {
    "linux": "GNOME: enable the AppIndicator extension (and unlock the screen)",
    "macos": "run the agent in the logged-in GUI session (LaunchAgent, not a daemon)",
    "windows": "allow the icon in Settings → Personalisation → Taskbar → other icons",
}
ICON = {"ok": "✅", "warn": "⚠️ ", "error": "❌"}


@dataclass
class Check:
    name: str
    level: Level
    detail: str
    fix: str = ""


def _version_tuple(text: str | None) -> tuple[int, ...]:
    if not text:
        return ()
    parts = []
    for p in text.split("."):
        digits = "".join(ch for ch in p if ch.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def _os_release() -> dict[str, str]:
    try:
        text = Path("/etc/os-release").read_text(encoding="utf-8")
    except OSError:
        return {}
    out = {}
    for line in text.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k] = v.strip().strip('"')
    return out


def run_checks(cfg: Config, *, ui_probe: bool = True) -> list[Check]:
    checks: list[Check] = []
    add = checks.append
    add(Check("config", "ok", f"valid ({cfg.digest()[:19]}…)"))
    from captation import terms

    acc = terms.current()
    add(
        Check(
            "terms",
            "ok" if acc else "error",
            f"disclaimer version {acc.version} accepted on {acc.accepted_at[:10]}"
            if acc
            else "disclaimer not accepted (or changed): arming and import are refused",
            "captation accept",
        )
    )

    # OBS installation and version
    try:
        inst = manage.locate(cfg)
    except launcher.ObsNotFound as exc:
        add(Check("obs", "error", "OBS Studio not found", str(exc)))
        inst = None
    if inst is not None:
        version = _version_tuple(inst.version)
        if version and version < MIN_OBS:
            add(
                Check(
                    "obs",
                    "error",
                    f"OBS {inst.version} ({inst.kind}) is too old",
                    "install OBS ≥ 32.0",
                )
            )
        else:
            add(Check("obs", "ok", f"OBS {inst.version or '?'} ({inst.kind})"))
        if current_os() == "linux" and inst.kind == "native":
            osr = _os_release()
            if osr.get("ID") == "ubuntu" and _version_tuple(osr.get("VERSION_ID")) < (26, 4):
                add(
                    Check(
                        "distro",
                        "warn",
                        f"Ubuntu {osr.get('VERSION_ID')}: OBS 33 dropped 24.04",
                        "use the OBS Flatpak",
                    )
                )
        add(
            Check(
                "obs.profiles",
                "ok"
                if manage.profile_exists(inst, cfg.obs.profile_audio)
                and manage.profile_exists(inst, cfg.obs.profile_video)
                and manage.collection_exists(inst, cfg.obs.scene_collection)
                else "error",
                "Captation profiles and scene collection",
                "captation obs bootstrap",
            )
        )
        if current_os() == "linux":
            add(
                Check(
                    "obs.updater",
                    "ok",
                    "no built-in updater or automatic crash upload on Linux (PF-012)",
                )
            )
        else:
            module = importlib.import_module(f"captation.platform.{current_os()}")
            reader = getattr(module, "crash_upload_settings", None)
            found = reader(Path(inst.config_dir)) if reader is not None else {}
            add(
                Check(
                    "obs.crash_upload",
                    "warn",
                    "crash-log upload setting: "
                    + (", ".join(f"{k}={v}" for k, v in found.items()) or "not found")
                    + " (key pending verification)",
                    "OBS Settings → General: switch off automatic crash-log upload",
                )
            )

    # websocket password and connection
    try:
        credentials.get_password(cfg.obs.websocket)
        add(Check("obs.password", "ok", "websocket password available (keyring or env)"))
    except credentials.PasswordMissing as exc:
        add(Check("obs.password", "error", "no websocket password", str(exc)))
    if manage.obs_running(cfg):
        try:
            ws = manage.connect(cfg)
            try:
                v = ws.request("GetVersion")
                kinds_available = set(
                    ws.request("GetInputKindList", {"unversioned": False})["inputKinds"]
                )
            finally:
                ws.close()
            add(
                Check(
                    "obs.websocket",
                    "ok",
                    f"connected, auth ok (obs-websocket {v.get('obsWebSocketVersion')})",
                )
            )
            app = kinds.capture_profile(current_os()).app_audio_kinds
            if app and not set(app) & kinds_available:
                add(
                    Check(
                        "obs.app_audio",
                        "warn",
                        "no per-app audio capture: remote falls back to desktop audio",
                        APP_AUDIO_FIX.get(current_os(), "update OBS Studio"),
                    )
                )
            else:
                add(Check("obs.app_audio", "ok", "per-app audio capture available"))
        except (ObsError, OSError, credentials.PasswordMissing) as exc:
            add(
                Check(
                    "obs.websocket",
                    "error",
                    f"cannot authenticate: {exc}",
                    "captation obs bootstrap / obs set-password",
                )
            )
    else:
        add(
            Check(
                "obs.websocket",
                "warn",
                "OBS is not running (it is started when arming)",
                "nothing to do; to check the connection now, start OBS and run doctor again",
            )
        )

    # exposure policy
    fw = firewall.check(cfg.obs.websocket.port)
    if fw.ok:
        add(Check("exposure", "ok", "; ".join(fw.details[-1:])))
    else:
        level: Level = "error" if cfg.obs.exposure_policy == "require_firewall" else "warn"
        add(
            Check(
                "exposure",
                level,
                "websocket port not blocked for non-loopback sources",
                "captation obs firewall --apply",
            )
        )

    # tools
    for tool in ("ffmpeg", "ffprobe"):
        add(
            Check(
                tool,
                "ok" if shutil.which(tool) else "error",
                shutil.which(tool) or "not found",
                f"install {tool}",
            )
        )

    platform = get_platform()
    # probe
    try:
        probe = platform.probe(exclude_pids=frozenset(), store_titles=False)
        try:
            snap = probe.snapshot()
        finally:
            probe.close()
        add(
            Check(
                "probe",
                "ok" if snap.confidence == "high" else "warn",
                f"confidence {snap.confidence}",
                "install PipeWire tools (pw-dump)",
            )
        )
    except Exception as exc:  # pragma: no cover - platform without probe
        add(
            Check(
                "probe",
                "error",
                f"probe unavailable: {type(exc).__name__}",
                "see docs/known-limitations.md",
            )
        )

    # tray and notifications
    if ui_probe:
        from captation.ui.base import default_backend

        backend = default_backend()
        try:
            backend.start()
            tray = backend.tray_available()
            caps = backend.notification_capabilities()
        except Exception:
            tray, caps = False, set()
        finally:
            backend.stop()
        add(
            Check(
                "tray",
                "ok" if tray else "error",
                "tray host available" if tray else "no tray host: arming is refused",
                TRAY_FIX.get(current_os(), "start the agent in the desktop session"),
            )
        )
        add(
            Check(
                "notifications",
                "ok" if "actions" in caps else "warn",
                f"capabilities: {', '.join(sorted(caps)) or 'none'}",
                "countdown falls back to the tray menu",
            )
        )

    # local input
    if cfg.recording.audio.local_input != "follow_default":
        add(
            Check(
                "local_input",
                "warn",
                f"'{cfg.recording.audio.local_input}' is resolved when recording starts",
                "check the device name in OBS",
            )
        )
    else:
        add(Check("local_input", "ok", "follows the default input"))

    # storage
    root = cfg.data_root_path
    try:
        root.mkdir(parents=True, exist_ok=True)
        probe_file = root / ".doctor-write-test"
        probe_file.write_text("x", encoding="utf-8")
        probe_file.unlink()
        add(Check("data_root", "ok", "writable"))
    except OSError as exc:
        add(Check("data_root", "error", f"not writable: {type(exc).__name__}", "fix data_root"))
    if str(root.resolve()).startswith("/tmp/") and inst is not None and inst.kind == "flatpak":
        add(
            Check(
                "data_root.flatpak",
                "error",
                "the OBS Flatpak cannot write to /tmp (PF-011)",
                "move data_root",
            )
        )
    free = storage.free_space_gb(root)
    add(
        Check(
            "free_space",
            "ok" if free >= cfg.storage.min_free_gb else "error",
            f"{free:.0f} GB free (minimum {cfg.storage.min_free_gb:g})",
            "free disk space",
        )
    )
    enc = platform.disk_encryption().status(root)
    verdict = storage.encryption_verdict(enc, cfg.storage.require_disk_encryption)
    enc_level: Level = "ok" if verdict == "ok" else ("error" if verdict == "block" else "warn")
    add(
        Check(
            "disk_encryption",
            enc_level,
            f"data_root is {enc}",
            "use an encrypted volume (e.g. /data on this machine)",
        )
    )

    # autostart and screen capture approval
    auto = platform.autostart().status()
    add(
        Check(
            "autostart",
            "ok" if auto.installed else "warn",
            f"{auto.method}" if auto.installed else "not installed",
            "captation install-autostart",
        )
    )
    token = Path(user_state_dir(APP_NAME, APP_AUTHOR)) / "screencast-token"
    if kinds.capture_profile(current_os()).needs_screen_token:
        add(
            Check(
                "screen_capture",
                "ok" if token.exists() else "warn",
                "approved" if token.exists() else "not approved: video segments record black",
                "captation obs approve-screen",
            )
        )

    # unverified rules
    from captation.rulesfile import load_rules

    rules = load_rules(cfg.detection.rules_file, cfg.detection.user_rules_file)
    unverified = [a.id for a in rules.apps if a.verify]
    add(
        Check(
            "rules",
            "warn" if unverified else "ok",
            f"verify:true for {', '.join(unverified)}" if unverified else "all rules verified",
            "confirm with the real-app checklist (docs/app-coverage.md)",
        )
    )
    return checks


def exit_code(checks: list[Check]) -> int:
    levels = {c.level for c in checks}
    return 2 if "error" in levels else 1 if "warn" in levels else 0


def render(checks: list[Check], as_json: bool) -> str:
    if as_json:
        return json.dumps(
            {"checks": [asdict(c) for c in checks], "exit_code": exit_code(checks)},
            ensure_ascii=False,
            indent=1,
        )
    lines = []
    for c in checks:
        fix = f"  → {c.fix}" if c.fix and c.level != "ok" else ""
        lines.append(f"{ICON[c.level]} {c.name:<18} {c.detail}{fix}")
    return "\n".join(lines)
