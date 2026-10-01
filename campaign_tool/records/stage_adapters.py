"""Installed WP1 validators and promotion helpers for the detect, review, compare
and privacy stages.

Each stage's *content* is a JSON document registered through
``stages.set_content``; the validator installed here checks that the content
is the right schema for the stage, is bound to the subject, and that its
inner evidence (locators, rule ids, hashes) is consistent. The receipt that
promotes the stage is built by :func:`promote` from a passing challenge
record so that review, compare and privacy can only be promoted with an
independent ``reviews[]`` entry, exactly as ``ledger.stages`` demands.
"""
from datetime import datetime, timezone
import hashlib
import json

from campaign_tool.digest.schema import validate_digest
from .challenge import CLASSIFICATIONS, review_entry
from .ledger import stages

VERSION = "records-stage-content-v1"
SCHEMAS = {
    "detect": "records-detect-manifest-v1",
    "review": "records-review-digest-v1",
    "compare": "records-comparisons-v1",
    "privacy": "records-public-proposal-v1",
}
ADAPTER_PREFIX = {stage: "records-" + stage + "-content-v1" for stage in SCHEMAS}
STRUCTURED_DETECTORS = (
    "search-before-training", "purpose-quality", "external-sharing",
    "retention-over-policy", "audit-gap", "cpra-deadline", "volume-anomaly")


class StageContentError(ValueError):
    pass


def require(condition, reason):
    if not condition:
        raise StageContentError(reason)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def decode(raw):
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError) as error:
        raise StageContentError("content_not_json") from error
    require(isinstance(value, dict), "content_object_required")
    return value


def _validate_detect(content, subject):
    require(set(content) >= {"schema", "subject_sha256", "detector_version", "unit_count",
                             "text_leads", "structured"}, "detect_fields")
    require(type(content["unit_count"]) is int and content["unit_count"] >= 0, "detect_unit_count")
    require(isinstance(content["text_leads"], list), "detect_text_leads")
    for hit in content["text_leads"]:
        require(isinstance(hit, dict) and isinstance(hit.get("detector"), str) and
                isinstance(hit.get("locator"), dict), "detect_hit_shape")
    structured = content["structured"]
    require(isinstance(structured, dict) and set(structured) == set(STRUCTURED_DETECTORS), "detect_structured_set")
    for name, entry in structured.items():
        require(isinstance(entry, dict) and entry.get("status") in ("evaluated", "skipped", "blocked") and
                isinstance(entry.get("reason"), str), "detect_structured_entry:" + name)


def _validate_review(content, subject):
    digest = content.get("digest")
    require(isinstance(digest, dict), "review_digest_required")
    try:
        validate_digest(digest)
    except ValueError as error:
        raise StageContentError("review_digest_invalid: " + str(error)) from error
    require(digest["sha256"] == subject, "review_digest_subject")
    challenge = content.get("challenge")
    require(isinstance(challenge, dict) and challenge.get("kind") == "review" and
            challenge.get("verdict") in ("pass", "challenge"), "review_challenge_record")


def _validate_compare(content, subject):
    rows = content.get("comparisons")
    require(isinstance(rows, list), "comparisons_list")
    require(isinstance(content.get("event_date"), str), "compare_event_date")
    for row in rows:
        require(isinstance(row, dict) and isinstance(row.get("rule_id"), str) and
                row.get("classification") in CLASSIFICATIONS and
                row.get("confidence") in ("verified", "likely", "needs_attorney_review") and
                isinstance(row.get("primary_source"), dict) and
                isinstance(row.get("evidence"), list), "comparison_row")


def _validate_privacy(content, subject):
    require(isinstance(content.get("public_markdown"), str) and content["public_markdown"].strip(),
            "public_markdown_required")
    require(content.get("public_content_sha256") == sha(content["public_markdown"].encode()),
            "public_content_hash")
    manifest = content.get("manifest")
    require(isinstance(manifest, dict) and manifest.get("original_sha256") == subject, "manifest_binding")
    require(content.get("manifest_sha256") == sha(encoded(manifest)), "manifest_hash")


VALIDATORS = {"detect": _validate_detect, "review": _validate_review,
              "compare": _validate_compare, "privacy": _validate_privacy}


class _InstalledContentValidator:
    """Fixed per-stage checker. Identity is (stage, database) so bindings cannot drift."""

    def __init__(self, stage, database):
        self.stage, self.database = stage, str(database)

    @property
    def adapter_id(self):
        return ADAPTER_PREFIX[self.stage] + ":" + sha(self.database.encode())[:24]

    def __eq__(self, other):
        return type(other) is _InstalledContentValidator and (other.stage, other.database) == (self.stage, self.database)

    def __hash__(self):
        return hash((self.stage, self.database))

    def __call__(self, context):
        if context.get("stage") != self.stage:
            return False
        subject = context.get("subject_sha256")
        content = decode(context["content"])
        require(content.get("schema") == SCHEMAS[self.stage], "content_schema:" + self.stage)
        require(content.get("subject_sha256") == subject, "content_subject:" + self.stage)
        VALIDATORS[self.stage](content, subject)
        return True


def install_validators(database):
    """Trusted startup: register the four content validators; return stage -> adapter id."""
    registry = stages._INSTALLED_VALIDATORS
    result = {}
    for stage in SCHEMAS:
        binding = _InstalledContentValidator(stage, database)
        prior = registry.get(binding.adapter_id)
        require(prior is None or prior == binding, "installed_content_binding_conflict:" + stage)
        registry[binding.adapter_id] = binding
        result[stage] = binding.adapter_id
    return result


def current_states(database, subject):
    from .ledger import store
    with store.ledger(database, readonly=True) as con:
        return {row["stage"]: dict(row) for row in
                con.execute("SELECT * FROM stage_state WHERE original_sha256=?", (subject,))}


def promote(runner, subject, stage, content, *, author_id, coverage, locators, challenges=(),
            tier="A", reason=None):
    """Register content, claim the stage and promote it with a built receipt.

    ``challenges`` is a list of ``(role, challenge_record)`` pairs. Every record
    must have passed; a failed challenge makes this call promote the stage as
    ``blocked`` with the disputes recorded, never as ``done``.
    """
    require(stage in SCHEMAS, "unsupported_stage")
    content_bytes = encoded(content)
    states = current_states(runner.database, subject)
    if states[stage]["status"] == "done" and states[stage]["receipt_sha256"]:
        from .ledger import store
        with store.ledger(runner.database, readonly=True) as con:
            prior = con.execute("SELECT payload FROM stage_artifacts WHERE sha256=?",
                                (states[stage]["receipt_sha256"],)).fetchone()
            head = con.execute("SELECT content_sha256 FROM stage_content WHERE subject_sha256=? AND stage=? "
                               "ORDER BY revision DESC LIMIT 1", (subject, stage)).fetchone()
        if prior is not None and head is not None and head[0] == sha(content_bytes):
            return runner.promote(subject, stage, bytes(prior[0]))  # exact replay
    registered = runner.set_content(subject, stage, content_bytes, author_id=author_id, tier=tier)
    states = current_states(runner.database, subject)
    supersedes = states[stage]["receipt_sha256"]
    claim = runner.claim(subject, stage, supersedes=supersedes)
    failed = [(role, record) for role, record in challenges if record["verdict"] != "pass"]
    reviews = [] if failed else [review_entry(record, role, registered["content_sha256"], subject, locators)
                                 for role, record in challenges]
    inputs = {"original": subject}
    for prerequisite in stages.PREREQUISITES[stage]:
        inputs[prerequisite] = states[prerequisite]["receipt_sha256"]
    receipt = {
        "schema": "ledger-stage-receipt-v1", "subject_sha256": subject, "stage": stage,
        "content_sha256": registered["content_sha256"], "author_id": author_id, "tier": tier,
        "reviewer_id": (reviews[0]["reviewer_id"] if reviews else runner.owner),
        "role": stages.ROLES[stage], "verdict": "challenge" if failed else "pass",
        "coverage": coverage, "locators": list(locators),
        "rationale": reason or ("Stage content validated by installed " + ADAPTER_PREFIX[stage] +
                                ("; independent challenge passed" if reviews else "")),
        "model_or_tool": VERSION, "created_at_tz": datetime.now(timezone.utc).isoformat(),
        "input_hashes": inputs, "reviews": reviews,
    }
    if supersedes:
        receipt["supersedes"] = supersedes
    if failed:
        receipt["reason"] = "challenge_disputed:" + ";".join(
            f"{role}:{d.get('field')}[{d.get('index')}]:{d.get('reason')}" for role, record in failed
            for d in record["disputes"][:8])[:1000]
        receipt["challenges"] = [{"role": role, "disputes": record["disputes"]} for role, record in failed]
    result = runner.promote(subject, stage, encoded(receipt), claim_id=claim["claim_id"])
    return {**result, "content_sha256": registered["content_sha256"], "disputed": bool(failed)}
