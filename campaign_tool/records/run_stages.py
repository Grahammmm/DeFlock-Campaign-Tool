"""Container-side stage adapters for the records run (T11i, Phase 1).

``python3 -m campaign_tool.records.run_stages STAGE --project DIR ...`` runs one
real work stage inside the Flock container (or an isolated TEST sandbox
container) and prints exactly one ``records-stage-result-v1`` JSON line last on
stdout. The host-side orchestrator (``run_orchestrator``) folds that line into
the run report.

``DIR`` is a records project: ``mail-history/``, ``processing-v1/`` and
``portal-queue-v1/``. Stages:

* ``sync``: inventory of the mail-history export. With ``--sync-script`` the
  existing Flock mail sync runs first, bounded; without it (TEST) the frozen
  export is only counted. No network is opened by this module.
* ``delta``: ``records adopt-mail`` dry run: what adoption would change.
* ``adoption``: ``records adopt-mail --apply`` with the owner-only config.
  Coded gaps (G option c) are counted, never dropped; identity conflicts and
  unaccepted gaps fail the stage (the adopter is fail-closed per identity).
  Before any apply that could write, the canonical ledger and intake catalog
  are backed up (SQLite backup API) into ``processing-v1/backups/<run_id>/``
  with a hash manifest, rotated to the newest 9 runs. A backup failure fails
  the stage as ``ADOPT_BACKUP_FAILED`` and the apply is never attempted. Only
  positive proof that apply can write nothing (the gate closed, or nothing
  pending / unfinalized / incomplete) skips the backup as
  ``ADOPT_BACKUP_SKIPPED``; an indeterminate dry-run takes the backup
  (fail closed). The backup is taken under the records-root writer lock.
* ``portal``: builds mail-delta notices from preserved messages in memory,
  runs T8 allowlist-gated intake into ``portal-queue-v1`` and then a fetch
  *dry run*. The allowlist is only read. Downloads are a separate, explicit
  step and are never done here.
* ``extract``: Phase 1 (G 2026-10-19) native text only (``native_extract``).
  Every original whose ledger extract stage is pending goes through the
  engine's existing extraction, enrollment and installed extraction check
  under the processing root lock. Each run has a document cap and its own
  deadline. Each document gets one outcome code (``EXTRACTED``,
  ``AWAITING_OCR``, ``CORRUPT_DOCUMENT``, ...). The stage result carries those
  codes as counts in ``outcomes``. Scanned/no-text documents are counted as
  ``AWAITING_OCR``, never dropped. The parsers come from the interpreter the
  stage runs under: the stage plan's ``CONTAINER_PYTHON`` is the campaign-tool
  parser venv (``/opt/campaign-tool/venv``). Without pypdf/openpyxl, PDF and
  XLSX documents are coded ``PARSER_MISSING`` and not attempted.
* ``digest_gates``: ledger stage-state roll-up for the gated stages; anything
  past privacy needs the owner, so it is review-required, never accepted.
* ``outputs``: writes a counts-only snapshot under ``DIR/run-outputs/``.
  Nothing is published or sent.

``digest_gates`` and ``outputs`` read the ledger (and portal
queue) with ``immutable=1``, like ``adopt-mail``'s dry run, so counting never
creates ``-wal``/``-shm`` sidecars. A non-empty WAL or rollback journal (an
active or crashed writer) is reported as ``*_LEDGER_BUSY`` and not read. The
writers (``adoption``, ``extract``) hold the records-root lock, and their last
connection close checkpoints the WAL.

Every stage runs under its own container-side deadline (``--timeout``): killing
a ``docker exec`` client does not stop work inside the container, so the
adapter enforces its own limit and kills its children's process group.

The result line carries only enums, counts and fixed codes: no paths, URLs,
addresses or record text. A machine-valid result is review-required, not
semantic acceptance of the run.
"""
import argparse
import contextlib
from datetime import datetime, timezone
from email import policy as email_policy
from email.parser import BytesParser
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import stat
import subprocess
import sys
import threading
import time

STAGES = ("sync", "delta", "adoption", "portal", "extract", "digest_gates", "outputs")
GATED = ("catalog", "detect", "review", "compare", "privacy")
MAX_EML_BYTES = 64 * 1024 * 1024
MAX_NOTICE_BODY = 2 * 1024 * 1024
BACKUP_KEEP = 9
BACKUP_DIR_NAME = "backups"
BACKUP_MANIFEST = "manifest.json"
# (backup file name, source path relative to the processing root)
BACKUP_SOURCES = (("ledger.sqlite", "ledger.sqlite"),
                  ("intake.sqlite", os.path.join("intake", "intake.sqlite")))


class StageTimeout(Exception):
    pass


def result(status, counts, error_code=None, **extra):
    out = {"stage_result_version": 1, "status": status,
           "counts": {key: int(counts.get(key, 0)) for key in ("input", "output", "failed", "skipped")},
           "error_code": error_code}
    for key in ("new_data", "review_required", "needs_g", "outcomes"):
        if extra.get(key):
            out[key] = extra[key]
    return out


def needs(*items):
    """``needs(("CODE", "action", n), ...)`` keeping only positive counts."""
    return [{"code": code, "severity": severity, "count": int(count)}
            for code, severity, count in items if count and count > 0]


MAX_CHILD_STDOUT = 16 * 1024 * 1024


def _kill_group(pgid):
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def run_child(argv, timeout):
    """Run a child in its own session; on timeout kill the whole group.

    stdout is captured for parsing only (never logged); stderr is discarded.
    The deadline is on the child's own exit, not on stdout EOF: a descendant
    left running with the inherited stdout would otherwise hold the stage open
    until the deadline. After the child exits (or times out) its whole process
    group is killed, which also closes any inherited copy of the pipe.
    """
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            stdin=subprocess.DEVNULL, start_new_session=True)
    chunks, size = [], 0

    def pump():
        nonlocal size
        while True:
            data = proc.stdout.read1(65536)
            if not data:
                return
            if size < MAX_CHILD_STDOUT:
                chunks.append(data[:MAX_CHILD_STDOUT - size])
                size += len(chunks[-1])

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_group(proc.pid)
        proc.wait()
        raise StageTimeout() from None
    finally:
        _kill_group(proc.pid)  # no descendant outlives the stage
        reader.join(5)
        if not reader.is_alive():
            proc.stdout.close()
    return proc.returncode, b"".join(chunks)


def last_json(raw):
    try:
        text = raw.decode("utf-8")
    except UnicodeError:
        return None
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else None
    except ValueError:
        pass
    for line in reversed([line for line in text.splitlines() if line.strip()]):
        try:
            value = json.loads(line)
        except ValueError:
            continue
        return value if isinstance(value, dict) else None
    return None


def paths(project):
    project = Path(project)
    return project / "mail-history", project / "processing-v1", project / "portal-queue-v1"


class LedgerBusy(Exception):
    """A non-empty WAL or a rollback journal: a writer is active or crashed."""


def _sidecars(path):
    keys = []
    for suffix in ("", "-wal", "-shm", "-journal"):
        try:
            info = os.lstat(str(path) + suffix)
            keys.append((info.st_ino, info.st_size, info.st_mtime_ns))
        except FileNotFoundError:
            keys.append(None)
    return keys


@contextlib.contextmanager
def snapshot_ro(path):
    """Count-only read that never creates, changes or removes a file.

    Same contract as ``mail_adoption._snapshot_db``: ``mode=ro`` alone creates
    ``-shm`` (and replays a crashed writer's WAL), so a cleanly checkpointed
    database is read with ``immutable=1``, touching no sidecar. A non-empty WAL
    or a rollback journal raises ``LedgerBusy`` before anything is opened, and
    a sidecar or file identity that changes during the read raises it after.
    The adoption stage (a writer under the records-root lock) settles the WAL
    before these stages run. A missing file is an error, never created.
    """
    path = Path(path)
    before = _sidecars(path)
    if before[0] is None:
        raise sqlite3.OperationalError("database_missing")
    if (before[1] is not None and before[1][1] > 0) or before[3] is not None:
        raise LedgerBusy()
    con = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True, timeout=5)
    try:
        con.execute("PRAGMA query_only=ON")
        yield con
    finally:
        con.close()
    if _sidecars(path) != before:
        raise LedgerBusy()


def ledger_ro(processing):
    return snapshot_ro(processing / "ledger.sqlite")


def busy(code):
    return result("warn", {"skipped": 1}, code, needs_g=needs(("LEDGER_WAL_PENDING", "info", 1))), 0


# --- sync -------------------------------------------------------------------

def stage_sync(args):
    mail, _, _ = paths(args.project)
    synced = None
    if args.sync_script:
        code, _ = run_child([sys.executable, "-B", args.sync_script], args.child_timeout)
        if code != 0:
            return result("fail", {"failed": 1}, "SYNC_FAILED"), 1
        synced = True
    receipts = mail / "receipts"
    if not receipts.is_dir():
        return result("fail", {"failed": 1}, "SYNC_EXPORT_MISSING"), 1
    count = sum(1 for path in receipts.iterdir() if path.suffix == ".json")
    status_ok = True
    try:
        json.loads((mail / "SYNC-STATUS.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        status_ok = False
    if not status_ok:
        return result("warn", {"input": count, "output": count}, "SYNC_STATUS_UNREADABLE"), 0
    if synced is None:
        # Offline TEST copy: nothing was fetched; the frozen export is counted only.
        return result("ok", {"input": count, "output": count},
                      needs_g=needs(("SYNC_OFFLINE_SNAPSHOT", "info", 1))), 0
    return result("ok", {"input": count, "output": count}), 0


# --- delta / adoption -------------------------------------------------------

def adopt_mail(args, apply, *, config=False):
    mail, processing, _ = paths(args.project)
    argv = [sys.executable, "-B", "-m", "campaign_tool.records", "adopt-mail",
            "--root", str(processing), "--mail-history", str(mail)]
    if config or apply:
        argv += ["--config", args.config]
    if apply:
        argv += ["--apply"]
    code, out = run_child(argv, args.child_timeout)
    return code, last_json(out)


def _adoption_needs(report):
    conflicts = sum((report.get("identity_conflicts") or {}).values())
    unaccepted = sum((report.get("unaccepted_coded_gaps") or {}).values())
    return conflicts, unaccepted, needs(
        ("ADOPTION_CODED_GAPS", "info", report.get("coded_gap_messages", 0)),
        ("ADOPTION_IDENTITY_CONFLICT", "blocking", conflicts),
        ("ADOPTION_UNACCEPTED_GAP", "blocking", unaccepted))


def stage_delta(args):
    code, report = adopt_mail(args, apply=False)
    if report is None or report.get("status") == "refused" or "receipts" not in report:
        return result("fail", {"failed": 1}, "DELTA_REFUSED"), 1
    conflicts, unaccepted, items = _adoption_needs(report)
    counts = {"input": report["receipts"], "output": report.get("eligible_new_messages", 0),
              "skipped": report.get("already_adopted", 0) + report.get("coded_gap_messages", 0),
              "failed": conflicts + unaccepted}
    if code not in (0, 3):
        return result("fail", counts, "DELTA_FAILED", needs_g=items), 1
    status, error = ("warn", "DELTA_BLOCKING_CONFLICTS") if code == 3 else ("ok", None)
    return result(status, counts, error, needs_g=items), 0


# --- adoption backup --------------------------------------------------------

class BackupFailure(Exception):
    """A pre-adoption backup could not be completed or verified."""


def _backup_database(source, target):
    """Copy one live SQLite database with the backup API, then verify the copy.

    The source is opened read-write (so a WAL left by a crashed writer is
    recovered into the copy), copied with ``Connection.backup`` (never a file
    copy of a live database), and the copy is integrity-checked. Returns
    ``(sha256_hex, byte_count)``. Raises ``BackupFailure`` on a missing or
    non-regular source, on any copy/read error (a corrupt source, a full disk,
    an I/O failure), or on an integrity check other than ``ok``.
    """
    source, target = Path(source), Path(target)
    try:
        info = source.lstat()
    except FileNotFoundError:
        raise BackupFailure("source_missing") from None
    except OSError as error:
        raise BackupFailure("source_unreadable") from error
    if not stat.S_ISREG(info.st_mode):
        raise BackupFailure("source_not_regular")
    try:
        src = sqlite3.connect(str(source), timeout=5)
        try:
            dst = sqlite3.connect(str(target))
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        check = sqlite3.connect("file:%s?mode=ro" % target, uri=True)
        try:
            row = check.execute("PRAGMA integrity_check").fetchone()
        finally:
            check.close()
        if row is None or row[0] != "ok":
            raise BackupFailure("integrity_check_failed")
        digest, size = hashlib.sha256(), 0
        with target.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
                size += len(block)
        return digest.hexdigest(), size
    except BackupFailure:
        raise
    except (sqlite3.Error, OSError) as error:
        raise BackupFailure("backup_copy_failed") from error


def _remove_backup_dir(path, backups_dir):
    """Unlink the regular files and symlinks directly in ``path``, then rmdir.

    Never recurses, never follows a symlink (neither on ``path`` itself nor on
    any entry inside it), and never removes a non-directory. ``path`` must sit
    directly under ``backups_dir``; any other path is refused untouched. This
    is the only removal path, so rotation can never delete outside ``backups/``.
    """
    path = Path(path)
    if path.parent != Path(backups_dir) or path.name in (".", ".."):
        return
    try:
        info = os.lstat(path)
    except (FileNotFoundError, OSError):
        return
    if not stat.S_ISDIR(info.st_mode):
        return
    try:
        with os.scandir(path) as entries:
            for entry in entries:
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                    os.unlink(entry.path)
    except OSError:
        return
    try:
        os.rmdir(path)
    except OSError:
        pass


def _rotate_backups(backups_dir, keep=BACKUP_KEEP):
    """Keep only the newest ``keep`` run-backup directories directly under
    ``backups_dir``; remove the rest one directory at a time.

    Refuses to act unless ``backups_dir`` is a real directory (``lstat``, so a
    symlink is not followed), so rotation can never follow a link outside the
    backup root. A missing or non-directory ``backups_dir`` is left untouched.
    """
    try:
        info = os.lstat(backups_dir)
    except (FileNotFoundError, OSError):
        return
    if not stat.S_ISDIR(info.st_mode):
        return
    candidates = []
    for entry in os.scandir(backups_dir):
        try:
            info = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        if stat.S_ISDIR(info.st_mode):
            candidates.append((info.st_mtime_ns, info.st_ino, entry.path))
    candidates.sort(key=lambda item: item[:2], reverse=True)
    for _, _, path in candidates[keep:]:
        _remove_backup_dir(path, backups_dir)


def _prepare_backups_dir(backups_dir):
    """Return the backups directory, creating it (0700) only when it is absent.

    The directory is resolved with ``lstat``, so an existing symlink (or any
    non-directory) is refused with ``BackupFailure`` rather than followed --
    neither the backup writes nor rotation may act through a link outside the
    records root. Creation is a single (non-parents) ``mkdir`` on the already
    resolved processing root, so an unchecked intermediate path is never walked.
    """
    try:
        info = os.lstat(backups_dir)
    except FileNotFoundError:
        try:
            os.mkdir(backups_dir, mode=0o700)
        except OSError as error:
            raise BackupFailure("backups_dir_create_failed") from error
        return Path(backups_dir)
    except OSError as error:
        raise BackupFailure("backups_dir_unreadable") from error
    if not stat.S_ISDIR(info.st_mode):
        raise BackupFailure("backups_dir_not_directory")
    return Path(backups_dir)


_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def _safe_run_dir_name(run_id):
    """Accept only a strict run-id token; never ``.``, ``..``, empty or dot-leading.

    The token becomes a single directory name directly under ``backups/``, so
    anything else is rejected (never silently sanitised into a different name)
    with ``BackupFailure`` rather than risk resolving outside the backup root.
    """
    if not isinstance(run_id, str) or not _RUN_ID_RE.match(run_id):
        raise BackupFailure("invalid_run_id")
    return run_id


def backup_adoption_dbs(processing, run_id):
    """Back up the canonical ledger and intake catalog before an adoption apply.

    Copies ``ledger.sqlite`` and ``intake/intake.sqlite`` into
    ``<processing>/backups/<run_id>/`` (directory 0700, files 0600) and writes
    a manifest ``{run_id, utc, files:[{name, sha256, bytes}]}``. Rotates the
    backup directory to the newest ``BACKUP_KEEP`` runs. Returns the total
    backup byte count. Raises ``BackupFailure`` for an invalid run-id, a
    missing/corrupt source, a copy or I/O failure, or a failed integrity check.
    """
    name = _safe_run_dir_name(run_id)
    backups_dir = _prepare_backups_dir(Path(processing) / BACKUP_DIR_NAME)
    run_dir = backups_dir / name
    # Defense in depth: the run directory must sit directly under backups/.
    if run_dir.parent != backups_dir or run_dir.name in (".", ".."):
        raise BackupFailure("invalid_run_id")
    old = os.umask(0o077)
    try:
        try:
            if run_dir.exists():
                _remove_backup_dir(run_dir, backups_dir)
            run_dir.mkdir(mode=0o700)
            os.chmod(run_dir, 0o700)
            files, total = [], 0
            for source_name, relative in BACKUP_SOURCES:
                target = run_dir / source_name
                digest, size = _backup_database(Path(processing) / relative, target)
                os.chmod(target, 0o600)
                files.append({"name": source_name, "sha256": digest, "bytes": size})
                total += size
            manifest = {"run_id": run_id,
                        "utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "files": files}
            manifest_path = run_dir / BACKUP_MANIFEST
            manifest_path.write_text(json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8")
            os.chmod(manifest_path, 0o600)
        except BackupFailure:
            raise
        except (OSError, sqlite3.Error) as error:
            raise BackupFailure("backup_io_failed") from error
    except BaseException:
        _remove_backup_dir(run_dir, backups_dir)
        raise
    finally:
        os.umask(old)
    _rotate_backups(backups_dir)
    return total


def _backup_under_lock(processing, run_id):
    """Take the pre-adoption backup while holding the records-root writer lock.

    ``_backup_database`` opens the canonical ledger read-write so a crashed
    writer's WAL is recovered into the copy; that recovery belongs only to a
    writer holding the root lock. Apply re-acquires the lock in its child
    process after release, so holding it here is safe. A contended lock is a
    ``BackupFailure`` so the apply is not attempted without a backup.
    """
    from .run import RunError, root_lock
    try:
        with root_lock(processing):
            return backup_adoption_dbs(processing, run_id)
    except RunError as error:
        if error.args == ("root_locked",):
            raise BackupFailure("root_locked") from None
        raise
    except OSError as error:
        raise BackupFailure("root_lock_unavailable") from error


def _adoption_needs_backup(dry_report):
    """True unless the dry-run positively proves the apply will write nothing.

    A valid parsed dry-run report (has ``receipts``) with the gate explicitly
    closed, or with the gate open but zero pending / unfinalized / incomplete
    work, is positive proof that apply can write nothing, so the backup may be
    skipped. Anything else -- a timeout, an unparseable or refused child output,
    a missing report -- is indeterminate and must take the backup (fail closed).
    """
    if not (isinstance(dry_report, dict) and "receipts" in dry_report):
        return True
    gate = dry_report.get("gate")
    if not isinstance(gate, dict):
        return True
    if gate.get("open") is False:
        return False
    if gate.get("open") is not True:
        return True
    # Gate open: apply writes unless nothing is pending, incomplete or unfinalized.
    return bool(dry_report.get("pending_messages")
                or dry_report.get("unfinalized_runs")
                or dry_report.get("incomplete_messages"))


def stage_adoption(args):
    if not args.config:
        return result("skipped", {}, "ADOPTION_NOT_CONFIGURED",
                      needs_g=needs(("ADOPTION_NOT_CONFIGURED", "action", 1))), 0
    _, processing, _ = paths(args.project)
    # A read-only dry-run with the owner config tells us whether apply could
    # write anything. Only skip the backup on positive proof that apply can
    # write nothing (gate closed, or nothing pending / unfinalized); any
    # indeterminate dry-run takes the backup so a ledger write is never unbacked.
    backup_bytes, skipped = 0, None
    _, dry_report = adopt_mail(args, apply=False, config=True)
    if _adoption_needs_backup(dry_report):
        try:
            backup_bytes = _backup_under_lock(processing, args.run_id)
        except (BackupFailure, OSError, sqlite3.Error):
            return result("fail", {"failed": 1}, "ADOPT_BACKUP_FAILED"), 1
    else:
        skipped = "ADOPT_BACKUP_SKIPPED"
    code, report = adopt_mail(args, apply=True)
    skip_items = needs((skipped, "info", 1)) if skipped else []
    if report is None or report.get("status") == "refused" or "receipts" not in report:
        return result("fail", {"failed": 1}, "ADOPTION_REFUSED", needs_g=skip_items), 1
    conflicts, unaccepted, items = _adoption_needs(report)
    adopted = int(report.get("adopted_messages", 0)) + int(report.get("resumed_messages", 0))
    failures = sum((report.get("failures") or {}).values())
    counts = {"input": report["receipts"], "output": adopted,
              "skipped": report.get("already_adopted", 0) + report.get("coded_gap_messages", 0),
              "failed": conflicts + unaccepted + failures}
    new_data = {}
    if adopted:
        new_data["documents"] = adopted
    if backup_bytes:
        new_data["backup_bytes"] = backup_bytes
    items += skip_items
    new_data = new_data or None
    if code == 3:
        return result("fail", counts, "ADOPTION_BLOCKED", new_data=new_data, needs_g=items), 1
    if code != 0:
        return result("fail", counts, "ADOPTION_FAILED", new_data=new_data, needs_g=items), 1
    status, error = ("warn", "ADOPTION_CODED_GAPS") if report.get("coded_gap_messages") else ("ok", None)
    return result(status, counts, error, new_data=new_data, needs_g=items), 0


# --- portal -----------------------------------------------------------------

def _resolve(mail, literal):
    if not isinstance(literal, str) or not literal or "\x00" in literal:
        return None
    path = Path(literal) if literal.startswith("/") else mail / literal
    path = Path(os.path.abspath(path))
    if os.path.commonpath((str(mail), str(path))) != str(mail):
        return None
    return path


def notice_bodies(raw):
    message = BytesParser(policy=email_policy.default).parsebytes(raw)
    parts = []
    for part in message.walk():
        if part.get_content_maintype() == "multipart" or part.get_filename():
            continue
        if part.get_content_type() in ("text/html", "text/plain"):
            try:
                parts.append(part.get_content())
            except (LookupError, ValueError):
                continue
    return "\n".join(text for text in parts if isinstance(text, str))


def iter_notices(mail):
    """Yield ``(notice, error)`` from each preserved message; ``notice`` is kept in memory only."""
    for receipt_path in sorted((mail / "receipts").glob("*.json")):
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            original = receipt.get("original_eml") or {}
            path = _resolve(mail, original.get("path"))
            expected = original.get("sha256")
            if path is None or not isinstance(expected, str):
                yield None, "notice_source_unresolved"
                continue
            with path.open("rb") as stream:
                raw = stream.read(MAX_EML_BYTES + 1)
            if len(raw) > MAX_EML_BYTES or hashlib.sha256(raw).hexdigest() != expected:
                yield None, "notice_source_mismatch"
                continue
            body = notice_bodies(raw)
        except (OSError, ValueError, UnicodeError, AttributeError):
            yield None, "notice_source_unreadable"
            continue
        if len(body.encode("utf-8")) > MAX_NOTICE_BODY:
            yield None, "notice_oversize"
            continue
        yield {"body": body, "request_id": "mail:" + expected[:32], "source_sha256": expected}, None


def stage_portal(args):
    from .portal.engine import fetch_queue
    from .portal.intake import intake_notices
    from .portal.policy import PortalError, load_approval
    from .portal.store import Queue

    mail, _, portal = paths(args.project)
    notices, errors = [], 0
    for notice, error in iter_notices(mail):
        if error:
            errors += 1
        elif notice and "/documents/" in notice["body"]:
            notices.append(notice)
    loader = lambda: load_approval(args.approval_file)  # read-only allowlist
    try:
        loader()
    except PortalError as error:
        # Fail closed: an allowlist the engine cannot load enrolls nothing. Never edited here.
        code = {"approval_expired": "PORTAL_ALLOWLIST_EXPIRED",
                "approval_invalid": "PORTAL_ALLOWLIST_INVALID"}.get(str(error), "PORTAL_ALLOWLIST_UNSAFE")
        return result("fail", {"input": len(notices), "skipped": len(notices), "failed": errors}, code,
                      needs_g=needs((code, "blocking", 1))), 1
    queue = None
    try:
        queue = Queue(str(portal))
        totals = {"enrolled": 0, "refused": 0, "skipped": 0}
        for start in range(0, len(notices), 200):
            outcome = intake_notices(queue, notices=notices[start:start + 200], approval_loader=loader)
            for key in totals:
                totals[key] += outcome[key]
        dry = fetch_queue(queue, apply=False, approval_loader=loader)
        refused_hosts = queue.db.execute("SELECT COUNT(DISTINCT host) FROM portal_refusals").fetchone()[0]
    except (PortalError, OSError, ValueError, sqlite3.Error):
        return result("fail", {"input": len(notices), "failed": 1}, "PORTAL_INTAKE_FAILED"), 1
    finally:
        if queue is not None:
            queue.close()
    pending = sum(1 for item in dry.get("items", []) if item.get("state") != "received")
    counts = {"input": totals["enrolled"] + totals["refused"] + totals["skipped"],
              "output": totals["enrolled"], "skipped": totals["skipped"] + totals["refused"],
              "failed": errors}
    items = needs(("PORTAL_HOST_NOT_ALLOWLISTED", "action", refused_hosts),
                  ("PORTAL_NOTICE_UNREADABLE", "action", errors),
                  ("PORTAL_FETCH_DRY_RUN_ONLY", "info", pending or totals["enrolled"]))
    if totals["refused"] or errors:
        code = "PORTAL_HOST_REFUSED" if totals["refused"] else "PORTAL_NOTICE_UNREADABLE"
        return result("warn", counts, code, needs_g=items), 0
    return result("ok", counts, needs_g=items), 0


# --- extract / digest_gates / outputs ----------------------------------------

def stage_counts(db):
    rows = db.execute("SELECT stage, status, COUNT(*) FROM stage_state GROUP BY stage, status").fetchall()
    counts = {}
    for stage, status, n in rows:
        counts.setdefault(stage, {})[status] = n
    return counts


def stage_extract(args):
    """Phase 1 native-text extraction; see ``native_extract`` for the per-document codes."""
    from . import native_extract as ne
    from .run import RunError

    _, processing, _ = paths(args.project)
    deadline = time.monotonic() + args.timeout - ne.DEADLINE_MARGIN
    try:
        summary = ne.run(processing, run_id=args.run_id, deadline=deadline,
                         max_documents=args.max_documents, document_timeout=args.document_timeout)
    except ne.RootNotInitialized:
        return result("fail", {"failed": 1}, "EXTRACT_ROOT_NOT_INITIALIZED"), 1
    except RunError as error:
        if error.args == ("root_locked",):
            # Another writer (adoption, a manual records run) holds the root: retry next run.
            return result("warn", {"skipped": 1}, "EXTRACT_ROOT_LOCKED",
                          needs_g=needs(("EXTRACT_ROOT_LOCKED", "info", 1))), 0
        return result("fail", {"failed": 1}, "EXTRACT_RUN_FAILED"), 1
    except (sqlite3.Error, ValueError, OSError):
        return result("fail", {"failed": 1}, "EXTRACT_LEDGER_UNREADABLE"), 1
    return extract_result(summary)


EXTRACT_NEEDS = (
    # outcome code, needs-G code, severity
    ("AWAITING_OCR", "AWAITING_OCR", "info"),
    ("PARSER_MISSING", "EXTRACT_NATIVE_PARSER_MISSING", "action"),
    ("CORRUPT_DOCUMENT", "EXTRACT_CORRUPT_DOCUMENT", "action"),
    ("EXTRACT_INCOMPLETE", "EXTRACT_INCOMPLETE", "action"),
    ("EXTRACT_TIMEOUT", "EXTRACT_TIMEOUT", "action"),
    ("EXTRACT_NOT_ACCEPTED", "EXTRACT_NOT_ACCEPTED", "action"),
    ("EXTRACT_ERROR", "EXTRACT_ERROR", "action"),
    ("WORKBOOK_POLICY_REQUIRED", "EXTRACT_WORKBOOK_POLICY_REQUIRED", "action"),
    ("DECODER_NEEDED", "EXTRACT_DECODER_NEEDED", "action"),
    ("PRESERVE_PENDING", "EXTRACT_PRESERVE_PENDING", "info"),
    ("DEFERRED_RUN_BUDGET", "EXTRACT_DEFERRED", "info"),
)
SYSTEMIC_MIN_ATTEMPTS = 3


def extract_result(summary):
    from . import native_extract as ne

    outcomes = summary["outcomes"]
    total = sum(outcomes.values())
    extracted = outcomes[ne.EXTRACTED]
    failed = sum(outcomes[code] for code in ne.FAILED)
    counts = {"input": total, "output": extracted, "failed": failed, "skipped": total - extracted - failed}
    items = needs(*((code, severity, outcomes[outcome]) for outcome, code, severity in EXTRACT_NEEDS))
    extra = {"needs_g": items,
             "outcomes": [{"code": code, "count": outcomes[code]} for code in ne.OUTCOMES if outcomes[code]],
             "new_data": {key: value for key, value in (("extracted_units", summary["new_units"]),
                                                       ("pages", summary["new_pages"])) if value} or None}
    attempted = summary["attempted"]
    if (attempted >= SYSTEMIC_MIN_ATTEMPTS and not extracted
            and summary["unexpected_failures"] == attempted):
        # Several documents tried, none extracted, every one refused or errored: the
        # extraction path itself is broken (not a property of one document).
        return result("fail", counts, "EXTRACT_ALL_ATTEMPTS_FAILED", **extra), 1
    if outcomes[ne.PARSER_MISSING]:
        return result("warn", counts, "EXTRACT_NATIVE_PARSER_MISSING", **extra), 0
    if failed:
        return result("warn", counts, "EXTRACT_DOCUMENT_FAILURES", **extra), 0
    if outcomes[ne.DECODER_NEEDED]:
        return result("warn", counts, "EXTRACT_DECODER_NEEDED", **extra), 0
    if outcomes[ne.WORKBOOK_HELD]:
        return result("warn", counts, "EXTRACT_WORKBOOK_POLICY_REQUIRED", **extra), 0
    if outcomes[ne.DEFERRED] or outcomes[ne.PRESERVE_PENDING]:
        return result("warn", counts, "EXTRACT_BACKLOG_DEFERRED", **extra), 0
    # Phase 1: documents awaiting OCR are an expected, counted deferral (info item).
    return result("ok", counts, **extra), 0


def stage_digest_gates(args):
    _, processing, _ = paths(args.project)
    try:
        with ledger_ro(processing) as db:
            counts = stage_counts(db)
            proposals = db.execute("SELECT COUNT(*) FROM proposals").fetchone()[0]
    except LedgerBusy:
        return busy("GATES_LEDGER_BUSY")
    except sqlite3.Error:
        return result("fail", {"failed": 1}, "GATES_LEDGER_UNREADABLE"), 1
    pending = sum(counts.get(stage, {}).get("pending", 0) for stage in GATED)
    blocked = sum(counts.get(stage, {}).get("blocked", 0) for stage in GATED)
    done = sum(counts.get(stage, {}).get("done", 0) for stage in GATED)
    out = {"input": pending + blocked + done, "output": done, "failed": blocked, "skipped": pending}
    # Every proposal past privacy needs the owner: review-required, never accepted here.
    items = needs(("GATE_BLOCKED", "action", blocked))
    if proposals or blocked:
        return result("warn", out, "GATE_REVIEW_REQUIRED", review_required=proposals, needs_g=items), 0
    return result("ok", out, needs_g=items), 0


def stage_outputs(args):
    mail, processing, portal = paths(args.project)
    try:
        with ledger_ro(processing) as db:
            snapshot = {"snapshot_version": 1, "run_id": args.run_id,
                        "stage_state": stage_counts(db),
                        "mail_messages": db.execute("SELECT COUNT(*) FROM mail_messages").fetchone()[0],
                        "originals": db.execute("SELECT COUNT(*) FROM originals").fetchone()[0]}
        with snapshot_ro(portal / "portal.sqlite") as pdb:
            snapshot["portal_items"] = pdb.execute("SELECT COUNT(*) FROM portal_items").fetchone()[0]
            tables = {row[0] for row in pdb.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            snapshot["portal_refusals"] = (pdb.execute("SELECT COUNT(*) FROM portal_refusals").fetchone()[0]
                                           if "portal_refusals" in tables else 0)
    except LedgerBusy:
        return busy("OUTPUTS_LEDGER_BUSY")
    except sqlite3.Error:
        return result("fail", {"failed": 1}, "OUTPUTS_LEDGER_UNREADABLE"), 1
    directory = Path(args.project) / "run-outputs"
    old = os.umask(0o077)
    try:
        directory.mkdir(mode=0o700, exist_ok=True)
        name = "".join(ch for ch in args.run_id if ch.isalnum() or ch in "-_.") or "run"
        target = directory / f"{name}.json"
        temporary = directory / f".{name}.json.tmp"
        temporary.write_text(json.dumps(snapshot, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, target)
    except OSError:
        return result("fail", {"input": 1, "failed": 1}, "OUTPUTS_WRITE_FAILED"), 1
    finally:
        os.umask(old)
    return result("ok", {"input": 1, "output": 1}), 0


HANDLERS = {"sync": stage_sync, "delta": stage_delta, "adoption": stage_adoption,
            "portal": stage_portal, "extract": stage_extract,
            "digest_gates": stage_digest_gates, "outputs": stage_outputs}


def parser():
    p = argparse.ArgumentParser(prog="run_stages", description="Records run stage adapter (one JSON result line).")
    p.add_argument("stage", choices=STAGES)
    p.add_argument("--project", required=True, help="records project directory")
    p.add_argument("--timeout", type=int, default=600, help="container-side stage deadline (seconds)")
    p.add_argument("--run-id", default="run")
    p.add_argument("--sync-script", help="sync: existing mail sync script to run first (live only)")
    p.add_argument("--config", help="adoption: owner-only mail-history-adoption-v1 config")
    p.add_argument("--approval-file", help="portal: root-owned allowlist (read only)")
    p.add_argument("--max-documents", type=int, default=200,
                   help="extract: documents attempted per run; the rest are counted as deferred")
    p.add_argument("--document-timeout", type=int, default=120,
                   help="extract: per-document parser deadline (seconds)")
    return p


def main(argv=None):
    os.umask(0o077)
    args = parser().parse_args(argv)
    if (not 1 <= args.timeout <= 2700 or not 1 <= args.max_documents <= 5000
            or not 10 <= args.document_timeout <= 600):
        print(json.dumps(result("fail", {"failed": 1}, "STAGE_CONFIG_INVALID")))
        return 1
    if args.stage == "portal" and not args.approval_file:
        print(json.dumps(result("fail", {"failed": 1}, "PORTAL_ALLOWLIST_MISSING")))
        return 1
    args.project = os.path.abspath(args.project)
    # Children get the remaining budget minus a small margin; the alarm is the hard stop.
    args.child_timeout = max(1, args.timeout - 5)

    def on_alarm(signum, frame):
        raise StageTimeout()

    signal.signal(signal.SIGALRM, on_alarm)
    signal.alarm(args.timeout)
    try:
        line, code = HANDLERS[args.stage](args)
    except StageTimeout:
        line, code = result("fail", {"failed": 1}, "STAGE_CONTAINER_TIMEOUT"), 1
    except Exception:  # noqa: BLE001 - never print exception text (may hold private values)
        line, code = result("fail", {"failed": 1}, "STAGE_INTERNAL_ERROR"), 1
    finally:
        signal.alarm(0)
    print(json.dumps(line, sort_keys=True))
    return code


if __name__ == "__main__":
    sys.exit(main())
