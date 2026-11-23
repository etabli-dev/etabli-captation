"""Linux probe against recorded, sanitised pw-dump fixtures and a fake /proc tree."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from captation.platform import linux
from captation.platform.linux import (
    LinuxDiskEncryption,
    LinuxObsLocator,
    LinuxProbe,
    parse_pw_dump,
    video_device_users,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "linux"
pytestmark = pytest.mark.skipif(os.name != "posix", reason="Linux probe")


def load(name: str) -> dict[str, Any]:
    doc: dict[str, Any] = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    return doc


def stream(
    doc: dict[str, Any], cls: str = "Stream/Input/Audio"
) -> tuple[dict[str, Any], dict[str, Any]]:
    objs = {o["id"]: o for o in doc["pw_dump"]}
    for o in doc["pw_dump"]:
        props = o["info"]["props"]
        if props.get("media.class") == cls and o["info"].get("state") == "running":
            return props, objs[props["client.id"]]["info"]["props"]
    raise AssertionError("no running stream in fixture")


class FakeProc:
    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir()

    def add(
        self,
        pid: int,
        exe_name: str,
        *,
        ppid: int = 1,
        nspid: list[int] | None = None,
        fds: list[str] = (),
    ) -> None:  # type: ignore[assignment]
        d = self.root / str(pid)
        d.mkdir()
        (d / "exe").symlink_to(f"/usr/bin/{exe_name}")
        (d / "stat").write_text(f"{pid} ({exe_name[:15]}) S {ppid} 0 0\n")
        ns = " ".join(str(x) for x in (nspid or [pid]))
        (d / "status").write_text(f"Name:\t{exe_name[:15]}\nNSpid:\t{ns}\n")
        (d / "fd").mkdir()
        for i, target in enumerate(fds):
            (d / "fd" / str(i)).symlink_to(target)


@pytest.fixture
def proc(tmp_path: Path) -> FakeProc:
    return FakeProc(tmp_path / "proc")


@pytest.mark.req("R-03", "R-07")
def test_native_client_fakecall(proc: FakeProc) -> None:
    doc = load("fakecall")
    _, client = stream(doc)
    pid = int(client["pipewire.sec.pid"])
    proc.add(pid, "fakecall-e2e")
    mic, cam = parse_pw_dump(doc["pw_dump"], proc.root)
    assert [(r.pid, r.process_name) for r in mic] == [(pid, "fakecall-e2e")]
    assert cam == []


@pytest.mark.req("R-07")
def test_pulse_client_chrome_uses_app_pid_not_pipewire_pulse(proc: FakeProc) -> None:
    doc = load("browser_chrome")
    node, client = stream(doc)
    app_pid, sec_pid = int(node["application.process.id"]), int(client["pipewire.sec.pid"])
    assert app_pid != sec_pid
    proc.add(app_pid, "chrome")
    proc.add(sec_pid, "pipewire-pulse")
    mic, _ = parse_pw_dump(doc["pw_dump"], proc.root)
    assert [(r.pid, r.process_name, r.parent_app) for r in mic] == [
        (app_pid, "chrome", "Google Chrome input")
    ]


@pytest.mark.req("R-35")
def test_flatpak_obs_maps_to_host_pid_and_name(proc: FakeProc) -> None:
    doc = load("obs_mic")
    node, client = stream(doc)
    ns_pid = int(node["application.process.id"])
    host = 2702116
    proc.add(host, "obs", nspid=[host, ns_pid])
    proc.add(int(client.get("pipewire.sec.pid", 4627)), "pipewire-pulse")
    mic, _ = parse_pw_dump(doc["pw_dump"], proc.root)
    assert [(r.pid, r.process_name) for r in mic] == [(host, "obs")]
    probe = LinuxProbe(
        exclude_pids=frozenset({host}),
        runner=lambda argv, t: subprocess.CompletedProcess(argv, 0, json.dumps(doc["pw_dump"]), ""),
        proc=proc.root,
    )
    assert probe.snapshot().mic_users == ()


def test_idle_fixture_has_no_users(proc: FakeProc) -> None:
    assert parse_pw_dump(load("idle")["pw_dump"], proc.root) == ([], [])


@pytest.mark.req("R-02")
def test_camera_users_from_proc_fds(proc: FakeProc) -> None:
    doc = load("camera_v4l2")
    assert len(doc["video_users"]) == 2  # producer and consumer were recorded live
    proc.add(500, "ffmpeg", fds=["/dev/video42", "/dev/null"])
    proc.add(501, "bash", fds=["/dev/pts/1"])
    users = video_device_users(proc.root, uid=os.getuid())
    assert [(u.pid, u.process_name) for u in users] == [(500, "ffmpeg")]
    assert video_device_users(proc.root, uid=os.getuid() + 1) == []


@pytest.mark.req("R-02", "R-35")
def test_snapshot_camera_only_scanned_during_a_call(proc: FakeProc) -> None:
    call = load("fakecall")
    _, client = stream(call)
    pid = int(client["pipewire.sec.pid"])
    proc.add(pid, "fakecall-e2e")
    proc.add(600, "zoom", fds=["/dev/video0"])
    proc.add(601, "obs", fds=["/dev/video9"])

    def runner(doc: dict[str, Any]) -> Any:
        return lambda argv, t: subprocess.CompletedProcess(argv, 0, json.dumps(doc["pw_dump"]), "")

    snap = LinuxProbe(runner=runner(call), proc=proc.root, clock=lambda: 12.5).snapshot()
    assert snap.ts_monotonic == 12.5 and snap.confidence == "high"
    assert snap.camera_active and [u.process_name for u in snap.camera_users] == ["zoom"]
    assert snap.window_titles is None
    idle = LinuxProbe(runner=runner(load("idle")), proc=proc.root).snapshot()
    assert idle.mic_users == () and idle.camera_active is False


def test_pw_dump_failure_lowers_confidence(proc: FakeProc) -> None:
    def broken(argv: list[str], t: float) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(argv, t)

    snap = LinuxProbe(runner=broken, proc=proc.root).snapshot()
    assert snap.confidence == "low" and snap.mic_users == ()
    bad = LinuxProbe(runner=lambda a, t: subprocess.CompletedProcess(a, 1, "", "x"), proc=proc.root)
    assert bad.snapshot().confidence == "low"


def test_helper_process_maps_to_parent(proc: FakeProc) -> None:
    proc.add(700, "firefox")
    proc.add(701, "firefox-helper", ppid=700)
    node = {
        "id": 1,
        "type": "PipeWire:Interface:Node",
        "info": {
            "state": "running",
            "props": {
                "media.class": "Stream/Input/Audio",
                "application.process.id": 701,
                "application.process.binary": "firefox-helper",
            },
        },
    }
    mic, _ = parse_pw_dump([node], proc.root)
    assert mic[0].parent_app == "firefox"


def test_nodes_without_pid_or_not_running_are_ignored(proc: FakeProc) -> None:
    nodes = [
        {
            "id": 1,
            "type": "PipeWire:Interface:Node",
            "info": {
                "state": "idle",
                "props": {"media.class": "Stream/Input/Audio", "application.process.id": 5},
            },
        },
        {
            "id": 2,
            "type": "PipeWire:Interface:Node",
            "info": {"state": "running", "props": {"media.class": "Stream/Input/Audio"}},
        },
        {
            "id": 3,
            "type": "PipeWire:Interface:Node",
            "info": {"state": "running", "props": {"media.class": "Audio/Sink"}},
        },
    ]
    assert parse_pw_dump(nodes, proc.root) == ([], [])


@pytest.mark.req("R-46")
@pytest.mark.parametrize(
    ("findmnt", "lsblk", "expected"),
    [
        (
            "/dev/mapper/data_crypt ext4\n",
            {
                "blockdevices": [
                    {
                        "name": "data_crypt",
                        "type": "crypt",
                        "children": [{"name": "p1", "type": "part"}],
                    }
                ]
            },
            "encrypted",
        ),
        (
            "/dev/nvme1n1p2 ext4\n",
            {
                "blockdevices": [
                    {"name": "p2", "type": "part", "children": [{"name": "d", "type": "disk"}]}
                ]
            },
            "unencrypted",
        ),
        ("/dev/sda1 zfs\n", {"blockdevices": [{"name": "sda1", "type": "part"}]}, "unknown"),
        ("tmpfs tmpfs\n", {}, "unknown"),
        ("", {}, "unknown"),
    ],
)
def test_disk_encryption(
    tmp_path: Path, findmnt: str, lsblk: dict[str, Any], expected: str
) -> None:
    def runner(argv: list[str], t: float) -> subprocess.CompletedProcess[str]:
        if argv[0] == "findmnt":
            return subprocess.CompletedProcess(argv, 0 if findmnt else 1, findmnt, "")
        return subprocess.CompletedProcess(argv, 0, json.dumps(lsblk), "")

    assert LinuxDiskEncryption(runner).status(tmp_path / "not" / "yet") == expected


def test_locator_prefers_flatpak(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(argv: list[str], timeout: float = 10) -> subprocess.CompletedProcess[str]:
        if argv[:2] == ["flatpak", "info"]:
            return subprocess.CompletedProcess(argv, 0, "OBS Studio\n     Version: 33.0.3\n", "")
        return subprocess.CompletedProcess(argv, 0, "OBS Studio - 32.1.0\n", "")

    monkeypatch.setattr(linux, "_run", fake_run)
    monkeypatch.setattr(linux.shutil, "which", lambda name: f"/usr/bin/{name}")
    found = LinuxObsLocator().find()
    assert found is not None and found.kind == "flatpak" and found.version == "33.0.3"
    assert found.config_dir.endswith(".var/app/com.obsproject.Studio/config/obs-studio")
    native = LinuxObsLocator("/opt/obs/bin/obs").find()
    assert native is not None and native.kind == "native" and native.version == "32.1.0"
    assert native.config_dir.endswith("obs-studio")
