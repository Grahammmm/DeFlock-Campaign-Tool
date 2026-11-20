"""GET /coverage: processing coverage of the newest run and its trend over the newest valid runs (DF1117L-04).

Read-only. The reports come from the run-history reader (``console_runs_model``) and every count from
``run_coverage.coverage_counts``, the sole home of the coverage contract. Processing metadata only: not evidence
review, approval or proof that records were read. ``None`` (unknown) is never shown as zero or blank."""
import urllib.parse
from datetime import datetime, timezone

from . import console_layout, console_page_runs as runs, console_runs_model as model, run_coverage
from .outcomes.reading import esc

PATHS = ("/coverage",)
NAV = ("Coverage", 15)
COUNTS = (("adopted", "Adopted", "Records adopted in total, not counting those held with a recorded adoption gap."),
          ("adopted_this_run", "Adopted this run", "Records the adoption step produced in this run."),
          ("coded_gap", "Coded gaps", "Messages held back with a recorded adoption-gap code."),
          ("awaiting_ocr", "Awaiting OCR", "Units the extraction step reported as waiting for OCR."),
          ("portal_held", "Portal held", "Items the portal step deferred or held (dry run only, not found, blocked or pending)."))
UNKNOWN = "unknown"
STUB = "Stub run"
MARKS = {1: "\u2191 up", -1: "\u2193 down", 0: "= same"}


def _show(value):
    return UNKNOWN if value is None else str(value)


def _mark(new, old):
    """Change mark of a count against the older run's; none when either side is unknown."""
    return "" if new is None or old is None else " <small>%s</small>" % MARKS[(new > old) - (new < old)]


def _row(report, now, older):
    started = datetime.strptime(report["started_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).astimezone(runs.ZONE)
    mode = esc(now["mode"]) + (" <strong>%s</strong>" % STUB if now["stub"] else "")
    cells = [esc(started.strftime("%Y-%m-%d %H:%M %Z")), mode]
    cells += [_show(now[key]) + (_mark(now[key], older[key]) if older else "") for key, _, _ in COUNTS]
    cells.append('<a href="/run?id=%s">Details</a>' % esc(urllib.parse.quote(report["run_id"])))
    return "<tr>%s</tr>" % "".join("<td>%s</td>" % c for c in cells)


def _latest(now):
    items = "".join("<dt>%s: %s</dt><dd>%s</dd>" % (esc(label), _show(now[key]), esc(text)) for key, label, text in COUNTS)
    items += "<dt>Mode: %s</dt><dd>How the engine ran this report.</dd>" % esc(now["mode"])
    items += "<dt>%s</dt><dd>%s</dd>" % ((STUB, "A stub backend was used, so these counts are not evidence of real processing.") if now["stub"] else (
        "Stub backend: none reported", "No stub backend was reported for this run."))
    return "<h2>Newest run</h2><dl>%s</dl>" % items


def render(history):
    body = ["<h1>Coverage</h1><p>Read-only processing counts from run reports. Not evidence review or approval; a finished run is "
            "machine-valid, not semantically accepted. Unknown means the report did not give a usable value.</p>"]
    state = history["state"]
    if state == "not_configured":
        body.append('<p role="status">coverage: not configured on this console</p>')
    elif state != "ok":
        body.append('<p role="status" class="diag">Coverage: unavailable (%s): %s.</p>' % (
            esc(history["code"]), esc(runs.UNAVAILABLE.get(history["code"], "it could not be read"))))
    else:
        reports = history["reports"]
        counts = [run_coverage.coverage_counts(r) for r in reports[:runs.SHOWN + 1]]  # the extra one only baselines the last row
        if reports:
            rows = "".join(_row(r, counts[i], counts[i + 1] if i + 1 < len(counts) else None) for i, r in enumerate(reports[:runs.SHOWN]))
            heads = ("Started (Pacific)", "Mode") + tuple(label for _, label, _ in COUNTS) + ("Run",)
            body.append(_latest(counts[0]))
            body.append('<div class="scroll" role="region" tabindex="0" aria-label="Coverage by run"><table style="min-width:60rem">'
                        "<caption>Newest %d valid runs; change marks compare with the run before</caption><thead><tr>%s</tr></thead>"
                        "<tbody>%s</tbody></table></div>" % (runs.SHOWN, "".join('<th scope="col">%s</th>' % esc(h) for h in heads), rows))
        elif history["skipped"]:
            body.append("<p>Coverage: no valid reports to show.</p>")
        else:
            body.append("<p>Coverage: no completed runs yet.</p>")
        body.append('<p role="status">Older valid runs not listed: %d. Reports not shown: %d (invalid or finished in the future).</p>' % (
            max(0, len(reports) - runs.SHOWN), history["skipped"]))
    return console_layout.shell("Coverage", "\n".join(body), active=NAV[0])


def serve(handler):
    if handler.path != "/coverage":  # a query is not part of this page
        return False
    handler._send(200, render(model.load_history(handler.options.get("run_report_path"))))
    return True
