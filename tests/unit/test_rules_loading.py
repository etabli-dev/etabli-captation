from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from captation.config import ConfigError
from captation.rules import compile_rules, merge_rules
from captation.rulesfile import BUILTIN_DEFAULT, load_rules


@pytest.mark.req("R-51")
def test_default_rules_validate_and_compile() -> None:
    rules = load_rules(BUILTIN_DEFAULT)
    ids = [a.id for a in rules.apps]
    assert ids[:3] == ["zoom", "teams", "webex"]
    assert all(p.pattern for a in rules.apps for p in a.process)
    browser = rules.app("browser")
    assert browser is not None and len(browser.title_rules) >= 13
    assert [m.reason for m in rules.modes] == [
        "meeting_app",
        "camera_active",
        "browser_meeting",
        "default",
    ]
    assert any(p.search("obs") for p in rules.ignore_process)
    assert any(p.search("captation-probe") for p in rules.ignore_process)


def _write(path: Path, doc: dict[str, object]) -> str:
    path.write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")
    return str(path)


def test_user_rules_merge_by_id(tmp_path: Path) -> None:
    user = {
        "version": 1,
        "apps": [
            {"id": "signal", "category": "call", "match": {"process": ["(?i)^signal-beta$"]}},
            {"id": "fakecall", "category": "messenger", "match": {"process": ["^fakecall-e2e$"]}},
        ],
        "modes": [{"when": {}, "mode": "audio", "reason": "default"}],
        "ignore": {"process": ["^myrecorder$"]},
    }
    default_ids = [a.id for a in load_rules(BUILTIN_DEFAULT).apps]
    rules = load_rules(BUILTIN_DEFAULT, _write(tmp_path / "user.yaml", user))
    ids = [a.id for a in rules.apps]
    assert ids.index("signal") == default_ids.index("signal")  # replaced in place, order kept
    assert ids[-1] == "fakecall"  # new ids are appended
    signal = rules.app("signal")
    assert signal is not None and signal.category == "call"
    assert [p.pattern for p in signal.process] == ["(?i)^signal-beta$"]
    assert [m.reason for m in rules.modes] == ["default"]  # user modes replace defaults
    assert any(p.pattern == "^myrecorder$" for p in rules.ignore_process)
    assert any(p.search("obs64.exe") for p in rules.ignore_process)  # defaults kept


def test_invalid_user_rules_are_rejected(tmp_path: Path) -> None:
    bad = {
        "version": 1,
        "apps": [{"id": "X", "category": "call", "match": {}}],
        "modes": [],
        "ignore": {},
    }
    with pytest.raises(ConfigError, match="invalid rules"):
        load_rules(BUILTIN_DEFAULT, _write(tmp_path / "bad.yaml", bad))


def test_bad_regex_is_rejected(tmp_path: Path) -> None:
    bad = {
        "version": 1,
        "apps": [{"id": "x", "category": "call", "match": {"process": ["(unclosed"]}}],
        "modes": [{"when": {}, "mode": "audio", "reason": "default"}],
        "ignore": {},
    }
    with pytest.raises(ConfigError):
        load_rules(BUILTIN_DEFAULT, _write(tmp_path / "bad.yaml", bad))


def test_compile_rules_rejects_a_bad_regex_directly() -> None:
    raw = {
        "apps": [{"id": "x", "category": "call", "match": {"process": ["(unclosed"]}}],
        "modes": [{"when": {}, "mode": "audio", "reason": "default"}],
    }
    with pytest.raises(ConfigError, match=r"apps\[0\] \(x\)\.match\.process\[0\]"):
        compile_rules(raw)


def test_merge_without_ignore_lists_has_empty_ignore() -> None:
    doc = {
        "version": 1,
        "apps": [{"id": "x", "category": "call", "match": {"process": ["^x$"]}}],
        "modes": [{"when": {}, "mode": "audio", "reason": "default"}],
        "ignore": {},
    }
    assert merge_rules(doc, doc)["ignore"] == {}
