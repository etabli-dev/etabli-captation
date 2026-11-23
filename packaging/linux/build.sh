#!/usr/bin/env bash
# Build the Linux one-folder bundle and tarball (ADR-005). OBS is never bundled.
# Usage: packaging/linux/build.sh   → dist/etabli-captation-<version>-linux-x86_64.tar.gz
set -euo pipefail
cd "$(dirname "$0")/../.."
version=$(uv run python -c 'from captation.version import __version__; print(__version__)')
rm -rf build/pyinstaller dist/captation
# Licence gate for this platform's runtime closure (fails on GPL, AGPL or unknown licences) and
# the third-party notices shipped in the bundle.
uv run python tools/licence_gate.py --notices build/THIRD_PARTY_NOTICES.txt
uv run --group build pyinstaller \
  --noconfirm --clean --onedir --name captation \
  --distpath dist --workpath build/pyinstaller --specpath build/pyinstaller \
  --collect-data captation \
  --collect-data jsonschema_specifications \
  --collect-submodules keyring.backends \
  --copy-metadata keyring \
  --hidden-import captation.platform.linux \
  --hidden-import captation.ui.linux \
  packaging/captation_entry.py
cp packaging/linux/install.sh dist/captation/
cp LICENSE DISCLAIMER.md build/THIRD_PARTY_NOTICES.txt config/config.example.yaml dist/captation/
name="etabli-captation-${version}-linux-$(uname -m)"
tar -C dist -czf "dist/${name}.tar.gz" --transform "s,^captation,${name}," captation
sha256sum "dist/${name}.tar.gz" > "dist/${name}.tar.gz.sha256"
echo "built dist/${name}.tar.gz"
