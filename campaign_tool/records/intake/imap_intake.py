"""IMAP intake: fetch new messages per folder, preserve them through the canonical
mail backend, and advance a durable (account, folder, uidvalidity, highest_uid)
checkpoint only past messages that were fully preserved.

Rules enforced here:
- credentials come from an owner-only ``mail.json`` on the host (or a password
  file it names); they are never logged, never echoed into receipts;
- messages are fetched with ``BODY.PEEK[]`` so read/unread flags are never touched
  and never used as state;
- a UIDVALIDITY change resets the folder checkpoint and re-enumerates; byte
  identity (sha256) makes the replay idempotent;
- folders the account exposes but the configuration does not list raise a keyed
  alert instead of being silently skipped;
- one failed message stops the checkpoint for that folder (it is retried next
  run) and never stops the other folders.
"""
from datetime import datetime, timezone
import hashlib
import imaplib
import json
import os
from pathlib import Path
import re
import stat

from . import eml_export
from ..runner.contracts import Folder

VERSION = "records-imap-intake-v1"
MAX_MESSAGE = eml_export.MAX_MESSAGE
MAX_PER_FOLDER_PER_RUN = 500
CHECKPOINTS_SQL = """
CREATE TABLE IF NOT EXISTS mail_checkpoints(
 account TEXT NOT NULL, folder TEXT NOT NULL, uidvalidity INTEGER NOT NULL,
 highest_uid INTEGER NOT NULL, last_success_at TEXT, updated_at TEXT NOT NULL,
 PRIMARY KEY(account, folder));
"""


class IntakeError(RuntimeError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def load_config(path):
    """Owner-only JSON config. Secrets are read here and never returned in reports."""
    path = Path(path)
    if not path.is_file() or path.is_symlink():
        raise IntakeError("mail_config_missing")
    mode = path.stat()
    if mode.st_uid != os.getuid() or stat.S_IMODE(mode.st_mode) & 0o077:
        raise IntakeError("mail_config_must_be_owner_only_0600")
    config = json.loads(path.read_bytes())
    for key in ("host", "username", "account_id"):
        if not isinstance(config.get(key), str) or not config[key].strip():
            raise IntakeError("mail_config_field_required:" + key)
    password = config.get("password")
    if not password and config.get("password_file"):
        secret = Path(config["password_file"])
        if not secret.is_absolute():
            secret = path.parent / secret
        smode = secret.stat()
        if smode.st_uid != os.getuid() or stat.S_IMODE(smode.st_mode) & 0o077:
            raise IntakeError("password_file_must_be_owner_only_0600")
        password = secret.read_text().strip()
    if not password and config.get("password_env"):
        password = os.environ.get(config["password_env"], "")
    if not password:
        raise IntakeError("mail_password_unavailable")
    folders = config.get("folders")
    if folders is not None and (not isinstance(folders, list) or not all(isinstance(f, str) and f for f in folders)):
        raise IntakeError("mail_config_folders_invalid")
    return {"host": config["host"], "port": int(config.get("port", 993)), "ssl": bool(config.get("ssl", True)),
            "username": config["username"], "password": password, "account_id": config["account_id"],
            "folders": folders, "timeout": int(config.get("timeout", 60))}


def redacted(config):
    return {key: value for key, value in config.items() if key != "password"}


def default_client_factory(config):
    if config["ssl"]:
        client = imaplib.IMAP4_SSL(config["host"], config["port"], timeout=config["timeout"])
    else:
        client = imaplib.IMAP4(config["host"], config["port"], timeout=config["timeout"])
    client.login(config["username"], config["password"])
    return client


_LIST = re.compile(rb'\((?P<flags>[^)]*)\)\s+(?P<delim>"[^"]*"|NIL)\s+(?P<name>"(?:[^"\\]|\\.)*"|\S+)')


def parse_list_line(line):
    """Mailbox name from one LIST response line; None for \\Noselect entries."""
    if isinstance(line, tuple):  # literal form: (b'(\\HasNoChildren) "/" {5}', b'INBOX')
        head, name = line
        if b"\\Noselect" in head:
            return None
        return name.decode("utf-8", "replace")
    match = _LIST.match(line)
    if not match:
        return None
    if b"\\Noselect" in match.group("flags"):
        return None
    name = match.group("name")
    if name.startswith(b'"'):
        name = name[1:-1].replace(b'\\"', b'"').replace(b"\\\\", b"\\")
    return name.decode("utf-8", "replace")


def _ok(status, what):
    if status != "OK":
        raise IntakeError("imap_" + what + "_failed")


def _quote(name):
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


class IMAPIntake:
    def __init__(self, config, *, ledger, mail_root, backend, alert, client_factory=None):
        self.config, self.ledger, self.mail_root = config, ledger, Path(mail_root)
        self.backend, self.alert = backend, alert
        self.client_factory = client_factory or default_client_factory

    # ----- checkpoints ---------------------------------------------------------------------
    def _checkpoint(self, con, folder):
        con.executescript(CHECKPOINTS_SQL)
        row = con.execute("SELECT uidvalidity,highest_uid FROM mail_checkpoints WHERE account=? AND folder=?",
                          (self.config["account_id"], folder)).fetchone()
        return (row[0], row[1]) if row else None

    def _save(self, con, folder, uidvalidity, highest_uid, success):
        con.execute("INSERT INTO mail_checkpoints(account,folder,uidvalidity,highest_uid,last_success_at,updated_at) "
                    "VALUES(?,?,?,?,?,?) ON CONFLICT(account,folder) DO UPDATE SET uidvalidity=excluded.uidvalidity,"
                    "highest_uid=excluded.highest_uid,last_success_at=COALESCE(excluded.last_success_at,mail_checkpoints.last_success_at),"
                    "updated_at=excluded.updated_at",
                    (self.config["account_id"], folder, uidvalidity, highest_uid, now() if success else None, now()))
        con.commit()

    # ----- server enumeration --------------------------------------------------------------
    def list_folders(self, client):
        status, lines = client.list()
        _ok(status, "list")
        names = [name for name in (parse_list_line(line) for line in lines or []) if name]
        return sorted(set(names))

    def select(self, client, folder):
        status, data = client.select(_quote(folder), readonly=True)
        _ok(status, "select")
        status, validity = client.response("UIDVALIDITY")
        values = [v for v in (validity or []) if v]
        if not values:
            raise IntakeError("imap_uidvalidity_missing")
        return int(values[0])

    def new_uids(self, client, after_uid):
        status, data = client.uid("SEARCH", None, f"UID {after_uid + 1}:*")
        _ok(status, "search")
        uids = sorted({int(x) for x in (data[0] or b"").split()} if data else set())
        return [uid for uid in uids if uid > after_uid][:MAX_PER_FOLDER_PER_RUN]

    def fetch(self, client, uid):
        status, data = client.uid("FETCH", str(uid), "(BODY.PEEK[])")
        _ok(status, "fetch")
        for item in data or []:
            if isinstance(item, tuple) and len(item) == 2 and isinstance(item[1], (bytes, bytearray)):
                raw = bytes(item[1])
                if len(raw) > MAX_MESSAGE:
                    raise IntakeError("message_size_limit")
                return raw
        raise IntakeError("imap_fetch_empty")

    # ----- run -----------------------------------------------------------------------------
    def run(self):
        from ..ledger import store
        report = {"schema": "records-imap-intake-report-v1", "account": self.config["account_id"],
                  "folders": [], "preserved": 0, "failures": 0, "unconfigured_folders": []}
        client = self.client_factory(self.config)
        try:
            available = self.list_folders(client)
            wanted = self.config["folders"] if self.config["folders"] is not None else available
            for name in available:
                if name not in wanted:
                    report["unconfigured_folders"].append(name)
                    self.alert("mail:unconfigured_folder:" + name, owner="owner")
            for folder in wanted:
                entry = {"folder": folder, "new": 0, "preserved": 0, "failed": None, "uidvalidity_reset": False}
                report["folders"].append(entry)
                if folder not in available:
                    entry["failed"] = "folder_missing"
                    self.alert("mail:missing_folder:" + folder, owner="owner")
                    report["failures"] += 1
                    continue
                try:
                    validity = self.select(client, folder)
                except IntakeError as error:
                    entry["failed"] = str(error)
                    report["failures"] += 1
                    continue
                with store.ledger(self.ledger) as con:
                    checkpoint = self._checkpoint(con, folder)
                    after = 0
                    if checkpoint is not None:
                        if checkpoint[0] != validity:
                            entry["uidvalidity_reset"] = True
                            self.alert("mail:uidvalidity_reset:" + folder, owner="runtime")
                        else:
                            after = checkpoint[1]
                    if checkpoint is None or checkpoint[0] != validity:
                        self._save(con, folder, validity, 0, success=False)
                uids = self.new_uids(client, after)
                entry["new"] = len(uids)
                highest = after
                for uid in uids:
                    try:
                        raw = self.fetch(client, uid)
                        receipt = eml_export.export_message(raw, mail_root=self.mail_root, account=self.config["account_id"],
                                                            mailbox=folder, uidvalidity=validity, uid=uid)
                        self.backend.preserve(receipt, self.config["account_id"], Folder(folder, validity), uid)
                        entry["preserved"] += 1
                        report["preserved"] += 1
                        highest = uid
                        with store.ledger(self.ledger) as con:
                            self._save(con, folder, validity, highest, success=True)
                    except Exception as error:  # stop advancing this folder; others continue
                        entry["failed"] = f"uid {uid}: {type(error).__name__}: {str(error)[:120]}"
                        report["failures"] += 1
                        self.alert(f"mail:preserve_failed:{folder}:{uid}", owner="runtime")
                        break
                else:
                    with store.ledger(self.ledger) as con:
                        self._save(con, folder, validity, highest, success=True)
        finally:
            try:
                client.logout()
            except Exception:
                pass
        return report


def fingerprint(config):
    """Non-secret identity of the mailbox configuration for run records."""
    return hashlib.sha256(json.dumps(redacted(config), sort_keys=True).encode()).hexdigest()
