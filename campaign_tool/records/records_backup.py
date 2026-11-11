"""records backup / backup-verify: attended adapter over backup_v2 (DFGL-12A). Output is codes and counts only.

Refuses unless a fresh ``records-quiescence-v1`` evidence file (written by the independent writer audit, DFGL-12B)
names exactly the project root. Sets are written under ``--into`` and every success appends to the separate trusted
list ``backup-digests.jsonl`` that ``backup-verify`` reads. Nothing is copied off-box or deleted.
"""
import argparse
import hashlib
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import pipeline_health
from .recovery import backup_v2 as v2
from .recovery.backup import RecoveryError
from .recovery.records_profile import ProfileIncomplete, build_profile

DIGESTS = "backup-digests.jsonl"
EVIDENCE_KEYS = {"schema", "basis", "roots", "observed_utc", "all_writers_stopped", "checks", "auditor"}
MAX_EVIDENCE = MAX_LIST = 1024 * 1024


def _emit(code, **fields):
    print(json.dumps({"code": code, **fields}, sort_keys=True))
    return 0 if code == "OK" else 1


def _read(path, limit):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        raw = os.read(fd, limit + 1)
    finally:
        os.close(fd)
    if len(raw) > limit:
        raise ValueError("too_large")
    return raw


def _evidence(path, trusted, roots):
    """CallerQuiescence from the audit file, or raise ValueError (reported as QUIESCENCE_REFUSED)."""
    raw = _read(path, MAX_EVIDENCE)
    digest = hashlib.sha256(raw).hexdigest()
    if trusted is not None and digest != trusted:
        raise ValueError("evidence_digest_mismatch")
    doc = json.loads(raw)
    if (type(doc) is not dict or set(doc) != EVIDENCE_KEYS or doc["schema"] != "records-quiescence-v1" or
            doc["basis"] != "independent_all_writer_audit" or doc["auditor"] != "records quiescence-audit" or
            type(doc["checks"]) is not dict or tuple(doc["roots"]) != roots or doc["all_writers_stopped"] is not True):
        raise ValueError("evidence_invalid")
    return v2.CallerQuiescence(roots, doc["observed_utc"], digest, all_writers_stopped=True)


def _incomplete(error):
    return _emit(error.code, **error.counts, **({"reason": error.reason} if error.reason else {}))


def _check_profile(project):
    report = {}
    try:
        v2._profile(build_profile(project, report))
    except ProfileIncomplete as error:
        return _incomplete(error)
    except sqlite3.Error:
        return _emit("PROFILE_INCOMPLETE", reason="database_unreadable", **report)
    except (RecoveryError, OSError, ValueError, KeyError) as error:
        code = "LIMIT_ENTRIES" if str(error) == "scope_entry_bound" else "PROFILE_INCOMPLETE"
        return _emit(code, reason=str(error) if isinstance(error, RecoveryError) else "scan_failed", **report)
    return _emit("OK", **report)


def backup_main(argv=None):
    parser = argparse.ArgumentParser(prog="records backup", description=__doc__.split("\n")[0])
    parser.add_argument("--project", required=True)
    parser.add_argument("--check-profile", action="store_true", help="print coverage counts only; write nothing")
    parser.add_argument("--into")
    parser.add_argument("--quiescence")
    parser.add_argument("--quiescence-sha256", help="trusted digest of the evidence file from the audit")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    project = os.path.abspath(args.project)
    if args.check_profile:
        return _check_profile(project)
    if not (args.into and args.quiescence):
        parser.error("--into and --quiescence are required")
    try:
        evidence = _evidence(args.quiescence, args.quiescence_sha256, (project,))
        v2._boundary(evidence, (Path(project),))
    except (OSError, ValueError, KeyError, TypeError, RecoveryError):
        return _emit("QUIESCENCE_REFUSED")
    try:
        profile = build_profile(project)
        into = Path(os.path.abspath(args.into))
        if Path(project) in (into, *into.parents) or into in Path(project).parents:
            return _emit("BACKUP_REFUSED", reason="destination_must_be_isolated")
        into.mkdir(mode=0o700, exist_ok=True)
        now = datetime.now(timezone.utc)
        target = into / ("set-" + now.strftime("%Y%m%dT%H%M%S%fZ"))
        report = v2.backup_v2(str(target), profile, quiescence=evidence)
        entry = {"set": str(target), "manifest_sha256": report["manifest_sha256"],
                 "evidence_sha256": evidence.evidence_sha256, "at": now.isoformat()}
        fd = os.open(into / DIGESTS, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            os.write(fd, (json.dumps(entry, sort_keys=True) + "\n").encode())
            os.fsync(fd)
        finally:
            os.close(fd)
    except ProfileIncomplete as error:
        return _incomplete(error)
    except RecoveryError as error:
        refused = str(error).startswith(("quiescence", "independent_caller", "utc_boundary"))
        return _emit("QUIESCENCE_REFUSED" if refused else "BACKUP_REFUSED", reason=str(error))
    except (OSError, ValueError, sqlite3.Error):
        return _emit("BACKUP_REFUSED")
    return _emit("OK", files=report["files"], sqlite_snapshots=report["sqlite_snapshots"])


def verify_main(argv=None):
    parser = argparse.ArgumentParser(prog="records backup-verify", description="Verify a set against the trusted list.")
    parser.add_argument("--set", required=True)
    parser.add_argument("--digests", required=True, help="the separately kept backup-digests.jsonl")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    source = os.path.abspath(args.set)
    try:
        entries = [json.loads(line) for line in _read(args.digests, MAX_LIST).decode().splitlines() if line.strip()]
        trusted = [e["manifest_sha256"] for e in entries if type(e) is dict and e.get("set") == source]
    except (OSError, ValueError, KeyError):
        return _emit("DIGESTS_UNREADABLE")
    if not trusted:
        return _emit("DIGEST_NOT_TRUSTED")
    try:
        report = v2.verify_v2(source, manifest_sha256=trusted[-1])
    except RecoveryError as error:
        return _emit("VERIFY_FAILED", reason=str(error))
    except (OSError, ValueError, sqlite3.Error):
        return _emit("VERIFY_FAILED")
    return _emit("OK", files=report["files"], sqlite_snapshots=report["sqlite_snapshots"])


def _check(into, max_age_days, now):
    """(code, fields) for the newest digest line; read-only on ``into``."""
    try:
        raw = _read(into / DIGESTS, MAX_LIST)
    except FileNotFoundError:
        return "BACKUP_NONE", {}
    except (OSError, ValueError):
        return "DIGESTS_INVALID", {}
    try:
        entries = [json.loads(line) for line in raw.decode().splitlines() if line.strip()]
        if not entries:
            return "BACKUP_NONE", {}
        newest = max(entries, key=lambda e: datetime.fromisoformat(e["at"]))
        taken = datetime.fromisoformat(newest["at"])
        name, digest = os.path.basename(newest["set"]), newest["manifest_sha256"]
        if (not name.startswith("set-") or taken.tzinfo is None or type(digest) is not str
                or not all(type(e) is dict for e in entries)):
            raise ValueError("entry")
    except (ValueError, KeyError, TypeError, UnicodeDecodeError):
        return "DIGESTS_INVALID", {}
    try:
        report = v2.verify_v2(str(into / name), manifest_sha256=digest)
    except (RecoveryError, OSError, ValueError, sqlite3.Error):
        return "BACKUP_VERIFY_FAILED", {}
    fields = {"files": report["files"], "sqlite_snapshots": report["sqlite_snapshots"]}
    if now - taken > timedelta(days=max_age_days):
        return "BACKUP_STALE", fields
    return "BACKUP_OK", fields


def check_main(argv=None):
    parser = argparse.ArgumentParser(prog="records backup-check", description="Verify the newest backup set; alert if stale or bad.")
    parser.add_argument("--into", required=True)
    parser.add_argument("--max-age-days", type=int, default=8)
    parser.add_argument("--alerts-dir", help="default: <into>-alerts beside the backup dir (never inside it)")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    into = Path(os.path.abspath(args.into))
    now = datetime.now(timezone.utc)
    code, fields = _check(into, args.max_age_days, now)
    alerts = Path(os.path.abspath(args.alerts_dir)) if args.alerts_dir else into.with_name(into.name + "-alerts")
    if code != "BACKUP_OK" or alerts.is_dir():  # an OK run only clears an earlier alert, it creates nothing
        state = "ok" if code == "BACKUP_OK" else "stop"
        report = {"now": now.isoformat(), "checks": [{"check": "backup", "state": state, "reason_code": code.lower()}]}
        try:
            pipeline_health.write_alerts(report, alerts)
        except (pipeline_health.HealthError, OSError):
            fields["alert"] = "unwritten"
    _emit(code, **fields)
    return 0 if code == "BACKUP_OK" else 1
