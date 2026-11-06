"""Admin console Screen C: documents and findings, per agency (REC-CON-2).

Read-only view over one run's private outputs: received items (REC-AUTO-3), text state, the release-link
register (REC-LINK-1), per-asked-item dispositions (REC-AUTO-8 via REC-ITEM-1) and gap/misuse proposals
(REC-AUTO-9). Each source fails alone and is named as unavailable; nothing is created, fetched, sent or
published. Agencies, requests and the playbook are data read from those files; nothing here is specific to one
campaign. Every finding is labelled review required and not publishable. Tier order follows the playbook
(audit and search/use logs first, then policy, retention, access and sharing, then contracts).

  records documents --project P --run-id R [--agency ID] [--now ISO] [--html OUT] [--json]
Served by `records panel` at GET /documents[?agency=ID].
"""
import argparse
import json
import os
import re
import stat
import sys
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

from . import item_tiers
from . import playbook as _pb
from .outcomes import release_links as _rl
from .outcomes import received_items as _ri
from .outcomes.reading import BANNER, esc

RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
AGENCY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
MAX_BYTES = 64 * 1024 * 1024
LABEL = "Review required, not publishable until G approves a public version."
NO_AGENCY = "(not linked to an agency)"
STATE_WORDS = {"text_reviewed": "text, checked", "ocr_unreviewed": "text from OCR, not visually checked",
               "awaiting_ocr": "waiting for OCR", "could_not_be_read": "could not be read",
               "waiting": "waiting", "unknown_state": "state not recognised"}


def _read(path, schema):
    """Bounded no-symlink read of a JSON file. Returns {'state':'ok','doc':..} or {'state':'unavailable','code':..}."""
    try:
        info = os.lstat(path)
        if not stat.S_ISREG(info.st_mode):
            return {"state": "unavailable", "code": "not_regular_file"}
        if info.st_size > MAX_BYTES:
            return {"state": "unavailable", "code": "too_large"}
        doc = json.loads(Path(path).read_bytes())
    except FileNotFoundError:
        return {"state": "unavailable", "code": "missing"}
    except (OSError, ValueError, RecursionError):
        return {"state": "unavailable", "code": "unreadable"}
    if type(doc) is not dict or doc.get("schema") != schema:
        return {"state": "unavailable", "code": "schema_invalid"}
    return {"state": "ok", "doc": doc}


def _aliases(path):
    """Optional operator-staged identifiers; reject the whole file on any invalid row."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("not_regular")
            data = stream.read(512 * 1024 + 1)
        if len(data) > 512 * 1024:
            raise ValueError("too_large")
        lines = data.decode("utf-8").replace("\r\n", "\n").split("\n")
        if lines[-1] == "":
            lines.pop()
        if not lines or lines[0] != "agency_key\talias" or len(lines) > 501:
            raise ValueError("header_or_rows")
        mapping, keys = {}, set()
        for line in lines[1:]:
            key, alias = line.split("\t")
            if not AGENCY.fullmatch(key) or not alias or len(alias) > 128 or not alias.isprintable():
                raise ValueError("field")
            if alias in mapping and mapping[alias] != key:
                raise ValueError("duplicate")
            mapping[alias] = key
            keys.add(key)
        if keys.intersection(mapping):
            raise ValueError("alias_is_key")
        return {"state": "ok", "map": mapping}
    except FileNotFoundError:
        return {"state": "absent"}
    except (OSError, ValueError):
        return {"state": "unavailable", "code": "aliases_invalid"}


def _key(model, ident):
    aliases = model.get("aliases", {})
    mapping = aliases.get("map") if isinstance(aliases, dict) and aliases.get("state") == "ok" else None
    return mapping.get(ident, ident) if isinstance(mapping, dict) else ident


def _dicts(obj, key):
    """Field absent, or a list whose members are all dicts. An explicit null or a non-list is malformed."""
    if key not in obj:
        return True
    value = obj[key]
    return type(value) is list and all(type(v) is dict for v in value)


def _opt_str(value):
    return value is None or type(value) is str


def _shape_ok(key, doc):
    """True when the fields this screen consumes have the expected types; a bad source degrades alone."""
    if key == "received":
        return _dicts(doc, "items") and all(
            all(_opt_str(i.get(k)) for k in ("agency_id", "item_id", "received_at", "original_sha256", "kind",
                                              "request_id", "ocr_review_status", "extraction_status"))
            and (i.get("findings") is None or type(i.get("findings")) is list)
            for i in doc.get("items", []))
    if key == "links":
        if not _dicts(doc, "links"):
            return False
        for link in doc.get("links", []):
            ids = link.get("agency_ids")
            if ids is not None and not (type(ids) is list and all(type(a) is str for a in ids)):
                return False
            if not _dicts(link, "sources") or not (link.get("expiry") is None or type(link.get("expiry")) is dict):
                return False
            if not all(_opt_str(s.get("received_at")) for s in link.get("sources", [])):
                return False
        return True
    if key == "proposals":
        if not _dicts(doc, "agencies"):
            return False
        return all(_opt_str(b.get("agency_id")) and _dicts(b, "requests")
                   and all(_dicts(r, "proposals") and all(
                       _opt_str(p.get("confidence")) and _opt_str(p.get("proposal_id"))
                       for p in r.get("proposals", [])) for r in b.get("requests", []))
                   for b in doc.get("agencies", []))
    if key == "response":
        return _dicts(doc, "requests") and all(_dicts(r, "asks") for r in doc.get("requests", []))
    if key == "textstate":
        return _dicts(doc, "records") and all(
            "sha256" not in r or (type(r["sha256"]) is str and type(r.get("state")) is str)
            for r in doc.get("records", []))
    return True


def _tiers_ok(report):
    if type(report) is not dict or type(report.get("requests")) is not list:
        return False
    for r in report["requests"]:
        if type(r) is not dict or type(r.get("asks")) is not list or type(r.get("logs_and_audits")) is not dict:
            return False
        if not _opt_str(r.get("agency_key")) or not _opt_str(r.get("request_id")):
            return False
        if type(r["logs_and_audits"].get("line")) is not str:
            return False
        for a in r["asks"]:
            if type(a) is not dict or not all(k in a for k in ("ask_id", "state", "tier", "tier_name", "text")):
                return False
            if type(a["ask_id"]) is not str or type(a["state"]) is not str or not _opt_str(a["tier_name"]):
                return False
            if not (a.get("evidence") is None or type(a.get("evidence")) is list):
                return False
            if any(type(ev) is dict and not _opt_str(ev.get("item_id")) for ev in a.get("evidence") or []):
                return False
            if type(a["text"]) is not str or not (a.get("evidence") is None or type(a.get("evidence")) is list):
                return False
    return True


def _check_run(project, run_id):
    if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id) or run_id in (".", ".."):
        raise ValueError("run_id_invalid")
    return Path(os.path.abspath(project)) / "run-outputs" / run_id


def _pages(project, items):
    """Page count per original hash from the ledger's current extraction; fails soft to {}."""
    try:
        from .run_stages import snapshot_ro
        processing = Path(os.path.abspath(project)) / "processing-v1"
        out = {}
        with snapshot_ro(processing / "ledger.sqlite") as db:
            import sqlite3
            db.row_factory = sqlite3.Row
            for item in items:
                digest = item.get("original_sha256")
                if item.get("extraction_status") == "current" and _ri.is_hash(digest):
                    units = _ri._current_units(db, processing, digest)[0]
                    out[digest] = len({u["page"] for u in units if isinstance(u.get("page"), int)})
        return out
    except Exception:  # any ledger problem means "pages not recorded", never a failed screen
        return {}


def load(project, run_id, *, playbook_path=None, pages=True):
    run_dir = _check_run(project, run_id)
    outcomes = run_dir / "outcomes"
    model = {"run_id": run_id, "aliases": _aliases(Path(project) / "agency-aliases.tsv"),
             "received": _read(outcomes / "received-items.json", _ri.SCHEMA),
             "links": _read(outcomes / _rl.ARTIFACT, _rl.SCHEMA),
             "proposals": _read(outcomes / "gap-proposals.json", "records-gap-proposals-v1"),
             "response": _read(outcomes / "response-items.json", item_tiers.SCHEMA_IN),
             "textstate": _read(run_dir / "text-state.json", "records-text-state-v1"),
             "tiers": {"state": "unavailable", "code": "no_response_items"}, "pages": {}}
    for key in ("received", "links", "proposals", "response", "textstate"):
        doc = model[key].get("doc")
        if doc is not None and doc.get("run_id") != run_id:
            model[key] = {"state": "unavailable", "code": "run_mismatch"}
    for key in ("received", "links", "proposals", "response", "textstate"):
        if model[key]["state"] == "ok" and not _shape_ok(key, model[key]["doc"]):
            model[key] = {"state": "unavailable", "code": "shape_invalid"}
    if model["response"]["state"] == "ok":
        try:
            report = item_tiers.tier_report(model["response"]["doc"], _pb.load(playbook_path))
            if not _tiers_ok(report):
                raise ValueError("shape")
            model["tiers"] = {"state": "ok", "doc": report}
        except _pb.PlaybookError as error:
            model["tiers"] = {"state": "unavailable", "code": error.code}
        except (TypeError, ValueError, KeyError, AttributeError):
            model["tiers"] = {"state": "unavailable", "code": "shape_invalid"}
    if pages and model["received"]["state"] == "ok":
        model["pages"] = _pages(project, model["received"]["doc"].get("items") or [])
    return model


def _items(model):
    return [i for i in (model["received"].get("doc") or {}).get("items", []) if type(i) is dict]


def agencies(model):
    ids = {i.get("agency_id") for i in _items(model)}
    ids |= {a for l in (model["links"].get("doc") or {}).get("links", []) for a in l.get("agency_ids") or []}
    ids |= {a.get("agency_id") for a in (model["proposals"].get("doc") or {}).get("agencies", [])}
    ids |= {r.get("agency_key") for r in (model["tiers"].get("doc") or {}).get("requests", [])}
    return sorted({_key(model, i) for i in ids if isinstance(i, str)})


def _status(link, now):
    try:
        return _rl.status(link, now)
    except (ValueError, KeyError, TypeError):
        return "unknown", None


def view(model, agency, now):
    """One agency's slice (agency None = items and links not linked to any agency). Pure."""
    state = model["textstate"].get("doc")
    text_of = {r["sha256"]: r["state"] for r in (state or {}).get("records", []) if type(r) is dict and "sha256" in r}
    answers = {}
    for ask_req in (model["tiers"].get("doc") or {}).get("requests", []):
        if _key(model, ask_req.get("agency_key")) == agency:
            for ask in ask_req["asks"]:
                if ask["state"] not in item_tiers.DONE:  # withheld, exempt, no records, unclear: not an answer
                    continue
                for ev in ask.get("evidence") or []:
                    answers.setdefault(ev.get("item_id") if type(ev) is dict else None, []).append(ask["ask_id"])
    items = []
    for item in sorted((i for i in _items(model) if _key(model, i.get("agency_id")) == agency),
                       key=lambda i: (i.get("received_at") or "9", i.get("item_id") or "")):
        digest = item.get("original_sha256")
        items.append({"item_id": item.get("item_id"), "received_at": item.get("received_at"), "kind": item.get("kind"),
                      "request_id": item.get("request_id"), "pages": model["pages"].get(digest),
                      "text_state": text_of.get(digest) if state else None,
                      "ocr_review_status": item.get("ocr_review_status"),
                      "answers_asks": sorted(set(answers.get(item.get("item_id"), []))),
                      "findings": len(item.get("findings") or [])})
    links = []
    for link in (model["links"].get("doc") or {}).get("links", []):
        ids = link.get("agency_ids") or []
        if (agency in {_key(model, ident) for ident in ids}) if agency is not None else not ids:
            status, days = _status(link, now)
            links.append({"link_id": link.get("link_id"), "host": link.get("host"), "platform": link.get("platform"),
                          "url_redacted": link.get("url_redacted"),
                          "first_seen": min((s.get("received_at") or "" for s in link.get("sources", [])), default="") or None,
                          "expiry": (link.get("expiry") or {}).get("date"), "expiry_state": link.get("expiry_state"),
                          "status": status, "days": days, "fetched": False})
    dispositions = []
    for req in (model["tiers"].get("doc") or {}).get("requests", []):
        if _key(model, req.get("agency_key")) == agency:
            dispositions.append({"request_id": req.get("request_id"), "logs_and_audits": req["logs_and_audits"]["line"],
                                 "asks": [{"ask_id": a["ask_id"], "tier": a["tier"], "tier_name": a["tier_name"],
                                           "state": a["state"], "text": a["text"]} for a in req["asks"]]})
    proposals = []
    for block in (model["proposals"].get("doc") or {}).get("agencies", []):
        if _key(model, block.get("agency_id")) == agency:
            for req in block.get("requests", []):
                for p in req.get("proposals", []):
                    proposals.append({k: p.get(k) for k in ("proposal_id", "type", "request_id", "quote", "page",
                                                            "policy_section", "rule_ref", "confidence", "question",
                                                            "review_bucket", "review_reasons")}
                                     | {"review_state": "review required" if p.get("review_required") else "unknown",
                                        "publishable": False})
    rank = {"high": 0, "medium": 1, "low": 2}
    proposals.sort(key=lambda p: (rank.get(p["confidence"], 3), p["proposal_id"] or ""))
    return {"agency_id": agency, "items": items, "release_links": links, "dispositions": dispositions,
            "findings": proposals, "label": LABEL}


def sources(model):
    return {k: ({"state": "ok"} if model[k]["state"] == "ok" else dict(model[k]))
            for k in ("received", "links", "proposals", "response", "textstate", "tiers", "aliases")
            if model.get(k, {}).get("state", "absent") != "absent"}


# ---- rendering ---------------------------------------------------------------------------------------

def _table(headers, rows, caption):
    if not rows:
        return "<p>None reported in this source.</p>"
    out = ["<table><caption>%s</caption><thead><tr>%s</tr></thead><tbody>" % (
        esc(caption), "".join('<th scope="col">%s</th>' % esc(h) for h in headers))]
    for row in rows:
        out.append("<tr>" + "".join("<td>%s</td>" % esc("" if c is None else c) for c in row) + "</tr>")
    return "".join(out) + "</tbody></table>"


def render_agency(v):
    name = v["agency_id"] if v["agency_id"] is not None else NO_AGENCY
    out = ['<section aria-label="%s"><h2>%s</h2>' % (esc(name), esc(name)), '<p class="notice">%s</p>' % esc(v["label"])]
    out.append("<h3>Received items</h3>" + _table(
        ("Received", "Item", "Kind", "Request", "Pages", "Text state", "Answers asked items", "Findings"),
        [(i["received_at"], i["item_id"], i["kind"], i["request_id"],
          i["pages"] if i["pages"] is not None else "not recorded",
          STATE_WORDS.get(i["text_state"], i["text_state"] or "not recorded"),
          ", ".join(i["answers_asks"]) or "none matched", i["findings"]) for i in v["items"]], "Received items"))
    out.append("<h3>Release links (listed, not fetched)</h3>" + _table(
        ("Host", "Platform", "First seen", "Expiry", "Status"),
        [(l["host"], l["platform"], l["first_seen"], l["expiry"] or l["expiry_state"],
          l["status"] + (" (%dd)" % l["days"] if l["days"] is not None else "")) for l in v["release_links"]], "Release links"))
    out.append("<h3>Asked items and dispositions (tier 1 first)</h3>")
    for d in v["dispositions"]:
        out.append("<p>Request %s: %s</p>" % (esc(d["request_id"]), esc(d["logs_and_audits"])))
        out.append(_table(("Tier", "Item", "Disposition", "Asked"),
                          [("%s %s" % (a["tier"] or "-", a["tier_name"]), a["ask_id"], a["state"], a["text"][:200])
                           for a in d["asks"]], "Asked items"))
    if not v["dispositions"]:
        out.append("<p>None reported in this source.</p>")
    out.append("<h3>Findings and gap proposals</h3>" + _table(
        ("Confidence", "Type", "Request", "Page", "Quote", "Policy section", "Review state", "Question"),
        [(p["confidence"], p["type"], p["request_id"], p["page"], p["quote"],
          json.dumps(p["policy_section"], sort_keys=True) if p["policy_section"] else "unavailable",
          p["review_state"] + (" (list)" if p["review_bucket"] == "review_list" else "") + ", not publishable",
          p["question"]) for p in v["findings"]], "Proposals; a lead is a reason to ask, not evidence"))
    return "".join(out) + "</section>"


def render(model, now, agency=...):
    """agency=... means all agencies (plus unlinked material); a string or None selects one."""
    chosen = (agencies(model) + [None]) if agency is ... else [agency]
    notes = ["<p class=\"diag\" role=\"status\">%s: unavailable (%s).</p>" % (esc(k), esc(s.get("code")))
             for k, s in sources(model).items() if s["state"] != "ok"]
    body = [render_agency(view(model, a, now)) for a in chosen]
    head = ['<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="robots" content="noindex,nofollow">'
            "<title>Documents and findings</title></head><body>", '<p class="notice">%s</p>' % esc(BANNER),
            "<h1>Documents and findings</h1>", "<p>Run %s. %s Nothing is fetched, sent or published.</p>" % (
                esc(model["run_id"]), esc(LABEL))]
    return "\n".join(head + notes + body + ["</body></html>"]) + "\n"


# ---- panel seam and CLI ------------------------------------------------------------------------------

def serve(handler):
    """Called by admin_panel's GET handler. Returns True when it answered /documents."""
    parts = urllib.parse.urlsplit(handler.path)
    if parts.path != "/documents":
        return False
    query = urllib.parse.parse_qs(parts.query)
    agency = query.get("agency", [...])[0]
    if agency is not ... and not AGENCY.fullmatch(agency):
        handler._fail(400)
        return True
    options = handler.server.options
    try:
        model = load(options["project"], options["run_id"])
    except ValueError:
        handler._fail(503)
        return True
    handler._send(200, render(model, datetime.now(timezone.utc), agency))
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(prog="records documents", description="Screen C: documents and findings per agency. Read only.")
    parser.add_argument("--project", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--agency")
    parser.add_argument("--now", help="ISO-8601 clock override")
    parser.add_argument("--html", help="write the page to this new file")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        now = datetime.fromisoformat(args.now.replace("Z", "+00:00")) if args.now else datetime.now(timezone.utc)
        if now.tzinfo is None:
            raise ValueError("now_invalid")
        if args.agency is not None and not AGENCY.fullmatch(args.agency):
            raise ValueError("agency_invalid")
        model = load(args.project, args.run_id)
    except ValueError as error:
        print("records documents: refused " + (str(error) or "now_invalid"), file=sys.stderr)
        return 2
    chosen = [args.agency] if args.agency else agencies(model) + [None]
    if args.html:
        text = render(model, now, args.agency if args.agency else ...)
        try:
            with open(args.html, "x", encoding="utf-8") as stream:
                stream.write(text)
        except OSError:
            print("records documents: refused html_output", file=sys.stderr)
            return 2
        return 0
    result = {"run_id": args.run_id, "sources": sources(model), "review_required": True, "publishable": False,
              "agencies": [view(model, a, now) for a in chosen]}
    if args.json:
        print(json.dumps(result, sort_keys=True))
        return 0
    for s, state in result["sources"].items():
        if state["state"] != "ok":
            print("source %s unavailable (%s)" % (s, state.get("code")))
    for v in result["agencies"]:
        print("%s: %d items, %d release links, %d requests rated, %d proposals  [%s]" % (
            v["agency_id"] or NO_AGENCY, len(v["items"]), len(v["release_links"]), len(v["dispositions"]),
            len(v["findings"]), "review required, not publishable"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
