"""ObsController: the only module that drives OBS during recording.

Per segment: switch profile/scene (only while not recording), create the Captation inputs,
route tracks, start, stop (waiting for the STOPPED event, PF-014), and remove the inputs again
so OBS releases the microphone (docs/principles.md §5.2, ADR-010).
"""

from __future__ import annotations

import contextlib
import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from captation.config import Config
from captation.models import AppRef, CaptureKind, Mode, ObsInstallation
from captation.obs import kinds
from captation.obs.client import ObsConnectionError, ObsError, ObsLost, ObsRequestError

log = logging.getLogger(__name__)

REMOTE = "Captation Remote"
LOCAL = "Captation Local"
EXTERNAL = "Captation External"
SCREEN = "Captation Screen"
INPUT_PREFIX = "Captation "
AUDIO_SCENE = "Audio"
VIDEO_SCENE = "Video"
STATE_STARTED = "OBS_WEBSOCKET_OUTPUT_STARTED"
STATE_STOPPED = "OBS_WEBSOCKET_OUTPUT_STOPPED"
OUTPUT_NOT_RUNNING = 501  # obs-websocket RequestStatus::OutputNotRunning
# Outputs of the user's own OBS use: a profile/collection switch would cut them off.
OTHER_OUTPUTS = (
    ("GetStreamStatus", "streaming"),  # PF-004
    ("GetVirtualCamStatus", "virtual camera"),  # PF-004
    ("GetReplayBufferStatus", "replay buffer"),  # PF-004
)


class Transport(Protocol):
    """What the controller needs from a connection (``ObsWebSocket`` or a fake)."""

    ws_version: str | None

    def request(
        self, request_type: str, data: dict[str, Any] | None = None, *, timeout_s: float = 10
    ) -> dict[str, Any]: ...

    def on_event(self, callback: Callable[[str, dict[str, Any]], None]) -> None: ...

    def close(self) -> None: ...

    @property
    def connected(self) -> bool: ...


@dataclass(frozen=True)
class ObsInfo:
    obs_version: str | None
    ws_version: str | None
    set_record_directory: bool


@dataclass(frozen=True)
class PreparedSegment:
    mode: Mode
    capture: CaptureKind
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class ObsHealth:
    connected: bool
    recording: bool


@dataclass
class _RecordState:
    state: str | None = None
    path: str | None = None
    changed: threading.Condition = field(default_factory=threading.Condition)


class ScreenTokenStore(Protocol):
    def load(self) -> str | None: ...

    def save(self, token: str) -> None: ...


class FileTokenStore:
    """Persists the screencast-portal restore token (user-only file)."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> str | None:
        try:
            return self._path.read_text(encoding="utf-8").strip() or None
        except OSError:
            return None

    def save(self, token: str) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = self._path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(token)
        os.replace(tmp, self._path)


def track_map(cfg: Config) -> dict[str, int]:
    t = cfg.recording.audio.tracks
    roles = {"remote": t.remote, "local": t.local}
    if t.external is not None:
        roles["external"] = t.external
    return roles


def tracks_for(role_track: int) -> dict[str, bool]:
    """Track 1 (mix) plus the role track; every other track off."""
    return {str(i): i in (1, role_track) for i in range(1, 7)}


class ObsController:
    def __init__(
        self,
        cfg: Config,
        connect: Callable[[], Transport],
        *,
        os_name: str,
        installation: ObsInstallation | None = None,
        launch: Callable[[], None] | None = None,
        token_store: ScreenTokenStore | None = None,
        on_lost: Callable[[], None] | None = None,
        on_record_stopped: Callable[[], None] | None = None,
    ) -> None:
        self.cfg = cfg
        self._connect = connect
        self._os = os_name
        self._installation = installation
        self._launch = launch
        self._tokens = token_store
        self._on_lost = on_lost
        self._on_record_stopped = on_record_stopped
        self._stopping = False
        self._t: Transport | None = None
        self._rec = _RecordState()
        self._kinds: set[str] = set()
        self._info: ObsInfo | None = None
        self._recording_expected = False
        self._cap = kinds.capture_profile(os_name)
        self._local_device: str | None = None  # device UID the local track follows (in memory)
        self._local_device_refused: str | None = None  # last UID OBS did not list

    # ---- connection --------------------------------------------------------------------

    @property
    def recording_expected(self) -> bool:
        """True between a confirmed StartRecord and our own StopRecord."""
        return self._recording_expected

    def _event(self, event_type: str, data: dict[str, Any]) -> None:
        if event_type == "RecordStateChanged":
            with self._rec.changed:
                self._rec.state = data.get("outputState")
                if data.get("outputPath"):
                    self._rec.path = data["outputPath"]
                self._rec.changed.notify_all()
            stopped = data.get("outputState") == STATE_STOPPED
            if stopped and self._recording_expected and not self._stopping:
                # OBS ended the recording itself (disk full, encoder error, Stop in the OBS
                # window): the indicator must not keep saying "recording" (§2.5).
                log.warning("obs: recording stopped without a stop request")
                self._recording_expected = False
                if self._on_record_stopped is not None:
                    self._on_record_stopped()
        elif event_type in ("ExitStarted", "ConnectionLost"):
            log.warning("obs: %s", event_type)
            with self._rec.changed:
                self._rec.changed.notify_all()
            if self._on_lost is not None:
                self._on_lost()

    def ensure_running(self, *, timeout_s: float = 30) -> ObsInfo:
        """Launch OBS if needed, connect with backoff (cap 10 s) and authenticate."""
        if self._t is not None and self._t.connected and self._info is not None:
            return self._info
        if self._launch is not None:
            self._launch()
        deadline = time.monotonic() + timeout_s
        delay = 0.5
        last: Exception | None = None
        while time.monotonic() < deadline:
            try:
                t = self._connect()
                break
            except ObsConnectionError as exc:
                last = exc
                time.sleep(min(delay, max(0.0, deadline - time.monotonic())))
                delay = min(delay * 2, 10.0)
        else:
            raise ObsConnectionError(f"cannot connect to OBS: {last}")
        t.on_event(self._event)
        self._t = t
        version = t.request("GetVersion")
        self._kinds = set(t.request("GetInputKindList", {"unversioned": False})["inputKinds"])
        self._info = ObsInfo(
            obs_version=version.get("obsVersion"),
            ws_version=version.get("obsWebSocketVersion"),
            set_record_directory="SetRecordDirectory" in version.get("availableRequests", []),
        )
        profiles = t.request("GetProfileList")["profiles"]
        for name in (self.cfg.obs.profile_audio, self.cfg.obs.profile_video):
            if name not in profiles:
                raise ObsError(f"profile {name} missing: run `captation obs bootstrap`")
        if not self._recording_expected:
            # A crash can leave Captation inputs in the saved collection; they would hold the
            # microphone while idle (principles §5.2). Not while a recording is expected: a
            # reconnect in the middle of a session must not tear down its inputs.
            try:
                if not t.request("GetRecordStatus").get("outputActive"):
                    self._remove_leftovers()
            except ObsError:
                log.warning("obs: could not check for leftover Captation inputs")
        return self._info

    @property
    def _req(self) -> Callable[..., dict[str, Any]]:
        if self._t is None or not self._t.connected:
            raise ObsLost("not connected to OBS")
        return self._t.request

    def close(self) -> None:
        if self._t is not None:
            self._t.close()
            self._t = None

    def health(self) -> ObsHealth:
        if self._t is None or not self._t.connected:
            return ObsHealth(connected=False, recording=False)
        try:
            active = bool(self._t.request("GetRecordStatus").get("outputActive"))
        except ObsError:
            return ObsHealth(connected=False, recording=False)
        return ObsHealth(connected=True, recording=active)

    # ---- segment preparation ------------------------------------------------------------

    def _first_kind(self, candidates: tuple[str, ...]) -> str | None:
        return next((k for k in candidates if k in self._kinds), None)

    def _switch_profile(self, name: str) -> None:
        R = self._req
        if R("GetProfileList")["currentProfileName"] == name:
            return
        R("SetCurrentProfile", {"profileName": name})
        deadline = time.monotonic() + 15
        while R("GetProfileList")["currentProfileName"] != name:
            if time.monotonic() > deadline:
                raise ObsError(f"timeout switching to profile {name}")
            time.sleep(0.1)

    def _ensure_collection(self) -> None:
        """Only ever touch the Captation collection (docs/principles.md §4.2).

        Switches to it if needed."""
        name = self.cfg.obs.scene_collection
        R = self._req
        if R("GetSceneCollectionList")["currentSceneCollectionName"] == name:
            return
        R("SetCurrentSceneCollection", {"sceneCollectionName": name})
        deadline = time.monotonic() + 15
        while R("GetSceneCollectionList")["currentSceneCollectionName"] != name:
            if time.monotonic() > deadline:
                raise ObsError(f"timeout switching to scene collection {name}")
            time.sleep(0.1)

    def _resolve_device(self, input_name: str, wanted: str) -> str | None:
        items = self._req(
            "GetInputPropertiesListPropertyItems",
            {"inputName": input_name, "propertyName": self._cap.mic_device_property},
        )["propertyItems"]
        low = wanted.casefold()
        for item in items:
            if low in (str(item.get("itemName", "")).casefold(), str(item["itemValue"]).casefold()):
                return str(item["itemValue"])
        return None

    def _apply_local_device(self, device: str) -> bool:
        """Point ``Captation Local`` at ``device`` (a UID OBS lists); False if OBS lacks it."""
        value = self._resolve_device(LOCAL, device)
        if value is None:
            return False
        self._req(
            "SetInputSettings",
            {"inputName": LOCAL, "inputSettings": {self._cap.mic_device_property: value}},
        )
        self._local_device = device
        return True

    def follow_local_device(self, device: str | None) -> bool:
        """``follow_call`` during a recording: the call app switched its input (a headset
        connected or was removed). Changes the device of the running input, so the recording
        goes on without a gap. True if the device changed."""
        if self.cfg.recording.audio.local_input != "follow_call" or not self._recording_expected:
            return False
        if device is None or device in (self._local_device, self._local_device_refused):
            return False  # unchanged, or OBS does not list it: not asked again every poll
        if LOCAL not in self._captation_inputs():
            return False
        if self._apply_local_device(device):
            return True
        self._local_device_refused = device
        return False

    def _create_audio(self, scene: str, name: str, kind: str, settings: dict[str, Any]) -> None:
        R = self._req
        R(
            "CreateInput",
            {"sceneName": scene, "inputName": name, "inputKind": kind, "inputSettings": settings},
        )

    def _route(self, name: str, role_track: int) -> None:
        self._req(
            "SetInputAudioTracks", {"inputName": name, "inputAudioTracks": tracks_for(role_track)}
        )
        self._req("SetInputAudioMonitorType", {"inputName": name, "monitorType": kinds.MONITOR_OFF})

    def _captation_inputs(self) -> list[str]:
        return [
            str(i["inputName"])
            for i in self._req("GetInputList")["inputs"]
            if str(i["inputName"]).startswith(INPUT_PREFIX)
        ]

    def _remove_inputs(self, names: list[str], *, timeout_s: float = 5) -> None:
        """RemoveInput completes asynchronously: wait until OBS no longer lists them."""
        for name in names:
            self._req("RemoveInput", {"inputName": name})
        deadline = time.monotonic() + timeout_s
        while set(names) & set(self._captation_inputs()):
            if time.monotonic() > deadline:
                raise ObsError("Captation inputs remain after teardown")
            time.sleep(0.05)

    def _remove_leftovers(self) -> None:
        self._remove_inputs(self._captation_inputs())

    def _ensure_idle(self, grace_s: float = 3) -> None:
        """Profile/scene switches only while not recording (docs/principles.md §5.4).

        ``GetRecordStatus`` can lag a moment behind a STOPPED event, so a short grace period
        is waited out. A recording that persists (not ours, or still running) is an error.
        """
        for request, what in OTHER_OUTPUTS:
            try:
                active = self._req(request).get("outputActive")
            except ObsRequestError:
                continue  # output type not available in this OBS build
            if active:
                raise ObsError(f"OBS {what} is active: Captation does not switch scenes now")
        deadline = time.monotonic() + grace_s
        while self._req("GetRecordStatus").get("outputActive"):
            if self._recording_expected or time.monotonic() > deadline:
                raise ObsError("OBS is recording: profile and scene switches are not allowed now")
            time.sleep(0.1)

    def prepare_segment(self, mode: Mode, target: AppRef | None) -> PreparedSegment:
        R = self._req
        self._ensure_idle()
        warnings: list[str] = []
        o = self.cfg.obs
        self._ensure_collection()
        self._switch_profile(o.profile_video if mode == "video" else o.profile_audio)
        scene = VIDEO_SCENE if mode == "video" else AUDIO_SCENE
        if R("GetCurrentProgramScene").get("currentProgramSceneName") != scene:
            R("SetCurrentProgramScene", {"sceneName": scene})
        self._remove_leftovers()
        roles = track_map(self.cfg)
        audio = self.cfg.recording.audio

        # remote: per-app capture of the call app, else desktop audio (warning)
        cap = self._cap
        app_kind = self._first_kind(cap.app_audio_kinds)
        if target is not None and app_kind is not None and cap.can_target_app(target.bundle_id):
            self._create_audio(
                scene,
                REMOTE,
                app_kind,
                cap.app_audio_settings(target.process_name, target.bundle_id, target.exe_path),
            )
        else:
            desk = self._first_kind(cap.desktop_audio_kinds)
            if desk is None:
                raise ObsError("no desktop-audio input kind available in this OBS")
            self._create_audio(scene, REMOTE, desk, {})
            warnings.append("remote:desktop_fallback")
        self._route(REMOTE, roles["remote"])

        # local: default input or a named device (fallback to default with a warning)
        mic_kind = self._first_kind(cap.mic_kinds)
        if mic_kind is None:
            raise ObsError("no microphone input kind available in this OBS")
        self._create_audio(scene, LOCAL, mic_kind, {cap.mic_device_property: cap.mic_default})
        self._local_device = None
        self._local_device_refused = None
        if audio.local_input == "follow_call":
            # The mic the call app records from (any headset), else the default input.
            device = target.input_device if target is not None else None
            if device is not None and not self._apply_local_device(device):
                warnings.append("local:call_device_not_found")
        elif audio.local_input != "follow_default":
            device = self._resolve_device(LOCAL, audio.local_input)
            if device is None:
                warnings.append("local:device_not_found")
            else:
                R(
                    "SetInputSettings",
                    {"inputName": LOCAL, "inputSettings": {cap.mic_device_property: device}},
                )
        self._route(LOCAL, roles["local"])

        if "external" in roles and audio.external_input_device:
            self._create_audio(
                scene, EXTERNAL, mic_kind, {cap.mic_device_property: cap.mic_default}
            )
            device = self._resolve_device(EXTERNAL, audio.external_input_device)
            if device is None:
                warnings.append("external:device_not_found")
                self._remove_inputs([EXTERNAL])
            else:
                R(
                    "SetInputSettings",
                    {"inputName": EXTERNAL, "inputSettings": {cap.mic_device_property: device}},
                )
                self._route(EXTERNAL, roles["external"])

        capture: CaptureKind = "none"
        if mode == "video":
            capture = self._prepare_video(scene, warnings, target)
        return PreparedSegment(mode=mode, capture=capture, warnings=tuple(warnings))

    def _prepare_video(self, scene: str, warnings: list[str], target: AppRef | None) -> CaptureKind:
        """Window capture of the call app where the OS allows it (ADR-008), else the screen.

        Linux/Wayland: portal screen capture with a stored restore token (PF-019).
        """
        cap = self._cap
        if self.cfg.recording.video.capture == "window_preferred" and target is not None:
            window_kind = self._first_kind(cap.window_kinds)
            settings = cap.window_settings(target.process_name, target.bundle_id, target.exe_path)
            if window_kind is not None and settings is not None:
                self._req(
                    "CreateInput",
                    {
                        "sceneName": scene,
                        "inputName": SCREEN,
                        "inputKind": window_kind,
                        "inputSettings": settings,
                    },
                )
                return "window"
        screen_kind = self._first_kind(cap.screen_kinds)
        token = self._tokens.load() if (self._tokens and cap.needs_screen_token) else None
        screen = cap.screen_settings(token)
        if screen_kind is None or (cap.needs_screen_token and screen is None):
            warnings.append("capture:not_approved")
            return "none"
        if screen is not None and cap.main_display is not None:
            screen = self._with_main_display(screen, *cap.main_display)
        self._req(
            "CreateInput",
            {
                "sceneName": scene,
                "inputName": SCREEN,
                "inputKind": screen_kind,
                "inputSettings": screen or {},
            },
        )
        if self.cfg.recording.video.capture == "window_preferred":
            warnings.append("capture:display_fallback")
        return "display"

    def _with_main_display(self, settings: dict[str, Any], kind: str, key: str) -> dict[str, Any]:
        """Name the main display explicitly: an empty display records black frames (macOS)."""
        if settings.get(key):
            return settings
        try:
            value = self._req("GetInputDefaultSettings", {"inputKind": kind})[
                "defaultInputSettings"
            ].get(key)
        except ObsError:
            value = None
        if not isinstance(value, str) or not value:
            log.warning("video: main display unknown, display capture may stay black")
            return settings
        return {**settings, key: value}

    # ---- recording -----------------------------------------------------------------------

    def _set_and_verify(self, key: tuple[str, str], value: str) -> None:
        cat, name = key
        self._req(
            "SetProfileParameter",
            {"parameterCategory": cat, "parameterName": name, "parameterValue": value},
        )
        got = self._req("GetProfileParameter", {"parameterCategory": cat, "parameterName": name})
        if got.get("parameterValue") != value:
            raise ObsError(f"OBS did not accept {cat}.{name}")

    def _wait_state(self, state: str, timeout_s: float) -> None:
        deadline = time.monotonic() + timeout_s
        with self._rec.changed:
            while self._rec.state != state:
                if self._t is None or not self._t.connected:
                    raise ObsLost(f"OBS lost while waiting for {state}")
                left = deadline - time.monotonic()
                if left <= 0:
                    raise ObsError(f"timeout waiting for {state}")
                self._rec.changed.wait(min(left, 0.5))

    def start_segment(self, record_dir: Path, basename: str) -> None:
        flatpak = self._installation is not None and self._installation.kind == "flatpak"
        if flatpak and str(record_dir.resolve()).startswith("/tmp/"):
            raise ObsError("the OBS Flatpak cannot write to /tmp (PF-011): move data_root")
        info = self._info or self.ensure_running()
        directory = str(record_dir.resolve())  # OBS resolves relative paths against its own cwd
        # The track layout, container and split setting decide which file holds which role:
        # set and verify them for every segment, not only at bootstrap (a changed config or
        # an edit in the OBS window would map tracks to the wrong role, audit 2026-11-24).
        from captation.obs.bootstrap import track_bitmask

        self._set_and_verify(kinds.P_REC_TRACKS, str(track_bitmask(self.cfg)))
        self._set_and_verify(kinds.P_REC_FORMAT, kinds.V_FORMAT_MKV)
        self._set_and_verify(kinds.P_REC_SPLIT, kinds.V_FALSE)
        if info.set_record_directory:
            self._req("SetRecordDirectory", {"recordDirectory": directory})
            if self._req("GetRecordDirectory").get("recordDirectory") != directory:
                raise ObsError("OBS did not accept the record directory")
        else:
            self._set_and_verify(kinds.P_REC_PATH, directory)
        self._set_and_verify(kinds.P_FILENAME_FORMAT, basename)
        # The track mask follows the config even if it changed after bootstrap.
        self._set_and_verify(kinds.P_REC_TRACKS, str(self._track_mask()))
        with self._rec.changed:
            self._rec.state = None
            self._rec.path = None
        self._req("StartRecord")
        self._recording_expected = True
        self._wait_state(STATE_STARTED, 15)

    def _track_mask(self) -> int:
        tracks = self.cfg.recording.audio.tracks
        numbers = [tracks.mix, tracks.remote, tracks.local]
        if tracks.external is not None:
            numbers.append(tracks.external)
        return sum(1 << (n - 1) for n in numbers)

    def stop_segment(self, timeout_s: float = 20) -> Path:
        """Stop and confirm. An unconfirmed stop is checked and retried once; if OBS still
        records afterwards this raises, and the caller must not finalise the session."""
        current = str(self._req("GetProfileList")["currentProfileName"])
        if current not in (self.cfg.obs.profile_audio, self.cfg.obs.profile_video):
            raise ObsError("refusing to stop a recording outside Captation profiles")
        self._stopping = True
        try:
            self._req("StopRecord")
            try:
                self._wait_state(STATE_STOPPED, timeout_s)
            except ObsLost:
                raise
            except ObsError as exc:
                if not self._req("GetRecordStatus").get("outputActive"):
                    msg = f"{exc} (no stop event; OBS reports the recording stopped)"
                    raise ObsError(msg) from exc
                log.warning("obs: stop not confirmed, retrying")
                with contextlib.suppress(ObsRequestError):
                    self._req("StopRecord")
                self._wait_state(STATE_STOPPED, timeout_s)
        finally:
            self._stopping = False
        self._recording_expected = False
        path = Path(self._rec.path or "")
        if not path.is_file() or path.stat().st_size == 0:
            raise ObsError("OBS reported a stopped recording but the file is missing or empty")
        return path

    def teardown_segment(self) -> None:
        names = self._captation_inputs()
        if SCREEN in names and self._tokens is not None:
            settings = self._req("GetInputSettings", {"inputName": SCREEN})["inputSettings"]
            token = settings.get(self._cap.screen_token_key or "")
            if token:
                self._tokens.save(str(token))
        self._remove_inputs(names)
        self._local_device = None

    def reset(self) -> None:
        """Best effort: stop a Captation recording and remove Captation inputs (recovery).

        A recording in a non-Captation profile belongs to the user and is left alone.
        """
        try:
            current = str(self._req("GetProfileList")["currentProfileName"])
            ours = current in (self.cfg.obs.profile_audio, self.cfg.obs.profile_video)
            if ours and self._req("GetRecordStatus").get("outputActive"):
                with self._rec.changed:
                    self._rec.state = None
                self._stopping = True
                try:
                    self._req("StopRecord")
                    self._wait_state(STATE_STOPPED, 20)
                finally:
                    self._stopping = False
        except ObsRequestError as exc:
            if exc.code != OUTPUT_NOT_RUNNING:
                raise
        self._recording_expected = False
        self._remove_leftovers()

    # ---- one-time screen-capture approval (Wayland portal) --------------------------------

    def approve_screen(self, *, timeout_s: float = 120) -> bool:
        """Create the screen source once so the user can approve the portal dialog."""
        screen_kind = self._first_kind(self._cap.screen_kinds)
        if screen_kind is None or self._tokens is None or not self._cap.needs_screen_token:
            return False
        R = self._req
        self._ensure_idle()  # never while a recording runs (release review)
        self._ensure_collection()
        self._switch_profile(self.cfg.obs.profile_video)
        self._remove_leftovers()
        R("CreateInput", {"sceneName": VIDEO_SCENE, "inputName": SCREEN, "inputKind": screen_kind})
        deadline = time.monotonic() + timeout_s
        token = None
        while time.monotonic() < deadline and not token:
            time.sleep(1)
            token = R("GetInputSettings", {"inputName": SCREEN})["inputSettings"].get(
                self._cap.screen_token_key or ""
            )
        self._remove_inputs([SCREEN])
        self._switch_profile(self.cfg.obs.profile_audio)
        if token:
            self._tokens.save(str(token))
        return bool(token)
