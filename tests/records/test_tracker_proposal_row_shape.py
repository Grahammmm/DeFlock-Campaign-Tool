"""Row-shape handling in the read-only tracker proposal checker (synthetic data only)."""
import contextlib
import io
import os
import tempfile
import unittest

from campaign_tool.records import tracker_proposal as tp

GOOD = {
    "request_id": "26-001", "agency": "Example PD", "scope": "fictional scope",
    "submitted_at": "2026-01-02", "agency received date": "2026-01-03",
    "receipt_basis": "recorded_receipt", "extension": "none", "promised_dates": "none",
    "status": "waiting_on_agency", "asked_items": "1 [T1] fictional item", "owner": "ops",
    "next_action": "wait", "source_message_ids": "none-in-mail-store",
    "non_mail_sources": "portal receipt",
}


def line(values):
    return ",".join(values)


class RowShapeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name

    def write(self, lines, header=None, bom=False):
        path = os.path.join(self.dir, "tracker.csv")
        head = line(header or tp.REQUIRED)
        with open(path, "w", encoding="utf-8-sig" if bom else "utf-8", newline="") as fh:
            fh.write("\n".join([head] + lines) + "\n")
        return path

    def good(self, **over):
        row = dict(GOOD, **over)
        return [row[c] for c in tp.REQUIRED]

    def test_short_rows_report_width_problem(self):
        full = self.good()
        for cut in (3, 4, 5, 9, 13):
            path = self.write([line(full[:cut])])
            rows, problems = tp.check_proposal(path)
            self.assertEqual(len(rows), 1)
            self.assertEqual(problems, ["row2:row_width_mismatch"])

    def test_surplus_cells_are_not_silently_accepted(self):
        full = self.good()
        for extra in (["x"], ["x", "y", "z"], [""]):
            path = self.write([line(full + extra)])
            _, problems = tp.check_proposal(path)
            self.assertEqual(problems, ["row2:row_width_mismatch"])

    def test_empty_cells_are_not_missing_cells(self):
        path = self.write([line(self.good(submitted_at="", **{"agency received date": ""}))])
        _, problems = tp.check_proposal(path)
        self.assertEqual(problems, ["26-001:submitted_at_not_iso_date",
                                    "26-001:agency_received_date_not_iso_date"])

    def test_mixed_rows_continue_and_cli_fails_read_only(self):
        rows = [line(self.good()), line(self.good(request_id="26-002")[:5]),
                line(self.good(request_id="26-003", status="bogus")), line(self.good() + ["x"])]
        path = self.write(rows)
        with open(path, "rb") as fh:
            before = fh.read()
        parsed, problems = tp.check_proposal(path)
        self.assertEqual(len(parsed), 4)
        self.assertEqual(problems, ["row3:row_width_mismatch", "26-003:status_invalid",
                                    "row5:row_width_mismatch"])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = tp.main([path])
        self.assertEqual(code, 1)
        self.assertIn("PROBLEM row3:row_width_mismatch", out.getvalue())
        self.assertIn("problems=3", out.getvalue())
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), before)

    def test_quoted_fields_bom_and_optional_columns_remain_valid(self):
        values = self.good(scope='"line one, with comma\nline two"') + ["extra value"]
        path = self.write([line(values)], header=list(tp.REQUIRED) + ["notes"], bom=True)
        rows, problems = tp.check_proposal(path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(problems, [])


if __name__ == "__main__":
    unittest.main()
