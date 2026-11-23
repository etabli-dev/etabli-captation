from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from captation import integrity
from captation.config import schema_errors
from captation.integrity import (
    IntegrityError,
    age_encrypt,
    make_read_only,
    sha256_file,
    verify_sha256sums,
    write_sha256sums,
)
from captation.postprocess import finalize_session, process_segment
from captation.storage import HostInfo, SessionRecord, new_session_id
from tests.fixtures.media import (
    POSIX,
    WRITE_PROTECTION_ENFORCED,
    make_config,
    make_container,
    needs_ffmpeg,
)

AGE = shutil.which("age")
AGE_KEYGEN = shutil.which("age-keygen")
needs_age = pytest.mark.skipif(AGE is None or AGE_KEYGEN is None, reason="age not installed")
SHA256SUM = shutil.which("sha256sum")


def _identity(tmp_path: Path) -> tuple[Path, str]:
    """A throw-away test key (tests only)."""
    key = tmp_path / "keys" / "test-identity.txt"
    key.parent.mkdir(parents=True)
    subprocess.run([str(AGE_KEYGEN), "-o", str(key)], check=True, capture_output=True)
    recipient = subprocess.run(
        [str(AGE_KEYGEN), "-y", str(key)], check=True, capture_output=True, text=True
    ).stdout.strip()
    return key, recipient


def _decrypt(path: Path, key: Path) -> bytes:
    return subprocess.run(
        [str(AGE), "--decrypt", "-i", str(key), str(path)], check=True, capture_output=True
    ).stdout


def test_sha256_file_matches_hashlib(tmp_path: Path) -> None:
    data = os.urandom(3 * integrity.CHUNK + 17)
    path = tmp_path / "blob"
    path.write_bytes(data)
    assert sha256_file(path) == hashlib.sha256(data).hexdigest()


@pytest.mark.req("R-44")
def test_sha256sums_roundtrip_and_tamper(tmp_path: Path) -> None:
    for name, payload in (("seg01_mix.flac", b"mix"), ("seg01_remote.flac", b"remote")):
        (tmp_path / name).write_bytes(payload)
    sums = write_sha256sums(tmp_path, ["seg01_mix.flac", "seg01_remote.flac"])
    lines = sums.read_text(encoding="utf-8").splitlines()
    assert lines[0] == f"{hashlib.sha256(b'mix').hexdigest()}  seg01_mix.flac"
    assert verify_sha256sums(tmp_path)
    if SHA256SUM and POSIX:
        proc = subprocess.run(
            [SHA256SUM, "-c", "SHA256SUMS"], cwd=tmp_path, capture_output=True, text=True
        )
        assert proc.returncode == 0, proc.stdout
    (tmp_path / "seg01_remote.flac").write_bytes(b"tampered")
    assert not verify_sha256sums(tmp_path)
    (tmp_path / "seg01_remote.flac").unlink()
    assert not verify_sha256sums(tmp_path)
    with pytest.raises(IntegrityError):
        write_sha256sums(tmp_path, ["../escape"])


@pytest.mark.req("R-44")
def test_make_read_only(tmp_path: Path) -> None:
    path = tmp_path / "seg01_mix.flac"
    path.write_bytes(b"x")
    make_read_only(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o444
    if WRITE_PROTECTION_ENFORCED:
        with pytest.raises(PermissionError):
            path.write_bytes(b"y")


@needs_age
@pytest.mark.req("R-47")
def test_age_round_trip(tmp_path: Path) -> None:
    key, recipient = _identity(tmp_path)
    plain = tmp_path / "seg01_mix.flac"
    payload = os.urandom(200_000)
    plain.write_bytes(payload)
    expected = hashlib.sha256(payload).hexdigest()

    out = age_encrypt(plain, [recipient], verify_identity=key)
    assert out.name == "seg01_mix.flac.age"
    assert not plain.exists()  # plaintext removed only after verification
    assert out.read_bytes().startswith(integrity.AGE_HEADER)
    decrypted = _decrypt(out, key)
    print("age round trip sha256:", expected, hashlib.sha256(decrypted).hexdigest())
    assert hashlib.sha256(decrypted).hexdigest() == expected


@needs_age
@pytest.mark.req("R-47")
def test_age_failure_keeps_plaintext(tmp_path: Path) -> None:
    key, _recipient = _identity(tmp_path)
    _other_key, other_recipient = _identity(tmp_path / "other")
    plain = tmp_path / "seg01_mix.flac"
    plain.write_bytes(b"audio" * 1000)
    # encrypted for a different recipient: decrypt-and-hash with our key fails
    with pytest.raises(IntegrityError):
        age_encrypt(plain, [other_recipient], verify_identity=key)
    assert plain.exists() and not (tmp_path / "seg01_mix.flac.age").exists()
    # an invalid recipient makes age itself fail
    with pytest.raises(IntegrityError):
        age_encrypt(plain, ["age1invalid"])
    assert plain.exists() and not (tmp_path / "seg01_mix.flac.age").exists()


@needs_age
@needs_ffmpeg
@pytest.mark.req("R-47", "R-44", "R-43")
def test_finalize_with_age_recipients(tmp_path: Path) -> None:
    key, recipient = _identity(tmp_path)
    cfg = make_config(tmp_path, {"storage.age_recipients": [recipient]})
    t0 = datetime(2026, 11, 23, 14, 15, 2).astimezone()
    rec = SessionRecord.create(
        cfg,
        session_id=new_session_id(t0, "a9e0"),
        app_id="signal",
        category="messenger",
        consent_ref="P-017",
        started_at=t0,
        host=HostInfo("linux", "test-os", "0.1.0", "33.0.3", "5.6.3"),
    )
    rec.begin_segment(1, "audio", "rule:default", t0, "none")
    rec.end_segment(1, t0 + timedelta(seconds=4))
    make_container(rec.directory / "seg01_container.mkv")
    process_segment(rec, 1, "audio", cfg)
    plain_hashes = {f.path: sha256_file(rec.directory / f.path) for _seg, f in rec.iter_files()}
    finalize_session(rec, cfg, status="complete")

    doc = json.loads(rec.session_file.read_text(encoding="utf-8"))
    assert schema_errors(doc, "session.schema.json") == []
    paths = [f["path"] for f in doc["segments"][0]["files"]]
    assert paths and all(p.endswith(".age") for p in paths)
    assert all(f["qc"] is not None for f in doc["segments"][0]["files"] if f["role"] == "mix")
    for name, digest in plain_hashes.items():
        assert not (rec.directory / name).exists()
        assert hashlib.sha256(_decrypt(rec.directory / f"{name}.age", key)).hexdigest() == digest
    assert verify_sha256sums(rec.directory)
    sums = (rec.directory / "SHA256SUMS").read_text(encoding="utf-8")
    assert all(line.endswith(".age") for line in sums.splitlines())
