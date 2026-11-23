from __future__ import annotations

import json
import re
import shutil
import sys
from importlib import metadata
from pathlib import Path
from typing import Any

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


def _trace_repo(tmp_path: Path, reqs: list[str], results: dict[str, Any]) -> Path:
    (tmp_path / "docs").mkdir(exist_ok=True)
    (tmp_path / "docs" / "requirements.md").write_text(
        "".join(f"| {r} | text | s | unit |\n" for r in reqs), encoding="utf-8"
    )
    (tmp_path / ".cache").mkdir(exist_ok=True)
    (tmp_path / ".cache" / "req-results.json").write_text(json.dumps(results), encoding="utf-8")
    return tmp_path


def test_traceability_reports_uncovered(tmp_path: Path) -> None:
    root = _trace_repo(
        tmp_path, ["R-01", "R-02"], {"t::a": {"reqs": ["R-01"], "outcome": "passed"}}
    )
    m = traceability.matrix(root)
    assert [(r.req, r.tests, r.status) for r in m.rows] == [
        ("R-01", [("t::a", "passed")], "passed"),
        ("R-02", [], "UNCOVERED"),
    ]
    assert m.with_status("UNCOVERED") == ["R-02"] and m.unknown == []
    assert traceability.main(["--root", str(root), "--strict"]) == 1


def test_traceability_counts_only_passed_tests_as_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(traceability, "EXTERNAL_EVIDENCE", {})
    results = {
        "t::p": {"reqs": ["R-01"], "outcome": "passed"},
        "t::s": {"reqs": ["R-01", "R-02"], "outcome": "skipped"},
        "t::n": {"reqs": ["R-03"], "outcome": "not run"},
        "t::n2": {"reqs": ["R-02"], "outcome": "not run"},
    }
    root = _trace_repo(tmp_path, ["R-01", "R-02", "R-03"], results)
    m = traceability.matrix(root)
    status = {r.req: r.status for r in m.rows}
    assert status == {"R-01": "passed", "R-02": "skipped only", "R-03": "not run"}
    counts = {r.req: dict(r.counts) for r in m.rows}
    assert counts["R-02"] == {"skipped": 1, "not run": 1}
    assert m.missing_evidence() == ["R-02", "R-03"] and not m.strict_ok()
    assert traceability.main(["--root", str(root)]) == 0  # report only
    out = capsys.readouterr().out
    assert "1/3 requirements with a passed test" in out
    assert "skipped only (no passed test): R-02" in out and "not run (no passed test): R-03" in out
    assert traceability.main(["--root", str(root), "--strict"]) == 1


def test_traceability_strict_rules(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reqs = ["R-01", "R-02"]
    monkeypatch.setattr(traceability, "EXTERNAL_EVIDENCE", {"R-02": "manual: real desktop"})
    passed = {"t::a": {"reqs": ["R-01"], "outcome": "passed"}}
    root = _trace_repo(tmp_path, reqs, passed)
    m = traceability.matrix(root)
    assert m.missing_evidence() == [] and m.strict_ok()  # R-02 is listed as external
    assert "R-02 [UNCOVERED]: manual: real desktop" in traceability.render(m)
    assert traceability.main(["--root", str(root), "--strict"]) == 0
    # a failed test, an unknown marker or a stale EXTERNAL_EVIDENCE entry each fail --strict
    for results, external in (
        ({**passed, "t::f": {"reqs": ["R-01"], "outcome": "failed"}}, {"R-02": "x"}),
        ({**passed, "t::u": {"reqs": ["R-99"], "outcome": "passed"}}, {"R-02": "x"}),
        (passed, {"R-02": "x", "R-77": "gone"}),
    ):
        monkeypatch.setattr(traceability, "EXTERNAL_EVIDENCE", external)
        root = _trace_repo(tmp_path, reqs, results)
        assert not traceability.matrix(root).strict_ok(), (results, external)
        assert traceability.main(["--root", str(root), "--strict"]) == 1
    (root / ".cache" / "req-results.json").unlink()
    assert traceability.main(["--root", str(root), "--strict"]) == 1  # no results at all


def test_traceability_external_evidence_matches_requirements(repo_root: Path) -> None:
    """Every requirement whose test level in docs/requirements.md is not (only) unit is listed."""
    text = (repo_root / "docs" / "requirements.md").read_text(encoding="utf-8")
    levels = {
        m.group(1): m.group(2).strip().lower()
        for m in re.finditer(r"^\|\s*(R-\d{2,3})\s*\|.*\|([^|]*)\|\s*$", text, re.MULTILINE)
    }
    assert len(levels) == len(traceability.requirement_ids(repo_root))
    needs_listing = {r for r, level in levels.items() if "unit" not in level or "manual" in level}
    assert needs_listing <= set(traceability.EXTERNAL_EVIDENCE), sorted(
        needs_listing - set(traceability.EXTERNAL_EVIDENCE)
    )
    assert set(traceability.EXTERNAL_EVIDENCE) <= set(levels)
    assert all(reason.strip() for reason in traceability.EXTERNAL_EVIDENCE.values())


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


def test_check_facts_release_gate_per_os(tmp_path: Path) -> None:
    rows = (
        "| PF-001 | verified | linux | | | | k | x | d |\n"
        "| PF-101 | pending | macos | | | | k | x | d |\n"
        "| PF-201 | pending | windows | | | | k | x | d |\n"
    )
    src = 'A = "a"  # PF-001\nB = "b"  # PF-101\nC = "c"  # PF-201\n'
    root = _fake_repo(tmp_path, rows, src)
    # a Linux release ships with macOS/Windows facts still pending (ADR-016)
    assert check_facts.check(root, os_name="linux", release=True) == (
        [],
        ["PF-101 (needs macos)", "PF-201 (needs windows)"],
    )
    problems, pending = check_facts.check(root, os_name="macos", release=True)
    assert len(problems) == 1 and "PF-101 is pending for macos" in problems[0]
    assert pending == ["PF-201 (needs windows)"]
    problems, pending = check_facts.check(root, os_name="windows", release=True)
    assert len(problems) == 1 and "PF-201" in problems[0] and pending == ["PF-101 (needs macos)"]
    # plain --release: every OS
    problems, pending = check_facts.check(root, release=True)
    assert len(problems) == 2 and pending == []
    assert check_facts.main(["--root", str(root), "--release", "--os", "linux"]) == 0
    assert check_facts.main(["--root", str(root), "--release", "--os", "macos"]) == 1
    assert check_facts.main(["--root", str(root), "--release"]) == 1


def test_licence_gate_reads_markers_and_writes_notices(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import licence_gate

    lines = [
        "# comment",
        "pydantic==2.14.4",
        "    # via etabli-captation",
        "pystray==0.19.5 ; sys_platform == 'nonexistent-os'",
        "websockets==17.4 ; python_version >= '3'",
        "missing-package==1.0",
    ]
    reqs = licence_gate.parse_requirements(lines)
    assert [r.name for r in reqs] == ["missing-package", "pydantic", "pystray", "websockets"]
    assert reqs[2].marker == "sys_platform == 'nonexistent-os'"
    assert licence_gate.applies_here("") and licence_gate.applies_here("python_version >= '3'")
    assert not licence_gate.applies_here(reqs[2].marker)
    assert licence_gate.applies_here("not a marker ((")  # unreadable: keep the package

    monkeypatch.setattr(licence_gate, "export_lines", lambda: lines)
    notices = tmp_path / "out" / "THIRD_PARTY_NOTICES.txt"
    assert licence_gate.main(["--notices", str(notices)]) == 0
    out = capsys.readouterr().out
    assert "not for this platform" in out and "not installed on this platform" in out
    text = notices.read_text(encoding="utf-8")
    for name in ("pydantic", "websockets"):
        assert f"{name} {metadata.version(name)}" in text
    assert "pystray" not in text
    assert "--- licenses/LICENSE ---" in text  # the licence text itself, from the dist-info
    assert "PyInstaller" in text and "Python runtime" in text and "OBS Studio is not" in text


def test_licence_gate_notes_replaceable_lgpl_modules() -> None:
    import licence_gate

    class FakeDist(metadata.Distribution):
        def read_text(self, filename: str) -> str | None:
            files = {
                "METADATA": "Metadata-Version: 2.1\nName: pystray\nVersion: 0.19.5\n"
                "License: LGPLv3\nClassifier: License :: OSI Approved :: "
                "GNU Lesser General Public License v3 (LGPLv3)\n"
                "Project-URL: Source, https://github.com/moses-palmer/pystray\n",
                "top_level.txt": "pystray\n",
            }
            return files.get(filename)

        def locate_file(self, path: Any) -> Any:
            return Path(str(path))

    dist = FakeDist()
    assert licence_gate.verdict(licence_gate.declared_licence(dist)) == "ok (LGPL)"
    text = licence_gate.notices([dist])
    assert "pystray 0.19.5" in text and "folder `pystray/`" in text
    assert "can be replaced by a\nmodified version of the library" in text
    assert "https://github.com/moses-palmer/pystray" in text
    assert "ships no licence file" in text
