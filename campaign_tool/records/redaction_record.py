"""Read-only reader for the private redaction record contract of docs/REDACTION.md (DOC-PUBLISH-1); it writes, signs and
approves nothing. Re-read here because tools/ and schemas/ do not ship in the wheel. ``<store>/agencies/<slug>/redaction/
<source_sha256>/`` holds PROPOSAL.json, SIGNOFF.json and ELIGIBILITY.json. A record's id is the SHA-256 of its ELIGIBILITY.json
bytes, which name the redacted derivative and its SHA-256, so the id pins the derivative bytes. Refusals never carry record text.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from .. import approval, privacy_patterns

MISMATCH, UNVERIFIED = "REDACTION_MISMATCH", "REDACTION_UNVERIFIED"
SLUG, SHA = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*"), re.compile(r"[0-9a-f]{64}")
MAX_RECORD, MAX_DERIVATIVE, REPO = 1 << 20, 32 << 20, Path(__file__).resolve().parents[2]


def _rx(pattern):
    return lambda v: type(v) is str and re.fullmatch(pattern, v) is not None


_SPAN = lambda i: (type(i) is dict and set(i) == {"category", "locator"} and i["category"] in ("email", "phone", "plate")  # noqa: E731
                   and type(i["locator"]) is dict and set(i["locator"]) == {"line", "offset", "length"}
                   and all(type(n) is int and n >= (k != "offset") for k, n in i["locator"].items()))
_HASH, _STAMP = _rx(SHA.pattern), _rx(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
_COMMON = {"schema_version": lambda v: type(v) is int and v == 1, "document_id": _rx(r"[A-Za-z0-9_-]{1,128}"),
           "source_sha256": _HASH}
_SIGNED = dict(_COMMON, proposal_sha256=_HASH, signed_by=lambda v: type(v) is str and v.strip() != "", signed_at=_STAMP)
SCHEMAS = {  # schemas/redaction-*-v1.json (closed vocabulary: every key required, no other key)
    "PROPOSAL.json": dict(_COMMON, redaction_required=lambda v: type(v) is bool, scan_receipt=_HASH, created_at=_STAMP,
                          proposed=lambda v: type(v) is list and all(_SPAN(i) for i in v),
                          not_detected_note=lambda v: type(v) is str and not privacy_patterns.categories(v)),
    "SIGNOFF.json": dict(_SIGNED, scope=lambda v: v == "flock-redaction",
                         signature=_rx(r"-----BEGIN SSH SIGNATURE-----[\s\S]+-----END SSH SIGNATURE-----\n?")),
    "ELIGIBILITY.json": dict(_SIGNED, derivative_sha256=_HASH, applied_at=_STAMP, derivative_path=lambda v: type(v) is str and v != ""),
}


class RedactionError(Exception):
    """args (code, reason): MISMATCH = no such record for this original; UNVERIFIED = it does not verify."""
    code = property(lambda self: self.args[0])
    reason = property(lambda self: self.args[1])


def _need(condition, code, reason):
    if not condition:
        raise RedactionError(code, reason)


def _or(reason, fn, *args, code=UNVERIFIED):  # fn(*args); any read, parse or verify failure becomes (code, reason)
    try:
        return fn(*args)
    except (OSError, ValueError, UnicodeError, TypeError, RecursionError, approval.ApprovalError):
        raise RedactionError(code, reason) from None


def record_dir(store, slug, source_sha256):  # the sole record location; invalid identifiers never become path components
    _need(isinstance(slug, str) and SLUG.fullmatch(slug) and isinstance(source_sha256, str) and SHA.fullmatch(source_sha256),
          MISMATCH, "source_mismatch")
    return Path(store) / "agencies" / slug / "redaction" / source_sha256


def _contained(store, literal):  # absolute, normalized, link-free, inside the store, outside this checkout
    root, path = Path(store), Path(literal) if isinstance(literal, str) and literal else None
    if not (path and "\x00" not in literal and path.is_absolute() and os.path.normpath(literal) == literal and root.is_absolute()) \
            or any(p.is_symlink() for p in (path, *path.parents)) or root.resolve() not in path.resolve().parents \
            or path.resolve() == REPO or REPO in path.resolve().parents:
        raise ValueError("unsafe_path")
    return path


def _bytes(path, limit):
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
        data = stream.read(limit + 1) if stat.S_ISREG(os.fstat(stream.fileno()).st_mode) else None
    if data is None or len(data) > limit:
        raise ValueError("not_regular_or_too_large")
    return data


def _load(folder, name):  # a 0600 regular file matching its closed schema; a duplicate key fails the schema (None)
    if stat.S_IMODE((folder / name).lstat().st_mode) != 0o600:
        raise ValueError("mode")
    raw = _bytes(folder / name, MAX_RECORD)
    value = json.loads(raw, object_pairs_hook=lambda pairs: dict(pairs) if len(dict(pairs)) == len(pairs) else None)
    if type(value) is not dict or set(value) != set(SCHEMAS[name]) or not all(SCHEMAS[name][k](value[k]) for k in value):
        raise ValueError("schema")
    return value, raw


def _owner_only(store, folder):  # every record directory within the private store, the store included, is 0700
    parts = (folder, *folder.parents)
    if any(p.is_symlink() or not p.is_dir() or stat.S_IMODE(p.stat().st_mode) != 0o700
           for p in parts[:parts.index(Path(store)) + 1]):
        raise ValueError("mode")


def verified_derivative(store, slug, source_sha256, record_id):
    """-> (derivative path, derivative bytes) of the applied record ``record_id`` for this original, verified afresh."""
    folder = record_dir(store, slug, source_sha256)
    raw = _or("no_record", lambda: _bytes(_contained(str(store), str(folder)) / "ELIGIBILITY.json", MAX_RECORD), code=MISMATCH)
    _need(isinstance(record_id, str) and hashlib.sha256(raw).hexdigest() == record_id, MISMATCH, "record_id_mismatch")
    applied, _ = _or("proposal_mismatch", _load, folder, "ELIGIBILITY.json")
    proposal, proposal_raw = _or("proposal_mismatch", _load, folder, "PROPOSAL.json")
    _need(applied["source_sha256"] == proposal["source_sha256"] == source_sha256, MISMATCH, "source_mismatch")
    _or("proposal_mismatch", _owner_only, store, folder)
    _need(proposal["redaction_required"], UNVERIFIED, "not_required")  # no derivative exists: nothing a site may publish
    signoff, _ = _or("unsigned", _load, folder, "SIGNOFF.json")
    _need((signoff["document_id"], signoff["source_sha256"], signoff["proposal_sha256"]) == (
        proposal["document_id"], source_sha256, hashlib.sha256(proposal_raw).hexdigest()), UNVERIFIED, "proposal_mismatch")
    principal = _or("signer_untrusted", lambda: approval.verify(approval.payload(
        "flock-redaction", proposal["document_id"], proposal_raw.decode("utf-8")), signoff["signature"]))
    _need(principal == signoff["signed_by"], UNVERIFIED, "signer_untrusted")
    _need(all(applied[k] == signoff[k] for k in ("document_id", "source_sha256", "proposal_sha256", "signed_by", "signed_at")),
          UNVERIFIED, "proposal_mismatch")
    derivative = _or("derivative_missing", _contained, str(store), applied["derivative_path"])
    data = _or("derivative_missing", _bytes, derivative, MAX_DERIVATIVE)
    _need(hashlib.sha256(data).hexdigest() == applied["derivative_sha256"], UNVERIFIED, "derivative_mismatch")
    return derivative, data
