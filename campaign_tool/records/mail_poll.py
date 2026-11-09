"""records mail-poll: one bounded pass that hands new records-inbox mail to intake.

Implicit TLS only; read-only SELECT and BODY.PEEK: nothing is sent, flagged, moved or deleted. The secret is read
from the operator's password_file at connect time and never logged, stored or reported; refusals are stable codes.
"""
import argparse
import contextlib
import hashlib
import imaplib
import json
import os
import re
import ssl
import stat
import sys
from email import policy
from email.parser import BytesParser
from pathlib import Path

from .intake import eml_export, folder, imap_intake

SCHEMA = "records-mail-poll-v1"
REPO = Path(__file__).resolve().parents[2]
MAX_PER_RUN = 200
NOTICE_LINK = re.compile(r"https?://\S+/documents/")
UNSAFE_NAME = re.compile(r"[/\\\x00]")
FIELDS = {"host", "port", "user", "password_file", "tls", "folder", "account_id", "timeout"}


class Refused(RuntimeError):
    """A named refusal. ``args[0]`` is a stable code; it never carries a value."""


def _owner_only(path, field):
    """Refuse symlinks, in-repo paths, non-regular, foreign or group/other-readable files."""
    if not isinstance(path, str) or not os.path.isabs(path):
        raise Refused(field + "_not_absolute")
    try:
        info = os.lstat(path)
    except (OSError, ValueError):
        raise Refused(field + "_unavailable") from None
    if stat.S_ISLNK(info.st_mode):
        raise Refused(field + "_symlink")
    if REPO in Path(os.path.realpath(path)).parents:
        raise Refused(field + "_inside_repo")
    if not stat.S_ISREG(info.st_mode):
        raise Refused(field + "_not_regular_file")
    if info.st_uid != os.getuid():
        raise Refused(field + "_not_owned_by_user")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise Refused(field + "_mode_not_0600")
    return path


def load_config(path):
    """Validated, secret-free config: the password stays in its file until connect."""
    path = _owner_only(str(path), "config")
    try:
        raw = imap_intake._read_owner_json(path, "config")
    except imap_intake.IntakeError as error:
        raise Refused(error.args[0] if error.args else "config_unavailable") from None
    if set(raw) - FIELDS:
        raise Refused("config_field_unknown")
    for key in ("host", "user", "password_file"):
        if not isinstance(raw.get(key), str) or not raw[key].strip() or "\0" in raw[key]:
            raise Refused("config_field:" + key)
    if raw.get("tls", "implicit") != "implicit":
        raise Refused("tls_required")
    port, timeout = raw.get("port", 993), raw.get("timeout", 60)
    for key, value, top in (("port", port, 65535), ("timeout", timeout, 600)):
        if type(value) is not int or not 0 < value <= top:
            raise Refused("config_field:" + key)
    config = {"host": raw["host"], "port": port, "user": raw["user"], "timeout": timeout,
              "password_file": str(_owner_only(raw["password_file"], "password_file")),
              "folder": raw.get("folder", "INBOX"), "account_id": raw.get("account_id", "records-inbox")}
    for key in ("folder", "account_id"):
        if not isinstance(config[key], str) or not config[key] or "\0" in config[key] or len(config[key]) > 200:
            raise Refused("config_field:" + key)
    return config


def _secret(config):
    path = _owner_only(config["password_file"], "password_file")
    try:
        with folder.secure_open(path) as stream:
            value = stream.read(imap_intake.MAX_CREDENTIAL_BYTES + 1).decode("utf-8").strip("\r\n")
    except (OSError, ValueError, UnicodeError):
        raise Refused("password_file_unavailable") from None
    if not value or len(value) > imap_intake.MAX_CREDENTIAL_BYTES:
        raise Refused("password_file_empty_or_oversize")
    return value


def default_connect(config, password):
    """Implicit TLS with certificate verification; there is no plaintext or STARTTLS path."""
    client = imaplib.IMAP4_SSL(config["host"], config["port"], ssl_context=ssl.create_default_context(),
                               timeout=config["timeout"])
    try:
        client.login(config["user"], password)
    except BaseException:
        client.shutdown()
        raise
    return client


def connect(config, factory):
    """At most one reconnect on a transport failure, then a named refusal. Never echo errors."""
    for attempt in (1, 2):
        try:
            return factory(config, _secret(config))
        except Refused:
            raise
        except ssl.SSLError:
            raise Refused("tls_unavailable") from None
        except imaplib.IMAP4.error:
            raise Refused("auth_refused") from None
        except (OSError, EOFError):
            if attempt == 2:
                raise Refused("connection_refused") from None
        except Exception:
            raise Refused("connection_failed") from None


def _ok(status, what):
    if status != "OK":
        raise Refused("imap_" + what + "_failed")


def _list(client, mailbox):
    _ok(client.select(imap_intake._quote(mailbox), readonly=True)[0], "select")
    validity = [v for v in (client.response("UIDVALIDITY")[1] or []) if v]
    if not validity:
        raise Refused("imap_uidvalidity_missing")
    status, data = client.uid("SEARCH", None, "ALL")
    _ok(status, "search")
    return int(validity[0]), sorted({int(x) for x in ((data or [b""])[0] or b"").split()})


def _fetch(client, uid):
    status, data = client.uid("FETCH", str(uid), "(BODY.PEEK[])")
    _ok(status, "fetch")
    for item in data or []:
        if isinstance(item, tuple) and len(item) == 2 and isinstance(item[1], (bytes, bytearray)):
            if len(item[1]) > eml_export.MAX_MESSAGE:
                raise Refused("message_size_limit")
            return bytes(item[1])
    raise Refused("imap_fetch_empty")


def classify(raw, validity, uid):
    """Provider id hash, notice flag and unsafe attachment parts. Only hashes leave this function."""
    message = BytesParser(policy=policy.default).parsebytes(raw)
    mid = (message.get("Message-ID") or "").strip()
    pid = hashlib.sha256(("mid:" + mid if mid else "uid:%d:%d" % (validity, uid)).encode()).hexdigest()
    attachments, unsafe, text = 0, [], []
    for index, part in enumerate(message.walk()):
        if part.is_multipart():
            continue
        name = part.get_filename()
        if name is not None:
            attachments += 1
            if UNSAFE_NAME.search(name):
                unsafe.append({"part": index, "reason": "attachment_name_unsafe"})
        elif part.get_content_type() in ("text/plain", "text/html"):
            with contextlib.suppress(LookupError, ValueError):
                text.append(part.get_content())
    notice = attachments == 0 and any(NOTICE_LINK.search(t) for t in text if isinstance(t, str))  # not a received file
    return pid, notice, attachments, unsafe


def _load_state(path):
    if not path.exists():
        return {"schema": SCHEMA, "uidvalidity": None, "cursor": 0, "seen": {}}
    try:
        with folder.secure_open(path) as stream:
            state = json.loads(stream.read(64 * 1024 * 1024))
    except (OSError, ValueError):
        raise Refused("cursor_unreadable") from None
    if not isinstance(state, dict) or state.get("schema") != SCHEMA or not isinstance(state.get("seen"), dict):
        raise Refused("cursor_unreadable")
    return state


def _write_once(path, data):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        with folder.private_file(path, "wb", exclusive=True) as stream:
            stream.write(data)
    except FileExistsError:
        with folder.secure_open(path) as stream:
            if stream.read() != data:
                raise Refused("receipt_conflict") from None


def _poll_receipt(receipts, receipt):
    """Keep the legacy first path; shared bytes may have distinct occurrence receipts."""
    sha, pid = receipt["sha256"], receipt["provider_id_sha256"]
    if not all(isinstance(v, str) and re.fullmatch(r"[0-9a-f]{64}", v) for v in (sha, pid)):
        raise Refused("receipt_conflict")
    data = (json.dumps(receipt, sort_keys=True) + "\n").encode()
    receipts.mkdir(mode=0o700, parents=True, exist_ok=True)
    for alternate in (False, True):
        path = receipts / (sha + ("-" + pid if alternate else "") + ".json")
        try:
            with folder.private_file(path, "wb", exclusive=True) as stream:
                stream.write(data)
        except FileExistsError:
            pass
        try:
            # Reject special files before the existing no-follow reader can block.
            if not stat.S_ISREG(path.lstat().st_mode):
                raise ValueError
            with folder.secure_open(path) as stream:
                info = os.fstat(stream.fileno())
                if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
                    raise ValueError
                stored = stream.read(64 * 1024 + 1)
            if len(stored) > 64 * 1024:
                raise ValueError
            if stored == data:
                return path
            if alternate:
                raise ValueError
            prior = json.loads(stored, object_pairs_hook=imap_intake._unique_object,
                               parse_constant=imap_intake._reject_constant)
            if (not isinstance(prior, dict) or prior.get("schema") != SCHEMA or prior.get("sha256") != sha
                    or not isinstance(prior.get("provider_id_sha256"), str)
                    or not re.fullmatch(r"[0-9a-f]{64}", prior["provider_id_sha256"])
                    or prior["provider_id_sha256"] == pid
                    or any(type(prior.get(k)) is not int or prior[k] <= 0 for k in ("uidvalidity", "uid"))):
                raise ValueError
        except (OSError, ValueError, TypeError):
            raise Refused("receipt_conflict") from None


def _guard(code, function, *args):
    try:
        return function(*args)
    except Refused:
        raise
    except Exception:
        raise Refused(code) from None


def poll(config, store, *, dry_run=False, factory=default_connect, crash_after_fetch=None):
    base = Path(store) / "mail-poll"
    state_path, mail_root, receipts, quarantine = (base / name for name in ("state.json", "mail", "receipts", "quarantine"))
    state = _load_state(state_path)
    counts = dict(new=0, already=0, conflict=0, notices=0, skipped=0)
    client = connect(config, factory)
    try:
        validity, uids = _guard("imap_list_failed", _list, client, config["folder"])
        cursor = state["cursor"] if state["uidvalidity"] == validity else 0
        pending = [uid for uid in uids if uid > cursor]
        counts["already"] = len(uids) - len(pending)
        counts["skipped"] += max(0, len(pending) - MAX_PER_RUN)
        seen, highest = dict(state["seen"]), cursor
        for uid in pending[:MAX_PER_RUN]:
            raw = _guard("imap_fetch_failed", _fetch, client, uid)
            if crash_after_fetch is not None:
                crash_after_fetch(uid)
            sha = hashlib.sha256(raw).hexdigest()
            pid, notice, attachments, unsafe = classify(raw, validity, uid)
            highest = uid
            prior = seen.get(pid)
            if prior is not None:
                kind = "already" if prior["sha256"] == sha else "conflict"
                counts[kind] += 1
                if kind == "conflict" and not dry_run:
                    _write_once(quarantine / (sha + ".eml"), raw)
                continue
            counts["notices" if notice else "new"] += 1
            counts["skipped"] += len(unsafe)
            if dry_run:
                continue
            receipt = {"schema": SCHEMA, "kind": "notice" if notice else "message", "provider_id_sha256": pid,
                       "sha256": sha, "bytes": len(raw), "uidvalidity": validity, "uid": uid, "held": None,
                       "received_files": 0, "refused_attachments": unsafe, "review": "review-required"}
            if unsafe:
                # Refuse the unsafe parts: hold the message bytes whole; nothing is split out by name.
                _write_once(quarantine / (sha + ".eml"), raw)
                receipt.update(intake_receipt=None, held="quarantine/" + sha + ".eml")
            else:
                try:
                    path = eml_export.export_message(raw, mail_root=mail_root, account=config["account_id"],
                                                     mailbox=config["folder"], uidvalidity=validity, uid=uid)
                except Exception as error:
                    raise Refused("intake:" + imap_intake._failure_code(error)) from None
                receipt.update(intake_receipt=str(Path(path).relative_to(mail_root)),
                               received_files=0 if notice else attachments)
            _poll_receipt(receipts, receipt)
            seen[pid] = {"sha256": sha, "kind": receipt["kind"]}
    finally:
        with contextlib.suppress(Exception):
            client.logout()
    if not dry_run:
        # Advanced once, only after every handed-over message is confirmed by intake.
        folder.atomic(state_path, {"schema": SCHEMA, "uidvalidity": validity, "cursor": highest, "seen": seen,
                                   "updated_at": folder.now()})
    return counts


def status_line(counts):
    return ("mail-poll | new={new} already={already} conflict={conflict} notices={notices} skipped={skipped}"
            " | review-required; nothing sent").format(**counts)


def main(argv=None, *, factory=default_connect):
    parser = argparse.ArgumentParser(prog="records mail-poll", description=__doc__.splitlines()[0])
    parser.add_argument("--config", required=True, help="operator-owned 0600 JSON outside the repo")
    parser.add_argument("--store", required=True, help="private store directory")
    parser.add_argument("--dry-run", action="store_true", help="connect, list and count; write nothing")
    args = parser.parse_args(argv)
    try:
        counts = poll(load_config(args.config), args.store, dry_run=args.dry_run, factory=factory)
    except Exception as error:  # never str(error): only a reviewed code leaves this process
        code = error.args[0] if type(error) is Refused else "mail_poll_failed"
        print("mail-poll | refused: %s | cursor unchanged; nothing sent" % code, file=sys.stderr)
        return 1
    print(status_line(counts))
    return 0
