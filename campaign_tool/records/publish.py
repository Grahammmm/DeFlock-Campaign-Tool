"""Owner approval and staged publication with correction and rollback.

The owner's approval is the only gate between a privacy-cleared proposal and
the staging target, and it binds to the exact public bytes (``approved_content_sha256``).
``records publish`` refuses any file whose hash differs from the approved hash,
writes the exact bytes to a staging directory with a versioned release manifest,
and records the publication in the ledger. ``--rollback`` restores the previous
release for that proposal. There is no live target in this module; wiring the
staging directory to a site repository or Worker is a separate adapter and a
separate owner approval.
"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
import re
from pathlib import Path
import sys
import stat
import uuid

from .ledger import store
from . import review_retirement
from .run import Root, root_lock

VERSION = "records-publish-v2"
DECISIONS_SQL = """
CREATE TABLE IF NOT EXISTS owner_decisions(
 id TEXT PRIMARY KEY, proposal_id TEXT NOT NULL REFERENCES proposals(id),
 decision TEXT NOT NULL CHECK(decision IN ('approved','rejected','revoked')),
 owner_id TEXT NOT NULL, content_sha256 TEXT NOT NULL, reason TEXT, decided_at TEXT NOT NULL);
"""


class PublishError(ValueError):
    pass


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def require(condition, reason):
    if not condition:
        raise PublishError(reason)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat()


def _identity(value):
    require(isinstance(value, str) and 0 < len(value) <= 128 and value.strip() == value and
            all(c.isalnum() or c in "._:@-" for c in value), "invalid_owner_identity")
    return value


def _proposal(con, proposal_id):
    row = con.execute("SELECT * FROM proposals WHERE id=?", (proposal_id,)).fetchone()
    require(row is not None, "proposal_missing")
    return dict(row)


def _public_bytes(root, proposal_id):
    require(isinstance(proposal_id, str) and re.fullmatch(r"prop_[A-Za-z0-9_-]{1,123}", proposal_id),
            "proposal_id_invalid")
    path = root.sub("proposals/public") / (proposal_id + ".md")
    return _read(path, "public_file_missing")


def _subject(root, proposal):
    """The private manifest binds the proposal to its original; its hash is in the ledger row."""
    path = root.sub("proposals/private") / (proposal["id"] + ".manifest.json")
    raw = _read(path, "manifest_missing")
    require(sha(raw) == proposal["manifest_sha256"], "manifest_changed")
    manifest = json.loads(raw)
    subject = manifest.get("original_sha256")
    require(isinstance(subject, str) and len(subject) == 64, "manifest_subject_invalid")
    return subject


def _privacy_current(con, root, proposal):
    """The privacy stage must still be done with the receipt this proposal was built from."""
    subject = _subject(root, proposal)
    state = con.execute("SELECT status,receipt_sha256 FROM stage_state WHERE stage='privacy' AND original_sha256=?",
                        (subject,)).fetchone()
    require(state is not None and state["status"] == "done" and
            state["receipt_sha256"] == proposal["privacy_receipt_sha256"], "privacy_clearance_not_current")
    return subject


def _retirement_clear(con):
    try:
        require(not review_retirement.pending(con), "review_authority_retirement_pending")
    except review_retirement.RetirementError:
        raise PublishError("review_authority_retirement_state_invalid") from None


def approve(root, proposal_id, *, owner_id, reject=False, reason=None):
    with _authority(root) as root_:
        return _approve_locked(root_, proposal_id, owner_id=owner_id, reject=reject, reason=reason)


def _approve_locked(root, proposal_id, *, owner_id, reject=False, reason=None):
    """Bind the owner's decision to the exact public bytes on disk and in the ledger."""
    owner = _identity(owner_id)
    public = _public_bytes(root, proposal_id)
    digest = sha(public)
    with store.ledger(root.ledger) as con:
        con.executescript(DECISIONS_SQL)
        con.execute("BEGIN IMMEDIATE")
        try:
            proposal = _proposal(con, proposal_id)
            if not reject:
                _retirement_clear(con)
            require(proposal["public_content_sha256"] == digest, "public_bytes_changed_since_privacy_review")
            _privacy_current(con, root, proposal)
            decision = "rejected" if reject else "approved"
            if not reject:
                require(proposal["owner_approval"] != "approved" or
                        proposal["approved_content_sha256"] == digest, "already_approved_other_content")
            if proposal["owner_approval"] == decision and (reject or proposal["approved_content_sha256"] == digest):
                con.rollback()
                return {"proposal_id": proposal_id, "decision": decision, "content_sha256": digest, "reused": True}
            require(reject or reason is None or isinstance(reason, str), "invalid_reason")
            decision_id = sha(("|".join([proposal_id, decision, owner, digest, now()])).encode())[:32]
            con.execute("INSERT INTO owner_decisions VALUES(?,?,?,?,?,?,?)",
                        (decision_id, proposal_id, decision, owner, digest, reason, now()))
            con.execute("UPDATE proposals SET owner_approval=?,approved_content_sha256=? WHERE id=?",
                        (decision, None if reject else digest, proposal_id))
            con.commit()
        except BaseException:
            con.rollback()
            raise
    receipt = {"schema": "records-owner-decision-v1", "proposal_id": proposal_id, "decision": decision,
               "owner_id": owner, "content_sha256": digest, "reason": reason, "decided_at": now()}
    path = root.sub("proposals/private") / (proposal_id + "." + decision + ".json")
    _write(path, encoded(receipt))
    return {"proposal_id": proposal_id, "decision": decision, "content_sha256": digest, "reused": False,
            "receipt": str(path)}


# Additive private journal: no filesystem release is trusted without this ledger binding.
JOURNAL_SQL = """
CREATE TABLE IF NOT EXISTS staging_journal(
 version TEXT PRIMARY KEY, target TEXT NOT NULL, proposal_id TEXT NOT NULL REFERENCES proposals(id),
 manifest BLOB NOT NULL, content BLOB, before_current BLOB, after_current BLOB NOT NULL,
 before_content BLOB, state TEXT NOT NULL CHECK(state IN ('prepared','complete','aborted')));
CREATE TRIGGER IF NOT EXISTS staging_journal_immutable BEFORE UPDATE ON staging_journal
 WHEN OLD.version IS NOT NEW.version OR OLD.target IS NOT NEW.target OR
 OLD.proposal_id IS NOT NEW.proposal_id OR OLD.manifest IS NOT NEW.manifest OR
 OLD.content IS NOT NEW.content OR OLD.before_current IS NOT NEW.before_current OR
 OLD.after_current IS NOT NEW.after_current OR OLD.before_content IS NOT NEW.before_content OR
 OLD.state!='prepared' OR NEW.state NOT IN ('complete','aborted')
 BEGIN SELECT RAISE(ABORT,'staging journal is immutable'); END;
CREATE TRIGGER IF NOT EXISTS staging_journal_no_replace BEFORE INSERT ON staging_journal
 WHEN EXISTS(SELECT 1 FROM staging_journal WHERE version=NEW.version)
 BEGIN SELECT RAISE(ABORT,'staging journal is immutable'); END;
CREATE TRIGGER IF NOT EXISTS staging_journal_no_delete BEFORE DELETE ON staging_journal
 BEGIN SELECT RAISE(ABORT,'staging journal is immutable'); END;
"""


def _safe_path(path):
    path = Path(os.path.abspath(path))
    require(not any(p.is_symlink() for p in (path, *path.parents)), "staging_symlink")
    return path


@contextmanager
def _directory(path, *, create=False):
    """Pin every ancestor; never traverse a link between checking and opening."""
    path = _safe_path(path)
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:]:
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=fd)
                    os.fsync(fd)
                except FileExistsError:
                    pass
            following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = following
        yield fd
    finally:
        os.close(fd)


def _read(path, reason="staging_file_missing", *, optional=False):
    path = _safe_path(path)
    try:
        with _directory(path.parent) as directory:
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    except FileNotFoundError:
        require(optional, reason)
        return None
    with os.fdopen(fd, "rb") as stream:
        require(stat.S_ISREG(os.fstat(stream.fileno()).st_mode), "staging_file_invalid")
        return stream.read()


def _json(raw):
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        raise PublishError("staging_manifest_invalid") from None
    require(isinstance(value, dict), "staging_manifest_invalid")
    return value


def _staging(target):
    target = _safe_path(target)
    with _directory(target, create=True):
        pass
    for name in ("content", "releases"):
        with _directory(target / name, create=True):
            pass
    return target


def _write(path, data):
    path = _safe_path(path)
    with _directory(path.parent) as directory:
        temporary = "." + path.name + "." + uuid.uuid4().hex + ".tmp"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path.name, src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        finally:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass


def _restore(path, data):
    if data is None:
        path = _safe_path(path)
        with _directory(path.parent) as directory:
            try:
                os.unlink(path.name, dir_fd=directory)
            except FileNotFoundError:
                pass
            os.fsync(directory)
    else:
        _write(path, data)


def _recover(con, target):
    """Undo interrupted output using ledger bytes, never replay an old approval.

    The root lock and target lock are held, with a SQLite write transaction. The
    prior output and current pointer can each be on either side of an atomic
    replace. Anything else is an unexplained edit and requires reconciliation.
    """
    rows = con.execute("SELECT * FROM staging_journal WHERE target=? AND state='prepared' ORDER BY rowid",
                       (str(target),)).fetchall()
    for row in rows:
        content_path = target / "content" / (row["proposal_id"] + ".md")
        actual = _read(content_path, optional=True)
        current = _read(target / "current.json", optional=True)
        require(actual in (row["before_content"], row["content"]) and
                current in (row["before_current"], row["after_current"]), "interrupted_staging_changed")
        _restore(content_path, row["before_content"])
        _restore(target / "current.json", row["before_current"])
        con.execute("UPDATE staging_journal SET state='aborted' WHERE version=?", (row["version"],))


def _recover_proposals(con, root):
    """Repair an interrupted proposal projection from the immutable reviewed revision."""
    if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='proposal_revisions'").fetchone() is None:
        return
    fields = ("public_content_sha256", "manifest_sha256", "privacy_receipt_sha256", "independent_receipt_sha256")
    rows = con.execute("SELECT * FROM proposal_revisions WHERE state='pending' ORDER BY rowid").fetchall()
    for row in rows:
        proposal = _proposal(con, row["proposal_id"])
        bindings = tuple(row[field] for field in fields)
        require(tuple(proposal[field] for field in fields) == bindings and
                row["revision_id"] == sha(encoded({"proposal_id": row["proposal_id"], "bindings": bindings})) and
                sha(row["public_content"]) == row["public_content_sha256"] and
                sha(row["manifest_content"]) == row["manifest_sha256"], "proposal_revision_changed")
        _write(root.sub("proposals/public") / (row["proposal_id"] + ".md"), row["public_content"])
        _write(root.sub("proposals/private") / (row["proposal_id"] + ".manifest.json"), row["manifest_content"])
        con.execute("UPDATE proposal_revisions SET state='complete' WHERE revision_id=?", (row["revision_id"],))


@contextmanager
def _authority(root):
    root = Root(root)
    with root_lock(root.path):
        with store.ledger(root.ledger) as con:
            con.executescript(JOURNAL_SQL)
            con.execute("BEGIN IMMEDIATE")
            _recover_proposals(con, root)
            con.commit()
            pending = con.execute("SELECT DISTINCT target FROM staging_journal WHERE state='prepared'").fetchall()
            for row in pending:
                target = _staging(row["target"])
                with root_lock(target):
                    con.execute("BEGIN IMMEDIATE")
                    _recover(con, target)
                    con.commit()
        yield root


def _current(con, target):
    raw = _read(target / "current.json", optional=True)
    row = con.execute("SELECT after_current FROM staging_journal WHERE target=? AND state='complete' "
                      "ORDER BY rowid DESC LIMIT 1", (str(target),)).fetchone()
    require(raw == (row[0] if row else None), "staging_current_unverified")
    return raw, _json(raw) if raw else {"schema": "records-staging-current-v1", "proposals": {}}


def _history(con, target, proposal_id, version):
    row = con.execute("SELECT * FROM staging_journal WHERE version=? AND target=? AND proposal_id=? "
                      "AND state='complete'", (version, str(target), proposal_id)).fetchone()
    require(row is not None, "release_history_unverified")
    folder = target / "releases" / version
    raw = _read(folder / "manifest.json", "release_manifest_missing")
    require(sha(raw) == sha(row["manifest"]), "release_manifest_changed")
    manifest = _json(raw)
    require(manifest.get("schema") == "records-staging-release-v2" and
            manifest.get("proposal_id") == proposal_id and manifest.get("version") == version,
            "release_identity_mismatch")
    content = _read(folder / (proposal_id + ".md"), "release_content_missing", optional=row["content"] is None)
    digest = sha(content) if content is not None else None
    require(content == row["content"] and digest == manifest.get("content_sha256"), "release_content_changed")
    return manifest, content


def _live(con, target, proposal_id, current):
    live = current["proposals"].get(proposal_id)
    actual = _read(target / "content" / (proposal_id + ".md"), optional=True)
    if live:
        manifest, content = _history(con, target, proposal_id, live["version"])
        require(content == actual and manifest["content_sha256"] == live["content_sha256"],
                "staging_content_changed")
    else:
        require(actual is None, "staging_content_unverified")
    return live, actual


def _approved(con, root, proposal_id, content):
    _retirement_clear(con)
    proposal = _proposal(con, proposal_id)
    require(proposal["owner_approval"] == "approved", "owner_approval_required")
    require(proposal["approved_content_sha256"] == sha(content) == proposal["public_content_sha256"],
            "approved_hash_mismatch")
    _privacy_current(con, root, proposal)
    return proposal


def _prepare(con, target, proposal_id, content, previous, action, before, before_content, current,
             *, proposal=None, restored=None, reason=None):
    version = "v-" + uuid.uuid4().hex
    manifest = {"schema": "records-staging-release-v2", "version": version, "proposal_id": proposal_id,
                "action": action, "content_sha256": sha(content) if content is not None else None,
                "previous_version": previous, "published_at": now(), "publisher": VERSION,
                "restored_from": restored}
    if proposal is not None:
        manifest["authority"] = {key: proposal[key] for key in
                                 ("approved_content_sha256", "manifest_sha256", "privacy_receipt_sha256",
                                  "independent_receipt_sha256")}
    if content is not None:
        current["proposals"][proposal_id] = {
            "version": version, "content_sha256": manifest["content_sha256"],
            "previous_version": None if action == "rollback" else previous,
            "published_at": manifest["published_at"]}
        if restored:
            current["proposals"][proposal_id]["restored_from"] = restored
        current.get("withdrawn", {}).pop(proposal_id, None)
    else:
        current["proposals"].pop(proposal_id, None)
        current.setdefault("withdrawn", {})[proposal_id] = {
            "version": version, "rolled_back_from": previous, "reason": reason}
    con.execute("INSERT INTO staging_journal VALUES(?,?,?,?,?,?,?,?, 'prepared')",
                (version, str(target), proposal_id, encoded(manifest), content, before, encoded(current), before_content))
    con.commit()  # Durable intent before the first release/output write.
    return manifest


def _apply(con, target, manifest):
    row = con.execute("SELECT * FROM staging_journal WHERE version=?", (manifest["version"],)).fetchone()
    folder = _safe_path(target / "releases" / manifest["version"])
    with _directory(target / "releases") as directory:
        os.mkdir(folder.name, mode=0o700, dir_fd=directory)
        os.fsync(directory)  # An existing release is never overwritten.
    if row["content"] is not None:
        _write(folder / (row["proposal_id"] + ".md"), row["content"])
    _write(folder / "manifest.json", row["manifest"])
    _restore(target / "content" / (row["proposal_id"] + ".md"), row["content"])
    _write(target / "current.json", row["after_current"])
    con.execute("UPDATE staging_journal SET state='complete' WHERE version=?", (row["version"],))


def publish(root, proposal_id, *, staging):
    """Serialize authority and staging, retaining a recoverable, dependency-bound intent."""
    with _authority(root) as root:
        target = _staging(staging)
        with root_lock(target), store.ledger(root.ledger) as con:
            con.execute("BEGIN IMMEDIATE")
            public = _public_bytes(root, proposal_id)
            proposal = _approved(con, root, proposal_id, public)
            before, current = _current(con, target)
            live, before_content = _live(con, target, proposal_id, current)
            digest = sha(public)
            authority = {key: proposal[key] for key in ("approved_content_sha256", "manifest_sha256",
                                                        "privacy_receipt_sha256", "independent_receipt_sha256")}
            publication_id = "pub_" + sha(encoded([str(target), proposal_id, digest, authority]))[:24]
            existing = con.execute("SELECT * FROM publications WHERE id=?", (publication_id,)).fetchone()
            if existing and live and live["content_sha256"] == digest and \
                    live["version"] == existing["deployed_version"] and existing["rollback_ref"] is None and \
                    _history(con, target, proposal_id, live["version"])[0].get("authority") == authority:
                return {"publication_id": publication_id, "version": existing["deployed_version"],
                        "reused": True, "content_sha256": digest}
            previous = live["version"] if live else None
            action = "correct" if previous else "publish"
            manifest = _prepare(con, target, proposal_id, public, previous, action, before, before_content,
                                current, proposal=proposal)
            # The journal commit released SQLite's writer lock. Check every dependency again.
            con.execute("BEGIN IMMEDIATE")
            require(_approved(con, root, proposal_id, public) == proposal, "publication_authority_changed")
            require(_public_bytes(root, proposal_id) == public, "approved_hash_mismatch")
            _apply(con, target, manifest)
            con.execute("INSERT INTO publications(id,proposal_id,site_pr_ref,deployed_version,published_at,rollback_ref) "
                        "VALUES(?,?,?,?,?,NULL) ON CONFLICT(id) DO UPDATE SET deployed_version=excluded.deployed_version,"
                        "published_at=excluded.published_at,rollback_ref=NULL",
                        (publication_id, proposal_id, "staging:" + str(target), manifest["version"], manifest["published_at"]))
            con.commit()
    return {"publication_id": publication_id, "version": manifest["version"], "reused": False,
            "content_sha256": digest, "action": action, "previous_version": previous}


def rollback(root, proposal_id, *, staging, reason=None):
    """Restore only a ledger-bound historical release, verified before touching output."""
    with _authority(root) as root:
        target = _staging(staging)
        with root_lock(target), store.ledger(root.ledger) as con:
            con.execute("BEGIN IMMEDIATE")
            _proposal(con, proposal_id)
            _retirement_clear(con)
            before, current = _current(con, target)
            live, before_content = _live(con, target, proposal_id, current)
            require(live is not None, "nothing_published_for_proposal")
            previous = live.get("previous_version")
            content = _history(con, target, proposal_id, previous)[1] if previous else None
            manifest = _prepare(con, target, proposal_id, content, live["version"], "rollback", before,
                                before_content, current, restored=previous, reason=reason)
            con.execute("BEGIN IMMEDIATE")
            _retirement_clear(con)
            _apply(con, target, manifest)
            con.execute("UPDATE publications SET rollback_ref=? WHERE proposal_id=? AND deployed_version=?",
                        (manifest["version"], proposal_id, live["version"]))
            con.commit()
    return {"proposal_id": proposal_id, "rolled_back": live["version"], "restored": previous,
            "version": manifest["version"], "withdrawn": content is None}


REOPEN_STAGES = ("extract", "catalog", "detect", "review", "compare", "privacy")


def reopen(root, proposal_id, *, stage="review", reason):
    """Owner-initiated correction: invalidate ``stage`` (and everything after it) for the
    proposal's original so the next ``records run`` regenerates the public content.

    The proposal's approval is cleared at once (its bytes are about to change); the
    staged publication, if any, stays as it is until a re-approved publish records
    the correction or a rollback withdraws it. Original bytes are never touched.
    """
    from .run import OWNER, Pipeline
    require(stage in REOPEN_STAGES, "reopen_stage_invalid")
    require(isinstance(reason, str) and reason.strip(), "reopen_reason_required")
    root_ = Root(root)
    pipeline = Pipeline(root_.path)  # prepares the root (briefly takes the lock itself)
    with _authority(root_.path):
        with store.ledger(root_.ledger) as con:
            proposal = _proposal(con, proposal_id)
        subject = _subject(root_, proposal)
        runner = pipeline._open_stage_run()["runner"]
        try:
            result = runner.invalidate(subject, stage, reason="owner_reopen:" + reason.strip()[:200])
            with store.ledger(root_.ledger) as con:
                con.execute("UPDATE proposals SET owner_approval='none',approved_content_sha256=NULL WHERE id=?", (proposal_id,))
                con.commit()
        finally:
            pipeline._close_stage_run({"reopen": proposal_id, "stage": stage})
    receipt = {"schema": "records-reopen-v1", "proposal_id": proposal_id, "original_sha256": subject, "stage": stage,
               "reason": reason, "invalidated": result["invalidated"], "run_id": pipeline.run_id, "owner": OWNER,
               "at": datetime.now(timezone.utc).isoformat()}
    path = root_.sub("proposals/private") / (proposal_id + ".reopen." + pipeline.run_id + ".json")
    _write(path, encoded(receipt))
    return receipt


def drifted_proposals(root):
    """Proposals whose public file on disk no longer matches the ledger's reviewed hash.

    A hand edit of ``proposals/public/<id>.md`` is never publishable (``publish`` checks
    the approved hash against the bytes), but it should be visible, not silent.
    """
    root_ = Root(root)
    drifted = []
    with store.ledger(root_.ledger, readonly=True) as con:
        rows = [dict(r) for r in con.execute("SELECT id,public_content_sha256 FROM proposals ORDER BY id")]
    for row in rows:
        path = root_.sub("proposals/public") / (row["id"] + ".md")
        if not path.is_file() or path.is_symlink():
            drifted.append({"id": row["id"], "problem": "public_file_missing"})
            continue
        actual = sha(path.read_bytes())
        if actual != row["public_content_sha256"]:
            drifted.append({"id": row["id"], "problem": "public_file_drifted", "ledger_sha256": row["public_content_sha256"],
                            "file_sha256": actual})
    return drifted


def reopen_main(argv=None):
    parser = argparse.ArgumentParser(prog="records reopen", description="Reopen a proposal's record for correction.")
    parser.add_argument("--root", required=True)
    parser.add_argument("--proposal", required=True)
    parser.add_argument("--stage", default="review", choices=REOPEN_STAGES, help="first stage to redo (default review)")
    parser.add_argument("--reason", required=True)
    args = parser.parse_args(argv)
    print(json.dumps(reopen(args.root, args.proposal, stage=args.stage, reason=args.reason), sort_keys=True))
    return 0


def approve_main(argv=None):
    parser = argparse.ArgumentParser(prog="records approve", description="Bind the owner's decision to exact public bytes.")
    parser.add_argument("--root", required=True)
    parser.add_argument("--proposal", required=True)
    parser.add_argument("--owner", required=True, help="owner identity recorded on the decision")
    parser.add_argument("--reject", action="store_true")
    parser.add_argument("--reason")
    args = parser.parse_args(argv)
    print(json.dumps(approve(args.root, args.proposal, owner_id=args.owner, reject=args.reject, reason=args.reason),
                     sort_keys=True))
    return 0


def publish_main(argv=None):
    parser = argparse.ArgumentParser(prog="records publish", description="Stage approved bytes; never live.")
    parser.add_argument("--root", required=True)
    parser.add_argument("--proposal", required=True)
    parser.add_argument("--staging", required=True, help="staging directory (content/, releases/, current.json)")
    parser.add_argument("--rollback", action="store_true")
    parser.add_argument("--reason")
    args = parser.parse_args(argv)
    if args.rollback:
        result = rollback(args.root, args.proposal, staging=args.staging, reason=args.reason)
    else:
        result = publish(args.root, args.proposal, staging=args.staging)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(publish_main())
