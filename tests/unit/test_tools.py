from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

import check_facts
import sync_specs
import traceability

FACTS_HEADER = (
    "| PF-ID | Status | OS | OS version | OBS / ws version | App + version | Fact | Evidence "
    "| Date |\n"
    "|---|---|---|---|---|---|---|---|---|\n"
)


def _fake_repo(tmp_path: Path, facts_rows: str, kinds_src: str) -> Path:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "platform-facts.md").write_text(
        FACTS_HEADER + facts_rows, encoding="utf-8"
    )
    pkg = tmp_path / "src" / "captation"
    (pkg / "obs").mkdir(parents=True)
    (pkg / "platform").mkdir()
    (pkg / "obs" / "kinds.py").write_text(kinds_src, encoding="utf-8")
    return tmp_path


def test_check_facts_passes_on_annotated_constants(tmp_path: Path) -> None:
    root = _fake_repo(
        tmp_path,
        "| PF-001 | verified | linux | | | | kind | x | 2026-11-23 |\n",
        '"""Doc."""\nKIND = "pulse_input_capture"  # PF-001\n',
    )
    assert check_facts.check(root) == ([], [])


def test_check_facts_fails_on_unknown_id(tmp_path: Path) -> None:
    root = _fake_repo(tmp_path, "", 'KIND = "x"  # PF-999\n')
    problems, _ = check_facts.check(root)
    assert any("PF-999 is not in" in p for p in problems)


def test_check_facts_fails_on_unverified_id(tmp_path: Path) -> None:
    root = _fake_repo(
        tmp_path, "| PF-002 | refuted | linux | | | | k | x | d |\n", 'K = "x"  # PF-002\n'
    )
    assert any("not verified" in p for p in check_facts.check(root)[0])


def test_check_facts_fails_on_unannotated_constant(tmp_path: Path) -> None:
    src = (
        '"""Doc."""\n'
        "import logging\nlog = logging.getLogger(__name__)\n"
        'KIND = "pipewire-audio-capture"\n'
        'log.info("log messages are allowed %s", 1)\n'
        'ENC = "utf-8"  # not-a-fact: text encoding\n'
        "def f(x: int) -> str:\n"
        '    """Docstring."""\n'
        '    raise ValueError("exception messages are allowed")\n'
    )
    root = _fake_repo(tmp_path, "", src)
    problems, _ = check_facts.check(root)
    assert len(problems) == 1 and "pipewire-audio-capture" in problems[0], problems


def test_sync_specs_detects_drift(tmp_path: Path, repo_root: Path) -> None:
    shutil.copytree(repo_root / "specs", tmp_path / "specs")
    shutil.copy(repo_root / "DISCLAIMER.md", tmp_path / "DISCLAIMER.md")
    (tmp_path / "src" / "captation" / "data").mkdir(parents=True)
    assert sync_specs.drift(tmp_path)  # missing files
    sync_specs.sync(tmp_path)
    assert sync_specs.drift(tmp_path) == []
    (tmp_path / "specs" / "rules.default.yaml").write_text("changed\n", encoding="utf-8")
    assert sync_specs.main(["--check", "--root", str(tmp_path)]) == 1


def test_repo_specs_are_in_sync(repo_root: Path) -> None:
    assert sync_specs.drift(repo_root) == []


def test_traceability_reports_uncovered(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "requirements.md").write_text(
        "| R-01 | a | s | unit |\n| R-02 | b | s | unit |\n", encoding="utf-8"
    )
    (tmp_path / ".cache").mkdir()
    (tmp_path / ".cache" / "req-results.json").write_text(
        '{"t::a": {"reqs": ["R-01"], "outcome": "passed"}}', encoding="utf-8"
    )
    rows, uncovered, unknown = traceability.matrix(tmp_path)
    assert rows[0] == ("R-01", ["t::a"], "passed")
    assert uncovered == ["R-02"] and unknown == []
    assert traceability.main(["--root", str(tmp_path), "--strict"]) == 1


@pytest.mark.req("R-33")
def test_licence_verdicts() -> None:
    import licence_gate

    assert licence_gate.verdict("MIT") == "ok"
    assert licence_gate.verdict("GPL-3.0-only").startswith("DENY")
    assert licence_gate.verdict("GNU Affero General Public License v3").startswith("DENY")
    assert licence_gate.verdict("AGPL-3.0-or-later").startswith("DENY")
    assert licence_gate.verdict("GNU Lesser General Public License v3 (LGPLv3)") == "ok (LGPL)"
    assert licence_gate.verdict("LGPL-3.0-only") == "ok (LGPL)"
    assert licence_gate.verdict("").startswith("DENY")
    assert licence_gate.verdict("BSD-3-Clause") == "ok"


def test_check_facts_pending_rules(tmp_path: Path) -> None:
    rows = "| PF-101 | pending | macos | | | | k | x | d |\n"
    root = _fake_repo(tmp_path, rows, 'K = "sck_audio_capture"  # PF-101\n')
    assert check_facts.check(root, os_name="linux") == ([], ["PF-101 (needs macos)"])
    problems, _ = check_facts.check(root, os_name="macos")
    assert any("pending for macos" in p for p in problems)
    problems, _ = check_facts.check(root, os_name="linux", release=True)
    assert problems
