"""REC-AUTO-11: the e2e-proof replay runs every stage with no manual step and sends nothing."""
import json
import tempfile
import unittest
from pathlib import Path

from campaign_tool.records import e2e_proof, notify_text
from tests.records.test_run_slice import synthetic_email


class E2EProofTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="e2e-proof-")
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        content = self.tmp / "campaign" / "content"
        content.mkdir(parents=True)
        (content / "agencies.json").write_text(json.dumps(
            [{"agency_id": "a-1", "name": "Agency", "profile": {"status": "Records requested", "summary": "s"}}]))
        self.campaign = self.tmp / "campaign"
        self.message = self.tmp / "m.eml"
        self.message.write_bytes(synthetic_email())

    def test_replay_passes_every_stage_twice_without_manual_steps(self):
        first = e2e_proof.replay(self.message, self.tmp / "out", self.campaign, label="one")
        second = e2e_proof.replay(self.message, self.tmp / "out", self.campaign, label="two")
        for report in (first, second):
            self.assertEqual(report["result"], "PASS")
            self.assertEqual(report["manual_steps"], [])
            self.assertIs(report["sent"], False)
            self.assertIs(report["published"], False)
            self.assertEqual([s["stage"] for s in report["stages"]], [
                "arrival_trigger", "save_mail", "extract", "digest_gates", "outputs", "next_steps",
                "telegram_text", "console"])
        self.assertTrue((self.tmp / "out" / "one" / "telegram-summary.txt").is_file())
        self.assertTrue((self.tmp / "out" / "one" / "proof-report.md").is_file())

    def test_replay_refuses_existing_output_dir(self):
        e2e_proof.replay(self.message, self.tmp / "out", self.campaign, label="one")
        with self.assertRaises(e2e_proof.ProofError) as caught:
            e2e_proof.replay(self.message, self.tmp / "out", self.campaign, label="one")
        self.assertEqual(caught.exception.code, "OUT_EXISTS")

    def test_message_required(self):
        with self.assertRaises(e2e_proof.ProofError) as caught:
            e2e_proof.pick_message(None, None, 1)
        self.assertEqual(caught.exception.code, "MESSAGE_REQUIRED")

    def test_notification_composer_is_inside_the_installed_package(self):
        # The installed engine ships campaign_tool only; the composer must not be read from deploy/.
        self.assertEqual(Path(notify_text.__file__).resolve().parent, Path(e2e_proof.__file__).resolve().parent)
        self.assertNotIn("records-runtime", Path(e2e_proof.__file__).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
