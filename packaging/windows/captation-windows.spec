# PyInstaller spec for Windows (ADR-005). OBS Studio is never bundled.
#
# One folder, two executables over the same _internal files:
#   captation.exe   console app for the CLI (doctor, arm, status, obs ..., install-autostart)
#   captationw.exe  windowed app (no console) that the Task Scheduler task and the Start-menu
#                   shortcut start with the argument "run" (PF-224, PF-270)
#
# Build with packaging/windows/build.ps1 (it passes --distpath/--workpath).
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

ROOT = Path(SPECPATH).resolve().parent.parent  # noqa: F821 - SPECPATH is set by PyInstaller

hiddenimports = [
    # loaded by name at runtime (platform/__init__.py, ui/base.py, obs/kinds.py, obs/firewall.py)
    "captation.platform.windows",
    "captation.ui.windows",
    "captation.obs.kinds_windows",
    "captation.obs.firewall_windows",
    # pystray and desktop-notifier pick their back ends at runtime
    "pystray._win32",
    "desktop_notifier.backends.winrt",
    *collect_submodules("keyring.backends"),
    *collect_submodules("winrt"),
]
datas = [
    *collect_data_files("captation"),
    *collect_data_files("jsonschema_specifications"),
    *collect_data_files("desktop_notifier"),
    *copy_metadata("keyring"),
]

a = Analysis(  # noqa: F821
    [str(ROOT / "packaging" / "captation_entry.py")],
    pathex=[str(ROOT / "src")],
    hiddenimports=hiddenimports,
    datas=datas,
    noarchive=False,
    # pystray (LGPL-3.0) stays a separate, replaceable module (docs/licensing.md): its .py
    # source files land in _internal\pystray\ instead of the PYZ archive.
    module_collection_mode={"pystray": "py"},
)
pyz = PYZ(a.pure)  # noqa: F821

console = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="captation",
    console=True,
    upx=False,
)
windowed = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="captationw",
    console=False,
    upx=False,
)
coll = COLLECT(  # noqa: F821
    console,
    windowed,
    a.binaries,
    a.datas,
    name="captation",
    upx=False,
)
