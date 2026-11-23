"""In-memory fake of the obs-websocket requests Captation uses (unit tests only)."""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from captation.obs.client import ObsLost, ObsRequestError

DEFAULT_KINDS = [
    "pipewire_audio_application_capture",
    "pipewire_audio_output_capture",
    "pulse_input_capture",
    "pulse_output_capture",
    "pipewire-screen-capture-source",
]
DEVICES = [
    {"itemName": "Default", "itemValue": "default", "itemEnabled": True},
    {"itemName": "Headset Mic", "itemValue": "alsa_input.headset", "itemEnabled": True},
]


class FakeObs:
    """Implements the Transport protocol of ``ObsController`` and the bootstrap requests."""

    def __init__(
        self,
        *,
        kinds: list[str] | None = None,
        profiles: list[str] | None = None,
        collections: list[str] | None = None,
        record_root: Path | None = None,
        set_record_directory: bool = True,
        special: dict[str, str | None] | None = None,
    ) -> None:
        self.ws_version = "5.7.6"
        self.kinds = kinds if kinds is not None else list(DEFAULT_KINDS)
        self.profiles = (
            profiles if profiles is not None else ["Untitled", "Captation-Audio", "Captation-Video"]
        )
        self.current_profile = self.profiles[0]
        self.collections = collections if collections is not None else ["Untitled", "Captation"]
        self.current_collection = self.collections[0]
        self.scenes: dict[str, list[str]] = {"Untitled": ["Scene"], "Captation": ["Audio", "Video"]}
        self.program_scene = "Audio"
        self.params: dict[str, dict[tuple[str, str], str]] = {p: {} for p in self.profiles}
        self.video: dict[str, dict[str, int]] = {
            p: {
                "baseWidth": 1920,
                "baseHeight": 1080,
                "outputWidth": 1280,
                "outputHeight": 720,
                "fpsNumerator": 30,
                "fpsDenominator": 1,
            }
            for p in self.profiles
        }
        self.inputs: dict[str, dict[str, Any]] = {}
        self.special = special or {"desktop1": None, "mic1": None}
        self.recording = False
        self.record_dir = "/home/user"
        self.record_root = record_root
        self.set_record_directory = set_record_directory
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
        self.callbacks: list[Callable[[str, dict[str, Any]], None]] = []
        self.alive = True
        self.fail: dict[str, ObsRequestError] = {}
        self.lock = threading.Lock()
        self.emit_stopped = True
        self.lag_active = 0  # number of GetRecordStatus calls that still report active

    # ---- Transport -------------------------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self.alive

    def on_event(self, callback: Callable[[str, dict[str, Any]], None]) -> None:
        self.callbacks.append(callback)

    def close(self) -> None:
        self.alive = False

    def emit(self, event: str, data: dict[str, Any]) -> None:
        for cb in list(self.callbacks):
            cb(event, data)

    def crash(self) -> None:
        self.alive = False
        self.emit("ConnectionLost", {})

    def request(
        self, request_type: str, data: dict[str, Any] | None = None, *, timeout_s: float = 10
    ) -> dict[str, Any]:
        if not self.alive:
            raise ObsLost("not connected")
        self.calls.append((request_type, data))
        if request_type in self.fail:
            raise self.fail[request_type]
        handler = getattr(self, f"_r_{request_type}", None)
        if handler is None:
            raise ObsRequestError(request_type, 204, "unknown request in fake")
        result: dict[str, Any] = handler(data or {})
        return result

    def names(self) -> list[str]:
        return [c[0] for c in self.calls]

    # ---- requests --------------------------------------------------------------------------

    def _r_GetVersion(self, d: dict[str, Any]) -> dict[str, Any]:
        reqs = ["GetVersion", "StartRecord"]
        if self.set_record_directory:
            reqs.append("SetRecordDirectory")
        return {
            "obsVersion": "33.0.3",
            "obsWebSocketVersion": self.ws_version,
            "availableRequests": reqs,
        }

    def _r_GetInputKindList(self, d: dict[str, Any]) -> dict[str, Any]:
        return {"inputKinds": list(self.kinds)}

    def _r_GetProfileList(self, d: dict[str, Any]) -> dict[str, Any]:
        return {"currentProfileName": self.current_profile, "profiles": list(self.profiles)}

    def _r_CreateProfile(self, d: dict[str, Any]) -> dict[str, Any]:
        name = d["profileName"]
        if name in self.profiles:
            raise ObsRequestError("CreateProfile", 601, "exists")
        self.profiles.append(name)
        self.params[name] = {}
        self.video[name] = dict(self.video[self.profiles[0]])
        self.current_profile = name
        return {}

    def _r_SetCurrentProfile(self, d: dict[str, Any]) -> dict[str, Any]:
        if self.recording:
            raise ObsRequestError("SetCurrentProfile", 500, "recording")
        self.current_profile = d["profileName"]
        return {}

    def _r_GetProfileParameter(self, d: dict[str, Any]) -> dict[str, Any]:
        key = (d["parameterCategory"], d["parameterName"])
        return {
            "parameterValue": self.params[self.current_profile].get(key),
            "defaultParameterValue": None,
        }

    def _r_SetProfileParameter(self, d: dict[str, Any]) -> dict[str, Any]:
        key = (d["parameterCategory"], d["parameterName"])
        self.params[self.current_profile][key] = d["parameterValue"]
        return {}

    def _r_GetVideoSettings(self, d: dict[str, Any]) -> dict[str, Any]:
        return dict(self.video[self.current_profile])

    def _r_SetVideoSettings(self, d: dict[str, Any]) -> dict[str, Any]:
        self.video[self.current_profile].update(d)
        return {}

    def _r_GetSceneCollectionList(self, d: dict[str, Any]) -> dict[str, Any]:
        return {
            "currentSceneCollectionName": self.current_collection,
            "sceneCollections": list(self.collections),
        }

    def _r_CreateSceneCollection(self, d: dict[str, Any]) -> dict[str, Any]:
        name = d["sceneCollectionName"]
        self.collections.append(name)
        self.scenes[name] = ["Scene"]
        self.current_collection = name
        return {}

    def _r_SetCurrentSceneCollection(self, d: dict[str, Any]) -> dict[str, Any]:
        self.current_collection = d["sceneCollectionName"]
        return {}

    def _r_GetSceneList(self, d: dict[str, Any]) -> dict[str, Any]:
        return {"scenes": [{"sceneName": s} for s in self.scenes[self.current_collection]]}

    def _r_CreateScene(self, d: dict[str, Any]) -> dict[str, Any]:
        self.scenes[self.current_collection].append(d["sceneName"])
        return {}

    def _r_RemoveScene(self, d: dict[str, Any]) -> dict[str, Any]:
        self.scenes[self.current_collection].remove(d["sceneName"])
        return {}

    def _r_GetCurrentProgramScene(self, d: dict[str, Any]) -> dict[str, Any]:
        return {"currentProgramSceneName": self.program_scene}

    def _r_SetCurrentProgramScene(self, d: dict[str, Any]) -> dict[str, Any]:
        self.program_scene = d["sceneName"]
        return {}

    def _r_GetSpecialInputs(self, d: dict[str, Any]) -> dict[str, Any]:
        return dict(self.special)

    def _r_GetInputList(self, d: dict[str, Any]) -> dict[str, Any]:
        return {
            "inputs": [{"inputName": n, "inputKind": i["kind"]} for n, i in self.inputs.items()]
        }

    def _r_CreateInput(self, d: dict[str, Any]) -> dict[str, Any]:
        name = d["inputName"]
        if name in self.inputs:
            raise ObsRequestError("CreateInput", 601, "exists")
        if d["inputKind"] not in self.kinds:
            raise ObsRequestError("CreateInput", 605, "invalid kind")
        self.inputs[name] = {
            "kind": d["inputKind"],
            "settings": dict(d.get("inputSettings") or {}),
            "tracks": None,
            "monitor": None,
            "scene": d["sceneName"],
        }
        return {"inputUuid": "x"}

    def _r_RemoveInput(self, d: dict[str, Any]) -> dict[str, Any]:
        name = d["inputName"]
        self.inputs.pop(name, None)
        for role, n in list(self.special.items()):
            if n == name:
                self.special[role] = None
        return {}

    def _r_GetInputSettings(self, d: dict[str, Any]) -> dict[str, Any]:
        i = self.inputs[d["inputName"]]
        return {"inputKind": i["kind"], "inputSettings": dict(i["settings"])}

    def _r_SetInputSettings(self, d: dict[str, Any]) -> dict[str, Any]:
        self.inputs[d["inputName"]]["settings"].update(d["inputSettings"])
        return {}

    def _r_GetInputPropertiesListPropertyItems(self, d: dict[str, Any]) -> dict[str, Any]:
        return {"propertyItems": DEVICES}

    def _r_GetInputDefaultSettings(self, d: dict[str, Any]) -> dict[str, Any]:
        return {"defaultInputSettings": {"kind": d["inputKind"]}}

    def _r_SetInputAudioTracks(self, d: dict[str, Any]) -> dict[str, Any]:
        self.inputs[d["inputName"]]["tracks"] = dict(d["inputAudioTracks"])
        return {}

    def _r_SetInputAudioMonitorType(self, d: dict[str, Any]) -> dict[str, Any]:
        self.inputs[d["inputName"]]["monitor"] = d["monitorType"]
        return {}

    def _r_SetRecordDirectory(self, d: dict[str, Any]) -> dict[str, Any]:
        self.record_dir = d["recordDirectory"]
        return {}

    def _r_GetRecordDirectory(self, d: dict[str, Any]) -> dict[str, Any]:
        return {"recordDirectory": self.record_dir}

    def _r_GetRecordStatus(self, d: dict[str, Any]) -> dict[str, Any]:
        if self.lag_active > 0:
            self.lag_active -= 1
            return {"outputActive": True}
        return {"outputActive": self.recording}

    def _r_StartRecord(self, d: dict[str, Any]) -> dict[str, Any]:
        if self.recording:
            raise ObsRequestError("StartRecord", 500, "already recording")
        self.recording = True
        directory = (
            self.record_dir
            if self.set_record_directory
            else self.params[self.current_profile].get(("AdvOut", "RecFilePath"), "")
        )
        name = self.params[self.current_profile].get(("Output", "FilenameFormatting"), "out")
        self.out_path = Path(directory) / f"{name}.mkv"
        self.emit(
            "RecordStateChanged",
            {"outputState": "OBS_WEBSOCKET_OUTPUT_STARTING", "outputActive": False},
        )
        self.emit(
            "RecordStateChanged",
            {
                "outputState": "OBS_WEBSOCKET_OUTPUT_STARTED",
                "outputActive": True,
                "outputPath": str(self.out_path),
            },
        )
        return {}

    def _r_StopRecord(self, d: dict[str, Any]) -> dict[str, Any]:
        if not self.recording:
            raise ObsRequestError("StopRecord", 501, "not running")
        self.recording = False
        self.out_path.parent.mkdir(parents=True, exist_ok=True)
        self.out_path.write_bytes(b"\x1a\x45\xdf\xa3fake-mkv")
        if self.emit_stopped:
            self.emit(
                "RecordStateChanged",
                {
                    "outputState": "OBS_WEBSOCKET_OUTPUT_STOPPED",
                    "outputActive": False,
                    "outputPath": str(self.out_path),
                },
            )
        return {"outputPath": str(self.out_path)}
