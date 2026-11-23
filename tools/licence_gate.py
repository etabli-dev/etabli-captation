"""Licence gate (docs/principles.md §4.5): no GPL, AGPL or unknown-licence runtime dependency.

Lists the runtime dependency closure of the project (``uv export --no-dev``) for the current
platform, with the licence each package declares (License-Expression, License, or the trove
classifiers). Exit 1 if any package is denied. LGPL is allowed (ADR-003).

With ``--notices PATH`` it also writes the third-party notices for a bundle built on this
platform: every runtime package with its licence and the licence texts shipped in its
``.dist-info``, the Python runtime licence and the PyInstaller bootloader note. LGPL packages
get a note that the bundle ships them as separate, replaceable modules.

Usage: ``uv run python tools/licence_gate.py [--notices dist/captation/THIRD_PARTY_NOTICES.txt]``
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

DENY = re.compile(r"(?<!L)(?<!Lesser )\bA?GPL|GNU (Affero )?General Public License", re.IGNORECASE)
LESSER = re.compile(r"LGPL|Lesser General Public", re.IGNORECASE)
LICENCE_FILE = re.compile(r"^(licen[cs]e|copying|notice|authors?)", re.IGNORECASE)
# Packages whose metadata carries no licence field but whose licence was checked by hand.
KNOWN = {
    "typing-extensions": "PSF-2.0",
}
PYINSTALLER_NOTE = (
    "The executables are built with PyInstaller (https://pyinstaller.org). Its bootloader is\n"
    "licensed under the GPL-2.0-or-later with a special exception that allows distributing the\n"
    "built application under any licence; see https://pyinstaller.org/en/stable/license.html.\n"
)
LGPL_NOTE = (
    "{name} is licensed under the {licence}.\n"
    "The bundle ships it as separate, unmodified Python source files (folder `{module}/`\n"
    "among the bundled files), not compiled into the executable, so it can be replaced by a\n"
    "modified version of the library. Its source code is available from {source}.\n"
)


@dataclass(frozen=True)
class Requirement:
    name: str
    marker: str  # environment marker from `uv export` ("" = all platforms)


def export_lines() -> list[str]:
    return subprocess.run(
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
    ).stdout.splitlines()


def parse_requirements(lines: list[str]) -> list[Requirement]:
    reqs: dict[str, Requirement] = {}
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith(("#", "-")):
            continue
        spec, _, marker = line.partition(";")
        name = re.split(r"[=<>~!\s\[]", spec.strip(), maxsplit=1)[0].lower()
        reqs[name] = Requirement(name, marker.strip())
    return [reqs[n] for n in sorted(reqs)]


def runtime_packages() -> list[str]:
    return [r.name for r in parse_requirements(export_lines())]


def applies_here(marker: str) -> bool:
    """True if the environment marker holds on this platform (True if it cannot be read)."""
    if not marker:
        return True
    try:
        from packaging.markers import Marker
    except ImportError:  # pragma: no cover - packaging ships with pytest and pip
        return True
    try:
        return bool(Marker(marker).evaluate())
    except (ValueError, KeyError):  # InvalidMarker, UndefinedComparison/-EnvironmentName
        return True


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


def licence_texts(dist: metadata.Distribution) -> list[tuple[str, str]]:
    """(file name, text) of the licence files in the package's .dist-info."""
    texts: list[tuple[str, str]] = []
    for path in dist.files or []:
        parts = path.parts
        if not parts or not parts[0].endswith(".dist-info") or len(parts) < 2:
            continue
        if not (parts[1] == "licenses" or LICENCE_FILE.match(path.name)):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        texts.append(("/".join(parts[1:]), text.strip()))
    if not texts:
        full = (dist.metadata.get("License") or "").strip()
        if "\n" in full:  # some packages put the whole licence text into the License field
            texts.append(("License (package metadata)", full))
    return sorted(texts)


def top_level_modules(dist: metadata.Distribution) -> list[str]:
    text = dist.read_text("top_level.txt")
    if text:
        return [m for m in text.split() if m]
    mods = {
        p.parts[0].removesuffix(".py")
        for p in dist.files or []
        if p.parts and not p.parts[0].endswith((".dist-info", ".data"))
    }
    return sorted(m for m in mods if m.isidentifier())


def python_licence() -> str | None:
    base = Path(sys.base_prefix)
    major_minor = f"python{sys.version_info.major}.{sys.version_info.minor}"
    for candidate in (base / "lib" / major_minor / "LICENSE.txt", base / "LICENSE.txt"):
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8", errors="replace").strip()
    return None


def project_url(dist: metadata.Distribution) -> str:
    meta = dist.metadata
    for entry in meta.get_all("Project-URL") or []:
        label, _, url = entry.partition(",")
        if label.strip().lower() in {"source", "source code", "repository", "homepage", "home"}:
            return url.strip()
    return (meta.get("Home-page") or "").strip() or f"https://pypi.org/project/{meta['Name']}/"


def notices(dists: list[metadata.Distribution]) -> str:
    rule = "=" * 78
    out = [
        "Établi Captation: third-party notices",
        "",
        "This bundle contains the following third-party software. Each component is",
        "distributed under its own licence, reproduced below as shipped with the component.",
        "OBS Studio is not part of this bundle: it is installed separately and used as a",
        "separate program.",
        "",
        "Python packages:",
    ]
    for dist in dists:
        out.append(f"  {dist.metadata['Name']} {dist.version}: {declared_licence(dist)}")
    out += ["", rule, "Python runtime", rule, ""]
    py = python_licence()
    out.append(
        py
        or f"Python {sys.version.split()[0]} is licensed under the PSF License Agreement; "
        "see https://docs.python.org/3/license.html."
    )
    out += ["", rule, "PyInstaller bootloader", rule, "", PYINSTALLER_NOTE.rstrip()]
    for dist in dists:
        name, licence = dist.metadata["Name"], declared_licence(dist)
        out += ["", rule, f"{name} {dist.version} ({licence})", rule]
        url = project_url(dist)
        out.append(f"Source: {url}")
        if verdict(licence) == "ok (LGPL)":
            modules = top_level_modules(dist) or [name]
            out += [
                "",
                LGPL_NOTE.format(
                    name=name, licence=licence, module=modules[0], source=url
                ).rstrip(),
            ]
        texts = licence_texts(dist)
        if not texts:
            out += ["", "(The package ships no licence file; licence as declared above.)"]
        for fname, text in texts:
            out += ["", f"--- {fname} ---", "", text]
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--notices", type=Path, help="write the third-party notices for this platform to PATH"
    )
    args = parser.parse_args(argv)
    denied = 0
    rows = []
    bundled: list[metadata.Distribution] = []
    for req in parse_requirements(export_lines()):
        if not applies_here(req.marker):
            rows.append((req.name, "-", f"not for this platform ({req.marker})", "skip"))
            continue
        try:
            dist = metadata.distribution(req.name)
        except metadata.PackageNotFoundError:
            rows.append((req.name, "-", "not installed on this platform", "skip"))
            continue
        lic = declared_licence(dist)
        v = verdict(lic)
        denied += v.startswith("DENY")
        rows.append((req.name, dist.version, lic, v))
        bundled.append(dist)
    width = max(len(r[0]) for r in rows) if rows else 10
    for name, ver, lic, v in rows:
        print(f"{name:<{width}}  {ver:<12} {v:<16} {lic}")
    print(f"\n{len(rows)} runtime packages, {len(bundled)} for this platform, {denied} denied")
    if denied:
        return 1
    if args.notices is not None:
        args.notices.parent.mkdir(parents=True, exist_ok=True)
        args.notices.write_text(notices(bundled), encoding="utf-8")
        print(f"wrote {args.notices}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
