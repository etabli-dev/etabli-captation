"""Local transcript, summary and media retention (ADR-023), with fake whisper/llama tools."""

from __future__ import annotations

import json
import subprocess
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import jsonschema
import pytest
import yaml

from captation import storage, transcribe
from captation.config import Config, data_text, parse_config
from captation.transcribe_worker import TranscriptionWorker

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = json.loads((ROOT / "specs" / "transcript.schema.json").read_text(encoding="utf-8"))
SID = "S-20261124-140000-ab12"
WHISPER: dict[str, list[tuple[int, int, str]]] = {
    "local": [
        (500, 3000, "Einverstanden, wir schließen die Rekrutierung bis zum 31. Januar ab."),
        (9000, 11000, "Ja, ich überarbeite den Ethikantrag bis zum 5. Dezember."),
    ],
    "remote": [
        (0, 400, "Hallo, schön dass es klappt."),
        (
            4000,
            8000,
            "Kannst du bitte den Ethikantrag bis 5. Dezember überarbeiten? Das ist dringend.",
        ),
    ],
}
LLAMA = {
    "summary": "Rekrutierung bis Ende Januar; Ethikantrag bis 5. Dezember.",
    "agreements": [
        {
            "text": "Rekrutierung bis 31. Januar",
            "evidence": "wir schließen die Rekrutierung bis zum 31. Januar ab",
        },
        {"text": "Erfunden", "evidence": "Wir treffen uns am Montag in Berlin zum Mittagessen"},
    ],
    "todos": [
        {
            "task": "Ethikantrag überarbeiten",
            "owner": "Ich",
            "urgency": "hoch",
            "deadline": "2023-12-05",
            "evidence": "ich überarbeite den Ethikantrag bis zum 5. Dezember",
        },
        {
            "task": "Hallo sagen",
            "owner": "Gegenseite",
            "urgency": "mittel",
            "deadline": None,
            "evidence": "Hallo, schön dass es klappt.",
        },
    ],
}


class FakeTools:
    """Stands in for ffmpeg (silencedetect, cutting), ``whisper-cli`` (writes ``<input>.json``
    per ``-f``) and ``llama-completion``; speech sections follow ``WHISPER``."""

    def __init__(
        self,
        llama: dict[str, Any] | None = None,
        *,
        whisper_fail: set[str] = frozenset(),
        crash_on: set[int] = frozenset(),
        llama_exit: int = 0,
        language: str = "de",
    ) -> None:
        self.calls: list[list[str]] = []
        self.llama = LLAMA if llama is None else llama
        self.whisper_fail = whisper_fail
        self.crash_on = crash_on  # utterance start times (ms) whose section crashes whisper
        self.llama_exit = llama_exit
        self.language = language
        self.cuts: dict[str, tuple[str, float]] = {}

    @staticmethod
    def _speaker(path: str) -> str:
        return "local" if "local" in Path(path).name else "remote"

    def __call__(self, argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        if argv[0] == "ffmpeg" and "-af" in argv:  # silencedetect
            utt = WHISPER[self._speaker(argv[argv.index("-i") + 1])]
            lines = ["  Duration: 00:00:12.00, start: 0.000000, bitrate: 500 kb/s"]
            cursor = 0
            for a, b, _ in utt:
                if a > cursor:
                    lines += [f"silence_start: {cursor / 1000}", f"silence_end: {a / 1000}"]
                cursor = b
            lines.append(f"silence_start: {cursor / 1000}")
            return subprocess.CompletedProcess(argv, 0, "", "\n".join(lines))
        if argv[0] == "ffmpeg":  # cut one section
            out = Path(argv[-1])
            out.write_bytes(b"RIFF" + b"\0" * 20000)
            self.cuts[str(out)] = (
                self._speaker(argv[argv.index("-i") + 1]),
                float(argv[argv.index("-ss") + 1]),
            )
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[0].endswith("whisper-cli"):
            inputs = [argv[k + 1] for k, a in enumerate(argv) if a == "-f"]
            for chunk in inputs:
                speaker, start = self.cuts[chunk]
                if speaker in self.whisper_fail:
                    return subprocess.CompletedProcess(argv, 1, "", "boom")
                items = [
                    (a, b, t)
                    for a, b, t in WHISPER[speaker]
                    if start * 1000 <= a < start * 1000 + 2000
                ]
                if any(a in self.crash_on for a, _, _ in items):
                    return subprocess.CompletedProcess(argv, -11, "", "")
            for chunk in inputs:
                speaker, start = self.cuts[chunk]
                items = [
                    (a, b, t)
                    for a, b, t in WHISPER[speaker]
                    if start * 1000 <= a < start * 1000 + 2000
                ]
                doc = {
                    "result": {"language": self.language},
                    "transcription": [
                        {
                            "offsets": {"from": a - int(start * 1000), "to": b - int(start * 1000)},
                            "text": " " + t,
                        }
                        for a, b, t in items
                    ],
                }
                Path(chunk + ".json").write_text(json.dumps(doc), encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, "", "")
        assert argv[0].endswith("llama-completion")
        return subprocess.CompletedProcess(
            argv, self.llama_exit, json.dumps(self.llama) + " [end of text]", ""
        )


def make_cfg(tmp_path: Path, **transcription: Any) -> Config:
    raw = yaml.safe_load(data_text("config.default.yaml"))
    raw["data_root"] = str(tmp_path / "data")
    models = tmp_path / "models"
    models.mkdir(parents=True, exist_ok=True)
    for name in ("w.bin", "vad.bin", "llm.gguf"):
        (models / name).write_bytes(b"x")
    bins = tmp_path / "bin"
    bins.mkdir(parents=True, exist_ok=True)
    for tool in ("whisper-cli", "llama-completion"):  # found on disk; the runner is faked
        (bins / tool).write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
        (bins / tool).chmod(0o755)
    summary = {"llama_bin": str(bins / "llama-completion"), "model": str(models / "llm.gguf")}
    summary.update(transcription.pop("summary", {}))
    raw["transcription"] = {
        "whisper_bin": str(bins / "whisper-cli"),
        "model": str(models / "w.bin"),
        "vad_model": str(models / "vad.bin"),
        "summary": summary,
        **transcription,
    }
    return parse_config(raw)


def make_session(
    cfg: Config,
    *,
    status: str = "complete",
    remote_qc: dict[str, float] | None = None,
    ended: str = "2026-11-24T14:00:48+01:00",
) -> Path:
    d = cfg.data_root_path / "unassigned" / SID
    d.mkdir(parents=True)
    files = []
    for role in ("local", "remote", "mix"):
        (d / f"seg01_{role}.flac").write_bytes(b"fLaC")
        qc = (
            remote_qc
            if role == "remote" and remote_qc
            else {"rms_dbfs": -30.0, "silent_ratio": 0.4}
        )
        files.append({"role": role, "path": f"seg01_{role}.flac", "sha256": "ab" * 32, "qc": qc})
    (d / "seg01_container.mkv").write_bytes(b"mkv")
    doc = {
        "schema_version": 1,
        "session_id": SID,
        "project_id": None,
        "consent_ref": "CR-1",
        "status": status,
        "origin": {"type": "obs"},
        "app": {"id": "signal", "category": "messenger", "window_title": None},
        "started_at": "2026-11-24T14:00:00+01:00",
        "ended_at": ended,
        "segments": [
            {
                "index": 1,
                "mode": "audio",
                "started_at": "2026-11-24T14:00:02+01:00",
                "ended_at": ended,
                "files": files,
            }
        ],
        "warnings": [],
    }
    (d / "session.json").write_text(json.dumps(doc), encoding="utf-8")
    return d


def run(
    session: Path, cfg: Config, tools: FakeTools
) -> tuple[transcribe.ProcessResult, dict[str, Any]]:
    result = transcribe.process_session(session, cfg, runner=tools)
    doc = json.loads((session / "processed" / "transcript.json").read_text(encoding="utf-8"))
    jsonschema.validate(doc, SCHEMA)
    return result, doc


@pytest.mark.req("R-62")
def test_transcript_summary_and_metadata(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    session = make_session(cfg)
    result, doc = run(session, cfg, FakeTools())
    assert result.status == "complete" and doc["status"] == "complete"
    meta = doc["session"]
    assert meta["session_id"] == SID and meta["consent_ref"] == "CR-1" and meta["duration_s"] == 48
    assert meta["app"] == {"id": "signal", "category": "messenger"} and meta["modes"] == ["audio"]
    # one conversation in time order, offset by the segment start (2 s), speakers kept apart
    lines = [(u["start_s"], u["speaker"]) for u in doc["transcript"]]
    assert lines == [(2.0, "remote"), (2.5, "local"), (6.0, "remote"), (11.0, "local")]
    s = doc["summary"]
    assert s["text"].startswith("Rekrutierung")
    assert [a["text"] for a in s["agreements"]] == ["Rekrutierung bis 31. Januar"]
    assert s["dropped_unsupported"] == 1  # the invented agreement had no quote in the call
    todo = s["todos"][0]
    assert todo["deadline"] == "2026-12-05"  # wrong model year corrected to after the call
    assert todo["urgency"] == "hoch"  # "dringend" was said next to it
    assert s["todos"][1]["urgency"] is None  # no urgency said: left empty
    processed = session / "processed"
    assert sorted(p.name for p in processed.iterdir()) == [
        "SHA256SUMS",
        "transcript.json",
        "transcript.md",
    ]
    md = (processed / "transcript.md").read_text(encoding="utf-8")
    assert (
        "## Vereinbarungen" in md and "| Ethikantrag überarbeiten | Ich | hoch | 2026-12-05 |" in md
    )
    assert all(
        not (processed / n).stat().st_mode & 0o222 for n in ("transcript.json", "transcript.md")
    )
    assert transcribe.processed_ok(session)


@pytest.mark.req("R-62")
def test_nothing_discussed_means_empty_sections(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    session = make_session(cfg)
    _, doc = run(session, cfg, FakeTools({"summary": "Smalltalk.", "agreements": [], "todos": []}))
    assert doc["summary"]["agreements"] == [] and doc["summary"]["todos"] == []


def test_the_users_language_is_used_for_the_other_side(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    session = make_session(cfg)
    tools = FakeTools()
    run(session, cfg, tools)
    whisper = [c for c in tools.calls if c[0].endswith("whisper-cli")]
    assert "seg01_local" in whisper[0][whisper[0].index("-f") + 1]  # own track first, auto
    assert whisper[0][whisper[0].index("-l") + 1] == "auto"
    assert whisper[1][whisper[1].index("-l") + 1] == "de"  # remote in the detected language
    assert "--vad" in whisper[0] and "-sns" in whisper[0]


def test_silent_and_failed_tracks(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    session = make_session(cfg, remote_qc={"rms_dbfs": -91.0, "silent_ratio": 1.0})
    tools = FakeTools()
    _, doc = run(session, cfg, tools)
    tracks = {t["speaker"]: t["status"] for t in doc["transcription"]["tracks"]}
    assert tracks == {"local": "ok", "remote": "silent"}  # silence is never sent to Whisper
    assert doc["status"] == "complete" and {u["speaker"] for u in doc["transcript"]} == {"local"}
    cfg2 = make_cfg(tmp_path / "b")
    session2 = make_session(cfg2)
    result, _ = run(session2, cfg2, FakeTools(whisper_fail={"remote"}))
    assert result.status == "partial" and not transcribe.processed_ok(session2)


def test_summary_failure_keeps_the_transcript(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    session = make_session(cfg)
    result, doc = run(session, cfg, FakeTools(llama_exit=1))
    assert result.status == "partial" and doc["summary"] is None
    assert doc["summary_error"] == "summary model failed (exit 1)" and doc["transcript"]


def test_long_calls_are_summarised_in_parts(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, summary={"context_tokens": 4096, "max_output_tokens": 256})
    session = make_session(cfg)
    long_text = "Wir besprechen den Fragebogen ausführlich. " * 60
    WHISPER_LONG = {k: [(i * 1000, i * 1000 + 900, long_text) for i in range(12)] for k in WHISPER}
    tools = FakeTools()
    original = dict(WHISPER)
    WHISPER.update(WHISPER_LONG)
    try:
        _, doc = run(session, cfg, tools)
    finally:
        WHISPER.update(original)
    llama_calls = [c for c in tools.calls if c[0].endswith("llama-completion")]
    assert doc["summary"]["parts"] > 1 and len(llama_calls) == doc["summary"]["parts"] + 1


def test_missing_tools_skip_without_writing(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    (tmp_path / "models" / "w.bin").unlink()
    session = make_session(cfg)
    result = transcribe.process_session(session, cfg, runner=FakeTools())
    assert result.status == "skipped" and not (session / "processed").exists()
    assert transcribe.missing_tools(cfg.transcription) == ["Whisper model"]


def test_needs_processing(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    session = make_session(cfg, status="cancelled")
    assert not transcribe.needs_processing(session)
    cfg2 = make_cfg(tmp_path / "b")
    session2 = make_session(cfg2)
    assert transcribe.needs_processing(session2)
    run(session2, cfg2, FakeTools())
    assert not transcribe.needs_processing(session2)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2023-12-05", "2026-12-05"),
        ("2026-01-31", "2027-01-31"),
        ("2026-11-25", "2026-11-25"),
        ("Ende Januar", "Ende Januar"),
        (None, None),
    ],
)
def test_fix_deadline(value: str | None, expected: str | None) -> None:
    assert transcribe.fix_deadline(value, date(2026, 11, 24)) == expected


def test_supported_quotes() -> None:
    corpus = transcribe._norm("Ja, ich überarbeite den Ethikantrag bis zum 5. Dezember.")
    words = set(corpus.split())
    assert transcribe.supported("ich überarbeite den Ethikantrag", corpus, words)
    assert not transcribe.supported("Wir fliegen nach Rom", corpus, words)
    assert not transcribe.supported("", corpus, words)


# ---- media retention (ADR-023) -------------------------------------------------------------


@pytest.mark.req("R-63")
def test_media_is_deleted_after_90_days_only_with_a_complete_transcript(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    session = make_session(cfg)
    old = datetime(2026, 11, 24, 14, tzinfo=datetime.now().astimezone().tzinfo) + timedelta(days=91)
    assert storage.apply_media_retention(cfg, old) == []  # no transcript yet: nothing deleted
    run(session, cfg, FakeTools())
    assert storage.apply_media_retention(cfg, old - timedelta(days=2)) == []  # 89 days: kept
    assert storage.apply_media_retention(cfg, old) == [SID]
    names = sorted(p.name for p in session.iterdir())
    assert names == ["processed", "session.json"]
    record = json.loads((session / "processed" / "media_deleted.json").read_text(encoding="utf-8"))
    assert record["media_retention_days"] == 90 and record["complete"] is True
    assert {f["path"] for f in record["files"]} == {
        "seg01_local.flac",
        "seg01_remote.flac",
        "seg01_mix.flac",
        "seg01_container.mkv",
    }
    assert (session / "processed" / "transcript.json").is_file()
    assert storage.apply_media_retention(cfg, old) == []  # once


def test_partial_transcripts_keep_their_media(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    session = make_session(cfg)
    run(session, cfg, FakeTools(llama_exit=1))
    old = datetime(2027, 6, 1, tzinfo=datetime.now().astimezone().tzinfo)
    assert storage.apply_media_retention(cfg, old) == []
    assert (session / "seg01_local.flac").exists()


# ---- the worker ----------------------------------------------------------------------------


def test_worker_waits_while_a_call_is_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = make_cfg(tmp_path)
    session = make_session(cfg)
    busy = threading.Event()
    busy.set()
    tools = FakeTools()
    worker = TranscriptionWorker(cfg, busy.is_set, pause_poll_s=0.02)
    monkeypatch.setattr(worker, "_run_tool", tools)
    worker.start()  # the catch-up scan finds the unprocessed session
    time.sleep(0.3)
    assert tools.calls == []  # nothing runs during a recording
    busy.clear()
    deadline = time.monotonic() + 5
    while not worker.processed and time.monotonic() < deadline:
        time.sleep(0.02)
    worker.stop()
    assert worker.processed and worker.processed[0].status == "complete"
    assert (session / "processed" / "transcript.json").is_file()


def test_worker_stops_cleanly_mid_queue(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    make_session(cfg)
    worker = TranscriptionWorker(cfg, lambda: True, pause_poll_s=0.02)
    worker.start()
    time.sleep(0.1)
    started = time.monotonic()
    worker.stop()
    assert time.monotonic() - started < 2 and not worker.processed


def test_config_defaults() -> None:
    cfg = parse_config(yaml.safe_load(data_text("config.default.yaml")))
    assert cfg.transcription.enabled and cfg.transcription.summary.enabled
    assert cfg.transcription.language == "auto" and cfg.storage.media_retention_days == 90
    assert transcribe.whisper_model(cfg.transcription).name == "ggml-large-v3-turbo-q5_0.bin"


def test_model_download_checks_the_pinned_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hashlib
    import io

    good = b"model-bytes"
    from captation import model_download

    monkeypatch.setattr(
        model_download,
        "MODELS",
        {"m.bin": ("https://example.invalid/m.bin", len(good), hashlib.sha256(good).hexdigest())},
    )
    served: list[bytes] = [good]
    fetch = lambda url: io.BytesIO(served[0])  # noqa: E731
    assert model_download.download_models(tmp_path, fetch=fetch) == ["m.bin"]
    assert (tmp_path / "m.bin").read_bytes() == good
    lines: list[str] = []
    model_download.download_models(tmp_path, fetch=fetch, progress=lines.append)
    assert lines == ["m.bin: present"]  # verified copy kept, nothing fetched
    (tmp_path / "m.bin").unlink()
    served[0] = b"tampered!!!"
    with pytest.raises(transcribe.TranscriptionError):
        model_download.download_models(tmp_path, fetch=fetch)
    assert not (tmp_path / "m.bin").exists() and not list(tmp_path.glob(".*part"))


def test_one_crashing_section_costs_only_that_section(tmp_path: Path) -> None:
    """whisper-cli segfaulted on an empty section (PF-136): a failing batch is retried section
    by section; the rest of the track survives and the session is partial (media kept)."""
    cfg = make_cfg(tmp_path)
    session = make_session(cfg)
    result, doc = run(session, cfg, FakeTools(crash_on={9000}))
    texts = [u["text"] for u in doc["transcript"]]
    assert "Ja, ich überarbeite den Ethikantrag bis zum 5. Dezember." not in texts
    assert "Einverstanden, wir schließen die Rekrutierung bis zum 31. Januar ab." in texts
    tracks = {t["speaker"]: t["status"] for t in doc["transcription"]["tracks"]}
    assert tracks["local"] == "partial" and result.status == "partial"
    assert not transcribe.processed_ok(session)


def test_speech_regions_use_the_real_track_length() -> None:
    stderr = "  Duration: 00:00:29.00, start: 0\nsilence_start: 2.2\nsilence_end: 7.5\n"

    def runner(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 0, "", stderr)

    regions = transcribe.speech_regions(Path("x.flac"), None, runner, 10)
    assert regions == [(0.0, 2.45), (7.25, 29.0)]  # padded, and never past the real end
