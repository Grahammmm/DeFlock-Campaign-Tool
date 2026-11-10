"""RF0115-13: event free text must be valid Unicode scalars; a JSON-escaped lone surrogate is refused at load.

Synthetic copies of the fictional example only; sockets are blocked and nothing is fetched.
"""
import json
import subprocess
import sys
import unittest

from campaign_tool.content import ContentError
from campaign_tool.content_check import content_problems
from campaign_tool.content_events import load_events, read_events
from campaign_tool.flyer import build_event
from tests.test_site_events import REPO_ROOT, TODAY, EventsCase, event

FIELDS = ("title", "venue", "description_md", "accessibility_note")
SURROGATES = ("\ud800", "\udbff", "\udc00", "\udfff")
MESSAGE = "must contain valid Unicode scalar values"


def positions(bad):
    return (f"{bad} tail", f"head {bad} tail", f"head {bad}")


class ScalarValidationTests(unittest.TestCase):
    def test_each_event_text_field_refuses_surrogate_boundaries(self):
        for field in FIELDS:
            for bad in SURROGATES:
                for text in positions(bad) + (bad + "\udc00", "\ud83d\ude00"):  # explicit adjacent code units are refused too
                    document = [event(1, **{field: text})]
                    with self.assertRaises(ContentError) as caught:
                        load_events(document)
                    self.assertEqual(str(caught.exception), f"events.json[0].{field}: {MESSAGE}")
                    problems = ["sentinel"]
                    self.assertIsNone(load_events(document, problems))
                    self.assertEqual(problems, ["sentinel", f"events.json[0].{field}: {MESSAGE}"])

    def test_accumulator_reports_independent_unicode_fields_and_rows(self):
        first = event(1, title="Bad \ud800 title", description_md="ok \udc00", accessibility_note="Note \udfff")
        second = event(2, venue="Venue \udbff")
        third = event(3, title=5, description_md="")  # legacy diagnostics, no scalar-check TypeError, not duplicated
        problems = ["sentinel"]
        snapshot = json.dumps([first, second, third], ensure_ascii=True)
        self.assertIsNone(load_events([first, second, third], problems))
        self.assertEqual(problems[0], "sentinel")
        self.assertEqual(problems[1:], [
            f"events.json[0].title: {MESSAGE}", f"events.json[0].description_md: {MESSAGE}",
            f"events.json[0].accessibility_note: {MESSAGE}", f"events.json[1].venue: {MESSAGE}",
            problems[5], problems[6]])
        self.assertEqual([p.split(":")[0] for p in problems[5:]], ["events.json[2].title", "events.json[2].description_md"])
        self.assertTrue(all(MESSAGE not in p for p in problems[5:]))
        self.assertEqual(json.dumps([first, second, third], ensure_ascii=True), snapshot)
        for text in problems:
            self.assertNotIn("ud800", text.lower())
            self.assertNotIn("\ud800", text)

    def test_valid_scalars_and_json_pairs_are_kept_exactly(self):
        pair = json.loads('"Bike \\ud83d\\udeb2 caf\\u00e9"')  # a correctly paired escape decodes to one scalar
        self.assertEqual(pair, "Bike \U0001F6B2 caf\u00e9")
        text = "caf\u00e9 \u81ea\u884c\u8f66 e\u0301 \U0001F6B2"
        document = [event(1, title=text, venue=pair, accessibility_note=text,
                          description_md=f"{text}\n\n\tindented **{pair}**")]
        accepted = load_events(document)
        self.assertEqual(accepted, document)
        self.assertEqual(accepted[0]["title"], text)  # no normalization or replacement
        self.assertEqual(accepted[0]["venue"].encode("utf-8"), pair.encode("utf-8"))
        with self.assertRaises(ContentError):  # LF stays disallowed in single-line fields
            load_events([event(1, title="a\nb")])


class FileCase(EventsCase):
    def write_escaped(self, field, bad="\ud800", rows=None):
        rows = rows if rows is not None else [event(1), event(2, **{field: f"Synthetic {bad} marker"})]
        data = json.dumps(rows, ensure_ascii=True).encode("ascii")
        self.assertIn(b"\\u" + format(ord(bad), "04x").encode(), data.lower())
        self.put(data)
        return data

    def snapshot(self):
        return {p.relative_to(self.root).as_posix(): (p.read_bytes() if p.is_file() else None, p.lstat().st_mode, p.lstat().st_mtime_ns)
                for p in sorted(self.root.rglob("*"))}

    def cli(self, *flags):
        return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "content-check", *flags,
                               "--directory", str(self.root)],
                              cwd=REPO_ROOT, capture_output=True, text=True, encoding="utf-8", timeout=120)


class ReaderCheckerTests(FileCase):
    def test_ascii_json_escaped_surrogates_refuse_in_reader_and_checker(self):
        for field in FIELDS:
            for bad in ("\ud800", "\udfff"):
                data = self.write_escaped(field, bad)
                line = f"events.json[1].{field}: {MESSAGE}"
                with self.assertRaises(ContentError) as caught:
                    read_events(self.root)
                self.assertEqual(str(caught.exception), line)
                problems = []
                self.assertIsNone(read_events(self.root, problems))
                self.assertEqual(problems, [line])
                self.assertIn(line, content_problems(self.root))
                self.assertEqual(self.path.read_bytes(), data)  # no repair or rewrite
        result = self.cli("--json")
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertEqual(result.stderr, "")
        records = json.loads(result.stdout)["problems"]
        self.assertIn({"file": "events.json[1].accessibility_note", "message": MESSAGE}, records)
        self.assertNotIn("Unicode", result.stdout.replace(MESSAGE, ""))
        self.assertNotIn("codec", result.stdout)
        self.assertNotIn("surrogate", result.stdout)
        self.assertNotIn("Synthetic", result.stdout)


class BuildTests(FileCase):
    def test_build_refuses_before_render_and_preserves_existing_outputs(self):
        self.put([event(1), event(2)])
        self.build()
        (self.root / "kit").mkdir(exist_ok=True)
        (self.root / "kit" / "private.txt").write_bytes(b"private kit bytes")
        for field in FIELDS:
            self.write_escaped(field)
            before = self.snapshot()
            with self.assertRaises(ContentError) as caught:
                self.build()
            self.assertEqual(str(caught.exception), f"events.json[1].{field}: {MESSAGE}")
            self.assertEqual(self.snapshot(), before)  # bytes, modes, mtimes and membership; no temp output

    def test_refusal_without_a_public_tree_creates_no_output(self):
        for field in FIELDS:
            self.write_escaped(field)
            before = self.snapshot()
            with self.assertRaises(ContentError):
                self.build()
            self.assertFalse(self.public.exists())
            self.assertEqual(self.snapshot(), before)

    def test_unicode_scalars_and_json_pairs_keep_exact_valid_output(self):
        title = "Caf\u00e9 \u81ea\u884c\u8f66 e\u0301 \U0001F6B2"
        rows = [event(1, title=title, description_md="Line **one**\n\tTabbed \u00e9 \U0001F6B2"), event(2)]
        self.put(rows)
        page = self.build()["events.html"].decode("utf-8")
        self.assertIn(title, page)
        self.assertIn("\U0001F6B2", page)
        paired = json.dumps(rows, ensure_ascii=True).encode("ascii")
        self.assertIn(b"\\ud83d\\udeb2", paired)  # a real paired escape, decoded to the same scalar
        self.put(paired)
        self.assertEqual(self.build()["events.html"].decode("utf-8"), page)

    def test_existing_schema_and_consumer_contracts_remain_unchanged(self):
        self.put([event(1), event(2, venue=None, online=True)])
        files = self.build()
        self.assertEqual(self.build(), files)
        (self.content / "events.json").unlink()
        self.assertIsNone(read_events(self.root))
        self.put([])
        self.assertEqual(read_events(self.root), [])
        self.assertEqual(load_events([event(1, title="<b>")], []), None)
        problems = []
        load_events([event(1, title="<b>", id="Bad Id")], problems)
        self.assertTrue(all(MESSAGE not in p for p in problems), problems)
        self.assertEqual(len(problems), 2)

    def test_consumer_refuses_malformed_event_text_before_private_output(self):
        self.write_escaped("description_md")
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, r"events\.json\[1\]\.description_md: " + MESSAGE):
            build_event(self.root, "forum-1", TODAY)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.root / "kit").exists())


if __name__ == "__main__":
    unittest.main()
