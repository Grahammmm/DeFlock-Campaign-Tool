"""Private comparison worksheet for the fact custodian (records-comparison-worksheet-v1).

Seeds agency/request inventory from a run's comparison.json, one blank qualification entry per CRITERIA field and the
reviewed (checked) fact-store facts as citation CANDIDATES. Candidates are never citations, attestations or
applicability: an unfilled worksheet compiles to all unknown. Writes only the new --out file; prints counts only.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import sys

from .outcomes import comparison as cmp

SCHEMA = "records-comparison-worksheet-v1"
REPO = Path(__file__).resolve().parents[2]
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
QUOTE_CAP = 400
# fact_type -> worksheet field: CRITERIA names map to themselves, plus four fixed aliases; anything else is unmapped.
FACT_FIELDS = {**{field: field for field in cmp.CRITERIA}, "retention_days": "retention", "search_log_fields": "access_logs",
               "audit_reports": "audit_practice", "training_records": "authorized_users_training"}


class WorksheetError(Exception):
    pass


def _unique(pairs):
    result = {}
    for key, item in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = item
    return result


def _read_comparison(path):
    """Bounded regular no-follow read; returns (run_id, as_of, agency ids, request pairs) from rows only."""
    try:
        path = cmp._checked(path)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise OSError("wrong_file_type")
            raw = handle.read(cmp.MAX_BYTES + 1)
        if len(raw) > cmp.MAX_BYTES:
            raise ValueError("bound")
        doc = json.loads(raw, object_pairs_hook=_unique)
        if type(doc) is not dict or doc.get("schema") != cmp.SCHEMA or type(doc.get("rows")) is not list:
            raise ValueError("schema")
        cmp._day(doc.get("as_of"))
        if type(doc.get("run_id")) is not str:
            raise ValueError("run_id")
        agencies, owners = set(), {}
        for row in doc["rows"]:
            aid = row.get("agency_id") if type(row) is dict else None
            if type(aid) is not str or not ID.fullmatch(aid) or aid in (".", ".."):
                raise ValueError("agency_id")
            agencies.add(aid)
            rid = row.get("request_id")
            if row.get("field") == "cpra_response_deadline" and rid is not None:
                if type(rid) is not str or not ID.fullmatch(rid) or owners.setdefault(rid, aid) != aid:
                    raise ValueError("request_id")
        return doc["run_id"], doc["as_of"], sorted(agencies), tuple(sorted((a, r) for r, a in owners.items()))
    except (OSError, ValueError, UnicodeError, RecursionError, AttributeError):
        raise WorksheetError("comparison_unreadable") from None


def build_requests(comparison_path, request_inventory):
    """DF1117L-08 seam: the request inventory only in this unit (no timing fields, no mismatches)."""
    return {"items": [{"agency_id": a, "request_id": r} for a, r in request_inventory], "mismatches": []}


def _tool():
    path = REPO / "tools" / "flock_facts.py"
    if not path.is_file():
        raise WorksheetError("fact_tool_unavailable")
    spec = importlib.util.spec_from_file_location("_comparison_worksheet_flock_facts", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _candidates(tool, store, agency_id):
    """(mapped {field: [candidate]}, unmapped [candidate], withheld counts) for one exact store slug; fails closed."""
    folder = store / "agencies" / agency_id
    try:
        for part in (store / "agencies", folder):
            if os.path.islink(part):
                raise WorksheetError("fact_store_unsafe")
        if not os.path.lexists(folder / "FACTS.csv"):
            return {}, [], {"disputed": 0, "unreviewed": 0}
        rows, _, codes = tool.read_reviewed(store, agency_id)
    except tool.Locked:
        raise WorksheetError("fact_store_busy") from None
    except OSError:
        raise WorksheetError("fact_store_unsafe") from None
    if codes:
        raise WorksheetError("fact_store_refused")
    mapped, unmapped = {}, []
    for row in sorted((r for r in rows if r["status"] == "checked"), key=lambda r: (r["fact_type"], r["fact_id"])):
        page = row["page"].strip()
        item = {"fact_id": row["fact_id"], "kind": row["kind"], "fact_type": row["fact_type"],
                "effective_from": row["effective_from"] or None, "effective_to": row["effective_to"] or None,
                "source_sha256_prefix": row["source_sha256"][:12], "page": int(page) if page.isdigit() else None,
                "verbatim_quote": row["verbatim_quote"][:QUOTE_CAP]}
        field = FACT_FIELDS.get(row["fact_type"])
        (mapped.setdefault(field, []) if field else unmapped).append(item)
    return mapped, unmapped, {s: sum(r["status"] == s for r in rows) for s in ("disputed", "unreviewed")}


def _blank(field, candidates):
    spec = cmp.WORKSHEET_FIELDS[field]
    return {"state": "unresolved", "counterevidence": [], "custodian_attested": False, "applicability_confirmed": False,
            "citations": [], "value": {key: None for key in spec["value"]},
            "descriptors": {"fact": spec["fact"], "value": spec["value"]}, "candidates": candidates}


def build_worksheet(comparison_path, facts_store):
    """The worksheet document; reads only; refuses a store inside the checkout. Never fills a value, role or attestation."""
    run_id, as_of, agencies, inventory = _read_comparison(comparison_path)
    try:
        store = cmp._checked(Path(facts_store).resolve(), directory=True)
    except OSError:
        raise WorksheetError("fact_store_unsafe") from None
    if store == REPO or REPO in store.parents:
        raise WorksheetError("fact_store_in_checkout")
    tool = _tool()
    entries, totals = [], {"candidates": 0, "unmapped": 0, "withheld_disputed": 0, "withheld_unreviewed": 0}
    for aid in agencies:
        mapped, unmapped, withheld = _candidates(tool, store, aid)
        entries.append({"agency_id": aid, "regulated_roles": [], "jurisdiction": None, "withheld": withheld,
                        "facts": {field: _blank(field, mapped.get(field, [])) for field in cmp.CRITERIA}, "unmapped": unmapped})
        totals["candidates"] += sum(map(len, mapped.values()))
        totals["unmapped"] += len(unmapped)
        totals["withheld_disputed"] += withheld["disputed"]
        totals["withheld_unreviewed"] += withheld["unreviewed"]
    doc = {"schema": SCHEMA, "private": True, "review_required": True, "legal_conclusion": False, "run_id": run_id,
           "as_of": as_of, "reviewer_role": "", "review_date": "", "agencies": entries,
           "requests": build_requests(comparison_path, inventory)}
    return doc, {"agencies": len(entries), "requests": len(inventory), "fields": len(entries) * len(cmp.CRITERIA), **totals}


def _project_roots(*paths):
    """Directories that look like a campaign project: a canonical run-outputs ancestor or a processing-v1 ledger folder."""
    roots = set()
    for path in paths:
        for parent in (path, *path.parents):
            if parent.name == "run-outputs":
                roots.add(parent.parent)
            if (parent / "processing-v1").is_dir() or (parent / cmp.INPUT_NAME).exists():
                roots.add(parent)
    return roots


def _write(out, comparison_path, facts_store, data):
    """Exclusive private create of a new file in a safe existing directory outside checkout, store and project."""
    out = Path(os.path.abspath(out))
    if os.path.lexists(out):
        raise WorksheetError("output_exists")
    try:
        parent = cmp._checked(out.parent, directory=True).resolve()
    except OSError:
        raise WorksheetError("output_parent_unsafe") from None
    store = Path(facts_store).resolve()
    guarded = {REPO, store, *_project_roots(parent, Path(os.path.abspath(comparison_path)).parent)}
    if any(parent == g or g in parent.parents for g in guarded):
        raise WorksheetError("output_location_refused")
    target = parent / out.name
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    info = os.fstat(fd)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException as exc:
        try:  # remove only the file this call created
            if (target.lstat().st_dev, target.lstat().st_ino) == (info.st_dev, info.st_ino):
                target.unlink()
        except OSError:
            pass
        if isinstance(exc, OSError):
            raise WorksheetError("output_write_failed") from None
        raise


def main(argv=None):
    os.umask(0o077)
    parser = argparse.ArgumentParser(prog="records comparison-worksheet", description=__doc__)
    for name in ("comparison", "facts-store", "out"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args(argv)
    try:
        doc, counts = build_worksheet(args.comparison, args.facts_store)
        _write(args.out, args.comparison, args.facts_store, (json.dumps(doc, sort_keys=True, indent=2) + "\n").encode("utf-8"))
    except WorksheetError as exc:
        print("comparison_worksheet_" + str(exc), file=sys.stderr)
        return 1
    print(" ".join(f"{k}={v}" for k, v in counts.items()) + " | private worksheet; candidates are not citations")
    return 0


if __name__ == "__main__":
    sys.exit(main())
