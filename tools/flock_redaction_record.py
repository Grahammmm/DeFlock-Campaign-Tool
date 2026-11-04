"""Read-only redaction contract. Eligibility is not human publication approval."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import sys

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from campaign_tool import approval  # noqa: E402
_spec = importlib.util.spec_from_file_location("_redaction_privacy", REPO / "tools/flock_privacy.py")
privacy = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(privacy)


def record_dir(store, slug, source_sha256):
    """The sole record location; invalid identifiers never become path components."""
    if not isinstance(slug, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug) or not isinstance(
            source_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", source_sha256):
        raise ValueError("source_mismatch")
    return Path(store) / "agencies" / slug / "redaction" / source_sha256


def _contained(store, literal):
    """Apply agency_timeline's absolute/normalized/link-free output rules to reads."""
    if not isinstance(literal, str) or not literal or "\x00" in literal:
        raise ValueError("source_mismatch")
    root, path = Path(store), Path(literal)
    if not path.is_absolute() or os.path.normpath(literal) != literal or not root.is_absolute():
        raise ValueError("source_mismatch")
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("source_mismatch")
    real, base = path.resolve(), root.resolve()
    if base not in real.parents or real == REPO or REPO in real.parents:
        raise ValueError("source_mismatch")
    return path


def original_hash(store, locations):
    """Hash exactly one cited location. Missing, unreadable or ambiguous is not a hash."""
    try:
        places = set(locations)
        if len(places) != 1:
            return None
        path = _contained(store, places.pop())
        return _hash(path)
    except (OSError, ValueError, TypeError):
        return None


def _bytes(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("source_mismatch")
        return stream.read()


def _hash(path):
    return hashlib.sha256(_bytes(path)).hexdigest()


def _valid(value, rule):
    """The schemas use this small, closed JSON Schema vocabulary (no dependency)."""
    kind = rule.get("type")
    types = {"object": dict, "array": list, "string": str, "integer": int, "boolean": bool}
    if kind and type(value) is not types[kind]:
        return False
    if "const" in rule and (type(value) is not type(rule["const"]) or value != rule["const"]):
        return False
    if "enum" in rule and value not in rule["enum"]:
        return False
    if kind == "object":
        props = rule["properties"]
        return set(rule["required"]) <= set(value) <= set(props) and all(_valid(v, props[k]) for k, v in value.items())
    if kind == "array":
        return all(_valid(v, rule["items"]) for v in value)
    if kind == "string":
        return len(value) >= rule.get("minLength", 0) and ("pattern" not in rule or re.fullmatch(rule["pattern"], value) is not None)
    return kind != "integer" or value >= rule.get("minimum", 0)


def _record(folder, name, schema):
    path = folder / name
    if stat.S_IMODE(path.lstat().st_mode) != 0o600:
        raise ValueError("proposal_mismatch")
    raw = _bytes(path)
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("proposal_mismatch")
            result[key] = value
        return result
    rec = json.loads(raw, object_pairs_hook=pairs)
    rule = json.loads((REPO / "schemas" / (schema + "-v1.json")).read_text())
    if not _valid(rec, rule) or any(not rec[k].strip() for k in ("signed_by",) if k in rec):
        raise ValueError("proposal_mismatch")
    if "proposed" in rec and (privacy.categories(rec["not_detected_note"]) or any(
            item["category"] not in {name for name, _ in privacy.PATTERNS} for item in rec["proposed"])):
        raise ValueError("proposal_mismatch")
    return rec, raw


def eligibility(store, slug, source_sha256, document_bytes_sha256):
    """Return (eligible|ineligible|no_record, one stable reason), never record text."""
    bad = lambda reason: ("ineligible", reason)
    try:
        folder = _contained(store, str(record_dir(store, slug, source_sha256)))
        if not os.path.lexists(folder):
            return "no_record", "no_record"
        # All record ancestors within the private store must remain owner-only.
        for part in (folder, *folder.parents):
            if not part.is_dir() or stat.S_IMODE(part.stat().st_mode) != 0o700:
                return bad("proposal_mismatch")
            if part == Path(store):
                break
        proposal, raw = _record(folder, "PROPOSAL.json", "redaction-proposal")
        if proposal["source_sha256"] != source_sha256 or document_bytes_sha256 != source_sha256:
            return bad("source_mismatch")
        if not proposal["redaction_required"]:
            return "eligible", "not_required"
        if not os.path.lexists(folder / "SIGNOFF.json"):
            return bad("unsigned")
        try:
            signoff, _ = _record(folder, "SIGNOFF.json", "redaction-signoff")
        except (OSError, ValueError, UnicodeError):
            return bad("unsigned")
        digest = hashlib.sha256(raw).hexdigest()
        for key, expected in (("document_id", proposal["document_id"]), ("source_sha256", source_sha256),
                              ("proposal_sha256", digest)):
            if signoff[key] != expected:
                return bad("proposal_mismatch")
        try:
            principal = approval.verify(approval.payload("flock-redaction", proposal["document_id"], raw.decode("utf-8")),
                                        signoff["signature"])
        except approval.ApprovalError:
            return bad("signer_untrusted")
        if principal != signoff["signed_by"]:
            return bad("signer_untrusted")
        try:
            applied, _ = _record(folder, "ELIGIBILITY.json", "redaction-eligibility")
        except FileNotFoundError:
            return bad("derivative_missing")
        if any(applied[k] != signoff[k] for k in ("document_id", "source_sha256", "proposal_sha256", "signed_by", "signed_at")):
            return bad("proposal_mismatch")
        try:
            derivative = _contained(store, applied["derivative_path"])
            digest = _hash(derivative)
        except (OSError, ValueError):
            return bad("derivative_missing")
        if digest != applied["derivative_sha256"]:
            return bad("derivative_mismatch")
        return "eligible", "ok"
    except (OSError, ValueError, UnicodeError, TypeError):
        return bad("proposal_mismatch")
