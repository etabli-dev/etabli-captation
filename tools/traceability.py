"""Requirement traceability matrix.

Reads the requirement IDs from ``docs/requirements.md`` and the test results written by the
pytest plugin in ``tests/conftest.py`` (``.cache/req-results.json``), then prints
R-ID → tests → last result and lists uncovered requirements.

Usage: ``python tools/traceability.py [--strict]`` (``--strict`` exits 1 if any requirement
has no test or a failing test).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REQ_ROW = re.compile(r"^\|\s*(R-\d{2,3})\s*\|", re.MULTILINE)
RESULTS = Path(".cache") / "req-results.json"


def requirement_ids(root: Path) -> list[str]:
    text = (root / "docs" / "requirements.md").read_text(encoding="utf-8")
    return REQ_ROW.findall(text)


def load_results(root: Path) -> dict[str, dict[str, object]]:
    path = root / RESULTS
    if not path.exists():
        return {}
    data: dict[str, dict[str, object]] = json.loads(path.read_text(encoding="utf-8"))
    return data


def matrix(root: Path) -> tuple[list[tuple[str, list[str], str]], list[str], list[str]]:
    reqs = requirement_ids(root)
    results = load_results(root)
    by_req: dict[str, list[tuple[str, str]]] = {r: [] for r in reqs}
    unknown: set[str] = set()
    for nodeid, info in results.items():
        for req in info.get("reqs", []):  # type: ignore[union-attr]
            if req in by_req:
                by_req[req].append((nodeid, str(info.get("outcome", "not run"))))
            else:
                unknown.add(str(req))
    rows = []
    for req in reqs:
        tests = by_req[req]
        outcomes = {o for _, o in tests}
        if not tests:
            result = "UNCOVERED"
        elif "failed" in outcomes:
            result = "failed"
        elif outcomes <= {"skipped"}:
            result = "skipped"
        elif "passed" in outcomes:
            result = "passed"
        else:
            result = ",".join(sorted(outcomes))
        rows.append((req, [n for n, _ in tests], result))
    uncovered = [r for r, tests, _ in rows if not tests]
    return rows, uncovered, sorted(unknown)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--verbose", "-v", action="store_true", help="list test node IDs")
    args = parser.parse_args(argv)
    rows, uncovered, unknown = matrix(args.root)
    print(f"{'R-ID':<6} {'tests':>5}  result")
    for req, tests, result in rows:
        print(f"{req:<6} {len(tests):>5}  {result}")
        if args.verbose:
            for t in tests:
                print(f"         {t}")
    print(f"\n{len(rows) - len(uncovered)}/{len(rows)} requirements covered")
    if uncovered:
        print("uncovered: " + ", ".join(uncovered))
    if unknown:
        print("markers for unknown requirement IDs: " + ", ".join(unknown))
    failed = [r for r, _, res in rows if res == "failed"]
    if args.strict and (uncovered or failed or unknown):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
