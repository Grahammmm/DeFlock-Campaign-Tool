"""A malformed comparison row must not prevent the inventory from being written."""
import hashlib
import json
from pathlib import Path
import unittest

from campaign_tool.records.outcomes import received_items as ri
from tests.records import test_outcome_received_items as fixtures


class ComparisonRowShapeTests(unittest.TestCase):
    # Reuse the synthetic ledger fixture, without collecting its tests twice.
    project: Path
    setUp = fixtures.ReceivedItemsTests.setUp
    private = fixtures.ReceivedItemsTests.private
    document = fixtures.ReceivedItemsTests.document
    enroll = fixtures.ReceivedItemsTests.enroll
    row = fixtures.ReceivedItemsTests.row

    def test_writer_skips_bad_rows_and_keeps_the_valid_rows_source_index(self):
        subject, _ = self.document(["We acknowledge receipt of your request."], "caltrans")
        valid = self.row("caltrans", subject, "page 1", field="cpra_response_deadline")
        # Matching evidence used to reach field.endswith() even for a null field.
        null_field = dict(valid, field=None)
        cases = {
            "null-row": [None],
            "null-field": [null_field],
            "mixed": [None, null_field],
            "empty-field": [dict(valid, field="")],
            "numeric-field": [dict(valid, field=42)],
            "missing-field": [{key: value for key, value in valid.items() if key != "field"}],
        }
        for run_id, malformed in cases.items():
            with self.subTest(run=run_id):
                run_dir = self.project / "run-outputs" / run_id
                comparison = {"schema": ri.COMPARISON_SCHEMA, "run_id": run_id, "rows": [*malformed, valid]}
                raw = json.dumps(comparison).encode()
                self.private(run_dir / "comparison.json", raw)

                report = ri.write_run_received_items(self.project, run_id)

                self.assertEqual(json.loads((run_dir / "outcomes" / "received-items.json").read_text()), report)
                self.assertIn("comparison_row_invalid", report["diagnostics"])
                item, = report["items"]
                self.assertEqual(item["original_sha256"], subject)
                self.assertEqual(item["comparison_status"], "mapped")
                finding, = item["findings"]
                self.assertEqual(finding["comparison_ref"], {
                    "report_sha256": hashlib.sha256(raw).hexdigest(),
                    "row_index": len(malformed),
                    "field": valid["field"],
                })
