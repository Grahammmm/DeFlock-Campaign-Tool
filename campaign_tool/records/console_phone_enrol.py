"""Console /phone/enrol (A6 PR 3): G's phone creates a passkey; the console keeps only its PUBLIC record.

Shown only with ``records panel --phone-enrol-dir DIR`` (journal mode; DIR an absolute owner-only 0700 directory outside
the export, the decisions journal and every other console location), behind the same Host/Origin and Access checks as
every page. GET serves a form carrying a fresh 32-byte server challenge, bound to this console by an HMAC tag and the
CSRF token, and a static same-origin script that calls ``navigator.credentials.create`` (ES256 only, user verification
required, attestation none). POST checks the token, the tag and the full registration (``phone_enrol.parse``), then
stores ONE 0600 record in DIR and shows the key fingerprint. Nothing is trusted, approved or journaled here: the trust
line is made by ``records phone-key line`` and added to the trust file at the attended sitting.
"""
import hmac
import hashlib
import secrets
import time
import urllib.parse

from . import console_layout, phone_enrol
from .outcomes.reading import esc

PATH, SCRIPT_PATH = "/phone/enrol", "/phone/enrol.js"
FIELDS = frozenset({"csrf_token", "challenge", "issued", "tag", "credential_id", "attestation_object", "client_data"})
CHALLENGE_TTL = 600  # seconds a page's challenge stays usable
PAGE_CSP = ("default-src 'none'; script-src 'self'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; "
            "frame-ancestors 'none'")
SCRIPT_HEADERS = (("Content-Type", "text/javascript; charset=utf-8"), ("Cache-Control", "no-store"),
                  ("X-Content-Type-Options", "nosniff"), ("Referrer-Policy", "same-origin"),
                  ("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'"))
TITLE = "Enrol phone for approvals"
SCRIPT = r"""(function () {
  "use strict";
  var form = document.getElementById("phone-enrol");
  var button = document.getElementById("phone-enrol-create");
  var status = document.getElementById("phone-enrol-status");
  if (!form || !button || !status) { return; }
  function bytes(text) {
    var s = text.replace(/-/g, "+").replace(/_/g, "/");
    while (s.length % 4) { s += "="; }
    var raw = atob(s), out = new Uint8Array(raw.length);
    for (var i = 0; i < raw.length; i++) { out[i] = raw.charCodeAt(i); }
    return out;
  }
  function text(buffer) {
    var b = new Uint8Array(buffer), s = "";
    for (var i = 0; i < b.length; i++) { s += String.fromCharCode(b[i]); }
    return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }
  if (!window.PublicKeyCredential || !navigator.credentials || !navigator.credentials.create) {
    status.textContent = "This browser cannot create a passkey. Nothing was recorded.";
    button.disabled = true;
    return;
  }
  button.addEventListener("click", function () {
    button.disabled = true;
    status.textContent = "Waiting for the passkey...";
    navigator.credentials.create({publicKey: {
      rp: {id: form.getAttribute("data-rp-id"), name: "DeFlock console"},
      user: {id: bytes(form.getAttribute("data-user-id")), name: "deflock-approver", displayName: "DeFlock approver"},
      challenge: bytes(form.elements.challenge.value),
      pubKeyCredParams: [{type: "public-key", alg: -7}],
      authenticatorSelection: {residentKey: "preferred", userVerification: "required"},
      attestation: "none",
      timeout: 120000
    }}).then(function (credential) {
      form.elements.credential_id.value = text(credential.rawId);
      form.elements.attestation_object.value = text(credential.response.attestationObject);
      form.elements.client_data.value = text(credential.response.clientDataJSON);
      status.textContent = "Recording the public key...";
      form.submit();
    }, function () {
      status.textContent = "No passkey was created. Nothing was recorded.";
      button.disabled = false;
    });
  });
}());
"""


def _rp_id(handler):
    """The WebAuthn relying-party id: the request's host, which must be the console's public HTTPS host (an IP or
    loopback address cannot hold a passkey). None otherwise."""
    public = getattr(handler.server, "public_pair", None)
    host = handler.headers.get_all("Host") or []
    return public[0] if public is not None and host == [public[0]] else None


def _tag(handler, rp_id, challenge, issued):
    message = ("%s\n%s\n%s\n%d" % (PATH, rp_id, challenge, issued)).encode("utf-8")
    return hmac.new(handler.server.phone_enrol_key, message, hashlib.sha256).hexdigest()


def _page(handler, rp_id):
    if rp_id is None:
        return console_layout.shell(TITLE, "<h1>%s</h1><p>Enrolment works only on the console's public address, "
                                    "opened on the phone. Nothing was recorded.</p>" % esc(TITLE))
    challenge, issued = phone_enrol.b64url(secrets.token_bytes(phone_enrol.CHALLENGE_BYTES)), int(time.time())
    hidden = {"csrf_token": handler.csrf_token(), "challenge": challenge, "issued": str(issued),
              "tag": _tag(handler, rp_id, challenge, issued), "credential_id": "", "attestation_object": "",
              "client_data": ""}
    return console_layout.shell(TITLE, (
        "<h1>%s</h1><p>This creates a passkey on this phone (Face ID or passcode) for this console. Only its "
        "<strong>public</strong> key is recorded here; the private key never leaves the phone.</p>"
        "<p>Recording it trusts nothing: the operator turns it into a trust line and adds it at the attended "
        "sitting, after you read the fingerprint aloud. Nothing is approved or sent from this page.</p>"
        '<form id="phone-enrol" method="post" action="%s" data-rp-id="%s" data-user-id="%s">%s'
        '<button type="button" id="phone-enrol-create">Create passkey</button>'
        '<p id="phone-enrol-status" role="status"></p></form><script src="%s" defer></script>') % (
        esc(TITLE), PATH, esc(rp_id), phone_enrol.b64url(secrets.token_bytes(16)),
        "".join('<input type="hidden" name="%s" value="%s">' % (esc(k), esc(v)) for k, v in hidden.items()),
        SCRIPT_PATH))


def serve(handler):
    path = handler.path.partition("?")[0]
    if path not in (PATH, SCRIPT_PATH) or getattr(handler.server, "phone_enrol_dir", None) is None:
        return False
    if path == SCRIPT_PATH:
        data = SCRIPT.encode("utf-8")
        handler.send_response(200)
        for key, value in SCRIPT_HEADERS + (("Content-Length", str(len(data))),):
            handler.send_header(key, value)
        handler.end_headers()
        handler.wfile.write(data)
        return True
    handler._send(200, _page(handler, _rp_id(handler)), csp=PAGE_CSP)
    return True


def post(handler):
    """POST /phone/enrol: store one public record or refuse with nothing stored."""
    directory = getattr(handler.server, "phone_enrol_dir", None)
    if handler.path != PATH or directory is None:
        return False
    fields = handler._body()
    if isinstance(fields, int):
        return handler._fail(fields) or True
    if set(fields) != FIELDS:
        return handler._fail(400) or True
    if not hmac.compare_digest(fields["csrf_token"].encode("utf-8"), handler.server.csrf_token.encode("ascii")):
        return handler._fail(403) or True
    rp_id = _rp_id(handler)
    if rp_id is None:
        return handler._fail(403, detail="Enrolment works only on the console's public address. "
                             "Nothing was recorded.") or True
    issued, now = fields["issued"], int(time.time())
    if not (issued.isascii() and issued.isdigit() and len(issued) <= 12
            and now - CHALLENGE_TTL <= int(issued) <= now + 5
            and hmac.compare_digest(fields["tag"].encode("utf-8"),
                                    _tag(handler, rp_id, fields["challenge"], int(issued)).encode("ascii"))):
        return handler._fail(403, detail="The page expired. Nothing was recorded. Reload it and try again.") or True
    try:
        values = [phone_enrol.unb64url(fields[k]) for k in ("challenge", "credential_id", "attestation_object",
                                                             "client_data")]
        name, parsed = phone_enrol.store(directory, rp_id, *values)
    except phone_enrol.EnrolError as error:
        return handler._fail(400, detail="The passkey was not accepted (%s). Nothing was recorded." % error) or True
    except phone_enrol.RecordLeftBehind:  # before OSError: it is one. Never claim "nothing was recorded" here
        return handler._fail(503, detail="The passkey could not be saved cleanly and a partial record may remain in "
                             "the enrolment folder. Do not rely on it. Ask the operator to check the folder.") or True
    except (OSError, ValueError):
        return handler._fail(503, detail="The enrolment folder is unavailable or full. Nothing was recorded. "
                             "Ask the operator.") or True
    handler._send(200, console_layout.shell(TITLE, (
        "<h1>Public key recorded</h1><p>Fingerprint: <code id=\"phone-enrol-fingerprint\">%s</code></p>"
        "<p>Record: <code>%s</code></p><p>Read the fingerprint aloud to the operator at the attended sitting. "
        "Nothing is trusted, approved or sent until that line is added.</p><p><a href=\"/\">Back to Home</a></p>")
        % (esc(parsed["fingerprint"]), esc(urllib.parse.quote(name)))))
    return True
