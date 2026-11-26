"""Reviewed text-only Notes contributions, generated after transcription completes.

Use ``python -m captation.notes_export SESSION --target EVENT.json --reviewed``.
Only generated envelopes from processed/notes-outbox belong in App/Notes/Captation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any
from uuid import UUID

from jsonschema import Draft202012Validator, FormatChecker


def canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def export(session: Path, target: Path | None = None, *, reviewed: bool = False) -> Path | None:
    target = target or session / "notes-target.json"
    if not target.is_file():
        return None
    event = json.loads(target.read_text())
    keys = ("note_id", "calendar_id", "event_uid", "occurrence_start")
    identity = {key: event[key] for key in keys}
    UUID(identity["note_id"])
    document = json.loads((session / "processed" / "transcript.json").read_text())
    summary = document.get("summary")
    if document.get("status") != "complete" or not summary or not summary.get("text"):
        return None  # no fallback to full transcript or incomplete summary
    item = document["session"]["session_id"]
    body = {
        "schema": "notes-contribution/1",
        "note_id": identity.pop("note_id"),
        "event": identity,
        "source": {"name": "Captation", "item_id": item, "revision": 1},
        "occurred_at": document["session"]["started_at"],
        "created_at": document["generated_at"],
        "title": event.get("title", "Recorded conversation")[:300],
        "participants": [],
        "project": document["session"].get("project_id"),
        "content": {
            "summary": summary["text"],
            "decisions": summary.get("agreements", []),
            "tasks": [
                {
                    "text": t["task"],
                    "owner": t.get("owner"),
                    "due": t.get("deadline"),
                    "evidence": t["evidence"],
                }
                for t in summary.get("todos", [])
            ],
            "open_questions": [],
        },
        "processing": {
            "engine": "captation-local",
            "model": summary.get("model", ""),
            "incomplete": False,
        },
        "review": {"status": "reviewed" if reviewed else "unreviewed"},
    }
    out = session / "processed" / "notes-outbox"
    out.mkdir(mode=0o700, exist_ok=True)
    # Immutable files are the revision ledger. Lock to avoid concurrent review/worker exports.
    with (out / ".lock").open("a+b") as lock:
        if sys.platform == "win32":
            import msvcrt

            lock.seek(0)
            lock.write(b"0")
            lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock, fcntl.LOCK_EX)
        prior = sorted(out.glob(f"*__Captation__{item}__r*.json"))
        if prior:
            old = json.loads(prior[-1].read_text())
            if old["note_id"] != body["note_id"] or old["event"] != body["event"]:
                raise ValueError("Published event identity cannot be reassigned")
            candidate = {**body, "source": old["source"], "created_at": old["created_at"]}
            if canonical(candidate) == canonical(
                {k: v for k, v in old.items() if k != "content_sha256"}
            ):
                return prior[-1]
            body["source"]["revision"] = old["source"]["revision"] + 1
        body["content_sha256"] = hashlib.sha256(canonical(body)).hexdigest()
        schema = json.loads(Path(__file__).with_name("note-contribution.schema.json").read_text())
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(body)
        name = f"{body['note_id']}__Captation__{item}__r{body['source']['revision']:04d}.json"
        final = out / name
        temp = out / (name + ".part")
        with temp.open("wb") as handle:
            os.chmod(temp, 0o600)
            handle.write(canonical(body) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, final)
        return final


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("session", type=Path)
    p.add_argument("--target", type=Path)
    p.add_argument(
        "--reviewed", action="store_true", help="Confirm checked summary, actions and event"
    )
    args = p.parse_args()
    result = export(args.session, args.target, reviewed=args.reviewed)
    if result is None:
        raise SystemExit("Complete summary and target identity are required")
    print(result)


if __name__ == "__main__":
    main()
