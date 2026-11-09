"""Console "Approve and send": one click sends one already-placed draft through the transactional outbox.

Ships switched off: ``records mail-send init`` writes ``approvals/send.json`` with ``enabled: false`` and creates the
kill file ``approvals/SEND-STOP``; nothing in code ever removes a kill file. A send needs, in order: the console
session's POST (csrf, loopback), the switches, the fingerprint the page showed, the draft re-read from the Drafts
mailbox (read-only SELECT/SEARCH/FETCH BODY.PEEK, byte-identical to what was approved), a typed confirmation for any
address not on the thread or in the agency list, the daily cap, and an *outbox* SSH approval signature over the exact
outgoing text. Every attempt, success or refusal, is appended to ``approvals/mail-send.jsonl`` (who, when, text
fingerprint, outbox key, message id). Refusal codes are stable and content-free; no credential is ever returned.
"""
import argparse
import fcntl
import imaplib
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses, parseaddr
from zoneinfo import ZoneInfo

from . import approval_queue, campaign_config, draft_residue, mail_drafts, mail_poll
from .intake import imap_intake
from .outcomes.reading import esc
from .. import approval, outbox

SCHEMA = "records-mail-send-v1"
CONFIG, RESULTS, LOCK, KILL = "send.json", "mail-send.jsonl", "send.lock", "SEND-STOP"
MODES_KILL = ("modes", "RECORDS-SEND-STOP")
MAX_RESULTS = 16 * 1024 * 1024
LOCK_SECONDS = 90.0
DEFAULT_CAP = 5
TZ = "America/Los_Angeles"
FIELDS = {"enabled", "daily_cap", "smtp_host", "smtp_port", "smtp_user", "smtp_password_file", "imap_config",
          "outbox_path", "agency_addresses", "kill_file", "live", "test_recipient"}
REASONS = {
    "test_mode_recipient": "Test mode: only the test recipient can receive mail; this draft is addressed to someone else. It stays Approved, not sent.",
    "test_recipient_missing": "Test mode is on and no test recipient is configured.",
    "send_disabled": "Sending is switched off in the send config.",
    "kill_switch": "The kill switch file is present.",
    "daily_cap_reached": "The daily send cap for this campaign is reached.",
    "campaign_timezone_invalid": "The campaign's timezone setting is invalid, so the daily send budget cannot be counted.",
    "not_configured": "The console was started without --send-config.",
}


class MailSendError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _need(condition, code):
    if not condition:
        raise MailSendError(code)


def _stamp(moment):
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---- config and switches -------------------------------------------------------------------

def config_path(project):
    return os.path.join(os.path.abspath(project), "approvals", CONFIG)


def load_config(path):
    """Validated config. Unknown fields refused; secrets stay in their files."""
    try:
        mail_poll._owner_only(str(path), "send_config")
        raw = imap_intake._read_owner_json(str(path), "send_config")
    except mail_poll.Refused as error:
        raise MailSendError(error.args[0] if error.args else "config_unavailable") from None
    except imap_intake.IntakeError as error:
        raise MailSendError(error.args[0] if error.args else "config_unavailable") from None
    _need(isinstance(raw, dict) and not set(raw) - FIELDS, "config_field_unknown")
    _need(type(raw.get("enabled")) is bool, "config_field:enabled")
    live = raw.get("live", False)
    _need(type(live) is bool, "config_field:live")
    test_recipient = raw.get("test_recipient", "")
    _need("test_recipient" not in raw or (isinstance(test_recipient, str)
          and mail_drafts._ADDR.fullmatch(test_recipient)), "config_field:test_recipient")
    cap = raw.get("daily_cap", DEFAULT_CAP)
    _need(type(cap) is int and 1 <= cap <= 20, "config_field:daily_cap")
    port = raw.get("smtp_port", 587)
    _need(type(port) is int and 0 < port <= 65535, "config_field:smtp_port")
    for key in ("smtp_host", "smtp_user", "smtp_password_file", "imap_config", "outbox_path"):
        _need(isinstance(raw.get(key), str) and raw[key].strip() and "\0" not in raw[key], "config_field:" + key)
    for key in ("smtp_password_file", "imap_config", "outbox_path"):
        _need(os.path.isabs(raw[key]), "config_field:" + key)
    agency = raw.get("agency_addresses", [])
    _need(isinstance(agency, list) and all(isinstance(a, str) and mail_drafts._ADDR.fullmatch(a) for a in agency),
          "config_field:agency_addresses")
    kill = raw.get("kill_file")
    _need(kill is None or (isinstance(kill, str) and os.path.isabs(kill) and "\0" not in kill), "config_field:kill_file")
    return {**raw, "live": live, "test_recipient": test_recipient.lower(), "daily_cap": cap, "smtp_port": port, "agency_addresses": [a.lower() for a in agency]}


def kill_files(project, config=None):
    root = os.path.abspath(project)
    files = [os.path.join(root, "approvals", KILL), os.path.join(root, *MODES_KILL)]
    if config and config.get("kill_file"):
        files.append(config["kill_file"])
    return files


def killed(project, config=None):
    return any(os.path.lexists(f) for f in kill_files(project, config))


def _local_day(moment, *, timezone_name=TZ):
    return moment.astimezone(ZoneInfo(timezone_name)).date()


def campaign_timezone(project):
    """The campaign's local-day zone (``campaign.json`` ``timezone``, Pacific when absent); invalid config fails closed."""
    try:
        return campaign_config.load_timezone(project)
    except ValueError:
        raise MailSendError("campaign_timezone_invalid") from None


def sent_today(outbox_path, now, *, timezone_name=TZ):
    """Console sends (sent or still sending) whose outbox timestamp falls on today's date in ``timezone_name``."""
    if not os.path.exists(outbox_path):
        return 0
    try:
        con = sqlite3.connect("file:%s?mode=ro" % outbox_path, uri=True)
    except sqlite3.Error:
        raise MailSendError("outbox_unavailable") from None
    try:
        rows = con.execute("SELECT draft_json, sent_at, sending_at FROM outbox WHERE kind='send_followup' "
                           "AND state IN ('sent','sending')").fetchall()
    except sqlite3.Error:
        raise MailSendError("outbox_unavailable") from None
    finally:
        con.close()
    today, count = _local_day(now, timezone_name=timezone_name), 0
    for draft_json, sent, sending in rows:
        try:
            if not str(json.loads(draft_json).get("intent_id", "")).startswith("recq-"):
                continue
            when = datetime.fromisoformat((sent or sending).replace("Z", "+00:00"))
        except (ValueError, TypeError, AttributeError):
            continue
        count += _local_day(when, timezone_name=timezone_name) == today
    return count


def switch_state(project, path, *, now=None):
    """What the console may do right now, without touching any server. ``reason`` is a stable code."""
    now = now or datetime.now(timezone.utc)
    if path is None:
        return {"can_send": False, "reason": "not_configured", "cap": None, "count": None}
    try:
        config = load_config(path)
        zone = campaign_timezone(project)
        count = sent_today(config["outbox_path"], now, timezone_name=zone)
    except MailSendError as error:
        return {"can_send": False, "reason": error.code, "cap": None, "count": None}
    reason = ("send_disabled" if not config["enabled"] else "kill_switch" if killed(project, config)
              else "daily_cap_reached" if count >= config["daily_cap"] else None)
    return {"can_send": reason is None, "reason": reason, "cap": config["daily_cap"], "count": count,
            "timezone": zone, "mode": "live" if config["live"] else "test", "test_recipient": config["test_recipient"]}


# ---- private files under <project>/approvals ------------------------------------------------

class _Dir:
    """Project root + approvals dir fds (no symlinks, owner-only); optional exclusive send lock."""

    def __init__(self, project, *, create=False, lock=False):
        self.project, self.create, self.lock, self.fds, self.dir_fd = os.path.abspath(project), create, lock, [], None

    def __enter__(self):
        try:
            root = approval_queue._open_chain(self.project)
            self.fds.append(root)
            self.dir_fd = approval_queue._open_approvals(root, self.create)
            _need(self.dir_fd is not None, "approvals_missing")
            self.fds.append(self.dir_fd)
            if self.lock:
                self._flock()
            return self
        except approval_queue.ApprovalQueueError:
            self.__exit__()
            raise MailSendError("send_io") from None
        except BaseException:
            self.__exit__()
            raise

    def _flock(self):
        fd = approval_queue._open_file(self.dir_fd, LOCK, os.O_RDWR)
        if fd is None:
            try:
                fd = approval_queue._open_file(self.dir_fd, LOCK, os.O_RDWR, create=True)
            except approval_queue.ApprovalQueueError:
                fd = approval_queue._open_file(self.dir_fd, LOCK, os.O_RDWR)
            _need(fd is not None, "send_io")
        self.fds.append(fd)
        deadline = time.monotonic() + LOCK_SECONDS
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                _need(time.monotonic() <= deadline, "send_busy")
                time.sleep(0.02)
            except OSError:
                raise MailSendError("send_io") from None

    def __exit__(self, *exc):
        for fd in reversed(self.fds):
            try:
                os.close(fd)
            except OSError:
                pass
        self.fds = []
        return False

    def append(self, row):
        data = (approval_queue._canon(row) + "\n").encode("utf-8")
        try:
            flags = os.O_WRONLY | os.O_APPEND
            fd = approval_queue._open_file(self.dir_fd, RESULTS, flags)
            if fd is None:
                fd = approval_queue._open_file(self.dir_fd, RESULTS, flags, create=True)
            try:
                _need(os.fstat(fd).st_size + len(data) <= MAX_RESULTS, "send_io")
                view = memoryview(data)
                while view:
                    view = view[os.write(fd, view):]
                os.fsync(fd)
            finally:
                os.close(fd)
        except (approval_queue.ApprovalQueueError, OSError):
            raise MailSendError("send_io") from None


def _log(project, **row):
    """Append one attempt row. Returns False (never raises) when the log cannot be written."""
    row = {"schema": SCHEMA, "at": _stamp(row.pop("now", None) or datetime.now(timezone.utc)), **row}
    try:
        with _Dir(project, create=False) as d:
            d.append(row)
        return True
    except (MailSendError, OSError):
        return False


def results(project, *, strict=False):
    """Latest log row per (queue_id, revision), oldest first. Read-only; a missing log is empty."""
    try:
        with _Dir(project) as d:
            fd = approval_queue._open_file(d.dir_fd, RESULTS, os.O_RDONLY)
            if fd is None:
                return []
            try:
                raw = os.read(fd, MAX_RESULTS + 1)
            finally:
                os.close(fd)
    except (MailSendError, approval_queue.ApprovalQueueError, OSError):
        if strict:
            raise MailSendError("send_io") from None
        return []
    if strict and (len(raw) > MAX_RESULTS or (raw and not raw.endswith(b"\n"))):
        raise MailSendError("send_results_invalid")
    latest = {}
    for line in raw.split(b"\n"):
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            if strict:
                raise MailSendError("send_results_invalid") from None
            continue
        if strict and not (isinstance(row, dict) and row.get("schema") == SCHEMA and isinstance(row.get("queue_id"), str)):
            raise MailSendError("send_results_invalid")
        if isinstance(row, dict) and row.get("schema") == SCHEMA and isinstance(row.get("queue_id"), str):
            latest[(row["queue_id"], row.get("revision"))] = row
    return list(latest.values())


# ---- the checks that read the draft ---------------------------------------------------------

def _fetch_draft(imap_config, jrow, factory):
    """The one message with the journal's Message-ID in the Drafts folder, fetched read-only."""
    try:
        config = mail_drafts.load_config(imap_config)
    except mail_drafts.MailDraftError:
        raise MailSendError("mailbox_config_invalid") from None
    client = None
    try:
        client = mail_poll.connect(config, factory)
        status = client.select(imap_intake._quote(jrow["folder"]), readonly=True)
        _need(isinstance(status, tuple) and status[0] == "OK", "mailbox_unavailable")
        status, data = client.uid("SEARCH", None, "HEADER", "Message-ID", imap_intake._quote(jrow["message_id"]))
        _need(status == "OK", "mailbox_unavailable")
        uids = ((data or [b""])[0] or b"").split()
        _need(len(uids) != 0, "draft_missing")
        _need(len(uids) == 1, "draft_ambiguous")
        raw = mail_poll._fetch(client, int(uids[0]))
    except mail_poll.Refused:
        raise MailSendError("mailbox_unavailable") from None
    except (imaplib.IMAP4.error, OSError, EOFError, ValueError):
        raise MailSendError("mailbox_unavailable") from None
    finally:
        if client is not None:
            try:
                client.logout()
            except (imaplib.IMAP4.error, OSError, EOFError):
                pass
    return raw, config["from_addr"]


def _recipient(msg, jrow, form_to, agency_addresses):
    """(address, known). ``known`` = on the thread (placed with a To) or in the agency list."""
    tos = [a for _, a in getaddresses([str(msg.get("To") or "")]) if a]
    if len(tos) > 1:
        raise MailSendError("recipient_ambiguous")
    form_to = (form_to or "").strip()
    if tos:
        _need(not form_to or form_to.lower() == tos[0].lower(), "recipient_ambiguous")
        address = tos[0]
    else:
        _need(bool(form_to), "recipient_missing")
        _need(parseaddr(form_to)[1] == form_to and mail_drafts._ADDR.fullmatch(form_to)
              and not mail_drafts._CTRL.search(form_to), "recipient_invalid")
        address = form_to
    known = bool(tos and jrow.get("threaded")) or address.lower() in agency_addresses
    return address, known


def _context(project, config, queue_id, *, page_fingerprint, form_to, imap_factory):
    """Everything a send is built from, after every check that reads the draft. Sends and writes nothing."""
    rows = {r["id"]: r for r in approval_queue.load_queue(project)}
    row = rows.get(queue_id)
    _need(row is not None, "unknown_draft")
    _need(row["status"] == "approved", "not_approved")
    _need(not draft_residue.codes(row["body"]), "body_has_draft_residue")
    mine = {r["revision"]: r for r in mail_drafts.placements(project) if r["queue_id"] == queue_id}
    jrow = mine.get(row["revision"])
    _need(jrow is not None and jrow["state"] in ("placed", "found_existing"),
          "draft_superseded" if any(r["state"] == "superseded" for r in mine.values()) else "not_placed")
    _need(isinstance(jrow.get("fingerprint"), str) and jrow.get("folder") and jrow.get("message_id"), "not_placed")
    if page_fingerprint is not None:
        _need(page_fingerprint == jrow["fingerprint"], "fingerprint_stale")
    raw, from_addr = _fetch_draft(config["imap_config"], jrow, imap_factory)
    _need(mail_drafts.fingerprint(raw) == jrow["fingerprint"], "draft_changed_in_mailbox")
    try:
        msg = BytesParser(policy=policy.default).parsebytes(raw)
        subject = " ".join(str(msg["Subject"] or "").split())
        parents = mail_drafts._MSGID.findall(str(msg.get("In-Reply-To") or ""))
    except Exception:
        raise MailSendError("draft_unparseable") from None
    _need(bool(subject) and len(parents) <= 1, "draft_unparseable")
    address, known = _recipient(msg, jrow, form_to, config["agency_addresses"])
    draft = outbox.RequestDraft(
        request_id=row["request_id"], agency_id=row["agency_id"], scope_version=row["revision"], channel="email",
        subject=subject, body=row["body"], to=address, from_addr=from_addr, kind="send_followup",
        in_reply_to=parents[0] if parents else "", intent_id="recq-" + queue_id[:40])
    key = outbox.idempotency_key(draft.kind, draft.request_id, draft.scope_version, draft.intent_id)
    return {"row": row, "jrow": jrow, "draft": draft, "key": key, "address": address, "known": known,
            "fingerprint": jrow["fingerprint"]}


def _test_recipient(config, address):
    if config["live"] is not True:
        _need(bool(config["test_recipient"]), "test_recipient_missing")
        _need(address.lower() == config["test_recipient"], "test_mode_recipient")


def prepare(project, path, queue_id, *, to=None, imap_factory=mail_poll.default_connect, now=None):
    """Read-only preview for the console: what would be sent and the exact payload G signs with the outbox key."""
    switch = switch_state(project, path, now=now)
    out = {"switch": switch, "queue_id": queue_id, "code": None}
    if not switch["can_send"]:  # switched off: no mailbox contact at all
        out["code"] = switch["reason"]
        return out
    try:
        config = load_config(path)
        ctx = _context(project, config, queue_id, page_fingerprint=None, form_to=to, imap_factory=imap_factory)
        _test_recipient(config, ctx["address"])
    except MailSendError as error:
        out["code"] = error.code
        return out
    except approval_queue.ApprovalQueueError:
        out["code"] = "queue_unavailable"
        return out
    draft = ctx["draft"]
    out.update(ctx, payload=approval.payload("outbox", ctx["key"], outbox.preview(draft.to_json()), 0).decode("utf-8"))
    return out


# ---- the send --------------------------------------------------------------------------------

def _refuse(project, state, code, **fields):
    _log(project, state=state, code=code, **fields)
    return {"state": state, "code": code, **{k: v for k, v in fields.items() if k in ("message_id", "outbox_key")}}


def send_one(project, path, queue_id, *, actor, page_fingerprint, signature, to="", confirm="",
             imap_factory=mail_poll.default_connect, smtp_factory=None, now=None):
    """One click, one message. Returns {"state": sent|refused|failed|unknown, "code": ...}; never raises on refusal."""
    now = now or datetime.now(timezone.utc)
    base = {"queue_id": queue_id, "actor": actor, "now": now}
    try:
        config = load_config(path)
        _need(config["enabled"], "send_disabled")
        _need(not killed(project, config), "kill_switch")
        _need(isinstance(signature, str) and signature.strip(), "signature_required")
        zone = campaign_timezone(project)  # resolved here, never taken from the preview or the posted form
        ctx = _context(project, config, queue_id, page_fingerprint=page_fingerprint or "", form_to=to,
                       imap_factory=imap_factory)
        _test_recipient(config, ctx["address"])
    except MailSendError as error:
        return _refuse(project, "refused", error.code, **base)
    except approval_queue.ApprovalQueueError:
        return _refuse(project, "refused", "queue_unavailable", **base)
    base.update(revision=ctx["row"]["revision"], fingerprint=ctx["fingerprint"], recipient_known=ctx["known"])
    if not ctx["known"] and confirm != ctx["address"]:
        return _refuse(project, "refused", "confirmation_required", **base)
    draft, key = ctx["draft"], ctx["key"]
    base["outbox_key"] = key
    try:
        with _Dir(project, lock=True):
            box = outbox.Outbox(config["outbox_path"], clock=(lambda: _stamp(now)) if now else None)
            try:
                return _send_locked(project, config, box, draft, key, signature, base, smtp_factory, now, zone)
            finally:
                box.close()
    except MailSendError as error:
        return _refuse(project, "refused", error.code, **base)


def _send_locked(project, config, box, draft, key, signature, base, smtp_factory, now, zone):
    existing = box.propose(draft)
    state = existing["state"]
    if existing["draft_json"] != draft.to_json():  # the key omits the recipient: never send a stored text that is not the one reviewed
        return _refuse(project, "refused", "draft_payload_mismatch", **base)
    if state == "sent":
        receipt = json.loads(existing["provider_receipt"] or "{}")
        _log(project, state="sent", code="already_sent", message_id=receipt.get("provider_message_id"), **base)
        return {"state": "sent", "code": "already_sent", "message_id": receipt.get("provider_message_id"),
                "outbox_key": key}
    if state == "sending":
        return _refuse(project, "unknown", "delivery_unknown", **base)
    if state not in ("proposed", "approved"):
        return _refuse(project, "refused", "outbox_" + state, **base)
    if sent_today(config["outbox_path"], now, timezone_name=zone) >= config["daily_cap"]:
        return _refuse(project, "refused", "daily_cap_reached", **base)
    if killed(project, config):
        return _refuse(project, "refused", "kill_switch", **base)
    # The decision record comes first: no approval step or SMTP happens unless this row is durably written.
    if not _log(project, state="attempt", code=None, **base):
        return _refuse(project, "refused", "audit_log_unwritable", **base)
    evidence = {"kind": "ssh", "signature": signature}
    if state == "proposed":
        try:
            approved = box.approve(key, evidence)
        except outbox.OutboxError:
            return _refuse(project, "refused", "approval_refused", **base)
        base["approver"] = approved["approved_by"]
    else:  # already approved (late kill earlier): the signature on this page must verify for this exact payload
        try:
            if box._verify(existing, evidence, box._round(key)) != existing["approved_by"]:
                raise outbox.OutboxError("approver mismatch")
        except outbox.OutboxError:
            return _refuse(project, "refused", "approval_refused", **base)
        base["approver"] = existing["approved_by"]
    if killed(project, config):  # the switch may be thrown between approval and send: the row stays approved
        return _refuse(project, "refused", "kill_switch", **base)
    try:
        password = mail_poll._secret({"password_file": config["smtp_password_file"]})
    except mail_poll.Refused:
        return _refuse(project, "refused", "smtp_password_unavailable", **base)
    settings = outbox.SmtpSettings(config["smtp_host"], config["smtp_port"], config["smtp_user"], password,
                                   message_id_domain=draft.from_addr.rsplit("@", 1)[-1])
    try:
        sent = box.send(key, lambda d: outbox.send_email(d, settings, smtp_factory))
    except outbox.Blocked as error:
        return _refuse(project, "refused", "outbox_" + error.reason, **base)
    except outbox.AmbiguousFailure:
        return _refuse(project, "unknown", "delivery_unknown", **base)
    except outbox.OutboxError:
        return _refuse(project, "failed", "send_failed", **base)
    mid = json.loads(sent["provider_receipt"] or "{}").get("provider_message_id")
    out = {"state": "sent", "code": None, "message_id": mid, "outbox_key": key}
    if not _log(project, state="sent", code=None, message_id=mid, **base):
        out["audit"] = "outcome_not_recorded"  # the attempt row above stands as the record; the outbox holds the receipt
    return out


# ---- rendering (called by admin_panel; HTML only) ---------------------------------------------

def _latest_for(model, d):
    rows = [r for r in (model.get("mail_send") or []) if r["queue_id"] == d["id"] and r.get("revision") == d["revision"]]
    return rows[-1] if rows else None


def send_cell(model, d, live):
    """The Approve and send control for one approved, placed draft. Disabled with the reason when switched off."""
    sw = model.get("send")
    if sw is None or d["status"] != "approved":
        return ""
    placed = [r for r in (model.get("mail_drafts") or {}).get("rows", []) if r["queue_id"] == d["id"]
              and r["revision"] == d["revision"] and r["state"] in ("placed", "found_existing")]
    if not placed:
        return ""
    last = _latest_for(model, d)
    if last and last["state"] == "sent":
        return "<dt>Send</dt><dd>Sent %s (message id <code>%s</code>)</dd>" % (esc(last["at"]), esc(last.get("message_id") or ""))
    if last and last["state"] == "unknown":
        return "<dt>Send</dt><dd>Delivery unknown: reconcile in the outbox before any retry.</dd>"
    if last and last["state"] == "attempt":
        return "<dt>Send</dt><dd>Attempt recorded but no outcome: check the outbox before any retry.</dd>"
    note = ("Send refused: %s" % esc(last["code"])) if last and last["state"] in ("refused", "failed") else ""
    if not live:
        return "<dt>Send</dt><dd>%s</dd>" % note if note else ""
    if not sw["can_send"]:
        why = REASONS.get(sw["reason"], "Send is unavailable (%s)." % sw["reason"])
        return ('<dt>Send</dt><dd><button type="button" disabled>Approve and send</button> '
                "<span>Disabled: %s (%s)</span> %s</dd>" % (esc(why), esc(sw["reason"]), esc(note)))
    return ('<dt>Send</dt><dd><form method="get" action="/send"><input type="hidden" name="id" value="%s">'
            '<button type="submit">Approve and send</button></form> %s One message per click. '
            "Sent today: %d of %d.</dd>" % (esc(d["id"]), esc(note), sw["count"], sw["cap"]))


def render_prepare(info, csrf_token):
    """The review page behind the button: exact text, fingerprint, payload to sign, and the confirmations."""
    head = ('<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Approve and send</title></head><body>'
            "<h1>Approve and send</h1>")
    foot = '<p><a href="/">Back to the dashboard</a></p></body></html>\n'
    sw = info["switch"]
    if info["code"] or not sw["can_send"]:
        code = info["code"] or sw["reason"]
        return head + ("<p role=\"alert\">Not sendable: %s. %s</p>" % (esc(code), esc(REASONS.get(code, "")))) + foot
    d = info["draft"]
    out = [head, "<dl><dt>From</dt><dd>%s</dd><dt>To</dt><dd>%s</dd><dt>Subject</dt><dd>%s</dd>"
           "<dt>Text fingerprint</dt><dd><code>%s</code></dd><dt>Text</dt><dd><pre>%s</pre></dd></dl>" % (
               esc(d.from_addr), esc(d.to), esc(d.subject), esc(info["fingerprint"]), esc(d.body))]
    out.append("<p>This sends ONE real email to the address above. Sign the exact payload below with the approver key "
               "(<code>ssh-keygen -Y sign -n deflock-approval</code>) and paste the signature.</p><pre>%s</pre>"
               % esc(info["payload"]))
    out.append('<form method="post" action="/send"><input type="hidden" name="csrf_token" value="%s">'
               '<input type="hidden" name="id" value="%s"><input type="hidden" name="fingerprint" value="%s">'
               '<input type="hidden" name="to" value="%s">' % (
                   esc(csrf_token), esc(info["queue_id"]), esc(info["fingerprint"]), esc(d.to)))
    if info["known"]:
        out.append('<input type="hidden" name="confirm" value="">')
    else:
        out.append('<p role="alert">This address is not on the thread or in the agency list. Type it exactly to '
                   'confirm: <code>%s</code></p><label for="cf">Typed confirmation</label>'
                   '<input type="text" id="cf" name="confirm" required autocomplete="off">' % esc(d.to))
    out.append('<label for="sg">Detached SSH signature</label><textarea id="sg" name="signature" rows="6" required>'
               '</textarea><button type="submit">Approve and send</button></form>')
    return "".join(out) + foot


def render_need_to(queue_id):
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Recipient</title></head><body>'
            "<h1>Recipient needed</h1><p>This draft has no recipient (it is not threaded). Enter one address.</p>"
            '<form method="get" action="/send"><input type="hidden" name="id" value="%s">'
            '<label for="to">To</label><input type="text" id="to" name="to" required>'
            '<button type="submit">Preview</button></form><p><a href="/">Back</a></p></body></html>\n' % esc(queue_id))


def render_result(result):
    state, code = result["state"], result.get("code")
    text = {"sent": "Sent. Message id: %s%s" % (result.get("message_id"), " (already sent; nothing new was sent)"
                                                if code == "already_sent" else ""),
            "unknown": "Delivery unknown. Reconcile in the outbox before any retry.",
            "failed": "The send failed; nothing was delivered (%s)." % code}.get(state, "Send refused: %s. %s" % (
                code, REASONS.get(code, "")))
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Send result</title></head><body>'
            '<h1>Send result</h1><p role="status">%s</p><p><a href="/">Back to the dashboard</a></p></body></html>\n'
            % esc(text))


# ---- command line ----------------------------------------------------------------------------

def init(project, args):
    config = {"enabled": False, "live": False, "daily_cap": args.daily_cap, "smtp_host": args.smtp_host, "smtp_port": args.smtp_port,
              "smtp_user": args.smtp_user, "smtp_password_file": args.smtp_password_file,
              "imap_config": args.imap_config, "outbox_path": args.outbox_path
              or os.path.join(os.path.abspath(project), "outbox", "outbox.sqlite")}
    if args.test_recipient is not None:
        _need(bool(mail_drafts._ADDR.fullmatch(args.test_recipient)), "config_field:test_recipient")
        config["test_recipient"] = args.test_recipient.lower()
    with _Dir(project, create=True) as d:
        try:
            for name, data in ((CONFIG, (json.dumps(config, indent=1, sort_keys=True) + "\n").encode()),
                               (KILL, b"send switched off; remove by hand to allow sending\n")):
                fd = approval_queue._open_file(d.dir_fd, name, os.O_WRONLY, create=True)
                _need(fd is not None, "send_io")
                with os.fdopen(fd, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
        except approval_queue.ApprovalQueueError:
            raise MailSendError("already_initialized") from None


def main(argv=None):
    parser = argparse.ArgumentParser(prog="records mail-send", description=(
        "Set up and inspect the console send switches. There is no send command: sending is the console click only."))
    sub = parser.add_subparsers(dest="action", required=True)
    init_p = sub.add_parser("init", help="write send.json (enabled false) and the SEND-STOP kill file")
    init_p.add_argument("--project", required=True)
    init_p.add_argument("--imap-config", required=True)
    init_p.add_argument("--smtp-host", required=True)
    init_p.add_argument("--smtp-user", required=True)
    init_p.add_argument("--smtp-password-file", required=True)
    init_p.add_argument("--smtp-port", type=int, default=587)
    init_p.add_argument("--daily-cap", type=int, default=DEFAULT_CAP)
    init_p.add_argument("--outbox-path")
    init_p.add_argument("--test-recipient")
    status_p = sub.add_parser("status", help="switch state, today's count and cap; no secrets")
    status_p.add_argument("--project", required=True)
    status_p.add_argument("--config")
    args = parser.parse_args(argv)
    try:
        if args.action == "init":
            init(args.project, args)
            print(json.dumps({"enabled": False, "kill_switch": True}))
            return 0
        state = switch_state(args.project, args.config or config_path(args.project))
        print(json.dumps({**state, "kill_switch": killed(args.project)}, sort_keys=True))
        return 0
    except MailSendError as error:
        print(error.code, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
