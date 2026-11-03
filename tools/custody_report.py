#!/usr/bin/env python3
"""Custody report: one private page per published finding that answers a challenge (document identity, not document text).

  custody_report.py build --store STORE --finding ID --out NEWDIR   writes NEWDIR/CUSTODY.md and NEWDIR/custody.json
  custody_report.py status --store STORE                             one line per published finding; writes nothing

STORE is a campaign folder: content/findings/*.json, content/sources.json, private/ledger.sqlite + objects (re-hashed by
`status --check`'s check_store), private/reviews/<id>.json and private/publication-log.jsonl. Read-only; marks nothing
reviewed. Every state comes from a recorded field, never from prose. The page carries ids, dates, hashes, enums and the
reviewer's name as recorded; never a quote, summary, title, rationale, note, file name or record text.
PRIVATE: NEWDIR must be absolute, normalised, absent, outside the repository and STORE, with a link-free parent; made 0700
with 0600 files after the page is built and checked. Never publish it.
Exit 0 chain complete, 1 chain incomplete or a source failed (page still written, gaps named), 2 usage/refusal."""
import argparse, importlib.util, json, os, re, sqlite3, sys  # noqa: E401
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from campaign_tool.findings import DECISIONS, reviewed_hash, reviewed_view  # noqa: E402
from campaign_tool.review import CLASSIFICATIONS, ROLES, review_blockers  # noqa: E402
from campaign_tool.store_check import check_store  # noqa: E402

_spec = importlib.util.spec_from_file_location("_custody_privacy", REPO / "tools" / "flock_privacy.py")
privacy = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(privacy)

SCHEMA, MD_NAME, JSON_NAME = "custody-report-v1", "CUSTODY.md", "custody.json"
ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
SAFE = re.compile(r"[A-Za-z0-9_.:-]{1,64}")
HEX = re.compile(r"[0-9a-f]{64}")
STAMP = re.compile(r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:\d{2})?)?")
WHO = re.compile(r"[A-Za-z][A-Za-z0-9 ._'-]{0,79}")
STALE = "stale review (content changed after review)"
ESTABLISH = (
    "The SHA-256 hashes on this page establish byte identity, not authenticity.",
    "They show only that the campaign has held these exact bytes since the recorded time.",
    "This page does not say that any document is genuine, complete or correctly understood.",
    "It is not a claim that any agency broke a law.")
Refused = type("Refused", (Exception,), {"__doc__": "A refusal: one fixed reason printed, nothing created."})

class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Refused("usage_invalid")

def safe(value, rx=SAFE):
    """A recorded value shown only when it is a plain identifier; anything else is named, never echoed."""
    return "not-recorded" if value is None or value == "" else value if isinstance(value, str) and rx.fullmatch(value) \
        and not privacy.categories(value) else "withheld"

def _json(path):
    if path.is_symlink() or not path.is_file():
        raise Refused("unreadable")
    return json.loads(path.read_text(encoding="utf-8"))

def published(store):
    """-> {id: finding} for content/findings/*.json with state published; an id twice is refused, never guessed."""
    folder, found = store / "content" / "findings", {}
    if folder.is_symlink() or not folder.is_dir():
        raise Refused("store_findings_unavailable")
    for path in sorted(folder.glob("*.json")):
        try:
            doc = _json(path)
        except (Refused, OSError, ValueError):
            continue
        if isinstance(doc, dict) and doc.get("state") == "published" and isinstance(doc.get("id"), str) \
                and ID.fullmatch(doc["id"]):
            found.setdefault(doc["id"], []).append(doc)
    return found

def read_store(store, errors):
    """Library, ledger receipts and objects, the re-hash result and the publication log: each failure is a named error."""
    library, receipts, objects, check, log = {}, {}, None, None, []
    try:
        for entry in _json(store / "content" / "sources.json"):
            if isinstance(entry, dict) and isinstance(entry.get("sha256"), str):
                library.setdefault(entry["sha256"], entry)
    except (Refused, OSError, ValueError, TypeError):
        errors.append("library: content/sources.json unreadable")
    ledger = store / "private" / "ledger.sqlite"
    if ledger.is_symlink() or not ledger.is_file():
        errors.append("ledger: private/ledger.sqlite missing; no source was re-hashed")
    else:
        try:
            db = sqlite3.connect(ledger.as_uri() + "?mode=ro", uri=True)
            try:
                objects = {row[0] for row in db.execute("SELECT sha256 FROM objects")}
                for rid, sha, source, when in db.execute(
                        "SELECT receipt_id, object_sha256, source_id, received_at FROM receipts ORDER BY receipt_id"):
                    receipts.setdefault(sha, []).append({"receipt_id": safe(rid), "request_id": safe(source),
                                                         "preserved_at": safe(when, STAMP)})
            finally:
                db.close()
            check = check_store(store)  # status --check's own re-hash; never re-implemented here
        except (sqlite3.Error, ValueError, OSError):
            errors.append("ledger: re-hash check could not run")
            objects, check = None, None
    path = store / "private" / "publication-log.jsonl"
    if path.is_file() and not path.is_symlink():
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            try:
                entry = json.loads(line)
                log.append(entry if isinstance(entry, dict) else {})
            except ValueError:
                errors.append("publication log: line %d unreadable" % n)
    return library, receipts, objects, check, log

def rehash(sha, objects, check):
    if not isinstance(sha, str) or not HEX.fullmatch(sha) or objects is None or check is None:
        return "not-checked", None
    if sha not in objects:
        return "not-in-ledger", None
    problem = next((p.get("problem") for p in check["problems"] if p.get("sha256") == sha), None)
    return ("differs", safe(problem)) if problem else ("matches", None)

def review_section(store, finding, digest, errors):
    path = store / "private" / "reviews" / (finding["id"] + ".json")
    raw = []
    if os.path.lexists(path):
        try:
            raw = _json(path)
            raw = raw if isinstance(raw, list) else []
        except (Refused, OSError, ValueError):
            errors.append("review receipts: private/reviews file unreadable")
            raw = []
    rows, counted = [], set()
    for r in (r for r in raw if isinstance(r, dict)):
        current = r.get("content_sha256") == digest
        role = r.get("role") if r.get("role") in ROLES else "unknown"
        decision = r.get("decision") if r.get("decision") in DECISIONS else "unknown"
        reviewer = safe(r.get("reviewer"), WHO)
        rows.append({"status": "current" if current else STALE, "reviewer": reviewer, "role": role,
                     "decision": decision, "reviewed_at": safe(r.get("reviewed_at"), STAMP),
                     "checks": ["%s review" % role, "decision %s" % decision,
                                "bound to %s content hash" % ("this" if current else "an older"),
                                "signature %s (not verified on this page)" % ("recorded" if "signature" in r else "none")]})
        if current and decision == "approve":
            counted.add((reviewer, role))
    rows.sort(key=lambda x: (x["status"] != "current", x["reviewed_at"], x["reviewer"], x["role"]))
    owed = review_blockers(reviewed_view(finding), [r for r in raw if isinstance(r, dict)]) if raw else ["no_review_receipt"]
    return {"counted_reviews": len(counted), "receipts": rows, "owed": [safe(b) for b in owed]}

def assemble(store, finding, as_of):
    errors = []
    library, receipts, objects, check, log = read_store(store, errors)
    digest = reviewed_hash(finding)
    entries = [e for e in log if e.get("id") == finding["id"] and e.get("action") in ("publish", "correct")]
    last = entries[-1] if entries else None
    drifted = "unknown" if last is None else "no" if last.get("content_sha256") == digest else "yes"
    head = {"id": finding["id"], "classification": finding.get("classification") if finding.get("classification")
            in CLASSIFICATIONS else "unknown", "publication_state": safe(finding.get("state")),
            "published_at": safe(finding.get("published_at"), STAMP), "content_sha256": digest, "drifted": drifted,
            "publication_record": "not-recorded" if last is None else "%s at %s" % (last["action"], safe(last.get("at"), STAMP))}
    sources, chain = [], []
    for n, src in enumerate(s for s in (finding.get("sources") or []) if isinstance(s, dict)):
        label, sha = "S%d" % (n + 1), src.get("sha256")
        state, problem = rehash(sha, objects, check)
        page = src.get("page") if type(src.get("page")) is int else safe(src.get("locator"))
        sources.append({"source": label, "sha256": sha if isinstance(sha, str) and HEX.fullmatch(sha) else "withheld",
                        "page": page, "rehash": state, **({"problem": problem} if problem else {})})
        lib, kept = library.get(sha), receipts.get(sha, [])
        chain.append({"source": label,
                      "received": {"state": "receipt", "received_at": safe(lib.get("received_at"), STAMP),
                                   "agency": safe(lib.get("agency_id"))} if lib else {"state": "no-receipt"},
                      "contact_role": "not-recorded",
                      "preservation": {"state": "receipt", "receipts": kept} if kept else {"state": "no-receipt"}})
    review = review_section(store, finding, digest, errors)
    missing = (["finding: drifted (content changed after its publication record)"] if drifted == "yes" else
               ["finding: no publication record"] if drifted == "unknown" else [])
    missing += ["sources: none recorded"] if not sources else []
    missing += ["source %s: re-hash %s" % (s["source"], s["rehash"]) for s in sources if s["rehash"] != "matches"]
    for c in chain:
        missing += ["receipt chain %s: %s no-receipt" % (c["source"], step) for step in ("received", "preservation")
                    if c[step]["state"] == "no-receipt"]
    if not review["counted_reviews"]:
        stale = any(r["status"] == STALE for r in review["receipts"])
        missing.append("review: " + ("only a " + STALE if stale else "no current approving review receipt"))
    elif review["owed"]:
        missing.append("review: owed " + ",".join(review["owed"]))
    verdict = {"state": "chain incomplete", "first_missing": missing[0], "missing": missing} if missing else \
        {"state": "chain complete", "first_missing": None, "missing": []}
    return {"schema": SCHEMA, "review_required": True, "finding_id": finding["id"], "as_of": as_of,
            "sections": {"finding": head, "sources": sources, "receipt_chain": chain, "review": review,
                         "establishes": list(ESTABLISH)},
            "verdict": verdict, "source_errors": sorted(errors)}

def render(doc):
    s, v = doc["sections"], doc["verdict"]
    line = v["state"] + ("" if v["first_missing"] is None else " — " + v["first_missing"])
    out = ["# Custody report — finding %s" % doc["finding_id"], "",
           "PRIVATE. Review required. Document identity, not document text. Not for publication.",
           "As of %s." % doc["as_of"], "", "**Verdict:** " + line, "", "## 1. The finding", ""]
    out += ["- %s: %s" % (k, s["finding"][k]) for k in sorted(s["finding"])]
    out += ["", "## 2. Its sources", ""]
    out += ["- %s: sha256 %s; page %s; re-hash %s%s" % (x["source"], x["sha256"], x["page"], x["rehash"],
            " (%s)" % x["problem"] if "problem" in x else "") for x in s["sources"]] or ["- none recorded: not established"]
    out += ["", "## 3. Receipt chain", ""]
    for c in s["receipt_chain"]:
        got, kept = c["received"], c["preservation"]
        out.append("- %s received: %s; contact role: %s" % (c["source"], "no-receipt" if got["state"] == "no-receipt" else
                   "at %s from agency %s" % (got["received_at"], got["agency"]), c["contact_role"]))
        out.append("- %s preservation: %s" % (c["source"], "no-receipt" if kept["state"] == "no-receipt" else "; ".join(
            "receipt %s at %s answering %s" % (r["receipt_id"], r["preserved_at"], r["request_id"]) for r in kept["receipts"])))
    out += [] if s["receipt_chain"] else ["- none recorded: not established"]
    out += ["", "## 4. Review", "", "Counted reviews (current, approving): %d" % s["review"]["counted_reviews"]]
    out += ["- %s: %s, %s, %s; checks: %s" % (r["status"], r["reviewer"], r["role"], r["reviewed_at"], "; ".join(r["checks"]))
            for r in s["review"]["receipts"]] or ["- no review receipt: not established"]
    out += ["- owed: " + (", ".join(s["review"]["owed"]) or "none")]
    out += ["", "## 5. What this does and does not establish", ""] + list(s["establishes"])
    out += ["", "## Missing links", ""] + (["- " + m for m in v["missing"]] or ["- none"])
    out += ["", "## Source errors", ""] + (["- " + e for e in doc["source_errors"]] or ["- none"])
    return "\n".join(out) + "\n"

def _linked(path):
    return any(p.is_symlink() for p in (path, *path.parents))

def _inside(path, root):
    return path == root or root in path.parents

def checked_out(raw, store):
    out = Path(raw)
    if not out.is_absolute() or os.path.normpath(raw) != raw:
        raise Refused("absolute_normalized_out_required")
    if os.path.lexists(out):
        raise Refused("out_exists")
    if _linked(out.parent) or not out.parent.is_dir():
        raise Refused("out_parent_unavailable")
    if _inside(out, Path(os.path.realpath(REPO))) or _inside(out, Path(os.path.realpath(store))):
        raise Refused("out_location_refused")
    return out

def write(out, files):
    out.mkdir(mode=0o700)
    os.chmod(out, 0o700)
    for name, text in files:
        fd = os.open(out / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(text.encode("utf-8"))

def main(argv=None):
    parser = _Parser(prog="custody_report.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    for flag, options in (("command", {"choices": ("build", "status")}), ("--store", {"required": True}), ("--finding", {}),
                          ("--out", {})):
        parser.add_argument(flag, **options)
    try:
        args = parser.parse_args(argv)
        store = Path(args.store)
        if not store.is_absolute() or store.is_symlink() or not store.is_dir():
            raise Refused("store_unavailable")
        found, as_of = published(store), datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if args.command == "status":
            if args.finding or args.out:
                raise Refused("usage_invalid")
            states = []
            for fid in sorted(found):
                v = assemble(store, found[fid][0], as_of)["verdict"] if len(found[fid]) == 1 else \
                    {"state": "chain incomplete", "first_missing": "finding: published twice"}
                states.append(v["state"])
                print("custody | %s | %s" % (fid, v["state"] + ("" if v["first_missing"] is None else " — " + v["first_missing"])))
            print("custody | published=%d complete=%d | private, review required" % (len(states), states.count("chain complete")))
            return int(states.count("chain complete") != len(states))
        if not args.finding or not ID.fullmatch(args.finding) or not args.out:
            raise Refused("usage_invalid")
        out = checked_out(args.out, store)
        if len(found.get(args.finding, [])) != 1:
            raise Refused("finding_not_published_once")
        doc = assemble(store, found[args.finding][0], as_of)
        body = json.dumps(doc, sort_keys=True, indent=2, ensure_ascii=True) + "\n"
        page = render(doc)
        if privacy.categories(page + body) or privacy.legal_claims(page):
            raise Refused("generated_text_failed_privacy_check")
        write(out, ((MD_NAME, page), (JSON_NAME, body)))
    except (Refused, OSError, ValueError, TypeError, KeyError, sqlite3.Error) as error:
        print("custody refused: %s" % (error if isinstance(error, Refused) else "store_unreadable"))
        return 2
    print("custody | %s | %s" % (doc["finding_id"], doc["verdict"]["state"]))
    return int(doc["verdict"]["state"] != "chain complete" or bool(doc["source_errors"]))

if __name__ == "__main__":
    raise SystemExit(main())
