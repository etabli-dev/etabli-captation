# PyInstaller spec for "Établi Captation.app" (ADR-005). Run via packaging/macos/build.sh,
# which also adds the Swift probe and signs the bundle.
#
# Info.plist: bundle ID de.etabli.captation, LSUIElement (menu-bar only, no Dock icon).
# No NSMicrophoneUsageDescription / NSCameraUsageDescription: the agent never opens the
# microphone or the camera. OBS does, and the agent starts OBS through LaunchServices
# (`open -a`, PF-103), so OBS is its own TCC-responsible process and asks for Microphone and
# Screen Recording itself. The probe only reads device state (PF-113/PF-115). If macOS ever
# shows a microphone prompt naming "Établi Captation", the launch path is wrong: fix that
# instead of adding a usage string.
import os

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

ROOT = os.path.abspath(os.path.join(SPECPATH, "..", ".."))  # noqa: F821 (PyInstaller global)
VERSION = os.environ.get("CAPTATION_VERSION", "0.0.0")
SHORT_VERSION = os.environ.get("CAPTATION_SHORT_VERSION", "0.0.0")
IDENTITY = os.environ.get("MACOS_SIGN_IDENTITY") or None
ENTITLEMENTS = os.path.join(SPECPATH, "entitlements.plist") if IDENTITY else None  # noqa: F821
# Written by tools/licence_gate.py --notices (packaging/macos/build.sh runs it first); a relative
# path is relative to the repository root.
NOTICES = os.path.join(
    ROOT, os.environ.get("CAPTATION_NOTICES") or os.path.join("build", "THIRD_PARTY_NOTICES.txt")
)

datas = (
    collect_data_files("captation")
    + collect_data_files("jsonschema_specifications")
    + copy_metadata("keyring")
    # Licence, disclaimer and third-party notices end up in Contents/Resources.
    + [
        (os.path.join(ROOT, "LICENSE"), "."),
        (os.path.join(ROOT, "DISCLAIMER.md"), "."),
        (NOTICES, "."),
    ]
)
hiddenimports = (
    collect_submodules("keyring.backends")
    + collect_submodules("desktop_notifier.backends")
    + [
        "captation.platform.macos",
        "captation.ui.macos",
        "captation.ui.pystray_ui",
        "captation.obs.kinds_macos",
        "captation.obs.firewall_macos",
        "pystray._darwin",
    ]
)

a = Analysis(  # noqa: F821
    [os.path.join(ROOT, "packaging", "captation_entry.py")],
    pathex=[os.path.join(ROOT, "src")],
    datas=datas,
    hiddenimports=hiddenimports,
    # pystray (LGPL-3.0) stays a separate, replaceable module (docs/licensing.md): its .py
    # source files are collected as files next to the other bundled files, not into the PYZ.
    module_collection_mode={"pystray": "py"},
    # tkinter stays in: `captation accept` shows the DISCLAIMER.md checkbox dialog with it (R-59).
)
pyz = PYZ(a.pure)  # noqa: F821
exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="captation",
    console=False,
    argv_emulation=False,
    codesign_identity=IDENTITY,
    entitlements_file=ENTITLEMENTS,
)
coll = COLLECT(exe, a.binaries, a.datas, name="captation")  # noqa: F821
app = BUNDLE(  # noqa: F821
    coll,
    name="Établi Captation.app",
    bundle_identifier="de.etabli.captation",
    version=SHORT_VERSION,
    info_plist={
        "CFBundleName": "Établi Captation",
        "CFBundleDisplayName": "Établi Captation",
        "CFBundleShortVersionString": SHORT_VERSION,
        "CFBundleVersion": SHORT_VERSION,
        "CaptationVersion": VERSION,
        "LSUIElement": True,
        "LSMinimumSystemVersion": "14.0",
        "NSHighResolutionCapable": True,
    },
)
