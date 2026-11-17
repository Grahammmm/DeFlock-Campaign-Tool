"""RF1501-6: a repeated JSON object key in events.json is refused before validation. Synthetic data only."""
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool import content_events
from campaign_tool.content import ContentError
from campaign_tool.content_check import content_problems
from campaign_tool.content_events import read_events
from campaign_tool.site import build_site

from tests.test_site_events import TODAY, EventsCase, event

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXED = "events.json: a JSON object repeats a key"


def good():
    return json.dumps([event(1)], ensure_ascii=False)


def with_extra(extra):
    """Literal bytes of one valid event with ``extra`` (raw JSON members) appended; json.dumps would erase repeats."""
    return (good()[:-2] + "," + extra + "}]").encode("utf-8")


def snapshot(root):
    return {p.relative_to(root).as_posix(): (p.read_bytes(), p.stat().st_mode, p.stat().st_mtime_ns)
            for p in sorted(root.rglob("*")) if p.is_file()}


class DuplicateKeyTests(EventsCase):
    def cli(self, *args):
        return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", *args, "--directory", str(self.root)],
                              cwd=REPO_ROOT, capture_output=True, text=True, encoding="utf-8", timeout=120)

    def test_repeated_event_properties_refused_before_schema(self):
        cases = {
            "title-different": with_extra('"title":"Replacement"'),
            "title-identical": with_extra('"title":"Teach-in 1"'),
            "bad-type-last": with_extra('"title":5'),
            "bad-type-first": b'[{"title":5,"title":"ok"}]',
            "id": with_extra('"id":"forum-9"'),
            "starts_at": with_extra('"starts_at":"2026-10-11T18:00:00Z"'),
            "venue": with_extra('"venue":"Elsewhere"'),
            "online": with_extra('"online":true,"online":false'),
            "description_md": with_extra('"description_md":"x"'),
            "three": with_extra('"title":"a","title":"b"'),
        }
        for name, data in cases.items():
            with self.subTest(name=name):
                self.put(data)
                with self.assertRaises(ContentError) as caught:
                    read_events(self.root)
                self.assertEqual(str(caught.exception), FIXED)
                self.assertIsNone(caught.exception.__cause__)

    def test_decoded_keys_and_nested_objects_are_checked(self):
        canary = "SECRET-CANARY-KEY"
        for name, data in (("escaped", with_extra('"ti\\u0074le":"x"')),
                           ("nested", with_extra('"surprise":{"a":1,"a":2}')),
                           ("unknown-key", with_extra('"%s":1,"%s":2' % (canary, canary)))):
            with self.subTest(name=name):
                self.put(data)
                with self.assertRaises(ContentError) as caught:
                    read_events(self.root)
                text = str(caught.exception)
                self.assertEqual(text, FIXED)
                for leak in (canary, "Teach-in", str(self.root)):
                    self.assertNotIn(leak, text)
        self.put([event(1), event(2)])  # same names in different objects are fine
        self.assertEqual(len(read_events(self.root)), 2)
        self.put(with_extra('"Title":"x"'))  # different case is a different key: the old schema error, not this one
        with self.assertRaises(ContentError) as caught:
            read_events(self.root)
        self.assertNotEqual(str(caught.exception), FIXED)

    def test_duplicate_reader_collects_once_and_skips_validation(self):
        self.put(b'[{"title":"a","title":"b"},' + good()[1:].encode("utf-8"))
        with mock.patch.object(content_events, "load_events", side_effect=AssertionError("validated")):
            problems = ["earlier"]
            self.assertIsNone(read_events(self.root, problems))
            self.assertEqual(problems, ["earlier", FIXED])
            with self.assertRaisesRegex(ContentError, FIXED):
                read_events(self.root)
        for _ in range(2):  # repeated failures do not leak the read stream
            with self.assertRaises(ContentError):
                read_events(self.root)

    def test_content_check_plain_and_json_report_duplicate(self):
        self.put(with_extra('"title":"x"'))
        self.put({"status": "postponed"}, name="meetings/d.json")
        problems = content_problems(self.root)
        self.assertEqual(problems.count(FIXED), 1)
        self.assertTrue(any("d.json" in p for p in problems), problems)
        text = self.cli("content-check")
        self.assertEqual((text.returncode, text.stderr), (3, ""))
        self.assertIn(FIXED, text.stdout.splitlines())
        as_json = self.cli("content-check", "--json")
        self.assertEqual((as_json.returncode, as_json.stderr), (3, ""))
        self.assertIn("a JSON object repeats a key", as_json.stdout)
        self.assertNotIn("Traceback", text.stdout + as_json.stdout)
        (self.content / "meetings" / "d.json").unlink()
        built = self.cli("build")
        self.assertEqual(built.returncode, 1)
        self.assertIn(FIXED, built.stdout + built.stderr)
        self.assertNotIn("Traceback", built.stdout + built.stderr)

    def test_duplicate_build_preserves_tree_and_recovers(self):
        for prebuilt in (False, True):
            with self.subTest(prebuilt=prebuilt):
                self.put(good().encode("utf-8"))
                if prebuilt:
                    build_site(self.root, today=TODAY)
                before = snapshot(self.root)
                self.put(with_extra('"title":"Replacement"'))
                changed = snapshot(self.root)
                with self.assertRaisesRegex(ContentError, FIXED):
                    build_site(self.root, today=TODAY)
                self.assertEqual(snapshot(self.root), changed, "a refused build changed the campaign")
                self.assertEqual(set(before) - {"content/events.json"}, set(changed) - {"content/events.json"})
                self.put(good().encode("utf-8"))
                build_site(self.root, today=TODAY)
                self.assertIn(b"Teach-in 1", (self.public / "events.html").read_bytes())
                self.assertNotIn(b"Replacement", (self.public / "events.html").read_bytes())

    def test_unique_key_controls_and_old_errors_unchanged(self):
        self.put(b"[]")
        self.assertEqual(read_events(self.root), [])
        self.put(('[{"ti\\u0074le":"Escaped","id":"a-1","starts_at":"2026-10-10T18:00:00Z","ends_at":"2026-10-10T19:00:00Z",'
                 '"venue":"Hall","description_md":"Plain \U0001F600"}]').encode("utf-8"))
        self.assertEqual(read_events(self.root)[0]["title"], "Escaped")
        self.path.unlink()
        self.assertIsNone(read_events(self.root))
        self.put(b"[{")
        with self.assertRaisesRegex(ContentError, "not valid UTF-8 JSON"):
            read_events(self.root)


if __name__ == "__main__":
    unittest.main()
