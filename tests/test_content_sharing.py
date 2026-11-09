"""DF1207-08: ``content agency-sharing`` imports an organizer-transcribed list of agencies the vendor transparency portal
names as receiving data into ``profile.sharing``; the agency page shows counts and the out-of-state and federal names
beside the two cited rules, never fetched and with no conclusion drawn. Offline, synthetic agencies and names only.
"""
import copy
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from campaign_tool import law
from campaign_tool.content import ContentError, load_agencies
from campaign_tool.content_check import content_problems
from campaign_tool.content_sharing import SharingImportError, import_sharing
from campaign_tool.legal_claims import legal_claims
from campaign_tool.site import build_site

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
TODAY = date(2026, 11, 1)  # fixed build and import date
AGENCY = "ca-cedar-police"
URL = "https://transparency.flocksafety.com/cedar-police"
CHECKED = "2026-10-31"
HEADING = "<h4>Who the vendor portal lists as receiving data</h4>"
RULE_ID = "ca-civ-1798.90.55-sharing-limits"
BULLETIN_ID = "ca-oag-2023-dle-06-out-of-state-sharing-guidance"
FORBIDDEN = ("compl", "violat", "unlawful", "illegal", "prohibited")
BASIC = ("name,state,kind\nCedar Hills PD,CA,local\nOak Valley PD,CA,local\nPine Bay Sheriff,CA,local\n"
         "Reno Test PD,NV,local\nFictional Federal Office,US,federal\n")


def refuse_network(*args, **kwargs):
    raise AssertionError("network access attempted")


class SharingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-sharing-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.root = self.tmp / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.public = self.root / "public"
        self.path = self.root / "content" / "agencies.json"
        self.base = json.loads(self.path.read_text())
        self.canonical(self.base)
        self.original = self.path.read_bytes()
        self.csv = self.tmp / "sharing.csv"

    def canonical(self, document):  # the format the import itself writes, so "the rest is byte-identical" is checkable
        self.path.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    def load(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def importing(self, text=BASIC, data=None, **kwargs):
        if data is None:
            self.csv.write_text(text, encoding="utf-8", newline="")
        else:
            self.csv.write_bytes(data)
        arguments = dict(agency_id=AGENCY, csv_path=self.csv, checked_on=CHECKED, source_url=URL, today=TODAY)
        arguments.update(kwargs)
        return import_sharing(self.root, **arguments)

    def refused(self, *fragments, **kwargs):
        """The import is refused with each fragment in its one message, and agencies.json is exactly as it was."""
        with self.assertRaises(SharingImportError) as caught:
            self.importing(**kwargs)
        for fragment in fragments:
            self.assertIn(fragment, str(caught.exception))
        self.assertEqual(self.path.read_bytes(), self.original)
        return str(caught.exception)

    def build(self):
        with mock.patch("urllib.request.urlopen", side_effect=refuse_network) as urlopen:
            build_site(self.root, today=TODAY)
        self.assertEqual(urlopen.call_count, 0)

    def snapshot(self):
        return {p.relative_to(self.public).as_posix(): p.read_bytes() for p in self.public.rglob("*") if p.is_file()}

    def read(self, name):
        return (self.public / name).read_text(encoding="utf-8")

    def section(self, page):
        return '<section class="agency-sharing"' + page.split('<section class="agency-sharing"', 1)[1].split("</section>", 1)[0] + "</section>"

    def package_without(self, *rule_ids):
        base = self.tmp / "jurisdictions"
        (base / "us-ca").mkdir(parents=True, exist_ok=True)
        package = json.loads((REPO_ROOT / "jurisdictions" / "us-ca" / "package.json").read_text())
        package["rules"] = [r for r in package["rules"] if r["rule_id"] not in rule_ids]
        for scope in package["request_scopes"]:  # load_package refuses a scope naming a rule the package lacks
            scope["rule_ids"] = [i for i in scope["rule_ids"] if i not in rule_ids]
        (base / "us-ca" / "package.json").write_text(json.dumps(package))
        return mock.patch.object(law, "JURISDICTIONS", base)

    # 1
    def test_counts_are_exact_and_only_out_of_state_and_federal_are_named(self):
        summary = self.importing()
        self.assertEqual(summary, {"agency_id": AGENCY, "recipients": 5, "checked_on": CHECKED, "source_url": URL})
        self.build()
        package = law.load_package("us-ca")
        citation = {r["rule_id"]: r["citation"] for r in package["rules"]}
        self.assertEqual(citation[RULE_ID], "Civ. Code § 1798.90.55(b)")
        for name in ("agencies/cedar.html", "agencies.html"):
            page = self.read(name)
            self.assertEqual(page.count('class="agency-sharing"'), 1, name)
            html = self.section(page)
            self.assertIn(HEADING, html)
            self.assertIn(f'<p>As listed on <a href="{URL}" rel="noopener noreferrer">the vendor transparency portal</a>, '
                          'transcribed by the organizer (checked 2026-10-31): 5 agencies.</p>', html)
            self.assertIn('<tbody><tr><td>CA</td><td>3</td></tr><tr><td>NV</td><td>1</td></tr><tr><td>US</td><td>1</td></tr></tbody>', html)
            self.assertIn('<tbody><tr><td>local</td><td>4</td></tr><tr><td>federal</td><td>1</td></tr></tbody>', html)
            self.assertIn("<p>Out of state and federal:</p><ul><li>Reno Test PD, NV (local)</li>"
                          "<li>Fictional Federal Office, US (federal)</li></ul>", html)
            for in_state in ("Cedar Hills PD", "Oak Valley PD", "Pine Bay Sheriff"):
                self.assertNotIn(in_state, html)
            self.assertIn(f"<p>Sharing rule: {citation[RULE_ID]}.</p><p>Attorney General bulletin: {citation[BULLETIN_ID]}.</p>"
                          "<p>Law package: draft, not legally reviewed.</p>", html)
        stored = self.load()[[a["agency_id"] for a in self.load()].index(AGENCY)]["profile"]["sharing"]
        self.assertEqual(stored["recipients"][0], {"name": "Cedar Hills PD", "state": "CA", "kind": "local"})
        self.assertEqual([(r["state"], r["name"]) for r in stored["recipients"]],
                         [("CA", "Cedar Hills PD"), ("CA", "Oak Valley PD"), ("CA", "Pine Bay Sheriff"), ("NV", "Reno Test PD"),
                          ("US", "Fictional Federal Office")])

    def test_a_federal_entry_with_the_home_state_is_still_listed_and_one_agency_is_singular(self):
        self.importing("name,state,kind\nFictional Field Office,CA,federal\n")
        self.build()
        html = self.section(self.read("agencies/cedar.html"))
        self.assertIn(": 1 agency.</p>", html)
        self.assertIn("<li>Fictional Field Office, CA (federal)</li>", html)

    # 2
    def test_header_must_be_exact_a_bom_is_accepted_and_the_column_count_is_checked(self):
        for header in ("Name,State,Kind", "name,state,kind,note", "name,state", "state,name,kind", "name, state, kind"):
            with self.subTest(header=header):
                self.refused("header must be exactly name,state,kind", text=f"{header}\nCedar Hills PD,CA,local\n")
        self.refused("header must be exactly", text="")
        self.refused("line 2: expected 3 columns, found 4", text="name,state,kind\nCedar Hills PD,CA,local,extra\n")
        self.refused("line 3: expected 3 columns, found 2", text="name,state,kind\nCedar Hills PD,CA,local\nOak Valley PD,CA\n")
        self.refused("line 3: blank line", text="name,state,kind\nCedar Hills PD,CA,local\n\nOak Valley PD,CA,local\n")
        self.refused("found none", text="name,state,kind\n")
        self.assertEqual(self.importing(data=b"\xef\xbb\xbf" + BASIC.encode())["recipients"], 5)

    def test_the_file_must_be_utf8_a_regular_file_and_not_a_symlink(self):
        self.refused("not a valid UTF-8 CSV", data=b"name,state,kind\nCaf\xe9 PD,CA,local\n")
        self.refused("line 2: name: at most 160 characters, no control characters", data=b"name,state,kind\nCedar\x00 PD,CA,local\n")
        self.refused("must be a regular file of at most", data=b"name,state,kind\n" + b"x" * (1024 * 1024))
        link = self.tmp / "link.csv"
        self.csv.write_text(BASIC)
        link.symlink_to(self.csv)
        self.refused("cannot read", csv_path=link)
        self.refused("must be a regular file", csv_path=self.tmp)  # a directory
        self.refused("cannot read", csv_path=self.tmp / "missing.csv")

    # 3
    def test_every_bad_row_is_named_by_line_and_nothing_is_written(self):
        text = ("name,state,kind\nGood PD,CA,local\n"
                "Bad State PD,XX,local\n"            # line 3
                "Fed Mislabelled,US,local\n"         # line 4
                "Odd Kind PD,CA,sheriff\n"           # line 5
                ",CA,local\n"                        # line 6
                + "x" * 161 + ",CA,local\n"          # line 7
                "Bell\x07 PD,CA,local\n"             # line 8
                "Lower PD,ca,local\n")               # line 9
        message = self.refused(text=text)
        self.assertNotIn("Good PD", message)
        for line, fragment in ((3, "state: a two-letter"), (4, "state: US is only for kind federal"), (5, "kind: choose one of"),
                               (6, "name: required"), (7, "name: at most 160 characters"), (8, "name: at most 160 characters"),
                               (9, "state: a two-letter")):
            self.assertRegex(message, rf"line {line}: {fragment}")
        self.assertIn("nothing was recorded", message)
        self.assertEqual(self.importing("name,state,kind\n" + "x" * 160 + ",CA,local\n")["recipients"], 1)  # 160 is allowed
        for state in ("US", "CA", "DC", "PR", "GU"):
            self.assertEqual(self.importing(f"name,state,kind\nFictional Agency,{state},federal\n", replace=True)["recipients"], 1)

    def test_at_most_twenty_problems_are_listed_then_how_many_more(self):
        text = "name,state,kind\n" + "".join(f"Agency {n},XX,local\n" for n in range(25))
        message = self.refused(text=text)
        self.assertEqual(len(re.findall(r"line \d+:", message)), 20)
        self.assertIn("line 21:", message)
        self.assertNotIn("line 22:", message)
        self.assertIn("and 5 more", message)

    # 4
    def test_the_row_limit_and_duplicates(self):
        rows = lambda n: "name,state,kind\n" + "".join(f"Agency {i:04d},CA,local\n" for i in range(n))  # noqa: E731
        self.refused("must list 1 to 2000 agencies, found 2001", text=rows(2001))
        self.assertEqual(self.importing(rows(2000))["recipients"], 2000)
        self.canonical(self.base)
        self.refused("line 4: duplicate of line 2 (same name and state)",
                     text="name,state,kind\nReno Test PD,NV,local\nOther PD,NV,local\nRENO test pd,NV,local\n")
        self.assertEqual(self.importing("name,state,kind\nReno Test PD,NV,local\nReno Test PD,CA,local\n", replace=True)["recipients"], 2)

    # 5
    def test_source_url_and_checked_on_are_checked(self):
        for url in ("http://transparency.flocksafety.com/cedar", "https://example.org/cedar", "https://transparency.flocksafety.com.example.org/x",
                    "https://flocksafety.com/cedar", "https://transparency.flocksafety.com:8443/cedar", "https://user@transparency.flocksafety.com/x",
                    "", None):
            with self.subTest(url=url):
                self.refused("--source-url: must be an https:// URL on transparency.flocksafety.com", source_url=url)
        for day in ("2026-11-02", "2999-01-01"):
            self.refused("--checked-on: must not be after today", checked_on=day)
        for day in ("2026-02-30", "11/01/2026", "", None):
            with self.assertRaises(ContentError):
                self.importing(checked_on=day)
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertEqual(self.importing(checked_on="2026-11-01")["checked_on"], "2026-11-01")  # today itself

    # 6
    def test_an_existing_list_needs_replace_and_nothing_else_in_agencies_json_changes(self):
        self.importing()
        recorded = self.path.read_bytes()
        with self.assertRaises(SharingImportError) as caught:
            self.importing("name,state,kind\nOnly One PD,NV,local\n")
        self.assertIn("already has a sharing list; pass --replace", str(caught.exception))
        self.assertEqual(self.path.read_bytes(), recorded)
        self.importing("name,state,kind\nOnly One PD,NV,local\n", replace=True)
        document = self.load()
        entry = next(a for a in document if a["agency_id"] == AGENCY)
        self.assertEqual(entry["profile"]["sharing"]["recipients"], [{"name": "Only One PD", "state": "NV", "kind": "local"}])
        del entry["profile"]["sharing"]
        self.assertEqual(document, self.base)  # parsed: every other agency and field equal
        self.canonical(document)
        self.assertEqual(self.path.read_bytes(), self.original)  # byte-identical once the list is taken out again

    def test_the_agency_must_exist_be_selected_and_the_file_must_be_a_plain_file(self):
        self.refused("agency ca-nowhere-police: not found in content/agencies.json", agency_id="ca-nowhere-police")
        document = copy.deepcopy(self.base)
        next(a for a in document if a["agency_id"] == AGENCY)["selected"] = False
        self.canonical(document)
        self.original = self.path.read_bytes()
        self.refused("is not selected")
        self.canonical(self.base)
        self.original = self.path.read_bytes()
        broken = copy.deepcopy(self.base)
        next(a for a in broken if a["agency_id"] == "ca-cedar-city-council")["kind"] = "not-a-kind"  # a problem elsewhere stops it too
        self.canonical(broken)
        self.original = self.path.read_bytes()
        with self.assertRaises(ContentError):
            self.importing()
        self.assertEqual(self.path.read_bytes(), self.original)
        real = self.tmp / "real.json"
        self.path.rename(real)
        self.path.symlink_to(real)
        with self.assertRaises(ContentError):
            self.importing()
        self.path.unlink()
        with self.assertRaises(SharingImportError):
            self.importing()

    def test_a_failed_write_leaves_the_file_as_it_was(self):
        with mock.patch.object(os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.importing()
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertEqual([p.name for p in self.path.parent.iterdir() if p.name.endswith(".tmp")], [])

    # 7
    def test_a_package_missing_a_rule_leaves_that_line_out_and_the_build_goes_on(self):
        self.importing()
        for missing, kept, gone in ((RULE_ID, "Attorney General bulletin: ", "Sharing rule: "),
                                    (BULLETIN_ID, "Sharing rule: ", "Attorney General bulletin: ")):
            with self.subTest(missing=missing), self.package_without(missing):
                self.build()
                for name in ("agencies/cedar.html", "agencies.html"):
                    html = self.section(self.read(name))
                    self.assertIn(kept, html)
                    self.assertNotIn(gone, html)
                    self.assertIn("Law package: draft, not legally reviewed.", html)
                    self.assertIn("5 agencies", html)
        with self.package_without(RULE_ID, BULLETIN_ID):
            self.build()
            html = self.section(self.read("agencies/cedar.html"))
            self.assertNotIn("Law package", html)
            self.assertNotIn("Sharing rule", html)
            self.assertIn("Reno Test PD, NV (local)", html)
        base = self.tmp / "jurisdictions"
        (base / "us-ca" / "package.json").write_text("{not json")  # unreadable: no citations, no refusal
        with mock.patch.object(law, "JURISDICTIONS", base):
            self.build()
        self.assertNotIn("Law package", self.section(self.read("agencies/cedar.html")))

    def test_wording_guard_and_escaping(self):
        self.importing(BASIC + 'Board <script>alert(1)</script> & Co,OR,private\n')
        self.build()
        for name in ("agencies/cedar.html", "agencies.html"):
            html = self.section(self.read(name))
            self.assertEqual(legal_claims(re.sub(r"<[^>]+>", " ", html)), [])
            for word in FORBIDDEN:
                self.assertNotIn(word, html.lower())
            self.assertNotIn("<script>", html)
            self.assertIn("Board &lt;script&gt;alert(1)&lt;/script&gt; &amp; Co, OR (private)", html)

    # 8
    def test_an_agency_without_sharing_is_unchanged_and_150_names_show_100_then_a_count(self):
        self.build()
        before = self.snapshot()
        self.assertFalse(any(b"agency-sharing" in data for data in before.values()))
        self.importing()
        self.build()
        after = self.snapshot()
        changed = {name for name in set(before) | set(after) if before.get(name) != after.get(name)}
        # DF0237-04: the new /sharing.html, the sitemap entry and the footer link on every page; still not in card JS, open data or feeds
        self.assertEqual(changed, {"agencies.html", "agencies/cedar.html", "sharing.html", "sitemap.xml", "404.html", "about.html",
                                   "cameras.html", "corrections.html", "findings/cedar-policy-posted.html", "findings/index.html",
                                   "findings/packet-all.html", "index.html", "meetings.html", "meetings/cedar-council-2026-10.html",
                                   "methods.html", "privacy.html", "search.html", "sources.html", "sources/beefbeefbeef.html",
                                   "sources/cafecafecafe.html", "timeline.html", "requests.html", "data.html"})  # data.html: DF2207-12 page carries the footer too
        rows = "name,state,kind\n" + "".join(f"Out Of State Agency {i:03d},NV,local\n" for i in range(150))
        self.importing(rows, replace=True)
        self.build()
        for name in ("agencies/cedar.html", "agencies.html"):
            html = self.section(self.read(name))
            self.assertEqual(html.count("<li>"), 100)
            self.assertIn("Out Of State Agency 099, NV (local)", html)
            self.assertNotIn("Out Of State Agency 100", html)
            self.assertIn("<p>and 50 more</p>", html)
            self.assertIn("150 agencies", html)

    # build and content-check agree on a hand-edited list
    def test_build_and_content_check_refuse_a_bad_hand_edited_list(self):
        good = {"checked_on": CHECKED, "source_url": URL, "recipients": [{"name": "Reno Test PD", "state": "NV", "kind": "local"}]}
        label = "content/agencies.json[1].profile.sharing"
        self.assertEqual(next(i for i, a in enumerate(self.base) if a["agency_id"] == AGENCY), 1)
        bad_cases = {
            ".recipients[0].state: a two-letter": dict(good, recipients=[dict(good["recipients"][0], state="XX")]),
            ".recipients[0].state: US is only for kind federal": dict(good, recipients=[dict(good["recipients"][0], state="US")]),
            ".recipients[1]: duplicate of recipients[0]": dict(good, recipients=good["recipients"] * 2),
            ".source_url: must be an https:// URL on transparency.flocksafety.com": dict(good, source_url="https://example.org/x"),
            ".checked_on: must not be after the build date": dict(good, checked_on="2999-01-01"),
            ".recipients: must list 1 to 2000 agencies": dict(good, recipients=[]),
            ": unknown field(s) ['verdict']": dict(good, verdict="x"),
        }
        for fragment, sharing in bad_cases.items():
            with self.subTest(fragment=fragment):
                document = copy.deepcopy(self.base)
                document[1].setdefault("profile", {})["sharing"] = sharing
                self.canonical(document)
                with self.assertRaises(ContentError) as caught:
                    self.build()
                self.assertIn(label + fragment, str(caught.exception))
                self.assertIn(str(caught.exception), content_problems(self.root))
        document = copy.deepcopy(self.base)
        document[1].setdefault("profile", {})["sharing"] = dict(good, recipients=list(reversed(good["recipients"] + [
            {"name": "Alpha PD", "state": "AK", "kind": "state"}])))
        self.canonical(document)
        self.assertEqual([r["state"] for r in load_agencies(self.load(), "content/agencies.json", TODAY)[1]["sharing"]["recipients"]],
                         ["AK", "NV"])  # a hand-edited list is shown sorted

    # the command line
    def test_the_command_records_and_a_refusal_is_one_stopped_line_with_nothing_written(self):
        self.csv.write_text(BASIC)
        arguments = [sys.executable, "-B", "-m", "campaign_tool", "content", "agency-sharing", "--directory", str(self.root), "--agency", AGENCY,
                     "--csv", str(self.csv), "--checked-on", CHECKED, "--source-url", URL]
        run = lambda *extra: subprocess.run(arguments + list(extra), cwd=REPO_ROOT, capture_output=True, text=True, timeout=120)  # noqa: E731
        done = run()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout.split("\nRecorded only")[0])["recipients"], 5)
        self.assertIn("Recorded only. Nothing was fetched or published.", done.stdout)
        recorded = self.path.read_bytes()
        again = run()
        self.assertNotEqual(again.returncode, 0)
        self.assertIn("Stopped: agency ca-cedar-police already has a sharing list", again.stderr)
        self.assertEqual(self.path.read_bytes(), recorded)
        self.csv.write_text("name,state,kind\nBad PD,XX,local\n")
        failed = run("--replace")
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("line 2: state:", failed.stderr)
        self.assertEqual(self.path.read_bytes(), recorded)


if __name__ == "__main__":
    unittest.main()
