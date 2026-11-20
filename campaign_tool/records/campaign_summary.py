"""Per-campaign records work counts for `campaigns today` (FLK-CMP-5).

``project`` runs once at console-export build time over the just-copied export (a read-only SnapshotSource, never the
live project) and returns the metadata-only ``records-campaign-summary-v1`` document that the builder writes as
``console-model/campaign-summary.json``. ``read_export`` is the host-side, counts-only reader: it pins the canonical
``current`` link, then opens only EXPORT.json and that one summary file. No writers, network, subprocess or live readers
live here. The summary is bound to its local export manifest by a SHA-256; that is not a signature or approval.
"""
import errno
import hashlib
import os
import re
import stat
from datetime import datetime, timezone

SCHEMA = "records-campaign-summary-v1"
REL = "console-model/campaign-summary.json"
COUNTS = ("drafts_waiting", "messages_to_tie", "requests_overdue", "holds_needing_decision")
MAX_SUMMARY = 16 * 1024
MAX_COUNT = 10 ** 9
STALE_HOURS = 24  # a display threshold for the organizer's list, not a records or legal policy
NO_EXPORT_TEXT = "no records export"
OLD_TEXT = "records summary not in this export; refresh the console export"
STAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
PROJECT_ID = re.compile(r"[0-9a-f]{16}")


def _iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ok(model, key):
    state = model.get(key)
    return isinstance(state, dict) and state.get("state") == "ok"


def from_home(model):
    """The four counts from a loaded console Home model; null (never zero) for anything Home could not load."""
    from . import console_home, draft_templates
    counts = dict.fromkeys(COUNTS)
    if _ok(model, "queue"):
        pending = [r for r in model["queue"]["rows"] if r["status"] == "pending"]
        counts["messages_to_tie"] = sum(r["kind"] == draft_templates.MESSAGE_LINK for r in pending)
        counts["drafts_waiting"] = len(pending) - counts["messages_to_tie"]
    if all(_ok(model, k) for k in ("timeline", "waiting", "received")):
        counts["requests_overdue"] = sum(bool(r["overdue"]) for r in model["waiting"]["rows"])
    if _ok(model, "public") and _ok(model, "packets"):
        rows = console_home.decision_rows(model, model["now"].date())
        counts["holds_needing_decision"] = sum(r["source"] in ("public", "packets") for r in rows)
    return counts


def project(dest, run_id, project_id, name, now):
    """The summary document for the freshly built export tree `dest`, evaluated at the export instant `now` (epoch)."""
    from . import console_home, console_snapshot as cs
    stamp = _iso(now)
    try:
        pin = cs.Pin(os.fspath(dest), run_id, project_id, name, stamp)
        model = console_home.load(os.fspath(dest), run_id, now=datetime.fromtimestamp(now, timezone.utc),
                                  source=cs.SnapshotSource(pin))
        counts = from_home(model)
        if any(v is not None and (type(v) is not int or v < 0) for v in counts.values()):
            raise ValueError("count shape")
    except Exception:  # an unexpected shape is all-unavailable, never an invented zero
        counts = dict.fromkeys(COUNTS)
    return {"schema": SCHEMA, "source_project_id": project_id, "run_id": run_id, "evaluated_at": stamp,
            "counts": counts, "unavailable": sorted(k for k, v in counts.items() if v is None)}


def _result(state, code=None, summary=None, exported_at=None, now=None):
    out = {"state": state, "code": code, "exported_at": None, "age_hours": None, "stale": None,
           **dict.fromkeys(COUNTS), "unavailable": sorted(COUNTS)}
    if summary is not None:
        out.update(summary["counts"])
        out["unavailable"] = list(summary["unavailable"])
        when = None
        if exported_at is not None and STAMP.fullmatch(exported_at):
            try:
                when = datetime.strptime(exported_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            except ValueError:
                pass  # A bad calendar date makes the time unknown, not the verified counts.
        if when is None or when > now:  # never clamp a future instant to a fresh export
            out["code"] = "RECORDS_EXPORT_TIME_UNAVAILABLE"
        else:
            hours = int((now - when).total_seconds() // 3600)
            out.update(exported_at=exported_at, age_hours=hours, stale=hours >= STALE_HOURS)
    return out


class _Refused(Exception):
    pass


def _need(ok):
    if not ok:
        raise _Refused


def _dir_at(parent, name):
    return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)


def _private(fd):
    info = os.fstat(fd)
    return stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid() and not info.st_mode & 0o077


def _read_leaf(dir_fd, name, cap):
    """Bytes of one regular file below the pinned dir_fd (no symlink, never blocking); None when absent."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    except FileNotFoundError:
        return None
    try:
        _need(stat.S_ISREG(os.fstat(fd).st_mode))
        chunks, total = [], 0
        while total <= cap:
            chunk = os.read(fd, cap + 1 - total)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
    finally:
        os.close(fd)
    _need(total <= cap)
    return b"".join(chunks)


def _count_ok(value):
    return value is None or (type(value) is int and 0 <= value <= MAX_COUNT)


def _summary(raw, files, manifest, pid):
    from . import approval_queue as aq
    _need(hashlib.sha256(raw).hexdigest() == files[REL])
    doc = aq._strict_load(raw, "summary")
    _need(type(doc) is dict and set(doc) == {"schema", "source_project_id", "run_id", "evaluated_at", "counts", "unavailable"})
    counts, missing = doc["counts"], doc["unavailable"]
    _need(doc["schema"] == SCHEMA and doc["source_project_id"] == pid == manifest["source_project_id"]
          and doc["run_id"] == manifest["run_id"] and doc["evaluated_at"] == manifest.get("exported_at"))
    _need(type(counts) is dict and set(counts) == set(COUNTS) and all(_count_ok(v) for v in counts.values()))
    _need(type(missing) is list and missing == sorted(k for k, v in counts.items() if v is None))
    return doc


def read_export(binding, *, now):
    """State, fixed code, export instant, age and the four counts from the campaign's current export; counts only.

    `binding` is {"export_root", "source_project_id"}; `now` an aware UTC datetime captured once by the caller."""
    from . import console_export as ce, console_snapshot as cs, approval_queue as aq
    fds = []
    try:
        try:
            current = "/"
            fds.append(os.open("/", os.O_RDONLY | os.O_DIRECTORY))
            for part in (p for p in binding["export_root"].split("/") if p):
                fds.append(_dir_at(fds[-1], part))
            root = fds[-1]
            _need(_private(root))
            target = os.readlink("current", dir_fd=root)
        except FileNotFoundError:
            return _result("no_export", "RECORDS_NO_EXPORT")
        match = cs.LINK.fullmatch(target)
        _need(match)
        name = match.group(1)
        fds.append(_dir_at(root, "exports"))
        _need(_private(fds[-1]))
        try:
            fds.append(_dir_at(fds[-1], name))
        except FileNotFoundError:
            raise _Refused from None
        top = fds[-1]
        _need(_private(top))
        raw = _read_leaf(top, ce.MANIFEST, cs.MAX_MANIFEST)
        _need(raw is not None)
        manifest = aq._strict_load(raw, "manifest")
        _need(type(manifest) is dict)
        run_id, pid, files = manifest["run_id"], manifest["source_project_id"], manifest["files"]
        _need(manifest["schema"] == ce.SCHEMA and isinstance(run_id, str) and ce.RUN_ID.fullmatch(run_id)
              and name[17:] == run_id and isinstance(pid, str) and PROJECT_ID.fullmatch(pid)
              and pid == binding["source_project_id"] and isinstance(files, dict)
              and all(isinstance(v, str) and ce.HEX.fullmatch(v) for v in files.values()))
        try:
            model = _dir_at(top, ce.MODEL_DIR)
        except FileNotFoundError:
            model = None
        if model is not None:
            fds.append(model)
        body = None if model is None else _read_leaf(model, os.path.basename(REL), MAX_SUMMARY)
        if REL not in files:
            _need(body is None)  # present but unlisted: foreign
            return _result("unavailable", "RECORDS_SUMMARY_NOT_IN_EXPORT")
        _need(body is not None)
        doc = _summary(body, files, manifest, pid)
        exported = manifest.get("exported_at")
        return _result("ok", None, doc, exported if isinstance(exported, str) else None, now)
    except (_Refused, OSError, ValueError, KeyError, TypeError, AttributeError, aq.ApprovalQueueError):
        return _result("unavailable", "RECORDS_EXPORT_UNAVAILABLE")
    finally:
        for fd in fds:
            try:
                os.close(fd)
            except OSError:
                pass


def line(row):
    """The text after `records:` for one records row (fixed labels; unknown for a null count)."""
    if row["state"] == "no_export":
        return NO_EXPORT_TEXT
    if row["code"] == "RECORDS_SUMMARY_NOT_IN_EXPORT":
        return OLD_TEXT
    if row["state"] != "ok":
        return "records export unavailable (%s)" % row["code"]
    show = ["unknown" if row[k] is None else str(row[k]) for k in COUNTS]
    when = "export time unknown" if row["age_hours"] is None else "exported %s, %s" % (
        row["exported_at"], "stale (%d hours old)" % row["age_hours"] if row["stale"] else "%d hours old" % row["age_hours"])
    return ("drafts waiting for review %s; unmatched messages to tie %s; requests overdue (on our records; not a legal "
            "finding) %s; holds needing a decision %s; %s") % (*show, when)
