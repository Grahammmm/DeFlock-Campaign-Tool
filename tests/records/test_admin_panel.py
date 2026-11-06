"""Local records dashboard: six sources, read-only loads, loopback forms that only touch the approval queue."""
import hashlib
import http.client
import json
import os
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import closing
from html.parser import HTMLParser
from pathlib import Path
from unittest import mock

from campaign_tool.records import admin_panel as ap
from campaign_tool.records import approval_queue as aq
from campaign_tool.records.ledger import store
from tests.approval_signing import Signer

REPO = Path(__file__).resolve().parents[2]
EXAMPLE = REPO / "examples" / "run-report" / "records-run-report-v1.synthetic.json"
RUN = "run_20260101T060000Z_0a1b2c3d"
AS_OF = "2026-11-05"
EML, ATT, DOC = "e" * 64, "a" * 64, "d" * 64
SECRET = "HEADERSECRET-x9"
EVIDENCE = {"findings": [], "deadlines": []}


def sha(n):
    return f"{n:064x}"


class Ids(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids, self.tags, self.attrs, self.forms = set(), [], [], []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        for k, v in attrs:
            self.attrs.append(k)
            if k == "id":
                self.ids.add(v)
        if tag == "form":
            self.forms.append(dict(attrs))


def parse(html):
    p = Ids()
    p.feed(html)
    return p


class Base(unittest.TestCase):
    def setUp(self):
        os.umask(0o077)
        self.tmp = tempfile.TemporaryDirectory(prefix="panel-")
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name) / "project"
        self.project.mkdir(mode=0o700)
        self.ledger = self.project / "processing-v1" / "ledger.sqlite"
        self.queue = self.project / "approvals" / "queue.jsonl"
        self.outputs = self.project / "run-outputs" / RUN / "outcomes"

    def build_ledger(self):
        store.initialize(self.ledger)
        with store.ledger(self.ledger) as con:
            con.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?)",
                        ("r1", "stage-runner", "2026-11-05T07:00:00+00:00", "2026-11-05T07:05:00+00:00", "t", None,
                         None, None, "completed", "{}"))
            for digest, mime, size, seen in ((EML, "message/rfc822", 10, "2026-11-02T18:59:00Z"),
                                             (ATT, "application/pdf", 20, "2026-11-02T18:59:01Z"),
                                             (DOC, "application/pdf", 30, "2026-11-03T00:00:00Z")):
                con.execute("INSERT INTO originals(sha256,bytes,mime_detected,first_seen_at,role,scope,"
                            "preservation_status,provenance_json) VALUES(?,?,?,?,?,?,?,?)",
                            (digest, size, mime, seen, "agency_original", "in_scope", "verified",
                             json.dumps({"secret": SECRET})))
            con.execute("INSERT INTO occurrences VALUES('o1',?,'mail','m',NULL,NULL,'t','{}')", (EML,))
            con.execute("INSERT INTO occurrences VALUES('o2',?,'attachment','/home/x/private-name.pdf','o1',NULL,'t','{}')",
                        (ATT,))
            con.execute("INSERT INTO mail_messages VALUES('msg-1','acct','INBOX',1,1,?,?,?,?,'x')",
                        ("<id@x>", EML, json.dumps({"fields": [["Subject", SECRET]]}), "2026-11-02T18:59:00Z"))
            for digest, stage, status, reason in ((ATT, "extract", "blocked", "EXTRACT_ERROR"),
                                                  (ATT, "preserve", "pending", None),
                                                  (DOC, "extract", "blocked", "AWAITING_OCR")):
                con.execute("INSERT INTO stage_state(original_sha256,stage,status,receipt_sha256,owner,updated_at,"
                            "run_id,reason) VALUES(?,?,?,NULL,'t','2026-11-05T07:00:00Z','r1',?)",
                            (digest, stage, status, reason))
            con.commit()

    def write(self, path, doc):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_text(doc if isinstance(doc, str) else json.dumps(doc), encoding="utf-8")
        os.chmod(path, 0o600)
        return path

    def received(self, items=None):
        item = {"item_id": sha(1), "agency_id": "caltrans", "request_id": "req-1", "kind": "extension",
                "findings": [{"finding_id": sha(9), "rating": "gap", "rule_ref": {"citation": "Gov. Code 7922.535"},
                              "policy_ref": None, "page": 2, "quote": "Camera logs are not enclosed."}],
                "deadlines": [{"date": "2026-11-30", "page": 1, "quote": "We will reply by November 30."},
                              {"date": "2026-11-01", "page": 1, "quote": "Earlier date."}]}
        doc = {"schema": "records-received-item-v1", "run_id": RUN, "items": items or [item], "diagnostics": []}
        return self.write(self.outputs / "received-items.json", doc)

    def report(self):
        return self.write(self.project / "report.json", json.loads(EXAMPLE.read_text(encoding="utf-8")))

    def draft(self, n=1, body="Please extend.\n</textarea><script>alert(1)</script> \"q\" javascript:x"):
        return aq.add_draft(self.project, item_id=sha(n), kind="reply-extension", agency_id="caltrans",
                            request_id="req-1", body=body, evidence=EVIDENCE, source_run_id=RUN)

    def full(self):
        self.build_ledger()
        self.received()
        self.report()
        return self.draft()

    def load(self, **kw):
        kw.setdefault("as_of", AS_OF)
        kw.setdefault("system", False)
        kw.setdefault("run_report_path", self.project / "report.json")
        return ap.load_dashboard(self.project, RUN, **kw)


def journal(project):
    path = project / "approvals" / "queue.jsonl"
    return path.read_bytes() if path.exists() else b""


def tree_state(project):
    out = {}
    for p in sorted(project.rglob("*")):
        if p.is_file() and p.suffix != ".sqlite":
            out[str(p)] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


class ReadTests(Base):
    def test_all_six_sources_and_run_provenance(self):
        self.full()
        html = ap.render_dashboard(self.load())
        self.assertEqual({"mail", "documents", "stages", "findings", "deadlines", "drafts"} - parse(html).ids, set())
        for needle in ("msg-1", "EXTRACT_ERROR", "AWAITING_OCR", "Camera logs are not enclosed.", "caltrans",
                       "GATE_REVIEW_REQUIRED", "Campaign deadlines not configured", "2026-11-30", RUN,
                       "reply-extension draft"):
            self.assertIn(needle, html)
        # one message, one attachment, three originals: counts are not conflated
        self.assertIn("Messages in ledger: 1", html)
        self.assertIn("Documents in ledger: 3", html)
        self.assertIn("<td>1</td>", html)
        for leaked in (SECRET, "private-name.pdf", "/home/x"):
            self.assertNotIn(leaked, html)
        self.assertNotIn("<form", html)
        self.assertIn("Static snapshot — decisions unavailable", html)

    def test_reads_never_mutate(self):
        self.full()
        before = tree_state(self.project)
        with closing(sqlite3.connect(self.ledger)) as con:
            dump = "\n".join(con.iterdump())
        with mock.patch.object(aq, "approve", side_effect=AssertionError), \
                mock.patch.object(aq, "add_draft", side_effect=AssertionError), \
                mock.patch.object(subprocess, "run", side_effect=AssertionError), \
                mock.patch.object(socket.socket, "connect", side_effect=AssertionError):
            for _ in range(2):
                ap.render_dashboard(self.load())
        with closing(sqlite3.connect(self.ledger)) as con:
            self.assertEqual("\n".join(con.iterdump()), dump)
        self.assertEqual(tree_state(self.project), before)
        missing = Path(self.tmp.name) / "nowhere"
        model = ap.load_dashboard(missing, RUN, as_of=AS_OF, system=False)
        self.assertEqual(model["ledger"]["code"], "ledger_missing")
        self.assertFalse(missing.exists())

    def test_partial_sources_fail_independently(self):
        self.full()
        self.report().write_text("{truncated", encoding="utf-8")
        model = self.load()
        self.assertEqual(model["report"]["code"], "report_invalid_json")
        self.assertEqual(model["ledger"]["state"], "ok")
        self.assertEqual(len(model["queue"]["drafts"]), 1)
        # a mismatched run report is stale, not healthy
        doc = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        doc["run_id"] = "run_20260102T060000Z_0a1b2c3d"
        self.write(self.project / "report.json", doc)
        self.assertEqual(self.load()["report"]["code"], "report_run_mismatch")
        # corrupt queue disables actions but keeps the rest visible
        self.queue.write_bytes(b"{not json}\n")
        html = ap.render_dashboard(self.load(), csrf_token="t")
        self.assertIn("queue_invalid", html)
        self.assertNotIn("<form", html)
        self.assertIn("msg-1", html)
        # bad inputs per source
        target = self.outputs / "received-items.json"
        for bad in ('{"a":1,"a":2}', '{"schema":"records-received-item-v1","items":[NaN]}', '"\\ud800"'):
            target.write_text(bad, encoding="utf-8")
            self.assertEqual(self.load()["received"]["code"], "received_invalid_json", bad)
        target.write_bytes(b"\xff\xfe")
        self.assertEqual(self.load()["received"]["code"], "received_invalid_json")
        target.write_bytes(b" " * (ap.MAX_JSON + 1))
        self.assertEqual(self.load()["received"]["code"], "received_oversize")
        target.unlink()
        os.mkfifo(target)
        self.assertEqual(self.load()["received"]["code"], "received_unsafe_path")
        target.unlink()
        real = self.write(self.project / "real.json", {})
        target.symlink_to(real)
        self.assertEqual(self.load()["received"]["code"], "received_unsafe_path")
        self.assertEqual(self.load(run_report_path=self.project / "absent.json")["report"]["code"], "report_missing")
        # ledger schema failure
        self.ledger.write_bytes(b"not sqlite")
        self.assertEqual(self.load()["ledger"]["state"], "unavailable")
        self.assertEqual(len(self.load()["queue"]["state"]), len("unavailable"))

    def test_missing_run_is_unavailable_not_empty(self):
        self.build_ledger()
        html = ap.render_dashboard(self.load(run_report_path=None))
        self.assertIn("run_outputs_missing", html)
        self.assertIn("received_missing", html)
        self.assertIn("Run report: not configured", html)

    def test_deadlines_use_existing_semantics(self):
        self.full()
        due = {"as_of": AS_OF, "law_package_status": "missing", "past_due": 1, "note": "KITNOTE no law package.",
               "open": [{"agency_id": "a2", "request": "r2", "due": "2026-12-01", "days": 26, "state": "upcoming"},
                        {"agency_id": "a1", "request": "r1", "due": "2026-11-01", "days": -4, "state": "past_due"},
                        {"agency_id": "a3", "request": "r3", "due": None, "days": None, "state": "no_law_package"}],
               "stated_passed": [{"agency_id": "a4", "request": "r4", "stated": "2026-10-01", "days": 35,
                                  "state": "stated_date_passed"}]}
        with mock.patch("campaign_tool.kit_track.due", return_value=due) as fake:
            model = self.load(kit_root=self.project)
        self.assertEqual(fake.call_args.kwargs["as_of"], AS_OF)
        html = ap.render_dashboard(model)
        self.assertIn("KITNOTE no law package.", html)
        self.assertLess(html.index(">a1<"), html.index(">a2<"))  # overdue first, undated last
        self.assertGreater(html.index(">a3<"), html.index(">a2<"))
        self.assertIn("a4", html)
        self.assertNotIn("Campaign deadlines not configured", html)
        self.assertNotIn("legal clock is paused", html)
        # dates stated in received mail: passed first, never a legal deadline
        self.assertLess(html.index("Earlier date."), html.index("We will reply by November 30."))
        with mock.patch("campaign_tool.kit_track.due", side_effect=ValueError("secret path /x")):
            html = ap.render_dashboard(self.load(kit_root=self.project))
        self.assertIn("kit_unreadable", html)
        self.assertNotIn("/x", html)

    def test_untrusted_plaintext_and_accessibility(self):
        self.full()
        self.write(self.outputs / "received-items.json", {
            "schema": "records-received-item-v1", "run_id": RUN, "diagnostics": [],
            "items": [{"item_id": sha(1), "agency_id": "<script>a</script>", "request_id": "r", "kind": "x",
                       "findings": [{"rating": "gap", "page": 1,
                                     "quote": '<img src="https://evil.invalid/x"> javascript:alert(1)'}]}]})
        html = ap.render_dashboard(self.load(), csrf_token="tok")
        p = parse(html)
        self.assertNotIn("script", p.tags)
        self.assertNotIn("img", p.tags)
        self.assertFalse([a for a in p.attrs if a.startswith("on")])
        self.assertEqual(len(p.forms), 3)
        self.assertIn("&lt;/textarea&gt;&lt;script&gt;", html)
        self.assertEqual(html.count("</textarea>"), 2)  # signature + edit only; draft text stays inert
        self.assertIn("focus-visible", html)
        self.assertIn('aria-label="Dashboard sections"', html)
        self.assertEqual({i.split("-")[0] for i in p.ids if "-" in i}, {"sig", "rej", "ed"})


class Http:
    """Raw HTTP so headers can be wrong on purpose."""

    def __init__(self, port):
        self.port = port

    def send(self, method, path, body=b"", headers=None, host=None, raw=None):
        h = {"Host": host or f"127.0.0.1:{self.port}", "Origin": f"http://127.0.0.1:{self.port}",
             "Content-Type": "application/x-www-form-urlencoded", "Content-Length": str(len(body))}
        h.update(headers or {})
        lines = [f"{method} {path} HTTP/1.1"] + [f"{k}: {v}" for k, v in h.items() if v is not None]
        data = raw if raw is not None else ("\r\n".join(lines) + "\r\n\r\n").encode() + body
        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as s:
            s.sendall(data)
            s.shutdown(socket.SHUT_WR)
            out = b""
            while True:
                try:
                    chunk = s.recv(65536)
                except socket.timeout:
                    break
                if not chunk:
                    break
                out += chunk
        head, _, rest = out.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        return int(lines[0].split()[1]), {k.lower(): v for k, v in (l.split(": ", 1) for l in lines[1:])}, rest.decode()

    def post(self, path, fields, **kw):
        from urllib.parse import urlencode
        return self.send("POST", path, urlencode(fields).encode(), **kw)


class ServerCase(Base):
    def setUp(self):
        super().setUp()
        self.draft_row = self.full()
        self.signer = Signer()
        self.addCleanup(self.signer.close)
        trust = self.signer.trust()
        trust.__enter__()
        self.addCleanup(trust.__exit__, None, None, None)
        self.server = self.start()

    def start(self, **kw):
        server = ap.build_server(self.project, RUN, actor="operator-1", as_of=AS_OF, system=False,
                                 run_report_path=self.project / "report.json", **kw)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()

        def stop():
            server.shutdown()
            thread.join(5)
            server.server_close()
        self.addCleanup(stop)
        self.http = Http(server.server_address[1]) if kw.get("replace", True) else self.http
        return server

    @property
    def tok(self):
        return self.server.csrf_token

    def row(self, ident=None):
        rows = {r["id"]: r for r in aq.load_queue(self.project)}
        return rows[ident or self.draft_row["id"]]


class LiveFormTests(ServerCase):
    def test_get_page_has_token_forms_and_security_headers(self):
        status, headers, body = self.http.send("GET", "/?action=approve&id=x", headers={"Content-Length": None,
                                                                                   "Content-Type": None})
        self.assertEqual(status, 200)
        self.assertIn(self.tok, body)
        self.assertEqual(headers["cache-control"], "no-store")
        self.assertIn("default-src 'none'", headers["content-security-policy"])
        self.assertEqual(headers["x-content-type-options"], "nosniff")
        self.assertEqual(headers["referrer-policy"], "no-referrer")
        self.assertEqual(self.row()["status"], "pending")
        self.assertEqual(self.server.server_address[0], "127.0.0.1")

    def test_approve_reject_edit_persist_only_queue_decisions(self):
        other = self.draft(2, body="second draft")
        third = self.draft(3, body="third draft")
        before = tree_state(self.project)
        ident = self.draft_row["id"]
        # edit -> still pending, new body, configured actor
        status, headers, _ = self.http.post("/approvals/edit", {"csrf_token": self.tok, "id": ident,
                                                                 "body": "new text\r\nline 2"})
        self.assertEqual((status, headers["location"]), (303, "/"))
        row = self.row()
        self.assertEqual((row["status"], row["body"], row["actor"]), ("pending", "new text\nline 2", "operator-1"))
        # approve with a real signature over the exact payload
        sig = self.signer.sign(aq.approval_payload(self.project, ident))
        self.assertEqual(self.http.post("/approvals/approve", {"csrf_token": self.tok, "id": ident,
                                                                "signature": sig})[0], 303)
        self.assertEqual(self.row()["status"], "approved")
        self.assertEqual(self.row()["approved_by"], self.signer.principal)
        # approved is terminal: reject conflicts, same signature replays without a new event
        size = len(journal(self.project))
        self.assertEqual(self.http.post("/approvals/reject", {"csrf_token": self.tok, "id": ident,
                                                               "reason": "late"})[0], 409)
        self.assertEqual(self.http.post("/approvals/approve", {"csrf_token": self.tok, "id": ident,
                                                                "signature": sig})[0], 303)
        self.assertEqual(len(journal(self.project)), size)
        # reject needs a reason and records the configured actor
        self.assertEqual(self.http.post("/approvals/reject", {"csrf_token": self.tok, "id": other["id"],
                                                               "reason": " "})[0], 400)
        self.assertEqual(self.row(other["id"])["status"], "pending")
        self.assertEqual(self.http.post("/approvals/reject", {"csrf_token": self.tok, "id": other["id"],
                                                               "reason": "not needed"})[0], 303)
        rejected = self.row(other["id"])
        self.assertEqual((rejected["status"], rejected["reason"], rejected["actor"]),
                         ("rejected", "not needed", "operator-1"))
        self.assertEqual(self.row(third["id"])["status"], "pending")
        # nothing but the journal changed
        after = tree_state(self.project)
        self.assertEqual({k for k in after if after[k] != before.get(k)}, {str(self.queue)})
        # the page shows the persisted state and the audit line
        page = self.http.send("GET", "/", headers={"Content-Length": None, "Content-Type": None})[2]
        self.assertIn("approved (revision", page)
        self.assertIn("last action by operator-1", page)

    def test_approval_refusals_never_append(self):
        ident = self.draft_row["id"]
        size = len(journal(self.project))
        # a typed name, an unsigned blob, a signature by an untrusted key, an unknown id
        stranger = Signer("stranger@example.invalid", trusted=False)
        self.addCleanup(stranger.close)
        bad = stranger.sign(aq.approval_payload(self.project, ident))
        for sig in ("G", "garbage", bad):
            self.assertEqual(self.http.post("/approvals/approve", {"csrf_token": self.tok, "id": ident,
                                                                    "signature": sig})[0], 403)
        self.assertEqual(self.http.post("/approvals/approve", {"csrf_token": self.tok, "id": sha(77),
                                                                "signature": bad})[0], 404)
        self.assertEqual(len(journal(self.project)), size)
        self.assertEqual(self.row()["status"], "pending")
        # upstream readback/write failure is a bounded 503 with nothing reflected
        with mock.patch.object(aq, "load_queue", side_effect=aq.ApprovalQueueError("queue_io")):
            status, _, body = self.http.post("/approvals/reject", {"csrf_token": self.tok, "id": ident,
                                                                    "reason": "SENTINEL-REASON"})
        self.assertEqual(status, 503)
        self.assertNotIn("SENTINEL-REASON", body)
        with mock.patch.object(aq, "edit", side_effect=aq.ApprovalQueueError("queue_io")):
            self.assertEqual(self.http.post("/approvals/edit", {"csrf_token": self.tok, "id": ident,
                                                                 "body": "x"})[0], 503)

    def test_local_only_csrf_and_host(self):
        ident = self.draft_row["id"]
        size = len(journal(self.project))
        good = {"csrf_token": self.tok, "id": ident, "reason": "r"}
        port = self.http.port
        cases = [
            dict(fields={**good, "csrf_token": "wrong"}),
            dict(fields={**good, "csrf_token": ""}),
            dict(fields=good, headers={"Origin": "http://evil.invalid"}),
            dict(fields=good, headers={"Origin": "null"}),
            dict(fields=good, headers={"Origin": None}),
            dict(fields=good, headers={"Origin": f"http://127.0.0.1:{port}.evil.invalid"}),
            dict(fields=good, host="evil.invalid"),
            dict(fields=good, host=f"127.0.0.1:{port}.evil.invalid"),
            dict(fields=good, host=f"localhost:{port}"),
            dict(fields=good, host=None, headers={"Host": None}),
        ]
        for case in cases:
            kw = {k: v for k, v in case.items() if k != "fields"}
            self.assertEqual(self.http.post("/approvals/reject", case["fields"], **kw)[0], 403, case)
        old = self.tok
        self.server.shutdown()
        other = ap.build_server(self.project, RUN, actor="op", system=False)
        self.addCleanup(other.server_close)
        self.assertNotEqual(old, other.csrf_token)
        self.assertEqual(len(journal(self.project)), size)
        self.assertEqual(self.row()["status"], "pending")

    def test_other_methods_and_paths_do_not_touch_storage(self):
        size = len(journal(self.project))
        for method in ("PUT", "DELETE", "PATCH", "OPTIONS", "TRACE"):
            self.assertEqual(self.http.send(method, "/")[0], 405, method)
        self.assertEqual(self.http.send("GET", "/approvals/approve", headers={"Content-Length": None})[0], 404)
        self.assertEqual(self.http.send("GET", "/%2e%2e/etc/passwd", headers={"Content-Length": None})[0], 404)
        self.assertEqual(self.http.send("GET", "/", host="evil.invalid", headers={"Content-Length": None})[0], 403)
        self.assertEqual(self.http.post("/approvals/other", {"csrf_token": self.tok})[0], 404)
        self.assertEqual(self.http.post("/", {"csrf_token": self.tok})[0], 404)
        self.assertEqual(len(journal(self.project)), size)

    def test_strict_post_routes_and_bounds(self):
        ident = self.draft_row["id"]
        size = len(journal(self.project))
        t = self.tok
        base = f"csrf_token={t}&id={ident}&reason=why"
        cases = [
            (400, {"body": (base + "&reason=again").encode()}),                      # duplicate field
            (400, {"body": (base + "&actor=G").encode()}),                           # arbitrary actor field
            (400, {"body": (base + "&project=/tmp").encode()}),                      # arbitrary project field
            (400, {"body": f"csrf_token={t}&id={ident}".encode()}),                  # missing reason
            (400, {"body": f"csrf_token={t}&id={ident}&reason=%FF".encode()}),       # invalid UTF-8
            (400, {"body": f"csrf_token={t}&id={ident}&reason=a%00b".encode()}),     # NUL
            (400, {"body": f"csrf_token={t}&id=../x&reason=why".encode()}),          # invalid id
            (400, {"body": f"csrf_token={t}&id={ident}&reason=".encode()}),          # empty reason
            (415, {"body": base.encode(), "headers": {"Content-Type": "application/json"}}),
            (415, {"body": base.encode(), "headers": {"Content-Type": None}}),
            (411, {"body": base.encode(), "headers": {"Content-Length": None}}),
            (400, {"body": base.encode(), "headers": {"Content-Length": "abc"}}),
            (400, {"body": base.encode(), "headers": {"Content-Length": "-1"}}),
            (413, {"body": base.encode(), "headers": {"Content-Length": str(ap.MAX_BODY + 1)}}),
            (400, {"body": base.encode(), "headers": {"Transfer-Encoding": "chunked"}}),
            (400, {"body": base.encode(), "headers": {"Content-Length": str(len(base) + 5)}}),  # short body
        ]
        for want, kw in cases:
            self.assertEqual(self.http.send("POST", "/approvals/reject", **kw)[0], want, kw)
        # two Content-Length headers
        raw = (f"POST /approvals/reject HTTP/1.1\r\nHost: 127.0.0.1:{self.http.port}\r\n"
               f"Origin: http://127.0.0.1:{self.http.port}\r\nContent-Type: application/x-www-form-urlencoded\r\n"
               f"Content-Length: {len(base)}\r\nContent-Length: {len(base)}\r\n\r\n{base}").encode()
        self.assertEqual(self.http.send("POST", "/x", raw=raw)[0], 400)
        # a stalled body times out instead of blocking the single-threaded server
        with mock.patch.object(ap._Handler, "timeout", 0.3):
            stalled = self.http.send("POST", "/approvals/reject", headers={"Content-Length": "500"}, body=b"a=")
        self.assertIn(stalled[0], (400, 408))
        self.assertEqual(self.http.send("GET", "/", headers={"Content-Length": None})[0], 200)
        self.assertEqual(len(journal(self.project)), size)

    def test_logs_are_silent(self):
        with mock.patch.object(sys, "stderr") as err:
            self.http.send("GET", "/?secret=SENTINEL", headers={"Content-Length": None})
        err.write.assert_not_called()


class StaticAndCliTests(Base):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, "-B", "-m", "campaign_tool.records", "panel", *args], cwd=REPO,
                              capture_output=True, text=True, timeout=120)

    def test_static_render_cli_no_socket_or_actions(self):
        self.full()
        out = Path(self.tmp.name) / "snap.html"
        args = ["--project", str(self.project), "--run-id", RUN, "--run-report", str(self.project / "report.json"),
                "--as-of", AS_OF, "--no-system", "--render", str(out)]
        done = self.run_cli(*args)
        self.assertEqual(done.returncode, 0, done.stderr)
        first = out.read_bytes()
        self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o600)
        html = first.decode()
        self.assertEqual({"mail", "documents", "stages", "findings", "deadlines", "drafts"} - parse(html).ids, set())
        self.assertNotIn("<form", html)
        self.assertNotIn("csrf", html)
        self.assertNotIn("<textarea", html)
        self.assertIn("Static snapshot — decisions unavailable", html)
        self.assertEqual(self.run_cli(*args).returncode, 0)  # atomic replace of a regular file
        self.assertEqual(out.read_bytes(), first)  # deterministic with fixed inputs
        self.assertEqual([p.name for p in out.parent.iterdir() if p.name.startswith(".snap")], [])
        # refusals leave sources and any existing output untouched
        before = tree_state(self.project)
        link = Path(self.tmp.name) / "link.html"
        link.symlink_to(out)
        fifo = Path(self.tmp.name) / "fifo.html"
        os.mkfifo(fifo)
        for target in (link, fifo, self.queue, self.project / "report.json",
                       self.outputs / "received-items.json", self.ledger):
            bad = self.run_cli(*args[:-1], str(target))
            self.assertEqual(bad.returncode, 1, target)
        self.assertEqual(tree_state(self.project), before)
        self.assertEqual(out.read_bytes(), first)
        linked = Path(self.tmp.name) / "d"
        linked.symlink_to(self.tmp.name)
        self.assertEqual(self.run_cli(*args[:-1], str(linked / "x.html")).returncode, 1)

    def test_help_and_invalid_args_do_no_io(self):
        done = self.run_cli("--help")
        self.assertEqual(done.returncode, 0)
        self.assertIn("docs/RECORDS-ADMIN-PANEL.md", done.stdout)
        missing = Path(self.tmp.name) / "nope"
        for extra in (["--run-id", "../x"], ["--run-id", "."], ["--run-id", RUN, "--as-of", "2026-13-40"],
                      ["--run-id", RUN, "--port", "99999", "--actor", "me"], ["--run-id", RUN],
                      ["--run-id", RUN, "--actor", "bad\x07", "--port", "9"]):
            bad = self.run_cli("--project", str(missing), *extra)
            self.assertEqual(bad.returncode, 2, extra)
        self.assertFalse(missing.exists())

    def test_build_server_is_loopback_only(self):
        for kw in ({"port": -1}, {"port": 70000}, {"port": "80"}, {"actor": " "}, {"actor": "a\nb"}):
            args = {"actor": "me", "port": 0, **kw}
            with self.assertRaises(ap.PanelError):
                ap.build_server(self.project, RUN, **args)
        server = ap.build_server(self.project, RUN, actor="me")
        self.addCleanup(server.server_close)
        self.assertEqual(server.server_address[0], "127.0.0.1")
        with self.assertRaises(TypeError):
            ap.build_server(self.project, RUN, actor="me", host="0.0.0.0")


if __name__ == "__main__":
    unittest.main()
