"""DFGL-07i: a human fact review survives the next real analysis ticks and yields exactly one pending, unsigned finding packet.

Every writer is real: the pipeline outputs (analysis-export, analysis-sync), the review DAG (analysis-tick CLI, flock_review mark,
flock_verify finish), flock_facts review, the outbox builder and the analysis-packets-import CLI. Nothing here writes a status,
REVIEW-STATE.json, PUBLIC-OK, FINDINGS or a packet by hand.
"""
import contextlib
import io
import json
import subprocess
import sys
import unittest

from campaign_tool.records import public_packets as pp, run_stages
from campaign_tool.records.ledger import store
from tests.records import test_analysis_bridge as tb, test_analysis_packet_outbox as to, test_analysis_packets as tap
from tests.records import test_outcome_received_items as tri

REPO = tb.REPO


class ReviewPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.o = to.PacketOutboxTests("test_empty_received_at_holds_the_finding_as_date_unavailable")
        self.o.start()  # real export + sync + the real DAG to DONE with unreviewed facts
        self.addCleanup(self.o.doCleanups)
        received = tri.ReceivedItemsTests("test_stage_artifact_modes_replay_conflict_and_no_coordinator_input")
        received.setUp()
        self.addCleanup(received.doCleanups)
        with store.ledger(received.database) as con:
            con.execute("INSERT INTO agencies VALUES(?,?,NULL,NULL)", (tb.AGY, tb.AGY))
            con.commit()
        self.assertEqual(received.document(tb.PAGES, tb.AGY)[0], self.o.digest)
        received.mail(self.o.digest, "2026-11-02T18:00:00Z")  # the same received document the exported run cites
        (received.processing / "runs").mkdir(mode=0o700)
        (received.processing / "runs" / ("extract-%s.json" % tb.R1)).write_text(json.dumps(
            {"schema": "records-extract-outcomes-v1", "run_id": tb.R1, "documents": []}))
        with contextlib.redirect_stdout(io.StringIO()):  # the real writer of received-items.json
            self.assertEqual(run_stages.main(["outputs", "--project", str(received.project), "--run-id", tb.R1]), 0)
        self.project = received.project
        self.journal = self.project / pp.JOURNAL

    def imported(self, path):
        done = subprocess.run([sys.executable, "-B", "-m", "campaign_tool.records", "analysis-packets-import", "--project", str(self.project),
                               "--from-tar", "-"], cwd=REPO, input=tap.tar_of((path.name, path.read_bytes())), capture_output=True, check=False)
        return done.returncode, done.stdout.decode()

    def test_unreviewed_done_is_held_then_one_human_review_gives_one_unsigned_packet_without_a_duplicate(self):
        o = self.o
        # (1) DONE with unreviewed facts: the actual tick holds everything and stages nothing
        code, text = o.t.tick(process=True)
        self.assertEqual(code, 0, text)
        self.assertIn(" PACKET_ALL_HELD n=0 held=4 EVIDENCE_FACT_UNREVIEWED=4", text)
        self.assertEqual(o.outbox(), [])
        # (2) a real second-reviewer flock_facts review; the next actual tick stages exactly one file; the actual import proposes it
        o.review("N1")
        code, text = o.t.tick(process=True)
        self.assertEqual(code, 0, text)
        self.assertIn(" PACKET_STAGED %s n=1 held=3 EVIDENCE_FACT_UNREVIEWED=3" % o.outbox()[0].name, text)
        (path,) = o.outbox()
        code, out = self.imported(path)
        self.assertEqual(code, 0, out)
        self.assertIn("proposed=1", out)
        (row,) = pp.pending(self.project)
        self.assertEqual((row["kind"], row["status"], row["signed_by"]), ("finding", "proposed", None))
        blob = pp.payload(self.project, row["id"]).decode()
        for needle in (o.digest, "2026-11-02", o.cited["N1"][:12], "Research draft - not legal advice"):
            self.assertIn(needle, blob)
        self.assertFalse((self.project / pp.OUT).exists())
        self.assertFalse((self.project / pp.SWITCH).exists())
        journal = self.journal.read_bytes()
        # (3) another actual tick keeps the checked fact and the review log and builds nothing new; the re-import is ALREADY
        checked, reviewed = o.facts.read_bytes(), o.log.read_bytes()
        code, text = o.t.tick(process=True)
        self.assertEqual(code, 0, text)
        self.assertIn(" PACKET_UNCHANGED", text)
        self.assertEqual(o.outbox(), [path])
        self.assertIn(" DONE", text)
        self.assertEqual((o.facts.read_bytes(), o.log.read_bytes()), (checked, reviewed))
        code, out = self.imported(path)
        self.assertEqual(code, 0, out)
        self.assertIn("ALREADY=1", out)
        self.assertEqual((self.journal.read_bytes(), len(pp.pending(self.project))), (journal, 1))
        blob = "\n".join(o.t.seen) + (o.root / ".analysis" / "analysis-ledger.jsonl").read_text()
        for private in tb.PAGES + ["search log", "The ALPR vendor"]:
            self.assertNotIn(private, blob)


if __name__ == "__main__":
    unittest.main()
