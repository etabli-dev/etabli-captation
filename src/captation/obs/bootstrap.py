"""OBS bootstrap: websocket config, Captation profiles and scene collection.

Rules (docs/principles.md §4.2): only profiles and collections whose names start with
``Captation`` are created or changed. The obs-websocket config is the single exception: it is
backed up before any change, auth is never disabled, and a config the user set up (server
enabled with a password we do not know) is never overwritten. Every step compares before it
writes, so a second run changes nothing.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from captation.config import Config
from captation.models import ObsInstallation
from captation.obs import credentials, kinds, launcher
from captation.obs.client import ObsWebSocket

log = logging.getLogger(__name__)

AUDIO_SCENE = "Audio"
VIDEO_SCENE = "Video"
SWITCH_TIMEOUT_S = 15.0

WsConfigState = Literal["ok", "written", "fixed", "needs_password", "obs_running"]


class BootstrapError(Exception):
    pass


@dataclass
class BootstrapReport:
    websocket: WsConfigState = "ok"
    changes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    obs_version: str | None = None
    ws_version: str | None = None

    def change(self, text: str) -> None:
        log.info("bootstrap: %s", text)
        self.changes.append(text)


# ---- websocket config ------------------------------------------------------------------------


def _write_ws_config(path: Path, doc: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        backup = path.with_name(f"config.json.bak-{datetime.now():%Y%m%d-%H%M%S}")
        shutil.copy2(path, backup)
        os.chmod(backup, 0o600)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=4)
    os.replace(tmp, path)


def ensure_websocket_config(
    installation: ObsInstallation, cfg: Config, *, obs_running: bool
) -> WsConfigState:
    path = launcher.websocket_config_path(installation)
    port = cfg.obs.websocket.port
    doc: dict[str, Any] = {}
    if path.exists():
        doc = json.loads(path.read_text(encoding="utf-8") or "{}")
    known = credentials.has_keyring_password()
    current_pw = doc.get(kinds.WS_KEY_PASSWORD) or ""
    ours = known and current_pw == credentials.get_password(cfg.obs.websocket)
    if ours and doc.get(kinds.WS_KEY_AUTH) is True:
        if doc.get(kinds.WS_KEY_ENABLED) is True and doc.get(kinds.WS_KEY_PORT) == port:
            return "ok"
        if obs_running:
            return "obs_running"
        doc[kinds.WS_KEY_ENABLED] = True
        doc[kinds.WS_KEY_PORT] = port
        _write_ws_config(path, doc)
        return "fixed"
    user_configured = (
        doc.get(kinds.WS_KEY_ENABLED) is True and doc.get(kinds.WS_KEY_AUTH) is True and current_pw
    )
    if user_configured:
        # The user set up obs-websocket: never overwrite. Ask for the password instead.
        return "needs_password"
    if obs_running:
        return "obs_running"
    password = credentials.generate()
    doc.update(
        {
            kinds.WS_KEY_ALERTS: False,
            kinds.WS_KEY_AUTH: True,
            kinds.WS_KEY_FIRST_LOAD: False,
            kinds.WS_KEY_ENABLED: True,
            kinds.WS_KEY_PASSWORD: password,
            kinds.WS_KEY_PORT: port,
        }
    )
    credentials.store_password(password)
    _write_ws_config(path, doc)
    return "written"


# ---- helpers ---------------------------------------------------------------------------------


def _wait(predicate: Callable[[], bool], what: str, timeout_s: float = SWITCH_TIMEOUT_S) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.2)
    raise BootstrapError(f"timeout waiting for {what}")


def track_bitmask(cfg: Config) -> int:
    tracks = cfg.recording.audio.tracks
    numbers = [tracks.mix, tracks.remote, tracks.local]
    if tracks.external is not None:
        numbers.append(tracks.external)
    return sum(1 << (n - 1) for n in numbers)


def pick_audio_encoder(cfg: Config, available: set[str]) -> str:
    for codec in cfg.recording.audio.codec_preference:
        enc = kinds.AUDIO_ENCODERS.get(codec)
        if enc and (not available or enc in available):
            return enc
    raise BootstrapError("none of recording.audio.codec_preference is available in OBS")


def pick_video_encoder(cfg: Config, available: set[str], os_name: str) -> str:
    for pref in cfg.recording.video.encoder_preference:
        candidates = (
            kinds.video_encoders_hardware(os_name)
            if pref == "hardware"
            else (kinds.VIDEO_ENCODER_X264,)
        )
        for enc in candidates:
            if not available or enc in available:
                return enc
    return kinds.VIDEO_ENCODER_X264


def scaled_output(base_w: int, base_h: int, max_h: int) -> tuple[int, int]:
    if base_h <= max_h:
        return base_w, base_h
    w = round(base_w * max_h / base_h)
    return w - (w % 2), max_h


# ---- bootstrap ---------------------------------------------------------------------------------


class Bootstrapper:
    def __init__(
        self,
        cfg: Config,
        ws: ObsWebSocket,
        *,
        available_encoders: set[str],
        os_name: str,
        report: BootstrapReport,
    ) -> None:
        self.cfg = cfg
        self.ws = ws
        self.R = ws.request
        self.available = available_encoders
        self.os_name = os_name
        self.report = report

    # profiles

    def _current_profile(self) -> str:
        return str(self.R("GetProfileList")["currentProfileName"])

    def _switch_profile(self, name: str) -> None:
        if self._current_profile() != name:
            self.R("SetCurrentProfile", {"profileName": name})
            _wait(lambda: self._current_profile() == name, f"profile {name}")

    def ensure_profile(self, name: str) -> bool:
        if name in self.R("GetProfileList")["profiles"]:
            return False
        self.R("CreateProfile", {"profileName": name})
        _wait(lambda: name in self.R("GetProfileList")["profiles"], f"profile {name} created")
        self.report.change(f"created profile {name}")
        return True

    def _set_param(self, key: tuple[str, str], value: str) -> bool:
        cat, pname = key
        cur = self.R("GetProfileParameter", {"parameterCategory": cat, "parameterName": pname})
        if cur.get("parameterValue") == value:
            return False
        self.R(
            "SetProfileParameter",
            {"parameterCategory": cat, "parameterName": pname, "parameterValue": value},
        )
        self.report.change(f"{self._current_profile()}: {cat}.{pname} = {value}")
        return True

    def _set_video(self, w: int, h: int, out_w: int, out_h: int, fps: int) -> bool:
        cur = self.R("GetVideoSettings")
        want = {
            "baseWidth": w,
            "baseHeight": h,
            "outputWidth": out_w,
            "outputHeight": out_h,
            "fpsNumerator": fps,
            "fpsDenominator": 1,
        }
        if all(cur.get(k) == v for k, v in want.items()):
            return False
        self.R("SetVideoSettings", want)
        self.report.change(f"{self._current_profile()}: video {out_w}x{out_h}@{fps}")
        return True

    def configure_profile(self, name: str, *, video: bool) -> bool:
        self._switch_profile(name)
        changed = False
        a = self.cfg.recording.audio
        changed |= self._set_param(kinds.P_OUTPUT_MODE, kinds.V_OUTPUT_ADVANCED)
        changed |= self._set_param(kinds.P_REC_TYPE, kinds.V_REC_STANDARD)
        changed |= self._set_param(kinds.P_REC_FORMAT, kinds.V_FORMAT_MKV)
        changed |= self._set_param(kinds.P_REC_TRACKS, str(track_bitmask(self.cfg)))
        changed |= self._set_param(kinds.P_REC_RB_ADV, kinds.V_FALSE)
        changed |= self._set_param(kinds.P_REC_RB_SIMPLE, kinds.V_FALSE)
        changed |= self._set_param(kinds.P_REC_SPLIT, kinds.V_FALSE)
        changed |= self._set_param(
            kinds.P_REC_AUDIO_ENCODER, pick_audio_encoder(self.cfg, self.available)
        )
        if video:
            enc = pick_video_encoder(self.cfg, self.available, self.os_name)
            changed |= self._set_param(kinds.P_REC_ENCODER, enc)
            cur = self.R("GetVideoSettings")
            bw, bh = int(cur["baseWidth"]), int(cur["baseHeight"])
            ow, oh = scaled_output(bw, bh, self.cfg.recording.video.max_height)
            changed |= self._set_video(bw, bh, ow, oh, self.cfg.recording.video.fps)
        else:
            changed |= self._set_param(kinds.P_REC_ENCODER, kinds.VIDEO_ENCODER_X264)
            w, h = kinds.AUDIO_CANVAS
            changed |= self._set_video(w, h, w, h, kinds.AUDIO_FPS)
        cat, pname = kinds.P_SAMPLE_RATE
        rate = self.R("GetProfileParameter", {"parameterCategory": cat, "parameterName": pname})
        if str(rate.get("parameterValue")) != str(a.sample_rate):
            self.report.warnings.append(
                f"{name}: OBS sample rate is {rate.get('parameterValue')}, config wants "
                f"{a.sample_rate} (change it in OBS settings and restart OBS)"
            )
        return changed

    # scene collection

    def _current_collection(self) -> str:
        return str(self.R("GetSceneCollectionList")["currentSceneCollectionName"])

    def ensure_collection(self) -> None:
        name = self.cfg.obs.scene_collection
        created = False
        if name not in self.R("GetSceneCollectionList")["sceneCollections"]:
            self.R("CreateSceneCollection", {"sceneCollectionName": name})
            _wait(lambda: self._current_collection() == name, f"collection {name} created")
            self.report.change(f"created scene collection {name}")
            created = True
        elif self._current_collection() != name:
            self.R("SetCurrentSceneCollection", {"sceneCollectionName": name})
            _wait(lambda: self._current_collection() == name, f"collection {name}")
        scenes = [s["sceneName"] for s in self.R("GetSceneList")["scenes"]]
        for scene in (AUDIO_SCENE, VIDEO_SCENE):
            if scene not in scenes:
                self.R("CreateScene", {"sceneName": scene})
                self.report.change(f"{name}: created scene {scene}")
        if created:
            for scene in scenes:
                if scene not in (AUDIO_SCENE, VIDEO_SCENE):
                    self.R("RemoveScene", {"sceneName": scene})
                    self.report.change(f"{name}: removed default scene")
        current = self.R("GetCurrentProgramScene").get("currentProgramSceneName")
        if current != AUDIO_SCENE:
            self.R("SetCurrentProgramScene", {"sceneName": AUDIO_SCENE})
        # Global audio devices would hold the mic while idle (docs/principles.md §5.2).
        for role, input_name in self.R("GetSpecialInputs").items():
            if input_name:
                self.R("RemoveInput", {"inputName": input_name})
                self.report.change(f"{name}: removed global audio device ({role})")
        # Leftover Captation inputs from an interrupted run.
        for item in self.R("GetInputList")["inputs"]:
            if str(item["inputName"]).startswith("Captation "):
                self.R("RemoveInput", {"inputName": item["inputName"]})
                self.report.change(f"{name}: removed leftover input")

    def run(self) -> None:
        o = self.cfg.obs
        version = self.R("GetVersion")
        self.report.obs_version = version.get("obsVersion")
        self.report.ws_version = version.get("obsWebSocketVersion")
        if self.R("GetRecordStatus").get("outputActive"):
            raise BootstrapError("OBS is recording; bootstrap only runs while OBS is idle")
        self.ensure_profile(o.profile_audio)
        self.ensure_profile(o.profile_video)
        changed = self.configure_profile(o.profile_video, video=True)
        changed |= self.configure_profile(o.profile_audio, video=False)
        if changed:
            # Output-mode changes apply when a profile is (re)loaded.
            self._switch_profile(o.profile_video)
            self._switch_profile(o.profile_audio)
        self.ensure_collection()
