"""Mirror the specs and the disclaimer into package data.

``specs/{config,session,rules}.schema.json``, ``specs/rules.default.yaml`` and the repository's
``DISCLAIMER.md`` are the source of truth. Runtime code loads only the mirrored copies in
``src/captation/data/``.

Usage: ``python tools/sync_specs.py`` (write) or ``python tools/sync_specs.py --check`` (exit 1
on drift).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILES = (
    "specs/config.schema.json",
    "specs/session.schema.json",
    "specs/rules.schema.json",
    "specs/rules.default.yaml",
    "DISCLAIMER.md",
)


def drift(root: Path = ROOT) -> list[str]:
    problems = []
    for rel in FILES:
        src = root / rel
        dst = root / "src" / "captation" / "data" / Path(rel).name
        if not dst.exists():
            problems.append(f"missing: {dst.relative_to(root)}")
        elif src.read_bytes() != dst.read_bytes():
            problems.append(f"differs: {dst.relative_to(root)} (source: {rel})")
    return problems


def sync(root: Path = ROOT) -> list[str]:
    written = []
    for rel in FILES:
        src = root / rel
        dst = root / "src" / "captation" / "data" / Path(rel).name
        if not dst.exists() or src.read_bytes() != dst.read_bytes():
            dst.write_bytes(src.read_bytes())
            written.append(str(dst.relative_to(root)))
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail on drift, write nothing")
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    if args.check:
        problems = drift(args.root)
        for line in problems:
            print(line)
        print("specs in sync" if not problems else f"{len(problems)} file(s) out of sync")
        return 1 if problems else 0
    for line in sync(args.root):
        print(f"wrote {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
