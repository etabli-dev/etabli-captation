"""Bootstrap, credentials, facts, firewall, launcher and manage (unit level)."""

from __future__ import annotations

import ipaddress
import json
import os
import socket
import subprocess
from pathlib import Path
from typing import Any

import pytest

from captation.models import ObsInstallation
from captation.obs import bootstrap, credentials, facts, firewall, launcher, manage
from captation.obs.client import ObsWebSocket
from tests.fakes import FakeObs
from tests.unit.test_obs_controller import cfg_with


class FakeKeyring:
    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, user: str) -> str | None:
        return self.store.get((service, user))

    def set_password(self, service: str, user: str, value: str) -> None:
        self.store[(service, user)] = value


@pytest.fixture
def keyring(monkeypatch: pytest.MonkeyPatch) -> FakeKeyring:
    fk = FakeKeyring()
    monkeypatch.setattr(credentials.keyring, "get_password", fk.get_password)
    monkeypatch.setattr(credentials.keyring, "set_password", fk.set_password)
    return fk


def inst(tmp_path: Path) -> ObsInstallation:
    return ObsInstallation(kind="native", command=("obs",), config_dir=str(tmp_path / "obs-studio"))


# ---- credentials -------------------------------------------------------------------------


@pytest.mark.req("R-34")
def test_credentials(tmp_path: Path, keyring: FakeKeyring, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = cfg_with(tmp_path).obs.websocket
    with pytest.raises(credentials.PasswordMissing):
        credentials.get_password(ws)
    monkeypatch.setenv("CAPTATION_OBS_PASSWORD", "from-env")
    assert credentials.get_password(ws) == "from-env"
    with pytest.raises(ValueError):
        credentials.store_password("short")
    pw = credentials.generate()
    assert len(pw) >= 32
    credentials.store_password(pw)
    assert credentials.get_password(ws) == pw and credentials.has_keyring_password()


# ---- websocket config ----------------------------------------------------------------------


@pytest.mark.req("R-31", "R-34")
def test_websocket_config_states(tmp_path: Path, keyring: FakeKeyring) -> None:
    cfg = cfg_with(tmp_path)
    i = inst(tmp_path)
    path = launcher.websocket_config_path(i)
    assert bootstrap.ensure_websocket_config(i, cfg, obs_running=True) == "obs_running"
    assert bootstrap.ensure_websocket_config(i, cfg, obs_running=False) == "written"
    doc = json.loads(path.read_text())
    assert doc["auth_required"] is True and doc["server_enabled"] is True
    assert doc["server_port"] == 4455 and len(doc["server_password"]) >= 32
    if os.name == "posix":  # Windows: ACLs, not mode bits
        assert path.stat().st_mode & 0o077 == 0
    assert bootstrap.ensure_websocket_config(i, cfg, obs_running=True) == "ok"
    doc["server_port"] = 4999
    path.write_text(json.dumps(doc))
    assert bootstrap.ensure_websocket_config(i, cfg, obs_running=True) == "obs_running"
    assert bootstrap.ensure_websocket_config(i, cfg, obs_running=False) == "fixed"
    assert list(path.parent.glob("config.json.bak-*"))  # backed up before the change
    # a config the user set up with a password we do not know is never overwritten
    path.write_text(
        json.dumps({"server_enabled": True, "auth_required": True, "server_password": "theirs"})
    )
    assert bootstrap.ensure_websocket_config(i, cfg, obs_running=False) == "needs_password"
    assert json.loads(path.read_text())["server_password"] == "theirs"


# ---- bootstrap -----------------------------------------------------------------------------


@pytest.mark.req("R-31", "R-22")
def test_bootstrap_creates_and_is_idempotent(tmp_path: Path) -> None:
    fake = FakeObs(
        profiles=["Untitled"],
        collections=["Untitled"],
        special={"desktop1": "Desktop Audio", "mic1": None},
    )
    cfg = cfg_with(tmp_path)
    report = bootstrap.BootstrapReport()
    run = bootstrap.Bootstrapper(
        cfg,
        fake,
        available_encoders={"obs_x264", "ffmpeg_pcm_s24le", "obs_nvenc_h264_tex"},
        os_name="linux",
        report=report,
    )  # type: ignore[arg-type]
    untitled_before = dict(fake.params["Untitled"])
    run.run()
    assert {"Captation-Audio", "Captation-Video"} <= set(fake.profiles)
    audio = fake.params["Captation-Audio"]
    assert audio[("Output", "Mode")] == "Advanced" and audio[("AdvOut", "RecFormat2")] == "mkv"
    assert audio[("AdvOut", "RecTracks")] == "7" and audio[("AdvOut", "RecRB")] == "false"
    assert audio[("AdvOut", "RecAudioEncoder")] == "ffmpeg_pcm_s24le"
    assert fake.params["Captation-Video"][("AdvOut", "RecEncoder")] == "obs_nvenc_h264_tex"
    assert fake.video["Captation-Audio"]["outputWidth"] == 128
    assert fake.scenes["Captation"] == ["Audio", "Video"]
    assert fake.special["desktop1"] is None  # global audio removed from our collection
    assert fake.params["Untitled"] == untitled_before
    assert any("warning" not in c for c in report.changes) and report.warnings
    second = bootstrap.BootstrapReport()
    bootstrap.Bootstrapper(
        cfg, fake, available_encoders=set(), os_name="linux", report=second
    ).run()  # type: ignore[arg-type]
    assert [c for c in second.changes if "RecAudioEncoder" not in c and "RecEncoder" not in c] == []


def test_bootstrap_refuses_while_recording(tmp_path: Path) -> None:
    fake = FakeObs()
    fake.recording = True
    with pytest.raises(bootstrap.BootstrapError):
        bootstrap.Bootstrapper(
            cfg_with(tmp_path),
            fake,
            available_encoders=set(),
            os_name="linux",
            report=bootstrap.BootstrapReport(),
        ).run()  # type: ignore[arg-type]


def test_encoder_and_scaling_helpers(tmp_path: Path) -> None:
    cfg = cfg_with(tmp_path)
    assert bootstrap.track_bitmask(cfg) == 0b111
    assert bootstrap.pick_audio_encoder(cfg, {"ffmpeg_flac"}) == "ffmpeg_flac"
    with pytest.raises(bootstrap.BootstrapError):
        bootstrap.pick_audio_encoder(cfg, {"unknown"})
    assert bootstrap.pick_video_encoder(cfg, {"obs_x264"}, "linux") == "obs_x264"
    assert bootstrap.pick_video_encoder(cfg, {"nothing"}, "linux") == "obs_x264"
    assert bootstrap.scaled_output(2560, 1440, 1080) == (1920, 1080)
    assert bootstrap.scaled_output(1280, 720, 1080) == (1280, 720)


def test_bootstrap_switches_to_existing_collection(tmp_path: Path) -> None:
    fake = FakeObs()
    fake.current_collection = "Untitled"
    fake.scenes["Captation"] = ["Audio"]
    fake.inputs["Captation Remote"] = {
        "kind": "x",
        "settings": {},
        "tracks": None,
        "monitor": None,
        "scene": "Audio",
    }
    report = bootstrap.BootstrapReport()
    b = bootstrap.Bootstrapper(
        cfg_with(tmp_path), fake, available_encoders=set(), os_name="linux", report=report
    )  # type: ignore[arg-type]
    b.ensure_collection()
    assert fake.current_collection == "Captation" and "Video" in fake.scenes["Captation"]
    assert "Captation Remote" not in fake.inputs


# ---- facts --------------------------------------------------------------------------------

LOG = """12:00:00.000: Available Encoders:
12:00:00.000:   Video Encoders:
12:00:00.000: 	- obs_x264 (x264)
12:00:00.000:   Audio Encoders:
12:00:00.000: 	- ffmpeg_pcm_s24le (FFmpeg PCM (24-bit))
12:00:00.000: ==== Startup complete ===
"""


@pytest.mark.linux  # Linux capture profile (macOS/Windows: tests/unit/test_*_kinds.py)
def test_facts_encoders_render_and_write(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    assert facts.available_encoders(tmp_path) == {"audio": set(), "video": set()}
    (logs / "2026-11-23.txt").write_text(LOG)
    enc = facts.available_encoders(tmp_path)
    assert enc == {"audio": {"ffmpeg_pcm_s24le"}, "video": {"obs_x264"}}
    data = facts.collect(FakeObs(), tmp_path)  # type: ignore[arg-type]
    assert data["obs_version"] == "33.0.3" and data["list_items"]
    section = facts.render_section("linux", data, {"Websocket bind": "all interfaces"})
    doc = tmp_path / "facts.md"
    doc.write_text("# Facts\n")
    facts.write_section(doc, "linux", section)
    facts.write_section(doc, "linux", section)
    text = doc.read_text()
    assert text.count("facts:linux:begin") == 1 and "Websocket bind" in text


# ---- firewall -----------------------------------------------------------------------------


@pytest.mark.linux  # nftables unit; macOS/Windows rules: test_*_firewall.py
@pytest.mark.req("R-32")
def test_firewall_rules_text() -> None:
    rules = firewall.nft_ruleset(4455)
    assert "tcp dport 4455 ip saddr != 127.0.0.0/8 drop" in rules
    assert "tcp dport 4455 ip6 saddr != ::1 drop" in rules
    assert rules.index("delete table") < rules.index("chain input")
    unit = firewall.systemd_unit("/usr/sbin/nft")
    assert "ExecStart=/usr/sbin/nft -f /etc/etabli-captation/obs-websocket-guard.nft" in unit
    assert "ExecStop=-/usr/sbin/nft delete table inet etabli_captation" in unit


@pytest.mark.linux
@pytest.mark.req("R-32")
def test_firewall_check_logic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rule = tmp_path / "guard.nft"
    monkeypatch.setattr(firewall, "RULE_FILE", rule)
    active = {"v": "inactive"}

    def fake_run(argv: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 0, stdout=active["v"] + "\n", stderr="")

    monkeypatch.setattr(firewall, "_run", fake_run)
    monkeypatch.setattr(firewall, "current_os", lambda: "linux")
    assert firewall.check(4455).ok is False
    rule.write_text(firewall.nft_ruleset(4455))
    active["v"] = "active"
    monkeypatch.setattr(firewall, "listening_publicly", lambda port: False)
    assert firewall.check(4455).ok is True
    monkeypatch.setattr(firewall, "listening_publicly", lambda port: True)
    monkeypatch.setattr(firewall, "local_addresses", lambda: ["192.0.2.10"])
    monkeypatch.setattr(firewall, "connect_from", lambda a, p: False)
    assert firewall.check(4455).ok is True
    monkeypatch.setattr(firewall, "connect_from", lambda a, p: True)
    status = firewall.check(4455)
    assert status.ok is False and "192.0.2.10" in " ".join(status.details)
    assert firewall.check(5000).ok is False  # rule for another port
    monkeypatch.setattr(firewall, "current_os", lambda: "macos")
    assert firewall.check(4455).ok is False


@pytest.mark.linux
def test_firewall_apply_and_remove_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    ran: list[list[str]] = []

    def fake_run(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[bytes]:
        ran.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(firewall.subprocess, "run", fake_run)
    firewall.apply(4455)
    assert ran[0][:2] == ["sudo", "install"] and ran[-1][-1] == firewall.UNIT_NAME
    ran.clear()
    firewall.remove()
    assert ran[0] == ["sudo", "systemctl", "disable", "--now", firewall.UNIT_NAME]
    assert ran[-1][-3:] == ["table", "inet", "etabli_captation"]

    def failing(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(argv, 1)

    monkeypatch.setattr(firewall.subprocess, "run", failing)
    with pytest.raises(RuntimeError):
        firewall.apply(4455)


@pytest.mark.linux
def test_firewall_probes_on_loopback() -> None:
    with socket.socket() as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen()
        port = srv.getsockname()[1]
        assert firewall.connect_from("127.0.0.1", port) is True
        assert firewall.connect_from("127.0.0.1", 1) is False
        # the real `ss`: a loopback-only listener is not public
        assert firewall.listening_publicly(port) is False
    # the real `ip -j addr`: only global-scope, non-loopback addresses
    for addr in firewall.local_addresses():
        assert not ipaddress.ip_address(addr).is_loopback
        assert not ipaddress.ip_address(addr).is_link_local


IP_ADDR_JSON = json.dumps(
    [
        {"ifname": "lo", "addr_info": [{"local": "127.0.0.1", "scope": "host"}]},
        {
            "ifname": "eth0",
            "addr_info": [
                {"local": "192.0.2.10", "scope": "global"},
                {"local": "fe80::1", "scope": "link"},
                {"local": "2001:db8::10", "scope": "global"},
            ],
        },
        {"ifname": "docker0", "addr_info": [{"local": "172.17.0.1", "scope": "global"}]},
        {"ifname": "lo2", "addr_info": [{"scope": "global"}]},  # no address: ignored
    ]
)
SS_LINES = {
    "public4": "LISTEN 0 4096 0.0.0.0:4455 0.0.0.0:*\n",
    "public6": "LISTEN 0 4096 *:4455 *:*\n",
    "lan": "LISTEN 0 4096 192.0.2.10:4455 0.0.0.0:*\n",
    "loopback": "LISTEN 0 4096 127.0.0.1:4455 0.0.0.0:*\nLISTEN 0 4096 [::1]:4455 [::]:*\n",
    "none": "",
}


@pytest.mark.linux
@pytest.mark.parametrize(
    ("ss_out", "public"),
    [("public4", True), ("public6", True), ("lan", True), ("loopback", False), ("none", False)],
)
def test_firewall_parses_ip_and_ss_output(
    monkeypatch: pytest.MonkeyPatch, ss_out: str, public: bool
) -> None:
    ran: list[list[str]] = []

    def fake_run(argv: list[str]) -> subprocess.CompletedProcess[str]:
        ran.append(argv)
        out = IP_ADDR_JSON if argv[0] == "ip" else SS_LINES[ss_out]
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")

    monkeypatch.setattr(firewall, "_run", fake_run)
    monkeypatch.setattr(firewall, "current_os", lambda: "linux")
    assert firewall.local_addresses() == ["192.0.2.10", "2001:db8::10", "172.17.0.1"]
    assert firewall.listening_publicly(4455) is public
    assert ["ss", "-ltnH", "sport = :4455"] in ran
    monkeypatch.setattr(
        firewall, "_run", lambda argv: subprocess.CompletedProcess(argv, 1, stdout="", stderr="x")
    )
    assert firewall.local_addresses() == []  # `ip` failed: nothing to probe


# ---- launcher / manage ------------------------------------------------------------------------


@pytest.mark.req("R-27", "R-34")
def test_launch_args_and_ports(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    args = launcher.launch_args(
        "Captation-Audio", "Captation", ["--verbose", "--websocket_password=x"]
    )
    assert "--disable-updater" in args and "--minimize-to-tray" in args
    assert args[args.index("--profile") + 1] == "Captation-Audio"
    assert not [a for a in args if "password" in a]
    with socket.socket() as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen()
        port = srv.getsockname()[1]
        assert launcher.port_open("localhost", port)
        assert launcher.wait_for_port("127.0.0.1", port, 1)
    assert launcher.wait_for_port("127.0.0.1", 1, 0.3) is False
    started: list[list[str]] = []
    envs: list[dict[str, str]] = []
    monkeypatch.setenv("CAPTATION_OBS_PASSWORD", "never-inherited")
    monkeypatch.setattr(
        launcher.subprocess,
        "Popen",
        lambda argv, **kw: (started.append(argv), envs.append(kw["env"])),
    )
    launcher.start(inst(tmp_path), profile=None, collection=None)
    assert started[0][0] == "obs"
    assert "CAPTATION_OBS_PASSWORD" not in envs[0] and "PATH" in envs[0]


def test_manage_profile_and_collection_detection(tmp_path: Path) -> None:
    i = inst(tmp_path)
    assert not manage.profile_exists(i, "Captation-Audio")
    prof = Path(i.config_dir) / "basic" / "profiles" / "CaptationAudio"
    prof.mkdir(parents=True)
    (prof / "basic.ini").write_text("[General]\nName=Captation-Audio\n")
    assert manage.profile_exists(i, "Captation-Audio")
    scenes = Path(i.config_dir) / "basic" / "scenes"
    scenes.mkdir(parents=True)
    (scenes / "Captation.json").write_text('{"name": "Captation"}')
    assert manage.collection_exists(i, "Captation")


def test_client_auth_string() -> None:
    # Known vector: sha256 chain over password+salt, then secret+challenge.
    value = ObsWebSocket.__module__ and __import__(
        "captation.obs.client", fromlist=["auth_string"]
    ).auth_string(
        "supersecretpassword",
        "lM1GncleQOaCu9lT1yeUZhFYnqhsLLP1G5lAGo3ixaI=",
        "+IxH4CnCiqpX1rM9scsNynZzbOe4KhDeYcTNS3PDaeY=",
    )
    assert value == "1Ct943GAT+6YQUUX47Ia/ncufilbe6+oD6lY+5kaCu4="
