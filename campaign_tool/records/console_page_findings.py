"""GET /findings: every finding the analysis lane holds, in every review state (DFCON17-10).

Read-only companion of ``/findings/review`` (which lists only findings waiting for a human). Reads the console's own
``--analysis-root`` through ``console_findings.load_all``: nothing is locked, created, written, approved or rejected, and no
journal entry is appended. Counts per review state and hold come first, then one row per finding. The signing payload, the
finding hash, reasons, quotes and any path are never shown. An approved finding is a completed human review, never clearance
to publish; every other row says it is review required and not publishable.
"""
import re
import urllib.parse

from . import analysis_config as ac, console_findings as cf, console_layout
from .outcomes.reading import esc

PATHS = ("/findings",)
NAV = ("Findings", 50)
CLAIM_CHARS, MAX_FACTS = 200, 20
NOT_PUBLISHABLE = "review required; not publishable"
NOT_CONFIGURED = "findings: not configured on this console"
STATES = {"unreviewed": "waiting for a human review", "approved": "approved (human review complete)",
          "rejected": "rejected by a human", "deferred": "review deferred (journal or trust anchor unavailable)"}
HOLDS = {None: "no hold", "FINDING_UNREVIEWED": "waiting for a human review", "FINDING_REJECTED": "rejected by a human",
         "FINDING_REVIEW_DEFERRED": "review deferred (journal or trust anchor unavailable)",
         "EVIDENCE_FINDING_MISSING": "the finding row is missing from the analysis"}
_HEX64, _HEX64_FOUND = re.compile(r"[0-9a-f]{64}"), re.compile(r"(?i)[0-9a-f]{64}")
_PATH = re.compile(r"(?<![\w:])/(?:[\w.~@+-]+/)+[\w.~@+-]*|\b[A-Za-z]:\\\S*|(?<![\w:/])~/\S*")
_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,59}")
_PAGE = re.compile(r"[1-9][0-9]{0,5}")


def _text(value, limit=None):
    """Escaped free text: cut to limit characters, then any 64-hex hash or file path in it is replaced, never shown."""
    if not isinstance(value, str) or not value.strip():
        return "unknown"
    cut = value[:limit] + (" …" if limit and len(value) > limit else "")
    cut = _PATH.sub("[path withheld]", _HEX64_FOUND.sub("[hash withheld]", cut))
    return esc(cut)


def _state(row):
    value = row.get("review_state")
    return value if isinstance(value, str) and value in STATES else "unknown"


def _hold(row):
    """(hold key for counting, plain words)."""
    code = row.get("hold_code")
    if code is None or (isinstance(code, str) and code in HOLDS):
        return code, HOLDS[code]
    if isinstance(code, str) and _CODE.fullmatch(code):
        return code, "held for another reason (%s)" % code
    return "unknown", "held for an unknown reason"


def _records(row):
    facts = row.get("facts")
    facts = facts if isinstance(facts, list) else []
    out = []
    for fact in facts[:MAX_FACTS]:
        fact = fact if isinstance(fact, dict) else {}
        sha, page = fact.get("source_sha256"), fact.get("page")
        page = str(page) if isinstance(page, (str, int)) and not isinstance(page, bool) else ""
        out.append("record %s, page %s" % (sha[:12] if isinstance(sha, str) and _HEX64.fullmatch(sha) else "unknown",
                                           page if _PAGE.fullmatch(page) else "unknown"))
    if len(facts) > MAX_FACTS:
        out.append("and %d more" % (len(facts) - MAX_FACTS))
    return out


def _counts(title, counts, plain):
    items = "".join("<li>%s: %d</li>" % (esc(plain(key)), n) for key, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0] or "")))
    return "<h2>%s</h2><ul>%s</ul>" % (esc(title), items)


def _row(row):
    agency = row.get("agency") if isinstance(row.get("agency"), str) and ac.SLUG.fullmatch(row["agency"]) else None
    state = _state(row)
    label = (cf.reviewable(row) and '<a href="/findings/review">review it</a>') or "not on the review page"
    notice = "review complete; publication is a separate approval" if state == "approved" else NOT_PUBLISHABLE
    records = _records(row)
    return ("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
        esc(agency or "unknown"), _text(row.get("finding_id"), 80), _text(row.get("topic"), 120), _text(row.get("claim"), CLAIM_CHARS),
        esc(STATES.get(state, "unknown")), esc(_hold(row)[1]), "<br>".join(esc(r) for r in records) or "none cited",
        "%s<br>%s" % (esc(notice), label)))


def render_body(doc):
    out = ["<h1>All findings</h1>"]
    if doc is None:
        return "".join(out) + '<p class="diag" role="status">%s.</p>' % esc(NOT_CONFIGURED)
    if doc["state"] != "ok":
        return "".join(out) + console_layout.sources_banner(
            [], ["Findings: unavailable (%s). Nothing is listed; this is not an empty list." % doc["code"]])
    rows = doc["rows"]
    out.append('<p role="status">%d finding%s in every review state. Read-only: this page cannot approve, reject or publish anything. '
               'Only a finding waiting for a human review is decided, on the finding review page.</p>' % (len(rows), "" if len(rows) == 1 else "s"))
    if doc["broken"]:
        out.append('<p class="diag" role="status">%d agency review%s could not be read (finding_inputs_unavailable); its findings are not listed.</p>' % (
            doc["broken"], "" if doc["broken"] == 1 else "s"))
    if not rows:
        return "".join(out) + "<p>The analysis root holds no findings.</p>"
    states, holds = {}, {}
    for row in rows:
        states[_state(row)] = states.get(_state(row), 0) + 1
        holds[_hold(row)[0]] = holds.get(_hold(row)[0], 0) + 1
    out.append(_counts("By review state", states, lambda k: STATES.get(k, "unknown")))
    out.append(_counts("By hold", holds, lambda k: HOLDS.get(k) or ("held for an unknown reason" if k == "unknown" else
                                                                    "held for another reason (%s)" % k)))
    out.append("<table><caption>Every finding</caption><thead><tr>%s</tr></thead><tbody>" % "".join(
        '<th scope="col">%s</th>' % h for h in ("Agency", "Finding", "Topic", "Claim (first 200 characters)", "Review state", "Hold",
                                                  "Cited records", "Status")))
    out += [_row(r) for r in rows]
    return "".join(out) + "</tbody></table>"


def serve(handler):
    if urllib.parse.urlsplit(handler.path).query:
        handler._fail(400)
        return True
    root = getattr(handler.server, "analysis_root", None)
    try:
        doc = None if root is None else cf.load_all(root)
        body = render_body(doc)
    except (TypeError, KeyError, AttributeError, ValueError):  # a row of an unexpected shape: said, never an empty list
        body = render_body({"state": "unavailable", "code": "finding_rows_invalid"})
    handler._send(200, console_layout.shell("All findings", body, active="Findings"))
    return True
