"""SHA-256 checksums, read-only flags and optional age encryption (stage 10).

Encryption uses the ``age`` command-line tool through a subprocess (argv list, timeout).
``pyrage`` is not used because it ships without licence metadata and fails the licence gate.
"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
import shutil
import subprocess
from collections.abc import Iterable, Sequence
from pathlib import Path

log = logging.getLogger(__name__)

CHUNK = 1024 * 1024
SUMS_NAME = "SHA256SUMS"
AGE_SUFFIX = ".age"
AGE_HEADER = b"age-encryption.org/v1\n"
_AGE_BASE_TIMEOUT_S = 600.0
_AGE_BYTES_PER_S = 5 * 1024 * 1024  # very conservative throughput for the timeout budget


class IntegrityError(Exception):
    """A checksum, read-only or encryption step failed. The message holds no personal data."""


def sha256_file(path: Path) -> str:
    """Hex SHA-256 of a file, read in 1 MiB chunks."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write_text(target: Path, text: str, mode: int = 0o600) -> None:
    tmp = target.with_name(f".{target.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        if target.exists() and os.name == "nt":  # pragma: no cover - Windows read-only attribute
            os.chmod(target, 0o600)
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def write_sha256sums(
    directory: Path, names: Iterable[str], *, digests: dict[str, str] | None = None
) -> Path:
    """Write ``SHA256SUMS`` (GNU ``sha256sum`` text format) for the files ``names``.

    ``digests`` may supply already computed hashes; missing ones are computed. The file is
    replaced atomically. Returns its path.
    """
    lines = []
    for name in names:
        if "/" in name or "\\" in name or name in {"", ".", ".."}:
            raise IntegrityError("SHA256SUMS entries must be plain file names")
        hexdigest = (digests or {}).get(name) or sha256_file(directory / name)
        lines.append(f"{hexdigest}  {name}\n")
    target = directory / SUMS_NAME
    _atomic_write_text(target, "".join(lines))
    return target


def verify_sha256sums(directory: Path) -> bool:
    """Recompute every entry of ``SHA256SUMS``. False on any mismatch or missing file."""
    sums = directory / SUMS_NAME
    if not sums.is_file():
        return False
    ok = True
    for line in sums.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        hexdigest, sep, name = line.partition("  ")
        if not sep:
            hexdigest, sep, name = line.partition(" *")
        if not sep or not name:
            return False
        path = directory / name
        if not path.is_file() or sha256_file(path) != hexdigest.strip():
            ok = False
    return ok


def make_read_only(path: Path) -> None:
    """chmod 0444. On Windows this sets the read-only attribute (only the write bit counts)."""
    os.chmod(path, 0o444)


def age_available() -> bool:
    return shutil.which("age") is not None


def _age_timeout(size: int) -> float:
    return _AGE_BASE_TIMEOUT_S + size / _AGE_BYTES_PER_S


def age_encrypt(
    path: Path,
    recipients: Sequence[str],
    *,
    verify_identity: Path | None = None,
    remove_plaintext: bool = True,
) -> Path:
    """Encrypt ``path`` to ``<path>.age`` for ``recipients`` and return the new path.

    Verification before the plaintext is removed:

    - always: ``age`` exited 0, the output starts with the age v1 header and is at least as
      large as the plaintext;
    - with ``verify_identity`` (tests, or an operator holding a key): decrypt and compare the
      SHA-256 with the plaintext.

    The plaintext is removed only after successful verification (and only if
    ``remove_plaintext``). On any failure the partial output is removed, the plaintext is kept,
    and :class:`IntegrityError` is raised.
    """
    if not recipients:
        raise IntegrityError("age: no recipients")
    exe = shutil.which("age")
    if exe is None:
        raise IntegrityError("age: executable not found on PATH")
    out = path.with_name(path.name + AGE_SUFFIX)
    size = path.stat().st_size
    argv = [exe, "--encrypt"]
    for recipient in recipients:
        argv += ["-r", recipient]
    argv += ["-o", str(out), str(path)]
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            timeout=_age_timeout(size),
            check=False,
            stdin=subprocess.DEVNULL,
        )
        if proc.returncode != 0:
            raise IntegrityError(f"age: encryption failed (exit {proc.returncode})")
        with out.open("rb") as fh:
            header = fh.read(len(AGE_HEADER))
        if header != AGE_HEADER or out.stat().st_size < size:
            raise IntegrityError("age: output failed the header/size check")
        if verify_identity is not None:
            expected = sha256_file(path)
            actual = _age_decrypt_sha256(exe, out, verify_identity, _age_timeout(size))
            if actual != expected:
                raise IntegrityError("age: decrypt-and-hash verification failed")
    except subprocess.TimeoutExpired as exc:
        out.unlink(missing_ok=True)
        raise IntegrityError("age: timed out") from exc
    except BaseException:
        out.unlink(missing_ok=True)
        raise
    if remove_plaintext:
        if os.name == "nt":  # pragma: no cover - read-only attribute blocks unlink on Windows
            os.chmod(path, 0o600)
        path.unlink()
    return out


def _age_decrypt_sha256(exe: str, encrypted: Path, identity: Path, timeout: float) -> str:
    """Stream-decrypt ``encrypted`` with ``identity`` and return the plaintext SHA-256."""
    digest = hashlib.sha256()
    proc = subprocess.Popen(
        [exe, "--decrypt", "-i", str(identity), str(encrypted)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )
    stdout = proc.stdout
    assert stdout is not None
    try:
        for chunk in iter(lambda: stdout.read(CHUNK), b""):
            digest.update(chunk)
        rc = proc.wait(timeout=timeout)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        stdout.close()
    if rc != 0:
        raise IntegrityError(f"age: decryption failed (exit {rc})")
    return digest.hexdigest()
