"""records panel --export-root DIR --run-id auto --decisions-dir J: every mutating console decision is checked against the
pinned snapshot, journaled outside the export and shown as pending apply across export swaps (DFGL-16B)."""
import hashlib
import html
import http.client
import io
import json
import os
import stat
import unittest
import urllib.parse
from contextlib import redirect_stderr
from unittest import mock

from campaign_tool import approval
from campaign_tool.records import admin_panel as ap
from campaign_tool.records import approval_queue as aq
from campaign_tool.records import console_journal, console_manual_events, console_snapshot, mail_send
from campaign_tool.records import public_packets as pp
from campaign_tool.records import public_updates as pu
from tests.records.test_console_export import tree_state
from tests.records.test_console_model import RUN
from tests.records.test_console_snapshot import RUN2, RUN3, RUN4, OpenSpy, Panel

LINE = "a" * 16


def holding_line(dest):
    path = dest / "console-model" / "public-updates.json"
    doc = json.loads(path.read_text())
    doc.update(lines=[{"id": LINE, "state": "holding", "agency_key": "ex-pd", "text": "Policy released.",
                       "hold_until": 4102444800, "proposed_at": 1}])
    path.write_text(json.dumps(doc))


def set_manifest(**keys):
    def mutate(dest):
        path = dest / "EXPORT.json"
        path.write_text(json.dumps({**json.loads(path.read_text()), **keys}))
    return mutate


class Journal(Panel):
    def setUp(self):
        super().setUp()
        self.j = self.tmp / "journal"
        self.j.mkdir(mode=0o700)

    def up(self, **kw):
        super().up(decisions_dir=str(self.j), **kw)

    def lines(self):
        path = self.j / console_journal.FILE
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def decide(self, path, **fields):
        port = self.server.server_port
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            connection.request("POST", path, urllib.parse.urlencode({"csrf_token": self.server.csrf_token, **fields}),
                               {"Origin": "http://127.0.0.1:%d" % port, "Content-Type": "application/x-www-form-urlencoded"})
            response = connection.getresponse()
            return response.status, response.getheader("Location"), response.read().decode()
        finally:
            connection.close()

    def packet_revision(self, ident):
        return str(next(r for r in pp.pending(self.live) if r["id"] == ident)["revision"])


class Decisions(Journal):
    def test_a_decision_between_two_refreshes_persists_while_export_and_project_stay_unchanged(self):
        self.make_export()
        live_before = tree_state(self.live)
        self.up()
        root_before = tree_state(self.root)
        spy = OpenSpy(self.root)
        with mock.patch.object(os, "open", spy):
            status, location, _ = self.decide("/approvals/reject", id=self.pending_id, reason="not ours", **{"return": "review"})
            self.assertEqual((status, location), (303, "/review?id=" + self.pending_id))
            self.assertIn("pending apply: reject", self.get("/review?id=" + self.pending_id))
        self.assertEqual(spy.writes, [])
        self.assertEqual(tree_state(self.root), root_before)
        self.assertEqual(tree_state(self.live), live_before)
        (entry,) = self.lines()
        body = {k: v for k, v in entry.items() if k != "entry_id"}
        self.assertEqual(entry["entry_id"], hashlib.sha256(console_journal._canon(body).encode()).hexdigest())
        self.assertEqual((entry["schema"], entry["route"], entry["action"], entry["target_id"], entry["revision_seen"],
                          entry["value"], entry["actor"], entry["source_project_id"], entry["export_run_id"]),
                         ("records-console-decision-v1", "/approvals/reject", "reject", self.pending_id, 0, "not ours",
                          "operator", self.pid, RUN))
        self.assertEqual(stat.S_IMODE(os.stat(self.j / console_journal.FILE).st_mode), 0o600)
        name = self.make_export(RUN2)  # the next refresh: a new run, the journal is untouched
        for path in ("/review?id=" + self.pending_id, "/dashboard"):
            page = self.get(path)
            self.assertIn("pending apply: reject", page, path)
            self.assertIn(name, page)
        approved = self.get("/review?id=" + self.approved_id)
        self.assertIn("signature verified on the live project", approved)
        self.assertNotIn("pending apply", approved)
        self.assertEqual(len(self.lines()), 1)

    def test_refusals_match_the_writer_and_journal_nothing(self):
        self.make_export()
        self.up()
        edit = {"id": self.pending_id, "body": "new text", "expected_revision": "7"}
        status, _, page = self.decide("/approvals/edit", **edit)
        self.assertEqual(status, 409)
        self.assertIn("Draft changed", page)
        self.assertNotIn("new text", page)
        unknown = "f" * 64
        for path, fields, code in (("/approvals/reject", {"id": unknown, "reason": "x"}, 404),
                                   ("/approvals/approve", {"id": self.approved_id, "signature": "sig"}, 409),
                                   ("/approvals/reject", {"id": self.approved_id, "reason": "x"}, 409),
                                   ("/approvals/reject", {"id": self.pending_id, "reason": "x" * 2001}, 400),
                                   ("/approvals/edit", {**edit, "body": "x" * (aq.MAX_BODY + 1), "expected_revision": "0"}, 400),
                                   ("/approvals/reject", {"id": self.pending_id, "reason": "  "}, 400)):
            self.assertEqual(self.decide(path, **fields)[0], code, (path, fields))
        self.assertEqual(self.lines(), [])
        self.assertEqual(self.decide("/approvals/edit", **{**edit, "expected_revision": "0"})[0], 303)
        self.assertEqual([(e["action"], e["revision_seen"], e["value"]) for e in self.lines()], [("edit", 0, "new text")])
        self.assertIn("pending apply: edit", self.get("/review?id=" + self.pending_id))

    def test_the_approve_page_shows_the_live_payload_and_the_signature_is_journaled_verbatim(self):
        self.make_export()
        self.up()
        page = html.unescape(self.get("/review?id=" + self.pending_id))
        self.assertIn(aq.approval_payload(self.live, self.pending_id).decode(), page)
        self.assertIn("\nproject: " + self.pid + "\n", page)
        self.assertIn('<form method="post" action="/approvals/approve">', page)
        before = tree_state(self.root)
        signature = "-----BEGIN SSH SIGNATURE-----\nabc\n-----END SSH SIGNATURE-----\n"
        self.assertEqual(self.decide("/approvals/approve", id=self.pending_id, signature=signature)[0], 303)
        (entry,) = self.lines()
        self.assertEqual((entry["action"], entry["value"], entry["revision_seen"]), ("approve", signature, 0))
        self.assertEqual(tree_state(self.root), before)
        after = self.get("/review?id=" + self.pending_id)
        self.assertIn("pending apply: approve", after)
        self.assertNotIn("Approved by", after)
        self.assertEqual(next(r for r in aq.load_queue(self.live) if r["id"] == self.pending_id)["status"], "pending")

    def test_packet_and_oversight_decisions_are_journaled_not_applied(self):
        self.make_export(RUN3, holding_line)
        self.up()
        live_before = tree_state(self.live)
        rev = self.packet_revision(self.proposed)
        for path, fields, code in (
                ("/packets/approve", {"id": self.proposed, "revision": rev, "signature": "SIG"}, 303),
                ("/packets/reject", {"id": self.approved_packet, "revision": self.packet_revision(self.approved_packet),
                                     "reason": "no"}, 303),
                ("/packets/approve", {"id": self.approved_packet, "revision": "0", "signature": "SIG"}, 409),
                ("/packets/approve", {"id": self.proposed, "revision": "9", "signature": "SIG"}, 409),
                ("/packets/approve", {"id": "0" * 16, "revision": "0", "signature": "SIG"}, 409),
                ("/packets/reject", {"id": self.proposed, "revision": rev, "reason": "a\x07"}, 400),
                ("/packets/reject", {"id": self.proposed, "revision": rev, "reason": " " * 501 + "x"}, 400),
                ("/packets/reject", {"id": self.proposed, "revision": rev, "reason": "x" * 501}, 400),
                ("/public-updates/retract", {"id": "1" * 16}, 409),
                ("/public-updates/retract", {"id": "xyz"}, 400),
                ("/public-updates/retract", {"id": LINE}, 303),
                ("/public-updates/off", {}, 303), ("/public-updates/kill", {}, 303)):
            self.assertEqual(self.decide(path, **fields)[0], code, (path, fields))
        with mock.patch.object(console_journal, "_now", return_value=2 ** 33):  # past the packet's expiry
            self.assertEqual(self.decide("/packets/approve", id=self.proposed, revision=rev, signature="SIG")[0], 409)
        self.assertEqual([(e["action"], e["target_id"]) for e in self.lines()],
                         [("packet_approve", self.proposed), ("packet_reject", self.approved_packet),
                          ("retract", LINE), ("off", "switch"), ("kill", "switch")])
        packets, public = html.unescape(self.get("/packets")), html.unescape(self.get("/public-updates"))
        self.assertIn("pending apply: packet_approve", packets)
        self.assertIn("pending apply: packet_reject", packets)
        self.assertIn("pending apply: retract", public)
        self.assertIn("pending apply: kill (" + console_journal.LATER, public)
        self.assertEqual({r["id"]: r["status"] for r in pp.pending(self.live)},
                         {self.proposed: "proposed", self.approved_packet: "approved"})
        self.assertEqual(pu.switch_state(self.live, read_only=True), "OFF")
        self.assertEqual(tree_state(self.live), live_before)

    def test_send_is_not_found_and_no_writer_or_live_reader_runs_in_journal_mode(self):
        self.make_export()
        self.up()
        boom = AssertionError("a writer or live reader ran")
        targets = [(aq, n) for n in ("approve", "reject", "edit", "load_queue", "approval_payload")]
        targets += [(pp, n) for n in ("approve", "decide", "payload", "pending", "status")]
        targets += [(pu, n) for n in ("retract", "turn_off", "kill")]
        targets += [(approval, "verify"), (mail_send, "send_one"), (console_manual_events, "append")]
        for module, name in targets:
            patch = mock.patch.object(module, name, side_effect=boom)
            patch.start()
            self.addCleanup(patch.stop)
        spy = OpenSpy(self.root)
        with mock.patch.object(os, "open", spy):
            for path in ("/", "/dashboard", "/next-steps", "/review?id=" + self.pending_id, "/packets", "/oversight",
                         "/public-updates"):
                self.get(path)
            self.get("/send?id=" + self.approved_id, 404)
            self.assertEqual(self.decide("/send", id=self.approved_id)[0], 404)
            self.assertEqual(self.decide("/approvals/reject", id=self.pending_id, reason="no")[0], 303)
            self.assertEqual(self.decide("/packets/reject", id=self.proposed, revision="0", reason="no")[0], 303)
            self.assertEqual(self.decide("/public-updates/off")[0], 303)
        self.assertEqual(spy.writes, [])
        self.assertGreater(spy.opened, 20)
        self.assertEqual(len(self.lines()), 3)

    def test_applied_entries_drop_out_and_refused_ones_show_their_code(self):
        self.make_export()
        self.up()
        self.decide("/approvals/reject", id=self.pending_id, reason="no")
        self.decide("/approvals/edit", id=self.pending_id, body="text", expected_revision="0")
        first, second = (e["entry_id"] for e in self.lines())
        self.make_export(RUN2, set_manifest(applied_journal_ids=[first]))
        self.assertIn("pending apply: edit", self.get("/review?id=" + self.pending_id))
        self.make_export(RUN3, set_manifest(applied_journal_ids=[first, second]))
        self.assertNotIn("pending apply", self.get("/review?id=" + self.pending_id))
        self.make_export(RUN4, set_manifest(applied_journal_ids=[first], apply_outcomes={second: {"refusal": "stale_revision"}}))
        self.assertIn("not applied: stale_revision", self.get("/review?id=" + self.pending_id))

    def test_an_unreadable_journal_fails_closed(self):
        self.make_export()
        self.up()
        (self.j / console_journal.FILE).write_text("not json\n")
        for path in ("/", "/review?id=" + self.pending_id):
            self.assertIn("journal_unavailable", self.get(path, 503))
        self.assertEqual(self.decide("/approvals/reject", id=self.pending_id, reason="no")[0], 503)
        self.assertEqual((self.j / console_journal.FILE).read_text(), "not json\n")

    def test_a_journal_past_its_size_cap_is_refused_not_grown_or_crashed_on(self):
        self.make_export()
        self.up()
        self.assertEqual(self.decide("/approvals/reject", id=self.pending_id, reason="no")[0], 303)
        with mock.patch.object(console_journal, "MAX_JOURNAL", 50):
            self.assertIn("journal_unavailable", self.get("/", 503))  # reading refuses an oversize file
            with self.assertRaises(console_journal.JournalError):
                console_journal.append(str(self.j), console_snapshot.Pinner(str(self.root)).pin(), "a", "/x", "reject", "t", 0, "v")
        self.assertEqual(len(self.lines()), 1)

    def test_without_a_journal_every_post_stays_refused_and_nothing_is_journaled(self):
        self.make_export()
        Panel.up(self)
        self.assertEqual(self.decide("/approvals/reject", id=self.pending_id, reason="no")[:2], (503, None))
        self.assertNotIn("<form", self.get("/review?id=" + self.pending_id))
        self.assertEqual(self.lines(), [])


class StartRefusals(Journal):
    def refused(self, code, **kw):
        with self.assertRaises(ap.PanelError) as caught:
            ap.build_server(run_id="auto", actor="operator", system=False, export_root=str(self.root), **kw)
        self.assertEqual(caught.exception.code, code)

    def test_the_journal_needs_an_export_and_a_private_real_directory_outside_it(self):
        with self.assertRaises(ap.PanelError) as caught:
            ap.build_server(str(self.live), RUN, actor="operator", system=False, decisions_dir=str(self.j))
        self.assertEqual(caught.exception.code, "journal_needs_export")
        inside = self.root / "journal"
        inside.mkdir(mode=0o700)
        around = self.tmp / "around"
        around.mkdir(mode=0o700)
        link = self.tmp / "link"
        link.symlink_to(self.j)
        loose = self.tmp / "loose"
        loose.mkdir(mode=0o750)
        os.chmod(loose, 0o750)
        for path in (str(inside), str(self.root), str(link), str(loose), "relative", str(self.tmp / "absent")):
            self.refused("decisions_dir_invalid", decisions_dir=path)
        self.assertEqual(console_journal.check_dir(str(self.j), str(self.root)), None)

    def test_command_line_refusals(self):
        base = ["--run-id", "auto", "--actor", "operator", "--port", "1"]
        for args, code in ((["--project", str(self.live), "--decisions-dir", str(self.j), "--run-id", RUN, "--actor", "o"],
                            "journal_needs_export"),
                           (["--export-root", str(self.root), "--decisions-dir", str(self.root / "j")] + base,
                            "decisions_dir_invalid")):
            with redirect_stderr(io.StringIO()) as err:
                self.assertEqual(ap.main(args), 2, args)
            self.assertIn(code, err.getvalue())


if __name__ == "__main__":
    unittest.main()
