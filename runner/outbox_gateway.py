"""Opt-in private HTTP adapter for the existing transactional outbox.

No listener, credentials, provider or schedule is installed by importing this module.
The deployment owner supplies the private journal, shared signing key and transports.
"""
import datetime as dt
import hashlib
import hmac
import json
import threading
from http.server import BaseHTTPRequestHandler

from campaign_tool.outbox import (AmbiguousFailure, Blocked, Outbox, OutboxError,
                                 RequestDraft, idempotency_key)

MAX_FRAME_BYTES = 64 * 1024
DOMAIN = b"deflock-outbox-v1\n"


def signature(secret, raw):
    return hmac.new(secret.encode("utf-8"), DOMAIN + raw, hashlib.sha256).hexdigest()


def _stamp(value):
    if not isinstance(value, str) or len(value) > 40 or not value.endswith("Z"):
        raise ValueError("timestamp")
    stamp = dt.datetime.fromisoformat(value[:-1] + "+00:00")
    if stamp.tzinfo is None:
        raise ValueError("timestamp")
    return stamp


def _text(value, limit, multiline=False):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError("text")
    if "\0" in value or (not multiline and ("\r" in value or "\n" in value)):
        raise ValueError("text")
    return value


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate field")
        result[key] = value
    return result


class ApprovedOutboxGateway:
    """One campaign, one private journal; only a trusted signer attests approval.

    The signing key authorizes outbound effects. Keep it separate from runner tokens.
    A forged/stale frame is refused before opening the journal or invoking transport.
    """
    def __init__(self, campaign_id, secret, journal, from_addr, transports,
                 campaign_fee_cap_cents=0, clock=None):
        if not isinstance(secret, str) or len(secret) < 32:
            raise ValueError("signing key must contain at least 32 characters")
        self.campaign_id = _text(campaign_id, 100)
        self.secret = secret
        self.journal = journal
        self.from_addr = _text(from_addr, 320)
        self.transports = dict(transports)
        self.fee_cap = campaign_fee_cap_cents
        self.clock = clock or (lambda: dt.datetime.now(dt.timezone.utc))
        self.lock = threading.Lock()

    def dispatch(self, raw, supplied_signature):
        if not isinstance(raw, bytes) or len(raw) > MAX_FRAME_BYTES:
            return 413, {"code": "frame_too_large"}
        if (not isinstance(supplied_signature, str) or len(supplied_signature) != 64 or
                not hmac.compare_digest(signature(self.secret, raw), supplied_signature)):
            return 401, {"code": "invalid_signature"}
        try:
            frame = json.loads(raw, object_pairs_hook=_unique_object)
            if not isinstance(frame, dict) or set(frame) != {
                    "v", "campaign_id", "action_id", "approved_by", "approved_at",
                    "issued_at", "canonical_key", "draft"}:
                raise ValueError("frame")
            if type(frame["v"]) is not int or frame["v"] != 1 or frame["campaign_id"] != self.campaign_id:
                raise ValueError("campaign")
            _text(frame["action_id"], 100)
            _text(frame["approved_by"], 320)
            issued = _stamp(frame["issued_at"])
            approved = _stamp(frame["approved_at"])
            now = self.clock()
            if abs((now - issued).total_seconds()) > 300 or approved > issued:
                raise ValueError("approval time")
            fields = frame["draft"]
            required = {"request_id", "agency_id", "scope_version", "channel", "subject", "body", "to", "fee_cap_cents", "kind"}
            if not isinstance(fields, dict) or not required <= set(fields) or set(fields) - required - {"in_reply_to", "intent_id"}:
                raise ValueError("draft")
            for key, limit in [("request_id", 100), ("agency_id", 100), ("subject", 200), ("to", 320)]:
                _text(fields[key], limit)
            _text(fields["body"], 32000, multiline=True)
            if type(fields["scope_version"]) is not int or fields["scope_version"] < 1:
                raise ValueError("scope")
            if type(fields["fee_cap_cents"]) is not int or fields["fee_cap_cents"] < 0:
                raise ValueError("fee")
            if fields["channel"] not in ("email", "muckrock") or fields["kind"] not in ("send_request", "send_followup"):
                raise ValueError("channel")
            if fields["kind"] == "send_followup":
                _text(fields.get("intent_id"), 100)
            elif "intent_id" in fields:
                raise ValueError("request must not have follow-up intent")
            if "in_reply_to" in fields:
                if not isinstance(fields["in_reply_to"], str):
                    raise ValueError("reply identity")
                if fields["in_reply_to"]:
                    _text(fields["in_reply_to"], 1000)
            draft = RequestDraft(**fields, from_addr=self.from_addr)
            key = idempotency_key(draft.kind, draft.request_id, draft.scope_version, draft.intent_id)
            if frame["canonical_key"] != key:
                raise ValueError("key")
        except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
            return 400, {"code": "invalid_frame"}
        transport = self.transports.get(draft.channel)
        if transport is None:
            return 503, {"code": "transport_not_configured"}
        def checked_transport(value):
            receipt = transport(value)
            try:
                if receipt.channel != draft.channel:
                    raise ValueError("receipt channel")
                _text(receipt.provider_message_id, 1000)
            except (AttributeError, ValueError, TypeError) as exc:
                # Provider may have accepted the message: preserve the journal hold.
                raise AmbiguousFailure("invalid provider receipt") from exc
            return receipt
        with self.lock:
            box = None
            try:
                box = Outbox(self.journal, campaign_fee_cap_cents=self.fee_cap,
                    clock=lambda: self.clock().astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z"))
                row = box.propose(draft)
                if row["draft_json"] != draft.to_json():
                    return 409, {"code": "draft_conflict"}
                if row["state"] == "proposed":
                    box.approve(key, frame["approved_by"])
                elif row["state"] == "approved" and row["approved_by"] != frame["approved_by"]:
                    return 409, {"code": "approval_conflict"}
                if row["state"] == "failed":
                    try:
                        box.approve_retry(key, frame["approved_by"], frame["approved_at"])
                    except OutboxError:
                        return 409, {"code": "fresh_outbox_approval_required"}
                row = box.send(key, checked_transport)
                receipt = json.loads(row["provider_receipt"] or "null")
                if not isinstance(receipt, dict) or receipt.get("channel") != draft.channel:
                    raise ValueError("receipt channel")
                _text(receipt.get("provider_message_id"), 1000)
                _stamp(row["sent_at"])
                return 200, {"v": 1, "action_id": frame["action_id"], "canonical_key": key,
                             "provider_message_id": receipt["provider_message_id"],
                             "provider": receipt["channel"], "sent_at": row["sent_at"]}
            except AmbiguousFailure:
                return 503, {"code": "outbox_ambiguous"}
            except Blocked as exc:
                return 409, {"code": "outbox_ambiguous" if exc.reason == "ambiguous_send_unresolved" else "outbox_blocked", "reason": exc.reason}
            except OutboxError:
                return 409, {"code": "outbox_refused"}
            except Exception:
                # Never return provider exception text, addresses, draft text or secrets.
                return 503, {"code": "outbox_ambiguous"}
            finally:
                if box is not None:
                    box.close()


def make_handler(gateway):
    """Handler factory only; caller must provide a private/TLS-protected listener."""
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            if self.path != "/send":
                self.send_error(404); return
            try:
                size = int(self.headers.get("Content-Length", "-1"))
            except ValueError:
                size = -1
            if size < 0 or size > MAX_FRAME_BYTES or self.headers.get("Transfer-Encoding"):
                self.send_error(413); return
            raw = self.rfile.read(size)
            if len(raw) != size:
                self.send_error(400); return
            status, value = gateway.dispatch(raw, self.headers.get("X-Outbox-Signature"))
            body = json.dumps(value).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers(); self.wfile.write(body)

        def log_message(self, *args):
            pass  # Do not log approval envelopes, headers or provider errors.
    return Handler
