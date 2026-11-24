"""PyInstaller entry point: the `captation` command, and the app opened from Finder."""

import sys

from captation.cli import app, open_app

if __name__ == "__main__":
    # Opened as an app (Finder, Spotlight, Launchpad): no arguments and no terminal. The agent
    # runs in the background; opening the app again shows its menu-bar menu.
    if len(sys.argv) == 1 and not sys.stdin.isatty():
        open_app()
    else:
        app()
