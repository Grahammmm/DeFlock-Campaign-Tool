"""Malformed receipts are accounted for without stopping valid ingestion."""
import json
import tempfile
import unittest
from pathlib import Path

from campaign_tool.records.agency_run import events_ingest
from tests.records.test_agency_events_ingest import ALIASES, receipt


class BadReceipts(unittest.TestCase):
    def test_bad_files_are_listed_and_valid_message_is_processed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = root / "store"
            receipts = store / "receipts"
            receipts.mkdir(parents=True)
            (store / "SYNC-STATUS.json").write_text('{"failures": []}')
            aliases = root / "aliases.json"
            aliases.write_text(json.dumps(ALIASES))
            (receipts / "broken.json").write_text("{bad")
            (receipts / "shape.json").write_text(json.dumps(receipt(2) | {"headers": ["bad"]}))
            (receipts / "valid.json").write_text(json.dumps(receipt(3)))
            out = root / "out"
            report = events_ingest.run(store, aliases, out)
            self.assertEqual((report["bad_receipts"], report["unclassified"], report["events"]), (2, 2, 3))
            rows = (out / "unclassified.tsv").read_text().splitlines()
            self.assertEqual(rows[0], "folder\tuid\tdate_pacific\treason")
            self.assertEqual(set(rows[1:]), {
                'receipts/"broken.json"\t\t\tbad_receipt',
                'receipts/"shape.json"\t\t\tbad_receipt',
            })
            self.assertIn("Sent:3 Message-ID", (out / "events.tsv").read_text())


if __name__ == "__main__":
    unittest.main()
