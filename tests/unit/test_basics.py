# Phone numbers in these tests are synthetic test values.
from __future__ import annotations

import json
import logging
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


@pytest.mark.req("R-30")
def test_obs_is_never_bundled_or_linked(repo_root: Path) -> None:
    """OBS is controlled only through obs-websocket, as a separate process (principles §4.1)."""
    pyproject = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))
    deps = " ".join(pyproject["project"]["dependencies"]).lower()
    assert "obs" not in deps.replace("obs-websocket", "")  # no OBS bindings or obsws libraries
    for script in (repo_root / "packaging").rglob("*"):
        if script.is_file():
            text = script.read_text(encoding="utf-8", errors="replace").lower()
            assert "obs-studio" not in text or "not bundled" in text or "never bundled" in text
            assert "libobs" not in text
    for py in (repo_root / "src").rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        assert "import obspython" not in text and 'ctypes.CDLL("libobs' not in text
