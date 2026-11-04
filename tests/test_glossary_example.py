"""DF1952-05: examples/glossary.json, the opt-in draft glossary starter, checked through DF1952-04's real loader and builder.

Everything runs on temporary synthetic campaigns; sockets are blocked and nothing is fetched or sent.
"""
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

from campaign_tool import law
from campaign_tool.cli import check_public_tree
from campaign_tool.content import ContentError, glossary_key, glossary_terms_used, load_glossary, read_glossary
from campaign_tool.content_check import content_problems
from campaign_tool.legal_claims import legal_claims
from campaign_tool.site import build_site
from campaign_tool.site_check import check_accessibility, check_links

REPO = Path(__file__).resolve().parents[1]
STARTER = REPO / "examples" / "glossary.json"
EXAMPLE = REPO / "examples" / "fictional-campaign"
GUIDE = REPO / "docs" / "NEW-COUNTY.md"
WRAPPER = REPO / "tests" / "fixtures" / "no_network_run.py"
TODAY = date(2026, 10, 1)
NOTICE = ("Draft glossary. The organizer must check every definition, citation and official source before use. "
          "This is not legal advice and makes no claim about whether any agency complied with or violated a rule.")
TOPICS = {
    "ALPR", "ALPR information", "ALPR operator", "ALPR end-user", "hot list", "retention period", "data sharing",
    "audit log", "search query", "usage and privacy policy", "records request", "determination", "extension",
    "exemption", "redaction", "fee estimate", "public record", "custodian", "transparency portal", "sharing agreement",
    "access permissions", "public meeting", "source record", "correction", "information gap"}
# The statutory senses and the existing us-ca package rule each one cites.
RULES = {
    "ALPR": "ca-civ-1798.90.5-definitions", "ALPR information": "ca-civ-1798.90.5-definitions",
    "ALPR operator": "ca-civ-1798.90.5-definitions", "ALPR end-user": "ca-civ-1798.90.5-definitions",
    "audit log": "ca-civ-1798.90.52-access-records",
    "retention period": "ca-civ-1798.90.51-usage-and-privacy-policy",
    "usage and privacy policy": "ca-civ-1798.90.51-usage-and-privacy-policy",
    "custodian": "ca-civ-1798.90.51-usage-and-privacy-policy",
    "data sharing": "ca-civ-1798.90.55-sharing-limits",
    "determination": "ca-gov-7922.535-determination-and-extension-2026",
    "extension": "ca-gov-7922.535-determination-and-extension-2026"}
# Words a neutral starter must not use: late/entitlement/guarantee wording, automatic outcomes, violation wording.
OVERCLAIM = re.compile(r"\b(?:late|overdue|entitled|entitlement|guarantee[ds]?|automatically|violat\w*|illegal|unlawful\w*|"
                       r"non-?complian\w*|breach\w*|has to remove)\b", re.I)
DEADLINE = re.compile(r"\b\d+[- ]?(?:calendar |business |working )?(?:days?|hours?|weeks?|months?|years?)\b", re.I)
NO_SOCKETS = mock.patch("socket.socket", side_effect=AssertionError("network access attempted"))


def starter():
    return json.loads(STARTER.read_text(encoding="utf-8"))


def sentences(text):
    return [part for part in re.split(r"(?<=[.!?])\s+", text.strip()) if part]


class StarterCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-glossary-starter-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.root = self.campaign("campaign")
        NO_SOCKETS.start()
        self.addCleanup(NO_SOCKETS.stop)

    def campaign(self, name):
        root = self.tmp / name
        shutil.copytree(EXAMPLE, root, ignore=shutil.ignore_patterns("public"))
        return root

    def copy_starter(self, root):
        """The documented opt-in step, done by hand: nothing in the engine copies the file."""
        shutil.copyfile(STARTER, root / "content" / "glossary.json")

    def build(self, root):
        build_site(root, today=TODAY)
        public = root / "public"
        return {p.relative_to(public).as_posix(): p.read_bytes() for p in sorted(public.rglob("*")) if p.is_file()}

    def refused(self, document):
        problems = []
        self.assertIsNone(load_glossary(document, self.root, problems))
        with self.assertRaises(ContentError):
            load_glossary(document, self.root)
        return problems


class StarterShapeTests(StarterCase):
    def test_starter_has_25_plain_language_terms_and_visible_draft_notice(self):
        document = starter()
        self.assertEqual(set(document), {"schema_version", "notice_md", "terms"})  # no comments, draft flag or metadata
        self.assertEqual(document["schema_version"], 1)
        self.assertEqual(document["notice_md"], NOTICE)
        terms = document["terms"]
        self.assertEqual(len(terms), 25)
        self.assertEqual({entry["term"] for entry in terms}, TOPICS)
        names = []
        for entry in terms:
            with self.subTest(term=entry["term"]):
                self.assertLessEqual(set(entry), {"term", "aliases", "definition_md", "rule_id", "source_url"})
                self.assertEqual("rule_id" in entry, "source_url" in entry)  # a rule never travels without its source
                self.assertTrue(1 <= len(entry["aliases"]) <= 3)
                self.assertTrue(entry["definition_md"].strip())
                self.assertTrue(1 <= len(sentences(entry["definition_md"])) <= 3, entry["definition_md"])
                names += [entry["term"], *entry["aliases"]]
        # globally unique under the loader's own normalization: no alias repeats another entry's name or alias
        self.assertEqual(len({glossary_key(name) for name in names}), len(names))
        # the real loader accepts it, in the notice's order, with one anchor per term
        self.copy_starter(self.root)
        loaded = read_glossary(self.root)
        self.assertEqual({t["term"] for t in loaded["terms"]}, TOPICS)
        self.assertEqual(loaded["notice_md"], NOTICE)
        self.assertEqual(len({t["anchor"] for t in loaded["terms"]}), 25)
        page = self.build(self.root)["glossary.html"].decode()
        self.assertEqual(len(re.findall(r'<h3 id="term-[a-z0-9-]+">', page)), 25)
        self.assertIn(NOTICE, page)  # the draft notice is on the rendered page itself
        self.assertLess(page.index("Draft glossary."), page.index('id="term-'))

    def test_loader_refuses_a_broken_copy_of_the_starter(self):
        # the validator really runs on this file: each edit below is refused by name, not tolerated
        for label, edit, pattern in (
                ("sixth alias", lambda d: d["terms"][0]["aliases"].extend(["a1", "a2", "a3"]), r"terms\[0\]\.aliases"),
                ("alias is another term", lambda d: d["terms"][1].update(aliases=["ALPR"]), r"repeats 'ALPR'"),
                ("blank definition", lambda d: d["terms"][2].update(definition_md="  "), r"terms\[2\]\.definition_md"),
                ("draft flag", lambda d: d.update(draft=True), r"unknown field")):
            with self.subTest(label):
                document = starter()
                edit(document)
                self.assertTrue(any(re.search(pattern, p) for p in self.refused(document)), label)


class StarterCitationTests(StarterCase):
    def test_statutory_citations_resolve_to_exact_package_sources(self):
        package = law.load_package("us-ca")
        rules = {rule["rule_id"]: rule for rule in package["rules"]}
        self.assertEqual(package["status"], "draft")  # the starter does not upgrade the package
        for entry in starter()["terms"]:
            with self.subTest(term=entry["term"]):
                self.assertEqual(entry.get("rule_id"), RULES.get(entry["term"]))
                if "rule_id" in entry:
                    self.assertIn(entry["rule_id"], rules)
                    self.assertIn(entry["source_url"], [s["url"] for s in rules[entry["rule_id"]]["sources"]])
        self.copy_starter(self.root)
        by_term = {t["term"]: t for t in read_glossary(self.root)["terms"]}
        for term, rule_id in RULES.items():
            self.assertEqual(by_term[term]["citation"], rules[rule_id]["citation"], term)
            self.assertEqual(by_term[term]["law_status"], "draft, not legally reviewed", term)
        page = self.build(self.root)["glossary.html"].decode()
        self.assertEqual(page.count("Law package citation:"), len(RULES))
        self.assertEqual(page.count("Status of this law package: draft, not legally reviewed"), len(RULES))
        self.assertNotIn("reviewed on", page)

    def test_citation_mutations_are_refused_not_stubbed(self):
        # an unknown rule id, a plain-http source and another rule's source each stop the real loader
        for label, change, pattern in (
                ("unknown rule", {"rule_id": "ca-civ-0-not-a-rule"}, r"is not in the us-ca law package"),
                ("http url", {"source_url": "http://leginfo.legislature.ca.gov/faces/codes_displaySection.xhtml?"
                                            "lawCode=CIV&sectionNum=1798.90.5."}, r"source_url"),
                ("another rule's source", {"source_url": next(e for e in starter()["terms"]
                                                              if e["term"] == "data sharing")["source_url"]},
                 r"is not one of the sources")):
            with self.subTest(label):
                self.copy_starter(self.root)
                document = starter()
                document["terms"][0].update(change)
                problems = self.refused(document)
                self.assertTrue(any(re.search(pattern, p) for p in problems), problems)
        # a rule id without its source is refused too
        document = starter()
        del document["terms"][0]["source_url"]
        self.assertTrue(any("source_url" in p and "required" in p for p in self.refused(document)))


class StarterProseTests(StarterCase):
    def texts(self):
        document = starter()
        return [("notice", document["notice_md"])] + [
            (f"{entry['term']}.{part}", text) for entry in document["terms"]
            for part, text in [("term", entry["term"]), ("definition", entry["definition_md"]),
                               *[(f"alias{n}", alias) for n, alias in enumerate(entry["aliases"])]]]

    def test_starter_prose_has_no_legal_claims(self):
        for where, text in self.texts():
            with self.subTest(where):
                self.assertEqual(legal_claims(text), [])
        # positive control: the same guard does flag assertive wording, so the empty results above mean something
        self.assertIn("violation-verb", legal_claims("The sheriff violated the statute."))
        self.assertIn("broke-the-law", legal_claims("The city unlawfully shared plate data."))
        self.assertIn("violation-verb", legal_claims(starter()["terms"][0]["definition_md"] + " The sheriff violated it."))
        for entry in starter()["terms"]:  # no universal deadline, fixed retention figure, entitlement or outcome
            with self.subTest(term=entry["term"]):
                self.assertIsNone(OVERCLAIM.search(entry["definition_md"]), entry["definition_md"])
                self.assertIsNone(DEADLINE.search(entry["definition_md"]), entry["definition_md"])
        definitions = {e["term"]: e["definition_md"] for e in starter()["terms"]}
        self.assertEqual(definitions["search query"],
                         "A search someone runs in an ALPR database, such as typing in a plate number or choosing a place and time. "
                         "Ask what search details the log records and what any blank or hidden fields mean.")
        self.assertIn("not automatic", definitions["extension"])
        self.assertIn("not the same as handing the records over", definitions["determination"])
        self.assertIn("does not decide", definitions["ALPR end-user"])

    def test_notice_is_rendered_and_html_is_escaped_by_the_normal_renderer(self):
        self.copy_starter(self.root)
        page = self.build(self.root)["glossary.html"].decode()
        self.assertIn(f"<p>{NOTICE}</p>", page)
        self.assertNotIn("<script", page)
        hrefs = re.findall(r'<a href="([^"]*sectionNum[^"]*)" rel="noopener noreferrer">Source</a>', page)
        self.assertEqual(len(hrefs), len(RULES))
        self.assertTrue(all("&amp;sectionNum" in h and "&sectionNum" not in h.replace("&amp;", "") for h in hrefs))
        # markup typed into a definition is shown as text, never as HTML
        document = starter()
        document["terms"][0]["definition_md"] = "Plain <script>alert(1)</script> text."
        (self.root / "content" / "glossary.json").write_text(json.dumps(document), encoding="utf-8")
        page = self.build(self.root)["glossary.html"].decode()
        self.assertNotIn("<script>alert", page)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page)


class StarterBuildTests(StarterCase):
    def test_copy_then_check_build_and_finding_links(self):
        before = self.build(self.campaign("without"))
        self.copy_starter(self.root)
        self.assertEqual(content_problems(self.root), [])
        files = self.build(self.root)
        self.assertEqual(set(files) - set(before), {"glossary.html"})  # the only new URL
        self.assertEqual(set(before) - set(files), set())
        self.assertIn("/glossary.html", files["sitemap.xml"].decode().replace("https://cedar-records.example.invalid", ""))
        self.assertEqual(check_links(self.root / "public"), [])
        self.assertEqual(check_accessibility(self.root / "public"), [])
        self.assertEqual(check_public_tree(self.root / "public"), [])
        self.assertNotIn("glossary.json", files)  # the input file is never published
        page = files["glossary.html"].decode()
        anchors = set(re.findall(r'\bid="(term-[a-z0-9-]+)"', page))
        self.assertEqual(len(anchors), 25)
        finding = files["findings/cedar-policy-posted.html"].decode()
        block = re.search(r'<h3 id="glossary-terms">Terms used on this page</h3><ul>(.*?)</ul>', finding, re.S).group(1)
        linked = re.findall(r'<a href="/glossary\.html#(term-[a-z0-9-]+)">([^<]+)</a>', block)
        self.assertTrue({anchor for anchor, _ in linked} <= anchors)
        # the fictional policy finding says "ALPR", "records request", "usage and privacy policy", "retention period"
        # and "data-sharing agreement" (found through the alias-free "sharing agreement" whole-word match)
        self.assertEqual([name for _, name in linked],
                         ["ALPR", "records request", "retention period", "sharing agreement", "usage and privacy policy"])

    def test_aliases_and_canonical_terms_are_detected_in_findings(self):
        self.copy_starter(self.root)
        glossary = read_glossary(self.root)

        def used(**fields):
            return [t["term"] for t in glossary_terms_used(dict({"title": "", "summary": "", "body_md": ""}, **fields), glossary)]

        self.assertEqual(used(title="Automated License Plate Reader audit log"), ["ALPR", "audit log"])
        self.assertEqual(used(summary="A CPRA request about the hotlist."), ["hot list", "records request"])
        self.assertEqual(used(body_md="An unusual circumstances extension and a Fee Estimate."), ["extension", "fee estimate"])
        self.assertEqual(used(title="Data retention", body_md="see the MOU"), ["retention period", "sharing agreement"])
        self.assertEqual(used(title="Nothing relevant here"), [])
        self.assertEqual(used(title="Hotlists and ALPRs"), [])  # plural forms are whole-word misses, never partial matches

    def test_opt_in_absent_and_legacy_output_unchanged(self):
        content = self.root / "content"
        inputs = {p.relative_to(content).as_posix(): p.read_bytes() for p in sorted(content.rglob("*")) if p.is_file()}
        self.assertNotIn("glossary.json", inputs)  # the fictional content never carries the starter
        self.assertFalse((EXAMPLE / "content" / "glossary.json").exists())
        plain = self.build(self.root)
        self.assertNotIn("glossary.html", plain)
        self.assertNotIn(b"Terms used on this page", b"".join(plain.values()))
        self.assertNotIn(b"/glossary.html", b"".join(plain.values()))
        self.assertEqual(inputs, {p.relative_to(content).as_posix(): p.read_bytes()
                                  for p in sorted(content.rglob("*")) if p.is_file()})  # a build never edits its inputs
        again = self.build(self.campaign("again"))
        self.assertEqual(plain, again)  # byte-identical whole public tree without the explicit copy
        # an empty glossary leaves the feature off
        (content / "glossary.json").write_text(json.dumps({"schema_version": 1, "terms": []}), encoding="utf-8")
        self.assertEqual(self.build(self.root), plain)
        # opting in and out again returns to the same bytes
        self.copy_starter(self.root)
        self.assertNotEqual(self.build(self.root), plain)
        (content / "glossary.json").unlink()
        shutil.rmtree(self.root / "public")
        self.assertEqual(self.build(self.root), plain)

    def test_init_and_content_init_never_insert_the_starter(self):
        for command in (("init", "--directory", "{d}/new", "--name", "Synthetic Humboldt", "--county", "Humboldt", "--state", "CA"),
                        ("content", "init", "--directory", "{d}/new")):
            self.cli(*[arg.format(d=self.tmp) for arg in command])
        self.assertTrue((self.tmp / "new" / "content").is_dir())
        self.assertEqual(sorted(p.name for p in (self.tmp / "new").rglob("glossary*")), [])

    def cli(self, *args, code=0):
        result = subprocess.run([sys.executable, "-B", "-W", "error::ResourceWarning", str(WRAPPER), *map(str, args)],
                                cwd=REPO, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        self.assertNotIn("NETWORK_FORBIDDEN", result.stderr)
        return result


class NewCountyGuideTests(StarterCase):
    cli = StarterBuildTests.cli

    def test_new_county_instructions_match_offline_copy(self):
        guide = GUIDE.read_text(encoding="utf-8")
        section = guide.split("## Optional: start from the draft glossary", 1)[1].split("\n## ", 1)[0]
        flat = " ".join(section.split())
        steps = re.findall(r"^(cp examples/glossary\.json \S+|python3 -m campaign_tool .+)$", section, re.M)
        self.assertEqual(steps, ["cp examples/glossary.json ../humboldt-demo/content/glossary.json",
                                 "python3 -m campaign_tool content-check --directory ../humboldt-demo",
                                 "python3 -m campaign_tool build --directory ../humboldt-demo --check"])
        for phrase in ("opt-in", "never copy it", "read it first", "repeat explicitly for `fresno-demo`",
                       "Do not overwrite a `content/glossary.json` you already edited", "Inspect and edit the copied JSON",
                       "check the current law", "review `glossary.html`", "copying it is not approval",
                       "another state needs its own checked law package", "not legal advice",
                       "must not be used to skip review or to publish the fictional county evidence"):
            self.assertIn(phrase, flat)
        # follow the documented steps on temporary Humboldt and Fresno previews, with the demo folders mapped to temp ones
        for county in ("Humboldt", "Fresno"):
            with self.subTest(county=county):
                demo = self.tmp / (county.lower() + "-demo")
                self.cli("init", "--directory", demo, "--name", "Synthetic " + county, "--county", county, "--state", "CA")
                shutil.copytree(EXAMPLE / "content", demo / "content")
                site = demo / "content" / "site.json"
                site.write_text(site.read_text(encoding="utf-8").replace("Cedar County", county + " County"), encoding="utf-8")
                self.assertFalse((demo / "content" / "glossary.json").exists())
                copy = steps[0].replace("../humboldt-demo", str(demo)).split()
                self.assertEqual(copy[:2], ["cp", "examples/glossary.json"])
                shutil.copyfile(REPO / copy[1], copy[2])
                self.assertEqual((demo / "content" / "glossary.json").read_bytes(), STARTER.read_bytes())
                checked = self.cli("content-check", "--directory", demo)
                self.assertNotIn("glossary.json", checked.stdout + checked.stderr)
                built = self.cli("build", "--directory", demo, "--check")
                self.assertIn("Public tree check passed", built.stdout)
                page = (demo / "public" / "glossary.html").read_text(encoding="utf-8")
                self.assertIn(NOTICE, page)
                self.assertEqual(len(re.findall(r'<h3 id="term-', page)), 25)
                self.assertIn('href="/glossary.html#term-alpr"', (demo / "public" / "findings" / "cedar-policy-posted.html").read_text(encoding="utf-8"))
        # a malformed copy stops the documented check before any build
        bad = self.tmp / "humboldt-demo" / "content" / "glossary.json"
        bad.write_text("{not json", encoding="utf-8")
        refused = subprocess.run([sys.executable, "-B", str(WRAPPER), "content-check", "--directory", str(self.tmp / "humboldt-demo")],
                                 cwd=REPO, capture_output=True, text=True, timeout=60)
        self.assertEqual(refused.returncode, 3, refused.stdout + refused.stderr)
        self.assertIn("glossary.json", refused.stdout + refused.stderr)


if __name__ == "__main__":
    unittest.main()
