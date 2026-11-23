#!/usr/bin/env bash
# Build "Établi Captation.app" (ADR-005, stage 11): PyInstaller one-folder .app plus the Swift
# probe in Contents/MacOS. OBS is never bundled.
#
# Usage (on a Mac, from anywhere):  packaging/macos/build.sh
#   → dist/etabli-captation-<version>-macos-<arch>.zip  (+ .dmg, + .sha256 files)
#
# Signing:
#   default                     ad-hoc signature (codesign -s -), not notarised
#   MACOS_SIGN_IDENTITY         "Developer ID Application: … (TEAMID)" in the keychain:
#                               hardened runtime + timestamp + packaging/macos/entitlements.plist
# Notarisation (only with MACOS_SIGN_IDENTITY), either
#   MACOS_NOTARY_PROFILE        a `xcrun notarytool store-credentials` keychain profile, or
#   MACOS_NOTARY_APPLE_ID + MACOS_NOTARY_TEAM_ID + MACOS_NOTARY_PASSWORD (app-specific password)
set -euo pipefail
cd "$(dirname "$0")/../.."

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "packaging/macos/build.sh runs on macOS only" >&2
  exit 2
fi

version=$(uv run python -c 'from captation.version import __version__; print(__version__)')
# CFBundleShortVersionString allows at most three integers: 0.1.0.dev0 → 0.1.0
short_version=$(printf '%s' "$version" | sed -E 's/^([0-9]+(\.[0-9]+){0,2}).*/\1/')
arch=$(uname -m)
app_name="Établi Captation.app"
app="dist/${app_name}"
name="etabli-captation-${version}-macos-${arch}"
identity="${MACOS_SIGN_IDENTITY:-}"
entitlements="packaging/macos/entitlements.plist"

# 1. Swift probe (ADR-004)
swift build -c release --package-path probes/macos/CaptationProbe
probe="probes/macos/CaptationProbe/.build/release/captation-probe"
test -x "$probe"

# 2. PyInstaller .app (the spec signs the embedded binaries with $MACOS_SIGN_IDENTITY or ad hoc)
rm -rf "$app" "dist/captation" build/pyinstaller-macos
CAPTATION_VERSION="$version" CAPTATION_SHORT_VERSION="$short_version" \
  uv run --group build pyinstaller --noconfirm --clean \
  --distpath dist --workpath build/pyinstaller-macos \
  packaging/macos/captation.spec

# 3. The probe lives next to the agent executable (MacProbe looks in Contents/MacOS, PF-119)
install -m 0755 "$probe" "$app/Contents/MacOS/captation-probe"

# 4. Sign: nested code first, then the bundle (adding the probe invalidated the seal)
if [[ -n "$identity" ]]; then
  sign=(codesign --force --options runtime --timestamp --sign "$identity")
  "${sign[@]}" "$app/Contents/MacOS/captation-probe"
  "${sign[@]}" --entitlements "$entitlements" "$app"
  signing="Developer ID (${identity})"
else
  codesign --force --sign - "$app/Contents/MacOS/captation-probe"
  codesign --force --sign - "$app"
  signing="ad-hoc (not notarised): first start needs right-click → Open, or System Settings → Privacy & Security → Open Anyway"
fi
codesign --verify --strict --verbose=2 "$app"

notarise() {  # $1 = file to submit
  if [[ -n "${MACOS_NOTARY_PROFILE:-}" ]]; then
    xcrun notarytool submit "$1" --keychain-profile "$MACOS_NOTARY_PROFILE" --wait
  else
    xcrun notarytool submit "$1" --apple-id "$MACOS_NOTARY_APPLE_ID" \
      --team-id "$MACOS_NOTARY_TEAM_ID" --password "$MACOS_NOTARY_PASSWORD" --wait
  fi
}

notary=false
if [[ -n "$identity" && ( -n "${MACOS_NOTARY_PROFILE:-}" || -n "${MACOS_NOTARY_APPLE_ID:-}" ) ]]; then
  notary=true
fi

# 5. Notarise the app (zip submission), staple the ticket to the app
if $notary; then
  ditto -c -k --sequesterRsrc --keepParent "$app" "dist/${name}.notary.zip"
  notarise "dist/${name}.notary.zip"
  rm -f "dist/${name}.notary.zip"
  xcrun stapler staple "$app"
  signing="Developer ID (${identity}), notarised and stapled"
fi

# 6. Artefacts: zip (ditto keeps the signature and extended attributes) and dmg
rm -f "dist/${name}.zip" "dist/${name}.dmg"
ditto -c -k --sequesterRsrc --keepParent "$app" "dist/${name}.zip"
staging=$(mktemp -d)
trap 'rm -rf "$staging"' EXIT
ditto "$app" "$staging/${app_name}"
ln -s /Applications "$staging/Applications"
hdiutil create -volname "Établi Captation" -srcfolder "$staging" -ov -format UDZO "dist/${name}.dmg"
if [[ -n "$identity" ]]; then
  codesign --force --timestamp --sign "$identity" "dist/${name}.dmg"
  if $notary; then
    notarise "dist/${name}.dmg"
    xcrun stapler staple "dist/${name}.dmg"
  fi
fi
for f in "dist/${name}.zip" "dist/${name}.dmg"; do
  shasum -a 256 "$f" > "$f.sha256"
done
# Release-notes snippet (the release workflow appends it to the draft release body)
printf '\nmacOS %s (%s): %s\n' "$version" "$arch" "$signing" > "dist/macos-signing.txt"
echo "built dist/${name}.zip and dist/${name}.dmg"
echo "signing: ${signing}"
