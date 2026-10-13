"""Synthetic proposal persistence checks; no extraction or independent review claims."""
import copy
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from campaign_tool.records import publish
from campaign_tool.records.ledger import store
from campaign_tool.records.run import Pipeline, RunError, encoded, root_lock, sha


class ProposalProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="proposal-provenance-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.pipeline = Pipeline(self.base / "root")
        self.root = self.pipeline.root
        self.subject = sha(b"synthetic provenance fixture")
        self.proposal_id = "prop_" + self.subject[:16]
        self.public = self.root.sub("proposals/public") / (self.proposal_id + ".md")
        self.private = self.root.sub("proposals/private") / (self.proposal_id + ".manifest.json")
        with store.ledger(self.root.ledger) as con:
            con.execute("INSERT INTO originals VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (self.subject, 28, "text/plain", "2000-01-01T00:00:00Z", "record", "in_scope",
                         "synthetic", "preserved", None, "{}"))
            con.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?)",
                        ("fixture", "synthetic", "2000-01-01T00:00:00Z", None, "fixture", None,
                         None, None, "completed", "{}"))
            con.commit()
        self.content, self.states = self.revision(1)
        self.record(self.content, self.states)

    def revision(self, number, *, public_markdown="# Synthetic record\n"):
        states = {}
        with store.ledger(self.root.ledger) as con:
            for stage in store.STAGES:
                receipt = sha((stage + ":" + str(number)).encode())
                con.execute("INSERT OR IGNORE INTO receipts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (receipt, stage, self.subject, "fixture", "synthetic", "pass", "{}", "[]",
                             "Synthetic persistence fixture only", "fixture", "[]", "2000-01-01T00:00:00Z",
                             None, "synthetic"))
                if number == 1:
                    con.execute("INSERT INTO stage_state VALUES(?,?,?,?,?,?,?,?)",
                                (self.subject, stage, "done", receipt, "fixture", "2000-01-01T00:00:00Z",
                                 "fixture", None))
                elif stage != "preserve":
                    con.execute("UPDATE stage_state SET receipt_sha256=? WHERE original_sha256=? AND stage=?",
                                (receipt, self.subject, stage))
                states[stage] = {"status": "done", "receipt_sha256": receipt}
            con.commit()
        manifest = {"schema": "records-proposal-manifest-v1", "original_sha256": self.subject,
                    "tier": "A", "review_receipt_sha256": states["review"]["receipt_sha256"],
                    "compare_receipt_sha256": states["compare"]["receipt_sha256"],
                    "catalog_receipt_sha256": states["catalog"]["receipt_sha256"]}
        return {"public_markdown": public_markdown, "public_content_sha256": sha(public_markdown.encode()),
                "manifest": manifest, "manifest_sha256": sha(encoded(manifest))}, states

    def record(self, content, states):
        with root_lock(self.root.path):
            return self.pipeline._record_proposal(self.subject, content, states)

    def proposal(self):
        with store.ledger(self.root.ledger, readonly=True) as con:
            return dict(con.execute("SELECT * FROM proposals WHERE id=?", (self.proposal_id,)).fetchone())

    def revisions(self):
        with store.ledger(self.root.ledger, readonly=True) as con:
            return [dict(row) for row in con.execute("SELECT * FROM proposal_revisions WHERE proposal_id=?",
                                                    (self.proposal_id,))]

    def approve(self):
        return publish.approve(self.root.path, self.proposal_id, owner_id="synthetic-owner")

    def test_unchanged_text_rereview_rebinds_manifest_and_receipts(self):
        self.approve()
        before = self.proposal()
        content, states = self.revision(2)
        self.assertEqual(content["public_content_sha256"], before["public_content_sha256"])
        self.record(content, states)
        after = self.proposal()
        self.assertEqual(after["manifest_sha256"], sha(self.private.read_bytes()))
        self.assertEqual(after["privacy_receipt_sha256"], states["privacy"]["receipt_sha256"])
        self.assertEqual(after["independent_receipt_sha256"], states["review"]["receipt_sha256"])
        self.assertEqual((after["owner_approval"], after["approved_content_sha256"]), ("none", None))
        self.assertFalse(self.approve()["reused"])

    def test_exact_replay_preserves_owner_approval(self):
        self.approve()
        before = self.proposal()
        self.record(self.content, self.states)
        self.assertEqual(self.proposal(), before)
        self.assertTrue(self.approve()["reused"])

    def test_manifest_only_change_resets_owner_approval(self):
        self.approve()
        content = copy.deepcopy(self.content)
        content["manifest"]["revision_note"] = "Synthetic provenance correction"
        content["manifest_sha256"] = sha(encoded(content["manifest"]))
        self.record(content, self.states)
        self.assertEqual(self.proposal()["owner_approval"], "none")
        self.assertEqual(self.proposal()["manifest_sha256"], content["manifest_sha256"])

    def test_privacy_receipt_only_change_resets_owner_approval(self):
        self.approve()
        _, fresh_states = self.revision(2)
        self.record(self.content, self.states)
        proposal = self.proposal()
        self.assertEqual(proposal["owner_approval"], "none")
        self.assertEqual(proposal["privacy_receipt_sha256"], fresh_states["privacy"]["receipt_sha256"])
        self.assertEqual(proposal["independent_receipt_sha256"], self.states["review"]["receipt_sha256"])

    def test_review_receipt_only_change_resets_owner_approval(self):
        self.approve()
        _, fresh_states = self.revision(2)
        with mock.patch.object(self.pipeline, "_states", return_value=self.states):
            self.record(self.content, fresh_states)
        self.assertEqual(self.proposal()["owner_approval"], "none")
        self.assertEqual(self.proposal()["independent_receipt_sha256"], fresh_states["review"]["receipt_sha256"])

    def test_text_change_still_requires_new_approval(self):
        self.approve()
        content, states = self.revision(2, public_markdown="# Corrected synthetic record\n")
        self.record(content, states)
        self.assertEqual(self.proposal()["owner_approval"], "none")
        self.assertEqual(self.public.read_bytes(), content["public_markdown"].encode())
        self.assertFalse(self.approve()["reused"])

    def test_file_write_crash_clears_approval_before_either_replacement_and_replay_repairs(self):
        self.approve()
        for revision, fail_at in ((2, 1), (3, 2)):
            with self.subTest(fail_at=fail_at):
                content, states = self.revision(revision)
                replace = os.replace
                calls = []

                def interrupt(*args, **kwargs):
                    calls.append(args)
                    self.assertEqual(self.proposal()["owner_approval"], "none")
                    if len(calls) == fail_at:
                        raise OSError("synthetic interrupted write")
                    return replace(*args, **kwargs)

                with mock.patch("campaign_tool.records.run.os.replace", side_effect=interrupt):
                    with self.assertRaisesRegex(OSError, "synthetic interrupted write"):
                        self.record(content, states)
                proposal = self.proposal()
                self.assertEqual((proposal["owner_approval"], proposal["approved_content_sha256"]), ("none", None))
                pending = [row for row in self.revisions() if row["state"] == "pending"]
                self.assertEqual(len(pending), 1)
                self.assertEqual(bytes(pending[0]["manifest_content"]), encoded(content["manifest"]))
                self.assertEqual(list(self.root.sub("proposals").rglob("*.tmp")), [])
                self.record(content, states)
                self.assertEqual(sha(self.private.read_bytes()), proposal["manifest_sha256"])
                self.assertTrue(all(row["state"] == "complete" for row in self.revisions()))
                self.assertFalse(self.approve()["reused"])

    def test_missing_files_are_reconstructed_from_exact_reviewed_payload(self):
        self.approve()
        before = self.proposal()
        self.public.unlink()
        self.private.unlink()
        self.record(self.content, self.states)
        self.assertEqual(self.proposal(), before)
        self.assertEqual(sha(self.public.read_bytes()), before["public_content_sha256"])
        self.assertEqual(sha(self.private.read_bytes()), before["manifest_sha256"])
        self.assertTrue(self.approve()["reused"])

    def test_replay_rejects_payload_hash_mismatch_before_touching_files(self):
        self.approve()
        before = self.proposal()
        for field in ("public_content_sha256", "manifest_sha256"):
            with self.subTest(field=field):
                content = copy.deepcopy(self.content)
                content[field] = "0" * 64
                with self.assertRaisesRegex(RunError, "proposal_content_hash_mismatch"):
                    self.record(content, self.states)
                self.assertEqual(self.proposal(), before)
                self.assertEqual(sha(self.private.read_bytes()), before["manifest_sha256"])

    def test_symlink_output_is_refused_without_touching_external_bytes(self):
        victim = self.base / "untouched.txt"
        victim.write_bytes(b"synthetic unrelated bytes")
        for path in (self.public, self.private):
            with self.subTest(path=path.name):
                original = path.read_bytes()
                path.unlink()
                path.symlink_to(victim)
                with self.assertRaisesRegex(RunError, "proposal_file_not_regular"):
                    self.record(self.content, self.states)
                self.assertEqual(victim.read_bytes(), b"synthetic unrelated bytes")
                self.assertTrue(path.is_symlink())
                path.unlink()
                path.write_bytes(original)

    def test_symlink_destination_directory_is_refused(self):
        original = self.root.sub("proposals/private")
        moved = self.root.sub("proposals/private-saved")
        original.rename(moved)
        original.symlink_to(moved, target_is_directory=True)
        before = self.private.read_bytes()
        with self.assertRaises(OSError):
            self.record(self.content, self.states)
        self.assertEqual(self.private.read_bytes(), before)

    def test_predictable_temporary_symlink_is_never_followed(self):
        victim = self.base / "untouched.txt"
        victim.write_bytes(b"synthetic unrelated bytes")
        for path in (self.public, self.private):
            path.with_suffix(path.suffix + ".tmp").symlink_to(victim)
        self.record(self.content, self.states)
        self.assertEqual(victim.read_bytes(), b"synthetic unrelated bytes")

    def test_exact_replay_cannot_restore_rejected_approval(self):
        self.approve()
        publish.approve(self.root.path, self.proposal_id, owner_id="synthetic-owner", reject=True)
        self.record(self.content, self.states)
        self.assertEqual(self.proposal()["owner_approval"], "rejected")
        with self.assertRaisesRegex(publish.PublishError, "owner_approval_required"):
            publish.publish(self.root.path, self.proposal_id, staging=self.base / "staging")

    def test_revision_history_tracks_provenance_separately_from_public_text(self):
        self.approve()
        first = self.revisions()[0]
        content, states = self.revision(2)
        self.record(content, states)
        revisions = self.revisions()
        self.assertEqual(len(revisions), 2)
        self.assertEqual({row["public_content_sha256"] for row in revisions}, {first["public_content_sha256"]})
        self.assertEqual(len({row["revision_id"] for row in revisions}), 2)
        with store.ledger(self.root.ledger) as con:
            con.execute("PRAGMA recursive_triggers=OFF")
            for sql in ("UPDATE proposal_revisions SET manifest_content=x'00'", "DELETE FROM proposal_revisions",
                        "INSERT OR REPLACE INTO proposal_revisions SELECT * FROM proposal_revisions"):
                with self.assertRaisesRegex(sqlite3.IntegrityError, "proposal revision is immutable"):
                    con.execute(sql)
        self.record(content, states)
        self.assertEqual(len(self.revisions()), 2)

    def test_new_revision_supersedes_interrupted_older_intent(self):
        self.approve()
        interrupted, interrupted_states = self.revision(2)
        with mock.patch("campaign_tool.records.run.os.replace", side_effect=OSError("synthetic crash")):
            with self.assertRaisesRegex(OSError, "synthetic crash"):
                self.record(interrupted, interrupted_states)
        pending = next(row for row in self.revisions() if row["state"] == "pending")
        current, states = self.revision(3)
        self.record(current, states)
        revisions = {row["revision_id"]: row for row in self.revisions()}
        self.assertEqual(revisions[pending["revision_id"]]["state"], "superseded")
        self.assertFalse(any(row["state"] == "pending" for row in revisions.values()))
        self.assertEqual(sha(self.private.read_bytes()), current["manifest_sha256"])
        self.assertEqual(self.proposal()["owner_approval"], "none")
        self.assertFalse(self.approve()["reused"])

    def test_approval_recovers_interrupted_provenance_without_pipeline_replay(self):
        self.approve()
        content, states = self.revision(2)
        with mock.patch("campaign_tool.records.run.os.replace", side_effect=OSError("synthetic crash")):
            with self.assertRaisesRegex(OSError, "synthetic crash"):
                self.record(content, states)
        self.assertEqual(self.proposal()["owner_approval"], "none")
        self.assertNotEqual(sha(self.private.read_bytes()), content["manifest_sha256"])
        self.assertFalse(self.approve()["reused"])
        self.assertEqual(sha(self.private.read_bytes()), content["manifest_sha256"])
        self.assertEqual(sha(self.public.read_bytes()), content["public_content_sha256"])
        self.assertTrue(all(row["state"] == "complete" for row in self.revisions()))
        with store.ledger(self.root.ledger, readonly=True) as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM owner_decisions WHERE decision='approved'").fetchone()[0], 2)

    def test_unchanged_text_republication_has_new_provenance_authority(self):
        self.approve()
        staging = self.base / "staging"
        first = publish.publish(self.root.path, self.proposal_id, staging=staging)
        content, states = self.revision(2)
        self.record(content, states)
        self.assertFalse(self.approve()["reused"])
        second = publish.publish(self.root.path, self.proposal_id, staging=staging)
        self.assertEqual(first["content_sha256"], second["content_sha256"])
        self.assertNotEqual(first["publication_id"], second["publication_id"])
        self.assertNotEqual(first["version"], second["version"])
        self.assertFalse(second["reused"])
        self.assertEqual((second["action"], second["previous_version"]), ("correct", first["version"]))
        manifest = json.loads((staging / "releases" / second["version"] / "manifest.json").read_bytes())
        self.assertEqual(manifest["authority"], {
            "approved_content_sha256": content["public_content_sha256"],
            "manifest_sha256": content["manifest_sha256"],
            "privacy_receipt_sha256": states["privacy"]["receipt_sha256"],
            "independent_receipt_sha256": states["review"]["receipt_sha256"],
        })
        replay = publish.publish(self.root.path, self.proposal_id, staging=staging)
        self.assertTrue(replay["reused"])
        self.assertEqual(replay["version"], second["version"])


if __name__ == "__main__":
    unittest.main()
