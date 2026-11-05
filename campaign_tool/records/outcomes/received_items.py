"""Private received-item inventory: classification, stated deadlines and a cited
projection of the SAME run's existing comparison ratings.

Deterministic rules only: no model, network, original-document parsing, OCR,
ledger write, kit write or send. Everything is documentary screening that needs
human and attorney review (the us-ca law package is draft); nothing here is a
legal conclusion and nothing is published. Text is read only from the current
extraction membership with the same hash bindings as factsheets.collect.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
from datetime import date, datetime, timezone

from ..ledger import store
from ..ledger_catalog import scope_excluded
from ..understanding.evidence import decode, is_hash, read_pinned, sha

SCHEMA = "records-received-item-v1"
COMPARISON_SCHEMA = "records-agency-comparison-v1"
MAX_ROWS = 100000
MAX_UNIT_BYTES = 1024 * 1024
MAX_TEXT_BYTES = 128 * 1024 * 1024
MAX_OUTPUT_BYTES = 64 * 1024 * 1024
MAX_LINE = 2000
KINDS = ("response", "extension", "denial", "fee", "records", "other")
SPECIFIC = ("extension", "denial", "fee", "records")
# Read-only meaning map onto kit_track.KINDS; kit tracking is never written here.
KIT_CROSSWALK = {"extension": "extension", "denial": "denied", "fee": "fee_estimate",
                 "records": "produced", "response": "acknowledged_or_determination", "other": None}
DISCLAIMER = ("Private documentary screening; REVIEW REQUIRED, not a legal conclusion. The us-ca law "
              "package is draft and unreviewed. A rating is the existing comparison row status and "
              "never a finding of compliance or violation. Not for publication.")
# Fields whose supplied citation can be the agency's own written policy. access_logs and
# public_comment cite logs/minutes, which are evidence, never the policy itself.
POLICY_FIELDS = frozenset({"usage_policy_published", "authorized_purposes", "authorized_users_training",
                           "retention", "sharing_limits", "audit_process", "custodian",
                           "accuracy_correction", "safeguards"})
ADMITTED_ROLES = frozenset({"agency_original", "container_child", "original"})

_I = re.I
PHRASES = {
    "response": [re.compile(p, _I) for p in (
        r"\bwe acknowledge receipt of your (?:(?:public )?records |cpra )?request\b",
        r"\bwe have received your (?:(?:public )?records |cpra )?request\b")],
    "extension": [re.compile(p, _I) for p in (
        r"\bwe (?:are|hereby) extending the (?:determination|response) period\b",
        r"\bwe (?:invoke|are invoking|hereby invoke) an extension\b",
        r"\bthe (?:time|period) (?:to|for) (?:respond|response|determination) (?:is|has been) extended\b",
        r"\bis exercising its authority under government code section 7922\.535\b[^.]{0,40}\bextend the time\b")],
    "denial": [re.compile(p, _I) for p in (
        r"\byour (?:(?:public )?records |cpra )?request (?:is|has been) denied\b",
        r"\bwe (?:deny|are denying) your (?:(?:public )?records |cpra )?request\b")],
    "fee": [re.compile(p, _I) for p in (
        r"\bthe (?:duplication |copying )?fee is\b",
        r"\bestimated duplication (?:cost|fee)s?\b")],
    "records": [re.compile(p, _I) for p in (
        r"\benclosed are (?:the )?responsive records\b",
        r"\battached are (?:the )?responsive records\b",
        r"\bthe responsive records are (?:enclosed|attached)\b")],
}
TITLE = re.compile(r"^(?:[A-Za-z0-9&.,'()/-]+ ){0,8}(?:Policy|Contract|Agreement|Audit Log|Search Log)"
                   r"(?: [A-Za-z0-9&.,'()/-]+){0,4}$")
NEGATION = re.compile(r"(?:\bnot\b|n't\b|\bnever\b|\bno\b|\bif\b|\bunless\b|\bwhether\b)[^.]{0,40}$", _I)
HISTORY = re.compile(r"^\s*(?:-{2,}\s*(?:original message|forwarded message)|begin forwarded message|"
                     r"on .{5,120} wrote:\s*$|from:\s.+\bsent:)", _I)
MONTHS = {m: i for i, m in enumerate(("january", "february", "march", "april", "may", "june", "july",
                                       "august", "september", "october", "november", "december"), 1)}
MONTH_RE = "|".join(MONTHS) + "|" + "|".join(m[:3] + r"\.?" for m in MONTHS)
DEADLINE = re.compile(r"\b(?:by|no later than|on or before|until|not later than)\s+((?:%s)\s+\d{1,2}(?:st|nd|rd|th)?"
                      r"(?:,?\s+\d{4})?)" % MONTH_RE, _I)
DEADLINE_CONTEXT = re.compile(r"\b(?:respond|response|reply|determination|records|extend|extension|provide|"
                              r"available|complete|produce|release)\w*", _I)


def _need(condition, code):
    if not condition:
        raise ValueError(code)


def _rows(db, query, args=()):
    rows = db.execute(query, args).fetchmany(MAX_ROWS + 1)
    _need(len(rows) <= MAX_ROWS, "received_items_metadata_bound")
    return rows


def _canon(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _digest(value):
    return hashlib.sha256(_canon(value).encode()).hexdigest()


def item_id(original_sha256):
    return _digest(["records-received-item-v1", original_sha256])


def _utc(value):
    """Timezone-aware ISO instant or None; a naive/ambiguous value is never guessed."""
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _date_from_text(text, anchor):
    match = re.fullmatch(r"([A-Za-z]+)\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?", text.strip())
    if match is None:
        return None
    month = MONTHS.get(match.group(1).lower())
    if month is None:
        month = next((n for name, n in MONTHS.items() if name.startswith(match.group(1).lower()[:3])), None)
    year = int(match.group(3)) if match.group(3) else (anchor.year if anchor else None)
    if month is None or year is None:
        return None
    try:
        return date(year, month, int(match.group(2))).isoformat()
    except ValueError:
        return None


def _quoted(line, start):
    return NEGATION.search(line[:start]) is not None


def _candidates(units):
    """units: [(page, ordinal, unit_id, text)] -> [{kind, page, quote, ...}] in source order."""
    found = []
    skip_rest = False  # a reply/forward block runs to the end of the message, across units
    for page, ordinal, unit_id, text in units:
        for number, raw in enumerate(text.splitlines()):
            line = raw.strip()
            if not line or len(line) > MAX_LINE:
                continue
            if HISTORY.match(line):
                skip_rest = True
            if skip_rest or line.startswith(">") or "?" in line:
                continue
            for kind, patterns in PHRASES.items():
                for pattern in patterns:
                    hit = pattern.search(line)
                    if hit and not _quoted(line, hit.start()):
                        found.append({"kind": kind, "page": page, "quote": line, "unit_id": unit_id,
                                      "_order": (page, ordinal, number, hit.start())})
                        break
            if page == 1 and number == 0 and len(line) <= 80 and not line.endswith(".") and TITLE.match(line):
                found.append({"kind": "records", "page": page, "quote": line, "unit_id": unit_id,
                              "basis": "opening_title", "_order": (page, ordinal, number, 0)})
    found.sort(key=lambda c: (c["_order"], c["kind"]))
    return found


def classify(units):
    """-> (kind, basis, candidates, diagnostics). Uncertain stays `other`, never guessed."""
    found = _candidates(units)
    clean = [{k: v for k, v in c.items() if k != "_order"} for c in found]
    specific = sorted({c["kind"] for c in found if c["kind"] in SPECIFIC})
    if len(specific) > 1:
        return "other", {"page": None, "quote": None}, clean, ["mixed_kind_candidates"]
    chosen = specific[0] if specific else ("response" if any(c["kind"] == "response" for c in found) else None)
    if chosen is None:
        return "other", {"page": None, "quote": None}, clean, []
    first = next(c for c in found if c["kind"] == chosen)
    _need(type(first["page"]) is int and first["page"] > 0 and first["quote"], "received_items_basis")
    return chosen, {"page": first["page"], "quote": first["quote"]}, clean, []


def deadlines(units, received_at):
    """Dates an item states for a response/extension/production. Quoted, not computed."""
    anchor = received_at
    out = []
    skip_rest = False
    for page, ordinal, unit_id, text in units:
        for number, raw in enumerate(text.splitlines()):
            line = raw.strip()
            if not line or len(line) > MAX_LINE:
                continue
            if HISTORY.match(line):
                skip_rest = True
            if skip_rest or line.startswith(">") or "?" in line:
                continue
            for hit in DEADLINE.finditer(line):
                if not DEADLINE_CONTEXT.search(line) or _quoted(line, hit.start()):
                    continue
                iso = _date_from_text(hit.group(1), anchor)
                out.append({"date_text": hit.group(1), "date": iso, "page": page, "quote": line,
                            "unit_id": unit_id, "confidence": "rule_match" if iso else "date_unresolved",
                            "year_inferred": iso is not None and not re.search(r"\d{4}", hit.group(1)),
                            "legal_deadline_calculation": False})
    seen, unique = set(), []
    for item in out:
        key = (item["date_text"], item["page"], item["quote"])
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def _current_units(db, processing, subject):
    """Current extraction units, hash-bound like factsheets.collect.

    -> (units, diagnostics, review_status). A missing extraction is a diagnostic;
    a corrupt binding raises ValueError (stable refusal).
    """
    current = db.execute("SELECT i.id,i.manifest_json,i.manifest_sha256 FROM extraction_adapter_current c "
                         "JOIN extraction_adapter_imports i ON i.id=c.import_id AND i.original_sha256=c.original_sha256 "
                         "WHERE c.original_sha256=?", (subject,)).fetchone()
    if current is None:
        return [], ["current_extraction_unavailable"], None
    _need(sha(current["manifest_json"].encode()) == current["manifest_sha256"], "received_items_manifest_hash")
    manifest = decode(current["manifest_json"])
    members = manifest.get("unit_ids")
    _need(manifest.get("original_sha256") == subject and type(members) is list and len(members) <= 10000
          and all(type(x) is str for x in members) and len(set(members)) == len(members),
          "received_items_manifest_members")
    rows = _rows(db, "SELECT * FROM units WHERE original_sha256=? AND json_valid(provenance_json) "
                     "AND json_extract(provenance_json,'$.extraction_import_id')=? ORDER BY legacy_ordinal",
                 (subject, current["id"]))
    _need([u["id"] for u in rows] == members, "received_items_current_unit_set")
    units, diagnostics, consumed = [], [], 0
    for ordinal, unit in enumerate(rows, 1):
        provenance = decode(unit["provenance_json"])
        digest = provenance.get("payload_sha256")
        _need(is_hash(digest) and provenance.get("ordinal") == ordinal, "received_items_payload_binding")
        path = Path(processing) / "artifacts" / "evidence" / digest
        _need(unit["derived_path"] == str(path), "received_items_payload_path")
        raw = read_pinned(path, digest, MAX_UNIT_BYTES)
        consumed += len(raw)
        _need(consumed <= MAX_TEXT_BYTES, "received_items_text_bound")
        payload = decode(raw)
        _need(type(payload) is dict and set(payload) == {"kind", "locator", "text", "data"}
              and type(payload["text"]) is str and type(payload["locator"]) is dict
              and payload["kind"] == unit["unit_type"] and payload["locator"] == decode(unit["locator"])
              and sha(payload["text"].encode()) == unit["text_sha256"]
              and unit["id"] == sha(json.dumps([current["id"], ordinal, digest], sort_keys=True,
                                               separators=(",", ":"), ensure_ascii=False).encode()),
              "received_items_unit_binding")
        locator = payload["locator"]
        text = payload["text"]
        pages = [locator[k] for k in ("page", "page_no") if k in locator]
        if not pages or any(type(p) is not int or p < 1 for p in pages) or len(set(pages)) != 1:
            if "page_locator_unavailable" not in diagnostics and payload["kind"] != "email_headers":
                diagnostics.append("page_locator_unavailable")
            continue
        _need(any(p.get("page_no") == pages[0] for p in manifest.get("pages", []) if isinstance(p, dict)),
              "received_items_page_not_in_manifest")
        units.append({"page": pages[0], "ordinal": ordinal, "unit_id": unit["id"], "text": payload["text"],
                      "text_sha256": unit["text_sha256"], "payload_sha256": digest})
    review = manifest.get("review_status") if isinstance(manifest.get("review_status"), str) else "not_reviewed"
    return units, diagnostics, review


def _resolve_evidence(entry, by_original):
    """Exact `sha256`/`sha256:<digest>` + `page N` only. -> (original, unit, line) or None."""
    source, locator = entry.get("source_id"), entry.get("locator")
    if not isinstance(source, str) or not isinstance(locator, str):
        return None
    digest = source[7:] if source.startswith("sha256:") else source
    page = re.fullmatch(r"page ([1-9][0-9]*)", locator)
    if page is None or re.fullmatch(r"[a-f0-9]{64}", digest) is None or digest not in by_original:
        return None
    matches = [u for u in by_original[digest]["units"] if u["page"] == int(page.group(1))]
    if len(matches) != 1:  # none, or several current units on one page: never pick one
        return None
    for line in matches[0]["text"].splitlines():
        if line.strip() and len(line.strip()) <= MAX_LINE:
            return digest, matches[0], line.strip()
    return None


def _policy_established(evidence, by_original, item):
    """True only when every supplied citation resolves to ONE source whose opening
    title names a policy (the classifier's own opening_title evidence)."""
    digests = set()
    for entry in evidence:
        resolved = _resolve_evidence(entry, by_original) if isinstance(entry, dict) else None
        if resolved is None:
            return False
        digests.add(resolved[0])
    return len(digests) == 1 and any(
        c.get("basis") == "opening_title" and re.search(r"\bpolicy\b", c["quote"], _I)
        for c in item["candidates"])


def _load_comparison(path, run_id):
    path = Path(os.path.abspath(path))
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None, None
    _need(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size <= 8 * 1024 * 1024,
          "received_items_comparison_file")
    raw = path.read_bytes()

    def unique(pairs):
        result = {}
        for key, item in pairs:
            _need(key not in result, "received_items_duplicate_key")
            result[key] = item
        return result
    try:
        report = json.loads(raw, object_pairs_hook=unique,
                            parse_constant=lambda _: (_ for _ in ()).throw(ValueError("received_items_nonfinite")))
    except (json.JSONDecodeError, UnicodeError, RecursionError):
        raise ValueError("received_items_comparison_json") from None
    _need(type(report) is dict and report.get("schema") == COMPARISON_SCHEMA
          and report.get("run_id") == run_id and type(report.get("rows")) is list, "received_items_comparison_stale")
    return report, hashlib.sha256(raw).hexdigest()


def collect(db, processing, comparison_report, *, run_id, comparison_sha256=None):
    """Pure read-only inventory + projection. `comparison_report` is the parsed
    same-run comparison.json (or None when it is unavailable)."""
    db.row_factory = sqlite3.Row
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    report = {"schema": SCHEMA, "run_id": run_id, "review_required": True, "legal_conclusion": False,
              "law_package_status": "draft", "disclaimer": DISCLAIMER, "comparison_sha256": comparison_sha256,
              "items": [], "diagnostics": []}
    needed = {"originals", "occurrences", "mail_messages", "joins", "requests"}
    if not needed <= tables:
        report["diagnostics"].append("inventory_tables_unavailable")
        return report
    mail_times = {}
    for row in _rows(db, "SELECT eml_sha256,received_at FROM mail_messages ORDER BY id"):
        when = _utc(row["received_at"])
        if when is not None and row["eml_sha256"]:
            mail_times.setdefault(row["eml_sha256"], []).append(when)
    occurrences = {}
    for row in _rows(db, "SELECT id,original_sha256,parent_occurrence_id,acquired_at FROM occurrences ORDER BY id"):
        occurrences.setdefault(row["original_sha256"], []).append(row)
    owner = {r["id"]: r["original_sha256"] for rows in occurrences.values() for r in rows}
    joins = {}
    for row in _rows(db, "SELECT j.original_sha256,j.agency_id,j.request_id,r.agency_id AS request_agency "
                         "FROM joins j LEFT JOIN requests r ON r.id=j.request_id WHERE j.status='typed' ORDER BY j.id"):
        joins.setdefault(row["original_sha256"], []).append(row)
    has_extraction = "extraction_adapter_current" in tables and "extraction_adapter_imports" in tables and "units" in tables
    by_original = {}
    for subject in [r["sha256"] for r in _rows(db, "SELECT sha256 FROM originals ORDER BY sha256")]:
        _need(is_hash(subject), "received_items_original_hash")
        diagnostics = []
        try:
            original = store.effective_original(db, subject)
        except ValueError:
            original = None
        # Fail closed: an original whose admission cannot be demonstrated is omitted
        # without reading or projecting any of its text.
        if original is None or scope_excluded(original) or original["role"] not in ADMITTED_ROLES:
            if original is None:
                report["diagnostics"].append("effective_original_unavailable")
            continue
        # Identity: only unambiguous typed joins consistent with request ownership.
        agencies = {j["agency_id"] for j in joins.get(subject, [])}
        requests = {j["request_id"] for j in joins.get(subject, []) if j["request_id"] is not None}
        agency_id = next(iter(agencies)) if len(agencies) == 1 else None
        request_id = next(iter(requests)) if len(requests) == 1 else None
        if len(agencies) > 1:
            diagnostics.append("ambiguous_agency_attribution")
        if len(requests) > 1:
            diagnostics.append("ambiguous_request_attribution")
        if any(j["request_id"] is not None and j["request_agency"] != j["agency_id"] for j in joins.get(subject, [])):
            agency_id = request_id = None
            diagnostics.append("typed_join_request_mismatch")
        if request_id is not None and agency_id is None:
            request_id = None
        # Time: earliest valid incoming mail time, else occurrence time, else a unique parent's mail time.
        received_at, source = None, None
        times = sorted(mail_times.get(subject, []))
        if times:
            received_at, source = times[0], "mail_received_at"
        else:
            acquired = [t for t in (_utc(o["acquired_at"]) for o in occurrences.get(subject, [])) if t]
            parents = {owner.get(o["parent_occurrence_id"]) for o in occurrences.get(subject, [])
                       if o["parent_occurrence_id"]} - {None}
            parent_times = [t for p in parents for t in mail_times.get(p, [])]
            if len(parents) == 1 and parent_times:
                received_at, source = sorted(parent_times)[0], "parent_mail_received_at"
            elif acquired:
                received_at, source = sorted(acquired)[0], "occurrence_acquired_at"
        if received_at is None:
            diagnostics.append("received_at_unavailable")
        units, extraction_status, review = [], "unavailable", None
        if has_extraction:
            units, found, review = _current_units(db, processing, subject)
            diagnostics += found
            extraction_status = "current" if "current_extraction_unavailable" not in found else "unavailable"
        else:
            diagnostics.append("current_extraction_unavailable")
        triples = [(u["page"], u["ordinal"], u["unit_id"], u["text"]) for u in units]
        kind, basis, candidates, extra = classify(triples)
        diagnostics += extra
        if not units and "page_locator_unavailable" not in diagnostics and extraction_status == "current":
            diagnostics.append("no_usable_text")
        item = {"item_id": item_id(subject), "original_sha256": subject,
                "occurrence_ids": sorted(o["id"] for o in occurrences.get(subject, [])),
                "agency_id": agency_id, "request_id": request_id,
                "received_at": received_at.isoformat().replace("+00:00", "Z") if received_at else None,
                "received_at_source": source, "kind": kind, "kind_basis": basis,
                "kit_equivalent": KIT_CROSSWALK[kind], "candidates": candidates,
                "deadlines": deadlines(triples, received_at), "decision_source": "rules",
                "model_decisions": [], "extraction_status": extraction_status,
                "ocr_review_status": review, "needs_visual_review_unresolved": review not in (None, "reviewed"),
                "comparison_status": "unresolved", "findings": [],
                "review_required": True, "legal_conclusion": False, "diagnostics": sorted(set(diagnostics))}
        report["items"].append(item)
        by_original[subject] = {"item": item, "units": units}
    report["items"].sort(key=lambda i: i["item_id"])
    _project(report, comparison_report, by_original)
    return report


def _project(report, comparison, by_original):
    """Attach existing comparison rows to items whose cited page they name. Never re-rates."""
    if comparison is None:
        report["diagnostics"].append("comparison_unavailable")
        return
    unresolved_agencies = set()
    for index, row in enumerate(comparison["rows"]):
        agency, field = row.get("agency_id"), row.get("field")
        evidence = row.get("evidence") if isinstance(row.get("evidence"), list) else []
        matched = False
        for entry in evidence:
            resolved = _resolve_evidence(entry, by_original) if isinstance(entry, dict) else None
            if resolved is None:
                continue
            digest, unit, line = resolved
            item = by_original[digest]["item"]
            if item["agency_id"] is None or item["agency_id"] != agency:
                continue
            if row.get("request_id") is not None and row.get("request_id") != item["request_id"]:
                continue
            matched = True
            if row.get("status") not in ("meets", "gap", "unknown") or row.get("citation") is None:
                report["diagnostics"].append("comparison_row_invalid")
                continue
            if field.endswith("_practice"):
                report["diagnostics"].append("practice_policy_citation_missing")
                continue
            cite = {"document_sha256": digest, "page": unit["page"], "quote": line, "unit_id": unit["unit_id"],
                    "text_sha256": unit["text_sha256"], "payload_sha256": unit["payload_sha256"]}
            observed = row.get("observed") if isinstance(row.get("observed"), dict) else {}
            policy = None
            if field in POLICY_FIELDS or field in ("access_logs", "public_comment"):
                if field in POLICY_FIELDS and _policy_established(evidence, by_original, item):
                    policy = dict(cite)
                else:
                    report["diagnostics"].append("policy_source_unestablished")
            if policy is not None:
                for key in ("policy_version", "policy_modality"):
                    if isinstance(observed.get(key), str):
                        policy[key] = observed[key]
            item["findings"].append({
                "finding_id": _digest(["records-received-finding-v1", item["item_id"], agency, field,
                                       row.get("request_id"), row["citation"]]),
                "rating": row["status"], "rule_ref": row["citation"], "policy_ref": policy,
                "page": unit["page"], "quote": line, "citation": cite,
                "comparison_ref": {"report_sha256": report["comparison_sha256"], "row_index": index, "field": field},
                "comparison_reason": row.get("reason"), "comparison_expected": row.get("expected"),
                "review_required": True, "needs_attorney_review": True, "legal_conclusion": False,
                "law_package_status": "draft", "disclaimer": DISCLAIMER,
                "ocr_review_status": item["ocr_review_status"]})
            break
        if not matched and evidence:
            report["diagnostics"].append("comparison_evidence_unresolved")
            unresolved_agencies.add(agency)
    report["diagnostics"] = sorted(set(report["diagnostics"]))
    for info in by_original.values():
        item = info["item"]
        item["findings"].sort(key=lambda f: f["finding_id"])
        if item["findings"]:
            item["comparison_status"] = "partial" if item["agency_id"] in unresolved_agencies else "mapped"


def _safe_dir(path):
    path = Path(os.path.abspath(path))
    for part in (*reversed(path.parents), path):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        _need(stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode), "received_items_unsafe_path")
    return path


def write_run_received_items(project, run_id):
    """Outputs-stage hook, called after write_run_comparison. Returns the report.

    Writes only run-outputs/<run>/outcomes/received-items.json (0700/0600).
    Identical replay is a no-op; a different existing artifact is refused.
    """
    from ..run_stages import LedgerBusy, snapshot_ro
    try:
        _need(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", run_id) is not None, "received_items_run_id")
        project = _safe_dir(project)
        processing = _safe_dir(project / "processing-v1")
        run_dir = _safe_dir(project / "run-outputs" / run_id)
        comparison, digest = _load_comparison(run_dir / "comparison.json", run_id)
        _need(not (processing / "ledger.sqlite").is_symlink(), "received_items_symlink_path")
        with snapshot_ro(processing / "ledger.sqlite") as db:
            report = collect(db, processing, comparison, run_id=run_id, comparison_sha256=digest)
        encoded = (_canon(report) + "\n").encode()
        _need(len(encoded) <= MAX_OUTPUT_BYTES, "received_items_output_bound")
        outcomes = _safe_dir(run_dir / "outcomes")
        outcomes.mkdir(mode=0o700, exist_ok=True)
        os.chmod(outcomes, 0o700)
        target = outcomes / "received-items.json"
        if os.path.lexists(target):
            info = target.lstat()
            _need(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, "received_items_existing_output")
            _need(read_pinned(target, sha(encoded), MAX_OUTPUT_BYTES) == encoded, "received_items_existing_output")
            return report
        temporary = outcomes / ".received-items.json.tmp"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            if os.path.lexists(target):  # the stage holds the run lock; re-check just before installing
                raise ValueError("received_items_existing_output")
            os.replace(temporary, target)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
        return report
    except (ValueError, sqlite3.Error, LedgerBusy) as error:
        raise OSError("RECEIVED_ITEMS_UNAVAILABLE") from error
