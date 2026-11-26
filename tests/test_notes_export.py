import json
from pathlib import Path

from captation.notes_export import export


def test_text_only_revision_and_review(tmp_path: Path) -> None:
    (tmp_path / "processed").mkdir()
    event = {
        "note_id": "a982d21e-3d77-4f20-9dbc-208a5dd31549",
        "calendar_id": "notes",
        "event_uid": "uid",
        "occurrence_start": "2026-11-26T12:00:00Z",
    }
    (tmp_path / "notes-target.json").write_text(json.dumps(event))
    transcript = {
        "status": "complete",
        "generated_at": "2026-11-26T13:00:00Z",
        "session": {"session_id": "S-20261126-110000-abcd", "started_at": "2026-11-26T11:00:00Z"},
        "transcript": "Private full transcript must stay here",
        "summary": {"text": "Short summary", "agreements": [], "todos": [], "model": "local"},
    }
    (tmp_path / "processed/transcript.json").write_text(json.dumps(transcript))
    first = export(tmp_path)
    assert first is not None
    assert "Private full transcript" not in first.read_text()
    assert export(tmp_path) == first
    reviewed = export(tmp_path, reviewed=True)
    assert reviewed is not None and reviewed != first
    body = json.loads(reviewed.read_text())
    assert body["source"]["revision"] == 2
    assert body["review"]["status"] == "reviewed"
    assert export(tmp_path, reviewed=True) == reviewed
    transcript["status"] = "partial"
    (tmp_path / "processed/transcript.json").write_text(json.dumps(transcript))
    assert export(tmp_path) is None
