"""Acceptance gate for ``DISCLAIMER.md`` (docs/principles.md §2.10, R-59, ADR-017).

Before the agent arms or an import runs, the packaged disclaimer must have been accepted with an
explicit checkbox (``captation accept``: a dialog, or a checkbox prompt in a terminal). The
acceptance record lives in the user's state directory and is bound to the SHA-256 of the text,
so a changed text has to be accepted again. The record is the user's own declaration, kept
locally; it is not copy protection and it does not replace the approvals the disclaimer requires.
"""

from __future__ import annotations

import getpass
import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass, fields
from datetime import datetime
from importlib import resources
from pathlib import Path

from platformdirs import user_state_dir

from captation.config import APP_AUTHOR, APP_NAME, ensure_private_dir

RECORD_NAME = "terms-acceptance.json"
USER_METHODS = ("dialog-checkbox", "terminal-checkbox")
# "test-environment" is written only by the end-to-end test fixture, into its own isolated state
# directory (tests/e2e/conftest.py); the product never writes it.
METHODS = (*USER_METHODS, "test-environment")


def disclaimer_text() -> str:
    """The packaged disclaimer (mirrored from the repository root by tools/sync_specs.py)."""
    return resources.files("captation.data").joinpath("DISCLAIMER.md").read_text(encoding="utf-8")


def text_digest(text: str | None = None) -> str:
    body = disclaimer_text() if text is None else text
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def text_version(text: str | None = None) -> str:
    body = disclaimer_text() if text is None else text
    match = re.search(r"^Version:\s*(\S+)\s*$", body, re.MULTILINE)
    return match.group(1) if match else "unknown"


def record_path() -> Path:
    return Path(user_state_dir(APP_NAME, APP_AUTHOR)) / RECORD_NAME


@dataclass(frozen=True)
class Acceptance:
    version: str
    sha256: str
    accepted_at: str
    user: str
    method: str
    app_version: str


def current(path: Path | None = None) -> Acceptance | None:
    """The stored acceptance if it matches the current text; ``None`` otherwise."""
    target = path or record_path()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
        acc = Acceptance(**{f.name: str(raw[f.name]) for f in fields(Acceptance)})
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if acc.sha256 != text_digest() or acc.method not in METHODS:
        return None
    return acc


def accepted(path: Path | None = None) -> bool:
    return current(path) is not None


def _user() -> str:
    try:
        return getpass.getuser()
    except (KeyError, OSError):
        return "unknown"


def record(method: str, path: Path | None = None) -> Acceptance:
    """Store the acceptance of the current text. Only call after a ticked checkbox."""
    if method not in USER_METHODS:
        raise ValueError(f"unknown acceptance method: {method}")
    from captation.version import __version__

    text = disclaimer_text()
    acc = Acceptance(
        version=text_version(text),
        sha256=text_digest(text),
        accepted_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        user=_user(),
        method=method,
        app_version=__version__,
    )
    target = path or record_path()
    ensure_private_dir(target.parent)
    partial = target.with_name(f".{target.name}.partial")
    partial.write_text(json.dumps(asdict(acc), indent=1) + "\n", encoding="utf-8")
    if os.name == "posix":
        os.chmod(partial, 0o600)
    os.replace(partial, target)
    return acc


def revoke(path: Path | None = None) -> bool:
    """Withdraw the acceptance. Returns whether a record existed."""
    target = path or record_path()
    existed = target.exists()
    target.unlink(missing_ok=True)
    return existed
