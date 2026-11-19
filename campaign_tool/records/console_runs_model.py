"""Read-only run history: the validated archive of <runtime>/<target>/reports/run_*.json (DFCON17-02).

Same directory pipeline_health._recent_reports reads, but this reader never caps filenames before validating,
never turns an unreadable directory into an empty one, and counts what it could not use. It returns only
validated reports; a consumer must project whitelisted fields (codes and counts, never raw payload).
"""
import os
import re
import stat
from datetime import datetime, timezone

from . import console_automation, pipeline_health, run_report

RUN_ID_RE = re.compile(r"run_[0-9]{8}T[0-9]{6}Z_[0-9a-f]{8}")  # the engine's archive filename and ?id= grammar
FILE_LIMIT = 2000  # matching archive files examined per request; beyond this the history is unavailable


def _history(state, code=None, skipped=0, reports=()):
    return {"state": state, "code": code, "skipped": skipped, "reports": list(reports)}


def _names(directory):
    """Exact archive basenames, or an unavailable code. A symlinked or non-directory archive is never followed."""
    try:
        if not stat.S_ISDIR(os.lstat(directory).st_mode):
            return "reports_unreadable"
        with os.scandir(directory) as entries:
            names = [e.name for e in entries if RUN_ID_RE.fullmatch(e.name[:-5]) and e.name.endswith(".json")]
    except FileNotFoundError:
        return "reports_missing"
    except OSError:
        return "reports_unreadable"
    return "history_limit" if len(names) > FILE_LIMIT else names


def _usable(directory, name, now, schema):
    """The validated report, or None when it is unreadable, invalid, mismatched with its filename or not yet finished."""
    try:
        data = console_automation._read(os.path.join(directory, name))
        run_report.validate_run_report(data, schema)
        if data["run_id"] + ".json" != name or pipeline_health._finished(data) > now:
            return None
    except (OSError, ValueError, KeyError, TypeError, OverflowError):
        return None
    return data


def load_history(run_report_path, now=None):
    """{"state": not_configured|unavailable|ok, "code", "skipped", "reports"}.

    ``skipped`` counts matching files that were invalid, mismatched or finished after ``now``; they take no
    slot in ``reports`` (every valid report, newest finish first, then run id). The display cap belongs to the page."""
    from . import console_oversight  # lazy: its import-time nav() loads the page registry, which loads our page

    target = console_oversight._target(run_report_path)
    if target is None:
        return _history("not_configured")
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    directory = str(target / "reports")
    names = _names(directory)
    if isinstance(names, str):
        return _history("unavailable", names)
    schema = run_report.load_schema()
    found = [_usable(directory, name, now, schema) for name in names]
    reports = sorted((r for r in found if r is not None),
                     key=lambda r: (pipeline_health._finished(r), r["run_id"]), reverse=True)
    return _history("ok", skipped=len(found) - len(reports), reports=reports)
