# Phone numbers in these tests are synthetic test values.
from __future__ import annotations

import ast
import json
import logging
import re
import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from captation import __version__
from captation.cli import app
from captation.logs import JsonFormatter, RedactionFilter


def test_version_matches_pyproject(repo_root: Path) -> None:
    pyproject = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["project"]["version"] == __version__


def test_cli_lists_all_commands() -> None:
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    for cmd in (
        "run",
        "arm",
        "disarm",
        "status",
        "stop",
        "doctor",
        "import",
        "install-autostart",
        "uninstall-autostart",
        "version",
        "obs",
    ):
        assert cmd in result.output
    obs = CliRunner().invoke(app, ["obs", "--help"])
    for cmd in ("bootstrap", "facts", "set-password", "firewall"):
        assert cmd in obs.output


def test_cli_version() -> None:
    result = CliRunner().invoke(app, ["version"])
    assert result.exit_code == 0 and result.output.strip() == __version__


def _format(record: logging.LogRecord, filt: RedactionFilter) -> dict[str, object]:
    assert filt.filter(record)
    doc: dict[str, object] = json.loads(JsonFormatter().format(record))
    return doc


@pytest.mark.req("R-25")
def test_redaction_drops_sensitive_fields_and_shortens_paths(tmp_path: Path) -> None:
    root = tmp_path / "CaptationData"
    filt = RedactionFilter(root)
    record = logging.makeLogRecord(
        {
            "name": "captation.test",
            "levelname": "INFO",
            "msg": "wrote %s",
            "args": (str(root / "PRJ-2026-001" / "S-20261123-141502-a3f9" / "seg01_mix.flac"),),
            "window_title": "Call with Jane Doe",
            "file_name": "+49 170 1234567.mp3",
            "app_id": "zoom",
        }
    )
    doc = _format(record, filt)
    text = json.dumps(doc)
    assert "Jane" not in text and "1234567" not in text and str(root) not in text
    assert doc["msg"] == "wrote <data_root>/S-20261123-141502-a3f9"
    assert doc["window_title"] == "[redacted]" and doc["app_id"] == "zoom"


# OBS files that a bundle must never contain: libraries, plugins, executables, app bundles.
OBS_ARTEFACT = re.compile(
    r"libobs|obs-plugins|obs-studio[\\/]|obs-frontend-api|obs-websocket\.(so|dll|plugin)"
    r"|\bobs(32|64)?\.exe\b|\bOBS\.app\b|\.plugin\b",
    re.IGNORECASE,
)
# Python bindings or wrappers of OBS (the in-house obs-websocket client is used instead).
OBS_MODULES = {"obspython", "obs", "obsws_python", "obswebsocket", "simpleobsws"}
NATIVE_LOADERS = {"CDLL", "WinDLL", "LoadLibrary", "find_library", "dlopen"}


def _packaging_files(repo_root: Path, *patterns: str) -> list[Path]:
    return sorted(p for pattern in patterns for p in (repo_root / "packaging").rglob(pattern))


@pytest.mark.req("R-30")
def test_obs_is_never_bundled_or_linked(repo_root: Path) -> None:
    """OBS is controlled only through obs-websocket, as a separate process (principles §4.1)."""
    # the pattern finds what a bundle must not contain
    for sample in (
        "--add-binary /usr/lib/x86_64-linux-gnu/libobs.so.30:.",
        r"C:\Program Files\obs-studio\obs-plugins\64bit\obs-websocket.dll",
        "/Applications/OBS.app/Contents/PlugIns/obs-websocket.plugin",
        "bin/64bit/obs64.exe",
    ):
        assert OBS_ARTEFACT.search(sample), sample

    # 1. no OBS binding or wrapper anywhere in the locked dependency set
    lock = tomllib.loads((repo_root / "uv.lock").read_text(encoding="utf-8"))
    names = {str(p["name"]).lower() for p in lock["package"]}
    assert not {n for n in names if n.startswith(("obs", "simpleobs"))}

    # 2. sources: no OBS module import, no native library that could be OBS
    for py in sorted((repo_root / "src" / "captation").rglob("*.py")):
        for node in ast.walk(ast.parse(py.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                tops = {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                tops = {node.module.split(".")[0]}
            else:
                tops = set()
            assert not tops & OBS_MODULES, (py, tops)
            if isinstance(node, ast.Call):
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                if name in NATIVE_LOADERS:
                    args = [a.value for a in node.args if isinstance(a, ast.Constant)]
                    assert not any("obs" in str(a).lower() for a in args), (py, node.lineno)

    # 3. packaging: the PyInstaller specs and build scripts collect nothing of OBS
    specs = _packaging_files(repo_root, "*.spec")
    assert len(specs) >= 2
    for spec in specs:
        for node in ast.walk(ast.parse(spec.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert not OBS_ARTEFACT.search(node.value), (spec, node.value)
    scripts = _packaging_files(repo_root, "*.sh", "*.ps1")
    assert len(scripts) >= 4
    for script in scripts:
        for lineno, line in enumerate(script.read_text(encoding="utf-8").splitlines(), 1):
            code = line.strip()
            if code.startswith(("#", "echo ", "Write-Host", "Write-Warning")):
                continue
            assert not OBS_ARTEFACT.search(code), (script, lineno, code)
    (iss,) = _packaging_files(repo_root, "*.iss")
    files = iss.read_text(encoding="ascii").split("[Files]", 1)[1].split("\n[", 1)[0]
    sources = re.findall(r'Source:\s*"([^"]+)"', files)
    assert sources and all(s.startswith("{#SourceDir}") for s in sources), sources
