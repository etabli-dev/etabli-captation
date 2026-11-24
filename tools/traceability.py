"""Requirement traceability matrix.

Reads the requirement IDs from ``docs/requirements.md`` and the test results written by the
pytest plugin in ``tests/conftest.py`` (``.cache/req-results.json``), then prints, per
requirement, how many of its tests passed, failed, were skipped or did not run.

Only a passed test counts as evidence. A requirement whose tests were all skipped or not run is
reported as such, not as covered.

Some requirements cannot be (fully) shown by the unit suite: their evidence is an end-to-end
run, an integration test against a real OBS, or a manual check. They are listed in
``EXTERNAL_EVIDENCE`` with the reason, and the report names them explicitly.

Usage: ``python tools/traceability.py [--strict] [-v]``

``--strict`` exits 1 if a test failed, if a marker names an unknown requirement, if
``EXTERNAL_EVIDENCE`` names an unknown requirement, or if a requirement that is not in
``EXTERNAL_EVIDENCE`` has no passed test.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REQ_ROW = re.compile(r"^\|\s*(R-\d{2,3})\s*\|", re.MULTILINE)
RESULTS = Path(".cache") / "req-results.json"
OUTCOMES = ("passed", "failed", "skipped", "not run")

# Requirements whose evidence lies (partly or wholly) outside the unit suite, with the reason.
# They are reported separately; ``--strict`` does not demand a passed unit test for them.
EXTERNAL_EVIDENCE: dict[str, str] = {
    "R-62": "manual: transcript quality with the real whisper.cpp/llama.cpp models (synthetic "
    "two-voice call with known agreements and to-dos; the owner's test calls); unit tests use "
    "fake tools",
    "R-18": "manual: the indicator is visible in the real tray (unit tests cover the schema "
    "and the stub tray only)",
    "R-19": "manual: the start reminder appears on the real desktop (unit tests use a stub "
    "notification server)",
    "R-26": "e2e (E10): check of the running agent's sockets; the unit guard only checks imports "
    "and the loopback-only websocket host",
    "R-27": "integration and e2e (E10): OBS launched with its updater disabled and the crash-"
    "upload setting read from a real OBS",
    "R-30": "packaging check: the built bundles contain no OBS binaries (unit test checks the "
    "build scripts, specs and sources)",
    "R-31": "integration (diff): bootstrap against a real OBS, config diff before/after",
    "R-33": "CI step: tools/licence_gate.py on the runtime closure of each OS (unit test "
    "covers the verdict logic)",
    "R-34": "integration and grep: the password never appears in process arguments, logs or "
    "reports of a real run",
    "R-36": "integration and e2e (E5): OBS releases the microphone while idle",
    "R-49": "integration and manual: autostart at a real login on each OS (unit tests use a "
    "temporary home and a fake runner)",
}


@dataclass
class Row:
    req: str
    tests: list[tuple[str, str]] = field(default_factory=list)  # (node ID, outcome)

    @property
    def counts(self) -> Counter[str]:
        return Counter(outcome for _, outcome in self.tests)

    @property
    def status(self) -> str:
        counts = self.counts
        if not self.tests:
            return "UNCOVERED"
        if counts["failed"]:
            return "failed"
        if counts["passed"]:
            return "passed"
        if counts["skipped"]:
            return "skipped only"
        return "not run"


@dataclass
class Matrix:
    rows: list[Row]
    unknown: list[str]  # requirement IDs in test markers that docs/requirements.md lacks
    stale_external: list[str]  # EXTERNAL_EVIDENCE entries that docs/requirements.md lacks

    def with_status(self, *statuses: str) -> list[str]:
        return [r.req for r in self.rows if r.status in statuses]

    def missing_evidence(self) -> list[str]:
        """Requirements without a passed test that are not in EXTERNAL_EVIDENCE."""
        return [
            r.req
            for r in self.rows
            if r.status not in ("passed", "failed") and r.req not in EXTERNAL_EVIDENCE
        ]

    def strict_ok(self) -> bool:
        problems = (
            self.with_status("failed"),
            self.missing_evidence(),
            self.unknown,
            self.stale_external,
        )
        return not any(problems)


def requirement_ids(root: Path) -> list[str]:
    text = (root / "docs" / "requirements.md").read_text(encoding="utf-8")
    return REQ_ROW.findall(text)


def load_results(root: Path) -> dict[str, dict[str, object]]:
    path = root / RESULTS
    if not path.exists():
        return {}
    data: dict[str, dict[str, object]] = json.loads(path.read_text(encoding="utf-8"))
    return data


def _outcome(info: dict[str, object]) -> str:
    outcome = str(info.get("outcome", "not run"))
    return outcome if outcome in OUTCOMES else "not run"


def matrix(root: Path) -> Matrix:
    reqs = requirement_ids(root)
    rows = {r: Row(r) for r in reqs}
    unknown: set[str] = set()
    for nodeid, info in sorted(load_results(root).items()):
        markers = info.get("reqs", [])
        for req in markers if isinstance(markers, list) else []:
            if req in rows:
                rows[req].tests.append((nodeid, _outcome(info)))
            else:
                unknown.add(str(req))
    stale = sorted(r for r in EXTERNAL_EVIDENCE if r not in rows)
    return Matrix([rows[r] for r in reqs], sorted(unknown), stale)


def render(m: Matrix, *, verbose: bool = False) -> str:
    lines = [f"{'R-ID':<6} {'passed':>6} {'failed':>6} {'skipped':>7} {'not run':>7}  status"]
    for row in m.rows:
        c = row.counts
        status = row.status + (" + external" if row.req in EXTERNAL_EVIDENCE else "")
        lines.append(
            f"{row.req:<6} {c['passed']:>6} {c['failed']:>6} {c['skipped']:>7} "
            f"{c['not run']:>7}  {status}"
        )
        if verbose:
            lines += [f"         {outcome:<8} {nodeid}" for nodeid, outcome in row.tests]
    total = len(m.rows)
    lines.append("")
    lines.append(f"{len(m.with_status('passed'))}/{total} requirements with a passed test")
    for label, statuses in (
        ("failed", ("failed",)),
        ("skipped only (no passed test)", ("skipped only",)),
        ("not run (no passed test)", ("not run",)),
        ("uncovered (no test)", ("UNCOVERED",)),
    ):
        ids = m.with_status(*statuses)
        if ids:
            lines.append(f"{label}: " + ", ".join(ids))
    present = [r.req for r in m.rows if r.req in EXTERNAL_EVIDENCE]
    if present:
        lines.append("")
        lines.append("evidence outside the unit suite (EXTERNAL_EVIDENCE):")
        status = {r.req: r.status for r in m.rows}
        for req in present:
            lines.append(f"  {req} [{status[req]}]: {EXTERNAL_EVIDENCE[req]}")
    missing = m.missing_evidence()
    if missing:
        lines.append("")
        lines.append("no passed test and not in EXTERNAL_EVIDENCE: " + ", ".join(missing))
    if m.unknown:
        lines.append("markers for unknown requirement IDs: " + ", ".join(m.unknown))
    if m.stale_external:
        stale = ", ".join(m.stale_external)
        lines.append(f"EXTERNAL_EVIDENCE names unknown requirement IDs: {stale}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--verbose", "-v", action="store_true", help="list test node IDs")
    args = parser.parse_args(argv)
    if not (args.root / RESULTS).exists():
        print(f"no test results in {RESULTS}: run pytest first")
        return 1 if args.strict else 0
    m = matrix(args.root)
    print(render(m, verbose=args.verbose))
    if args.strict:
        ok = m.strict_ok()
        print("\nstrict: " + ("ok" if ok else "FAILED"))
        return 0 if ok else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
