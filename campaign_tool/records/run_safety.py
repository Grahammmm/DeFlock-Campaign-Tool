"""Fixed-code, durable unattended-run budgets. No exception narratives are evidence."""
import hashlib
import json
import time
from dataclasses import dataclass

from .ledger import store

EXPECTED_HELDS = frozenset({"decoder_needed", "unsupported_rfc822_part", "ambiguous_inline_body_part", "ambiguous_inline_text_part"})
MAIL_CODES = frozenset({
    "invalid_mime_content_type",
    "invalid_related_container",
    "ambiguous_related_content_id",
    "invalid_related_content_id",
    "invalid_related_start",
    "missing_related_root",
    "related_root_type_mismatch",
    "invalid_multipart_disposition",
    "unsupported_attached_multipart_part",
    "invalid_mime_container",
    "invalid_leaf_disposition",
    "empty_message", "message_size_limit", "invalid_identity_numbers", "export_identity_conflict",
    "part_payload_mismatch", "export_scope_mismatch", "attachment_occurrence_missing", "receipt_changed",
    "mime_part_limit", "mime_payload_size_limit", "invalid_eml", "receipt_size_limit",
    "receipt_total_size_limit", "attachment_count_limit", "invalid_receipt_file", "incomplete_receipt",
    "invalid_receipt_bytes", "invalid_eml_bytes", "existing_blob_invalid", "new_blob_cleanup_failed",
    "mail_delta_io_or_ledger_error", "missing_output", "unsafe_output_path", "output_not_owner_only",
    "unsafe_output_type", "invalid_source_path", "source_path_escape", "source_is_output",
    "invalid_sha256", "invalid_account", "invalid_folder", "invalid_uidvalidity", "invalid_uid",
}) | EXPECTED_HELDS
INTAKE_CODES = frozenset({"imap_fetch_failed", "imap_fetch_empty", "message_size_limit"})
EXTRACTION_FAULTS = frozenset({"extraction_timeout", "decoder_unavailable_or_failed", "invalid_or_bounded_parser_output"})
LOCAL_CODES = frozenset({
    "fetch_or_preserve_failed", "stage_failed", "privacy_redaction_fault", "invalid_stage_result",
    "model_configuration_forbidden", "image_activation_forbidden", "time_budget_exceeded",
    "failure_rate_exceeded", "repeated_failure_code", "interrupted_attempt", "invalid_safety_state",
    "unexpected_run_fault", "mail_connection_failed", "retention_integrity_failed", "retention_identity_conflict",
    "folder_visibility_failed", "configured_folder_missing", "review_required", "ocr_runtime_unavailable",
    "report_write_failed",
})
ALL_CODES = MAIL_CODES | INTAKE_CODES | EXTRACTION_FAULTS | LOCAL_CODES
STATE_KEY = "unattended-safety-v1"


def fixed_code(error):
    from .intake import imap_intake, mail_delta
    allowed = MAIL_CODES if type(error) is mail_delta.Rejected else INTAKE_CODES if type(error) is imap_intake.IntakeError else ()
    if len(error.args) == 1 and type(error.args[0]) is str and error.args[0] in allowed:
        return error.args[0]
    return "fetch_or_preserve_failed"


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def valid_sha(value):
    return type(value) is str and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


@dataclass(frozen=True)
class RunSafetyPolicy:
    max_originals: int = 200
    max_fetches: int = 200
    seconds: int = 2700

    def __post_init__(self):
        for value, limit in ((self.max_originals, 200), (self.max_fetches, 200), (self.seconds, 2700)):
            if type(value) is not int or not 1 <= value <= limit:
                raise ValueError("invalid_run_safety_policy")


class RunSafety:
    """Caller holds the root lock. Counters are phase-local; repeated codes persist."""
    def __init__(self, database, policy=None, clock=time.monotonic):
        self.database, self.policy, self.clock = database, policy or RunSafetyPolicy(), clock
        self.deadline = clock() + self.policy.seconds
        self.phases = {name: {"attempted": 0, "failed": 0, "held": 0, "review_required": 0} for name in ("intake", "advance")}
        self.state = {"schema": STATE_KEY, "hold": None, "active": None, "repeats": {}}
        with store.ledger(database, readonly=True) as con:
            row = con.execute("SELECT value FROM ledger_meta WHERE key=?", (STATE_KEY,)).fetchone()
        if row:
            try:
                state = json.loads(row[0])
                if set(state) != set(self.state) or state["schema"] != STATE_KEY:
                    raise ValueError()
                if state["hold"] is not None and state["hold"] not in ALL_CODES:
                    raise ValueError()
                if state["active"] is not None and (type(state["active"]) is not dict or
                        set(state["active"]) != {"phase", "identity"} or state["active"]["phase"] not in self.phases or
                        not valid_sha(state["active"]["identity"])):
                    raise ValueError()
                if type(state["repeats"]) is not dict or len(state["repeats"]) > len(ALL_CODES) * 2:
                    raise ValueError()
                for key, values in state["repeats"].items():
                    phase, code = key.split(":", 1)
                    if phase not in self.phases or code not in ALL_CODES or type(values) is not list or len(values) > 3 or any(not valid_sha(v) for v in values) or len(set(values)) != len(values):
                        raise ValueError()
                self.state = state
            except (ValueError, TypeError, KeyError):
                self.state["hold"] = "invalid_safety_state"
        if self.state["active"] is not None and self.state["hold"] is None:
            self.state["hold"] = "interrupted_attempt"
        self._persist()

    @property
    def stopped(self):
        return self.state["hold"] is not None

    def _persist(self):
        with store.ledger(self.database) as con:
            con.execute("INSERT INTO ledger_meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (STATE_KEY, json.dumps(self.state, sort_keys=True)))
            con.commit()

    def stop(self, code):
        if not self.stopped:
            self.state["hold"] = code if code in ALL_CODES else "unexpected_run_fault"
        self._persist()

    def remaining_seconds(self):
        return max(0, self.deadline - self.clock())

    def check(self):
        if not self.stopped and self.remaining_seconds() <= 0:
            self.stop("time_budget_exceeded")
        return not self.stopped

    def begin(self, phase, identity):
        if phase not in self.phases or not valid_sha(identity):
            raise ValueError("invalid_safety_attempt")
        if not self.check():
            return False
        limit = self.policy.max_fetches if phase == "intake" else self.policy.max_originals
        if self.phases[phase]["attempted"] >= limit:
            return False
        if self.state["active"] is not None:
            self.stop("interrupted_attempt")
            return False
        self.phases[phase]["attempted"] += 1
        self.state["active"] = {"phase": phase, "identity": identity}
        self._persist()
        return True

    def finish(self, outcome="success", code=None, originals=()):
        active = self.state["active"]
        if active is None or outcome not in {"success", "fault", "held", "review_required"}:
            raise ValueError("invalid_safety_outcome")
        if outcome != "success" and code not in ALL_CODES:
            raise ValueError("invalid_safety_code")
        if any(not valid_sha(v) for v in originals):
            raise ValueError("invalid_safety_original")
        phase = active["phase"]
        counters = self.phases[phase]
        self.state["active"] = None
        if outcome == "fault":
            counters["failed"] += 1
            key = phase + ":" + code
            prior = set(self.state["repeats"].get(key, []))
            prior.update(originals)
            self.state["repeats"][key] = sorted(prior)[:3]
            if code == "privacy_redaction_fault":
                self.state["hold"] = code
            elif len(prior) >= 3:
                self.state["hold"] = "repeated_failure_code"
            elif counters["failed"] * 10 > counters["attempted"]:
                self.state["hold"] = "failure_rate_exceeded"
        elif outcome == "held":
            counters["held"] += 1
            self.state["hold"] = code
        elif outcome == "review_required":
            counters["review_required"] += 1
        self._persist()
        self.check()

    def report(self):
        return {"schema": STATE_KEY, "stop_code": self.state["hold"], "phases": self.phases,
                "max_fetches": self.policy.max_fetches, "max_originals": self.policy.max_originals,
                "seconds": self.policy.seconds, "requires_owner_acknowledgement": self.stopped}
