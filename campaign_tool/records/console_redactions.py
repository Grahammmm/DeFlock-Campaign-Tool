"""Console page for redaction sign-off (DF2007-08, CON-REDACT-1): ``records panel --flock-store STORE``, ``GET /redactions``.

Lists each proposal ``tools/flock_redact.py propose`` made that has no SIGNOFF.json: agency, document hash prefix, the proposal
and the exact text ``flock_redact payload`` prints (the one a reviewer signs). ``POST /redactions/signoff`` (a pasted detached SSH
signature) only appends a ``redaction_signoff`` entry to the console decision journal; ``flock_redact.py signoff-apply
--decisions DIR`` later replays it through the one existing signoff function. Success here means "submitted, awaiting
signoff-apply", never "signed". STORE is read-only to the console: nothing in it is created, locked, repaired or written, no
original is opened and no derivative is built. No signature is verified here.
"""
import hmac
import importlib.util
import json
import os
import stat
import urllib.parse
from pathlib import Path

from . import console_layout
from .outcomes.reading import esc

ACTION, ROUTE = "redaction_signoff", "/redactions/signoff"
FIELDS = {"csrf_token", "slug", "sha", "proposal_sha256", "signature"}
REFUSED = "Refused: a redaction sign-off is taken only from a pinned export with a decision journal and a Flock store. Nothing was saved."
NOTE = ("Your pasted signature is recorded here and checked only when an operator runs `flock_redact.py signoff-apply`; "
        "until then the proposal is unsigned. Eligibility is not publication approval. Nothing is sent or published.")
_TOOL = []


def tool():
    """tools/flock_redact.py, loaded once from the repository (ValueError(redact_tool_unavailable) when absent)."""
    if not _TOOL:
        path = Path(__file__).resolve().parents[2] / "tools" / "flock_redact.py"
        try:
            spec = importlib.util.spec_from_file_location("_console_flock_redact", path)
            module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
            spec.loader.exec_module(module)  # type: ignore[union-attr]
        except (OSError, ImportError, AttributeError, SyntaxError):
            raise ValueError("redact_tool_unavailable") from None
        _TOOL.append(module)
    return _TOOL[0]


def check_store(store, *others):
    """The real STORE path, or ValueError(flock_store_invalid): absolute, link-free, owner-only 0700, outside the repository
    and disjoint from every console write location. Never creates or changes anything."""
    try:
        real = os.path.realpath(store)
        info = os.lstat(real)
        repo = os.path.realpath(Path(__file__).resolve().parents[2])
        if not (isinstance(store, str) and os.path.isabs(store) and real == store and stat.S_ISDIR(info.st_mode)
                and info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o700
                and not (real == repo or real.startswith(repo + os.sep) or repo.startswith(real + os.sep))):
            raise ValueError
        for other in others:
            if other is not None:
                other = os.path.realpath(other)
                if other == real or other.startswith(real + os.sep) or real.startswith(other + os.sep):
                    raise ValueError
    except (ValueError, OSError, TypeError):
        raise ValueError("flock_store_invalid") from None
    return real


def _private_chain(store, folder):
    path = folder
    while True:
        info = os.lstat(path)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError("proposal_unsafe")
        if str(path) == str(store):
            return
        path = path.parent


def _read_capped(path, cap):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise ValueError("proposal_unsafe")
        chunks, size = [], 0
        while size <= cap:
            block = os.read(fd, min(1 << 20, cap + 1 - size))
            if not block:
                break
            chunks.append(block)
            size += len(block)
    finally:
        os.close(fd)
    if size > cap:
        raise ValueError("proposal_oversized")
    return b"".join(chunks)


def _pairs(items):
    out = {}
    for key, value in items:
        if key in out:
            raise ValueError("duplicate_key")
        out[key] = value
    return out


def read_proposal(store, slug, sha):
    """-> row dict of one unsigned, valid proposal; ValueError(code) when it is signed, unsafe or malformed. Read-only."""
    from . import admin_panel
    t = tool()
    folder = t.record.record_dir(Path(store), slug, sha)
    _private_chain(Path(store), folder)
    if os.path.lexists(folder / "SIGNOFF.json"):  # any entry, a dangling link included, closes the proposal
        raise ValueError("signed")
    raw = _read_capped(folder / "PROPOSAL.json", admin_panel.MAX_JSON)
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs)
        rule = json.loads((t.REPO / "schemas" / "redaction-proposal-v1.json").read_text())
        if (not t.record._valid(value, rule) or value["source_sha256"] != sha or value["document_id"] != sha
                or value["redaction_required"] != bool(value["proposed"]) or t.encode(value) != raw
                or t.record.privacy.categories(value["not_detected_note"])
                or any(i["category"] not in {n for n, _ in t.record.privacy.PATTERNS} for i in value["proposed"])):
            raise ValueError
        text = t.payload(raw, sha).decode("utf-8")
    except (ValueError, UnicodeError, KeyError, TypeError, OSError):
        raise ValueError("proposal_invalid") from None
    return {"agency": slug, "sha": sha, "prefix": sha[:12], "required": value["redaction_required"], "proposed": value["proposed"],
            "note": value["not_detected_note"], "payload": text, "digest": t.digest(raw)}


def load(store):
    """-> {state: ok|unavailable, code, rows (unsigned proposals), refused (unsafe), overflow}. Read-only; never a silent empty list."""
    from . import admin_panel
    try:
        tool()
        agencies = Path(store) / "agencies"
        names = sorted(e.name for e in os.scandir(agencies) if e.is_dir(follow_symlinks=False)) if os.path.lexists(agencies) else []
        rows, refused, overflow = [], 0, False
        for slug in names:
            folder = agencies / slug / "redaction"
            if not os.path.lexists(folder):
                continue
            for entry in sorted(os.scandir(folder), key=lambda e: e.name):
                if not (tool().HEX64.fullmatch(entry.name) and entry.is_dir(follow_symlinks=False)):
                    refused += 1
                    continue
                try:
                    row = read_proposal(store, slug, entry.name)
                except ValueError as error:
                    if str(error) != "signed":
                        refused += 1
                    continue
                if len(rows) >= admin_panel.ROW_LIMIT:
                    overflow = True
                else:
                    rows.append(row)
    except (OSError, ValueError):
        return {"state": "unavailable", "code": "flock_store_unavailable"}
    return {"state": "ok", "rows": rows, "refused": refused, "overflow": overflow}


def pending(handler, store):
    """({(target_id, proposal digest)} journaled for this store and not yet receipted, {entry_id: outcome});
    ValueError(redaction_pending_unreadable) when the journal or receipts cannot be read."""
    from . import console_journal as cj
    t = tool()
    try:
        sid, done = t.store_id(store), t.read_receipts(handler.server.decisions_dir)
        held, last = set(), {}
        for e in cj.read(handler.server.decisions_dir):
            if e.get("action") != ACTION or e.get("source_project_id") != handler.pin.source_project_id:
                continue
            try:
                entry_store, _ = t.entry_signature(e)
            except ValueError:
                continue
            if entry_store != sid:
                continue
            if e["entry_id"] in done:
                last[(e["target_id"], e["revision_seen"])] = done[e["entry_id"]]
            else:
                held.add((e["target_id"], e["revision_seen"]))
        return held, last
    except (cj.JournalError, OSError, ValueError, KeyError):
        raise ValueError("redaction_pending_unreadable") from None


def _forms(token, row):
    hidden = "".join('<input type="hidden" name="%s" value="%s">' % (k, esc(v)) for k, v in
                     (("csrf_token", token), ("slug", row["agency"]), ("sha", row["sha"]), ("proposal_sha256", row["digest"])))
    key = "%s-%s" % (row["agency"], row["prefix"])
    return ('<form method="post" action="%s">%s<label for="sig-%s">Pasted signature of the text above</label>'
            '<textarea id="sig-%s" name="signature" rows="8" required></textarea><button type="submit">Submit sign-off</button></form>'
            % (ROUTE, hidden, esc(key), esc(key)))


def _item(row, token, held, last):
    key = (row["agency"] + ":" + row["sha"], row["digest"])
    spans = "".join("<li>%s: line %d, byte offset %d, %d bytes</li>" % (
        esc(p["category"]), p["locator"]["line"], p["locator"]["offset"], p["locator"]["length"]) for p in row["proposed"])
    out = ['<section class="draft"><h2>%s: document %s</h2><dl><dt>Agency</dt><dd>%s</dd><dt>Document</dt><dd>%s</dd>'
           "<dt>Redaction required</dt><dd>%s</dd></dl>" % (
               esc(row["agency"]), esc(row["prefix"]), esc(row["agency"]), esc(row["sha"]), "yes" if row["required"] else "no")]
    out.append("<h3>Proposed redactions</h3>" + ("<ul>%s</ul>" % spans if spans else "<p>None proposed.</p>"))
    out.append("<p>%s</p>" % esc(row["note"]))
    out.append("<h3>Exact text to sign (flock-redaction)</h3><pre>%s</pre>" % esc(row["payload"]))
    if key in held:
        out.append('<p role="status">Sign-off submitted; awaiting signoff-apply. The proposal is still unsigned.</p>')
    else:
        if key in last:
            out.append('<p role="status">The last submitted sign-off was not applied (%s).</p>' % esc(last[key]))
        if token is not None:
            out.append(_forms(token, row))
    return "".join(out) + "</section>"


def render_body(doc, token, held=frozenset(), last=None):
    out = ["<h1>Redaction sign-off</h1>"]
    if doc is None:
        out.append('<p class="diag" role="status">Redaction sign-off needs configuration: start the console with --flock-store STORE.</p>')
        return "".join(out)
    if doc["state"] != "ok":
        out.append(console_layout.sources_banner([], ["Redaction sign-off: unavailable (%s). Nothing is listed; this is not an empty list." % doc["code"]]))
        return "".join(out)
    out.append('<p role="status">%d proposal%s waiting for a sign-off.</p>' % (len(doc["rows"]), "" if len(doc["rows"]) == 1 else "s"))
    out.append("<p>%s</p>" % esc(NOTE))
    if token is None:
        out.append('<p class="diag" role="status">Read-only: a sign-off can be submitted only from the exported console with a decision journal.</p>')
    if doc["refused"]:
        out.append('<p class="diag" role="status">%d proposal%s could not be read safely and %s no form.</p>' % (
            doc["refused"], "" if doc["refused"] == 1 else "s", "has" if doc["refused"] == 1 else "have"))
    if doc["overflow"]:
        out.append('<p class="diag" role="status">More proposals are waiting than are listed here; sign off these first.</p>')
    if not doc["rows"]:
        out.append("<p>No redaction proposal is waiting for a sign-off.</p>")
    out += [_item(r, token, held, last or {}) for r in doc["rows"]]
    return "".join(out)


def render_page(doc, token, held=frozenset(), last=None):
    return console_layout.shell("Redaction sign-off", render_body(doc, token, held, last), active="Redaction sign-off")


def serve(handler):
    path = urllib.parse.urlsplit(handler.path)
    if path.path != "/redactions":
        return False
    if path.query:
        handler._fail(400)
        return True
    store = getattr(handler.server, "flock_store", None)
    if store is None:
        handler._send(200, render_page(None, None))
        return True
    form_ok = handler.pin is not None and handler.server.decisions_dir is not None
    doc, held, last, token = load(store), frozenset(), {}, handler.csrf_token() if form_ok else None
    if form_ok:
        try:
            held, last = pending(handler, store)
        except ValueError:
            doc, token = {"state": "unavailable", "code": "redaction_pending_unreadable"}, None
    handler._send(200, render_page(doc, token, held, last))
    return True


def post(handler):
    """POST /redactions/signoff: journal one pasted signature for the proposal the person saw. True when the path is ours."""
    if handler.path != ROUTE:
        return False
    from . import console_journal, console_origin
    if not console_origin.request_ok(handler, post=True):
        handler._fail(403)
        return True
    store = getattr(handler.server, "flock_store", None)
    if handler.pin is None or handler.server.decisions_dir is None or store is None:
        handler._fail(503, REFUSED)
        return True
    fields = handler._body()
    if isinstance(fields, int):
        handler._fail(fields)
        return True
    if set(fields) != FIELDS:
        handler._fail(400)
        return True
    if not hmac.compare_digest(fields["csrf_token"].encode("utf-8"), handler.server.csrf_token.encode("ascii")):
        handler._fail(403)
        return True
    try:
        t = tool()
        slug, sha, digest = fields["slug"], fields["sha"], fields["proposal_sha256"]
        signature = t.signature_text(fields["signature"].encode("utf-8"))
        if not (t.HEX64.fullmatch(sha) and t.HEX64.fullmatch(digest)):
            raise ValueError("bad")
        t.record.record_dir(Path(store), slug, sha)
    except ValueError as error:
        handler._fail(503 if str(error) == "redact_tool_unavailable" else 400)
        return True
    try:  # the proposal is read again now: the page the person saw must still be current and unsigned
        row = read_proposal(store, slug, sha)
    except FileNotFoundError:
        handler._fail(409)
        return True
    except (OSError, ValueError) as error:
        handler._fail(409 if str(error) == "signed" else 503)
        return True
    if row["digest"] != digest:
        handler._fail(409)
        return True
    try:
        held, _ = pending(handler, store)
    except ValueError:
        handler._fail(503)
        return True
    if (slug + ":" + sha, digest) in held:
        handler._fail(409)
        return True
    value = console_journal._canon({"store_id": t.store_id(store), "signature": signature})
    if console_journal.redaction_signoff(handler, slug + ":" + sha, digest, value):
        handler._send(303, "", location="/redactions")
    return True
