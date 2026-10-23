"""Explicit preservation-only trust transport, never selected by a ledger row.

S3a owns production provenance of these pins. S2 callers/tests must supply the
original policy objects and their independently pinned hashes, not just a later
policy authorizing additional receipts. Generic ledger readers stay fail-closed.
"""
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from .mail_resolution_policy import ApprovedMailResolutionPolicy, sha
from . import original_resolution


@dataclass(frozen=True, slots=True)
class MailResolutionReadContext:
    output_root: Path
    policy: ApprovedMailResolutionPolicy | None = None
    historical_policies: Mapping[str, tuple[str, ApprovedMailResolutionPolicy]] | None = None

    def __post_init__(self):
        root = Path(self.output_root)
        if not root.is_absolute() or ".." in root.parts:
            raise ValueError("original_resolution_storage_binding")
        if self.policy is not None and type(self.policy) is not ApprovedMailResolutionPolicy:
            raise ValueError("resolution_policy_invalid")
        pinned = {}
        for subject, pair in dict(self.historical_policies or {}).items():
            if not sha(subject) or type(pair) is not tuple or len(pair) != 2:
                raise ValueError("resolution_policy_invalid")
            pin, policy = pair
            if not sha(pin) or type(policy) is not ApprovedMailResolutionPolicy or policy.sha256 != pin:
                raise ValueError("resolution_policy_mismatch")
            pinned[subject] = pair
        object.__setattr__(self, "output_root", root)
        object.__setattr__(self, "historical_policies", MappingProxyType(pinned))

    @property
    def fingerprint(self):
        return original_resolution.digest({"output_root": str(self.output_root),
            "policy": self.policy.sha256 if self.policy is not None else None,
            "historical_pins": {subject: pair[0] for subject, pair in (self.historical_policies or {}).items()}})

    def effective_original(self, con, subject):
        # Selection uses caller-provided subject->pin, NEVER row.policy_sha256.
        pair = (self.historical_policies or {}).get(subject)
        policy = pair[1] if pair is not None else self.policy
        try:
            return original_resolution.effective_original(con, subject,
                output_root=self.output_root, mail_policy=policy)
        except ValueError as exc:
            if policy is None and str(exc) == "resolution_policy_invalid":
                raise ValueError("resolution_policy_mismatch") from None
            raise
