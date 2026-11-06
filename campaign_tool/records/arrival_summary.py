"""Metadata-only summary of one arrival-triggered records run (REC-AUTO-6).

Two read-only counts feed the Telegram summary, never record content:

* ``received_by_kind``: NEW logical originals this run, by received-item kind. "New" means the
  original's SHA-256 was not in the per-run baseline captured at the start of sync
  (``run-outputs/<run_id>/arrival-baseline.json``: version, run_id, sorted digests only).
* ``pending_drafts``: drafts pending in the approval queue now (a backlog count, not newly drafted).

``None`` means unavailable (missing, corrupt or stale source); it is never replaced by zero.
Nothing here writes the ledger, the queue, receipts or holds, and nothing is sent.
"""
import json
import os
import re
import stat
from pathlib import Path

VERSION = 1
KINDS = ("response", "extension", "denial", "fee", "records", "other")
RUN_ID_RE = re.compile(r"^run_[0-9]{8}T[0-9]{6}Z_[0-9a-f]{8}$")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_BYTES = 64 * 1024 * 1024
BASELINE_NAME = "arrival-baseline.json"


class ArrivalSummaryError(Exception):
    """Fixed-code failure; never carries private text."""


def _need(condition, code):
    if not condition:
        raise ArrivalSummaryError(code)


def _safe_dir(path):
    path = Path(os.path.abspath(path))
    for part in (*reversed(path.parents), path):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        _need(stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode), "unsafe_path")
    return path


def baseline_path(project, run_id):
    _need(isinstance(run_id, str) and RUN_ID_RE.fullmatch(run_id) is not None, "run_id")
    return _safe_dir(project) / "run-outputs" / run_id / BASELINE_NAME


def _read_private(path, limit=MAX_BYTES):
    """Read a regular, single-link, owner-only file without following symlinks or blocking."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        raise ArrivalSummaryError("unreadable") from None
    try:
        info = os.fstat(fd)
        _need(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size <= limit, "unsafe_file")
        chunks = []
        while True:
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                return b"".join(chunks), info
            chunks.append(chunk)
    except OSError:
        raise ArrivalSummaryError("unreadable") from None
    finally:
        os.close(fd)


def _ledger_digests(project):
    """Original digests in the ledger; an absent ledger is empty, an unreadable one is unavailable."""
    import sqlite3
    from .run_stages import LedgerBusy, snapshot_ro
    ledger = _safe_dir(project) / "processing-v1" / "ledger.sqlite"
    if not os.path.lexists(ledger):
        return []
    _need(not ledger.is_symlink(), "ledger_unavailable")
    try:
        with snapshot_ro(ledger) as db:
            rows = db.execute("SELECT sha256 FROM originals").fetchall()
    except (OSError, LedgerBusy, ValueError, sqlite3.Error) as error:  # StageTimeout is not caught here
        raise ArrivalSummaryError("ledger_unavailable") from error
    digests = sorted({row[0] for row in rows})
    _need(all(isinstance(d, str) and HASH_RE.fullmatch(d) for d in digests), "ledger_unavailable")
    return digests


def _parse_baseline(raw, run_id):
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise ArrivalSummaryError("baseline_invalid") from None
    _need(isinstance(doc, dict) and set(doc) == {"version", "run_id", "originals"}, "baseline_invalid")
    _need(type(doc["version"]) is int and doc["version"] == VERSION and doc["run_id"] == run_id, "baseline_invalid")
    originals = doc["originals"]
    _need(isinstance(originals, list) and all(isinstance(d, str) and HASH_RE.fullmatch(d) for d in originals)
          and originals == sorted(set(originals)), "baseline_invalid")
    return originals


def load_baseline(project, run_id):
    path = baseline_path(project, run_id)
    raw, info = _read_private(path)
    _need(info.st_mode & 0o077 == 0, "baseline_unsafe")
    return set(_parse_baseline(raw, run_id))


def capture_baseline(project, run_id):
    """Write the first baseline for ``run_id``; a repeat verifies and reuses the first, never replaces it."""
    target = baseline_path(project, run_id)
    digests = _ledger_digests(project)
    runs = target.parent.parent
    old = os.umask(0o077)
    try:
        runs.mkdir(mode=0o700, exist_ok=True)
        _safe_dir(runs)
        target.parent.mkdir(mode=0o700, exist_ok=True)
        _safe_dir(target.parent)
        if os.path.lexists(target):
            load_baseline(project, run_id)
            return target
        encoded = (json.dumps({"version": VERSION, "run_id": run_id, "originals": digests},
                              sort_keys=True, separators=(",", ":")) + "\n").encode()
        temporary = target.parent / f".{BASELINE_NAME}.{os.getpid()}.tmp"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, target)  # atomic first capture: never replaces an existing baseline
            except FileExistsError:
                load_baseline(project, run_id)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
    except OSError:
        raise ArrivalSummaryError("baseline_write") from None
    finally:
        os.umask(old)
    return target


def new_counts(project, run_id):
    """New-original counts by kind, from this run's received-items file against the baseline."""
    baseline = load_baseline(project, run_id)
    source = _safe_dir(project) / "run-outputs" / run_id / "outcomes" / "received-items.json"
    raw, _ = _read_private(source)
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise ArrivalSummaryError("received_invalid") from None
    _need(isinstance(doc, dict) and doc.get("schema") == "records-received-item-v1"
          and doc.get("run_id") == run_id and isinstance(doc.get("items"), list), "received_invalid")
    counts = {kind: 0 for kind in KINDS}
    ids, digests = set(), set()
    for item in doc["items"]:
        _need(isinstance(item, dict) and item.get("kind") in KINDS, "received_invalid")
        ident, digest = item.get("item_id"), item.get("original_sha256")
        _need(isinstance(ident, str) and ident not in ids and isinstance(digest, str)
              and HASH_RE.fullmatch(digest) and digest not in digests, "received_invalid")
        ids.add(ident)
        digests.add(digest)
        if digest not in baseline:
            counts[item["kind"]] += 1
    return counts


def pending_count(project):
    """Drafts pending now, from the current folded queue state (not event history)."""
    from .approval_queue import ApprovalQueueError, load_queue
    try:
        rows = load_queue(project)
    except (ApprovalQueueError, OSError, ValueError) as error:  # StageTimeout is not caught here
        raise ArrivalSummaryError("queue_unavailable") from error
    return sum(1 for row in rows if row.get("status") == "pending")


def summarize(project, run_id):
    """``{received_by_kind, pending_drafts}``; each independently None when unavailable."""
    try:
        received = new_counts(project, run_id)
    except ArrivalSummaryError:
        received = None
    try:
        pending = pending_count(project)
    except ArrivalSummaryError:
        pending = None
    return {"received_by_kind": received, "pending_drafts": pending}
