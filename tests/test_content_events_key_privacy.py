"""RF0645-6: an unknown campaign-event key the shared privacy lint matches is named by length, never echoed.

Synthetic copies of the fictional example only; sockets are blocked and nothing is fetched.
"""
import json
import subprocess
import sys
import unittest

from campaign_tool.content import ContentError
from campaign_tool.content_check import content_problems
from campaign_tool.content_events import load_events, read_events
from tests.test_site_events import REPO_ROOT, EventsCase, event

STANDARD, COMMERCIAL = "7ABC123", "4A12345"
PLATES = (STANDARD, COMMERCIAL)


def with_key(key, number=1, **changes):
    row = event(number, **changes)
    row[key] = "canary-value"
    return row


class ValidatorTests(unittest.TestCase):
    def test_unknown_standard_and_commercial_plate_keys_use_length_only_labels(self):
        for key in PLATES:
            document = [with_key(key)]
            with self.assertRaises(ContentError) as caught:
                load_events(document)
            self.assertEqual(str(caught.exception), "events.json[0]: unknown field 7 chars")
            problems = ["sentinel"]
            self.assertIsNone(load_events(document, problems))
            self.assertEqual(problems, ["sentinel", "events.json[0]: unknown field 7 chars"])
            for text in [str(caught.exception)] + problems:
                self.assertNotIn(key, text)
                self.assertNotIn("canary", text)
        escaped = json.loads('[{"id": "forum-1", "title": "T", "starts_at": "2026-10-10T18:00:00Z", '
                             '"ends_at": "2026-10-10T19:00:00Z", "description_md": "D", "online": true, "\\u0037ABC\\u0031\\u0032\\u0033": 1}]')
        problems = []
        self.assertIsNone(load_events(escaped, problems))
        self.assertEqual(problems, ["events.json[0]: unknown field 7 chars"])

    def test_multiple_private_keys_remain_independent_sorted_problems(self):
        first = with_key(STANDARD, 1)
        first[COMMERCIAL] = 2
        first["surprise"] = 3
        first["a<b"] = 4
        second = event(2, title=5)
        second["7XYZ999"] = 1
        document = [first, second]
        snapshot = json.dumps(document, sort_keys=True)
        problems = ["sentinel"]
        self.assertIsNone(load_events(document, problems))
        self.assertEqual(problems[0], "sentinel")
        rows = [p for p in problems[1:] if p.startswith("events.json[0]")]
        self.assertEqual(rows, ["events.json[0]: unknown field 7 chars", "events.json[0]: unknown field 7 chars",
                                "events.json[0]: unknown field 3 chars", "events.json[0]: unknown field surprise"])
        self.assertEqual([p for p in problems if p.startswith("events.json[1]: ")], ["events.json[1]: unknown field 7 chars"])
        self.assertTrue(any(p.startswith("events.json[1].title") for p in problems))
        self.assertEqual(len(problems), 1 + 4 + 2)
        self.assertEqual(json.dumps(document, sort_keys=True), snapshot)
        joined = "\n".join(problems)
        for leak in PLATES + ("7XYZ999", "canary"):
            self.assertNotIn(leak, joined)

    def test_harmless_and_already_masked_key_labels_keep_legacy_behavior(self):
        literal = ("a", "surprise", "x_1", "A" * 40)
        masked = {"": "0 chars", "A" * 41: "41 chars", "a<b": "3 chars", "line\nbreak": "10 chars",
                  "caf\u00e9": "4 chars", "\ud800x": "2 chars", "jane@example.com": "16 chars",
                  "(805) 555-0142": "14 chars"}
        for key in literal:
            with self.assertRaises(ContentError) as caught:
                load_events([with_key(key)])
            self.assertEqual(str(caught.exception), f"events.json[0]: unknown field {key}")
        for key, label in masked.items():
            with self.assertRaises(ContentError) as caught:
                load_events([with_key(key)])
            self.assertEqual(str(caught.exception), f"events.json[0]: unknown field {label}")
        row = event(1)
        row[5] = 1  # a direct-API scalar key keeps the old length-only fallback
        with self.assertRaises(ContentError) as caught:
            load_events([row])
        self.assertEqual(str(caught.exception), "events.json[0]: unknown field 1 chars")
        self.assertEqual(load_events([event(1)]), [event(1)])


class FileCase(EventsCase):
    def write_plate_key(self, key=STANDARD):
        data = json.dumps([event(1), with_key(key, 2)], ensure_ascii=True).encode("ascii")
        self.put(data)
        return data

    def snapshot(self):
        return {p.relative_to(self.root).as_posix(): (p.read_bytes() if p.is_file() else None, p.lstat().st_mode, p.lstat().st_mtime_ns)
                for p in sorted(self.root.rglob("*"))}

    def cli(self, *args):
        return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", *args], cwd=REPO_ROOT,
                              capture_output=True, text=True, encoding="utf-8", timeout=120)

    def assert_clean(self, result, *leaks):
        text = result.stdout + result.stderr
        for leak in leaks + PLATES + ("canary", "Traceback"):
            self.assertNotIn(leak, text)
        for escaped in ("\\u0037", "7\\u0041"):
            self.assertNotIn(escaped, text)


class ReaderCheckerTests(FileCase):
    def test_real_reader_and_content_checker_share_sanitized_key_refusal(self):
        for key in PLATES:
            data = self.write_plate_key(key)
            line = "events.json[1]: unknown field 7 chars"
            with self.assertRaises(ContentError) as caught:
                read_events(self.root)
            self.assertEqual(str(caught.exception), line)
            problems = []
            self.assertIsNone(read_events(self.root, problems))
            self.assertEqual(problems, [line])
            found = [p for p in content_problems(self.root) if p.startswith("events.json")]
            self.assertEqual(found, [line])
            self.assertEqual(self.path.read_bytes(), data)
        result = self.cli("content-check", "--json", "--directory", str(self.root))
        self.assertEqual(result.returncode, 3, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["schema"], "content-check-v1")
        records = [r for r in report["problems"] if r["file"].startswith("events.json")]
        self.assertEqual(records, [{"file": "events.json[1]", "message": "unknown field 7 chars"}])
        self.assertEqual(result.stderr, "")
        self.assert_clean(result)
        plain = self.cli("content-check", "--directory", str(self.root))
        self.assertEqual(plain.returncode, 3, plain.stderr)
        self.assertIn("events.json[1]: unknown field 7 chars", plain.stdout)
        self.assert_clean(plain)


class CliTests(FileCase):
    def test_cli_build_and_event_list_never_echo_private_unknown_keys(self):
        for key in PLATES:
            self.write_plate_key(key)
            build = self.cli("build", "--directory", str(self.root))
            self.assertEqual(build.returncode, 1, build.stderr)
            self.assertIn("events.json[1]: unknown field 7 chars", build.stderr)
            self.assert_clean(build)
            listing = self.cli("event", "list", "--directory", str(self.root), "--as-of", "2026-10-01")
            self.assertNotEqual(listing.returncode, 0)
            self.assertIn("unknown field 7 chars", listing.stdout + listing.stderr)
            self.assert_clean(listing)

    def test_refusal_preserves_existing_and_absent_outputs(self):
        self.put([event(1)])
        self.build()
        (self.root / "kit").mkdir(exist_ok=True)
        (self.root / "kit" / "private.txt").write_bytes(b"private kit bytes")
        for with_public in (True, False):
            if not with_public:
                import shutil
                shutil.rmtree(self.public)
            self.write_plate_key()
            before = self.snapshot()
            with self.assertRaises(ContentError) as caught:
                self.build()
            self.assertEqual(str(caught.exception), "events.json[1]: unknown field 7 chars")
            read_events(self.root, [])
            content_problems(self.root)
            self.cli("build", "--directory", str(self.root))
            self.cli("event", "list", "--directory", str(self.root), "--as-of", "2026-10-01")
            self.assertEqual(self.snapshot(), before)
            self.assertEqual(self.public.exists(), with_public)

    def test_valid_events_unicode_missing_and_empty_outputs_are_unchanged(self):
        title = "Caf\u00e9 \u81ea\u884c\u8f66 \U0001F6B2"
        rows = [event(1, title=title), event(2, venue=None, online=True)]
        self.assertEqual(load_events(rows), rows)
        self.put(rows)
        first = self.build()
        self.assertIn(title, first["events.html"].decode("utf-8"))
        self.assertEqual(self.build(), first)
        self.path.unlink()
        self.assertIsNone(read_events(self.root))
        self.put([])
        self.assertEqual(read_events(self.root), [])


if __name__ == "__main__":
    unittest.main()
