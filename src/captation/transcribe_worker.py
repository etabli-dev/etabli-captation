"""Background worker for transcripts and media retention (ADR-023).

One thread works through finished sessions in order. It never competes with a call: before
every heavy step it waits while the agent records (or counts down, stops, recovers). On start
it queues sessions that were finished but not yet processed (agent stopped mid-way, tools
installed later) and runs the media retention; the retention repeats every few hours.
"""

from __future__ import annotations

import contextlib
import logging
import os
import queue
import shutil
import signal
import subprocess
import tempfile
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
TOOL_POLL_S = 1.0
# Pausing needs POSIX job-control signals; on Windows a tool simply keeps running.
SIGSTOP: int | None = getattr(signal, "SIGSTOP", None)
SIGCONT: int | None = getattr(signal, "SIGCONT", None)
SIGKILL: int = getattr(signal, "SIGKILL", signal.SIGTERM)
STALE_TEMP_S = 24 * 3600.0  # temp folders of a killed run (they hold transcript text)


class _Stopped(BaseException):
    """The agent shuts down: leave the session for the next start. A BaseException, so the
    pipeline's "record any error as failed" handler never swallows a shutdown."""


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
        with self._lock:
            proc = self._proc
        if proc is not None:
            self._signal(proc, SIGCONT)  # a paused tool must be able to exit
            self._signal(proc, signal.SIGTERM)
        if self._thread is not None:
            self._thread.join(timeout_s)

    @staticmethod
    def _signal(proc: subprocess.Popen[str], sig: int | None) -> None:
        """To the tool's own process group (``nice`` execs the tool in place)."""
        if sig is None or proc.poll() is not None:
            return
        killpg = getattr(os, "killpg", None)
        try:
            if killpg is None:
                raise OSError("no process groups")
            killpg(proc.pid, sig)
        except OSError:
            with contextlib.suppress(OSError):
                if sig == SIGKILL:
                    proc.kill()
                else:
                    proc.send_signal(sig)

    def idle(self) -> bool:
        return self._queue.empty() and self._proc is None and not self._queued

    # ---- the thread -----------------------------------------------------------------------

    @staticmethod
    def _remove_stale_temp() -> None:
        base = Path(tempfile.gettempdir())
        cutoff = time.time() - STALE_TEMP_S
        for path in base.glob("captation-transcript-*"):
            with contextlib.suppress(OSError):
                if path.is_dir() and not path.is_symlink() and path.stat().st_mtime < cutoff:
                    shutil.rmtree(path)

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
        """Run one tool. While a call is recorded the tool is frozen (SIGSTOP) and resumed
        afterwards (SIGCONT): it never competes with a recording, also not on the GPU. The
        timeout counts running time only."""
        if self._stop.is_set():
            raise _Stopped
        proc = subprocess.Popen(
            transcribe.niced(argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            start_new_session=True,
        )
        with self._lock:
            self._proc = proc
        out: list[str] = []
        err: list[str] = []
        try:
            if self._stop.is_set():  # stop() ran before _proc was set
                self._signal(proc, signal.SIGTERM)
            readers = [
                threading.Thread(target=lambda: out.append(proc.stdout.read()), daemon=True),  # type: ignore[union-attr]
                threading.Thread(target=lambda: err.append(proc.stderr.read()), daemon=True),  # type: ignore[union-attr]
            ]
            for r in readers:
                r.start()
            used = 0.0
            paused = False
            while proc.poll() is None:
                if self._stop.is_set():
                    self._signal(proc, SIGCONT)
                    self._signal(proc, signal.SIGTERM)
                    proc.wait(10)
                    break
                busy = self._busy()
                if busy and not paused:
                    self._signal(proc, SIGSTOP)
                    paused = True
                elif not busy and paused:
                    self._signal(proc, SIGCONT)
                    paused = False
                time.sleep(TOOL_POLL_S)
                if not paused:
                    used += TOOL_POLL_S
                if used > timeout:
                    self._signal(proc, SIGKILL)
                    proc.wait(10)
                    raise subprocess.TimeoutExpired(argv, timeout)
            for r in readers:
                r.join(10)
        finally:
            with self._lock:
                self._proc = None
            if proc.poll() is None:
                self._signal(proc, SIGKILL)
        if self._stop.is_set():
            raise _Stopped
        return subprocess.CompletedProcess(argv, proc.returncode, "".join(out), "".join(err))

    def _run(self) -> None:
        self._remove_stale_temp()
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
                # This callback runs only after the transcript and summary have been committed.
                # The legacy session-complete hook remains a recording-finalization hook.
                from captation.notes_export import export

                export(item)
            except _Stopped:
                break
            except Exception:
                # Only the type: a message could quote a file's contents.
                log.exception("transcript: %s failed unexpectedly", item.name)
            finally:
                with self._lock:
                    self._queued.discard(item)
