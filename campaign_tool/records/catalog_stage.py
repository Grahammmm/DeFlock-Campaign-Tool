"""Opt-in trusted catalog-stage adapter. Never a document-review adapter.

No registration, run creation, promotion or I/O occurs at import. Inputs are
untrusted data. Default catalog build/export remain read-only.
"""
from datetime import datetime, timezone
import hashlib
import json
import os
import re
from pathlib import Path
import stat
import tempfile

from . import ledger_catalog as catalog

ADAPTER_VERSION = "catalog-evidence-v1"
MAX_CARD_BYTES = 256 * 1024
MAX_UNIT_BYTES = 2 * 1024 * 1024
MAX_SUPPORT_BYTES = 16 * 1024 * 1024
MAX_SUPPORTS = 64
METADATA_FIELDS = ("title", "doc_type", "date_from", "date_to", "summary", "parties",
                   "value_score", "value_reason", "low_value_reason")
FACT_FIELDS = ("title", "doc_type", "date_from", "date_to", "summary", "parties")


class CatalogStageError(ValueError):
    pass


def require(condition, code):
    if not condition:
        raise CatalogStageError(code)


def _sha(value):
    return hashlib.sha256(value).hexdigest()


def _decode(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, "duplicate_json_key")
            result[key] = value
        return result
    try:
        return json.loads(raw, object_pairs_hook=pairs,
                          parse_constant=lambda value: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, TypeError, RecursionError) as error:
        raise CatalogStageError("invalid_card_json") from error


def _locator(value):
    """A bounded typed position, not equality between two empty values."""
    require(type(value) is str and 0 < len(value) <= 500 and value == value.strip(),
            "invalid_exact_locator")
    kinds = {"page", "line", "item", "part", "paragraph", "row", "column", "sheet", "cell", "mime"}
    if value.startswith("{"):
        item = _decode(value)
        require(type(item) is dict and item and not set(item) - kinds,
                "invalid_exact_locator")
        positions = item.items()
    else:
        match = re.fullmatch(r"([a-z]+):([^\r\n]+)", value)
        require(match is not None and match[1] in kinds, "invalid_exact_locator")
        positions = [(match[1], match[2])]
    for kind, position in positions:
        if kind in {"page", "line", "item", "paragraph", "row", "column"}:
            require(type(position) is int and position > 0 or
                    type(position) is str and position.isascii() and position.isdecimal() and int(position) > 0,
                    "invalid_exact_locator")
        else:
            require(type(position) is str and position.strip() == position and position and
                    position.casefold() not in {"null", "none", "n/a", "unknown"} and
                    not any(ord(character) < 32 for character in position), "invalid_exact_locator")
    return value


class CatalogAdapter:
    def __init__(self, database, private_artifact_root):
        from .ledger import stages, store
        self.stages, self.store = stages, store
        self.database = catalog._safe_path(database)
        self.root = catalog._safe_path(private_artifact_root, directory=True)
        catalog._outside_repo(self.root)
        binding = catalog.encoded({"database": str(self.database), "artifact_root": str(self.root)})
        self.adapter_id = ADAPTER_VERSION + ":" + _sha(binding)[:32]

    def _read(self, value, limit):
        path = Path(value)
        require(path.is_absolute() and ".." not in path.parts and self.root in path.parents,
                "artifact_outside_private_root")
        path = catalog._safe_path(path)
        require(path.stat().st_size <= limit, "artifact_size_limit")
        from .intake.folder import secure_open
        with secure_open(path) as source:
            before = os.fstat(source.fileno())
            raw = source.read(limit + 1)
            after = os.fstat(source.fileno())
        identity = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        require(len(raw) <= limit and stat.S_ISREG(before.st_mode) and
                identity(before) == identity(after) == identity(path.stat(follow_symlinks=False)),
                "artifact_changed_or_oversize")
        return raw

    def _unit(self, con, proof):
        require(type(proof) is dict and set(proof) ==
                {"unit_id", "source_sha256", "locator", "artifact_sha256", "quote"},
                "invalid_support_schema")
        require(type(proof["unit_id"]) is str and 0 < len(proof["unit_id"]) <= 256,
                "invalid_unit_id")
        require(type(proof["quote"]) is str and 0 < len(proof["quote"]) <= 4000,
                "empty_or_oversize_quote")
        _locator(proof["locator"])
        row = con.execute("SELECT * FROM units WHERE id=?", (proof["unit_id"],)).fetchone()
        require(row is not None, "support_unit_missing")
        _locator(row["locator"])
        require(row["original_sha256"] == proof["source_sha256"] and row["locator"] == proof["locator"],
                "support_locator_binding")
        if row["status"] == "candidate_extracted":
            from .catalog_wp4_support import verified_text_line
            return verified_text_line(self, con, row, proof)
        require(row["status"] == "ok" and row["parser"] and row["parser_version"] and
                type(row["text_sha256"]) is str and catalog.HASH.fullmatch(row["text_sha256"]),
                "support_unit_unverified")
        source = con.execute("SELECT * FROM originals WHERE sha256=?", (proof["source_sha256"],)).fetchone()
        require(source is not None and not catalog.scope_excluded(source), "support_original_missing")
        raw = self._read(row["derived_path"], MAX_UNIT_BYTES)
        require(_sha(raw) == proof["artifact_sha256"], "support_artifact_hash_mismatch")
        try:
            text = raw.decode("utf-8")
        except UnicodeError as error:
            raise CatalogStageError("support_not_utf8") from error
        if row["unit_type"] == "catalog-proof-json":
            item = _decode(raw)
            require(type(item) is dict and item.get("schema") == "records-unit-v1" and
                    item.get("original_sha256") == proof["source_sha256"] and
                    item.get("locator") == proof["locator"] and type(item.get("text")) is str,
                    "support_json_binding")
            text = item["text"]
        else:
            require(row["unit_type"] == "text", "unsupported_support_artifact_format")
        require(_sha(text.encode("utf-8")) == row["text_sha256"], "support_text_hash_mismatch")
        require(proof["quote"] in text, "quote_not_in_source")
        return {**proof, "text_sha256": row["text_sha256"], "parser": row["parser"],
                "parser_version": row["parser_version"]}, len(raw)

    def prepare(self, artifact, expected_sha256):
        require(type(expected_sha256) is str and catalog.HASH.fullmatch(expected_sha256), "invalid_card_hash")
        raw = self._read(artifact, MAX_CARD_BYTES)
        require(_sha(raw) == expected_sha256, "card_artifact_hash_mismatch")
        card = _decode(raw)
        require(type(card) is dict and set(card) ==
                {"schema", "subject_sha256", "metadata", "supports", "field_support"},
                "invalid_card_schema")
        require(card["schema"] == "catalog-card-evidence-v1", "invalid_card_schema")
        subject = card["subject_sha256"]
        require(type(subject) is str and catalog.HASH.fullmatch(subject), "invalid_subject")
        supports = card["supports"]
        require(type(supports) is list and 1 <= len(supports) <= MAX_SUPPORTS, "actual_evidence_required")
        with self.store.ledger(self.database, readonly=True) as con:
            original = con.execute("SELECT * FROM originals WHERE sha256=?", (subject,)).fetchone()
            require(original is not None and not catalog.scope_excluded(original),
                    "canonical_original_unavailable")
            referenced = {subject}
            for proof in supports:
                require(type(proof) is dict and type(proof.get("source_sha256")) is str,
                        "invalid_support_schema")
                referenced.add(proof["source_sha256"])
            metadata = catalog.validate_overlay({subject: card["metadata"]}, referenced)[subject]
            # Support locators are separate from candidate display metadata.
            metadata.pop("evidence", None)
            normalized = {field: metadata.get(field, [] if field == "parties" else None)
                          for field in METADATA_FIELDS}
            proofs, seen, consumed = [], set(), 0
            for proof in supports:
                verified, size = self._unit(con, proof)
                require(verified["unit_id"] not in seen, "duplicate_support_unit")
                seen.add(verified["unit_id"])
                consumed += size
                require(consumed <= MAX_SUPPORT_BYTES, "support_budget_exceeded")
                proofs.append(verified)
            require(any(p["source_sha256"] == subject for p in proofs), "primary_original_support_required")
            field_support = card["field_support"]
            require(type(field_support) is dict and not set(field_support) - set(METADATA_FIELDS),
                    "invalid_field_support")
            for field, identities in field_support.items():
                require(type(identities) is list and identities and len(identities) <= MAX_SUPPORTS and
                        all(type(identity) is str and identity in seen for identity in identities),
                        "field_support_unit_missing")
            for field in METADATA_FIELDS:
                if normalized[field] not in (None, [], ""):
                    require(field in field_support, "known_field_without_support")
            typed_joins = []
            joins = con.execute("SELECT * FROM joins WHERE original_sha256=? AND status='typed' ORDER BY id",
                                (subject,)).fetchall()
            require(len(joins) <= MAX_SUPPORTS, "typed_join_limit")
            verified_locators = {(p["source_sha256"], p["locator"]) for p in proofs}
            for row in joins:
                evidence = _decode(row["evidence"])
                require(type(evidence) is dict and
                        (evidence.get("source_sha256"), evidence.get("locator")) in verified_locators,
                        "typed_join_support_missing")
                require(row["agency_id"] is not None and con.execute(
                    "SELECT 1 FROM agencies WHERE id=?", (row["agency_id"],)).fetchone() is not None,
                    "typed_agency_missing")
                if row["request_id"] is not None:
                    request = con.execute("SELECT agency_id FROM requests WHERE id=?", (row["request_id"],)).fetchone()
                    require(request is not None and request[0] == row["agency_id"], "typed_request_agency_mismatch")
                typed_joins.append({"agency_id": row["agency_id"], "request_id": row["request_id"],
                                    "join_type": row["join_type"],
                                    "evidence": {"source_sha256": evidence["source_sha256"], "locator": evidence["locator"]}})
        # Sort and deduplicate classification content; no global or run state.
        typed_joins = [_decode(item) for item in sorted({catalog.encoded(j) for j in typed_joins})]
        proofs.sort(key=lambda p: p["unit_id"])
        envelope = {"schema": "catalog-classification-v1", "subject_sha256": subject,
                    "original_byte_length": original["bytes"], "metadata": normalized,
                    "metadata_status": "candidate", "agency_ids": sorted({j["agency_id"] for j in typed_joins}),
                    "request_ids": sorted({j["request_id"] for j in typed_joins if j["request_id"]}),
                    "agency_status": "typed" if typed_joins else "unknown",
                    "typed_joins": typed_joins, "supports": proofs,
                    "field_support": {key: sorted(set(values)) for key, values in sorted(field_support.items())},
                    "low_value_status": "proposed low-value" if normalized["low_value_reason"] else None,
                    "unknown_reasons": {field: "No supported catalog value" for field in FACT_FIELDS
                                        if normalized[field] in (None, [], "")},
                    "review_complete": False, "publication_ready": False}
        content = catalog.encoded(envelope)
        require(len(content) <= MAX_CARD_BYTES, "classification_envelope_size")
        return {"subject_sha256": subject, "content": content, "content_sha256": _sha(content),
                "envelope": envelope, "card_bytes": raw, "artifact": {"relative_path": str(Path(artifact).relative_to(self.root)),
                                                "sha256": expected_sha256},
                "locators": sorted({p["source_sha256"] + ":" + p["locator"] for p in proofs})}

    def preserve_card(self, prepared):
        """Keep exact incoming card bytes before binding a durable stage receipt."""
        directory = self.root / "catalog-card-artifacts"
        directory.mkdir(mode=0o700, exist_ok=True)
        catalog._safe_path(directory, directory=True)
        target = directory / (prepared["artifact"]["sha256"] + ".json")
        fd, temporary = tempfile.mkstemp(prefix=".catalog-card-", dir=directory)
        try:
            with os.fdopen(fd, "wb") as out:
                out.write(prepared["card_bytes"])
                out.flush()
                os.fchmod(out.fileno(), 0o400)
                os.fsync(out.fileno())
            try:
                os.link(temporary, target)
            except FileExistsError:
                require(self._read(target, MAX_CARD_BYTES) == prepared["card_bytes"], "preserved_card_conflict")
        finally:
            os.unlink(temporary)
        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        root_fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(root_fd)
        finally:
            os.close(root_fd)
        prepared["artifact"] = {"relative_path": str(target.relative_to(self.root)),
                                "sha256": prepared["artifact"]["sha256"]}

    def validate(self, context):
        require(context["stage"] == "catalog" and context["receipt"]["verdict"] == "pass",
                "catalog_only_pass_adapter")
        binding = context["receipt"].get("catalog_artifact")
        require(type(binding) is dict and set(binding) == {"relative_path", "sha256"},
                "card_artifact_binding_required")
        relative = Path(binding["relative_path"])
        require(not relative.is_absolute() and ".." not in relative.parts, "unsafe_card_artifact_path")
        prepared = self.prepare(self.root / relative, binding["sha256"])
        require(prepared["subject_sha256"] == context["subject_sha256"] == context["original"]["sha256"] and
                prepared["envelope"]["original_byte_length"] == context["original"]["bytes"] and
                prepared["content"] == context["content"] and
                context["receipt"]["locators"] == prepared["locators"], "classification_binding_mismatch")
        require(context["receipt"].get("coverage") == {"denominator": {"kind": "items", "total": 1},
                "covered": 1, "scope": "selected"}, "catalog_coverage_binding")
        return True


def register_catalog_adapter(database, private_artifact_root):
    """Trusted startup only. Neither record bytes nor receipt data call this."""
    adapter = CatalogAdapter(database, private_artifact_root)
    registry = adapter.stages._INSTALLED_VALIDATORS
    existing = registry.get(adapter.adapter_id)
    if existing is not None:
        owner = getattr(existing, "__self__", None)
        require(type(owner) is CatalogAdapter and owner.database == adapter.database and owner.root == adapter.root,
                "installed_adapter_collision")
    else:
        registry[adapter.adapter_id] = adapter.validate
    return adapter.adapter_id


def catalog_run_factory(database, private_artifact_root, *, run_id, owner, profile_id,
                        engine_version, config_sha256, additional_adapters=None):
    """Configure once before any stage binds this run; never creates a run row."""
    adapter = CatalogAdapter(database, private_artifact_root)
    adapter_id = register_catalog_adapter(database, private_artifact_root)
    validators = dict(additional_adapters or {})
    require("catalog" not in validators or validators["catalog"] == adapter_id, "catalog_adapter_override")
    validators["catalog"] = adapter_id
    adapter.stages.configure_installed_profile(profile_id, engine_version=engine_version,
                                               config_sha256=config_sha256, validators=validators)
    runner = adapter.stages.installed_runner(database, run_id=run_id, owner=owner, profile_id=profile_id)
    return CatalogStage(adapter, runner)


class CatalogStage:
    def __init__(self, adapter, runner):
        require(Path(runner.database) == adapter.database, "runner_database_mismatch")
        self.adapter, self.runner = adapter, runner

    def _revalidate_accepted(self, subject):
        """Check only the bound current receipt, never a rejected new submission.

        Deterministic drift reopens this stage and its dependents through WP1.
        I/O, permissions and concurrent reads remain a gap, not proof of drift.
        The application still holds its existing single-runner control lock.
        """
        with self.adapter.store.ledger(self.adapter.database, readonly=True) as con:
            self.runner._check(con)
            state = con.execute("SELECT * FROM stage_state WHERE original_sha256=? AND stage='catalog'",
                                (subject,)).fetchone()
            if state is None or state["status"] != "done":
                return None
            authority = con.execute("SELECT * FROM stage_validation_authority WHERE receipt_sha256=?",
                                    (state["receipt_sha256"],)).fetchone()
            require(authority is not None and authority["validator_id"] == self.runner.validators["catalog"][0]
                    and bool(authority["test_only"]) == bool(self.runner.test_only),
                    "catalog_adapter_authority_changed")
            saved = con.execute("SELECT payload FROM stage_artifacts WHERE sha256=?",
                                (state["receipt_sha256"],)).fetchone()
            require(saved is not None and _sha(bytes(saved[0])) == state["receipt_sha256"],
                    "accepted_catalog_receipt_unavailable")
            packet = _decode(bytes(saved[0]))
            content = con.execute("SELECT payload FROM stage_artifacts WHERE sha256=?",
                                  (packet["content_sha256"],)).fetchone()
            require(content is not None, "accepted_catalog_content_unavailable")
            original = con.execute("SELECT * FROM originals WHERE sha256=?", (subject,)).fetchone()
            require(original is not None, "canonical_original_unavailable")
            context = {"stage": "catalog", "receipt": packet, "subject_sha256": subject,
                       "content": bytes(content[0]), "original": dict(original)}
        confirmed_drift = {
            "support_artifact_hash_mismatch", "support_text_hash_mismatch", "support_json_binding",
            "support_locator_binding", "support_unit_missing", "support_unit_unverified",
            "support_original_missing", "support_not_utf8", "quote_not_in_source",
            "invalid_exact_locator", "classification_binding_mismatch", "card_artifact_hash_mismatch",
            "typed_join_support_missing", "typed_agency_missing", "typed_request_agency_mismatch",
            "canonical_original_unavailable", "unsupported_support_artifact_format",
        }
        try:
            self.adapter.validate(context)
        except CatalogStageError as error:
            from .catalog_wp4_support import DRIFT_REASONS
            if str(error) not in confirmed_drift | DRIFT_REASONS:
                raise
            result = self.runner.invalidate(subject, "catalog", reason="accepted_catalog_evidence_drift:" + str(error))
            return {"status": "blocked", "reason": "accepted_catalog_evidence_drift:" + str(error),
                    "promoted": False, "canonical_stage_changed": bool(result["invalidated"]),
                    "invalidated": result["invalidated"], "review_complete": False, "publication_ready": False}
        return None

    def process(self, artifact, expected_sha256, *, author_id, tier="A"):
        """Explicit catalog work only. Missing evidence returns a gap before mutation."""
        try:
            # Identify the requested original only from exact, structurally bound
            # incoming bytes. Never invalidate from arbitrary malformed input.
            raw = self.adapter._read(artifact, MAX_CARD_BYTES)
            require(type(expected_sha256) is str and catalog.HASH.fullmatch(expected_sha256), "invalid_card_hash")
            require(_sha(raw) == expected_sha256, "card_artifact_hash_mismatch")
            candidate = _decode(raw)
            require(type(candidate) is dict and candidate.get("schema") == "catalog-card-evidence-v1"
                    and type(candidate.get("subject_sha256")) is str
                    and catalog.HASH.fullmatch(candidate["subject_sha256"]), "invalid_card_schema")
            drift = self._revalidate_accepted(candidate["subject_sha256"])
            if drift is not None:
                return drift
            prepared = self.adapter.prepare(artifact, expected_sha256)
        except (CatalogStageError, catalog.CatalogError, OSError) as error:
            return {"status": "blocked", "reason": "artifact_io_unavailable" if isinstance(error, OSError) else str(error), "promoted": False,
                    "canonical_stage_changed": False}
        except (TypeError, KeyError, RecursionError):
            return {"status": "blocked", "reason": "invalid_card_or_support_structure", "promoted": False,
                    "canonical_stage_changed": False}
        subject = prepared["subject_sha256"]
        with self.adapter.store.ledger(self.adapter.database, readonly=True) as con:
            self.runner._check(con)
            extraction_state = con.execute("SELECT * FROM stage_state WHERE original_sha256=? AND stage='extract'", (subject,)).fetchone()
            if extraction_state is None or extraction_state["status"] not in ("done", "blocked", "inapplicable") or not extraction_state["receipt_sha256"]:
                return {"status": "blocked", "reason": "accepted_extraction_receipt_required",
                        "promoted": False, "canonical_stage_changed": False}
            if not self.runner.test_only:
                verified = con.execute("SELECT test_only FROM stage_validation_authority WHERE receipt_sha256=?",
                                       (extraction_state["receipt_sha256"],)).fetchone()
                if verified is None or verified["test_only"]:
                    return {"status": "blocked", "reason": "production_extraction_authority_required",
                            "promoted": False, "canonical_stage_changed": False}
            lease = con.execute("SELECT * FROM work_leases WHERE item_key=?", ("stage:" + subject + ":catalog",)).fetchone()
            if lease and datetime.fromisoformat(lease["expires_at"]) > datetime.now(timezone.utc):
                current_claim = con.execute("SELECT owner,run_id FROM stage_claims WHERE subject_sha256=? AND stage='catalog' AND leased_at=?",
                                            (subject, lease["leased_at"])).fetchone()
                if current_claim is None or current_claim["owner"] != self.runner.owner or current_claim["run_id"] != self.runner.run_id:
                    return {"status": "blocked", "reason": "catalog_lease_owned_elsewhere",
                            "promoted": False, "canonical_stage_changed": False}
            state = con.execute("SELECT * FROM stage_state WHERE original_sha256=? AND stage='catalog'", (subject,)).fetchone()
            head = con.execute("SELECT * FROM stage_content WHERE subject_sha256=? AND stage='catalog' ORDER BY revision DESC LIMIT 1", (subject,)).fetchone()
            if state["status"] == "done" and head and head["content_sha256"] == prepared["content_sha256"] and head["author_id"] == author_id.casefold() and head["tier"] == tier:
                transition = con.execute("SELECT * FROM stage_transitions WHERE receipt_sha256=?", (state["receipt_sha256"],)).fetchone()
                authority = con.execute("SELECT * FROM stage_validation_authority WHERE receipt_sha256=?", (state["receipt_sha256"],)).fetchone()
                extraction = con.execute("SELECT receipt_sha256 FROM stage_state WHERE original_sha256=? AND stage='extract'", (subject,)).fetchone()
                require(transition is not None and authority is not None and
                        bool(authority["test_only"]) == bool(self.runner.test_only) and
                        transition["revision"] == head["revision"] and
                        _decode(transition["input_receipts"]) == {"original": subject, "extract": extraction[0]},
                        "stale_catalog_acceptance")
                require(authority["validator_id"] == self.runner.validators["catalog"][0],
                        "catalog_adapter_authority_changed")
                return {"status": "done", "reused": True, "promoted": False,
                        "receipt_sha256": state["receipt_sha256"], "content_sha256": prepared["content_sha256"],
                        "review_complete": False, "publication_ready": False}
        self.adapter.preserve_card(prepared)
        registered = self.runner.set_content(subject, "catalog", prepared["content"], author_id=author_id, tier=tier)
        claim = self.runner.claim(subject, "catalog")
        with self.adapter.store.ledger(self.adapter.database, readonly=True) as con:
            extraction = con.execute("SELECT receipt_sha256 FROM stage_state WHERE original_sha256=? AND stage='extract'", (subject,)).fetchone()[0]
        packet = {"schema": "ledger-stage-receipt-v1", "subject_sha256": subject, "stage": "catalog",
                  "content_sha256": registered["content_sha256"], "tier": tier, "author_id": author_id.casefold(),
                  "reviewer_id": self.runner.owner, "role": "cataloger", "verdict": "pass",
                  "coverage": {"denominator": {"kind": "items", "total": 1}, "covered": 1, "scope": "selected"},
                  "locators": prepared["locators"], "rationale": "Candidate classification bound to verified unit bytes and canonical joins; not independent review.",
                  "model_or_tool": ADAPTER_VERSION, "created_at_tz": datetime.now(timezone.utc).isoformat(),
                  "input_hashes": {"original": subject, "extract": extraction}, "reviews": [],
                  "catalog_artifact": prepared["artifact"]}
        result = self.runner.promote(subject, "catalog", catalog.encoded(packet), claim_id=claim["claim_id"])
        return {**result, "promoted": not result["reused"], "content_sha256": prepared["content_sha256"],
                "review_complete": False, "publication_ready": False}
