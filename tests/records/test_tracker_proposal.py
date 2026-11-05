import csv
import shutil
import tempfile
import unittest
from pathlib import Path

from campaign_tool.records import tracker_proposal as tp
from campaign_tool.records.outcomes.register import read_tracker

PROPOSED = Path(__file__).resolve().parents[2] / "data" / "slo-pilot" / "MASTER-REQUEST-TRACKER.proposed.csv"


class TrackerProposalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="trk-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def rewrite(self, edit):
        with open(PROPOSED, newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        names = list(rows[0])
        edit(rows)
        out = self.tmp / "t.csv"
        with open(out, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, names, lineterminator="\n")
            w.writeheader()
            w.writerows(rows)
        return out

    def test_shipped_proposal_is_clean_with_seventeen_rows(self):
        rows, problems = tp.check_proposal(PROPOSED)
        self.assertEqual(problems, [])
        self.assertEqual(len(rows), 17)

    def test_existing_register_reader_accepts_it(self):
        rows, gaps = read_tracker(PROPOSED)
        self.assertEqual(gaps, [])
        self.assertEqual(len(rows), 17)
        self.assertTrue(all(r["submitted_at"] and r["received_at"] for r in rows))

    def test_every_row_has_receipt_basis_and_source(self):
        rows, _ = tp.check_proposal(PROPOSED)
        for r in rows:
            self.assertIn(r["receipt_basis"], tp.BASES)
            self.assertTrue(r["source_message_ids"] and r["asked_items"])

    def test_missing_column_is_reported(self):
        out = self.tmp / "t.csv"
        shutil.copy(PROPOSED, out)
        out.write_text(out.read_text(encoding="utf-8").replace("receipt_basis", "basis", 1), encoding="utf-8")
        self.assertEqual(tp.check_proposal(out)[1], ["header_missing:receipt_basis"])

    def test_bad_value_is_reported(self):
        rid = tp.check_proposal(PROPOSED)[0][0]["request_id"]
        cases = [
            ("receipt_basis", "guessed", "receipt_basis_invalid"),
            ("submitted_at", "08/24/2026", "submitted_at_not_iso_date"),
            ("status", "overdue", "status_invalid"),
            ("asked_items", "1 [T1] a || 3 [T2] b", "asked_item_number:3!=2"),
            ("source_message_ids", "Sent:9..3", "source_id_malformed:Sent:9..3"),
            ("source_message_ids", "", "source_message_ids_blank"),
        ]
        for col, value, code in cases:
            with self.subTest(code=code):
                out = self.rewrite(lambda rows: rows[0].update({col: value}))
                self.assertIn(rid + ":" + code, tp.check_proposal(out)[1])

    def test_duplicate_request_id_is_reported(self):
        out = self.rewrite(lambda rows: rows[1].update({"request_id": rows[0]["request_id"].upper()}))
        self.assertTrue(any(p.endswith(":request_id_duplicate") for p in tp.check_proposal(out)[1]))

    def test_unresolved_source_id_is_reported_against_index(self):
        index = self.tmp / "index.tsv"
        index.write_text("folder\tuid\nSent\t1\n", encoding="utf-8")
        problems = tp.check_proposal(PROPOSED, tp.load_mail_index(index))[1]
        self.assertTrue(any("source_id_not_in_mail_index:Sent:33" in p for p in problems))

    def test_no_mail_marker_needs_another_source(self):
        out = self.rewrite(lambda rows: rows[0].update({"source_message_ids": tp.NO_MAIL, "non_mail_sources": ""}))
        self.assertTrue(any(p.endswith(":no_mail_source_without_other_source") for p in tp.check_proposal(out)[1]))

    def test_cli_exit_codes(self):
        self.assertEqual(tp.main([str(PROPOSED), "--expect-rows", "17"]), 0)
        self.assertEqual(tp.main([str(PROPOSED), "--expect-rows", "16"]), 1)


if __name__ == "__main__":
    unittest.main()
