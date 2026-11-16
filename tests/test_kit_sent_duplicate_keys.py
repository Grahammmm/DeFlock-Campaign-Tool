"""Duplicate decoded JSON member names in kit/sent.json refuse the whole store. Offline, synthetic."""
import json
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import kit_fees, kit_timing, kit_track  # noqa: E402
import tests.test_kit_sent as base  # noqa: E402

run = base.run

REFUSAL = "kit/sent.json unreadable: duplicate JSON keys"


class KitSentDuplicateKeyTests(unittest.TestCase):
    setUp = base.KitSentTests.setUp
    send = base.KitSentTests.send

    def record(self):
        code, _, err = self.send()
        self.assertEqual(code, 0, err)
        return self.sent.read_text()

    def entry_text(self, text, extra=""):
        """Return (text with a duplicate member spliced into the first request entry)."""
        return text.replace('"channel": "email"', '"channel": "email"' + extra, 1)

    def assert_reader_refuses(self, text):
        self.sent.write_text(text)
        with self.assertRaises(ValueError) as caught:
            kit_track.read_sent(self.root)
        self.assertEqual(str(caught.exception), REFUSAL)

    def test_duplicate_root_requests_cannot_hide_then_erase_prior_request(self):
        good = self.record()
        ambiguous = good.rstrip().rstrip("}").rstrip() + ',\n  "requests": {}\n}\n'
        self.sent.write_text(ambiguous)
        with self.assertRaises(ValueError) as caught:
            kit_track.read_sent(self.root)
        self.assertEqual(str(caught.exception), REFUSAL)
        code, out, err = self.send(self.other)
        self.assertEqual((code, out), (1, ""))
        self.assertIn(REFUSAL, err)
        self.assertEqual(self.sent.read_text(), ambiguous)
        self.sent.write_text(good)
        code, _, err = self.send(self.other)
        self.assertEqual(code, 0, err)
        self.assertEqual(set(kit_track.read_sent(self.root)), {self.agency, self.other})

    def test_duplicate_members_at_every_object_depth_refuse(self):
        good = self.record()
        cases = {
            "schema_version": good.replace('"schema_version": 1', '"schema_version": 1,\n  "schema_version": 1', 1),
            "sent_on equal": good.replace('"sent_on": "2026-03-02"', '"sent_on": "2026-03-02", "sent_on": "2026-03-02"', 1),
            "sent_on conflict": good.replace('"sent_on": "2026-03-02"', '"sent_on": "2026-03-03", "sent_on": "2026-03-02"', 1),
            "channel": good.replace('"channel": "email"', '"channel": "email", "channel": "portal"', 1),
            "agency key": good.replace(f'"{self.agency}": {{', f'"{self.agency}": {{}}, "{self.agency}": {{', 1),
            "unknown nested": self.entry_text(good, ', "x": {"a": 1, "a": 2}'),
            "object in array": self.entry_text(good, ', "x": [{"a": 1, "a": null}]'),
        }
        for label, text in cases.items():
            with self.subTest(label=label):
                self.assertNotEqual(text, good)
                self.assert_reader_refuses(text)

    def test_nested_step_and_fee_duplicates_refuse(self):
        self.record()
        code, _, err = run("kit-response", "--directory", self.root, "--agency", self.agency, "--date", "2026-03-05",
                           "--kind", "fee_estimate", "--amount", "100")
        self.assertEqual(code, 0, err)
        good = self.sent.read_text()
        self.assertTrue(kit_fees.fees(self.root))
        match = re.search(r'"amount_cents": \d+', good)
        assert match is not None, good
        for dup in (match.group(0) + ", " + '"amount_cents": 1', '"amount_cents": 1, ' + match.group(0)):
            with self.subTest(dup=dup):
                self.sent.write_text(good.replace(match.group(0), dup, 1))
                with self.assertRaises(ValueError) as caught:
                    kit_fees.fees(self.root)
                self.assertEqual(str(caught.exception), REFUSAL)
        kind = '"kind": "fee_estimate"'
        self.assertIn(kind, good)
        self.assert_reader_refuses(good.replace(kind, kind + ", " + kind, 1))

    def test_decoded_member_aliases_collide_but_distinct_strings_do_not(self):
        good = self.record()
        canary = "PRIVATE-CANARY-9"
        alias = good.rstrip().rstrip("}").rstrip() + ',\n  "requ\\u0065sts": {"' + canary + '": 1}\n}\n'
        self.sent.write_text(alias)
        with self.assertRaises(ValueError) as caught:
            kit_track.read_sent(self.root)
        self.assertEqual(str(caught.exception), REFUSAL)
        self.assertNotIn(canary, str(caught.exception))
        self.assertNotIn(str(self.root), str(caught.exception))
        nested = self.entry_text(good, ', "k": 1, "\\u006b": 2')
        self.assert_reader_refuses(nested)
        distinct = self.entry_text(good, ', "k": 1, "K": 2, "\\u00e9": 3, "e\\u0301": 4')
        self.sent.write_text(distinct)
        self.assertEqual(kit_track.read_sent(self.root)[self.agency]["K"], 2)

    def test_unique_objects_and_arrays_preserve_existing_results(self):
        self.record()
        code, _, err = run("kit-response", "--directory", self.root, "--agency", self.agency, "--date", "2026-03-05",
                           "--kind", "acknowledged")
        self.assertEqual(code, 0, err)
        before = kit_track.read_sent(self.root)
        text = self.sent.read_text()
        self.sent.write_text(self.entry_text(text, ', "x": [{"a": 1}, {"a": 1}, {"kind": 1}], "y": {"kind": 2}'))
        after = kit_track.read_sent(self.root)
        self.assertEqual(after[self.agency]["steps"], before[self.agency]["steps"])
        self.assertEqual(after[self.agency]["x"], [{"a": 1}, {"a": 1}, {"kind": 1}])
        self.sent.write_text(text)
        self.assertEqual(kit_track.read_sent(self.root), before)

    def test_reports_and_exports_refuse_without_partial_success(self):
        good = self.record()
        self.sent.write_text(good.rstrip().rstrip("}").rstrip() + ',\n  "requests": {}\n}\n')
        before = self.sent.read_bytes()
        out_dir = self.root.parent / "exports"
        out_dir.mkdir()
        commands = [
            ("kit-status",), ("kit-status", "--csv", out_dir / "s.csv"),
            ("kit-fees",), ("kit-fees", "--csv", out_dir / "f.csv"),
            ("kit-timing", "--as-of", "2026-01-01"), ("kit-timing", "--csv", out_dir / "t.csv"),
        ]
        for command in commands:
            with self.subTest(command=command[0], extra=len(command)):
                code, out, err = run(*command[:1], "--directory", self.root, *command[1:])
                self.assertEqual((code, out), (1, ""))
                self.assertIn(REFUSAL, err)
                self.assertNotIn("Traceback", err)
        code, out, err = run("kit-calendar", "--directory", self.root, "--out", out_dir / "d.ics")
        self.assertEqual((code, out), (3, ""))
        self.assertIn(REFUSAL, err)
        self.assertEqual(list(out_dir.iterdir()), [])
        self.assertEqual(self.sent.read_bytes(), before)
        for api in (kit_fees.fees, lambda r: kit_timing.report(r, "2026-04-01"), kit_track.status):
            with self.assertRaises(ValueError):
                api(self.root)

    def test_tracking_mutators_leave_ambiguous_store_untouched(self):
        self.record()
        code, _, err = run("kit-response", "--directory", self.root, "--agency", self.agency, "--date", "2026-03-05",
                           "--kind", "acknowledged")
        self.assertEqual(code, 0, err)
        good = self.sent.read_text()
        self.sent.write_text(good.rstrip().rstrip("}").rstrip() + ',\n  "requests": {}\n}\n')
        before = self.sent.read_bytes()

        def tree():
            return sorted((str(p.relative_to(self.root)), p.stat().st_mtime_ns, p.stat().st_ino)
                          for p in self.root.rglob("*") if p.is_file())
        snapshot = tree()
        for command in (
            ("kit-response", "--agency", self.agency, "--date", "2026-03-06", "--kind", "closed"),
            ("kit-response-undo", "--agency", self.agency, "--reason", "test"),
        ):
            with self.subTest(command=command[0]):
                with mock.patch.object(kit_track.kit, "_write_json", side_effect=AssertionError("write")):
                    code, out, err = run(command[0], "--directory", self.root, *command[1:])
                self.assertEqual((code, out), (1, ""))
                self.assertIn(REFUSAL, err)
        code, out, err = self.send(self.other)
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(self.sent.read_bytes(), before)
        self.assertEqual(tree(), snapshot)

    def test_existing_reader_failures_stay_distinct(self):
        self.assertEqual(kit_track.read_sent(self.root), {})
        for text, reason in (("", "not JSON"), ("{bad", "not JSON"), ("[]", "expected schema_version 1")):
            with self.subTest(text=text):
                self.sent.write_text(text)
                with self.assertRaises(ValueError) as caught:
                    kit_track.read_sent(self.root)
                self.assertIn(reason, str(caught.exception))
                self.assertNotIn("duplicate", str(caught.exception))
        self.sent.write_text("{}")
        with mock.patch.object(kit_track.json, "loads", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                kit_track.read_sent(self.root)


if __name__ == "__main__":
    unittest.main()
