"""Private manual-event form and append-only journal (REC-CON-4); no transport."""
import hashlib
import hmac
import json
import os
import re
import secrets
import time
import urllib.parse
from datetime import date
from pathlib import Path

from . import approval_queue as aq
from . import console_documents as cd
from .outcomes import timeline as tl
from .outcomes.reading import esc

TYPES = ("call", "portal check", "hand note")
FIELDS = ("csrf_token", "id", "ticket", "type", "date", "summary", "corrects")
ROUTE = ("manual", FIELDS)
TTL = 3600


class ManualError(ValueError):
    def __init__(self, status):
        self.status = status


def _require(ok, status=400):
    if not ok:
        raise ManualError(status)


def _register(project, run_id):
    run = cd._check_run(project, run_id)
    raw = tl._read_bytes(run / "CURRENT-REQUEST-REGISTER.json")
    doc = tl._loads(raw)
    _require(type(doc) is dict and type(doc.get("requests")) is list
             and all(type(row) is dict for row in doc["requests"]), 503)
    return doc, hashlib.sha256(raw).hexdigest()


def _target(doc, agency, request):
    return any(tl._matches(row, agency, request) for row in doc["requests"])


def _sign(payload, key):
    return hmac.new(key.encode(), payload.encode(), hashlib.sha256).hexdigest()


def ticket(project, run_id, agency, request, key, ident, now=None):
    doc, fingerprint = _register(project, run_id)
    _require(_target(doc, agency, request), 404)
    payload = json.dumps([run_id, agency, request, ident, fingerprint, int(time.time() if now is None else now)],
                         separators=(",", ":"))
    return payload + "." + _sign(payload, key)


def append(project, run_id, actor, key, fields, now=None):
    """Validate a form's target/version, then append once under the existing journal lock discipline."""
    from .admin_panel import check_actor
    check_actor(actor)
    payload, separator, signature = fields["ticket"].rpartition(".")
    _require(separator and re.fullmatch(r"[a-f0-9]{64}", signature)
             and hmac.compare_digest(signature, _sign(payload, key)), 403)
    try:
        run, agency, request, ident, fingerprint, issued = json.loads(payload)
    except (ValueError, TypeError):
        raise ManualError(400) from None
    _require(run == run_id and ident == fields["id"] and re.fullmatch(r"[a-f0-9]{32}", ident))
    now = time.time() if now is None else now
    _require(type(issued) is int and 0 <= now - issued <= TTL, 410)
    doc, current = _register(project, run_id)
    _require(current == fingerprint and _target(doc, agency, request), 409)
    kind, day, summary, corrects = (fields[k] for k in ("type", "date", "summary", "corrects"))
    _require(kind in TYPES and re.fullmatch(r"\d{4}-\d{2}-\d{2}", day))
    try:
        date.fromisoformat(day)
    except ValueError:
        raise ManualError(400) from None
    _require(0 < len(summary.strip()) <= 2000 and all(c.isprintable() for c in summary))
    _require(not corrects or re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", corrects))
    event = dict(event_id=ident, agency_id=agency, request_id=request, type=kind, date=day,
                 actor=actor, summary=summary)
    if corrects:
        event["corrects"] = corrects
    # Reuse the approval store's private, symlink-safe lock; never append to its journal.
    with aq._Store(project, True):
        root = aq._open_chain(project)
        fd = None
        try:
            fd = aq._open_file(root, "manual-events.jsonl", os.O_RDWR | os.O_APPEND)
            if fd is None:
                fd = aq._open_file(root, "manual-events.jsonl", os.O_RDWR | os.O_APPEND, create=True)
            if fd is None:
                raise OSError("manual_open_failed")
            info = os.fstat(fd)
            _require(info.st_size <= tl.MAX_INPUT_BYTES, 503)
            raw = os.read(fd, tl.MAX_INPUT_BYTES + 1)
            _require(not raw or raw.endswith(b"\n"), 503)
            # The reader validates the entire existing journal before any new append.
            events = tl.read_manual_events(Path(project) / "manual-events.jsonl")
            previous = next((e for e in events if e["event_id"] == ident), None)
            if previous is not None:
                _require(previous == event, 409)
                return event
            _require(len(events) < tl.MAX_MANUAL, 503)
            if corrects:
                _require(any(e["event_id"] == corrects and e["agency_id"] == agency
                             and e["request_id"] == request for e in events), 409)
            data = (aq._canon(event) + "\n").encode()
            _require(info.st_size + len(data) <= tl.MAX_INPUT_BYTES, 503)
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                if not written:
                    raise OSError("manual_write_failed")
                view = view[written:]
            os.fsync(fd)
        finally:
            if fd is not None:
                os.close(fd)
            os.close(root)
    return event


def serve(handler):
    parts = urllib.parse.urlsplit(handler.path)
    if parts.path != "/manual-event":
        return False
    try:
        args = urllib.parse.parse_qs(parts.query, keep_blank_values=True)
        _require(set(args) == {"agency", "request"} and all(len(v) == 1 for v in args.values()))
        agency, request = args["agency"][0], args["request"][0]
        options = handler.server.options
        ident = secrets.token_hex(16)
        signed = ticket(options["project"], options["run_id"], agency, request, handler.server.manual_event_key, ident)
        back = "/agency?" + urllib.parse.urlencode({"agency": agency})
        hidden = dict(csrf_token=handler.server.csrf_token, id=ident, ticket=signed)
        page = '<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Manual event</title></head><body>'
        page += '<h1>Manual event</h1><p>Private; review required. Nothing is sent or published.</p>'
        page += '<p>Request %s; actor %s (local attribution, not authenticated identity).</p>' % (esc(request), esc(handler.server.actor))
        page += '<form method="post" action="/manual-event">'
        page += "".join('<input type="hidden" name="%s" value="%s">' % (k, esc(v)) for k, v in hidden.items())
        page += '<label>Type <select name="type">' + "".join('<option>%s</option>' % esc(k) for k in TYPES) + '</select></label>'
        page += '<label>Date <input type="date" name="date" required></label>'
        page += '<label>Summary <input name="summary" maxlength="2000" required></label>'
        page += '<label>Corrects event id (optional) <input name="corrects" maxlength="128"></label>'
        page += '<button>Append event</button></form><p><a href="%s">Cancel</a></p></body></html>' % esc(back)
        handler._send(200, page)
    except ManualError as error:
        handler._fail(error.status)
    except (OSError, ValueError, aq.ApprovalQueueError):
        handler._fail(503)
    return True


def post(handler, fields):
    try:
        options = handler.server.options
        event = append(options["project"], options["run_id"], handler.server.actor, handler.server.manual_event_key, fields)
        # Refresh only the derived timeline, never the tracker or coordinator ledger.
        tl.write_run_timeline(options["project"], options["run_id"])
        location = "/agency?" + urllib.parse.urlencode({"agency": event["agency_id"]})
        handler._send(303, "", location=location)
    except ManualError as error:
        handler._fail(error.status)
    except (OSError, ValueError, aq.ApprovalQueueError):
        handler._fail(503)
