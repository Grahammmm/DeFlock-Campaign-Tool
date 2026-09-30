"""Offline-only campaign setup, evidence intake, and public starter generation."""
import argparse
import hashlib
import html
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

MAX_BYTES = 50 * 1024 * 1024


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


def ingest(args, root):
    read_config(root)
    source = Path(args.file)
    if source.is_symlink() or not source.is_file():
        raise ValueError("Input must be a regular, non-symlink file")
    with source.open("rb") as incoming:
        data = incoming.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("Alpha limit: 50 MiB per file; original was not imported")
    digest = hashlib.sha256(data).hexdigest()
    identity = hashlib.sha256(json.dumps(
        [args.source_id, digest], separators=(",", ":")).encode()).hexdigest()
    db = database(root)
    try:
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
        with db:
            db.execute("INSERT OR IGNORE INTO objects VALUES (?, ?, ?)",
                       (digest, len(data), now()))
            cursor = db.execute("INSERT OR IGNORE INTO receipts VALUES (?, ?, ?, ?, ?)",
                                (identity, digest, args.source_id, now(), source.name))
        print(json.dumps({"sha256": digest, "receipt_id": identity,
                          "new_receipt": cursor.rowcount == 1,
                          "analysis_status": "not_started"}, indent=2))
    finally:
        db.close()


def status(root):
    read_config(root)
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


def doctor(root):
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
    from .kit import kit_status
    report.update(kit_status(root))
    print(json.dumps(report, indent=2))


def kit(args, root):
    from .kit import build_kit
    summary = build_kit(root, online=args.online, include=args.include)
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    print("Kit written to " + str(root / "kit") + ". Nothing was sent.", file=sys.stdout)


def build(root):
    from .site import preview
    cfg = read_config(root)
    site = preview(cfg)
    public = root / "public"
    public.mkdir(exist_ok=True)
    (public / "index.html").write_text(site["html"], encoding="utf-8")
    (public / "style.css").write_text(site["css"], encoding="utf-8")
    (public / "_headers").write_text(
        "/*\n  X-Content-Type-Options: nosniff\n  Referrer-Policy: no-referrer\n"
        "  Content-Security-Policy: default-src 'none'; style-src 'self'; "
        "base-uri 'none'; form-action 'none'; frame-ancestors 'none'\n", encoding="utf-8")
    print("Starter written to " + str(public) + ". No private documents exported.")


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
            cmd.add_argument("--file", required=True)
            cmd.add_argument("--source-id", required=True,
                             help="Stable non-secret production/message identity, not a signed URL")
    kit_cmd = commands.add_parser("kit", help="Suggest agencies and draft records requests offline")
    kit_cmd.add_argument("--directory", required=True)
    kit_cmd.add_argument("--online", action="store_true",
                         help="Fall back to the Census geocoder when the seed cannot resolve the location")
    kit_cmd.add_argument("--include", action="append",
                         help="Agency kind to include (repeatable): sheriff, police, county_board, "
                              "city_council, district_attorney, chp")
    args = parser.parse_args()
    root = Path(args.directory).expanduser().resolve()
    try:
        if args.command == "init":
            initialize(args, root)
        elif args.command == "ingest":
            ingest(args, root)
        elif args.command == "kit":
            kit(args, root)
        else:
            {"doctor": doctor, "status": status, "build": build}[args.command](root)
    except (OSError, ValueError, sqlite3.Error) as exc:
        print("Stopped: " + str(exc), file=sys.stderr)
        sys.exit(1)
