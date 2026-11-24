"""Every bundle ships LICENSE, DISCLAIMER.md and the third-party notices; pystray stays separate.

The build scripts run on their own OS only, so these checks read the scripts and the PyInstaller
specs. The Linux build itself is exercised by the release workflow.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

PACKAGING = Path(__file__).resolve().parents[2] / "packaging"
NOTICES = "THIRD_PARTY_NOTICES.txt"


def text(rel: str) -> str:
    return (PACKAGING / rel).read_text(encoding="utf-8")


def analysis_keywords(spec: str) -> dict[str, ast.expr]:
    tree = ast.parse(text(spec))
    (call,) = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "Analysis"
    ]
    return {kw.arg: kw.value for kw in call.keywords if kw.arg}


def code_lines(rel: str) -> list[str]:
    return [line.strip() for line in text(rel).splitlines() if not line.strip().startswith("#")]


@pytest.mark.parametrize(
    ("script", "gate"),
    [
        ("linux/build.sh", "tools/licence_gate.py --notices build/THIRD_PARTY_NOTICES.txt"),
        ("macos/build.sh", 'tools/licence_gate.py --notices "$notices"'),
        ("windows/build.ps1", "tools/licence_gate.py --notices $notices"),
    ],
)
def test_builds_run_the_licence_gate_and_write_notices(script: str, gate: str) -> None:
    lines = code_lines(script)
    gate_line = next(i for i, line in enumerate(lines) if gate in line)
    build_line = next(i for i, line in enumerate(lines) if "--group build pyinstaller" in line)
    assert gate_line < build_line  # a denied licence stops the build before PyInstaller runs


def test_linux_tarball_contents() -> None:
    copied = " ".join(line for line in code_lines("linux/build.sh") if line.startswith("cp "))
    for name in ("LICENSE", "DISCLAIMER.md", f"build/{NOTICES}", "config/config.example.yaml"):
        assert name in copied, name
    assert "dist/captation/" in copied
    assert "config.example.yaml" in text("linux/install.sh")


def test_macos_app_contents_and_dmg() -> None:
    kw = analysis_keywords("macos/captation.spec")
    assert ast.literal_eval(kw["module_collection_mode"]) == {"pystray": "py"}
    spec = text("macos/captation.spec")
    for name in ('"LICENSE"', '"DISCLAIMER.md"', "NOTICES, "):
        assert name in spec, name
    script = "\n".join(code_lines("macos/build.sh"))
    for name in ("LICENSE", "DISCLAIMER.md", NOTICES):
        assert name in script, name
    assert '"$app/Contents/Resources/$f"' in script  # checked inside the built app
    assert "*/pystray/__init__.py" in script
    assert 'cp LICENSE DISCLAIMER.md "$notices" "$staging/"' in script  # next to the app


def test_windows_folder_zip_and_installer() -> None:
    kw = analysis_keywords("windows/captation-windows.spec")
    assert ast.literal_eval(kw["module_collection_mode"]) == {"pystray": "py"}
    script = code_lines("windows/build.ps1")
    for copy in (
        "Copy-Item LICENSE dist\\captation\\",
        "Copy-Item DISCLAIMER.md dist\\captation\\",
    ):
        assert copy in script, copy
    assert "Copy-Item $notices dist\\captation\\" in script
    joined = "\n".join(script)
    assert "[switch]$RequireInstaller" in joined
    assert "throw 'Inno Setup (ISCC.exe) not found, but -RequireInstaller was given'" in joined
    assert "throw 'pystray is not collected as separate source files'" in joined
    iss = text("windows/etabli-captation.iss")
    assert 'Source: "{#SourceDir}\\*"' in iss and "LicenseFile={#SourceDir}\\LICENSE" in iss


def test_specs_are_valid_python() -> None:
    for spec in sorted(PACKAGING.rglob("*.spec")):
        compile(spec.read_text(encoding="utf-8"), str(spec), "exec")
    kw: dict[str, Any] = analysis_keywords("macos/captation.spec")
    assert "hiddenimports" in kw and "datas" in kw


@pytest.mark.parametrize("spec", ["macos/captation.spec", "windows/captation-windows.spec"])
def test_notifier_resources_are_bundled(spec: str) -> None:
    """desktop-notifier loads ``desktop_notifier.resources`` by name: PyInstaller cannot see it,
    and without it the notifier never starts in the bundle (found live, 2026-11-24)."""
    lines = code_lines(spec)
    assert any('"desktop_notifier.resources"' in line for line in lines)
    assert any('collect_data_files("desktop_notifier")' in line for line in lines)
