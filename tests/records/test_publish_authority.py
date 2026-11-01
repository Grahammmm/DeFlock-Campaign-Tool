"""Synthetic authority, history and interrupted-publication regressions."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from campaign_tool.records import publish
from campaign_tool.records.ledger import store
from campaign_tool.records.run import Root, RunError, root_lock
from contextlib import closing


class PublicationFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="publish-authority-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.root = Root(self.base / "root").prepare()
        self.staging = self.base / "staging"
        self.subject = publish.sha(b"synthetic original")
        self.proposal = "prop_" + self.subject[:16]
        self.receipt = publish.sha(b"synthetic privacy receipt")
        manifest = publish.encoded({"original_sha256": self.subject})
        self.public_path = self.root.sub("proposals/public") / (self.proposal + ".md")
        self.public_path.write_bytes(b"Synthetic reviewed text.\n")
        (self.root.sub("proposals/private") / (self.proposal + ".manifest.json")).write_bytes(manifest)
        with store.ledger(self.root.ledger) as con:
            con.execute("INSERT INTO originals(sha256,bytes,role,scope,preservation_status,provenance_json) VALUES(?,0,'fixture','fixture','fixture','{}')", (self.subject,))
            con.execute("INSERT INTO runs(run_id,kind,started_at,engine_version,status,summary) VALUES('fixture','fixture','2000','fixture','complete','{}')")
            con.execute("INSERT INTO receipts(sha256,stage,subject_sha256,reviewer_id,role,verdict,coverage,locators,rationale,model_or_tool,input_hashes,created_at_tz,path) VALUES(?,'privacy',?,'fixture','privacy','pass','{}','[]','fixture','fixture','{}','2000','fixture')", (self.receipt, self.subject))
            con.execute("INSERT INTO stage_state VALUES(?,'privacy','done',?,'fixture','2000','fixture',NULL)", (self.subject, self.receipt))
            con.execute("INSERT INTO proposals(id,tier,public_content_sha256,manifest_sha256,privacy_receipt_sha256,owner_approval) VALUES(?,'A',?,?,?,'none')", (self.proposal, publish.sha(self.public_path.read_bytes()), publish.sha(manifest), self.receipt))
            con.commit()

    def approve(self):
        return publish.approve(self.root.path, self.proposal, owner_id="synthetic-owner")

    def stage(self):
        return publish.publish(self.root.path, self.proposal, staging=self.staging)

    def rollback(self):
        return publish.rollback(self.root.path, self.proposal, staging=self.staging)

    def change_text(self):
        self.public_path.write_bytes(b"Synthetic reviewed correction.\n")
        with store.ledger(self.root.ledger) as con:
            con.execute("UPDATE proposals SET owner_approval='none',approved_content_sha256=NULL,public_content_sha256=? WHERE id=?", (publish.sha(self.public_path.read_bytes()), self.proposal))
            con.commit()
        self.approve()

    def two_releases(self):
        self.approve()
        first = self.stage()
        self.change_text()
        return first, self.stage()

    def current_bytes(self):
        return ((self.staging / "current.json").read_bytes(),
                (self.staging / "content" / (self.proposal + ".md")).read_bytes())


class AuthorityRegressionTests(PublicationFixture):
    def test_reopen_cannot_interleave_after_authority_check(self):
        self.approve()
        original = publish._privacy_current
        attempts = []
        def interleave(con, root, proposal):
            result = original(con, root, proposal)
            try:
                # Deterministic authority-clear interleave uses reopen's root-lock contract.
                with root_lock(root.path), store.ledger(root.ledger) as other:
                    other.execute("UPDATE proposals SET owner_approval='none',approved_content_sha256=NULL WHERE id=?", (proposal["id"],))
                    other.commit()
            except RunError as error:
                attempts.append(str(error))
            return result
        with patch.object(publish, "_privacy_current", side_effect=interleave):
            self.stage()
        self.assertTrue(attempts)
        self.assertEqual(set(attempts), {"root_locked"})
        with store.ledger(self.root.ledger) as con:
            self.assertEqual(con.execute("SELECT owner_approval FROM proposals").fetchone()[0], "approved")

    def test_rollback_refuses_modified_historical_bytes_before_output_change(self):
        first, _ = self.two_releases()
        before = self.current_bytes()
        (self.staging / "releases" / first["version"] / (self.proposal + ".md")).write_bytes(b"Unreviewed synthetic tamper")
        with self.assertRaises(publish.PublishError):
            self.rollback()
        self.assertEqual(self.current_bytes(), before)

    def test_rollback_refuses_missing_historical_file_before_output_change(self):
        first, _ = self.two_releases()
        before = self.current_bytes()
        (self.staging / "releases" / first["version"] / (self.proposal + ".md")).unlink()
        with self.assertRaises(publish.PublishError):
            self.rollback()
        self.assertEqual(self.current_bytes(), before)

    def test_rollback_refuses_manifest_tamper_even_when_content_hash_is_updated(self):
        first, _ = self.two_releases()
        before = self.current_bytes()
        folder = self.staging / "releases" / first["version"]
        tamper = b"Unreviewed synthetic history"
        (folder / (self.proposal + ".md")).write_bytes(tamper)
        manifest = json.loads((folder / "manifest.json").read_bytes())
        manifest["content_sha256"] = publish.sha(tamper)
        (folder / "manifest.json").write_bytes(publish.encoded(manifest))
        with self.assertRaisesRegex(publish.PublishError, "release_manifest_changed"):
            self.rollback()
        self.assertEqual(self.current_bytes(), before)

    def test_current_predecessor_pointer_cannot_select_another_release(self):
        first, _ = self.two_releases()
        current_path = self.staging / "current.json"
        current = json.loads(current_path.read_bytes())
        current["proposals"][self.proposal]["previous_version"] = None
        current_path.write_bytes(publish.encoded(current))
        before = self.current_bytes()
        with self.assertRaisesRegex(publish.PublishError, "staging_current_unverified"):
            self.rollback()
        self.assertEqual(self.current_bytes(), before)

    def test_symlink_history_files_and_ancestors_are_refused(self):
        first, _ = self.two_releases()
        before = self.current_bytes()
        folder = self.staging / "releases" / first["version"]
        for path in (folder / "manifest.json", folder / (self.proposal + ".md"), folder):
            with self.subTest(path=path.name):
                moved = path.with_name(path.name + ".saved")
                path.rename(moved)
                path.symlink_to(moved, target_is_directory=moved.is_dir())
                try:
                    with self.assertRaisesRegex(publish.PublishError, "symlink"):
                        self.rollback()
                    self.assertEqual(self.current_bytes(), before)
                finally:
                    path.unlink()
                    moved.rename(path)

    def test_missing_historical_manifest_is_not_withdrawal(self):
        first, _ = self.two_releases()
        before = self.current_bytes()
        (self.staging / "releases" / first["version"] / "manifest.json").unlink()
        with self.assertRaisesRegex(publish.PublishError, "release_manifest_missing"):
            self.rollback()
        self.assertEqual(self.current_bytes(), before)

    def test_missing_current_output_is_not_silently_repaired(self):
        self.approve()
        self.stage()
        content = self.staging / "content" / (self.proposal + ".md")
        content.unlink()
        with self.assertRaisesRegex(publish.PublishError, "staging_content_changed"):
            self.stage()

    def test_rejected_proposal_and_changed_privacy_receipt_block_publish(self):
        self.approve()
        publish.approve(self.root.path, self.proposal, owner_id="synthetic-owner", reject=True)
        with self.assertRaisesRegex(publish.PublishError, "owner_approval_required"):
            self.stage()
        self.approve()
        with store.ledger(self.root.ledger) as con:
            con.execute("UPDATE stage_state SET status='pending' WHERE original_sha256=? AND stage='privacy'", (self.subject,))
            con.commit()
        with self.assertRaisesRegex(publish.PublishError, "privacy_clearance_not_current"):
            self.stage()

    def test_all_authority_mutations_respect_existing_root_lock(self):
        self.approve()
        self.stage()
        with root_lock(self.root.path):
            for operation in (self.approve, self.stage, self.rollback,
                              lambda: publish.reopen(self.root.path, self.proposal, reason="synthetic")):
                with self.subTest(operation=operation), self.assertRaisesRegex(RunError, "root_locked"):
                    operation()

    def test_revocation_during_prepared_commit_gap_prevents_any_new_artifact(self):
        self.approve()
        prepare = publish._prepare
        def revoke(*args, **kwargs):
            result = prepare(*args, **kwargs)
            with store.ledger(self.root.ledger) as con:
                con.execute("UPDATE proposals SET owner_approval='none',approved_content_sha256=NULL WHERE id=?", (self.proposal,))
                con.commit()
            return result
        with patch.object(publish, "_prepare", side_effect=revoke):
            with self.assertRaisesRegex(publish.PublishError, "owner_approval_required"):
                self.stage()
        self.assertFalse((self.staging / "current.json").exists())
        self.assertEqual(list((self.staging / "releases").iterdir()), [])
        with self.assertRaisesRegex(publish.PublishError, "owner_approval_required"):
            self.stage()  # Recovery aborts the intent; it never replays its old authority.
        with store.ledger(self.root.ledger) as con:
            self.assertEqual([r[0] for r in con.execute("SELECT state FROM staging_journal")], ["aborted"])
            self.assertEqual(con.execute("SELECT count(*) FROM publications").fetchone()[0], 0)

    def test_crash_after_each_output_write_recovers_previous_state_before_rejection(self):
        self.approve()
        self.stage()
        before = self.current_bytes()
        self.change_text()
        write = publish._write
        for fail_at in range(1, 5):
            with self.subTest(fail_at=fail_at):
                calls = []
                def crash(path, data):
                    write(path, data)
                    calls.append(path)
                    if len(calls) == fail_at:
                        raise SystemExit("synthetic process death")
                with patch.object(publish, "_write", side_effect=crash):
                    with self.assertRaisesRegex(SystemExit, "synthetic process death"):
                        self.stage()
                # Reject is itself an authority mutation and must recover before recording a decision.
                publish.approve(self.root.path, self.proposal, owner_id="synthetic-owner", reject=True)
                self.assertEqual(self.current_bytes(), before)
                with store.ledger(self.root.ledger) as con:
                    self.assertEqual(con.execute("SELECT count(*) FROM staging_journal WHERE state='prepared'").fetchone()[0], 0)
                    self.assertEqual(con.execute("SELECT count(*) FROM publications").fetchone()[0], 1)
                self.approve()
        final = self.stage()
        self.assertFalse(final["reused"])
        self.assertEqual((self.staging / "content" / (self.proposal + ".md")).read_bytes(), self.public_path.read_bytes())

    def test_interrupted_rollback_recovers_current_correction(self):
        self.two_releases()
        before = self.current_bytes()
        write = publish._write
        def crash(path, data):
            write(path, data)
            if path.name == "current.json":
                raise SystemExit("synthetic rollback death")
        with patch.object(publish, "_write", side_effect=crash), self.assertRaises(SystemExit):
            self.rollback()
        self.approve()
        self.assertEqual(self.current_bytes(), before)
        self.assertFalse(self.rollback()["withdrawn"])
        self.assertTrue(self.rollback()["withdrawn"])

    def test_recovery_refuses_unexplained_output_edits(self):
        self.approve()
        write = publish._write
        def crash(path, data):
            write(path, data)
            if path.name == "current.json":
                raise SystemExit("synthetic death")
        with patch.object(publish, "_write", side_effect=crash), self.assertRaises(SystemExit):
            self.stage()
        path = self.staging / "content" / (self.proposal + ".md")
        path.write_bytes(b"unexplained synthetic edit")
        before = self.current_bytes()
        with self.assertRaisesRegex(publish.PublishError, "interrupted_staging_changed"):
            self.approve()
        self.assertEqual(self.current_bytes(), before)

    def test_publish_withdraw_republish_retains_immutable_release_bytes(self):
        self.approve()
        first = self.stage()
        manifest = self.staging / "releases" / first["version"] / "manifest.json"
        before = manifest.read_bytes()
        self.assertTrue(self.stage()["reused"])
        self.assertTrue(self.rollback()["withdrawn"])
        again = self.stage()
        self.assertNotEqual(again["version"], first["version"])
        self.assertEqual(manifest.read_bytes(), before)
        self.assertEqual(len(list((self.staging / "releases").iterdir())), 3)

    def test_staging_target_has_independent_publication_identity(self):
        self.approve()
        first = self.stage()
        other = publish.publish(self.root.path, self.proposal, staging=self.base / "other-staging")
        self.assertNotEqual(first["publication_id"], other["publication_id"])
        self.assertTrue(self.stage()["reused"])
        rolled = self.rollback()
        with store.ledger(self.root.ledger) as con:
            rows = con.execute("SELECT rollback_ref FROM publications ORDER BY site_pr_ref").fetchall()
            self.assertEqual({row[0] for row in rows}, {None, rolled["version"]})

    def test_target_lock_prevents_second_root_or_external_cooperative_writer(self):
        self.approve()
        publish._staging(self.staging)
        with root_lock(self.staging), self.assertRaisesRegex(RunError, "root_locked"):
            self.stage()

    def test_legacy_unjournaled_staging_is_not_blessed_on_republish(self):
        self.approve()
        publish._staging(self.staging)
        (self.staging / "current.json").write_bytes(publish.encoded({"proposals": {}}))
        with self.assertRaisesRegex(publish.PublishError, "staging_current_unverified"):
            self.stage()

    def test_predictable_temporary_symlink_is_not_used(self):
        self.approve()
        publish._staging(self.staging)
        outside = self.base / "unrelated"
        outside.write_bytes(b"untouched")
        (self.staging / "current.json.tmp").symlink_to(outside)
        self.stage()
        self.assertEqual(outside.read_bytes(), b"untouched")

    def test_directory_swapped_after_precheck_cannot_redirect_write(self):
        self.approve()
        publish._staging(self.staging)
        outside = self.base / "outside"
        outside.mkdir()
        directory = self.staging / "content"
        moved = self.staging / "content-old"
        safe = publish._safe_path
        def swap(path):
            result = safe(path)
            if Path(path) == directory and not moved.exists():
                directory.rename(moved)
                directory.symlink_to(outside, target_is_directory=True)
            return result
        with patch.object(publish, "_safe_path", side_effect=swap):
            with self.assertRaises((OSError, publish.PublishError)):
                publish._write(directory / "synthetic.md", b"do not redirect")
        self.assertEqual(list(outside.iterdir()), [])

    def test_sqlite_authority_writer_is_blocked_during_validation_and_apply(self):
        self.approve()
        check = publish._privacy_current
        attempts = []
        def interleave(con, root, proposal):
            result = check(con, root, proposal)
            with closing(sqlite3.connect(root.ledger, timeout=0)) as other, other:
                with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
                    other.execute("UPDATE proposals SET owner_approval='none',approved_content_sha256=NULL")
            attempts.append(True)
            return result
        with patch.object(publish, "_privacy_current", side_effect=interleave):
            self.stage()
        self.assertEqual(len(attempts), 2)

    def test_crash_during_recovery_can_be_retried(self):
        self.two_releases()
        before = self.current_bytes()
        write = publish._write
        def crash(path, data):
            write(path, data)
            if path.name == "current.json":
                raise SystemExit("synthetic interruption")
        with patch.object(publish, "_write", side_effect=crash), self.assertRaises(SystemExit):
            self.rollback()
        # The restoring current pointer itself can be durable before journal abort.
        with patch.object(publish, "_write", side_effect=crash), self.assertRaises(SystemExit):
            self.approve()
        self.approve()
        self.assertEqual(self.current_bytes(), before)

    def test_staging_journal_bindings_cannot_be_mutated_or_replaced(self):
        self.approve()
        result = self.stage()
        with store.ledger(self.root.ledger) as con:
            con.execute("PRAGMA recursive_triggers=OFF")
            row = con.execute("SELECT * FROM staging_journal").fetchone()
            for sql, args in (("UPDATE staging_journal SET content=? WHERE version=?", (b"tampered", result["version"])),
                              ("DELETE FROM staging_journal", ()),
                              ("INSERT OR REPLACE INTO staging_journal VALUES(?,?,?,?,?,?,?,?,?)", tuple(row))):
                with self.subTest(sql=sql), self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                    con.execute(sql, args)
