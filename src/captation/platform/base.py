"""Platform interfaces. Shared code depends only on these protocols.

OS modules (``linux.py``, ``macos.py``, ``windows.py``) are imported lazily by
:func:`captation.platform.get_platform`, so shared modules never import OS code at top level.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from captation.models import AutostartStatus, EncryptionStatus, ObsInstallation, ProbeSnapshot


class CallProbe(Protocol):
    """Reports which processes use the microphone and camera."""

    def snapshot(self) -> ProbeSnapshot: ...

    def close(self) -> None: ...


class AutostartManager(Protocol):
    """Installs the agent to start at login. Autostart never arms."""

    def install(self, *, systemd: bool = False) -> list[Path]: ...

    def uninstall(self) -> list[Path]: ...

    def status(self) -> AutostartStatus: ...


class DiskEncryptionCheck(Protocol):
    def status(self, path: Path) -> EncryptionStatus: ...


class ObsLocator(Protocol):
    """Finds OBS. Returns ``None`` if OBS is not installed (never installs anything)."""

    def find(self) -> ObsInstallation | None: ...


class PlatformServices(Protocol):
    """Bundle of the OS-specific services."""

    name: str

    def probe(
        self,
        *,
        exclude_pids: frozenset[int],
        store_titles: bool,
        obs_capturing: Callable[[], bool] | None = None,
    ) -> CallProbe: ...

    def autostart(self) -> AutostartManager: ...

    def disk_encryption(self) -> DiskEncryptionCheck: ...

    def obs_locator(self, executable: str | None) -> ObsLocator: ...
