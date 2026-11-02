"""Offline-only campaign setup, evidence intake, and public starter generation."""
import argparse
import hashlib
import html
import json
import os
import re
import sqlite3
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import fixes

MAX_BYTES = 50 * 1024 * 1024
MAX_DIR_FILES = 2000
OUTCOMES = ("stored", "present", "symlink", "not_regular", "too_large", "hidden")


def now():
    return datetime.now(timezone.utc).isoformat()


def read_config(root):
    cfg = json.loads((root / "campaign.json").read_text(encoding="utf-8"))
    if cfg.get("schema_version") != 1:
        raise ValueError("Unsupported campaign schema version")
    for key in ("name", "county", "state"):
        if not isinstance(cfg.get(key), str) or not cfg[key].strip():
            raise ValueError("Missing campaign field: " + key)
    if not re.fullmatch(r"[A-Z]{2}", cfg["state"]):
        raise ValueError("Use a two-letter state code; jurisdiction is not auto-verified")
    return cfg


def database(root):
    private = root / "private"
    private.mkdir(mode=0o700, exist_ok=True)
    os.chmod(private, 0o700)
    path = private / "ledger.sqlite"
    db = sqlite3.connect(path)
    os.chmod(path, 0o600)
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS objects (
            sha256 TEXT PRIMARY KEY, byte_count INTEGER NOT NULL,
            stored_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS receipts (
            receipt_id TEXT PRIMARY KEY, object_sha256 TEXT NOT NULL
                REFERENCES objects(sha256),
            source_id TEXT NOT NULL, received_at TEXT NOT NULL,
            original_name TEXT NOT NULL
        );
    """)
    return db


def initialize(args, root):
    for field in ('name', 'county'):
        if not getattr(args, field).strip():
            raise ValueError('Missing campaign field: ' + field)
    root.mkdir(parents=True, exist_ok=True)
    cfg = {"schema_version": 1, "name": args.name, "county": args.county,
           "state": args.state.upper(), "country": "US",
           "jurisdiction_verified": False, "law_package_status": "unreviewed",
           "external_sends": "disabled", "publication": "manual",
           "newsletter": {"mode": "not_configured"}}
    if getattr(args, "location", None) and args.location.strip():
        cfg["location_query"] = args.location.strip()
    if not re.fullmatch(r"[A-Z]{2}", cfg["state"]):
        raise ValueError("State must be a two-letter code")
    with (root / "campaign.json").open("x", encoding="utf-8") as out:
        json.dump(cfg, out, indent=2)
        out.write("\n")
    (root / "private").mkdir(mode=0o700, exist_ok=True)
    print("Campaign initialized. No accounts created, messages sent, or laws verified.")


def _ingest_one(db, root, path, source_id, dir_fd=None):
    """Store one regular file (no link followed) in its own transaction; returns the --file JSON plus outcome.

    With dir_fd, `path` is a bare name opened relative to that verified directory descriptor."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    except OSError:
        if dir_fd is None:
            raise
        raise ValueError("Input changed or is not a regular, non-symlink file") from None
    with os.fdopen(fd, "rb") as incoming:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("Input must be a regular, non-symlink file")
        data = incoming.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("Alpha limit: 50 MiB per file; original was not imported")
    digest = hashlib.sha256(data).hexdigest()
    identity = hashlib.sha256(json.dumps(
        [source_id, digest], separators=(",", ":")).encode()).hexdigest()
    with db:  # one transaction per file; the object is written before its rows
        objects = root / "private" / "objects"
        objects.mkdir(mode=0o700, exist_ok=True)
        target = objects / digest
        if target.exists():
            if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                raise ValueError("Stored object integrity mismatch; intake stopped")
        else:
            # Write a same-directory temporary object before an atomic rename.
            import tempfile
            fd, temporary = tempfile.mkstemp(dir=objects, prefix=".intake-")
            try:
                with os.fdopen(fd, "wb") as output:
                    output.write(data)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, target)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        new_object = db.execute("INSERT OR IGNORE INTO objects VALUES (?, ?, ?)",
                                (digest, len(data), now())).rowcount == 1
        cursor = db.execute("INSERT OR IGNORE INTO receipts VALUES (?, ?, ?, ?, ?)",
                            (identity, digest, source_id, now(), os.path.basename(path)))
    return {"sha256": digest, "receipt_id": identity, "new_receipt": cursor.rowcount == 1,
            "analysis_status": "not_started", "outcome": "stored" if new_object else "present"}


def ingest(args, root):
    read_config(root)
    if getattr(args, "dir", None) is not None:
        return _ingest_dir(args, root)
    source = Path(args.file)
    if source.is_symlink() or not source.is_file():
        raise ValueError("Input must be a regular, non-symlink file")
    if source.stat().st_size > MAX_BYTES:
        raise ValueError("Alpha limit: 50 MiB per file; original was not imported")
    db = database(root)
    try:
        result = _ingest_one(db, root, source, args.source_id)
    finally:
        db.close()
    print(json.dumps({k: result[k] for k in ("sha256", "receipt_id", "new_receipt", "analysis_status")}, indent=2))


def _open_dir(top_fd, parts):
    """Descriptor for top/parts..., every component opened relative to its verified parent without following links."""
    fd = os.dup(top_fd)
    try:
        for part in parts:
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except OSError:
                raise ValueError("Folder changed during the run (not a real directory)") from None
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _open_root(path):
    """Open --dir without following a symlink, even when written as link/ or link/. (O_NOFOLLOW only
    covers the final component, so the last real name is opened relative to its parent descriptor)."""
    text = os.fspath(path)
    # Drop empty and "." components only; ".." is kept so valid parent-relative paths (../production-001)
    # still work. Symlinks in the parent chain are ordinary path resolution; only the final name is guarded.
    parts = [p for p in text.split("/") if p not in ("", ".")]
    if not parts:
        return os.open("/" if text.startswith("/") else ".", os.O_RDONLY | os.O_DIRECTORY)
    name = parts[-1]
    parent = ("/" if text.startswith("/") else "") + "/".join(parts[:-1])
    parent_fd = os.open(parent or ".", os.O_RDONLY | os.O_DIRECTORY)
    try:
        return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
    finally:
        os.close(parent_fd)


def _ingest_dir(args, root):
    """One production folder: list it without following links (hidden names skipped), then ingest file by file.

    All traversal and opens are anchored to directory descriptors, so a subfolder swapped for a link
    after listing is refused rather than followed."""
    entries, stack = [], [()]
    try:
        top_fd = _open_root(args.dir)
    except OSError:
        raise ValueError("--dir must be a real directory, not a symlink or a file") from None
    try:
        while stack:
            parts = stack.pop()
            folder_fd = _open_dir(top_fd, parts)
            try:
                with os.scandir(folder_fd) as listing:
                    for entry in listing:
                        hidden = entry.name.startswith(".")
                        if entry.is_dir(follow_symlinks=False) and not hidden:
                            stack.append(parts + (entry.name,))
                            continue
                        entries.append(("/".join(parts + (entry.name,)), parts, entry.name,
                                        "hidden" if hidden else "symlink" if entry.is_symlink()
                                        else "not_regular" if not entry.is_file(follow_symlinks=False)
                                        else "too_large" if entry.stat(follow_symlinks=False).st_size > MAX_BYTES else None))
                        if len(entries) > MAX_DIR_FILES:
                            raise ValueError(f"{args.dir}: more than {MAX_DIR_FILES} files; split the folder")
            finally:
                os.close(folder_fd)
        files, db = [], database(root)
        try:
            for rel, parts, name, code in sorted(entries):
                try:
                    if code:
                        result = {"sha256": None, "new_receipt": False}
                    else:
                        folder_fd = _open_dir(top_fd, parts)
                        try:
                            result = _ingest_one(db, root, name, args.source_id, dir_fd=folder_fd)
                        finally:
                            os.close(folder_fd)
                except ValueError as exc:
                    raise ValueError(f"{exc}: {rel}") from None
                files.append({"path": rel, "sha256": result["sha256"] and result["sha256"][:12],
                              "outcome": code or result["outcome"], "new_receipt": result["new_receipt"]})
        finally:
            db.close()
    finally:
        os.close(top_fd)
    print(json.dumps({"dir": os.path.basename(os.path.abspath(args.dir)), "files": files,
                      "counts": {k: sum(f["outcome"] == k for f in files) for k in OUTCOMES}}, indent=2))


def status(root, check=False):
    read_config(root)
    from .store_check import check_store
    result = check_store(root) if check else None
    status_counts(root)
    if result is not None:
        print(json.dumps({"check": result}, indent=2))
        if result["problems"]:
            raise SystemExit(3)


def status_counts(root):
    path = root / "private" / "ledger.sqlite"
    if not path.exists():
        print(json.dumps({"stored_objects": 0, "receipt_occurrences": 0,
                          "substantive_review": "not_tracked_in_alpha"}))
        return
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        print(json.dumps({
            "stored_objects": db.execute("SELECT count(*) FROM objects").fetchone()[0],
            "receipt_occurrences": db.execute("SELECT count(*) FROM receipts").fetchone()[0],
            "substantive_review": "not_tracked_in_alpha"}, indent=2))
    finally:
        db.close()


def law_package_report(cfg):
    """Status of the jurisdiction law package: draft, reviewed or missing."""
    from .law import package_status
    jurisdiction = (str(cfg.get("country", "US")).lower() + "-" + cfg["state"].lower())
    status, error = package_status(jurisdiction)
    report = {"law_package_status": status, "reviewed_law_package": status == "reviewed"}
    if error:
        report["law_package_error"] = error
    return report


def doctor(root, contact_max_age_days=None):
    print(json.dumps(doctor_report(root, contact_max_age_days), indent=2))


def doctor_report(root, contact_max_age_days=None):
    cfg = read_config(root)
    law = law_package_report(cfg)
    report = {
        "config_readable": True,
        "jurisdiction_verified": cfg.get("jurisdiction_verified") is True,
        "reviewed_law_package": law["reviewed_law_package"],
        "law_package_status": law["law_package_status"],
        "public_deployment_checked": False,
        "newsletter_checked": False,
        "safe_to_send_automatically": False,
        "production_ready": False,
        "next_actions": [
            "Verify county/state and official agency contacts.",
            "Obtain independently reviewed event-date laws and local policies.",
            "Follow docs/CLOUDFLARE.md before publishing a live campaign.",
            "Configure and test signup and suppression with one approved address."
        ]
    }
    if "law_package_error" in law:
        report["law_package_error"] = law["law_package_error"]
    from .kit import kit_status, stale_contacts
    report.update(kit_status(root))
    if contact_max_age_days is not None and (found := stale_contacts(root, contact_max_age_days)) is not None:
        report.update(stale_contacts=found[:100], **({"stale_contacts_more": len(found) - 100} if found[100:] else {}))
        report["next_actions"] += [f"Re-check {len(found)} agency contact(s) on their official sites (kit-verify-agency)."] * bool(found)
    from .meeting_stage import stale_kits
    stale = report["stale_comment_kits"] = stale_kits(root)
    if stale:
        report["next_actions"].append("Re-stage meetings with corrected findings: meetings-stage --id "
                                      + ", ".join(sorted({e["meeting"] for e in stale})) + " --replace")
    return report


def kit(args, root):
    from .kit import build_kit
    summary = build_kit(root, online=args.online, include=args.include)
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    print("Kit written to " + str(root / "kit") + ". Nothing was sent.", file=sys.stdout)


def social_cmd(root):
    from .social import build_social
    print(json.dumps(build_social(root), indent=2, ensure_ascii=False))
    print("Drafts written to kit/social. Nothing was posted.")


def check_public_tree(public):
    """Run the repository leak scan over public/; returns (path, line, label) hits."""
    from .records.public_scan import violations
    hits = []
    for path in sorted(public.rglob("*")):
        if path.is_symlink():
            hits.append((str(path.relative_to(public)), 0, "symlink_not_scanned"))
        elif path.is_file():
            hits.extend(violations(str(path.relative_to(public)), path.read_bytes()))
    return hits


def build(root, check=False):
    from .site import build_site
    public = root / "public"
    written = build_site(root)
    content_mode = (root / "content").is_dir()
    print(("Site" if content_mode else "Starter") + " written to " + str(public)
          + ". No private documents exported.")
    total = 0
    for path in sorted(written):
        size = path.stat().st_size
        total += size
        print(f"  {path.relative_to(public).as_posix():<40} {size:>9} bytes")
    print(f"  {len(written)} files, {total} bytes")
    if check:
        hits = check_public_tree(public)
        for path, line, label in hits:
            print(f"public/{path}:{line}: {label}", file=sys.stderr)
        if hits:
            raise ValueError(f"public tree check found {len(hits)} potential leak(s); fix content and rebuild")
        print("Public tree check passed (pattern scan only; human review still required).")


def backup_cmd(args, root):
    from .backup import export
    manifest = export(root, args.out)
    print(json.dumps({"out": str(args.out), "files": manifest["counts"]["files"], "bytes": manifest["counts"]["bytes"],
                      "objects": manifest["counts"].get("objects", 0), "engine_version": manifest["engine_version"],
                      "manifest_sha256": manifest["manifest_sha256"]},
                     indent=2))
    print("Archive is unencrypted; encrypt it before storing it anywhere shared.", file=sys.stdout)
    print("Record manifest_sha256 somewhere other than with the archive and pass it to "
          "`verify --expect-manifest-sha256`: without it verify checks integrity, not tampering.", file=sys.stdout)


def restore_cmd(args, root):
    from .backup import restore
    report = restore(args.file, root)
    print(json.dumps({"restored_to": report["restored_to"], "files": report["files"],
                      "engine_version": report["engine_version"], "created_at": report["created_at"]}, indent=2))


def verify_cmd(args):
    from .backup import verify
    report = verify(args.file, expected_manifest_sha256=args.expect_manifest_sha256)
    if not args.expect_manifest_sha256:
        report["note"] = "integrity check only; pass --expect-manifest-sha256 <digest from backup> to detect a rewritten archive"
    print(json.dumps(report, indent=2))


def backup_drill_cmd(args):
    from .backup import drill
    if args.receipt and os.path.lexists(args.receipt):
        raise ValueError(f"{args.receipt} exists; the drill never overwrites a receipt")
    receipt = drill(args.file, args.expect_manifest_sha256, doctor=doctor_report)
    text = json.dumps(receipt, indent=2)
    if args.receipt:
        with os.fdopen(os.open(args.receipt, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as out:
            out.write(text + "\n")
    print(text)
    if not receipt["passed"]:
        sys.exit(3)


def backup_list_cmd(args):
    from .backup import list_archives
    report = list_archives(args.dir, args.keep_daily, args.keep_weekly)
    print(json.dumps(report, indent=2) + "\nReport only; nothing was deleted.")
    sys.exit(3 if any(item["verify"] == "failed" for item in report["archives"]) else 0)


def backup_diff_cmd(args, root):
    from .backup import diff
    report = diff(args.file, root, expected_manifest_sha256=args.expect_manifest_sha256)
    print(json.dumps(report, indent=2))
    if not report["current"]:
        sys.exit(3)


def meetings_cmd(args, root):
    from datetime import date
    from .meetings import LegistarClient, compare_meetings, default_opener, fixture_opener, relevant_events, resolve_zone, write_meetings
    zone = resolve_zone(args.tz)
    if zone is None:
        print(f"meetings: unknown time zone {args.tz}", file=sys.stderr)
        raise SystemExit(2)
    read_config(root)
    if args.from_json:
        opener = fixture_opener(json.loads(Path(args.from_json).read_text(encoding="utf-8")))
    elif args.online:
        opener = default_opener
    else:
        raise ValueError("meetings needs --online to query the Legistar Web API, or --from-json with a recorded response")
    client = LegistarClient(args.client, opener=opener)
    since = date.fromisoformat(args.since) if args.since else None
    meetings = relevant_events(client, since=since, zone=zone)
    previous = _read_previous(root / "kit" / "meetings.json")
    changes = ({"previous": previous} if previous == "unreadable"
               else compare_meetings(previous, meetings, args.client, since or date.today()))
    target = write_meetings(root, meetings, args.client, None if previous is None else changes, zone=zone)
    report = {"client": args.client, "matched_meetings": len(meetings), "written": str(target), "previous": changes["previous"],
              "cancelled_meetings": sum(m["status"] == "cancelled" for m in meetings)}
    if "added" in changes:
        report["changes"] = {k: len(changes[k]) for k in ("added", "removed", "rescheduled", "agenda_changed", "past")}
    print(json.dumps(dict(report, note="keyword hits only; verify the posted agenda before publishing"), indent=2))


def _read_previous(path):
    """Previous kit/meetings.json: None if absent; "unreadable" for a symlink, > 4 MiB, non-UTF-8 or non-JSON."""
    try:
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as handle:
            data = handle.read(4 * 1024 * 1024 + 1)
        return (json.loads(data.decode("utf-8")) or "unreadable") if len(data) <= 4 * 1024 * 1024 else "unreadable"
    except FileNotFoundError:
        return None
    except (OSError, ValueError, RecursionError):
        return "unreadable"


def meeting_stage_cmd(args, root):
    from .meeting_stage import stage
    print(json.dumps(stage(root, args.id, args.agency_id, args.location, args.replace, args.participation),
                     indent=2))


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "ingest", "doctor", "status", "build"):
        cmd = commands.add_parser(name)
        cmd.add_argument("--directory", required=True)
        if name == "init":
            cmd.add_argument("--name", required=True)
            cmd.add_argument("--county", required=True)
            cmd.add_argument("--state", required=True)
            cmd.add_argument("--location", help="City or county to resolve (default: the county)")
        if name == "ingest":
            source = cmd.add_mutually_exclusive_group(required=True)
            source.add_argument("--file")
            source.add_argument("--dir", help="One local production folder; symlinks are never followed")
            cmd.add_argument("--source-id", required=True,
                             help="Stable non-secret production/message identity, not a signed URL")
        if name == "build":
            cmd.add_argument("--check", action="store_true",
                             help="Scan the generated public/ tree for private paths and credential patterns")
        if name == "status":
            cmd.add_argument("--check", action="store_true", help="Re-hash private/objects against the ledger (read-only)")
        if name == "doctor":
            cmd.add_argument("--contact-max-age-days", type=int, metavar="N", help="Name verified kit contacts checked over N days ago")
            cmd.add_argument("--checklist", action="store_true", help="Print each check as ok/todo with its next step and owner")
            cmd.add_argument("--json", action="store_true", help="With --checklist: print the checklist rows as JSON")
    kit_cmd = commands.add_parser("kit", help="Suggest agencies and draft records requests offline")
    kit_cmd.add_argument("--directory", required=True)
    kit_cmd.add_argument("--online", action="store_true",
                         help="Fall back to the Census geocoder when the seed cannot resolve the location")
    kit_cmd.add_argument("--include", action="append",
                         help="Agency kind to include (repeatable): sheriff, police, county_board, "
                              "city_council, district_attorney, chp")
    commands.add_parser("social", help="Draft one private post per published finding in kit/social; posts nothing"
                        ).add_argument("--directory", required=True)
    backup_p = commands.add_parser("backup", help="Write a verified tar of the campaign directory")
    backup_p.add_argument("--directory", required=True)
    backup_p.add_argument("--out", required=True, help="Archive path to create (must not exist)")
    restore_p = commands.add_parser("restore", help="Restore a backup into an empty directory")
    restore_p.add_argument("--file", required=True)
    restore_p.add_argument("--directory", required=True)
    verify_p = commands.add_parser("verify", help="Re-hash every member of a backup against its manifest")
    verify_p.add_argument("--file", required=True)
    verify_p.add_argument("--expect-manifest-sha256", default=None, help="manifest digest printed by `backup`, kept apart from the archive")
    diff_p = commands.add_parser("backup-diff", help="List files added/removed/changed since a backup (read-only; exit 3 on drift)")
    diff_p.add_argument("--file", required=True)
    diff_p.add_argument("--directory", required=True)
    diff_p.add_argument("--expect-manifest-sha256", default=None, help="manifest digest printed by `backup`")
    drill_p = commands.add_parser("backup-drill", help="Restore a backup into a temp copy, check it there, remove it (exit 3 on fail)")
    drill_p.add_argument("--file", required=True)
    drill_p.add_argument("--expect-manifest-sha256", default=None, help="manifest digest printed by `backup`")
    drill_p.add_argument("--receipt", help="Also write the receipt JSON here (must not exist)")
    list_p = commands.add_parser("backup-list", help="Re-verify every *.tar in a folder; report what a keep rule keeps (deletes nothing)")
    list_p.add_argument("--dir", required=True)
    from .backup import KEEP_LIMITS, keep_count
    for flag, key, period in (("--keep-daily", "keep_daily", "UTC day"), ("--keep-weekly", "keep_weekly", "ISO week")):
        list_p.add_argument(flag, type=keep_count(KEEP_LIMITS[key]), help=f"Newest per {period}, most recent N (0-{KEEP_LIMITS[key]})")
    meetings_p = commands.add_parser("meetings", help="Find upcoming ALPR agenda items on Legistar; writes kit/meetings.json")
    meetings_p.add_argument("--directory", required=True)
    meetings_p.add_argument("--client", required=True, help="Legistar client slug (the part after webapi.legistar.com/v1/)")
    meetings_p.add_argument("--online", action="store_true", help="Query the Legistar Web API (the only network use)")
    meetings_p.add_argument("--from-json", help="Recorded responses keyed by URL path (offline)")
    meetings_p.add_argument("--since", help="Earliest event date, YYYY-MM-DD (default today)")
    meetings_p.add_argument("--tz", default="America/Los_Angeles", help="IANA time zone for starts_at (default America/Los_Angeles)")
    stage_p = commands.add_parser("meetings-stage", help="Write content/meetings/<id>.json from one kit/meetings.json entry")
    stage_p.add_argument("--directory", required=True)
    stage_p.add_argument("--id", required=True, help="Meeting id from kit/meetings.json")
    stage_p.add_argument("--agency-id", help="Cite only published findings for this agency")
    stage_p.add_argument("--location")
    stage_p.add_argument("--participation", help="Organizer-verified participation JSON (docs/MEETINGS.md)")
    stage_p.add_argument("--replace", action="store_true", help="Overwrite an existing content/meetings/<id>.json")
    commands.add_parser("content-check", help="List every content/ problem build would refuse (read-only; exit 3)"
                        ).add_argument("--directory", required=True)
    evidence_p = commands.add_parser("evidence", help="Check that published findings' cited records are registered and stored (read-only)")
    evidence_p.add_argument("--directory", required=True)
    evidence_p.add_argument("--check", action="store_true", help="Exit 3 when any citation has a problem")
    verify_agency_p = commands.add_parser("kit-verify-agency", help="Record an organizer-checked records contact in kit/agencies.json")
    for flag in ("--directory", "--agency-id", "--source-url", "--checked-on"):
        verify_agency_p.add_argument(flag, required=True)
    for flag in ("--records-email", "--records-url", "--portal"):
        verify_agency_p.add_argument(flag, help="Value the organizer checked on the official page (never fetched)")
    agency_cmds = commands.add_parser("kit-agency", help="Add, select or deselect agencies in kit/agencies.json; the next"
                                      " kit run drafts requests for selected law-enforcement agencies"
                                      ).add_subparsers(dest="agency_command", required=True)
    for name, text in (("select", "Mark one agency selected"), ("deselect", "Mark one agency not selected"),
                       ("add", "Append an organizer-added agency (selected, verified: false)")):
        agency_p = agency_cmds.add_parser(name, help=text, description=text + "; the next kit run drafts requests for"
                                          " selected law-enforcement agencies. Nothing is sent.")
        agency_p.add_argument("--directory", required=True)
        agency_p.add_argument("--id", required=True, dest="agency_id")
        if name == "add":
            agency_p.add_argument("--name", required=True)
            agency_p.add_argument("--kind", required=True, help="sheriff, police, chp, county_board, city_council, "
                                  "district_attorney or other")
            agency_p.add_argument("--note", default=None, help="Up to 300 characters")
    sent_p = commands.add_parser("kit-sent", help="Record a request the organizer sent by hand in kit/sent.json; sends nothing")
    for flag in ("--directory", "--agency", "--date"):
        sent_p.add_argument(flag, required=True)
    sent_p.add_argument("--channel", required=True, choices=("email", "portal", "mail"))
    sent_p.add_argument("--replace", action="store_true", help="Correct an entry that has no recorded steps")
    commands.add_parser("kit-status", help="List recorded requests with provisional due dates (read-only)"
                        ).add_argument("--directory", required=True)
    response_p = commands.add_parser("kit-response", help="Record a dated agency response step on a sent request")
    for flag in ("--directory", "--agency", "--date"):
        response_p.add_argument(flag, required=True)
    response_p.add_argument("--kind", required=True, choices=("acknowledged", "extension", "determination",
                                                               "produced", "denied", "closed"))
    response_p.add_argument("--note", help="Short plain-text note (<= 200 chars; never record contents)")
    sources = commands.add_parser("sources", help="Manage content/sources.json (the public source library)"
                                  ).add_subparsers(dest="sources_command", required=True)
    sources_add = sources.add_parser(
        "add", help="Register one ingested record by hash prefix; validates the whole list, publishes nothing")
    for flag in ("--directory", "--sha256", "--title", "--agency-id", "--received-at", "--fragment"):
        sources_add.add_argument(flag, required=True)
    sources_add.add_argument("--public-url", default=None, help="Recorded only; never fetched or uploaded")
    sources_add.add_argument("--pages", type=int, default=None)
    sources_add.add_argument("--note", default="")
    sources.add_parser("list", help="Print every library entry and the published findings citing it (read-only)"
                       ).add_argument("--directory", required=True)
    sources_edit = sources.add_parser("edit", help="Change title, note, public_url or pages of one entry; publishes nothing")
    sources_remove = sources.add_parser("remove", help="Delete one entry unless a published finding cites its record")
    for sub in (sources_edit, sources_remove):  # entries chosen by >= 12 hex of the library hash (+ --fragment)
        for flag, required in (("--directory", True), ("--sha256", True), ("--fragment", False)):
            sub.add_argument(flag, required=required, default=None)
    for flag in ("--title", "--note", "--public-url"):
        sources_edit.add_argument(flag, default=None)
    sources_edit.add_argument("--clear-public-url", action="store_true")
    sources_edit.add_argument("--pages", type=int, default=None)
    finding_cmds = commands.add_parser("finding", help="Offline finding review receipts (private/reviews/)"
                                       ).add_subparsers(dest="finding_command", required=True)
    review_p = finding_cmds.add_parser("review", help="Record one self-asserted review receipt for a draft")
    for flag in ("--directory", "--role", "--decision", "--reviewer", "--rationale"):
        review_p.add_argument(flag, required=True)
    finding_cmds.add_parser("review-status", help="Print review_blockers for a draft (read-only)"
                            ).add_argument("--directory", required=True)
    for sub in finding_cmds.choices.values():
        sub.add_argument("draft", metavar="DRAFT", help="Finding draft JSON file")
    args = parser.parse_args()
    if getattr(args, "contact_max_age_days", None) is not None and args.contact_max_age_days < 1:
        parser.error("--contact-max-age-days must be at least 1")
    if getattr(args, "json", False) and not args.checklist:
        parser.error("--json requires --checklist")
    if args.command in ("verify", "backup-drill", "backup-list"):
        try:
            {"verify": verify_cmd, "backup-drill": backup_drill_cmd, "backup-list": backup_list_cmd}[args.command](args)
        except (OSError, ValueError) as exc:
            fixes.emit(exc)
            sys.exit(1)
        return
    root = Path(args.directory).expanduser().resolve()
    try:
        if args.command == "init":
            initialize(args, root)
        elif args.command == "ingest":
            ingest(args, root)
        elif args.command == "kit":
            kit(args, root)
        elif args.command == "social":
            social_cmd(root)
        elif args.command == "build":
            build(root, check=args.check)
        elif args.command == "backup":
            backup_cmd(args, root)
        elif args.command == "restore":
            restore_cmd(args, root)
        elif args.command == "backup-diff":
            backup_diff_cmd(args, root)
        elif args.command == "meetings":
            meetings_cmd(args, root)
        elif args.command == "meetings-stage":
            meeting_stage_cmd(args, root)
        elif args.command == "sources" and args.sources_command == "add":
            from .sources_cmd import add_source
            print(json.dumps(add_source(root, sha=args.sha256, title=args.title, agency_id=args.agency_id,
                                        received_at=args.received_at, fragment=args.fragment,
                                        public_url=args.public_url, pages=args.pages, note=args.note)))
        elif args.command == "sources" and args.sources_command == "list":
            from .sources_cmd import list_sources
            print(json.dumps(list_sources(root), indent=2, ensure_ascii=False))
        elif args.command == "sources":
            from .sources_cmd import edit_source, remove_source
            print(json.dumps(remove_source(root, args.sha256, args.fragment) if args.sources_command == "remove" else
                             edit_source(root, args.sha256, args.fragment, title=args.title, note=args.note,
                                         public_url=args.public_url, pages=args.pages, clear_public_url=args.clear_public_url)))
            print("Nothing was published.")
        elif args.command == "finding":
            from . import findings
            read_config(root)
            draft = findings.load_draft(args.draft)
            print(json.dumps(findings.add_receipt(root, draft, args.role, args.decision, args.reviewer, args.rationale)
                             if args.finding_command == "review" else findings.review_status(root, draft), indent=2))
        elif args.command == "content-check":
            from .content_check import run_cli as content_check_cli
            content_check_cli(root)
        elif args.command == "evidence":
            from .evidence import run_cli
            run_cli(root, check=args.check)
        elif args.command == "kit-verify-agency":
            from .kit import verify_agency
            print(json.dumps(verify_agency(root, args.agency_id, source_url=args.source_url, checked_on=args.checked_on,
                                           records_email=args.records_email, records_url=args.records_url,
                                           portal=args.portal), indent=2))
            print("Nothing was sent.")
        elif args.command == "kit-agency":
            from . import kit_agencies
            print(json.dumps(kit_agencies.add_agency(root, args.agency_id, args.name, args.kind, note=args.note)
                             if args.agency_command == "add" else
                             kit_agencies.set_selected(root, args.agency_id, args.agency_command == "select"),
                             indent=2, ensure_ascii=False))
            print("Nothing was sent.")
        elif args.command == "kit-sent":
            from .kit_track import record_sent
            print(json.dumps(record_sent(root, args.agency, args.date, args.channel, replace=args.replace), indent=2))
            print("Recorded only. Nothing was sent.")
        elif args.command == "kit-status":
            from .kit_track import status as kit_track_status
            print(json.dumps(kit_track_status(root), indent=2))
        elif args.command == "kit-response":
            from .kit_track import record_step
            print(json.dumps(record_step(root, args.agency, args.date, args.kind, note=args.note), indent=2))
            print("Recorded only. Nothing was sent.")
        elif args.command == "doctor" and args.checklist:
            from .doctor_checklist import render
            print(render(doctor_report(root, args.contact_max_age_days), args.directory, as_json=args.json))
        else:
            status(root, check=args.check) if args.command == "status" else doctor(root, args.contact_max_age_days)
    except (OSError, ValueError, sqlite3.Error) as exc:
        fixes.emit(exc)
        sys.exit(1)
