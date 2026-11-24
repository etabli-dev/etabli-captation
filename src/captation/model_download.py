"""Model download for transcripts and summaries (ADR-023): an explicit setup command.

The only module of the package that may open a network connection besides the loopback
obs-websocket client. Only ``captation models download`` (cli.py) imports it; the agent never
does (tests/unit/test_no_network.py). Sources and SHA-256 are pinned: a changed or corrupted
download is refused.
"""

from __future__ import annotations

import hashlib
import os
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from captation import integrity
from captation.transcribe import TranscriptionError, models_dir

# Pinned sources and SHA-256 (PF-136, PF-137): a changed or corrupted download is refused.
MODELS: dict[str, tuple[str, int, str]] = {
    "ggml-large-v3-turbo-q5_0.bin": (
        "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo-q5_0.bin",
        574041195,
        "394221709cd5ad1f40c46e6031ca61bce88931e6e088c188294c6d5a55ffa7e2",
    ),
    "ggml-silero-v5.1.2.bin": (
        "https://huggingface.co/ggml-org/whisper-vad/resolve/main/ggml-silero-v5.1.2.bin",
        885098,
        "29940d98d42b91fbd05ce489f3ecf7c72f0a42f027e4875919a28fb4c04ea2cf",
    ),
    "Qwen2.5-7B-Instruct-Q4_K_M.gguf": (
        "https://huggingface.co/bartowski/Qwen2.5-7B-Instruct-GGUF/resolve/main/"
        "Qwen2.5-7B-Instruct-Q4_K_M.gguf",
        4683074240,
        "65b8fcd92af6b4fefa935c625d1ac27ea29dcb6ee14589c55a8f115ceaaa1423",
    ),
}


def download_models(
    dest: Path | None = None,
    *,
    fetch: Callable[[str], Any] | None = None,
    progress: Callable[[str], None] = lambda line: None,
) -> list[str]:
    """Download the default models into ``models_dir()`` and verify size and SHA-256. Already
    present, verified files are kept. Returns the names that are ready."""
    target = dest or models_dir()
    target.mkdir(parents=True, exist_ok=True)
    os.chmod(target, 0o700)
    opener = fetch or (
        lambda url: urllib.request.urlopen(
            urllib.request.Request(url, headers={"User-Agent": "etabli-captation"}), timeout=120
        )
    )
    ready: list[str] = []
    for name, (url, size, sha) in MODELS.items():
        path = target / name
        if path.is_file() and path.stat().st_size == size and integrity.sha256_file(path) == sha:
            progress(f"{name}: present")
            ready.append(name)
            continue
        part = path.with_name(f".{name}.part")
        digest = hashlib.sha256()
        got = 0
        with opener(url) as response, open(part, "wb") as fh:
            while chunk := response.read(1 << 20):
                fh.write(chunk)
                digest.update(chunk)
                got += len(chunk)
        if got != size or digest.hexdigest() != sha:
            part.unlink(missing_ok=True)
            raise TranscriptionError(f"{name}: size or SHA-256 does not match the pinned value")
        os.chmod(part, 0o600)
        os.replace(part, path)
        progress(f"{name}: downloaded and verified")
        ready.append(name)
    return ready
