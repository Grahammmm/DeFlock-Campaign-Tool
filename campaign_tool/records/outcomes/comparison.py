"""Private documentary comparison, not a compliance or legal determination.

Optional coordinator input: PROJECT/agency-fact-sheets.json, schema
records-agency-fact-sheets-v1, with agencies [{agency_id, name, jurisdiction,
regulated_roles, facts, requests}]. See synthetic fixture README for the shape.
The canonical ledger supplies the agency/request denominator, not proof of law
applicability, policy publication, receipt, or a lawful extension. Missing facts
stay unknown. No extraction, network, model calls, ledger writes or promotion.
"""
from datetime import date, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat

SCHEMA = "records-agency-comparison-v1"
INPUT_SCHEMA = "records-agency-fact-sheets-v1"
INPUT_NAME = "agency-fact-sheets.json"
MAX_BYTES = 8 * 1024 * 1024
DISCLAIMER = (
    "Private documentary screening; REVIEW REQUIRED, not a legal conclusion. "
    "meets means only the supplied cited documentary criteria match; gap means "
    "a cited documentary discrepancy, not a violation; unknown means unresolved. "
    "Missing production or redaction is not proof of absent controls. "
    "Every row needs attorney review. The us-ca law package remains draft; "
    "current law, scope, exceptions, policy versions and practice need review."
)
BASE_URL = "https://leginfo.legislature.ca.gov/faces/codes_displaySection.xhtml"
# Requirements are documentary predicates, never universal retention limits or
# audit frequencies. Based on the existing draft us-ca package, not new law.
CRITERIA = {
    "retention_practice": ("1798.90.51(b); 1798.90.53(b)",
        "Compare observed retention only to supplied operative policy bounds for the same data layer; policy modality and exceptions require review.", ()),
    "sharing_practice": ("1798.90.51(b); 1798.90.53(b); 1798.90.55(b)",
        "Reconcile cited actual disclosures (not configured network access) to operative policy recipient, purpose and approval restrictions; not a legal recipient determination.",
        ("recipient_scope_matches_policy", "purpose_matches_policy", "approval_process_matches_policy")),
    "audit_practice": ("1798.90.53(b)",
        "Reconcile complete cited native audit evidence to the operative policy's audit process; missing production is unknown, not proof no audit occurred.",
        ("process_followed",)),
    "usage_policy_published": ("1798.90.51(b); 1798.90.53(b)",
        "Written policy publicly available; conspicuous online if a website exists.",
        ("written_public",)),
    "authorized_purposes": ("1798.90.51(b); 1798.90.53(b)",
        "Policy identifies authorized purposes.", ("purposes_documented",)),
    "authorized_users_training": ("1798.90.51(b); 1798.90.53(b)",
        "Policy identifies authorized personnel/designations and training requirements.",
        ("authorized_designations_documented", "training_requirements_documented")),
    "retention": ("1798.90.51(b); 1798.90.53(b)",
        "Policy states retention length and destruction decision process for the identified data layer; no universal numerical limit asserted.",
        ("length_documented", "destruction_process_documented")),
    "sharing_limits": ("1798.90.51(b); 1798.90.53(b); 1798.90.55(b)",
        "Policy identifies sharing purposes, processes and restrictions; public-agency recipient scope, actual disclosure and exceptions need separate review.",
        ("purposes_documented", "process_documented", "restrictions_documented")),
    "audit_process": ("1798.90.53(b)",
        "End-user policy identifies monitoring and a periodic system-audit process; no fixed audit interval asserted.",
        ("monitoring_documented", "periodic_audit_process_documented")),
    "access_logs": ("1798.90.52(a)-(b)",
        "Operator access records include date/time, query elements, username/affiliation and purpose; use limited to authorized policy purposes.",
        ("date_time", "query_elements", "username", "affiliation", "purpose", "authorized_purpose_control")),
    "custodian": ("1798.90.51(b); 1798.90.53(b)",
        "Policy identifies responsible custodian/owner title.", ("title_documented",)),
    "accuracy_correction": ("1798.90.51(b); 1798.90.53(b)",
        "Policy describes reasonable accuracy and error-correction measures.", ("measures_documented",)),
    "safeguards": ("1798.90.51(a); 1798.90.53(a)",
        "Operational, administrative, technical and physical safeguards are documented; adequacy not assessed.",
        ("operational", "administrative", "technical", "physical")),
    "public_comment": ("1798.90.55(a)",
        "Public-agency operator provided public comment at a regular governing-body meeting before implementation.",
        ("regular_meeting", "public_comment_opportunity", "before_implementation")),
}


def citation(section, law="CIV"):
    number = section.split(";")[0].split("(")[0]
    return {"label": ("Civ. Code §§ " if law == "CIV" else "Gov. Code § ") + section,
            "url": BASE_URL + f"?lawCode={law}&sectionNum={number}.",
            "baseline": "jurisdictions/us-ca/package.json (draft)"}


def _cites(value):
    return (type(value) is list and bool(value) and all(
        type(c) is dict and all(isinstance(c.get(k), str) and str(c[k]).strip()
                               for k in ("source_id", "locator")) for c in value))


def _day(value):
    if type(value) is not str or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("date_required")
    return date.fromisoformat(value)


def _qualified(agency, fact, field):
    roles = agency.get("regulated_roles", [])
    role = ("end_user" if field in ("audit_process", "audit_practice") else "operator" if field == "access_logs"
            else "public_agency" if field == "public_comment" else None)
    return (agency.get("jurisdiction") == "us-ca" and type(roles) is list
            and (role in roles if role else any(r in roles for r in ("operator", "end_user")))
            and (field != "public_comment" or "operator" in roles)
            and type(fact) is dict and fact.get("applicability_confirmed") is True
            and _cites(fact.get("citations")))


def _row(agency_id, field, section, expected, status="unknown", reason="missing_or_unqualified_evidence", **extra):
    return {"agency_id": agency_id, "field": field, "status": status,
            "citation": citation(section), "expected": expected, "reason": reason,
            "needs_attorney_review": True, "legal_conclusion": False,
            "evidence": [], **extra}


def _fact_row(agency, field, fact):
    section, expected, keys = CRITERIA[field]
    row = _row(agency["agency_id"], field, section, expected)
    if not _qualified(agency, fact, field):
        return row
    row["evidence"] = fact["citations"]
    if fact.get("state") != "observed":
        row["reason"] = "missing_redacted_or_unresolved_fact"
        return row
    value = fact.get("value")
    row["observed"] = value
    row["comparison_scope"] = "documentary_predicates_only_not_overall_practice"
    if type(value) is not dict:
        return row
    if field.endswith("_practice"):
        row["comparison_scope"] = "supplied_policy_and_cited_practice_only"
        if (value.get("native_coverage_complete") is not True
                or not isinstance(value.get("policy_version"), str) or not value["policy_version"].strip()
                or value.get("policy_modality") not in ("shall", "should")
                or value.get("exceptions_resolved") is not True or fact.get("counterevidence")):
            row["reason"] = "practice_scope_policy_or_exceptions_unresolved"
            return row
        if field == "sharing_practice" and value.get("event_type") != "disclosure":
            row["reason"] = "configured_access_is_not_actual_disclosure"
            return row
        if field == "retention_practice":
            observed, low, high = (value.get(k) for k in ("observed_days", "min_days", "max_days"))
            if (not isinstance(value.get("data_layer"), str) or not value["data_layer"].strip()
                    or value.get("policy_data_layer") != value["data_layer"]
                    or type(observed) is not int or observed < 0 or (low is None and high is None)
                    or any(v is not None and (type(v) is not int or v < 0) for v in (low, high))
                    or (low is not None and high is not None and low > high)):
                row["reason"] = "retention_bounds_or_data_layer_unresolved"
                return row
            matches = (low is None or observed >= low) and (high is None or observed <= high)
            row.update(status="meets" if matches else "gap",
                       reason="supplied_policy_" + value["policy_modality"] + ("_match" if matches else "_discrepancy"))
            return row
    if field == "retention" and not (type(value.get("data_layer")) is str and value["data_layer"].strip()):
        row["reason"] = "retention_data_layer_unresolved"
        return row
    keys = list(keys)
    if field == "usage_policy_published":
        if type(value.get("has_website")) is not bool:
            return row
        if value["has_website"]:
            keys.append("conspicuous_online")
    values = [value.get(k) for k in keys]
    # Incomplete or contradictory observations cannot certify even a single gap.
    if any(type(v) is not bool for v in values) or fact.get("counterevidence"):
        row["reason"] = "incomplete_or_contradictory_evidence"
    else:
        row.update(status="meets" if all(values) else "gap",
                   reason="documentary_criteria_match" if all(values) else "documentary_criterion_discrepancy")
        if field.endswith("_practice"):
            row["reason"] = "supplied_policy_" + value["policy_modality"] + ("_match" if all(values) else "_discrepancy")
    return row


def _cpra_row(agency, request, as_of):
    row = _row(agency["agency_id"], "cpra_response_deadline", "7922.535(a)-(c)",
               "Determination within 10 calendar days of confirmed receipt; a qualified written unusual-circumstances extension is at most 14 additional days. Not a production deadline.",
               request_id=request["request_id"])
    row["citation"] = citation("7922.535(a)-(c); 6800; 6700", "GOV")
    if not (agency.get("jurisdiction") == "us-ca" and "public_agency" in agency.get("regulated_roles", [])
            and request.get("applicability_confirmed") is True and _cites(request.get("citations"))):
        return row
    row["evidence"] = request["citations"]
    row["observed"] = {k: request.get(k) for k in (
        "received_on", "determination_state", "determination_on", "extension_claimed", "extension_days")}
    try:
        if (request.get("receipt_confirmed") is not True or request.get("calendar_confirmed") is not True
                or request.get("counterevidence")):
            raise ValueError("receipt_or_calendar_unresolved")
        received = _day(request.get("received_on"))
        start, end = _day(request.get("calendar_start")), _day(request.get("calendar_end"))
        if not start <= received <= as_of <= end:
            raise ValueError("calendar_coverage_or_request_date_unresolved")
        holidays = request.get("holidays")
        if type(holidays) is not list:
            raise ValueError("holiday_calendar_unresolved")
        holidays = {_day(d) for d in holidays}

        def rolled(day):
            for _ in range(367):
                if not start <= day <= end:
                    raise ValueError("calendar_coverage_insufficient")
                if day.weekday() < 5 and day not in holidays:
                    return day
                day += timedelta(days=1)
            raise ValueError("calendar_roll_bound")

        base = rolled(received + timedelta(days=10))
        extended = request.get("extension_claimed")
        if type(extended) is not bool:
            raise ValueError("extension_status_unresolved")
        due = base
        if extended:
            days = request.get("extension_days")
            if (type(days) is not int or not 1 <= days <= 14
                    or request.get("extension_notice_qualified") is not True
                    or not received <= _day(request.get("extension_notice_on")) <= min(base, as_of)):
                raise ValueError("extension_unqualified")
            due = rolled(base + timedelta(days=days))
        state = request.get("determination_state")
        if state == "made":
            assessed = _day(request.get("determination_on"))
            if not received <= assessed <= as_of:
                raise ValueError("determination_date_inconsistent")
            status = "meets" if assessed <= due else "gap"
        elif state == "pending" and request.get("determination_on") is None:
            # Before/on due is pending, not proof of timely determination.
            status = "gap" if as_of > due else "unknown"
        else:
            raise ValueError("determination_status_unresolved")
        row.update(status=status, base_due_on=base.isoformat(), due_on=due.isoformat(),
                   reason="documentary_timing_discrepancy" if status == "gap" else
                   "documentary_timing_match" if status == "meets" else "determination_still_pending")
        row["observed"].update(base_due_on=base.isoformat(), due_on=due.isoformat())
    except (ValueError, OverflowError):
        row["reason"] = "receipt_calendar_extension_or_determination_unresolved"
    return row


def compare(agencies, *, run_id, as_of):
    """Compare explicit cited fact predicates; never infer facts from prose/counts."""
    as_of = _day(as_of)
    rows = []
    for agency in sorted(agencies, key=lambda a: a["agency_id"]):
        facts = agency.get("facts", {})
        for field in CRITERIA:
            rows.append(_fact_row(agency, field, facts.get(field)))
        # Preserve unmapped fact-sheet fields rather than silently omit them.
        for field in sorted(set(facts) - set(CRITERIA)):
            rows.append(_row(agency["agency_id"], field, "1798.90.5; 1798.90.51–1798.90.55",
                             "No documentary comparison mapping supplied.", reason="unmapped_fact_sheet_field"))
        requests = agency.get("requests", [])
        if not requests:
            requests = [{"request_id": None}]
        rows.extend(_cpra_row(agency, request, as_of) for request in requests)
    return {"schema": SCHEMA, "run_id": run_id, "as_of": as_of.isoformat(),
            "review_required": True, "legal_conclusion": False, "disclaimer": DISCLAIMER,
            "agency_count": len(agencies), "rows": rows}


def _checked(path, *, directory=False, missing=False):
    """Refuse symlink components and special files before reading/writing."""
    path = Path(os.path.abspath(path))
    for item in (*reversed(path.parents), path):
        try:
            info = item.lstat()
        except FileNotFoundError:
            if missing:
                continue
            raise
        if stat.S_ISLNK(info.st_mode) or (item != path and not stat.S_ISDIR(info.st_mode)):
            raise OSError("comparison_unsafe_path")
        if item == path and not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
            raise OSError("comparison_wrong_file_type")
    return path


def _load(project):
    path = _checked(Path(project) / INPUT_NAME, missing=True)
    if not path.exists():
        return {"schema": INPUT_SCHEMA, "agencies": []}, "fact_sheets_missing"
    if path.stat().st_size > MAX_BYTES:
        raise OSError("comparison_input_bound")
    try:
        with path.open("rb") as source:
            raw = source.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError("input_bound")
        def unique(pairs):
            result = {}
            for key, item in pairs:
                if key in result:
                    raise ValueError("duplicate_json_key")
                result[key] = item
            return result
        value = json.loads(raw, object_pairs_hook=unique)
        if type(value) is not dict or value.get("schema") != INPUT_SCHEMA or type(value.get("agencies")) is not list:
            raise ValueError("input_schema")
        seen, request_ids = set(), set()
        for a in value["agencies"]:
            aid = a.get("agency_id") if type(a) is dict else None
            if type(aid) is not str or not aid.strip() or aid in seen:
                raise ValueError("agency_identity")
            seen.add(aid)
            if type(a.get("facts", {})) is not dict or type(a.get("requests", [])) is not list or type(a.get("regulated_roles", [])) is not list:
                raise ValueError("agency_shape")
            ids = set()
            for r in a.get("requests", []):
                rid = r.get("request_id") if type(r) is dict else None
                if type(rid) is not str or not rid.strip() or rid in ids or rid in request_ids:
                    raise ValueError("request_identity")
                ids.add(rid)
                request_ids.add(rid)
        value["input_sha256"] = hashlib.sha256(raw).hexdigest()
        return value, "fact_sheets_supplied"
    except (ValueError, UnicodeError, RecursionError, TypeError):
        # Existing outputs error handler catches OSError; don't leak record text.
        raise OSError("comparison_input_invalid") from None


def markdown(report):
    def cell(value):
        # Render source identifiers as inert text, not HTML or Markdown links.
        text = str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        return re.sub(r"([\\`*_{}\[\]()!#])", r"\\\1", text).replace("|", "&#124;").replace("\n", " ").replace("\r", " ")
    lines = ["# Agency documentary comparison", "", DISCLAIMER, "",
             "Run: " + cell(report["run_id"]) + "; as of " + report["as_of"],
             "Input: " + report["input_status"],
             "Missing inventory tables: " + cell(", ".join(report.get("inventory_gaps", [])) or "none; supplied inventory only"), "",
             "| Agency | Field / request | Status | Observed | Expected | Reason | Citation | Evidence | needs_attorney_review |",
             "|---|---|---|---|---|---|---|---|---|"]
    for row in report["rows"]:
        evidence = "; ".join(c["source_id"] + ": " + c["locator"] for c in row["evidence"])
        field = row["field"] + (" / " + str(row["request_id"]) if row.get("request_id") else "")
        lines.append("| " + " | ".join(cell(v) for v in (row["agency_id"], field, row["status"],
            json.dumps(row.get("observed"), sort_keys=True),
            row["expected"], row["reason"], row["citation"]["label"] + " " + row["citation"]["url"],
            evidence or "not supplied", "true")) + " |")
    return "\n".join(lines) + "\n"


def write_run_comparison(project, run_id):
    """Outputs-stage hook. Read canonical inventory with the existing snapshot guard."""
    from ..run_stages import LedgerBusy, ledger_ro
    supplied, input_status = _load(project)
    inventory = {a["agency_id"]: a for a in supplied["agencies"]}
    request_owners = {r["request_id"]: a["agency_id"] for a in supplied["agencies"] for r in a.get("requests", [])}
    inventory_gaps = []
    try:
        _checked(Path(project) / "processing-v1/ledger.sqlite")
        with ledger_ro(Path(project) / "processing-v1") as db:
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            inventory_gaps = sorted({"agencies", "requests"} - tables)
            for aid, name in (db.execute("SELECT id, name FROM agencies ORDER BY id") if "agencies" in tables else []):
                inventory.setdefault(aid, {"agency_id": aid, "name": name})
            for rid, aid in (db.execute("SELECT id, agency_id FROM requests ORDER BY id") if "requests" in tables else []):
                if rid in request_owners and request_owners[rid] != aid:
                    raise OSError("comparison_request_agency_mismatch")
                a = inventory.setdefault(aid, {"agency_id": aid})
                requests = a.setdefault("requests", [])
                if not any(r["request_id"] == rid for r in requests):
                    requests.append({"request_id": rid})
    except LedgerBusy:
        raise OSError("comparison_ledger_busy") from None
    except sqlite3.Error:
        raise OSError("comparison_ledger_unreadable") from None
    report = compare(list(inventory.values()), run_id=run_id, as_of=datetime.now().astimezone().date().isoformat())
    report["input_status"] = input_status
    report["input_sha256"] = supplied.get("input_sha256")
    report["inventory_gaps"] = inventory_gaps
    report["inventory_complete"] = not inventory_gaps
    name = "".join(ch for ch in run_id if ch.isalnum() or ch in "-_.") or "run"
    if name in (".", ".."):
        raise OSError("comparison_run_id_invalid")
    directory = _checked(Path(project) / "run-outputs" / name, directory=True, missing=True)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    for suffix, text in (("json", json.dumps(report, sort_keys=True, indent=2) + "\n"), ("md", markdown(report))):
        target = _checked(directory / ("comparison." + suffix), missing=True)
        temporary = _checked(directory / (".comparison." + suffix + ".tmp"), missing=True)
        with temporary.open("w", encoding="utf-8") as output:
            output.write(text)
        os.replace(temporary, target)
    return report
