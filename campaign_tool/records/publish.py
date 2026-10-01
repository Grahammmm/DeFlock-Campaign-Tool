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
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys

from .ledger import store
from .run import Root

VERSION = "records-publish-v1"
DECISIONS_SQL = """
CREATE TABLE IF NOT EXISTS owner_decisions(
 id TEXT PRIMARY KEY, proposal_id TEXT NOT NULL REFERENCES proposals(id),
 decision TEXT NOT NULL CHECK(decision IN ('approved','rejected','revoked')),
 owner_id TEXT NOT NULL, content_sha256 TEXT NOT NULL, reason TEXT, decided_at TEXT NOT NULL);
"""


class PublishError(ValueError):
    pass


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
    path = root.sub("proposals/public") / (proposal_id + ".md")
    require(path.is_file() and not path.is_symlink(), "public_file_missing")
    return path.read_bytes()


def _subject(root, proposal):
    """The private manifest binds the proposal to its original; its hash is in the ledger row."""
    path = root.sub("proposals/private") / (proposal["id"] + ".manifest.json")
    require(path.is_file() and not path.is_symlink(), "manifest_missing")
    raw = path.read_bytes()
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


def approve(root, proposal_id, *, owner_id, reject=False, reason=None):
    """Bind the owner's decision to the exact public bytes on disk and in the ledger."""
    root = Root(root)
    owner = _identity(owner_id)
    public = _public_bytes(root, proposal_id)
    digest = sha(public)
    with store.ledger(root.ledger) as con:
        con.executescript(DECISIONS_SQL)
        con.execute("BEGIN IMMEDIATE")
        try:
            proposal = _proposal(con, proposal_id)
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
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(json.dumps(receipt, sort_keys=True, indent=1).encode())
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return {"proposal_id": proposal_id, "decision": decision, "content_sha256": digest, "reused": False,
            "receipt": str(path)}


def _staging(target):
    target = Path(os.path.abspath(target))
    target.mkdir(parents=True, exist_ok=True)
    for name in ("content", "releases"):
        (target / name).mkdir(exist_ok=True)
    return target


def _current(target):
    path = target / "current.json"
    if not path.exists():
        return {"schema": "records-staging-current-v1", "proposals": {}}
    return json.loads(path.read_bytes())


def _write(path, data):
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _release(target, proposal_id, content, previous, action):
    version = "v-" + sha((proposal_id + "|" + (sha(content) if content is not None else "withdrawn") + "|" +
                          str(previous)).encode())[:16]
    folder = target / "releases" / version
    folder.mkdir(exist_ok=True)
    manifest = {"schema": "records-staging-release-v1", "version": version, "proposal_id": proposal_id,
                "action": action, "content_sha256": sha(content) if content is not None else None,
                "previous_version": previous, "published_at": now(), "publisher": VERSION}
    if content is not None:
        _write(folder / (proposal_id + ".md"), content)
    _write(folder / "manifest.json", json.dumps(manifest, sort_keys=True, indent=1).encode())
    return manifest


def publish(root, proposal_id, *, staging):
    """Copy exactly the approved bytes to the staging target; idempotent per (proposal, content)."""
    root = Root(root)
    target = _staging(staging)
    public = _public_bytes(root, proposal_id)
    digest = sha(public)
    with store.ledger(root.ledger) as con:
        proposal = _proposal(con, proposal_id)
        require(proposal["owner_approval"] == "approved", "owner_approval_required")
        require(proposal["approved_content_sha256"] == digest == proposal["public_content_sha256"],
                "approved_hash_mismatch")
        _privacy_current(con, root, proposal)
        publication_id = "pub_" + sha((proposal_id + "|" + digest).encode())[:16]
        current = _current(target)
        live = current["proposals"].get(proposal_id)
        existing = con.execute("SELECT * FROM publications WHERE id=?", (publication_id,)).fetchone()
        if existing is not None and live and live.get("content_sha256") == digest and \
                live.get("version") == existing["deployed_version"] and existing["rollback_ref"] is None:
            return {"publication_id": publication_id, "version": existing["deployed_version"], "reused": True,
                    "content_sha256": digest}
        previous = live.get("version") if live else None
        action = "correct" if previous else "publish"
        manifest = _release(target, proposal_id, public, previous, action)
        _write(target / "content" / (proposal_id + ".md"), public)
        current["proposals"][proposal_id] = {"version": manifest["version"], "content_sha256": digest,
                                            "previous_version": previous, "published_at": manifest["published_at"]}
        _write(target / "current.json", json.dumps(current, sort_keys=True, indent=1).encode())
        con.execute("BEGIN IMMEDIATE")
        try:
            if existing is None:
                con.execute("INSERT INTO publications(id,proposal_id,site_pr_ref,deployed_version,published_at,rollback_ref) "
                            "VALUES(?,?,?,?,?,?)", (publication_id, proposal_id, "staging:" + str(target),
                                                    manifest["version"], manifest["published_at"], None))
            else:
                con.execute("UPDATE publications SET deployed_version=?,published_at=?,rollback_ref=NULL WHERE id=?",
                            (manifest["version"], manifest["published_at"], publication_id))
            con.commit()
        except BaseException:
            con.rollback()
            raise
    return {"publication_id": publication_id, "version": manifest["version"], "reused": False,
            "content_sha256": digest, "action": action, "previous_version": previous}


def rollback(root, proposal_id, *, staging, reason=None):
    """Restore the previous staged release for this proposal (or withdraw it if none)."""
    root = Root(root)
    target = _staging(staging)
    current = _current(target)
    live = current["proposals"].get(proposal_id)
    require(live is not None, "nothing_published_for_proposal")
    previous = live.get("previous_version")
    content = None
    if previous:
        path = target / "releases" / previous / (proposal_id + ".md")
        if path.exists():
            content = path.read_bytes()
    manifest = _release(target, proposal_id, content, live["version"], "rollback")
    if content is not None:
        _write(target / "content" / (proposal_id + ".md"), content)
        current["proposals"][proposal_id] = {"version": manifest["version"], "content_sha256": sha(content),
                                            "previous_version": None, "published_at": manifest["published_at"],
                                            "restored_from": previous}
    else:
        (target / "content" / (proposal_id + ".md")).unlink(missing_ok=True)
        current["proposals"].pop(proposal_id, None)
        current.setdefault("withdrawn", {})[proposal_id] = {"version": manifest["version"],
                                                            "rolled_back_from": live["version"], "reason": reason}
    _write(target / "current.json", json.dumps(current, sort_keys=True, indent=1).encode())
    with store.ledger(root.ledger) as con:
        con.execute("BEGIN IMMEDIATE")
        try:
            con.execute("UPDATE publications SET rollback_ref=? WHERE proposal_id=? AND deployed_version=?",
                        (manifest["version"], proposal_id, live["version"]))
            con.commit()
        except BaseException:
            con.rollback()
            raise
    return {"proposal_id": proposal_id, "rolled_back": live["version"], "restored": previous,
            "version": manifest["version"], "withdrawn": content is None}


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
