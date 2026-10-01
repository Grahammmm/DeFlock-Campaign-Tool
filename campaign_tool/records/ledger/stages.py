"""Generic stage receipt invariants; domain adapters and publication are separate."""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
import sqlite3
import uuid
from types import MappingProxyType

from . import store
from .migrations import stages_v001 as migration
from .migrations import stages_authority_v001 as authority

STAGES = ("preserve", "extract", "catalog", "detect", "review", "compare", "privacy")
ROLES = dict(zip(STAGES, ("preserver", "extractor", "cataloger", "detector",
                         "factual", "legal", "privacy")))
PREREQUISITES = {
 "preserve": {}, "extract": {"preserve": ("done",)},
 "catalog": {"extract": ("done", "blocked", "inapplicable")},
 "detect": {"catalog": ("done",)}, "review": {"catalog": ("done",)},
 "compare": {"review": ("done",)},
 "privacy": {"review": ("done",), "compare": ("done", "inapplicable")},
}
DEPENDENTS = {
 "extract": ("extract", "catalog", "detect", "review", "compare", "privacy"),
 "catalog": ("catalog", "detect", "review", "compare", "privacy"),
 "detect": ("detect", "compare", "privacy"),
 "review": ("review", "compare", "privacy"), "compare": ("compare", "privacy"),
 "privacy": ("privacy",),
}
MAX_CONTENT_BYTES = 4 * 1024 * 1024
MAX_RECEIPT_BYTES = 64 * 1024
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")


class StageError(ValueError):
    pass


def _hash(value):
    return hashlib.sha256(value).hexdigest()


def _require(value, reason):
    if not value:
        raise StageError(reason)


def _identity(value):
    _require(isinstance(value, str) and _ID.fullmatch(value), "invalid_identity")
    return value.casefold()


def _sha(value):
    _require(isinstance(value, str) and _SHA.fullmatch(value), "invalid_hash")
    return value


def _time(value):
    _require(isinstance(value, str), "invalid_timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise StageError("invalid_timestamp") from exc
    _require(parsed.tzinfo is not None and parsed.utcoffset() is not None, "timezone_required")
    return parsed


def _decode(payload):
    def pairs(items):
        result = {}
        for key, value in items:
            _require(key not in result, "duplicate_json_key")
            result[key] = value
        return result
    def constant(value):
        raise StageError("nonfinite_json")
    try:
        result = json.loads(payload, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise StageError("malformed_json") from exc
    _require(isinstance(result, dict), "receipt_object_required")
    return result


def _statements(sql=None):
    value = ""
    for character in (migration.SQL if sql is None else sql):
        value += character
        if character == ";" and sqlite3.complete_statement(value):
            yield value.strip().rstrip(";")
            value = ""
    _require(not value.strip(), "invalid_migration")


class _Writer:
    def __init__(self, connection):
        self.connection, self.pending = connection, None
        connection.create_function("stage_append_allowed", -1,
                                   lambda *values: int(self.pending == values))

    @contextmanager
    def allow(self, table, values):
        _require(self.pending is None, "nested_write")
        self.pending = (table, *values)
        try:
            yield
        finally:
            self.pending = None

    def insert(self, table, values):
        columns = (migration.TABLES if table in migration.TABLES else authority.TABLES)[table]
        with self.allow(table, values):
            self.connection.execute(
                "INSERT INTO " + table + "(" + ",".join(columns) + ") VALUES(" +
                ",".join("?" for _ in columns) + ")", values)


def _install_base(connection):
    marker, checksum = "extension:" + migration.NAME, _hash(migration.SQL.encode())
    current = connection.execute("SELECT value FROM ledger_meta WHERE key=?", (marker,)).fetchone()
    if current:
        _require(current[0] == checksum, "controller_schema_mismatch")
        for sql in _statements():
            match = re.match(r"CREATE (TABLE|INDEX|TRIGGER) (\w+)", sql)
            _require(match is not None, "unsupported_schema")
            kind, name = match.groups()
            row = connection.execute("SELECT sql FROM sqlite_master WHERE type=? AND name=?",
                                     (kind.lower(), name)).fetchone()
            _require(row is not None and row[0].strip().rstrip(";") == sql,
                     "controller_protection_missing")
        return
    _require(connection.execute(
        "SELECT 1 FROM stage_state WHERE status IN ('done','inapplicable') LIMIT 1"
    ).fetchone() is None, "uncontrolled_stage_history_requires_migration")
    for sql in _statements():
        connection.execute(sql)
    connection.execute("INSERT INTO ledger_meta VALUES(?,?)", (marker, checksum))


def _install(connection):
    _install_base(connection)
    marker, checksum = "extension:" + authority.NAME, _hash(authority.SQL.encode())
    current = connection.execute("SELECT value FROM ledger_meta WHERE key=?", (marker,)).fetchone()
    if current:
        _require(current[0] == checksum, "authority_schema_mismatch")
        for sql in _statements(authority.SQL):
            kind, name = re.match(r"CREATE (TABLE|INDEX|TRIGGER) (\w+)", sql).groups()
            row = connection.execute("SELECT sql FROM sqlite_master WHERE type=? AND name=?",
                                     (kind.lower(), name)).fetchone()
            _require(row is not None and row[0].strip().rstrip(";") == sql, "authority_protection_missing")
        return
    _require(connection.execute("SELECT 1 FROM stage_transitions LIMIT 1").fetchone() is None,
             "preauthority_acceptance_requires_migration")
    for sql in _statements(authority.SQL):
        connection.execute(sql)
    connection.execute("INSERT INTO ledger_meta VALUES(?,?)", (marker, checksum))


@contextmanager
def _transaction(database):
    with store.ledger(database) as connection:
        _require(connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1, "foreign_keys_required")
        writer = _Writer(connection)
        connection.execute("BEGIN IMMEDIATE")
        try:
            _install(connection)
            yield connection, writer
            connection.commit()
        except BaseException:
            connection.rollback()
            raise


# Installed application code owns these registries. Receipt submissions cannot
# register callables. This generic slice ships no production domain adapters.
_INSTALLED_VALIDATORS = {}
_INSTALLED_PROFILES = {}
_RUNNER_TOKEN = object()


def configure_installed_profile(profile_id, *, engine_version, config_sha256, validators):
    """Trusted startup only: resolve stage -> preinstalled adapter ID, not callable."""
    _identity(profile_id)
    _sha(config_sha256)
    _require(isinstance(engine_version, str) and engine_version.strip(), "engine_version_required")
    _require(isinstance(validators, dict) and all(stage in STAGES for stage in validators),
             "invalid_stage_adapter_map")
    resolved = {}
    for stage, adapter_id in validators.items():
        _require(isinstance(adapter_id, str) and adapter_id in _INSTALLED_VALIDATORS,
                 "unregistered_installed_validator")
        resolved[stage] = (adapter_id, _INSTALLED_VALIDATORS[adapter_id])
    profile = (engine_version, config_sha256, MappingProxyType(resolved))
    _require(profile_id not in _INSTALLED_PROFILES or _INSTALLED_PROFILES[profile_id] == profile,
             "installed_profile_is_immutable")
    _INSTALLED_PROFILES[profile_id] = profile


class StageRunner:
    """Trusted, configured execution authority, separate from receipt data."""

    def __init__(self, token, database, run_id, owner, profile_id, engine, config, validators, test_only):
        _require(token is _RUNNER_TOKEN, "runner_factory_required")
        self.database, self.run_id, self.owner = database, run_id, _identity(owner)
        self.profile_id, self.engine_version, self.config_sha256 = profile_id, engine, config
        self.validators, self.test_only = MappingProxyType(dict(validators)), bool(test_only)
        descriptor = {"profile_id": profile_id, "engine_version": engine, "config_sha256": config,
                      "test_only": self.test_only,
                      "validators": {stage: item[0] for stage, item in validators.items()}}
        self.profile_sha256 = _hash(store.canonical(descriptor).encode())
        with _transaction(database) as (c, writer):
            self._check_run(c)
            values = (run_id, profile_id, self.profile_sha256, engine, config, int(self.test_only))
            old = c.execute("SELECT * FROM stage_runner_bindings WHERE run_id=?", (run_id,)).fetchone()
            if old:
                _require(tuple(old)[:6] == values, "run_profile_substitution")
            else:
                writer.insert("stage_runner_bindings", (*values, store.now()))

    def _check_run(self, c):
        run = c.execute("SELECT * FROM runs WHERE run_id=?", (self.run_id,)).fetchone()
        _require(run is not None and run["kind"] in ("stage-runner", "records-runner") and
                 run["status"] == "running" and run["ended_at"] is None, "live_runner_run_required")
        _require(run["engine_version"] == self.engine_version and
                 run["config_sha256"] == self.config_sha256, "run_config_version_mismatch")
        _require(_time(run["started_at"]) <= datetime.now(timezone.utc), "run_not_started")

    def _check(self, c):
        self._check_run(c)
        binding = c.execute("SELECT * FROM stage_runner_bindings WHERE run_id=?", (self.run_id,)).fetchone()
        _require(binding is not None and binding["profile_sha256"] == self.profile_sha256 and
                 binding["test_only"] == int(self.test_only), "run_profile_substitution")

    def set_content(self, subject, stage, content, *, author_id, tier):
        return set_content(self, subject, stage, content, author_id=author_id, tier=tier)

    def set_preservation_evidence(self, subject, evidence, *, author_id, tier="A"):
        return set_preservation_evidence(self, subject, evidence, author_id=author_id, tier=tier)

    def claim(self, subject, stage, *, ttl_seconds=300, supersedes=None):
        return claim(self, subject, stage, ttl_seconds=ttl_seconds, supersedes=supersedes)

    def promote(self, subject, stage, receipt_bytes, *, claim_id=None):
        return promote(self, subject, stage, receipt_bytes, claim_id=claim_id)

    def invalidate(self, subject, stage, *, reason):
        return invalidate(self, subject, stage, reason=reason)


def installed_runner(database, *, run_id, owner, profile_id):
    _require(profile_id in _INSTALLED_PROFILES, "installed_profile_missing")
    engine, config, validators = _INSTALLED_PROFILES[profile_id]
    return StageRunner(_RUNNER_TOKEN, database, run_id, owner, profile_id, engine, config, validators, False)


def testing_runner(database, *, run_id, owner, engine_version, config_sha256,
                   validators, version="synthetic-v1"):
    """Injected callbacks are always synthetic and cannot certify production."""
    _sha(config_sha256)
    _identity(version)
    _require(isinstance(validators, dict) and all(
        stage in STAGES and callable(callback) for stage, callback in validators.items()), "invalid_test_validators")
    resolved = {stage: ("test-only:" + version + ":" + stage, callback)
                for stage, callback in validators.items()}
    return StageRunner(_RUNNER_TOKEN, database, run_id, owner, "test-only:" + version,
                       engine_version, config_sha256, resolved, True)


def _runner(value):
    _require(isinstance(value, StageRunner), "configured_runner_required")
    return value


def _subject(connection, subject, stage, run_id):
    _sha(subject)
    _require(stage in STAGES, "invalid_stage")
    original = connection.execute("SELECT * FROM originals WHERE sha256=?", (subject,)).fetchone()
    _require(original is not None, "original_missing")
    _require(original["scope"] != "out_of_scope", "original_out_of_scope")
    _require(connection.execute("SELECT 1 FROM runs WHERE run_id=?", (run_id,)).fetchone(),
             "run_missing")
    return original, _states(connection, subject)


def _states(connection, subject):
    rows = connection.execute("SELECT * FROM stage_state WHERE original_sha256=?", (subject,)).fetchall()
    _require(len(rows) == 7 and {row["stage"] for row in rows} == set(STAGES), "exact_seven_slots_required")
    for row in rows:
        history = connection.execute(
            "SELECT * FROM stage_events WHERE original_sha256=? AND stage=? "
            "ORDER BY sequence DESC LIMIT 1", (subject, row["stage"])).fetchone()
        _require(history is not None and all(history[key] == row[key] for key in row.keys()),
                 "stage_history_mismatch")
    return {row["stage"]: row for row in rows}


def _head(connection, subject, stage):
    return connection.execute(
        "SELECT * FROM stage_content WHERE subject_sha256=? AND stage=? "
        "ORDER BY revision DESC LIMIT 1", (subject, stage)).fetchone()


def _artifact(connection, writer, payload, limit):
    _require(isinstance(payload, bytes) and len(payload) <= limit, "artifact_size_or_type")
    digest = _hash(payload)
    existing = connection.execute("SELECT payload FROM stage_artifacts WHERE sha256=?", (digest,)).fetchone()
    if existing:
        _require(existing[0] == payload, "artifact_conflict")
    else:
        writer.insert("stage_artifacts", (digest, payload))
    return digest


def _change(connection, writer, subject, stage, status, receipt, owner, run_id, reason=None):
    values = (subject, stage, status, receipt, owner, store.now(), run_id, reason)
    with writer.allow("stage_state", values):
        connection.execute(
            "UPDATE stage_state SET status=?,receipt_sha256=?,owner=?,updated_at=?,run_id=?,reason=? "
            "WHERE original_sha256=? AND stage=?", (*values[2:], subject, stage))
    return connection.execute(
        "SELECT max(sequence) FROM stage_events WHERE original_sha256=? AND stage=?",
        (subject, stage)).fetchone()[0]


def _invalidate(connection, writer, subject, stage, run_id, reason, *, include_self=True):
    _require(stage != "preserve", "preservation_is_immutable")
    changed = []
    for dependent in DEPENDENTS[stage]:
        if dependent == stage and not include_self:
            continue
        connection.execute("DELETE FROM work_leases WHERE item_key=?", ("stage:" + subject + ":" + dependent,))
        row = connection.execute(
            "SELECT * FROM stage_state WHERE original_sha256=? AND stage=?", (subject, dependent)).fetchone()
        if row["status"] != "pending" or row["receipt_sha256"] is not None:
            _change(connection, writer, subject, dependent, "pending", None,
                    "stage_revalidation", run_id, reason)
            changed.append(dependent)
    return changed



def _preservation_content(connection, original, content):
    """Validate explicit evidence bindings, not the truth of an external byte-verification claim."""
    subject = original["sha256"]
    if _hash(content) == subject:
        _require(len(content) == original["bytes"], "preservation_bytes_mismatch")
        return "original_bytes", None
    _require(len(content) <= MAX_RECEIPT_BYTES, "preservation_metadata_size")
    evidence = _decode(content)
    _require(evidence.get("schema") == "preservation-evidence-v1", "preservation_evidence_schema")
    _require(evidence.get("original_sha256") == subject and type(evidence.get("byte_length")) is int
             and evidence["byte_length"] == original["bytes"], "preservation_original_binding")
    _require(isinstance(evidence.get("storage_ref"), str) and evidence["storage_ref"].strip(),
             "preservation_storage_reference_required")
    _sha(evidence.get("verification_receipt_sha256"))
    ids = evidence.get("occurrence_ids")
    _require(isinstance(ids, list) and 1 <= len(ids) <= 1024 and
             all(isinstance(item, str) and item.strip() for item in ids) and len(set(ids)) == len(ids),
             "preservation_occurrence_ids_required")
    for identity in ids:
        _require(connection.execute(
            "SELECT 1 FROM occurrences WHERE id=? AND original_sha256=?", (identity, subject)).fetchone(),
            "preservation_occurrence_binding")
    return "preservation_evidence", evidence


def set_preservation_evidence(runner, subject, evidence, *, author_id, tier="A"):
    """Register small hash/length/storage/receipt metadata; original bytes stay in their store."""
    _require(isinstance(evidence, dict), "preservation_evidence_object_required")
    raw = store.canonical(evidence).encode()
    _require(len(raw) <= MAX_RECEIPT_BYTES, "preservation_metadata_size")
    return set_content(runner, subject, "preserve", raw, author_id=author_id, tier=tier)


def set_content(runner, subject, stage, content, *, author_id, tier):
    """Register exact bytes and reopen affected stages; preserve is never reopened."""
    runner = _runner(runner)
    database, run_id = runner.database, runner.run_id
    author = _identity(author_id)
    _require(tier in ("A", "B"), "invalid_tier")
    with _transaction(database) as (connection, writer):
        runner._check(connection)
        original, states = _subject(connection, subject, stage, run_id)
        digest = _artifact(connection, writer, content, MAX_CONTENT_BYTES)
        if stage == "preserve":
            _preservation_content(connection, original, content)
        previous = _head(connection, subject, stage)
        if previous and (previous["content_sha256"], previous["author_id"], previous["tier"]) == (digest, author, tier):
            return {"content_sha256": digest, "revision": previous["revision"], "reused": True, "invalidated": []}
        if stage == "preserve":
            _require(previous is None and states[stage]["status"] != "done", "preservation_is_immutable")
        invalidated = [] if stage == "preserve" else _invalidate(
            connection, writer, subject, stage, run_id, "content_or_attribution_changed")
        revision = previous["revision"] + 1 if previous else 1
        writer.insert("stage_content", (subject, stage, revision, digest, author, tier, run_id, store.now()))
        return {"content_sha256": digest, "revision": revision, "reused": False, "invalidated": invalidated}


def invalidate(runner, subject, stage, *, reason):
    """Trusted runner revision signal; does not change original bytes."""
    runner = _runner(runner)
    database, run_id = runner.database, runner.run_id
    _require(isinstance(reason, str) and reason.strip(), "invalidation_reason_required")
    with _transaction(database) as (connection, writer):
        runner._check(connection)
        _subject(connection, subject, stage, run_id)
        return {"invalidated": _invalidate(connection, writer, subject, stage, run_id, reason)}


def _inputs(states, stage, subject):
    result = {"original": subject}
    for prerequisite, allowed in PREREQUISITES[stage].items():
        row = states[prerequisite]
        _require(row["status"] in allowed and row["receipt_sha256"] is not None,
                 "prerequisite_" + prerequisite)
        result[prerequisite] = row["receipt_sha256"]
    return result


def recover_abandoned_leases(database, *, owner, reason="runner_lock_recovered"):
    """Expire every live lease held by ``owner`` and return its stages to pending.

    Only a caller that has proven no other process of that owner is alive (for
    example by holding the root's exclusive writer lock) may call this: a crash
    between claim and promote otherwise leaves the stage ``in_progress`` until the
    lease TTL passes and the next run fails ``lease_run_mismatch``. Nothing
    accepted is touched; ``done``, ``inapplicable`` and ``blocked`` stages are
    not leases. Returns the recovered ``(subject, stage)`` pairs.
    """
    _require(isinstance(owner, str) and owner.strip(), "owner_required")
    recovered = []
    with _transaction(database) as (c, writer):
        now = datetime.now(timezone.utc)
        rows = c.execute("SELECT item_key,stage,leased_at FROM work_leases WHERE owner=? AND expires_at>?",
                         (owner, now.isoformat())).fetchall()
        for lease in rows:
            if not lease["item_key"].startswith("stage:"):
                continue
            subject = lease["item_key"][len("stage:"):-len(lease["stage"]) - 1]
            c.execute("UPDATE work_leases SET expires_at=?,last_error=? WHERE item_key=?",
                      (now.isoformat(), reason, lease["item_key"]))
            state = c.execute("SELECT status,owner,run_id FROM stage_state WHERE original_sha256=? AND stage=?",
                              (subject, lease["stage"])).fetchone()
            if state is not None and state["status"] == "in_progress":
                _change(c, writer, subject, lease["stage"], "pending", None, owner, state["run_id"], reason)
            recovered.append((subject, lease["stage"]))
    return recovered


def claim(runner, subject, stage, *, ttl_seconds=300, supersedes=None):
    runner = _runner(runner)
    owner, run_id, database = runner.owner, runner.run_id, runner.database
    _require(type(ttl_seconds) is int and 1 <= ttl_seconds <= 3600, "invalid_lease_duration")
    _require(stage in STAGES, "invalid_stage")
    # Refuse before any mutation: a runner that cannot validate this stage must
    # never reopen an accepted receipt (or invalidate its dependents) via
    # supersession. Fresh claims stay allowed: queue leasing relies on them, and
    # promote records "blocked" only when the stage has no accepted receipt.
    _require(supersedes is None or stage in runner.validators, "domain_adapter_unavailable")
    with _transaction(database) as (c, writer):
        runner._check(c)
        _, states = _subject(c, subject, stage, run_id)
        inputs = store.canonical(_inputs(states, stage, subject))
        head = _head(c, subject, stage)
        _require(head is not None, "current_content_required")
        key = "stage:" + subject + ":" + stage
        lease = c.execute("SELECT * FROM work_leases WHERE item_key=?", (key,)).fetchone()
        now = datetime.now(timezone.utc)
        if lease and _time(lease["expires_at"]) > now:
            current = c.execute("SELECT * FROM stage_claims WHERE subject_sha256=? AND stage=? AND leased_at=?",
                                (subject, stage, lease["leased_at"])).fetchone()
            _require(current is not None and lease["owner"] == owner and current["owner"] == owner,
                     "lease_owned_elsewhere")
            _require(current["run_id"] == run_id and current["profile_sha256"] == runner.profile_sha256,
                     "lease_run_mismatch")
            _require(current["revision"] == head["revision"] and current["content_sha256"] == head["content_sha256"]
                     and current["input_receipts"] == inputs and states[stage]["status"] == "in_progress",
                     "stale_lease_revision")
            return {"reused": True, "claim_id": current["claim_id"], "expires_at": current["expires_at"]}
        if states[stage]["status"] in ("done", "inapplicable"):
            _require(stage != "preserve" and supersedes == states[stage]["receipt_sha256"], "explicit_supersession_required")
        else:
            _require(states[stage]["status"] in ("pending", "in_progress", "blocked"), "stage_not_claimable")
        attempts = lease["attempts"] + 1 if lease else 1
        if states[stage]["status"] == "in_progress":
            _change(c, writer, subject, stage, "pending", None, owner, run_id, "lease_expired")
        issued, expires = now.isoformat(), (now + timedelta(seconds=ttl_seconds)).isoformat()
        claim_id = uuid.uuid4().hex
        writer.insert("stage_claims", (claim_id, subject, stage, head["revision"], head["content_sha256"],
                      inputs, owner, run_id, runner.profile_sha256, issued, expires))
        c.execute("INSERT INTO work_leases VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(item_key) DO UPDATE SET "
                  "stage=excluded.stage,owner=excluded.owner,leased_at=excluded.leased_at,"
                  "expires_at=excluded.expires_at,attempts=excluded.attempts,last_error=excluded.last_error,"
                  "next_eligible_at=excluded.next_eligible_at",
                  (key,stage,owner,issued,expires,attempts,None,None))
        # A replacement claim immediately makes dependent acceptances stale.
        # Keep this stage's prior receipt to enforce exact supersession at promotion.
        if states[stage]["receipt_sha256"] is not None and stage != "preserve":
            _invalidate(c, writer, subject, stage, run_id, "upstream_receipt_revalidation", include_self=False)
        _change(c, writer, subject, stage, "in_progress", states[stage]["receipt_sha256"], owner, run_id)
        return {"reused": False, "claim_id": claim_id, "expires_at": expires}


def _check_claim(c, runner, subject, stage, head, states, inputs, claim_id):
    _require(isinstance(claim_id, str), "current_claim_required")
    issued = c.execute("SELECT * FROM stage_claims WHERE claim_id=?", (claim_id,)).fetchone()
    _require(issued is not None and issued["subject_sha256"] == subject and issued["stage"] == stage,
             "claim_subject_stage_mismatch")
    _require(issued["owner"] == runner.owner and issued["run_id"] == runner.run_id and
             issued["profile_sha256"] == runner.profile_sha256, "claim_execution_authority_mismatch")
    _require(issued["revision"] == head["revision"] and issued["content_sha256"] == head["content_sha256"]
             and issued["input_receipts"] == store.canonical(inputs), "stale_lease_revision")
    _require(_time(issued["expires_at"]) > datetime.now(timezone.utc), "lease_expired")
    lease = c.execute("SELECT * FROM work_leases WHERE item_key=?", ("stage:" + subject + ":" + stage,)).fetchone()
    _require(lease is not None and lease["owner"] == runner.owner and lease["leased_at"] == issued["leased_at"]
             and lease["expires_at"] == issued["expires_at"] and states[stage]["status"] == "in_progress"
             and states[stage]["owner"] == runner.owner and states[stage]["run_id"] == runner.run_id,
             "claim_not_current")
    return issued


def _coverage(receipt, stage):
    coverage = receipt.get("coverage")
    _require(isinstance(coverage, dict), "coverage_required")
    denominator = coverage.get("denominator")
    _require(isinstance(denominator, dict) and denominator.get("kind") in
             ("bytes", "pages", "rows", "sheets", "items"), "coverage_denominator")
    total, covered = denominator.get("total"), coverage.get("covered")
    _require(type(total) is int and type(covered) is int and 0 <= covered <= total,
             "coverage_counts")
    if stage in ("review", "compare", "privacy"):
        _require(total > 0 and covered > 0, "review_coverage_unknown")
    _require(coverage.get("scope") in ("selected", "full_text", "full_visual", "all_rows"),
             "coverage_scope")
    locators = receipt.get("locators")
    _require(isinstance(locators, list) and 1 <= len(locators) <= 1024 and
             all(isinstance(value, str) and value.strip() for value in locators),
             "exact_locators_required")


def _reviews(receipt, head, subject, stage):
    required = {"review": {"factual"}, "compare": {"legal"},
                "privacy": {"factual", "privacy"}}.get(stage, set())
    if stage == "privacy" and head["tier"] == "B":
        required.add("legal")
    reviews = receipt.get("reviews", [])
    _require(isinstance(reviews, list) and len(reviews) <= 16, "review_list")
    roles, identities = set(), set()
    for review in reviews:
        _require(isinstance(review, dict), "invalid_review")
        identity = _identity(review.get("reviewer_id"))
        _require(identity != head["author_id"], "self_review")
        role = review.get("role")
        _require(role in ("factual", "legal", "privacy"), "unknown_review_role")
        _require(review.get("verdict") == "pass", "unresolved_review")
        _require(review.get("content_sha256") == head["content_sha256"] and
                 review.get("reviewed_primary_sha256") == subject, "stale_review_content")
        _time(review.get("reviewed_at"))
        _require(isinstance(review.get("rationale"), str) and review["rationale"].strip(), "review_rationale")
        checked = review.get("checked_locators")
        _require(isinstance(checked, list) and all(isinstance(x, str) for x in checked) and
                 set(receipt["locators"]).issubset(set(checked)), "review_locator_coverage")
        if role == "factual":
            _require(review.get("blind_first_pass") is True, "blind_factual_review_required")
        roles.add(role)
        identities.add(identity)
    _require(required.issubset(roles), "missing_independent_roles")
    if stage == "privacy" and head["tier"] == "B":
        _require(len(identities) >= 2, "tier_b_two_reviewers_required")


def _receipt(connection, writer, receipt_hash, receipt):
    values = (
        receipt_hash, receipt["stage"], receipt["subject_sha256"],
        _identity(receipt["reviewer_id"]), receipt["role"], receipt["verdict"],
        store.canonical(receipt["coverage"]), store.canonical(receipt["locators"]),
        receipt["rationale"], receipt["model_or_tool"], store.canonical(receipt["input_hashes"]),
        receipt["created_at_tz"], receipt.get("supersedes"), "ledger-stage-artifact:" + receipt_hash)
    existing = connection.execute("SELECT * FROM receipts WHERE sha256=?", (receipt_hash,)).fetchone()
    if existing:
        _require(tuple(existing) == values, "receipt_conflict")
    else:
        connection.execute("INSERT INTO receipts VALUES(" + ",".join("?" for _ in values) + ")", values)


def promote(runner, subject, stage, receipt_bytes, *, claim_id=None):
    """Untrusted submissions cannot select validator, profile, owner or run."""
    runner = _runner(runner)
    database, run_id = runner.database, runner.run_id
    _require(isinstance(receipt_bytes, bytes) and len(receipt_bytes) <= MAX_RECEIPT_BYTES,
             "receipt_size_or_type")
    receipt_hash, error, result = _hash(receipt_bytes), None, None
    with _transaction(database) as (connection, writer):
        runner._check(connection)
        original, states = _subject(connection, subject, stage, run_id)
        _artifact(connection, writer, receipt_bytes, MAX_RECEIPT_BYTES)
        connection.execute("SAVEPOINT stage_promotion")
        try:
            receipt = _decode(receipt_bytes)
            _require(not set(receipt).intersection(
                {"validator","validator_id","profile","profile_id","config_sha256","run_id","owner"}),
                "submission_policy_override")
            head = _head(connection, subject, stage)
            _require(head is not None, "current_content_required")
            _require(receipt.get("schema") == "ledger-stage-receipt-v1", "receipt_schema")
            _require(receipt.get("subject_sha256") == subject and receipt.get("stage") == stage,
                     "stage_subject_binding")
            _require(receipt.get("content_sha256") == head["content_sha256"] and
                     receipt.get("tier") == head["tier"] and
                     _identity(receipt.get("author_id")) == head["author_id"], "content_binding")
            actor = _identity(receipt.get("reviewer_id"))
            _require(receipt.get("role") == ROLES[stage], "stage_role_binding")
            if stage in ("review", "compare", "privacy"):
                _require(actor != head["author_id"], "self_review")
            _time(receipt.get("created_at_tz"))
            for field in ("rationale", "model_or_tool"):
                _require(isinstance(receipt.get(field), str) and receipt[field].strip(), field + "_required")
            _coverage(receipt, stage)
            expected_inputs = _inputs(states, stage, subject)
            _require(receipt.get("input_hashes") == expected_inputs, "stale_or_wrong_inputs")
            status = {"pass": "done", "blocked": "blocked", "challenge": "blocked",
                      "inapplicable": "inapplicable"}.get(receipt.get("verdict"))
            _require(status is not None, "invalid_verdict")
            _require(not (stage == "preserve" and status == "inapplicable"), "preservation_required")
            if status in ("blocked", "inapplicable"):
                _require(isinstance(receipt.get("reason"), str) and receipt["reason"].strip(), "reason_required")
            previous = connection.execute(
                "SELECT * FROM stage_transitions WHERE receipt_sha256=?", (receipt_hash,)).fetchone()
            if previous:
                _require(states[stage]["receipt_sha256"] == receipt_hash and
                         previous["revision"] == head["revision"] and
                         previous["status"] == states[stage]["status"], "stale_receipt_replay")
                validated = connection.execute(
                    "SELECT * FROM stage_validation_authority WHERE receipt_sha256=?", (receipt_hash,)).fetchone()
                _require(validated is not None and validated["owner"] == runner.owner and
                         validated["run_id"] == run_id and validated["profile_sha256"] == runner.profile_sha256,
                         "replay_authority_mismatch")
                result = {"receipt_sha256": receipt_hash, "status": status, "reused": True,
                          "test_only": bool(validated["test_only"])}
            else:
                _check_claim(connection, runner, subject, stage, head, states, expected_inputs, claim_id)
                if receipt.get("supersedes") is not None:
                    old = connection.execute("SELECT * FROM receipts WHERE sha256=?",
                                             (receipt["supersedes"],)).fetchone()
                    _require(old is not None and old["subject_sha256"] == subject and old["stage"] == stage,
                             "supersedes_binding")
                if states[stage]["receipt_sha256"] is not None:
                    _require(stage != "preserve" and
                             receipt.get("supersedes") == states[stage]["receipt_sha256"],
                             "explicit_supersession_required")
                if status == "done":
                    _require(not receipt.get("holds") and not receipt.get("challenges"), "unresolved_hold")
                    _reviews(receipt, head, subject, stage)
                adapter = runner.validators.get(stage)
                _require(adapter is not None, "domain_adapter_unavailable")
                validator_id, validator = adapter
                if not runner.test_only:
                    for prerequisite, digest in expected_inputs.items():
                        if prerequisite != "original":
                            checked = connection.execute(
                                "SELECT test_only FROM stage_validation_authority WHERE receipt_sha256=?",
                                (digest,)).fetchone()
                            _require(checked is not None and checked["test_only"] == 0,
                                     "synthetic_prerequisite_not_production")
                payload = connection.execute("SELECT payload FROM stage_artifacts WHERE sha256=?",
                                             (head["content_sha256"],)).fetchone()[0]
                _require(_hash(payload) == head["content_sha256"], "current_content_corrupt")
                content_kind, preservation_evidence = (
                    _preservation_content(connection, original, payload) if stage == "preserve"
                    else ("stage_content", None))
                try:
                    verified = validator({"receipt": _decode(receipt_bytes), "content": payload,
                                          "original": dict(original), "stage": stage, "subject_sha256": subject,
                                          "content_kind": content_kind,
                                          "preservation_evidence": preservation_evidence})
                except Exception as exc:
                    raise StageError("domain_validation_failed") from exc
                _require(verified is True, "domain_validation_failed")
                if stage == "preserve" and status == "done":
                    _require(connection.execute(
                        "SELECT 1 FROM occurrences WHERE original_sha256=? LIMIT 1", (subject,)).fetchone(),
                        "preservation_occurrence_required")
                if stage != "preserve" and states[stage]["receipt_sha256"] != receipt_hash:
                    _invalidate(connection, writer, subject, stage, run_id, "superseding_stage_receipt")
                _receipt(connection, writer, receipt_hash, receipt)
                sequence = _change(connection, writer, subject, stage, status, receipt_hash,
                                   runner.owner, run_id, receipt.get("reason"))
                writer.insert("stage_transitions", (
                    receipt_hash, subject, stage, head["revision"], status,
                    store.canonical(expected_inputs), run_id, sequence, validator_id))
                writer.insert("stage_validation_authority", (
                    receipt_hash, claim_id, run_id, runner.owner, runner.profile_sha256,
                    int(runner.test_only), validator_id))
                connection.execute("DELETE FROM work_leases WHERE item_key=?", ("stage:" + subject + ":" + stage,))
                result = {"receipt_sha256": receipt_hash, "status": status, "reused": False,
                          "test_only": runner.test_only}
            connection.execute("RELEASE stage_promotion")
        except StageError as exc:
            connection.execute("ROLLBACK TO stage_promotion")
            connection.execute("RELEASE stage_promotion")
            error = str(exc)
            if (error == "domain_adapter_unavailable"
                    and _states(connection, subject)[stage]["receipt_sha256"] is None):
                # Never replace an accepted receipt with a receipt-less block.
                _change(connection, writer, subject, stage, "blocked", None, runner.owner, run_id, error)
                connection.execute("DELETE FROM work_leases WHERE item_key=?", ("stage:" + subject + ":" + stage,))
        writer.insert("stage_attempts", (
            uuid.uuid4().hex, subject, stage, receipt_hash,
            "rejected" if error else ("replayed" if result["reused"] else "accepted"),
            error or "validated", run_id, store.now()))
    if error:
        raise StageError(error)
    return result


def counts(database):
    """Authoritative stage totals; unknown/malformed slots never disappear from counts."""
    with _transaction(database) as (connection, writer):
        total = connection.execute("SELECT count(*) FROM originals").fetchone()[0]
        _require(total <= 10000, "counts_metadata_bound_exceeded")
        completed, synthetic_completed, candidate_completed = 0, 0, 0
        totals = {stage: dict.fromkeys(("done", "in_progress", "pending", "blocked", "inapplicable"), 0)
                  for stage in STAGES}
        for original in connection.execute("SELECT sha256 FROM originals"):
            subject = original[0]
            states = _states(connection, subject)
            synthetic_subject = False
            for stage, row in states.items():
                totals[stage][row["status"]] += 1
                if row["status"] in ("done", "inapplicable", "blocked") and row["receipt_sha256"]:
                    transition = connection.execute(
                        "SELECT * FROM stage_transitions WHERE receipt_sha256=?",
                        (row["receipt_sha256"],)).fetchone()
                    head = _head(connection, subject, stage)
                    _require(transition is not None and head is not None and
                             transition["revision"] == head["revision"] and
                             transition["status"] == row["status"] and
                             transition["subject_sha256"] == subject and transition["stage"] == stage,
                             "uncontrolled_or_stale_stage")
                    _require(_json_inputs(transition["input_receipts"]) ==
                             _inputs(states, stage, subject), "stale_stage_dependencies")
                    validated = connection.execute(
                        "SELECT * FROM stage_validation_authority WHERE receipt_sha256=?",
                        (row["receipt_sha256"],)).fetchone()
                    _require(validated is not None and validated["run_id"] == transition["run_id"] and
                             validated["validator_id"] == transition["validator_id"],
                             "stage_validation_authority_missing")
                    synthetic_subject = synthetic_subject or bool(validated["test_only"])
            all_done = all(row["status"] in ("done", "inapplicable") for row in states.values())
            candidate_completed += int(all_done)
            synthetic_completed += int(all_done and synthetic_subject)
            completed += int(all_done and not synthetic_subject)
        return {"originals": total, "stage_slots_expected": total * 7,
                "stage_slots_observed": sum(sum(values.values()) for values in totals.values()),
                "stages": totals, "end_to_end_complete": completed,
                "verified_seven_stage_complete": completed, "candidate": True,
                "candidate_seven_stage_complete": candidate_completed,
                "synthetic_seven_stage_complete": synthetic_completed,
                "acceptance": "candidate_with_explicit_validation_authority", "publication_ready": False,
                "owner_approval": False}


def _json_inputs(value):
    return _decode(value)


def query_counts(database):
    """Runner-facing alias: numeric verified counts with an explicit candidate qualifier."""
    return counts(database)
