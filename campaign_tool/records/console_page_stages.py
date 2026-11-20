"""GET /stages: read-only go-live evidence from archived run reports."""
import os
import re
from urllib.parse import parse_qs, quote

from . import console_layout, console_runs_model as model, golive_stages
from .console_page_runs import UNAVAILABLE
from .outcomes.reading import esc

PATHS = ("/stages",)
NAV = ("Go-live stages", 15)
COLUMNS = (
    ("stage", "Stage"), ("name", "Name"), ("state", "State"),
    ("last_proven_run", "Last proven run"), ("runs_since_last_proof", "Runs since proof"),
    ("first_warning", "First warning code"), ("last_warning", "Last warning code"),
    ("masked_by", "Masked by"),
)
INTRO = ("<h1>Go-live stages</h1><p>Read-only. Proven means the run report metadata qualifies; "
         "it is machine-valid evidence, not semantic acceptance. External stages are not provable from run reports.</p>")
# The CLI includes the directory on archive errors. Only allow named report errors with a basename.
REPORT_ERROR = re.compile(r'''(['"])[^/\\\r\n]+\.json\1: (?:invalid report|unreadable report|duplicate run ID \w+)''')


def _status(text, diagnostic=False):
    style = ' class="diag"' if diagnostic else ''
    return f'<p role="status"{style}>{esc(text)}</p>'


def _unavailable(code):
    return _status(f"Run history: unavailable ({code}): {UNAVAILABLE[code]}.", diagnostic=True)


def _cell(key, value):
    if value is None:
        return "—"
    if key == "last_proven_run":
        return '<a href="/run?id=%s">%s</a>' % (esc(quote(value, safe="")), esc(value))
    return esc(value)


def _table(rows):
    columns = COLUMNS + (("basis", "Basis"),) if any("basis" in row for row in rows) else COLUMNS
    headings = "".join(f'<th scope="col">{esc(label)}</th>' for _, label in columns)
    body = "".join("<tr>" + "".join(f"<td>{_cell(key, row.get(key))}</td>" for key, _ in columns) + "</tr>"
                   for row in rows)
    return ('<div class="scroll" role="region" tabindex="0" aria-label="Stage evidence">'
            '<table style="min-width:60rem"><caption>Stage evidence</caption>'
            f'<thead><tr>{headings}</tr></thead><tbody>{body}</tbody></table></div>')


def _evidence(run_report_path, since_values):
    from .console_oversight import _target  # same resolution as load_history; defer until after registry import

    target = _target(run_report_path)
    if target is None:
        return _status("Run history: not configured on this console.")
    directory = str(target / "reports")
    fd = model._open_dir(directory)
    if isinstance(fd, str):
        return _unavailable(fd)
    try:
        # The strict reader considers all .json files, not just canonical run filenames.
        try:
            with os.scandir(fd) as entries:
                for count, _ in enumerate((e for e in entries if e.name.endswith(".json")), 1):
                    if count > model.FILE_LIMIT:
                        return _unavailable("history_limit")
        except OSError:
            return _unavailable("reports_unreadable")
        # Keep the limit check and strict loader on the same directory, even if its pathname changes.
        reports = golive_stages.load_reports(f"/proc/self/fd/{fd}/.")
    except ValueError as exc:
        message = str(exc)
        if message.endswith(": reports directory unreadable"):
            return _unavailable("reports_unreadable")
        return _status("golive_stages: " + (message if REPORT_ERROR.fullmatch(message) else "invalid report"),
                       diagnostic=True)
    except OSError:
        return _status("golive_stages: bundled run-report schema unreadable", diagnostic=True)
    finally:
        os.close(fd)

    since = since_values[0] if since_values else None
    if len(since_values) > 1 or (since is not None and since not in {r["run_id"] for r in reports}):
        return _status("unknown run")
    summary = golive_stages.summarize(reports, since)
    window = _status("Warning codes after " + since + "." if since is not None else "Warning codes from all eligible runs.")
    return window + _table(summary["stages"])


def serve(handler):
    path, _, query = handler.path.partition("?")
    if path not in PATHS:
        return False
    since = parse_qs(query, keep_blank_values=True).get("since", ())
    body = _evidence(handler.options.get("run_report_path"), since)
    handler._send(200, console_layout.shell(NAV[0], INTRO + body, active=NAV[0]))
    return True
