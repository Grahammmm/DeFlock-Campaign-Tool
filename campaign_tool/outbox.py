"""Transactional outbox: the only code that may send a records request.

The outbox keeps a local SQLite journal (``outbox.sqlite``, mode 0600) with
one row per intended send. Rows move proposed -> approved -> sending ->
sent | failed. The idempotency key is ``sha256(kind + request_id +
scope_version)`` for requests/legacy follow-ups. New follow-ups append a
versioned, approved intent identity; replay never becomes another send.
Safeguards, all enforced in
:meth:`Outbox.send`:

* nothing sends unless the row is ``approved`` with an ``approved_by``;
* one send per agency per UTC day;
* the request's fee cap must be within the campaign cap;
* a send that ended in an ambiguous state (``sending`` with no provider
  receipt, e.g. a crash or timeout after SMTP DATA) blocks every later send
  for that request until :meth:`Outbox.reconcile` resolves it.

Transports: ``send_email`` (smtplib with STARTTLS; Message-ID minted here)
and ``file_muckrock`` (MuckRock API v2 ``POST /foia/``; HTTP 402 becomes
``blocked: no_credits``). Both take their credentials as arguments and
never read them from the environment. Workspace executors in a later phase
mirror this journal; the CLI below is for an organizer's own machine.

Usage:
    python3 -m campaign_tool.outbox propose|approve|send|reconcile|list ...
"""
import argparse
import datetime as dt
import email.utils
import hashlib
import json
import os
import smtplib
import ssl
import sqlite3
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from email.message import EmailMessage
from pathlib import Path

STATES = ("proposed", "approved", "sending", "sent", "failed", "blocked")
KINDS = ("send_request", "send_followup")
CHANNELS = ("email", "muckrock")
DAILY_AGENCY_CAP = 1
SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox (
  idempotency_key TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  request_id TEXT NOT NULL,
  agency_id TEXT NOT NULL,
  scope_version INTEGER NOT NULL,
  channel TEXT NOT NULL,
  draft_json TEXT NOT NULL,
  fee_cap_cents INTEGER NOT NULL DEFAULT 0,
  state TEXT NOT NULL,
  approved_by TEXT,
  approved_at TEXT,
  sending_at TEXT,
  sent_at TEXT,
  provider_receipt TEXT,
  error TEXT,
  resolved_by TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS outbox_agency_day ON outbox(agency_id, sent_at);
CREATE TABLE IF NOT EXISTS outbox_retry_approval (
  event_id INTEGER PRIMARY KEY,
  idempotency_key TEXT NOT NULL,
  prior_approved_by TEXT,
  prior_approved_at TEXT,
  failed_at TEXT NOT NULL,
  prior_error TEXT,
  resolved_by TEXT,
  approved_by TEXT NOT NULL,
  approved_at TEXT NOT NULL,
  recorded_at TEXT NOT NULL
);
"""


class OutboxError(Exception):
    pass


class Blocked(OutboxError):
    """The send was refused by a safeguard; ``reason`` is machine-readable."""

    def __init__(self, reason, detail=""):
        super().__init__(reason + (": " + detail if detail else ""))
        self.reason = reason
        self.detail = detail


class AmbiguousFailure(OutboxError):
    """The transport may or may not have delivered; the journal stays ``sending``."""


def now_iso():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def idempotency_key(kind, request_id, scope_version, intent_id=""):
    if kind not in KINDS:
        raise ValueError("kind must be one of " + "|".join(KINDS))
    if not isinstance(intent_id, str):
        raise ValueError("invalid follow-up intent")
    identity = f"{kind}\x00{request_id}\x00{int(scope_version)}"
    if intent_id:
        if kind != "send_followup" or not isinstance(intent_id, str) or len(intent_id) > 100 or any(
                c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-" for c in intent_id):
            raise ValueError("invalid follow-up intent")
        identity += "\x00followup-intent-v1\x00" + intent_id
    return hashlib.sha256(identity.encode()).hexdigest()


@dataclass
class RequestDraft:
    """What will be sent, verbatim. ``to`` is a mailbox for email or a MuckRock agency id."""
    request_id: str
    agency_id: str
    scope_version: int
    channel: str
    subject: str
    body: str
    to: str
    from_addr: str = ""
    fee_cap_cents: int = 0
    kind: str = "send_request"
    in_reply_to: str = ""
    extra: dict = field(default_factory=dict)
    intent_id: str = ""

    def to_json(self):
        fields = dict(self.__dict__)
        if not self.intent_id:
            fields.pop("intent_id")  # Preserve existing journal bytes for legacy keys.
        return json.dumps(fields, sort_keys=True)

    @classmethod
    def from_json(cls, text):
        return cls(**json.loads(text))


@dataclass
class SmtpSettings:
    host: str
    port: int = 587
    username: str = ""
    password: str = ""
    starttls: bool = True
    timeout: int = 60
    message_id_domain: str = ""


@dataclass
class ProviderReceipt:
    channel: str
    provider_message_id: str
    url: str = ""
    raw: dict = field(default_factory=dict)

    def to_json(self):
        return json.dumps(self.__dict__, sort_keys=True)


def _mint_message_id(domain):
    return email.utils.make_msgid(domain=domain or None)


def send_email(draft, settings, smtp_factory=None):
    """Deliver ``draft`` over SMTP (STARTTLS unless disabled). Returns a ProviderReceipt.

    Raises AmbiguousFailure when the failure happened after the message may
    have been accepted, so the caller keeps the journal row in ``sending``.
    """
    if draft.channel != "email":
        raise ValueError("draft channel is not email")
    if not draft.to or not draft.from_addr:
        raise ValueError("email drafts need to and from_addr")
    message = EmailMessage()
    message["From"] = draft.from_addr
    message["To"] = draft.to
    message["Subject"] = draft.subject
    message["Date"] = email.utils.formatdate(localtime=False)
    message_id = _mint_message_id(settings.message_id_domain or draft.from_addr.rsplit("@", 1)[-1])
    message["Message-ID"] = message_id
    if draft.in_reply_to:
        message["In-Reply-To"] = draft.in_reply_to
        message["References"] = draft.in_reply_to
    message.set_content(draft.body)
    factory = smtp_factory or (lambda: smtplib.SMTP(settings.host, settings.port, timeout=settings.timeout))
    accepted = False
    in_data = False  # set just before DATA: an error before it cannot have delivered
    try:
        with factory() as smtp:
            smtp.ehlo()
            if settings.starttls:
                # Verified TLS with hostname check: the password and the request body
                # never go to a server presenting an arbitrary certificate.
                smtp.starttls(context=ssl.create_default_context())
                smtp.ehlo()
            if settings.username:
                smtp.login(settings.username, settings.password)
            in_data = True
            refused = smtp.send_message(message)
            accepted = True
            if refused:
                # send_message returns normally only when at least one recipient
                # was accepted. A nonempty refusal map therefore means partial
                # delivery, not a safe-to-retry rejection of the whole message.
                raise AmbiguousFailure("smtp partial recipient acceptance; reconcile before retry")
    except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused, smtplib.SMTPAuthenticationError, smtplib.SMTPHeloError) as exc:
        raise OutboxError("smtp refused: " + str(exc)) from None
    except (smtplib.SMTPException, OSError) as exc:
        if accepted:
            raise AmbiguousFailure("smtp error after DATA: " + str(exc)) from None
        if not in_data:
            raise OutboxError("smtp connection failed before DATA: " + str(exc)) from None
        raise AmbiguousFailure("smtp error during DATA; delivery unknown: " + str(exc)) from None
    return ProviderReceipt("email", message_id.strip("<>"), raw={"to": draft.to, "subject": draft.subject})


def file_muckrock(draft, token, base_url="https://www.muckrock.com/api_v2", opener=None):
    """POST /foia/ on MuckRock API v2. 402 -> Blocked('no_credits')."""
    if draft.channel != "muckrock":
        raise ValueError("draft channel is not muckrock")
    if not token:
        raise ValueError("MuckRock token required")
    try:
        agency_id = int(draft.to)
    except (TypeError, ValueError):
        raise ValueError("draft.to must be a MuckRock agency id") from None
    payload = {"agencies": [agency_id], "title": draft.subject, "requested_docs": draft.body}
    request = urllib.request.Request(
        base_url.rstrip("/") + "/foia/",
        data=json.dumps(payload).encode("utf-8"),
        headers={"authorization": "Token " + token, "content-type": "application/json", "accept": "application/json"},
        method="POST")
    open_fn = opener or urllib.request.urlopen
    try:
        with open_fn(request, timeout=60) as response:
            body = json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        if exc.code == 402:
            raise Blocked("no_credits", "MuckRock returned 402 Payment Required") from None
        if exc.code in (401, 403):
            raise OutboxError(f"muckrock auth failure {exc.code}") from None
        if exc.code >= 500:
            raise AmbiguousFailure(f"muckrock HTTP {exc.code}; filing state unknown") from None
        raise OutboxError(f"muckrock HTTP {exc.code}") from None
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise AmbiguousFailure("muckrock unreachable after request: " + str(getattr(exc, "reason", exc))) from None
    requests = body.get("requests") or ([body] if "id" in body else [])
    if not requests or "id" not in requests[0]:
        raise AmbiguousFailure("muckrock reply had no request id")
    first = requests[0]
    url = first.get("absolute_url") or first.get("url") or ""
    return ProviderReceipt("muckrock", str(first["id"]), url=url, raw={"status": first.get("status")})


class Outbox:
    def __init__(self, path, campaign_fee_cap_cents=0, daily_agency_cap=DAILY_AGENCY_CAP, clock=None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        existed = self.path.exists()
        self.db = sqlite3.connect(self.path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        if not existed or (os.stat(self.path).st_mode & 0o777) != 0o600:
            os.chmod(self.path, 0o600)
        self.campaign_fee_cap_cents = None if campaign_fee_cap_cents is None else int(campaign_fee_cap_cents)
        self.daily_agency_cap = int(daily_agency_cap)
        self.clock = clock or now_iso

    def close(self):
        self.db.close()

    # -- journal -------------------------------------------------------------
    def row(self, key):
        r = self.db.execute("SELECT * FROM outbox WHERE idempotency_key = ?", (key,)).fetchone()
        return dict(r) if r else None

    def rows(self, state=None):
        if state:
            cur = self.db.execute("SELECT * FROM outbox WHERE state = ? ORDER BY created_at", (state,))
        else:
            cur = self.db.execute("SELECT * FROM outbox ORDER BY created_at")
        return [dict(r) for r in cur]

    def propose(self, draft):
        """Insert a ``proposed`` row; idempotent on the key (returns the existing row)."""
        if draft.channel not in CHANNELS:
            raise ValueError("channel must be one of " + "|".join(CHANNELS))
        key = idempotency_key(draft.kind, draft.request_id, draft.scope_version, draft.intent_id)
        existing = self.row(key)
        if existing:
            return existing
        stamp = self.clock()
        self.db.execute(
            "INSERT INTO outbox (idempotency_key, kind, request_id, agency_id, scope_version, channel, draft_json, fee_cap_cents, state, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,'proposed',?,?)",
            (key, draft.kind, draft.request_id, draft.agency_id, int(draft.scope_version), draft.channel, draft.to_json(), int(draft.fee_cap_cents), stamp, stamp))
        return self.row(key)

    def approve(self, key, approved_by):
        if not approved_by:
            raise OutboxError("approval requires an identity")
        row = self.row(key)
        if not row:
            raise OutboxError("unknown outbox row")
        if row["state"] != "proposed":
            raise OutboxError(f"row is {row['state']}, not proposed")
        stamp = self.clock()
        self.db.execute("UPDATE outbox SET state='approved', approved_by=?, approved_at=?, updated_at=? WHERE idempotency_key=?",
                        (approved_by, stamp, stamp, key))
        return self.row(key)

    def _set(self, key, **fields):
        fields["updated_at"] = self.clock()
        cols = ", ".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE outbox SET {cols} WHERE idempotency_key=?", (*fields.values(), key))

    def approve_retry(self, key, approved_by, approved_at):
        """Trusted caller's new approval after definite failure; never clear uncertainty.

        This does not send. The usual send caps and unresolved-request check remain.
        The caller must authenticate the approver and bind approval to this draft.
        """
        if not isinstance(approved_by, str) or not approved_by.strip():
            raise OutboxError("retry approval requires an identity")
        try:
            if not isinstance(approved_at, str) or len(approved_at) > 40 or not approved_at.endswith("Z"):
                raise ValueError("UTC approval required")
            approval = dt.datetime.fromisoformat(approved_at.replace("Z", "+00:00"))
        except (TypeError, ValueError) as exc:
            raise OutboxError("invalid retry approval time") from exc
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.row(key)
            if not row or row["state"] != "failed" or row["provider_receipt"] or row["sending_at"] or row["sent_at"]:
                raise OutboxError("retry requires a definite failure without delivery evidence")
            failed = dt.datetime.fromisoformat(row["updated_at"].replace("Z", "+00:00"))
            if approval <= failed:
                raise OutboxError("retry approval must follow the failure")
            stamp = self.clock()
            self.db.execute("INSERT INTO outbox_retry_approval (idempotency_key, prior_approved_by, prior_approved_at, "
                "failed_at, prior_error, resolved_by, approved_by, approved_at, recorded_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (key, row["approved_by"], row["approved_at"], row["updated_at"], row["error"], row["resolved_by"],
                 approved_by, approved_at, stamp))
            self.db.execute("UPDATE outbox SET state='approved', approved_by=?, approved_at=?, error=NULL, updated_at=? "
                "WHERE idempotency_key=? AND state='failed'", (approved_by, approved_at, stamp, key))
            self.db.execute("COMMIT")
        except BaseException:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise
        return self.row(key)

    # -- safeguards ----------------------------------------------------------
    def _check(self, row):
        if row["state"] == "sent":
            return "already_sent"
        if row["state"] == "sending":
            raise Blocked("ambiguous_send_unresolved", "a previous attempt is still 'sending'; run reconcile first")
        if row["state"] == "blocked":
            raise Blocked("blocked", row.get("error") or "")
        if row["state"] != "approved" or not row["approved_by"]:
            raise Blocked("not_approved", f"state is {row['state']}")
        unresolved = self.db.execute(
            "SELECT count(*) FROM outbox WHERE request_id=? AND state='sending' AND idempotency_key<>?",
            (row["request_id"], row["idempotency_key"])).fetchone()[0]
        if unresolved:
            raise Blocked("ambiguous_send_unresolved", "another send for this request is unresolved")
        if row["kind"] == "send_followup" and RequestDraft.from_json(row["draft_json"]).intent_id:
            legacy = idempotency_key("send_followup", row["request_id"], row["scope_version"])
            if self.row(legacy):
                raise Blocked("legacy_followup_intent_unresolved", "legacy follow-up has no trustworthy intent mapping; inspect before migration")
        # A cap of 0 (the default) blocks any draft that offers a fee; only an explicit
        # None lifts the gate, so forgetting --fee-cap-cents can never allow a fee.
        if self.campaign_fee_cap_cents is not None and row["fee_cap_cents"] > self.campaign_fee_cap_cents:
            raise Blocked("fee_cap_exceeded", f"{row['fee_cap_cents']} > campaign cap {self.campaign_fee_cap_cents}")
        today = self.clock()[:10]
        sent_today = self.db.execute(
            "SELECT count(*) FROM outbox WHERE agency_id=? AND state IN ('sent','sending') AND substr(coalesce(sent_at, sending_at),1,10)=?",
            (row["agency_id"], today)).fetchone()[0]
        if sent_today >= self.daily_agency_cap:
            raise Blocked("daily_agency_cap", f"{sent_today} send(s) to {row['agency_id']} today")
        return None

    # -- send ----------------------------------------------------------------
    def send(self, key, transport):
        """Run ``transport(draft) -> ProviderReceipt`` for an approved row.

        Returns the row. A second call with the same key after success is a
        no-op that returns the sent row. Blocked raises; an ambiguous failure
        leaves the row in ``sending`` and raises AmbiguousFailure.
        """
        # Serialize the safeguards and claim across keys, not only the final row UPDATE.
        # Otherwise concurrent requests can both observe an unused agency/day budget.
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.row(key)
            if not row:
                raise OutboxError("unknown outbox row")
            if self._check(row) == "already_sent":
                self.db.execute("COMMIT")
                return row
            draft = RequestDraft.from_json(row["draft_json"])
            stamp = self.clock()
            claimed = self.db.execute(
                "UPDATE outbox SET state='sending', sending_at=?, error=NULL, updated_at=? WHERE idempotency_key=? AND state='approved'",
                (stamp, stamp, key)).rowcount
            if claimed != 1:
                raise Blocked("ambiguous_send_unresolved", "another send claimed this row first; run reconcile if it did not finish")
            self.db.execute("COMMIT")
        except BaseException:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise
        # The network operation must never hold the SQLite write transaction open.
        try:
            receipt = transport(draft)
        except Blocked as exc:
            self._set(key, state="blocked", error=exc.reason + (": " + exc.detail if exc.detail else ""), sending_at=None)
            raise
        except AmbiguousFailure as exc:
            self._set(key, error="ambiguous: " + str(exc))
            raise
        except OutboxError as exc:
            self._set(key, state="failed", error=str(exc), sending_at=None)
            raise
        except Exception as exc:  # unknown transport state: keep 'sending'
            self._set(key, error="ambiguous: " + repr(exc))
            raise AmbiguousFailure(repr(exc)) from exc
        self._set(key, state="sent", sent_at=self.clock(), provider_receipt=receipt.to_json())
        return self.row(key)

    def reconcile(self, key, outcome, resolved_by, provider_receipt=None):
        """Resolve a ``sending`` row by hand: ``delivered`` -> sent, ``not_delivered`` -> failed."""
        row = self.row(key)
        if not row:
            raise OutboxError("unknown outbox row")
        if row["state"] != "sending":
            raise OutboxError(f"row is {row['state']}, nothing to reconcile")
        if not resolved_by:
            raise OutboxError("reconcile requires an identity")
        if outcome == "delivered":
            self._set(key, state="sent", sent_at=row["sending_at"] or self.clock(), resolved_by=resolved_by,
                      provider_receipt=provider_receipt.to_json() if isinstance(provider_receipt, ProviderReceipt) else (provider_receipt or row["provider_receipt"]))
        elif outcome == "not_delivered":
            self._set(key, state="failed", sending_at=None, resolved_by=resolved_by, error="reconciled as not delivered")
        else:
            raise ValueError("outcome must be delivered|not_delivered")
        return self.row(key)

    def unresolved(self):
        return self.rows("sending")


# -- CLI ----------------------------------------------------------------------

def main(argv=None):
    p = argparse.ArgumentParser(description="Local outbox journal for records requests; never sends without an approved row.")
    p.add_argument("--journal", required=True, help="path to outbox.sqlite (private directory)")
    p.add_argument("--fee-cap-cents", type=int, default=0, help="largest fee a draft may offer; 0 (default) blocks any fee")
    sub = p.add_subparsers(dest="command", required=True)
    pr = sub.add_parser("propose"); pr.add_argument("draft_json", help="file with RequestDraft fields")
    ap = sub.add_parser("approve"); ap.add_argument("key"); ap.add_argument("--by", required=True)
    se = sub.add_parser("send"); se.add_argument("key")
    se.add_argument("--smtp-host"); se.add_argument("--smtp-port", type=int, default=587)
    se.add_argument("--smtp-user", default=""); se.add_argument("--smtp-password-env", default="OUTBOX_SMTP_PASSWORD")
    se.add_argument("--muckrock-token-env", default="MUCKROCK_TOKEN")
    rc = sub.add_parser("reconcile"); rc.add_argument("key"); rc.add_argument("outcome", choices=["delivered", "not_delivered"]); rc.add_argument("--by", required=True)
    ls = sub.add_parser("list"); ls.add_argument("--state")
    args = p.parse_args(argv)
    box = Outbox(args.journal, campaign_fee_cap_cents=args.fee_cap_cents)
    try:
        if args.command == "propose":
            draft = RequestDraft(**json.loads(Path(args.draft_json).read_text(encoding="utf-8")))
            row = box.propose(draft)
            print(json.dumps({"idempotency_key": row["idempotency_key"], "state": row["state"]}))
        elif args.command == "approve":
            print(json.dumps({"state": box.approve(args.key, args.by)["state"]}))
        elif args.command == "send":
            row = box.row(args.key)
            if not row:
                p.error("unknown key")
            if row["channel"] == "email":
                if not args.smtp_host:
                    p.error("--smtp-host required for email")
                settings = SmtpSettings(args.smtp_host, args.smtp_port, args.smtp_user, os.environ.get(args.smtp_password_env, ""))
                transport = lambda draft: send_email(draft, settings)
            else:
                token = os.environ.get(args.muckrock_token_env, "")
                transport = lambda draft: file_muckrock(draft, token)
            try:
                row = box.send(args.key, transport)
                print(json.dumps({"state": row["state"], "provider_receipt": row["provider_receipt"]}))
            except Blocked as exc:
                print(json.dumps({"state": "blocked", "reason": exc.reason}))
                return 2
            except AmbiguousFailure as exc:
                print(json.dumps({"state": "sending", "error": str(exc), "next": "reconcile before any resend"}))
                return 3
        elif args.command == "reconcile":
            print(json.dumps({"state": box.reconcile(args.key, args.outcome, args.by)["state"]}))
        elif args.command == "list":
            for row in box.rows(args.state):
                print(json.dumps({k: row[k] for k in ("idempotency_key", "kind", "request_id", "agency_id", "channel", "state", "sent_at")}))
    finally:
        box.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
