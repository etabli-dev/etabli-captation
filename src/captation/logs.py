"""Structured JSON-lines logging with rotation and a personal-data redaction filter.

Personal data never reaches the log: known sensitive ``extra`` fields are dropped, and paths
below the data root are cut down to the session ID (docs/principles.md §3.5).
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import re
import time
from pathlib import Path
from typing import Any

from platformdirs import user_log_dir

from captation.config import APP_AUTHOR, APP_NAME, ensure_private_dir

SENSITIVE_FIELDS = frozenset(
    {
        "title",
        "titles",
        "window_title",
        "window_titles",
        "file_name",
        "filename",
        "source_name",
        "source_path",
        "contact",
        "phone",
        "meeting_id",
        "password",
    }
)
_STANDARD_ATTRS = frozenset(vars(logging.makeLogRecord({})).keys()) | {"message", "asctime"}
_SESSION_RE = re.compile(r"S-\d{8}-\d{6}-[0-9a-f]{4}")
REDACTED = "[redacted]"


class RedactionFilter(logging.Filter):
    """Drops sensitive extras and shortens data-root paths to the session ID."""

    def __init__(self, data_root: Path | None = None) -> None:
        super().__init__()
        self._root = str(data_root.expanduser()) if data_root else None

    def _scrub(self, text: str) -> str:
        if not self._root or self._root not in text:
            return text
        pattern = re.escape(self._root) + r"[^\s'\"]*"

        def repl(match: re.Match[str]) -> str:
            sid = _SESSION_RE.search(match.group(0))
            return f"<data_root>/{sid.group(0)}" if sid else "<data_root>"

        return re.sub(pattern, repl, text)

    def filter(self, record: logging.LogRecord) -> bool:
        for key in list(vars(record)):
            if key in SENSITIVE_FIELDS:
                setattr(record, key, REDACTED)
        if record.args:
            record.msg = record.getMessage()
            record.args = None
        record.msg = self._scrub(str(record.msg))
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        doc: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created))
            + f".{int(record.msecs):03d}",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in vars(record).items():
            if key not in _STANDARD_ATTRS and not key.startswith("_"):
                doc[key] = (
                    value if isinstance(value, (str, int, float, bool, type(None))) else str(value)
                )
        if record.exc_info and record.exc_info[0] is not None:
            doc["exc"] = record.exc_info[0].__name__
        return json.dumps(doc, ensure_ascii=False)


def log_dir(configured: str | None = None) -> Path:
    if configured:
        return ensure_private_dir(Path(configured).expanduser())
    return ensure_private_dir(Path(user_log_dir(APP_NAME, APP_AUTHOR)))


def setup_logging(
    level: str = "INFO",
    directory: str | None = None,
    data_root: Path | None = None,
    *,
    console: bool = False,
) -> Path:
    """Configure the root ``captation`` logger. Returns the log file path."""
    path = log_dir(directory) / "agent.jsonl"
    logger = logging.getLogger("captation")
    logger.setLevel(level)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    redaction = RedactionFilter(data_root)
    file_handler = logging.handlers.RotatingFileHandler(
        path, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(JsonFormatter())
    file_handler.addFilter(redaction)
    logger.addHandler(file_handler)
    if console:
        stream = logging.StreamHandler()
        stream.setFormatter(JsonFormatter())
        stream.addFilter(redaction)
        logger.addHandler(stream)
    path.chmod(0o600)
    logger.propagate = False
    return path
