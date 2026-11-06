"""Private local records dashboard with draft decisions. Nothing here publishes or runs anything, and it sends
only through the opt-in, switched-off-by-default Approve and send path in ``mail_send`` (``--send-config``).

Read model over the canonical ledger, an explicit run report, one run's received items, an optional kit
root and the approval queue; see docs/RECORDS-ADMIN-PANEL.md. Stdlib only, loopback only.
"""
import argparse
import hmac
import http.server
import json
import os
import re
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import approval_queue, console_board, console_documents, console_home, console_manual_events, console_oversight, console_packets, draft_templates, mail_drafts, mail_send, run_report, schedule
from .ledger import store
from .outcomes.reading import BANNER, esc

RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
DRAFT_ID = re.compile(r"[0-9a-f]{64}")
MAX_JSON = 8 * 1024 * 1024
MAX_BODY = 256 * 1024
ROW_LIMIT = 500
STAGE_LIMIT = 20000
SOCKET_TIMEOUT = 10
DEFAULT_TIMERS = ("records-run.timer",)
RETURNS = ("/", "/dashboard", "/next-steps")  # the only places a decision may send the browser back to
NEXT_SECTIONS = (("social", "Draft social media post", "next-social-post", "approved-to-publish"),
                 ("site", "Proposed website update (diff)", "next-site-update", "approved-to-publish"),
                 ("followup", "Follow-up to the agency (probe)", "next-followup-probe", "approved-to-send"))
NEXT_CAVEAT = "Approving records your decision on this draft only. Nothing is posted, published, edited or sent."
SECTIONS = (("mail", "Inbox: mail received"), ("documents", "Documents"), ("stages", "Stage status and run health"),
            ("findings", "Findings"), ("deadlines", "Deadlines"), ("drafts", "Drafts"))
# Upstream codes are content-free; only these are ever shown.
QUEUE_CODES = frozenset({"queue_invalid", "queue_io", "unknown_id", "invalid_transition", "approval_required",
                         "invalid_draft", "invalid_actor", "source_invalid", "source_conflict"})
ACTION_STATUS = {"unknown_id": 404, "invalid_transition": 409, "approval_required": 403,
                 "invalid_draft": 400, "invalid_actor": 400}
REASONS = {400: "Bad request", 403: "Refused", 404: "Not found", 405: "Method not allowed", 408: "Request timed out",
           409: "Conflict: the draft is not in a state that allows this", 411: "Length required",
           413: "Request too large", 415: "Unsupported media type", 503: "A source is unavailable"}
STYLE = """
*{box-sizing:border-box}body{margin:0;background:#fffdf6;color:#173e3a;font:16px/1.5 system-ui,sans-serif}
header,main,footer{max-width:1200px;margin:auto;padding:16px}h1{line-height:1.2}h2{margin-top:32px}
a{display:inline-block;min-height:44px;padding:10px;color:#173e3a}nav{display:flex;flex-wrap:wrap;gap:8px}
:focus-visible{outline:3px solid #804000;outline-offset:3px}.notice{padding:12px;border:2px solid #173e3a}
.diag{padding:8px;border-left:6px solid #804000;background:#fff0d6}table{width:100%;border-collapse:collapse;margin:8px 0}
th,td{padding:6px;text-align:left;vertical-align:top;border:1px solid #729186;overflow-wrap:anywhere;white-space:pre-wrap}
code,pre,q{overflow-wrap:anywhere;white-space:pre-wrap}textarea,input[type=text]{width:100%;font:inherit}
button{min-height:44px;padding:8px 16px;font:inherit}.draft{border:2px solid #729186;padding:12px;margin:12px 0}
.overdue{color:#8a1c1c;font-weight:bold}
"""


class PanelError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


# ---- bounded private readers ---------------------------------------------------------------------

def _no_symlinks(path):
    path = Path(os.path.abspath(path))
    try:
        if any(p.is_symlink() for p in (path, *path.parents)):
            raise PanelError("unsafe_path")
    except OSError:
        raise PanelError("unreadable") from None
    return path


def _unique(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate")
        out[key] = value
    return out


def _nonfinite(_):
    raise ValueError("nonfinite")


def _read_json(path):
    """Returns (document, sha-free diagnostic code). Never follows symlinks or opens FIFOs blocking."""
    path = _no_symlinks(path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        raise PanelError("missing") from None
    except OSError:
        raise PanelError("unsafe_path") from None
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise PanelError("unsafe_path")
        if info.st_size > MAX_JSON:
            raise PanelError("oversize")
        raw = stream.read(MAX_JSON + 1)
    if len(raw) > MAX_JSON:
        raise PanelError("oversize")
    try:
        doc = json.loads(raw, object_pairs_hook=_unique, parse_constant=_nonfinite)
        json.dumps(doc, ensure_ascii=False).encode("utf-8")  # lone surrogates are refused
    except (ValueError, UnicodeError, RecursionError):
        raise PanelError("invalid_json") from None
    return doc


def _text(value, limit=240):
    if value is None:
        return ""
    value = str(value)
    value = "".join(c if c in "\n\t" or ord(c) >= 32 and ord(c) != 127 else "?" for c in value)
    return value if len(value) <= limit else value[:limit] + "…"


def _diag(code):
    return {"state": "unavailable", "code": code}


# ---- sources ---------------------------------------------------------------------------------------

def _load_ledger(path):
    try:
        path = _no_symlinks(path)
        if not path.exists():
            return _diag("ledger_missing")
        with store.ledger(path, readonly=True) as con:
            con.execute("BEGIN")
            mail = [dict(r) for r in con.execute(
                "SELECT m.id, m.received_at, m.eml_sha256, (SELECT COUNT(*) FROM occurrences a "
                "JOIN occurrences p ON a.parent_occurrence_id=p.id WHERE a.kind='attachment' AND p.kind='mail' "
                "AND p.original_sha256=m.eml_sha256) AS attachments FROM mail_messages m "
                "ORDER BY m.received_at DESC, m.id LIMIT ?", (ROW_LIMIT,))]
            mail_total = con.execute("SELECT COUNT(*) FROM mail_messages").fetchone()[0]
            docs, doc_total = [], con.execute("SELECT COUNT(*) FROM originals").fetchone()[0]
            for row in con.execute("SELECT sha256, bytes, mime_detected, first_seen_at FROM originals "
                                   "ORDER BY first_seen_at DESC, sha256 LIMIT ?", (ROW_LIMIT,)).fetchall():
                try:
                    fields = store.effective_original_fields(con, row)
                except ValueError:
                    fields = {k: row[k] for k in row.keys()}
                docs.append({k: fields[k] for k in ("sha256", "bytes", "mime_detected", "first_seen_at")})
            stage_rows = [dict(r) for r in con.execute(
                "SELECT original_sha256, stage, status, reason, updated_at, run_id FROM stage_state "
                "ORDER BY original_sha256, stage LIMIT ?", (STAGE_LIMIT,))]
            matrix = [dict(r) for r in con.execute(
                "SELECT stage, status, COUNT(*) AS n FROM stage_state GROUP BY stage, status ORDER BY stage, status")]
            causes = [dict(r) for r in con.execute(
                "SELECT stage, COALESCE(reason,'(no reason recorded)') AS reason, COUNT(*) AS n FROM stage_state "
                "WHERE status='blocked' GROUP BY stage, reason ORDER BY n DESC, stage, reason LIMIT 100")]
            pages = [dict(r) for r in con.execute(
                "SELECT status, needs_visual_review AS review, COUNT(*) AS n FROM page_state "
                "GROUP BY status, needs_visual_review ORDER BY status, review")]
            con.execute("COMMIT")
        return {"state": "ok", "mail": mail, "mail_total": mail_total, "documents": docs, "documents_total": doc_total,
                "stage_rows": stage_rows, "matrix": matrix, "causes": causes, "pages": pages}
    except PanelError as error:
        return _diag(error.code)
    except (ValueError, OSError):
        return _diag("ledger_unreadable")
    except Exception as error:  # sqlite3 busy/locked/corrupt: a category, never the message
        return _diag("ledger_busy" if "lock" in str(error).lower() else "ledger_unreadable")


def _load_report(path, run_id):
    if path is None:
        return {"state": "not_configured"}
    try:
        doc = _read_json(path)
        run_report.validate_run_report(doc)
        if doc.get("run_id") != run_id:
            return _diag("report_run_mismatch")
        return {"state": "ok", "report": doc}
    except PanelError as error:
        return _diag("report_" + error.code)
    except (ValueError, TypeError, KeyError, OSError):
        return _diag("report_invalid")


def _load_received(project, run_id):
    path = project / "run-outputs" / run_id / "outcomes" / "received-items.json"
    try:
        doc = _read_json(path)
        if type(doc) is not dict or doc.get("schema") != "records-received-item-v1" \
                or not isinstance(doc.get("items"), list) or any(type(i) is not dict for i in doc["items"]):
            return _diag("received_schema_invalid")
        if doc.get("run_id") != run_id:
            return _diag("received_run_mismatch")
        return {"state": "ok", "items": doc["items"], "diagnostics": [_text(d, 80) for d in doc.get("diagnostics") or []
                                                                      if isinstance(d, str)]}
    except PanelError as error:
        return _diag("received_" + error.code)


def _load_kit(kit_root, day):
    if kit_root is None:
        return {"state": "not_configured"}
    try:
        from .. import kit_track
        return {"state": "ok", "due": kit_track.due(kit_root, as_of=day.isoformat())}
    except (ValueError, OSError, KeyError, TypeError):
        return _diag("kit_unreadable")


def _load_queue(project):
    try:
        rows = approval_queue.load_queue(project)
    except approval_queue.ApprovalQueueError as error:
        return _diag(error.code if error.code in QUEUE_CODES else "queue_invalid")
    except OSError:
        return _diag("queue_io")
    drafts = []
    for row in rows:
        row = dict(row)
        row["payload"] = None
        if row["status"] == "pending":
            try:
                row["payload"] = approval_queue.approval_payload(project, row["id"]).decode("utf-8", "replace")
            except (approval_queue.ApprovalQueueError, OSError):
                row["payload"] = None
        drafts.append(row)
    return {"state": "ok", "drafts": drafts}


def _load_mail_drafts(project):
    try:
        return {"state": "ok", "rows": mail_drafts.placements(project)}
    except mail_drafts.MailDraftError as error:
        return _diag(error.code)
    except OSError:
        return _diag("journal_io")


def _timer_state(unit):
    try:
        done = subprocess.run(["systemctl", "--user", "is-active", unit], capture_output=True, text=True, timeout=3)
        out = done.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return out if out in ("active", "inactive", "failed", "activating", "deactivating") else "unknown"


def _load_system(project, timers):
    out = {"disk": None, "timers": [(unit, _timer_state(unit)) for unit in timers]}
    try:
        usage = shutil.disk_usage(project)
        out["disk"] = {"total": usage.total, "free": usage.free}
    except OSError:
        pass
    return out


def _load_health(processing, now):
    try:
        report = schedule.health(processing, now=now)
        keys = ("status", "reason", "last_run", "next_slot", "missed_in_lookback", "open_alerts", "blocked_stages")
        return {"state": "ok", **{k: report.get(k) for k in keys}}
    except Exception:  # health parses the ledger itself; any failure is a category only
        return _diag("health_unreadable")


def parse_as_of(value):
    """Aware UTC instant and day for a fixed deadline/health clock; None means capture now once."""
    if value is None:
        now = datetime.now(timezone.utc)
        return now, now.date()
    try:
        day = date.fromisoformat(value)
    except ValueError:
        raise PanelError("as_of_invalid") from None
    return datetime(day.year, day.month, day.day, tzinfo=timezone.utc), day


def check_run_id(run_id):
    if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id) or run_id in (".", ".."):
        raise PanelError("run_id_invalid")
    return run_id


def load_dashboard(project, run_id, *, run_report_path=None, kit_root=None, as_of=None, timers=DEFAULT_TIMERS,
                   system=True, send_config=None):
    """Read-only. Every source fails independently; nothing is created or written."""
    check_run_id(run_id)
    now, day = parse_as_of(as_of)
    project = Path(os.path.abspath(project))
    processing = project / "processing-v1"
    run_dir = project / "run-outputs" / run_id
    try:
        present = run_dir.is_dir()
    except OSError:
        present = False
    model = {"run_id": run_id, "captured_at": now.isoformat(), "as_of": day.isoformat(),
             "run_outputs_present": present,
             "ledger": _load_ledger(processing / "ledger.sqlite"),
             "report": _load_report(run_report_path, run_id), "received": _load_received(project, run_id),
             "kit": _load_kit(kit_root, day), "queue": _load_queue(project),
             "mail_drafts": _load_mail_drafts(project),
             "send": mail_send.switch_state(project, send_config, now=now) if send_config else None,
             "mail_send": mail_send.results(project) if send_config else None,
             "health": _load_health(processing, now),
             "system": _load_system(project, timers) if system else None}
    return model


# ---- rendering ---------------------------------------------------------------------------------------

def _table(headers, rows, caption):
    if not rows:
        return "<p>None reported in this source.</p>"
    out = ["<table><caption>" + esc(caption) + "</caption><thead><tr>"]
    out += ['<th scope="col">' + esc(h) + "</th>" for h in headers]
    out.append("</tr></thead><tbody>")
    for row in rows:
        out.append("<tr>" + "".join("<td>" + (c if isinstance(c, _Raw) else esc(_text(c))) + "</td>" for c in row)
                   + "</tr>")
    out.append("</tbody></table>")
    return "".join(out)


class _Raw(str):
    """Markup already escaped by this module."""


def _source_note(name, source):
    state = source.get("state")
    if state == "ok":
        return ""
    if state == "not_configured":
        return '<p class="diag" role="status">' + esc(name) + ": not configured.</p>"
    return '<p class="diag" role="status">' + esc(name) + ": unavailable (" + esc(source.get("code")) + ").</p>"


def _short(sha):
    return _Raw("<code>" + esc(str(sha or "")[:12]) + "</code>")


def _mail_section(model):
    led = model["ledger"]
    out = [_source_note("Ledger", led)]
    if led["state"] != "ok":
        return "".join(out)
    stages = {}
    for r in led["stage_rows"]:
        stages.setdefault(r["original_sha256"], []).append(r["stage"] + ":" + r["status"])
    out.append("<p>Messages in ledger: %d (showing %d). Attachment counts come from ledger occurrences only; "
               "a message count is not a received-item count.</p>" % (led["mail_total"], len(led["mail"])))
    out.append(_table(("Message", "Received", "Original hash", "Attachments", "Canonical stages of the message file"),
                      [(m["id"], m["received_at"], _short(m["eml_sha256"]), m["attachments"],
                        ", ".join(stages.get(m["eml_sha256"], [])) or "no stage rows") for m in led["mail"]],
                      "Mail messages"))
    return "".join(out)


def _documents_section(model):
    led = model["ledger"]
    out = [_source_note("Ledger", led)]
    if led["state"] != "ok":
        return "".join(out)
    by_doc = {}
    for r in led["stage_rows"]:
        by_doc.setdefault(r["original_sha256"], []).append(r)
    out.append("<p>Documents in ledger: %d (showing %d). Pages: %s.</p>" % (
        led["documents_total"], len(led["documents"]),
        esc(", ".join("%s%s=%d" % (p["status"], " needs-visual-review" if p["review"] else "", p["n"])
                      for p in led["pages"]) or "no page state")))
    out.append(_table(("Hash", "MIME", "Bytes", "First seen", "Stage state (reason)"),
                      [(_short(d["sha256"]), d["mime_detected"], d["bytes"], d["first_seen_at"],
                        "\n".join("%s: %s%s" % (r["stage"], r["status"], " (" + _text(r["reason"], 120) + ")"
                                                if r["reason"] else "") for r in by_doc.get(d["sha256"], []))
                        or "no stage rows") for d in led["documents"]], "Documents"))
    out.append("<h3>Errors by cause (blocked stages, all documents)</h3>")
    out.append(_table(("Stage", "Cause", "Documents"), [(c["stage"], c["reason"], c["n"]) for c in led["causes"]],
                      "Blocked stages grouped by recorded reason"))
    return "".join(out)


def _stages_section(model):
    out = []
    rep = model["report"]
    out.append("<h3>Orchestration run report (the eight run stages)</h3>" + _source_note("Run report", rep))
    if rep["state"] == "ok":
        r = rep["report"]
        out.append("<p>Run %s: overall %s, %s to %s. Metadata only; review required, not semantic acceptance.</p>" % (
            esc(r["run_id"]), esc(r["overall_status"]), esc(r["started_at"]), esc(r["finished_at"])))
        out.append(_table(("Stage", "Status", "Error code", "Input", "Output", "Failed", "Skipped"),
                          [(s["name"], s["status"], s["error_code"], *(s["counts"].get(k) for k in
                                                                      ("input", "output", "failed", "skipped")))
                           for s in r["stages"]], "Run report stages"))
        out.append(_table(("Code", "Stage", "Count"), [(n.get("code"), n.get("stage"), n.get("count"))
                                                       for n in r.get("needs_g", [])], "Needs G (unchanged)"))
    led = model["ledger"]
    out.append("<h3>Canonical per-document stages (the seven evidence stages)</h3>" + _source_note("Ledger", led))
    if led["state"] == "ok":
        out.append(_table(("Stage", "Status", "Documents"), [(m["stage"], m["status"], m["n"]) for m in led["matrix"]],
                          "Stage slots observed; missing, held or unknown is never treated as passed"))
        out.append("<p>End-to-end completion is not established by these counts.</p>")
    h = model["health"]
    out.append("<h3>Run health</h3>" + _source_note("Health", h))
    if h["state"] == "ok":
        last = h.get("last_run") or {}
        out.append("<p>Status: %s%s. Last run: %s %s (%s, ended %s). Next slot: %s. Missed in lookback: %s.</p>" % (
            esc(h["status"]), esc(" (" + h["reason"] + ")" if h.get("reason") else ""), esc(last.get("run_id", "none")),
            esc(last.get("status", "")), esc(last.get("started_at", "")), esc(last.get("ended_at", "")),
            esc(h.get("next_slot")), esc(h.get("missed_in_lookback"))))
    sysm = model["system"]
    if sysm is None:
        out.append("<p>System checks skipped.</p>")
    else:
        out.append("<p>Timers: %s. Disk: %s.</p>" % (
            esc(", ".join("%s is %s" % t for t in sysm["timers"]) or "none configured"),
            esc("%.1f GiB free of %.1f GiB" % (sysm["disk"]["free"] / 2**30, sysm["disk"]["total"] / 2**30)
                if sysm["disk"] else "unknown")))
    return "".join(out)


def _findings_section(model):
    rec = model["received"]
    out = [_source_note("Received items", rec)]
    if rec["state"] != "ok":
        return "".join(out)
    rows = []
    for item in rec["items"]:
        for f in item.get("findings") or []:
            f = f if isinstance(f, dict) else {}
            rule = f.get("rule_ref")
            rows.append((item.get("agency_id"), item.get("request_id"), item.get("item_id"), item.get("kind"),
                         f.get("rating"), json.dumps(rule, sort_keys=True, ensure_ascii=False)
                         if isinstance(rule, (dict, list)) else rule,
                         json.dumps(f.get("policy_ref"), sort_keys=True, ensure_ascii=False)
                         if f.get("policy_ref") else "", f.get("page"), f.get("quote")))
    out.append("<p>%d received items in run %s; a blank list means none reported in this source and run, not "
               "compliance. Ratings are documentary (meets/gap/unknown), review required, not legal findings.</p>"
               % (len(rec["items"]), esc(model["run_id"])))
    out.append(_table(("Agency", "Request", "Item", "Kind", "Rating", "Rule", "Policy", "Page", "Quote"), rows,
                      "Findings from received items"))
    return "".join(out)


def _days(value, day):
    try:
        return (date.fromisoformat(value) - day).days
    except (TypeError, ValueError):
        return None


def _deadlines_section(model):
    day = date.fromisoformat(model["as_of"])
    kit = model["kit"]
    out = ["<p>As of %s. Provisional dates only; not a legal calculation and no gap proves a violation.</p>"
           % esc(model["as_of"])]
    if kit["state"] == "not_configured":
        out.append('<p class="diag">Campaign deadlines not configured.</p>')
    else:
        out.append(_source_note("Campaign deadlines", kit))
    if kit["state"] == "ok":
        due = kit["due"]
        # stated_passed rows carry the agency's stated date ("stated") and a positive count of days past it;
        # show the date and a negative (overdue) day count so they sort and render as overdue, not as future days.
        rows = [(r.get("days"), r.get("due"), r.get("state"), r.get("agency_id"), r.get("request"), r.get("note"))
                for r in due["open"]]
        rows += [(-abs(r["days"]) if isinstance(r.get("days"), int) else None, r.get("stated"), r.get("state"),
                  r.get("agency_id"), r.get("request"), r.get("note")) for r in due["stated_passed"]]
        rows.sort(key=lambda r: (r[0] is None, r[0] if r[0] is not None else 0))
        out.append("<p>Law package: %s. %s</p>" % (esc(due["law_package_status"]), esc(due["note"])))
        out.append(_table(("Days (negative is overdue)", "Due", "State", "Agency", "Request", "Note"),
                          [(_Raw('<span class="overdue">%s</span>' % esc(r[0])) if isinstance(r[0], int) and r[0] < 0
                            else r[0], *r[1:]) for r in rows], "Campaign deadlines, overdue first"))
    rec = model["received"]
    stated = []
    if rec["state"] == "ok":
        for item in rec["items"]:
            for d in item.get("deadlines") or []:
                if isinstance(d, dict):
                    stated.append((_days(d.get("date"), day), d.get("date"), item.get("agency_id"),
                                   item.get("request_id"), item.get("item_id"), d.get("page"), d.get("quote")))
    stated.sort(key=lambda r: (r[0] is None, r[0] if r[0] is not None else 0))
    out.append("<h3>Dates stated in received mail</h3>" + _source_note("Received items", rec))
    if rec["state"] == "ok":
        out.append(_table(("Days (negative is passed)", "Stated date", "Agency", "Request", "Item", "Page", "Quote"),
                          [(_Raw('<span class="overdue">%s</span>' % esc(r[0])) if isinstance(r[0], int) and r[0] < 0
                            else r[0], *r[1:]) for r in stated], "Stated dates, passed first (not legal deadlines)"))
    return "".join(out)


def _form(action, token, ident, label, field_html, button, back=None):
    ret = '<input type="hidden" name="return" value="%s">' % esc(back) if back else ""
    return ('<form method="post" action="%s"><input type="hidden" name="csrf_token" value="%s">'
            '<input type="hidden" name="id" value="%s">%s%s<button type="submit">%s</button></form>'
            % (esc(action), esc(token), esc(ident), ret, field_html, esc(button)))


def _next_label(d, approved_word):
    return {"pending": "proposed", "approved": approved_word, "rejected": "denied"}.get(d["status"], d["status"])


def _next_row(d, approved_word, token, project_live):
    i = esc(d["id"])
    request = d["request_id"] if d["request_id"] != "agency-wide" else "agency-wide"
    ev = d.get("evidence") if isinstance(d.get("evidence"), dict) else {}
    out = ['<article class="draft" aria-label="Next step %s"><h3>%s, request: %s</h3>' % (i[:12], esc(d["agency_id"]), esc(request))]
    out.append("<dl><dt>Status</dt><dd>%s</dd><dt>Why recommended</dt><dd>%s</dd><dt>Wording</dt><dd>%s</dd>" % (
        esc(_next_label(d, approved_word)), esc(_text(ev.get("why"), 400)),
        esc("Template draft" if ev.get("wording") == "template" else "Draft: model-assisted wording, review required")))
    if d.get("reason"):
        out.append("<dt>Denial reason</dt><dd>%s</dd>" % esc(d["reason"]))
    if d.get("approved_by"):
        out.append("<dt>Decided by</dt><dd>%s at %s</dd>" % (esc(d["approved_by"]), esc(d["updated_at"])))
    elif d["status"] == "rejected":
        out.append("<dt>Decided by</dt><dd>%s at %s</dd>" % (esc(d["actor"]), esc(d["updated_at"])))
    out.append("<dt>Source finding(s)</dt><dd>")
    for p in ev.get("proposals") or []:
        if isinstance(p, dict):
            out.append("<p>%s on page %s, record %s: <q>%s</q></p>" % (esc(_text(p.get("type"), 40)), esc(p.get("page")),
                                                                        esc(_text(p.get("item_id"), 80)),
                                                                        esc(_text(p.get("quote"), 600))))
    out.append("</dd><dt>Text</dt><dd><pre>%s</pre></dd></dl>" % esc(d["body"]))
    if project_live and d["status"] == "pending":
        out.append("<p>%s</p>" % esc(NEXT_CAVEAT))
        if d.get("payload") is not None:
            out.append("<p>To approve, sign this exact payload with the approver key "
                       "(<code>ssh-keygen -Y sign -n deflock-approval</code>) and paste the signature.</p><pre>%s</pre>"
                       % esc(d["payload"]))
            out.append(_form("/approvals/approve", token, d["id"], "", (
                '<label for="sig-%s">Detached SSH signature</label><textarea id="sig-%s" name="signature" rows="6" '
                'required></textarea>' % (i[:12], i[:12])), "Approve (%s only)" % approved_word, "/next-steps"))
        out.append(_form("/approvals/reject", token, d["id"], "", (
            '<label for="rej-%s">Denial reason (required)</label><input type="text" id="rej-%s" name="reason" '
            'required maxlength="2000">' % (i[:12], i[:12])), "Deny", "/next-steps"))
    out.append("</article>")
    return "".join(out)


def _next_sections(model, token):
    """The three Next Steps sections (stable ids social, site, followup) from the queue's current rows."""
    q = model["queue"]
    out = ["<p>%s</p>" % esc(NEXT_CAVEAT), _source_note("Approval queue", q)]
    if q["state"] != "ok":
        return "".join(out)
    for ident, title, kind, approved_word in NEXT_SECTIONS:
        rows = [d for d in q["drafts"] if d["kind"] == kind][:ROW_LIMIT]
        out.append('<section id="%s"><h2>%s</h2>' % (ident, esc(title)))
        out.append("".join(_next_row(d, approved_word, token, token is not None) for d in rows)
                   or "<p>No recommendations of this kind.</p>")
        out.append("</section>")
    return "".join(out)


def render_next_steps(model, *, csrf_token=None):
    """Next Steps page. csrf_token=None renders the static, non-actionable snapshot (no forms, no token)."""
    mode = ("Live: a decision is recorded only; nothing is sent or published." if csrf_token is not None
            else "Static snapshot — decisions unavailable")
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<meta name="robots" content="noindex,nofollow"><title>Next steps</title><style>' + STYLE + "</style></head><body><header>"
            '<p class="notice">' + esc(BANNER) + '</p><h1>Next steps</h1><p class="notice">' + esc(mode) + "</p>"
            '<nav aria-label="Pages"><a href="/">Home</a> <a href="/dashboard">Dashboard</a></nav></header><main id="content">'
            + _next_sections(model, csrf_token) + '</main><footer><p class="notice">' + esc(BANNER)
            + "</p></footer></body></html>\n")


def _mailbox_cell(model, d):
    """Mailbox state of one draft from the placement journal. Nothing here sends or contacts a server."""
    mb = model.get("mail_drafts")
    if mb is None:
        return ""
    if mb.get("state") != "ok":
        return "<dt>Mailbox</dt><dd>Placement state unavailable: %s</dd>" % esc(mb.get("code", "unknown"))
    mine = {r["revision"]: r for r in mb["rows"] if r["queue_id"] == d["id"]}
    now = mine.get(d["revision"])
    if d["status"] != "approved":
        earlier = [r for r in mine.values() if r["state"] in ("placed", "found_existing")]
        if not earlier:
            return ""
        return ("<dt>Mailbox</dt><dd>Earlier revision %s is in Drafts; the edited text is not placed until it is "
                "approved. Draft only. Nothing has been sent.</dd>" % esc(max(r["revision"] for r in earlier)))
    lines = []
    if now is None:
        lines.append("Not placed yet")
    elif now["state"] == "failed":
        lines.append("Placement failed: %s" % esc(now.get("code", "unknown")))
    else:
        mid = str(now.get("message_id") or "")
        lines.append("In Drafts (%s)" % ("threaded" if now.get("threaded")
                                         else "not threaded: " + esc(now.get("thread_reason") or "thread_not_found")))
        lines.append('<a href="mid:%s">open by Message-ID</a> <code>%s</code>' % (
            esc(urllib.parse.quote(mid.strip("<>"), safe="@")), esc(mid)))
        lines.append("Text fingerprint <code>%s</code>" % esc(now.get("fingerprint") or ""))
    for r in sorted(mine.values(), key=lambda r: r["revision"]):
        if r["state"] == "superseded":
            lines.append("Revision %s superseded by revision %s" % (esc(r["revision"]), esc(r.get("superseded_by"))))
    lines.append("Draft only. Nothing has been sent.")
    return "<dt>Mailbox</dt><dd>%s</dd>" % "<br>".join(lines)


def _drafts_section(model, token):
    q = model["queue"]
    live = token is not None
    out = [_source_note("Approval queue", q)]
    out.append("<p>Approval marks a correspondence draft approved-to-send for a human. It does not send anything, "
               "publish, verify facts, or clear OCR/review flags. Attribution of reject/edit is the local "
               "operator label, not authenticated identity.</p>")
    if q["state"] != "ok":
        return "".join(out) + "<p>Decisions are disabled while the queue is unavailable.</p>"
    if not q["drafts"]:
        out.append("<p>No drafts in the queue.</p>")
    for d in q["drafts"][:ROW_LIMIT]:
        i = esc(d["id"])
        out.append('<article class="draft" aria-label="Draft %s"><h3>%s draft for %s</h3>' % (
            i[:12], esc(d["kind"]), esc(d["agency_id"])))
        out.append("<dl><dt>Status</dt><dd>%s (revision %s)</dd><dt>Audit</dt><dd>last action by %s at %s; created %s"
                   "</dd><dt>Item / request / run</dt><dd>%s / %s / %s</dd>" % (
                       esc(d["status"]), esc(d["revision"]), esc(d["actor"]), esc(d["updated_at"]),
                       esc(d["created_at"]), esc(d["item_id"]), esc(d["request_id"]), esc(d["source_run_id"])))
        if d.get("reason"):
            out.append("<dt>Rejection reason</dt><dd>%s</dd>" % esc(d["reason"]))
        if d.get("approved_by"):
            out.append("<dt>Approved by</dt><dd>%s</dd>" % esc(d["approved_by"]))
        out.append(_mailbox_cell(model, d))
        out.append(mail_send.send_cell(model, d, live))
        out.append("<dt>Text</dt><dd><pre>%s</pre></dd></dl>" % esc(d["body"]))
        if live and d["status"] == "pending":
            pid = "sig-" + i[:12]
            if d.get("payload") is not None:
                out.append("<p>To approve, sign this exact payload with the approver key "
                           "(<code>ssh-keygen -Y sign -n deflock-approval</code>) and paste the signature.</p>"
                           "<pre>%s</pre>" % esc(d["payload"]))
                out.append(_form("/approvals/approve", token, d["id"], "", (
                    '<label for="%s">Detached SSH signature</label><textarea id="%s" name="signature" rows="6" '
                    'required></textarea>' % (pid, pid)), "Approve (marks approved-to-send only)"))
            out.append(_form("/approvals/reject", token, d["id"], "", (
                '<label for="rej-%s">Rejection reason (required)</label><input type="text" id="rej-%s" '
                'name="reason" required maxlength="2000">' % (i[:12], i[:12])), "Reject"))
            out.append(_form("/approvals/edit", token, d["id"], "", (
                '<label for="ed-%s">Draft text</label><textarea id="ed-%s" name="body" rows="10" required>%s'
                "</textarea>" % (i[:12], i[:12], esc(d["body"]))), "Save edit"))
        out.append("</article>")
    return "".join(out)


def render_dashboard(model, *, csrf_token=None):
    """Same model, same bytes. csrf_token=None renders the static, non-actionable snapshot."""
    live = csrf_token is not None
    mode = ("Live: approvals mark approved-to-send only; nothing is sent." if live
            else "Static snapshot — decisions unavailable")
    sections = {"mail": _mail_section(model), "documents": _documents_section(model),
                "stages": _stages_section(model), "findings": _findings_section(model),
                "deadlines": _deadlines_section(model), "drafts": _drafts_section(model, csrf_token)}
    out = ['<!doctype html><html lang="en"><head><meta charset="utf-8">',
           '<meta name="viewport" content="width=device-width, initial-scale=1">',
           '<meta name="robots" content="noindex,nofollow"><title>Private records dashboard</title>',
           "<style>" + STYLE + "</style></head><body><header>", '<p class="notice">' + esc(BANNER) + "</p>",
           '<a href="#content">Skip to content</a><h1>Records pipeline dashboard</h1>',
           '<p class="notice">' + esc(mode) + "</p>",
           "<p>Run %s, captured %s, deadlines as of %s. Current ledger state and the selected run's outcomes are "
           "separate sources.</p>" % (esc(model["run_id"]), esc(model["captured_at"]), esc(model["as_of"])),
           '<nav aria-label="Dashboard sections">']
    out += ['<a href="#%s">%s</a>' % (i, esc(t)) for i, t in SECTIONS]
    out.append('<a href="/next-steps">Next steps</a>' if live else '<a href="#social">Next steps</a>')
    out.append('</nav></header><main id="content" tabindex="-1">')
    if not model["run_outputs_present"]:
        out.append('<p class="diag" role="status">Selected run outputs unavailable (run_outputs_missing).</p>')
    for ident, title in SECTIONS:
        out.append('<section id="%s"><h2>%s</h2>%s</section>' % (ident, esc(title), sections[ident]))
    if not live:
        out.append(_next_sections(model, None))
    out.append('</main><footer><p class="notice">' + esc(BANNER) + "</p></footer></body></html>")
    return "\n".join(out) + "\n"


# ---- static writer -----------------------------------------------------------------------------------

def write_snapshot(out_path, text, protected=()):
    out = Path(os.path.abspath(out_path))
    parent = out.parent
    if any(p.is_symlink() for p in (parent, *parent.parents)) or not parent.is_dir():
        raise PanelError("output_unsafe_path")
    if out in {Path(os.path.abspath(p)) for p in protected}:
        raise PanelError("output_collides_with_source")
    try:
        info = os.lstat(out)
    except FileNotFoundError:
        info = None
    if info is not None and not stat.S_ISREG(info.st_mode):
        raise PanelError("output_unsafe_path")
    tmp = parent / (".%s.%s.tmp" % (out.name, secrets.token_hex(6)))
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        info = None
        try:
            info = os.lstat(out)
        except FileNotFoundError:
            pass
        if info is not None and not stat.S_ISREG(info.st_mode):
            raise PanelError("output_unsafe_path")
        os.replace(tmp, out)
    except OSError:
        raise PanelError("output_write_failed") from None
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


# ---- server ------------------------------------------------------------------------------------------

def check_actor(actor):
    if not (isinstance(actor, str) and actor.strip() and len(actor) <= 80
            and not any(ord(c) < 32 or ord(c) == 127 for c in actor)):
        raise PanelError("actor_invalid")
    return actor


ROUTES = {"/approvals/approve": ("approve", ("csrf_token", "id", "signature")),
          "/approvals/reject": ("reject", ("csrf_token", "id", "reason")),
          "/approvals/edit": ("edit", ("csrf_token", "id", "body"))}
SEND_ROUTE = ("send", ("csrf_token", "id", "fingerprint", "signature", "to", "confirm"))
SECURITY = (("Cache-Control", "no-store"), ("X-Content-Type-Options", "nosniff"), ("Referrer-Policy", "no-referrer"),
            ("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
                                        "base-uri 'none'; frame-ancestors 'none'"))


def _place_after_approve(project, config_path, ident):
    """Draft-only placement of the one approved row. A failure is journaled and shown; it never undoes the approval."""
    try:
        mail_drafts.place(project, mail_drafts.load_config(config_path), only_id=ident)
    except Exception as error:  # nothing here may break the approval that already happened
        code = error.code if isinstance(error, mail_drafts.MailDraftError) else "placement_failed"
        try:
            mail_drafts.record_failure(project, ident, code)
        except Exception:
            pass


class _Handler(http.server.BaseHTTPRequestHandler):
    server_version = "RecordsPanel"
    sys_version = ""
    protocol_version = "HTTP/1.0"
    timeout = SOCKET_TIMEOUT

    def log_message(self, *args):  # request lines and bodies may carry record text or tokens
        pass

    def _send(self, status, body, location=None):
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        for k, v in SECURITY:
            self.send_header(k, v)
        if location:
            self.send_header("Location", location)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _fail(self, status):
        text = REASONS.get(status, "Error")
        self._send(status, '<!doctype html><html lang="en"><head><meta charset="utf-8"><title>%s</title></head>'
                           "<body><h1>%s</h1><p>%s</p><p><a href=\"/\">Back to the dashboard</a></p></body></html>\n"
                   % (esc(text), esc(text), esc(BANNER)))

    def _host_ok(self):
        hosts = self.headers.get_all("Host") or []
        return len(hosts) == 1 and hosts[0] == "127.0.0.1:%d" % self.server.server_address[1]

    def do_GET(self):
        if not self._host_ok():
            return self._fail(403)
        if console_packets.serve(self):
            return
        if console_home.serve(self):
            return
        if console_manual_events.serve(self):
            return
        if console_documents.serve(self):  # Screen C
            return
        if console_board.serve(self):  # Screens A and B
            return
        if console_oversight.serve(self):  # Screens E and F
            return
        path, _, query = self.path.partition("?")
        if path == "/send":
            return self._prepare_send(query)
        route = path
        if route not in RETURNS:
            return self._fail(404)
        try:
            if route == "/next-steps":
                model = {"queue": _load_queue(self.server.options["project"])}
            else:
                model = load_dashboard(**self.server.options)
        except PanelError:
            return self._fail(503)
        render = render_next_steps if route == "/next-steps" else render_dashboard
        self._send(200, render(model, csrf_token=self.server.csrf_token))

    def _prepare_send(self, query):
        """Review page behind Approve and send: read-only, contacts the Drafts mailbox only when sending is on."""
        path = self.server.options.get("send_config")
        args = urllib.parse.parse_qs(query, keep_blank_values=True)
        ident = (args.get("id") or [""])[0]
        if path is None or not DRAFT_ID.fullmatch(ident) or len(args.get("to", [])) > 1:
            return self._fail(404 if path is None else 400)
        hooks = self.server.send_hooks
        info = mail_send.prepare(self.server.options["project"], path, ident, to=(args.get("to") or [None])[0],
                                 imap_factory=hooks.get("imap_factory", mail_send.mail_poll.default_connect))
        if info["code"] == "recipient_missing":
            return self._send(200, mail_send.render_need_to(ident))
        self._send(200, mail_send.render_prepare(info, self.server.csrf_token))

    def _post_send(self, fields):
        path = self.server.options.get("send_config")
        if path is None:
            return self._fail(404)
        hooks = self.server.send_hooks
        result = mail_send.send_one(
            self.server.options["project"], path, fields["id"], actor=self.server.actor,
            page_fingerprint=fields["fingerprint"], signature=fields["signature"].replace("\r\n", "\n"),
            to=fields["to"], confirm=fields["confirm"], imap_factory=hooks.get("imap_factory", mail_send.mail_poll.default_connect),
            smtp_factory=hooks.get("smtp_factory"))
        self._send(409 if result["state"] == "refused" else 200, mail_send.render_result(result))

    def _refuse_method(self):
        self._fail(405)

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_HEAD = do_TRACE = do_CONNECT = _refuse_method

    def _body(self):
        """Parsed urlencoded fields, or an HTTP status int on any violation."""
        if self.headers.get("Transfer-Encoding") is not None:
            return 400
        lengths = self.headers.get_all("Content-Length") or []
        if len(lengths) != 1:
            return 411 if not lengths else 400
        if not (lengths[0].isascii() and lengths[0].isdigit()):
            return 400
        length = int(lengths[0])
        if length > MAX_BODY:
            return 413
        kinds = self.headers.get_all("Content-Type") or []
        if len(kinds) != 1 or kinds[0].split(";")[0].strip().lower() != "application/x-www-form-urlencoded":
            return 415
        try:
            raw = self.rfile.read(length)
        except (socket.timeout, TimeoutError, OSError):
            return 408
        if len(raw) != length:
            return 400
        try:
            pairs = urllib.parse.parse_qsl(raw.decode("ascii"), keep_blank_values=True, strict_parsing=True,
                                           encoding="utf-8", errors="strict")
        except (UnicodeError, ValueError):
            return 400
        fields = dict(pairs)
        if len(fields) != len(pairs) or any("\x00" in v for v in fields.values()):
            return 400
        return fields

    def do_POST(self):
        if not self._host_ok():
            return self._fail(403)
        if console_packets.post(self):
            return
        if console_oversight.post(self):  # Screen F: Undo, OFF and kill only
            return
        route = ROUTES.get(self.path) or (SEND_ROUTE if self.path == "/send" else console_manual_events.ROUTE if self.path == "/manual-event" else None)
        if route is None:
            return self._fail(404)
        origins = self.headers.get_all("Origin") or []
        if origins != ["http://127.0.0.1:%d" % self.server.server_address[1]]:
            return self._fail(403)
        fields = self._body()
        if isinstance(fields, int):
            return self._fail(fields)
        action, names = route
        back = fields.pop("return", "/")  # optional; it only chooses the 303 target
        if set(fields) != set(names):
            return self._fail(400)
        if not hmac.compare_digest(fields["csrf_token"].encode("utf-8"), self.server.csrf_token.encode("ascii")):
            return self._fail(403)
        if back not in RETURNS:
            return self._fail(400)
        if action == "manual":
            return console_manual_events.post(self, fields)
        ident = fields["id"]
        if not DRAFT_ID.fullmatch(ident):
            return self._fail(400)
        if action == "send":  # one message per POST; nothing else is read from the body
            return self._post_send(fields)
        value = fields[names[2]].replace("\r\n", "\n")
        if not value.strip():
            return self._fail(400)
        project, actor = self.server.options["project"], self.server.actor
        hook = False
        try:
            if action == "approve":
                approval_queue.approve(project, ident, {"kind": "ssh", "signature": value})
                expected = {"approved"}
                hook = True
            elif action == "reject":
                approval_queue.reject(project, ident, actor, value)
                expected = {"rejected"}
            else:
                approval_queue.edit(project, ident, actor, value)
                expected = {"pending"}
            rows = {r["id"]: r for r in approval_queue.load_queue(project)}
        except approval_queue.ApprovalQueueError as error:
            return self._fail(ACTION_STATUS.get(error.code, 503))
        except OSError:
            return self._fail(503)
        if rows.get(ident, {}).get("status") not in expected:  # never claim success from a return value alone
            return self._fail(503)
        if hook and self.server.mail_drafts_config and rows[ident].get("kind") not in draft_templates.NEXT_STEP_KINDS:
            _place_after_approve(project, self.server.mail_drafts_config, ident)
        self._send(303, "", location=back)


def build_server(project, run_id, *, actor, port=0, run_report_path=None, kit_root=None, as_of=None,
                 timers=DEFAULT_TIMERS, system=True, mail_drafts_config=None, send_config=None):
    """Unstarted loopback-only server; the caller serves and closes it."""
    check_run_id(run_id)
    check_actor(actor)
    parse_as_of(as_of)
    if type(port) is not int or not 0 <= port <= 65535:
        raise PanelError("port_invalid")
    server = http.server.HTTPServer(("127.0.0.1", port), _Handler)
    server.csrf_token = secrets.token_urlsafe(32)
    server.manual_event_key = secrets.token_urlsafe(32)
    server.actor = actor
    server.mail_drafts_config = mail_drafts_config
    server.send_hooks = {}  # tests inject imap_factory / smtp_factory; production uses the real ones
    server.options = {"project": os.path.abspath(project), "run_id": run_id, "run_report_path": run_report_path,
                      "kit_root": kit_root, "as_of": as_of, "timers": tuple(timers), "system": system,
                      "send_config": send_config}
    return server


# ---- command line ------------------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="records panel", description="Private local records dashboard; sends nothing.",
        epilog="Documentation: docs/RECORDS-ADMIN-PANEL.md")
    parser.add_argument("--project", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-report")
    parser.add_argument("--kit-root")
    parser.add_argument("--as-of", help="YYYY-MM-DD; fixes the deadline day and health clock (UTC midnight)")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--actor", help="operator label for Reject and Edit (not authenticated identity)")
    parser.add_argument("--render", metavar="OUT.html", help="write a static, non-actionable snapshot and exit")
    parser.add_argument("--timer-unit", action="append", help="systemd user timer to report (repeatable)")
    parser.add_argument("--no-system", action="store_true", help="skip timer and disk checks")
    parser.add_argument("--mail-drafts-config", metavar="FILE",
                        help="owner-only mailbox config: after Approve, place that one draft in the Drafts folder")
    parser.add_argument("--send-config", metavar="FILE",
                        help="owner-only send.json: enables the Approve and send button (still off until enabled)")
    args = parser.parse_args(argv)
    timers = tuple(args.timer_unit) if args.timer_unit else DEFAULT_TIMERS
    try:
        check_run_id(args.run_id)
        parse_as_of(args.as_of)
        if args.render:
            model = load_dashboard(args.project, args.run_id, run_report_path=args.run_report, kit_root=args.kit_root,
                                   as_of=args.as_of, timers=timers, system=not args.no_system)
            project = Path(os.path.abspath(args.project))
            protected = [project / "processing-v1" / "ledger.sqlite", project / "approvals" / "queue.jsonl",
                         project / "run-outputs" / args.run_id / "outcomes" / "received-items.json"]
            protected += [Path(p) for p in (args.run_report,) if p]
            write_snapshot(args.render, render_dashboard(model), protected)
            return 0
        if not args.actor:
            raise PanelError("actor_required")
        if args.mail_drafts_config:
            try:
                mail_drafts.load_config(args.mail_drafts_config)
            except mail_drafts.MailDraftError:
                raise PanelError("mail_drafts_config_invalid") from None
        if not 1 <= args.port <= 65535:
            raise PanelError("port_invalid")
        server = build_server(args.project, args.run_id, actor=args.actor, port=args.port,
                              run_report_path=args.run_report, kit_root=args.kit_root, as_of=args.as_of,
                              timers=timers, system=not args.no_system, mail_drafts_config=args.mail_drafts_config,
                              send_config=args.send_config)
    except PanelError as error:
        print(error.code, file=sys.stderr)
        return 2 if error.code.endswith(("_invalid", "_required")) else 1
    except OSError:
        print("panel_start_failed", file=sys.stderr)
        return 1
    print("Records dashboard on http://127.0.0.1:%d/ (loopback only). %s" % (
        server.server_address[1], "Approve and send is configured (off until enabled)." if args.send_config
        else "Nothing is sent."), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
