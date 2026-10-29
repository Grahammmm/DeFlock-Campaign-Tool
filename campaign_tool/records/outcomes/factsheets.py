"""Private, review-required agency fact sheets from existing extracted text.

No model, original-document parsing, fetching, stage promotion or publication.
Values are exact source statements, NOT adjudicated present-day facts. In
particular policy dates are quoted, never used to infer which policy is current.
Non-paginated sources cannot meet the page citation contract and remain gaps.
"""
from __future__ import annotations

import html
import json
import os
from pathlib import Path
import re
import sqlite3

from ..ledger import store
from ..ledger_catalog import scope_excluded
from ..understanding.evidence import decode, is_hash, read_pinned, sha

SCHEMA = "records-agency-factsheets-v1"
MAX_ROWS = 100000
MAX_UNIT_BYTES = 1024 * 1024
MAX_TEXT_BYTES = 128 * 1024 * 1024
MAX_OUTPUT_BYTES = 64 * 1024 * 1024
FIELDS = {
    "vendor": ("Vendor", r"\bvendor\s*:\s*\S|\b(?:Flock Safety|Motorola Solutions|Axon Enterprise)\b"),
    "contract": ("Contract", r"\b(?:contract|agreement|order form)\b"),
    "cost": ("Cost", r"(?:\$\s*\d|\b(?:cost|price|annual fee|amount due)\s*:)"),
    "camera_count": ("Camera count", r"(?:\b\d+\s+(?:(?:ALPR|LPR|fixed|mobile)\s+)?cameras?\b|\bcamera (?:count|quantity)\s*:)"),
    "camera_locations": ("Camera locations", r"\b(?:camera locations?\s*:|cameras?\s+(?:located|installed|deployed)\s+(?:at|on|near))"),
    "retention": ("Retention", r"\b(?:retention|retain(?:ed)?|deleted? after|purged? after)\b"),
    "outside_sharing": ("Outside sharing", r"\b(?:sharing|shared? with|outside access|external access)\b"),
    "audits": ("Audits", r"\baudit(?:s|ed|ing)?\b"),
    "search_logs": ("Search logs", r"\b(?:search|query|access) logs?\b"),
    "policy_in_force": ("Policy in force (source statement only)", r"\b(?:policy.*\b(?:effective|in force|adopted|superseded|rescinded)|(?:effective|in force|adopted|superseded|rescinded).*\bpolicy)\b"),
}
PATTERNS = {key: re.compile(pattern, re.I) for key, (_, pattern) in FIELDS.items()}


def _need(condition, code):
    if not condition:
        raise ValueError(code)


def _rows(db, query, args=()):
    rows = db.execute(query, args).fetchmany(MAX_ROWS + 1)
    _need(len(rows) <= MAX_ROWS, "factsheet_metadata_bound")
    return rows


def _path(value):
    path = Path(os.path.abspath(value))
    _need(not any(p.is_symlink() for p in (path, *path.parents)), "factsheet_symlink_path")
    return path


def collect(db, processing, run_id):
    """Read canonical metadata and pinned current-unit bytes; never write the ledger.

    An agency gets a sheet even with no eligible records. Hinted/blocked and
    ambiguous multi-agency attribution cannot supply facts. Current extraction
    membership and payload/text hashes are checked, not claimed semantic review.
    """
    db.row_factory = sqlite3.Row
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    agencies = _rows(db, "SELECT id,name FROM agencies ORDER BY id") if "agencies" in tables else []
    sheets = {a["id"]: {"agency_id": a["id"], "agency": a["name"],
              "document_sha256s": [], "gaps": [], "fields": {
                  key: {"status": "NOT FOUND", "statements": []} for key in FIELDS}}
              for a in agencies}
    result = {"schema": SCHEMA, "run_id": run_id, "review_required": True,
              "semantic_acceptance": False, "publication_ready": False,
              "basis": "Exact source excerpts; NOT FOUND means no supported page-cited statement, not absence of conduct.",
              "gaps": [], "agencies": list(sheets.values())}
    if "agencies" not in tables:
        result["gaps"].append("agency_registry_unavailable")
        return result
    if "extraction_adapter_current" not in tables:
        result["gaps"].append("current_extraction_unavailable")
        return result
    _need({"extraction_adapter_imports", "units", "joins", "requests"} <= tables,
          "factsheet_incomplete_schema")
    joined = {}
    for row in _rows(db, "SELECT j.original_sha256,j.agency_id,j.request_id,r.agency_id AS request_agency "
                    "FROM joins j LEFT JOIN requests r ON r.id=j.request_id WHERE j.status='typed' ORDER BY j.id"):
        agency = row["agency_id"]
        _need(agency in sheets and (row["request_id"] is None or row["request_agency"] == agency),
              "factsheet_typed_join_mismatch")
        joined.setdefault(row["original_sha256"], set()).add(agency)
    consumed = 0
    for subject, agency_ids in sorted(joined.items()):
        _need(is_hash(subject), "factsheet_original_hash")
        try:
            original = store.effective_original(db, subject)
        except ValueError:
            for agency in agency_ids:
                sheets[agency]["gaps"].append({"document_sha256": subject, "code": "effective_original_unavailable"})
            continue
        if original is None:
            raise ValueError("factsheet_original_missing")
        if scope_excluded(original) or original["role"] not in {"agency_original", "container_child", "original"}:
            continue
        if len(agency_ids) != 1:
            for agency in agency_ids:
                sheets[agency]["gaps"].append({"document_sha256": subject, "code": "ambiguous_agency_attribution"})
            continue
        sheet = sheets[next(iter(agency_ids))]
        sheet["document_sha256s"].append(subject)
        current = db.execute("SELECT i.id,i.manifest_json,i.manifest_sha256 FROM extraction_adapter_current c "
                             "JOIN extraction_adapter_imports i ON i.id=c.import_id AND i.original_sha256=c.original_sha256 "
                             "WHERE c.original_sha256=?", (subject,)).fetchone()
        if current is None:
            sheet["gaps"].append({"document_sha256": subject, "code": "current_extraction_unavailable"})
            continue
        _need(sha(current["manifest_json"].encode()) == current["manifest_sha256"], "factsheet_manifest_hash")
        manifest = decode(current["manifest_json"])
        members = manifest.get("unit_ids")
        _need(manifest.get("original_sha256") == subject and type(members) is list
              and len(members) <= 10000 and all(type(x) is str for x in members)
              and len(set(members)) == len(members), "factsheet_manifest_members")
        units = _rows(db, "SELECT * FROM units WHERE original_sha256=? AND json_valid(provenance_json) "
                      "AND json_extract(provenance_json,'$.extraction_import_id')=? ORDER BY legacy_ordinal", (subject, current["id"]))
        _need([u["id"] for u in units] == members, "factsheet_current_unit_set")
        for ordinal, unit in enumerate(units, 1):
            provenance = decode(unit["provenance_json"])
            digest = provenance.get("payload_sha256")
            _need(is_hash(digest) and provenance.get("ordinal") == ordinal, "factsheet_payload_binding")
            # Fixed producer-owned CAS root; record paths cannot select arbitrary files.
            expected_path = Path(processing) / "artifacts" / "evidence" / digest
            _need(unit["derived_path"] == str(expected_path), "factsheet_payload_path")
            raw = read_pinned(expected_path, digest, MAX_UNIT_BYTES)
            consumed += len(raw)
            _need(consumed <= MAX_TEXT_BYTES, "factsheet_text_bound")
            payload = decode(raw)
            _need(type(payload) is dict and set(payload) == {"kind", "locator", "text", "data"}
                  and type(payload["text"]) is str and type(payload["locator"]) is dict
                  and payload["kind"] == unit["unit_type"]
                  and payload["locator"] == decode(unit["locator"])
                  and sha(payload["text"].encode()) == unit["text_sha256"]
                  and unit["id"] == sha(json.dumps([current["id"], ordinal, digest], sort_keys=True,
                                       separators=(",", ":"), ensure_ascii=False).encode()), "factsheet_unit_binding")
            locator = payload["locator"]
            pages = [locator[k] for k in ("page", "page_no") if k in locator]
            if not pages or any(type(p) is not int or p < 1 for p in pages) or len(set(pages)) != 1:
                sheet["gaps"].append({"document_sha256": subject, "unit_id": unit["id"], "code": "page_locator_unavailable"})
                continue
            _need(any(p.get("page_no") == pages[0] for p in manifest.get("pages", [])
                      if isinstance(p, dict)), "factsheet_page_not_in_manifest")
            for line in payload["text"].splitlines():
                quote = line.strip()
                if not quote or "?" in quote:
                    continue
                if len(quote) > 2000:
                    sheet["gaps"].append({"document_sha256": subject, "unit_id": unit["id"], "code": "statement_too_long"})
                    continue
                for field, pattern in PATTERNS.items():
                    if not pattern.search(quote):
                        continue
                    # A heading alone is not a statement of a value.
                    if quote.rstrip(":").casefold() in {field.replace("_", " "), FIELDS[field][0].casefold()}:
                        continue
                    statement = {"quote": quote, "citation": {"document_sha256": subject, "page": pages[0],
                                 "unit_id": unit["id"], "text_sha256": unit["text_sha256"],
                                 "payload_sha256": digest}}
                    values = sheet["fields"][field]
                    if statement not in values["statements"]:
                        values["statements"].append(statement)
                        values["status"] = "SOURCE STATEMENT — REVIEW REQUIRED"
    return result


def markdown(report):
    def escape(value):
        text = html.escape(str(value), quote=True).replace("\n", " ").replace("\r", " ")
        return "".join("&#" + str(ord(c)) + ";" if c in "[]()*_`!#\\" else c for c in text)
    lines = ["# Agency fact sheets", "", "PRIVATE — REVIEW REQUIRED — NOT FOR PUBLICATION", "",
             "Run: " + escape(report["run_id"]), "", report["basis"],
             "Policy dates, proposals, negations and conflicting statements are not resolved automatically.", ""]
    if report["gaps"]:
        lines += ["Coverage gaps: " + escape(json.dumps(report["gaps"])), ""]
    if not report["agencies"]:
        lines += ["No agencies registered in the canonical ledger.", ""]
    for sheet in report["agencies"]:
        lines += ["## " + escape(sheet["agency"]) + " (" + escape(sheet["agency_id"]) + ")", ""]
        for field, (label, _) in FIELDS.items():
            lines.append("### " + label)
            values = sheet["fields"][field]
            if not values["statements"]:
                lines.append("NOT FOUND")
            for item in values["statements"]:
                cite = item["citation"]
                # HTML-escaped text inside code prevents source Markdown links,
                # headings, images or active HTML from becoming presentation.
                quote = escape(item["quote"]).replace("`", "&#96;")
                lines.append("- Source statement: <code>" + quote + "</code> [document sha256 "
                             + cite["document_sha256"] + ", page " + str(cite["page"]) + "]")
            lines.append("")
        if sheet["gaps"]:
            lines += ["Coverage gaps: " + escape(json.dumps(sheet["gaps"], sort_keys=True)), ""]
    return "\n".join(lines) + "\n"


def write_run_factsheets(processing, output, run_id):
    """One outputs-stage call; own files only in run-outputs/<run>/.

    An identical replay is read-only; changed/partial existing output is refused.
    Errors flow to the
    existing OUTPUTS_WRITE_FAILED handler; no success result is emitted. Old
    snapshots and canonical stores are never replaced by this adapter.
    """
    from ..run_stages import LedgerBusy, snapshot_ro
    try:
        processing, output = _path(processing), _path(output)
        _need(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", run_id) is not None,
              "factsheet_run_id")
        _need(output.name == run_id and output.parent.name == "run-outputs"
              and output.parent.parent == processing.parent, "factsheet_output_root")
        _path(processing / "ledger.sqlite")
        with snapshot_ro(processing / "ledger.sqlite") as db:
            report = collect(db, processing, run_id)
        encoded = (json.dumps(report, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n").encode()
        rendered = markdown(report).encode()
        _need(len(encoded) + len(rendered) <= MAX_OUTPUT_BYTES, "factsheet_output_bound")
        files = (("agency-factsheets.json", encoded), ("agency-factsheets.md", rendered))
        if output.exists():
            _need({p.name for p in output.iterdir()} == {name for name, _ in files}, "factsheet_existing_output")
            for name, raw in files:
                read_pinned(output / name, sha(raw), MAX_OUTPUT_BYTES)
            return
        output.mkdir(mode=0o700)  # Refuse existing output, including any symlink.
        for name, raw in files:
            fd = os.open(output / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
    except (ValueError, sqlite3.Error, LedgerBusy) as error:
        raise OSError("FACTSHEETS_UNAVAILABLE") from error
