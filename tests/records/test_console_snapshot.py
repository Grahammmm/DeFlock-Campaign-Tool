"""records panel --export-root DIR --run-id auto: one pinned real export per request, read through the
nonmutating snapshot readers over its console-model (DFGL-16A)."""
import hashlib
import html
import http.client
import io
import json
import os
import threading
import unittest
import urllib.parse
from contextlib import redirect_stderr
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
                    ("/manual-event", {"id": "0" * 32, "ticket": "t"})):
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
                swapped.append(self.make_export(RUN2))  # `current` now points at the second export
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
        self.assertIn("follow-up planned sends: unavailable (sends_unreadable)", calendar)

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
