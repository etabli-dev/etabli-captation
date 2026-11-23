"""Licence gate (docs/principles.md §4.5): no GPL, AGPL or unknown-licence runtime dependency.

Lists the runtime dependency closure of the project (``uv export --no-dev``) as installed for
the current platform, with the licence each package declares (License-Expression, License,
or the trove classifiers). Exit 1 if any package is denied. LGPL is allowed (ADR-003).

Usage: ``uv run python tools/licence_gate.py``
"""

from __future__ import annotations

import re
import subprocess
import sys
from importlib import metadata

DENY = re.compile(r"(?<!L)(?<!Lesser )\bA?GPL|GNU (Affero )?General Public License", re.IGNORECASE)
LESSER = re.compile(r"LGPL|Lesser General Public", re.IGNORECASE)
# Packages whose metadata carries no licence field but whose licence was checked by hand.
KNOWN = {
    "typing-extensions": "PSF-2.0",
}


def runtime_packages() -> list[str]:
    out = subprocess.run(
        [
            "uv",
            "export",
            "--no-dev",
            "--no-hashes",
            "--no-emit-project",
            "--format",
            "requirements-txt",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    ).stdout
    names = []
    for line in out.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "-")):
            continue
        names.append(re.split(r"[=<>;\s\[]", line, maxsplit=1)[0].lower())
    return sorted(set(names))


def declared_licence(dist: metadata.Distribution) -> str:
    meta = dist.metadata
    expr = meta.get("License-Expression")
    if expr:
        return expr.strip()
    classifiers = [
        c.split("::")[-1].strip()
        for c in meta.get_all("Classifier") or []
        if c.startswith("License ::")
    ]
    lic = (meta.get("License") or "").strip().splitlines()
    first = lic[0][:80] if lic else ""
    if classifiers:
        return "; ".join(classifiers)
    return first or KNOWN.get(dist.metadata["Name"].lower(), "")


def verdict(licence: str) -> str:
    if not licence or licence.upper() in {"UNKNOWN", "OTHER/PROPRIETARY LICENSE"}:
        return "DENY (unknown)"
    if LESSER.search(licence):
        return "ok (LGPL)"
    if DENY.search(licence):
        return "DENY (GPL/AGPL)"
    return "ok"


def main() -> int:
    denied = 0
    rows = []
    for name in runtime_packages():
        try:
            dist = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            rows.append((name, "-", "not installed on this platform", "skip"))
            continue
        lic = declared_licence(dist)
        v = verdict(lic)
        denied += v.startswith("DENY")
        rows.append((name, dist.version, lic, v))
    width = max(len(r[0]) for r in rows) if rows else 10
    for name, ver, lic, v in rows:
        print(f"{name:<{width}}  {ver:<12} {v:<16} {lic}")
    print(f"\n{len(rows)} runtime packages, {denied} denied")
    return 1 if denied else 0


if __name__ == "__main__":
    sys.exit(main())
