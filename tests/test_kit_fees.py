"""kit-fees: one private, read-only fee report across every recorded request, exact cents. Offline, synthetic."""
import csv
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import kit, kit_fees  # noqa: E402
from tests.test_kit_check import ready_drafts  # noqa: E402
from tests.test_kit_status_csv import run  # noqa: E402

SENT_ON = "2026-03-02"
HEADER = ["agency_id", "request", "name", "reference", "fee_estimate", "fee_state", "fee_paid", "over_fee_cap"]
TRAILER = "Private file; nothing was sent or published.\n"
READ_ONLY = "Read-only. Nothing was sent.\n"
EMPTY = {"quoted": "$0.00", "paid": "$0.00", "unpaid": "$0.00",
         "counts": {"requests_with_fee": 0, "unpaid": 0, "paid": 0, "waived": 0, "over_cap": 0}}


class KitFeesTests(unittest.TestCase):
    def setUp(self):
        offline = mock.patch("urllib.request.urlopen", side_effect=AssertionError("network used"))
        offline.start()
        self.addCleanup(offline.stop)
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-kit-fees-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.root = self.tmp / "campaign"
        self.cmd("init", "--directory", self.root, "--name", "Synthetic", "--county", "San Luis Obispo", "--state", "CA")
        self.cmd("kit", "--directory", self.root)
        self.a, self.b, self.c = (p.stem for p in sorted((self.root / "kit" / "requests").glob("*.md"))[:3])
        ready_drafts(self.root)
        self.sent = self.root / "kit" / "sent.json"
        self.private = self.tmp / "private"
        self.private.mkdir()

    def cmd(self, *arguments):
        code, out, err = run(*arguments)
        self.assertEqual(code, 0, err)
        return out

    def record(self, agency, on, kind, *extra):
        if not self.sent.is_file() or agency not in json.loads(self.sent.read_text())["requests"]:
            self.cmd("kit-sent", "--directory", self.root, "--agency", agency, "--date", SENT_ON, "--channel", "email")
        self.cmd("kit-response", "--directory", self.root, "--agency", agency, "--date", on, "--kind", kind, *extra)

    def fee(self, *extra):
        code, out, err = run("kit-fees", "--directory", self.root, *extra)
        return code, out, err

    def report(self):
        code, out, err = self.fee()
        self.assertEqual((code, err), (0, READ_ONLY))
        return json.loads(out)

    def test_no_sent_file_is_an_empty_report(self):
        self.assertFalse(self.sent.exists())
        report = self.report()
        self.assertEqual((report["schema"], report["requests"], report["totals"]), ("kit-fees-v1", [], EMPTY))
        self.assertIn("No fee estimates recorded.", report["note"])
        self.assertIn("Nothing was sent.", report["note"])
        self.assertFalse(self.sent.exists())

    def test_requests_without_fees_are_absent_and_estimate_alone_is_unpaid(self):
        self.record(self.a, "2026-03-05", "acknowledged")
        self.assertEqual(self.report()["requests"], [])
        self.assertIn("No fee estimates recorded.", self.report()["note"])
        self.record(self.b, "2026-03-05", "fee_estimate", "--amount", "245.50")
        report = self.report()
        [row] = report["requests"]
        self.assertEqual((row["agency_id"], row["request"], row["fee_estimate"], row["fee_state"], row["fee_paid"],
                          row["over_fee_cap"]), (self.b, "main", "$245.50", "unpaid", None, None))
        self.assertEqual(set(row), set(HEADER))
        self.assertEqual((report["totals"]["quoted"], report["totals"]["unpaid"], report["totals"]["paid"]),
                         ("$245.50", "$245.50", "$0.00"))
        self.assertEqual(report["totals"]["counts"]["unpaid"], 1)
        self.assertNotIn("No fee estimates recorded.", report["note"])

    def test_only_the_latest_estimate_is_quoted(self):
        self.record(self.a, "2026-03-05", "fee_estimate", "--amount", "50")
        self.record(self.a, "2026-03-06", "fee_estimate", "--amount", "75.25")
        report = self.report()
        [row] = report["requests"]
        self.assertEqual(row["fee_estimate"], "$75.25")
        self.assertEqual((report["totals"]["quoted"], report["totals"]["counts"]["requests_with_fee"]), ("$75.25", 1))

    def test_paid_with_and_without_amount_and_totals_and_order(self):
        self.record(self.a, "2026-03-05", "fee_estimate", "--amount", "100")
        self.record(self.a, "2026-03-06", "fee_paid", "--amount", "99.99")
        self.record(self.b, "2026-03-05", "fee_estimate", "--amount", "40")
        self.record(self.b, "2026-03-06", "fee_paid")  # paid, amount not recorded
        self.record(self.c, "2026-03-05", "fee_estimate", "--amount", "10.50")
        report = self.report()
        states = [(r["fee_state"], r["agency_id"], r["fee_paid"]) for r in report["requests"]]
        self.assertEqual(states, [("unpaid", self.c, None), *sorted([("paid", self.a, "$99.99"), ("paid", self.b, None)],
                                                                   key=lambda s: s[1])])
        self.assertEqual(report["totals"], {"quoted": "$150.50", "paid": "$99.99", "unpaid": "$10.50",
                                            "counts": {"requests_with_fee": 3, "unpaid": 1, "paid": 2, "waived": 0,
                                                       "over_cap": 0}})

    def test_waived_is_not_unpaid_and_over_cap_follows_the_cap_file(self):
        self.record(self.a, "2026-03-05", "fee_estimate", "--amount", "245.50")
        self.record(self.a, "2026-03-06", "fee_waived")
        [row] = self.report()["requests"]
        self.assertEqual((row["fee_state"], row["over_fee_cap"], row["fee_paid"]), ("waived", None, None))  # no cap file
        (self.root / "kit" / kit.SETTINGS_NAME).write_text(json.dumps({"schema_version": 1, "fee_cap_usd": 100}))
        report = self.report()
        [row] = report["requests"]
        self.assertEqual((row["fee_state"], row["over_fee_cap"]), ("waived", True))
        self.assertEqual(report["totals"], {"quoted": "$245.50", "paid": "$0.00", "unpaid": "$0.00",
                                            "counts": {"requests_with_fee": 1, "unpaid": 0, "paid": 0, "waived": 1,
                                                       "over_cap": 1}})

    def test_totals_are_exact_cents(self):
        for agency in (self.a, self.b, self.c):
            self.record(agency, "2026-03-05", "fee_estimate", "--amount", "0.10")
        totals = self.report()["totals"]
        self.assertEqual((totals["quoted"], totals["unpaid"]), ("$0.30", "$0.30"))
        self.assertEqual(kit_fees.fees(self.root)["totals"]["quoted"], "$0.30")

    def test_malformed_sent_file_stops_with_the_message_and_no_stdout(self):
        self.sent.write_text("{not json", encoding="utf-8")
        for extra in ((), ("--csv", self.private / "x.csv")):
            code, out, err = self.fee(*extra)
            self.assertEqual((code, out), (1, ""))
            self.assertIn("kit/sent.json unreadable", err)
            self.assertNotIn("Traceback", err)
        self.assertEqual(list(self.private.iterdir()), [])

    def test_csv_rows_total_row_mode_and_formula_safe_cells(self):
        self.record(self.a, "2026-03-05", "fee_estimate", "--amount", "10")
        self.record(self.a, "2026-03-06", "fee_paid", "--amount", "10")
        self.record(self.b, "2026-03-05", "fee_estimate", "--amount", "5.25")
        path = self.root / "kit" / "agencies.json"
        agencies = json.loads(path.read_text(encoding="utf-8"))
        next(e for e in agencies if e["agency_id"] == self.b)["name"] = "=cmd|' /C calc'!A0"
        path.write_text(json.dumps(agencies), encoding="utf-8")
        out = self.private / "fees.csv"
        code, stdout, err = self.fee("--csv", out)
        self.assertEqual((code, err), (0, ""))
        self.assertTrue(stdout.endswith(TRAILER), stdout)
        summary = json.loads(stdout[:-len(TRAILER)])
        self.assertEqual((summary["written"], summary["rows"], summary["totals"]["quoted"]),
                         (str(out.resolve()), 2, "$15.25"))
        self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o600)
        data = out.read_bytes()
        with open(out, encoding="utf-8", newline="") as handle:
            rows = list(csv.reader(handle))
        self.assertEqual(data.count(b"\r\n"), len(rows))
        self.assertEqual(rows[0], HEADER)
        by_agency = {r[0]: r for r in rows[1:]}
        self.assertEqual(by_agency[self.b][2], "'=cmd|' /C calc'!A0")
        self.assertEqual((by_agency[self.b][4], by_agency[self.b][5], by_agency[self.b][6], by_agency[self.b][7]),
                         ("$5.25", "unpaid", "", ""))
        self.assertEqual(by_agency[self.a][4:7], ["$10.00", "paid", "$10.00"])
        self.assertEqual([r[0] for r in rows[1:-1]], [self.b, self.a])  # unpaid first
        self.assertEqual(rows[-1], ["TOTAL", "", "unpaid $5.25", "", "$15.25", "", "$10.00", ""])

    def test_csv_refused_in_public_content_existing_file_and_wrong_name(self):
        self.record(self.a, "2026-03-05", "fee_estimate", "--amount", "10")
        existing = self.private / "have.csv"
        existing.write_text("keep", encoding="utf-8")
        for folder in ("public", "content"):
            (self.root / folder).mkdir(exist_ok=True)
        cases = ((self.root / "public" / "fees.csv", "must not be inside the campaign's public/"),
                 (self.root / "content" / "fees.csv", "must not be inside the campaign's content/"),
                 (existing, "exists; choose a new file name"), (self.private / "fees.txt", "must end in .csv"),
                 (self.private / "nope" / "fees.csv", "does not exist"))
        for target, words in cases:
            with self.subTest(target=target.name):
                code, out, err = self.fee("--csv", target)
                self.assertEqual((code, out), (1, ""))
                self.assertIn(words, err)
        self.assertEqual(existing.read_text(encoding="utf-8"), "keep")
        self.assertEqual(sorted(p.name for p in self.private.iterdir()), ["have.csv"])
        self.assertEqual(os.listdir(self.root / "public"), [])
        link = self.private / "link.csv"
        link.symlink_to(self.private / "elsewhere.csv")
        code, out, err = self.fee("--csv", link)
        self.assertEqual((code, out), (1, ""))
        self.assertFalse((self.private / "elsewhere.csv").exists())

    def test_kit_status_and_sent_file_are_unchanged_by_kit_fees(self):
        self.record(self.a, "2026-03-05", "fee_estimate", "--amount", "245.50")
        self.record(self.b, "2026-03-05", "fee_estimate", "--amount", "10")
        self.record(self.b, "2026-03-06", "fee_paid", "--amount", "10")
        before = run("kit-status", "--directory", self.root)
        sent = self.sent.read_bytes()
        self.fee()
        self.fee("--csv", self.private / "fees.csv")
        self.assertEqual(run("kit-status", "--directory", self.root), before)
        self.assertEqual(self.sent.read_bytes(), sent)
        self.assertNotIn("kit-fees", before[1])


if __name__ == "__main__":
    unittest.main()
