"""RF1511-6: lone surrogate text is refused at the shared decisions-elsewhere loader (synthetic campaigns only)."""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import tests.test_site_elsewhere as base
from campaign_tool.content import ContentError, load_content
from campaign_tool.content_check import content_problems
from campaign_tool.content_elsewhere import load_elsewhere, read_elsewhere
from campaign_tool.meeting_elsewhere import load_cited_elsewhere
from campaign_tool.site import build_site

FIELDS = ("place", "body", "as_stated")
SURROGATES = ("\ud800", "\udbff", "\udc00", "\udfff")
DAY = base.BUILD_DAY


def refusal(field, index=0):
    return (f"elsewhere.json[{index}].{field}: must be trimmed plain single-line text of 1-200 characters "
            "without angle brackets or control characters")


class ScalarCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-scalars-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(base.EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.path = self.root / "content" / "elsewhere.json"
        patch = mock.patch("socket.socket", side_effect=AssertionError("network access attempted"))
        patch.start()
        self.addCleanup(patch.stop)

    def put(self, rows):
        self.path.write_bytes(json.dumps(rows).encode("utf-8"))  # ASCII escapes: valid UTF-8 bytes

    def cli(self, *args):
        return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", *args, "--directory", str(self.root)],
                              cwd=base.REPO_ROOT, capture_output=True, text=True, timeout=300)


class ScalarTests(ScalarCase):
    def test_elsewhere_text_rejects_all_surrogate_boundaries(self):
        for field in FIELDS:
            for sur in SURROGATES:
                for text in (sur, f"Alpha {sur} beta", "a" * 199 + sur, sur + "a" * 199):
                    with self.subTest(field=field, code=hex(ord(sur)), length=len(text)):
                        with self.assertRaises(ContentError) as caught:
                            load_elsewhere([base.entry(1, **{field: text})], today=DAY)
                        self.assertEqual(str(caught.exception), refusal(field))
                        self.assertNotIn(sur, str(caught.exception))

    def test_escaped_and_direct_surrogates_have_matching_refusals(self):
        escaped = json.loads('[{"place": "Bad \\ud800 Town"}]')[0]["place"]
        self.assertEqual(escaped, "Bad \ud800 Town")
        messages = []
        for place in (escaped, "Bad \ud800 Town", "Bad \ud83d\ude00 Town"):  # last: high+low code points, not a scalar
            with self.assertRaises(ContentError) as caught:
                load_elsewhere([base.entry(1, place=place)], today=DAY)
            messages.append(str(caught.exception))
        self.assertEqual(set(messages), {refusal("place")})

    def test_collector_retains_seed_and_reports_each_invalid_field(self):
        rows = [base.entry(1), base.entry(2, place="X\ud800", body="Y\udfff"), base.entry(3, as_stated="Z\udc00")]
        copy = json.loads(json.dumps(rows, ensure_ascii=True))
        problems = ["seed"]
        self.assertIsNone(load_elsewhere(rows, today=DAY, problems=problems))
        self.assertEqual(problems, ["seed", refusal("place", 1), refusal("body", 1), refusal("as_stated", 2)])
        self.assertEqual(json.dumps(rows), json.dumps(copy))  # input objects untouched

    def test_content_check_and_build_refuse_before_html_gate(self):
        self.put([base.entry(1)])
        build_site(self.root, today=DAY)
        public = self.root / "public"
        snapshot = {p.relative_to(public).as_posix(): (p.read_bytes(), p.stat().st_mode) for p in sorted(public.rglob("*")) if p.is_file()}
        for field in FIELDS:
            rows = [base.entry(1, **{field: "Bad \ud800 text"})]
            with self.subTest(field=field):
                self.put(rows)
                raw = self.path.read_bytes()
                with self.assertRaises(ContentError) as caught:
                    load_content(self.root, DAY)
                self.assertEqual(str(caught.exception), refusal(field))
                self.assertEqual(content_problems(self.root), [refusal(field)])
                with self.assertRaises(ContentError) as built:
                    build_site(self.root, today=DAY)
                self.assertEqual(str(built.exception), refusal(field))
                checked = self.cli("content-check")
                self.assertNotEqual(checked.returncode, 0)
                self.assertNotIn("Traceback", checked.stdout + checked.stderr)
                self.assertNotIn("Bad", checked.stdout + checked.stderr)
                after = {p.relative_to(public).as_posix(): (p.read_bytes(), p.stat().st_mode) for p in sorted(public.rglob("*")) if p.is_file()}
                self.assertTrue(after == snapshot, "last good public tree changed")
                self.assertEqual(list(self.root.glob(".public.*")), [])
                self.assertEqual(self.path.read_bytes(), raw)

    def test_valid_non_bmp_and_pair_escape_round_trip(self):
        pair = json.loads('"Fun \\ud83d\\ude00 City"')
        self.assertEqual(pair, "Fun \U0001F600 City")
        rows = [base.entry(1, place=pair, as_stated="Caf\u00e9 e\u0301 \U0001D11E"),
                base.entry(2, place="  padded  ", as_stated="b" * 200), base.entry(3, place="Fun \U0001F601 City")]
        self.put(rows)
        model = load_content(self.root, DAY)
        self.assertEqual([e["place"] for e in model["elsewhere"]], ["Fun \U0001F600 City", "padded", "Fun \U0001F601 City"])
        self.assertEqual(model["elsewhere"][0]["as_stated"], "Caf\u00e9 e\u0301 \U0001D11E")
        self.assertEqual(len(model["elsewhere"][1]["as_stated"]), 200)
        build_site(self.root, today=DAY)
        first = (self.root / "public" / "elsewhere.html").read_bytes()
        build_site(self.root, today=DAY)
        self.assertEqual((self.root / "public" / "elsewhere.html").read_bytes(), first)
        self.assertIn("\U0001F600".encode("utf-8"), first)

    def test_shared_elsewhere_consumers_keep_the_boundary(self):
        good, bad = [base.entry(1)], [base.entry(1, body="Council \ud800")]
        self.assertEqual(load_cited_elsewhere(good, "meeting.cited_elsewhere", today=DAY)[0]["place"], good[0]["place"])
        with self.assertRaises(ContentError) as caught:
            load_cited_elsewhere(bad, "meeting.cited_elsewhere", today=DAY)
        self.assertEqual(str(caught.exception), refusal("body").replace("elsewhere.json", "meeting.cited_elsewhere", 1))
        self.put(good)
        self.assertEqual(len(read_elsewhere(self.root, DAY)), 1)
        self.put(bad)
        problems = []
        self.assertIsNone(read_elsewhere(self.root, DAY, problems))
        self.assertEqual(problems, [refusal("body")])


if __name__ == "__main__":
    unittest.main()
