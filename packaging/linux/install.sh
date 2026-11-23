#!/usr/bin/env bash
# Install Établi Captation for the current user (no root needed).
# OBS Studio is NOT bundled: install it from https://obsproject.com/download
# (Linux: flatpak install --user flathub com.obsproject.Studio
#         com.obsproject.Studio.Plugin.PipeWireAudioCapture).
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
target="${XDG_DATA_HOME:-$HOME/.local/share}/etabli-captation"
bindir="$HOME/.local/bin"
mkdir -p "$target" "$bindir"
rm -rf "$target/current.new"
cp -a "$here" "$target/current.new"
rm -rf "$target/current"
mv "$target/current.new" "$target/current"
ln -sfn "$target/current/captation" "$bindir/captation"
echo "installed: $bindir/captation -> $target/current/captation"
echo
# Disclaimer and conditions of use (R-59): accepted with a checkbox before the first use.
if [ -t 0 ] && [ -t 1 ]; then
  "$bindir/captation" accept || echo "not accepted: the agent will not arm until 'captation accept' is done"
else
  echo "Run 'captation accept' before the first use (disclaimer checkbox)."
fi
echo
echo "Next steps:"
echo "  captation accept                   # disclaimer checkbox (if not done above)"
echo "  captation obs bootstrap            # profiles, scene collection, websocket password"
echo "  captation obs firewall --apply     # blocks the websocket port for the LAN (sudo, once)"
echo "  captation obs approve-screen       # one-time screen-capture approval (Wayland)"
echo "  captation install-autostart        # start at login (always disarmed)"
echo "  captation doctor                   # check everything"
