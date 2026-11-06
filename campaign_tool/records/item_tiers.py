"""Tier and state for each asked item of a records request, and a logs-and-audits summary (REC-ITEM-1).

Reads the private per-asked-item rating (response-items.json from REC-AUTO-8) and the
records playbook (REC-PLAY-1). For every asked item it adds the playbook tier
(audit and search/use logs first, then policy, retention, access and sharing, then
contracts) and a plain state word, orders the items tier 1 first, and summarises
per request whether logs and audits were asked for and what came back.

Pure and read-only: no network, no model, no ledger or file write, nothing sent or
published. Nothing here is specific to one campaign or agency; the tiers are the
playbook's data. A state is a machine reading and needs human review.
"""
import argparse
import json
import re
import sys
from pathlib import Path

from . import playbook as _pb

SCHEMA = "records-item-tiers-v1"
SCHEMA_IN = "records-response-items-v1"
MAX_BYTES = 64 * 1024 * 1024
STATES = {
    "answered": "produced",
    "withheld": "withheld",
    "exemption_cited": "exemption cited",
    "no_records_stated": "no records stated",
    "missing": "not addressed",
    "unclear": "unclear",
}
NOT_RATED = "not rated"
DONE = frozenset({"produced"})
_AUDIT = re.compile(r"\baudits?\b|\baudit trail\b", re.I)
_LOG = re.compile(r"\blogs?\b|\bsearch history\b|\baccess records?\b|\bquery records?\b|\bqueries\b", re.I)
LIMITS = (
    "Tier comes from the playbook keywords; state comes from the REC-AUTO-8 machine rating. Review required.",
    "A state is not a legal finding and not proof that records exist or that a duty was missed. Private; not for publication.",
)


def state_of(rating):
    return STATES.get(rating, NOT_RATED)


def _kinds(text):
    kinds = []
    if _AUDIT.search(text or ""):
        kinds.append("audits")
    if _LOG.search(text or ""):
        kinds.append("logs")
    return kinds


def _part(asks):
    if not asks:
        return {"asked": 0, "status": "not asked", "states": {}}
    states = {}
    for ask in asks:
        states[ask["state"]] = states.get(ask["state"], 0) + 1
    if all(a["state"] in DONE for a in asks):
        status = "produced"
    elif any(a["state"] == NOT_RATED for a in asks):
        status = "not rated"
    else:
        status = "open"
    return {"asked": len(asks), "status": status, "states": dict(sorted(states.items()))}


def logs_audits(asks):
    """Summary of the tier-1 asks of one request: logs and audits, each with a status."""
    tier1 = [a for a in asks if a["tier"] == 1]
    out: dict = {"tier1_asked": len(tier1)}
    for kind in ("logs", "audits"):
        out[kind] = _part([a for a in tier1 if kind in _kinds(a["text"])])
    unkinded = [a for a in tier1 if not _kinds(a["text"])]
    if unkinded:
        out["other_tier1"] = _part(unkinded)
    out["line"] = "logs: %s; audits: %s" % (out["logs"]["status"], out["audits"]["status"])
    return out


def tier_report(report, book):
    """report: parsed response-items.json (or the same shape); book: a validated playbook."""
    if not isinstance(report, dict) or report.get("schema") != SCHEMA_IN or not isinstance(report.get("requests"), list):
        raise _pb.PlaybookError("response_items_invalid", "schema")
    names = {t["id"]: t["name"] for t in book["tiers"]}
    requests = []
    for req in report["requests"]:
        asks = []
        for position, ask in enumerate(req.get("asks") or []):
            text = ask.get("text") or ""
            tier = _pb.tier_for(book, text)
            asks.append({"ask_id": ask.get("ask_id"), "text": text, "tier": tier,
                         "tier_name": names.get(tier, "untiered"), "rating": ask.get("rating"),
                         "state": state_of(ask.get("rating")), "evidence": ask.get("evidence") or [],
                         "flags": list(ask.get("flags") or []), "_pos": position})
        # tier 1 first, then 2, then 3, untiered last; document order inside a tier
        asks.sort(key=lambda a: (a["tier"] if a["tier"] is not None else len(names) + 1, a["_pos"]))
        for a in asks:
            del a["_pos"]
        requests.append({"agency_key": req.get("agency_key"), "request_id": req.get("request_id"),
                         "flags": list(req.get("flags") or []), "asks": asks, "logs_and_audits": logs_audits(asks)})
    return {"schema": SCHEMA, "run_id": report.get("run_id"), "playbook": book["id"], "review_required": True,
            "requests": requests, "limits": list(LIMITS)}


def _load_report(path):
    target = Path(path)
    try:
        with open(target, "rb") as handle:
            raw = handle.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise _pb.PlaybookError("too_large", target.name)
        return json.loads(raw.decode("utf-8"))
    except OSError:
        raise _pb.PlaybookError("unreadable", target.name) from None
    except (UnicodeDecodeError, ValueError):
        raise _pb.PlaybookError("not_json", target.name) from None


def main():
    parser = argparse.ArgumentParser(prog="records items", description="Tier and state per asked item, from response-items.json.")
    parser.add_argument("file", help="response-items.json from a records run")
    parser.add_argument("--playbook", help="playbook JSON; default: the shipped records-alpr-v1")
    parser.add_argument("--json", action="store_true", help="print the full report as JSON")
    args = parser.parse_args()
    try:
        result = tier_report(_load_report(args.file), _pb.load(args.playbook))
    except _pb.PlaybookError as error:
        print("records items: refused " + error.code + (" (" + error.detail + ")" if error.detail else ""), file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    for req in result["requests"]:
        print("%s %s  [%s]" % (req["agency_key"], req["request_id"], req["logs_and_audits"]["line"]))
        for ask in req["asks"]:
            print("  tier %s  %-17s %s" % (ask["tier"] or "-", ask["state"], ask["text"][:90]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
