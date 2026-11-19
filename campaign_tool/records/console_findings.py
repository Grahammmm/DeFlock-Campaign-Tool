"""Console page for the human finding review (DF2007-06): ``records panel --analysis-root ROOT``, ``GET /findings/review``.

Lists each finding DF2007-05 currently holds as FINDING_UNREVIEWED: agency, topic, claim, every cited fact (quote, record
prefix and page), the F4 verdict (a model check, not a human review) and the exact ``finding-review-v1`` text a reviewer signs.
``POST /findings/review/approve`` (a pasted detached SSH signature) and ``/findings/review/reject`` (a private reason) only
append a ``finding_review`` entry to the console decision journal; the next ``records analysis-tick --from-journal`` applies it
through ``finding_review`` (see ``finding_review_apply``). Success here means "recorded for the next analysis tick", never
"approved". The analysis root is read-only to the console: nothing in it is created, locked, repaired or written, and the page
is served with forms only from a pinned export with a decision journal. No signing happens here.
"""
import hmac
import os
import re
import stat
import urllib.parse
from pathlib import Path

from . import analysis_config as ac, console_layout, finding_review as fr, finding_review_apply as fa
from .outcomes.reading import esc

ROUTES = {"/findings/review/approve": "finding_approve", "/findings/review/reject": "finding_reject"}
FIELDS = {"finding_approve": {"csrf_token", "agency", "finding_id", "finding_sha256", "signature"},
          "finding_reject": {"csrf_token", "agency", "finding_id", "finding_sha256", "reason"}}
HEX64 = re.compile(r"[0-9a-f]{64}")
REFUSED = "Refused: a finding is reviewed only from a pinned export with a decision journal and an analysis root. Nothing was saved or decided."
NOTE = ("The checks below are a model's (F4); only your signature or rejection is the human review. Approve records your pasted "
        "signature and Reject your reason; either is recorded here and applied at the next analysis tick. Nothing is sent or published.")


def check_root(root, *write_locations):
    """The real analysis-root path, or ValueError(analysis_root_invalid). Never creates or changes anything."""
    try:
        real = os.path.realpath(root)
        if not (isinstance(root, str) and os.path.isabs(root) and real == root and os.path.isdir(real)):
            raise ValueError
        fr._admit(real)
        inner = os.path.join(real, ".analysis")
        if os.path.lexists(inner) and not stat.S_ISDIR(os.lstat(inner).st_mode):
            raise ValueError
        for other in write_locations:  # every enabled console write location: none may be, hold or sit inside the root
            if other is not None:
                other = os.path.realpath(other)
                if other == real or other.startswith(real + os.sep) or real.startswith(other + os.sep):
                    raise ValueError
    except (ValueError, OSError, fr.FindingReviewError, TypeError):
        raise ValueError("analysis_root_invalid") from None
    return real


def reviewable(row):
    """True for a row the review page lists: held FINDING_UNREVIEWED with the exact payload and hash to sign."""
    return row.get("hold_code") == "FINDING_UNREVIEWED" and bool(row.get("payload")) and bool(row.get("finding_sha256"))


def load_all(root):
    """-> {state: ok|unavailable, code, rows (every finding, any review state, unfiltered), broken agencies}. Read-only; never a silent empty list."""
    try:
        if not os.path.isdir(root):  # a removed or unreadable root must not look like a root with no findings (glob swallows it)
            raise OSError
        if os.path.lexists(os.path.join(root, "agencies")):
            os.listdir(os.path.join(root, "agencies"))
        names = sorted(p.parent.name for p in Path(root).glob("agencies/*/review-config.json"))
        rows, broken = [], 0
        for slug in names:
            try:
                rows += fr.list_findings(root, agency=slug)
            except (fr.FindingReviewError, OSError, ValueError, KeyError, TypeError):
                broken += 1  # one agency's inputs cannot be read: counted and said, never dropped silently
    except (OSError, ValueError, TypeError):
        return {"state": "unavailable", "code": "finding_inputs_unavailable"}
    if broken and broken == len(names):
        return {"state": "unavailable", "code": "finding_inputs_unavailable"}
    return {"state": "ok", "rows": rows, "broken": broken}


def load(root):
    """-> {state: ok|unavailable, code, rows (FINDING_UNREVIEWED), counts, broken agencies}. Read-only; never a silent empty list."""
    doc = load_all(root)
    if doc["state"] != "ok":
        return doc
    rows = doc["rows"]
    waiting = [r for r in rows if reviewable(r)]
    count = lambda state: sum(1 for r in rows if r["review_state"] == state)  # noqa: E731
    return {"state": "ok", "rows": waiting, "rejected": count("rejected"), "deferred": count("deferred"), "broken": doc["broken"],
            "other": len(rows) - len(waiting) - count("rejected") - count("deferred")}


def pending(handler, root):
    """{(target_id, finding sha256)} journaled for this root and not yet receipted; raises ValueError when unreadable."""
    from . import console_journal as cj
    try:
        done, mine = fa.read_receipts(root), fa.root_binding(root)
        return {(e["target_id"], e["revision_seen"]) for e in cj.read(handler.server.decisions_dir)
                if e.get("kind") == cj.FINDING_KIND and e["source_project_id"] == handler.pin.source_project_id
                and cj.finding_value(e)["analysis_root_sha256"] == mine and e["entry_id"] not in done}
    except (fa.ApplyError, cj.JournalError, OSError, KeyError, ValueError):
        raise ValueError("finding_pending_unreadable") from None


def _fact(fact):
    sha = fact.get("source_sha256") or ""
    return "<li>%s<br><span class=\"muted\">record %s, page %s (%s)</span></li>" % (
        esc(fact.get("quote") or "(no quote on file)"), esc(sha[:12] or "unknown"), esc(fact.get("page") or "unknown"), esc(fact["fact_id"]))


def _forms(token, row):
    hidden = "".join('<input type="hidden" name="%s" value="%s">' % (k, esc(v)) for k, v in
                     (("csrf_token", token), ("agency", row["agency"]), ("finding_id", row["finding_id"]),
                      ("finding_sha256", row["finding_sha256"])))
    key = "%s-%s" % (row["agency"], row["finding_sha256"][:12])
    return ('<form method="post" action="/findings/review/approve">%s<label for="sig-%s">Pasted signature of the text above</label>'
            '<textarea id="sig-%s" name="signature" rows="8" required></textarea><button type="submit">Record approval</button></form>'
            '<form method="post" action="/findings/review/reject">%s<label for="why-%s">Reason for rejecting (private)</label>'
            '<textarea id="why-%s" name="reason" rows="3" maxlength="%d" required></textarea><button type="submit">Record rejection</button></form>'
            % (hidden, esc(key), esc(key), hidden, esc(key), esc(key), fr.MAX_REASON))


def _item(row, token, waiting):
    out = ['<section class="draft"><h2>%s: %s</h2><dl><dt>Topic</dt><dd>%s</dd><dt>Claim</dt><dd>%s</dd>'
           '<dt>F4 verdict (model check, not a human review)</dt><dd>%s</dd><dt>Finding hash</dt><dd>%s</dd></dl>' % (
               esc(row["agency"]), esc(row["finding_id"]), esc(row["topic"]), esc(row["claim"]), esc(row["f4_verdict"]),
               esc(row["finding_sha256"][:12]))]
    out.append("<h3>Cited facts</h3><ul>%s</ul>" % "".join(_fact(f) for f in row["facts"]))
    out.append("<h3>Exact text to sign (finding-review-v1)</h3><pre>%s</pre>" % esc(row["payload"]))
    if waiting:
        out.append('<p role="status">Decision recorded; it is applied at the next analysis tick.</p>')
    elif token is not None:
        out.append(_forms(token, row))
    return "".join(out) + "</section>"


def render_body(doc, token, held=frozenset()):
    """The page fragment. token None: the read-only list (no form, button, token or submit URL)."""
    out = ["<h1>Finding review</h1>"]
    if doc is None:
        out.append('<p class="diag" role="status">Finding review needs configuration: start the console with --analysis-root ROOT.</p>')
        return "".join(out)
    if doc["state"] != "ok":
        out.append(console_layout.sources_banner([], ["Finding review: unavailable (%s). Nothing is listed; this is not an empty list." % doc["code"]]))
        return "".join(out)
    out.append('<p role="status">%d finding%s waiting for a human review. %d rejected, %d deferred, %d held for another reason.</p>' % (
        len(doc["rows"]), "" if len(doc["rows"]) == 1 else "s", doc["rejected"], doc["deferred"], doc["other"]))
    out.append("<p>%s</p>" % esc(NOTE))
    if token is None:
        out.append('<p class="diag" role="status">Read-only: a finding can be decided only from the exported console with a decision journal.</p>')
    if doc["deferred"]:
        out.append('<p class="diag" role="status">%d finding%s deferred: the review journal or trust anchor is unavailable.</p>' % (
            doc["deferred"], "" if doc["deferred"] == 1 else "s"))
    if doc["broken"]:
        out.append('<p class="diag" role="status">%d agency review%s could not be read (finding_inputs_unavailable); its findings are not listed.</p>' % (
            doc["broken"], "" if doc["broken"] == 1 else "s"))
    if not doc["rows"]:
        out.append("<p>No finding is waiting for a human review.</p>")
    out += [_item(r, token, (r["agency"] + ":" + r["finding_id"], r["finding_sha256"]) in held) for r in doc["rows"]]
    return "".join(out)


def render_page(doc, token, held=frozenset()):
    return console_layout.shell("Finding review", render_body(doc, token, held), active="Finding review")


def serve(handler):
    path = urllib.parse.urlsplit(handler.path)
    if path.path != "/findings/review":
        return False
    if path.query:
        handler._fail(400)
        return True
    root = getattr(handler.server, "analysis_root", None)
    if root is None:
        handler._send(200, render_page(None, None))
        return True
    form_ok = handler.pin is not None and handler.server.decisions_dir is not None
    doc, held, token = load(root), frozenset(), handler.csrf_token() if form_ok else None
    if form_ok:
        try:
            held = pending(handler, root)
        except ValueError:
            doc, token = {"state": "unavailable", "code": "finding_pending_unreadable"}, None
    handler._send(200, render_page(doc, token, held))
    return True


def post(handler):
    """POST /findings/review/approve|reject: journal one decision for the finding the person saw. True when the path is ours."""
    action = ROUTES.get(handler.path)
    if action is None:
        return False
    from . import console_journal, console_origin
    if not console_origin.request_ok(handler, post=True):
        handler._fail(403)
        return True
    root = getattr(handler.server, "analysis_root", None)
    if handler.pin is None or handler.server.decisions_dir is None or root is None:
        handler._fail(503, REFUSED)
        return True
    fields = handler._body()
    if isinstance(fields, int):
        handler._fail(fields)
        return True
    if set(fields) != FIELDS[action]:
        handler._fail(400)
        return True
    if not hmac.compare_digest(fields["csrf_token"].encode("utf-8"), handler.server.csrf_token.encode("ascii")):
        handler._fail(403)
        return True
    agency, finding_id, digest = fields["agency"], fields["finding_id"], fields["finding_sha256"]
    key = "signature" if action == "finding_approve" else "reason"
    text = fields[key].replace("\r\n", "\n")
    limit = 8192 if key == "signature" else fr.MAX_REASON
    if not (ac.SLUG.fullmatch(agency) and 0 < len(finding_id) <= 200 and finding_id.isprintable() and HEX64.fullmatch(digest)
            and text.strip() and len(text) <= limit and (key == "signature" or text.isprintable())):
        handler._fail(400)
        return True
    doc = load(root)  # the finding is read again now: the page the person saw must still be current
    if doc["state"] != "ok":
        handler._fail(503)
        return True
    row = next((r for r in doc["rows"] if (r["agency"], r["finding_id"]) == (agency, finding_id)), None)
    if row is None or row["finding_sha256"] != digest:
        handler._fail(409)
        return True
    try:
        if (agency + ":" + finding_id, digest) in pending(handler, root):
            handler._fail(409)
            return True
    except ValueError:
        handler._fail(503)
        return True
    value = console_journal._canon({"analysis_root_sha256": fa.root_binding(root), key: text})
    if console_journal.finding(handler, action, agency + ":" + finding_id, digest, value):
        handler._send(303, "", location="/findings/review")
    return True
