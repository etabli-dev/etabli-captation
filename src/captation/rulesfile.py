"""Reading rules files: packaged default, user rules file, schema validation (stage 02).

This is the I/O half of the rules handling. ``rules.py`` holds the pure half (data classes,
merging, compiling and ``classify``) and must stay free of file, process and clock access.

``builtin:default`` loads the packaged ``rules.default.yaml``. A user rules file (same schema)
is merged by app ``id`` (see ``captation.rules.merge_rules``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from captation.config import ConfigError, data_text, schema_errors
from captation.rules import Rules, compile_rules, merge_rules

BUILTIN_DEFAULT = "builtin:default"


def _read_rules_document(text: str, source: str) -> dict[str, Any]:
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{source}: YAML error: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{source}: rules must be a mapping")
    problems = schema_errors(raw, "rules.schema.json")
    if problems:
        raise ConfigError(f"{source}: invalid rules:\n  " + "\n  ".join(problems))
    return raw


def load_rules(rules_file: str = BUILTIN_DEFAULT, user_rules_file: str | None = None) -> Rules:
    """Load, validate, merge and compile the rules."""
    if rules_file == BUILTIN_DEFAULT:
        base = _read_rules_document(data_text("rules.default.yaml"), BUILTIN_DEFAULT)
    else:
        path = Path(rules_file).expanduser()
        if not path.exists():
            raise ConfigError(f"rules file not found: {path}")
        base = _read_rules_document(path.read_text(encoding="utf-8"), str(path))
    if user_rules_file:
        upath = Path(user_rules_file).expanduser()
        if not upath.exists():
            raise ConfigError(f"user rules file not found: {upath}")
        user = _read_rules_document(upath.read_text(encoding="utf-8"), str(upath))
        base = merge_rules(base, user)
    return compile_rules(base)
