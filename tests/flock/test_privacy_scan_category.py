"""RF0720-12: flock_privacy_scan.py --category NAME (repeatable) checks only the chosen kinds and ends the totals line
` categories=a,b`; --by-category appends ` <kind>=N` per checked kind. Without either flag stdout and the --json-out receipt are
unchanged. Synthetic fixtures only (example.* emails, 555-01xx phones, made-up plates)."""
import json, unittest

from tests.flock.test_privacy_scan import PrivacyScanTest

EMAIL, PHONE, PLATE = "records@city.example.gov", "(805) 555-0142", "8XYZ417"
SENTINEL = "SENTINEL-RF0720-12-k3w"
SECRETS = (EMAIL, PHONE, PLATE, "records@", "555", "8XYZ", SENTINEL)


class CategoryScanTest(unittest.TestCase):
    # borrow the RF1720-5 helpers without re-running its tests here
    setUp, put, run_scan, lines = PrivacyScanTest.setUp, PrivacyScanTest.put, PrivacyScanTest.run_scan, PrivacyScanTest.lines

    def draft(self):
        return self.put("d/req.md", f"To: {EMAIL} {SENTINEL}\nSeen plate {PLATE} {SENTINEL}\n")

    def assert_quiet(self, out):
        # The random temporary root can contain "555" by chance; exclude only that known root (as the JSON test does).
        out = out.replace(str(self.tmp), "<temporary-root>")
        for secret in SECRETS:
            self.assertNotIn(secret, out)

    def test_plate_only(self):
        f = self.draft()
        code, out = self.lines(f, "--category", "plate")
        self.assertEqual((code, out), (3, [f"{f}:2: plate", "files=1 scanned=1 skipped=0 findings=1 allowed=0 categories=plate"]))
        self.assert_quiet("\n".join(out))

    def test_phone_only_is_clean(self):
        f = self.draft()
        self.assertEqual(self.lines(f, "--category", "phone"),
                         (0, ["files=1 scanned=1 skipped=0 findings=0 allowed=0 categories=phone"]))

    def test_by_category_without_category_counts_every_kind(self):
        f = self.draft()
        code, out = self.lines(f, "--by-category")
        self.assertEqual((code, out), (3, [f"{f}:1: email", f"{f}:2: plate",
                                           "files=1 scanned=1 skipped=0 findings=2 allowed=0 email=1 phone=0 plate=1"]))
        self.assert_quiet("\n".join(out))

    def test_patterns_order_whatever_the_flag_order(self):
        f = self.draft()
        self.assertEqual(self.lines(f, "--category", "plate", "--category", "email", "--by-category"),
                         (3, [f"{f}:1: email", f"{f}:2: plate",
                              "files=1 scanned=1 skipped=0 findings=2 allowed=0 categories=email,plate email=1 plate=1"]))

    def test_all_three_named_finds_what_no_flag_finds(self):
        f = self.draft()
        code, out = self.lines(f, "--category", "phone", "--category", "plate", "--category", "email")
        self.assertEqual((code, out[-1]), (3, "files=1 scanned=1 skipped=0 findings=2 allowed=0 categories=email,phone,plate"))
        self.assertEqual(out[:-1], self.lines(f)[1][:-1])

    def test_usage_errors_scan_nothing(self):
        f = self.draft()
        for args in (("--category", "nope"), ("--category", "email", "--category", "email"),
                     ("--category", "plate", "--category", "email", "--category", "plate"), ("--category",),
                     ("--category", "EMAIL"), ("--by-category=1",)):
            code, out, err = self.run_scan(f, *args)
            self.assertEqual((code, out), (2, ""), args)
            self.assertIn("usage:", err)
            self.assert_quiet(err)
        self.assertIn("--category NAME given twice", self.run_scan(f, "--category", "email", "--category", "email")[2])

    def test_allowed_email_counts_allowed_not_kind(self):
        f = self.draft()
        allow = self.put("allow.txt", f"# agency office {SENTINEL}\n{EMAIL.upper()}\n")
        code, out = self.lines(f, "--allow", allow, "--category", "email", "--by-category")
        self.assertEqual((code, out), (0, ["files=1 scanned=1 skipped=0 findings=0 allowed=1 categories=email email=0"]))
        self.assert_quiet("\n".join(out))
        self.assertNotIn("agency", "\n".join(out))
        # a plate-only scan never consults the email: nothing is allowed and the plate is still reported
        self.assertEqual(self.lines(f, "--allow", allow, "--category", "plate")[1][-1],
                         "files=1 scanned=1 skipped=0 findings=1 allowed=0 categories=plate")

    def test_missing_path_beats_findings(self):
        f, gone = self.draft(), self.tmp / "gone.md"
        code, out = self.lines(f, gone, "--category", "plate", "--by-category")
        self.assertEqual((code, out), (1, [f"{f}:2: plate", f"{gone}: missing",
                                           "files=1 scanned=1 skipped=0 findings=1 allowed=0 categories=plate plate=1"]))

    def test_no_flags_unchanged(self):
        # golden lines from RF1720-5's three-category case
        f = self.put("d/story.md", f"intro\nwrite {EMAIL} {SENTINEL}\ncall {PHONE} {SENTINEL}\nplate {PLATE} {SENTINEL}\n")
        code, out, err = self.run_scan(f.parent)
        name = f"{f.parent}/story.md"
        self.assertEqual((code, err), (3, ""))
        self.assertEqual(out, f"{name}:2: email\n{name}:3: phone\n{name}:4: plate\nfiles=1 scanned=1 skipped=0 findings=3 allowed=0\n")
        self.assert_quiet(out)

    def test_receipt_keys_only_with_a_flag(self):
        f = self.draft()
        plain, narrowed, counted = self.tmp / "plain.json", self.tmp / "narrowed.json", self.tmp / "counted.json"
        self.assertEqual(self.run_scan(f, "--json-out", plain)[0], 3)
        doc = json.loads(plain.read_text())
        self.assertNotIn("categories", doc)
        self.assertNotIn("by_category", doc)
        self.assertEqual(self.run_scan(f, "--json-out", narrowed, "--category", "phone")[0], 0)
        doc = json.loads(narrowed.read_text())
        self.assertEqual((doc["categories"], doc["by_category"], doc["files"][0]["findings"]), (["phone"], {"phone": 0}, []))
        self.assertEqual(self.run_scan(f, "--json-out", counted, "--by-category")[0], 3)
        raw = counted.read_text()
        doc = json.loads(raw)
        self.assertEqual((doc["categories"], doc["by_category"]), (["email", "phone", "plate"], {"email": 1, "phone": 0, "plate": 1}))
        self.assertEqual(doc["files"][0]["findings"], [{"line": 1, "category": "email"}, {"line": 2, "category": "plate"}])
        self.assert_quiet(raw)


if __name__ == "__main__":
    unittest.main()
