"""Owner-private immutable review bundles; no models, ledger writes or publication.

Installed Authority objects and their authenticator/key are trusted startup state.
Untrusted proposal/review JSON cannot choose its author, reviewer or owner.
"""
from contextlib import contextmanager
import errno
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import stat
import tempfile

from .gates import validate_findings as gate

VERSION = "wp8-review-bundle-v1"
MAX_JSON = 2 * 1024 * 1024
MAX_PUBLIC = 128 * 1024
MAX_ITEMS = 256
MAX_ARTIFACT = 64 * 1024 * 1024
MAX_TOTAL = 256 * 1024 * 1024
KINDS = ("originals", "units", "digests", "rules", "sources", "counterevidence")
ROLES = {"author", "factual", "legal", "privacy", "owner"}
PUBLIC_FIELDS = {"title", "claim", "limitations"}
PROPOSAL_FIELDS = {"proposal_id", "revision", "tier", "agency", "classification",
                   "next_action", "event_date", "primary_evidence", "rules",
                   "counterevidence", "missing_evidence", "exceptions_checked",
                   "bindings", "coverage", "supersedes"}


class BundleError(ValueError):
    pass


def require(condition, reason):
    if not condition:
        raise BundleError(reason)


def encoded(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=True, allow_nan=False) + "\n").encode()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def decode(raw, maximum=MAX_JSON):
    require(type(raw) is bytes and len(raw) <= maximum, "json_byte_bound")
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, "duplicate_json_key")
            result[key] = value
        return result
    try:
        return json.loads(raw, object_pairs_hook=pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError) as error:
        raise BundleError("invalid_json") from error


def _text(value, limit=4000):
    return type(value) is str and bool(value.strip()) and len(value) <= limit


def _private(path, directory=False):
    path = Path(path)
    require(path.is_absolute() and ".." not in path.parts, "canonical_absolute_path_required")
    require(not any(p.is_symlink() for p in (path, *path.parents)), "symlink_path")
    info = path.stat()
    require(info.st_uid == os.geteuid() and not info.st_mode & 0o077, "owner_only_path_required")
    require(stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode), "path_type")
    return path


def _root(path):
    path = _private(path, True)
    require(not any((p / ".git").exists() for p in (path, *path.parents)), "private_root_inside_repository")
    return path


def _read(path, maximum=MAX_JSON):
    path = _private(path)
    # O_NOFOLLOW closes the check-then-open window: a final-component symlink
    # swapped in after _private fails here, and fstat binds checks to the fd.
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as error:
        require(error.errno != errno.ELOOP, "symlink_path")
        raise
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode), "path_type")
        require(info.st_size <= maximum, "file_byte_bound")
        raw = stream.read(maximum + 1)
    require(len(raw) <= maximum, "file_byte_bound")
    return raw


def _sync(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextmanager
def _locked(root):
    root = _root(root)
    fd = os.open(root / "review-bundle.lock",
                 os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()
                and not info.st_mode & 0o077, "unsafe_lock")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield root
    finally:
        os.close(fd)


def _new(path, raw):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


class Authority:
    """Construct only at trusted startup; authenticate consumes an opaque session.

    The authenticator returns a registered principal key, not receipt-controlled
    identity. Signing keys are never serialized. Synthetic profiles cannot yield
    production review completion or owner handoff readiness.
    """
    def __init__(self, *, profile_id, principals, authenticate, signing_key, test_only=True):
        require(_text(profile_id, 200) and callable(authenticate), "trusted_profile_required")
        require(type(signing_key) is bytes and len(signing_key) >= 32, "signing_key_required")
        require(type(test_only) is bool and type(principals) is dict
                and 1 <= len(principals) <= 128, "invalid_principal_registry")
        registry, identities = {}, set()
        for key, value in principals.items():
            require(gate.agent_identity(key) is not None and type(value) is dict
                    and set(value) == {"identity", "provider", "model", "roles"}, "invalid_principal")
            identity = gate.agent_identity(value["identity"])
            require(identity is not None and identity not in identities, "duplicate_or_invalid_identity")
            require(_text(value["provider"], 200) and _text(value["model"], 200), "provider_model_required")
            require(type(value["roles"]) is list and value["roles"]
                    and all(type(role) is str and role in ROLES for role in value["roles"]), "invalid_roles")
            identities.add(identity)
            registry[key] = dict(identity=identity, provider=value["provider"],
                                 model=value["model"], roles=sorted(set(value["roles"])))
        self._registry = decode(encoded(registry))
        self._authenticate = authenticate
        self._key = signing_key
        self.test_only = test_only
        self.policy = {"profile_id": profile_id, "principals": registry,
                       "test_only": test_only, "adapter_version": VERSION,
                       "gate_source_sha256": gate.sha256(Path(gate.__file__))}
        self.policy_sha256 = sha(encoded(self.policy))

    def principal(self, context, role):
        try:
            key = self._authenticate(context)
        except Exception as error:
            raise BundleError("authentication_failed") from error
        require(type(key) is str and key in self._registry, "unregistered_authenticated_principal")
        actor = self._registry[key]
        require(role in actor["roles"], "authenticated_role_not_allowed")
        return decode(encoded(actor))

    def seal(self, body):
        raw = encoded(body)
        require(len(raw) <= MAX_JSON, "envelope_byte_bound")
        result = {"body": body, "signature": hmac.new(self._key, raw, hashlib.sha256).hexdigest()}
        require(len(encoded(result)) <= MAX_JSON, "envelope_byte_bound")
        return result

    def verify(self, envelope):
        require(type(envelope) is dict and set(envelope) == {"body", "signature"}
                and gate.valid_hash(envelope["signature"]), "invalid_signed_envelope")
        expected = hmac.new(self._key, encoded(envelope["body"]), hashlib.sha256).hexdigest()
        require(hmac.compare_digest(expected, envelope["signature"]), "authentication_signature_mismatch")
        body = envelope["body"]
        require(type(body) is dict and body.get("policy_sha256") == self.policy_sha256,
                "authority_policy_changed")
        return body


def _public(raw):
    value = decode(raw, MAX_PUBLIC)
    require(type(value) is dict and set(value) == PUBLIC_FIELDS, "public_field_allowlist")
    require(_text(value["title"], 500) and _text(value["claim"], 16000), "public_text_required")
    require(type(value["limitations"]) is list and len(value["limitations"]) <= 100
            and all(_text(item) for item in value["limitations"]), "public_limitations_invalid")
    return value


def _proposal(value):
    require(type(value) is dict and set(value) == PROPOSAL_FIELDS, "proposal_field_allowlist")
    require(len(encoded(value)) <= MAX_JSON, "proposal_byte_bound")
    require(_text(value["proposal_id"], 200) and type(value["revision"]) is int
            and 1 <= value["revision"] <= 1000000, "proposal_identity")
    require(value["tier"] in ("A", "B") and _text(value["agency"])
            and _text(value["next_action"]), "proposal_context")
    require(type(value["classification"]) is str and value["classification"] in gate.CLASSES,
            "proposal_classification")
    if value["tier"] == "A":
        require(value["classification"] in gate.TIER_A_CLASSES and value["rules"] == [],
                "tier_a_cannot_carry_legal_classification_or_rules")
    require(value["supersedes"] is None or gate.valid_hash(value["supersedes"]), "supersedes_hash")
    require(type(value["exceptions_checked"]) is bool, "exceptions_boolean")
    require(value["event_date"] is None or type(value["event_date"]) is str, "event_date_type")
    for field in ("primary_evidence", "rules", "counterevidence", "missing_evidence", "coverage"):
        require(type(value[field]) is list and len(value[field]) <= MAX_ITEMS, "bounded_proposal_lists")
    require(all(_text(item) for item in value["missing_evidence"]), "missing_evidence_type")
    bindings = value["bindings"]
    require(type(bindings) is dict and set(bindings) == set(KINDS), "binding_categories")
    all_items = []
    for kind in KINDS:
        items = bindings[kind]
        require(type(items) is list and len(items) <= MAX_ITEMS, "binding_item_bound")
        ids = set()
        for item in items:
            require(type(item) is dict and set(item) == {"id", "path", "sha256", "bytes"}, "binding_shape")
            require(_text(item["id"], 200) and item["id"] not in ids and gate.valid_hash(item["sha256"]),
                    "binding_identity")
            require(type(item["path"]) is str and Path(item["path"]).is_absolute()
                    and ".." not in Path(item["path"]).parts, "binding_path")
            require(type(item["bytes"]) is int and 0 <= item["bytes"] <= MAX_ARTIFACT, "binding_size")
            ids.add(item["id"])
            all_items.append(item)
    require(len(all_items) <= MAX_ITEMS and sum(item["bytes"] for item in all_items) <= MAX_TOTAL,
            "total_binding_bound")
    originals = {(item["path"], item["sha256"]) for item in bindings["originals"]}
    hashes = {kind: {item["sha256"] for item in bindings[kind]} for kind in KINDS}
    for item in value["primary_evidence"]:
        require(type(item) is dict and set(item) == {"path", "sha256", "locator", "observation", "coverage"},
                "primary_evidence_shape")
        require((item["path"], item["sha256"]) in originals and _text(item["locator"])
                and _text(item["observation"]) and item["coverage"] in gate.COVERAGE, "primary_evidence_binding")
    for rule in value["rules"]:
        require(type(rule) is dict and gate.valid_hash(rule.get("source_sha256"))
                and rule["source_sha256"] in hashes["rules"], "rule_byte_binding")
    for item in value["counterevidence"]:
        require(type(item) is dict and set(item) == {"sha256", "locator", "observation"}
                and item["sha256"] in hashes["counterevidence"]
                and _text(item["locator"]) and _text(item["observation"]), "counterevidence_binding")
    seen = set()
    for item in value["coverage"]:
        require(type(item) is dict and set(item) == {"original_sha256", "unit_sha256", "locator", "scope"},
                "coverage_shape")
        require(item["original_sha256"] in hashes["originals"] and item["unit_sha256"] in hashes["units"]
                and _text(item["locator"]) and item["scope"] in gate.COVERAGE, "coverage_binding")
        key = encoded(item)
        require(key not in seen, "duplicate_coverage")
        seen.add(key)
    return value


def _byte_blockers(proposal):
    blockers = []
    for kind in KINDS:
        if kind in ("originals", "units", "digests", "sources") and not proposal["bindings"][kind]:
            blockers.append("missing_" + kind + "_bindings")
        for item in proposal["bindings"][kind]:
            try:
                path = _private(item["path"])
                require(path.stat().st_size == item["bytes"], "artifact_size_changed")
                require(gate.sha256(path) == item["sha256"], "artifact_bytes_changed")
            except (OSError, BundleError):
                blockers.append("unavailable_or_changed_" + kind + "_binding")
    if not proposal["coverage"]:
        blockers.append("missing_exact_coverage")
    for kind, field in (("originals", "original_sha256"), ("units", "unit_sha256")):
        if {item["sha256"] for item in proposal["bindings"][kind]} != {item[field] for item in proposal["coverage"]}:
            blockers.append("incomplete_" + kind + "_coverage")
    if {item["sha256"] for item in proposal["bindings"]["counterevidence"]} != {item["sha256"] for item in proposal["counterevidence"]}:
        blockers.append("counterevidence_observations_incomplete")
    if proposal["missing_evidence"]:
        blockers.append("unresolved_evidence_gaps")
    return blockers


def create_bundle(root, public_content, proposal, *, authority, auth_context):
    actor = authority.principal(auth_context, "author")
    _public(public_content)
    proposal = _proposal(decode(encoded(proposal)))
    body = {"schema": VERSION, "policy_sha256": authority.policy_sha256, "author": actor,
            "public_content_sha256": sha(public_content), "proposal": proposal,
            "test_only": authority.test_only}
    envelope = authority.seal(body)
    raw = encoded(envelope)
    bundle_id = sha(raw)
    with _locked(root) as root:
        destination = root / bundle_id
        if destination.exists() or destination.is_symlink():
            _load(root, bundle_id, authority)
            require(_read(destination / "manifest.json") == raw and
                    _read(destination / "public_content.json", MAX_PUBLIC) == public_content, "bundle_replay_conflict")
            reused = True
        else:
            with tempfile.TemporaryDirectory(prefix=".review-bundle-", dir=root) as directory:
                stage = Path(directory)
                _new(stage / "manifest.json", raw)
                _new(stage / "public_content.json", public_content)
                (stage / "reviews").mkdir(mode=0o700)
                (stage / "owners").mkdir(mode=0o700)
                _sync(stage)
                stage.rename(destination)
            _sync(root)
            reused = False
    return {"bundle_id": bundle_id, "reused": reused, "status": "awaiting_reviews",
            "owner_approval": "awaiting_owner", "publication_ready": False, "stage_promotions": 0}


def _load(root, bundle_id, authority):
    require(gate.valid_hash(bundle_id), "invalid_bundle_id")
    directory = _private(root / bundle_id, True)
    raw = _read(directory / "manifest.json")
    require(sha(raw) == bundle_id, "bundle_manifest_changed")
    body = authority.verify(decode(raw))
    require(body.get("schema") == VERSION and body.get("test_only") is authority.test_only, "bundle_version")
    _proposal(body["proposal"])
    public = _read(directory / "public_content.json", MAX_PUBLIC)
    require(sha(public) == body["public_content_sha256"], "public_content_changed")
    _public(public)
    return directory, body, public


def _finding(directory, body, public):
    """Portable-gate finding for the proposed public state.

    privacy_status is hardcoded to "cleared" only so a privacy receipt can bind
    the exact proposed state digest. It is NOT a clearance: no privacy review has
    happened here, and an independent privacy-role review receipt is still
    required before this finding may be treated as cleared or published.
    """
    p = body["proposal"]
    return {"finding_id": p["proposal_id"], "agency": p["agency"], "classification": p["classification"],
            "claim": _public(public)["claim"], "author_agent": body["author"]["identity"],
            "next_action": p["next_action"], "event_date": p["event_date"],
            "primary_evidence": p["primary_evidence"], "rules": p["rules"],
            "counterevidence": p["counterevidence"], "missing_evidence": p["missing_evidence"],
            "exceptions_checked": p["exceptions_checked"], "privacy_status": "cleared",
            "publication_status": "draft", "reviews": [],
            "public_artifacts": [{"path": str(directory / "public_content.json"),
                                  "sha256": body["public_content_sha256"]}],
            "wp8_private_binding": body}


def review_target(root, bundle_id, *, authority):
    """Private review target. Its privacy_status="cleared" is a proposed gate state
    for receipts to bind, not actual clearance; a privacy-role review is still required."""
    with _locked(root) as root:
        directory, body, public = _load(root, bundle_id, authority)
        finding = _finding(directory, body, public)
        return {"bundle_id": bundle_id, "finding_digest": gate.digest(finding),
                "public_content_sha256": body["public_content_sha256"],
                "bindings_sha256": sha(encoded(body["proposal"]["bindings"])),
                "coverage": body["proposal"]["coverage"],
                "checked_locators": [{"path": e["path"], "sha256": e["sha256"], "locator": e["locator"]}
                                     for e in body["proposal"]["primary_evidence"]],
                "checked_public_artifacts": finding["public_artifacts"]}


def _append(directory, envelope):
    raw = encoded(envelope)
    name = sha(raw) + ".json"
    target = directory / name
    _private(directory, True)
    if target.exists() or target.is_symlink():
        require(_read(target) == raw, "receipt_replay_conflict")
        return name[:-5], True
    # Under the root lock, a fsynced temporary and rename expose only a full receipt.
    with tempfile.TemporaryDirectory(prefix=".receipt-", dir=directory) as temporary:
        staged = Path(temporary) / "receipt.json"
        _new(staged, raw)
        staged.rename(target)
    _sync(directory)
    return name[:-5], False


def _entries(directory, authority):
    entries = []
    with os.scandir(directory) as paths:
        for path in paths:
            # Abandoned private staging directories carry no committed receipt.
            if path.name.startswith(".receipt-"):
                require(path.is_dir(follow_symlinks=False), "invalid_receipt_staging")
                continue
            require(len(entries) < MAX_ITEMS and path.name.endswith(".json")
                    and gate.valid_hash(path.name[:-5]), "receipt_inventory")
            raw = _read(Path(path.path))
            require(sha(raw) == path.name[:-5], "receipt_bytes_changed")
            entries.append((path.name[:-5], authority.verify(decode(raw))))
    return sorted(entries, key=lambda row: row[0])


def _review_payload(payload, finding, body, bundle_id):
    expected = {"bundle_id", "finding_digest", "public_content_sha256", "bindings_sha256",
                "coverage", "checked_locators", "checked_public_artifacts",
                "role", "verdict", "rationale", "reviewed_at", "reviewed_primary"}
    require(type(payload) is dict and set(payload) == expected, "review_field_allowlist")
    require(payload["bundle_id"] == bundle_id and payload["finding_digest"] == gate.digest(finding)
            and payload["public_content_sha256"] == body["public_content_sha256"]
            and payload["bindings_sha256"] == sha(encoded(body["proposal"]["bindings"])), "stale_review_binding")
    require(encoded(payload["coverage"]) == encoded(body["proposal"]["coverage"]), "exact_receipt_coverage_required")
    locators = [{"path": e["path"], "sha256": e["sha256"], "locator": e["locator"]}
                for e in body["proposal"]["primary_evidence"]]
    require(payload["checked_locators"] == locators, "exact_checked_locators_required")
    require(payload["checked_public_artifacts"] == finding["public_artifacts"], "exact_public_artifacts_required")
    require(type(payload["role"]) is str and payload["role"] in gate.ROLES
            and payload["verdict"] in ("pass", "challenge", "blocked")
            and _text(payload["rationale"]) and gate.reviewed_timestamp(payload["reviewed_at"])
            and payload["reviewed_primary"] is True, "review_evidence_required")


def record_review(root, bundle_id, payload, *, authority, auth_context):
    payload = decode(encoded(payload))
    require(type(payload) is dict and type(payload.get("role")) is str
            and payload["role"] in gate.ROLES, "review_role")
    actor = authority.principal(auth_context, payload["role"])
    with _locked(root) as root:
        directory, body, public = _load(root, bundle_id, authority)
        require(actor["identity"] != body["author"]["identity"], "self_review_rejected")
        finding = _finding(directory, body, public)
        _review_payload(payload, finding, body, bundle_id)
        envelope = authority.seal({"kind": "review", "bundle_id": bundle_id,
                                   "policy_sha256": authority.policy_sha256,
                                   "actor": actor, "payload": payload})
        identity, reused = _append(directory / "reviews", envelope)
        return {"receipt_id": identity, "reused": reused, "publication_ready": False}


def _assess(root, bundle_id, authority):
    directory, body, public = _load(root, bundle_id, authority)
    finding = _finding(directory, body, public)
    blockers = _byte_blockers(body["proposal"])
    reviews, ids = [], []
    for identity, item in _entries(directory / "reviews", authority):
        require(item.get("kind") == "review" and item.get("bundle_id") == bundle_id, "receipt_bundle_mismatch")
        actor, payload = item["actor"], item["payload"]
        _review_payload(payload, finding, body, bundle_id)
        require(actor["identity"] != body["author"]["identity"] and payload["role"] in actor["roles"],
                "receipt_authority_mismatch")
        reviews.append({**payload, "finding_id": finding["finding_id"], "reviewer_agent": actor["identity"]})
        ids.append(identity)
    # Reuse every existing structural/content/source check. Never synthesize a pass.
    report = gate.gate(finding, reviews, check_files=True, tier=body["proposal"]["tier"])
    blockers.extend(report["blockers"])
    review_set = sha(encoded(ids))
    owner_state = "awaiting_owner"
    for _, item in _entries(directory / "owners", authority):
        require(item.get("kind") == "owner" and item.get("bundle_id") == bundle_id
                and "owner" in item["actor"]["roles"], "owner_authority_mismatch")
        payload = item["payload"]
        require(payload["public_content_sha256"] == body["public_content_sha256"]
                and payload["finding_digest"] == report["finding_digest"], "stale_owner_content")
        if payload["review_set_sha256"] != review_set:
            blockers.append("owner_approval_review_set_changed")
        elif payload["decision"] == "reject":
            blockers.append("owner_rejected")
        elif payload["decision"] == "approve":
            owner_state = "approved"
        else:
            blockers.append("invalid_owner_decision")
    blockers = sorted(set(blockers))
    reviewed = not blockers
    if blockers:
        owner_state = "blocked" if owner_state == "approved" else owner_state
    return {"bundle_id": bundle_id, "finding_digest": report["finding_digest"],
            "public_content_sha256": body["public_content_sha256"], "review_set_sha256": review_set,
            "review_receipts": len(ids), "blockers": blockers, "review_complete": reviewed,
            "production_review_complete": reviewed and not authority.test_only,
            "owner_approval": owner_state, "test_only": authority.test_only,
            "handoff_ready": reviewed and owner_state == "approved" and not authority.test_only,
            "publication_ready": False, "stage_promotions": 0}


def assess_bundle(root, bundle_id, *, authority):
    with _locked(root) as root:
        return _assess(root, bundle_id, authority)


def record_owner_decision(root, bundle_id, payload, *, authority, auth_context):
    actor = authority.principal(auth_context, "owner")
    payload = decode(encoded(payload))
    require(type(payload) is dict and set(payload) == {"bundle_id", "finding_digest", "public_content_sha256",
            "review_set_sha256", "decision", "rationale", "decided_at"}, "owner_field_allowlist")
    require(payload["decision"] in ("approve", "reject") and _text(payload["rationale"])
            and gate.reviewed_timestamp(payload["decided_at"]), "owner_decision_invalid")
    with _locked(root) as root:
        assessment = _assess(root, bundle_id, authority)
        require(all(payload[key] == assessment[key] for key in
                    ("bundle_id", "finding_digest", "public_content_sha256", "review_set_sha256")),
                "stale_owner_binding")
        require(payload["decision"] == "reject" or assessment["review_complete"], "reviews_not_complete")
        directory = root / bundle_id
        envelope = authority.seal({"kind": "owner", "bundle_id": bundle_id,
                                   "policy_sha256": authority.policy_sha256,
                                   "actor": actor, "payload": payload})
        identity, reused = _append(directory / "owners", envelope)
        return {"receipt_id": identity, "reused": reused, "publication_ready": False}
