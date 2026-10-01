"""Deterministic detectors over explicit supplied units, joins and versioned rules."""

from collections import defaultdict
from datetime import date, datetime, timedelta
import hashlib
import json
import re

VERSION = "wp6-pure-detectors-v2"
DETECTORS = (
    "search-before-training", "purpose-quality", "external-sharing",
    "retention-over-policy", "audit-gap", "cpra-deadline", "volume-anomaly",
)
TYPES = {
    "search-before-training": {"search-log"}, "purpose-quality": {"search-log"},
    "external-sharing": {"sharing-list", "network-search"},
    "retention-over-policy": {"retention-setting"}, "audit-gap": {"audit"},
    "cpra-deadline": {"request"}, "volume-anomaly": {"search-log"},
}
MAX_UNITS = 5000
MAX_INPUT_BYTES = 8 * 1024 * 1024
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_CASE = re.compile(r"(?:case\s*#?\s*)?[0-9]{2,4}[-/][0-9]{3,12}\Z", re.I)
LIMITS = [
    "Triage signal only; not a legal conclusion or publication approval.",
    "A produced export may omit native fields; redaction is not absence.",
    "Nonblank purpose does not establish authorization.",
    "Sharing permission does not establish disclosure.",
    "Missing produced audits do not establish that no audit occurred.",
]


class Blocked(ValueError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def need(value, reason):
    if not value:
        raise Blocked(reason)


def sha(value):
    return isinstance(value, str) and _SHA.fullmatch(value) is not None


def day(value):
    need(isinstance(value, str), "date_missing")
    try:
        result = date.fromisoformat(value)
    except ValueError as exc:
        raise Blocked("date_invalid") from exc
    need(result.isoformat() == value, "date_not_canonical")
    return result


def moment(value):
    need(isinstance(value, str), "event_time_missing")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise Blocked("event_time_invalid") from exc
    need(result.tzinfo is not None and result.utcoffset() is not None, "event_timezone_missing")
    return result


def field(unit, key):
    fields = unit.get("fields", {})
    need(isinstance(fields, dict), "fields_not_object")
    if key not in fields:
        return "missing", None
    value = fields[key]
    if isinstance(value, dict) and "state" in value:
        state = value.get("state")
        need(state in ("observed", "missing", "blank", "redacted"), "unknown_field_state")
        if state != "observed":
            return state, None
        value = value.get("value")
    if value is None:
        return "missing", None
    if isinstance(value, str):
        if not value.strip():
            return "blank", value
        if value.strip().casefold() in ("[redacted]", "[withheld]"):
            return "redacted", None
    return "observed", value


def observed(unit, key, kind):
    state, value = field(unit, key)
    need(state == "observed", "field_" + key + "_" + state)
    valid = type(value) is kind if kind in (int, bool) else isinstance(value, kind)
    need(valid, "field_" + key + "_type")
    return value


def integer(parameters, key, low=0, high=1000000):
    value = parameters.get(key)
    need(type(value) is int and low <= value <= high, "rule_" + key + "_invalid")
    return value


def reference(unit):
    need(sha(unit.get("original_sha256")), "original_hash_missing")
    locator = unit.get("locator")
    need(isinstance(locator, dict) and bool(locator), "exact_locator_missing")
    need(len(locator) <= 16, "exact_locator_bound")
    def component(value):
        return ((type(value) is int and 0 <= value <= 2**63 - 1) or
                (isinstance(value, str) and bool(value.strip()) and len(value) <= 1024))
    for key, value in locator.items():
        need(isinstance(key, str) and bool(key.strip()) and len(key) <= 64,
             "exact_locator_key_invalid")
        if isinstance(value, list):
            need(0 < len(value) <= 32 and all(component(part) for part in value),
                 "exact_locator_path_invalid")
        else:
            need(component(value), "exact_locator_value_invalid")
    return {"original_sha256": unit["original_sha256"], "locator": locator}


def _records(units, joins):
    agencies = defaultdict(set)
    for join in joins:
        if (isinstance(join, dict) and join.get("status") == "typed" and sha(join.get("original_sha256"))
                and isinstance(join.get("agency_id"), str) and join["agency_id"].strip()):
            agencies[join["original_sha256"]].add(join["agency_id"])
    grouped = defaultdict(list)
    for unit in units:
        need(isinstance(unit, dict), "unit_not_object")
        candidates = agencies.get(unit.get("original_sha256"), set())
        agency = next(iter(candidates)) if len(candidates) == 1 else None
        identity = unit.get("event_id")
        kind = unit.get("record_type")
        valid_identity = isinstance(identity, str) and bool(identity.strip()) and len(identity) <= 256
        key = [agency, kind, identity] if valid_identity else ["unresolved", digest(unit)]
        grouped[canonical(key)].append((unit, agency, valid_identity))
    result = []
    for key, occurrences in sorted(grouped.items()):
        semantics = {canonical({k: v for k, v in unit.items() if k not in ("original_sha256", "locator")})
                     for unit, _, _ in occurrences}
        first, agency, valid_identity = occurrences[0]
        refs = {}
        error = None
        for unit, unit_agency, _ in occurrences:
            try:
                ref = reference(unit)
                refs[canonical(ref)] = ref
                need(unit_agency is not None, "typed_agency_join_missing_or_ambiguous")
                need(valid_identity, "stable_event_identity_missing")
            except Blocked as exc:
                error = str(exc)
        if len(semantics) > 1:
            error = "conflicting_duplicate_event"
        result.append({"key": key, "unit": first, "agency": agency,
                       "references": [refs[k] for k in sorted(refs)], "error": error,
                       "observation_sha256": digest(sorted(semantics)),
                       "duplicate_rows": len(occurrences) - 1})
    return result


def _rule(detector, record, rules):
    need(isinstance(rules.get("version"), str) and rules["version"].strip(), "rules_version_missing")
    event = moment(record["unit"].get("event_at"))
    matches, possible = [], False
    for rule in rules.get("entries", []):
        if not isinstance(rule, dict) or rule.get("detector") != detector or rule.get("agency_id") != record["agency"]:
            continue
        possible = True
        start = day(rule.get("effective_from"))
        end = day(rule["effective_to"]) if rule.get("effective_to") is not None else None
        need(end is None or end > start, "invalid_rule_interval")
        if start <= event.date() and (end is None or event.date() < end):
            need(isinstance(rule.get("rule_id"), str) and rule["rule_id"].strip() and
                 isinstance(rule.get("version"), str) and rule["version"].strip() and
                 sha(rule.get("source_sha256")) and isinstance(rule.get("parameters"), dict),
                 "rule_provenance_missing")
            matches.append(rule)
    if not matches and possible:
        raise Blocked("rule_not_effective")
    need(len(matches) == 1, "applicable_rule_missing_or_ambiguous")
    return matches[0], event


def _hit(detector, record, rule, severity, signal, values, extra=(), group=None, observations=()):
    references = {canonical(ref): ref for ref in record["references"]}
    for ref in extra:
        references[canonical(ref)] = ref
    refs = [references[key] for key in sorted(references)]
    identity = group if group is not None else record["key"]
    rule_fingerprint = digest(rule)
    observation_fingerprint = digest(sorted(set(observations or (record["observation_sha256"],))))
    return {
        "detector": detector, "detector_version": VERSION, "severity": severity,
        "signal": signal,
        "dedupe_key": digest([VERSION, detector, rule_fingerprint, identity, signal,
                              severity, values, observation_fingerprint]),
        "rule_fingerprint_sha256": rule_fingerprint,
        "semantic_observations_sha256": observation_fingerprint,
        "original_sha256": refs[0]["original_sha256"], "locator": refs[0]["locator"],
        "evidence": refs, "observed_values_private": values,
        "applicability": {"status": "applicable_to_supplied_context", "agency_id": record["agency"],
                          "rule_id": rule["rule_id"], "rule_version": rule["version"],
                          "rule_source_sha256": rule["source_sha256"],
                          "effective_from": rule["effective_from"], "effective_to": rule.get("effective_to")},
        "triage_only": True, "publication_ready": False, "limits": LIMITS,
    }


def _training(record, rule, event, records):
    unit = record["unit"]
    user = observed(unit, "user_id", str)
    matching, completions, refs = [], [], []
    for candidate in records:
        if candidate["unit"].get("record_type") != "training-roster" or candidate["agency"] != record["agency"]:
            continue
        state, identity = field(candidate["unit"], "user_id")
        if state == "observed" and identity == user:
            matching.append(candidate)
    need(matching, "training_context_missing")
    for candidate in matching:
        need(candidate["error"] is None, "training_context_invalid")
        completions.append(moment(observed(candidate["unit"], "training_completed_at", str)))
        refs.extend(candidate["references"])
    earliest = min(completions)
    if event < earliest:
        return [_hit("search-before-training", record, rule, 3, "search_precedes_supplied_training_completion",
                     {"event_at": unit["event_at"], "earliest_supplied_completion": earliest.isoformat(),
                      "user_id": user}, refs,
                     observations=[record["observation_sha256"]] +
                     [candidate["observation_sha256"] for candidate in matching])]
    return []


def _sharing(record, rule, event, records):
    unit, parameters = record["unit"], rule["parameters"]
    enabled = observed(unit, "access_enabled", bool)
    if not enabled:
        return []
    level = observed(unit, "recipient_level", str)
    need(level in ("federal", "state", "local", "private"), "recipient_level_unsupported")
    home = parameters.get("home_jurisdiction")
    need(isinstance(home, str) and home.strip(), "home_jurisdiction_missing")
    jurisdiction = observed(unit, "recipient_jurisdiction", str)
    if level == "federal" or jurisdiction != home:
        return [_hit("external-sharing", record, rule, 3, "external_permission_observed",
                     {"recipient_level": level, "recipient_jurisdiction": jurisdiction,
                      "home_jurisdiction": home, "access_enabled": True, "disclosure_established": False})]
    return []


def _retention(record, rule, event, records):
    unit, parameters = record["unit"], rule["parameters"]
    need(parameters.get("unit") == "calendar_days", "retention_rule_unit_unsupported")
    need(observed(unit, "retention_mode", str) == "days", "retention_mode_unsupported")
    setting = observed(unit, "retention_days", int)
    need(setting >= 0, "retention_value_invalid")
    maximum = integer(parameters, "maximum_days")
    if setting > maximum:
        return [_hit("retention-over-policy", record, rule, 3, "setting_exceeds_supplied_policy_period",
                     {"setting_days": setting, "policy_days": maximum})]
    return []


def _audit(record, rule, event, records):
    unit, parameters = record["unit"], rule["parameters"]
    start, end = day(observed(unit, "period_start", str)), day(observed(unit, "period_end", str))
    need(start < end and (end - start).days <= 3660, "audit_period_invalid_or_oversize")
    need(start >= day(rule["effective_from"]) and
         (rule.get("effective_to") is None or end <= day(rule["effective_to"])), "audit_period_crosses_policy_version")
    need(observed(unit, "production_complete", bool), "audit_production_scope_unknown")
    produced = observed(unit, "audit_dates", list)
    need(len(produced) <= 5000, "audit_dates_bound")
    dates = sorted({day(value) for value in produced})
    frequency = integer(parameters, "frequency_days", 1, 3660)
    first = day(parameters.get("first_due"))
    due = first
    if due < start:
        due += timedelta(days=((start - due).days + frequency - 1) // frequency * frequency)
    expected = []
    while due < end:
        expected.append(due)
        due += timedelta(days=frequency)
    need(expected, "no_due_audit_in_window")
    missing = []
    for index, due in enumerate(expected):
        previous = due - timedelta(days=frequency)
        # Adjacent windows are (previous, due]. Only the first clipped window
        # may include a production start equal to its previous boundary.
        if not any(start <= value <= due and
                   (value > previous or (index == 0 and value == start == previous))
                   for value in dates):
            missing.append(due.isoformat())
    if missing:
        severity = 3 if len(missing) == len(expected) else 2
        return [_hit("audit-gap", record, rule, severity, "produced_audit_documentation_gap",
                     {"period_start": start.isoformat(), "period_end_exclusive": end.isoformat(),
                      "due_dates": [d.isoformat() for d in expected], "missing_documentation_due_dates": missing,
                      "produced_dates": [d.isoformat() for d in dates], "absence_of_actual_audit_established": False})]
    return []


def _cpra(record, rule, event, rules, config):
    unit, p = record["unit"], rule["parameters"]
    received = day(observed(unit, "request_received", str))
    need(received == event.date(), "request_rule_event_mismatch")
    as_of = day(config.get("as_of"))
    need(as_of >= received, "as_of_precedes_request")
    need(p.get("counting") == "calendar" and type(p.get("exclude_received_day")) is bool,
         "deadline_counting_rule_unsupported")
    days = integer(p, "determination_days", 1, 366)
    extension_max = integer(p, "extension_days_max", 0, 366)
    reminder = integer(p, "reminder_days", 0, 30)
    weekends = p.get("weekend_days")
    need(isinstance(weekends, list) and all(type(x) is int and 0 <= x <= 6 for x in weekends)
         and len(set(weekends)) < 7, "weekend_rule_invalid")
    table_hash = p.get("holiday_table_sha256")
    need(sha(table_hash), "holiday_table_hash_missing")
    table = rules.get("holiday_tables", {}).get(table_hash)
    need(isinstance(table, dict) and digest(table) == table_hash, "holiday_table_hash_mismatch")
    need(isinstance(table.get("version"), str) and table["version"], "holiday_version_missing")
    dates = table.get("dates")
    need(isinstance(dates, list) and len(dates) <= 3660, "holiday_dates_invalid")
    holidays = {day(value) for value in dates}
    lower, upper = day(table.get("coverage_start")), day(table.get("coverage_end"))
    need(lower <= received <= as_of <= upper, "holiday_coverage_insufficient")
    roll = p.get("roll_due")
    need(roll in ("none", "next_business_day"), "deadline_roll_rule_unsupported")
    def rolled(value):
        for _ in range(367):
            need(lower <= value <= upper, "holiday_coverage_insufficient")
            if roll == "none" or (value.weekday() not in weekends and value not in holidays):
                return value
            value += timedelta(days=1)
        raise Blocked("deadline_roll_resource_bound")
    base = rolled(received + timedelta(days=days - (0 if p["exclude_received_day"] else 1)))
    extended = observed(unit, "extension_claimed", bool)
    deadline = base
    extension_days = 0
    if extended:
        extension_days = observed(unit, "extension_days", int)
        need(0 < extension_days <= extension_max, "extension_days_outside_supplied_rule")
        notice = day(observed(unit, "extension_notice_at", str))
        basis = observed(unit, "extension_basis", str)
        allowed = p.get("extension_allowed_bases")
        need(isinstance(allowed, list) and basis in allowed, "extension_basis_not_established")
        need(p.get("extension_notice_deadline") == "base_due" and received <= notice <= base,
             "extension_notice_outside_supplied_rule")
        need(p.get("extension_calculation") == "from_base_due", "extension_calculation_unsupported")
        deadline = rolled(base + timedelta(days=extension_days))
    state = observed(unit, "determination_state", str)
    need(state in ("made", "pending"), "determination_state_unsupported")
    determination = None
    if state == "made":
        determination = day(observed(unit, "determination_at", str))
        need(received <= determination <= as_of, "determination_date_inconsistent")
    assessment = determination if determination is not None else as_of
    severity = 2 if assessment > deadline else (
        1 if determination is None and 0 <= (deadline - as_of).days <= reminder else 0)
    if severity:
        return [_hit("cpra-deadline", record, rule, severity,
                     "determination_timing_observation" if severity == 2 else "determination_due_reminder",
                     {"request_received": received.isoformat(), "base_due": base.isoformat(),
                      "determination_due": deadline.isoformat(), "determination_state": state,
                      "determination_at": determination.isoformat() if determination else None,
                      "as_of": as_of.isoformat(), "extension_days": extension_days,
                      "holiday_table_sha256": table_hash, "production_is_not_determination": True,
                      "legal_conclusion": None})]
    return []


def _aggregate(detector, prepared):
    groups = defaultdict(list)
    for record, rule, event in prepared:
        unit = record["unit"]
        user = observed(unit, "user_id", str)
        base = (record["agency"], rule["rule_id"], rule["version"], digest(rule))
        if detector == "purpose-quality":
            purpose = observed(unit, "purpose", str).strip()
            generics = rule["parameters"].get("generic_reasons")
            need(isinstance(generics, list) and all(isinstance(x, str) and x.strip() for x in generics),
                 "generic_reason_rule_missing")
            qualified_bad = purpose.casefold() in {x.casefold() for x in generics} or bool(_CASE.fullmatch(purpose))
            for scope in (("overall",), ("user", user)):
                groups[base + scope].append((record, rule, event, qualified_bad))
        else:
            offset = integer(rule["parameters"], "timezone_offset_minutes", -840, 840)
            need(event.utcoffset().total_seconds() == offset * 60, "event_policy_timezone_mismatch")
            groups[base + ("user_day", user, event.date().isoformat())].append((record, rule, event, False))
    hits = []
    for group, entries in sorted(groups.items()):
        record, rule = entries[0][0], entries[0][1]
        refs = [ref for item in entries for ref in item[0]["references"]]
        if detector == "purpose-quality":
            bad, total = sum(int(item[3]) for item in entries), len(entries)
            severity = 3 if bad * 100 > total * 25 else (2 if bad * 100 > total * 5 else 0)
            values = {"scope": list(group[4:]), "qualified_problem_observations": bad,
                      "qualified_observations": total, "blank_missing_redacted_excluded": True}
            signal = "qualified_purpose_quality_rate"
        else:
            p = rule["parameters"]
            threshold = integer(p, "daily_count_threshold", 1)
            off_threshold = integer(p, "off_hours_threshold", 0)
            hours = p.get("business_hours")
            need(isinstance(hours, list) and len(hours) == 2 and all(type(x) is int for x in hours)
                 and 0 <= hours[0] < hours[1] <= 24, "business_hours_rule_invalid")
            off = sum(not hours[0] <= item[2].hour < hours[1] for item in entries)
            severity = 1 if len(entries) > threshold or off > off_threshold else 0
            values = {"scope": list(group[4:]), "unique_searches": len(entries),
                      "off_hours_searches": off, "daily_threshold": threshold, "off_hours_threshold": off_threshold}
            signal = "search_volume_triage"
        if severity:
            identity = [list(group), sorted(item[0]["key"] for item in entries)]
            hits.append(_hit(detector, record, rule, severity, signal, values, refs, identity,
                             observations=[item[0]["observation_sha256"] for item in entries]))
    return hits


def evaluate(detector, units, joins, rules, config):
    """Return private hits and a deterministic classified manifest; never performs IO."""
    if detector not in DETECTORS:
        raise ValueError("unknown detector")
    if not isinstance(units, list) or len(units) > MAX_UNITS or not isinstance(joins, list) or len(joins) > MAX_UNITS * 2:
        raise ValueError("input row bound")
    if not isinstance(rules, dict) or not isinstance(config, dict):
        raise ValueError("rules/config must be objects")
    if not isinstance(rules.get("entries", []), list) or len(rules.get("entries", [])) > 256:
        raise ValueError("rule bound")
    supplied = {"units": units, "joins": joins, "rules": rules, "config": config}
    raw = canonical(supplied)
    if len(raw.encode()) > MAX_INPUT_BYTES:
        raise ValueError("input byte bound")
    # Copy caller objects so outputs never alias or mutate caller-owned inputs.
    copied = json.loads(raw)
    units, joins, rules, config = (copied[k] for k in ("units", "joins", "rules", "config"))
    records = _records(units, joins)
    outcomes, hits, prepared = [], [], []
    eligible = 0
    for record in records:
        unit = record["unit"]
        outcome = {"event_key": digest(record["key"]), "evidence": record["references"],
                   "duplicate_rows": record["duplicate_rows"]}
        if unit.get("record_type") not in TYPES[detector]:
            outcome.update(disposition="skipped", reason="not_detector_input_type")
            outcomes.append(outcome)
            continue
        eligible += 1
        try:
            need(record["error"] is None, record["error"] or "invalid_record")
            rule, event = _rule(detector, record, rules)
            if detector in ("purpose-quality", "volume-anomaly"):
                # Validate this record in isolation first; malformed rows never pollute a group.
                _aggregate(detector, [(record, rule, event)])
                prepared.append((record, rule, event))
            elif detector == "search-before-training":
                hits.extend(_training(record, rule, event, records))
            elif detector == "external-sharing":
                hits.extend(_sharing(record, rule, event, records))
            elif detector == "retention-over-policy":
                hits.extend(_retention(record, rule, event, records))
            elif detector == "audit-gap":
                hits.extend(_audit(record, rule, event, records))
            else:
                hits.extend(_cpra(record, rule, event, rules, config))
            outcome.update(disposition="evaluated", reason="supplied_context_evaluated")
        except (Blocked, TypeError, KeyError, ValueError, OverflowError) as exc:
            reason = str(exc) if isinstance(exc, Blocked) else "unsupported_or_invalid_context"
            outcome.update(disposition="skipped" if reason == "rule_not_effective" else "blocked", reason=reason)
        outcomes.append(outcome)
    if prepared:
        hits.extend(_aggregate(detector, prepared))
    # Rule-pack identity is explicit in every result, including ruleless volume baselines.
    for hit in hits:
        hit["rules_version"] = rules.get("version")
    hits = sorted(hits, key=lambda hit: hit["dedupe_key"])
    counts = {name: sum(row["disposition"] == name for row in outcomes)
              for name in ("evaluated", "skipped", "blocked")}
    counts.update(input_rows=len(units), unique_records=len(records), eligible=eligible,
                  duplicate_rows=sum(record["duplicate_rows"] for record in records), hits=len(hits))
    manifest = {"schema": "pure-detector-manifest-v1", "detector": detector, "detector_version": VERSION,
                "input_sha256": digest(supplied), "rules_version": rules.get("version"),
                "rules_sha256": digest(rules), "config_sha256": digest(config),
                "counts": counts, "outcomes": outcomes, "hit_keys": [hit["dedupe_key"] for hit in hits],
                "stage_promotions": 0, "acceptance": "supplied_evidence_triage_only", "publication_ready": False}
    manifest["manifest_sha256"] = digest(manifest)
    return {"hits": hits, "manifest": manifest}


def run_all(units, joins, rules, config):
    return {detector: evaluate(detector, units, joins, rules, config) for detector in DETECTORS}
