"""BF-df-agency-cards-public: confirmed / not known / what we asked, from organizer content only (synthetic)."""
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from campaign_tool.content import ContentError, load_content
from campaign_tool.content_check import content_problems
from campaign_tool.site import Site, build_site

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
TODAY = date(2026, 11, 1)
BANNED = re.compile(r"ignored|late|overdue|violat|illegal|stonewall", re.I)
BLOCK = re.compile(r'<section class="agency-block">.*?</section>')
THREE = [{"topic": "ALPR retention policy", "asked_on": "2026-08-01", "state": "answered", "updated_on": "2026-09-01"},
         {"topic": "Data-sharing list", "asked_on": "2026-10-01", "state": "awaiting_response"},
         {"topic": "Camera count", "asked_on": "2026-09-15", "state": "declined_in_writing",
          "updated_on": "2026-10-20", "note": "Cited an exemption."}]


class AgencyAskedTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root)
        self.path = self.root / "content" / "agencies.json"
        self.agencies = json.loads(self.path.read_text())

    def tearDown(self):
        self._tmp.cleanup()

    def save(self, index=None, **changes):
        if index is not None:
            self.agencies[index].update(changes)
        self.path.write_text(json.dumps(self.agencies))

    def build(self, today=TODAY):
        build_site(self.root, today=today)
        public = self.root / "public"
        return {p.relative_to(public).as_posix(): p.read_bytes() for p in public.rglob("*") if p.is_file()}

    def article(self, files, name):
        page = files["agencies.html"].decode()
        start = page.index("<h3>" + name)
        return page[start:page.index("</article>", start)]

    def blocks(self, files, name):
        return BLOCK.findall(self.article(files, name))

    def error(self, today=TODAY):
        with self.assertRaises(ContentError) as caught:
            load_content(self.root, today)
        return str(caught.exception)

    def cli(self):
        return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "content-check", "--directory", str(self.root)],
                              cwd=REPO_ROOT, capture_output=True, text=True, timeout=120)

    def test_three_blocks_in_order_newest_first_with_exact_sentences(self):
        self.agencies[1]["profile"]["facts"] = ["Posted a policy (synthetic)"]
        self.save(1, asked=THREE)
        confirmed, unknown, asked = self.blocks(self.build(), "Cedar Police Department")
        self.assertEqual(confirmed, '<section class="agency-block"><h4>What is confirmed</h4><ul><li>Posted a policy '
                                    '(synthetic)</li><li>1 published finding(s) with sources</li></ul></section>')
        self.assertEqual(unknown, '<section class="agency-block"><h4>What is not known</h4><ul><li>Whether any agreement '
                                  'governs data sharing is an open information gap.</li><li>Fictional example</li></ul></section>')
        self.assertEqual(re.findall(r"<li>(.*?)</li>", asked), [
            "Data-sharing list — asked 2026-10-01. No written response recorded as of 2026-11-01.",
            "Camera count — asked 2026-09-15. Declined in writing on 2026-10-20. Cited an exemption.",
            "ALPR retention policy — asked 2026-08-01. Answered 2026-09-01."])
        page = self.article(self.build(), "Cedar Police Department")
        self.assertLess(page.index("A synthetic policy document"), page.index("What is confirmed"))
        self.assertLess(page.index("What we asked"), page.index('href="/findings/'))

    def test_every_state_sentence_exact_dated_and_undated(self):
        dated = {"awaiting_response": "No written response recorded as of 2026-10-05.",
                 "acknowledged": "Acknowledged 2026-10-05; no records yet.", "partly_answered": "Partly answered 2026-10-05.",
                 "answered": "Answered 2026-10-05.", "declined_in_writing": "Declined in writing on 2026-10-05.",
                 "withdrawn": "We withdrew this request 2026-10-05."}
        undated = {"awaiting_response": "No written response recorded as of 2026-11-01.",
                   "acknowledged": "Acknowledged; no records yet.", "partly_answered": "Partly answered.",
                   "answered": "Answered.", "declined_in_writing": "Declined in writing.", "withdrawn": "We withdrew this request."}
        for expected, extra in ((dated, {"updated_on": "2026-10-05"}), (undated, {})):
            for state, sentence in expected.items():
                with self.subTest(state=state, dated=bool(extra)):
                    self.save(1, asked=[{"topic": "T", "asked_on": "2026-10-01", "state": state, **extra}])
                    asked = self.blocks(self.build(), "Cedar Police Department")[2]
                    self.assertIn(f"<li>T — asked 2026-10-01. {sentence}</li>", asked)
                    self.assertIsNone(BANNED.search(sentence))
                    self.assertNotIn("refused", asked.lower())

    def test_empty_agency_says_so_in_each_block(self):
        confirmed, unknown, asked = self.blocks(self.build(), "Cedar City Council")
        self.assertIn("<p>Nothing is confirmed by a published finding yet.</p>", confirmed)
        self.assertIn("<p>Nothing listed yet.</p>", unknown)
        self.assertIn("<p>No request is listed for this agency yet.</p>", asked)

    def test_awaiting_uses_injected_build_date_else_update(self):
        self.save(1, asked=[{"topic": "T", "asked_on": "2026-10-01", "state": "awaiting_response"}])
        for today in (date(2026, 11, 1), date(2026, 12, 24)):
            with self.subTest(today=today):
                self.assertIn(f"as of {today.isoformat()}.", self.blocks(self.build(today), "Cedar Police")[2])
        self.save(1, asked=[{"topic": "T", "asked_on": "2026-10-01", "state": "awaiting_response", "updated_on": "2026-10-09"}])
        asked = self.blocks(self.build(date(2026, 12, 24)), "Cedar Police")[2]
        self.assertIn("as of 2026-10-09.", asked)
        self.assertNotIn("2026-12-24", asked)

    def test_each_invalid_field_refused_naming_the_dotted_field(self):
        base = {"topic": "T", "asked_on": "2026-10-01", "state": "answered"}
        cases = [({**base, "state": "ignored"}, "[0].state"),
                 ({**base, "state": "Answered"}, "[0].state"),
                 ({**base, "state": "refused"}, "[0].state"),
                 ({**base, "asked_on": "2026-11-02"}, "[0].asked_on"),
                 ({**base, "updated_on": "2026-11-02"}, "[0].updated_on"),
                 ({**base, "asked_on": "2026-02-30"}, "[0].asked_on"),
                 ({**base, "updated_on": "2026-09-30"}, "[0].updated_on"),
                 ({**base, "topic": "x" * 121}, "[0].topic"),
                 ({**base, "topic": ""}, "[0].topic"),
                 ({**base, "topic": "<script>alert(1)</script>"}, "[0].topic"),
                 ({**base, "note": "n" * 201}, "[0].note"),
                 ({**base, "source": "kit/outbox"}, "[0]: unknown field 'source'"),
                 ({"topic": "T", "asked_on": "2026-10-01"}, "[0].state")]
        for item, field in cases:
            with self.subTest(field=field, item=item):
                self.save(1, asked=[item])
                self.assertIn(f"content/agencies.json[1].asked{field}", self.error())
        for value in ({"topic": "T"}, "awaiting", [dict(base)] * 21):
            with self.subTest(asked=str(value)[:30]):
                self.save(1, asked=value)
                self.assertIn("content/agencies.json[1].asked: must be a list of at most 20 items", self.error())
        self.save(1, asked=["not an object"])
        self.assertIn("content/agencies.json[1].asked[0]: must be an object", self.error())

    def test_limits_and_today_are_accepted(self):
        items = [{"topic": "x" * 120, "asked_on": "2026-11-01", "state": "acknowledged", "updated_on": "2026-11-01",
                  "note": "n" * 200}] * 20
        self.save(1, asked=items)
        self.assertEqual(len(self.blocks(self.build(), "Cedar Police")[2].split("<li>")), 21)

    def test_content_check_lists_every_problem_once_each_across_agencies(self):
        base = {"topic": "T", "asked_on": "2026-10-01", "state": "answered"}
        self.save(0, asked=[{**base, "state": "ignored"}, {**base, "asked_on": "2099-01-01"},
                            {**base, "updated_on": "2026-09-01"}, {**base, "topic": "x" * 121},
                            {**base, "note": "ok", "extra": 1}, {**base, "state": "ignored"}])
        self.save(1, asked="not a list")
        self.save(2, asked=[dict(base)] * 21)
        expected = ["content/agencies.json[0].asked[0].state", "content/agencies.json[0].asked[1].asked_on",
                    "content/agencies.json[0].asked[2].updated_on", "content/agencies.json[0].asked[3].topic",
                    "content/agencies.json[0].asked[4]: unknown field 'extra'", "content/agencies.json[0].asked[5].state",
                    "content/agencies.json[1].asked: must be a list", "content/agencies.json[2].asked: must be a list"]
        problems = content_problems(self.root)
        self.assertEqual(len(problems), len(expected), problems)
        for prefix in expected:
            self.assertEqual(sum(line.startswith(prefix) for line in problems), 1, (prefix, problems))
        result = self.cli()
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertEqual(result.stdout.splitlines()[0], f"content/: {len(expected)} problem(s)")
        with self.assertRaises(ContentError) as caught:  # build stays fail-closed on the first problem
            load_content(self.root)
        self.assertIn(str(caught.exception), problems)

    def test_unselected_agency_skipped_by_both_build_and_check(self):
        self.save(2, selected=False, asked="not a list")
        self.assertEqual(content_problems(self.root), [])
        self.assertNotIn("Cedar City Council", self.build()["agencies.html"].decode())

    def test_single_defect_parity_between_build_and_check(self):
        self.save(1, asked=[{"topic": "T", "asked_on": "2026-10-01", "state": "overdue"}])
        problems = content_problems(self.root)
        self.assertEqual(problems, [self.error(date.today())])

    def test_escaping_and_no_new_script_style_or_csp_change(self):
        before = self.build()
        self.save(1, asked=[{"topic": "Fees & \"quotes\" 'here'", "asked_on": "2026-10-01", "state": "answered",
                             "note": "A & B \"said\""}])
        after = self.build()
        asked = self.blocks(after, "Cedar Police")[2]
        self.assertIn("Fees &amp; &quot;quotes&quot; &#x27;here&#x27; — asked", asked)
        self.assertIn("A &amp; B &quot;said&quot;", asked)
        for page in (before, after):
            self.assertNotIn("style=", page["agencies.html"].decode())
        self.assertEqual(before["agencies.html"].count(b"<script"), after["agencies.html"].count(b"<script"))
        self.assertEqual(before["_headers"], after["_headers"])  # the CSP header text csp() writes
        self.assertIn(b"Content-Security-Policy: default-src 'none'", after["_headers"])

    def test_rest_of_page_unchanged_apart_from_blocks(self):
        with mock.patch.object(Site, "agency_blocks", return_value=""):
            without = self.build()["agencies.html"].decode()
        self.assertEqual(BLOCK.sub("", self.build()["agencies.html"].decode()), without)
        self.save(1, asked=THREE)
        # DF0137-12: the At a glance table's "Records items asked" cell for Cedar Police (2nd row) now shows the count.
        cell, row = '<td data-label="Records items asked">{}</td>', without.index('<a href="/agencies/cedar.html">')
        without = without[:row] + without[row:].replace(cell.format("0"), cell.format('<a href="#cedar-asked">3</a>'), 1)
        self.assertEqual(BLOCK.sub("", self.build()["agencies.html"].decode()), without)

    def test_data_and_feeds_byte_identical_field_not_exported(self):
        before = self.build()
        self.save(1, asked=THREE)
        after = self.build()
        exported = [name for name in before if name.startswith("data/") or name.endswith(".xml")]
        self.assertIn("data/findings.json", exported)
        self.assertIn("feed.xml", exported)
        for name in exported:
            if name == "data/datapackage.json":
                old, new = json.loads(before[name]), json.loads(after[name])
                for left, right in zip(old["resources"], new["resources"], strict=True):
                    if left["path"] in ("agencies.json", "agencies.csv"):
                        for key in ("bytes", "hash"):
                            self.assertNotEqual(left[key], right[key])
                            left.pop(key)
                            right.pop(key)
                self.assertEqual(old, new)  # all other descriptor metadata stays identical
                continue
            if name in ("data/agencies.json", "data/agencies.csv"):  # DF1422-12/DF2207-11: exported here, and only here
                continue
            self.assertEqual(before[name], after[name], name)
            self.assertNotIn(b"Data-sharing list", after[name])
        # Directory enumeration order varies by filesystem; only this exact inventory is permitted.
        self.assertCountEqual([n for n in after if b"Data-sharing list" in after[n]],
                         ["timeline.html", "agencies.html", "data/agencies.csv", "data/agencies.json"])


if __name__ == "__main__":
    unittest.main()
