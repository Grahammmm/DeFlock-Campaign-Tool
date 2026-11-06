"""Place approved records reply drafts in the campaign's outbound Drafts mailbox. This module never sends.

Only queue rows whose approval was re-verified (``status == "approved"``) are placed. Each becomes one message
appended with the ``\\Draft`` flag to the configured Drafts folder, threaded (In-Reply-To/References) to the agency
message it answers when the ledger links the received item to a stored message. The only IMAP calls are SELECT,
UID SEARCH by our own deterministic Message-ID, APPEND and LOGOUT: nothing is stored, flagged, moved, copied or
deleted, and there is no SMTP or HTTP call. Credentials stay in the mailbox config's password_file, read only by
``mail_poll`` at connect time. Journal: ``<project>/approvals/mail-drafts.jsonl`` (append-only, 0600).
Refusals are stable, content-free codes.
"""
import argparse
import fcntl
import hashlib
import imaplib
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import format_datetime, getaddresses

from . import approval_queue, draft_templates, mail_poll
from .intake import folder as intake_folder
from .intake import imap_intake
from .ledger import store

SCHEMA = "records-mail-draft-v1"
JOURNAL, LOCK = "mail-drafts.jsonl", "mail-drafts.lock"
MAX_JOURNAL = 16 * 1024 * 1024
MAX_EML_HEADERS = 1024 * 1024
MAX_REFERENCES = 20
LOCK_SECONDS = 5.0
STATES = ("placed", "found_existing", "failed", "superseded")
DONE = ("placed", "found_existing", "superseded")
_CTRL = re.compile(r"[\r\n\x00]")
_MSGID = re.compile(r"<[^<>\s\x00-\x1f]{1,300}>")
_ADDR = re.compile(r"[^@\s<>\x00-\x1f\"]+@[^@\s<>\x00-\x1f\"]+")


class MailDraftError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _need(condition, code):
    if not condition:
        raise MailDraftError(code)


def _safe(value):
    _need(isinstance(value, str) and not _CTRL.search(value), "header_unsafe")
    return value


def _now():
    return datetime.now(timezone.utc)


def _stamp(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---- config --------------------------------------------------------------------------------

def load_config(path):
    """mail_poll's owner-only config plus an explicit Drafts folder; the From address is the config user."""
    try:
        config = mail_poll.load_config(path)
        raw = imap_intake._read_owner_json(str(path), "config")
    except mail_poll.Refused as error:
        raise MailDraftError(error.args[0] if error.args else "config_unavailable") from None
    except imap_intake.IntakeError as error:
        raise MailDraftError(error.args[0] if error.args else "config_unavailable") from None
    _need("folder" in raw and config["folder"].strip().upper() != "INBOX", "folder_required")
    user = config["user"].strip()
    _need(user.count("@") == 1 and _ADDR.fullmatch(user) is not None, "from_address_unknown")
    return {**config, "from_addr": user}


# ---- pure message build --------------------------------------------------------------------

def message_id(queue_id, revision, from_addr):
    digest = hashlib.sha256(("%s|%s" % (queue_id, revision)).encode("utf-8")).hexdigest()[:32]
    return "<rec-%s@%s>" % (digest, from_addr.rsplit("@", 1)[1])


def fingerprint(message_bytes):
    """sha256 of the exact message bytes, normalized only for CRLF."""
    return hashlib.sha256(message_bytes.replace(b"\r\n", b"\n")).hexdigest()


def _subject(thread, row):
    if thread is None:
        return "Records request %s follow-up" % row["request_id"]
    subject = " ".join(_safe(thread.get("subject") or "").split())
    if not subject:
        return "Re: Records request %s" % row["request_id"]
    return subject if re.match(r"re\s*:", subject, re.I) else "Re: " + subject


def build_message(row, thread, from_addr, *, now=None):
    """RFC 822 bytes for one approved queue row. ``thread`` is ``find_thread``'s dict or None."""
    _safe(from_addr)
    _need(_ADDR.fullmatch(from_addr) is not None, "from_address_unknown")
    msg = EmailMessage(policy=policy.SMTP)
    msg["From"] = from_addr
    to = ""
    if thread is not None:
        to = _safe(thread.get("reply_to") or "")
        _need(not to or _ADDR.fullmatch(to) is not None, "header_unsafe")
    if to:
        msg["To"] = to
    msg["Subject"] = _safe(_subject(thread, row))
    msg["Date"] = format_datetime(now or _now())
    msg["Message-ID"] = message_id(row["id"], row["revision"], from_addr)
    if thread is not None:
        parent = _safe(thread["message_id"])
        _need(_MSGID.fullmatch(parent) is not None, "header_unsafe")
        refs = [_safe(r) for r in thread.get("references", [])]
        _need(all(_MSGID.fullmatch(r) for r in refs), "header_unsafe")
        chain = list(dict.fromkeys(refs + [parent]))[-MAX_REFERENCES:]
        msg["In-Reply-To"] = parent
        msg["References"] = " ".join(chain)
    msg["X-Records-Queue-Id"] = _safe(row["id"])
    msg["X-Records-Revision"] = str(int(row["revision"]))
    msg.set_content(row["body"])
    return msg.as_bytes()


# ---- read-only thread lookup ---------------------------------------------------------------

def _eml_for(con, subject):
    """The one stored message this original is (or was attached to), through the ledger's occurrence chain."""
    emls, seen = set(), set()
    pending = [r["id"] for r in con.execute("SELECT id FROM occurrences WHERE original_sha256=?", (subject,))]
    while pending and len(seen) < 64:
        oid = pending.pop()
        if oid in seen:
            continue
        seen.add(oid)
        occ = con.execute("SELECT kind,original_sha256,parent_occurrence_id FROM occurrences WHERE id=?",
                          (oid,)).fetchone()
        if occ is None:
            continue
        if occ["kind"] == "mail":
            emls.add(occ["original_sha256"])
        elif occ["parent_occurrence_id"]:
            pending.append(occ["parent_occurrence_id"])
    if len(emls) != 1:
        return None
    eml = next(iter(emls))
    if con.execute("SELECT 1 FROM mail_messages WHERE eml_sha256=? LIMIT 1", (eml,)).fetchone() is None:
        return None
    return eml


def _address(value):
    for _, addr in getaddresses([str(value or "")]):
        if _ADDR.fullmatch(addr or "") and not _CTRL.search(addr):
            return addr
    return ""


def find_thread(project, item_id):
    """(thread, None) or (None, reason). Headers only, bounded; a thread is never guessed from subject text."""
    from .outcomes import received_items
    ledger = os.path.join(os.path.abspath(project), "processing-v1", "ledger.sqlite")
    try:
        if not os.path.exists(ledger):
            return None, "thread_not_found"
        with store.ledger(ledger, readonly=True) as con:
            subject = next((r[0] for r in con.execute("SELECT sha256 FROM originals ORDER BY sha256")
                            if received_items.item_id(r[0]) == item_id), None)
            eml = _eml_for(con, subject) if subject else None
            if eml is None:
                return None, "thread_not_found"
            path = store.effective_original(con, eml)["storage_path"]
        if not path:
            return None, "eml_missing"
        with intake_folder.secure_open(path) as stream:
            raw = stream.read(MAX_EML_HEADERS)
    except (sqlite3.Error, ValueError, KeyError, TypeError):
        return None, "thread_not_found"
    except OSError:
        return None, "eml_missing"
    headers = BytesParser(policy=policy.default).parsebytes(raw, headersonly=True)
    mids = _MSGID.findall(str(headers.get("Message-ID") or ""))
    if len(mids) != 1:
        return None, "message_id_missing"
    return {"message_id": mids[0], "references": _MSGID.findall(str(headers.get("References") or "")),
            "subject": " ".join(str(headers.get("Subject") or "").split()),
            "reply_to": _address(headers.get("Reply-To")) or _address(headers.get("From"))}, None


# ---- private journal -----------------------------------------------------------------------

def _mapped(error):
    code = {"queue_busy": "journal_busy", "queue_io": "journal_io"}.get(error.code, "journal_invalid")
    return MailDraftError(code)


class _Journal:
    """Root dir + approvals dir + lock held; rows() folds, append() adds rows."""

    def __init__(self, root, write):
        self.root, self.write, self.fds = os.path.abspath(root), write, []
        self.dir_fd = None

    def __enter__(self):
        try:
            root_fd = approval_queue._open_chain(self.root)
            self.fds.append(root_fd)
            self.dir_fd = approval_queue._open_approvals(root_fd, self.write)
            if self.dir_fd is not None:
                self.fds.append(self.dir_fd)
                lock = self._lock()
                if lock is not None:
                    self.fds.append(lock)
            return self
        except approval_queue.ApprovalQueueError as error:
            self.__exit__()
            raise _mapped(error) from None
        except BaseException:
            self.__exit__()
            raise

    def __exit__(self, *exc):
        for fd in reversed(self.fds):
            try:
                os.close(fd)
            except OSError:
                pass
        self.fds = []
        return False

    def _lock(self):
        flags = os.O_RDWR
        fd = approval_queue._open_file(self.dir_fd, LOCK, flags)
        if fd is None:
            if not self.write:
                return None
            try:
                fd = approval_queue._open_file(self.dir_fd, LOCK, flags, create=True)
            except approval_queue.ApprovalQueueError:
                fd = approval_queue._open_file(self.dir_fd, LOCK, flags)  # lost a creation race
            _need(fd is not None, "journal_io")
        deadline = time.monotonic() + LOCK_SECONDS
        while True:
            try:
                fcntl.flock(fd, (fcntl.LOCK_EX if self.write else fcntl.LOCK_SH) | fcntl.LOCK_NB)
                return fd
            except BlockingIOError:
                if time.monotonic() > deadline:
                    os.close(fd)
                    raise MailDraftError("journal_busy") from None
                time.sleep(0.02)
            except OSError:
                os.close(fd)
                raise MailDraftError("journal_io") from None

    def rows(self):
        """Every journal row in order; a truncated tail or any malformed row refuses."""
        if self.dir_fd is None:
            return []
        try:
            fd = approval_queue._open_file(self.dir_fd, JOURNAL, os.O_RDONLY)
        except approval_queue.ApprovalQueueError as error:
            raise _mapped(error) from None
        if fd is None:
            return []
        try:
            _need(os.fstat(fd).st_size <= MAX_JOURNAL, "journal_invalid")
            chunks = []
            while True:
                chunk = os.read(fd, 1 << 20)
                if not chunk:
                    break
                chunks.append(chunk)
        except OSError:
            raise MailDraftError("journal_io") from None
        finally:
            os.close(fd)
        raw = b"".join(chunks)
        if not raw:
            return []
        _need(raw.endswith(b"\n"), "journal_invalid")
        out = []
        for line in raw.split(b"\n")[:-1]:
            try:
                row = approval_queue._strict_load(line, "journal_invalid")
            except approval_queue.ApprovalQueueError:
                raise MailDraftError("journal_invalid") from None
            _need(isinstance(row, dict) and row.get("schema") == SCHEMA and isinstance(row.get("queue_id"), str)
                  and type(row.get("revision")) is int and row.get("state") in STATES
                  and isinstance(row.get("at"), str), "journal_invalid")
            out.append(row)
        return out

    def append(self, rows):
        data = b"".join((approval_queue._canon(r) + "\n").encode("utf-8") for r in rows)
        try:
            fd = approval_queue._open_file(self.dir_fd, JOURNAL, os.O_WRONLY | os.O_APPEND)
            if fd is None:
                fd = approval_queue._open_file(self.dir_fd, JOURNAL, os.O_WRONLY | os.O_APPEND, create=True)
        except approval_queue.ApprovalQueueError as error:
            raise _mapped(error) from None
        try:
            _need(os.fstat(fd).st_size + len(data) <= MAX_JOURNAL, "journal_invalid")
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        except OSError:
            raise MailDraftError("journal_io") from None
        finally:
            os.close(fd)


def _latest(rows):
    out = {}
    for row in rows:
        out[(row["queue_id"], row["revision"])] = row
    return out


def placements(project):
    """Read-only fold: the latest journal row per (queue_id, revision). Creates nothing."""
    with _Journal(project, False) as journal:
        latest = _latest(journal.rows())
    return [latest[k] for k in sorted(latest)]


# ---- placement -----------------------------------------------------------------------------

def _row(row, state, at, **extra):
    base = {"schema": SCHEMA, "queue_id": row["id"] if "id" in row else row["queue_id"], "revision": row["revision"],
            "state": state, "at": _stamp(at)}
    base.update(extra)
    return base


def _imap_ok(result, code):
    _need(isinstance(result, tuple) and result and result[0] == "OK", code)
    return result[1] if len(result) > 1 else None


def _stable_date(row, now):
    """The Date header comes from the row's own last-event time, so a retry after a lost journal write
    rebuilds byte-identical message bytes (the fingerprint then matches the message already in the mailbox)."""
    try:
        return datetime.strptime(row["updated_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (KeyError, TypeError, ValueError):
        return now


def _place_one(client, config, row, thread, reason, now):
    mid = message_id(row["id"], row["revision"], config["from_addr"])
    raw = build_message(row, thread, config["from_addr"], now=_stable_date(row, now))
    found = _imap_ok(client.uid("SEARCH", None, "HEADER", "Message-ID", imap_intake._quote(mid)), "imap_search_failed")
    exists = bool(found and found[0] and found[0].split())
    if not exists:
        _imap_ok(client.append(imap_intake._quote(config["folder"]), "(\\Draft)", now, raw), "imap_append_failed")
    return _row(row, "found_existing" if exists else "placed", now, message_id=mid, folder=config["folder"],
                fingerprint=fingerprint(raw), threaded=thread is not None,
                thread_reason=None if thread is not None else reason)


def place(project, config, *, factory=mail_poll.default_connect, now=None, only_id=None, dry_run=False):
    """Place every approved, not yet placed row. Content-free summary; never sends."""
    now = now or _now()
    rows = [r for r in approval_queue.load_queue(project) if r["status"] == "approved" and r["kind"] not in draft_templates.NEXT_STEP_KINDS
            and (only_id is None or r["id"] == only_id)]
    summary = {"placed": 0, "existing": 0, "failed": 0, "skipped": 0}
    with _Journal(project, not dry_run) as journal:
        latest = _latest(journal.rows())
        todo = [r for r in rows if latest.get((r["id"], r["revision"]), {}).get("state") not in DONE]
        summary["skipped"] = len(rows) - len(todo)
        if dry_run:
            summary["would_place"] = len(todo)
            return summary
        if not todo:
            return summary
        client = None
        try:
            try:
                client = mail_poll.connect(config, factory)
                _imap_ok(client.select(imap_intake._quote(config["folder"])), "imap_select_failed")
            except mail_poll.Refused as error:
                return _fail_all(journal, todo, summary, error.args[0], now)
            except MailDraftError as error:
                return _fail_all(journal, todo, summary, error.code, now)
            except (imaplib.IMAP4.error, OSError, EOFError):
                return _fail_all(journal, todo, summary, "imap_failed", now)
            for row in todo:
                thread, reason = find_thread(project, row["item_id"])
                try:
                    result = _place_one(client, config, row, thread, reason, now)
                except MailDraftError as error:
                    journal.append([_row(row, "failed", now, code=error.code)])
                    summary["failed"] += 1
                    continue
                except (imaplib.IMAP4.error, OSError, EOFError):
                    journal.append([_row(row, "failed", now, code="imap_failed")])
                    summary["failed"] += 1
                    continue
                extra = [_row(old, "superseded", now, message_id=old.get("message_id"), folder=old.get("folder"),
                              fingerprint=old.get("fingerprint"), threaded=old.get("threaded"),
                              superseded_by=row["revision"])
                         for (qid, rev), old in sorted(_latest(journal.rows()).items())
                         if qid == row["id"] and rev < row["revision"] and old["state"] in ("placed", "found_existing")]
                journal.append([result] + extra)
                summary["existing" if result["state"] == "found_existing" else "placed"] += 1
        finally:
            if client is not None:
                try:
                    client.logout()
                except (imaplib.IMAP4.error, OSError, EOFError):
                    pass
    return summary


def _fail_all(journal, todo, summary, code, now):
    journal.append([_row(r, "failed", now, code=str(code)[:80]) for r in todo])
    summary["failed"] += len(todo)
    return summary


def record_failure(project, queue_id, code, *, now=None):
    """Journal a placement failure (for example a refused config) for one approved row; sends nothing."""
    now = now or _now()
    rows = [r for r in approval_queue.load_queue(project) if r["id"] == queue_id and r["status"] == "approved"
            and r["kind"] not in draft_templates.NEXT_STEP_KINDS]
    with _Journal(project, True) as journal:
        journal.append([_row(r, "failed", now, code=str(code)[:80]) for r in rows])


# ---- command line --------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(prog="records mail-drafts",
                                     description="Place approved reply drafts in the Drafts mailbox. Sends nothing.")
    sub = parser.add_subparsers(dest="action", required=True)
    place_p = sub.add_parser("place")
    place_p.add_argument("--project", required=True)
    place_p.add_argument("--config", required=True, help="owner-only mailbox config with an explicit Drafts folder")
    place_p.add_argument("--dry-run", action="store_true", help="connect nowhere and write nothing")
    list_p = sub.add_parser("list")
    list_p.add_argument("--project", required=True)
    args = parser.parse_args(argv)
    try:
        if args.action == "list":
            for r in placements(args.project):
                print(json.dumps({k: r.get(k) for k in ("queue_id", "revision", "state", "folder", "threaded",
                                                        "thread_reason", "message_id", "fingerprint", "code",
                                                        "superseded_by", "at")}, sort_keys=True))
            return 0
        config = load_config(args.config)
        print(json.dumps(place(args.project, config, dry_run=args.dry_run), sort_keys=True))
    except (MailDraftError, approval_queue.ApprovalQueueError) as error:
        print(error.code, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
