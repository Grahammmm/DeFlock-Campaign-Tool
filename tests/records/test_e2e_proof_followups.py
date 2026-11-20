"""FLK-REC-6: e2e-proof --seed with a ``followup`` block proves the reply follow-up route and what its owner queued."""
import contextlib
import hashlib
import io
import json
import shutil
import sqlite3
import tempfile
import unittest
from datetime import datetime
from email.message import EmailMessage
from email.policy import SMTP
from pathlib import Path
from unittest import mock

from campaign_tool.records import approval_queue, e2e_proof, followup_engine, native_extract
from campaign_tool.records.outcomes import register
from tests.records.test_e2e_proof import text_pdf

HAVE_PARSERS = not native_extract.missing_parsers()
FIXTURES = Path(__file__).parent / "fixtures" / "e2e-followups"
CAMPAIGN = Path(__file__).parent / "fixtures" / "e2e-campaign"
# name -> (owner, queued draft kind, pattern)
EXPECTED = {"fee": ("received_items", "fee-reply", None),
            "partial": ("engine", "followup-playbook", "partial_production"),
            "denial": ("received_items", "appeal-denial", None),
            "extension": ("received_items", "reply-extension", "extension")}
# The synthetic reply wording (PDF text layer); fixture templates pin the PDF's sha256 in eight hex chunks.
# A .eml is built at test time because the public-tree gate refuses committed .eml files.
REPLIES = {
    "fee": ("Fictional Valley Records Unit", "The duplication fee is $12.00 for the requested records.",
            "Please remit payment before we release them."),
    "partial": ("Fictional Valley Vendor Agreement", "Attached are the responsive records.",
                "The vendor agreement for the ALPR cameras is provided."),
    "denial": ("Fictional Valley Records Unit", "Your records request is denied.",
               "The ALPR search logs are withheld under Government Code section 7923.600."),
    "extension": ("Fictional Valley Records Unit", "We are extending the determination period.",
                  "We will respond by December 30, 2026."),
}


def build_eml(name, path):
    message = EmailMessage()
    message["From"], message["To"] = "records@fictional-valley.example.invalid", "requests@campaign.example.invalid"
    message["Subject"], message["Date"] = "Reply to records request " + name, "Thu, 05 Nov 2026 10:00:00 -0800"
    message["Message-ID"] = "<%s-reply@fictional-valley.example.invalid>" % name
    message.set_content("The reply is attached.\n")
    message.add_attachment(text_pdf(REPLIES[name]), maintype="application", subtype="pdf", filename="reply-%s.pdf" % name)
    path.write_bytes(message.as_bytes(policy=SMTP))
    return path


class Frozen(datetime):
    """The register's clock: the request (2026-11-02) is recent, so no unrelated silence reminder fires."""

    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 11, 12, 12, 0, tzinfo=tz)


@unittest.skipUnless(HAVE_PARSERS, "UNSUPPORTED_RUNTIME: pypdf/openpyxl not in this interpreter")
class FollowupProofTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="e2e-followups-")
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        shutil.copytree(CAMPAIGN / "content", self.tmp / "campaign" / "content")
        clock = mock.patch.object(register, "datetime", Frozen)
        clock.start()
        self.addCleanup(clock.stop)
        # DF2307-01: the offline .eml ingest records no mail receipt time, so the register would now flag
        # arrival_unresolved for a person. These tests prove follow-up routing, not arrival, so keep arrival "none".
        arrival = mock.patch.object(register, "answer_arrived",
                                    return_value={"state": "none", "first_arrival": None, "occurrence_ids": []})
        arrival.start()
        self.addCleanup(arrival.stop)

    def eml(self, name):
        path = self.tmp / (name + ".eml")
        return path if path.exists() else build_eml(name, path)

    def fixture(self, name):
        doc = json.loads((FIXTURES / (name + ".seed.json")).read_text())
        # Synthetic public provenance only: reconstruct the unchanged pin before replay.
        parts = doc["followup"].pop("source_sha256_parts")
        self.assertIsInstance(parts, list)
        self.assertEqual(len(parts), 8)
        for part in parts:
            self.assertIsInstance(part, str)
            self.assertRegex(part, r"^[0-9a-f]{8}$")
        doc["followup"]["source_sha256"] = "".join(parts)
        return doc

    def seed(self, name, **changes):
        doc = self.fixture(name)
        doc["followup"].update(changes)
        path = self.tmp / (name + "-%d.seed.json" % len(list(self.tmp.glob("*.seed.json"))))
        path.write_text(json.dumps(doc))
        return path

    def replay(self, name, label, **changes):
        seed = self.seed(name, **changes)
        with contextlib.redirect_stdout(io.StringIO()):
            return e2e_proof.replay(self.eml(name), self.tmp / "out", self.tmp / "campaign", label=label,
                                    seed=seed)

    def row(self, report):
        stage = next(s for s in report["stages"] if s["stage"] == "followups")
        return stage, stage["detail"]["requests"][0]

    def queue(self, label):
        return approval_queue.load_queue(str(self.tmp / "out" / label / "project"))

    def test_each_reply_kind_reports_its_owner_and_exactly_one_queued_draft(self):
        for name in EXPECTED:  # the reconstructed seed pins the PDF this test builds
            doc = self.fixture(name)
            self.assertEqual(doc["followup"]["source_sha256"], hashlib.sha256(text_pdf(REPLIES[name])).hexdigest())
        for name, (owner, kind, pattern) in EXPECTED.items():
            with self.subTest(name):
                report = self.replay(name, name)
                stage, row = self.row(report)
                self.assertEqual((report["result"], report["manual_steps"]), ("PASS", []), report["stages"])
                self.assertEqual((stage["status"], row["handled_by"], row["pattern"], row["status"]),
                                 ("ok", owner, pattern, "queued"))
                self.assertEqual(row["queued_by_kind"], {kind: 1})
                letters = [q for q in self.queue(name) if q["kind"] == kind]
                self.assertEqual([q["status"] for q in letters], ["pending"])
                self.assertEqual([q["kind"] for q in self.queue(name) if q["kind"] != "message_link"], [kind])
                self.assertEqual(report["sent"] or report["published"], False)
        partial = next(q for q in self.queue("partial") if q["kind"] == "followup-playbook")
        self.assertEqual(partial["evidence"]["asks_for"], [2])  # the answered vendor agreement is not asked again

    def test_engine_route_with_drafting_off_is_reported_and_still_passes(self):
        report = self.replay("partial", "off", autonomy="off")
        stage, row = self.row(report)
        self.assertEqual((report["result"], report["manual_steps"]), ("PASS", []))
        self.assertEqual((stage["code"], row["handled_by"], row["status"], row["queued_by_kind"]),
                         ("FOLLOWUP_DRAFTING_OFF", "engine", "drafting_off", {}))
        self.assertEqual([q["kind"] for q in self.queue("off")], ["message_link"])
        markdown = (self.tmp / "out" / "off" / "proof-report.md").read_text()
        self.assertIn("route found, drafting off", markdown)
        # received-items drafts are not gated by autonomy
        fee = self.replay("fee", "fee-off", autonomy="off")
        self.assertEqual((fee["result"], self.row(fee)[1]["queued_by_kind"]), ("PASS", {"fee-reply": 1}))

    def test_engine_owner_that_queues_nothing_fails_with_a_named_manual_step(self):
        with mock.patch.object(followup_engine, "run", return_value={}):
            report = self.replay("partial", "noengine")
        stage, row = self.row(report)
        self.assertEqual((report["result"], stage["code"], row["status"], row["handled_by"]),
                         ("FAIL", "FOLLOWUP_DRAFT_MISSING", "missing_draft", "engine"))
        self.assertIn({"stage": "followups", "why": "FOLLOWUP_DRAFT_MISSING"}, report["manual_steps"])
        self.assertIn("FOLLOWUP_DRAFT_MISSING", (self.tmp / "out" / "noengine" / "proof-report.md").read_text())

    def test_received_items_owner_that_queues_nothing_fails_even_if_the_route_shifts(self):
        for name in ("fee", "extension"):
            with self.subTest(name), mock.patch.object(approval_queue, "queue_received_items", return_value={}):
                report = self.replay(name, "norecv-" + name)
                stage, row = self.row(report)
                self.assertEqual((report["result"], stage["code"], row["status"]), ("FAIL", "FOLLOWUP_DRAFT_MISSING", "missing_draft"))
                self.assertIn({"stage": "followups", "why": "FOLLOWUP_DRAFT_MISSING"}, report["manual_steps"])

    def test_duplicate_owner_drafts_fail_with_a_count_code(self):
        original = e2e_proof._followup_check

        def doubled(project, run_id, target):
            row = next(q for q in approval_queue.load_queue(str(project)) if q["kind"] == "followup-playbook")
            approval_queue.add_draft(str(project), item_id="route:" + "0" * 32, kind="followup-playbook",
                                     agency_id=row["agency_id"], request_id=row["request_id"], body=row["body"],
                                     evidence=row["evidence"], source_run_id=row["source_run_id"])
            return original(project, run_id, target)
        with mock.patch.object(e2e_proof, "_followup_check", doubled):
            report = self.replay("partial", "dup")
        self.assertEqual((report["result"], self.row(report)[0]["code"]), ("FAIL", "FOLLOWUP_DRAFT_COUNT_INVALID"))

    def test_cli_prints_the_route_and_exits_one_on_a_missing_draft(self):
        argv = ["--message", str(self.eml("partial")), "--campaign-root", str(self.tmp / "campaign"),
                "--out", str(self.tmp / "out"), "--seed", str(self.seed("partial"))]
        with mock.patch.object(followup_engine, "run", return_value={}), contextlib.redirect_stdout(io.StringIO()) as out:
            code = e2e_proof.main(argv + ["--label", "cli"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out.getvalue())["manual_steps"], [{"stage": "followups", "why": "FOLLOWUP_DRAFT_MISSING"}])

    def test_bad_followup_blocks_refuse_before_any_write(self):
        for changes, code in (({"autonomy": "on"}, "SEED_INVALID"), ({"asked": " "}, "SEED_INVALID"),
                              ({"asked": "x" * (16 * 1024 + 1)}, "SEED_INVALID"), ({"source_sha256": "AB"}, "SEED_INVALID"),
                              ({"source_sha256": "0" * 64}, "SEED_FOLLOWUP_SOURCE_INVALID")):
            with self.subTest(changes=list(changes)):
                label = "bad%d" % len(list((self.tmp / "out").glob("bad*")) if (self.tmp / "out").exists() else [])
                report = self.replay("partial", label, **changes)
                self.assertEqual(next(s for s in report["stages"] if s["stage"] == "seed")["code"], code)
                self.assertEqual(report["result"], "FAIL")
                project = self.tmp / "out" / label / "project"
                self.assertFalse((project / "MASTER-REQUEST-TRACKER.csv").exists())
                self.assertFalse((project / "followup-settings.json").exists())
                db = sqlite3.connect(str(project / "processing-v1" / "ledger.sqlite"))
                self.assertEqual(db.execute("SELECT (SELECT COUNT(*) FROM agencies) + (SELECT COUNT(*) FROM joins)").fetchone()[0], 0)
                db.close()

    def test_the_eml_itself_is_not_an_admissible_follow_up_source(self):
        eml_sha = hashlib.sha256(self.eml("partial").read_bytes()).hexdigest()
        report = self.replay("partial", "emlsrc", source_sha256=eml_sha)
        self.assertEqual(next(s for s in report["stages"] if s["stage"] == "seed")["code"], "SEED_FOLLOWUP_SOURCE_INVALID")

    def test_legacy_seed_without_the_block_has_no_followups_stage_or_extra_files(self):
        doc = json.loads((FIXTURES / "partial.seed.json").read_text())
        del doc["followup"]
        legacy = self.tmp / "legacy.seed.json"
        legacy.write_text(json.dumps(doc))
        with contextlib.redirect_stdout(io.StringIO()):
            report = e2e_proof.replay(self.eml("partial"), self.tmp / "out", self.tmp / "campaign", label="legacy", seed=legacy)
        self.assertNotIn("followups", [s["stage"] for s in report["stages"]])
        self.assertFalse((self.tmp / "out" / "legacy" / "project" / "followup-settings.json").exists())
        self.assertFalse((self.tmp / "out" / "legacy" / "project" / "MASTER-REQUEST-TRACKER.csv").exists())


if __name__ == "__main__":
    unittest.main()
