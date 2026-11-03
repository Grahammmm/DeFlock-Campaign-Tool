"""Human approval as a verified signature, never as a typed name or a sentence.

An approval is an SSH signature (``ssh-keygen -Y sign -n deflock-approval``) over
the exact payload printed for review: the full text that would go out plus its
sha256. It counts only when it verifies against the root-owned trust file
``TRUSTED_SIGNERS`` and the root-owned ``SSH_KEYGEN``. An agent running as the
campaign user can neither edit the trust file nor substitute the verifier, so
it cannot approve its own proposal. There is no override argument, environment
variable or flag; tests patch this module in-process.

Setup (owner, once, with sudo; see docs/APPROVALS.md):
  install -d -o root -g root -m 0755 /etc/deflock
  install -o root -g root -m 0644 approvers.allowed_signers /etc/deflock/
"""
import hashlib
import os
import stat
import subprocess
import tempfile
from pathlib import Path

NAMESPACE = "deflock-approval"
TRUSTED_SIGNERS = Path("/etc/deflock/approvers.allowed_signers")
SSH_KEYGEN = Path("/usr/bin/ssh-keygen")
MAX_SIGNATURE = 8192
HEADER = "deflock-approval-v1"


class ApprovalError(Exception):
    """The evidence does not prove a human approver's decision on these bytes."""


def payload(scope, subject, content, round_=0):
    """The exact bytes the approver reads and signs: header lines, then the full content."""
    for value in (scope, subject):
        if not isinstance(value, str) or not value or any(c in value for c in "\r\n\0"):
            raise ApprovalError("invalid approval scope or subject")
    if not isinstance(content, str) or type(round_) is not int or round_ < 0:
        raise ApprovalError("invalid approval content")
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return (f"{HEADER}\nscope: {scope}\nsubject: {subject}\nround: {round_}\nsha256: {digest}\n"
            f"--- exact text being approved ---\n{content}").encode("utf-8")


def _require_root(path):
    """Refuse unless the path and every ancestor are root-owned and not group/other writable."""
    path = Path(path)
    if not path.is_absolute():
        raise ApprovalError("trust path must be absolute")
    try:
        leaf = os.lstat(path)
    except OSError as exc:
        raise ApprovalError(f"trust file missing: {path}") from exc
    if not stat.S_ISREG(leaf.st_mode):
        raise ApprovalError(f"trust path is not a regular file: {path}")
    for part in (path, *path.parents):
        st = os.lstat(part)
        if st.st_uid != 0 or st.st_mode & 0o022:
            raise ApprovalError(f"trust path is not root-owned and protected: {part}")


def verify(data, signature):
    """Return the verified principal for ``signature`` over ``data``, or raise ApprovalError."""
    if not isinstance(data, bytes) or not isinstance(signature, str) or len(signature) > MAX_SIGNATURE \
            or "-----BEGIN SSH SIGNATURE-----" not in signature:
        raise ApprovalError("approval requires an SSH signature, not a name")
    _require_root(TRUSTED_SIGNERS)
    _require_root(SSH_KEYGEN)
    with tempfile.TemporaryDirectory(prefix="deflock-approval-") as tmp:
        sig = Path(tmp) / "approval.sig"
        sig.write_text(signature, encoding="utf-8")
        try:
            found = subprocess.run([str(SSH_KEYGEN), "-Y", "find-principals", "-s", str(sig), "-f", str(TRUSTED_SIGNERS)],
                                   capture_output=True, text=True, timeout=30, check=False)
            names = found.stdout.split()
            if found.returncode != 0 or len(names) != 1:
                raise ApprovalError("signature is not from a trusted approver")
            done = subprocess.run([str(SSH_KEYGEN), "-Y", "verify", "-f", str(TRUSTED_SIGNERS), "-I", names[0],
                                   "-n", NAMESPACE, "-s", str(sig)], input=data, capture_output=True, timeout=30, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            raise ApprovalError("signature check could not run") from exc
    if done.returncode != 0:
        raise ApprovalError("signature does not match the approved text")
    return names[0]
