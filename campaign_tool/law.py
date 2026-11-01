"""Jurisdiction law packages: load, validate, query, and render request drafts.

Packages live at jurisdictions/<country>-<state>/package.json and follow
schemas/law-package.schema.json. This module uses only the standard library
and never touches the network. Validation is a hand-written subset of JSON
Schema (type, const, enum, pattern, required, additionalProperties, items,
minItems, minLength, minimum, and local ``$ref`` into ``$defs``), which is
enough to check every rule in the law-package schema exactly.

A package with ``status: draft`` can be previewed, but nothing derived from
it is a reviewed legal conclusion. ``deadline`` models calendar days or
business days (Saturday and Sunday skipped only). Results are provisional
reminders, not legal due dates: public holidays, actual receipt and lawful
extension prerequisites are not established by this arithmetic. The CLI's
``deadline`` reminders are provisional too; see ``HOLIDAY_NOTE``.
"""
import argparse
import csv
import json
import os
import re
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
RESOURCE_ROOT = Path(__file__).resolve().parent / "_resources"
SCHEMA_PATH = RESOURCE_ROOT / "schemas" / "law-package.schema.json"
JURISDICTIONS = RESOURCE_ROOT / "jurisdictions"
TEMPLATE_PATH = RESOURCE_ROOT / "templates" / "records-request.md"
JURISDICTION_PATTERN = re.compile(r"^[a-z]{2}-[a-z]{2}$")
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
HOLIDAY_NOTE = ("Provisional reminders only, not legal due dates. Business-day "
                "counts skip Saturday and Sunday only; public holidays are not modeled. "
                "Confirm actual receipt, applicable last-day holiday exclusions and "
                "lawful extension notice before any lateness conclusion.")

_TYPES = {
    "object": dict, "array": list, "string": str, "integer": int,
    "number": (int, float), "boolean": bool, "null": type(None),
}


def load_schema(path=None):
    return json.loads(Path(path or SCHEMA_PATH).read_text(encoding="utf-8"))


def _matches_type(value, name):
    if name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if name == "boolean":
        return isinstance(value, bool)
    return isinstance(value, _TYPES[name])


def _resolve(schema, root):
    ref = schema.get("$ref")
    if ref is None:
        return schema
    if not ref.startswith("#/"):
        raise ValueError("Unsupported schema $ref: " + ref)
    node = root
    for part in ref[2:].split("/"):
        node = node[part]
    merged = dict(node)
    merged.update({k: v for k, v in schema.items() if k != "$ref"})
    return merged


def validate(document, schema, root=None, where="$"):
    """Check ``document`` against the JSON-schema subset; raise ValueError."""
    root = root if root is not None else schema
    schema = _resolve(schema, root)
    if "const" in schema and document != schema["const"]:
        raise ValueError(f"{where}: expected constant {schema['const']!r}, got {document!r}")
    if "enum" in schema and document not in schema["enum"]:
        raise ValueError(f"{where}: {document!r} is not one of {schema['enum']}")
    declared = schema.get("type")
    if declared is not None:
        names = declared if isinstance(declared, list) else [declared]
        if not any(_matches_type(document, name) for name in names):
            raise ValueError(f"{where}: expected type {'|'.join(names)}, got "
                             f"{type(document).__name__}")
    if isinstance(document, str):
        if "pattern" in schema and not re.search(schema["pattern"], document):
            raise ValueError(f"{where}: {document!r} does not match pattern {schema['pattern']!r}")
        if len(document) < schema.get("minLength", 0):
            raise ValueError(f"{where}: string shorter than minLength {schema['minLength']}")
    if _matches_type(document, "number") and "minimum" in schema and document < schema["minimum"]:
        raise ValueError(f"{where}: {document!r} is below minimum {schema['minimum']}")
    if isinstance(document, dict):
        for key in schema.get("required", []):
            if key not in document:
                raise ValueError(f"{where}: missing required field {key!r}")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            extra = sorted(set(document) - set(properties))
            if extra:
                raise ValueError(f"{where}: unexpected field(s) {extra}")
        for key, value in document.items():
            if key in properties:
                validate(value, properties[key], root, f"{where}.{key}")
    if isinstance(document, list):
        if len(document) < schema.get("minItems", 0):
            raise ValueError(f"{where}: fewer than minItems {schema['minItems']} entries")
        if "items" in schema:
            for index, entry in enumerate(document):
                validate(entry, schema["items"], root, f"{where}[{index}]")


def parse_date(text, where="date"):
    if not isinstance(text, str) or not DATE_PATTERN.match(text):
        raise ValueError(f"{where}: expected YYYY-MM-DD, got {text!r}")
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{where}: {exc}") from None


def _semantic_checks(package, jurisdiction):
    if package["jurisdiction"] != jurisdiction:
        raise ValueError(f"$.jurisdiction: {package['jurisdiction']!r} does not match "
                         f"directory {jurisdiction!r}")
    if package["status"] == "reviewed":
        if not package["reviewed_by"]:
            raise ValueError("$.reviewed_by: a reviewed package needs at least one reviewer")
        if package["reviewed_at"] is None:
            raise ValueError("$.reviewed_at: a reviewed package needs a review date")
    if package["reviewed_at"] is not None:
        parse_date(package["reviewed_at"], "$.reviewed_at")
    for source in package["records_law"]["sources"]:
        parse_date(source["accessed"], "$.records_law.sources[].accessed")
    seen = set()
    for index, rule in enumerate(package["rules"]):
        where = f"$.rules[{index}]"
        if rule["rule_id"] in seen:
            raise ValueError(f"{where}.rule_id: duplicate {rule['rule_id']!r}")
        seen.add(rule["rule_id"])
        if not rule["rule_id"].startswith(jurisdiction[3:] + "-"):
            raise ValueError(f"{where}.rule_id: must start with {jurisdiction[3:]!r}-")
        start = parse_date(rule["effective_from"], where + ".effective_from")
        if rule["effective_to"] is not None:
            end = parse_date(rule["effective_to"], where + ".effective_to")
            if end < start:
                raise ValueError(f"{where}: effective_to precedes effective_from")
        for source in rule["sources"]:
            parse_date(source["accessed"], where + ".sources[].accessed")
    scopes = set()
    for index, scope in enumerate(package["request_scopes"]):
        where = f"$.request_scopes[{index}]"
        if scope["scope_id"] in scopes:
            raise ValueError(f"{where}.scope_id: duplicate {scope['scope_id']!r}")
        scopes.add(scope["scope_id"])
        unknown = sorted(set(scope["rule_ids"]) - seen)
        if unknown:
            raise ValueError(f"{where}.rule_ids: unknown rule id(s) {unknown}")


def _valid_jurisdiction(jurisdiction):
    """True only for a whole lowercase ``cc-ss`` string (no trailing newline)."""
    return isinstance(jurisdiction, str) and JURISDICTION_PATTERN.fullmatch(jurisdiction) is not None


def package_path(jurisdiction, base=None):
    if not _valid_jurisdiction(jurisdiction):
        raise ValueError(f"Jurisdiction must look like 'us-ca', got {jurisdiction!r}")
    return Path(base or JURISDICTIONS) / jurisdiction / "package.json"


def load_package(jurisdiction, base=None, schema_path=None):
    """Read and validate jurisdictions/<jurisdiction>/package.json."""
    path = package_path(jurisdiction, base)
    if not path.is_file():
        raise ValueError(f"No law package at {path}")
    try:
        package = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path}: invalid JSON: {exc}") from None
    validate(package, load_schema(schema_path))
    _semantic_checks(package, jurisdiction)
    return package


def package_status(jurisdiction, base=None):
    """Return ('draft'|'reviewed'|'missing', error_or_None) without raising."""
    try:
        path = package_path(jurisdiction, base)
    except ValueError as exc:
        return "missing", str(exc)
    if not path.is_file():
        return "missing", None
    try:
        return load_package(jurisdiction, base)["status"], None
    except ValueError as exc:
        return "missing", str(exc)


def rules_in_force(package, event_date):
    """Rules whose effective interval contains ``event_date`` (inclusive)."""
    if isinstance(event_date, str):
        event_date = parse_date(event_date, "event_date")
    if not isinstance(event_date, date):
        raise ValueError("event_date must be a date or YYYY-MM-DD string")
    result = []
    for rule in package["rules"]:
        start = date.fromisoformat(rule["effective_from"])
        end = rule["effective_to"] and date.fromisoformat(rule["effective_to"])
        if start <= event_date and (end is None or event_date <= end):
            result.append(rule)
    return result


def _add_days(start, days, day_type):
    # Compare integers before constructing timedelta: schema-valid counts have
    # no upper limit, whereas both date and timedelta have finite ranges.
    available = date.max.toordinal() - start.toordinal()
    if day_type == "calendar":
        if days > available:
            raise ValueError("law_deadline_out_of_range")
        return start + timedelta(days=days)

    # Count weekdays remaining in the date domain, with at most six integer
    # remainder checks and no date stepping, even for an impossible huge count.
    weeks, tail = divmod(available, 7)
    weekday = start.weekday()
    capacity = weeks * 5 + sum((weekday + offset) % 7 < 5
                               for offset in range(1, tail + 1))
    if days > capacity:
        raise ValueError("law_deadline_out_of_range")
    if days == 0:
        return start

    # A weekend start reaches Monday as its first counted business day.
    offset = 0
    if weekday >= 5:
        offset = 7 - weekday
        weekday = 0
        days -= 1
    weeks, remainder = divmod(days, 5)
    offset += weeks * 7 + remainder
    if weekday + remainder >= 5:
        offset += 2
    return start + timedelta(days=offset)


def deadline(package, sent_date, extension=False):
    """Provisional determination reminder counted from ``sent_date``.

    Uses ``records_law.determination_days`` plus, when ``extension`` is true,
    ``determination_extension_days``. Calendar days count every day. Business
    days skip Saturday and Sunday only (see HOLIDAY_NOTE). The statutory
    period usually runs from the agency's receipt, which may be later than
    the send date; this function does not model delivery delay, applicable
    last-day holiday exclusions, or lawful extension-notice prerequisites.
    Neither extension=True nor a returned date establishes legal timeliness.
    """
    if isinstance(sent_date, str):
        sent_date = parse_date(sent_date, "sent_date")
    if not isinstance(sent_date, date):
        raise ValueError("sent_date must be a date or YYYY-MM-DD string")
    law = package["records_law"]
    days = law["determination_days"]
    if extension:
        days += law["determination_extension_days"]
    return _add_days(sent_date, days, law["day_type"])


def request_scope(package, scope_id):
    for scope in package["request_scopes"]:
        if scope["scope_id"] == scope_id:
            return scope
    known = [scope["scope_id"] for scope in package["request_scopes"]]
    raise ValueError(f"Unknown request scope {scope_id!r}; known scopes: {known}")


def _clean(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    if "<" in value or ">" in value:
        raise ValueError(f"{name} must not contain angle brackets")
    if any(ord(ch) < 32 and ch not in "\n\t" for ch in value):
        raise ValueError(f"{name} must not contain control characters")
    return value.strip()


def _date_range_text(date_range):
    if isinstance(date_range, str):
        return _clean(date_range, "date_range")
    try:
        start, end = date_range
    except (TypeError, ValueError):
        raise ValueError("date_range must be a string or a (start, end) pair") from None
    parts = []
    for label, value in (("start", start), ("end", end)):
        if isinstance(value, date):
            value = value.isoformat()
        parts.append(parse_date(value, "date_range " + label).isoformat())
    if parts[1] < parts[0]:
        raise ValueError("date_range end precedes start")
    return f"{parts[0]} through {parts[1]}"


def render_request(package, scope, agency_name, custodian_hint, date_range,
                   fee_cap, requester_block):
    """Markdown draft in the style of templates/records-request.md.

    ``scope`` is a scope id or a scope dict from the package. All text
    arguments are checked for angle brackets and control characters so the
    output can be embedded safely. The result is a draft only: no send, no
    statutory deadline determination, no legal conclusion.
    """
    if isinstance(scope, str):
        scope = request_scope(package, scope)
    agency = _clean(agency_name, "agency_name")
    custodian = _clean(custodian_hint, "custodian_hint")
    fee = _clean(fee_cap, "fee_cap")
    requester = _clean(requester_block, "requester_block")
    span = _date_range_text(date_range)
    law = package["records_law"]
    cited = [rule for rule in package["rules"] if rule["rule_id"] in set(scope["rule_ids"])]
    lines = [
        "# Draft records request",
        "",
        "DRAFT ONLY. Verify the recipient on an official agency source and review the",
        "jurisdiction's applicable request process before sending. Law package status: "
        + package["status"] + ".",
        "",
        f"To: {custodian}, {agency}",
        f"Subject: Records concerning ALPR use by {agency}, {span}",
        "",
        f"Under the {law['name']}, {law['citation']}, please provide existing records",
        f"concerning {scope['title'].lower()}:",
    ]
    lines += [f"- {item}" for item in scope["items"]]
    lines += [
        "",
        f"The date range for this request is {span}.",
        "",
        "Electronic native formats are preferred where maintained. Please identify omitted",
        "exhibits and explain any withholding with its stated legal basis. Please advise",
        f"before incurring fees beyond {fee}. Rolling production is welcome.",
        "",
        requester,
        "",
        "## Statutory context (for the requester, not for sending)",
        "",
        f"- Determination period: {law['determination_days']} {law['day_type']} days from receipt, "
        f"with one extension of up to {law['determination_extension_days']} days in unusual circumstances.",
        f"- Fee basis: {law['fee_basis']}",
        f"- Enforcement: {law['appeal']}",
    ]
    if cited:
        lines += ["", "Rules this scope relates to:"]
        lines += [f"- {rule['rule_id']}: {rule['citation']} (review: {rule['review']})"
                  for rule in cited]
    lines += [
        "",
        "Operator checklist: record exact scope, recipient source, date, fee cap, send",
        "approval and delivery receipt. This draft does not determine a statutory deadline.",
        "",
    ]
    return "\n".join(lines)


def summary(package):
    counts = {"verified": 0, "likely": 0, "needs_attorney_review": 0}
    for rule in package["rules"]:
        counts[rule["review"]] += 1
    law = package["records_law"]
    return {
        "jurisdiction": package["jurisdiction"],
        "status": package["status"],
        "reviewed_by": list(package["reviewed_by"]),
        "reviewed_at": package["reviewed_at"],
        "records_law": {
            "name": law["name"], "citation": law["citation"],
            "determination_days": law["determination_days"],
            "determination_extension_days": law["determination_extension_days"],
            "day_type": law["day_type"],
        },
        "rule_count": len(package["rules"]),
        "review_counts": counts,
        "rules": [_rule_view(r) for r in package["rules"]],
        "request_scopes": [s["scope_id"] for s in package["request_scopes"]],
        "note": HOLIDAY_NOTE,
    }


def _rule_view(rule):
    return {"rule_id": rule["rule_id"], "citation": rule["citation"],
            "effective_from": rule["effective_from"], "effective_to": rule["effective_to"],
            "review": rule["review"]}


def _command_output(args, package):
    head = {"jurisdiction": package["jurisdiction"], "status": package["status"]}
    if args.command == "review-sheet":
        return _review_sheet_output(args, package)
    if args.command == "rules":
        rules = [r for r in rules_in_force(package, parse_date(args.on, "--on"))
                 if args.review is None or r["review"] == args.review]
        return {**head, "on": args.on, "rule_count": len(rules),
                "rules": [_rule_view(r) for r in rules], "note": HOLIDAY_NOTE}
    law = package["records_law"]
    days = law["determination_days"] + (law["determination_extension_days"] if args.extension else 0)
    reminder = deadline(package, parse_date(args.sent, "--sent"), args.extension)
    return {**head, "sent": args.sent, "extension": args.extension, "day_type": law["day_type"],
            "days": days, "reminder": reminder.isoformat(), "note": HOLIDAY_NOTE}


SHEET_COLUMNS = tuple("rule_id citation actor activity duty exceptions remedy effective_from effective_to "
                      "current_review source_titles source_urls oldest_access source_age_days "
                      "reviewer_conclusion reviewer_notes reviewer_name reviewed_on".split())


def review_sheet(package, as_of, stale_days=180):
    """records_law row, then rules in order (reviewer columns blank), and sources older than stale_days."""
    law = package["records_law"]
    rows, stale = [], []
    for rule in [{"rule_id": "records_law", "citation": law["citation"], "duty": law["name"],
                  "sources": law["sources"]}] + package["rules"]:
        sources = rule["sources"]
        ages = [(as_of - date.fromisoformat(s["accessed"])).days for s in sources]
        oldest = max(range(len(sources)), key=lambda i: (ages[i], -i))
        rows.append({**{column: rule.get(column) or "" for column in SHEET_COLUMNS},
                     "exceptions": " | ".join(rule.get("exceptions", [])), "current_review": rule.get("review", ""),
                     "source_titles": " | ".join(s["title"] for s in sources),
                     "source_urls": " | ".join(s["url"] for s in sources),
                     "oldest_access": sources[oldest]["accessed"], "source_age_days": str(ages[oldest])})
        for source, age in zip(sources, ages):
            if age > stale_days:
                stale.append({"rule_id": rule["rule_id"], **{k: source[k] for k in ("title", "url", "accessed")},
                              "age_days": age})
    return rows, stale


def write_review_sheet(out, rows):
    """Plain CSV, mode 0600, written atomically; never overwrites and never inside the repository."""
    target = Path(out).resolve()
    if target == REPO_ROOT or REPO_ROOT in target.parents:
        raise ValueError(f"--out must be outside the repository: {out}")
    if os.path.lexists(out):
        raise ValueError(f"--out already exists, not overwriting: {out}")
    handle, temporary = tempfile.mkstemp(prefix=".review-sheet-", dir=target.parent)  # mode 0600
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=SHEET_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
        os.link(temporary, target)  # refuses, never replaces, a target that appeared meanwhile
    except FileExistsError:
        raise ValueError(f"--out already exists, not overwriting: {out}") from None
    finally:
        os.unlink(temporary)


def _stale_days(text):
    if not re.fullmatch(r"\d{1,4}", text) or not 1 <= int(text) <= 3650:
        raise argparse.ArgumentTypeError(f"expected an integer 1..3650, got {text!r}")
    return int(text)


def _review_sheet_output(args, package):
    rows, stale = review_sheet(package, parse_date(args.as_of, "--as-of"), args.stale_days)
    write_review_sheet(args.out, rows)
    return {"jurisdiction": package["jurisdiction"], "status": package["status"],
            "reviewed_by": list(package["reviewed_by"]), "as_of": args.as_of, "stale_days": args.stale_days,
            "rule_count": len(package["rules"]), "rows_written": len(rows), "stale_sources": stale,
            "note": ("Worksheet for a human legal review. It is not legal advice and not a review; the package "
                     f"stays {package['status']} until a human reviewer records reviewed_by and reviewed_at.")}


def main(argv=None):
    parser = argparse.ArgumentParser(prog="campaign_tool.law", description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    show = commands.add_parser("show", help="Print a summary of a jurisdiction package")
    rules = commands.add_parser("rules", help="Print the rules in force on a date")
    rules.add_argument("--on", required=True, help="Event date, YYYY-MM-DD")
    rules.add_argument("--review", choices=("verified", "likely", "needs_attorney_review"))
    remind = commands.add_parser("deadline", help="Print the provisional reminder date")
    remind.add_argument("--sent", required=True, help="Send date, YYYY-MM-DD")
    remind.add_argument("--extension", action="store_true", help="Add the extension days")
    sheet = commands.add_parser("review-sheet", help="Write a private worksheet for a human legal review")
    sheet.add_argument("--out", required=True, help="New CSV path outside the repository")
    sheet.add_argument("--as-of", required=True, help="Date ages are counted to, YYYY-MM-DD")
    sheet.add_argument("--stale-days", type=_stale_days, default=180, help="1..3650 (default 180)")
    for sub in (show, rules, remind, sheet):
        sub.add_argument("jurisdiction")
        sub.add_argument("--base", help="Alternate jurisdictions directory")
    args = parser.parse_args(argv)
    try:
        package = load_package(args.jurisdiction, args.base)
        output = summary(package) if args.command == "show" else _command_output(args, package)
    except (OSError, ValueError) as exc:
        print("Stopped: " + str(exc), file=sys.stderr)
        return 1
    print(json.dumps(output, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
