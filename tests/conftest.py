"""Shared fixtures and the requirement-traceability recorder (tools/traceability.py)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
_RESULTS: dict[str, dict[str, Any]] = {}


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        reqs: list[str] = []
        for mark in item.iter_markers(name="req"):
            reqs.extend(str(a) for a in mark.args)
        if reqs:
            _RESULTS[item.nodeid] = {"reqs": sorted(set(reqs)), "outcome": "not run"}


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    entry = _RESULTS.get(report.nodeid)
    if entry is None:
        return
    if report.when == "call" or (report.when == "setup" and report.outcome != "passed"):
        entry["outcome"] = report.outcome


def pytest_sessionfinish(session: pytest.Session) -> None:
    if not _RESULTS or getattr(session.config.option, "collectonly", False):
        return
    out = ROOT / ".cache" / "req-results.json"
    out.parent.mkdir(exist_ok=True)
    previous: dict[str, Any] = {}
    if out.exists():
        try:
            previous = json.loads(out.read_text(encoding="utf-8"))
        except ValueError:
            previous = {}
    for nodeid, entry in _RESULTS.items():
        # Deselected tests stay "not run": never overwrite an earlier real result with that.
        if entry["outcome"] != "not run" or nodeid not in previous:
            previous[nodeid] = entry
    known = {item.nodeid for item in session.items} | set(_RESULTS)
    for nodeid in list(previous):
        path, _, name = nodeid.partition("::")
        test_file = ROOT / path
        func = name.split("[")[0]
        if not test_file.exists() or f"def {func}(" not in test_file.read_text(encoding="utf-8"):
            del previous[nodeid]  # the test was renamed or removed
    del known
    out.write_text(json.dumps(previous, indent=1, sort_keys=True), encoding="utf-8")


def pytest_runtest_setup(item: pytest.Item) -> None:
    here = (
        "linux"
        if sys.platform.startswith("linux")
        else "macos"
        if sys.platform == "darwin"
        else "windows"
    )
    for name in ("linux", "macos", "windows"):
        if item.get_closest_marker(name) is not None and name != here:
            pytest.skip(f"{name}-only test")


@pytest.fixture
def repo_root() -> Path:
    return ROOT


@pytest.fixture(autouse=True)
def _terms_accepted_for_tests(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unit and integration tests run as if DISCLAIMER.md had been accepted.

    The gate itself is tested in tests/unit/test_terms.py (marker ``terms_gate`` keeps the real
    check). The end-to-end suite writes a test acceptance into its own state directory instead.
    """
    if request.node.get_closest_marker("terms_gate") or request.node.get_closest_marker("e2e"):
        return
    from captation import terms

    monkeypatch.setattr(terms, "accepted", lambda path=None: True)
