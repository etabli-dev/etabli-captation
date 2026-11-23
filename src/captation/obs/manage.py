"""Shared OBS plumbing for the CLI, doctor and the controller: locate, launch, connect."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from platformdirs import user_state_dir

from captation.config import APP_AUTHOR, APP_NAME, Config, ensure_private_dir
from captation.models import ObsInstallation
from captation.obs import credentials, launcher
from captation.obs.client import ObsConnectionError, ObsWebSocket
from captation.obs.controller import FileTokenStore, ObsController
from captation.platform import current_os, get_platform

log = logging.getLogger(__name__)

INSTALL_HINT = {
    "linux": " (Linux: flatpak install --user flathub com.obsproject.Studio "
    "com.obsproject.Studio.Plugin.PipeWireAudioCapture)",
    "macos": " (macOS: the .dmg, then move OBS.app to /Applications)",
    "windows": " (Windows: the installer, default path %ProgramFiles%\\obs-studio)",
}


def locate(cfg: Config) -> ObsInstallation:
    inst = get_platform().obs_locator(cfg.obs.executable).find()
    if inst is None:
        hint = INSTALL_HINT.get(current_os(), "")
        raise launcher.ObsNotFound(
            f"OBS Studio not found. Install it from {launcher.OBS_DOWNLOAD_URL}{hint}"
        )
    return inst


def profile_exists(inst: ObsInstallation, name: str) -> bool:
    root = Path(inst.config_dir) / "basic" / "profiles"
    if not root.is_dir():
        return False
    for ini in root.glob("*/basic.ini"):
        for line in ini.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.strip() == f"Name={name}":
                return True
    return False


def collection_exists(inst: ObsInstallation, name: str) -> bool:
    scenes = Path(inst.config_dir) / "basic" / "scenes"
    return any(
        p.is_file() and name in p.read_text(encoding="utf-8", errors="replace")[:4096]
        for p in scenes.glob("*.json")
    )


def obs_running(cfg: Config) -> bool:
    return launcher.port_open(cfg.obs.websocket.host, cfg.obs.websocket.port)


def ensure_running(cfg: Config, inst: ObsInstallation, *, timeout_s: float = 30) -> bool:
    """Start OBS with the Captation profile and collection if it is not reachable.

    Returns True if OBS was started by this call.
    """
    ws = cfg.obs.websocket
    if launcher.port_open(ws.host, ws.port):
        return False
    profile = cfg.obs.profile_audio if profile_exists(inst, cfg.obs.profile_audio) else None
    coll = cfg.obs.scene_collection
    collection = coll if collection_exists(inst, coll) else None
    launcher.start(inst, profile=profile, collection=collection, extra=cfg.obs.launch_args)
    if not launcher.wait_for_port(ws.host, ws.port, timeout_s):
        raise ObsConnectionError(
            f"OBS did not open the websocket on port {ws.port} within {timeout_s:.0f} s "
            "(after a crash OBS may wait for 'Run Normally' in its safe-mode dialog)"
        )
    return True


def connect(cfg: Config, *, timeout_s: float = 5) -> ObsWebSocket:
    ws_cfg = cfg.obs.websocket
    client = ObsWebSocket(ws_cfg.host, ws_cfg.port, credentials.get_password(ws_cfg))
    client.connect(timeout_s)
    return client


def _launch(cfg: Config, inst: ObsInstallation) -> None:
    ensure_running(cfg, inst)


def make_controller(
    cfg: Config,
    *,
    on_lost: Callable[[], None] | None = None,
    on_record_stopped: Callable[[], None] | None = None,
    launch: bool = True,
) -> ObsController:
    """An ObsController wired to the real OBS: locate, launch on demand, keyring auth."""
    inst = locate(cfg)
    state = ensure_private_dir(Path(user_state_dir(APP_NAME, APP_AUTHOR)))
    return ObsController(
        cfg,
        lambda: connect(cfg),
        os_name=current_os(),
        installation=inst,
        launch=(lambda: _launch(cfg, inst)) if launch else None,
        token_store=FileTokenStore(state / "screencast-token"),
        on_lost=on_lost,
        on_record_stopped=on_record_stopped,
    )
