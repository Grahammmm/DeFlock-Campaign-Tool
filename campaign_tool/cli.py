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


def doctor(root):
    cfg = read_config(root)
    report = {
        "config_readable": True,
        "jurisdiction_verified": cfg.get("jurisdiction_verified") is True,
        "reviewed_law_package": False,
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
    print(json.dumps(report, indent=2))


def build(root):
    cfg = read_config(root)
    # Only these fields are exported. Evidence and arbitrary config never enter HTML.
    name, county, state = (html.escape(cfg[k], quote=True)
                           for k in ("name", "county", "state"))
    public = root / "public"
    public.mkdir(exist_ok=True)
    page = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="description" content="A local initiative for transparent ALPR policies and evidence-based public participation.">
<title>__NAME__ | Local accountability</title><link rel="stylesheet" href="style.css">
</head><body><header><a href="#main">__NAME__</a><span>__COUNTY__, __STATE__</span></header>
<main id="main"><p class="eyebrow">Local records. Public understanding.</p>
<h1>Know how your community uses license plate readers.</h1>
<p class="intro">We are building a source-backed picture of ALPR policies, contracts,
and oversight in __COUNTY__. Questions deserve records, not assumptions.</p>
<nav aria-label="Sections"><a href="#records">Our process</a><a href="#participate">Get involved</a></nav>
<section id="records"><h2>From records to understanding</h2>
<div class="grid"><article><span>01</span><h3>Request</h3><p>Identify agencies and ask for policies, agreements, sharing settings, and audit records.</p></article>
<article><span>02</span><h3>Understand</h3><p>Preserve originals, compare applicable rules, and independently challenge every material finding.</p></article>
<article><span>03</span><h3>Participate</h3><p>Share reviewed evidence and use official public-meeting channels to ask informed questions.</p></article></div></section>
<section id="participate"><h2>A campaign built on evidence.</h2><p>This is a starter preview.
Local contacts, meeting links, map data, newsletter signup, and reviewed findings have not yet been configured.</p>
<p>No email addresses are collected by this preview.</p></section></main>
<footer>Independent local initiative. No agency findings are asserted by this starter.
Built with DeFlock Campaign Tool.</footer></body></html>"""
    for key, value in (("__NAME__", name), ("__COUNTY__", county), ("__STATE__", state)):
        page = page.replace(key, value)
    (public / "index.html").write_text(page, encoding="utf-8")
    css = """*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;color:#142e2d;background:#f4f0e5;font:18px/1.6 Georgia,serif}
header,main,footer{max-width:1160px;margin:auto;padding:24px}header{display:flex;justify-content:space-between;gap:16px;border-bottom:1px solid #b3beb0}
a{color:inherit;text-underline-offset:5px}header a{font-weight:bold}main{padding-top:72px;background:radial-gradient(ellipse at top right,#dce5ce,transparent 65%)}
.eyebrow,article span{font:14px/1.4 monospace;text-transform:uppercase;letter-spacing:2px;color:#426952}
h1{font-size:clamp(2.4rem,6vw,5rem);line-height:1.04;max-width:950px;font-weight:normal;letter-spacing:-2px;margin:28px 0}
.intro{max-width:670px;font-size:22px}nav{display:flex;gap:24px;flex-wrap:wrap;margin:32px 0}
nav a{background:#183e36;color:#fff;padding:12px 20px;text-decoration:none;border-radius:4px}
section{padding:40px 0}.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:28px}article{border-top:3px solid #b95436;padding-top:18px}
h2{font-size:32px}h3{margin:12px 0}footer{font-size:14px;border-top:1px solid #b3beb0}
a:focus-visible{outline:3px solid #b95436;outline-offset:5px}@media(max-width:640px){header{flex-direction:column}main{padding-top:36px}.grid{grid-template-columns:1fr}h1{letter-spacing:-1px}.intro{font-size:19px}}
@media(prefers-reduced-motion:reduce){html{scroll-behavior:auto}}"""
    (public / "style.css").write_text(css, encoding="utf-8")
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
        if name == "ingest":
            cmd.add_argument("--file", required=True)
            cmd.add_argument("--source-id", required=True,
                             help="Stable non-secret production/message identity, not a signed URL")
    args = parser.parse_args()
    root = Path(args.directory).expanduser().resolve()
    try:
        if args.command == "init":
            initialize(args, root)
        elif args.command == "ingest":
            ingest(args, root)
        else:
            {"doctor": doctor, "status": status, "build": build}[args.command](root)
    except (OSError, ValueError, sqlite3.Error) as exc:
        print("Stopped: " + str(exc), file=sys.stderr)
        sys.exit(1)
