"""Cross-agency precedent lookup, private draft evidence only (REC-PLAY-4).

When agency A withholds, denies or does not answer an asked item (tier-1 logs or
audits), this shows which other agency in the register released that kind of record
and in what form. The register is data (``precedents.csv``): nothing here names an
agency, county or campaign.

G decision 8 was denied: a follow-up must NOT name the precedent agency. So each hit
has two parts. ``private_evidence`` names the agency, the form, the date and the
source, for the reviewer only. ``draft_phrase`` says "another county agency" and
contains no agency name; a register row whose text names any agency in the register
is refused at load, and the phrase is checked again before it is returned.

Pure and read-only: no network, no model, no write, nothing sent or published. A hit
is a machine reading and needs human review before any use.
"""
import argparse
import csv
import io
import json
import re
import sys
from datetime import date
from pathlib import Path

from . import item_tiers as _it
from . import playbook as _pb

SCHEMA = "records-precedents-v1"
HEADER = ["agency", "kind", "form", "released_on", "source_ref"]
MAX_BYTES = 5 * 1024 * 1024
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# asks in these states are the ones a precedent can help with
OPEN_STATES = frozenset({"withheld", "exemption cited", "no records stated", "not addressed", "unclear"})
KINDS = ("logs", "audits")
PHRASE = "Another county agency has produced an existing %s record in this form: %s."
LIMITS = (
    "Private draft evidence. Not for publication. The precedent agency is never named in a follow-up (G decision 8 denied).",
    "A precedent shows a release is possible; it is not a legal finding and does not decide any exemption. Review required.",
)


def _problem(line, field, reason):
    return {"line": line, "field": field, "reason": reason}


def _name_words(agency):
    return [w for w in re.split(r"[-_]+", agency) if w]


def names_agency(text, agencies):
    """True when ``text`` names any agency (its slug, or its slug words in order)."""
    low = (text or "").lower()
    for agency in agencies:
        words = _name_words(agency)
        if not words:
            continue
        if re.search(r"\b" + r"[\s_-]+".join(re.escape(w) for w in words) + r"\b", low):
            return True
    return False


def load(path):
    """Read a register. Returns (rows, problems); rows hold only valid entries."""
    p = Path(path)
    try:
        if p.is_symlink() or not p.is_file():
            return [], [_problem(0, "file", "not a regular file")]
        if p.stat().st_size > MAX_BYTES:
            return [], [_problem(0, "file", "too large")]
        text = p.read_bytes().decode("utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return [], [_problem(0, "file", "unreadable")]
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames != HEADER:
        return [], [_problem(0, "header", "expected " + ",".join(HEADER))]
    raw = list(reader)
    agencies = sorted({(r.get("agency") or "").strip() for r in raw if SLUG_RE.match((r.get("agency") or "").strip())})
    rows, problems, seen = [], [], set()
    for line, r in enumerate(raw, start=2):
        row = {c: (r.get(c) or "").strip() for c in HEADER}
        bad = [_problem(line, c, "required") for c in HEADER if not row[c]]
        if row["agency"] and not SLUG_RE.match(row["agency"]):
            bad.append(_problem(line, "agency", "not a lowercase slug"))
        if row["kind"] and row["kind"] not in KINDS:
            bad.append(_problem(line, "kind", "not one of " + ", ".join(KINDS)))
        if row["released_on"]:
            try:
                if not DATE_RE.match(row["released_on"]):
                    raise ValueError
                date.fromisoformat(row["released_on"])
            except ValueError:
                bad.append(_problem(line, "released_on", "not an ISO date YYYY-MM-DD"))
        if len(row["form"]) > 200:
            bad.append(_problem(line, "form", "longer than 200 characters"))
        if names_agency(row["form"], agencies):
            bad.append(_problem(line, "form", "names an agency; the form text goes into follow-ups"))
        key = (row["agency"], row["kind"], row["released_on"], row["form"])
        if not bad and key in seen:
            bad.append(_problem(line, "row", "duplicate"))
        seen.add(key)
        if bad:
            problems.extend(bad)
        else:
            rows.append(row)
    return rows, problems


def lookup(rows, agency, kind):
    """Other agencies that released ``kind``, newest first. Never the asking agency."""
    hits = [r for r in rows if r["kind"] == kind and r["agency"] != agency]
    return sorted(hits, key=lambda r: (r["released_on"], r["agency"]), reverse=True)


def precedents(tiers, rows):
    """tiers: an item_tiers report. Returns the private precedent evidence per open tier-1 ask."""
    if not isinstance(tiers, dict) or tiers.get("schema") != _it.SCHEMA or not isinstance(tiers.get("requests"), list):
        raise _pb.PlaybookError("item_tiers_invalid", "schema")
    agencies = sorted({r["agency"] for r in rows})
    requests = []
    for req in tiers["requests"]:
        agency, found = req.get("agency_key"), []
        for ask in req.get("asks") or []:
            if ask.get("tier") != 1 or ask.get("state") not in OPEN_STATES:
                continue
            for kind in _it._kinds(ask.get("text")):
                hits = lookup(rows, agency, kind)
                if not hits:
                    continue
                best = hits[0]
                phrase = PHRASE % (kind[:-1] if kind.endswith("s") else kind, best["form"].rstrip("."))
                if names_agency(phrase, agencies):  # belt and braces: load already refuses such a form
                    continue
                found.append({"ask_id": ask.get("ask_id"), "ask_text": ask.get("text"), "ask_state": ask.get("state"),
                              "kind": kind, "draft_phrase": phrase,
                              "private_evidence": [{k: h[k] for k in HEADER} for h in hits]})
        requests.append({"agency_key": agency, "request_id": req.get("request_id"), "precedents": found})
    return {"schema": SCHEMA, "run_id": tiers.get("run_id"), "private": True, "publishable": False,
            "review_required": True, "requests": requests, "limits": list(LIMITS)}


def main():
    parser = argparse.ArgumentParser(prog="records precedent", description="Private precedent evidence for open tier-1 asks.")
    parser.add_argument("file", help="response-items.json from a records run")
    parser.add_argument("--register", required=True, help="precedents.csv")
    parser.add_argument("--playbook", help="playbook JSON; default: the shipped records-alpr-v1")
    parser.add_argument("--json", action="store_true", help="print the full report as JSON")
    args = parser.parse_args()
    rows, problems = load(args.register)
    if problems:
        for p in problems:
            print("records precedent: register line %s %s: %s" % (p["line"], p["field"], p["reason"]), file=sys.stderr)
        return 2
    try:
        result = precedents(_it.tier_report(_it._load_report(args.file), _pb.load(args.playbook)), rows)
    except _pb.PlaybookError as error:
        print("records precedent: refused " + error.code + (" (" + error.detail + ")" if error.detail else ""), file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    print("PRIVATE: reviewer evidence only; not for publication; the draft phrase never names the agency.")
    for req in result["requests"]:
        print("%s %s: %d precedent(s)" % (req["agency_key"], req["request_id"], len(req["precedents"])))
        for hit in req["precedents"]:
            print("  ask %s [%s] %s" % (hit["ask_id"], hit["ask_state"], hit["ask_text"][:70]))
            print("    draft phrase: " + hit["draft_phrase"])
            for ev in hit["private_evidence"]:
                print("    evidence: %s released %s on %s (%s)" % (ev["agency"], ev["form"], ev["released_on"], ev["source_ref"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
