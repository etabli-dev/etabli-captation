"""obs-websocket password: OS keyring first, environment variable as fallback.

The password is never logged, never written to reports and never passed on a command line.
"""

from __future__ import annotations

import os
import secrets

import keyring
from keyring.errors import KeyringError

from captation.config import WebsocketConfig
from captation.obs.client import ObsConnectionError

KEYRING_SERVICE = "etabli-captation"
KEYRING_USER = "obs-websocket"
MIN_LENGTH = 32


class PasswordMissing(ObsConnectionError):
    """No password: OBS cannot be reached, handled like any connection failure."""


def generate() -> str:
    """A random password of 43 URL-safe characters (≥ 32 required)."""
    return secrets.token_urlsafe(32)


def get_password(ws: WebsocketConfig) -> str:
    if ws.password_source == "keyring":
        try:
            value = keyring.get_password(KEYRING_SERVICE, KEYRING_USER)
        except KeyringError:
            value = None  # keyring unreachable: fall back to the environment variable
        if value:
            return str(value)
    env = os.environ.get(ws.password_env)
    if env:
        return env
    raise PasswordMissing(
        "no obs-websocket password: run `captation obs bootstrap` or `captation obs set-password`"
    )


def has_keyring_password() -> bool:
    try:
        return bool(keyring.get_password(KEYRING_SERVICE, KEYRING_USER))
    except KeyringError:
        return False


def store_password(password: str) -> None:
    if len(password) < MIN_LENGTH:
        raise ValueError(f"the obs-websocket password must have at least {MIN_LENGTH} characters")
    keyring.set_password(KEYRING_SERVICE, KEYRING_USER, password)
