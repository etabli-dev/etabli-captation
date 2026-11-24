from __future__ import annotations

import copy
import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest
import yaml

from captation.config import ConfigError, data_text, load_config, parse_config, schema_errors


def example(repo_root: Path) -> dict[str, Any]:
    raw: dict[str, Any] = yaml.safe_load(
        (repo_root / "config" / "config.example.yaml").read_text(encoding="utf-8")
    )
    return raw


@pytest.mark.req("R-51")
def test_example_and_builtin_default_are_valid(repo_root: Path) -> None:
    cfg = parse_config(example(repo_root))
    assert cfg.consent.require_armed is True
    assert cfg.import_.require_consent_ref is True
    default = parse_config(yaml.safe_load(data_text("config.default.yaml")))
    assert default.project_id is None
    assert default.digest().startswith("sha256:") and len(default.digest()) == 71


@pytest.mark.req("R-51")
def test_repo_example_config_is_valid(repo_root: Path) -> None:
    raw = yaml.safe_load((repo_root / "config" / "config.example.yaml").read_text(encoding="utf-8"))
    assert schema_errors(raw, "config.schema.json") == []
    parse_config(raw)


@pytest.mark.req("R-43")
@pytest.mark.parametrize("name", ["session.example.json", "session.import.example.json"])
def test_session_examples_validate(repo_root: Path, name: str) -> None:
    doc = json.loads((repo_root / "specs" / "examples" / name).read_text(encoding="utf-8"))
    assert schema_errors(doc, "session.schema.json") == []


def _mutate(base: dict[str, Any], path: str, value: Any) -> dict[str, Any]:
    doc = copy.deepcopy(base)
    keys = path.split(".")
    node = doc
    for k in keys[:-1]:
        node = node[k]
    node[keys[-1]] = value
    return doc


NEGATIVE_CASES = [
    pytest.param("unknown_key", ("surprise", True), "Additional properties", id="unknown-key"),
    pytest.param(
        "require_armed",
        ("consent.require_armed", False),
        "True was expected",
        id="require-armed-false",
    ),
    pytest.param(
        "dup_tracks", ("recording.audio.tracks.local", 2), "unique", id="duplicate-tracks"
    ),
    pytest.param(
        "host", ("obs.websocket.host", "0.0.0.0"), "is not one of", id="non-loopback-host"
    ),
    pytest.param("project", ("project_id", "Projekt 1"), "does not match", id="bad-project-id"),
    pytest.param("tray", ("ui", {"tray": False}), "Additional properties", id="ui-tray-present"),
    pytest.param(
        "notif",
        ("ui", {"notifications": False}),
        "Additional properties",
        id="ui-notifications-present",
    ),
    pytest.param(
        "preroll", ("recording.preroll_seconds", 5), "Additional properties", id="preroll-present"
    ),
    pytest.param(
        "import",
        ("import.require_consent_ref", False),
        "True was expected",
        id="import-consent-ref-false",
    ),
    pytest.param("regex", ("consent.consent_ref_pattern", "(["), "regex", id="bad-consent-pattern"),
]


@pytest.mark.req("R-14", "R-21", "R-22", "R-32", "R-51", "R-54")
@pytest.mark.parametrize(("name", "change", "expected"), NEGATIVE_CASES)
def test_negative_cases_are_rejected(
    repo_root: Path, name: str, change: tuple[str, Any], expected: str
) -> None:
    doc = _mutate(example(repo_root), *change)
    with pytest.raises(ConfigError) as info:
        parse_config(doc, source="test.yaml")
    message = str(info.value)
    assert expected in message, message
    print(f"[{name}] {message}")


@pytest.mark.req("R-51")
def test_load_config_creates_private_data_root(tmp_path: Path, repo_root: Path) -> None:
    doc = example(repo_root)
    doc["data_root"] = str(tmp_path / "data" / "root")
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(yaml.safe_dump(doc), encoding="utf-8")
    cfg = load_config(cfg_file)
    assert cfg.data_root_path.is_dir()
    if os.name == "posix":  # Windows: ACLs, not mode bits
        assert stat.S_IMODE(cfg.data_root_path.stat().st_mode) == 0o700


def test_missing_config_file_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.yaml")


def test_digest_changes_with_content(repo_root: Path) -> None:
    a = parse_config(example(repo_root))
    b = parse_config(_mutate(example(repo_root), "consent.countdown_seconds", 7))
    assert a.digest() != b.digest()


@pytest.mark.req("R-16")
def test_unlimited_study_mode_needs_the_reminder(repo_root: Path) -> None:
    """ADR-021: auto_disarm null is allowed only together with a reminder interval."""
    raw = example(repo_root)
    assert raw["consent"]["auto_disarm_after_minutes"] is None  # the default since ADR-021
    assert raw["consent"]["armed_reminder_minutes"] == 240
    assert schema_errors(raw, "config.schema.json") == []
    parse_config(raw)
    bad = copy.deepcopy(raw)
    bad["consent"]["armed_reminder_minutes"] = None
    assert schema_errors(bad, "config.schema.json")
    with pytest.raises(ConfigError):
        parse_config(bad)
    limited = copy.deepcopy(bad)
    limited["consent"]["auto_disarm_after_minutes"] = 240  # a time limit needs no reminder
    assert schema_errors(limited, "config.schema.json") == []
    parse_config(limited)
