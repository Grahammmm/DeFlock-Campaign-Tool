"""RF1740-12: agency-card text refuses control, zero-width and direction-override characters per field."""
import copy
import json
import unittest
from pathlib import Path

from campaign_tool.agency_cards import problems, render, text

CONFIG = json.loads((Path(__file__).resolve().parents[1] / "examples/fictional-campaign/agency-cards.json").read_text())
MARK = "SECRETVALUE"


def config(**profile):
    value = copy.deepcopy(CONFIG)
    value["county_profile"].update(profile)
    return value


class InvisibleCharacterTests(unittest.TestCase):
    def assert_refused(self, value, expected):
        found = problems(value)
        self.assertEqual(found, expected)
        self.assertNotIn(MARK, "\n".join(found))
        with self.assertRaises(ValueError) as caught:
            render(value)
        self.assertEqual(str(caught.exception), "; ".join(expected))
        self.assertNotIn(MARK, str(caught.exception))

    def test_plain_text_with_accents_emoji_quotes_newline_tab_accepted(self):
        plain = "Caf\u00e9 \u00d1and\u00fa \U0001F4F7 \u201cquoted\u201d \u2018x\u2019 \u2014 line\nnext\tcol"
        value = config(name=plain, summary=plain, facts=[plain], flags=[plain])
        value["source_link_label"] = plain
        self.assertTrue(text(plain))
        self.assertEqual(problems(value), [])
        self.assertIn("\U0001F4F7", render(value))

    def test_invisible_and_direction_characters_named_per_field(self):
        cases = [
            ("summary", f"a{MARK}\u200bb", "county_profile.summary: not plain text"),
            ("name", f"{MARK}\u202egnp.exe", "county_profile.name: not plain text"),
            ("facts", ["ok", f"\u2066{MARK}\u2069"], "county_profile.facts: item 1 not plain text"),
        ]
        for field, bad, expected in cases:
            with self.subTest(field=field):
                self.assert_refused(config(**{field: bad}), [expected])
        value = copy.deepcopy(CONFIG)
        value["source_link_label"] = f"\ufeff{MARK}"
        self.assert_refused(value, ["source_link_label: blank or not plain text"])

    def test_every_profile_field_checked(self):
        for field in sorted(CONFIG["county_profile"]):
            bad = [f"{MARK}\u200d"] if field in ("facts", "flags") else f"{MARK}\u200d"
            suffix = "item 0 not plain text" if field in ("facts", "flags") else "not plain text"
            with self.subTest(field=field):
                self.assert_refused(config(**{field: bad}), [f"county_profile.{field}: {suffix}"])

    def test_control_and_separator_characters_refused(self):
        for char in ("\x00", "\x1b", "\x7f", "\x85", "\r", "\u2028", "\u2029", "\u200e", "\u200f",
                     "\u202a", "\u202d", "\u2067", "\u2068", "\u00ad"):
            with self.subTest(char=hex(ord(char))):
                self.assertFalse(text(f"{MARK}{char}"))
                self.assertFalse(text(char))
                self.assert_refused(config(status=f"{MARK}{char}x"), ["county_profile.status: not plain text"])

    def test_wording_bypass_still_refused(self):
        # a refused char anywhere (start, end, alone between words, after a long prefix) is caught
        for bad in ("\u200b", f"{MARK} \u200b", "x" * 19990 + "\u202e", "\u2069\n\t", "\t\u202c"):
            with self.subTest(length=len(bad)):
                self.assertFalse(text(bad))
        self.assertFalse(text("\u202e" * 20001))
        self.assertFalse(text("<\u200b>"))

    def test_several_bad_fields_all_listed_sorted(self):
        value = config(vendor=f"{MARK}\x00", name=f"{MARK}\u202e", flags=["ok", "\u2066", "<b>"],
                       open=f"{MARK}\u2028")
        value["source_link_label"] = "\ufeff"
        expected = sorted([
            "county_profile.flags: item 1 not plain text",
            "county_profile.flags: item 2 not plain text",
            "county_profile.name: not plain text",
            "county_profile.open: not plain text",
            "county_profile.vendor: not plain text",
            "source_link_label: blank or not plain text",
        ])
        self.assert_refused(value, expected)

    def test_render_refuses_before_rendering_anything(self):
        value = config(summary=f"{MARK}\u200b")
        with self.assertRaises(ValueError) as caught:
            render(value)
        self.assertEqual(str(caught.exception), "county_profile.summary: not plain text")
        self.assertNotIn("setCityText", str(caught.exception))

    def test_value_not_echoed_by_check_command(self):
        import io
        import os
        import tempfile
        from contextlib import redirect_stderr, redirect_stdout
        from campaign_tool.agency_cards import check
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "cards.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(config(name=f"{MARK}\u202e"), handle, ensure_ascii=False)
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = check(path)
        self.assertEqual(code, 1)
        self.assertEqual(out.getvalue(), "county_profile.name: not plain text\nproblems=1\n")
        self.assertNotIn(MARK, out.getvalue() + err.getvalue())


if __name__ == "__main__":
    unittest.main()
