"""End-to-end harness: the real agent process, real OBS, real PipeWire probe.

Only the desktop shell is stubbed (tray host + notification server on a private D-Bus), and
audio uses virtual PipeWire nodes, so no real microphone, speaker or camera is touched:
- ``fakecall-e2e`` / ``fakemeet-e2e``: copies of ``pw-cat`` (PipeWire reports the binary name)
  that record from the virtual mic and play a 1 kHz tone into a null sink;
- ``Captation Test Mic``: a pw-loopback virtual source fed with a 1320 Hz tone;
- camera: v4l2loopback ``/dev/video42`` (E3).
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import yaml

from captation.config import data_text
from tests.dbus_stub import StubDesktop
from tests.integration.conftest import TEST_MIC_DESCRIPTION, TEST_MIC_INPUT, TEST_SINK, tone_file

ROOT = Path(__file__).resolve().parents[2]
VENV_BIN = ROOT / ".venv" / "bin"
CACHE = Path.home() / ".cache" / "captation-test"
RULES = Path(__file__).with_name("rules.e2e.yaml")
CAMERA = "/dev/video42"


def wait_for(pred: Callable[[], Any], timeout: float, step: float = 0.2) -> Any:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = pred()
        if value:
            return value
        time.sleep(step)
    return pred()


def obs_password() -> str:
    import keyring

    from captation.obs.credentials import KEYRING_SERVICE, KEYRING_USER

    value = keyring.get_password(KEYRING_SERVICE, KEYRING_USER)
    assert value, "run `captation obs bootstrap` first"
    return str(value)


class Secret:
    """Keeps the websocket password out of reprs (pytest prints test arguments on failure)."""

    def __init__(self, value: str) -> None:
        self.value = value

    def __repr__(self) -> str:
        return "Secret(<redacted>)"


class Rig:
    """One agent process with its own config and data root."""

    def __init__(self, work: Path, desk: StubDesktop, password: Secret, **overrides: Any) -> None:
        self.work = work
        self.desk = desk
        self.data = work / "data"
        raw = yaml.safe_load(data_text("config.default.yaml"))
        raw["data_root"] = str(self.data)
        raw["detection"].update(
            {
                "poll_interval_ms": 500,
                "start_debounce_s": 1,
                "stop_debounce_s": 3,
                "user_rules_file": str(RULES),
            }
        )
        raw["consent"].update({"countdown_seconds": 3, "auto_disarm_after_minutes": 5})
        raw["recording"]["audio"]["local_input"] = TEST_MIC_DESCRIPTION
        raw["obs"]["websocket"]["password_source"] = "env"
        raw["qc"]["silence_warn_s"] = 5
        # Test data lives in ~/.cache (unencrypted on the test machine); the product default
        # "block" (ADR-016) would refuse arming, so the harness opts into "warn".
        raw["storage"]["require_disk_encryption"] = "warn"
        raw["logging"] = {"level": "DEBUG", "dir": str(work / "logs")}
        for dotted, value in overrides.items():
            node = raw
            keys = dotted.split("__")
            for k in keys[:-1]:
                node = node[k]
            node[keys[-1]] = value
        self.config = work / "config.yaml"
        self.config.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
        self.env = dict(os.environ)
        self.env["DBUS_SESSION_BUS_ADDRESS"] = desk.address
        self.env["CAPTATION_OBS_PASSWORD"] = password.value
        self.proc: subprocess.Popen[bytes] | None = None
        self.helpers: list[subprocess.Popen[bytes]] = []

    # ---- agent ----------------------------------------------------------------------------

    def cli(self, *args: str, timeout: float = 90) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(VENV_BIN / "captation"), *args],
            capture_output=True,
            text=True,
            env=self.env,
            timeout=timeout,
            check=False,
        )

    def status(self) -> dict[str, Any]:
        out = self.cli("status", timeout=20)
        try:
            doc: dict[str, Any] = json.loads(out.stdout)
        except ValueError:
            return {}
        return doc

    def start(self) -> None:
        log = open(self.work / "agent.out", "ab")  # noqa: SIM115 - kept open for the process
        self.proc = subprocess.Popen(
            [str(VENV_BIN / "captation"), "run", "--config", str(self.config)],
            stdout=log,
            stderr=subprocess.STDOUT,
            env=self.env,
            start_new_session=True,
        )
        assert wait_for(lambda: self.status().get("ok"), 30), "agent did not come up"

    def stop(self, timeout: float = 120) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(10)
        for h in self.helpers:
            if h.poll() is None:
                h.terminate()
                try:
                    h.wait(5)
                except subprocess.TimeoutExpired:
                    h.kill()
        self.helpers.clear()

    def arm(self, ref: str = "P-017") -> dict[str, Any]:
        out = self.cli("arm", "--ref", ref)
        doc: dict[str, Any] = json.loads(out.stdout) if out.stdout.strip().startswith("{") else {}
        return doc

    # ---- fake call / audio ------------------------------------------------------------------

    def _fake(self, name: str) -> Path:
        exe = self.work / name
        if not exe.exists():
            shutil.copy2(os.path.realpath(shutil.which("pw-cat") or "pw-cat"), exe)
        return exe

    def spawn(self, argv: list[str]) -> subprocess.Popen[bytes]:
        p = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.helpers.append(p)
        return p

    def call(
        self,
        name: str = "fakecall-e2e",
        seconds: float = 20,
        *,
        tone: bool = True,
        mic_feed: bool = True,
    ) -> list[subprocess.Popen[bytes]]:
        exe = self._fake(name)
        procs = [self.spawn([str(exe), "--record", "--target", "captation-test-mic", "/dev/null"])]
        if tone:
            wav = tone_file(self.work, 1000, seconds)
            procs.append(self.spawn([str(exe), "--playback", "--target", TEST_SINK, str(wav)]))
        if mic_feed:
            wav = tone_file(self.work, 1320, seconds)
            procs.append(self.spawn(["pw-play", "--target", TEST_MIC_INPUT, str(wav)]))
        return procs

    def hang_up(self, procs: list[subprocess.Popen[bytes]]) -> None:
        for p in procs:
            if p.poll() is None:
                p.terminate()
                p.wait(5)

    # ---- sessions ----------------------------------------------------------------------------

    def session_dirs(self) -> list[Path]:
        root = self.data / "unassigned"
        return sorted(root.glob("S-*")) if root.exists() else []

    def sessions(self) -> list[dict[str, Any]]:
        out = []
        for d in self.session_dirs():
            f = d / "session.json"
            if f.exists():
                out.append(json.loads(f.read_text(encoding="utf-8")))
        return out

    def final_sessions(self) -> list[dict[str, Any]]:
        return [s for s in self.sessions() if s["status"] != "recording"]

    def log_text(self) -> str:
        parts = [
            p.read_text(encoding="utf-8", errors="replace")
            for p in (self.work / "logs").glob("*.jsonl*")
        ]
        return "\n".join(parts)

    # ---- stub desktop helpers ----------------------------------------------------------------

    def countdown_notification(self) -> int | None:
        for i, n in enumerate(self.desk.notify.sent, start=1):
            if n["actions"][:1] == ["cancel"]:
                return i
        return None

    def tray_status(self) -> str | None:
        if not self.desk.watcher.items:
            return None
        name = self.desk.watcher.items[-1]
        try:
            return str(
                self.desk.get_prop(
                    name, "/StatusNotifierItem", "org.kde.StatusNotifierItem", "Status"
                )
            )
        except AssertionError:
            return None


def ensure_obs_real_env() -> None:
    """Start OBS from the test process (real session bus) so no test agent launches it with
    the stub D-Bus environment."""
    from captation.config import load_config
    from captation.obs import manage

    cfg = load_config(create_data_root=False)
    manage.ensure_running(cfg, manage.locate(cfg), timeout_s=40)


@contextmanager
def rig(desk: StubDesktop, password: Secret, **overrides: Any) -> Iterator[Rig]:
    ensure_obs_real_env()
    CACHE.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="e2e-", dir=CACHE))
    r = Rig(work, desk, password, **overrides)
    r.start()
    try:
        yield r
    finally:
        r.stop()
        keep = os.environ.get("CAPTATION_E2E_KEEP")
        if not keep:
            shutil.rmtree(work, ignore_errors=True)


def tcp_peers(pid: int) -> list[tuple[str, str, str]]:
    """(state, local, peer) TCP sockets of one process (``ss -tnp``)."""
    out = subprocess.run(["ss", "-tnpH"], capture_output=True, text=True, timeout=10).stdout
    socks = []
    for line in out.splitlines():
        if f"pid={pid}," in line:
            parts = line.split()
            socks.append((parts[0], parts[3], parts[4]))
    return socks


# OBS's bundled rtmp-services plugin fetches the service list (obsproject.com) and the Twitch /
# Amazon IVS ingest lists over HTTPS at every start (PF-023); those sockets then linger.
DOCUMENTED_OBS_HOSTS = ("obsproject.com", ".amazonaws.com", "twitch.tv", "justin.tv")


def documented_obs_peer(peer: str) -> bool:
    import socket

    host, _, port = peer.rpartition(":")
    if port != "443":
        return False
    try:
        name = socket.gethostbyaddr(host.strip("[]"))[0]
    except OSError:
        return False
    return name == "obsproject.com" or name.endswith(DOCUMENTED_OBS_HOSTS)


def restore_obs_after_crash() -> None:
    """Test-environment cleanup after a deliberate OBS crash (E7).

    After a crash OBS 33 waits in its Safe Mode dialog (PF-022, ADR-013). The product never
    touches OBS's crash marker; the test harness kills the waiting instance and removes the
    stale marker, which is what choosing "Run Normally" does, so later tests find a normal OBS.
    """
    subprocess.run(["pkill", "-9", "-x", "obs"], check=False)
    time.sleep(2)
    sentinel = Path.home() / ".var/app/com.obsproject.Studio/config/obs-studio/.sentinel"
    for marker in sentinel.glob("run_*"):
        marker.unlink()


def is_loopback(addr: str) -> bool:
    host = addr.rsplit(":", 1)[0].strip("[]")
    return host in {"127.0.0.1", "::1", "::ffff:127.0.0.1"}


def ensure_camera() -> bool:
    return os.path.exists(CAMERA) and os.access(CAMERA, os.W_OK)


__all__ = [
    "ROOT",
    "Rig",
    "ensure_camera",
    "is_loopback",
    "obs_password",
    "rig",
    "tcp_peers",
    "tone_file",
    "wait_for",
]
