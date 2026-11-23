"""Machine check for "verify, don't assume" (docs/principles.md §6).

1. Every ``# PF-xxx`` reference in ``src/`` must exist in ``docs/platform-facts.md`` with
   status ``verified``. Exception (ADR-015): a ``pending`` fact belongs to another OS (its OS
   column) and was prepared without that OS; it is reported but tolerated, except when the
   check runs on that OS (``--os``, default: the current OS) or with ``--release``, where every
   pending fact is an error. ``refuted``/``superseded`` facts may never be referenced.
2. In ``src/captation/obs/kinds.py``, ``src/captation/platform/*.py`` and the per-OS firewall
   and UI modules every string constant
   needs a PF reference on the same line. Allowed without one: docstrings, f-string parts,
   log messages (first argument of ``log.*``/``logger.*`` calls), exception messages, and
   lines marked ``# not-a-fact: <reason>``.
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PF_REF = re.compile(r"PF-\d{3}")
PF_ROW = re.compile(r"^\|\s*(PF-\d{3})\s*\|\s*([a-z]+)\s*\|\s*([a-z-]+)\s*\|", re.MULTILINE)
ALLOW_MARK = "# not-a-fact:"
LOG_METHODS = {"debug", "info", "warning", "error", "exception", "critical"}
LOGGER_NAMES = {"log", "logger", "logging", "_log", "LOG"}


def known_facts(facts_md: Path) -> dict[str, tuple[str, str]]:
    """PF-ID → (status, os)."""
    if not facts_md.exists():
        return {}
    text = facts_md.read_text(encoding="utf-8")
    return {m.group(1): (m.group(2), m.group(3)) for m in PF_ROW.finditer(text)}


def current_os() -> str:
    if sys.platform.startswith("linux"):
        return "linux"
    return "macos" if sys.platform == "darwin" else "windows"


def strict_files(root: Path) -> list[Path]:
    pkg = root / "src" / "captation"
    files = [pkg / "obs" / "kinds.py", *sorted((pkg / "obs").glob("kinds_*.py"))]
    files += sorted((pkg / "platform").glob("*.py"))
    files += sorted((pkg / "obs").glob("firewall_*.py"))
    files += [f for f in sorted((pkg / "ui").glob("*.py")) if f.stem in {"macos", "windows"}]
    return [f for f in files if f.exists() and f.name not in {"__init__.py", "base.py"}]


def _exempt_nodes(tree: ast.AST) -> set[int]:
    """ids of Constant nodes that are docstrings, log/exception messages or f-string parts."""
    exempt: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                exempt.add(id(body[0].value))
        elif isinstance(node, ast.JoinedStr):
            for part in ast.walk(node):
                if isinstance(part, ast.Constant):
                    exempt.add(id(part))
        elif isinstance(node, ast.Call) and node.args:
            func = node.func
            is_log = (
                isinstance(func, ast.Attribute)
                and func.attr in LOG_METHODS
                and isinstance(func.value, ast.Name)
                and func.value.id in LOGGER_NAMES
            )
            is_exc = (isinstance(func, ast.Name) and func.id.endswith(("Error", "Exception"))) or (
                isinstance(func, ast.Attribute) and func.attr.endswith(("Error", "Exception"))
            )
            if is_log or is_exc:
                for part in ast.walk(node.args[0]):
                    if isinstance(part, ast.Constant):
                        exempt.add(id(part))
    return exempt


def unannotated_constants(path: Path) -> list[str]:
    source = path.read_text(encoding="utf-8")
    lines = source.splitlines()
    tree = ast.parse(source)
    exempt = _exempt_nodes(tree)
    problems = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        if id(node) in exempt or node.value == "":
            continue
        span = lines[node.lineno - 1 : (node.end_lineno or node.lineno)]
        if any(PF_REF.search(line) or ALLOW_MARK in line for line in span):
            continue
        problems.append(f"{path}:{node.lineno}: string constant {node.value!r} without # PF-xxx")
    return problems


def check(
    root: Path = ROOT, *, os_name: str | None = None, release: bool = False
) -> tuple[list[str], list[str]]:
    """(problems, pending notes)."""
    facts = known_facts(root / "docs" / "platform-facts.md")
    here = os_name or current_os()
    problems: list[str] = []
    pending: set[str] = set()
    for path in sorted((root / "src").rglob("*.py")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "#" not in line:
                continue
            for ref in PF_REF.findall(line[line.index("#") :]):
                entry = facts.get(ref)
                if entry is None:
                    problems.append(f"{path}:{lineno}: {ref} is not in docs/platform-facts.md")
                    continue
                status, fact_os = entry
                if status == "verified":
                    continue
                if status == "pending" and not release and fact_os != here:
                    pending.add(f"{ref} (needs {fact_os})")
                elif status == "pending":
                    problems.append(
                        f"{path}:{lineno}: {ref} is pending for {fact_os}: verify it on {fact_os}"
                    )
                else:
                    problems.append(f"{path}:{lineno}: {ref} has status '{status}', not verified")
    for path in strict_files(root):
        problems += unannotated_constants(path)
    return problems, sorted(pending)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--os", dest="os_name", choices=["linux", "macos", "windows"])
    parser.add_argument("--release", action="store_true", help="pending facts are errors")
    args = parser.parse_args(argv)
    problems, pending = check(args.root, os_name=args.os_name, release=args.release)
    for line in problems:
        print(line)
    if pending:
        print(f"{len(pending)} pending fact(s) to verify on their OS: " + ", ".join(pending))
    print("facts ok" if not problems else f"{len(problems)} fact problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
