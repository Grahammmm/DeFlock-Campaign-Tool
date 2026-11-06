"""REC-AUTO-6: arrival summary projection, report schema block and Telegram arrival text."""
import copy
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from campaign_tool.records import arrival_summary as summary  # noqa: E402
from campaign_tool.records import run_report  # noqa: E402

EXAMPLE = REPO / "examples" / "run-report" / "records-run-report-v1.synthetic.json"
RUN = "run_20261105T000000Z_abcdef01"
D1, D2, D3 = ("1" * 64, "2" * 64, "3" * 64)
SENTINEL = "SENTINEL-Private-Subject"


def load_notify():
    spec = importlib.util.spec_from_file_location("records_notify_under_test",
                                                  REPO / "deploy" / "records-runtime" / "notify.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_ledger(project, digests):
    path = Path(project) / "processing-v1"
    path.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path / "ledger.sqlite")
    db.execute("CREATE TABLE originals(sha256 TEXT)")
    db.executemany("INSERT INTO originals VALUES (?)", [(d,) for d in digests])
    db.commit()
    db.close()


def write_received(project, items, run_id=RUN):
    out = Path(project) / "run-outputs" / run_id / "outcomes"
    out.mkdir(parents=True, exist_ok=True)
    doc = {"schema": "records-received-item-v1", "run_id": run_id, "items": items}
    target = out / "received-items.json"
    target.write_text(json.dumps(doc))
    os.chmod(target, 0o600)


def item(ident, kind, digest):
    return {"item_id": ident, "kind": kind, "original_sha256": digest}


class BaselineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = Path(self.tmp.name) / "project"
        self.project.mkdir()
        self.addCleanup(self.tmp.cleanup)

    def test_first_capture_only_and_private(self):
        make_ledger(self.project, [D1])
        target = summary.capture_baseline(self.project, RUN)
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertEqual(target.parent.stat().st_mode & 0o777, 0o700)
        db = sqlite3.connect(self.project / "processing-v1" / "ledger.sqlite")
        db.execute("INSERT INTO originals VALUES (?)", (D2,))
        db.commit()
        db.close()
        summary.capture_baseline(self.project, RUN)  # retry after ingestion: reuse, never replace
        self.assertEqual(summary.load_baseline(self.project, RUN), {D1})

    def test_absent_ledger_is_empty_corrupt_is_unavailable(self):
        summary.capture_baseline(self.project, RUN)
        self.assertEqual(summary.load_baseline(self.project, RUN), set())
        other = "run_20261105T000001Z_abcdef02"
        (self.project / "processing-v1").mkdir()
        (self.project / "processing-v1" / "ledger.sqlite").write_bytes(b"not a database")
        with self.assertRaises(summary.ArrivalSummaryError):
            summary.capture_baseline(self.project, other)

    def test_unsafe_run_id_and_bad_baselines_refused(self):
        for bad in ("../x", "run_1", "", None):
            with self.assertRaises(summary.ArrivalSummaryError):
                summary.baseline_path(self.project, bad)
        target = summary.capture_baseline(self.project, RUN)
        os.chmod(target, 0o644)
        with self.assertRaises(summary.ArrivalSummaryError):
            summary.load_baseline(self.project, RUN)
        os.chmod(target, 0o600)
        target.write_text(json.dumps({"version": 1, "run_id": "run_other", "originals": []}))
        with self.assertRaises(summary.ArrivalSummaryError):
            summary.load_baseline(self.project, RUN)
        target.write_text("{oops")
        with self.assertRaises(summary.ArrivalSummaryError):
            summary.load_baseline(self.project, RUN)

    def test_symlinked_baseline_refused(self):
        target = summary.capture_baseline(self.project, RUN)
        real = target.with_name("real.json")
        target.rename(real)
        target.symlink_to(real)
        with self.assertRaises(summary.ArrivalSummaryError):
            summary.load_baseline(self.project, RUN)


    def test_symlinked_ancestors_are_unavailable_without_traversal(self):
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        # symlinked run-outputs/<run> directory
        (outside / "arrival-baseline.json").write_text("{}")
        runs = self.project / "run-outputs"
        runs.mkdir()
        (runs / RUN).symlink_to(outside, target_is_directory=True)
        with self.assertRaises(summary.ArrivalSummaryError):
            summary.load_baseline(self.project, RUN)
        with self.assertRaises(summary.ArrivalSummaryError):
            summary.capture_baseline(self.project, RUN)
        self.assertEqual(sorted(p.name for p in outside.iterdir()), ["arrival-baseline.json"])

    def test_symlinked_outcomes_dir_is_unavailable(self):
        summary.capture_baseline(self.project, RUN)
        outside = Path(self.tmp.name) / "outside2"
        outside.mkdir()
        (self.project / "run-outputs" / RUN / "outcomes").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(summary.ArrivalSummaryError):
            summary.new_counts(self.project, RUN)

    def test_symlinked_processing_dir_is_not_an_empty_root(self):
        outside = Path(self.tmp.name) / "outside3"
        outside.mkdir()
        (self.project / "processing-v1").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(summary.ArrivalSummaryError):
            summary.capture_baseline(self.project, RUN)


class CountTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = Path(self.tmp.name) / "project"
        self.project.mkdir()
        self.addCleanup(self.tmp.cleanup)

    def test_counts_are_new_originals_not_cumulative_inventory(self):
        make_ledger(self.project, [D1])
        summary.capture_baseline(self.project, RUN)
        write_received(self.project, [item("a", "denial", D1), item("b", "other", D2),
                                      item("c", "extension", D3)])
        counts = summary.new_counts(self.project, RUN)
        self.assertEqual(counts, {"response": 0, "extension": 1, "denial": 0, "fee": 0, "records": 0, "other": 1})

    def test_true_zero_differs_from_unavailable(self):
        make_ledger(self.project, [D1])
        summary.capture_baseline(self.project, RUN)
        write_received(self.project, [item("a", "denial", D1)])
        self.assertEqual(sum(summary.new_counts(self.project, RUN).values()), 0)
        Path(self.project, "run-outputs", RUN, "outcomes", "received-items.json").unlink()
        self.assertIsNone(summary.summarize(self.project, RUN)["received_by_kind"])

    def test_duplicate_digest_or_id_or_kind_or_run_is_unavailable(self):
        summary.capture_baseline(self.project, RUN)
        for items in ([item("a", "other", D1), item("b", "other", D1)],
                      [item("a", "other", D1), item("a", "other", D2)],
                      [item("a", "bogus", D1)]):
            write_received(self.project, items)
            with self.assertRaises(summary.ArrivalSummaryError):
                summary.new_counts(self.project, RUN)
        write_received(self.project, [item("a", "other", D1)], run_id="run_20261105T000009Z_abcdef09")
        shutil_target = Path(self.project, "run-outputs", "run_20261105T000009Z_abcdef09", "outcomes",
                             "received-items.json")
        Path(self.project, "run-outputs", RUN, "outcomes").mkdir(parents=True, exist_ok=True)
        Path(self.project, "run-outputs", RUN, "outcomes", "received-items.json").write_bytes(
            shutil_target.read_bytes())
        os.chmod(Path(self.project, "run-outputs", RUN, "outcomes", "received-items.json"), 0o600)
        with self.assertRaises(summary.ArrivalSummaryError):  # run binding mismatch
            summary.new_counts(self.project, RUN)

    def test_pending_counts_current_state_and_fails_independently(self):
        rows = [{"status": "pending"}, {"status": "approved"}, {"status": "rejected"}, {"status": "pending"}]
        with mock.patch("campaign_tool.records.approval_queue.load_queue", return_value=rows):
            self.assertEqual(summary.pending_count(self.project), 2)
        from campaign_tool.records.approval_queue import ApprovalQueueError
        with mock.patch("campaign_tool.records.approval_queue.load_queue",
                        side_effect=ApprovalQueueError("queue_corrupt")):
            result = summary.summarize(self.project, RUN)
        self.assertIsNone(result["pending_drafts"])
        self.assertIsNone(result["received_by_kind"])  # no baseline either: independent nulls


class SchemaTests(unittest.TestCase):
    def report(self, block):
        report = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        report["trigger"] = "arrival"
        report["mode"] = "arrival"
        report["arrivals"] = []
        if block is not None:
            report["arrival_summary"] = block
        return report

    GOOD = {"received_by_kind": {"response": 0, "extension": 1, "denial": 0, "fee": 0, "records": 0, "other": 2},
            "pending_drafts": 3}

    def invalid(self, block):
        with self.assertRaises(ValueError):
            run_report.validate_run_report(self.report(block))

    def test_valid_null_and_absent_blocks(self):
        run_report.validate_run_report(self.report(self.GOOD))
        run_report.validate_run_report(self.report({"received_by_kind": None, "pending_drafts": None}))
        run_report.validate_run_report(self.report(None))

    def test_malformed_blocks_refused(self):
        partial = copy.deepcopy(self.GOOD)
        del partial["received_by_kind"]["fee"]
        extra = copy.deepcopy(self.GOOD)
        extra["received_by_kind"]["mystery"] = 1
        extra_top = dict(self.GOOD, note="text")
        for bad in (partial, extra, extra_top, dict(self.GOOD, pending_drafts=True),
                    dict(self.GOOD, pending_drafts=-1), dict(self.GOOD, pending_drafts=1.5),
                    dict(self.GOOD, pending_drafts=float("inf"))):
            self.invalid(bad)

    def test_non_arrival_report_cannot_carry_block(self):
        report = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        report["arrival_summary"] = self.GOOD
        with self.assertRaises(ValueError):
            run_report.validate_run_report(report)


class ComposeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.notify = load_notify()

    def report(self, block, **changes):
        report = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        report["trigger"] = "arrival"
        report["mode"] = "arrival"
        report["arrivals"] = []
        if block is not None:
            report["arrival_summary"] = block
        report.update(changes)
        return report

    def test_three_sections_short_and_bounded(self):
        block = {"received_by_kind": {"response": 0, "extension": 1, "denial": 0, "fee": 0, "records": 0,
                                      "other": 2}, "pending_drafts": 4}
        text = self.notify.compose(self.report(block), SENTINEL, 0)
        lines = text.splitlines()
        self.assertLessEqual(len(lines), 6)
        self.assertLess(len(text), 4096)
        self.assertIn("Came in: 3 new item(s) (1 extension, 2 other). Items, not messages.", text)
        self.assertIn("Done:", text)
        self.assertIn("4 draft(s) waiting for approval", text)
        self.assertNotIn(SENTINEL, text)  # the arbitrary --label never enters the arrival text
        self.assertNotIn("notify", lines[2])

    def test_unavailable_is_not_zero(self):
        text = self.notify.compose(self.report({"received_by_kind": None, "pending_drafts": None}), "x", 0)
        self.assertIn("Came in: unavailable.", text)
        self.assertIn("drafts waiting for approval: unavailable", text)
        legacy_missing = self.notify.compose(self.report(None), "x", 0)
        self.assertIn("Came in: unavailable.", legacy_missing)

    def test_zero_new_items_is_stated(self):
        block = {"received_by_kind": {k: 0 for k in summary.KINDS}, "pending_drafts": 0}
        self.assertIn("Came in: 0 new items.", self.notify.compose(self.report(block), "x", 0))

    def test_failed_stage_is_named(self):
        report = self.report({"received_by_kind": None, "pending_drafts": None})
        report["stages"][2]["status"] = "fail"
        report["stages"][2]["error_code"] = "STAGE_FAILED"
        text = self.notify.compose(report, "x", 1)
        self.assertIn("FAILED at", text)
        self.assertIn("(exit 1)", text)

    def test_preflight_refusal_claims_no_work(self):
        report = self.report(None)
        code = next(iter(self.notify.PREFLIGHT_MESSAGES))
        report["stages"][0].update(status="fail", error_code=code)
        text = self.notify.compose(report, "x", 1)
        self.assertIn("No work attempted", text)
        self.assertIn("Came in: unavailable.", text)

    def test_scheduled_run_keeps_legacy_text(self):
        report = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        text = self.notify.compose(report, "label", 0)
        self.assertNotIn("Came in", text)


if __name__ == "__main__":
    unittest.main()
