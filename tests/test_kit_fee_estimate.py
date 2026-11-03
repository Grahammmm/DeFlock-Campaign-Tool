"""kit-response --kind fee_estimate records a dated fee estimate in cents; kit-status compares the latest one with the
request-settings fee cap. Nothing is paid or sent. Offline, synthetic."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import kit  # noqa: E402
from tests.test_kit_check import ready_drafts  # noqa: E402
from tests.test_kit_response import SENT_ON, run  # noqa: E402

FLAG = "over your fee cap: decide before paying"
OLD_KEYS = {"agency_id", "request", "name", "sent_on", "channel", "draft_changed_since_sent", "determination_due",
            "with_extension", "steps", "extended", "closed", "last_step", "records", "record_prefixes"}


class FeeEstimateTests(unittest.TestCase):
    def setUp(self):
        offline = mock.patch("urllib.request.urlopen", side_effect=AssertionError("network used"))
        offline.start()
        self.addCleanup(offline.stop)
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-kit-fee-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
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
        self.assertIn("Recorded only. Nothing was sent.", out)
        return json.loads(out.split("Recorded only.")[0])

    def refused_untouched(self, result_args, *words):
        before = self.sent.read_bytes()
        code, out, err = self.step(*result_args)
        self.assertEqual(code, 1, out)
        self.assertTrue(err.startswith("Stopped: "), err)
        for word in words:
            self.assertIn(word, err)
        self.assertEqual(self.sent.read_bytes(), before)

    def cap(self, value):
        document = {"schema_version": 1, **({} if value is None else {"fee_cap_usd": value})}
        (self.kit / kit.SETTINGS_NAME).write_text(json.dumps(document), encoding="utf-8")

    def status(self, *extra):
        code, out, err = run("kit-status", "--directory", self.root, *extra)
        self.assertEqual(code, 0, err)
        return {(r["agency_id"], r["request"]): r for r in json.loads(out)["requests"]}, err

    def row(self):
        rows, err = self.status()
        return rows[(self.agency, "main")], err

    def test_estimate_stored_in_cents_and_compared_with_cap(self):
        printed = self.ok("2026-03-05", "fee_estimate", "--amount", "245.50")
        self.assertEqual(printed["steps"][-1]["amount_cents"], 24550)
        stored = json.loads(self.sent.read_text())["requests"][self.agency]["steps"]
        self.assertEqual([(s["on"], s["kind"], s["amount_cents"]) for s in stored],
                         [("2026-03-05", "fee_estimate", 24550)])
        self.cap(200)
        row, err = self.row()
        self.assertEqual((row["fee_estimate"], row["over_fee_cap"]), ("$245.50", True))
        self.assertIn(f"{self.agency}: fee estimate $245.50 is {FLAG}", err)
        self.cap(245.5)  # equal is not over
        row, err = self.row()
        self.assertEqual((row["fee_estimate"], row["over_fee_cap"]), ("$245.50", False))
        self.assertNotIn(FLAG, err)
        self.ok("2026-03-06", "fee_estimate", "--amount", "0")
        self.cap(0)  # cap 0, estimate 0: not over
        row, err = self.row()
        self.assertEqual((row["fee_estimate"], row["over_fee_cap"]), ("$0.00", False))
        self.assertNotIn(FLAG, err)

    def test_no_settings_or_no_cap_shows_estimate_without_comparison(self):
        self.ok("2026-03-05", "fee_estimate", "--amount", "100000")
        for setup in (lambda: None, lambda: self.cap(None)):
            setup()
            row, err = self.row()
            self.assertEqual((row["fee_estimate"], row["over_fee_cap"]), ("$100,000.00", None))
            self.assertNotIn(FLAG, err)

    def test_latest_date_then_latest_recorded_wins(self):
        self.cap(100)
        self.ok("2026-03-05", "fee_estimate", "--amount", "50")
        self.ok("2026-03-09", "fee_estimate", "--amount", "150")
        self.assertEqual(self.row()[0]["fee_estimate"], "$150.00")
        self.ok("2026-03-09", "fee_estimate", "--amount", "99.99")  # same date: the later recorded step wins
        row, err = self.row()
        self.assertEqual((row["fee_estimate"], row["over_fee_cap"]), ("$99.99", False))
        self.assertNotIn(FLAG, err)
        self.refused_untouched(("2026-03-08", "fee_estimate", "--amount", "500"), "before the previous step")
        self.ok("2026-03-10", "closed")
        self.refused_untouched(("2026-03-11", "fee_estimate", "--amount", "500"), "no step can follow closed")
        self.assertEqual(self.row()[0]["fee_estimate"], "$99.99")

    def test_amount_and_kind_must_match(self):
        self.refused_untouched(("2026-03-05", "produced", "--amount", "10"), "--amount is only for fee_estimate")
        self.refused_untouched(("2026-03-05", "fee_estimate"), "fee_estimate needs --amount")
        self.assertEqual(json.loads(self.sent.read_text())["requests"][self.agency]["steps"], [])

    def test_bad_amounts_refused_and_nothing_written(self):
        for amount in ("-1", "100000.01", "1e3", "12.345", "NaN", "1,000", "", " 5", "\u0663", "1000000", ".5"):
            with self.subTest(amount=amount):
                self.refused_untouched(("2026-03-05", "fee_estimate", "--amount", amount), "--amount must be")
        self.assertEqual(json.loads(self.sent.read_text())["requests"][self.agency]["steps"], [])

    def test_malformed_amount_in_file_refused_never_rewritten(self):
        base = {"on": "2026-03-05", "recorded_at": "2026-03-05T00:00:00+00:00"}
        bad_steps = ({"kind": "produced", "amount_cents": 100}, {"kind": "fee_estimate", "amount_cents": 245.5},
                     {"kind": "fee_estimate", "amount_cents": True}, {"kind": "fee_estimate"},
                     {"kind": "fee_estimate", "amount_cents": 10_000_001}, {"kind": "fee_estimate", "amount_cents": -1})
        for bad in bad_steps:
            with self.subTest(step=bad):
                document = json.loads(self.sent.read_text())
                document["requests"][self.agency]["steps"] = [{**base, **bad}]
                self.sent.write_text(json.dumps(document), encoding="utf-8")
                self.refused_untouched(("2026-03-06", "fee_estimate", "--amount", "1"), "malformed steps")
                code, out, err = run("kit-status", "--directory", self.root)
                self.assertEqual(code, 1, out)
                self.assertIn("malformed steps", err)

    def test_old_files_unchanged_plus_two_none_keys(self):
        self.ok("2026-03-05", "acknowledged")
        (self.kit / kit.SETTINGS_NAME).write_text("not json", encoding="utf-8")  # unread without an estimate
        row, err = self.row()
        self.assertEqual(set(row), OLD_KEYS | {"fee_estimate", "over_fee_cap", "stated_production_date"})
        self.assertEqual((row["fee_estimate"], row["over_fee_cap"], row["steps"]), (None, None, 1))
        self.assertEqual(row["last_step"], {"on": "2026-03-05", "kind": "acknowledged"})
        self.assertEqual(err, "")

    def test_request_scopes_the_estimate(self):
        named = self.kit / "requests" / f"{self.agency}--fees.md"
        named.write_text("Narrowed request: the 2026 contract only.\n", encoding="utf-8")
        code, _, err = run("kit-sent", "--directory", self.root, "--agency", self.agency, "--date", SENT_ON,
                           "--channel", "email", "--request", "fees")
        self.assertEqual(code, 0, err)
        self.cap(10)
        self.ok("2026-03-05", "fee_estimate", "--amount", "20", "--request", "fees")
        rows, err = self.status()
        self.assertEqual((rows[(self.agency, "main")]["fee_estimate"], rows[(self.agency, "main")]["over_fee_cap"]),
                         (None, None))
        self.assertEqual((rows[(self.agency, "fees")]["fee_estimate"], rows[(self.agency, "fees")]["over_fee_cap"]),
                         ("$20.00", True))
        self.assertIn(f"{self.agency}/fees: fee estimate $20.00 is {FLAG}", err)
        self.assertEqual(err.count(FLAG), 1)
        rows, err = self.status("--request", "main")
        self.assertEqual(list(rows), [(self.agency, "main")])
        self.assertNotIn(FLAG, err)


if __name__ == "__main__":
    unittest.main()
