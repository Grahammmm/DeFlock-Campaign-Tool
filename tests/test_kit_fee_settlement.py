"""kit-response --kind fee_paid|fee_waived settles the latest fee estimate; kit-status, its --csv and kit-coverage show
estimated vs paid and name unpaid estimates. Nothing is paid or sent. Offline, synthetic."""
import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import kit, kit_track  # noqa: E402
from tests.test_kit_check import ready_drafts  # noqa: E402
from tests.test_kit_response import SENT_ON, run  # noqa: E402

FLAG = "over your fee cap: decide before paying"
MALFORMED = "kit/sent.json unreadable: malformed steps"
STAMP = "2026-03-05T12:00:00+00:00"
# The pre-existing kit-status fields and --csv cells (as on main) for a request whose only step is a $245.50 estimate
GOLDEN_ROW = {"request": "main", "sent_on": SENT_ON, "channel": "email", "draft_changed_since_sent": False,
              "determination_due": "2026-03-12", "with_extension": "2026-03-26", "steps": 1, "extended": False,
              "closed": False, "last_step": {"on": "2026-03-05", "kind": "fee_estimate"}, "records": 0,
              "record_prefixes": [], "fee_estimate": "$245.50", "over_fee_cap": None, "stated_production_date": None,
              "waiting_on_you_since": None, "producing": False, "next_installment": None, "reference": None}
GOLDEN_CSV = [SENT_ON, "email", "2026-03-12", "2026-03-26", "no", "no", "1", "2026-03-05", "fee_estimate", "0", "no"]


class FeeSettlementTests(unittest.TestCase):
    def setUp(self):
        offline = mock.patch("urllib.request.urlopen", side_effect=AssertionError("network used"))
        offline.start()
        self.addCleanup(offline.stop)
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-kit-fee-settle-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.root = self.tmp / "campaign"
        for arguments in (("init", "--directory", self.root, "--name", "Synthetic", "--county", "San Luis Obispo",
                           "--state", "CA"), ("kit", "--directory", self.root)):
            code, _, err = run(*arguments)
            self.assertEqual(code, 0, err)
        self.kit, self.sent = self.root / "kit", self.root / "kit" / "sent.json"
        self.agency = sorted((self.kit / "requests").glob("*.md"))[0].stem
        ready_drafts(self.root)
        code, _, err = run("kit-sent", "--directory", self.root, "--agency", self.agency, "--date", SENT_ON,
                           "--channel", "email")
        self.assertEqual(code, 0, err)

    def step(self, on, kind, *extra):
        return run("kit-response", "--directory", self.root, "--agency", self.agency, "--date", on, "--kind", kind,
                   *extra)

    def ok(self, on, kind, *extra):
        code, out, err = self.step(on, kind, *extra)
        self.assertEqual(code, 0, err)
        return json.loads(out.split("Recorded only.")[0])

    def refused_untouched(self, result_args, *words):
        before = self.sent.read_bytes()
        code, out, err = self.step(*result_args)
        self.assertEqual(code, 1, out)
        self.assertTrue(err.startswith("Stopped: "), err)
        for word in words:
            self.assertIn(word, err)
        self.assertEqual(self.sent.read_bytes(), before)

    def status(self, request="main"):
        code, out, err = run("kit-status", "--directory", self.root)
        self.assertEqual(code, 0, err)
        rows = {(r["agency_id"], r["request"]): r for r in json.loads(out)["requests"]}  # one JSON document
        return rows[(self.agency, request)], err

    def csv_row(self, name):
        out = self.tmp / f"{name}.csv"
        code, _, err = run("kit-status", "--directory", self.root, "--csv", out)
        self.assertEqual(code, 0, err)
        with open(out, encoding="utf-8", newline="") as handle:
            header, row = list(csv.reader(handle))
        return dict(zip(header, row)), row

    def coverage(self):
        report = kit_track.coverage(self.root)
        return next(r for r in report["rows"] if r["agency_id"] == self.agency), report["totals"]

    def edit_steps(self, steps):
        document = json.loads(self.sent.read_text())
        document["requests"][self.agency]["steps"] = steps
        self.sent.write_text(json.dumps(document), encoding="utf-8")

    def test_paid_with_amount_shown_in_status_csv_and_coverage(self):
        self.ok("2026-03-05", "fee_estimate", "--amount", "245.50")
        printed = self.ok("2026-03-07", "fee_paid", "--amount", "245.50")
        self.assertEqual(printed["steps"][-1]["amount_cents"], 24550)
        row, err = self.status()
        self.assertEqual((row["fee_estimate"], row["fee_state"], row["fee_paid"]), ("$245.50", "paid", "$245.50"))
        self.assertNotIn("unpaid", err)
        cells, _ = self.csv_row("paid")
        self.assertEqual((cells["fee_estimate"], cells["fee_state"], cells["fee_paid"]), ("$245.50", "paid", "$245.50"))
        cover, totals = self.coverage()
        self.assertEqual((cover["fee_state"], totals["fee_unpaid"]), ("paid", 0))
        code, out, _ = run("kit-coverage", "--directory", self.root, "--csv")
        self.assertEqual(code, 0)
        coverage_rows = {r[0]: r for r in csv.reader(out.splitlines())}
        self.assertEqual((coverage_rows["agency_id"][-4], coverage_rows[self.agency][-4]), ("fee", "paid"))
        scopes = json.loads(self.sent.read_text())["requests"][self.agency]["scopes"]
        self.assertEqual((coverage_rows["agency_id"][-3], coverage_rows[self.agency][-3]),
                         ("scopes", f"0/{len(scopes)} answered"))
        self.assertEqual((coverage_rows["agency_id"][-2:], coverage_rows[self.agency][-2:]),
                         (["request", "owner"], ["main", ""]))

    def test_waived_and_amount_on_waived_refused(self):
        self.ok("2026-03-05", "fee_estimate", "--amount", "20")
        self.refused_untouched(("2026-03-06", "fee_waived", "--amount", "5"), "--amount is only for fee_estimate")
        self.ok("2026-03-06", "fee_waived")
        row, err = self.status()
        self.assertEqual((row["fee_estimate"], row["fee_state"], row["fee_paid"]), ("$20.00", "waived", None))
        self.assertNotIn("unpaid", err)
        self.assertEqual(self.coverage()[0]["fee_state"], "waived")
        self.assertIn(" waived", kit_track.format_coverage(kit_track.coverage(self.root)))

    def test_order_rules(self):
        for kind in ("fee_paid", "fee_waived"):
            self.refused_untouched(("2026-03-04", kind), f"{kind} needs a fee_estimate earlier")
        self.ok("2026-03-05", "fee_estimate", "--amount", "50")
        self.ok("2026-03-06", "fee_paid", "--amount", "50")
        for kind in ("fee_paid", "fee_waived"):  # one settlement per estimate, never a duplicate
            self.refused_untouched(("2026-03-07", kind), "the 2026-03-05 estimate is already paid")
        self.ok("2026-03-08", "fee_estimate", "--amount", "75")  # a new estimate re-opens settlement
        row, err = self.status()
        self.assertEqual((row["fee_estimate"], row["fee_state"], row["fee_paid"]), ("$75.00", "unpaid", None))
        self.assertIn(f"{self.agency}: fee estimate $75.00 is unpaid", err)
        self.refused_untouched(("2026-03-07", "fee_paid"), "before the previous step")  # out of order
        self.ok("2026-03-09", "fee_waived")
        self.refused_untouched(("2026-03-10", "fee_paid"), "the 2026-03-08 estimate is already waived")
        self.ok("2026-03-10", "fee_estimate", "--amount", "80")
        self.ok("2026-03-11", "closed")
        self.refused_untouched(("2026-03-12", "fee_paid"), "no step can follow closed")
        self.assertEqual(self.status()[0]["fee_state"], "unpaid")

    def test_estimate_on_another_request_does_not_count(self):
        (self.kit / "requests" / f"{self.agency}--fees.md").write_text("Narrowed request.\n", encoding="utf-8")
        code, _, err = run("kit-sent", "--directory", self.root, "--agency", self.agency, "--date", SENT_ON,
                           "--channel", "email", "--request", "fees")
        self.assertEqual(code, 0, err)
        self.ok("2026-03-05", "fee_estimate", "--amount", "10", "--request", "fees")
        self.refused_untouched(("2026-03-06", "fee_paid"), "fee_paid needs a fee_estimate earlier")
        self.ok("2026-03-06", "fee_paid", "--request", "fees")
        self.assertEqual(self.status("fees")[0]["fee_state"], "paid")
        self.assertIsNone(self.status()[0]["fee_state"])

    def test_paid_without_amount_and_bad_amounts(self):
        self.ok("2026-03-05", "fee_estimate", "--amount", "12")
        for amount in ("100000.01", "-1", "1e3", "12.345", ""):
            with self.subTest(amount=amount):
                self.refused_untouched(("2026-03-06", "fee_paid", "--amount", amount), "--amount must be")
        printed = self.ok("2026-03-06", "fee_paid")
        self.assertNotIn("amount_cents", printed["steps"][-1])
        row, _ = self.status()
        self.assertEqual((row["fee_state"], row["fee_paid"]), ("paid", None))
        cells, _ = self.csv_row("no-amount")
        self.assertEqual((cells["fee_state"], cells["fee_paid"]), ("paid", ""))

    def test_unpaid_and_over_cap_reminders_on_stderr(self):
        self.ok("2026-03-05", "fee_estimate", "--amount", "245.50")
        row, err = self.status()
        self.assertEqual(err, f"{self.agency}: fee estimate $245.50 is unpaid\n")
        (self.kit / kit.SETTINGS_NAME).write_text(json.dumps({"schema_version": 1, "fee_cap_usd": 100}))
        row, err = self.status()
        self.assertEqual(err, f"{self.agency}: fee estimate $245.50 is {FLAG}\n"
                              f"{self.agency}: fee estimate $245.50 is unpaid\n")
        self.assertEqual((row["over_fee_cap"], row["fee_state"]), (True, "unpaid"))
        self.ok("2026-03-06", "fee_paid", "--amount", "100")  # the fee-cap rule is unchanged by a payment
        row, err = self.status()
        self.assertEqual(err, f"{self.agency}: fee estimate $245.50 is {FLAG}\n")
        self.assertEqual((row["fee_estimate"], row["over_fee_cap"], row["fee_paid"]), ("$245.50", True, "$100.00"))
        cover, totals = self.coverage()
        self.assertEqual((cover["fee_state"], totals["fee_unpaid"]), ("paid", 0))

    def test_legacy_estimate_only_file_reads_as_on_main(self):
        self.edit_steps([{"on": "2026-03-05", "kind": "fee_estimate", "amount_cents": 24550, "recorded_at": STAMP}])
        before = self.sent.read_bytes()
        row, err = self.status()
        self.assertEqual((row.pop("fee_state"), row.pop("fee_paid")), ("unpaid", None))
        self.assertEqual(row.pop("withheld_basis"), [])
        self.assertEqual(row.pop("scopes"), [{"scope_id": scope, "answer": None, "on": None}
                                           for scope in json.loads(self.sent.read_text())["requests"][self.agency]["scopes"]])
        self.assertEqual({k: v for k, v in row.items() if k not in ("agency_id", "name")}, GOLDEN_ROW)
        self.assertEqual(err, f"{self.agency}: fee estimate $245.50 is unpaid\n")
        cells, values = self.csv_row("legacy")
        self.assertEqual(values[2:13], GOLDEN_CSV)
        self.assertEqual(values[13:], ["$245.50", "unpaid", "", "", ""])  # fee columns, then request_url and owner
        cover, totals = self.coverage()
        self.assertEqual((cover["fee_state"], totals["fee_unpaid"]), ("unpaid", 1))
        table = kit_track.format_coverage(kit_track.coverage(self.root))
        self.assertEqual(table.splitlines()[0].split()[-4:], ["fee", "scopes", "request", "owner"])
        self.assertIn("fee_unpaid 1", table)
        self.assertEqual(self.sent.read_bytes(), before)

    def test_hand_edited_amount_on_waived_is_malformed(self):
        estimate = {"on": "2026-03-05", "kind": "fee_estimate", "amount_cents": 100, "recorded_at": STAMP}
        for bad in ({"kind": "fee_waived", "amount_cents": 100}, {"kind": "fee_paid", "amount_cents": 1.5},
                    {"kind": "fee_paid", "amount_cents": 10_000_001}, {"kind": "fee_paid", "amount_cents": True}):
            with self.subTest(step=bad):
                self.edit_steps([estimate, {"on": "2026-03-06", "recorded_at": STAMP, **bad}])
                self.refused_untouched(("2026-03-07", "acknowledged"), MALFORMED)
                code, out, err = run("kit-status", "--directory", self.root)
                self.assertEqual((code, out), (1, ""))
                self.assertIn(MALFORMED, err)

    def test_csv_formula_guard_in_new_layout(self):
        path = self.kit / "agencies.json"
        agencies = json.loads(path.read_text(encoding="utf-8"))
        next(a for a in agencies if a["agency_id"] == self.agency)["name"] = "=HYPERLINK(\"x\")"
        path.write_text(json.dumps(agencies), encoding="utf-8")
        self.ok("2026-03-05", "fee_estimate", "--amount", "5")
        self.ok("2026-03-06", "fee_paid", "--amount", "5")
        cells, values = self.csv_row("guard")
        self.assertEqual(cells["name"], "'=HYPERLINK(\"x\")")
        self.assertEqual(values[13:], ["$5.00", "paid", "$5.00", "", ""])


if __name__ == "__main__":
    unittest.main()
