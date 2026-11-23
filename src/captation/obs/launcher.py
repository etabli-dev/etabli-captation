"""Locate, start and reach OBS Studio. OBS is never installed or bundled (docs/principles.md §4.1).

Only verified CLI flags are used (``obs/kinds.py``, PF references). The websocket password is
never passed on the command line: it lives in the obs-websocket config file and the keyring.
"""

from __future__ import annotations

import logging
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

from captation.models import ObsInstallation
from captation.obs import kinds

log = logging.getLogger(__name__)

OBS_DOWNLOAD_URL = "https://obsproject.com/download"
SECRET_ENV_PREFIX = "CAPTATION_"  # covers CAPTATION_OBS_PASSWORD (obs.websocket.password_env)


class ObsNotFound(Exception):
    pass


def launch_args(
    profile: str | None, collection: str | None, extra: list[str], os_name: str | None = None
) -> list[str]:
    """Verified flags only (per OS). The updater-disable flag is included where it exists."""
    from captation.platform import current_os

    verified = kinds.launch_flags(os_name or current_os())
    args = [
        f
        for f in (
            kinds.FLAG_MINIMIZE_TO_TRAY,
            kinds.FLAG_DISABLE_MISSING_FILES_CHECK,
            kinds.FLAG_DISABLE_UPDATER,
        )
        if f in verified
    ]
    if profile and kinds.FLAG_PROFILE in verified:
        args += [kinds.FLAG_PROFILE, profile]
    if collection and kinds.FLAG_COLLECTION in verified:
        args += [kinds.FLAG_COLLECTION, collection]
    from captation.config import SAFE_OBS_LAUNCH_ARGS

    return args + [a for a in extra if a in SAFE_OBS_LAUNCH_ARGS]


def start(
    installation: ObsInstallation,
    *,
    profile: str | None,
    collection: str | None,
    extra: list[str] | None = None,
) -> subprocess.Popen[bytes]:
    argv = [*installation.command, *launch_args(profile, collection, extra or [])]
    log.info("starting OBS (%s)", installation.kind)
    # OBS must not inherit the websocket password (env fallback) or any other secret of ours.
    env = {k: v for k, v in os.environ.items() if not k.startswith(SECRET_ENV_PREFIX)}
    # Windows: OBS finds its data relative to the working directory (PF-205).
    cwd = os.path.dirname(installation.command[0]) if sys.platform == "win32" else None
    return subprocess.Popen(
        argv,
        env=env,
        cwd=cwd or None,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def port_open(host: str, port: int, timeout_s: float = 0.5) -> bool:
    family = socket.AF_INET6 if host == "::1" else socket.AF_INET
    target = "127.0.0.1" if host == "localhost" else host
    with socket.socket(family, socket.SOCK_STREAM) as s:
        s.settimeout(timeout_s)
        try:
            s.connect((target, port))
        except OSError:
            return False
    return True


def wait_for_port(host: str, port: int, timeout_s: float = 30) -> bool:
    """Exponential backoff (0.25 s … 4 s) until the websocket port accepts connections."""
    deadline = time.monotonic() + timeout_s
    delay = 0.25
    while time.monotonic() < deadline:
        if port_open(host, port):
            return True
        time.sleep(min(delay, max(0.0, deadline - time.monotonic())))
        delay = min(delay * 2, 4.0)
    return port_open(host, port)


def websocket_config_path(installation: ObsInstallation) -> Path:
    return Path(installation.config_dir) / kinds.WEBSOCKET_CONFIG_RELPATH
