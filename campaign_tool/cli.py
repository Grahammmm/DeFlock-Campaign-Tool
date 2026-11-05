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
from datetime import date, datetime, timezone
from pathlib import Path

from . import fixes, registry
from .agency_actions import ACTION_KINDS
from .supporters import HOWS as SUPPORTER_HOWS, KINDS as SUPPORTER_KINDS

MAX_BYTES = 50 * 1024 * 1024
MAX_DIR_FILES = 2000
OUTCOMES = ("stored", "present", "symlink", "not_regular", "too_large", "hidden")


def now():
    return datetime.now(timezone.utc).isoformat()


def read_config(root):
    cfg = json.loads((root / "campaign.json").read_text(encoding="utf-8"))
    if not isinstance(cfg, dict) or cfg.get("schema_version") != 1:
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
        CREATE TABLE IF NOT EXISTS contains (
            container_sha256 TEXT NOT NULL REFERENCES objects(sha256),
            member_sha256 TEXT NOT NULL REFERENCES objects(sha256),
            member_path TEXT NOT NULL,
            PRIMARY KEY (container_sha256, member_path)
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
    from .findings import _ensure_campaign_id
    _ensure_campaign_id(root)  # a new campaign binds its review receipts from birth (private/campaign-id.json)
    print("Campaign initialized. No accounts created, messages sent, or laws verified.")


def _read_checked(path, dir_fd=None):
    """Read one regular file (no link followed) within the alpha limit; returns (data, sha256 hex).

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
    return data, hashlib.sha256(data).hexdigest()


def _receipt_id(source_id, digest):
    return hashlib.sha256(json.dumps([source_id, digest], separators=(",", ":")).encode()).hexdigest()


def _ledger_lookup(root):
    """Dry run: (known(digest, receipt_id) -> (object_known, receipt_known), close) over the ledger and this run.

    SQLite never opens the ledger file: even mode=ro creates -wal/-shm sidecars for a WAL-mode
    database. The bytes are read once (no link followed) and queried from an in-memory copy.
    A journal or WAL sidecar means the file alone may not hold the committed state: refused."""
    path, seen, db = root / "private" / "ledger.sqlite", set(), None
    if os.path.lexists(path) or path.parent.is_symlink():  # absent ledger: nothing stored yet
        if any(os.path.lexists(f"{path}-{suffix}") for suffix in ("journal", "wal", "shm")):
            raise ValueError("ledger has a journal or WAL file beside it; nothing was written")
        try:
            if path.parent.is_symlink():
                raise OSError
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise OSError
                with os.fdopen(os.dup(fd), "rb") as handle:
                    data = bytearray(handle.read())
            finally:
                os.close(fd)
            if data[:16] == b"SQLite format 3\0" and data[18:20] == b"\2\2":
                data[18:20] = b"\1\1"  # WAL header, no -wal file: the file is complete; read it as rollback
            db = sqlite3.connect(":memory:")
            db.deserialize(bytes(data))
            db.execute("SELECT count(*) FROM objects JOIN receipts ON 0").fetchone()
        except (OSError, sqlite3.Error):
            db and db.close()
            raise ValueError("ledger is not readable; nothing was written") from None

    def known(digest, identity):
        hits = tuple(key in seen or (db is not None and db.execute(query, (key,)).fetchone() is not None)
                     for key, query in ((digest, "SELECT 1 FROM objects WHERE sha256 = ?"),
                                        (identity, "SELECT 1 FROM receipts WHERE receipt_id = ?")))
        seen.update((digest, identity))
        return hits
    return known, (db.close if db is not None else lambda: None)


def _preview_one(known, path, source_id, dir_fd=None):
    """Dry run of _ingest_one: same read and hashing, outcome would_store/present, nothing written."""
    digest = _read_checked(path, dir_fd)[1]
    object_known, receipt_known = known(digest, identity := _receipt_id(source_id, digest))
    return {"sha256": digest, "receipt_id": identity, "new_receipt": not receipt_known,
            "analysis_status": "not_started", "outcome": "present" if object_known else "would_store"}


def _ingest_one(db, root, path, source_id, dir_fd=None):
    """Store one regular file (no link followed) in its own transaction; returns the --file JSON plus outcome."""
    return _store_bytes(db, root, _read_checked(path, dir_fd)[0], source_id, os.path.basename(path))


def _store_bytes(db, root, data, source_id, name, contained_in=None):
    """Store bytes under their hash with one receipt named `name` (and, for an archive member, its
    `contains` row given as (container_sha256, member_path)) in one transaction."""
    digest = hashlib.sha256(data).hexdigest()
    identity = _receipt_id(source_id, digest)
    with db:  # one transaction per object; the object is written before its rows
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
                            (identity, digest, source_id, now(), name))
        if contained_in is not None:
            db.execute("INSERT OR IGNORE INTO contains VALUES (?, ?, ?)", (contained_in[0], digest, contained_in[1]))
    return {"sha256": digest, "receipt_id": identity, "new_receipt": cursor.rowcount == 1,
            "analysis_status": "not_started", "outcome": "stored" if new_object else "present"}


def ingest(args, root):
    read_config(root)
    if getattr(args, "dir", None) is not None:
        return _ingest_dir(args, root)
    if getattr(args, "zip", None) is not None:
        from .ingest_zip import ingest_zip
        return ingest_zip(args, root)
    if getattr(args, "eml", None) is not None:
        from .ingest_eml import ingest_eml
        return ingest_eml(args, root)
    if getattr(args, "mbox", None) is not None:
        from .ingest_mbox import ingest_mbox
        return ingest_mbox(args, root)
    source = Path(args.file)
    if source.is_symlink() or not source.is_file():
        raise ValueError("Input must be a regular, non-symlink file")
    if source.stat().st_size > MAX_BYTES:
        raise ValueError("Alpha limit: 50 MiB per file; original was not imported")
    if getattr(args, "dry_run", False):
        known, close = _ledger_lookup(root)
        try:
            print(json.dumps({**_preview_one(known, source, args.source_id), "dry_run": True}, indent=2))
        finally:
            close()
        print("Dry run: nothing was written.")
        return
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
        dry = getattr(args, "dry_run", False)
        files, (known, close) = [], _ledger_lookup(root) if dry else (None, None)
        db = None if dry else database(root)
        try:
            for rel, parts, name, code in sorted(entries):
                try:
                    if code:
                        result = {"sha256": None, "new_receipt": False}
                    else:
                        folder_fd = _open_dir(top_fd, parts)
                        try:
                            result = (_preview_one(known, name, args.source_id, dir_fd=folder_fd) if dry else
                                      _ingest_one(db, root, name, args.source_id, dir_fd=folder_fd))
                        finally:
                            os.close(folder_fd)
                except ValueError as exc:
                    raise ValueError(f"{exc}: {rel}") from None
                files.append({"path": rel, "sha256": result["sha256"] and result["sha256"][:12],
                              "outcome": code or result["outcome"], "new_receipt": result["new_receipt"]})
        finally:
            close() if dry else db.close()
    finally:
        os.close(top_fd)
    print(json.dumps({"dir": os.path.basename(os.path.abspath(args.dir)), "files": files,
                      "counts": {k: sum(f["outcome"] == k for f in files)
                                 for k in (("would_store",) + OUTCOMES[1:] if dry else OUTCOMES)},
                      **({"dry_run": True} if dry else {})}, indent=2))


def status(root, check=False, fix=False):
    read_config(root)
    from .store_check import check_store, fix_modes
    if fix:
        print(json.dumps(fix_modes(root), indent=2))
    result = check_store(root) if check or fix else None
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


LAW_REVIEW_ACTION = ("reviewed_law_package: false — needs a second reader independent of the author, who checks each "
                     "rule against its cited primary text and signs reviewed_by in a PR (docs/LAW-PACKAGES.md); "
                     "build the packet with 'campaign-tool law review-packet'")


def law_package_report(cfg):
    """Status of the jurisdiction law package: draft, reviewed or missing."""
    from .law import package_status
    jurisdiction = (str(cfg.get("country", "US")).lower() + "-" + cfg["state"].lower())
    status, error = package_status(jurisdiction)
    report = {"law_package_status": status, "reviewed_law_package": status == "reviewed"}
    if error:
        report["law_package_error"] = error
    return report


def doctor(root, contact_max_age_days=None, stale_days=None):
    print(json.dumps(doctor_report(root, contact_max_age_days, stale_days), indent=2))


def doctor_report(root, contact_max_age_days=None, stale_days=None):
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
    if law["law_package_status"] == "draft":
        report["next_actions"].append(LAW_REVIEW_ACTION)
    from .kit import kit_status, stale_contacts
    report.update(kit_status(root))
    if contact_max_age_days is not None and (found := stale_contacts(root, contact_max_age_days)) is not None:
        report.update(stale_contacts=found[:100], **({"stale_contacts_more": len(found) - 100} if found[100:] else {}))
        report["next_actions"] += [f"Re-check {len(found)} agency contact(s) on their official sites (kit-verify-agency)."] * bool(found)
    if stale_days is not None:
        from .freshness import stale_inputs
        found = stale_inputs(root, cfg, stale_days)
        report.update(stale_inputs=found[:100], **({"stale_inputs_more": len(found) - 100} if found[100:] else {}))
        if names := sorted({e["input"] for e in found if e["status"] != "absent"}):
            report["next_actions"].append("Refresh stale inputs: " + ", ".join(names))
    from .meeting_stage import stale_kits
    stale = report["stale_comment_kits"] = stale_kits(root)
    if stale:
        report["next_actions"].append("Re-stage meetings with corrected findings: meetings-stage --id "
                                      + ", ".join(sorted({e["meeting"] for e in stale})) + " --replace")
    from .correction_requests import open_overdue
    try:
        if (overdue := open_overdue(root, date.today())) is not None:
            report["correction_requests_open_over_14_days"] = len(overdue)
            report["next_actions"] += [f"Decide correction requests open over 14 days: id {', '.join(map(str, overdue))}"] * bool(overdue)
    except ValueError as exc:
        report["correction_requests_error"] = str(exc)
    from .content import ContentError, load_content
    try:
        model = load_content(root)
    except (ContentError, OSError):
        model = None  # build/content-check name the problem; doctor stays as before
    if model is not None:
        ending = report["contracts_ending"] = [
            {"agency_id": a["agency_id"], "end": a["contract"]["end"], "days": days}
            for a in sorted((a for a in model["agencies"] if a["contract"]), key=lambda a: a["contract"]["end"])
            if 0 <= (days := (date.fromisoformat(a["contract"]["end"]) - date.today()).days) <= 90]
        if ending:
            report["next_actions"].append("Contract terms ending within 90 days: "
                                          + ", ".join(f'{c["agency_id"]} ({c["end"]})' for c in ending))
    return report


def readiness(root, campaign_id=None, out=None, contact_max_age_days=None, stale_days=None):
    from datetime import datetime, timezone
    from .privacy_patterns import categories
    campaign_id = campaign_id if campaign_id is not None else read_config(root).get("campaign_id")
    if not isinstance(campaign_id, str) or not campaign_id.strip():
        raise ValueError("readiness_campaign_id_absent: supply --campaign-id or config campaign_id")
    report = doctor_report(root, contact_max_age_days, stale_days)
    actions = report["next_actions"]
    for index, sentence in enumerate(actions):
        if found := categories(sentence):
            raise ValueError(f"readiness_unsafe_next_action: index {index}, category {found[0]}")
        if len(sentence) > 240:
            raise ValueError(f"readiness_next_action_length: index {index}")
    if len(actions) > 40:
        raise ValueError("readiness_next_actions_count: maximum 40")
    counts = {key: len(report.get(key, [])) + report.get(key + "_more", 0)
              for key in ("stale_contacts", "stale_inputs", "stale_comment_kits", "contracts_ending")}
    counts["correction_requests_open_over_14_days"] = report.get("correction_requests_open_over_14_days", 0)
    snapshot = {"schema_version": 1, "generated_at": datetime.now(timezone.utc).isoformat(),
                "campaign_id": campaign_id, "law_package_status": report["law_package_status"],
                "production_ready": report["production_ready"],
                "safe_to_send_automatically": report["safe_to_send_automatically"],
                "next_actions": actions, "counts": counts}
    encoded = json.dumps(snapshot, indent=2, ensure_ascii=False) + "\n"
    if out is None:
        print(encoded, end="")
    else:
        Path(out).write_text(encoded, encoding="utf-8")


def kit(args, root):
    from .kit import build_kit
    summary = build_kit(root, online=args.online, include=args.include, scopes=args.scope)
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    print("Kit written to " + str(root / "kit") + ". Nothing was sent.", file=sys.stdout)


def social_cmd(root, platforms=None, meetings=False, within_days=14, outcomes=False, thread_finding=None):
    from .social import (build_meeting_social, build_outcome_social, build_social, meeting_review_notes,
                         outcome_review_notes, review_notes)
    if thread_finding is not None:  # DF2207-08: one private numbered thread for one published finding; nothing is posted
        from .social_threads import build_thread_social, thread_notes
        print(json.dumps(build_thread_social(root, thread_finding, platforms), indent=2, ensure_ascii=False))
        for note in thread_notes(root, thread_finding):
            print(note, file=sys.stderr)
        print("Drafts written to kit/social/threads. Nothing was posted.")
        return
    if outcomes:  # DF1952-14: private drafts quoting recorded meeting outcomes; nothing is posted
        print(json.dumps(build_outcome_social(root, platforms, within_days), indent=2, ensure_ascii=False))
        for note in outcome_review_notes(root, within_days):
            print(note, file=sys.stderr)
        print("Drafts written to kit/social/outcomes. Nothing was posted.")
        return
    if meetings:  # DF1207-11: private drafts for upcoming meetings with verified participation; finding drafts untouched
        print(json.dumps(build_meeting_social(root, platforms, within_days), indent=2, ensure_ascii=False))
        for note in meeting_review_notes(root, within_days):
            print(note, file=sys.stderr)
        print("Drafts written to kit/social/meetings. Nothing was posted.")
        return
    print(json.dumps(build_social(root, platforms), indent=2, ensure_ascii=False))
    for note in review_notes(root):  # DF-4 alert-only: stderr; the stdout summary and exit code are unchanged
        print(note, file=sys.stderr)
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


def _site_claim_notes(root):
    from .legal_claims import site_notes
    return site_notes(root)


def build(root, check=False, budget=None, weights_out=None, weights_baseline=None):
    from .site import build_site
    public = root / "public"
    notes = []
    written = build_site(root, notes=notes)
    for note in notes:  # informational: the exit code is unchanged
        print(f"note: {note}", file=sys.stderr)
    content_mode = (root / "content").is_dir()
    print(("Site" if content_mode else "Starter") + " written to " + str(public)
          + ". No private documents exported.")
    total = 0
    for path in sorted(written):
        size = path.stat().st_size
        total += size
        print(f"  {path.relative_to(public).as_posix():<40} {size:>9} bytes")
    print(f"  {len(written)} files, {total} bytes")
    for note in _site_claim_notes(root):  # DF-4 alert-only: notes for the publish gate, never an error or an exit code
        print(note)
    if check:
        from .site_check import check_accessibility, check_links
        hits = check_public_tree(public)
        for path, line, label in hits:
            print(f"public/{path}:{line}: {label}", file=sys.stderr)
        broken = check_links(public)
        for page, href in broken:
            print(f"public/{page}: broken link {href}", file=sys.stderr)
        a11y = check_accessibility(public)  # pattern lint, not an audit
        for page, code, detail in a11y:
            print(f"public/{page}: {code} {detail}", file=sys.stderr)
        if a11y:  # one Stopped: line with a count per kind
            found = [f"{len(hits)} potential leak(s)"] * bool(hits) + [f"{len(broken)} broken link(s)"] * bool(broken)
            raise ValueError(f"public tree check found {' and '.join(found + [f'{len(a11y)} accessibility issue(s)'])}; "
                             "fix content and rebuild")
        if hits and broken:  # both lists are already on stderr; one Stopped: line names both counts
            raise ValueError(f"public tree check found {len(hits)} potential leak(s) and {len(broken)} broken link(s); "
                             "fix content and rebuild")
        if hits:
            raise ValueError(f"public tree check found {len(hits)} potential leak(s); fix content and rebuild")
        if broken:
            raise ValueError(f"public tree check found {len(broken)} broken link(s); fix content and rebuild")
        print("Public tree check passed (pattern scan, internal links and accessibility pattern lint; "
              "not an accessibility audit; human review still required).")
    return 0 if budget is None else weight_budget(public, budget, weights_out, weights_baseline)


def weight_budget(public, budget, weights_out=None, weights_baseline=None):
    from .site_check import page_weights, read_weights, weight_changes, weights_record, write_weights
    rows = page_weights(public)
    base = None if weights_baseline is None else read_weights(weights_baseline)  # read first: a bad file prints nothing
    print("Page weight (first-party: html + referenced site-local css/js/images, uncompressed):")
    for line in [f"  {row['page']} {row['total']} bytes" for row in rows] if base is None else weight_changes(rows, base):
        print(line)
    over = [row for row in rows if row["total"] > budget * 1024]
    for row in over:
        named = ", ".join(f"{p} {n}" for p, n in sorted(row["assets"], key=lambda a: (-a[1], a[0]))[:3])
        print(f"over budget: {row['page']} {row['total']} bytes > {budget} KB; largest: {named or '(no assets)'}")
    if not over:
        print(f"Page weight within {budget} KB (first-party, uncompressed).")
    if weights_out is not None:  # last: the record is private and never changes the exit code
        print(f"Page weights written to {write_weights(public.parent, weights_out, weights_record(rows, budget))} (private).")
    return 3 if over else 0


def _positive_kb(value):
    if not value.isdigit() or int(value) < 1:
        raise argparse.ArgumentTypeError("must be a positive whole number of KB")
    return int(value)


def backup_cmd(args, root):
    from .backup import BackupError, export, export_into
    if args.into is not None:
        try:
            name, manifest = export_into(root, args.into, args.encrypt_to)
        except (OSError, ValueError) as exc:  # one stderr line so cron mail stays one line
            text = " ".join((str(exc) if isinstance(exc, BackupError) else getattr(exc, "strerror", None) or type(exc).__name__).split())
            print(text if text.startswith("backup: ") else f"backup failed: {text}", file=sys.stderr)
            sys.exit(1)
        counts = manifest["counts"]
        print(f"backup ok: {name} {'encrypted' if manifest.get('encrypted') else 'UNENCRYPTED'} "
              f"files={counts['files']} bytes={counts['bytes']} manifest_sha256={manifest['manifest_sha256']}")
        return
    manifest = export(root, args.out, encrypt_to=args.encrypt_to)
    print(json.dumps({"out": str(args.out), "files": manifest["counts"]["files"], "bytes": manifest["counts"]["bytes"],
                      "objects": manifest["counts"].get("objects", 0), "engine_version": manifest["engine_version"],
                      "manifest_sha256": manifest["manifest_sha256"]},
                     indent=2))
    if manifest.get("encrypted"):
        print(f"Archive is encrypted to {manifest['recipient']}; keep the private key and its passphrase apart from the archive.")
    else:
        print("Archive is unencrypted; encrypt it before storing it anywhere shared.", file=sys.stdout)
    print("Record manifest_sha256 somewhere other than with the archive and pass it to "
          "`verify --expect-manifest-sha256`: without it verify checks integrity, not tampering.", file=sys.stdout)


def restore_cmd(args, root):
    from .backup import restore
    report = restore(args.file, root, gnupg_home=args.gnupg_home)
    print(json.dumps({"restored_to": report["restored_to"], "files": report["files"],
                      "engine_version": report["engine_version"], "created_at": report["created_at"]}, indent=2))


def verify_cmd(args):
    from .backup import verify
    report = verify(args.file, expected_manifest_sha256=args.expect_manifest_sha256, gnupg_home=args.gnupg_home)
    if not args.expect_manifest_sha256:
        report["note"] = "integrity check only; pass --expect-manifest-sha256 <digest from backup> to detect a rewritten archive"
    print(json.dumps(report, indent=2))


def backup_drill_cmd(args):
    from .backup import drill
    if args.receipt and os.path.lexists(args.receipt):
        raise ValueError(f"{args.receipt} exists; the drill never overwrites a receipt")
    receipt = drill(args.file, args.expect_manifest_sha256, doctor=doctor_report, gnupg_home=args.gnupg_home)
    text = json.dumps(receipt, indent=2)
    if args.receipt:
        with os.fdopen(os.open(args.receipt, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as out:
            out.write(text + "\n")
    print(text)
    if not receipt["passed"]:
        sys.exit(3)


def backup_list_cmd(args):
    from .backup import list_archives
    report = list_archives(args.dir, args.keep_daily, args.keep_weekly, gnupg_home=args.gnupg_home)
    print(json.dumps(report, indent=2) + "\nReport only; nothing was deleted.")
    sys.exit(3 if any(item["verify"] == "failed" for item in report["archives"]) else 0)


def backup_diff_cmd(args, root):
    from .backup import diff
    report = diff(args.file, root, expected_manifest_sha256=args.expect_manifest_sha256, gnupg_home=args.gnupg_home)
    print(json.dumps(report, indent=2))
    if not report["current"]:
        sys.exit(3)


def meetings_cmd(args, root):
    from datetime import date
    from .meetings import CivicClerkClient, LegistarClient, compare_meetings, default_opener, fixture_opener, relevant_events, resolve_zone, write_meetings
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
    client = (CivicClerkClient if args.platform == "civicclerk" else LegistarClient)(args.client, opener=opener)
    since = date.fromisoformat(args.since) if args.since else None
    meetings = relevant_events(client, since=since, zone=zone)
    previous = _read_previous(root / "kit" / "meetings.json")
    changes = ({"previous": previous} if previous == "unreadable"
               else compare_meetings(previous, meetings, args.client, since or date.today()))
    target = write_meetings(root, meetings, args.client, None if previous is None else changes, zone=zone, source=client.source)
    report = {"client": args.client, "platform": client.source, "matched_meetings": len(meetings), "written": str(target), "previous": changes["previous"],
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
    report = stage(root, args.id, args.agency_id, args.location, args.replace, args.participation,
                   finding_ids=args.finding, asks=args.ask, cosign_ids=args.cosign)
    print(json.dumps(report, indent=2))
    for note in report.get("review_flags", []):  # DF2207-07: advisory legal-claim notes, positions only
        print(note, file=sys.stderr)


def finding_preview(root, draft_path, out, now=None):
    """DF1822-02: one draft's finding page as build renders it (plus banner + noindex) written privately to
    ``out``/findings/<slug>.html and ``out``/style.css. public/, feed, sitemap and data/ are never touched."""
    import tempfile
    from . import finding_law, findings
    from .content import load_content, load_finding_preview
    from .site import EXTRA_CSS, Site
    out = Path(out).expanduser()
    if out.is_symlink():
        raise ValueError(f"{out}: --out is a symlink; choose a real private folder outside public/ and content/")
    target = out.resolve()
    for name in ("public", "content"):
        protected = (root / name).resolve()
        if target == protected or protected in target.parents:
            raise ValueError(f"{out}: --out is inside {name}/; choose a private folder outside public/ and content/")
    folder = target / "findings"
    for path in (target, folder):
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            raise ValueError(f"{path}: must be a real folder, not a symlink or a file")
    finding = load_finding_preview(findings.load_draft(draft_path), str(draft_path), now=now)
    model = load_content(root)
    if model is None:
        raise ValueError("finding preview needs content/ (run content init first)")
    # Render with the draft in the model, as build will after `finding publish` (review t_608228c8): Site reads the
    # law package's citations and status only when a model finding cites a rule. In memory only; content/ unchanged.
    others = [f for f in model["findings"] if f["id"] != finding["id"]]  # a published copy of this draft is replaced
    clash = next((f["id"] for f in others if f["slug"] == finding["slug"]), None)
    if clash:
        raise ValueError(f"{draft_path}: slug {finding['slug']} is already used by finding {clash}; build would refuse the duplicate")
    problems = finding_law.check_findings(root, [finding])
    if problems:
        raise ValueError(f"draft {problems[0]}; build would refuse it")
    model = dict(model, findings=sorted(others + [finding], key=lambda f: (f["published_at"], f["slug"]), reverse=True))
    page = Site(root, read_config(root), model).finding_preview_html(finding)
    if not target.exists():
        target.mkdir(mode=0o700, parents=True)
        os.chmod(target, 0o700)
    folder.mkdir(mode=0o700, exist_ok=True)
    written = []
    for path, text in ((folder / f"{finding['slug']}.html", page["html"]), (target / "style.css", page["css"] + EXTRA_CSS)):
        fd, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(temp, path)  # replaces a symlink entry itself, never writes through it
        written.append(str(path))
    return {"written": written, "slug": finding["slug"]}


def _campaign_option(parser):
    """Every parser that takes --directory also takes --campaign NAME (a registered campaign), never both."""
    for action in list(parser._actions):
        if isinstance(action, argparse._SubParsersAction):
            for sub in action.choices.values():
                _campaign_option(sub)
    directory = next((a for a in parser._actions if "--directory" in a.option_strings), None)
    if directory is not None and "--campaign" not in parser._option_string_actions:
        group = parser.add_mutually_exclusive_group(required=directory.required)
        group._group_actions.append(directory)
        directory.required = False
        group.add_argument("--campaign", metavar="NAME", action=registry.CampaignAction, help="A registered campaign, not --directory")


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    setup_p = commands.add_parser("setup", help="Guided offline setup; nothing sent or published")
    setup_p.add_argument("--directory", required=True)
    for flag in ("--name", "--county", "--state", "--location"):
        setup_p.add_argument(flag)
    setup_p.add_argument("--yes", action="store_true", help="Use flags only; never prompt")
    import_p = commands.add_parser("import-existing", help="Preserve a local folder with private receipts; no parsing")
    for flag in ("--directory", "--label"):
        import_p.add_argument(flag, required=True)
    import_p.add_argument("--from", dest="folder", required=True)
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
            source.add_argument("--zip", help="One production .zip: archive and regular members stored by hash, "
                                "never extracted to disk; nested archives are stored, not opened")
            source.add_argument("--eml", help="One emailed production .eml: message stored as is, each attachment "
                                "stored and linked to it; headers never printed")
            source.add_argument("--mbox", help="One local mbox export: each message stored like --eml, linked to "
                                "the mailbox; headers never printed")
            cmd.add_argument("--source-id", required=True,
                             help="Stable non-secret production/message identity, not a signed URL")
            cmd.add_argument("--dry-run", action="store_true", help="Read and hash only; ledger read-only, nothing written")
        if name == "build":
            mode = cmd.add_mutually_exclusive_group()
            mode.add_argument("--check", action="store_true",
                              help="Scan the generated public/ tree for private paths and credential patterns, "
                                   "and fail on site-local links or #fragments that do not resolve")
            mode.add_argument("--preview", action="store_true",
                              help="List files a rebuild would add, remove or change in public/; writes nothing")
            cmd.add_argument("--budget", type=_positive_kb, metavar="KB", help="Print each page's first-party "
                             "weight (html + referenced css/js/images, uncompressed); exit 3 when a page exceeds KB")
            cmd.add_argument("--weights-out", metavar="FILE", help="With --budget: save the per-page weights to a new "
                             "private page-weights-v1 JSON file (not inside public/ or content/)")
            cmd.add_argument("--weights-baseline", metavar="FILE", help="With --budget: print each page's growth "
                             "against a saved page-weights-v1 file")
        if name == "status":
            cmd.add_argument("--check", action="store_true", help="Re-hash private/objects against the ledger (read-only)")
            cmd.add_argument("--fix-modes", action="store_true",
                             help="chmod loose private dirs/files to 0700/0600, then run --check")
            cmd.add_argument("--list", action="store_true", help="List ingested receipts with 12-char hash prefixes")
            cmd.add_argument("--source-id", help="With --list: only receipts with this exact source_id")
            cmd.add_argument("--since", help="With --list: received on/after YYYY-MM-DD or an ISO timestamp with offset")
            cmd.add_argument("--not-in-sources", action="store_true",
                             help="With --list: only objects not registered in content/sources.json")
        if name == "doctor":
            cmd.add_argument("--contact-max-age-days", type=int, metavar="N", help="Name verified kit contacts checked over N days ago")
            cmd.add_argument("--stale-days", type=int, metavar="N", help="Name inputs dated over N days ago (1-3650)")
            cmd.add_argument("--checklist", action="store_true", help="Print each check as ok/todo with its next step and owner")
            cmd.add_argument("--json", action="store_true", help="With --checklist: print the checklist rows as JSON")
    readiness_p = commands.add_parser("readiness", help="Project doctor as a private snapshot; no network")
    readiness_p.add_argument("--directory", required=True)
    readiness_p.add_argument("--campaign-id", help="Otherwise use config campaign_id; absence refuses")
    readiness_p.add_argument("--out", help="Write only after all checks pass; otherwise print JSON")
    readiness_p.add_argument("--contact-max-age-days", type=int, metavar="N", help="Name verified contacts checked over N days ago (at least 1)")
    readiness_p.add_argument("--stale-days", type=int, metavar="N", help="Name inputs dated over N days ago (1-3650)")
    kit_cmd = commands.add_parser("kit", help="Suggest agencies and draft records requests offline")
    kit_cmd.add_argument("--directory", required=True)
    kit_cmd.add_argument("--online", action="store_true",
                         help="Fall back to the Census geocoder when the seed cannot resolve the location")
    kit_cmd.add_argument("--include", action="append",
                         help="Agency kind to include (repeatable): sheriff, police, county_board, "
                              "city_council, district_attorney, chp, campus_police (opt-in), ccd_police (opt-in), school_police (opt-in), district_police (opt-in)")
    kit_cmd.add_argument("--scope", action="append", help="Law-package request scope id to draft (repeatable; default all)")
    social_p = commands.add_parser("social", help="Draft one private post per published finding in kit/social; posts nothing")
    social_p.add_argument("--directory", required=True)
    social_p.add_argument("--check", action="store_true", help="Report draft problems without writing (exit 3 on any)")
    social_p.add_argument("--platform", action="append", choices=["x", "bluesky", "mastodon"],
                          help="Draft for this platform's own length rule into kit/social/<platform> (repeatable)")
    social_p.add_argument("--meetings", action="store_true",
                          help="Draft one private post per scheduled upcoming meeting with verified participation into kit/social/meetings")
    social_p.add_argument("--outcomes", action="store_true",
                          help="Draft one private post per meeting with a recorded outcome dated in the last N days into kit/social/outcomes")
    social_p.add_argument("--thread", action="store_true",
                          help="With --finding: draft one private numbered thread for that published finding into kit/social/threads")
    social_p.add_argument("--finding", action="append", metavar="ID",
                          help="With --thread: the exact id of one published finding")
    social_p.add_argument("--within-days", type=int, default=None, metavar="N",
                          help="With --meetings: meetings starting within N days; with --outcomes: outcomes recorded in the last N "
                               "calendar days, today included; 1 to 60 (default 14)")
    press = commands.add_parser("press", help="Draft private press material; sends nothing")
    press_commands = press.add_subparsers(dest="press_command", required=True)
    press_draft = press_commands.add_parser("draft")
    press_draft.add_argument("--directory", required=True)
    press_draft.add_argument("--finding", action="append", default=[])
    press_draft.add_argument("--date")
    supporters = commands.add_parser("supporters", help="Consent-backed supporting organizations: public entry in "
                                     "content/supporters.json, private consent in kit/").add_subparsers(
        dest="supporters_command", required=True)
    supporters_add = supporters.add_parser("add", help="List one organization (never an individual) after recording its "
                                           "permission; publishes nothing")
    supporters_add.add_argument("--directory", required=True)
    supporters_add.add_argument("--name", required=True)
    supporters_add.add_argument("--kind", required=True, choices=SUPPORTER_KINDS)
    supporters_add.add_argument("--url", default=None, help="Optional https address of the organization")
    supporters_add.add_argument("--organization", action="store_true",
                                help="Attest that this is an organization, not an individual (required)")
    supporters_add.add_argument("--confirmed-on", required=True, metavar="DATE", help="YYYY-MM-DD the organization confirmed")
    supporters_add.add_argument("--how", required=True, choices=SUPPORTER_HOWS)
    supporters_add.add_argument("--correspondence", default=None, metavar="PREFIX",
                                help="12 to 64 hex characters of an ingested record; kept privately")
    supporters_consent = supporters.add_parser("consent", help="Grant or revoke permission to name one listed organization "
                                               "under a staged written comment; edits only the private file")
    supporters_consent.add_argument("--directory", required=True)
    supporters_consent.add_argument("--id", required=True)
    grant_group = supporters_consent.add_mutually_exclusive_group(required=True)
    grant_group.add_argument("--cosign", nargs="+", metavar="any|MEETING_ID",
                             help="The word any (alone) or the meeting ids the organization agreed to co-sign")
    grant_group.add_argument("--clear-cosign", action="store_true", help="Remove the co-sign grant")
    supporters_consent.add_argument("--confirmed-on", default=None, metavar="DATE", help="YYYY-MM-DD it agreed to co-sign")
    supporters_consent.add_argument("--how", default=None, choices=SUPPORTER_HOWS)
    supporters_consent.add_argument("--correspondence", default=None, metavar="PREFIX",
                                    help="12 to 64 hex characters of an ingested record; kept privately")
    supporters_remove = supporters.add_parser("remove", help="Drop one organization's public entry and private record")
    supporters_remove.add_argument("--directory", required=True)
    supporters_remove.add_argument("--id", required=True)
    press_letter = press_commands.add_parser("letter", help="Draft a private letter to the editor; sends nothing")
    press_letter.add_argument("--directory", required=True)
    press_letter.add_argument("--finding", action="append", default=[], help="Published finding id (repeat for 1 to 3)")
    press_letter.add_argument("--words", type=int, default=250, metavar="N", help="Word limit, 150 to 400 (default 250)")
    press_letter.add_argument("--outlet", help="Outlet name for the salutation (plain text)")
    press_letter.add_argument("--date")
    backup_p = commands.add_parser("backup", help="Write a verified tar of the campaign directory")
    backup_p.add_argument("--directory", required=True)
    backup_out = backup_p.add_mutually_exclusive_group(required=True)
    backup_out.add_argument("--out", help="Archive path to create (must not exist)")
    backup_out.add_argument("--into", help="Existing private folder; writes a new deflock-backup-<UTC stamp>.tar[.gpg] and prints one status line")
    backup_p.add_argument("--encrypt-to", default=None, help="Public key file; the archive is encrypted to it (GnuPG)")
    restore_p = commands.add_parser("restore", help="Restore a backup into an empty directory")
    restore_p.add_argument("--file", required=True)
    restore_p.add_argument("--directory", required=True)
    restore_p.add_argument("--gnupg-home", default=None, help="GnuPG home holding the private key (encrypted archives)")
    verify_p = commands.add_parser("verify", help="Re-hash every member of a backup against its manifest")
    verify_p.add_argument("--file", required=True)
    verify_p.add_argument("--expect-manifest-sha256", default=None, help="manifest digest printed by `backup`, kept apart from the archive")
    verify_p.add_argument("--gnupg-home", default=None, help="GnuPG home holding the private key (encrypted archives)")
    diff_p = commands.add_parser("backup-diff", help="List files added/removed/changed since a backup (read-only; exit 3 on drift)")
    diff_p.add_argument("--file", required=True)
    diff_p.add_argument("--directory", required=True)
    diff_p.add_argument("--expect-manifest-sha256", default=None, help="manifest digest printed by `backup`")
    diff_p.add_argument("--gnupg-home", default=None, help="GnuPG home holding the private key (encrypted archives)")
    drill_p = commands.add_parser("backup-drill", help="Restore a backup into a temp copy, check it there, remove it (exit 3 on fail)")
    drill_p.add_argument("--file", required=True)
    drill_p.add_argument("--expect-manifest-sha256", default=None, help="manifest digest printed by `backup`")
    drill_p.add_argument("--receipt", help="Also write the receipt JSON here (must not exist)")
    drill_p.add_argument("--gnupg-home", default=None, help="GnuPG home holding the private key (encrypted archives)")
    list_p = commands.add_parser("backup-list", help="Re-verify every *.tar and *.tar.gpg in a folder; report what a keep rule keeps (deletes nothing)")
    list_p.add_argument("--dir", required=True)
    list_p.add_argument("--gnupg-home", default=None, help="GnuPG home holding the private key (encrypted archives)")
    from .backup import KEEP_LIMITS, keep_count
    for flag, key, period in (("--keep-daily", "keep_daily", "UTC day"), ("--keep-weekly", "keep_weekly", "ISO week")):
        list_p.add_argument(flag, type=keep_count(KEEP_LIMITS[key]), help=f"Newest per {period}, most recent N (0-{KEEP_LIMITS[key]})")
    meetings_p = commands.add_parser("meetings", help="Find upcoming ALPR agenda items on Legistar; writes kit/meetings.json")
    meetings_p.add_argument("--directory", required=True)
    meetings_p.add_argument("--client", required=True, help="Legistar client slug (the part after webapi.legistar.com/v1/)")
    meetings_p.add_argument("--platform", choices=("legistar", "civicclerk"), default="legistar", help="Agenda platform (default legistar)")
    meetings_p.add_argument("--online", action="store_true", help="Query the Legistar Web API (the only network use)")
    meetings_p.add_argument("--from-json", help="Recorded responses keyed by URL path (offline)")
    meetings_p.add_argument("--since", help="Earliest event date, YYYY-MM-DD (default today)")
    meetings_p.add_argument("--tz", default="America/Los_Angeles", help="IANA time zone for starts_at (default America/Los_Angeles)")
    stage_p = commands.add_parser("meetings-stage", help="Write content/meetings/<id>.json from one kit/meetings.json entry")
    stage_p.add_argument("--directory", required=True)
    stage_p.add_argument("--id", required=True, help="Meeting id from kit/meetings.json")
    stage_p.add_argument("--agency-id", help="The meeting's agency; its published findings are cited first")
    stage_p.add_argument("--finding", action="append", metavar="ID",
                         help="Published finding id to cite (repeatable, max 3, in this order; default: 3 chosen)")
    stage_p.add_argument("--ask", action="append", metavar="T", default=None,
                         help="This meeting's own public-comment ask (repeatable, 1-3, 1-200 characters); "
                              "replaces site.json comment_asks for this kit only")
    stage_p.add_argument("--cosign", action="append", metavar="SUPPORTER_ID", default=None,
                         help="Name a consenting supporting organization under this meeting's written comment "
                              "(repeatable, max 30; needs --participation with a written_comment; sends nothing)")
    stage_p.add_argument("--location")
    stage_p.add_argument("--participation", help="Organizer-verified participation JSON (docs/MEETINGS.md)")
    stage_p.add_argument("--replace", action="store_true", help="Overwrite an existing content/meetings/<id>.json")
    outcome_p = commands.add_parser("meetings-outcome", help="Record an organizer-verified outcome on a past staged meeting")
    for flag in ("--directory", "--id"):
        outcome_p.add_argument(flag, required=True)
    for flag in ("--result", "--on", "--source", "--verified-by"):  # required in creation mode, checked there (DF2207-05)
        outcome_p.add_argument(flag)
    outcome_p.add_argument("--minutes-url", help="https URL of the body's approved official minutes (confirms an existing outcome)")
    outcome_p.add_argument("--minutes-approved-on", metavar="YYYY-MM-DD", help="Day the body approved those minutes")
    outcome_p.add_argument("--vote", help="Vote as stated by the official record, e.g. 4-1")
    outcome_p.add_argument("--votes", metavar="FILE", help="name,vote CSV (yes|no|abstain|absent|recused) read from the --source record")
    outcome_p.add_argument("--replace", action="store_true", help="Overwrite an existing outcome")
    outcome_p.add_argument("--recording-url", help="https URL of the body's official video or audio of the meeting")
    outcome_p.add_argument("--recording-start", metavar="H:MM:SS", help="Where the discussion starts in the recording, at most 12:00:00")
    outcome_p.add_argument("--continued-to", metavar="MEETING_ID", help="With --result continued: the staged later meeting of the same body the official source says the item moved to")
    outcome_p.add_argument("--continued-to-date", metavar="YYYY-MM-DD", help="With --result continued: the later date the official source states (not with --continued-to)")
    outcome_p.add_argument("--direction", metavar="TEXT", help="A direction the body gave its staff, as the official source states it (1-200 plain-text characters; needs --return-by; alone it updates a recorded outcome)")
    outcome_p.add_argument("--return-by", metavar="YYYY-MM-DD", help="The date the official source says staff should report back (on or after the meeting)")
    outcome_p.add_argument("--direction-source", metavar="URL", help="https URL where the direction is stated (default: the outcome's --source)")
    outcome_p.add_argument("--spoke", metavar="N", help="How many members of the public spoke on the item, as the official minutes or record state (0-999; alone with --written or --comment-source it updates a recorded outcome)")
    outcome_p.add_argument("--written", metavar="M", help="How many written comments the official record says were received on the item (0-999)")
    outcome_p.add_argument("--comment-source", metavar="URL", help="https URL of the record that states the counts (default: the outcome's minutes URL, else its --source)")
    sent_p = commands.add_parser("meetings-comment-sent", help="Record a comment you submitted by hand on a staged "
                                 "meeting in kit/comments-sent.json; sends nothing")
    for flag in ("--directory", "--id", "--on"):
        sent_p.add_argument(flag, required=True)
    sent_p.add_argument("--channel", required=True, choices=("email", "portal", "mail", "in_person"))
    sent_p.add_argument("--file", help="The letter you sent: only its SHA-256 and size are kept, never its text or path")
    sent_p.add_argument("--replace", action="store_true", help="Overwrite an existing record for this meeting")
    content_check_p = commands.add_parser("content-check", help="List every content/ problem build would refuse (read-only; exit 3)")
    content_check_p.add_argument("--directory", required=True)
    content_check_p.add_argument("--json", dest="as_json", action="store_true", help="JSON report instead of text")
    evidence_p = commands.add_parser("evidence", help="Check that published findings' cited records are registered and stored (read-only)")
    evidence_p.add_argument("--directory", required=True)
    evidence_p.add_argument("--check", action="store_true", help="Exit 3 when any citation has a problem")
    verify_agency_p = commands.add_parser("kit-verify-agency", help="Record an organizer-checked records contact in kit/agencies.json;"
                                          " 'import --csv FILE' records many at once")
    verify_agency_p.add_argument("action", nargs="?", choices=("import",), help="import: many checked contacts from one CSV")
    verify_agency_p.add_argument("--directory", required=True)
    for flag in ("--agency-id", "--source-url", "--checked-on"):
        verify_agency_p.add_argument(flag, help="Required without 'import'")
    for flag in ("--records-email", "--records-url", "--portal"):
        verify_agency_p.add_argument(flag, help="Value the organizer checked on the official page (never fetched)")
    verify_agency_p.add_argument("--mailing-address", help="Postal address the organizer checked on the same official page: "
                                 "2-5 lines of 1-80 characters separated by | (never fetched; not with 'import')")
    verify_agency_p.add_argument("--csv", help="import: header agency_id,source_url,checked_on,records_email,records_url,"
                                 "portal_vendor,portal_url; 1-500 rows, all or none")
    verify_agency_p.add_argument("--check", action="store_true", help="import: validate only; write nothing")
    agency_cmds = commands.add_parser("kit-agency", help="Add, select or deselect agencies in kit/agencies.json; the next"
                                      " kit run drafts requests for selected law-enforcement agencies"
                                      ).add_subparsers(dest="agency_command", required=True)
    for name, text in (("select", "Mark one agency selected"), ("deselect", "Mark one agency not selected"),
                       ("add", "Append an organizer-added agency (selected, verified: false)"),
                       ("import", "Append many organizer-added agencies from one CSV, all or none")):
        agency_p = agency_cmds.add_parser(name, help=text, description=text + "; the next kit run drafts requests for"
                                          " selected law-enforcement agencies. Nothing is sent.")
        agency_p.add_argument("--directory", required=True)
        if name == "import":
            agency_p.add_argument("--csv", required=True, help="Header agency_id,name,kind[,note]; 1-500 rows")
            agency_p.add_argument("--check", action="store_true", help="Validate only; write nothing")
            continue
        agency_p.add_argument("--id", required=True, dest="agency_id")
        if name == "add":
            agency_p.add_argument("--name", required=True)
            agency_p.add_argument("--kind", required=True, help="sheriff, police, chp, county_board, city_council, "
                                  "district_attorney or other")
            agency_p.add_argument("--note", default=None, help="Up to 300 characters")
    atlas_text = "Cross-check a local Atlas CSV (private, read-only). An Atlas listing is a lead, not evidence."
    atlas_p = agency_cmds.add_parser("atlas", help=atlas_text, description=atlas_text, allow_abbrev=False)
    atlas_p.add_argument("--csv", required=True, help="A saved Atlas of Surveillance export (UTF-8, at most 10 MiB)")
    atlas_p.add_argument("--directory", required=True)
    atlas_p.add_argument("--json", action="store_true", dest="as_json", help="Print the report as JSON")
    from .kit_track import CHANNELS as kit_track_channels
    sent_p = commands.add_parser("kit-sent", help="Record a request the organizer sent by hand in kit/sent.json; sends nothing")
    for flag in ("--directory", "--agency", "--date"):
        sent_p.add_argument(flag, required=True)
    sent_p.add_argument("--channel", required=True, choices=kit_track_channels)
    sent_p.add_argument("--request-url", metavar="URL", help="With --channel portal: the https:// page of the request you "
                        "filed there, on the agency's verified portal host (kit-verify-agency --portal); with --channel "
                        "muckrock: the https://www.muckrock.com/foi/ page. Recorded, never fetched")
    sent_p.add_argument("--replace", action="store_true", help="Correct an entry that has no recorded steps")
    sent_p.add_argument("--request", default="main", help="Name of a further request to this agency, drafted in "
                        "kit/requests/<agency>--<name>.md (default main: kit/requests/<agency>.md)")
    sent_p.add_argument("--reference", metavar="REF", help="The agency's own request number (1-40: A-Z a-z 0-9 - _ # .)")
    sent_p.add_argument("--replace-reference", action="store_true", help="Change a different stored --reference")
    sent_p.add_argument("--correspondence", action="append", default=[], metavar="PREFIX",
                        help="Repeatable: >= 12 hex of the ingested sent message or confirmation; not records")
    sent_p.add_argument("--covers-through", metavar="D", help="YYYY-MM-DD: the last day of records this request asked "
                        "for, when the draft's 'The date range for this request is FROM through TO.' line is missing "
                        "or edited (not after --date); kit-repeat starts the follow-on the day after")
    repeat_p = commands.add_parser("kit-repeat", help="Draft a follow-on request for the records created since an "
                                   "earlier sent request (private, unsent; nothing is sent)")
    for flag in ("--directory", "--agency", "--request", "--as"):
        repeat_p.add_argument(flag, required=True, dest="new_request" if flag == "--as" else None,
                              help="Name of the new request" if flag == "--as" else None)
    repeat_p.add_argument("--through", metavar="D", help="YYYY-MM-DD last day to ask for (default today, written as a date)")
    repeat_p.add_argument("--from", dest="date_from", metavar="D", help="YYYY-MM-DD first day, required when the "
                          "earlier request has no recorded coverage end (sent before this feature, imported or unknown)")
    status_p = commands.add_parser("kit-status", help="List recorded requests with provisional due dates (read-only)")
    status_p.add_argument("--directory", required=True)
    status_p.add_argument("--request", help="Only requests with this name (main is the first one)")
    status_p.add_argument("--owner", metavar="NAME", help="Only requests assigned to this teammate label (kit-assign)")
    status_p.add_argument("--csv", metavar="FILE", help="Write a new private .csv tracker (0600) outside public/ and content/")
    fees_p = commands.add_parser("kit-fees", help="Fee estimates, payments and waivers across every recorded request, "
                                 "with exact totals (read-only)")
    fees_p.add_argument("--directory", required=True)
    fees_p.add_argument("--csv", metavar="FILE", help="Write a new private .csv (0600) outside public/ and content/")
    check_p = commands.add_parser("kit-check", help="Unfilled [placeholders] and unverified recipients in each "
                                  "selected agency's draft (read-only; exit 3)")
    check_p.add_argument("--directory", required=True)
    check_p.add_argument("--json", dest="as_json", action="store_true", help="JSON report instead of text")
    copy_p = commands.add_parser("kit-copy", help="Sendable part of a draft as kit/outgoing/<id>.txt, an unsent .eml, or "
                                 "(--for mail) a private printable <id>.letter.html and .letter.txt; sends and prints nothing")
    for flag in ("--directory", "--agency"):
        copy_p.add_argument(flag, required=True)
    copy_p.add_argument("--for", dest="copy_for", required=True, choices=("portal", "email", "mail"))
    copy_p.add_argument("--json", dest="as_json", action="store_true", help="JSON instead of text")
    coverage_p = commands.add_parser("kit-coverage", help="One row per selected agency: draft, sent, last step, "
                                     "records, sources, findings (read-only)")
    coverage_p.add_argument("--directory", required=True)
    coverage_p.add_argument("--csv", action="store_true", help="RFC 4180 CSV on stdout instead of a table")
    due_p = commands.add_parser("kit-due", help="Open requests by provisional determination date; exit 3 when any is past")
    due_p.add_argument("--directory", required=True)
    due_p.add_argument("--as-of", help="YYYY-MM-DD to count from (default today)")
    due_p.add_argument("--request", help="Only requests with this name (refused when none is recorded)")
    due_p.add_argument("--owner", metavar="NAME", help="Only requests assigned to this teammate label (kit-assign)")
    assign_p = commands.add_parser("kit-assign", help="Record which teammate owns a recorded request (private "
                                   "kit/assignments.json; nothing is sent)",
                                   description="Assign a recorded request to a teammate's label, or clear it. A label "
                                   "is an alias, not a verified identity. Dates and request state never change.")
    assign_p.add_argument("--directory", required=True)
    assign_p.add_argument("--agency", required=True, help="Agency ID in kit/agencies.json")
    assign_p.add_argument("--request", default="main", help="Name of the recorded request (default main)")
    assign_action = assign_p.add_mutually_exclusive_group(required=True)
    assign_action.add_argument("--to", metavar="NAME", help="The teammate's label, 1-40 characters; not an email "
                               "address or phone number")
    assign_action.add_argument("--clear", action="store_true", help="Remove the request's owner")
    timing_p = commands.add_parser("kit-timing", help="Private response-time report from kit/sent.json: calendar days "
                                   "between recorded steps (read-only; provisional, not a legal deadline)",
                                   description="Calendar days between recorded steps; provisional; not a legal deadline. "
                                   "Reads kit/sent.json only; nothing is sent.")
    timing_p.add_argument("--directory", required=True)
    timing_p.add_argument("--as-of", help="YYYY-MM-DD to count to (default today); not a reconstruction of past knowledge")
    timing_p.add_argument("--csv", metavar="FILE", help="Write a new private .csv (0600) outside public/ and content/")
    calendar_p = commands.add_parser("kit-calendar", help="Private .ics of open requests' dates and comment deadlines")
    for flag in ("--directory", "--out", "--as-of"):  # --as-of YYYY-MM-DD: earlier comment deadlines left out
        calendar_p.add_argument(flag, required=flag != "--as-of")
    response_p = commands.add_parser("kit-response", help="Record a dated agency response step on a sent request")
    for flag in ("--directory", "--agency", "--date"):
        response_p.add_argument(flag, required=True)
    response_p.add_argument("--kind", required=True, choices=("acknowledged", "extension", "determination",
                                                               "produced", "denied", "closed", "fee_estimate",
                                                               "clarification_requested", "clarified", "fee_paid",
                                                               "fee_waived", "withdrawn"))
    response_p.add_argument("--amount", metavar="USD", help="fee_estimate (required) or fee_paid (optional): 0-100000 "
                            "(e.g. 245.50); recorded only, nothing is paid")
    response_p.add_argument("--note", help="Short plain-text note (<= 200 chars; never record contents); required for "
                            "withdrawn, the campaign's own withdrawal of its request (not an agency closure)")
    response_p.add_argument("--record", action="append", default=[], metavar="PREFIX",
                            help="produced only, repeatable: >= 12 hex of an ingested record's SHA-256")
    response_p.add_argument("--correspondence", action="append", default=[], metavar="PREFIX",
                            help="any kind, repeatable: >= 12 hex of an ingested letter or confirmation; not records")
    response_p.add_argument("--request", default="main", help="Which sent request to this agency (default main)")
    response_p.add_argument("--expected-on", metavar="YYYY-MM-DD", help="determination or a later step: the agency's "
                            "own stated production date (provisional, not a legal deadline)")
    response_p.add_argument("--reference", metavar="REF", help="The agency's own request number, stored once")
    response_p.add_argument("--replace-reference", action="store_true", help="Change a different stored --reference")
    response_p.add_argument("--partial", action="store_true", help="produced only: part of the records; the request "
                            "stays open (--expected-on: the agency's next installment date)")
    response_p.add_argument("--withheld-basis", action="append", default=[], metavar="TEXT",
                            help="denied or produced only, repeatable (at most 5): the agency's own stated reason or "
                            "code section, as written (1-200 chars, no control characters; never record contents)")
    response_p.add_argument("--download-expires-on", metavar="YYYY-MM-DD", help="produced only: the date the agency says "
                            "its download link expires (not before --date). Only the date is kept; the link itself is "
                            "never stored or fetched. The production stays 'download pending' until records are "
                            "linked with a later produced --record step")
    response_p.add_argument("--scope", action="append", default=[], metavar="ID",
                            help="repeatable, with --disposition: a law-package scope this request asked for that the "
                            "agency's answer covers (not on acknowledged, extension, clarification or fee_estimate steps)")
    response_p.add_argument("--disposition", choices=("produced", "partly_produced", "withheld", "no_records_stated"),
                            help="with --scope: what the agency said about those scopes (your record, not a legal finding)")
    undo_p = commands.add_parser("kit-response-undo", help="Take the latest recorded step off a hand-sent request (kept in "
                                 "the private kit/sent-undone.json); fetches and sends nothing")
    for flag in ("--directory", "--agency", "--reason"):
        undo_p.add_argument(flag, required=True)
    undo_p.add_argument("--request", default="main", help="Which sent request to this agency (default main)")
    muckrock_p = commands.add_parser("kit-muckrock-import", help="Propose the kit-response step a MuckRock request JSON you "
                                     "saved by hand implies; fetches and sends nothing")
    for flag in ("--directory", "--agency", "--from-json"):
        muckrock_p.add_argument(flag, required=True)
    muckrock_p.add_argument("--request", default="main", help="Which sent request to this agency (default main)")
    muckrock_p.add_argument("--apply", action="store_true", help="Record the step (default: dry run, nothing written)")
    muckrock_p.add_argument("--json", dest="as_json", action="store_true", help="JSON instead of text")
    import_p = commands.add_parser("kit-response-import", help="Record a transcribed portal or letter timeline (CSV) as "
                                   "kit-response steps in one checked write; fetches and sends nothing")
    for flag in ("--directory", "--agency", "--csv"):
        import_p.add_argument(flag, required=True)
    import_p.add_argument("--request", default="main", help="Which sent request to this agency (default main)")
    import_p.add_argument("--apply", action="store_true", help="Record every row (default: check only, nothing written)")
    sent_import_p = commands.add_parser("kit-sent-import", help="Record requests sent before you used the tool (CSV) "
                                        "as imported entries in one checked write; sends nothing")
    for flag in ("--directory", "--csv"):
        sent_import_p.add_argument(flag, required=True)
    sent_import_p.add_argument("--apply", action="store_true", help="Record every row (default: check only, nothing written)")
    followup_p = commands.add_parser("kit-followup", help="Private follow-up draft for a past-due sent request; never sent")
    for flag in ("--directory", "--agency"):
        followup_p.add_argument(flag, required=True)
    followup_p.add_argument("--as-of", help="YYYY-MM-DD, not after today (default today)")
    followup_p.add_argument("--replace", action="store_true", help="Rewrite an existing draft for the same day")
    followup_p.add_argument("--request", default="main", help="Which sent request to this agency (default main)")
    followup_p.add_argument("--cite-contact", action="store_true", help="Add one line naming your latest recorded contact "
                            "(kit-contact) on or before --as-of: channel and job title only, never its note")
    from .kit_track import CONTACT_CHANNELS as contact_channels
    contact_p = commands.add_parser("kit-contact", help="Record your own call, voicemail, counter visit or email to the "
                                    "records office on a sent request, privately; sends nothing")
    for flag in ("--directory", "--agency", "--on"):
        contact_p.add_argument(flag, required=True)
    contact_p.add_argument("--channel", required=True, choices=contact_channels)
    contact_p.add_argument("--with", dest="role", required=True, metavar="ROLE", help="Job title of who you spoke or "
                           "wrote to (1-60 characters), not a person's name; no email address or phone number")
    contact_p.add_argument("--note", required=True, help="1-500 characters of plain text; no personal details")
    contact_p.add_argument("--request", default="main", help="Which sent request to this agency (default main)")
    preserve_p = commands.add_parser("kit-preserve", help="Private preservation-request draft for a sent, unanswered request; never sent")
    for flag in ("--directory", "--agency"):
        preserve_p.add_argument(flag, required=True)
    preserve_p.add_argument("--request", default="main", help="Which sent request to this agency (default main)")
    denial_p = commands.add_parser("kit-denial-reply", help="Private reply draft after a recorded denied step; never sent")
    for flag in ("--directory", "--agency"):
        denial_p.add_argument(flag, required=True)
    denial_p.add_argument("--rule", action="append", default=[], metavar="RULE_ID",
                          help="Repeatable: a law-package rule_id in force on the denial date, cited with its status")
    denial_p.add_argument("--replace", action="store_true", help="Rewrite an existing draft for the same denial")
    asked_p = commands.add_parser("kit-asked", help="Propose content/agencies.json 'asked' items from kit/sent.json "
                                  "(dry run by default); nothing is sent or published")
    asked_p.add_argument("--directory", required=True)
    asked_p.add_argument("--apply", action="store_true", help="Write the proposal (validated, one atomic write)")
    withheld_p = commands.add_parser("kit-withheld", help="Every recorded withheld basis, grouped by the Gov. Code "
                                     "section number in the agency's own words (read-only)")
    withheld_p.add_argument("--directory", required=True)
    withheld_p.add_argument("--json", dest="as_json", action="store_true", help="JSON instead of text")
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
    batch = sources.add_parser("add-batch", help="Add every receipt of one ingest source_id (or every production record "
                               "of one request, --from-request) not yet listed; titles marked [EDIT ME: title]; "
                               "publishes nothing")
    batch.add_argument("--directory", required=True)
    for flag in ("--source-id", "--agency-id", "--received-at"):
        batch.add_argument(flag, default=None, help="Required unless --from-request")
    batch.add_argument("--from-request", metavar="AGENCY", default=None,
                       help="Use the production records linked on this agency's produced steps instead")
    batch.add_argument("--request", default=None, help="With --from-request: the request name (default main)")
    batch.add_argument("--tz", default=None,
                       help="With --from-request: IANA time zone for received_at (default America/Los_Angeles)")
    batch.add_argument("--fragment", default=None, help="Default: the agency's one existing fragment")
    batch.add_argument("--dry-run", action="store_true", help="Print what would be added; write nothing")
    verify = sources.add_parser("verify-copy", help="Hash a copy you downloaded from an entry's public_url and record "
                                "whether it is the same file; nothing is fetched")
    for flag in ("--directory", "--sha256", "--file", "--checked-on"):
        verify.add_argument(flag, required=True)
    verify.add_argument("--differs-because", choices=("redacted", "converted", "unknown"), default=None)
    verify.add_argument("--fragment", default=None, help="Library fragment whose public_url identifies the copy; "
                        "needed when the record has several public URLs")
    content_cmds = commands.add_parser("content", help="Scaffold content/ for the public site").add_subparsers(
        dest="content_command", required=True)
    content_init = content_cmds.add_parser(
        "init", help="Write a minimal valid content/ with [EDIT ME: ...] placeholders; refuses an existing content/"
    )
    content_init.add_argument("--directory", required=True)
    content_init.add_argument("--from", dest="source_campaign")
    strip_p = content_cmds.add_parser("strip-metadata", help="Write <name>.clean<ext> beside a JPEG/PNG/WebP without "
                                      "its EXIF, GPS, XMP, IPTC, comment or text metadata (no re-encode)")
    strip_p.add_argument("file")
    strip_p.add_argument("--out", default=None, help="Default <stem>.clean<suffix> beside FILE; never overwritten")
    strip_p.set_defaults(directory=".")
    action_cmds = content_cmds.add_parser(
        "agency-action", help="Record or remove an action an agency took outside a meeting, as its official source states it "
        "(content/agency-actions.json only; nothing is built, sent or deployed)").add_subparsers(
        dest="agency_action_command", required=True)
    action_add = action_cmds.add_parser("add", help="Append one organizer-verified action; refuses a duplicate agency, date, kind and source")
    for flag, text in (("--directory", "Campaign directory (required)"), ("--agency", "Agency id from the loaded agencies"),
                       ("--on", "YYYY-MM-DD the action took effect"),
                       ("--as-stated", "1-200 plain-text characters exactly as the official source states it"),
                       ("--source-url", "https:// address of that official source (recorded, never fetched)"),
                       ("--checked-on", "YYYY-MM-DD you checked the source (not before --on, not in the future)")):
        action_add.add_argument(flag, required=True, help=text)
    action_add.add_argument("--kind", required=True, choices=ACTION_KINDS, help="What kind of action it was")
    action_remove = action_cmds.add_parser("remove", help="Remove one action by its 1-based position in the agency's stored order")
    action_remove.add_argument("--directory", required=True, help="Campaign directory (required)")
    action_remove.add_argument("--agency", required=True)
    action_remove.add_argument("--index", required=True, help="1-based position among that agency's actions in the file, not the page order")
    sharing_p = content_cmds.add_parser("agency-sharing", help="Record the agencies the vendor transparency portal names as receiving data, "
                                        "as the organizer transcribed them (CSV header name,state,kind; 1-2000 rows, all or none; never fetched)")
    sharing_p.add_argument("--directory", required=True)
    sharing_p.add_argument("--agency", required=True, help="agency_id in content/agencies.json")
    sharing_p.add_argument("--csv", required=True, help="Header name,state,kind; state is a two-letter code or US for federal")
    sharing_p.add_argument("--checked-on", required=True, metavar="YYYY-MM-DD", help="Day the organizer read the portal")
    sharing_p.add_argument("--source-url", required=True, help="https URL of the portal page (transparency.flocksafety.com)")
    sharing_p.add_argument("--replace", action="store_true", help="Overwrite an existing sharing list")
    coverage_cmds = content_cmds.add_parser("coverage", help="Record press coverage in content/coverage.json for /news.html "
                                            "(typed by the organizer; nothing is fetched, quoted or published)"
                                            ).add_subparsers(dest="coverage_command", required=True)
    coverage_add = coverage_cmds.add_parser("add", help="List one article: outlet, headline as published, https link, date")
    coverage_add.add_argument("--directory", required=True)
    coverage_add.add_argument("--outlet", required=True, help="1-80 characters")
    coverage_add.add_argument("--headline", required=True, help="The headline exactly as published, 1-200 characters")
    coverage_add.add_argument("--url", required=True, help="https link to the article (an exact, unique string)")
    coverage_add.add_argument("--published-on", required=True, metavar="YYYY-MM-DD", help="Publication date, not after today")
    coverage_add.add_argument("--finding", action="append", default=None, metavar="ID",
                              help="A published finding id the story reported; repeat for up to three")
    coverage_add.add_argument("--kind", default="news", choices=("news", "opinion", "broadcast", "other"))
    coverage_remove = coverage_cmds.add_parser("remove", help="Drop the one entry with exactly this URL")
    coverage_remove.add_argument("--directory", required=True)
    coverage_remove.add_argument("--url", required=True)
    finding_cmds = commands.add_parser("finding", help="Offline finding review receipts (private/reviews/)"
                                       ).add_subparsers(dest="finding_command", required=True)
    review_p = finding_cmds.add_parser("review", help="Record one review receipt for a draft (self-asserted unless --signature)")
    payload_p = finding_cmds.add_parser("review-payload", help="Print the receipt bytes to ssh-keygen -Y sign (read-only)")
    for flag in ("--directory", "--role", "--decision", "--reviewer", "--rationale"):
        review_p.add_argument(flag, required=True)
        payload_p.add_argument(flag, required=True)
    review_p.add_argument("--signature", metavar="SIG", help="SSH signature over the review-payload bytes")
    finding_cmds.add_parser("review-status", help="Print review_blockers for a draft and who was asked to review "
                            "each role (read-only)").add_argument("--directory", required=True)
    assign_p = finding_cmds.add_parser("assign", help="Record who was asked to review one role of a draft "
                                       "(private note; sends nothing and is not a review)")
    assign_p.add_argument("--directory", required=True)
    assign_p.add_argument("--role", required=True, help="factual, legal or privacy")
    assign_p.add_argument("--reviewer", required=True)
    assign_p.add_argument("--on", required=True, metavar="YYYY-MM-DD", help="Date asked, not after today")
    assign_p.add_argument("--replace", action="store_true", help="Replace a different reviewer already assigned")
    publish_p = finding_cmds.add_parser("publish", help="Publish a fully reviewed draft to content/findings/ (no build)")
    for flag in ("--directory", "--approved-by"):
        publish_p.add_argument(flag, required=True)
    reply_req_p = finding_cmds.add_parser("reply-request", help="Write a private, never-sent request for comment to the "
                                          "draft's agency (kit/replies/<id>.md); preparing it is not asking")
    reply_req_p.add_argument("--directory", required=True)
    reply_req_p.add_argument("--by", required=True, metavar="YYYY-MM-DD", help="Reply deadline, today or later")
    reply_p = finding_cmds.add_parser("reply", help="Record the agency's reply or silence on the draft (nothing is sent; "
                                      "any change needs fresh review)")
    reply_p.add_argument("--directory", required=True)
    reply_p.add_argument("--status", required=True, choices=("received", "declined", "no_response"))
    reply_p.add_argument("--on", required=True, metavar="YYYY-MM-DD", help="Date of the reply, or of the check for no_response")
    reply_p.add_argument("--asked-on", required=True, metavar="YYYY-MM-DD", help="Date YOU actually asked the agency, by hand; "
                         "an unsent prepared request is not evidence of asking")
    reply_p.add_argument("--text", help="The reply as written, 1-2000 characters (received needs it)")
    reply_p.add_argument("--record", metavar="PREFIX", help="With received: 12-64 hex of the library record holding the reply")
    preview_p = finding_cmds.add_parser("preview", help="Render one draft's finding page privately to --out "
                                        "(banner, noindex; never public/, feed or sitemap)")
    for flag in ("--directory", "--out"):
        preview_p.add_argument(flag, required=True)
    packet_p = finding_cmds.add_parser("packet", help="Private packet for an outside reviewer in a new --out folder")
    for flag in ("--directory", "--out"):
        packet_p.add_argument(flag, required=True)
    for sub in finding_cmds.choices.values():
        sub.add_argument("draft", metavar="DRAFT", help="Finding draft JSON file")
    plain_p = finding_cmds.add_parser("check-plain", help="Advisory plain-language check of a draft or published finding "
                                      "(read-only, never a gate; exit 3 when it has suggestions)")
    plain_p.add_argument("target", metavar="DRAFT|ID", help="Finding draft JSON file, or the id of a published finding")
    plain_p.add_argument("--directory", required=True)
    plain_p.add_argument("--json", dest="as_json", action="store_true", help="Print the report as one JSON object")
    status_p = finding_cmds.add_parser("status", help="Every draft, published and withdrawn finding with its state "
                                       "and blockers by name (read-only)")
    status_p.add_argument("--directory", required=True)
    status_p.add_argument("--draft", dest="extra_drafts", action="append", default=[], metavar="PATH",
                          help="Also list this draft (private/drafts/*.json are always listed); repeatable")
    status_p.add_argument("--json", dest="as_json", action="store_true", help="Print the rows as JSON")
    bind_p = finding_cmds.add_parser("bind-receipts", help="Bind this campaign's pre-binding review receipts to it "
                                     "(dry run unless --confirm; receipts bound elsewhere are never touched)")
    bind_p.add_argument("--directory", required=True)
    bind_p.add_argument("--confirm", action="store_true", help="Stamp and log them; without it nothing is written")
    correct_p = finding_cmds.add_parser("correct", help="Append a dated correction to a published finding (no build)")
    correct_p.add_argument("id", metavar="ID")
    for flag in ("--directory", "--date", "--note"):
        correct_p.add_argument(flag, required=True)
    correct_p.add_argument("--from", dest="from_draft", metavar="DRAFT", help="Reviewed draft with the corrected text")
    correct_p.add_argument("--approved-by", help="Named person approving this correction (the publication gate refuses without one)")
    update_p = finding_cmds.add_parser("update", help="Append a dated Update to a published finding for new information "
                                       "that does not change what it says (no build; not a correction)")
    update_p.add_argument("id", metavar="ID")
    for flag in ("--directory", "--on", "--note"):
        update_p.add_argument(flag, required=True)
    update_p.add_argument("--approved-by", help="Named person approving this update (the publication gate refuses without one)")
    withdraw_p = finding_cmds.add_parser("withdraw", help="Move a published finding to private/withdrawn/ (no notice page, no build)")
    withdraw_p.add_argument("id", metavar="ID")
    for flag in ("--directory", "--reason"):
        withdraw_p.add_argument(flag, required=True)
    roster_cmds = finding_cmds.add_parser("reviewers", help="Private roster in private/reviewers.json (roles, not identity)"
                                          ).add_subparsers(dest="reviewers_command", required=True)
    roster_add = roster_cmds.add_parser("add", help="Add a reviewer or merge roles into an existing one")
    roster_add.add_argument("--name", required=True)
    roster_add.add_argument("--role", action="append", required=True, help="factual, legal or privacy; repeatable")
    roster_add.add_argument("--ssh-key", metavar="FILE.pub", help="Set or replace this reviewer's signing key")
    roster_add.add_argument("--interest-agency", metavar="ID", action="append",
                            help="Agency this reviewer works for or otherwise declares; repeatable; replaces the list")
    roster_add.add_argument("--interest-note", metavar="T", help="Private note (1-200 characters); never printed or published")
    roster_add.add_argument("--clear-interests", action="store_true", help="Remove the declared agencies and note")
    roster_cmds.add_parser("list", help="Print the roster (read-only)")
    for sub in roster_cmds.choices.values():
        sub.add_argument("--directory", required=True)
    new_p = finding_cmds.add_parser("new", help="Write a private/drafts/fnd_<id>.json skeleton with [EDIT ME: ...] markers")
    new_p.add_argument("--directory", required=True)
    new_p.add_argument("--agency", help="Required unless --gap")
    new_p.add_argument("--title", help="Required unless --gap")
    new_p.add_argument("--source", action="append", default=[], metavar="PREFIX", help="12-64 hex of a library sha256; repeatable")
    new_p.add_argument("--author")
    new_p.add_argument("--gap", metavar="AGENCY", help="Draft an information_gap from this agency's denied or closed request "
                       "that produced no records (dates, stated basis and library correspondence prefilled)")
    new_p.add_argument("--request", help="With --gap: the request name (default main)")
    diff_p = finding_cmds.add_parser("diff", help="Field-level diff of public fields: ID --from DRAFT, or OLD NEW "
                                     "(read-only; exit 3 when they differ)")
    diff_p.add_argument("inputs", nargs="+", metavar="ID | OLD NEW")
    diff_p.add_argument("--directory", required=True)
    diff_p.add_argument("--from", dest="from_draft", metavar="DRAFT", help="Draft to compare with the published ID")
    diff_p.add_argument("--json", dest="as_json", action="store_true", help="Print the change list as JSON")
    cr_cmds = commands.add_parser("correction-request", help="Private log of correction requests and their decisions "
                                  "(nothing is sent or published)").add_subparsers(dest="cr_command", required=True)
    cr_add, cr_decide = cr_cmds.add_parser("add"), cr_cmds.add_parser("decide")
    for flag in ("--finding", "--received", "--summary", "--directory"):
        cr_add.add_argument(flag, required=True)
    cr_decide.add_argument("decision", choices=("accepted", "declined"))
    cr_decide.add_argument("--id", type=int, required=True)
    for flag in ("--on", "--note", "--directory"):
        cr_decide.add_argument(flag, required=True)
    cr_cmds.add_parser("list", help="Open requests first, with days_open").add_argument("--directory", required=True)
    today_p = commands.add_parser("today", help="One ranked to-do list with the command for each item (read-only)")
    today_p.add_argument("--directory", required=True)
    today_p.add_argument("--as-of", help="YYYY-MM-DD to plan from (default today)")
    today_p.add_argument("--owner", metavar="NAME", help="Only the request work assigned to this teammate label; "
                         "meetings, findings, contracts and contacts are still listed")
    today_p.add_argument("--json", dest="as_json", action="store_true", help="Print the items as JSON")
    from .today import CONTRACT_DAYS, REVIEW_DAYS, contract_days, review_days
    today_p.add_argument("--contract-days", type=contract_days, default=CONTRACT_DAYS, metavar="N", help="Contracts "
                         "ending within N days, default 90; ended in the last 30 days are always listed")
    today_p.add_argument("--review-days", type=review_days, default=REVIEW_DAYS, metavar="N", help="Assigned finding "
                         "reviews with no receipt after N days, default 7")
    recent_p = commands.add_parser("recent", help="Private what-changed report for a period: requests, records, findings, "
                                   "meetings (read-only; --md writes one new file)")
    recent_p.add_argument("--directory", required=True)
    recent_p.add_argument("--since", required=True, metavar="YYYY-MM-DD", help="First day of the period")
    recent_p.add_argument("--until", metavar="YYYY-MM-DD", help="Last day, not after today (default today)")
    recent_p.add_argument("--md", metavar="FILE", help="Also write the report as Markdown to this new 0600 file "
                          "(refused inside public/ or content/; never overwrites)")
    recent_p.add_argument("--json", dest="as_json", action="store_true", help="Print the events as JSON")

    law_packet = commands.add_parser("law", help="Law package tools; nothing here marks a package reviewed"
                                     ).add_subparsers(dest="law_command", required=True).add_parser(
        "review-packet", help="Private packet for a human law reviewer in a new --out folder; reviews nothing")
    law_packet.add_argument("--jurisdiction", required=True)
    law_packet.add_argument("--out", required=True, help="New absolute folder outside the repository")
    law_packet.add_argument("--as-of", help="Worksheet source ages counted to YYYY-MM-DD (default today)")
    law_packet.add_argument("--base", help=argparse.SUPPRESS)
    pack = commands.add_parser("meeting-pack", help="Write a private printable meeting pack; publishes nothing")
    pack.add_argument("--directory", required=True)
    pack_which = pack.add_mutually_exclusive_group(required=True)
    pack_which.add_argument("--id")
    pack_which.add_argument("--upcoming", action="store_true", help="Every staged meeting in the next --within-days")
    pack.add_argument("--within-days", type=int, help="With --upcoming: 1..90 days (default 14)")
    pack.add_argument("--max-findings", type=int, choices=range(1, 11), default=5)
    pack.add_argument("--replace", action="store_true")
    pack.add_argument("--allow-past", action="store_true")
    flyer = commands.add_parser("flyer", help="Write a private printable one-page campaign flyer; prints and publishes nothing")
    flyer.add_argument("--directory", required=True)
    flyer.add_argument("--max-findings", type=int, choices=range(1, 7), default=3)
    flyer.add_argument("--replace", action="store_true")
    _campaign_option(parser)
    campaigns = commands.add_parser("campaigns", help="A named register of campaign directories; names and paths only"
                                    ).add_subparsers(dest="campaigns_command", required=True)
    for verb, text in (("add", "Register NAME at --directory"), ("remove", "Unregister NAME; deletes nothing"),
                       ("list", "The registered names and directories"),
                       ("status", "One private line per campaign: counts and waiting gates; publishes nothing")):
        verb_p = campaigns.add_parser(verb, help=text)
        if verb in ("add", "remove"):
            verb_p.add_argument("name")
        if verb == "add":
            verb_p.add_argument("--directory", required=True)
    args = parser.parse_args()
    if args.command == "campaigns":
        sys.exit(registry.run(args))
    if getattr(args, "campaign", None):  # from here no path under another registered campaign can be opened or written
        registry.confine(args.directory, args.registered)
    if args.command == "meeting-pack" and (args.within_days is not None or args.upcoming):
        if not args.upcoming or args.allow_past or args.within_days not in (None, *range(1, 91)):
            pack.error("--within-days must be 1..90 with --upcoming; --allow-past is only for --id")
    if getattr(args, "finding_command", None) == "diff" and len(args.inputs) != (1 if args.from_draft else 2):
        diff_p.error("use either ID --from DRAFT, or OLD NEW")
    if getattr(args, "finding_command", None) == "new":
        if args.gap is not None and (args.agency is not None or args.source):
            new_p.error("--gap takes the agency from the request; it cannot be combined with --agency or --source")
        if args.gap is None and args.request is not None:
            new_p.error("--request is only for --gap")
        if args.gap is None and (args.agency is None or args.title is None):
            new_p.error("the following arguments are required: " + ", ".join(
                flag for flag, value in (("--agency", args.agency), ("--title", args.title)) if value is None)
                + " (or --gap AGENCY)")
    if getattr(args, "contact_max_age_days", None) is not None and args.contact_max_age_days < 1:
        parser.error("--contact-max-age-days must be at least 1")
    if getattr(args, "stale_days", None) is not None and not 1 <= args.stale_days <= 3650:
        parser.error("--stale-days must be 1 to 3650")
    if getattr(args, "json", False) and not args.checklist:
        parser.error("--json requires --checklist")
    if args.command == "social" and args.check and args.platform:
        parser.error("--check reads kit/social only; it cannot be combined with --platform")
    if args.command == "social" and args.meetings and args.check:
        parser.error("--check reads finding drafts only; it cannot be combined with --meetings")
    if args.command == "social" and args.outcomes and (args.meetings or args.check):
        parser.error("--outcomes cannot be combined with " + ("--meetings" if args.meetings else "--check (--check reads finding drafts only)"))
    if args.command == "social" and args.finding is not None and not args.thread:
        parser.error("--finding needs --thread")
    if args.command == "social" and args.thread:
        if args.check or args.meetings or args.outcomes or args.within_days is not None:
            parser.error("--thread cannot be combined with --check, --meetings, --outcomes or --within-days")
        if args.finding is None or len(args.finding) != 1:
            parser.error("--thread needs exactly one --finding ID")
    if args.command == "social" and args.within_days is not None and not (args.meetings or args.outcomes):
        parser.error("--within-days needs --meetings or --outcomes")
    if args.command == "social" and args.within_days is not None and not 1 <= args.within_days <= 60:
        parser.error("--within-days must be 1 to 60")
    if args.command == "build" and args.preview and args.budget is not None:
        parser.error("--budget measures a written build; it cannot be combined with --preview")
    if args.command == "build" and args.budget is None and (args.weights_out is not None or args.weights_baseline is not None):
        parser.error("--weights-out and --weights-baseline need --budget")
    if args.command == "sources" and args.sources_command == "add-batch":
        given = {"--source-id": args.source_id, "--agency-id": args.agency_id, "--received-at": args.received_at}
        if args.from_request is not None:
            if any(value is not None for value in given.values()):
                batch.error("--from-request takes the agency, records and dates from the request; "
                            "it cannot be combined with --source-id, --agency-id or --received-at")
            # Resolve defaults only after preserving whether request-only options were supplied.
            if args.request is None:
                args.request = "main"
            if args.tz is None:
                args.tz = "America/Los_Angeles"
        elif any(value is None for value in given.values()):
            batch.error("the following arguments are required: --source-id, --agency-id, --received-at "
                        "(or --from-request AGENCY)")
        elif args.request is not None or args.tz is not None:
            batch.error("--request and --tz are only for --from-request")
    if args.command in ("verify", "backup-drill", "backup-list"):
        try:
            {"verify": verify_cmd, "backup-drill": backup_drill_cmd, "backup-list": backup_list_cmd}[args.command](args)
        except (OSError, ValueError) as exc:
            fixes.emit(exc)
            sys.exit(1)
        return
    if args.command == "law":
        from .law_packet import main as law_packet_main
        sys.exit(law_packet_main(args))
    root = Path(args.directory).expanduser().resolve()
    try:
        if args.command == "init":
            initialize(args, root)
        elif args.command == "setup":
            from .setup_cmd import run_cli
            run_cli(args, root, parser, sys.stdin, sys.stdout)
        elif args.command == "import-existing":
            from .import_existing import run
            report = run(root, args.folder, args.label)
            print(json.dumps(report, indent=2))
            sys.exit(3 if report["stopped"] or report["counts"]["error"] else 0)
        elif args.command == "ingest":
            ingest(args, root)
        elif args.command == "kit":
            kit(args, root)
        elif args.command == "social" and args.check:
            from .social import check_social
            report = check_social(root)
            print(json.dumps(report, indent=2, ensure_ascii=False))
            sys.exit(3 if report["problems"] else 0)
        elif args.command == "social":
            social_cmd(root, args.platform, args.meetings, 14 if args.within_days is None else args.within_days, args.outcomes,
                       args.finding[0] if args.thread else None)
        elif args.command == "press":
            if args.press_command == "letter":
                from .press import build_letter
                result = build_letter(root, args.finding, args.date, words=args.words, outlet=args.outlet)
                print(json.dumps(result, indent=2, ensure_ascii=False))
                print(f"Letter word count: {result['words']}/{result['word_limit']}.")
                for note in result["review_flags"]:
                    print(note, file=sys.stderr)
                print("Private letter draft written. Nothing was sent.")
            else:
                from .press import build_press
                print(json.dumps(build_press(root, args.finding, args.date), indent=2, ensure_ascii=False))
                print("Draft written to kit/press. Nothing was sent.")
        elif args.command == "build" and args.preview:
            from .site import preview_site
            report = preview_site(root)
            print(json.dumps(report, indent=2))
            print("Preview only; public/ was not changed.")
            sys.exit(3 if report["added"] or report["removed"] or report["changed"] else 0)
        elif args.command == "build":
            code = build(root, check=args.check, budget=args.budget, weights_out=args.weights_out,
                         weights_baseline=args.weights_baseline)
            if code:
                sys.exit(code)
        elif args.command == "backup":
            backup_cmd(args, root)
        elif args.command == "restore":
            restore_cmd(args, root)
        elif args.command == "backup-diff":
            backup_diff_cmd(args, root)
        elif args.command == "meetings":
            meetings_cmd(args, root)
        elif args.command == "flyer":
            from .flyer import build as flyer_build
            print(json.dumps(flyer_build(Path(args.directory).expanduser(), args.max_findings, replace=args.replace)))
            print("Private file; nothing was printed, sent or published.")
        elif args.command == "meeting-pack" and args.upcoming:
            from .meeting_pack import build_upcoming
            report = build_upcoming(Path(args.directory).expanduser(), args.within_days or 14,
                                    max_findings=args.max_findings, replace=args.replace)
            print(json.dumps(report, indent=2))
            for built in report["built"]:
                if built["comment_sent"]:
                    print(f"{built['id']}: Comment recorded as submitted on {built['comment_sent']['on']} "
                          f"({built['comment_sent']['channel']}).")
            sys.exit(3 if report["skipped"] else 0)
        elif args.command == "meeting-pack":
            from .meeting_pack import build as pack_build
            report = pack_build(Path(args.directory).expanduser(), args.id, args.max_findings,
                                replace=args.replace, allow_past=args.allow_past)
            print(json.dumps(report))
            if report["comment_sent"]:
                print(f"Comment recorded as submitted on {report['comment_sent']['on']} "
                      f"({report['comment_sent']['channel']}).")
        elif args.command == "meetings-stage":
            meeting_stage_cmd(args, root)
        elif args.command == "meetings-outcome":
            from .meeting_outcome import run_outcome_command
            print("\n".join(run_outcome_command(root, args)))
        elif args.command == "meetings-comment-sent":
            from .meeting_comments import record
            entry = record(root, args.id, args.on, args.channel, args.file, args.replace)
            shown = {"id": args.id, **{k: v for k, v in entry.items() if k != "letter_sha256"},
                     **({"letter_sha256_prefix": entry["letter_sha256"][:12]} if "letter_sha256" in entry else {})}
            print(json.dumps(shown, indent=2))
            print("Recorded only. Nothing was sent.")
        elif args.command == "supporters":
            from .supporters import add_supporter, remove_supporter, set_cosign_consent
            if args.supporters_command == "consent":
                result = set_cosign_consent(root, args.id, cosign=args.cosign, clear=args.clear_cosign,
                                            confirmed_on=args.confirmed_on, how=args.how, correspondence=args.correspondence)
            elif args.supporters_command == "remove":
                result = remove_supporter(root, args.id)
            else:
                result = add_supporter(root, name=args.name, kind=args.kind, organization=args.organization,
                                       confirmed_on=args.confirmed_on, how=args.how, url=args.url,
                                       correspondence=args.correspondence)
            print(json.dumps(result))
            print("Recorded only. Nothing was published.")
        elif args.command == "sources" and args.sources_command == "add":
            from .sources_cmd import add_source
            print(json.dumps(add_source(root, sha=args.sha256, title=args.title, agency_id=args.agency_id,
                                        received_at=args.received_at, fragment=args.fragment,
                                        public_url=args.public_url, pages=args.pages, note=args.note)))
        elif args.command == "sources" and args.sources_command == "list":
            from .sources_cmd import list_sources
            print(json.dumps(list_sources(root), indent=2, ensure_ascii=False))
        elif args.command == "sources" and args.sources_command == "add-batch":
            from .sources_cmd import add_batch, add_from_request
            if args.from_request is not None:
                result = add_from_request(root, args.from_request, request=args.request, tz=args.tz,
                                          fragment=args.fragment, dry_run=args.dry_run)
                print(json.dumps(result))
                if not result["added"]:
                    print("nothing to add")
            else:
                print(json.dumps(add_batch(root, args.source_id, args.agency_id, args.received_at, args.fragment,
                                           dry_run=args.dry_run)))
        elif args.command == "sources" and args.sources_command == "verify-copy":
            from .sources_cmd import verify_copy
            try:
                print(json.dumps(verify_copy(root, args.sha256, args.file, args.checked_on, args.differs_because,
                                              fragment=args.fragment)))
            except ValueError as exc:
                print(f"Stopped: {exc}", file=sys.stderr)
                sys.exit(3)
        elif args.command == "sources":
            from .sources_cmd import edit_source, remove_source
            print(json.dumps(remove_source(root, args.sha256, args.fragment) if args.sources_command == "remove" else
                             edit_source(root, args.sha256, args.fragment, title=args.title, note=args.note,
                                         public_url=args.public_url, pages=args.pages, clear_public_url=args.clear_public_url)))
            print("Nothing was published.")
        elif args.command == "finding" and args.finding_command == "diff":
            from .finding_diff import run_cli as diff_cli
            sys.exit(diff_cli(root, args.inputs, args.from_draft, args.as_json))
        elif args.command == "finding" and args.finding_command == "check-plain":
            from .plain_language import run_cli as plain_cli
            read_config(root)
            code = plain_cli(root, args.target, args.as_json)
            if code:
                sys.exit(code)
        elif args.command == "finding" and args.finding_command == "reviewers":
            from . import findings
            read_config(root)
            add = args.reviewers_command == "add"
            print(json.dumps(findings.add_reviewer(root, args.name, args.role, ssh_key=args.ssh_key,
                                                   interest_agencies=args.interest_agency,
                                                   interest_note=args.interest_note,
                                                   clear_interests=args.clear_interests) if add
                             else findings.list_reviewers(root), indent=2, ensure_ascii=False))
            if add:
                print("Private roster updated; nothing was published.")
        elif args.command == "finding" and args.finding_command == "preview":
            try:
                result = finding_preview(root, args.draft, args.out)
            except (OSError, ValueError) as exc:  # load_draft/load_finding refusals: exit 2, nothing written
                fixes.emit(exc)
                sys.exit(2)
            print(json.dumps(result))
            print("Private preview only; public/ was not changed and nothing was published.")
            from .plain_language import preview_lines
            for line in preview_lines(root, args.draft):  # DF0022-04: advisory only; never changes the exit code
                print(line)
        elif args.command == "finding" and args.finding_command == "packet":
            from .finding_packet import build_packet
            try:
                print(json.dumps(build_packet(root, args.draft, args.out)))
            except (OSError, ValueError) as exc:
                sys.exit(fixes.emit(exc) or 2)
            print("Private packet only; nothing was zipped, uploaded or sent.")
        elif args.command == "finding" and args.finding_command == "new":
            from . import findings
            read_config(root)
            if args.gap is not None:
                result = findings.new_gap_draft(root, args.gap, args.request or "main", args.title, author=args.author)
                print(json.dumps({key: value for key, value in result.items() if key != "blockers"}))
                for prefix in result["blockers"]:
                    print(f"blocker: {prefix} is not in the source library (sources add-batch --from-request or sources add)")
            else:
                print(json.dumps(findings.new_draft(root, args.agency, args.title, args.source, author=args.author)))
            print('Private draft only; replace every "[EDIT ME: ...]" before review. Nothing was published.')
        elif args.command == "finding":
            from . import findings
            read_config(root)
            if args.finding_command == "status":
                rows = findings.status_rows(root, [Path(p).expanduser() for p in args.extra_drafts])
                print(json.dumps(rows, indent=2, ensure_ascii=False) if args.as_json else findings.status_table(rows))
                return
            if args.finding_command == "bind-receipts":
                print(json.dumps(findings.bind_receipts(root, confirm=args.confirm)))
                print("Receipts bound and logged in private/reviews/binding-log.jsonl; nothing was published."
                      if args.confirm else "Dry run: Nothing was changed. Check that every listed receipt was granted "
                      "in this campaign, then rerun with --confirm.")
                return
            if args.finding_command == "correct":
                print(json.dumps(findings.correct(root, args.id, args.date, args.note, args.from_draft, None, args.approved_by)))
                print("Not built or deployed. Run build, then review the site before any deploy.")
                return
            if args.finding_command == "update":
                from .legal_claims import legal_notes
                print(json.dumps(findings.update(root, args.id, args.on, args.note, None, args.approved_by)))
                for line in legal_notes([("--note", args.note)]):  # alert-only: rule names, never the note
                    print(line, file=sys.stderr)
                print("Not built or deployed. Run build, then review the site before any deploy.")
                return
            if args.finding_command == "withdraw":
                print(json.dumps(findings.withdraw(root, args.id, args.reason)))
                print("Run build to remove it from public/. Nothing was deployed.")
                return
            if args.finding_command == "publish":
                result = findings.publish(root, args.draft, args.approved_by)
                print(json.dumps({k: v for k, v in result.items() if k != "receipt_trust"}))
                print("Not built or deployed. Run build, then review the site before any deploy.")
                print(findings.trust_line(result["receipt_trust"]))
                return
            if args.finding_command == "reply-request":
                print(json.dumps(findings.reply_request(root, args.draft, args.by)))
                print("Recorded only. Nothing was sent. Asking the agency is your manual step; "
                      "record it with finding reply --asked-on.")
                return
            if args.finding_command == "reply":
                print(json.dumps(findings.record_reply(root, args.draft, args.status, args.on, args.asked_on,
                                                       text=args.text, record=args.record)))
                print("Recorded only. Nothing was sent. The reply changes the reviewed text: review it again.")
                return
            draft = findings.load_draft(args.draft)
            if args.finding_command == "review-payload":
                sys.stdout.buffer.write(findings.review_payload(root, draft, args.role, args.decision, args.reviewer, args.rationale))
                return
            if args.finding_command == "assign":
                print(json.dumps(findings.assign(root, draft, args.role, args.reviewer, args.on, replace=args.replace),
                                 indent=2, ensure_ascii=False))
                print("Recorded only. Nothing was sent. An assignment is not a review.")
                return
            print(json.dumps(findings.add_receipt(root, draft, args.role, args.decision, args.reviewer, args.rationale, signature=args.signature)
                             if args.finding_command == "review" else
                             {**findings.review_status(root, draft), "assignments": findings.assignment_states(root, draft)},
                             indent=2))
        elif args.command == "correction-request":
            from . import correction_requests as cr
            read_config(root)
            try:
                result = (cr.add(root, args.finding, args.received, args.summary) if args.cr_command == "add" else
                          cr.decide(root, args.id, args.decision, args.on, args.note) if args.cr_command == "decide"
                          else cr.list_requests(root))
            except ValueError as exc:
                fixes.emit(exc)
                sys.exit(3)
            print(json.dumps(result, indent=2, ensure_ascii=False) + "\nPrivate log only; nothing was sent or published.")
        elif args.command == "content-check":
            from .content_check import run_cli as content_check_cli
            content_check_cli(root, as_json=args.as_json)
        elif args.command == "evidence":
            from .evidence import run_cli
            run_cli(root, check=args.check)
        elif args.command == "kit-verify-agency" and args.action == "import":
            from .kit import verify_agencies_csv
            single = [f for f in ("agency_id", "source_url", "checked_on", "records_email", "records_url", "portal",
                                "mailing_address")
                      if getattr(args, f) is not None]
            if single:
                verify_agency_p.error("import takes only --directory, --csv and --check, not "
                                      + ", ".join("--" + f.replace("_", "-") for f in single))
            if args.csv is None:
                verify_agency_p.error("the following arguments are required: --csv")
            print(json.dumps(verify_agencies_csv(root, Path(args.csv).expanduser(), check=args.check), indent=2))
            print("Nothing was fetched or sent.")
        elif args.command == "kit-verify-agency":
            from .kit import verify_agency
            stray = [f"--{f}" for f in ("csv",) if getattr(args, f) is not None] + ["--check"] * args.check
            if stray:
                verify_agency_p.error(", ".join(stray) + " only with 'import'")
            if missing := [f"--{f.replace('_', '-')}" for f in ("agency_id", "source_url", "checked_on")
                           if getattr(args, f) is None]:
                verify_agency_p.error("the following arguments are required: " + ", ".join(missing))
            print(json.dumps(verify_agency(root, args.agency_id, source_url=args.source_url, checked_on=args.checked_on,
                                           records_email=args.records_email, records_url=args.records_url,
                                           portal=args.portal, mailing_address=args.mailing_address), indent=2))
            print("Nothing was sent.")
        elif args.command == "content" and args.content_command == "strip-metadata":
            from .image_meta import strip_file
            print(json.dumps(strip_file(args.file, args.out), indent=2))
        elif args.command == "content" and args.content_command == "agency-action":
            from . import agency_actions
            if args.agency_action_command == "add":
                done = agency_actions.add_action(root, args.agency, on=args.on, kind=args.kind, as_stated=args.as_stated,
                                                 source_url=args.source_url, checked_on=args.checked_on)
                for note in done["notes"]:  # alert-only legal-claim notes: stderr, never the supplied text, exit unchanged
                    print(note, file=sys.stderr)
                print(f"Agency action added: {done['agency_id']}, index {done['index']}.")
            else:
                done = agency_actions.remove_action(root, args.agency, agency_actions.parse_index(args.index))
                print(f"Agency action removed: {done['agency_id']}, index {done['index']}.")
            print("Content only; nothing was built, sent or deployed.")
        elif args.command == "content" and args.content_command == "agency-sharing":
            from .content_sharing import import_sharing
            print(json.dumps(import_sharing(root, args.agency, Path(args.csv).expanduser(), args.checked_on, args.source_url,
                                            replace=args.replace), indent=2))
            print("Recorded only. Nothing was fetched or published.")
        elif args.command == "content" and args.content_command == "coverage":
            from .coverage import add_coverage, remove_coverage
            if args.coverage_command == "remove":
                result = remove_coverage(root, url=args.url)
            else:
                result = add_coverage(root, outlet=args.outlet, headline=args.headline, url=args.url,
                                      published_on=args.published_on, finding_ids=args.finding, kind=args.kind)
            print(json.dumps(result))
            print("Nothing was built, published or sent.")
        elif args.command == "content":
            from .content_init import init_content
            print(json.dumps(init_content(root, args.source_campaign), indent=2))
            print(f"content/ written from {Path(args.source_campaign).name}. Review every copied page and replace anything specific to the other locality; nothing was built or deployed." if args.source_campaign is not None else 'content/ written. Replace every "[EDIT ME: ...]" before you publish; nothing was built or deployed.')
        elif args.command == "kit-agency" and args.agency_command == "atlas":
            from . import kit_atlas
            report = kit_atlas.compare_atlas(root, Path(args.csv).expanduser())
            print(json.dumps(report, indent=2, ensure_ascii=False) if args.as_json else kit_atlas.render_atlas(report))
        elif args.command == "kit-agency":
            from . import kit_agencies
            print(json.dumps(kit_agencies.add_agency(root, args.agency_id, args.name, args.kind, note=args.note)
                             if args.agency_command == "add" else
                             kit_agencies.import_csv(root, Path(args.csv).expanduser(), check=args.check)
                             if args.agency_command == "import" else
                             kit_agencies.set_selected(root, args.agency_id, args.agency_command == "select"),
                             indent=2, ensure_ascii=False))
            print("Nothing was sent.")
        elif args.command == "kit-sent":
            from .kit_track import record_sent
            print(json.dumps(record_sent(root, args.agency, args.date, args.channel, request=args.request,
                                         replace=args.replace, reference=args.reference,
                                         replace_reference=args.replace_reference,
                                         correspondence=args.correspondence, request_url=args.request_url,
                                         covers_through=args.covers_through), indent=2))
            print("Recorded only. Nothing was sent.")
        elif args.command == "kit-repeat":
            from .kit_repeat import repeat_request
            print(json.dumps(repeat_request(root, args.agency, args.request, args.new_request, through=args.through,
                                            date_from=args.date_from), indent=2))
            print("Unsent draft written. Review it, then kit-check and kit-sent. Nothing was sent.")
        elif args.command == "kit-sent-import":
            from .kit_import_sent import import_sent
            print(json.dumps(import_sent(root, Path(args.csv).expanduser(), apply=args.apply), indent=2))
            print("Recorded only. Nothing was sent." if args.apply else "Check only. Nothing was written or sent.",
                  file=sys.stderr)
        elif args.command == "kit-muckrock-import":
            from .kit_muckrock import render, run as muckrock_run
            result = muckrock_run(root, args.agency, Path(args.from_json).expanduser(), args.request, args.apply)
            print(json.dumps(result, indent=2) if args.as_json else render(result))
            print(f"Read only from {args.from_json}. Nothing was fetched or sent.")
            if result.get("refusal"):
                raise ValueError(result["refusal"])
        elif args.command == "kit-response-import":
            from .kit_import_steps import import_steps, render
            result = import_steps(root, args.agency, Path(args.csv).expanduser(), request=args.request, apply=args.apply)
            print(render(result))
            print("Recorded only. Nothing was fetched or sent." if result["applied"] else
                  "Checked only; nothing was written, fetched or sent.")
        elif args.command == "kit-check":
            from .kit_check import run_cli as kit_check_cli
            kit_check_cli(root, as_json=args.as_json)
        elif args.command == "kit-copy":
            from .kit_copy import run_cli as kit_copy_cli
            kit_copy_cli(root, args.agency, args.copy_for, as_json=args.as_json)
        elif args.command == "kit-status" and args.csv:
            from .kit_export import status_csv
            print(json.dumps(status_csv(root, args.csv, request=args.request, owner=args.owner), indent=2))
            print("Private file; nothing was sent or published.")
        elif args.command == "kit-status":
            from .kit_track import OVER_CAP, status as kit_track_status
            report = kit_track_status(root, request=args.request, owner=args.owner)
            print(json.dumps(report, indent=2))
            for row in report["requests"]:  # a reminder on stderr; stdout stays one JSON document
                label = row["agency_id"] + ("" if row["request"] == "main" else "/" + row["request"])
                if "withdrawn_on" in row:  # ended by us: history stays in the JSON, but no reminder is actionable
                    print(f"{label}: withdrawn by us on {row['withdrawn_on']}", file=sys.stderr)
                if row["over_fee_cap"] and "withdrawn_on" not in row:
                    print(f"{label}: fee estimate {row['fee_estimate']} is {OVER_CAP}", file=sys.stderr)
                if row["fee_state"] == "unpaid" and "withdrawn_on" not in row:
                    print(f"{label}: fee estimate {row['fee_estimate']} is unpaid", file=sys.stderr)
                if row.get("undone_steps"):
                    print(f"{label}: {row['undone_steps']} step(s) undone", file=sys.stderr)
                for item in row.get("downloads_pending", ()):
                    print(f"{label}: {item['line']}", file=sys.stderr)
                if row["channel"] == "portal" and row.get("request_url"):
                    print(f"{label}: Portal request: {row['request_url']}", file=sys.stderr)
        elif args.command == "kit-fees":
            from . import kit_fees
            report = kit_fees.fees(root)
            print(json.dumps(kit_fees.write_csv(root, args.csv, report) if args.csv else report, indent=2))
            print("Private file; nothing was sent or published." if args.csv else "Read-only. Nothing was sent.",
                  file=sys.stdout if args.csv else sys.stderr)
        elif args.command == "kit-timing":
            from . import kit_timing
            report = kit_timing.report(root, args.as_of)
            if args.csv:
                kit_timing.check_csv(root, args.csv)  # a refused path prints no table
            print(kit_timing.format_report(report))
            if args.csv:
                print(json.dumps(kit_timing.write_csv(root, args.csv, report), indent=2))
            print("Read-only. Nothing was sent.", file=sys.stderr)
        elif args.command == "kit-coverage":
            from .kit_track import coverage, format_coverage
            sys.stdout.write(format_coverage(coverage(root), as_csv=args.csv))
            print("Read-only. Nothing was sent.", file=sys.stderr)
        elif args.command == "today":
            from .today import run_cli as today_cli
            read_config(root)  # not a campaign folder: Stopped, before any source is read
            print(today_cli(root, args.as_of, args.as_json, args.contract_days, args.review_days, owner=args.owner))
        elif args.command == "recent":
            from .recent import run_cli as recent_cli
            read_config(root)
            code = recent_cli(root, args.since, args.until, args.md, args.as_json)
            if code:
                sys.exit(code)

        elif args.command == "kit-due":
            from .kit_track import due as kit_track_due
            report = kit_track_due(root, args.as_of, request=args.request, owner=args.owner)
            print(json.dumps(report, indent=2))
            if report["past_due"] + report["stated_date_passed"] + report["installment_passed"] \
                    + report.get("download_expired", 0):
                sys.exit(3)
        elif args.command == "kit-assign":
            from .kit_assign import assign
            print(json.dumps(assign(root, args.agency, request=args.request, to=args.to, clear=args.clear),
                             indent=2, ensure_ascii=False))
            print("Recorded only. Nothing was sent.")
        elif args.command == "kit-calendar":
            from .kit_calendar import calendar
            try:
                print(json.dumps(calendar(root, args.out, args.as_of), indent=2))
            except ValueError as exc:
                print(f"Stopped: {exc}", file=sys.stderr)
                sys.exit(3)
        elif args.command == "kit-response":
            from .kit_track import record_step
            print(json.dumps(record_step(root, args.agency, args.date, args.kind, note=args.note, records=args.record,
                                         request=args.request, amount=args.amount, expected_on=args.expected_on,
                                         reference=args.reference, replace_reference=args.replace_reference,
                                         partial=args.partial, correspondence=args.correspondence,
                                         withheld_basis=args.withheld_basis, scope_ids=args.scope,
                                         disposition=args.disposition, download_expires_on=args.download_expires_on),
                             indent=2))
            print("Recorded only. Nothing was sent.")
        elif args.command == "kit-response-undo":
            from .kit_undo import undo_step
            result = undo_step(root, args.agency, request=args.request, reason=args.reason)
            print(f"{result['label']}: removed the {result['kind']} step of {result['on']}; "
                  f"{result['undone_steps']} step(s) undone.")
            print("Recorded only. Nothing was sent.")
        elif args.command == "kit-asked":
            from .kit_asked import run_cli as kit_asked_cli
            code = kit_asked_cli(root, args.apply)
            if code:
                sys.exit(code)
        elif args.command == "kit-withheld":
            from .kit_track import format_withheld, withheld
            report = withheld(root)
            print(json.dumps(report, indent=2) if args.as_json else format_withheld(report), end="\n" if args.as_json else "")
            print(report["note"], file=sys.stderr)
        elif args.command == "status" and (args.list or args.not_in_sources or (args.source_id, args.since) != (None, None)):
            read_config(root)
            from .ledger_list import run_cli as list_cli
            print(json.dumps(list_cli(args, root), indent=2))
        elif args.command == "kit-followup":
            from .kit_track import followup
            print(json.dumps(followup(root, args.agency, args.as_of, replace=args.replace, request=args.request,
                                      cite_contact=args.cite_contact), indent=2))
            print("Draft only. Nothing was sent.")
        elif args.command == "kit-contact":
            from .kit_track import record_contact
            print(json.dumps(record_contact(root, args.agency, args.on, args.channel, args.role, args.note,
                                            request=args.request), indent=2))
            print("Recorded only. Nothing was sent.")
        elif args.command == "kit-preserve":
            from .kit_preserve import preserve_request
            print(json.dumps(preserve_request(Path(args.directory).expanduser(), args.agency, request=args.request), indent=2))
            print("Draft only. Nothing was sent.")
        elif args.command == "kit-denial-reply":
            from .kit_track import denial_reply
            print(json.dumps(denial_reply(root, args.agency, args.rule, replace=args.replace), indent=2))
            print("Draft only. Nothing was sent.")
        elif args.command == "readiness":
            readiness(root, args.campaign_id, args.out, args.contact_max_age_days, args.stale_days)
        elif args.command == "doctor" and args.checklist:
            from .doctor_checklist import render
            print(render(doctor_report(root, args.contact_max_age_days, args.stale_days), args.directory, as_json=args.json,
                         agency_list_exists=(root / "kit" / "agencies.json").is_file()))
        else:
            if args.command == "status":
                status(root, check=args.check, fix=args.fix_modes)
            else:
                doctor(root, args.contact_max_age_days, args.stale_days)
    except (OSError, ValueError, sqlite3.Error) as exc:
        fixes.emit(exc)
        sys.exit(getattr(exc, "exit_code", 1))  # kit_track.WaitingOnYou: 3
