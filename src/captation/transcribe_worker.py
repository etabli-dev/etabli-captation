"""Background worker for transcripts and media retention (ADR-023).

One thread works through finished sessions in order. It never competes with a call: before
every heavy step it waits while the agent records (or counts down, stops, recovers). On start
it queues sessions that were finished but not yet processed (agent stopped mid-way, tools
installed later) and runs the media retention; the retention repeats every few hours.
"""

from __future__ import annotations

import logging
import queue
import subprocess
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from captation import storage, transcribe
from captation.config import Config

log = logging.getLogger(__name__)

PAUSE_POLL_S = 5.0
RETENTION_EVERY_S = 6 * 3600.0


class _Stopped(Exception):
    """The agent shuts down: leave the session for the next start."""


class TranscriptionWorker:
    def __init__(
        self,
        cfg: Config,
        busy: Callable[[], bool],
        *,
        now: Callable[[], datetime] = lambda: datetime.now().astimezone(),
        retention_every_s: float = RETENTION_EVERY_S,
        pause_poll_s: float = PAUSE_POLL_S,
    ) -> None:
        self.cfg = cfg
        self._busy = busy
        self._now = now
        self._retention_every_s = retention_every_s
        self._pause_poll_s = pause_poll_s
        self._queue: queue.Queue[Path | None] = queue.Queue()
        self._queued: set[Path] = set()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._proc: subprocess.Popen[str] | None = None
        self._thread: threading.Thread | None = None
        self._next_retention = 0.0
        self.processed: list[transcribe.ProcessResult] = []  # for tests and status

    # ---- control --------------------------------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="captation-transcribe", daemon=True)
        self._thread.start()

    def submit(self, session_dir: Path) -> None:
        with self._lock:
            if session_dir in self._queued:
                return
            self._queued.add(session_dir)
        self._queue.put(session_dir)

    def stop(self, timeout_s: float = 15.0) -> None:
        self._stop.set()
        self._queue.put(None)
        proc = self._proc
        if proc is not None and proc.poll() is None:
            proc.terminate()
        if self._thread is not None:
            self._thread.join(timeout_s)

    def idle(self) -> bool:
        return self._queue.empty() and self._proc is None and not self._queued

    # ---- the thread -----------------------------------------------------------------------

    def _catch_up(self) -> None:
        for session_dir in storage.iter_session_dirs(self.cfg.data_root_path):
            if transcribe.needs_processing(session_dir):
                self.submit(session_dir)

    def _retention_if_due(self) -> None:
        if time.monotonic() < self._next_retention:
            return
        self._next_retention = time.monotonic() + self._retention_every_s
        try:
            storage.apply_media_retention(self.cfg, self._now())
        except Exception:
            log.exception("media retention failed")

    def _pause(self) -> None:
        while self._busy() and not self._stop.is_set():
            self._stop.wait(self._pause_poll_s)
        if self._stop.is_set():
            raise _Stopped

    def _run_tool(self, argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        if self._stop.is_set():
            raise _Stopped
        proc = subprocess.Popen(
            [transcribe.NICE, "-n", transcribe.NICE_LEVEL, *argv],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            start_new_session=True,
        )
        self._proc = proc
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            raise
        finally:
            self._proc = None
        if self._stop.is_set():
            raise _Stopped
        return subprocess.CompletedProcess(argv, proc.returncode, out, err)

    def _run(self) -> None:
        try:
            self._catch_up()
        except Exception:
            log.exception("transcript: catch-up scan failed")
        while not self._stop.is_set():
            self._retention_if_due()
            try:
                item = self._queue.get(timeout=60)
            except queue.Empty:
                continue
            if item is None:
                break
            try:
                self._pause()
                result = transcribe.process_session(
                    item, self.cfg, runner=self._run_tool, now=self._now, should_pause=self._pause
                )
                self.processed.append(result)
            except _Stopped:
                break
            except Exception:
                # Only the type: a message could quote a file's contents.
                log.exception("transcript: %s failed unexpectedly", item.name)
            finally:
                with self._lock:
                    self._queued.discard(item)
