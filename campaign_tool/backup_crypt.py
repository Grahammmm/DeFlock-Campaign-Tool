"""OpenPGP encryption of backup archives through the ``gpg`` binary (docs/BACKUP.md).

Encryption runs in a fresh empty private ``--homedir`` with ``--recipient-file``,
so the organizer's keyring is never read or changed and the private key never
has to be on the backup machine. Decryption uses the organizer's own GnuPG home
(or ``gnupg_home``). Error texts name the step, never a path.
"""
import contextlib
import os
import secrets
import shutil
import subprocess
import tempfile
from pathlib import Path

SHORT_TIMEOUT = 600
LONG_TIMEOUT = 6 * 3600


def _error(message):
    from .backup import BackupError
    return BackupError(message)


def gpg_path():
    found = shutil.which("gpg")
    if not found:
        raise _error("gpg not found; install GnuPG to write or read encrypted backups")
    return found


def _gpg(args, timeout=SHORT_TIMEOUT, stdout=subprocess.PIPE):
    try:
        return subprocess.run([gpg_path(), "--batch", "--no-tty", *args], stdin=subprocess.DEVNULL, stdout=stdout,
                              stderr=subprocess.PIPE, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        raise _error(f"gpg timed out ({args[-2] if len(args) > 1 else 'call'})") from None


def is_encrypted(path):
    """True for an OpenPGP packet stream: first byte has bit 0x80 set and bytes 257-262 are not ``ustar``."""
    with open(path, "rb") as handle:
        head = handle.read(263)
    return bool(head) and bool(head[0] & 0x80) and head[257:262] != b"ustar"


def _check_key_file(pubkey):
    pubkey = Path(pubkey)
    if pubkey.is_symlink() or not pubkey.is_file():
        raise _error("--encrypt-to must name a regular public key file (not missing, a link or a folder)")
    return pubkey


def recipient_ids(pubkey):
    """Return ``(fingerprint, key_ids)`` of the public key in ``pubkey``; refuse anything else."""
    pubkey = _check_key_file(pubkey)
    with tempfile.TemporaryDirectory(prefix="deflock-gpg-") as home:
        result = _gpg(["--homedir", home, "--no-autostart", "--with-colons", "--import-options", "show-only",
                       "--import", str(pubkey)])
    lines = [line.split(":") for line in result.stdout.decode("utf-8", "replace").splitlines()]
    if result.returncode != 0 or not any(f[0] == "pub" for f in lines):
        raise _error("--encrypt-to is not an OpenPGP public key")
    if sum(f[0] == "pub" for f in lines) != 1:
        raise _error("--encrypt-to must hold exactly one public key")
    key_ids = {f[4].upper() for f in lines if f[0] in ("pub", "sub") and len(f) > 4 and f[4]}
    fingerprints = [f[9].upper() for f in lines if f[0] == "fpr" and len(f) > 9]
    if not fingerprints or not key_ids:
        raise _error("--encrypt-to is not an OpenPGP public key")
    return fingerprints[0], key_ids


def encrypt(plain, out, pubkey):
    """Encrypt ``plain`` to ``pubkey`` into a new ``out`` (never replaced); returns the recipient fingerprint."""
    out = Path(out)
    fingerprint, key_ids = recipient_ids(pubkey)
    partial = out.parent / f".{out.name}.{secrets.token_hex(6)}.partial"
    try:
        with tempfile.TemporaryDirectory(prefix="deflock-gpg-") as home:
            fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as handle:
                result = _gpg(["--homedir", home, "--no-autostart", "--trust-model", "always",
                               "--recipient-file", str(pubkey), "--output", "-", "--encrypt", str(plain)],
                              timeout=LONG_TIMEOUT, stdout=handle)
            if result.returncode != 0:
                raise _error("gpg encryption failed")
            listed = _gpg(["--homedir", home, "--no-autostart", "--list-only", "--list-packets", str(partial)])
        packets = [line.split() for line in listed.stdout.decode("utf-8", "replace").splitlines()
                   if line.startswith(":pubkey enc packet:")]
        if listed.returncode != 0 or not any(word.upper() in key_ids for packet in packets for word in packet):
            raise _error("encrypted archive is not addressed to the --encrypt-to key")
        try:
            os.link(partial, out)
        except FileExistsError:
            raise _error("archive appeared during encryption; choose a new file name") from None
    finally:
        partial.unlink(missing_ok=True)
    return fingerprint


@contextlib.contextmanager
def decrypted(path, gnupg_home=None):
    """Yield a plain tar path decrypted from ``path`` inside a 0700 temp dir that is always removed."""
    home = ["--homedir", str(Path(gnupg_home).expanduser())] if gnupg_home else []
    tmp = tempfile.mkdtemp(prefix="deflock-decrypt-")
    try:
        os.chmod(tmp, 0o700)
        plain = Path(tmp) / "archive.tar"
        fd = os.open(plain, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            result = _gpg([*home, "--quiet", "--output", "-", "--decrypt", str(path)], timeout=LONG_TIMEOUT, stdout=handle)
        if result.returncode != 0:
            raise _error("cannot decrypt: no matching private key or wrong passphrase")
        yield plain
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
