"""Lazy platform factory."""

from __future__ import annotations

import sys
from importlib import import_module

from captation.models import OsName
from captation.platform.base import PlatformServices


def current_os() -> OsName:
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform == "darwin":
        return "macos"
    if sys.platform in ("win32", "cygwin"):
        return "windows"
    raise RuntimeError(f"unsupported platform: {sys.platform}")


def get_platform() -> PlatformServices:
    """Import the OS module only when it is needed."""
    module = import_module(f"captation.platform.{current_os()}")
    services: PlatformServices = module.SERVICES
    return services
