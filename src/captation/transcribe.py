"""Local transcript and summary of a finalised session (ADR-023).

Runs after post-processing, entirely on this machine: ``whisper-cli`` (whisper.cpp) turns the
local and the remote track of every segment into timed text, the two are merged into one
conversation (``local`` = the user, ``remote`` = the other side), and ``llama-completion``
(llama.cpp) writes a summary with agreements and to-dos, constrained to a JSON schema. Both
tools are external processes with local model files: no network, no cloud (principles §3.3).

Everything lands in ``<session>/processed/``:

- ``transcript.json``: machine-readable (``specs/transcript.schema.json``): the session's metadata,
  the transcript and the summary;
- ``transcript.md``: the same for people;
- ``SHA256SUMS``: checksums of the two files.

Agreements and to-dos are only listed when the call contains them: every item must quote the
transcript (``evidence``), items whose quote is not found are dropped, so a model that invents
an item cannot get it into the file. No transcript text is ever logged.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal

from captation import integrity
from captation.config import APP_AUTHOR, APP_NAME, Config, TranscriptionConfig

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
PROCESSED_DIR = "processed"
TRANSCRIPT_JSON = "transcript.json"
TRANSCRIPT_MD = "transcript.md"
SESSION_FILE = "session.json"
Speaker = Literal["local", "remote"]
SPEAKERS: tuple[Speaker, ...] = ("local", "remote")
NICE = "/usr/bin/nice"  # PF-136: lower priority, so a running call is never starved
NICE_LEVEL = "10"
# A track this quiet is not transcribed: Whisper invents text in silence (PF-136).
SILENT_RMS_DBFS = -70.0
SILENT_RATIO = 0.98
# llama.cpp budget: ~3.2 characters per token for German/English text (conservative).
CHARS_PER_TOKEN = 3.2
PROMPT_RESERVE_TOKENS = 1500
URGENCIES = ("hoch", "mittel", "niedrig")

Runner = Callable[[list[str], float], subprocess.CompletedProcess[str]]


class TranscriptionError(RuntimeError):
    """A step failed; the message never contains transcript text."""


@dataclass(frozen=True)
class Utterance:
    start_s: float
    end_s: float
    speaker: Speaker
    text: str
    segment: int


@dataclass
class TrackResult:
    segment: int
    speaker: Speaker
    status: Literal["ok", "partial", "silent", "missing", "failed"]
    language: str | None = None
    utterances: list[Utterance] = field(default_factory=list)


# ---- paths and tools ----------------------------------------------------------------------------


def models_dir() -> Path:
    from platformdirs import user_data_dir

    return Path(user_data_dir(APP_NAME, APP_AUTHOR)) / "models"


def _resolve(path: str | None, default_name: str) -> Path:
    if path:
        return Path(path).expanduser()
    return models_dir() / default_name


def whisper_model(tc: TranscriptionConfig) -> Path:
    return _resolve(tc.model, tc.default_model)


def vad_model(tc: TranscriptionConfig) -> Path | None:
    if not tc.vad:
        return None
    return _resolve(tc.vad_model, tc.default_vad_model)


def summary_model(tc: TranscriptionConfig) -> Path:
    return _resolve(tc.summary.model, tc.summary.default_model)


def missing_tools(tc: TranscriptionConfig) -> list[str]:
    """What is not installed (for ``doctor`` and the log); empty when ready."""
    missing: list[str] = []
    if shutil.which(tc.whisper_bin) is None:
        missing.append(f"whisper.cpp ({tc.whisper_bin})")
    if not whisper_model(tc).is_file():
        missing.append("Whisper model")
    vad = vad_model(tc)
    if vad is not None and not vad.is_file():
        missing.append("VAD model")
    if tc.summary.enabled:
        if shutil.which(tc.summary.llama_bin) is None:
            missing.append(f"llama.cpp ({tc.summary.llama_bin})")
        if not summary_model(tc).is_file():
            missing.append("summary model")
    return missing


def _default_runner(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [NICE, "-n", NICE_LEVEL, *argv],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        stdin=subprocess.DEVNULL,
    )


# ---- session metadata ---------------------------------------------------------------------------


def _ts(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.astimezone()


def load_session(session_dir: Path) -> dict[str, Any]:
    doc = json.loads((session_dir / SESSION_FILE).read_text(encoding="utf-8"))
    if not isinstance(doc, dict):
        raise TranscriptionError("session.json is not an object")
    return doc


def session_metadata(doc: dict[str, Any]) -> dict[str, Any]:
    """The session's metadata for the processed file (no window titles, no file contents)."""
    started, ended = _ts(doc.get("started_at")), _ts(doc.get("ended_at"))
    segments = []
    for seg in doc.get("segments") or []:
        files = [
            {k: f.get(k) for k in ("role", "path", "sha256", "bytes", "duration_s", "codec", "qc")}
            for f in seg.get("files") or []
        ]
        segments.append(
            {
                "index": seg.get("index"),
                "mode": seg.get("mode"),
                "trigger": seg.get("trigger"),
                "started_at": seg.get("started_at"),
                "ended_at": seg.get("ended_at"),
                "interrupted": seg.get("interrupted"),
                "capture": seg.get("capture"),
                "files": files,
            }
        )
    app = doc.get("app") or {}
    return {
        "session_id": doc.get("session_id"),
        "project_id": doc.get("project_id"),
        "consent_ref": doc.get("consent_ref"),
        "status": doc.get("status"),
        "origin": (doc.get("origin") or {}).get("type"),
        "app": {"id": app.get("id"), "category": app.get("category")},
        "started_at": doc.get("started_at"),
        "ended_at": doc.get("ended_at"),
        "duration_s": (ended - started).total_seconds() if started and ended else None,
        "modes": sorted({s["mode"] for s in segments if s.get("mode")}),
        "host": doc.get("host"),
        "warnings": doc.get("warnings") or [],
        "segments": segments,
    }


def needs_processing(session_dir: Path) -> bool:
    """A finalised session with audio tracks and no ``processed/transcript.json`` yet."""
    if (session_dir / PROCESSED_DIR / TRANSCRIPT_JSON).is_file():
        return False
    try:
        doc = load_session(session_dir)
    except (OSError, ValueError, TranscriptionError):
        return False
    if doc.get("status") not in ("complete", "interrupted"):
        return False
    return any(
        f.get("role") in SPEAKERS and (session_dir / str(f.get("path"))).is_file()
        for seg in doc.get("segments") or []
        for f in seg.get("files") or []
    )


# ---- transcription ------------------------------------------------------------------------------


def _silent(file_doc: dict[str, Any]) -> bool:
    qc = file_doc.get("qc") or {}
    rms, ratio = qc.get("rms_dbfs"), qc.get("silent_ratio")
    return (isinstance(rms, (int, float)) and rms <= SILENT_RMS_DBFS) or (
        isinstance(ratio, (int, float)) and ratio >= SILENT_RATIO
    )


def _whisper_argv(
    tc: TranscriptionConfig, inputs: list[Path], language: str | None = None
) -> list[str]:
    """One run for several inputs (one model load); each writes ``<input>.json`` (PF-136)."""
    argv = [tc.whisper_bin, "-m", str(whisper_model(tc))]
    for audio in inputs:
        argv += ["-f", str(audio)]
    argv += [
        "-l",
        language or tc.language,
        "-t",
        str(tc.threads),
        "-oj",  # PF-136
        "-np",
        "-sns",
    ]
    vad = vad_model(tc)
    if vad is not None:
        argv += ["--vad", "-vm", str(vad)]  # PF-136
    return argv


def _parse_whisper(
    doc: dict[str, Any], segment: int, speaker: Speaker, offset_s: float
) -> TrackResult:
    utterances = []
    for item in doc.get("transcription") or []:
        text = " ".join(str(item.get("text", "")).split())
        offsets = item.get("offsets") or {}
        start, end = offsets.get("from"), offsets.get("to")
        if not text or not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
            continue
        utterances.append(
            Utterance(
                start_s=round(offset_s + start / 1000.0, 2),
                end_s=round(offset_s + max(end, start) / 1000.0, 2),
                speaker=speaker,
                text=text,
                segment=segment,
            )
        )
    language = (doc.get("result") or {}).get("language")
    return TrackResult(segment, speaker, "ok", str(language) if language else None, utterances)


FFMPEG = "ffmpeg"  # as in post-processing: resolved on PATH
# Each track holds one speaker; pauses while the other side talks split it into turns (PF-136:
# Whisper's own VAD merges turns and interpolates their times).
SILENCE_NOISE_DB = (-45, -35)  # the second try for tracks with a noise floor
SILENCE_MIN_S = 0.8
REGION_PAD_S = 0.25
MIN_REGION_S = 0.3
WHISPER_BATCH = 150  # input files per whisper-cli run (one model load)
_SILENCE_RE = re.compile(r"silence_(start|end): (-?[0-9.]+)")
_DURATION_RE = re.compile(r"Duration: (\d+):(\d{2}):(\d{2}(?:\.\d+)?)")
MIN_CHUNK_BYTES = 44 + int(16000 * 2 * MIN_REGION_S)  # 16 kHz mono s16 WAV of MIN_REGION_S


def speech_regions(
    audio: Path, duration_s: float | None, runner: Runner, timeout: float
) -> list[tuple[float, float]] | None:
    """Speech sections of a one-speaker track (ffmpeg silencedetect); None if ffmpeg failed."""
    end_of_track = duration_s if duration_s and duration_s > 0 else None
    regions: list[tuple[float, float]] = []
    for noise in SILENCE_NOISE_DB:
        argv = [
            FFMPEG,
            "-hide_banner",
            "-nostats",
            "-i",
            str(audio),
            "-af",
            f"silencedetect=noise={noise}dB:d={SILENCE_MIN_S}",
            "-f",
            "null",
            "-",
        ]
        res = runner(argv, timeout)
        if res.returncode != 0:
            return None
        found = _DURATION_RE.search(res.stderr or "")
        if found:  # the real length beats session.json (never cut past the end, PF-136)
            h, m, sec = found.groups()
            end_of_track = int(h) * 3600 + int(m) * 60 + float(sec)
        if end_of_track is None:
            return None
        cursor = 0.0
        regions = []
        silent_from: float | None = None
        for kind, value in _SILENCE_RE.findall(res.stderr or ""):
            t = max(0.0, float(value))
            if kind == "start":
                silent_from = t
                if t - cursor > 0:
                    regions.append((cursor, t))
            else:
                cursor = t
                silent_from = None
        if silent_from is None and end_of_track - cursor > 0:
            regions.append((cursor, end_of_track))
        padded = [
            (max(0.0, a - REGION_PAD_S), min(end_of_track, b + REGION_PAD_S))
            for a, b in regions
            if b - a >= MIN_REGION_S
        ]
        merged: list[tuple[float, float]] = []
        for a, b in padded:
            if merged and a <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], b))
            else:
                merged.append((a, b))
        regions = merged
        if len(regions) >= 2:
            break
    return regions


def transcribe_track(
    tc: TranscriptionConfig,
    audio: Path,
    segment: int,
    speaker: Speaker,
    offset_s: float,
    workdir: Path,
    runner: Runner = _default_runner,
    language: str | None = None,
    duration_s: float | None = None,
) -> TrackResult:
    """Transcribe one track turn by turn: every speech section is its own Whisper input, so
    each utterance keeps its real start time (one whisper-cli run per batch of sections)."""
    regions = speech_regions(audio, duration_s, runner, tc.timeout_s)
    if regions is None:
        log.warning("transcript: speech detection failed for segment %d (%s)", segment, speaker)
        return TrackResult(segment, speaker, "failed")
    if not regions:
        return TrackResult(segment, speaker, "ok", language)
    chunk_dir = workdir / f"seg{segment:02d}_{speaker}"
    chunk_dir.mkdir(mode=0o700, exist_ok=True)
    chunks_: list[tuple[Path, float]] = []
    for n, (a, b) in enumerate(regions, 1):
        chunk = chunk_dir / f"c{n:05d}.wav"
        cut = [FFMPEG, "-v", "error", "-y", "-ss", f"{a:.3f}", "-to", f"{b:.3f}", "-i", str(audio),
               "-ac", "1", "-ar", "16000", str(chunk)]  # fmt: skip
        if runner(cut, tc.timeout_s).returncode != 0:
            log.warning("transcript: could not cut segment %d (%s)", segment, speaker)
            return TrackResult(segment, speaker, "failed")
        # whisper-cli crashes on an empty input (exit 11, PF-136): too short sections are skipped
        if chunk.is_file() and chunk.stat().st_size >= MIN_CHUNK_BYTES:
            chunks_.append((chunk, a))
    result = TrackResult(segment, speaker, "ok")
    languages: list[str] = []
    lost = 0

    def run_batch(batch: list[tuple[Path, float]]) -> bool:
        res = runner(_whisper_argv(tc, [c for c, _ in batch], language), tc.timeout_s)
        if res.returncode != 0:
            return False
        for chunk, region_start in batch:
            out = chunk.with_name(chunk.name + ".json")
            try:
                doc = json.loads(out.read_text(encoding="utf-8", errors="replace"))
            except (OSError, ValueError):
                return False
            part = _parse_whisper(doc, segment, speaker, offset_s + region_start)
            result.utterances.extend(part.utterances)
            if part.language:
                languages.append(part.language)
        return True

    for start in range(0, len(chunks_), WHISPER_BATCH):
        batch = chunks_[start : start + WHISPER_BATCH]
        before = len(result.utterances)
        if run_batch(batch):
            continue
        del result.utterances[before:]
        # One bad section must not cost the whole track: retry each on its own.
        for item in batch:
            if not run_batch([item]):
                lost += 1
    if lost:
        log.warning(
            "transcript: %d section(s) of segment %d (%s) not transcribed", lost, segment, speaker
        )
        result.status = "failed" if lost == len(chunks_) else "partial"
    result.language = language or (max(set(languages), key=languages.count) if languages else None)
    return result


def merge(tracks: Iterable[TrackResult]) -> list[Utterance]:
    """One conversation in time order (ties: the remote side first, it usually asked)."""
    order = {"remote": 0, "local": 1}
    items = [u for t in tracks for u in t.utterances]
    return sorted(items, key=lambda u: (u.start_s, order[u.speaker], u.end_s))


# ---- summary ------------------------------------------------------------------------------------

SUMMARY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "agreements", "todos"],
    "properties": {
        "summary": {"type": "string"},
        "agreements": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["text", "evidence"],
                "properties": {"text": {"type": "string"}, "evidence": {"type": "string"}},
            },
        },
        "todos": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["task", "owner", "urgency", "deadline", "evidence"],
                "properties": {
                    "task": {"type": "string"},
                    "owner": {"type": ["string", "null"]},
                    "urgency": {"enum": [*URGENCIES, None]},
                    "deadline": {"type": ["string", "null"]},
                    "evidence": {"type": "string"},
                },
            },
        },
    },
}
REDUCE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary"],
    "properties": {"summary": {"type": "string"}},
}

SYSTEM_PROMPT = {
    "de": (
        "Du fasst ein Gesprächstranskript sachlich auf Deutsch zusammen. Sprecher: 'Ich' ist "
        "die Person, die aufgenommen hat; 'Gegenseite' sind die anderen Teilnehmenden. "
        "Verwende ausschließlich, was im Transkript steht; erfinde nichts. "
        "'summary': 3 bis 8 Sätze zum Inhalt. "
        "'agreements': nur ausdrücklich getroffene Vereinbarungen oder Entscheidungen; sonst []. "
        "'todos': nur konkrete Aufgaben, die im Gespräch genannt wurden; sonst []. "
        "'owner': wer die Aufgabe übernimmt ('Ich', 'Gegenseite' oder ein genannter Name), "
        "null wenn nicht gesagt. 'urgency': 'hoch', 'mittel' oder 'niedrig' nur wenn die "
        "Dringlichkeit gesagt oder eindeutig ist, sonst null. 'deadline': die genannte Frist "
        "als ISO-Datum JJJJ-MM-TT, auch bei relativen Angaben wie 'morgen', 'Freitag' oder "
        "'Ende Januar', gerechnet ab dem Gesprächsdatum; wörtlich, wenn kein Datum bestimmbar "
        "ist; null, wenn keine Frist genannt wurde. "
        "'evidence': ein wörtliches Zitat aus dem Transkript, das den Punkt belegt."
    ),
    "en": (
        "You summarise a conversation transcript factually in English. Speakers: 'Me' is the "
        "person who recorded; 'Other side' are the other participants. Use only what the "
        "transcript says; invent nothing. 'summary': 3 to 8 sentences. 'agreements': only "
        "explicit agreements or decisions; otherwise []. 'todos': only concrete tasks named in "
        "the call; otherwise []. 'owner': who takes the task ('Me', 'Other side' or a named "
        "person), null if not said. 'urgency': 'hoch', 'mittel' or 'niedrig' only if stated or "
        "unambiguous, else null. 'deadline': the stated deadline as ISO date YYYY-MM-DD, also "
        "for relative words like 'tomorrow' or 'Friday', counted from the call date; verbatim "
        "if no date can be determined; null if none was stated. 'evidence': a verbatim quote "
        "from the transcript "
        "that supports the item."
    ),
}
REDUCE_PROMPT = {
    "de": "Fasse diese Teilzusammenfassungen eines Gesprächs zu einer Zusammenfassung mit 3 bis "
    "8 Sätzen zusammen; erfinde nichts.",
    "en": "Merge these partial summaries of one conversation into one summary of 3 to 8 "
    "sentences; invent nothing.",
}
SPEAKER_NAMES = {
    "de": {"local": "Ich", "remote": "Gegenseite"},
    "en": {"local": "Me", "remote": "Other side"},
}


def _lang(language: str | None) -> Literal["de", "en"]:
    return "en" if language == "en" else "de"


def _clock(seconds: float) -> str:
    total = int(seconds)
    return f"{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"


def transcript_lines(utterances: list[Utterance], lang: Literal["de", "en"]) -> list[str]:
    names = SPEAKER_NAMES[lang]
    return [f"[{_clock(u.start_s)}] {names[u.speaker]}: {u.text}" for u in utterances]


def chunks(lines: list[str], max_chars: int) -> list[list[str]]:
    out: list[list[str]] = []
    current: list[str] = []
    size = 0
    for line in lines:
        if current and size + len(line) + 1 > max_chars:
            out.append(current)
            current, size = [], 0
        current.append(line[:max_chars])
        size += len(line) + 1
    if current:
        out.append(current)
    return out


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold()
    return " ".join(re.findall(r"\w+", text))


def supported(evidence: str, corpus_norm: str, corpus_words: set[str]) -> bool:
    """The quote appears in the transcript (verbatim after normalisation, or ≥ 80 % of its
    words in the transcript for a short paraphrase of a long sentence)."""
    quote = _norm(evidence)
    if len(quote) < 3:
        return False
    if quote in corpus_norm:
        return True
    words = quote.split()
    return len(words) >= 4 and sum(w in corpus_words for w in words) / len(words) >= 0.8


def _run_llama(
    tc: TranscriptionConfig,
    system: str,
    user: str,
    schema: dict[str, Any],
    workdir: Path,
    runner: Runner,
    tag: str,
) -> dict[str, Any]:
    sc = tc.summary
    sys_file, user_file, schema_file = (
        workdir / f"{tag}.{n}" for n in ("sys.txt", "user.txt", "schema.json")
    )
    sys_file.write_text(system, encoding="utf-8")
    user_file.write_text(user, encoding="utf-8")
    schema_file.write_text(json.dumps(schema), encoding="utf-8")
    argv = [
        sc.llama_bin,
        "-m",
        str(summary_model(tc)),
        "-c",
        str(sc.context_tokens),
        "-n",
        str(sc.max_output_tokens),
        "-t",
        str(tc.threads),
        "--temp",
        "0.1",
        "-sysf",
        str(sys_file),
        "-f",
        str(user_file),
        "-jf",
        str(schema_file),
        "-st",
        "--simple-io",
        "--no-display-prompt",
        # Not --log-disable: in llama.cpp 1.0 it also suppresses the generated text (PF-137).
    ]  # PF-137
    res = runner(argv, sc.timeout_s)
    if res.returncode != 0:
        raise TranscriptionError(f"summary model failed (exit {res.returncode})")
    text = res.stdout.strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise TranscriptionError("summary model returned no JSON")
    try:
        doc = json.loads(text[start : end + 1])
    except ValueError as exc:
        raise TranscriptionError("summary model returned invalid JSON") from exc
    if not isinstance(doc, dict):
        raise TranscriptionError("summary model returned no object")
    return doc


URGENCY_CUES = re.compile(
    r"\b(dringend\w*|eilig\w*|sofort|umgehend|schnellstmöglich|asap|wichtig\w*|priorität\w*|"
    r"zeitkritisch\w*|keine eile|nicht eilig|hat zeit|urgent\w*|immediately|priority|"
    r"important|no rush|whenever)\b",
    re.IGNORECASE,
)


def urgency_said(evidence: str, utterances: list[Utterance]) -> bool:
    """An urgency counts only if the passage around the quote names one (no guessing)."""
    quote = set(_norm(evidence).split())
    if not quote or not utterances:
        return False
    scores = [len(quote & set(_norm(u.text).split())) for u in utterances]
    best = max(range(len(utterances)), key=scores.__getitem__)
    window = utterances[max(0, best - 1) : best + 2]
    return any(URGENCY_CUES.search(u.text) for u in window)


def _clean_items(
    doc: dict[str, Any], corpus_norm: str, corpus_words: set[str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    agreements: list[dict[str, Any]] = []
    todos: list[dict[str, Any]] = []
    dropped = 0
    for item in doc.get("agreements") or []:
        text, ev = str(item.get("text", "")).strip(), str(item.get("evidence", "")).strip()
        if text and supported(ev, corpus_norm, corpus_words):
            agreements.append({"text": text, "evidence": ev})
        else:
            dropped += 1
    for item in doc.get("todos") or []:
        task, ev = str(item.get("task", "")).strip(), str(item.get("evidence", "")).strip()
        if not task or not supported(ev, corpus_norm, corpus_words):
            dropped += 1
            continue
        urgency = item.get("urgency")
        owner, deadline = item.get("owner"), item.get("deadline")
        todos.append(
            {
                "task": task,
                "owner": str(owner).strip() or None if owner else None,
                "urgency": urgency if urgency in URGENCIES else None,
                "deadline": str(deadline).strip() or None if deadline else None,
                "evidence": ev,
            }
        )
    return agreements, todos, dropped


def _dedupe(items: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out = []
    for item in items:
        k = _norm(str(item.get(key, "")))
        if k and k not in seen:
            seen.add(k)
            out.append(item)
    return out


WEEKDAYS = {
    "de": ("Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"),
    "en": ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"),
}


def fix_deadline(value: str | None, call_day: date | None) -> str | None:
    """An ISO date before the call has a wrong year (the model's own idea of "now"): move it
    to the next such day on or after the call. Any other text is kept as said."""
    if not value or call_day is None or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return value
    try:
        day = date.fromisoformat(value)
    except ValueError:
        return value
    if day >= call_day:
        return value
    for year in (call_day.year, call_day.year + 1):
        try:
            moved = day.replace(year=year)
        except ValueError:  # 29 February
            continue
        if moved >= call_day:
            return moved.isoformat()
    return value


def summarize(
    tc: TranscriptionConfig,
    utterances: list[Utterance],
    language: str | None,
    workdir: Path,
    runner: Runner = _default_runner,
    call_day: date | None = None,
) -> dict[str, Any]:
    """Summary, agreements and to-dos; long calls are summarised in parts, then merged."""
    lang = _lang(language)
    lines = transcript_lines(utterances, lang)
    corpus = "\n".join(u.text for u in utterances)
    corpus_norm = _norm(corpus)
    corpus_words = set(corpus_norm.split())
    budget = int(
        (tc.summary.context_tokens - tc.summary.max_output_tokens - PROMPT_RESERVE_TOKENS)
        * CHARS_PER_TOKEN
    )
    parts = chunks(lines, max(2000, budget))
    agreements: list[dict[str, Any]] = []
    todos: list[dict[str, Any]] = []
    summaries: list[str] = []
    dropped = 0
    head = "Transkript:" if lang == "de" else "Transcript:"
    if call_day is not None:
        label = "Gesprächsdatum" if lang == "de" else "Call date"
        head = f"{label}: {call_day.isoformat()} ({WEEKDAYS[lang][call_day.weekday()]})\n{head}"
    for i, part in enumerate(parts, 1):
        doc = _run_llama(
            tc,
            SYSTEM_PROMPT[lang],
            head + "\n" + "\n".join(part),
            SUMMARY_SCHEMA,
            workdir,
            runner,
            f"part{i}",
        )
        a, t, d = _clean_items(doc, corpus_norm, corpus_words)
        for todo in t:
            todo["deadline"] = fix_deadline(todo["deadline"], call_day)
            if todo["urgency"] is not None and not urgency_said(todo["evidence"], utterances):
                todo["urgency"] = None  # not said in the call: left empty
        agreements += a
        todos += t
        dropped += d
        summaries.append(str(doc.get("summary", "")).strip())
    summary = summaries[0] if len(summaries) == 1 else ""
    if len(summaries) > 1:
        merged = _run_llama(
            tc,
            REDUCE_PROMPT[lang],
            "\n\n".join(f"({i}) {s}" for i, s in enumerate(summaries, 1)),
            REDUCE_SCHEMA,
            workdir,
            runner,
            "reduce",
        )
        summary = str(merged.get("summary", "")).strip()
    return {
        "language": lang,
        "text": summary,
        "agreements": _dedupe(agreements, "text"),
        "todos": _dedupe(todos, "task"),
        "parts": len(parts),
        "dropped_unsupported": dropped,
    }


# ---- the whole session --------------------------------------------------------------------------


@dataclass
class ProcessResult:
    session_id: str
    status: Literal["complete", "partial", "failed", "skipped"]
    reason: str | None = None


def _segment_offsets(doc: dict[str, Any]) -> dict[int, float]:
    start = _ts(doc.get("started_at"))
    out: dict[int, float] = {}
    for seg in doc.get("segments") or []:
        seg_start = _ts(seg.get("started_at"))
        idx = seg.get("index")
        if isinstance(idx, int):
            out[idx] = (seg_start - start).total_seconds() if start and seg_start else 0.0
    return out


MD_LABELS = {
    "de": {
        "title": "Transkript",
        "start": "Beginn",
        "end": "Ende",
        "duration": "Dauer",
        "mode": "Modus",
        "consent": "Einwilligung",
        "project": "Projekt",
        "summary": "Zusammenfassung",
        "agreements": "Vereinbarungen",
        "todos": "Aufgaben",
        "cols": ("Aufgabe", "Verantwortlich", "Dringlichkeit", "Frist"),
    },
    "en": {
        "title": "Transcript",
        "start": "Start",
        "end": "End",
        "duration": "Duration",
        "mode": "Mode",
        "consent": "Consent",
        "project": "Project",
        "summary": "Summary",
        "agreements": "Agreements",
        "todos": "To-dos",
        "cols": ("Task", "Owner", "Urgency", "Deadline"),
    },
}


def _render_md(result: dict[str, Any]) -> str:
    meta = result["session"]
    lang: Literal["de", "en"] = _lang((result.get("transcription") or {}).get("language"))
    names = SPEAKER_NAMES[lang]
    lb: dict[str, Any] = MD_LABELS[lang]
    out = [
        f"# {lb['title']} {meta['session_id']}",
        "",
        f"- App: {meta['app']['id']} ({meta['app']['category']})",
        f"- {lb['start']}: {meta['started_at']}  ·  {lb['end']}: {meta['ended_at']}",
        f"- {lb['duration']}: {_clock(meta['duration_s'] or 0)}  ·  "
        f"{lb['mode']}: {', '.join(meta['modes'])}",
        f"- {lb['consent']}: {meta['consent_ref'] or '–'}  ·  "
        f"{lb['project']}: {meta['project_id'] or 'unassigned'}",
        "",
    ]
    summary = result.get("summary")
    out.append(f"## {lb['summary']}")
    out.append("")
    out.append((summary or {}).get("text") or "–")
    out.append("")
    out.append(f"## {lb['agreements']}")
    out.append("")
    agreements = (summary or {}).get("agreements") or []
    out += [f"- {a['text']}" for a in agreements] or ["–"]
    out.append("")
    out.append(f"## {lb['todos']}")
    out.append("")
    todos = (summary or {}).get("todos") or []
    if todos:
        hdr = list(lb["cols"])
        out.append("| " + " | ".join(hdr) + " |")
        out.append("|---|---|---|---|")
        for t in todos:
            cells = [t["task"], t["owner"] or "–", t["urgency"] or "–", t["deadline"] or "–"]
            out.append("| " + " | ".join(c.replace("|", "/") for c in cells) + " |")
    else:
        out.append("–")
    out.append("")
    out.append(f"## {lb['title']}")
    out.append("")
    for u in result.get("transcript") or []:
        out.append(f"**[{_clock(u['start_s'])}] {names[u['speaker']]}:** {u['text']}  ")
    out.append("")
    return "\n".join(out)


def _write(processed: Path, result: dict[str, Any]) -> None:
    processed.mkdir(mode=0o700, exist_ok=True)
    for name in (TRANSCRIPT_JSON, TRANSCRIPT_MD, integrity.SUMS_NAME):
        target = processed / name
        if target.exists():
            os.chmod(target, 0o600)  # a re-run replaces the earlier result
    integrity._atomic_write_text(
        processed / TRANSCRIPT_JSON, json.dumps(result, ensure_ascii=False, indent=1) + "\n"
    )
    integrity._atomic_write_text(processed / TRANSCRIPT_MD, _render_md(result))
    integrity.write_sha256sums(processed, [TRANSCRIPT_JSON, TRANSCRIPT_MD])
    for name in (TRANSCRIPT_JSON, TRANSCRIPT_MD, integrity.SUMS_NAME):
        integrity.make_read_only(processed / name)


def process_session(
    session_dir: Path,
    cfg: Config,
    *,
    runner: Runner = _default_runner,
    now: Callable[[], datetime] = lambda: datetime.now().astimezone(),
    should_pause: Callable[[], None] = lambda: None,
) -> ProcessResult:
    """Transcribe and summarise one finalised session into ``processed/``.

    ``should_pause`` is called before every heavy step; the agent blocks there while a call is
    being recorded. Never raises for a failing tool: the result says what happened.
    """
    tc = cfg.transcription
    sid = session_dir.name
    try:
        doc = load_session(session_dir)
    except (OSError, ValueError, TranscriptionError):
        return ProcessResult(sid, "skipped", "no readable session.json")
    if doc.get("status") not in ("complete", "interrupted"):
        return ProcessResult(sid, "skipped", f"status {doc.get('status')}")
    missing = missing_tools(tc)
    if missing:
        log.warning("transcript: %s not processed, missing: %s", sid, ", ".join(missing))
        return ProcessResult(sid, "skipped", "tools missing")
    offsets = _segment_offsets(doc)
    tracks: list[TrackResult] = []
    with tempfile.TemporaryDirectory(prefix="captation-transcript-") as tmp:
        work = Path(tmp)
        os.chmod(work, 0o700)
        # The user's own track first: its language (clear speech, one speaker) is used for the
        # other side too, so a misdetected remote track cannot garble the transcript.
        language_hint: str | None = None if tc.language == "auto" else tc.language
        for speaker in ("local", "remote"):
            for seg in doc.get("segments") or []:
                idx = seg.get("index")
                if not isinstance(idx, int):
                    continue
                f = {x.get("role"): x for x in seg.get("files") or []}.get(speaker)
                path = session_dir / str((f or {}).get("path", ""))
                if f is None or not path.is_file():
                    tracks.append(TrackResult(idx, speaker, "missing"))
                    continue
                if _silent(f):
                    tracks.append(TrackResult(idx, speaker, "silent"))
                    continue
                should_pause()
                duration = f.get("duration_s")
                track = transcribe_track(
                    tc,
                    path,
                    idx,
                    speaker,
                    offsets.get(idx, 0.0),
                    work,
                    runner,
                    language_hint,
                    float(duration) if isinstance(duration, (int, float)) else None,
                )
                tracks.append(track)
            if language_hint is None:
                found = [t.language for t in tracks if t.language and t.speaker == "local"]
                language_hint = max(set(found), key=found.count) if found else None
        tracks.sort(key=lambda t: (t.segment, SPEAKERS.index(t.speaker)))
        utterances = merge(tracks)
        languages = [t.language for t in tracks if t.language]
        language = max(set(languages), key=languages.count) if languages else None
        transcribed = [t for t in tracks if t.status in ("ok", "partial")]
        failed = [t for t in tracks if t.status in ("failed", "partial")]
        summary: dict[str, Any] | None = None
        summary_error: str | None = None
        if tc.summary.enabled and utterances:
            should_pause()
            try:
                started = _ts(doc.get("started_at"))
                call_day = started.date() if started else None
                summary = summarize(tc, utterances, language, work, runner, call_day)
            except (TranscriptionError, subprocess.TimeoutExpired, OSError) as exc:
                summary_error = (
                    type(exc).__name__ if not isinstance(exc, TranscriptionError) else str(exc)
                )
                log.warning("transcript: %s summary failed: %s", sid, summary_error)
        elif tc.summary.enabled:
            summary = {
                "language": _lang(language),
                "text": "",
                "agreements": [],
                "todos": [],
                "parts": 0,
                "dropped_unsupported": 0,
            }
    if not transcribed and failed:
        status: Literal["complete", "partial", "failed"] = "failed"
    elif failed or summary_error:
        status = "partial"
    else:
        status = "complete"
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": now().isoformat(timespec="seconds"),
        "status": status,
        "session": session_metadata(doc),
        "transcription": {
            "engine": "whisper.cpp",
            "model": whisper_model(tc).name,
            "vad": vad_model(tc) is not None,
            "language": language,
            "tracks": [
                {
                    "segment": t.segment,
                    "speaker": t.speaker,
                    "status": t.status,
                    "language": t.language,
                }
                for t in tracks
            ],
        },
        "transcript": [asdict(u) for u in utterances],
        "summary": (
            None
            if summary is None
            else {
                "engine": "llama.cpp",
                "model": summary_model(tc).name,
                **summary,
            }
        ),
        "summary_error": summary_error,
    }
    _write(session_dir / PROCESSED_DIR, result)
    log.info(
        "transcript: %s %s (%d utterances, %d tracks)",
        sid,
        status,
        len(utterances),
        len(transcribed),
    )
    return ProcessResult(sid, status, summary_error)


def processed_ok(session_dir: Path) -> bool:
    """The transcript of this session is complete (the condition for deleting its media)."""
    try:
        doc = json.loads(
            (session_dir / PROCESSED_DIR / TRANSCRIPT_JSON).read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return False
    return isinstance(doc, dict) and doc.get("status") == "complete"
