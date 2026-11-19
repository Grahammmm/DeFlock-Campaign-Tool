"""GET /runs: the newest completed pipeline runs, their trigger and stage outcomes (DFCON17-02).

Read-only. Shows codes and counts from validated run reports, never paths, record ids or report text. A run
report is machine-valid evidence, not semantic acceptance of its outputs."""
import urllib.parse
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from . import console_layout, console_runs_model as model
from .outcomes.reading import esc

PATHS = ("/runs",)
NAV = ("Run history", 10)
SHOWN = 30
ZONE = ZoneInfo("America/Los_Angeles")
HEADERS = ("Started (Pacific)", "Trigger", "State", "Duration", "Stages", "Error codes", "Run")
CHIP = ' style="display:inline-block;border:1px solid #888;border-radius:.5rem;padding:0 .4rem;margin:.1rem"'
UNAVAILABLE = {"reports_missing": "the reports directory does not exist", "reports_unreadable": "the reports directory cannot be read",
               "history_limit": "there are more than %d archived reports" % model.FILE_LIMIT}


def _duration(seconds):
    seconds = int(round(seconds))
    return "%dm %ds" % divmod(seconds, 60) if seconds >= 60 else "%ds" % seconds


def _row(report):
    started = datetime.strptime(report["started_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).astimezone(ZONE)
    trigger = report.get("trigger") or (report["mode"] if report["mode"] in ("arrival", "manual") else "unknown")
    stages = report["stages"]
    chips = " ".join("<span%s>%s: %s</span>" % (CHIP, esc(s["name"]), esc(s["status"])) for s in stages)
    codes = "<br>".join("%s %s" % (esc(s["name"]), esc(s["error_code"])) for s in stages if s["error_code"])
    cells = (started.strftime("%Y-%m-%d %H:%M %Z"), trigger, report["overall_status"], _duration(report["duration_seconds"]))
    link = '<a href="/run?id=%s">Details</a>' % esc(urllib.parse.quote(report["run_id"]))
    return "<tr>%s</tr>" % "".join("<td>%s</td>" % c for c in (*map(esc, cells), chips, codes or "none", link))


def render(history):
    body = ["<h1>Run history</h1><p>Read-only. Newest first; a finished run is machine-valid evidence, not semantic acceptance.</p>"]
    state = history["state"]
    if state == "not_configured":
        body.append('<p role="status">Run history: not configured on this console.</p>')
    elif state != "ok":
        body.append('<p role="status" class="diag">Run history: unavailable (%s): %s.</p>' % (
            esc(history["code"]), esc(UNAVAILABLE.get(history["code"], "it could not be read"))))
    else:
        reports = history["reports"]
        if reports:
            rows = "".join(_row(r) for r in reports[:SHOWN])
            body.append('<div class="scroll" role="region" tabindex="0" aria-label="Run history"><table style="min-width:60rem">'
                        "<caption>Newest %d completed runs</caption><thead><tr>%s</tr></thead><tbody>%s</tbody></table></div>" % (
                            SHOWN, "".join('<th scope="col">%s</th>' % esc(h) for h in HEADERS), rows))
        elif not history["skipped"]:
            body.append("<p>Run history: no completed runs yet.</p>")
        body.append('<p role="status">Older valid runs not listed: %d. Reports not shown: %d (invalid or finished in the future).</p>' % (
            max(0, len(reports) - SHOWN), history["skipped"]))
    return console_layout.shell("Run history", "\n".join(body), active=NAV[0])


def serve(handler):
    if handler.path != "/runs":  # a query is not part of this page
        return False
    handler._send(200, render(model.load_history(handler.options.get("run_report_path"))))
    return True
