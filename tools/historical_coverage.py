"""BF-df-historical: what can be established about the past from what we hold.

Per agency, the earliest evidenced date for installation, policy and vendor relationship, each cited to a held
document (id, page, source_sha256); a period no held document speaks for is unevidenced, never inferred or filled
from another agency. Document identity only, no text. Private, review-required, not a legal finding.

  historical_coverage.py build --store ABS --out NEWDIR [--agency SLUG ...] [--as-of YYYY-MM-DD] [--json]
  historical_coverage.py report DIR [--json]
Exit: 0 every source loaded, 1 any failed (rows still written, failures named), 2 usage. Stdlib only; read-only over the store.
"""
import argparse
import csv
import importlib.util
import json
import os
import re
import sys
from datetime import date, timedelta
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
SCHEMA = "historical-coverage-v1"
JSON_NAME, MD_NAME = "historical-coverage.json", "historical-coverage-report.md"
DIMENSIONS = ("installation", "policy", "vendor")
BASIS = {"installation": "document_date", "policy": "in_force_statement", "vendor": "contract_term_start"}
TYPES = {"installation": {"installation", "in-operation", "operational", "system-installed", "deployment"},
         "vendor": {"vendor", "vendor-contract", "contract", "invoice", "vendor-relationship", "procurement"}}
SEQUENCE_HEADING = "Dates in sequence"
SEQUENCE_SENTENCE = "These are dates from documents the campaign holds; this is not a claim that any agency violated a law."
STATUS_TAIL = "private; review-required; not a legal finding"
SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
HEX = re.compile(r"[0-9a-f]{64}")
_MODS = {}


class Usage(Exception):
    pass


def _module(name):
    """The sibling tool by file path, never a copy; loaded once."""
    if name not in _MODS:
        spec = importlib.util.spec_from_file_location("_hc_" + name[:-3], TOOLS / name)
        _MODS[name] = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        spec.loader.exec_module(_MODS[name])  # type: ignore[union-attr]
    return _MODS[name]


def _prec(text):
    """3 = day, 2 = month, 1 = year; None when the text is not a real date at one of those precisions."""
    if not re.fullmatch(r"[0-9]{4}(-[0-9]{2}(-[0-9]{2})?)?", text or ""):
        return None
    try:
        _lo(text)
    except ValueError:
        return None
    return text.count("-") + 1


def _lo(text):
    """First day of the stated period, for ordering only; the stated precision stays in the text."""
    y, m, d = (text.split("-") + ["1", "1"])[:3]
    return date(int(y), int(m), int(d))


def _after(text):
    """The next period after a date at its own precision (a year never widens to a day)."""
    kind = _prec(text)
    if kind == 3:
        return (_lo(text) + timedelta(days=1)).isoformat()
    year, month = int(text[:4]), int(text[5:7] or 0)
    if kind == 2:
        return "%04d-%02d" % (year + (month == 12), month % 12 + 1)
    return "%04d" % (year + 1)


def _hi(text): return _lo(_after(text)) - timedelta(days=1)


def _key(text): return (_lo(text), _prec(text))  # the coarser reading first on a tie; nothing is widened


def _cite(dim, when, doc, page, others):
    corr = [{"date": d, "document_id": h, "page": p, "source_sha256": h}
            for d, h, p in sorted(set(others) - {(when, doc, page)}, key=lambda o: (_key(o[0]), o[1], str(o[2])))]
    return {"status": "evidenced", "date": when, "document_id": doc, "page": page, "source_sha256": doc,
            "basis": BASIS[dim], "corroborating": corr}


def _none(reason): return {"status": "unevidenced", "reason": reason}


def load_facts(path, slug, recorder):
    """(usable rows, errors) for one agency's FACTS.csv; a missing file is clean and empty, a linked one refuses."""
    facts = _module("flock_facts.py")
    if recorder:
        recorder(path)
    if os.path.islink(path):
        return [], ["FACTS.csv: is a link; refusing"]
    if not path.is_file():
        return [], []
    try:
        with open(path, encoding="utf-8-sig", newline="") as handle:
            records = list(csv.reader(handle))
    except (OSError, UnicodeDecodeError, csv.Error):
        return [], ["FACTS.csv: file unreadable"]
    if not records or records[0] not in (facts.V1, facts.V2, facts.V3):
        return [], ["FACTS.csv: header does not match FACTS.csv"]
    usable, errors = [], []
    for num, rec in enumerate(records[1:], 2):
        if len(rec) != len(records[0]):
            errors += ["FACTS.csv: row %d: columns do not match the header" % num] if rec else []
            continue
        row = dict(zip(records[0], (c.strip() for c in rec)))
        bad = [(f, w) for f, w in facts.validate(row, records[0] in (facts.V2, facts.V3), slug)
               if not (f == "effective_from" and _prec(row.get(f, "")) in (1, 2))]  # a stated year or month is kept as stated
        errors += ["FACTS.csv: row %d: %s %s" % (num, f, w) for f, w in bad]
        usable += [] if bad else [row]
    return usable, errors


def _facts_dim(dim, rows, textless):
    mine = [r for r in rows if r["fact_type"] in TYPES[dim]]
    dated = sorted(((r.get("effective_from", ""), r["source_sha256"], int(r["page"])) for r in mine
                    if r["value"] != "UNREADABLE" and _prec(r.get("effective_from", ""))),
                   key=lambda o: (_key(o[0]), o[1], o[2]))
    if dated:
        return _cite(dim, *dated[0], dated[1:])
    if not mine:
        return _none("no-text" if textless else "no-document")
    return _none("no-date-in-document" if any(r["value"] != "UNREADABLE" for r in mine) else "document-unreadable")


def _policy_dim(prows, textless):
    """The earliest governs_from of an adopted POLICY row (flock_law_policy's governing windows)."""
    day = _module("flock_law_policy.py").day
    adopted = sorted((r["governs_from"], r["doc_sha256"], r["policy_row_id"]) for _, r in prows
                     if r["adoption_evidence"] not in ("", "unverified") and day(r["governs_from"])
                     and HEX.fullmatch(r["doc_sha256"]) and HEX.fullmatch(r["adoption_sha256"]))
    if adopted:
        return _cite("policy", *adopted[0], adopted[1:])
    return _none("no-date-in-document") if prows else _none("no-text" if textless else "no-document")


def build_agency(store, slug, recorder=None):
    """One agency built from its own two files only. (row, hashes)."""
    base = store / "agencies" / slug
    rows, errors = load_facts(base / "FACTS.csv", slug, recorder)
    ppath, prows, perrs = base / "POLICY.csv", [], []
    if recorder:
        recorder(ppath)
    if os.path.islink(ppath):
        perrs = ["is a link; refusing"]
    elif ppath.is_file():
        prows, perrs = _module("flock_law_policy.py").load_policy(ppath, slug)
    values = {h: {r["value"] for r in rows if r["source_sha256"] == h} for h in {r["source_sha256"] for r in rows}}
    textless = any(v == {"UNREADABLE"} for v in values.values())  # a document is held, nothing was extracted
    dims = {"installation": _facts_dim("installation", rows, textless), "vendor": _facts_dim("vendor", rows, textless),
            "policy": _policy_dim(prows, textless)}
    return ({"agency": slug, "dimensions": {k: dims[k] for k in DIMENSIONS}, "gaps": [], "sequence": None, "_policy_rows": prows,
             "errors": errors + ["POLICY.csv: " + e for e in perrs]},
            {d["source_sha256"] for d in dims.values() if d["status"] == "evidenced"})


def _finish(row, as_of):
    """The gap map and the sequence, from this agency's own dates and the as-of date."""
    dims, prows = row["dimensions"], row.pop("_policy_rows", [])
    policy_tool = _module("flock_law_policy.py")
    prows = [(n, r) for n, r in prows if not list(policy_tool.problems(r, "policy", row["agency"]))]
    got = sorted((d["date"] for d in dims.values() if d["status"] == "evidenced"), key=_key)
    inst, pol = dims["installation"], dims["policy"]
    try:
        start = _after(got[0]) if got and as_of else None
        if start and _lo(start) <= _hi(as_of):
            row["gaps"] = [{"from": start, "to": as_of, "dimension": k, "reason": dims[k]["reason"]}
                           for k in DIMENSIONS if dims[k]["status"] == "unevidenced"]
            for gap in policy_tool.gaps_rows(prows, _hi(as_of).isoformat())[0]:
                if gap["kind"] not in ("gap", "closed"):
                    continue  # wholly unestablished policy is already covered above
                lo = _after(gap["to"]) if gap["kind"] == "closed" else gap["from"]
                hi = as_of if gap["kind"] == "closed" else min(gap["to"], _hi(as_of).isoformat())
                lo = start if _lo(start) > _lo(lo) else lo
                if _lo(lo) <= _hi(hi):
                    row["gaps"].append({"from": lo, "to": hi, "dimension": "policy", "reason": "no-document"})
        if inst["status"] == pol["status"] == "evidenced" and _hi(inst["date"]) < _lo(pol["date"]):
            days = (_lo(pol["date"]) - _lo(inst["date"])).days if _prec(inst["date"]) == _prec(pol["date"]) == 3 else None
            row["sequence"] = {"installation": inst["date"], "policy": pol["date"], "interval_days": days,
                               "heading": SEQUENCE_HEADING, "sentence": SEQUENCE_SENTENCE}
    except (OverflowError, ValueError):
        pass  # a date at the edge of the calendar: shown as stated, no range computed


def build(store, slugs, as_of, recorder=None):
    """(report dict, any_failed) for the given agencies, each built from its own files."""
    built = [build_agency(store, slug, recorder) for slug in sorted(slugs)]
    rows, hashes = [b[0] for b in built], set().union(*(b[1] for b in built))
    if not as_of:  # no clock: the latest evidenced date in the requested agencies
        dates = [d["date"] for r in rows for d in r["dimensions"].values() if d["status"] == "evidenced"]
        as_of = max(dates, key=_key) if dates else None
    for row in rows:
        _finish(row, as_of)
    state = [{d["status"] for d in r["dimensions"].values()} for r in rows]
    totals = {"agencies": len(rows), "evidenced": sum(s == {"evidenced"} for s in state),
              "partial": sum(len(s) == 2 for s in state), "unevidenced": sum(s == {"unevidenced"} for s in state),
              "no_text": sum(any(d.get("reason") == "no-text" for d in r["dimensions"].values()) for r in rows),
              "documents": len(hashes)}
    return ({"schema": SCHEMA, "review_required": True, "as_of": as_of, "agencies": rows, "totals": totals,
             "note": STATUS_TAIL}, any(r["errors"] for r in rows))


def status_line(doc):
    t = doc["totals"]
    return "historical | agencies=%d evidenced=%d partial=%d unevidenced=%d no-text=%d | %s" % (
        t["agencies"], t["evidenced"], t["partial"], t["unevidenced"], t["no_text"], STATUS_TAIL)


def _dim_text(dim, d):
    if d["status"] == "unevidenced":
        return "| %s | unevidenced | - | - | %s |" % (dim, d["reason"])
    corr = "; corroborated by " + ", ".join("%s %s p.%s" % (c["date"], c["source_sha256"][:12], c["page"])
                                            for c in d["corroborating"]) if d["corroborating"] else ""
    return "| %s | evidenced | %s | %s | document %s page %s%s |" % (
        dim, d["date"], d["basis"], d["source_sha256"][:12], d["page"], corr)


def render_markdown(doc):
    lines = ["# Historical coverage", "", "Every date cites a held document; a period with no held document is shown as unevidenced, "
             "never interpolated.", "", "As of: %s" % (doc["as_of"] or "not set (no evidenced date)"), ""]
    for row in doc["agencies"]:
        lines += ["## %s" % row["agency"], "", "| dimension | status | date | basis | citation or reason |",
                  "| --- | --- | --- | --- | --- |"] + [_dim_text(k, row["dimensions"][k]) for k in DIMENSIONS] + [""]
        if row["gaps"]:
            lines += ["### Gap map", "", "| from | to | dimension | reason |", "| --- | --- | --- | --- |"]
            lines += ["| %(from)s | %(to)s | %(dimension)s | unevidenced: %(reason)s |" % g for g in row["gaps"]] + [""]
        seq = row["sequence"]
        if seq:
            span = "%s days apart" % seq["interval_days"] if seq["interval_days"] else "interval not stated at partial precision"
            lines += ["### %s" % SEQUENCE_HEADING, "", "installation %s, policy %s: %s." % (seq["installation"], seq["policy"], span),
                      "", SEQUENCE_SENTENCE, ""]
        if row["errors"]:
            lines += ["### Source notes", ""] + ["- %s" % e for e in row["errors"]] + [""]
    return "\n".join(lines + [status_line(doc)]) + "\n"


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Usage("usage: build --store ABS --out NEWDIR [--agency SLUG ...] [--as-of YYYY-MM-DD] [--json] | report DIR [--json]")


def _checked(argv):
    """The validated arguments or Usage; nothing is created."""
    p = _Parser(prog="historical_coverage", description="Per-agency earliest evidenced dates (private, review-required, not a legal finding).")
    sub = p.add_subparsers(dest="kind")
    b, r = sub.add_parser("build"), sub.add_parser("report")
    for opt in ("--store", "--out", "--as-of"):
        b.add_argument(opt)
    b.add_argument("--agency", action="append", default=[])
    r.add_argument("dir")
    for s in (b, r):
        s.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    if a.kind == "report":
        found = Path(a.dir) / JSON_NAME
        if not Path(a.dir).is_absolute() or os.path.islink(found) or not found.is_file():
            raise Usage("report DIR must be an absolute folder holding %s" % JSON_NAME)
        return a
    if a.kind != "build" or not (a.store and a.out):
        raise Usage("build needs --store and --out")
    store, out = Path(a.store), Path(a.out)
    for label, path in (("--store", store), ("--out", out)):
        if not path.is_absolute() or Path(os.path.normpath(str(path))) != path:
            raise Usage("%s must be an absolute normalized path" % label)
    agencies = store / "agencies"
    if not agencies.is_dir() or os.path.islink(agencies):
        raise Usage("--store must be a folder with an agencies folder")
    if os.path.lexists(out) or not out.parent.is_dir() or any(x.is_symlink() for x in (out.parent, *out.parent.parents)):
        raise Usage("--out must not exist yet; its parent must exist and be link-free")
    if any(root in (out, *out.parents) for root in (TOOLS.parent, agencies)):
        raise Usage("--out must be outside the repository and outside the store agencies folder")
    if a.as_of and _prec(a.as_of) != 3:
        raise Usage("--as-of must be YYYY-MM-DD")
    for slug in a.agency:
        if not SLUG_RE.match(slug) or len(slug) > 64 or os.path.islink(agencies / slug) or not (agencies / slug).is_dir():
            raise Usage("--agency must name an agency folder in the store")
    a.slugs = sorted(set(a.agency)) or [x.name for x in agencies.iterdir() if x.is_dir() and not x.is_symlink() and SLUG_RE.match(x.name)]
    return a


def _write_new(dirpath, name, text):
    fd = os.open(dirpath / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(text.encode("utf-8"))


def _dump(doc):
    return json.dumps(doc, sort_keys=True, indent=2, ensure_ascii=True) + "\n"


def main(argv=None):
    sys.dont_write_bytecode = True
    try:
        a = _checked(sys.argv[1:] if argv is None else argv)
        if a.kind == "report":
            try:
                doc = json.loads((Path(a.dir) / JSON_NAME).read_text(encoding="utf-8"))
                if doc.get("schema") != SCHEMA:
                    raise ValueError("schema")
                text = _dump(doc) if a.json else render_markdown(doc)
            except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
                raise Usage("report DIR does not hold a readable %s file" % SCHEMA) from exc
            sys.stdout.write(text)
            return 0
        doc, failed = build(Path(a.store), a.slugs, a.as_of)
        out = Path(a.out)
        out.mkdir(mode=0o700)
        os.chmod(out, 0o700)
        _write_new(out, JSON_NAME, _dump(doc))
        _write_new(out, MD_NAME, render_markdown(doc))
        sys.stdout.write(_dump(doc) if a.json else status_line(doc) + "\n")
        return 1 if failed else 0
    except Usage as u:
        print(str(u), file=sys.stderr)
        return 2
    except OSError:
        print("io: output write failed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
