"""``hooks.on_session_complete``: argv lists, no shell, timeout (stage 10, task 9).

Placeholders ``{session_dir}``, ``{session_id}`` and ``{project_id}`` are replaced inside
single argv elements, so a value with spaces stays one argument. A hook failure produces a
warning for ``session.json`` and never changes media. Examples: ``docs/hooks.md``.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
from pathlib import Path

log = logging.getLogger(__name__)

PLACEHOLDERS = ("{session_dir}", "{session_id}", "{project_id}")
OUTPUT_LOG_LIMIT = 500
WARN_HOOK_NOT_FOUND = "hook:not_found"
WARN_HOOK_TIMEOUT = "hook:timeout"
WARN_HOOK_ERROR = "hook:error"


def substitute(
    argv_template: list[str], *, session_dir: Path, session_id: str, project_id: str | None
) -> list[str]:
    """Replace the placeholders in each argument. No splitting, no shell parsing."""
    values = {
        "{session_dir}": str(session_dir),
        "{session_id}": session_id,
        "{project_id}": project_id or "unassigned",
    }
    argv = []
    for arg in argv_template:
        for token, value in values.items():
            arg = arg.replace(token, value)
        argv.append(arg)
    return argv


def _truncate(data: bytes) -> str:
    text = data.decode("utf-8", errors="replace").strip()
    if len(text) <= OUTPUT_LOG_LIMIT:
        return text
    return text[:OUTPUT_LOG_LIMIT] + f"… [{len(text) - OUTPUT_LOG_LIMIT} more chars]"


def _kill_tree(proc: subprocess.Popen[bytes]) -> None:
    try:
        if sys.platform == "win32":  # pragma: no cover - Windows
            proc.kill()
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def run_hook(
    argv_template: list[str] | None,
    *,
    session_dir: Path,
    session_id: str,
    project_id: str | None,
    timeout_s: float,
    password_env: str = "CAPTATION_OBS_PASSWORD",
) -> str | None:
    """Run one hook in ``session_dir``. Returns a warning string, or None on success/no hook.

    The hook runs in its own process group; on timeout the whole group is killed. Its output is
    never logged (it can hold transcript text or names, docs/principles.md §3.5): only byte counts.
    The obs-websocket password variable is removed from the hook's environment.
    """
    if not argv_template:
        return None
    argv = substitute(
        argv_template, session_dir=session_dir, session_id=session_id, project_id=project_id
    )
    program = Path(argv[0]).name
    posix = os.name == "posix"
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=session_dir,
            env={k: v for k, v in os.environ.items() if k != password_env},
            shell=False,
            start_new_session=posix,
            creationflags=0 if posix else getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
    except FileNotFoundError:
        log.warning("hook for session %s: program %s not found", session_id, program)
        return WARN_HOOK_NOT_FOUND
    except OSError as exc:
        log.warning("hook for session %s: %s could not start (%s)", session_id, program, exc)
        return WARN_HOOK_ERROR
    try:
        out, err = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        try:
            out, err = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:  # a daemonised child still holds the pipes
            out, err = b"", b""
        log.warning("hook for session %s: %s timed out after %s s", session_id, program, timeout_s)
        _log_output(session_id, out, err)
        return WARN_HOOK_TIMEOUT
    _log_output(session_id, out, err)
    if proc.returncode != 0:
        log.warning("hook for session %s: %s exited %d", session_id, program, proc.returncode)
        return f"hook:failed:exit_{proc.returncode}"
    log.info("hook for session %s: %s completed", session_id, program)
    return None


def _log_output(session_id: str, out: bytes, err: bytes) -> None:
    if out or err:
        log.info(
            "hook output (session %s): %d bytes stdout, %d bytes stderr",
            session_id,
            len(out),
            len(err),
        )
