"""Test-only approver: a throwaway SSH key trusted in-process by patching campaign_tool.approval.

Nothing here is reachable from a CLI or subprocess: production trust is the root-owned file only.
"""
import shutil
import subprocess
import tempfile
from pathlib import Path
from unittest import mock

try:
    from campaign_tool import approval
except ImportError:  # lets the failing-first tests run against code without the gate
    approval = None

KEYGEN = shutil.which("ssh-keygen") or "/usr/bin/ssh-keygen"


class Signer:
    def __init__(self, principal="owner@example.invalid", trusted=True):
        self.dir = Path(tempfile.mkdtemp(prefix="approval-test-"))
        self.principal = principal
        self.key = self.dir / "key"
        subprocess.run([KEYGEN, "-q", "-t", "ed25519", "-N", "", "-C", principal, "-f", str(self.key)], check=True)
        self.signers = self.dir / "allowed_signers"
        pub = (self.dir / "key.pub").read_text(encoding="utf-8").split()
        line = f'{principal} namespaces="deflock-approval" {pub[0]} {pub[1]}\n'
        self.signers.write_text(line if trusted else "", encoding="utf-8")

    def sign(self, data, namespace="deflock-approval"):
        msg = self.dir / "payload"
        msg.write_bytes(data)
        sig = self.dir / "payload.sig"
        sig.unlink(missing_ok=True)
        subprocess.run([KEYGEN, "-q", "-Y", "sign", "-f", str(self.key), "-n", namespace, str(msg)],
                       check=True, capture_output=True)
        return sig.read_text(encoding="utf-8")

    def trust(self, signers=None):
        """Patch the module trust anchor to this key's file (in-process only)."""
        stack = [mock.patch.object(approval, "TRUSTED_SIGNERS", Path(signers or self.signers)),
                 mock.patch.object(approval, "SSH_KEYGEN", Path(KEYGEN)),
                 mock.patch.object(approval, "_require_root", lambda path: None)]
        return _Many(stack)

    def close(self):
        shutil.rmtree(self.dir, True)


class Approvers:
    """Several trusted test principals in one allowed_signers file; start()/stop() around a test module."""

    def __init__(self, *principals):
        self.signers = {p: Signer(p) for p in principals}
        first = next(iter(self.signers.values()))
        self.file = first.dir / "combined_signers"
        self.file.write_text("".join(s.signers.read_text(encoding="utf-8") for s in self.signers.values()),
                             encoding="utf-8")
        self.patches = first.trust(self.file)

    def start(self):
        self.patches.__enter__()

    def stop(self):
        self.patches.__exit__(None, None, None)
        for s in self.signers.values():
            s.close()

    def evidence(self, box, key, who, round_=None):
        if who not in self.signers:
            return who  # an unsigned name: the outbox must refuse it
        return {"kind": "ssh", "signature": self.signers[who].sign(box.approval_payload(key, round_))}

    def approve(self, box, key, who):
        return box.approve(key, self.evidence(box, key, who))

    def approve_retry(self, box, key, who, approved_at=None):
        return box.approve_retry(key, self.evidence(box, key, who, box._round(key) + 1), approved_at)


class _Many:
    def __init__(self, patches):
        self.patches = patches

    def __enter__(self):
        for p in self.patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in reversed(self.patches):
            p.stop()
