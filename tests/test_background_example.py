"""DF1407-12: examples/pages/background.json, the draft "Background reading" page that a fresh `content init` seeds.

Everything runs on temporary synthetic campaigns; sockets are blocked and no linked document is fetched.
The roster below is the independently frozen list of documents; the committed page is compared to it.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock
from urllib.parse import urlsplit

from campaign_tool import content_init
from campaign_tool.content import load_pages
from campaign_tool.content_check import content_problems
from campaign_tool.legal_claims import legal_claims, legal_notes
from campaign_tool.markdown_lite import lint
from campaign_tool.site import build_site

REPO = Path(__file__).resolve().parents[1]
STARTER = REPO / "examples" / "pages" / "background.json"
MIRROR = REPO / "campaign_tool" / "_resources" / "examples" / "pages" / "background.json"
MARKER = "[EDIT ME: verify for this campaign] "
OFFICIAL_HOSTS = {"information.auditor.ca.gov", "www.auditor.ca.gov", "auditor.ca.gov", "www.oag.ca.gov", "oag.ca.gov",
                  "leginfo.legislature.ca.gov", "leginfo.ca.gov"}
GROUPS = ["Audit and guidance", "California statutes and public records", "Legislative background"]
LEG = "https://leginfo.legislature.ca.gov/faces/"
ROSTER = [  # (group index, title, publisher, date label, date, url)
    (0, "Automated License Plate Readers: To Better Protect Individuals’ Privacy, Law Enforcement Must Increase Its Safeguards for the Data It Collects",
     "California State Auditor (report 2019-118)", "Published", "February 13, 2020", "https://information.auditor.ca.gov/pdfs/reports/2019-118.pdf"),
    (0, "California Automated License Plate Reader Data Guidance",
     "California Department of Justice, Division of Law Enforcement (Information Bulletin 2023-DLE-06)", "Issued", "October 27, 2023",
     "https://www.oag.ca.gov/system/files/media/2023-dle-06.pdf"),
    (1, "Civil Code, Title 1.81.23. Collection of License Plate Information (sections 1798.90.5 to 1798.90.55)",
     "California Legislative Information, Office of Legislative Counsel", "Effective", "January 1, 2016",
     LEG + "codes_displayText.xhtml?division=3.&lawCode=CIV&part=4.&title=1.81.23."),
    (1, "Government Code section 7920.000", "California Legislative Information, Office of Legislative Counsel", "Effective",
     "January 1, 2022; operative January 1, 2023", LEG + "codes_displaySection.xhtml?lawCode=GOV&sectionNum=7920.000."),
    (1, "Government Code section 7922.530", "California Legislative Information, Office of Legislative Counsel", "Effective",
     "January 1, 2022; operative January 1, 2023", LEG + "codes_displaySection.xhtml?lawCode=GOV&sectionNum=7922.530."),
    (1, "Government Code section 7922.535", "California Legislative Information, Office of Legislative Counsel", "Effective",
     "January 1, 2026 (amended by Stats. 2025, ch. 34, AB 370)", LEG + "codes_displaySection.xhtml?lawCode=GOV&sectionNum=7922.535."),
    (1, "Government Code section 7923.000", "California Legislative Information, Office of Legislative Counsel", "Effective",
     "January 1, 2022; operative January 1, 2023", LEG + "codes_displaySection.xhtml?lawCode=GOV&sectionNum=7923.000."),
    (2, "SB 34 (Hill), Automated license plate recognition systems: use of data, Senate Floor Analysis (Unfinished Business)",
     "California State Senate, Office of Senate Floor Analyses, on the Legislature's leginfo site", "Analysis date", "September 3, 2015",
     "https://leginfo.ca.gov/pub/15-16/bill/sen/sb_0001-0050/sb_34_cfa_20150903_180229_sen_floor.html"),
    (2, "AB 370, California Public Records Act: cyberattacks (Chapter 34, Statutes of 2025)",
     "California Legislative Information, Office of Legislative Counsel", "Approved", "July 14, 2025",
     LEG + "billTextClient.xhtml?bill_id=202520260AB370"),
]
NO_SOCKETS = mock.patch("socket.socket", side_effect=AssertionError("network access attempted"))


def starter():
    return json.loads(STARTER.read_text(encoding="utf-8"))


def entries(document):
    """Parse the page into (group, title, fields) with the same strictness the roster test asserts."""
    found, group = [], None
    for block in re.split(r"\n(?=#{2,3} )", document["body_md"]):
        if block.startswith("## "):
            group = block[3:].split("\n")[0].strip()
        elif block.startswith("### "):
            lines = [line for line in block.split("\n") if line.strip()]
            found.append((group, lines[0][4:].strip(), lines[1:]))
    return found


def structural_problems(document):
    """Problems in the page's citation structure (a test helper, not a runtime schema)."""
    problems, seen = [], set()
    for group, title, rest in entries(document):
        if len(rest) != 4 or not rest[0].startswith("Publisher: ") or not re.match(r"^[A-Za-z ]+: \S", rest[1]):
            problems.append(f"{title}: expected publisher, dated label, link, sentence")
            continue
        link = re.fullmatch(r"\[(.+)\]\((\S+)\)", rest[2])
        if not link or link.group(1) != title:
            problems.append(f"{title}: expected one title link")
            continue
        parts = urlsplit(link.group(2))
        if parts.scheme != "https" or parts.hostname not in OFFICIAL_HOSTS:
            problems.append(f"{title}: not an https link on an official host")
        if link.group(2) in seen:
            problems.append(f"{title}: duplicate link")
        seen.add(link.group(2))
        if len(re.findall(r"[.!?](?:\s|$)", rest[3][len(MARKER):])) != 1 or not rest[3].startswith(MARKER):
            problems.append(f"{title}: expected one marked sentence")
    return problems


def cli(*args):
    return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", *map(str, args)], cwd=REPO,
                          capture_output=True, text=True, timeout=120)


class BackgroundCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-background-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        NO_SOCKETS.start()
        self.addCleanup(NO_SOCKETS.stop)

    def campaign(self, name="campaign", state="CA"):
        root = self.tmp / name
        result = cli("init", "--directory", root, "--name", "Synthetic Cedar Watch", "--county", "Cedar", "--state", state)
        self.assertEqual(result.returncode, 0, result.stderr)
        return root


class StarterTests(BackgroundCase):
    def test_roster_has_verified_official_documents_and_required_coverage(self):
        document = starter()
        self.assertEqual(list(document), ["title", "description", "body_md", "nav"])
        self.assertEqual(document["title"], "Background reading")
        self.assertEqual(document["description"], "Official California ALPR and public-records background for organizer review.")
        self.assertIs(document["nav"], True)
        found = entries(document)
        self.assertEqual(len(found), 9)
        self.assertEqual(re.findall(r"^## (.+)$", document["body_md"], flags=re.M), GROUPS)
        for roster, (found_group, found_title, rest) in zip(ROSTER, found):
            index, title, publisher, label, when, url = roster
            self.assertEqual((found_group, found_title), (GROUPS[index], title))
            self.assertEqual(rest[:3], ["Publisher: " + publisher, f"{label}: {when}", f"[{title}]({url})"])
        self.assertEqual(structural_problems(document), [])
        self.assertEqual(len({r[5] for r in ROSTER}), 9)
        self.assertTrue(all(urlsplit(r[5]).hostname in OFFICIAL_HOSTS and r[5].startswith("https://") for r in ROSTER))

    def test_structural_helper_rejects_bad_citations(self):
        good = starter()

        def mutate(old, new):
            document = dict(good, body_md=good["body_md"].replace(old, new, 1))
            self.assertNotEqual(document["body_md"], good["body_md"], old)
            return structural_problems(document)

        self.assertTrue(mutate("(https://www.oag.ca.gov/", "(http://www.oag.ca.gov/"))
        self.assertTrue(mutate("(https://www.oag.ca.gov/", "(https://www.oag.ca.gov.example.com/"))
        self.assertTrue(mutate("(https://www.oag.ca.gov/", "(https://evil-oag.ca.gov/"))
        self.assertTrue(mutate("Publisher: California State Auditor (report 2019-118)", "Publisher:"))
        self.assertTrue(mutate("Published: February 13, 2020", "Published:"))
        self.assertTrue(mutate("(https://information.auditor.ca.gov/pdfs/reports/2019-118.pdf)",
                               "(https://www.oag.ca.gov/system/files/media/2023-dle-06.pdf)"))  # duplicate link
        self.assertTrue(mutate("(https://leginfo.ca.gov/pub/15-16/bill/sen/sb_0001-0050/sb_34_cfa_20150903_180229_sen_floor.html)", "(#)"))

    def test_plain_document_coverage_and_edit_markers(self):
        body = starter()["body_md"]
        self.assertTrue(body.startswith(MARKER + "Re-check every title, publisher, date and official link before publishing this page."))
        self.assertEqual(body.count("[EDIT ME: "), 10)  # one intro marker and one per document
        for phrase in ("not findings about any agency", "not legal advice", "draft", "California"):
            self.assertIn(phrase, body)
        self.assertNotRegex(body, r"(?i)<|!\[|<form|\bviolat|\bunlawful|\billegal|\brefus|\bcomplian")
        self.assertEqual(re.findall(r"\]\((?!https://)[^)]*\)", body), [])
        self.assertEqual(lint(body), [])

    def test_load_pages_content_check_and_legal_guard(self):
        document = starter()
        root = self.campaign()
        content_init.init_content(root)
        self.assertEqual(content_problems(root), [])
        checked = cli("content-check", "--directory", root)
        self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
        tree = self.tmp / "content-tree"
        (tree / "pages").mkdir(parents=True)
        (tree / "pages" / "background.json").write_text(json.dumps(document), encoding="utf-8")
        self.assertEqual([(page["slug"], page["nav"]) for page in load_pages(tree, set())], [("background", True)])
        fields = [(f"pages/background.json.{key}", document[key]) for key in ("title", "description", "body_md")]
        self.assertEqual(legal_notes(fields), [])
        for index, (location, text) in enumerate(fields):  # the guard still flags an unquoted accusation in each field
            changed = list(fields)
            changed[index] = (location, text + "\n\nThe synthetic agency violated the law.")
            notes = legal_notes(changed)
            self.assertEqual(len(notes), 1, location)
            self.assertIn(location, notes[0])
            self.assertIn("review-required", notes[0])
            self.assertNotIn("synthetic agency", notes[0])
        self.assertIn("violation-verb", legal_claims(document["body_md"] + "\n\nThe synthetic agency violated the law."))


class FreshInitTests(BackgroundCase):
    def test_fresh_init_build_nav_links_and_opt_out(self):
        for state in ("CA", "OR"):  # the same California-scoped page for any state
            root = self.campaign("fresh-" + state, state)
            report = content_init.init_content(root)
            self.assertEqual(report["written"], sorted(report["written"]))
            self.assertEqual(report["written"].count("content/pages/"), 1)
            self.assertIn("content/pages/background.json", report["written"])
            content = root / "content"
            self.assertEqual((content / "pages" / "background.json").read_bytes().decode("utf-8"),
                             json.dumps(starter(), indent=2, ensure_ascii=False) + "\n")
            texts = [(content / name).read_text(encoding="utf-8") for name in ("site.json", "faq.json", "pages/help-map.json", "pages/background.json")]
            self.assertEqual(report["placeholders"], sum(text.count("[EDIT ME: ") for text in texts))
            self.assertEqual(content_problems(root), [])
            build_site(root, today=date(2026, 10, 1))
            public = root / "public"
            page = (public / "background.html").read_text(encoding="utf-8")
            self.assertEqual(page.count("[EDIT ME: verify for this campaign]"), 10)
            for _, title, _, _, _, url in ROSTER:
                self.assertIn(url.replace("&", "&amp;"), page)
                self.assertIn(title.replace("&", "&amp;"), page)
            index = (public / "index.html").read_text(encoding="utf-8")
            self.assertIn('href="/background.html"', index)
            self.assertIn('href="/help-map.html"', index)
            build = cli("build", "--directory", root, "--check")
            self.assertEqual(build.returncode, 0, build.stderr)
        # deleting the page removes it on the next build and leaves the other starter alone
        (content / "pages" / "background.json").unlink()
        build_site(root, today=date(2026, 10, 1))
        self.assertFalse((public / "background.html").exists())
        self.assertNotIn("background.html", (public / "index.html").read_text(encoding="utf-8"))
        self.assertTrue((public / "help-map.html").is_file())

    def test_sitemap_lists_background_when_base_url_is_set(self):
        root = self.campaign()
        content_init.init_content(root)
        site = json.loads((root / "content" / "site.json").read_text(encoding="utf-8"))
        site["base_url"] = "https://example.org/"
        (root / "content" / "site.json").write_text(json.dumps(site), encoding="utf-8")
        build_site(root, today=date(2026, 10, 1))
        self.assertIn("https://example.org/background.html", (root / "public" / "sitemap.xml").read_text(encoding="utf-8"))

    def test_installed_background_without_checkout_and_fresh_dict(self):
        self.assertEqual(MIRROR.read_bytes(), STARTER.read_bytes())
        self.assertEqual(content_init.BACKGROUND_STARTER, MIRROR)
        real_read_text = Path.read_text

        def no_checkout_examples(path, *args, **kwargs):
            if (REPO / "examples") in Path(path).parents:
                raise AssertionError("runtime read the checkout examples directory")
            return real_read_text(path, *args, **kwargs)

        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        previous = Path.cwd()
        os.chdir(elsewhere)
        try:
            with mock.patch.object(Path, "read_text", no_checkout_examples):
                document = content_init.background_document()
        finally:
            os.chdir(previous)
        self.assertEqual(document, starter())
        document["title"] = "changed"
        self.assertEqual(content_init.background_document(), starter())
        broken = self.tmp / "broken.json"
        broken.write_text(json.dumps(dict(starter(), body_md="Text <b>bold</b> and ![x](https://example.org/a.png)")), encoding="utf-8")
        with mock.patch.object(content_init, "BACKGROUND_STARTER", broken):
            with self.assertRaises(RuntimeError):
                content_init.background_document()
        broken.write_text("{not json", encoding="utf-8")
        with mock.patch.object(content_init, "BACKGROUND_STARTER", broken):
            with self.assertRaises(ValueError):
                content_init.background_document()
        with mock.patch.object(content_init, "BACKGROUND_STARTER", self.tmp / "missing.json"):
            with self.assertRaises(OSError):
                content_init.background_document()

    def test_init_failure_leaves_nothing_and_modes_are_private(self):
        root = self.campaign("failures")
        before = sorted(path.name for path in root.iterdir())
        real_write = content_init._write

        def failing(path, text):
            if path.name == "background.json":
                raise OSError("injected page write failure")
            return real_write(path, text)

        for label, patches in (("write", mock.patch.object(content_init, "_write", side_effect=failing)),
                               ("load", mock.patch.object(content_init, "load_pages", return_value=[])),
                               ("starter", mock.patch.object(content_init, "BACKGROUND_STARTER", self.tmp / "missing.json"))):
            with self.subTest(label), patches:
                with self.assertRaises((OSError, RuntimeError)):
                    content_init.init_content(root)
            self.assertFalse(os.path.lexists(root / "content"))
            self.assertEqual(list(root.glob(".content.new-*")), [])
            self.assertEqual(sorted(path.name for path in root.iterdir()), before)
        content_init.init_content(root)
        content = root / "content"
        for path in [content, *content.rglob("*")]:
            self.assertEqual(path.stat().st_mode & 0o777, 0o700 if path.is_dir() else 0o600, path)

    def test_from_copies_only_supplied_background(self):
        old, new = self.campaign("old"), self.campaign("new", "OR")
        (old / "content" / "pages").mkdir(parents=True)
        (old / "content" / "site.json").write_text(json.dumps(
            {"schema_version": 1, "tagline": "A tagline", "mission_md": "A mission", "about_md": "About"}), encoding="utf-8")
        report = content_init.init_content(new, source_root=old)
        self.assertFalse((new / "content" / "pages" / "background.json").exists())  # a missing source page is not injected
        self.assertNotIn("content/pages/background.json", report["copied"])
        (old / "content" / "pages" / "background.json").write_text(json.dumps(
            {"title": "Our own reading", "body_md": "Organizer text."}), encoding="utf-8")
        newer = self.campaign("newer", "OR")
        content_init.init_content(newer, source_root=old)
        copied = json.loads((newer / "content" / "pages" / "background.json").read_text(encoding="utf-8"))
        self.assertEqual(copied["title"], "Our own reading")
        with self.assertRaisesRegex(ValueError, "content/ already exists; nothing was written"):
            content_init.init_content(newer, source_root=old)


if __name__ == "__main__":
    unittest.main()
