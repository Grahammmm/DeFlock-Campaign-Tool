"""records panel --export-root DIR --run-id auto: one pinned real export per request, read through the
nonmutating snapshot readers over its console-model (DFGL-16A)."""
import hashlib
import html
import http.client
import io
import json
import os
import re
import sqlite3
import threading
import unittest
import urllib.parse
from contextlib import closing, redirect_stderr
from unittest import mock

from campaign_tool.records import admin_panel as ap
from campaign_tool.records import approval_queue as aq
from campaign_tool.records import console_export, console_layout, console_manual_events, console_snapshot, mail_send
from campaign_tool.records import public_packets as pp
from campaign_tool.records import public_updates as pu
from campaign_tool.records.ledger import store
from tests.records.test_console_export import tree_state, unlock
from tests.records.test_console_model import RUN, SEND_ROW, LiveBase

RUN2, RUN3, RUN4 = (RUN + "-" + c for c in "234")
WRITE = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC


class OpenSpy:
    """Replaces os.open for a whole test and records every writing open below root (relative opens resolved)."""

    def __init__(self, root):
        self.root, self.real, self.writes, self.opened = str(root), os.open, [], 0

    def __call__(self, path, flags, mode=0o777, *, dir_fd=None):
        base = os.readlink("/proc/self/fd/%d" % dir_fd) if dir_fd is not None else ""
        full = os.path.join(base, os.fsdecode(path))
        if full.startswith(self.root):
            self.opened += 1
            if flags & WRITE:
                self.writes.append((full, flags))
        return self.real(path, flags, mode, dir_fd=dir_fd)


class Panel(LiveBase):
    def setUp(self):
        super().setUp()
        for run in (RUN2, RUN3, RUN4):
            (self.live / "run-outputs" / run).mkdir()
            (self.live / "run-outputs" / run / "text-state.json").write_text("{}")
        store.initialize(self.live / "processing-v1" / "ledger.sqlite")
        self.root = self.tmp / "root"
        self.root.mkdir(mode=0o700)
        self.addCleanup(unlock, self.root)
        self.pid = aq._project_id(self.live)
        self.server = None

    def make_export(self, run=RUN, mutate=None):
        """Export the live project and install it under the root; mutate(dest) may alter it before sealing."""
        dest = self.tmp / ("dest%d" % len(list(self.tmp.glob("dest*"))))
        dest.mkdir(mode=0o700)
        console_export.build(str(self.live), run, str(dest))
        if mutate:
            mutate(dest)
            manifest = json.loads((dest / "EXPORT.json").read_text())
            manifest["files"] = {str(p.relative_to(dest)): hashlib.sha256(p.read_bytes()).hexdigest()
                                 for p in sorted(dest.rglob("*")) if p.is_file() and p.name != "EXPORT.json"}
            (dest / "EXPORT.json").write_text(json.dumps(manifest))
        buf = io.BytesIO()
        console_export.write_tar(str(dest), buf)
        buf.seek(0)
        return console_export.install(str(self.root), buf)

    def up(self, **kw):
        self.server = ap.build_server(export_root=str(self.root), run_id="auto", actor="operator", system=False, **kw)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (self.server.shutdown(), self.server.server_close(), thread.join()))

    def request(self, method, path, fields=None):
        origin = "http://127.0.0.1:%d" % self.server.server_port
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        try:
            connection.request(method, path, urllib.parse.urlencode(fields) if fields is not None else None,
                               {"Origin": origin, "Content-Type": "application/x-www-form-urlencoded"})
            response = connection.getresponse()
            return response.status, response.read().decode()
        finally:
            connection.close()

    def get(self, path, status=200):
        code, page = self.request("GET", path)
        self.assertEqual(code, status, (path, page[:300]))
        return page

    def drop_queue(self, dest):
        (dest / "console-model" / "queue.json").unlink()

    def foreign_queue(self, dest):
        path = dest / "console-model" / "queue.json"
        path.write_text(json.dumps({**json.loads(path.read_text()), "source_project_id": "0" * 16}))


class ServedExport(Panel):
    def test_home_review_and_packets_show_the_exported_data_read_only_and_change_nothing(self):
        self.make_export()
        spy = OpenSpy(self.root)
        with mock.patch.object(os, "open", spy):
            self.up()
            before = tree_state(self.root)
            home = self.get("/")
            self.assertIn("Follow-ups 1", home)  # the one pending draft
            self.assertIn("Packets 1", home)  # the proposed packet awaits a decision
            self.assertIn("/review?id=" + self.pending_id, home)
            for source in ("approval queue", "update packets", "sent-mail results", "public updates"):
                self.assertNotIn(source + ": unavailable", home)
            pending = html.unescape(self.get("/review?id=" + self.pending_id))
            self.assertIn(aq.approval_payload(self.live, self.pending_id).decode(), pending)
            self.assertIn("\nproject: " + self.pid + "\n", pending)
            approved = self.get("/review?id=" + self.approved_id)
            self.assertIn("Approved by owner@example.invalid: signature verified on the live project (%s)" % self.pid,
                          approved)
            self.assertIn(self.signer.principal, approved)
            evidence = next(r for r in aq.load_queue(self.live) if r["id"] == self.approved_id)["approval_evidence"]
            self.assertIn("signature sha256 " + evidence["signature_sha256"][:12], approved)
            self.assertIn("Approved, not sent", approved)  # the mail kind reads the exported send results
            for page in (pending, approved):
                for tag in ("<form", "<button", "csrf_token"):
                    self.assertNotIn(tag, page)
                self.assertIn("export " + os.listdir(self.root / "exports")[0], page)
                self.assertIn("Run " + RUN, page)
            packets = html.unescape(self.get("/packets"))
            self.assertIn(pp.payload(self.live, self.proposed).decode(), packets)
            self.assertNotIn("<form", packets)
            for row in pp.pending(self.live):
                self.assertIn(row["id"], packets)
            for path in ("/dashboard", "/next-steps", "/oversight", "/public-updates", "/settings", "/calendar",
                         "/board", "/documents"):
                page = self.get(path)
                self.assertNotIn("<form", page, path)
            self.assertIn(self.pending_id[:12], self.get("/dashboard"))
            self.get("/next-steps")
        self.assertGreater(spy.opened, 20)  # the wrapper saw the model reads, so an empty write list means something
        self.assertEqual(spy.writes, [])
        self.assertEqual(tree_state(self.root), before)
        for _, _, names in os.walk(self.root):
            self.assertEqual([n for n in names if n.endswith(("-journal", "-wal", "-shm"))], [])

    def test_every_post_is_refused_before_any_writer_runs(self):
        self.make_export()
        self.up()
        before = tree_state(self.root)
        token = self.server.csrf_token
        calls = [(aq, "approve"), (aq, "reject"), (aq, "edit"), (pp, "approve"), (pp, "decide"), (pu, "retract"),
                 (pu, "turn_off"), (pu, "kill"), (mail_send, "send_one"), (console_manual_events, "append_events"),
                 (console_manual_events, "append")]
        boom = AssertionError("a writer ran")
        with mock.patch.multiple(aq, approve=mock.DEFAULT, reject=mock.DEFAULT, edit=mock.DEFAULT) as writers:
            for fn in writers.values():
                fn.side_effect = boom
            patches = [mock.patch.object(m, n, side_effect=boom) for m, n in calls[3:]]
            for patch in patches:
                patch.start()
                self.addCleanup(patch.stop)
            for path, fields in (
                    ("/approvals/approve", {"id": self.pending_id, "signature": "x"}),
                    ("/approvals/reject", {"id": self.pending_id, "reason": "no"}),
                    ("/approvals/edit", {"id": self.pending_id, "body": "b", "expected_revision": "0"}),
                    ("/packets/approve", {"id": self.proposed, "revision": "0", "signature": "x"}),
                    ("/packets/reject", {"id": self.proposed, "revision": "0", "reason": "no"}),
                    ("/public-updates/off", {}), ("/public-updates/kill", {}),
                    ("/public-updates/retract", {"id": "0" * 16}), ("/send", {"id": self.approved_id}),
                    ("/manual-event", {"id": "0" * 32, "ticket": "t"}), ("/holds", {})):
                status, page = self.request("POST", path, {"csrf_token": token, **fields})
                self.assertEqual(status, 503, path)
                self.assertIn("read_only_export", page)
            for fn in writers.values():
                fn.assert_not_called()
        self.assertEqual(tree_state(self.root), before)

    def test_no_export_yet_is_503_and_a_missing_queue_or_foreign_model_is_unavailable_not_empty(self):
        self.up()
        with redirect_stderr(io.StringIO()):
            self.assertIn("export_unavailable", self.get("/", 503))
        self.make_export(RUN3, self.drop_queue)
        home = self.get("/")
        self.assertIn("console_model_missing", home)
        self.assertNotIn("0 waiting", home)
        self.assertIn("console_model_missing", self.get("/dashboard"))
        self.get("/review?id=" + self.pending_id, 503)
        self.make_export(RUN4, self.foreign_queue)
        self.assertIn("console_model_mismatch", self.get("/"))
        self.get("/review?id=" + self.pending_id, 503)
        self.make_export(RUN2, lambda dest: (dest / "console-model" / "queue.json").write_text("[]"))
        self.assertIn("console_model_invalid", self.get("/"))
        self.get("/review?id=" + self.pending_id, 503)

    def test_an_unavailable_packets_model_is_unavailable_never_an_empty_list(self):
        (self.live / pp.JOURNAL).write_text("not json\n")
        self.make_export()
        self.up()
        self.assertIn("update packets: unavailable", self.get("/"))
        self.get("/packets", 503)

    def test_a_swap_during_a_request_cannot_mix_exports_and_the_next_request_follows_without_restart(self):
        self.make_export()
        self.up()
        original, swapped = console_snapshot.SnapshotSource.queue_rows, []

        def swapping(source):
            if not swapped:
                swapped.append(None)  # claim the swap first: the export build now reads the copy through queue_rows too
                swapped[0] = self.make_export(RUN2)  # `current` now points at the second export
            return original(source)

        with mock.patch.object(console_snapshot.SnapshotSource, "queue_rows", swapping):
            first = self.get("/")
        self.assertEqual(len(swapped), 1)
        self.assertIn("Run " + RUN + ",", first)
        self.assertNotIn(RUN2, first)
        second = self.get("/")
        self.assertIn("Run " + RUN2 + ",", second)
        self.assertIn(swapped[0], second)

    def test_an_invalid_new_export_keeps_serving_the_last_good_one(self):
        name = self.make_export()
        self.up()
        self.assertIn(name, self.get("/"))
        bad = self.root / "exports" / ("20991231T235959Z-" + RUN2)
        bad.mkdir(mode=0o700)
        (bad / "EXPORT.json").write_text("{not json")
        os.chmod(bad, 0o500)
        os.symlink("exports/" + bad.name, self.root / ".t")
        os.replace(self.root / ".t", self.root / "current")
        with redirect_stderr(io.StringIO()) as err:
            self.assertIn(name, self.get("/"))
        self.assertIn("export_pin_failed", err.getvalue())

    def test_a_tampered_export_file_fails_the_hash_check(self):
        name = self.make_export()
        unlock(self.root)
        path = self.root / "exports" / name / "console-model" / "queue.json"
        path.write_text(path.read_text().replace("pending", "approved"))
        pinner = console_snapshot.Pinner(str(self.root))
        with self.assertRaises(console_snapshot.SnapshotError), redirect_stderr(io.StringIO()) as err:
            pinner.pin()
        self.assertIn("export_hash_mismatch", err.getvalue())


CANARY = "/srv/canary-agency/jane.doe@example.org/secret.pdf"
# (n, preserved, alert key tail, alert count, run-list row): one held document per non-EXTRACTED outcome, plus an unlisted
# detail canary (3), a two-detail row (2) and a retryable error still below its attempt limit (12).
DOCS = ((1, True, "ocr_runtime_unconfigured", 1, None),
        (2, True, None, 0, ("CORRUPT_DOCUMENT", ["no_text_after_ocr", "invalid_or_bounded_parser_output"])),
        (3, True, None, 0, ("EXTRACT_INCOMPLETE", ["zz_canary_code"])),
        (4, True, None, 0, ("WORKBOOK_POLICY_REQUIRED", ["workbook_policy_refused"])),
        (5, True, None, 0, ("PARSER_MISSING", ["parser_missing"])),
        (6, True, None, 0, ("DECODER_NEEDED", ["decoder_needed"])),
        (7, True, "extraction_timeout", 3, None),
        (8, True, "extraction_check_refused:accept_status_rejected", 1, None),
        (9, True, "extraction_stage_error", 3, None),
        (10, False, None, 0, None),
        (11, True, None, 0, ("DEFERRED_RUN_BUDGET", ["deferred_run_budget"])),
        (12, True, "extraction_stage_error", 1, ("EXTRACT_ERROR", ["extraction_stage_error"])))
RETRY, WAIT, SCOPE = "retry with records holds release (attended)", "wait for the next run", "G scope decision"
# (prefix, outcome, detail code, attempts, route, next step), sorted by outcome order then prefix.
ROWS = (("111111111111", "AWAITING_OCR", "ocr_runtime_unconfigured", 1, None, "review OCR readiness"),
        ("222222222222", "CORRUPT_DOCUMENT", "invalid_or_bounded_parser_output,no_text_after_ocr", 0, None, "review required"),
        ("333333333333", "EXTRACT_INCOMPLETE", "unlisted", 0, None, "review required"),
        ("444444444444", "WORKBOOK_POLICY_REQUIRED", "workbook_policy_refused", 0, None, "review required"),
        ("555555555555", "PARSER_MISSING", "parser_missing", 0, None, "operator checks parser availability"),
        ("666666666666", "DECODER_NEEDED", "decoder_needed", 0, None, "review required"),
        ("777777777777", "EXTRACT_TIMEOUT", "extraction_timeout", 3, None, RETRY),
        ("888888888888", "EXTRACT_NOT_ACCEPTED", "accept_status_rejected", 1, None, "review required"),
        ("999999999999", "EXTRACT_ERROR", "extraction_stage_error", 3, None, RETRY),
        ("cccccccccccc", "EXTRACT_ERROR", "extraction_stage_error", 1, None, WAIT),
        ("aaaaaaaaaaaa", "PRESERVE_PENDING", "preserve_pending", 0, "R5", SCOPE),
        ("bbbbbbbbbbbb", "DEFERRED_RUN_BUDGET", "deferred_run_budget", 0, None, WAIT))


class HoldsView(Panel):
    """DF2222-07: console-model/holds.json and the read-only GET /holds page, live and from a pinned export."""

    def seed(self):
        """A synthetic ledger holding one document per outcome, with the extract outcome list of the run."""
        processing = self.live / "processing-v1"
        (processing / "runs").mkdir(mode=0o700)
        (self.live / "mail-history" / "objects").mkdir(parents=True)
        listed = []
        with closing(sqlite3.connect(processing / "ledger.sqlite")) as db:
            db.execute("INSERT INTO runs VALUES ('r1','records-runner','2026-11-01T00:00:00Z',NULL,'e',NULL,NULL,NULL,'completed','{}')")
            for n, preserved, tail, count, row in DOCS:
                sha = f"{n:x}" * 64
                db.execute("INSERT INTO originals(sha256,bytes,first_seen_at,role,scope,storage_path,preservation_status,"
                           "provenance_json) VALUES (?,1,'2026-10-01T00:00:00+00:00','agency_original','in_scope',?,?,?)",
                           (sha, CANARY if preserved else None, "preserved" if preserved else "pending",
                            json.dumps({"subject": CANARY})))
                db.execute("INSERT INTO stage_state VALUES (?,'extract','pending',NULL,'runtime','t','r1',NULL)", (sha,))
                db.execute("INSERT INTO stage_state VALUES (?,'preserve',?,?,'runtime','t','r1',NULL)",
                           (sha, "done" if preserved else "pending", "x" if preserved else None))
                if tail:
                    db.execute("INSERT INTO alerts VALUES (?,?,'2026-11-01T07:00:00+00:00','2026-11-13T07:00:00+00:00',?,"
                               "'runtime','open')", (sha[-32:], f"extract:{sha}:{tail}", count))
                if row:
                    listed.append({"original_sha256": sha, "outcome": row[0], "issue_codes": row[1]})
            db.commit()
        (processing / "runs" / f"extract-{RUN}.json").write_text(json.dumps(
            {"schema": "records-extract-outcomes-v1", "run_id": RUN, "documents": listed}))

    def live_page(self):
        self.server = ap.build_server(str(self.live), RUN, actor="operator", system=False)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda server=self.server: (server.shutdown(), server.server_close(), thread.join()))
        return self.get("/holds")

    def tables(self, page):
        return re.findall(r"<table>.*?</table>", page, re.S)

    def holds_json(self):
        return json.loads((self.root / "current" / "console-model" / "holds.json").read_text())

    def test_golden_model_and_page_live_equals_export(self):
        self.seed()
        live = self.live_page()
        target = "/holds?outcome=EXTRACT_ERROR"
        live_filtered = self.get(target)
        self.make_export()
        model = self.holds_json()
        items = [{"sha256_prefix": p, "code": c, "detail_code": d, "attempts": a, "route": r} for p, c, d, a, r, _ in ROWS]
        self.assertEqual(model, {
            "schema": console_export.MODEL_SCHEMA, "source_project_id": self.pid, "state": "ok", "items": items,
            "counts": {"AWAITING_OCR": 1, "CORRUPT_DOCUMENT": 1, "EXTRACT_INCOMPLETE": 1, "WORKBOOK_POLICY_REQUIRED": 1,
                       "PARSER_MISSING": 1, "DECODER_NEEDED": 1, "EXTRACT_TIMEOUT": 1, "EXTRACT_NOT_ACCEPTED": 1,
                       "EXTRACT_ERROR": 2, "PRESERVE_PENDING": 1, "DEFERRED_RUN_BUDGET": 1},
            "detail_counts": {"AWAITING_OCR": {"ocr_runtime_unconfigured": 1},
                              "CORRUPT_DOCUMENT": {"invalid_or_bounded_parser_output": 1, "no_text_after_ocr": 1},
                              "EXTRACT_INCOMPLETE": {"unlisted": 1}, "WORKBOOK_POLICY_REQUIRED": {"workbook_policy_refused": 1},
                              "PARSER_MISSING": {"parser_missing": 1}, "DECODER_NEEDED": {"decoder_needed": 1},
                              "EXTRACT_TIMEOUT": {"extraction_timeout": 1}, "EXTRACT_NOT_ACCEPTED": {"accept_status_rejected": 1},
                              "EXTRACT_ERROR": {"extraction_stage_error": 2}, "PRESERVE_PENDING": {"preserve_pending": 1},
                              "DEFERRED_RUN_BUDGET": {"deferred_run_budget": 1}}})  # CORRUPT: one row, two detail counts
        self.assertTrue(all(set(i) == {"sha256_prefix", "code", "detail_code", "attempts", "route"}
                            and re.fullmatch(r"[0-9a-f]{12}", i["sha256_prefix"]) for i in items))
        self.up()
        page = self.get("/holds")
        for text in (json.dumps(model), page, live):
            self.assertNotRegex(text, r"[0-9a-f]{64}")
            for canary in ("/srv", "canary", "jane.doe", "secret.pdf", "zz_canary_code"):
                self.assertNotIn(canary, text)
        for tag in ("<form", "<button", "csrf_token"):
            self.assertNotIn(tag, page)
        self.assertIn("read only; the coordinator owns these documents", page)
        self.assertLess(page.index("Totals"), page.index("<h2>AWAITING_OCR</h2>"))
        order = [page.index(">%s<" % row[0]) for row in ROWS]
        self.assertEqual(order, sorted(order))
        for row in ROWS:
            self.assertIn("<td>%s</td>" % row[5], page)
        self.assertEqual(self.tables(page), self.tables(live))  # live and export render the same rows
        # A filter must not narrow the totals or leave unrelated detail tables in either reader.
        filtered = self.get(target)
        totals, errors = self.tables(filtered)
        self.assertEqual([totals, errors], self.tables(live_filtered))
        self.assertEqual(totals, self.tables(page)[0])
        self.assertIn(f'<a href="{target}">EXTRACT_ERROR</a>', totals)
        self.assertIn('<a href="/holds">Clear filter</a>', filtered)
        self.assertIn('<a href="/holds">Clear filter</a>', live_filtered)
        for text in (filtered, live_filtered):
            self.assertIn("<h2>EXTRACT_ERROR</h2>", text)
            self.assertNotIn("aaaaaaaaaaaa", text)
        self.assertIn("<td>999999999999</td>", errors)
        self.assertIn("<td>cccccccccccc</td>", errors)

    def test_missing_invalid_or_empty_is_unavailable_never_none(self):
        self.make_export(RUN2)
        self.up()
        self.assertIn("No held documents.", self.get("/holds"))  # control: an exported empty listing is a real zero
        self.assertIn("No held documents.", self.get("/holds?outcome=EXTRACT_ERROR"))
        self.seed()

        def edit(change):
            def mutate(dest):
                path = dest / "console-model" / "holds.json"
                doc = json.loads(path.read_text())
                path.write_text(json.dumps(change(doc)))
            return mutate
        item = lambda doc: doc["items"][0]  # noqa: E731
        cases = {"missing": lambda dest: (dest / "console-model" / "holds.json").unlink(),
                 "extra field": edit(lambda doc: item(doc).update(path=CANARY) or doc),
                 "long hash": edit(lambda doc: item(doc).update(sha256_prefix="1" * 64) or doc),
                 "bad outcome": edit(lambda doc: item(doc).update(code="EXTRACTED") or doc),
                 "bad count": edit(lambda doc: doc["counts"].update(AWAITING_OCR=True) or doc),
                 "total off": edit(lambda doc: doc["counts"].update(EXTRACT_ERROR=1) or doc),
                 "foreign project": edit(lambda doc: {**doc, "source_project_id": "0" * 16}),
                 "unavailable": edit(lambda doc: {**doc, "state": "unavailable", "code": "holds_unreadable", "items": []})}
        for n, (name, mutate) in enumerate(cases.items()):
            run = RUN + "-%d" % (n + 5)
            (self.live / "run-outputs" / run).mkdir()
            (self.live / "run-outputs" / run / "text-state.json").write_text("{}")
            self.make_export(run, mutate)
            page = self.get("/holds")
            self.assertIn("holds: unavailable", page, name)
            for text in ("No held documents.", "<table>", CANARY, "AWAITING_OCR"):
                self.assertNotIn(text, page, name)
            self.get("/oversight")  # the rest of the console still serves

    def test_post_to_holds_is_refused_on_a_write_capable_panel_and_changes_nothing(self):
        self.seed()
        self.live_page()
        before = tree_state(self.live)
        fields = {"csrf_token": self.server.csrf_token}
        for path in ("/holds", "/holds?outcome=EXTRACT_ERROR"):
            with self.subTest(path=path):
                self.assertEqual(self.request("POST", path, fields)[0], 404)

        # Reject ambiguity (including blank fields parse_qs can otherwise drop) and never echo rejected input.
        for query in ("outcome=NOT_AN_OUTCOME", "outcome=EXTRACTED", "outcome=extract_error", "outcome=", "outcome",
                      "outcome=+", "outcome=EXTRACT_ERROR&outcome=EXTRACT_ERROR",
                      "outcome=EXTRACT_ERROR&outcome=PRESERVE_PENDING", "outcome=&outcome=EXTRACT_ERROR",
                      "outcome=EXTRACT_ERROR&%6Futcome=", "outcome=EXTRACT_ERROR&id=", "id=1",
                      "outcome=EXTRACT_ERROR&id", "outcome=EXTRACT_ERROR&", "outcome=EXTRACT_ERROR;id=1",
                      "outcome=EXTRACT_ERROR%ZZ", "outcome=%FF", "outcome=%3Cdfg3-canary%3E"):
            with self.subTest(query=query):
                self.assertNotIn("dfg3-canary", self.get("/holds?" + query, status=400))
        self.assertIn("<h2>EXTRACT_ERROR</h2>", self.get("/holds?outcome=EXTRACT%5FERROR"))
        self.assertEqual(tree_state(self.live), before)


class SendResults(Panel):
    """DFGL-16R design 2: an unavailable sends.json stays an error and fails the review page closed."""

    def check(self, code):
        self.make_export()
        self.up()
        source = console_snapshot.SnapshotSource(console_snapshot.Pinner(str(self.root)).pin())
        with self.assertRaises(mail_send.MailSendError) as caught:
            source.send_results()
        self.assertEqual(caught.exception.code, code)
        self.get("/review?id=" + self.approved_id, 503)
        self.get("/review?id=" + self.pending_id)  # a pending row never reads the send results

    def test_valid_row_then_non_json_line(self):
        self.write_sends(json.dumps(SEND_ROW).encode() + b"\nnot json\n")
        self.check("send_results_invalid")

    def test_unreadable_journal(self):
        copy = self.live / "approvals" / "copy.jsonl"
        copy.write_bytes(json.dumps(SEND_ROW).encode() + b"\n")
        os.chmod(copy, 0o600)
        self.send_journal().unlink()
        self.send_journal().symlink_to(copy)
        self.check("send_io")

    def home_and_calendar(self, mutate=None):
        self.make_export(RUN3, mutate)
        self.up()
        home, calendar = self.get("/"), self.get("/calendar")
        return home, calendar

    def drop_sends(self, dest):
        (dest / "console-model" / "sends.json").unlink()

    def check_unavailable(self, home, calendar, wording):
        self.assertIn("sent-mail results: unavailable. " + wording, html.unescape(home))
        self.assertIn("unknown need your decision", home)  # never a numeric count over unreadable send state
        self.assertNotRegex(home, r"\d+ need your decision")
        self.assertIn(console_layout.unavailable_note("follow-up planned sends", "sends_unreadable"), calendar)

    def test_home_and_calendar_show_a_missing_sends_document_unavailable_without_send_config(self):
        """Export mode refuses send_config; the snapshot's own sends state still reaches Home and the calendar."""
        home, calendar = self.home_and_calendar(self.drop_sends)
        self.check_unavailable(home, calendar, console_layout.reason("console_model_missing"))

    def test_home_and_calendar_show_an_invalid_sends_document_unavailable(self):
        home, calendar = self.home_and_calendar(lambda dest: (dest / "console-model" / "sends.json").write_text("[]"))
        self.check_unavailable(home, calendar, console_layout.reason("console_model_invalid"))

    def test_home_and_calendar_show_an_exported_unavailable_send_journal(self):
        self.write_sends(json.dumps(SEND_ROW).encode() + b"\nnot json\n")
        home, calendar = self.home_and_calendar()
        self.check_unavailable(home, calendar, console_layout.reason("send_results_invalid"))

    def test_no_journal_is_an_empty_list_and_the_approved_row_renders(self):
        self.send_journal().unlink()
        self.make_export()
        self.up()
        source = console_snapshot.SnapshotSource(console_snapshot.Pinner(str(self.root)).pin())
        self.assertEqual(source.send_results(), [])
        self.assertIn("Approved, not sent", self.get("/review?id=" + self.approved_id))


class StartRefusals(Panel):
    def refused(self, **kw):
        with self.assertRaises(ap.PanelError) as caught:
            ap.build_server(actor="operator", system=False, **kw)
        self.assertEqual(caught.exception.code, "export_mode_invalid")

    def test_export_mode_is_exclusive_and_needs_a_real_private_root(self):
        self.refused(project=str(self.live), run_id="auto", export_root=str(self.root))
        self.refused(run_id=RUN, export_root=str(self.root))
        self.refused(run_id="auto", export_root=str(self.root), send_config="/x/send.json")
        self.refused(run_id="auto", export_root=str(self.root), mail_drafts_config="/x/drafts.json")
        self.refused(project=str(self.live), run_id="auto")
        link = self.tmp / "link"
        link.symlink_to(self.root)
        self.refused(run_id="auto", export_root=str(link))
        self.refused(run_id="auto", export_root="relative")
        os.chmod(self.root, 0o750)
        self.refused(run_id="auto", export_root=str(self.root))
        os.chmod(self.root, 0o700)
        inside = self.root / "events"
        inside.mkdir(mode=0o700)
        with self.assertRaises(ap.PanelError) as caught:
            ap.build_server(run_id="auto", export_root=str(self.root), actor="operator", manual_events_dir=str(inside))
        self.assertEqual(caught.exception.code, "manual_events_dir_invalid")

    def test_command_line_refusals(self):
        base = ["--run-id", "auto", "--actor", "operator", "--port", "1"]
        for args in (["--project", str(self.live), "--export-root", str(self.root)] + base,
                     ["--export-root", str(self.root), "--run-id", RUN],
                     ["--export-root", str(self.root), "--send-config", "/x"] + base,
                     ["--export-root", str(self.root), "--render", str(self.tmp / "out.html")] + base,
                     ["--export-root", str(self.root), "--mail-drafts-config", "/x"] + base,
                     ["--run-id", "auto"]):
            with redirect_stderr(io.StringIO()) as err:
                self.assertEqual(ap.main(args), 2, args)
            self.assertIn("export_mode_invalid", err.getvalue())


if __name__ == "__main__":
    unittest.main()
