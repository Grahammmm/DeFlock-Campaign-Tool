"""Read-only run history: the validated archive of <runtime>/<target>/reports/run_*.json (DFCON17-02).

Same directory pipeline_health._recent_reports reads, but this reader never caps filenames before validating,
never turns an unreadable directory into an empty one, and counts what it could not use. It returns only
validated reports; a consumer must project whitelisted fields (codes and counts, never raw payload).
"""
import json
import os
import re
import stat
from datetime import datetime, timezone

from . import pipeline_health, run_report

RUN_ID_RE = re.compile(r"run_[0-9]{8}T[0-9]{6}Z_[0-9a-f]{8}")  # the engine's archive filename and ?id= grammar
FILE_LIMIT = 2000  # matching archive files examined per request; beyond this the history is unavailable


def _history(state, code=None, skipped=0, reports=()):
    return {"state": state, "code": code, "skipped": skipped, "reports": list(reports)}


def _open_dir(directory):
    """A retained no-follow descriptor on the archive directory, or an unavailable code.

    Every later enumeration and file open goes through this descriptor, so replacing the pathname with a symlink
    (or another directory) after this point cannot redirect what is read. A symlink or non-directory is refused."""
    try:
        return os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return "reports_missing"
    except OSError:  # includes ELOOP for a symlink and ENOTDIR for a file
        return "reports_unreadable"


def _names(dir_fd):
    """Exact archive basenames listed from the descriptor, or an unavailable code."""
    try:
        with os.scandir(dir_fd) as entries:
            names = [e.name for e in entries if RUN_ID_RE.fullmatch(e.name[:-5]) and e.name.endswith(".json")]
    except OSError:
        return "reports_unreadable"
    return "history_limit" if len(names) > FILE_LIMIT else names


def _read_at(dir_fd, name):
    """Bounded, nonblocking, no-follow JSON object read of ``name`` relative to ``dir_fd``; never opens a FIFO or device."""
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dir_fd)
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ValueError("not_regular")
        raw = handle.read(pipeline_health.MAX_JSON_BYTES + 1)
    if len(raw) > pipeline_health.MAX_JSON_BYTES:
        raise ValueError("too_large")
    try:
        data = json.loads(raw)
    except RecursionError as exc:
        raise ValueError("metadata_json_depth") from exc
    if not isinstance(data, dict):
        raise ValueError("not_object")
    return data


def _usable(dir_fd, name, now, schema):
    """The validated report, or None when it is unreadable, invalid, mismatched with its filename or not yet finished."""
    try:
        data = _read_at(dir_fd, name)
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
    dir_fd = _open_dir(str(target / "reports"))
    if isinstance(dir_fd, str):
        return _history("unavailable", dir_fd)
    try:
        names = _names(dir_fd)
        if isinstance(names, str):
            return _history("unavailable", names)
        schema = run_report.load_schema()
        found = [_usable(dir_fd, name, now, schema) for name in names]
    finally:
        os.close(dir_fd)
    reports = sorted((r for r in found if r is not None),
                     key=lambda r: (pipeline_health._finished(r), r["run_id"]), reverse=True)
    return _history("ok", skipped=len(found) - len(reports), reports=reports)
