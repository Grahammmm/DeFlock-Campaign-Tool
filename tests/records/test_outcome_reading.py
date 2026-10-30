"""Synthetic private packet reading contract; no live project access."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from campaign_tool.records.outcomes import reading, register
from tests.records.test_outcome_comparison import report
from tests.records.test_outcome_register import data


class ReadingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "run-outputs" / "synthetic-run"
        self.directory.mkdir(parents=True)
        self.facts = {"schema": "records-agency-factsheets-v1", "run_id": self.directory.name,
                      "agencies": [{"agency_id": "a1", "agency": "Invented Agency", "fields": {
                          "vendor": {"status": "NOT FOUND", "statements": []}}}]}
        self.comparison = report()
        self.register = register.build_register(data(), as_of="2030-01-12")
        self.save()

    def save(self):
        for name, value in (("agency-factsheets.json", self.facts),
                            ("comparison.json", self.comparison),
                            ("CURRENT-REQUEST-REGISTER.json", self.register)):
            (self.directory / name).write_text(json.dumps(value), encoding="utf-8")

    def test_real_reports_deterministic_and_identified(self):
        text = reading.render(self.directory)
        self.assertEqual(text, reading.render(self.directory))
        for value in ("NOT FOUND", self.comparison["disclaimer"], "Invented Agency", "Fable Bay Police"):
            self.assertIn(value, text)
        self.assertIn(self.directory.name, text)
        for path in self.directory.iterdir():
            self.assertIn(hashlib.sha256(path.read_bytes()).hexdigest()[:12], text)
        self.assertEqual(text.count("PRIVATE — REVIEW REQUIRED — NOT FOR PUBLICATION"), 2)

    def test_escaping_and_accessible_structure(self):
        from html.parser import HTMLParser
        import html
        attack = '<script>\"><img onerror=alert(1)>javascript:&amp;`'
        self.facts["agencies"][0]["agency"] = attack
        self.facts["agencies"][0]["fields"]["vendor"] = {
            "status": "SOURCE STATEMENT — REVIEW REQUIRED", "statements": [{"quote": attack,
            "citation": {"document_sha256": "a" * 64, "page": 7}}]}
        self.comparison["rows"][0]["citation"]["url"] = attack
        self.comparison["rows"][0]["evidence"] = [{"source_id": attack, "locator": "page 9"}]
        self.register["requests"][0]["asked"] = attack
        self.save()
        text = reading.render(self.directory)
        self.assertGreaterEqual(text.count(html.escape(attack, quote=True)), 5)
        self.assertIn("aaaaaaaaaaaa</code>, page 7", text)
        self.assertIn("page 9", text)
        class Page(HTMLParser):
            def __init__(self):
                super().__init__()
                self.elements = []
            def handle_starttag(self, tag, attrs):
                self.elements.append((tag, dict(attrs)))
        page = Page()
        page.feed(text)
        allowed = set("html head meta title style body header p a h1 nav main section h2 h3 dl dt dd q code ul li table tbody tr th td footer".split())
        self.assertTrue(all(tag in allowed for tag, _ in page.elements))
        self.assertEqual(sum(tag == "h1" for tag, _ in page.elements), 1)
        ids = {attrs["id"] for _, attrs in page.elements if "id" in attrs}
        for tag, attrs in page.elements:
            self.assertFalse(any(key.startswith("on") or key == "src" for key in attrs))
            if "href" in attrs:
                self.assertTrue(attrs["href"].startswith("#"))
                self.assertIn(attrs["href"][1:], ids)
            if tag == "th":
                self.assertIn("scope", attrs)
        self.assertIn(("html", {"lang": "en"}), page.elements)
        self.assertTrue(any(t == "meta" and a.get("name") == "viewport" for t, a in page.elements))
        for literal in ('href="#content">Skip to content', "min-height:44px", ":focus-visible", "@media", '<q>'):
            self.assertIn(literal, text)

    def test_missing_and_invalid_sections_are_isolated(self):
        (self.directory / "CURRENT-REQUEST-REGISTER.json").unlink()
        text = reading.render(self.directory)
        section = text.split('<section id="register">')[1].split('</section>')[0]
        self.assertIn("Not produced in this run.", section)
        self.assertNotIn("<table", section)
        for raw in (b'{secret', b'{"schema":"wrong"}',
                    b'{"schema":"records-agency-comparison-v1","rows":[],"rows":[]}',
                    b'{"schema":"records-agency-comparison-v1","rows":[null]}',
                    b'{"schema":"records-agency-comparison-v1","rows":"secret"}',
                    b'{"schema":"records-agency-comparison-v1","rows":[],"bad":NaN}',
                    b' ' * (8 * 1024 * 1024 + 1)):
            with self.subTest(raw=raw[:50]):
                (self.directory / "comparison.json").write_bytes(raw)
                text = reading.render(self.directory)
                self.assertIn("Unreadable (reading_", text)
                self.assertNotIn("secret", text)
                self.assertIn("Invented Agency", text)
                self.assertIn("NOT FOUND", text)
        self.save()
        (self.directory / "comparison.json").write_bytes(b'{')
        self.assertIn("Fable Bay Police", reading.render(self.directory))

    def test_cli_atomic_private_output_and_readonly_render(self):
        import stat
        before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.directory.iterdir()}
        reading.render(self.directory)
        self.assertEqual(set(before), {p.name for p in self.directory.iterdir()})
        for _ in range(2):
            self.assertEqual(reading.main(["--run-dir", str(self.directory)]), 0)
            output = self.directory / "reading.html"
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
            self.assertEqual(output.read_text(), reading.render(self.directory))
        self.assertEqual(set(before) | {"reading.html"}, {p.name for p in self.directory.iterdir()})
        self.assertEqual(before, {p.name: (p.read_bytes(), p.stat().st_mtime_ns)
                                 for p in self.directory.iterdir() if p.name != "reading.html"})

    def test_path_boundary_codes_and_cli_errors(self):
        import contextlib
        import io
        alias = Path(self.temp.name) / "alias"
        alias.symlink_to(self.directory, target_is_directory=True)
        ancestor = Path(self.temp.name) / "ancestor"
        ancestor.symlink_to(self.directory.parent, target_is_directory=True)
        wrong = Path(self.temp.name) / "other" / "valid"
        wrong.mkdir(parents=True)
        invalid = self.directory.parent / ".hidden"
        invalid.mkdir()
        for path, code in ((alias, "reading_unsafe_path"), (ancestor / self.directory.name, "reading_unsafe_path"),
                           (wrong, "reading_not_run_outputs"), (invalid, "reading_run_id_invalid"),
                           (self.directory / ".." / self.directory.name, "reading_unsafe_path")):
            with self.subTest(path=path):
                with self.assertRaisesRegex(OSError, code):
                    reading.render(path)
                out = io.StringIO()
                with contextlib.redirect_stderr(out):
                    self.assertEqual(reading.main(["--run-dir", str(path)]), 1)
                self.assertEqual(out.getvalue().strip(), code)

    def test_input_and_output_symlinks_and_stale_temporary_are_not_followed(self):
        import contextlib
        import io
        victim = Path(self.temp.name) / "victim"
        victim.write_text("private unrelated text")
        source = self.directory / "comparison.json"
        source.unlink()
        source.symlink_to(victim)
        self.assertIn("Unreadable (reading_unsafe_path)", reading.render(self.directory))
        self.assertNotIn("private unrelated text", reading.render(self.directory))
        for name in ("reading.html", ".reading.html.tmp"):
            path = self.directory / name
            path.symlink_to(victim)
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(reading.main(["--run-dir", str(self.directory)]), 1)
            self.assertTrue(path.is_symlink())
            self.assertEqual(victim.read_text(), "private unrelated text")
            path.unlink()
        temporary = self.directory / ".reading.html.tmp"
        temporary.write_text("retained")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(reading.main(["--run-dir", str(self.directory)]), 1)
        self.assertEqual(temporary.read_text(), "retained")

    def test_caps_bad_unicode_special_files_and_markdown_are_isolated(self):
        from unittest.mock import patch
        import os
        path = self.directory / "comparison.json"
        size = path.stat().st_size
        with patch.object(reading, "MAX_BYTES", size):
            self.assertNotIn("Unreadable", reading.render(self.directory))
        with patch.object(reading, "MAX_BYTES", size - 1):
            self.assertIn("Unreadable (reading_input_bound)", reading.render(self.directory))
        for raw in (b'\xff', b'{"schema":"records-agency-comparison-v1","rows":[],"bad":"\\ud800"}',
                    b'[' * 1100):
            path.write_bytes(raw)
            self.assertIn("Unreadable (reading_json_invalid)", reading.render(self.directory))
        path.unlink()
        os.mkfifo(path)
        self.assertIn("Unreadable (reading_unsafe_path)", reading.render(self.directory))
        for name in ("comparison.md", "agency-factsheets.md", "CURRENT-REQUEST-REGISTER.md"):
            (self.directory / name).symlink_to(path)
        self.assertIn("Invented Agency", reading.render(self.directory))

    def test_failed_replace_preserves_existing_output_and_cleans_owned_temp(self):
        import contextlib
        import io
        from unittest.mock import patch
        output = self.directory / "reading.html"
        output.write_text("previous private page")
        with patch.object(reading.os, "replace", side_effect=OSError("sensitive path")):
            errors = io.StringIO()
            with contextlib.redirect_stderr(errors):
                self.assertEqual(reading.main(["--run-dir", str(self.directory)]), 1)
            self.assertEqual(errors.getvalue(), "reading_write_failed\n")
        self.assertEqual(output.read_text(), "previous private page")
        self.assertFalse((self.directory / ".reading.html.tmp").exists())

    def test_documented_module_command(self):
        import subprocess
        result = subprocess.run(["python3", "-B", "-m", "campaign_tool.records.outcomes.reading",
                                 "--run-dir", str(self.directory)], capture_output=True, text=True)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        self.assertEqual((self.directory / "reading.html").read_text(), reading.render(self.directory))


if __name__ == "__main__":
    unittest.main()
