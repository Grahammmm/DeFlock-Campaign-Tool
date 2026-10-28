"""New synthetic CLI/adapter gate; no pipeline constructor or real ledger.

Real Scope/source_path and root locking; capture queue/inventory/core. Runtime
probing, native intake, original receipts, network and models are not exercised.
"""
import argparse
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from campaign_tool.records import export_selection as selection
from campaign_tool.records import run


class ExportSelectionCLITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="a04-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.canonical = self.base / "canonical"
        self.exports = self.base / "exports"
        self.exports.mkdir(mode=0o700)
        self.report = {"status": "completed_with_gaps", "exit_code": 3,
                       "intake": {"events": [{"retrieval_complete": False}]}}
        self.queue = Mock()
        self.inventory = Mock()
        self.queue_factory = self.enterContext(patch.object(selection, "Queue", return_value=self.queue))
        self.inventory_factory = self.enterContext(patch.object(selection, "CollectionInventory", return_value=self.inventory))
        self.runtime = self.enterContext(patch("campaign_tool.records.runtime.require", return_value=True))
        self.build = self.enterContext(patch.object(run, "build_pipeline"))
        self.enterContext(patch("campaign_tool.records.intake.imap_intake.load_config",
                                side_effect=AssertionError("credential loader forbidden")))

    def args(self, **overrides):
        values = dict(root=str(self.canonical), export_root=str(self.exports),
                      export_receipt=["receipts/one.json"], collection_host=["archive.example.invalid"],
                      collection_exclude_host=["social.example.invalid"], inbox=None, mail_config=None,
                      documents=None, document_manifest_sha256=None)
        values.update(overrides)
        return argparse.Namespace(**values)

    def cli(self, extra):
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(io.StringIO()):
            code = run.main(["--root", str(self.canonical), "--json", *extra])
        return code, output.getvalue()

    def reject_cli(self, extra):
        with self.assertRaises(SystemExit) as caught:
            self.cli(extra)
        self.assertEqual(caught.exception.code, 2)
        self.runtime.assert_not_called()
        self.build.assert_not_called()
        self.queue_factory.assert_not_called()
        self.assertFalse(self.canonical.exists())

    def pipeline(self, core=None):
        self.canonical.mkdir(mode=0o700)
        pipeline = run.Pipeline.__new__(run.Pipeline)
        pipeline.root = SimpleNamespace(path=self.canonical)
        pipeline._run_locked = Mock(side_effect=core, return_value=self.report)
        return pipeline

    def test_selected_cli_reuses_driver_under_lock_and_preserves_gap_exit(self):
        def make_queue(root):
            self.assertEqual(root, self.canonical / "portal")
            run.require_root_lock(self.canonical)
            return self.queue

        def make_inventory(queue, scope):
            run.require_root_lock(self.canonical)
            self.assertIs(queue, self.queue)
            self.assertEqual(scope.hosts, frozenset({"archive.example.invalid"}))
            self.assertEqual(scope.excluded_hosts, frozenset({"social.example.invalid"}))
            return self.inventory

        def core(inbox, mail_config, **kwargs):
            run.require_root_lock(self.canonical)
            self.assertIsNone(inbox)
            self.assertIsNone(mail_config)
            self.assertEqual(kwargs["exports"], {"root": self.exports,
                "receipts": (self.exports / "receipts/one.json", self.exports / "receipts/two.json"),
                "inventory": self.inventory})
            return self.report

        self.queue_factory.side_effect = make_queue
        self.inventory_factory.side_effect = make_inventory
        pipeline = self.pipeline(core)
        self.build.return_value = pipeline
        code, output = self.cli(["--export-root", str(self.exports),
            "--export-receipt", "receipts/one.json", "--export-receipt", "receipts/two.json",
            "--collection-host", "archive.example.invalid", "--collection-exclude-host", "social.example.invalid"])
        self.assertEqual(code, 3)
        self.assertEqual(json.loads(output), self.report)
        pipeline._run_locked.assert_called_once()
        self.queue.close.assert_called_once_with()
        with self.assertRaises(run.RunError):
            run.require_root_lock(self.canonical)

    def test_ordinary_cli_does_not_open_export_inventory(self):
        self.build.return_value.run.return_value = self.report
        code, _ = self.cli(["--inbox", str(self.base / "inbox")])
        self.assertEqual(code, 3)
        self.build.return_value.run.assert_called_once_with(str(self.base / "inbox"), None,
            documents=None, document_manifest_sha256=None)
        self.queue_factory.assert_not_called()

    def test_incomplete_groups_reject_before_runtime_or_root_creation(self):
        for extra in (["--export-root", str(self.exports)], ["--export-receipt", "one.json"],
                      ["--collection-host", "archive.example.invalid"],
                      ["--collection-exclude-host", "social.example.invalid"],
                      ["--export-root", "", "--export-receipt", "one.json"]):
            with self.subTest(extra=extra):
                self.reject_cli(extra)

    def test_other_input_modes_reject_before_runtime_or_root_creation(self):
        for extra in (["--inbox", "inbox"], ["--mail-config", "not-read.json"],
                      ["--documents", "drop", "--document-manifest-sha256", "a" * 64],
                      ["--document-manifest-sha256", "a" * 64], ["--unattended"]):
            with self.subTest(extra=extra):
                self.reject_cli(["--export-root", str(self.exports), "--export-receipt", "one.json", *extra])

    def test_receipt_count_bound_accepts_sixty_and_rejects_sixty_one(self):
        receipts = [f"receipts/{index}.json" for index in range(61)]
        self.assertEqual(len(selection.from_cli(self.args(export_receipt=receipts[:60])).receipts), 60)
        extra = ["--export-root", str(self.exports)]
        for receipt in receipts:
            extra += ["--export-receipt", receipt]
        self.reject_cli(extra)

    def test_duplicate_normalized_receipts_reject_before_runtime(self):
        self.reject_cli(["--export-root", str(self.exports), "--export-receipt", "receipts/one.json",
                         "--export-receipt", str(self.exports / "receipts/./one.json")])

    def test_source_escape_and_output_paths_reject_before_runtime(self):
        cases = [(str(self.exports), "../one.json"), (str(self.exports), str(self.base / "outside.json")),
                 (str(self.exports), ""), (str(self.base), "canonical/intake/one.json")]
        for root, receipt in cases:
            with self.subTest(root=root, receipt=receipt):
                self.reject_cli(["--export-root", root, "--export-receipt", receipt])

    def test_symlink_source_path_rejects_before_runtime(self):
        alias = self.base / "alias"
        alias.symlink_to(self.exports, target_is_directory=True)
        self.reject_cli(["--export-root", str(alias), "--export-receipt", "one.json"])

    def test_scope_validation_and_empty_scope_reuse_existing_contract(self):
        empty = selection.from_cli(self.args(collection_host=None, collection_exclude_host=None))
        self.assertEqual(empty.scope.hosts, frozenset())
        for flags in (["--collection-host", "https://archive.example.invalid"],
                      ["--collection-host", "archive.example.invalid", "--collection-exclude-host", "archive.example.invalid"]):
            with self.subTest(flags=flags):
                self.reject_cli(["--export-root", str(self.exports), "--export-receipt", "one.json", *flags])

    def test_inventory_cannot_open_outside_existing_root_lock(self):
        selected = selection.from_cli(self.args())
        with self.assertRaises(run.RunError):
            with selected.open(self.canonical):
                self.fail("unlocked inventory opened")
        self.queue_factory.assert_not_called()

    def test_selection_cannot_move_to_another_canonical_root(self):
        selected = selection.from_cli(self.args())
        with self.assertRaisesRegex(ValueError, "export_selection_root_mismatch"):
            with selected.open(self.base / "other"):
                self.fail("root binding changed")
        self.queue_factory.assert_not_called()

    def test_queue_closes_when_core_raises(self):
        selected = selection.from_cli(self.args())
        pipeline = self.pipeline(RuntimeError("synthetic core failure"))
        with self.assertRaisesRegex(RuntimeError, "synthetic core failure"):
            pipeline.run(exports=selected)
        self.queue.close.assert_called_once_with()
        with self.assertRaises(run.RunError):
            run.require_root_lock(self.canonical)

    def test_queue_closes_when_inventory_construction_raises(self):
        selected = selection.from_cli(self.args())
        pipeline = self.pipeline()
        self.inventory_factory.side_effect = RuntimeError("synthetic inventory failure")
        with self.assertRaisesRegex(RuntimeError, "synthetic inventory failure"):
            pipeline.run(exports=selected)
        self.queue.close.assert_called_once_with()
        pipeline._run_locked.assert_not_called()

    def test_existing_library_mapping_keeps_its_interface(self):
        pipeline = self.pipeline()
        mapping = {"root": self.exports, "receipts": [self.exports / "one.json"], "inventory": self.inventory}
        self.assertIs(pipeline.run(exports=mapping), self.report)
        self.assertIs(pipeline._run_locked.call_args.kwargs["exports"], mapping)
        self.queue_factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
