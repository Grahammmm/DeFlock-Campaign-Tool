"""Owner approval bound to exact bytes; staged publish, correction and rollback are idempotent."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from campaign_tool.records import publish
from campaign_tool.records.run import Pipeline, status
from tests.records.test_run_slice import synthetic_email, REPO


class PublishTests(unittest.TestCase):
    def setUp(self):
        os.umask(0o077)
        self.tmp = tempfile.TemporaryDirectory(prefix="records-publish-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        os.chmod(self.base, 0o700)
        self.inbox = self.base / "inbox"
        self.inbox.mkdir(mode=0o700)
        (self.inbox / "response.eml").write_bytes(synthetic_email())
        self.root = self.base / "root"
        self.staging = self.base / "staging"
        Pipeline(self.root).run(self.inbox)
        self.proposals = [p["id"] for p in status(self.root)["proposals"]]
        self.assertEqual(len(self.proposals), 2)
        self.policy = next(p for p in self.proposals if "retained for 90 days" in self.public(p))

    def public(self, proposal_id):
        return (self.root / "proposals/public" / (proposal_id + ".md")).read_text()

    def ledger(self, sql, values=()):
        con = sqlite3.connect(self.root / "ledger.sqlite")
        try:
            return con.execute(sql, values).fetchall()
        finally:
            con.close()

    def test_publish_requires_owner_approval_bound_to_exact_bytes(self):
        with self.assertRaisesRegex(publish.PublishError, "owner_approval_required"):
            publish.publish(self.root, self.policy, staging=self.staging)
        decision = publish.approve(self.root, self.policy, owner_id="owner@example.invalid")
        self.assertEqual(decision["decision"], "approved")
        self.assertFalse(decision["reused"])
        self.assertTrue(publish.approve(self.root, self.policy, owner_id="owner@example.invalid")["reused"])
        row = self.ledger("SELECT owner_approval,approved_content_sha256,public_content_sha256 FROM proposals WHERE id=?", (self.policy,))[0]
        self.assertEqual(row[0], "approved")
        self.assertEqual(row[1], row[2])
        self.assertEqual(self.ledger("SELECT decision,owner_id FROM owner_decisions"), [("approved", "owner@example.invalid")])
        # Tampering with the public file after approval is refused at publish time.
        path = self.root / "proposals/public" / (self.policy + ".md")
        original = path.read_bytes()
        path.write_bytes(original + b"\nAdded after approval.\n")
        with self.assertRaisesRegex(publish.PublishError, "approved_hash_mismatch"):
            publish.publish(self.root, self.policy, staging=self.staging)
        path.write_bytes(original)
        result = publish.publish(self.root, self.policy, staging=self.staging)
        self.assertEqual(result["action"], "publish")
        self.assertFalse(result["reused"])
        staged = (self.staging / "content" / (self.policy + ".md")).read_bytes()
        self.assertEqual(staged, original)
        current = json.loads((self.staging / "current.json").read_bytes())
        self.assertEqual(current["proposals"][self.policy]["content_sha256"], result["content_sha256"])
        manifest = json.loads((self.staging / "releases" / result["version"] / "manifest.json").read_bytes())
        self.assertEqual(manifest["action"], "publish")
        self.assertIsNone(manifest["previous_version"])
        # Replay is a no-op.
        again = publish.publish(self.root, self.policy, staging=self.staging)
        self.assertTrue(again["reused"])
        self.assertEqual(again["version"], result["version"])
        self.assertEqual(len(self.ledger("SELECT id FROM publications")), 1)
        self.assertEqual(len(list((self.staging / "releases").iterdir())), 1)

    def test_rejection_blocks_publish(self):
        publish.approve(self.root, self.policy, owner_id="owner", reject=True, reason="not yet")
        with self.assertRaisesRegex(publish.PublishError, "owner_approval_required"):
            publish.publish(self.root, self.policy, staging=self.staging)
        self.assertEqual(self.ledger("SELECT owner_approval FROM proposals WHERE id=?", (self.policy,)), [("rejected",)])

    def test_approval_refused_when_public_bytes_drifted_from_privacy_review(self):
        path = self.root / "proposals/public" / (self.policy + ".md")
        path.write_bytes(path.read_bytes() + b"\nEdited.\n")
        with self.assertRaisesRegex(publish.PublishError, "public_bytes_changed_since_privacy_review"):
            publish.approve(self.root, self.policy, owner_id="owner")

    def test_correction_and_rollback(self):
        publish.approve(self.root, self.policy, owner_id="owner")
        first = publish.publish(self.root, self.policy, staging=self.staging)
        first_bytes = (self.staging / "content" / (self.policy + ".md")).read_bytes()
        # A corrected record arrives: new bytes for the same original invalidate downstream stages,
        # the pipeline re-runs privacy, the proposal's approval resets, the owner re-approves.
        subject = json.loads((self.root / "proposals/private" / (self.policy + ".manifest.json")).read_bytes())["original_sha256"]
        pipeline = Pipeline(self.root, redaction_denylist=["purged"])  # changes the public statements
        pipeline._open_stage_run()["runner"].invalidate(subject, "review", reason="synthetic_correction")
        pipeline.advance_all()
        row = self.ledger("SELECT owner_approval,public_content_sha256 FROM proposals WHERE id=?", (self.policy,))[0]
        self.assertEqual(row[0], "none")
        self.assertNotEqual(row[1], first["content_sha256"])
        with self.assertRaisesRegex(publish.PublishError, "owner_approval_required"):
            publish.publish(self.root, self.policy, staging=self.staging)
        publish.approve(self.root, self.policy, owner_id="owner")
        second = publish.publish(self.root, self.policy, staging=self.staging)
        self.assertEqual(second["action"], "correct")
        self.assertEqual(second["previous_version"], first["version"])
        self.assertNotEqual((self.staging / "content" / (self.policy + ".md")).read_bytes(), first_bytes)
        self.assertEqual(len(self.ledger("SELECT id FROM publications")), 2)
        rolled = publish.rollback(self.root, self.policy, staging=self.staging, reason="owner request")
        self.assertEqual(rolled["rolled_back"], second["version"])
        self.assertEqual(rolled["restored"], first["version"])
        self.assertFalse(rolled["withdrawn"])
        self.assertEqual((self.staging / "content" / (self.policy + ".md")).read_bytes(), first_bytes)
        self.assertEqual(self.ledger("SELECT rollback_ref FROM publications WHERE deployed_version=?", (second["version"],)),
                         [(rolled["version"],)])
        # Rolling back the original release withdraws the item from staging entirely.
        withdrawn = publish.rollback(self.root, self.policy, staging=self.staging)
        self.assertTrue(withdrawn["withdrawn"])
        self.assertFalse((self.staging / "content" / (self.policy + ".md")).exists())
        current = json.loads((self.staging / "current.json").read_bytes())
        self.assertNotIn(self.policy, current["proposals"])
        self.assertIn(self.policy, current["withdrawn"])

    def test_reopen_is_the_owner_correction_path_through_the_cli(self):
        publish.approve(self.root, self.policy, owner_id="owner")
        first = publish.publish(self.root, self.policy, staging=self.staging)
        before = self.ledger("SELECT owner_approval,public_content_sha256 FROM proposals WHERE id=?", (self.policy,))[0]
        self.assertEqual(before[0], "approved")
        result = subprocess.run([sys.executable, "-B", "-m", "campaign_tool.records", "reopen", "--root", str(self.root),
                                 "--proposal", self.policy, "--reason", "agency sent a corrected page"],
                                cwd=REPO, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(result.stdout)
        self.assertEqual(receipt["stage"], "review")
        self.assertTrue(receipt["invalidated"])
        # Approval cleared at once; review and everything after it reopened; staged release untouched.
        row = self.ledger("SELECT owner_approval,approved_content_sha256 FROM proposals WHERE id=?", (self.policy,))[0]
        self.assertEqual(tuple(row), ("none", None))
        subject = receipt["original_sha256"]
        states = dict(self.ledger("SELECT stage,status FROM stage_state WHERE original_sha256=?", (subject,)))
        self.assertEqual(states["catalog"], "done")
        self.assertNotEqual(states["review"], "done")
        self.assertNotEqual(states["privacy"], "done")
        self.assertTrue((self.staging / "content" / (self.policy + ".md")).exists())
        with self.assertRaisesRegex(publish.PublishError, "owner_approval_required"):
            publish.publish(self.root, self.policy, staging=self.staging)
        receipts = list((self.root / "proposals/private").glob(self.policy + ".reopen.*.json"))
        self.assertEqual(len(receipts), 1)
        # The next run regenerates the content with a fresh privacy receipt; re-approve; publish records a correction.
        Pipeline(self.root, redaction_denylist=["purged"]).run(self.inbox)
        after = self.ledger("SELECT owner_approval,public_content_sha256,privacy_receipt_sha256 FROM proposals WHERE id=?",
                            (self.policy,))[0]
        self.assertEqual(after[0], "none")
        self.assertNotEqual(after[1], before[1])
        publish.approve(self.root, self.policy, owner_id="owner")
        second = publish.publish(self.root, self.policy, staging=self.staging)
        self.assertEqual((second["action"], second["previous_version"]), ("correct", first["version"]))
        with self.assertRaisesRegex(publish.PublishError, "reopen_reason_required"):
            publish.reopen(self.root, self.policy, reason="  ")
        with self.assertRaisesRegex(publish.PublishError, "proposal_missing"):
            publish.reopen(self.root, "prop_nope", reason="x")

    def test_hand_edited_public_file_is_reported_as_drifted_and_never_published(self):
        self.assertEqual(status(self.root)["drifted_proposals"], [])
        publish.approve(self.root, self.policy, owner_id="owner")
        path = self.root / "proposals/public" / (self.policy + ".md")
        path.write_bytes(path.read_bytes() + b"\nhand edit\n")
        drifted = status(self.root)["drifted_proposals"]
        self.assertEqual([(d["id"], d["problem"]) for d in drifted], [(self.policy, "public_file_drifted")])
        with self.assertRaisesRegex(publish.PublishError, "approved_hash_mismatch|public_bytes_changed|hash"):
            publish.publish(self.root, self.policy, staging=self.staging)
        path.unlink()
        self.assertEqual(status(self.root)["drifted_proposals"][0]["problem"], "public_file_missing")

    def test_cli_commands(self):
        env = {**os.environ, "PYTHONPATH": str(REPO)}
        run = lambda *args: subprocess.run([sys.executable, "-B", "-m", "campaign_tool.records", *args], cwd=REPO,
                                           env=env, capture_output=True, text=True, timeout=120)
        approved = run("approve", "--root", str(self.root), "--proposal", self.policy, "--owner", "owner")
        self.assertEqual(approved.returncode, 0, approved.stderr[-1500:])
        self.assertEqual(json.loads(approved.stdout)["decision"], "approved")
        published = run("publish", "--root", str(self.root), "--proposal", self.policy, "--staging", str(self.staging))
        self.assertEqual(published.returncode, 0, published.stderr[-1500:])
        self.assertEqual(json.loads(published.stdout)["action"], "publish")
        rolled = run("publish", "--root", str(self.root), "--proposal", self.policy, "--staging", str(self.staging), "--rollback")
        self.assertEqual(rolled.returncode, 0, rolled.stderr[-1500:])
        self.assertTrue(json.loads(rolled.stdout)["withdrawn"])


if __name__ == "__main__":
    unittest.main()
