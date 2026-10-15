"""Pure analytical revision and applicability contracts (ARCH04/05).

These functions read no files, call no models, and change no ledger state. Inputs
are private, source-bound snapshots supplied by the runner, not a new registry.
Hashes establish input identity, not the truth or completeness of those inputs.
"""
from datetime import date
import hashlib
import json
import re

SCHEMA = "records-context-revision-v1"
DATE_ROLES = ("conduct", "receipt", "disclosure", "retention")
STAGES = ("extract", "catalog", "detect", "review", "compare", "privacy")
VERSION_KEYS = frozenset({"extraction", "catalog", "detector", "digest", "model",
                          "prompt", "challenge_model", "challenge_prompt", "redaction",
                          "context", "applicability", "privacy"})
UNRESOLVED = "VERSION_OR_APPLICABILITY_UNRESOLVED"
_SHA = re.compile(r"[0-9a-f]{64}\Z")


def _require(condition, code):
    if not condition:
        raise ValueError(code)


def canonical(value):
    """Unambiguous JSON: no coerced mapping keys, NaN, tuples or custom objects."""
    def check(item):
        if isinstance(item, dict):
            _require(all(type(key) is str for key in item), "json_string_keys_required")
            for child in item.values():
                check(child)
        elif isinstance(item, list):
            for child in item:
                check(child)
        else:
            _require(item is None or type(item) in (str, int, float, bool), "json_value_required")
    check(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("utf-8")


def fingerprint(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def _sha(value):
    _require(isinstance(value, str) and _SHA.fullmatch(value), "sha256_required")
    return value


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _date(value):
    _require(isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value),
             "calendar_date_required")
    return date.fromisoformat(value)


def _evidence(value):
    return (isinstance(value, list) and bool(value) and
            all(isinstance(ref, dict) and isinstance(ref.get("source_sha256"), str)
                and _SHA.fullmatch(ref["source_sha256"]) and _text(ref.get("locator"))
                for ref in value))


def event_context(evidence_context, dates=None):
    """Bind canonical occurrence/request/agency rows and separately sourced dates.

    Each date role is a list of {event_kind, start, end, evidence}; empty is unknown.
    Receipt/acquisition/production dates must never be copied into another role.
    Multiple or conflicting periods stay visible; applicability requires one
    unambiguous period. Retention always needs both endpoints. Dates are calendar
    dates already resolved in the source's jurisdiction, never UTC truncations.
    """
    _require(isinstance(evidence_context, dict) and evidence_context, "evidence_context_required")
    dates = {} if dates is None else dates
    _require(isinstance(dates, dict) and not set(dates) - set(DATE_ROLES), "date_roles_required")
    normalized = {}
    for role in DATE_ROLES:
        periods = dates.get(role, [])
        _require(isinstance(periods, list), "date_period_list_required")
        for period in periods:
            _require(isinstance(period, dict) and set(period) <= {"event_kind", "start", "end", "evidence"},
                     "date_period_fields")
            start, end = period.get("start"), period.get("end")
            if start is not None:
                _date(start)
            if end is not None:
                _date(end)
            _require(start is None or end is None or start <= end, "reversed_date_period")
        # Lists of assertions have no semantic ordering, but duplicates remain bound.
        normalized[role] = sorted(periods, key=canonical)
    payload = {"schema": "records-event-context-v1", "evidence_context": evidence_context,
               "dates": normalized}
    # Return an independent snapshot; caller mutations cannot change this identity.
    return json.loads(canonical(payload))


def rule_revision(rule):
    """Stable rule IDs are labels; the whole rule, including sources, is identity."""
    _require(isinstance(rule, dict) and _text(rule.get("rule_id")), "rule_id_required")
    return fingerprint(rule)


def _checked_context(context):
    _require(isinstance(context, dict) and set(context) == {"schema", "evidence_context", "dates"}
             and context["schema"] == "records-event-context-v1", "event_context_required")
    _require(event_context(context["evidence_context"], context["dates"]) == context,
             "noncanonical_event_context")
    return context


def build_revision(*, subject_sha256, extraction, context, rules, versions,
                   applicability_bases=None, rule_context=None):
    """Bind all supplied analytical dependencies, with reusable stage identities.

    extraction includes receipt/unit hashes and coverage, not only a parser name.
    rules is the complete candidate universe used by detection/narrative, including
    historical versions; rule_context carries package status/sources/overlays.
    versions uses explicit revisions (or 'disabled'), never credentials. A model
    alias alone cannot establish a pinned model revision. Additional version keys
    conservatively invalidate every analytical stage.
    """
    _sha(subject_sha256)
    _require(isinstance(extraction, dict) and extraction, "extraction_manifest_required")
    _sha(extraction.get("receipt_sha256"))
    _checked_context(context)
    _require(isinstance(rules, list), "rule_list_required")
    # Preserve actual input order: legacy detectors select the first citation hit.
    rule_hashes = [rule_revision(rule) for rule in rules]
    _require(isinstance(versions, dict) and VERSION_KEYS <= set(versions)
             and all(_text(value) for value in versions.values()), "analytical_versions_required")
    bases = [] if applicability_bases is None else applicability_bases
    _require(isinstance(bases, list), "applicability_basis_list_required")
    dependencies = {
        "subject_sha256": subject_sha256,
        "extraction": fingerprint(extraction),
        "evidence_context": fingerprint(context["evidence_context"]),
        "event_context": fingerprint(context),
        "rules": fingerprint({"revisions": rule_hashes, "context": rule_context}),
        "applicability_bases": fingerprint(sorted(bases, key=canonical)),
        "versions": dict(versions),
    }
    stage_hashes = _stage_hashes(dependencies)
    payload = {"schema": SCHEMA, "dependencies": dependencies, "stage_hashes": stage_hashes}
    return {**payload, "revision_sha256": fingerprint(payload)}


def _stage_hashes(dependencies):
    """Derive stage identities again when reading a stored manifest."""
    _require(isinstance(dependencies, dict) and set(dependencies) == {
        "subject_sha256", "extraction", "evidence_context", "event_context", "rules",
        "applicability_bases", "versions"}, "revision_dependencies_required")
    for key, value in dependencies.items():
        if key != "versions":
            _sha(value)
    subject_sha256, versions = dependencies["subject_sha256"], dependencies["versions"]
    _require(isinstance(versions, dict) and VERSION_KEYS <= set(versions)
             and all(_text(value) for value in versions.values()), "analytical_versions_required")
    extra_versions = {k: v for k, v in versions.items() if k not in VERSION_KEYS}
    stage_hashes = {}
    def stage(name, values, version_keys):
        stage_hashes[name] = fingerprint({
            "schema": SCHEMA, "stage": name, "original": subject_sha256,
            "dependencies": values, "extra_versions": extra_versions,
            "versions": {key: versions[key] for key in version_keys},
        })
    stage("extract", [dependencies["extraction"]], ["extraction"])
    stage("catalog", [stage_hashes["extract"], dependencies["evidence_context"],
                       dependencies["event_context"]], ["catalog", "context"])
    stage("detect", [stage_hashes["catalog"], dependencies["rules"]], ["detector"])
    stage("review", [stage_hashes["detect"], dependencies["event_context"]],
          ["digest", "model", "prompt", "challenge_model", "challenge_prompt", "redaction", "context"])
    stage("compare", [stage_hashes["review"], dependencies["applicability_bases"]], ["applicability"])
    stage("privacy", [stage_hashes["compare"]], ["privacy"])
    return stage_hashes


def _valid_revision(value):
    try:
        return (isinstance(value, dict) and value.get("schema") == SCHEMA
                and set(value) == {"schema", "dependencies", "stage_hashes", "revision_sha256"}
                and value["revision_sha256"] == fingerprint({k: v for k, v in value.items()
                                                            if k != "revision_sha256"})
                and value["stage_hashes"] == _stage_hashes(value["dependencies"]))
    except (KeyError, TypeError, ValueError):
        return False


def invalidation_plan(previous, current):
    """Return every changed stage, not only the first (detect does not reopen review).

    The integrator applies this through the ledger's trusted runner plus the shared
    publication authority. This pure plan never deletes historical receipts or
    grants approval. Legacy/unbound history is deliberately not cacheable.
    """
    _require(_valid_revision(current), "current_revision_invalid")
    valid_previous = bool(_valid_revision(previous))
    if valid_previous:
        _require(previous["dependencies"]["subject_sha256"] == current["dependencies"]["subject_sha256"],
                 "revision_subject_mismatch")
    changed = [name for name in STAGES if not valid_previous or
               previous["stage_hashes"][name] != current["stage_hashes"][name]]
    return {"schema": "records-context-invalidation-v1", "stages": changed,
            "revoke_approval": bool(changed), "retain_history": True,
            "reason": "analytical_dependencies_changed" if valid_previous else "unbound_prior_revision",
            "previous_revision_sha256": previous["revision_sha256"] if valid_previous else None,
            "revision_sha256": current["revision_sha256"]}


def resolve_applicability(rule_id, rules, context, bases=()):
    """Resolve one duty family, conservatively, using source-bound sidecar bases.

    Each basis binds rule_revision_sha256 and context_sha256, selects event_role,
    and supplies version_verified/version_evidence plus scope, scope_checks and
    rationale. Each scope check (jurisdiction, regulated_role, policy_adoption)
    has status applies/does_not_apply/unknown and source evidence. No field is inferred from
    the rule's actor text, a keyword hit, mail date, acquisition date or today's date.
    Sidecars are assertions for a trusted adapter to verify, not review authority.
    """
    _require(_text(rule_id), "rule_id_required")
    _checked_context(context)
    _require(isinstance(rules, list), "rule_list_required")
    for rule in rules:
        rule_revision(rule)
    _require(isinstance(bases, (list, tuple)), "applicability_basis_list_required")
    context_hash = fingerprint(context)
    candidates = {rule_revision(rule): rule for rule in rules if rule.get("rule_id") == rule_id}
    result = {"rule_id": rule_id, "classification": UNRESOLVED, "applicability": "unknown",
              "context_sha256": context_hash, "rule_revision_sha256": None,
              "event_role": None, "event_period": None, "reasons": []}
    if not candidates:
        return {**result, "reasons": ["rule_version_missing"]}
    covering, excluded, gaps, roles = [], [], [], set()
    for revision, rule in sorted(candidates.items()):
        matching = [b for b in bases if isinstance(b, dict) and
                    b.get("rule_revision_sha256") == revision and b.get("context_sha256") == context_hash]
        if len(matching) != 1:
            gaps.append("missing_or_ambiguous_applicability_basis")
            continue
        basis = matching[0]
        role = basis.get("event_role")
        if role not in DATE_ROLES:
            gaps.append("controlling_event_unresolved")
            continue
        roles.add(role)
        if (basis.get("version_verified") is not True or not _evidence(basis.get("version_evidence"))
                or not _text(basis.get("rationale"))):
            gaps.append("rule_version_unverified")
            continue
        periods = context["dates"][role]
        if len(periods) != 1:
            gaps.append("event_date_missing_or_ambiguous")
            continue
        period = periods[0]
        start, end = period.get("start"), period.get("end")
        if (start is None or (role == "retention" and end is None)
                or not _evidence(period.get("evidence")) or not _text(period.get("event_kind"))
                or basis.get("event_kind") != period["event_kind"]):
            gaps.append("event_date_not_source_bound")
            continue
        end = end or start
        try:
            _require("effective_to" in rule, "rule_end_boundary_unknown")
            rule_start = _date(rule.get("effective_from"))
            rule_end = _date(rule["effective_to"]) if rule.get("effective_to") is not None else date.max
            _require(rule_start <= rule_end, "reversed_rule_interval")
        except (ValueError, TypeError):
            gaps.append("rule_effective_interval_unresolved")
            continue
        event_start, event_end = _date(start), _date(end)
        if event_end < rule_start or event_start > rule_end:
            excluded.append(revision)
            continue
        if not (rule_start <= event_start <= event_end <= rule_end):
            gaps.append("event_period_crosses_rule_versions")
            continue
        checks = basis.get("scope_checks")
        valid_checks = (isinstance(checks, dict) and
                        set(checks) == {"jurisdiction", "regulated_role", "policy_adoption"} and
                        all(isinstance(check, dict) and check.get("status") in ("applies", "does_not_apply")
                            and _evidence(check.get("evidence")) for check in checks.values()))
        scope = ("does_not_apply" if valid_checks and any(check["status"] == "does_not_apply"
                                                        for check in checks.values()) else "applies")
        if not valid_checks or basis.get("scope") != scope:
            gaps.append("jurisdiction_role_or_scope_unresolved")
            continue
        covering.append((revision, basis, period))
    if len(roles) > 1:
        gaps.append("conflicting_controlling_events")
    if len(covering) > 1:
        gaps.append("overlapping_rule_versions")
    if gaps:
        return {**result, "reasons": sorted(set(gaps))}
    if not covering:
        return {**result, "reasons": ["historical_version_or_coverage_unresolved"],
                "excluded_revision_sha256s": excluded}
    revision, basis, period = covering[0]
    applies = basis["scope"] == "applies"
    return {**result, "classification": "LEGAL_REVIEW_REQUIRED" if applies else "NOT_APPLICABLE",
            "applicability": "applies" if applies else "does_not_apply",
            "rule_revision_sha256": revision, "event_role": basis["event_role"],
            "event_period": period, "basis_sha256": fingerprint(basis),
            "reasons": ["source_bound_scope_requires_legal_review" if applies else "source_bound_scope_exclusion"]}


def compare_applicability(duty_ids, rules, context, bases=()):
    """An empty matched-duty list is a coverage gap, never proof of inapplicability."""
    _require(isinstance(duty_ids, list) and all(_text(value) for value in duty_ids), "duty_ids_required")
    rows = [resolve_applicability(rule_id, rules, context, bases) for rule_id in sorted(set(duty_ids))]
    unresolved = not rows or any(row["classification"] == UNRESOLVED for row in rows)
    return {"schema": "records-applicability-v1", "classification": UNRESOLVED if unresolved else "LEGAL_REVIEW_REQUIRED",
            "status": "blocked" if unresolved else "requires_legal_review", "comparisons": rows,
            "reasons": ["no_matched_duties_is_not_inapplicability"] if not rows else []}
