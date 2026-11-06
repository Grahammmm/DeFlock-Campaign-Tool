"""First-request drafter: playbook + agency policy register -> one DRAFT letter (never sent).

The letter follows the request playbook: an exit ramp first, then the lead items in tier order
(native log export for one short named window with its fields and a pre-offered plate/case-number
redaction; completed audits under the agency's own policy section; sharing lists and MOUs), then
optionally the remaining items. Only citations the playbook marks ``verified-in-package`` reach the
body; the others are listed as omitted. The banned-phrase lint runs on the finished text. Nothing
here names an agency, county or campaign: all of that is data passed in. It sends, posts and
publishes nothing, reads no credentials and writes no file; ``queue`` only appends a pending draft
to the private approval journal, where it still needs G's signed approval.
"""
import argparse
import hashlib
import json
import re
import sys
from datetime import date
from pathlib import Path

from . import agency_policies, draft_templates, playbook as pb

KIND = "first-request"
MAX_WINDOW_DAYS = 92  # "one named window of at most one quarter"
EDIT = draft_templates.EDIT
FOOTER = "---\nDraft only. Not legal advice. Review and G's signed approval are required; sending stays manual."
_TEXT = re.compile(r"[^\x00-\x1f\x7f]{1,200}")
_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_WINDOW_SLOT = re.compile(r"\[one named window[^\]]*\]")
_POLICY_SLOT = "[policy_section]"
_EXIT_DATE = "[date]"


class FirstRequestError(Exception):
    def __init__(self, code, detail=""):
        super().__init__(code + (": " + detail if detail else ""))
        self.code = code
        self.detail = detail


def _need(condition, code, detail=""):
    if not condition:
        raise FirstRequestError(code, detail)


def _day(value, name):
    try:
        _need(isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value), "window_invalid", name)
        return date.fromisoformat(value)
    except ValueError:
        raise FirstRequestError("window_invalid", name) from None


def check_window(start, end):
    """Both ends ISO dates, start <= end, at most one quarter. Returns (start_date, end_date)."""
    first, last = _day(start, "start"), _day(end, "end")
    _need(first <= last, "window_invalid", "end before start")
    _need((last - first).days + 1 <= MAX_WINDOW_DAYS, "window_too_long", "more than one quarter")
    return first, last


_AUDIT_WORD = re.compile(r"(?<![a-z0-9])audit(?:s|ed|ing)?(?![a-z0-9])", re.I)
_AUDIT_LOG_NOUN = re.compile(
    r"(?<![a-z0-9])audit(?:ing)?[\s-]+(?:logs?|trails?|records?|entr(?:y|ies)|data|files?|events?)(?![a-z0-9])",
    re.I,
)


def _speaks_of_audits(quote):
    """True when the quote requires actual audits, not just audit-log/trail retention."""
    return bool(_AUDIT_WORD.search(_AUDIT_LOG_NOUN.sub(" ", quote)))


def audit_policy_row(book, rows, agency):
    """The agency's first policy row whose quote itself speaks of audits, else None.

    A tier-1 hit alone (an access or search-log row) is not an audit-policy section.
    """
    for row in agency_policies.sections(rows, agency):
        quote = row["quote"]
        if pb.tier_for(book, quote) == 1 and _speaks_of_audits(quote):
            return row
    return None


def _policy_label(row):
    return f"{row['policy_id']}, section {row['section']}"


def _text(value, name):
    _need(isinstance(value, str) and _TEXT.fullmatch(value.strip()), "field_invalid", name)
    return value.strip()


def _item_lines(number, item, book, window, row, used, omitted):
    ask = _WINDOW_SLOT.sub(window, item["ask"])
    ask = ask.replace(_POLICY_SLOT, _policy_label(row) if row else EDIT.format("agency policy section"))
    lines = [f"{number}. {ask}"]
    if item.get("fields"):
        lines.append("   Fields requested: " + "; ".join(item["fields"]) + ".")
    if item.get("redaction_offer"):
        lines.append("   " + item["redaction_offer"])
    if item.get("if_none"):
        lines.append("   " + item["if_none"])
    if item["key"] == "completed_audits" and row:
        lines.append(f"   The policy states (page {row['page']} of the policy document): \"{row['quote']}\"")
    for ref in item.get("cite", []):
        cite = book["citations"][ref]
        if cite["status"] == "verified-in-package":
            used.append(ref)
            lines.append(f"   Reference: {cite['ref']} ({cite['gives']}).")
        else:
            omitted.append(ref)
    return lines


def draft(book, rows, *, agency, agency_name, start, end, basis, requester=None, all_items=False):
    """Build the first-request draft. Returns a dict; raises FirstRequestError on bad input."""
    pb.validate(book)
    _need(isinstance(agency, str) and _SLUG.match(agency), "agency_invalid")
    name, basis = _text(agency_name, "agency_name"), _text(basis, "basis")
    requester = _text(requester, "requester") if requester else None
    first, last = check_window(start, end)
    window = f"{first.isoformat()} through {last.isoformat()}"
    plan = book["first_request"]
    items = [i for i in plan["items"] if all_items or i.get("lead")]
    _need(items, "no_items")
    row = audit_policy_row(book, rows, agency)
    used, omitted = [], []

    out = [draft_templates.DRAFT_BANNER, "", f"To: Records custodian, {name}",
           f"Subject: Public records request for access and audit records, {window}", "",
           f"This is a request for existing records under {basis}.", "",
           plan["exit_ramp"].replace(_EXIT_DATE, first.isoformat()), "", "Records requested:"]
    for number, item in enumerate(items, 1):
        out.extend(_item_lines(number, item, book, window, row, used, omitted))
    out += ["", "Please state, for each item, whether records are produced (naming the files), withheld (with the basis),"
            " not located (naming the systems searched) or not searched, and confirm the date range you produced.", ""]
    out += list(plan.get("closing", []))
    out += ["", requester or EDIT.format("requester name and contact"), "", FOOTER]
    body = "\n".join(out) + "\n"

    lint = pb.lint(book, body)
    reasons = []
    if lint["hits"]:
        reasons.append("banned phrase")
    if row is None:
        reasons.append("no agency audit policy row")
    if "[EDIT ME:" in body:
        reasons.append("unfilled placeholder")
    evidence = {
        "playbook": {"id": book["id"], "version": book["version"]}, "agency": agency, "window": [first.isoformat(), last.isoformat()],
        "items": [i["key"] for i in items], "policy_row": None if row is None else {
            k: row[k] for k in ("policy_id", "section", "page", "doc_sha256")},
        "citations_used": used, "citations_omitted": omitted, "lint": lint["status"], "needs_edit": reasons,
    }
    return {"kind": KIND, "body": body, "evidence": evidence, "lint": lint, "needs_edit": reasons,
            "status": "needs edit" if reasons else "ready for approval"}


def queue(root, result, *, agency, request_id, run_id):
    """Append the draft to the private approval journal (pending; sends nothing). Duplicate-safe."""
    from . import approval_queue
    ev = result["evidence"]
    item_id = hashlib.sha256(json.dumps([agency, request_id, ev["window"], ev["items"], ev["playbook"]],
                                        sort_keys=True).encode()).hexdigest()
    return approval_queue.add_draft(root, item_id=item_id, kind=KIND, agency_id=agency, request_id=request_id,
                                    body=result["body"], evidence=ev, source_run_id=run_id)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="records first-request",
                                 description="Draft a first records request from the playbook. Sends nothing.")
    ap.add_argument("--agency", required=True, help="agency slug used in the policy register")
    ap.add_argument("--agency-name", required=True)
    ap.add_argument("--policies", required=True, help="agency-policies.csv")
    ap.add_argument("--window-start", required=True, help="YYYY-MM-DD")
    ap.add_argument("--window-end", required=True, help="YYYY-MM-DD (window at most one quarter)")
    ap.add_argument("--basis", required=True, help="the public-records law to cite, as you want it written")
    ap.add_argument("--requester")
    ap.add_argument("--playbook")
    ap.add_argument("--all-items", action="store_true", help="also ask the non-lead items (policy versions .. contracts)")
    ap.add_argument("--queue", metavar="PROJECT", help="append the draft to this project's approval journal")
    ap.add_argument("--request-id", help="request id for --queue (default: first-<window start>)")
    ap.add_argument("--run-id", default="first-request")
    args = ap.parse_args(argv)
    try:
        book = pb.load(args.playbook)
        rows, problems = agency_policies.load(args.policies)
        _need(not problems, "policies_invalid", f"{len(problems)} problems")
        result = draft(book, rows, agency=args.agency, agency_name=args.agency_name, start=args.window_start,
                       end=args.window_end, basis=args.basis, requester=args.requester, all_items=args.all_items)
        if args.queue:
            queue(args.queue, result, agency=args.agency, run_id=args.run_id,
                  request_id=args.request_id or "first-" + args.window_start)
    except (FirstRequestError, pb.PlaybookError) as error:
        print("records first-request: refused " + error.code + (" (" + error.detail + ")" if error.detail else ""),
              file=sys.stderr)
        return 2
    except Exception as error:  # approval journal refusals carry a content-free code
        code = getattr(error, "code", None)
        if code is None:
            raise
        print("records first-request: refused " + str(code), file=sys.stderr)
        return 2
    sys.stdout.write(result["body"])
    for hit in result["lint"]["hits"]:
        print("  " + hit["id"] + " at " + str(hit["start"]) + ": " + hit["reason"], file=sys.stderr)
    print("status: " + result["status"] + (" (" + "; ".join(result["needs_edit"]) + ")" if result["needs_edit"] else ""),
          file=sys.stderr)
    return 0 if not result["needs_edit"] else 1


if __name__ == "__main__":
    sys.exit(main())
