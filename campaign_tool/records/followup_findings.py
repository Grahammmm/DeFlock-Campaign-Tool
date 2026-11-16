"""Read-only source of reviewed contradiction findings for the follow-up engine (FLK-REC-2).

A produced record may contradict an agency's policy. Only a finding whose exact current text carries a valid signed
`finding-review-v1` approval (finding_review, DF2007-05) is ever routed. Nothing here approves, signs, writes, sends or
publishes; a model verdict, an old packet or a review-state string is not approval.

Convention: the signed topic of the finding is exactly `contradiction` (the signature binds topic, claim, fact ids and
rule ids, not a separate kind field). Each cited fact must be a checked, unflagged fact (the existing evidence evaluator
holds the finding otherwise) with a source hash, a positive page and a verbatim quote, and every cited source must join
one received item of the same agency and one single request in the run's received-items report.
"""
import collections
import hashlib
import json
import re

from . import finding_review as fr, followup_state as fs
from .outcomes import received_items

KIND = "contradiction"
UNAVAILABLE = "followup_findings_unavailable"
_HEX = re.compile(r"[0-9a-f]{64}")


class FindingsUnavailable(ValueError):
    """The supplied analysis root could not be read; the message is one fixed, content-free code."""

    def __init__(self):
        super().__init__(UNAVAILABLE)


def evidence_digest(facts):
    raw = json.dumps(facts, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def locator(descriptor):
    return {"kind": descriptor["kind"], "finding_id": descriptor["finding_id"],
            "finding_sha256": descriptor["finding_sha256"], "evidence_sha256": descriptor["evidence_sha256"],
            "fact_ids": [f["fact_id"] for f in descriptor["facts"]]}


def _facts(row):
    """-> the ordered, checked cited facts of a finding row, or None when any citation is incomplete."""
    facts, seen = [], set()
    for fact in row.get("facts") or []:
        page = fact.get("page")
        ok = (isinstance(fact.get("fact_id"), str) and fact["fact_id"] and fact["fact_id"] not in seen
              and isinstance(fact.get("source_sha256"), str) and _HEX.fullmatch(fact["source_sha256"])
              and isinstance(page, str) and re.fullmatch(r"[1-9][0-9]*", page)
              and isinstance(fact.get("quote"), str) and fact["quote"].strip())
        if not ok:
            return None
        seen.add(fact["fact_id"])
        facts.append({"fact_id": fact["fact_id"], "source_sha256": fact["source_sha256"], "page": int(page),
                      "quote": fact["quote"]})
    return facts or None


def _descriptor(agency_key, row, received):
    base = {"kind": KIND, "agency_key": agency_key, "request_id": None, "finding_id": row["finding_id"],
            "finding_sha256": row["finding_sha256"], "evidence_sha256": None, "facts": [], "item_ids": [], "valid": False}
    facts = _facts(row)
    if facts is None:
        return base
    items = []
    for sha in dict.fromkeys(f["source_sha256"] for f in facts):
        match = [i for i in received if i.get("original_sha256") == sha and i.get("agency_id") == agency_key]
        if len(match) != 1 or not isinstance(match[0].get("request_id"), str) or not match[0]["request_id"].strip():
            return base
        items.append(match[0])
    requests = {i["request_id"] for i in items}
    if len(requests) != 1:
        return base
    return dict(base, request_id=requests.pop(), evidence_sha256=evidence_digest(facts), facts=facts,
                item_ids=sorted({i["item_id"] for i in items if isinstance(i.get("item_id"), str)}), valid=True)


def load_reviewed_contradictions(project, run_id, analysis_root):
    """-> descriptors of every currently approved, unheld contradiction finding (valid or not), read fresh each call.

    `valid` False means the finding is approved but its citation or request mapping is not usable; such a descriptor
    carries no facts and makes the route manual. Unreviewed, rejected and signature-refused findings are never listed.
    """
    try:
        root = fr._admit(analysis_root)
        project = received_items._safe_dir(project)
        try:
            report = fs._read(project / "run-outputs" / run_id / "outcomes" / "received-items.json")
        except FileNotFoundError:
            report = {}
        received = [i for i in report.get("items", []) if isinstance(i, dict)]
        slugs = collections.defaultdict(list)
        for path in sorted(root.glob("agencies/*/review-config.json")):
            slug = path.parent.name
            try:
                agency = json.loads((root / "profiles" / (slug + ".json")).read_text(encoding="utf-8")).get("records_agency_id")
            except (OSError, ValueError, AttributeError):
                continue
            if isinstance(agency, str) and agency:
                slugs[agency].append(slug)
        if any(len(v) > 1 for v in slugs.values()):
            raise FindingsUnavailable()
        out = []
        for agency_key, (slug,) in sorted(slugs.items()):
            try:
                rows = fr.list_findings(root, agency=slug)
            except fr.FindingReviewError:
                continue
            out += [_descriptor(agency_key, r, received) for r in rows
                    if r.get("topic") == KIND and r.get("review_state") == "approved" and r.get("hold_code") is None]
        return out
    except FindingsUnavailable:
        raise
    except (OSError, ValueError, KeyError, TypeError, AttributeError, fr.FindingReviewError):
        raise FindingsUnavailable() from None


def for_request(descriptors, agency_key, request_id):
    """The descriptors that apply to one request: its own, plus unmappable ones of the same agency."""
    return [d for d in descriptors or () if d["agency_key"] == agency_key and d["request_id"] in (request_id, None)]
