"""Private, counts-only F-043 digest. No model, ledger, network or source reads.

The host supplies already-admitted F-042 detectors-run-v1 results, F-044
records-understanding-requests-v1 results, authoritative source_bindings and
fresh pipeline.CanonicalJoins. This is a projection, not an evidence validator
or legal assessment. Input hashes bind the exact supplied objects. Missing
citations are UNRESOLVED; blocked/inapplicable never become usage observations.

Host seam: run_stages.stage_outputs(args) accepts optional args.digest_context,
a dict of keyword arguments to write_digest. Absent context preserves baseline.
No new CLI/plan parser is introduced: F-046 owns those bindings and the frozen
handoff. Required context: plan, run_id, detector_results, requests,
request_provenance, source_bindings, joins, admitted_root, output_root.
plan.digest.enabled defaults false; enabled requires plan.synthetic is True.
admitted_root is an owner-supplied boundary, NEVER a value loaded from the plan.
output_root must be a NEW direct child of that existing admitted root. Caller
must bind that root to its admitted synthetic project before invoking this API.

Only fixed enums/counts/identities are emitted. Free text (quotes, names,
observed_values_private, request numbers, warnings) is omitted, not inferred.
Locators/message IDs are represented by SHA256, preserving exact identity
without leaking arbitrary text. Agency headings come solely from typed join
IDs (ASCII ID grammar), never quotes. This stronger data-minimization gate
precedes both JSON and Markdown; it is not semantic redaction approval.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from ..detectors import core, pipeline

FLAGS = {"publication_ready": False, "stage_promotions": 0, "review_required": True,
         "qualified": False, "activation_allowed": False}
MAX_BYTES = core.MAX_INPUT_BYTES
ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
CODE = re.compile(r"[a-z][a-z0-9_]{0,127}\Z")
STATES = {"sent", "acknowledged", "extension", "fulfilled", "denied", "delivery_recorded",
          "partial_delivery", "UNRESOLVED", "NOT_ESTABLISHED"}
DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")


class DigestRefused(ValueError):
    """Stable, text-free admission failure. No fallback path."""


def need(condition, reason):
    if not condition:
        raise DigestRefused(reason)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def code(value):
    return value if isinstance(value, str) and CODE.fullmatch(value) else "unresolved_reason"


def identity(value):
    need(isinstance(value, str) and ID.fullmatch(value), "invalid_opaque_identity")
    return value


def unique(rows):
    return [json.loads(raw) for raw in sorted({canonical(row) for row in rows})]


def enabled(plan):
    need(type(plan) is dict, "plan_shape")
    settings = plan.get("digest", {})
    need(type(settings) is dict, "digest_config_shape")
    flag = settings.get("enabled", False)
    need(type(flag) is bool, "digest_enabled_not_boolean")
    return flag


def _root(value):
    need(isinstance(value, (str, Path)), "output_root_invalid")
    literal = str(value)
    path = Path(literal)
    need(path.is_absolute() and str(path) == literal and ".." not in path.parts,
         "output_root_not_absolute_normalized")
    need(not any(p.is_symlink() for p in (path, *path.parents)), "output_root_symlink")
    return path


def validate_roots(admitted_root, output_root):
    root, output = _root(admitted_root), _root(output_root)
    need(root.is_dir() and root != Path(root.anchor), "admitted_root_invalid")
    need(output.parent == root and output.name not in {"tests", "fixtures", "repo"},
         "output_root_outside_admission")
    need(not os.path.lexists(output), "output_root_not_exclusive")
    return output


def project(*, run_id, detector_results, requests, request_provenance, source_bindings, joins):
    """Pure projection. Hash-bound input identities are not semantic acceptance."""
    identity(run_id)
    try:
        raw = canonical([detector_results, requests, request_provenance, source_bindings])
    except (TypeError, ValueError, RecursionError):
        raise DigestRefused("input_json_invalid") from None
    need(len(raw.encode()) <= MAX_BYTES, "input_byte_bound")
    need(type(detector_results) is list, "detector_results_shape")
    try:
        sources = pipeline._sources(source_bindings)
    except (ValueError, TypeError, KeyError, AttributeError):
        raise DigestRefused("source_bindings_invalid") from None
    need(isinstance(joins, pipeline.CanonicalJoins), "typed_joins_required")
    need(joins.source_bindings_sha256 == core.digest(source_bindings), "joins_inventory_mismatch")
    need(core.sha(joins.revision_sha256), "joins_revision_missing")
    need(isinstance(joins.rows_json, str) and len(joins.rows_json.encode()) <= MAX_BYTES,
         "joins_byte_bound")
    try:
        join_rows = json.loads(joins.rows_json)
    except (ValueError, TypeError):
        raise DigestRefused("joins_invalid") from None
    need(type(join_rows) is list, "joins_invalid")
    agencies = {}
    for row in join_rows:
        need(type(row) is dict and row.get("status") == "typed" and core.sha(row.get("original_sha256")),
             "joins_invalid")
        agencies.setdefault(row["original_sha256"], set()).add(identity(row.get("agency_id")))
    allowed_agencies = set().union(*agencies.values()) if agencies else set()
    refs = {}
    for row in sources.values():
        ref = core.reference(row)
        refs[digest(ref)] = ref

    def citations(values, agency=None):
        need(type(values) is list, "citations_shape")
        result = []
        for value in values:
            try:
                ref = core.reference(value)
            except (ValueError, TypeError, AttributeError):
                return None
            if digest(ref) not in refs or (agency is not None and agencies.get(ref["original_sha256"]) != {agency}):
                return None
            result.append({"original_sha256": ref["original_sha256"], "locator_sha256": digest(ref["locator"])})
        return unique(result) if result else None

    rows, unresolved, status_rows, provenance = [], [], [], []
    if not detector_results:
        unresolved.append({"kind": "detector_result_unresolved", "reason": "detector_results_missing"})
    seen_subjects = set()
    for result in detector_results:
        need(type(result) is dict and result.get("schema") == "detectors-run-v1", "detector_result_schema")
        need(result.get("run_id") == run_id, "detector_run_mismatch")
        subject = result.get("subject_sha256")
        need(core.sha(subject) and subject not in seen_subjects, "detector_subject_conflict")
        seen_subjects.add(subject)
        need(type(result.get("inputs")) is dict and set(result["inputs"]) ==
             {"inventory", "units", "canonical_copy", "rules", "config"}, "detector_inputs_missing")
        for name, binding in result["inputs"].items():
            need(binding is None or (type(binding) is dict and set(binding) == {"sha256"} and
                                    core.sha(binding.get("sha256"))),
                 "detector_input_identity_invalid")
        provenance.append({"run_id": run_id, "subject_sha256": subject,
                           "result_sha256": digest(result), "inputs": result["inputs"]})
        structured = result.get("structured")
        if structured is None:
            unresolved.append({"subject_sha256": subject, "reason": code(result.get("reason")),
                               "kind": "detector_result_unresolved"})
            continue
        need(type(structured) is dict and set(structured) == set(core.DETECTORS), "detector_population_mismatch")
        for name in core.DETECTORS:
            entry = structured[name]
            need(type(entry) is dict and entry.get("status") in {"evaluated", "blocked", "inapplicable"},
                 "detector_status_invalid")
            status = entry["status"]
            status_rows.append({"subject_sha256": subject, "detector": name, "status": status,
                                "reason": code(entry.get("reason")),
                                "suppressed_hit_count": entry.get("suppressed_hit_count", 0)})
            need(type(entry.get("suppressed_hit_count", 0)) is int and entry.get("suppressed_hit_count", 0) >= 0,
                 "detector_count_invalid")
            need(type(entry.get("hits")) is list, "detector_hits_invalid")
            manifest = entry.get("manifest")
            outcomes = []
            if manifest is not None:
                need(type(manifest) is dict and type(manifest.get("outcomes")) is list,
                     "detector_manifest_invalid")
                for outcome in manifest["outcomes"]:
                    need(type(outcome) is dict and core.sha(outcome.get("event_key")),
                         "detector_outcome_invalid")
                    need(outcome.get("disposition") in {"evaluated", "skipped", "blocked"},
                         "detector_outcome_invalid")
                    outcomes.append({"event_key": outcome["event_key"],
                                     "disposition": outcome["disposition"],
                                     "reason": code(outcome.get("reason")),
                                     "citations": citations(outcome.get("evidence", [])) or []})
            status_rows[-1]["outcomes"] = unique(outcomes)
            if status != "evaluated":
                continue
            for hit in entry["hits"]:
                need(type(hit) is dict, "hit_shape")
                app = hit.get("applicability", {})
                agency = app.get("agency_id") if type(app) is dict else None
                evidence = citations(hit.get("evidence", []), agency)
                rule_source = app.get("rule_source_sha256") if type(app) is dict else None
                rule_bound = any(r["original_sha256"] == rule_source for r in sources.values())
                if (agency not in allowed_agencies or not evidence or not rule_bound or
                        not core.sha(hit.get("dedupe_key")) or hit.get("detector") != name):
                    unresolved.append({"subject_sha256": subject, "detector": name,
                                       "kind": "usage_observation", "reason": "citation_or_agency_unresolved",
                                       "candidate_sha256": digest(hit)})
                    continue
                rows.append({"agency_id": agency, "detector": name, "observation_id": hit["dedupe_key"],
                             "citations": evidence, "rule_source_sha256": rule_source})
    need(type(requests) is dict and requests.get("schema") == "records-understanding-requests-v1",
         "requests_schema")
    need(type(request_provenance) is dict and set(request_provenance) == {"run_id", "sha256"} and
         request_provenance.get("sha256") == digest(requests) and
         request_provenance.get("run_id") == run_id, "request_provenance_mismatch")
    need(type(requests.get("requests")) is list and type(requests.get("holds")) is list, "requests_shape")
    request_rows = []
    for request in requests["requests"]:
        need(type(request) is dict and type(request.get("transitions")) is list, "request_shape")
        agency = request.get("agency_id")
        # Request identities may be human-facing; hash rather than expose them.
        key = digest(request.get("request_id"))
        reasons = [code(r) for r in request.get("unresolved_reasons", [])]
        cited = []
        hashes = request.get("message_sha256s", [])
        for transition in request["transitions"]:
            cite = transition.get("citation", {}) if type(transition) is dict else {}
            if (type(cite) is not dict or not core.sha(cite.get("message_sha256")) or
                    cite.get("message_sha256") not in hashes or not isinstance(cite.get("message_id"), str)):
                reasons.append("request_citation_unresolved")
            else:
                cited.append({"message_sha256": cite["message_sha256"],
                              "message_id_sha256": digest(cite["message_id"])})
        if not cited:
            reasons.append("request_citation_unresolved")
        if agency not in allowed_agencies:
            reasons.append("request_agency_unresolved")
        state = request.get("state")
        if state not in STATES or state in {"UNRESOLVED", "NOT_ESTABLISHED"}:
            reasons.append("request_state_unresolved")
        reminder = request.get("reminders", {})
        need(type(reminder) is dict, "request_reminders_shape")
        candidates = {}
        for name in ("ten_day_candidate", "twenty_four_day_candidate"):
            value = reminder.get(name)
            need(value is None or (isinstance(value, str) and DATE.fullmatch(value)), "reminder_date_invalid")
            candidates[name] = value
        anchor = reminder.get("anchor") or {}
        anchor_cite = anchor.get("citation", {}) if type(anchor) is dict else {}
        if any(candidates.values()) and not any(
                c["message_sha256"] == anchor_cite.get("message_sha256") and
                c["message_id_sha256"] == digest(anchor_cite.get("message_id")) for c in cited):
            reasons.append("reminder_citation_unresolved")
        if reasons:
            candidates = {name: None for name in candidates}
        request_rows.append({"agency_id": agency if agency in allowed_agencies else None,
                             "request_id_sha256": key, "state": "UNRESOLVED" if reasons else state,
                             "candidate_state": request.get("candidate_state") if request.get("candidate_state") in STATES else None,
                             "unresolved_reasons": sorted(set(reasons)), "citations": unique(cited),
                             "scope_uncertainty": bool(request.get("scope_uncertainty")),
                             "no_response": "NOT_ESTABLISHED", "reminders": {
                                 **candidates, "status": "UNRESOLVED" if reasons else (
                                     "candidate" if reminder.get("status") == "candidate" else "NOT_ESTABLISHED"),
                                 "legal_due_date": False, "needs_attorney_review": True,
                                 "calendar_policy": "unadjusted_calendar_days_not_legal_deadlines",
                                 "receipt_status": ("agency_statement_candidate_unverified" if
                                     reminder.get("receipt_status") == "agency_statement_candidate_unverified"
                                     else "NOT_ESTABLISHED"),
                                 "extension_status": ("possible_notice_requires_review" if
                                     reminder.get("extension_status") == "possible_notice_requires_review"
                                     else "not_verified_in_message_text"),
                                 "extension_applied": False},
                             "send": False, "legal_conclusion": "NOT_ASSESSED"})
    observations = unique(rows)
    identities = {}
    for row in observations:
        key = row["observation_id"]
        need(key not in identities or identities[key] == row, "observation_identity_conflict")
        identities[key] = row
    aggregates = []
    for agency, name in sorted({(r["agency_id"], r["detector"]) for r in observations}):
        selected = [r for r in observations if r["agency_id"] == agency and r["detector"] == name]
        aggregates.append({"agency_id": agency, "detector": name, "cited_observations": len(selected),
                           "observation_ids": sorted(r["observation_id"] for r in selected),
                           "citations": unique([c for r in selected for c in r["citations"]])})
    return {"schema": "records-understanding-digest-v1", "run_id": run_id,
            "provenance": {"detectors": unique(provenance), "requests": request_provenance,
                           "source_bindings_sha256": digest(source_bindings),
                           "joins_revision_sha256": joins.revision_sha256},
            "aggregates": aggregates, "observations": observations, "detector_statuses": unique(status_rows),
            "unresolved": unique(unresolved), "requests": unique(request_rows),
            "request_holds": [{"hold_sha256": digest(h), "status": "UNRESOLVED"} for h in requests["holds"]],
            "legal_conclusion": "NOT_ASSESSED", "semantic_assessment": "NOT_ASSESSED", **FLAGS}


def markdown(report):
    lines = ["# Private usage digest", "", "REVIEW REQUIRED; no semantic or legal acceptance.",
             "publication_ready=false; qualified=false; activation_allowed=false.", ""]
    for agency in sorted({r["agency_id"] for r in report["aggregates"]}):
        lines += ["## Agency ID: " + agency, ""]
        for row in report["aggregates"]:
            if row["agency_id"] == agency:
                lines.append("- " + row["detector"] + ": " + str(row["cited_observations"]) + " cited triage observations")
                for cite in row["citations"]:
                    lines.append("  - source " + cite["original_sha256"] + "; locator SHA256 " + cite["locator_sha256"])
        lines.append("")
    lines += ["## Unresolved and inapplicable evidence", ""]
    for row in report["detector_statuses"]:
        lines.append("- " + row["subject_sha256"] + " / " + row["detector"] + ": " + row["status"] + " / " + row["reason"])
    lines += ["", "Unresolved candidate rows: " + str(len(report["unresolved"])),
              "Request candidates (not legal deadlines or nonresponse findings): " + str(len(report["requests"])),
              "Request holds: " + str(len(report["request_holds"])), "", "## Provenance", "", canonical(report["provenance"]), ""]
    # Complete structured candidates/statuses remain inspectable without free text.
    lines += ["## Request candidates and unresolved rows", "", canonical({
        k: report[k] for k in ("requests", "request_holds", "unresolved")}), ""]
    return "\n".join(lines)


def prepare_digest(*, plan, admitted_root=None, output_root=None, **inputs):
    """Validate the ENTIRE operation before the first write (also used by host)."""
    if not enabled(plan):
        return None
    need(plan.get("synthetic") is True, "synthetic_plan_required")
    output = validate_roots(admitted_root, output_root)
    report = project(**inputs)
    data = (canonical(report) + "\n").encode()
    text = markdown(report).encode()
    need(len(data) + len(text) <= MAX_BYTES, "output_byte_bound")
    return output, report, data, text


def commit_digest(prepared):
    """Exclusive private output. Never overwrite a prior run, including residue."""
    if prepared is None:
        return {"status": "disabled", "files_written": 0}
    output, report, data, text = prepared
    # Recheck ancestry immediately before mkdir; no recursive ancestor creation.
    validate_roots(output.parent, output)
    # Walk every directory component with O_NOFOLLOW, then use descriptor-
    # relative operations. A substituted symlink cannot redirect the write.
    root_fd = os.open(output.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in output.parent.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
            os.close(root_fd)
            root_fd = child
        os.mkdir(output.name, 0o700, dir_fd=root_fd)
        output_fd = os.open(output.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
        try:
            for name, value in (("digest.json", data), ("digest.md", text)):
                fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=output_fd)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(value)
                    stream.flush()
                    os.fsync(stream.fileno())
            os.fsync(output_fd)
        finally:
            os.close(output_fd)
        os.fsync(root_fd)
    finally:
        os.close(root_fd)
    unresolved = bool(report["unresolved"] or report["request_holds"] or
                      any(r["status"] == "blocked" for r in report["detector_statuses"]) or
                      any(r["state"] == "UNRESOLVED" for r in report["requests"]))
    return {"status": "unresolved" if unresolved else "written", "files_written": 2, **FLAGS}


def write_digest(**context):
    return commit_digest(prepare_digest(**context))
