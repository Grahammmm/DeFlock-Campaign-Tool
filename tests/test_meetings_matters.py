"""DF2022-12: meetings --matters (Legistar Matters with ALPR keywords). Recorded synthetic JSON only; no network."""
import contextlib
import io
import json
import shutil
import sys
import tempfile
import unittest
import urllib.parse
from datetime import date
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import cli  # noqa: E402
from campaign_tool.meetings import LegistarClient, MeetingsError, fixture_opener, relevant_matters  # noqa: E402

FIXTURE = REPO_ROOT / "tests" / "fixtures" / "meetings" / "legistar-matters.json"
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
SINCE = date(2026, 1, 1)


def recorded():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def run(mapping, since=SINCE, urls=None):
    inner = fixture_opener(mapping)

    def opener(url):
        if urls is not None:
            urls.append(url)
        return inner(url)
    return relevant_matters(LegistarClient("examplecity", opener=opener), since=since)


def with_matter(**changes):
    mapping = recorded()
    mapping["/v1/examplecity/matters"][0].update(changes)
    return mapping


class Import(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch("urllib.request.urlopen", side_effect=AssertionError("network")))

    def test_two_hits_sorted_unrelated_absent(self):
        urls = []
        found = run(recorded(), urls=urls)
        self.assertEqual([m["id"] for m in found], ["legistar-examplecity-matter-502", "legistar-examplecity-matter-501"])
        first, second = found
        self.assertEqual(first["matched_terms"], ["surveillance"])
        self.assertEqual(second["matched_terms"], ["flock", "license plate"])
        self.assertEqual((second["file"], second["type"], second["status"], second["body"]),
                         ("26-0501", "Contract", "Passed", "Example City Council"))
        self.assertEqual((second["intro_date"], second["agenda_date"], second["passed_date"]),
                         ("2026-03-10", "2026-04-14", "2026-04-14"))
        self.assertEqual(second["url"], "https://examplecity.legistar.com/LegislationDetail.aspx?ID=501&GUID=AAAAAAAA-1111-2222-3333-444444444444")
        self.assertEqual(first["agenda_date"], None)

    def test_request_url_filter_and_top(self):
        urls = []
        run(recorded(), urls=urls)
        self.assertEqual(len(urls), 1)
        parts = urllib.parse.urlsplit(urls[0])
        self.assertEqual(parts.path, "/v1/examplecity/matters")
        query = urllib.parse.parse_qs(parts.query)
        self.assertEqual(query["$filter"], ["MatterLastModifiedUtc ge datetime'2026-01-01'"])
        self.assertEqual(query["$orderby"], ["MatterLastModifiedUtc desc"])
        self.assertLessEqual(int(query["$top"][0]), 100)
        urls.clear()
        LegistarClient("examplecity", opener=lambda u: (urls.append(u), b"[]")[1]).matters(top=5000)
        self.assertEqual(urllib.parse.parse_qs(urllib.parse.urlsplit(urls[0]).query)["$top"], ["100"])

    def test_guid_and_id_validation(self):
        for guid in (None, "", "not-a-guid", 12345, "AAAAAAAA-1111-2222-3333-4444444444&x=1"):
            self.assertIsNone(run(with_matter(MatterGuid=guid))[1]["url"], guid)
        self.assertEqual(len(run(with_matter(MatterId="501"))), 1)  # string id skipped
        self.assertEqual(len(run(with_matter(MatterId=True))), 1)

    def test_text_cleaning_and_dates(self):
        found = run(with_matter(MatterTitle="<script>alert(1)</script> ALPR " + "x" * 1000))[1]
        self.assertNotIn("<", found["title"])
        self.assertNotIn(">", found["title"])
        self.assertLessEqual(len(found["title"]), 300)
        for bad in (None, "garbage", 5, "2026-13-45T00:00:00", ""):
            found = run(with_matter(MatterIntroDate=bad, MatterAgendaDate=bad, MatterPassedDate=bad))
            self.assertEqual(len(found), 2)
            old = next(m for m in found if m["file"] == "26-0501")
            self.assertEqual((old["intro_date"], old["agenda_date"], old["passed_date"]), (None, None, None), bad)

    def test_keyword_in_file_number_only(self):
        mapping = recorded()
        mapping["/v1/examplecity/matters"][2]["MatterFile"] = "ALPR-26-1"
        self.assertEqual(len(run(mapping)), 3)


class Cli(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="synthetic-matters-")
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.root = self.tmp / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))

    def main(self, fixture=None, *extra, online=False):
        argv = ["campaign_tool", "meetings", "--directory", str(self.root), "--client", "examplecity", "--since", "2026-01-01"]
        if fixture is not None:
            path = self.tmp / "rec.json"
            path.write_text(json.dumps(fixture))
            argv += ["--from-json", str(path)]
        argv += list(extra)
        out, err, code = io.StringIO(), io.StringIO(), 0
        with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                cli.main()
            except SystemExit as exc:
                code = exc.code
            except ValueError as exc:  # the existing refusal for no source
                code, err = 1, io.StringIO(str(exc))
        return code, out.getvalue(), err.getvalue()

    def test_write(self):
        code, out, err = self.main(recorded(), "--matters")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["matched_matters"], 2)
        doc = json.loads((self.root / "kit" / "matters.json").read_text())
        self.assertEqual((doc["schema_version"], doc["source"], doc["client"], doc["since"]), (1, "legistar", "examplecity", "2026-01-01"))
        self.assertEqual(len(doc["matters"]), 2)
        self.assertIn("Verify on the official Legistar page", doc["note"])
        self.assertFalse((self.root / "kit" / "meetings.json").exists())

    def test_bad_responses_write_nothing(self):
        for bad in ({"/v1/examplecity/matters": {"unexpected": 1}}, {}):
            code, _, err = self.main(bad, "--matters")
            self.assertEqual(code, 1)
            self.assertIn("Stopped:", err)
            self.assertFalse((self.root / "kit" / "matters.json").exists())
        with mock.patch("campaign_tool.meetings.fixture_opener", return_value=lambda u: b"<html>"):
            code, _, err = self.main(recorded(), "--matters")
        self.assertEqual(code, 1)
        self.assertIn("Stopped:", err)
        self.assertFalse((self.root / "kit" / "matters.json").exists())

    def test_other_platform_refused(self):
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("network")):
            code, _, err = self.main(recorded(), "--matters", "--platform", "civicclerk")
        self.assertEqual(code, 1)
        self.assertIn("Stopped: --matters is Legistar only", err)
        self.assertFalse((self.root / "kit" / "matters.json").exists())

    def test_no_source_refused(self):
        code, _, err = self.main(None, "--matters")
        self.assertNotEqual(code, 0)
        self.assertIn("meetings needs --online", err)
        self.assertFalse((self.root / "kit" / "matters.json").exists())


if __name__ == "__main__":
    unittest.main()
