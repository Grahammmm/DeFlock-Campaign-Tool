"""Synthetic-only legacy adoption; read-only mounts are simulated, not bypassed."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from campaign_tool.records import legacy_adoption as adoption
from campaign_tool.records.intake import local_capture as capture
from campaign_tool.records.ledger import store
from campaign_tool.records.ledger.occurrence_projection import project_occurrences
from campaign_tool.records.run import Pipeline
from tests.records import test_ledger_occurrences as fixtures


class LegacyAdoptionTests(unittest.TestCase):
    def setUp(self):
        self.helper = fixtures.OccurrenceProjectionTests()
        self.helper.setUp()
        self.addCleanup(self.helper.doCleanups)
        self.base = self.helper.database.parent
        # Source trees live under the system temp root, not tempfile's TMPDIR: agent
        # runtimes point TMPDIR below a `.hermes` directory, which source_class()
        # refuses by design, so the fixture must not inherit the runner's TMPDIR.
        sources = tempfile.TemporaryDirectory(dir=os.path.realpath("/tmp"))
        self.addCleanup(sources.cleanup)
        self.sources = Path(sources.name)
        self.export = self.sources / "export"
        self.supplement = self.sources / "supplement"
        self.export.mkdir(mode=0o700)
        self.supplement.mkdir(mode=0o700)
        for root in (self.export, self.supplement):
            self.assertNotIn(".hermes", root.parts)
        self.rows = []
        for index, form in enumerate(("txt", "pdf", "png", "json", "csv", "other")):
            data = b"synthetic original" if index == 0 else ("synthetic " + form).encode()
            subject = capture.digest(data)
            if index:
                self.helper.add_doc(data.decode(), form)
                self.helper.add_occurrence(form, subject)
            source = self.export / "received" / (str(index) + ".fictional")
            source.parent.mkdir(mode=0o700, exist_ok=True)
            source.write_bytes(data)
            source.chmod(0o400)
            self.rows.append({"sha256": subject, "size": len(data), "source_path": str(source)})
        project_occurrences(self.helper.database, self.helper.capture())
        self.pipeline = Pipeline(self.base)
        self.manifest = self.base / "manifest.tsv"
        self.policy = self.base / "policy.json"
        self.config = self.base / "config.json"
        self.policy_value = {"schema": "approved-local-capture-policy-v1",
            "inventory_sha256": "a" * 64, "approval_sha256": "b" * 64,
            "originals": {row["sha256"]: {"bytes": row["size"], "role": "original",
                "scope": "in_scope", "evidence_sha256": capture.digest(capture.encoded(row))}
                for row in self.rows[:3]}}
        self.save_inputs()

    def save_inputs(self, *, enabled=True):
        self.manifest.write_text("sha256\tsize\tsource_path\n" + "".join(
            f'{r["sha256"]}\t{r["size"]}\t{r["source_path"]}\n' for r in self.rows))
        self.policy.write_bytes(capture.encoded(self.policy_value))
        value = {"schema": "legacy-adoption-v1", "manifest_sha256": capture.digest(self.manifest.read_bytes()),
            "policy_sha256": capture.digest(self.policy.read_bytes()), "export_root": str(self.export),
            "supplement_root": str(self.supplement), "adoption": "enabled" if enabled else "disabled"}
        self.config.write_bytes(capture.encoded(value))
        for path in (self.manifest, self.policy, self.config):
            path.chmod(0o600)

    def invoke(self, *extra):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = adoption.main(["--root", str(self.base), "--config", str(self.config),
                "--manifest", str(self.manifest), "--policy", str(self.policy), *extra])
        counts = json.loads(output.getvalue())
        self.assertNotIn(str(self.base), output.getvalue())
        self.assertNotIn("fictional", output.getvalue())
        return status, counts

    def snapshot(self):
        return (self.helper.database.read_bytes(), sorted(self.pipeline.root.intake.rglob("*")))

    def ledger_rows(self, sql):
        with store.ledger(self.helper.database, readonly=True) as con:
            return [tuple(row) for row in con.execute(sql)]

    def test_apply_and_replay_share_one_locked_lifecycle(self):
        from types import SimpleNamespace
        with patch.object(capture.os, "fstatvfs", return_value=SimpleNamespace(f_flag=os.ST_RDONLY)):
            status, counts = self.invoke("--apply")
            self.assertEqual(status, 0)
            self.assertEqual(counts["adopted"], 3)
            self.assertEqual(self.ledger_rows("SELECT count(*) FROM original_resolutions"), [(3,)])
            for row in self.rows[:3]:
                with store.ledger(self.helper.database, readonly=True) as con:
                    original = store.effective_original(con, row["sha256"])
                    state = con.execute("SELECT status FROM stage_state WHERE original_sha256=? AND stage='preserve'",
                                        (row["sha256"],)).fetchone()
                self.assertEqual(original["storage_path"], str(self.pipeline.root.intake / "blobs" / row["sha256"]))
                self.assertEqual(state[0], "done")
            untouched = self.ledger_rows("SELECT role,scope,storage_path FROM originals WHERE legacy_format IN ('json','csv','other')")
            self.assertEqual(untouched, [("unresolved", "unresolved", None)] * 3)
            occurrences = self.ledger_rows("SELECT * FROM occurrences ORDER BY id")
            status, counts = self.invoke("--apply")
            self.assertEqual((status, counts["adopted"], counts["already_adopted"]), (0, 0, 3))
            self.assertEqual(self.ledger_rows("SELECT * FROM occurrences ORDER BY id"), occurrences)
            self.assertEqual(self.ledger_rows("SELECT count(*) FROM original_resolutions"), [(3,)])
        receipts = sorted((self.base / "legacy-adoption").glob("*.json"))
        self.assertEqual(len(receipts), 2)
        for path in receipts:
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            receipt = json.loads(path.read_bytes())
            self.assertEqual(receipt["schema"], "legacy-adoption-receipt-v1")
            self.assertNotIn("source_path", path.read_text())
        self.assertEqual(self.ledger_rows("SELECT status FROM runs WHERE run_id LIKE 'legacy-adoption-%'"),
                         [("completed_with_gaps",)] * 2)

    def test_hash_mismatch_counts_and_whole_apply_refusal(self):
        source = Path(self.rows[1]["source_path"])
        source.chmod(0o600)
        source.write_bytes(b"x" * self.rows[1]["size"])
        source.chmod(0o400)
        before = self.snapshot()
        status, counts = self.invoke()
        self.assertEqual((status, counts["hash_mismatch"]), (3, 1))
        from types import SimpleNamespace
        with patch.object(capture.os, "fstatvfs", return_value=SimpleNamespace(f_flag=os.ST_RDONLY)):
            self.assertEqual(self.invoke("--apply")[0], 2)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.ledger_rows("SELECT count(*) FROM runs WHERE run_id LIKE 'legacy-adoption-%'"), [(0,)])

    def test_forbidden_paths_refuse_before_source_open(self):
        for path, code in ((self.export / ".hermes" / "unread.pdf", "legacy_source_path_forbidden"),
                           (self.sources / "outside.pdf", "legacy_source_root_not_admitted"),
                           (self.export / "agent-work" / "flock-mvp" / "unread.pdf", "legacy_source_path_forbidden"),
                           (self.export / "unknown" / "unread.pdf", "legacy_source_class_unknown")):
            with self.subTest(code=code):
                self.rows[0]["source_path"] = str(path)
                self.save_inputs()
                real_open = os.open
                def guarded_open(name, *args, **kwargs):
                    if str(name) in (str(path), path.name):
                        self.fail("forbidden source opened")
                    return real_open(name, *args, **kwargs)
                with patch("os.open", side_effect=guarded_open), patch("builtins.open", side_effect=AssertionError("unexpected open")):
                    status, counts = self.invoke()
                self.assertEqual((status, counts), (2, {code: 1}))

    def test_owner_mode_and_hash_pins_refuse_without_writes(self):
        for target, code in ((self.config, "legacy_input_mode"),
                             (self.manifest, "legacy_manifest_hash_mismatch"),
                             (self.policy, "legacy_policy_hash_mismatch")):
            with self.subTest(target=target.name):
                self.save_inputs()
                if target == self.config:
                    target.chmod(0o644)
                else:
                    target.write_bytes(target.read_bytes() + b" ")
                before = self.snapshot()
                self.assertEqual(self.invoke(), (2, {code: 1}))
                self.assertEqual(self.invoke("--apply"), (2, {code: 1}))
                self.assertEqual(self.snapshot(), before)

    def test_disabled_apply_and_policy_gap_fail_closed(self):
        self.save_inputs(enabled=False)
        self.assertEqual(self.invoke()[0], 0)
        self.assertEqual(self.invoke("--apply"), (2, {"legacy_adoption_disabled": 1}))
        self.policy_value["originals"].pop(self.rows[2]["sha256"])
        self.save_inputs()
        before = self.snapshot()
        from types import SimpleNamespace
        with patch.object(capture.os, "fstatvfs", return_value=SimpleNamespace(f_flag=os.ST_RDONLY)):
            self.assertEqual(self.invoke("--apply"), (2, {"capture_not_in_approved_inventory": 1}))
        self.assertEqual(self.snapshot(), before)

    def test_ranked_source_receipt_and_conflicting_sizes(self):
        lower = dict(self.rows[0], source_path=str(self.supplement / "copy.txt"))
        self.rows.append(lower)
        self.save_inputs()
        from types import SimpleNamespace
        with patch.object(capture.os, "fstatvfs", return_value=SimpleNamespace(f_flag=os.ST_RDONLY)):
            self.assertEqual(self.invoke("--apply")[0], 0)
        receipt = json.loads(next((self.base / "legacy-adoption").glob("*.json")).read_bytes())
        chosen = next(row for row in receipt["rows"] if row["original_sha256"] == lower["sha256"])
        self.assertEqual(chosen["source_class"], "received")
        lower["size"] += 1
        self.save_inputs()
        before = self.snapshot()
        self.assertEqual(self.invoke("--apply"), (2, {"legacy_manifest_conflict": 1}))
        self.assertEqual(self.snapshot(), before)

    def test_policy_proposal_is_private_and_uses_canonical_row_evidence(self):
        proposal = self.base / "proposal.json"
        before = self.snapshot()
        self.assertEqual(self.invoke("--write-policy-proposal", str(proposal))[0], 0)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(proposal.stat().st_mode & 0o777, 0o600)
        value = json.loads(proposal.read_bytes())
        self.assertEqual(value["inventory_sha256"], capture.digest(self.manifest.read_bytes()))
        self.assertEqual(value["approval_sha256"], capture.digest(self.config.read_bytes()))
        self.assertEqual(value["originals"], self.policy_value["originals"])
        with self.assertRaises(FileExistsError):
            # Never truncate another proposal or follow a link.
            adoption.write_proposal(proposal, value)

    def test_replay_policy_gap_is_refused_before_run_writes(self):
        from types import SimpleNamespace
        with patch.object(capture.os, "fstatvfs", return_value=SimpleNamespace(f_flag=os.ST_RDONLY)):
            self.assertEqual(self.invoke("--apply")[0], 0)
            self.policy_value["originals"].pop(self.rows[2]["sha256"])
            self.save_inputs()
            before = self.snapshot()
            self.assertEqual(self.invoke("--apply")[0], 2)
            self.assertEqual(self.snapshot(), before)

    def test_interrupted_enrollment_resumes_pending_preservation(self):
        from types import SimpleNamespace
        backend = adoption.CanonicalOriginalReconciler
        with patch.object(capture.os, "fstatvfs", return_value=SimpleNamespace(f_flag=os.ST_RDONLY)):
            with patch.object(backend, "_accept_locked", side_effect=ValueError("synthetic_failure")):
                self.assertEqual(self.invoke("--apply")[0], 2)
            status, counts = self.invoke("--apply")
            self.assertEqual((status, counts["adopted"]), (0, 3))
        self.assertEqual(self.ledger_rows("SELECT count(*) FROM stage_state WHERE stage='preserve' AND status='done'"), [(3,)])
        self.assertEqual(self.ledger_rows("SELECT count(*) FROM original_resolutions"), [(3,)])

    def test_internal_callbacks_require_the_shared_lock(self):
        backend = adoption.CanonicalOriginalReconciler(self.base / "mail", self.base / "intake",
            self.helper.database, policy=capture.ApprovedCapturePolicy(self.policy.read_bytes(),
                expected_sha256=capture.digest(self.policy.read_bytes())))
        for callback in (lambda: backend._accept_locked(self.rows[0]["sha256"]),
                         lambda: backend._enroll_local_capture_locked(self.rows[0]["source_path"],
                             original_sha256=self.rows[0]["sha256"])):
            with self.assertRaisesRegex(RuntimeError, "context_root_lock_required"):
                callback()

    def test_strict_manifest_and_source_safety(self):
        from types import SimpleNamespace
        for mutate in (lambda: self.rows.append(dict(self.rows[0])),
                       lambda: self.rows[0].update(sha256="F" * 64),
                       lambda: self.rows[0].update(size=-1),
                       lambda: self.rows[0].update(source_path="relative.txt")):
            saved = [dict(row) for row in self.rows]
            mutate()
            self.save_inputs()
            before = self.snapshot()
            with patch.object(adoption, "verify_source", side_effect=AssertionError("source opened")):
                self.assertEqual(self.invoke("--apply")[0], 2)
            self.assertEqual(self.snapshot(), before)
            self.rows = saved
        self.save_inputs()
        source = Path(self.rows[1]["source_path"])
        source.unlink()
        source.symlink_to(self.rows[0]["source_path"])
        with patch.object(capture.os, "fstatvfs", return_value=SimpleNamespace(f_flag=os.ST_RDONLY)):
            before = self.snapshot()
            self.assertEqual(self.invoke("--apply")[0], 2)
            self.assertEqual(self.snapshot(), before)

    def test_stdout_only_fixed_count_keys_and_numbers(self):
        _, counts = self.invoke()
        def assert_counts(value):
            self.assertIsInstance(value, dict)
            for key, count in value.items():
                self.assertRegex(key, r"^[a-z][a-z_-]+$")
                if isinstance(count, dict):
                    assert_counts(count)
                else:
                    self.assertIs(type(count), int)
        assert_counts(counts)
        with patch.object(adoption, "preflight", side_effect=OSError("synthetic private filename and content")):
            status, counts = self.invoke()
        self.assertEqual(status, 2)
        self.assertEqual(counts, {"legacy_adoption_refused": 1})
        assert_counts(counts)

    def test_apply_preflight_checks_later_sources_before_any_capture(self):
        from types import SimpleNamespace
        source = Path(self.rows[2]["source_path"])
        source.chmod(0o644)
        before = self.snapshot()
        with patch.object(capture.os, "fstatvfs", return_value=SimpleNamespace(f_flag=os.ST_RDONLY)):
            self.assertEqual(self.invoke("--apply"), (2, {"legacy_source_unsafe": 1}))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(list((self.pipeline.root.intake / "blobs").iterdir()), [])

    def test_manifest_row_bound_and_bad_header(self):
        config = json.loads(self.config.read_bytes())
        header = b"sha256\tsize\tsource_path\n"
        body = b"".join(f'{index:064x}\t0\t{self.export}/received/{index}\n'.encode()
                        for index in range(10001))
        with self.assertRaisesRegex(ValueError, "legacy_manifest_invalid"):
            adoption.parse_manifest(header + body, config)
        with self.assertRaisesRegex(ValueError, "legacy_manifest_invalid"):
            adoption.parse_manifest(b"sha256 size source_path\n", config)

    def test_dry_run_counts_and_readonly_ledger(self):
        before = self.snapshot()
        status, counts = self.invoke()
        self.assertEqual(status, 0)
        self.assertEqual(counts, {"eligible": 3, "excluded_metadata_class": 2,
            "format_unknown": 1, "already_bound": 0, "already_adopted": 0, "adopted": 0,
            "hash_mismatch": 0, "source_classes": {"received": 6, "received-user-files": 0,
                "portal-intake": 0, "productions": 0, "supplement": 0}})
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.base / "legacy-adoption").exists())
