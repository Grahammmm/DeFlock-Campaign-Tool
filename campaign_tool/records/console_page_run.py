"""GET /run?id=RUN_ID: one finished run explained stage by stage in plain words (DFCON17-03).

Read-only. The run is looked up by id inside the validated history of DFCON17-02 (``console_runs_model``); the id
is a key, never a path. Shows only whitelisted report fields (state, code sentence, counts, needs-G items,
new-data counts, notify outcome), never paths, log lines or raw metadata. A valid report is machine-valid
evidence, not semantic acceptance of the run's outputs.
"""
import urllib.parse
from datetime import datetime, timezone

from . import console_codes, console_layout, console_page_runs as runs, console_runs_model as model, run_report
from .outcomes.reading import esc

PATHS = ("/run",)
NAV = None  # reached from the Run history page's Details links
SEVERITY = {"info": "for information", "action": "needs action", "blocking": "blocking"}
COUNTS = ("input", "output", "failed", "skipped")
NEW_DATA = ("documents", "pages", "extracted_units", "portal_releases", "request_status_changes")


def parse(query):
    """The run id of a query that is exactly one ``id`` in the engine's grammar, else None (400)."""
    try:
        pairs = urllib.parse.parse_qsl(query, keep_blank_values=True, strict_parsing=bool(query), max_num_fields=4)
    except ValueError:
        return None
    if len(pairs) != 1 or pairs[0][0] != "id" or not model.RUN_ID_RE.fullmatch(pairs[0][1]):
        return None
    return pairs[0][1]


def sentence(code):
    """The code's sentence with the code itself beside it; an unmapped code is the code and 'no description yet'."""
    text = console_codes.explain(code)
    return "%s (%s)" % (text, code) if code in console_codes.CODES else text


def _pacific(stamp):
    when = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).astimezone(runs.ZONE)
    return when.strftime("%Y-%m-%d %H:%M:%S %Z")


def _notify(stage):
    """Said as 'sent' only when the notify stage says so; pending, skipped and failed are never worded as sent."""
    if stage["status"] == "ok":
        return "The summary notification was sent."
    if stage["error_code"] == "NOTIFY_PENDING":
        return "Not sent yet: " + sentence(stage["error_code"])
    return "Not sent: " + (sentence(stage["error_code"]) if stage["error_code"] else "the notify stage did not complete.")


def _gaps(stage):
    """Delta/adoption: the coded adoption gaps recorded in the stage (DF1117L-06), read through run_report only.

    The disposition is the recorded bool, never current policy or stage status; counts are numbers, sentences escaped.
    """
    rows = run_report.adoption_gap_breakdown(stage)
    if rows is None:
        return "<p>breakdown not recorded by this engine</p>"
    if not rows:
        return "<p>No coded adoption gaps.</p>"
    rows = sorted(rows, key=lambda r: (-r["count"], r["code"], not r["accepted"]))
    return ("<table><caption>Adoption gaps</caption><thead><tr><th>Gap</th><th>Messages</th><th>Disposition</th></tr></thead>"
            "<tbody>%s</tbody></table>" % "".join("<tr><td>%s</td><td>%d</td><td>%s</td></tr>" % (
                esc(sentence(r["code"])), r["count"], "accepted" if r["accepted"] else "blocking") for r in rows))


def _stage(number, stage, report):
    body = ["<h2>%d. %s: %s</h2>" % (number, esc(stage["name"]), esc(stage["status"]))]
    body.append("<p>%s</p>" % (esc(sentence(stage["error_code"])) if stage["error_code"] else "No problem reported."))
    body.append("<p>Counts: %s.</p>" % ", ".join("%s %d" % (key, stage["counts"][key]) for key in COUNTS))
    items = [i for i in report["needs_g"] if i["stage"] == stage["name"]]
    if items:
        body.append("<p>Needs attention:</p><ul>%s</ul>" % "".join("<li>%s: %s, %d</li>" % (
            esc(sentence(i["code"])), esc(SEVERITY.get(i["severity"], i["severity"])), i["count"]) for i in items))
    if stage["name"] in run_report.GAP_STAGES:
        body.append(_gaps(stage))
    if stage["name"] == "notify":
        body.append('<p role="status">%s</p>' % esc(_notify(stage)))
    return "<section>%s</section>" % "".join(body)


def _link(label, report):
    if report is None:
        return esc(label) + ": none"
    return '<a href="/run?id=%s">%s</a>' % (esc(urllib.parse.quote(report["run_id"])), esc(label))


def render(reports, index):
    report = reports[index]
    trigger = report.get("trigger") or (report["mode"] if report["mode"] in ("arrival", "manual") else "unknown")
    body = ["<h1>Run details</h1><p>Read-only. A finished run is machine-valid evidence, not semantic acceptance.</p>",
            "<p>Run %s. Started %s, finished %s (Pacific time). Trigger: %s. Overall: %s. Items to review: %d.</p>" % (
                esc(report["run_id"]), esc(_pacific(report["started_at"])), esc(_pacific(report["finished_at"])),
                esc(trigger), esc(report["overall_status"]), report["review_required_count"])]
    body += [_stage(n, s, report) for n, s in enumerate(report["stages"], 1)]
    body.append("<p>New data this run: %s.</p>" % ", ".join(
        "%s %d" % (k.replace("_", " "), report["new_data"][k]) for k in NEW_DATA))
    older = reports[index + 1] if index + 1 < len(reports) else None
    newer = reports[index - 1] if index else None
    body.append('<nav aria-label="Neighbouring runs">%s %s <a href="/runs">All runs</a></nav>' % (
        _link("Previous (older) run", older), _link("Next (newer) run", newer)))
    return console_layout.shell("Run details", "\n".join(body), active=runs.NAV[0])


def unavailable(history):
    if history["state"] == "not_configured":
        note = "not configured on this console."
    else:
        note = "unavailable (%s): %s." % (esc(history["code"]), esc(runs.UNAVAILABLE.get(history["code"], "it could not be read")))
    return console_layout.shell("Run details", '<h1>Run details</h1><p role="status" class="diag">Run history: %s</p>' % note,
                                active=runs.NAV[0])


def serve(handler):
    ident = parse(urllib.parse.urlsplit(handler.path).query)
    if ident is None:
        handler._fail(400)
        return True
    history = model.load_history(handler.options.get("run_report_path"))
    if history["state"] != "ok":
        handler._send(200, unavailable(history))
        return True
    index = next((i for i, r in enumerate(history["reports"]) if r["run_id"] == ident), None)
    if index is None:
        handler._fail(404)
    else:
        handler._send(200, render(history["reports"], index))
    return True
