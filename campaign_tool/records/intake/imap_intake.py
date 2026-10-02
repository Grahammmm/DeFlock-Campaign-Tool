"""IMAP intake with owner-only credentials and a global attempted-fetch budget.

Messages use BODY.PEEK[]; only fully preserved messages advance checkpoints.
Failures stop their folder, not visibility of the remaining folders.
"""
from datetime import datetime, timezone
import hashlib
import imaplib
import json
import os
from pathlib import Path
import re
import stat

from . import eml_export, mail_delta
from ..runner.contracts import Folder

VERSION = "records-imap-intake-v1"
MAX_MESSAGE = eml_export.MAX_MESSAGE
DEFAULT_MAX_MESSAGES_PER_RUN = 200
MAX_CREDENTIAL_BYTES = 64 * 1024
CHECKPOINTS_SQL = """
CREATE TABLE IF NOT EXISTS mail_checkpoints(
 account TEXT NOT NULL, folder TEXT NOT NULL, uidvalidity INTEGER NOT NULL,
 highest_uid INTEGER NOT NULL, last_success_at TEXT, updated_at TEXT NOT NULL,
 PRIMARY KEY(account, folder));
"""


class IntakeError(RuntimeError):
    pass


# Only reviewed literal codes are reportable, and only from exact project types.
# Do not call str(error), trust a matching message from an arbitrary exception,
# or allow subclasses to authorize user-controlled exception formatting.
_INTAKE_FAILURE_CODES = frozenset({
    "imap_fetch_failed", "imap_fetch_empty", "message_size_limit",
})
_MAIL_DELTA_FAILURE_CODES = frozenset({
    "ambiguous_inline_body_part", "ambiguous_inline_text_part", "unsupported_rfc822_part",
    "invalid_mime_content_type",
    "invalid_related_container",
    "ambiguous_related_content_id",
    "invalid_related_content_id",
    "invalid_related_start",
    "missing_related_root",
    "related_root_type_mismatch",
    "invalid_multipart_disposition",
    "unsupported_attached_multipart_part",
    "invalid_mime_container",
    "invalid_leaf_disposition",
    "empty_message", "message_size_limit", "invalid_identity_numbers",
    "export_identity_conflict", "part_payload_mismatch", "export_scope_mismatch",
    "attachment_occurrence_missing", "receipt_changed",
    "mime_part_limit", "mime_payload_size_limit", "invalid_eml",
    "receipt_size_limit", "receipt_total_size_limit", "attachment_count_limit",
    "invalid_receipt_file", "incomplete_receipt", "invalid_receipt_bytes", "invalid_eml_bytes",
    "existing_blob_invalid", "new_blob_cleanup_failed", "mail_delta_io_or_ledger_error",
    "missing_output", "unsafe_output_path", "output_not_owner_only", "unsafe_output_type",
    "invalid_source_path", "source_path_escape", "source_is_output",
    "invalid_sha256", "invalid_account", "invalid_folder", "invalid_uidvalidity", "invalid_uid",
})


def _failure_code(error):
    if type(error) is IntakeError:
        allowed = _INTAKE_FAILURE_CODES
    elif type(error) is mail_delta.Rejected:
        allowed = _MAIL_DELTA_FAILURE_CODES
    else:
        return "fetch_or_preserve_failed"
    if len(error.args) == 1:
        code = error.args[0]
        if type(code) is str and len(code) <= 64 and code in allowed:
            return code
    return "fetch_or_preserve_failed"


def now():
    return datetime.now(timezone.utc).isoformat()


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError("nonfinite_json_value")


def _file_identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
            info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _read_owner_json(path, prefix):
    """Bounded descriptor read; never include paths, JSON or OS errors in failures."""
    if not hasattr(os, "O_NOFOLLOW"):
        raise IntakeError(prefix + "_secure_open_unavailable")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except (OSError, ValueError, TypeError):
        raise IntakeError(prefix + "_unavailable") from None
    try:
        with os.fdopen(fd, "rb") as source:
            before = os.fstat(source.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise IntakeError(prefix + "_must_be_regular_file")
            if before.st_uid != os.getuid() or stat.S_IMODE(before.st_mode) & 0o077:
                raise IntakeError(prefix + "_must_be_owner_only_0600")
            if before.st_size > MAX_CREDENTIAL_BYTES:
                raise IntakeError(prefix + "_size_limit")
            raw = source.read(MAX_CREDENTIAL_BYTES + 1)
            after = os.fstat(source.fileno())
            if _file_identity(before) != _file_identity(after):
                raise IntakeError(prefix + "_changed_during_read")
            if len(raw) > MAX_CREDENTIAL_BYTES:
                raise IntakeError(prefix + "_size_limit")
            if len(raw) != after.st_size:
                raise IntakeError(prefix + "_changed_during_read")
    except IntakeError:
        raise
    except (OSError, ValueError):
        raise IntakeError(prefix + "_unavailable") from None
    try:
        value = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise IntakeError(prefix + "_invalid_json") from None
    if not isinstance(value, dict):
        raise IntakeError(prefix + "_must_be_object")
    return value


def _message_limit(config):
    limit = config.get("max_messages_per_run", DEFAULT_MAX_MESSAGES_PER_RUN)
    if type(limit) is not int or not 1 <= limit <= DEFAULT_MAX_MESSAGES_PER_RUN:
        raise IntakeError("mail_config_max_messages_per_run_invalid")
    return limit


def _json_password(source, config_path):
    if (not isinstance(source, dict) or set(source) != {"type", "path", "key"}
            or source["type"] != "json"
            or not all(isinstance(source[k], str) and source[k].strip() and "\0" not in source[k]
                       for k in ("path", "key"))):
        raise IntakeError("mail_password_source_invalid")
    path = Path(source["path"])
    if not path.is_absolute():
        path = config_path.parent / path
    data = _read_owner_json(path, "mail_password_source")
    password = data.get(source["key"])
    if not isinstance(password, str) or not password.strip() or "\0" in password:
        raise IntakeError("mail_password_source_key_invalid")
    # Do not strip valid passwords: spaces can be meaningful credential bytes.
    return password


def load_config(path):
    """Owner-only JSON config. Secrets are read here and never returned in reports."""
    path = Path(path)
    config = _read_owner_json(path, "mail_config")
    for key in ("host", "username", "account_id"):
        if not isinstance(config.get(key), str) or not config[key].strip():
            raise IntakeError("mail_config_field_required:" + key)
    limit = _message_limit(config)
    if "password_source" in config:
        if any(key in config for key in ("password", "password_file", "password_env")):
            raise IntakeError("mail_password_source_ambiguous")
        password = _json_password(config["password_source"], path)
    else:
        # Preserve legacy password > password_file > password_env precedence.
        password = config.get("password")
        if not password and config.get("password_file"):
            try:
                secret = Path(config["password_file"])
                if not secret.is_absolute():
                    secret = path.parent / secret
                smode = secret.stat()
                if smode.st_uid != os.getuid() or stat.S_IMODE(smode.st_mode) & 0o077:
                    raise IntakeError("password_file_must_be_owner_only_0600")
                password = secret.read_text().strip()
            except (OSError, ValueError, TypeError):
                raise IntakeError("mail_password_file_unavailable") from None
        if not password and config.get("password_env"):
            try:
                password = os.environ.get(config["password_env"], "")
            except (TypeError, ValueError):
                raise IntakeError("mail_password_env_invalid") from None
        if not password:
            raise IntakeError("mail_password_unavailable")
    folders = config.get("folders")
    if folders is not None and (not isinstance(folders, list) or not all(isinstance(f, str) and f for f in folders)):
        raise IntakeError("mail_config_folders_invalid")
    try:
        port, timeout = int(config.get("port", 993)), int(config.get("timeout", 60))
    except (ValueError, TypeError, OverflowError):
        raise IntakeError("mail_config_connection_invalid") from None
    return {"host": config["host"], "port": port, "ssl": bool(config.get("ssl", True)),
            "username": config["username"], "password": password, "account_id": config["account_id"],
            "folders": folders, "timeout": timeout, "max_messages_per_run": limit}


def redacted(config):
    # Allowlist operational fields: neither credentials nor source documents/descriptors
    # (including unknown nested fields) can enter run identity.
    fields = ("host", "port", "ssl", "username", "account_id", "folders", "timeout", "max_messages_per_run")
    return {key: config[key] for key in fields if key in config}


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
    if isinstance(line, tuple):
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
        return [uid for uid in uids if uid > after_uid]

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

    def run(self):
        from ..ledger import store
        limit = _message_limit(self.config)
        report = {"schema": "records-imap-intake-report-v1", "account": self.config["account_id"],
                  "folders": [], "preserved": 0, "failures": 0, "unconfigured_folders": [],
                  "max_messages_per_run": limit, "attempted": 0, "deferred": 0, "limit_reached": False}
        try:
            client = self.client_factory(self.config)
        except Exception:
            raise IntakeError("imap_connection_or_login_failed") from None
        try:
            available = self.list_folders(client)
            wanted = self.config["folders"] if self.config["folders"] is not None else available
            for name in available:
                if name not in wanted:
                    report["unconfigured_folders"].append(name)
                    self.alert("mail:unconfigured_folder:" + name, owner="owner")
            for folder in wanted:
                entry = {"folder": folder, "new": 0, "preserved": 0, "failed": None, "uidvalidity_reset": False,
                         "attempted": 0, "deferred": 0, "limit_reached": False}
                report["folders"].append(entry)
                if folder not in available:
                    entry["failed"] = "folder_missing"
                    self.alert("mail:missing_folder:" + folder, owner="owner")
                    report["failures"] += 1
                    continue
                try:
                    validity = self.select(client, folder)
                except Exception:
                    entry["failed"] = "imap_select_failed"
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
                try:
                    uids = self.new_uids(client, after)
                except Exception:
                    entry["failed"] = "imap_search_failed"
                    report["failures"] += 1
                    continue
                entry["new"] = len(uids)
                highest = after
                for uid in uids:
                    if report["attempted"] >= limit:
                        break
                    # Charge before FETCH, including failed fetches and replayed bytes.
                    entry["attempted"] += 1
                    report["attempted"] += 1
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
                    except Exception as error:
                        code = _failure_code(error)
                        entry["failed"] = f"uid {uid}: {code}"
                        report["failures"] += 1
                        self.alert(f"mail:preserve_failed:{folder}:{uid}", owner="runtime")
                        break
                else:
                    with store.ledger(self.ledger) as con:
                        self._save(con, folder, validity, highest, success=True)
                entry["deferred"] = len(uids) - entry["attempted"]
                report["deferred"] += entry["deferred"]
                entry["limit_reached"] = report["attempted"] >= limit and entry["deferred"] > 0
            report["limit_reached"] = report["attempted"] >= limit
        except Exception:
            raise IntakeError("imap_intake_failed") from None
        finally:
            try:
                client.logout()
            except Exception:
                pass
        return report


def fingerprint(config):
    """Non-secret identity of the mailbox configuration for run records."""
    return hashlib.sha256(json.dumps(redacted(config), sort_keys=True).encode()).hexdigest()
