"""Console intake of an "Approve with phone" tap (A6 PR 2), journal mode only.

The review page of a pending draft offers a passkey tap beside the typed signature form. The server builds the envelope
(``phone_approval.envelope``) over the pinned export's exact approval payload for that row, with a fresh nonce, and the
page shows the text, the payload hash prefix, the expiry (Pacific) and the WebAuthn challenge. A static same-origin script
(``SCRIPT``; the page's CSP then allows ``script-src 'self'`` and nothing inline) asks the phone to sign that challenge and
posts the assertion to ``ROUTE``.

The POST is checked against the pinned export as the typed approve is (CSRF, pending at the same revision, no edit waiting)
plus the envelope's shape, binding (this id, the hash of this export's payload), validity window and an unused nonce, then
journaled as ``phone_approve`` (value: the canonical JSON tap). Nothing here verifies a signature or writes the project:
``records decisions-apply`` verifies the tap with the root-owned ``ssh-keygen`` and trust file at the next records run.
Approving a draft never sends.
"""
import base64
import hashlib
import hmac
import json
import re
import secrets
import time
import urllib.parse
from datetime import datetime
from zoneinfo import ZoneInfo

from .. import approval, phone_approval
from . import approval_queue as aq, console_journal, console_layout, draft_templates
from .outcomes.reading import esc

ROUTE = "/approvals/phone-approve"
SCRIPT = "/phone-approve.js"
PATHS = (ROUTE, SCRIPT)
ACTION = "phone_approve"  # console_apply.PHONE_APPROVE
PACIFIC = ZoneInfo("America/Los_Angeles")
ASSERTION = ("authenticator_data", "client_data", "signature")
FIELDS = frozenset(("csrf_token", "id", "expected_revision", "envelope") + ASSERTION)
_B64URL = re.compile(r"[A-Za-z0-9_-]{1,%d}" % phone_approval.MAX_FIELD)
RECORDED = "Recorded; applied at the next records run."
REFUSED = {  # one plain sentence per refusal; nothing was journaled
    "phone_mismatch": "The phone approval names another draft or another text. Nothing was approved. "
                      "Reload the draft and tap again.",
    "phone_expired": "The phone approval expired (or is not valid yet). Nothing was approved. Reload the draft and tap again.",
    "phone_replay": "This phone approval was already recorded. Nothing new was approved. Reload the draft and tap again.",
}

# Static, same-origin and inline-free. The button stays disabled without a passkey-capable browser, so a page without the
# script posts nothing; the typed signature form is the fallback.
SCRIPT_BODY = r"""(function () {
  "use strict";
  var form = document.getElementById("phone-approve");
  var button = document.getElementById("phone-approve-button");
  var status = document.getElementById("phone-status");
  if (!form || !button || !status) { return; }
  if (!window.PublicKeyCredential || !navigator.credentials) {
    status.textContent = "This browser cannot use a passkey here. Use the typed signature instead.";
    return;
  }
  function decode(text) {
    var raw = atob(text.replace(/-/g, "+").replace(/_/g, "/") + "====".slice(text.length % 4 || 4));
    var out = new Uint8Array(raw.length);
    for (var i = 0; i < raw.length; i++) { out[i] = raw.charCodeAt(i); }
    return out;
  }
  function encode(buffer) {
    var bytes = new Uint8Array(buffer), raw = "";
    for (var i = 0; i < bytes.length; i++) { raw += String.fromCharCode(bytes[i]); }
    return btoa(raw).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }
  button.disabled = false;
  status.textContent = "Ready. Your phone will ask for Face ID or your passcode.";
  form.addEventListener("submit", function (event) {
    event.preventDefault();
    button.disabled = true;
    status.textContent = "Waiting for your phone...";
    navigator.credentials.get({publicKey: {challenge: decode(form.getAttribute("data-challenge")),
                                           rpId: window.location.hostname, userVerification: "required",
                                           timeout: 120000}})
      .then(function (credential) {
        var response = credential.response;
        form.elements.authenticator_data.value = encode(response.authenticatorData);
        form.elements.client_data.value = encode(response.clientDataJSON);
        form.elements.signature.value = encode(response.signature);
        status.textContent = "Sending the approval...";
        form.submit();
      }, function (error) {
        button.disabled = false;
        status.textContent = "No approval was made (" + (error && error.name ? error.name : "cancelled") + "). " +
          "You can tap again or use the typed signature.";
      });
  });
}());
"""
SCRIPT_BYTES = SCRIPT_BODY.encode("utf-8")


def _now():
    return time.time()


def _b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def section(handler, row, payload, choice=None):
    """The "Approve with phone" block for one pending row over ``payload`` (the pinned export's exact approval bytes),
    or "" when this console takes no journaled decision. Rendering it allows the page's one same-origin script."""
    if handler.server.decisions_dir is None or handler.pin is None or payload is None:
        return ""
    issued = int(_now())
    envelope = phone_approval.envelope(row["id"], payload, issued, secrets.token_hex(16), phone_approval.DEFAULT_TTL)
    expires = datetime.fromtimestamp(issued + phone_approval.DEFAULT_TTL, PACIFIC).strftime("%Y-%m-%d %H:%M %Z")
    hidden = [("csrf_token", handler.csrf_token()), ("id", row["id"]), ("expected_revision", str(row["revision"]))]
    hidden += [("choice", choice)] if choice is not None else []
    hidden += [("envelope", envelope)] + [(name, "") for name in ASSERTION]
    handler.allow_script = True
    return ('<section id="phone-approval"><h2>Approve with phone</h2>'
            '<p>Your phone signs the envelope below with its passkey (Face ID or passcode). It names this draft and the '
            'sha256 of the exact approval text shown above, which starts <code id="phone-payload-sha256">%s</code>. '
            'It expires <span id="phone-expires">%s</span>. The tap is recorded here and verified and applied at the next '
            'records run. Approval does not send or publish.</p><pre id="phone-envelope">%s</pre>'
            '<form id="phone-approve" method="post" action="%s" data-challenge="%s">%s'
            '<button type="submit" id="phone-approve-button" disabled>Approve with phone</button>'
            '<p id="phone-status" role="status">Needs a browser with a passkey for this site. '
            'The typed signature still works.</p></form><script src="%s"></script></section>') % (
        esc(hashlib.sha256(payload).hexdigest()[:12]), esc(expires), esc(envelope), esc(ROUTE),
        esc(_b64url(phone_approval.challenge(envelope))),
        "".join('<input type="hidden" name="%s" value="%s">' % (esc(k), esc(v)) for k, v in hidden), esc(SCRIPT))


def serve(handler):
    """GET of the static script; False for any other path."""
    if handler.path != SCRIPT:
        return False
    from .admin_panel import SECURITY
    handler.send_response(200)
    handler.send_header("Content-Type", "text/javascript; charset=utf-8")
    handler.send_header("Content-Length", str(len(SCRIPT_BYTES)))
    for k, v in SECURITY:
        handler.send_header(k, v)
    handler.end_headers()
    handler.wfile.write(SCRIPT_BYTES)
    return True


def journaled_nonces(journal):
    """Every nonce named by a phone_approve entry already in the journal (any project, any outcome)."""
    used = set()
    for entry in console_journal.read(journal):
        if entry["action"] != ACTION:
            continue
        try:
            used.add(phone_approval.tap_envelope(json.loads(entry["value"]))["nonce"])
        except (ValueError, TypeError, approval.ApprovalError):
            continue
    return used


def post(handler):
    """POST ROUTE: journal one phone tap after the checks above; False for any other path."""
    if handler.path != ROUTE:
        return False
    from . import admin_panel as ap
    if handler.server.decisions_dir is None or handler.pin is None:
        handler._fail(404)  # journal mode only: a live console keeps the typed path
        return True
    fields = handler._body()
    if isinstance(fields, int):
        handler._fail(fields)
        return True
    if not hmac.compare_digest(fields.get("csrf_token", "").encode("utf-8"), handler.server.csrf_token.encode("ascii")):
        handler._fail(403)
        return True
    choice = fields.pop("choice", None)
    if (set(fields) != FIELDS or not ap.DRAFT_ID.fullmatch(fields["id"]) or not ap.REVISION.fullmatch(fields["expected_revision"])
            or not all(_B64URL.fullmatch(fields[name]) for name in ASSERTION)):
        handler._fail(400)
        return True
    ident = fields["id"]
    tap = {name: fields[name] for name in ASSERTION}
    tap["envelope"] = fields["envelope"].replace("\r\n", "\n")  # a form post sends line breaks as CRLF
    try:
        envelope = phone_approval.tap_envelope(tap)
    except approval.ApprovalError:
        handler._fail(400)
        return True
    source = handler.options["source"]
    try:
        row = next((r for r in source.queue_rows() if r["id"] == ident), None)
        if row is None:
            handler._fail(404)
            return True
        if row["status"] != "pending" or row.get("approval_unverified"):
            handler._fail(409)
            return True
        if row["revision"] != int(fields["expected_revision"]):
            handler._stale(ident)
            return True
        if (row["kind"] == draft_templates.MESSAGE_LINK) != (choice is not None):
            handler._fail(400)
            return True
        if choice is not None:
            aq.message_link_choice_line(row, choice)  # exactly one exported candidate, else refused
        payload = source.queue_payload(ident, choice)
    except aq.ApprovalQueueError as error:
        handler._fail(400 if error.code == "message_link_choice" else 503)
        return True
    except (OSError, ValueError, KeyError, TypeError):
        handler._fail(503)
        return True
    try:
        if console_journal.pending_edit(handler.server, ident, pin=handler.pin):
            handler._approve_refused("approve_edit_pending")
            return True
        used = journaled_nonces(handler.server.decisions_dir)
    except (OSError, ValueError, KeyError, TypeError, console_journal.JournalError, aq.ApprovalQueueError):
        handler._fail(503, "journal_unavailable")
        return True
    now = _now()
    if envelope["decision"] != ident or envelope["payload_sha256"] != hashlib.sha256(payload).hexdigest():
        code = "phone_mismatch"
    elif not (envelope["issued"] < envelope["expires"] <= envelope["issued"] + phone_approval.MAX_TTL
              and envelope["issued"] - phone_approval.SKEW <= now <= envelope["expires"]):
        code = "phone_expired"
    elif envelope["nonce"] in used:
        code = "phone_replay"
    else:
        code = None
    if code is not None:
        handler._fail(409, code, detail=REFUSED[code])
        return True
    if console_journal._record(handler, ACTION, ident, row["revision"], console_journal._canon(tap), choice):
        back = esc("/review?id=" + urllib.parse.quote(ident))
        handler._send(200, console_layout.shell("Recorded", '<h1>Recorded</h1><p role="status">%s</p><p>Nothing is sent or '
                                                "published. Approval is verified against the trust file when it is applied."
                                                '</p><p><a href="%s">Back to the draft</a></p>' % (esc(RECORDED), back),
                                                active="Approvals"))
    return True
