"""kit-sent-import: a prospective sent.json that cannot be encoded as UTF-8 is a fixed exit-2 refusal. Offline, synthetic."""
import json
import subprocess
import sys
import unittest
from unittest import mock

from campaign_tool import kit, kit_import_sent, kit_track
from tests import test_kit_sent_import as base

FIXED = "kit/sent.json: import cannot be encoded as UTF-8; nothing was written"
GOOD = {"imported": True, "sent_on": "2026-03-02", "channel": "email", "recorded_at": "2026-03-03T10:00:00+00:00",
        "steps": []}


class SentImportEncodingTests(unittest.TestCase):
    setUp = base.KitSentImportTests.setUp
    csv, do, api, stored = (base.KitSentImportTests.csv, base.KitSentImportTests.do,
                            base.KitSentImportTests.api, base.KitSentImportTests.stored)

    def put(self, text):
        """Write the sent store as real ASCII-escaped JSON text (escapes are spliced in as given)."""
        self.sent.write_text(text, encoding="utf-8")

    def store_with(self, extension_json):
        entry = json.dumps(GOOD)[:-1] + ", " + extension_json + "}"
        self.put('{"schema_version": 1, "requests": {"%s": %s}}' % (self.c, entry))

    def test_lone_surrogate_extensions_refuse_preview_and_apply(self):
        path = self.csv(f"{self.a},,2026-03-02,email,,")
        for label, ext in {
                "high value": '"x": "a\\ud800b"', "low value": '"x": "\\udfff"',
                "key": '"k\\udc00": 1', "nested": '"x": [{"y": ["\\ud800"]}]'}.items():
            with self.subTest(label):
                self.store_with(ext)
                kit_track.read_sent(self.root)  # the shared reader really admits it
                before = base.snapshot(self.root)
                for apply in (False, True):
                    with mock.patch.object(kit, "_write_json") as writer:
                        with self.assertRaises(kit_import_sent.SentImportError) as raised:
                            self.api(path, apply=apply)
                    self.assertEqual(str(raised.exception), FIXED)
                    self.assertEqual(raised.exception.exit_code, 2)
                    self.assertTrue(raised.exception.__suppress_context__)
                    writer.assert_not_called()
                self.assertEqual(base.snapshot(self.root), before)

    def test_cli_and_subprocess_refusal_is_only_the_stopped_line(self):
        path = self.csv(f"{self.a},,2026-03-02,email,,")
        self.store_with('"x": "\\ud800"')
        before = base.snapshot(self.root)
        for extra in ((), ("--apply",)):
            self.assertEqual(self.do(path, *extra), (2, "", f"Stopped: {FIXED}\n"))
        done = subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "kit-sent-import", "--directory",
                               str(self.root), "--csv", str(path), "--apply"], cwd=base.REPO_ROOT,
                              capture_output=True, text=True, timeout=60)
        self.assertEqual((done.returncode, done.stdout, done.stderr), (2, "", f"Stopped: {FIXED}\n"))
        self.assertEqual(base.snapshot(self.root), before)
        # restoring a healthy store lets the same CSV import
        self.put(json.dumps({"schema_version": 1, "requests": {}}))
        self.assertEqual(self.api(path, apply=True)["imported"], 1)

    def test_valid_unicode_and_escaped_surrogate_pair_still_import(self):
        self.store_with('"x": "caf\\u00e9 \\ud83d\\udeb2 \\u00e9"')
        path = self.csv(f"{self.a},,2026-03-02,email,,")
        self.assertEqual(self.api(path)["written"], False)
        self.assertEqual(self.api(path, apply=True)["imported"], 1)
        requests = self.stored()
        self.assertEqual(requests[self.c]["x"], "caf\u00e9 \U0001f6b2 \u00e9")
        self.assertEqual(list(requests), sorted(requests))
        self.assertTrue(self.sent.read_bytes().endswith(b"\n"))
        self.assertEqual(self.sent.stat().st_mode & 0o777, 0o600)

    def test_capacity_counts_utf8_bytes_of_the_whole_document(self):
        path = self.csv(f"{self.a},,2026-03-02,email,,")
        for extra, ok in ((0, True), (1, False)):
            with self.subTest(extra=extra):
                self.store_with('"x": "\u00e9"')
                path2 = self.csv(f"{self.b},,2026-03-02,email,,", name="two.csv")
                # measure the exact prospective size once, then set the cap around it
                with mock.patch.object(kit, "_write_json") as writer:
                    self.api(path2, apply=True)
                document = writer.call_args[0][1]
                exact = len((json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
                self.assertGreater(exact, len(json.dumps(document)) - 1000)
                with mock.patch.object(kit_track, "MAX_BYTES", exact - extra), \
                        mock.patch.object(kit, "_write_json") as writer:
                    if ok:
                        self.api(path2, apply=True)
                        writer.assert_called_once()
                    else:
                        with self.assertRaisesRegex(kit_import_sent.SentImportError, "larger than 1 MiB"):
                            self.api(path2, apply=True)
                        writer.assert_not_called()

    def test_existing_bad_row_refusal_keeps_precedence(self):
        self.store_with('"x": "\\ud800"')
        message = self.refuse_text(self.csv(f"{self.a},,not-a-date,email,,"))
        self.assertIn("line 2", message)
        self.assertNotEqual(message, FIXED)

    def refuse_text(self, path):
        with self.assertRaises(kit_import_sent.SentImportError) as raised:
            self.api(path, apply=True)
        return str(raised.exception)

    def test_new_catch_is_local_to_preflight(self):
        path = self.csv(f"{self.a},,2026-03-02,email,,")
        for error in (RuntimeError("x"), RecursionError("x"), AssertionError("x")):
            with self.subTest(error=type(error).__name__), \
                    mock.patch.object(kit_import_sent.json, "dumps", side_effect=error):
                with self.assertRaises(type(error)) as raised:
                    self.api(path, apply=True)
                self.assertIs(raised.exception, error)
        boom = UnicodeEncodeError("utf-8", "x", 0, 1, "writer")
        with mock.patch.object(kit, "_write_json", side_effect=boom):
            with self.assertRaises(UnicodeEncodeError) as raised:
                self.api(path, apply=True)
        self.assertIs(raised.exception, boom)


if __name__ == "__main__":
    unittest.main()
