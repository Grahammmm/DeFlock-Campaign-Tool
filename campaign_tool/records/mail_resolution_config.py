"""Owner-config-only policy selection for adoption and the read-only census.

S3b must call load_settings again under the root writer lock before any
recovery/write, and compare config_sha256 and context.fingerprint. No row,
receipt, command-line hash or policy artifact can add a historical trust pin.
"""
from dataclasses import dataclass
from datetime import datetime
import hashlib
import os
from pathlib import Path

from .ledger.mail_resolution_context import MailResolutionReadContext
from .ledger.mail_resolution_policy import ApprovedMailResolutionPolicy, decode, read_regular, sha

V1 = "mail-history-adoption-v1"
V2 = "mail-history-adoption-v2"
HISTORICAL = "legacy_resolution_historical_policy_pins"
CURRENT = "legacy_resolution_policy_sha256"
MAX_CONFIG = 2 * 1024 * 1024


def validate(config):
    fields = {"schema", "source", "adoption", "disable_marker_sha256"}
    if type(config) is not dict or config.get("schema") not in (V1, V2):
        raise ValueError("adoption_config_invalid")
    if config["schema"] == V2:
        fields.update((CURRENT, HISTORICAL))
    if (set(config) - fields or config.get("source") != "mail-history"
            or config.get("adoption") not in ("enabled", "disabled")
            or any(config.get(key) is not None and not sha(config[key])
                   for key in ("disable_marker_sha256", CURRENT))):
        raise ValueError("adoption_config_invalid")
    pins = config.get(HISTORICAL, [])
    if type(pins) is not list or len(pins) > 10000:
        raise ValueError("adoption_config_invalid")
    subjects, hashes = set(), []
    for pin in pins:
        if (type(pin) is not dict or set(pin) != {"policy_sha256", "introduced_at", "provenance_sha256", "originals"}
                or not sha(pin["policy_sha256"]) or not sha(pin["provenance_sha256"])
                or type(pin["introduced_at"]) is not str):
            raise ValueError("adoption_config_invalid")
        try:
            stamp = datetime.strptime(pin["introduced_at"], "%Y-%m-%dT%H:%M:%SZ")
            if stamp.strftime("%Y-%m-%dT%H:%M:%SZ") != pin["introduced_at"]:
                raise ValueError("adoption_config_invalid")
        except ValueError:
            raise ValueError("adoption_config_invalid") from None
        originals = pin["originals"]
        if (type(originals) is not list or not originals or not all(sha(s) for s in originals)
                or originals != sorted(set(originals)) or subjects.intersection(originals)):
            raise ValueError("adoption_config_invalid")
        subjects.update(originals)
        hashes.append(pin["policy_sha256"])
    if len(subjects) > 10000 or hashes != sorted(set(hashes)):
        raise ValueError("adoption_config_invalid")
    return config


def load_config(path):
    raw = read_regular(Path(os.path.abspath(path)), MAX_CONFIG, owner_only=True)
    config = validate(decode(raw))
    if config["schema"] == V1 and len(raw) > 64 * 1024:
        raise ValueError("adoption_config_invalid")
    return config, hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class ResolutionSettings:
    config_sha256: str | None
    context: MailResolutionReadContext

    @property
    def policy(self):
        return self.context.policy


def load_settings(root, config_path=None):
    """Securely reload the whole trust closure, without opening any database.

    Historical subject membership is explicitly pinned in the config; it is
    never inferred from the current policy's entries or a resolution row.
    """
    config, digest = load_config(config_path) if config_path is not None else ({}, None)
    pin = config.get(CURRENT)
    policy = ApprovedMailResolutionPolicy.load(root, expected_sha256=pin) if pin else None
    historical = {}
    for entry in config.get(HISTORICAL, []):
        old = ApprovedMailResolutionPolicy.load(root, expected_sha256=entry["policy_sha256"])
        admissions = decode(old.raw)["originals"]
        for subject in entry["originals"]:
            # A pinned policy must actually admit each explicitly pinned subject.
            value = admissions.get(subject)
            if value is None:
                raise ValueError("resolution_policy_mismatch")
            old.admission(subject, value["bytes"])
            historical[subject] = (entry["policy_sha256"], old)
    return ResolutionSettings(digest, MailResolutionReadContext(Path(root) / "intake",
                              policy=policy, historical_policies=historical))
