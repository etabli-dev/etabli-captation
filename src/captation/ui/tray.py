"""Tray indicator entry point: picks the platform back end (see captation.ui.base)."""

from captation.ui.base import MenuItem, TrayState, UiBackend, UnavailableUi, default_backend

__all__ = ["MenuItem", "TrayState", "UiBackend", "UnavailableUi", "default_backend"]
