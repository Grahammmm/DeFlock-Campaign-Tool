"""Bounded private SQLite sidecar, independent of the canonical ledger schema."""
import contextlib
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import stat
from pathlib import Path
from .policy import PortalError, hostname, safe_ancestors, url_parts

SCHEMA = """
CREATE TABLE IF NOT EXISTS portal_meta(version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS portal_items(
 id TEXT PRIMARY KEY, host TEXT NOT NULL, request_id TEXT NOT NULL, item_id TEXT NOT NULL,
 last_url_private TEXT NOT NULL, generation INTEGER NOT NULL DEFAULT 1,
 state TEXT NOT NULL DEFAULT 'pending', reason TEXT, tries INTEGER NOT NULL DEFAULT 0,
 next_attempt REAL NOT NULL DEFAULT 0, current_sha256 TEXT,
 UNIQUE(host,request_id,item_id));
CREATE TABLE IF NOT EXISTS portal_notices(
 item TEXT REFERENCES portal_items(id), source_sha256 TEXT NOT NULL,
 PRIMARY KEY(item,source_sha256));
CREATE TABLE IF NOT EXISTS portal_attempts(
 id INTEGER PRIMARY KEY, item TEXT REFERENCES portal_items(id), generation INTEGER NOT NULL,
 started REAL NOT NULL, finished REAL, result TEXT NOT NULL, evidence_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS portal_versions(
 id TEXT PRIMARY KEY, item TEXT REFERENCES portal_items(id), sha256 TEXT NOT NULL,
 byte_count INTEGER NOT NULL, previous_sha256 TEXT, attempt INTEGER REFERENCES portal_attempts(id),
 provenance_json TEXT NOT NULL, ledger_state TEXT NOT NULL DEFAULT 'pending', UNIQUE(item,sha256,previous_sha256,attempt));
"""


def digest_bytes(raw):
    return hashlib.sha256(raw).hexdigest()


def check_hash(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
        raise PortalError("invalid_hash")
    return value


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}", value):
        raise PortalError("invalid_identity")
    return value


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class Queue:
    def __init__(self, root):
        self.root = safe_ancestors(root)
        self.root.mkdir(mode=0o700, parents=False, exist_ok=True)
        self.objects = self.root / "objects"
        self.objects.mkdir(mode=0o700, exist_ok=True)
        for path in (self.root, self.objects):
            info = path.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
                raise PortalError("store_not_private")
        self.database = self.root / "portal.sqlite"
        fd = os.open(self.database, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077 or info.st_nlink != 1:
                raise PortalError("database_not_private")
        finally:
            os.close(fd)
        self.db = sqlite3.connect(self.database, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA synchronous=FULL")
        with self.lock():
            self.db.executescript(SCHEMA)
            versions = self.db.execute("SELECT version FROM portal_meta").fetchall()
            if not versions:
                self.db.execute("INSERT INTO portal_meta VALUES(1)")
                self.db.commit()
            elif len(versions) != 1 or versions[0][0] != 1:
                raise PortalError("schema_version_unsupported")

    def close(self):
        self.db.close()

    @contextlib.contextmanager
    def lock(self):
        fd = os.open(self.root / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077 or info.st_nlink != 1:
                raise PortalError("lock_not_private")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise PortalError("queue_busy") from None
            yield
        finally:
            os.close(fd)

    def inventory(self, host, request_id, item_id, url, source_sha256):
        hostname(host)
        identifier(request_id)
        identifier(item_id)
        check_hash(source_sha256)
        if url_parts(url).hostname != host:
            raise PortalError("initial_host_mismatch")
        key = digest_bytes(json.dumps([host, request_id, item_id], separators=(",", ":")).encode())
        with self.lock(), self.db:
            row = self.db.execute("SELECT * FROM portal_items WHERE id=?", (key,)).fetchone()
            if row is None:
                self.db.execute("INSERT INTO portal_items(id,host,request_id,item_id,last_url_private) VALUES(?,?,?,?,?)", (key,host,request_id,item_id,url))
            elif row["last_url_private"] != url:
                self.db.execute("UPDATE portal_items SET last_url_private=?,generation=generation+1,state='pending',reason=NULL,tries=0,next_attempt=0 WHERE id=?", (url,key))
            self.db.execute("INSERT OR IGNORE INTO portal_notices VALUES(?,?)", (key,source_sha256))
        return key

    def status(self):
        return {"items": [dict(row) for row in self.db.execute(
            "SELECT id,host,request_id,item_id,generation,state,reason,tries,current_sha256 FROM portal_items ORDER BY id")],
            "attempts": self.db.execute("SELECT count(*) FROM portal_attempts").fetchone()[0],
            "versions": self.db.execute("SELECT count(*) FROM portal_versions").fetchone()[0],
            "ledger_pending": self.db.execute("SELECT count(*) FROM portal_versions WHERE ledger_state='pending'").fetchone()[0]}

    def verify_object(self, digest, size):
        check_hash(digest)
        fd = os.open(self.objects / digest, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077 or info.st_size != size:
                raise PortalError("object_integrity_failed")
            sha = hashlib.sha256()
            while chunk := os.read(fd, 65536):
                sha.update(chunk)
            if sha.hexdigest() != digest:
                raise PortalError("object_integrity_failed")
        finally:
            os.close(fd)

    def promote(self, path, digest, size):
        check_hash(digest)
        destination = self.objects / digest
        try:
            os.link(path, destination, follow_symlinks=False)
            os.chmod(destination, 0o400)
            sync_dir(self.objects)
        except FileExistsError:
            pass
        self.verify_object(digest, size)

    def deliver(self, ledger, limit=100):
        """At-least-once outbox; ledger must upsert by receipt id atomically."""
        if ledger is None:
            return 0
        count = 0
        for row in self.db.execute("SELECT * FROM portal_versions WHERE ledger_state='pending' ORDER BY rowid LIMIT ?", (limit,)).fetchall():
            self.verify_object(row["sha256"], row["byte_count"])
            receipt = dict(row)
            receipt.pop("ledger_state")
            receipt["provenance"]=json.loads(receipt.pop("provenance_json"))
            ledger.record_original(receipt, self.objects / row["sha256"])
            with self.db:
                self.db.execute("UPDATE portal_versions SET ledger_state='delivered' WHERE id=?", (row["id"],))
            count += 1
        return count
