"""Record requests an organizer sent by hand in kit/sent.json (date, channel, draft SHA-256); list
provisional due dates from ``law.deadline``. Nothing is sent from this module."""
import hashlib
import json
import os
import re
import unicodedata
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from . import kit, kit_check, law

CHANNELS = ("email", "portal", "mail")
KINDS = ("acknowledged", "extension", "determination", "produced", "denied", "closed", "fee_estimate",
         "clarification_requested", "clarified")
WAITING = "waiting on you since {} (the agency asked to clarify or narrow the request)"
WAIT_NOTE = (" Waiting on you: the agency asked to clarify or narrow the request; reply first. Not counted as past"
             " due; no claim that this pauses any legal period.")


def _waiting_since(steps):  # date of the latest clarification_requested not yet clarified (or closed), else None
    asked = [i for i, s in enumerate(steps) if s.get("kind") == "clarification_requested"]
    ended = asked and any(s.get("kind") in ("clarified", "closed") for s in steps[asked[-1] + 1:])
    return steps[asked[-1]].get("on") if asked and not ended else None


class WaitingOnYou(ValueError):  # kit-followup refusal while the agency waits on the organizer: exit 3
    exit_code = 3


class ReferenceConflict(ValueError):  # a different agency reference already stored: exit 3, nothing written
    exit_code = 3


REFERENCE = re.compile(r"[A-Za-z0-9_#.\-]{1,40}")  # the agency's own request number, e.g. R0123-110125


def _reference(reference, replace_reference):  # validated before anything is read or written
    if replace_reference and reference is None:
        raise ValueError("--replace-reference needs --reference")
    if reference is not None and (not isinstance(reference, str) or not REFERENCE.fullmatch(reference)):
        raise ValueError("--reference must be 1-40 letters, digits, - _ # or .")
    return reference


def _set_reference(entry, reference, replace_reference):  # stored once on the entry, never on a step
    stored = entry.get("reference")
    if reference is None or reference == stored:
        return
    if stored is not None and not replace_reference:
        raise ReferenceConflict(f"reference already recorded as {stored}; pass --replace-reference to change it")
    entry["reference"] = reference


def reference_lines(entry):
    """(subject suffix, first body line) quoting the stored agency reference; empty strings when none."""
    reference = entry.get("reference")
    return (f" (Your reference {reference})", f"Regarding your reference {reference},") if reference else ("", "")


AMOUNT = re.compile(r"[0-9]{1,6}(\.[0-9]{1,2})?")  # plain USD: no sign, exponent, NaN or thousands separator
AMOUNT_MAX_CENTS = 10_000_000  # $100,000.00
OVER_CAP = "over your fee cap: decide before paying"
NOTE_MAX = 200
RECORDS_MAX, PREFIXES_MAX = 200, 50  # linked records per produced step; prefixes listed per request
MAX_BYTES = 1 << 20
EARLIEST = date(2000, 1, 1)
SHA256 = re.compile(r"[0-9a-f]{64}")
STAMP = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00")  # UTC, as record_sent writes recorded_at
NOTE = "Provisional reminders only; the period runs from the agency's receipt. " + law.HOLIDAY_NOTE


def _day(value, pattern=kit.DATE, parse=date.fromisoformat):
    try:
        return parse(value) if isinstance(value, str) and pattern.fullmatch(value) else None
    except ValueError:
        return None


REQUEST = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")  # several requests per agency; "main" is the original one


def _request(request):
    if not isinstance(request, str) or not REQUEST.fullmatch(request):
        raise ValueError("--request must be 1-40 characters a-z, 0-9 or -, starting with a letter or digit")
    return request


def _key(agency_id, request="main"):  # sent.json key: the bare agency id for main (old files read unchanged)
    return agency_id if request == "main" else f"{agency_id}#{request}"


def _split(key):  # (agency_id, request) of a sent.json key; None for "#main" or a bad request name
    agency_id, mark, request = key.partition("#")
    if mark and (request == "main" or not REQUEST.fullmatch(request)):
        return None
    return agency_id, request or "main"


def _stem(agency_id, request="main"):  # draft file stem: requests/<agency>.md for main, <agency>--<request>.md else
    return agency_id if request == "main" else f"{agency_id}--{request}"


def _draft_path(agency_id, request="main"):
    return f"requests/{_stem(agency_id, request)}.md"


def _label(agency_id, request):
    return agency_id if request == "main" else f"{agency_id}/{request}"


def _entry_ok(key, entry):
    parts = _split(key)
    return (parts and kit.AGENCY_ID.match(parts[0]) and isinstance(entry, dict) and _day(entry.get("sent_on"))
            and entry.get("channel") in CHANNELS and entry.get("draft") == _draft_path(*parts)
            and isinstance(entry.get("draft_sha256"), str) and SHA256.fullmatch(entry["draft_sha256"])
            and _day(entry.get("recorded_at"), STAMP, datetime.fromisoformat) and isinstance(entry.get("steps"), list)
            and ("reference" not in entry or isinstance(entry["reference"], str)
                 and REFERENCE.fullmatch(entry["reference"])))


def read_sent(root):
    """Return the ``requests`` dict of kit/sent.json; {} when absent. Never resets a bad file."""
    path = Path(root) / "kit" / "sent.json"
    if not os.path.lexists(path):
        return {}

    def bad(reason):
        return ValueError("kit/sent.json unreadable: " + reason)
    if path.is_symlink() or not path.is_file():
        raise bad("not a regular file")
    if path.stat().st_size > MAX_BYTES:
        raise bad("larger than 1 MiB")
    try:
        document = json.loads(path.read_bytes().decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise bad("not JSON") from None
    if not isinstance(document, dict) or document.get("schema_version") != 1 \
            or not isinstance(document.get("requests"), dict):
        raise bad("expected schema_version 1 with a requests object")
    for agency_id, entry in document["requests"].items():
        if not _entry_ok(agency_id, entry):
            raise bad(f"malformed entry {agency_id!r}")
    return document["requests"]


def _draft(root, agency_id, request="main"):
    path = Path(root) / "kit" / _draft_path(agency_id, request)
    if path.is_symlink() or not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _check(root, agency, request):  # main: kit-check's own report; a named request: the same check on its draft
    if request == "main":
        return kit_check.agency_report(root, agency)
    text = (Path(root) / "kit" / _draft_path(agency["agency_id"], request)).read_text(encoding="utf-8",
                                                                                       errors="replace")
    hits = kit_check.draft_problems(text)
    return {"placeholder_count": len(hits), "ok": not hits and agency.get("verified") is True}


def record_sent(root, agency_id, sent_on, channel, *, request="main", replace=False, today=None, reference=None,
                replace_reference=False):
    """Record one hand-sent request in kit/sent.json and return its entry."""
    root, request = Path(root), _request(request)
    reference = _reference(reference, replace_reference)
    path = root / "kit" / "agencies.json"
    if not path.is_file() or path.is_symlink():
        raise ValueError("run kit first: kit/agencies.json is missing")
    agencies = kit._load_existing(path)
    agency = next((entry for entry in agencies if entry["agency_id"] == agency_id), None)
    if agency is None:
        raise ValueError(f"agency_id {agency_id!r} is not in kit/agencies.json")
    draft, stem = _draft_path(agency_id, request), _stem(agency_id, request)
    if request != "main" and any(entry["agency_id"] == stem for entry in agencies):
        raise ValueError(f"kit/requests/{stem}.md is another agency's draft; choose another --request name")
    digest = _draft(root, agency_id, request)
    if digest is None:
        raise ValueError(f"kit/requests/{stem}.md must be a regular file (not a symlink)")
    check = _check(root, agency, request)
    if not check["ok"]:
        raise ValueError(f"kit/requests/{stem}.md still has {check.get('placeholder_count', 0)} unfilled "
                         "placeholder(s) or an unverified recipient; run kit-check")
    sent = _day(sent_on)
    if sent is None:
        raise ValueError("--date must be a real YYYY-MM-DD date")
    if sent > (today or date.today()) or sent < EARLIEST:
        raise ValueError(f"--date {sent_on} must be between 2000-01-01 and today")
    if channel not in CHANNELS:
        raise ValueError("--channel must be one of " + ", ".join(CHANNELS))
    requests = read_sent(root)
    key, label = _key(agency_id, request), _label(agency_id, request)
    previous = requests.get(key)
    if previous and not replace:
        raise ValueError(f"{label} already recorded as sent on {previous['sent_on']}; use --replace to correct it")
    if previous and previous["steps"]:
        raise ValueError(f"{label} has recorded steps; its entry sent on {previous['sent_on']} cannot be replaced")
    entry = {"sent_on": sent_on, "channel": channel, "draft": draft, "draft_sha256": digest,
             "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "steps": []}
    if previous and "reference" in previous:  # a --replace correction keeps the stored reference
        entry["reference"] = previous["reference"]
    _set_reference(entry, reference, replace_reference)
    requests[key] = entry
    kit._write_json(root / "kit" / "sent.json",
                    {"schema_version": 1, "requests": dict(sorted(requests.items()))})
    return {"agency_id": agency_id, "request": request, **entry}


def _clean_note(note):
    if note is None:
        return None
    if not isinstance(note, str) or any(c in "<>" or unicodedata.category(c).startswith("C") for c in note):
        raise ValueError("--note must be plain text without <, > or control characters")
    text = " ".join(note.split())
    if not text or len(text) > NOTE_MAX:
        raise ValueError(f"--note must be 1-{NOTE_MAX} characters after collapsing whitespace")
    return text


def _step_records(step):  # the valid full record hashes linked on one stored step
    value = step.get("records", []) if isinstance(step, dict) else []
    return [h for h in value if isinstance(h, str) and SHA256.fullmatch(h)] if isinstance(value, list) else []


def _resolve_records(root, records, steps):  # every prefix is resolved before anything is written
    from .sources_cmd import resolve_hash
    linked = {digest: s["on"] for s in steps for digest in _step_records(s)}
    hashes = []
    for number, prefix in enumerate(records, 1):
        try:
            digest = resolve_hash(root, prefix)
        except ValueError as exc:
            raise ValueError(f"--record #{number}: " + str(exc).removeprefix("--sha256: ")) from None
        if digest in hashes:
            raise ValueError(f"--record #{number}: the same record is given twice")
        if digest in linked:
            raise ValueError(f"--record #{number}: already linked on {linked[digest]}")
        hashes.append(digest)
    return sorted(hashes)


def _amount_cents(amount):  # decimal string to whole cents with no float math; refused before anything is read
    if not isinstance(amount, str) or not AMOUNT.fullmatch(amount):
        raise ValueError("--amount must be US dollars like 245 or 245.50 (no sign, commas or exponent)")
    cents = int(Decimal(amount) * 100)
    if cents > AMOUNT_MAX_CENTS:
        raise ValueError("--amount must be from 0 to 100000")
    return cents


def _amount_ok(step):  # amount_cents only on fee_estimate steps, always there, an int from 0 to 10,000,000
    if step.get("kind") != "fee_estimate":
        return "amount_cents" not in step
    value = step.get("amount_cents")
    return type(value) is int and 0 <= value <= AMOUNT_MAX_CENTS


def _dollars(cents):
    return f"${cents // 100:,}.{cents % 100:02d}"


NOT_STATED = ("produced", "denied", "closed")  # kinds that never carry the agency's stated production date
STATED = "stated date passed — the agency's own date; provisional, not a legal deadline"


def _expected_ok(steps):  # expected_on only on the determination or a later restating step; a date not before its own
    seen = False
    for s in steps:
        seen = seen or s.get("kind") == "determination"
        if "expected_on" in s and (not seen or s.get("kind") in NOT_STATED or not _day(s["expected_on"])
                                   or not _day(s.get("on")) or s["expected_on"] < s["on"]):
            return False
    return True


def record_step(root, agency_id, on, kind, *, note=None, today=None, records=(), request="main", amount=None,
                expected_on=None, reference=None, replace_reference=False):
    """Append one dated agency response step to a hand-sent request and return the updated entry."""
    root, request = Path(root), _request(request)
    reference = _reference(reference, replace_reference)
    if kind not in KINDS:
        raise ValueError("--kind must be one of " + ", ".join(KINDS))
    records = list(records or ())  # produced only: ledger prefixes; full hashes stored, 12-char prefixes returned
    if records and kind != "produced":
        raise ValueError("--record is only for produced steps")
    if len(records) > RECORDS_MAX:
        raise ValueError(f"--record: at most {RECORDS_MAX} records per step")
    note = _clean_note(note)
    requests = read_sent(root)
    entry = requests.get(_key(agency_id, request)) if "#" not in agency_id else None
    if entry is None and request != "main":
        raise ValueError(f"no sent request {agency_id}/{request}; run kit-sent --request {request} first")
    if entry is None:
        raise ValueError(f"no hand-sent request recorded for {agency_id}; run kit-sent first")
    label = _label(agency_id, request)
    steps = entry["steps"]
    if not all(isinstance(s, dict) and s.get("kind") in KINDS and _day(s.get("on")) and _amount_ok(s)
               and _step_records(s) == s.get("records", []) for s in steps) or not _expected_ok(steps):
        raise ValueError(f"kit/sent.json unreadable: malformed steps for {label!r}")
    day = _day(on)
    if day is None:
        raise ValueError("--date must be a real YYYY-MM-DD date")
    if day < _day(entry["sent_on"]):
        raise ValueError(f"--date {on} is before the request was sent on {entry['sent_on']}")
    if steps and day < _day(steps[-1]["on"]):
        raise ValueError(f"--date {on} is before the previous step ({steps[-1]['kind']} on {steps[-1]['on']})")
    if day > (today or date.today()):
        raise ValueError(f"--date {on} is in the future")
    kinds = [s["kind"] for s in steps]
    if "closed" in kinds:
        raise ValueError(f"{label} is closed; no step can follow closed")
    if kind == "acknowledged" and kinds:
        raise ValueError("acknowledged is allowed only as the first step, once")
    if kind == "extension" and "extension" in kinds:
        raise ValueError("extension is allowed only once")
    if kind == "extension" and {"determination", "produced", "denied"} & set(kinds):
        raise ValueError("extension is not allowed after a determination, produced or denied step")
    if kind in ("determination", "denied") and kind in kinds:
        raise ValueError(f"{kind} is allowed at most once")
    if kind == "closed" and not kinds:
        raise ValueError("closed needs at least one prior step")
    if kind == "clarification_requested" and {"produced", "denied"} & set(kinds):
        raise ValueError("clarification_requested is not allowed after a produced or denied step")
    if kind == "clarification_requested" and _waiting_since(steps):
        raise ValueError(f"a clarification request is already open since {_waiting_since(steps)}")
    if kind == "clarified" and not _waiting_since(steps):
        raise ValueError("no open clarification request to answer")
    if kind == "fee_estimate" and amount is None:
        raise ValueError("fee_estimate needs --amount USD")
    if kind != "fee_estimate" and amount is not None:
        raise ValueError("--amount is only for fee_estimate steps")
    if expected_on is not None and (kind in NOT_STATED or (kind != "determination" and "determination" not in kinds)):
        raise ValueError("--expected-on is for the determination or a later step that restates the date")
    if expected_on is not None and _day(expected_on) is None:
        raise ValueError("--expected-on must be a real YYYY-MM-DD date")
    if expected_on is not None and _day(expected_on) < day:
        raise ValueError(f"stated date {expected_on} is before the {kind} on {on}")
    cents = None if amount is None else _amount_cents(amount)  # every refusal above and here writes nothing
    hashes = _resolve_records(root, records, steps) if records else []
    _set_reference(entry, reference, replace_reference)  # last refusal: nothing written on a conflict
    step = {"on": on, "kind": kind, **({"expected_on": expected_on} if expected_on is not None else {}),
            **({"amount_cents": cents} if cents is not None else {}),
            **({"note": note} if note else {}), **({"records": hashes} if hashes else {}),
            "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    entry["steps"] = [*steps, step]
    kit._write_json(root / "kit" / "sent.json",
                    {"schema_version": 1, "requests": dict(sorted(requests.items()))})
    return {"agency_id": agency_id, "request": request, **entry, "steps": [
        {**s, "records": [h[:12] for h in s["records"]]} if "records" in s else s for s in entry["steps"]]}


def status(root, today=None, law_base=None, *, request=None):
    """Every recorded request (or only those named ``request``) with provisional determination dates (read-only)."""
    from .cli import read_config  # local import: cli imports this module lazily
    root = Path(root)
    request = None if request is None else _request(request)
    requests = read_sent(root)
    path = root / "kit" / "agencies.json"
    names = {entry["agency_id"]: entry["name"] for entry in kit._load_existing(path)} \
        if requests and path.is_file() and not path.is_symlink() else {}
    try:
        package = law.load_package(kit._jurisdiction(read_config(root)), law_base)
        package_status = package["status"]
    except ValueError:
        package, package_status = None, "missing"
    rows, cap = [], ()  # cap: () until a request with an estimate needs kit/request-settings.json
    for (agency_id, name), entry in sorted(((_split(key), entry) for key, entry in requests.items()),
                                           key=lambda item: (item[1]["sent_on"], *item[0])):
        if request is not None and name != request:
            continue
        steps = [s for s in entry["steps"] if isinstance(s, dict)]
        kinds = [s.get("kind") for s in steps]
        fees = [(s.get("on"), i, s) for i, s in enumerate(steps) if "amount_cents" in s or kinds[i] == "fee_estimate"]
        if not all(_amount_ok(s) and _day(on) for on, _, s in fees) or not _expected_ok(steps):
            raise ValueError(f"kit/sent.json unreadable: malformed steps for {_label(agency_id, name)!r}")
        estimate = max(fees)[2]["amount_cents"] if fees else None  # latest date, then the later recorded step
        stated = next((s["expected_on"] for s in reversed(steps) if "expected_on" in s), None)  # latest one wins
        if estimate is not None and cap == ():
            cap = kit.fee_cap_cents(root / "kit", today)
        linked = sorted({digest for s in steps for digest in _step_records(s)})
        extended = law.deadline(package, entry["sent_on"], extension=True).isoformat() if package else None
        due = (extended if "extension" in kinds else law.deadline(package, entry["sent_on"]).isoformat()) \
            if package else None
        rows.append({"agency_id": agency_id, "request": name, "name": names.get(agency_id),
                     "sent_on": entry["sent_on"], "channel": entry["channel"],
                     "draft_changed_since_sent": _draft(root, agency_id, name) != entry["draft_sha256"],
                     "determination_due": due, "with_extension": extended, "steps": len(entry["steps"]),
                     "extended": "extension" in kinds, "closed": "closed" in kinds,
                     "last_step": {"on": steps[-1].get("on"), "kind": kinds[-1]} if steps else None,
                     "records": len(linked), "record_prefixes": [h[:12] for h in linked[:PREFIXES_MAX]],
                     **({"more": len(linked) - PREFIXES_MAX} if len(linked) > PREFIXES_MAX else {}),
                     "fee_estimate": None if estimate is None else _dollars(estimate),
                     "over_fee_cap": None if estimate is None or cap is None else estimate > cap,
                     "stated_production_date": stated, "waiting_on_you_since": _waiting_since(steps),
                     "reference": entry.get("reference")})
    waiting = any(row["waiting_on_you_since"] for row in rows)
    return {"requests": rows, "law_package_status": package_status, "note": NOTE + (WAIT_NOTE if waiting else "")}


ANSWERED = {"determination", "produced", "denied"}
CSV_HEADER = ("agency_id", "name", "drafted", "sent_on", "channel", "last_step_on", "last_step_kind", "records",
              "sources", "findings")


def coverage(root):
    """One row per selected kit agency from draft to published finding; read-only, unsent requests first."""
    from .content import load_content
    root = Path(root)
    path = root / "kit" / "agencies.json"
    if not path.is_file() or path.is_symlink():
        raise ValueError("run kit first: kit/agencies.json is missing")
    selected = {}
    for entry in kit._load_existing(path):
        if entry.get("selected") is True:
            selected.setdefault(entry["agency_id"], entry["name"])
    requests = read_sent(root)
    model = load_content(root)
    sources = [s["agency_id"] for s in model["sources"]] if model else []
    findings = [f["agency_id"] for f in model["findings"]] if model else []
    rows = []
    for agency_id, name in selected.items():
        entry = requests.get(agency_id) or {}
        steps = [s for s in entry.get("steps", []) if isinstance(s, dict)]
        draft = root / "kit" / "requests" / (agency_id + ".md")
        rows.append({"agency_id": agency_id, "name": name, "drafted": draft.is_file() and not draft.is_symlink(),
                     "sent_on": entry.get("sent_on"), "channel": entry.get("channel"),
                     "last_step": {"on": steps[-1].get("on"), "kind": steps[-1].get("kind")} if steps else None,
                     "records": len({h for s in steps if s.get("kind") == "produced" for h in _step_records(s)}),
                     "sources": sources.count(agency_id), "findings": findings.count(agency_id),
                     "answered": bool(ANSWERED & {s.get("kind") for s in steps})})
    rows.sort(key=lambda row: (row["sent_on"] is not None, row["agency_id"]))
    totals = {"selected": len(rows), "drafted": sum(r["drafted"] for r in rows),
              "sent": sum(r["sent_on"] is not None for r in rows), "answered": sum(r.pop("answered") for r in rows),
              "with_records": sum(r["records"] > 0 for r in rows), "with_findings": sum(r["findings"] > 0 for r in rows),
              "unselected_with_requests": len({parts[0] for parts in map(_split, requests)
                                               if parts and parts[0] not in selected})}
    note = ("Read-only counts from kit/ and content/." if model else
            "No content/ folder: sources and findings are 0.") + " Agencies with no sent request come first."
    return {"rows": rows, "totals": totals, "note": note}


def _cells(row):
    step = row["last_step"] or {}
    return [row["agency_id"], row["name"], row["drafted"], row["sent_on"], row["channel"], step.get("on"),
            step.get("kind"), row["records"], row["sources"], row["findings"]]


def _csv_cell(value):
    text = "" if value is None else ("true" if value else "false") if isinstance(value, bool) else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@") else text  # spreadsheet formula guard


def format_coverage(report, as_csv=False):
    """The coverage report as a fixed-column table, or RFC 4180 CSV with a spreadsheet formula guard."""
    import csv
    import io
    if as_csv:
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(CSV_HEADER)
        for row in report["rows"]:
            writer.writerow([_csv_cell(c) for c in _cells(row)])
        return out.getvalue()
    line = "{:<36} {:<40} {:<7} {:<10} {:<7} {:<10} {:<13} {:>7} {:>7} {:>8}"
    lines = [line.format("agency_id", "name", "drafted", "sent_on", "channel", "last_on", "last_kind", "records",
                         "sources", "findings")]
    for row in report["rows"]:
        cells = _cells(row)
        cells[1] = "".join(c if c.isprintable() else " " for c in cells[1])[:40]
        cells[2] = "yes" if cells[2] else "no"
        lines.append(line.format(*("-" if c is None else c for c in cells)))
    lines.append("totals: " + ", ".join(f"{k} {v}" for k, v in report["totals"].items()))
    return "\n".join(lines + [report["note"]]) + "\n"


def due(root, as_of=None, *, today=None, law_base=None, request=None):
    """Open (unanswered, unclosed) requests with days to or past the provisional determination date (read-only)."""
    day = _day(as_of) if as_of is not None else (today or date.today())
    if day is None:
        raise ValueError("--as-of must be a real YYYY-MM-DD date")
    report = status(root, law_base=law_base, request=request)
    if request is not None and not report["requests"]:
        raise ValueError(f"no sent request */{request}: no agency has a request recorded under that name")
    entries = {_split(key): [s for s in entry["steps"] if isinstance(s, dict)] for key, entry in read_sent(root).items()}
    open_rows, answered, stated_passed = [], [], []
    for row in report["requests"]:
        if row["closed"]:
            continue
        base = {"agency_id": row["agency_id"], "request": row["request"], "name": row["name"],
                "sent_on": row["sent_on"], "reference": row["reference"]}
        steps = entries.get((row["agency_id"], row["request"]), [])
        if row["waiting_on_you_since"]:  # no claim that this pauses any legal clock; the provisional date stays shown
            since, due_on = row["waiting_on_you_since"], row["determination_due"]
            open_rows.append({**base, "due": due_on, "days": due_on and (date.fromisoformat(due_on) - day).days,
                              "state": "waiting_on_you", "since": since, "note": WAITING.format(since)})
        elif {s.get("kind") for s in steps} & ANSWERED:
            answered.append({**base, "last_step": row["last_step"]})
            setter = max((i for i, s in enumerate(steps) if "expected_on" in s), default=None)
            stated = row["stated_production_date"]  # a produced step after the step that set it clears it
            if setter is not None and not any(s.get("kind") == "produced" for s in steps[setter + 1:]) \
                    and day > date.fromisoformat(stated):
                stated_passed.append({**base, "state": "stated_date_passed", "stated": stated,
                                      "days": (day - date.fromisoformat(stated)).days})
        elif row["determination_due"] is None:
            open_rows.append({**base, "due": None, "days": None, "state": "no_law_package"})
        else:
            days = (date.fromisoformat(row["determination_due"]) - day).days
            open_rows.append({**base, "due": row["determination_due"], "days": days,
                              "state": "past_due" if days < 0 else "due_today" if days == 0 else "upcoming"})
    open_rows.sort(key=lambda row: (row["due"] or "", row["agency_id"], row["request"]))
    waiting = sum(row["state"] == "waiting_on_you" for row in open_rows)  # never sets exit 3 by itself
    stated_passed.sort(key=lambda row: (row["stated"], row["agency_id"], row["request"]))
    missing = report["law_package_status"] == "missing"
    return {"as_of": day.isoformat(), "law_package_status": report["law_package_status"], "open": open_rows,
            "answered": answered, "past_due": sum(row["state"] == "past_due" for row in open_rows),
            "stated_passed": stated_passed, "stated_date_passed": len(stated_passed),
            "waiting_on_you": waiting,
            "note": ("No law package: determination dates cannot be computed. " if missing else "") + NOTE
            + (f" {STATED[0].upper()}{STATED[1:]}." if stated_passed else "") + (WAIT_NOTE if waiting else "")}


def _followup_target(root, name, replace):  # private kit/followups/ (0700); symlinks and silent overwrites refused
    folder = Path(root) / "kit" / "followups"
    kit._kit_folder(folder, 0o700)
    target = folder / name
    if os.path.islink(target):
        raise ValueError(f"kit/followups/{target.name} must not be a symlink")
    if os.path.lexists(target) and not replace:
        raise ValueError(f"kit/followups/{target.name} already exists; use --replace to rewrite it")
    return target


def followup(root, agency_id, as_of=None, *, replace=False, law_base=None, today=None):
    """Write a private, never-sent follow-up draft to kit/followups/ for a request past its provisional date."""
    from .cli import read_config  # local import: cli imports this module lazily
    root, today = Path(root), today or date.today()
    day = _day(as_of) if as_of is not None else today
    if day is None:
        raise ValueError("--as-of must be a real YYYY-MM-DD date")
    if day > today:
        raise ValueError(f"--as-of {day.isoformat()} is in the future")
    entry = read_sent(root).get(agency_id)
    if entry is None or not kit.AGENCY_ID.fullmatch(agency_id):
        raise ValueError(f"no hand-sent request recorded for {agency_id}")
    steps = [s for s in entry["steps"] if isinstance(s, dict)]
    if any(s.get("kind") == "closed" for s in steps):
        raise ValueError(f"request for {agency_id} is closed")
    if _waiting_since(steps):
        raise WaitingOnYou(f"waiting on you since {_waiting_since(steps)} — reply to the agency's clarification first")
    answer = next((s for s in steps if s.get("kind") in ANSWERED), None)
    if answer:
        raise ValueError(f"request for {agency_id} was answered ({answer['kind']} on {answer.get('on')}); "
                         "no follow-up drafted")
    row = next((r for r in due(root, day.isoformat(), law_base=law_base)["open"]
                if (r["agency_id"], r["request"]) == (agency_id, "main")), None)
    if row is None or row["state"] == "no_law_package":
        raise ValueError("no law package: dates cannot be computed")
    if row["state"] != "past_due":
        raise ValueError(f"not past due: provisional determination date {row['due']}")
    try:
        package = law.load_package(kit._jurisdiction(read_config(root)), law_base)
    except ValueError:
        raise ValueError("no law package: dates cannot be computed") from None
    target = _followup_target(root, f"{agency_id}-{day.isoformat()}.md", replace)
    statute, name = package["records_law"], row["name"] or agency_id
    extended = " (extended, as the agency noted)" if any(s.get("kind") == "extension" for s in steps) else ""
    subject, first = reference_lines(entry)
    lines = [f"# Follow-up draft: {name}{subject}", "", "DRAFT — NOT SENT. Review before sending it yourself.", "",
             f"To: {name}, records custodian", "", *([first, ""] if first else []),
             f"On {entry['sent_on']} we sent a records request by {entry['channel']} to {name}. Its provisional "
             f"determination date{extended} was {row['due']}; the period runs from the agency's receipt of the "
             f"request. As of {day.isoformat()} we have no record of a determination, records produced or a denial.",
             "", f"Please tell us the status of the request under the {statute['name']}, {statute['citation']}, "
             "and when you expect to respond.", "", kit.DEFAULT_REQUESTER, "", "---", ""]
    if package["status"] != "reviewed":
        lines.append(f"Law package status: {package['status']} (not independently reviewed)")
    kit._write_text(target, "\n".join([*lines, law.HOLIDAY_NOTE]) + "\n")
    return {"written": f"kit/followups/{target.name}", "due": row["due"], "days_past": -row["days"], "note": NOTE}


DENIAL_TEMPLATE = """# Denial reply draft: {name}{subject}

DRAFT — NOT SENT. Review before sending it yourself.

To: {name}, records custodian

{first}On {sent_on} we sent a records request by {channel} to {name}. On {denied_on} we recorded the agency's response
as a denial. We asked for: [EDIT ME: what you asked for]

Please:
1. Tell us which exemption or other legal basis the agency relied on for each record or category withheld.
2. Send the parts of the requested records that are not exempt, with only the exempt portions redacted.
3. Explain the denial in writing.
{citations}
Thank you,

[name]

---
Draft only. Not legal advice.
law package: {status}
"""


def denial_reply(root, agency_id, rules=(), *, replace=False, law_base=None):
    """Write a private, never-sent reply draft to kit/followups/ for a request with a recorded denied step."""
    from .cli import read_config  # local import: cli imports this module lazily
    root, rules = Path(root), list(rules or ())
    entry = read_sent(root).get(agency_id) if kit.AGENCY_ID.fullmatch(agency_id) else None
    denied = [s for s in (entry or {}).get("steps", []) if isinstance(s, dict) and s.get("kind") == "denied"
              and _day(s.get("on"))]
    if not denied:
        raise ValueError(f"no recorded denial for {agency_id}")
    denied_on = max(s["on"] for s in denied)  # ISO dates: the latest recorded denial
    try:
        package = law.load_package(kit._jurisdiction(read_config(root)), law_base)
    except ValueError:
        package = None
    if rules and package is None:
        raise ValueError("no law package: --rule cannot be checked")
    known = {r["rule_id"] for r in package["rules"]} if package else set()
    in_force = {r["rule_id"]: r for r in law.rules_in_force(package, denied_on)} if package else {}
    for number, rule_id in enumerate(rules, 1):
        if rule_id in rules[:number - 1]:
            raise ValueError(f"--rule {rule_id} is given twice")
        if rule_id not in known:
            raise ValueError(f"--rule {rule_id} is not in the law package")
        if rule_id not in in_force:
            raise ValueError(f"--rule {rule_id} is not in force on the denial date {denied_on}")
    status = package["status"] if package else "missing"
    citations = "".join(f"- {r} (law package status: {status}): {in_force[r]['citation']}\n" for r in rules)
    path = root / "kit" / "agencies.json"
    names = {e["agency_id"]: e["name"] for e in kit._load_existing(path)} \
        if path.is_file() and not path.is_symlink() else {}
    name = names.get(agency_id) or agency_id
    target = _followup_target(root, f"{agency_id}-denial-{denied_on}.md", replace)
    subject, first = reference_lines(entry)
    kit._write_text(target, DENIAL_TEMPLATE.format(
        name=name, subject=subject, first=first and first + "\n\n", sent_on=entry["sent_on"],
        channel=entry["channel"], denied_on=denied_on, status=status,
        citations=f"\nRules we noted for this reply:\n{citations}" if rules else ""))
    return {"written": f"kit/followups/{target.name}", "rules": rules, "law_package_status": status}
