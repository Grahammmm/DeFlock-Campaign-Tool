"""DF2007-02: a wrong message tie is corrected by a signed, append-only replacement line (supersedes + message_link_stale)."""
import contextlib
import hashlib
import io
import json
import os
import shutil
import stat
import tempfile
import threading
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from campaign_tool.records import approval_queue as aq, console_apply, console_board as cb, console_export, console_journal, console_snapshot
from campaign_tool.records.ledger import store
from campaign_tool.records.outcomes import unmatched
from tests.records import test_console_board as tb
from tests.records.test_console_apply import Apply
from tests.records.test_console_export import tree_state
from tests.records.test_console_model import RUN
from tests.records.test_console_snapshot import RUN2, Panel
from tests.records import test_outcome_received_items as _ri
from tests.records import test_outcome_unmatched as _u
from tests.records.test_message_link_choice_binding import entry


def line_hash(raw_line):
    return hashlib.sha256(raw_line).hexdigest()


class Relink(unittest.TestCase):
    """Synthetic ledger of test_outcome_unmatched plus a newest request req-c, so a tie has two alternatives."""
    setUp0 = _ri.ReceivedItemsTests.setUp
    private, document, enroll, tree, stage = (_ri.ReceivedItemsTests.private, _ri.ReceivedItemsTests.document,
                                              _ri.ReceivedItemsTests.enroll, _ri.ReceivedItemsTests.tree,
                                              _ri.ReceivedItemsTests.stage)
    outcomes, run_outputs, server, row = (_u.UnmatchedTests.outcomes, _u.UnmatchedTests.run_outputs,
                                          _u.UnmatchedTests.server, _u.UnmatchedTests.row)

    def setUp(self):
        _u.UnmatchedTests.setUp(self)
        with store.ledger(self.database) as con:
            con.execute("INSERT INTO requests VALUES(?,?,?,?,?,?,?,?)", ("req-c", "caltrans", "req-c", "2041-01-09", None, None, None, "open"))
            con.commit()
        self.message = self.sha

    @property
    def links(self):
        return self.project / "message-links.jsonl"

    def tie(self):
        self.run_outputs()
        return self.row()["id"]

    def sign(self, ident, choice):
        return self.signer.sign(aq.approval_payload(self.project, ident, choice=choice))

    def lines(self):
        return self.links.read_bytes().split(b"\n")[:-1]

    def tie_first(self, choice="req-a"):
        """The ordinary signed tie (DF2007-01) of the one untied message."""
        ident = self.tie()
        self.assertEqual(console_apply.outcome(self.project, entry(ident, self.sign(ident, choice), choice, name="tie")), "APPLIED")
        return ident

    def wrong(self, run="test-run"):
        """'This tie is wrong' for the message's current line -> (replacement row, fingerprint it replaces)."""
        current = unmatched.current_link_rows(self.project)[0][self.message]["line_sha256"]
        ident, _ = unmatched.queue_relink(self.project, run, self.message, current)
        return next(r for r in aq.load_queue(self.project) if r["id"] == ident), current

    def relink_entry(self, row, choice, name, signature=None):
        return entry(row["id"], signature or self.sign(row["id"], choice), choice, name=name)

    def test_tie_relink_and_next_outputs_use_new_request(self):
        first = self.tie_first("req-a")
        self.assertEqual(self.run_outputs("run-2")[0], 0)
        before = (self.outcomes("run-2") / "received-items.json").read_bytes()
        self.assertEqual(json.loads(before)["items"][0]["request_id"], "req-a")  # wrong: the reply really answers req-b
        (original,) = self.lines()
        row, current = self.wrong("run-2")
        self.assertEqual(current, line_hash(original))
        self.assertEqual((row["kind"], row["status"], row["item_id"], row["evidence"]["supersedes"]), ("message_link", "pending", self.message, current))
        self.assertEqual(row["evidence"]["replaces"], {"agency_id": "caltrans", "request_id": "req-a"})
        self.assertEqual([c["request_id"] for c in row["evidence"]["candidates"]], ["req-c", "req-b"])  # the current tie is left out
        self.assertNotEqual(row["id"], first)
        self.assertEqual(len([r for r in aq.load_queue(self.project) if r["kind"] == "message_link"]), 2)
        self.assertEqual(self.lines(), [original])  # queueing moved nothing
        sig = self.sign(row["id"], "req-b")
        self.assertIn("choice: req-b (agency caltrans)", aq.approval_payload(self.project, row["id"], choice="req-b").decode())
        self.assertIn(current, aq.approval_payload(self.project, row["id"], choice="req-b").decode())  # the fingerprint is signed
        self.assertEqual(console_apply.outcome(self.project, self.relink_entry(row, "req-b", "relink", sig)), "APPLIED")
        old, new = self.lines()
        self.assertEqual(old, original)  # the earlier line is never edited
        self.assertEqual((json.loads(new)["request_id"], json.loads(new)["supersedes"]), ("req-b", line_hash(original)))
        self.assertEqual(unmatched.read_links(self.project), ({self.message: ("caltrans", "req-b")}, 0))
        self.assertEqual(next(r for r in aq.load_queue(self.project) if r["id"] == row["id"])["approved_choice"], "req-b")
        self.assertEqual(self.run_outputs("next-run")[0], 0)  # the normal next run
        item = json.loads((self.outcomes("next-run") / "received-items.json").read_text())["items"][0]
        self.assertEqual((item["original_sha256"], item["agency_id"], item["request_id"]), (self.message, "caltrans", "req-b"))
        self.assertEqual((self.outcomes("run-2") / "received-items.json").read_bytes(), before)  # the earlier run stays as it was
        self.assertEqual(console_apply.outcome(self.project, self.relink_entry(row, "req-b", "relink", sig)), "APPLIED")  # replay
        self.assertEqual(len(self.lines()), 2)

    def test_changed_current_line_makes_queued_relink_stale(self):
        self.tie_first("req-a")
        row, h1 = self.wrong()
        (original,) = self.lines()
        # another decision moves the message on from the very line this row was queued against
        self.assertTrue(unmatched.append_link(self.project, self.message, "other", "req-x", "someone", supersedes=h1))
        after = self.links.read_bytes()
        sig = self.sign(row["id"], "req-b")
        before = self.tree()
        self.assertEqual(console_apply.outcome(self.project, self.relink_entry(row, "req-b", "stale", sig)), "REFUSED message_link_stale")
        self.assertEqual((self.links.read_bytes(), self.row_of(row["id"])["status"]), (after, "pending"))  # no approval, no link
        self.assertEqual(self.tree(), before)
        # a later line naming the very same target but another lineage is stale as well: no missed correction is masked
        raw = self.links.read_bytes()
        forged = json.dumps({"schema": unmatched.LINK_SCHEMA, "message_sha256": self.message, "agency_id": "caltrans",
                             "request_id": "req-b", "actor": "x", "at": "2041-02-03T00:00:00Z", "supersedes": "ab" * 32}) + "\n"
        self.links.write_bytes(raw + forged.encode())
        with self.assertRaises(ValueError) as caught:
            unmatched.append_link(self.project, self.message, "caltrans", "req-b", "x", supersedes=h1)
        self.assertEqual(str(caught.exception), "message_link_stale")
        self.assertEqual(console_apply.outcome(self.project, self.relink_entry(row, "req-b", "stale2", sig)), "REFUSED message_link_stale")
        self.assertEqual(self.links.read_bytes(), raw + forged.encode())
        self.assertEqual(line_hash(original), h1)

    def row_of(self, ident):
        return next(r for r in aq.load_queue(self.project) if r["id"] == ident)

    def test_stale_after_approval_keeps_the_approval_and_appends_nothing(self):
        self.tie_first("req-a")
        row, h1 = self.wrong()
        sig = self.sign(row["id"], "req-b")
        with mock.patch.object(console_apply, "_append_link", side_effect=OSError("synthetic")):
            self.assertIsNone(console_apply.settle(self.project, self.relink_entry(row, "req-b", "r"), {}))
        self.assertEqual(self.row_of(row["id"])["status"], "approved")  # the approval record stays
        unmatched.append_link(self.project, self.message, "other", "req-x", "someone", supersedes=h1)  # the line moves on meanwhile
        kept = self.links.read_bytes()
        self.assertEqual(console_apply.outcome(self.project, self.relink_entry(row, "req-b", "r", sig)), "REFUSED message_link_stale")
        self.assertEqual(self.links.read_bytes(), kept)

    def test_stale_refusals_without_a_current_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            with self.assertRaises(ValueError) as caught:  # no links file at all
                unmatched.append_link(project, self.message, "caltrans", "req-b", "x", supersedes="ab" * 32)
            self.assertEqual(str(caught.exception), "message_link_stale")
            self.assertEqual(os.listdir(project), [])  # a refused replacement creates nothing
            self.assertEqual((unmatched.current_link_rows(project), os.listdir(project)), (({}, 0), []))
            unmatched.append_link(project, "cd" * 32, "caltrans", "req-a", "x")  # another message's line
            other = unmatched.current_link_rows(project)[0]["cd" * 32]["line_sha256"]
            unmatched.append_link(project, self.message, "caltrans", "req-a", "x")
            kept = (project / unmatched.LINKS_NAME).read_bytes()
            with self.assertRaises(ValueError) as caught:
                unmatched.append_link(project, self.message, "caltrans", "req-b", "x", supersedes=other)
            self.assertEqual(str(caught.exception), "message_link_stale")
            for bad in ("", "AB" * 32, "ab", 7):
                with self.assertRaises(ValueError) as caught:
                    unmatched.append_link(project, self.message, "caltrans", "req-b", "x", supersedes=bad)
                self.assertEqual(str(caught.exception), "message_link_invalid")
            mine = unmatched.current_link_rows(project)[0][self.message]["line_sha256"]
            with self.assertRaises(ValueError) as caught:  # the same request is not a correction
                unmatched.append_link(project, self.message, "caltrans", "req-a", "x", supersedes=mine)
            self.assertEqual(str(caught.exception), "message_link_invalid")
            with self.assertRaises(ValueError) as caught:  # a different choice without a replacement line is still refused
                unmatched.append_link(project, self.message, "caltrans", "req-b", "x")
            self.assertEqual(str(caught.exception), "message_link_conflict")
            self.assertEqual((project / unmatched.LINKS_NAME).read_bytes(), kept)

    def test_unsigned_wrong_choice_and_swapped_supersedes_journal_refuse(self):
        self.tie_first("req-a")
        row, h1 = self.wrong()
        pid = aq._project_id(self.project)
        pin = types.SimpleNamespace(source_project_id=pid, run_id="test-run", name="export-1")
        journal = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, journal, True)
        good = self.sign(row["id"], "req-b")
        for choice, signature in (("req-b", "junk"), ("req-b", ""), ("req-b", "-----BEGIN SSH SIGNATURE-----\nnot a signature"),
                                  ("req-c", good), ("req-b", self.sign(row["id"], "req-c"))):
            console_journal.append(str(journal), pin, "operator", "/approvals/approve", "approve", row["id"], 0, signature, choice)
        before, tree = self.links.read_bytes(), self.tree()
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            self.assertEqual(console_apply.main(["--project", str(self.project), "--from-journal", str(journal / console_journal.FILE)]), 0)
        self.assertEqual([line.split(" ", 3)[3] for line in out.getvalue().splitlines()], ["SIGNATURE_REFUSED"] * 5)
        self.assertEqual((self.links.read_bytes(), self.row_of(row["id"])["status"]), (before, "pending"))
        # a signature made for another replacement row (another predecessor) is not this row's
        self.assertTrue(unmatched.append_link(self.project, self.message, "caltrans", "req-b", "x", supersedes=h1))
        row2, h2 = self.wrong()
        self.assertNotEqual((row2["id"], h2), (row["id"], h1))
        kept = self.links.read_bytes()
        self.assertEqual(console_apply.outcome(self.project, entry(row2["id"], self.sign(row["id"], "req-c"), "req-c", name="swap")),
                         "SIGNATURE_REFUSED")
        self.assertEqual(self.links.read_bytes(), kept)
        # the queue-only action creates pending work and moves no link
        console_journal.append(str(journal), pin, "operator", "/message-links/relink", "message_link_relink", self.message, None, h2)
        rows_before = len(aq.load_queue(self.project))
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            console_apply.main(["--project", str(self.project), "--from-journal", str(journal / console_journal.FILE)])
        self.assertEqual((self.links.read_bytes(), len(aq.load_queue(self.project))), (kept, rows_before))  # same row: nothing reopened

    def test_queue_only_journal_action_and_its_refusals(self):
        self.tie_first("req-a")
        h1 = unmatched.current_link_rows(self.project)[0][self.message]["line_sha256"]
        base = {"entry_id": "e" * 64, "action": "message_link_relink", "target_id": self.message, "revision_seen": None,
                "value": h1, "actor": "operator", "export_run_id": "test-run"}
        kept = self.links.read_bytes()
        self.assertEqual(console_apply.outcome(self.project, base), "APPLIED")
        self.assertEqual(self.links.read_bytes(), kept)
        (row,) = [r for r in aq.load_queue(self.project) if r["id"] == aq.draft_id(self.message, "message_link", supersedes=h1)]
        self.assertEqual(row["status"], "pending")
        self.assertEqual(console_apply.outcome(self.project, base), "APPLIED")  # a replay queues nothing more
        self.assertEqual(len([r for r in aq.load_queue(self.project) if r["kind"] == "message_link"]), 2)
        for change, code in (({"value": "ab" * 32}, "REFUSED message_link_stale"), ({"value": "nope"}, "REFUSED relink_entry_invalid"),
                             ({"target_id": "cd" * 32}, "REFUSED message_link_stale"), ({"export_run_id": None}, "REFUSED relink_entry_invalid")):
            self.assertEqual(console_apply.outcome(self.project, {**base, **change}), code, change)
        self.assertEqual(len([r for r in aq.load_queue(self.project) if r["kind"] == "message_link"]), 2)
        self.assertEqual(self.links.read_bytes(), kept)

    def test_raw_line_hash_is_exact_not_reserialized(self):
        a, b = "ab" * 32, "cd" * 32
        legacy = '{"schema":"records-message-link-v1","message_sha256":"%s","request_id":"r1","agency_id":"ag","actor":"old","at":"2040-01-01T00:00:00Z"}' % a
        spaced = ('{ "request_id" : "r\u00e9", "agency_id":"ag" , "message_sha256":"%s", "schema":"records-message-link-v1", '
                  '"extra":{"unknown":[1,2]}, "supersedes":"%s" }' % (a, line_hash(legacy.encode())))
        crlf = '{"schema":"records-message-link-v1","message_sha256":"%s","request_id":"r2","agency_id":"ag"}\r' % b
        raw = (legacy + "\n").encode() + (spaced + "\n").encode("utf-8") + b"not json\n" + (crlf + "\n").encode() + b'{"schema":"x"}\n'
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / unmatched.LINKS_NAME
            path.write_bytes(raw)
            rows, invalid = unmatched.current_link_rows(tmp)
            self.assertEqual(invalid, 2)
            self.assertEqual(unmatched.read_links(tmp), ({a: ("ag", "r\u00e9"), b: ("ag", "r2")}, 2))  # same winners and count
            self.assertEqual(rows[a]["line_sha256"], hashlib.sha256(spaced.encode("utf-8")).hexdigest())
            self.assertEqual(rows[b]["line_sha256"], hashlib.sha256(crlf.encode()).hexdigest())  # a CR before the LF is part of the line
            self.assertNotEqual(rows[a]["line_sha256"], hashlib.sha256(unmatched._canon(json.loads(spaced)).encode()).hexdigest())
            self.assertEqual(rows[a]["supersedes"], line_hash(legacy.encode()))
            self.assertIsNone(rows[b]["supersedes"])
            self.assertEqual(unmatched.link_fingerprints(tmp)[a], {"agency_id": "ag", "request_id": "r\u00e9", "line_sha256": rows[a]["line_sha256"]})
            # the legacy line stays readable and a replacement of the current (odd) line appends after it, byte for byte
            self.assertTrue(unmatched.append_link(tmp, a, "ag", "r3", "me", supersedes=rows[a]["line_sha256"]))
            self.assertTrue(path.read_bytes().startswith(raw))
            self.assertEqual(unmatched.read_links(tmp)[0][a], ("ag", "r3"))
            # a line naming a malformed fingerprint is skipped like any bad line
            path.write_bytes(raw + (legacy.replace('"r1"', '"r9"').replace("}", ',"supersedes":"short"}') + "\n").encode())
            self.assertEqual(unmatched.current_link_rows(tmp)[1], 3)
            self.assertEqual(unmatched.read_links(tmp)[0][a], ("ag", "r\u00e9"))

    def test_single_draft_per_message_and_replaced_line(self):
        self.tie_first("req-a")
        row, h1 = self.wrong()
        again, created = unmatched.queue_relink(self.project, "test-run", self.message, h1)
        self.assertEqual((again, created), (row["id"], False))
        self.assertEqual(len([r for r in aq.load_queue(self.project) if r["kind"] == "message_link"]), 2)
        self.assertEqual(aq.draft_id(self.message, "message_link"), aq.draft_id(self.message, "message_link", supersedes=None))
        self.assertEqual(aq.draft_id(self.message, "message_link"), hashlib.sha256(aq._canon([self.message, "message_link"]).encode()).hexdigest())
        self.assertEqual(row["id"], hashlib.sha256(aq._canon([self.message, "message_link", h1]).encode()).hexdigest())
        aq.reject(self.project, row["id"], "operator", "the tie was right")  # a rejected correction is not reopened by asking again
        self.assertEqual(unmatched.queue_relink(self.project, "test-run", self.message, h1), (row["id"], False))
        self.assertEqual(self.row_of(row["id"])["status"], "rejected")
        # a later line makes a different row for the same message
        unmatched.append_link(self.project, self.message, "caltrans", "req-b", "x", supersedes=h1)
        row2, h2 = self.wrong()
        self.assertEqual((row2["item_id"], row2["evidence"]["replaces"]["request_id"]), (self.message, "req-b"))
        self.assertNotEqual(row2["id"], row["id"])
        self.assertEqual([c["request_id"] for c in row2["evidence"]["candidates"]], ["req-c", "req-a"])
        # an ordinary tie row cannot carry a fingerprint under the old identity, and a malformed one is refused
        with self.assertRaises(aq.ApprovalQueueError) as caught:
            aq.queue_message_links(self.project, "test-run", [{**unmatched._draft(
                {"message_sha256": "ef" * 32, "received_date": None, "sender_domain": None, "attachment_count": 0, "candidates": [],
                 "agency_id": "caltrans"}), "evidence": {"source": "unmatched", "supersedes": "short"}}])
        self.assertEqual(caught.exception.code, "invalid_draft")
        forged = {"item_id": "ef" * 32, "kind": "message_link", "agency_id": "caltrans", "request_id": "unmatched", "body": "x",
                  "evidence": {"source": "unmatched", "supersedes": h2}, "source_run_id": "test-run"}
        with aq._Store(self.project, True) as store_:
            store_.append([aq._event("draft", aq.draft_id("ef" * 32, "message_link"), 0, aq.PIPELINE_ACTOR, snapshot=forged)])
        with self.assertRaises(aq.ApprovalQueueError) as caught:
            aq.load_queue(self.project)
        self.assertEqual(caught.exception.code, "queue_invalid")

    def test_replay_after_each_interruption_is_lineage_safe(self):
        self.tie_first("req-a")
        row, h1 = self.wrong()
        sig = self.sign(row["id"], "req-b")
        mine = self.relink_entry(row, "req-b", "mine", sig)
        kept = self.links.read_bytes()
        with mock.patch.object(aq, "approve", side_effect=aq.ApprovalQueueError("queue_io")):  # fault before the approval
            self.assertIsNone(console_apply.settle(self.project, mine, {}))
        self.assertEqual((self.links.read_bytes(), self.row_of(row["id"])["status"]), (kept, "pending"))
        with mock.patch.object(console_apply, "_append_link", side_effect=OSError("synthetic")):  # after the approval, before the link
            self.assertIsNone(console_apply.settle(self.project, mine, {}))
        self.assertEqual((self.links.read_bytes(), self.row_of(row["id"])["status"]), (kept, "approved"))
        console_apply._receipt(self.project, mine["entry_id"], console_apply.INTENT, 0)
        intents = console_apply.ledger(self.project)[1]
        self.assertEqual(console_apply.settle(self.project, mine, intents), "APPLIED")  # completes against the expected old line
        done = self.links.read_bytes()
        self.assertEqual(len(self.lines()), 2)
        self.assertEqual(console_apply.settle(self.project, mine, intents), "APPLIED")  # after the append, receipt lost: exact lineage replay
        self.assertEqual(self.links.read_bytes(), done)
        # the line then moves on: the same entry can neither re-append nor retarget the message
        unmatched.append_link(self.project, self.message, "other", "req-x", "x", supersedes=unmatched.current_link_rows(self.project)[0][self.message]["line_sha256"])
        moved = self.links.read_bytes()
        self.assertEqual(console_apply.settle(self.project, mine, intents), "REFUSED message_link_stale")
        changed = {**mine, "entry_id": hashlib.sha256(b"changed").hexdigest(), "choice": "req-c"}
        self.assertEqual(console_apply.settle(self.project, changed, {}), "SIGNATURE_REFUSED")
        self.assertEqual(self.links.read_bytes(), moved)

    def test_atomic_check_append_loses_one_competing_relink(self):
        with tempfile.TemporaryDirectory() as tmp:
            unmatched.append_link(tmp, self.message, "caltrans", "req-a", "x")
            h1 = unmatched.current_link_rows(tmp)[0][self.message]["line_sha256"]
            results, barrier = [], threading.Barrier(8)

            def attempt(n):
                barrier.wait()
                try:
                    results.append(unmatched.append_link(tmp, self.message, "caltrans", "req-%d" % n, "x%d" % n, supersedes=h1))
                except ValueError as error:
                    results.append(str(error))
            threads = [threading.Thread(target=attempt, args=(n,)) for n in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(30)
            self.assertEqual(sorted(map(str, results)), ["True"] + ["message_link_stale"] * 7)
            lines = (Path(tmp) / unmatched.LINKS_NAME).read_bytes().split(b"\n")[:-1]
            self.assertEqual(len(lines), 2)
            self.assertEqual(json.loads(lines[1])["supersedes"], h1)

    def test_unsafe_link_files_refuse_and_keep_their_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            target = project / "elsewhere.jsonl"
            target.write_bytes(b"keep\n")
            (project / unmatched.LINKS_NAME).symlink_to(target)
            for supersedes in (None, "ab" * 32):
                with self.assertRaises(OSError):
                    unmatched.append_link(project, self.message, "caltrans", "req-b", "x", supersedes=supersedes)
            self.assertEqual(target.read_bytes(), b"keep\n")
            (project / unmatched.LINKS_NAME).unlink()
            os.mkfifo(project / unmatched.LINKS_NAME)
            with self.assertRaises(ValueError) as caught:
                unmatched.append_link(project, self.message, "caltrans", "req-b", "x")
            self.assertEqual(str(caught.exception), "message_links_file")
            (project / unmatched.LINKS_NAME).unlink()
            path = project / unmatched.LINKS_NAME
            unmatched.append_link(project, self.message, "caltrans", "req-a", "x")
            kept = path.read_bytes()
            with mock.patch.object(unmatched, "MAX_FILE", len(kept) + 10):  # no room for the replacement line
                with self.assertRaises(ValueError) as caught:
                    unmatched.append_link(project, self.message, "caltrans", "req-b", "x",
                                          supersedes=unmatched.current_link_rows(project)[0][self.message]["line_sha256"])
                self.assertEqual(str(caught.exception), "message_links_file")
            with mock.patch.object(unmatched, "MAX_FILE", len(kept) - 1):
                with self.assertRaises(ValueError):
                    unmatched.current_link_rows(project)
            self.assertEqual((path.read_bytes(), stat.S_IMODE(path.stat().st_mode)), (kept, 0o600))

    def test_empty_candidates_missing_ledger_and_trust_unavailable(self):
        self.tie_first("req-a")
        h1 = unmatched.current_link_rows(self.project)[0][self.message]["line_sha256"]
        with store.ledger(self.database) as con:
            con.execute("DELETE FROM requests WHERE agency_id='caltrans' AND id!='req-a'")
            con.commit()
        row, _ = self.wrong()
        self.assertEqual(row["evidence"]["candidates"], [])
        self.assertIn("No other candidate", row["body"])
        page = self.server()
        http = page[1]
        text = http.send("GET", "/review?id=" + row["id"], headers={"Content-Length": None, "Content-Type": None})[2]
        self.assertIn("Replace the tie for this message", text)
        self.assertIn("/approvals/reject", text)
        self.assertNotIn("/approvals/approve", text)
        # a missing ledger makes no partial decision
        other = "ee" * 32
        unmatched.append_link(self.project, other, "caltrans", "req-a", "x")
        h2 = unmatched.current_link_rows(self.project)[0][other]["line_sha256"]
        self.database.rename(self.database.with_name("gone.sqlite"))
        queue = (self.project / "approvals" / "queue.jsonl").read_bytes()
        with self.assertRaises(ValueError) as caught:
            unmatched.queue_relink(self.project, "test-run", other, h2)
        self.assertEqual(str(caught.exception), "message_link_source_missing")
        self.assertEqual((self.project / "approvals" / "queue.jsonl").read_bytes(), queue)
        self.assertEqual(console_apply.outcome(self.project, {"entry_id": "f" * 64, "action": "message_link_relink", "target_id": other,
                                                              "revision_seen": None, "value": h2, "actor": "o", "export_run_id": "test-run"}),
                         "REFUSED message_link_source_missing")
        self.assertTrue(h1)

    def test_trust_unavailable_defers_a_relink_and_leaves_the_link(self):
        from campaign_tool import approval
        self.tie_first("req-a")
        row, _ = self.wrong()
        sig = self.sign(row["id"], "req-b")
        kept, tree = self.links.read_bytes(), self.tree()
        with mock.patch.object(approval, "verify", side_effect=approval.TrustUnavailable("anchor")):
            self.assertIsNone(console_apply.outcome(self.project, self.relink_entry(row, "req-b", "t", sig)))
        self.assertEqual((self.links.read_bytes(), self.tree()), (kept, tree))
        self.assertEqual(console_apply.outcome(self.project, self.relink_entry(row, "req-b", "t", sig)), "APPLIED")

    def test_direct_console_wrong_tie_post_queues_and_stale_is_409(self):
        self.tie_first("req-a")
        server, http = self.server()
        h1 = unmatched.current_link_rows(self.project)[0][self.message]["line_sha256"]
        post = lambda **fields: http.post("/message-links/relink", {"csrf_token": server.csrf_token, "message_sha256": self.message,
                                                                     "supersedes": h1, **fields})
        before = self.links.read_bytes()
        status, headers, _ = post()
        ident = aq.draft_id(self.message, "message_link", supersedes=h1)
        self.assertEqual((status, headers["location"]), (303, "/review?id=" + ident))
        self.assertEqual(self.links.read_bytes(), before)
        self.assertEqual(self.row_of(ident)["status"], "pending")
        self.assertEqual(post()[0], 303)  # again: the same row, nothing reopened
        self.assertEqual(len([r for r in aq.load_queue(self.project) if r["kind"] == "message_link"]), 2)
        for fields, code in (({"csrf_token": "bad"}, 403), ({"supersedes": "ab" * 32}, 409), ({"supersedes": "short"}, 400),
                             ({"message_sha256": "x"}, 400), ({"extra": "1"}, 400), ({"message_sha256": "cd" * 32}, 409)):
            self.assertEqual(post(**fields)[0], code, fields)
        page = http.send("GET", "/review?id=" + ident, headers={"Content-Length": None, "Content-Type": None})[2]
        self.assertIn("Replace the tie for this message", page)
        self.assertIn("Currently tied to request <strong>req-a</strong>", page)
        sig = self.sign(ident, "req-c")
        unmatched.append_link(self.project, self.message, "other", "req-x", "someone", supersedes=h1)  # the line moves on
        moved = self.links.read_bytes()
        self.assertEqual(self.post_approve(http, server, ident, sig, "req-c"), 409)
        self.assertEqual((self.links.read_bytes(), self.row_of(ident)["status"]), (moved, "pending"))

    def post_approve(self, http, server, ident, sig, choice):
        return http.post("/approvals/approve", {"csrf_token": server.csrf_token, "id": ident, "signature": sig, "choice": choice})[0]

    def test_direct_console_approves_the_replacement_with_the_signed_fingerprint(self):
        self.tie_first("req-a")
        server, http = self.server()
        row, h1 = self.wrong()
        sig = self.sign(row["id"], "req-b")
        self.assertEqual(self.post_approve(http, server, row["id"], sig, "req-b"), 303)
        old, new = self.lines()
        self.assertEqual(json.loads(new)["supersedes"], line_hash(old))
        self.assertEqual(self.post_approve(http, server, row["id"], sig, "req-b"), 303)  # replay
        self.assertEqual(len(self.lines()), 2)
        self.assertEqual(h1, line_hash(old))


class JournalRelink(Apply):
    """The hosted journal: 'This tie is wrong' is a queue-only entry; applying it queues the decision and moves no link."""
    message = "ab" * 32

    def tie_live(self):
        unmatched.append_link(self.live, self.message, "caltrans", "req-a", "operator")
        return unmatched.current_link_rows(self.live)[0][self.message]["line_sha256"]

    def wrong(self, **fields):
        return self.decide("/message-links/relink", **{"message_sha256": self.message, "supersedes": self.h1, **fields})

    def test_queue_only_entry_is_journaled_applied_once_and_moves_no_link(self):
        self.h1 = self.tie_live()
        self.make_export()
        self.up()
        live_before = tree_state(self.live)
        status, _, page = self.wrong()
        self.assertEqual(status, 200)
        self.assertIn("Queued for next apply", page)
        self.assertNotIn("tie has changed", page)
        (entry_,) = self.lines()
        self.assertEqual((entry_["action"], entry_["target_id"], entry_["value"], entry_["revision_seen"], entry_["export_run_id"],
                          entry_["source_project_id"], entry_["route"]),
                         ("message_link_relink", self.message, self.h1, None, RUN, self.pid, "/message-links/relink"))
        self.assertNotIn("choice", entry_)
        self.assertEqual(tree_state(self.live), live_before)  # the journal alone moved nothing
        links = (self.live / "message-links.jsonl").read_bytes()
        self.assertEqual(self.apply(), ["APPLIED"])
        self.assertEqual((self.live / "message-links.jsonl").read_bytes(), links)
        row = self.row(aq.draft_id(self.message, "message_link", supersedes=self.h1))
        self.assertEqual((row["status"], row["evidence"]["replaces"]["request_id"]), ("pending", "req-a"))
        self.assertEqual(self.apply(), ["ALREADY"])
        again = {**entry_, "entry_id": "9" * 64}  # asked a second time: the same decision comes back, nothing is reopened
        self.assertEqual(console_apply.outcome(self.live, again), "APPLIED")
        self.assertEqual(len([r for r in aq.load_queue(self.live) if r["kind"] == "message_link"]), 1)

    def test_refusals_journal_nothing(self):
        self.h1 = self.tie_live()
        self.make_export()
        self.up()
        for fields, code in (({"supersedes": "cd" * 32}, 409), ({"message_sha256": "cd" * 32}, 409), ({"supersedes": "short"}, 400),
                             ({"csrf_token": "bad"}, 403), ({"extra": "1"}, 400)):
            self.assertEqual(self.wrong(**fields)[0], code, fields)
        self.assertEqual(self.lines(), [])

    def test_export_without_tie_metadata_and_read_only_export_refuse(self):
        self.h1 = self.tie_live()
        real = console_export._model_queue

        def old(project, now):
            model = real(project, now)
            del model["message_links"]
            return model
        with mock.patch.dict(console_export.MODEL, {"queue.json": old}):
            self.make_export()
        self.up()
        self.assertEqual(self.wrong()[0], 503)  # refresh the export first
        self.assertEqual(self.lines(), [])
        self.server.shutdown()
        self.server.server_close()
        self.make_export(RUN2)
        Panel.up(self)  # a read-only export: no journal, no mutation form
        self.assertEqual(self.wrong()[0], 503)
        self.assertFalse((self.j / console_journal.FILE).exists())


class Timeline(unittest.TestCase):
    """The agency timeline lists each tied message with 'This tie is wrong'; an export without metadata asks for a refresh."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name)
        tb.build(self.project)
        self.m1, self.m2, self.m3 = "11" * 32, "22" * 32, "33" * 32
        unmatched.append_link(self.project, self.m1, "alpha-pd", "L-R-1", "t")
        unmatched.append_link(self.project, self.m2, "alpha-pd", "L-R-2", "t")
        unmatched.append_link(self.project, self.m3, "tracker:beta sheriff / coroner", "L-R-3", "t")
        self.links = unmatched.link_fingerprints(self.project)

    def model(self, **state):
        model = cb.load(self.project, tb.RUN)
        model["message_links"] = {"state": "ok", "links": self.links, "token": "tok<en>", "age": None, **state}
        return model

    def test_each_tied_message_of_the_agency_has_one_control_and_escapes(self):
        page = cb.render_agency(self.model(), "alpha-pd")
        self.assertEqual(page.count("This tie is wrong"), 2)
        for sha in (self.m1, self.m2):
            self.assertEqual(page.count('name="message_sha256" value="%s"' % sha), 1)
            self.assertIn('name="supersedes" value="%s"' % self.links[sha]["line_sha256"], page)
        self.assertNotIn(self.m3, page)  # another agency's message
        self.assertIn('action="/message-links/relink"', page)
        self.assertIn("tok&lt;en&gt;", page)
        self.assertNotIn("tok<en>", page)
        self.assertEqual(cb.render_agency(self.model(), "tracker:beta sheriff / coroner").count("This tie is wrong"), 1)
        hostile = {**self.links, self.m1: {**self.links[self.m1], "agency_id": "<b>x</b>"}}
        self.assertNotIn("<b>x</b>", cb.render_agency(self.model(links=hostile), "alpha-pd"))
        self.assertNotIn("subject", page.lower().split("message ties")[1].split("</section>")[0])

    def test_the_live_agency_page_lists_current_ties_and_names_an_unreadable_file(self):
        import http.client
        from campaign_tool.records import admin_panel as ap
        server = ap.build_server(self.project, tb.RUN, actor="op", system=False)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        def get():
            with contextlib.closing(http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)) as conn:
                conn.request("GET", "/agency?agency=alpha-pd")
                response = conn.getresponse()
                return response.status, response.read().decode()
        status, body = get()
        self.assertEqual(status, 200)
        self.assertEqual(body.count("This tie is wrong"), 2)
        self.assertIn('name="csrf_token" value="%s"' % server.csrf_token, body)
        self.assertNotIn(self.m3, body)
        path = self.project / unmatched.LINKS_NAME
        path.chmod(0o600)
        os.link(path, self.project / "second-name")  # a file with two names is not a safe tie source
        status, body = get()
        self.assertEqual(status, 200)
        self.assertIn("Message ties are unavailable", body)
        self.assertNotIn("This tie is wrong", body)

    def test_read_only_snapshot_age_and_old_export_say_so(self):
        page = cb.render_agency(self.model(token=None, age="export taken 2026-11-14T10:00:00Z"), "alpha-pd")
        self.assertNotIn("This tie is wrong", page)
        self.assertNotIn("/message-links/relink", page)
        self.assertIn("Read-only", page)
        self.assertIn("export taken 2026-11-14T10:00:00Z", page)
        self.assertIn("the timeline above is not rewritten", page)
        old = self.model()
        old["message_links"] = {"state": "unavailable", "code": "message_links_missing"}
        text = cb.render_agency(old, "alpha-pd")
        self.assertIn("Refresh the export", text)
        self.assertNotIn("This tie is wrong", text)
        empty = cb.render_agency(self.model(links={}), "alpha-pd")
        self.assertIn("No received message is tied", empty)
        self.assertNotIn("Refresh the export", empty)  # an empty set is not missing metadata
        self.assertNotIn("Message ties", cb.render_agency(cb.load(self.project, tb.RUN), "alpha-pd"))  # no tie data: page unchanged

    def test_export_and_snapshot_carry_the_same_current_ties_and_old_exports_are_refused(self):
        from tests.records import test_console_model as tm

        class Export(tm.LiveBase):
            def runTest(self):
                pass
        base = Export()
        base.setUp()
        base.addCleanup(base.doCleanups)
        unmatched.append_link(base.live, self.m1, "alpha-pd", "L-R-1", "t")
        dest, _, model = base.export()
        self.assertEqual(model["queue.json"]["message_links"], unmatched.link_fingerprints(base.live))
        for link in model["queue.json"]["message_links"].values():  # hashes, the current request and the fingerprint only
            self.assertEqual(sorted(link), ["agency_id", "line_sha256", "request_id"])
        pin = console_snapshot.Pin(str(dest), tm.RUN, aq._project_id(base.live), "export", "2026-11-14T10:00:00Z")
        self.assertEqual(console_snapshot.SnapshotSource(pin).message_links(), console_snapshot.LiveSource(base.live).message_links())
        queue_path = dest / "console-model" / "queue.json"
        doc = json.loads(queue_path.read_text())
        doc["message_links"] = {}
        queue_path.write_text(json.dumps(doc))
        self.assertEqual(console_snapshot.SnapshotSource(pin).message_links(), {})  # empty is a real, empty set
        del doc["message_links"]
        queue_path.write_text(json.dumps(doc))
        with self.assertRaises(aq.ApprovalQueueError) as caught:
            console_snapshot.SnapshotSource(pin).message_links()
        self.assertEqual(caught.exception.code, "message_links_missing")

    def test_the_approvals_document_describes_the_correction(self):
        text = (Path(__file__).resolve().parents[2] / "docs" / "RECORDS-CONSOLE-APPROVALS.md").read_text()
        for needle in ("This tie is wrong", "supersedes", "message_link_stale", "next run"):
            self.assertIn(needle, text)


if __name__ == "__main__":
    unittest.main()
