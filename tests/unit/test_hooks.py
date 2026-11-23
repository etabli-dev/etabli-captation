from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from captation.hooks import WARN_HOOK_NOT_FOUND, WARN_HOOK_TIMEOUT, run_hook, substitute
from captation.integrity import sha256_file
from captation.postprocess import finalize_session
from captation.storage import HostInfo, SessionRecord, new_session_id
from tests.fixtures.media import make_config

RECORDER = """\
import json, os, sys
out = sys.argv[1]
with open(out, "w", encoding="utf-8") as fh:
    json.dump({"argv": sys.argv[2:], "cwd": os.getcwd()}, fh)
print("hook ran")
"""


def _recorder(tmp_path: Path) -> tuple[Path, Path]:
    script = tmp_path / "hook script.py"
    script.write_text(RECORDER, encoding="utf-8")
    return script, tmp_path / "hook output.json"


@pytest.mark.req("R-48")
def test_placeholders_are_whole_arguments_with_spaces(tmp_path: Path) -> None:
    session_dir = tmp_path / "Captation Data" / "PRJ-2026-001" / "S-20261123-141502-a3f9"
    session_dir.mkdir(parents=True)
    script, out = _recorder(tmp_path)
    warning = run_hook(
        [
            sys.executable,
            str(script),
            str(out),
            "{session_dir}",
            "{session_id}",
            "{project_id}",
            "--out={session_dir}/transcripts",
            "$(touch pwned); echo {session_id}",
        ],
        session_dir=session_dir,
        session_id="S-20261123-141502-a3f9",
        project_id="PRJ-2026-001",
        timeout_s=30,
    )
    assert warning is None
    got = json.loads(out.read_text(encoding="utf-8"))
    assert got["argv"] == [
        str(session_dir),
        "S-20261123-141502-a3f9",
        "PRJ-2026-001",
        f"--out={session_dir}/transcripts",
        "$(touch pwned); echo S-20261123-141502-a3f9",  # no shell: passed verbatim
    ]
    assert os.path.samefile(got["cwd"], session_dir)
    assert not list(tmp_path.rglob("pwned"))


def test_substitute_unassigned_project() -> None:
    argv = substitute(
        ["x", "{project_id}"], session_dir=Path("/d"), session_id="S-1", project_id=None
    )
    assert argv == ["x", "unassigned"]


@pytest.mark.req("R-48")
def test_no_hook_configured() -> None:
    for template in (None, []):
        assert (
            run_hook(template, session_dir=Path("."), session_id="S", project_id=None, timeout_s=1)
            is None
        )


@pytest.mark.req("R-48")
def test_failures_are_warnings(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    common = {"session_dir": tmp_path, "session_id": "S-1", "project_id": None}
    with caplog.at_level("INFO", logger="captation.hooks"):
        failed = run_hook(
            [sys.executable, "-c", "import sys; print('x' * 5000); sys.exit(3)"],
            timeout_s=30,
            **common,  # type: ignore[arg-type]
        )
    assert failed == "hook:failed:exit_3"
    messages = [r.getMessage() for r in caplog.records]
    # 5000 characters plus the line ending (LF, or CRLF on Windows)
    assert any(re.search(r"500[12] bytes stdout", m) for m in messages)
    assert not any("xxxx" in m for m in messages)  # hook output is never logged (§3.5)
    missing = run_hook(
        [str(tmp_path / "does-not-exist")],
        timeout_s=30,
        **common,  # type: ignore[arg-type]
    )
    assert missing == WARN_HOOK_NOT_FOUND
    start = time.monotonic()
    timed_out = run_hook(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        timeout_s=1,
        **common,  # type: ignore[arg-type]
    )
    assert timed_out == WARN_HOOK_TIMEOUT
    assert time.monotonic() - start < 10


@pytest.mark.req("R-48")
def test_failing_hook_adds_warning_and_keeps_media(tmp_path: Path) -> None:
    script = tmp_path / "bad hook.py"
    script.write_text(
        "import pathlib, sys\n"
        "for p in pathlib.Path(sys.argv[1]).glob('seg*'):\n"
        "    try:\n"
        "        p.write_bytes(b'overwritten')\n"
        "    except PermissionError:\n"
        "        pass\n"
        "sys.exit(1)\n",
        encoding="utf-8",
    )
    cfg = make_config(
        tmp_path,
        {"hooks.on_session_complete": [sys.executable, str(script), "{session_dir}"]},
    )
    t0 = datetime(2026, 11, 23, 14, 15, 2).astimezone()
    rec = SessionRecord.create(
        cfg,
        session_id=new_session_id(t0, "c0de"),
        app_id="signal",
        category="messenger",
        consent_ref="P-017",
        started_at=t0,
        host=HostInfo("linux", "test-os", "0.1.0", None, None),
    )
    rec.begin_segment(1, "audio", "rule:default", t0, "none")
    rec.end_segment(1, t0 + timedelta(seconds=1))
    media = rec.directory / "seg01_container.mkv"
    media.write_bytes(b"not really media")
    rec.register_file(1, "container", media)
    before = sha256_file(media)
    result = finalize_session(rec, cfg, status="complete")
    assert "hook:failed:exit_1" in result.warnings
    doc = json.loads(rec.session_file.read_text(encoding="utf-8"))
    assert "hook:failed:exit_1" in doc["warnings"]
    assert sha256_file(media) == before  # read-only before the hook ran


def test_hook_not_run_for_cancelled_sessions(tmp_path: Path) -> None:
    script, out = _recorder(tmp_path)
    cfg = make_config(
        tmp_path,
        {"hooks.on_session_complete": [sys.executable, str(script), str(out), "{session_id}"]},
    )
    t0 = datetime(2026, 11, 23, 14, 15, 2).astimezone()
    rec = SessionRecord.create(
        cfg,
        session_id=new_session_id(t0, "dead"),
        app_id="signal",
        category="messenger",
        consent_ref="P-017",
        started_at=t0,
        host=HostInfo("linux", "test-os", "0.1.0", None, None),
    )
    finalize_session(rec, cfg, status="cancelled")
    assert not out.exists()
    assert not (rec.directory / "SHA256SUMS").exists()
    assert sorted(p.name for p in rec.directory.iterdir()) == ["session.json"]


@pytest.mark.req("R-48", "R-34")
def test_hook_environment_has_no_websocket_password(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CAPTATION_OBS_PASSWORD", "secret-value-for-test")
    out = tmp_path / "env.txt"
    warning = run_hook(
        [
            sys.executable,
            "-c",
            f"import os; open({str(out)!r}, 'w').write(repr(sorted(os.environ)))",
        ],
        session_dir=tmp_path,
        session_id="S-1",
        project_id=None,
        timeout_s=30,
    )
    assert warning is None and "CAPTATION_OBS_PASSWORD" not in out.read_text()
