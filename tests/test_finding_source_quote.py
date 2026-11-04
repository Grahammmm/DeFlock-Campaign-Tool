"""DF1952-08: optional ``sources[].quote`` (a short passage the campaign transcribed from the cited record).

Synthetic content only: temporary copies of the fictional example; no network, no real records. Nothing here checks a
quote against record text: the feature only stores, shows, exports and review-binds what a person typed.

Run from the repository root: python3 -m unittest tests.test_finding_source_quote
"""
import copy
import json
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import findings, finding_diff, labels  # noqa: E402
from campaign_tool.cli import finding_preview  # noqa: E402
from campaign_tool.content import ContentError, load_content, load_finding  # noqa: E402
from campaign_tool.site import PUBLIC_SOURCE_FIELDS, build_site, e  # noqa: E402
from tests import test_findings_publish as publish_tests  # noqa: E402  (module import: not re-collected here)

EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
SCHEMA = json.loads((REPO_ROOT / "schemas" / "findings-open-data.schema.json").read_text(encoding="utf-8"))
SLUG = "cedar-policy-posted"
CAFE, BEEF, GONE = "cafe" * 16, "beef" * 16, "d00d" * 16
TODAY = date(2026, 3, 1)
EXCERPT_CANARY = "SYNTHETIC-EXCERPT-TEXT"
TRICKY = '  **bold** <b>x</b> & "q" \'s\' é日本 [link](http://x.invalid)  '


def quoted_sources(first="First passage as written.", second=TRICKY, third="Third passage."):
    """cafe twice (different locators, so a repeated sha keeps its own quote) and beef with a page."""
    return [{"sha256": CAFE, "locator": "page:1", "title": "Synthetic Cedar Police ALPR policy", "quote": first},
            {"sha256": BEEF, "locator": "page:1", "title": "Synthetic vendor excerpt", "page": 1, "quote": second},
            {"sha256": CAFE, "locator": "page:2", "title": "Synthetic Cedar Police ALPR policy", "quote": third}]


class QuoteCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-source-quote-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.content, self.public = self.root / "content", self.root / "public"
        self.path = self.content / "findings" / f"{SLUG}.json"
        self.original = json.loads(self.path.read_text(encoding="utf-8"))

    def document(self, sources):
        return dict(self.original, sources=sources)

    def set_sources(self, sources):
        self.path.write_text(json.dumps(self.document(sources)), encoding="utf-8")

    def build(self):
        build_site(self.root, today=TODAY)
        return self.tree()

    def tree(self):
        return {p.relative_to(self.public).as_posix(): p.read_bytes() for p in self.public.rglob("*") if p.is_file()}

    def read(self, name):
        return (self.public / name).read_text(encoding="utf-8")

    def cli(self, *arguments):
        return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", *map(str, arguments)],
                              cwd=REPO_ROOT, capture_output=True, text=True, timeout=120)

    def refusal(self, sources):
        with self.assertRaises(ContentError) as caught:
            load_finding(self.document(sources), "findings/x.json")
        return str(caught.exception)


class QuoteShapeTests(QuoteCase):
    def test_quote_shape_bounds_controls_and_locator(self):
        keep = ["x", "y" * 400, "  outer spaces  ", "<b>&amp;</b> **md**", "é日本\u00a0", "\u2028edge", 'a "q" \'s\''] 
        for quote in keep:
            with self.subTest(accepted=quote[:12]):
                [source] = load_finding(self.document([dict(quoted_sources()[0], quote=quote)]), "f.json")["sources"]
                self.assertEqual(source["quote"], quote)  # never stripped, collapsed, normalized or truncated
        bad = {"empty": "", "blank": "   ", "nbsp only": "\u00a0 ", "401": "z" * 401, "none": None, "number": 5,
               "bool": True, "list": ["a"], "dict": {"a": 1}, "tab": EXCERPT_CANARY + "\tx", "newline": EXCERPT_CANARY + "\nx",
               "cr": EXCERPT_CANARY + "\rx", "nul": EXCERPT_CANARY + "\x00x", "del": EXCERPT_CANARY + "\x7f", "c1": EXCERPT_CANARY + "\x85",
               "bidi": EXCERPT_CANARY + "\u202e", "zero width": EXCERPT_CANARY + "\u200b", "bom": EXCERPT_CANARY + "\ufeff"}
        for name, quote in bad.items():
            with self.subTest(refused=name):
                message = self.refusal([quoted_sources()[0], dict(quoted_sources()[1], quote=quote)])
                self.assertIn("findings/x.json.sources[1].quote", message)
                self.assertNotIn(EXCERPT_CANARY, message)  # the diagnostic names the key, never the excerpt
        # The locator rule stays for every source, with or without a quote or page.
        no_locator = {"sha256": CAFE, "title": "T", "quote": "Present.", "page": 1}
        self.assertIn("locator", self.refusal([no_locator]))  # also named by the review blockers, before any key check
        for locator in ("", "   "):
            self.assertIn("locator", self.refusal([dict(no_locator, locator=locator)]))
        for page in (0, 100001, True, 1.0, "1"):
            self.assertIn("sources[0].page", self.refusal([dict(no_locator, locator="page:1", page=page)]))
        # Absent stays absent: no default, no null key.
        [plain] = load_finding(self.document([{"sha256": CAFE, "locator": "page:1"}]), "f.json")["sources"]
        self.assertNotIn("quote", plain)

    def test_loader_content_check_and_preview_agree(self):
        self.set_sources([quoted_sources()[0], dict(quoted_sources()[1], quote=EXCERPT_CANARY + "\nsecond line")])
        with self.assertRaises(ContentError) as caught:
            load_content(self.root)
        self.assertIn("sources[1].quote", str(caught.exception))
        check = self.cli("content-check", "--directory", self.root)
        self.assertNotEqual(check.returncode, 0)
        self.assertIn("sources[1].quote", check.stdout + check.stderr)
        self.assertNotIn(EXCERPT_CANARY, check.stdout + check.stderr)
        draft = dict(self.document(copy.deepcopy(quoted_sources()[:2])), state="draft", published_at=None, corrections=[])
        draft["sources"][1]["quote"] = EXCERPT_CANARY + "\ttab"
        drafts = self.root / "private" / "drafts"
        drafts.mkdir(parents=True)
        (drafts / "d.json").write_text(json.dumps(draft), encoding="utf-8")
        preview = self.cli("finding", "preview", drafts / "d.json", "--directory", self.root, "--out", self.base / "out")
        self.assertEqual(preview.returncode, 2)
        self.assertTrue(preview.stderr.startswith("Stopped: "), preview.stderr)
        self.assertIn("sources[1].quote", preview.stderr)
        self.assertNotIn(EXCERPT_CANARY, preview.stderr)
        self.assertFalse((self.base / "out").exists())


class QuoteRenderTests(QuoteCase):
    def li_blocks(self, html):
        return re.findall(r"<li>(.*?)</li>", html, re.S)

    def test_verbatim_plain_text_and_page_attribution(self):
        self.set_sources(quoted_sources())
        self.build()
        note = "Transcribed by the campaign; not checked against the record text."
        finding, packet = self.read(f"findings/{SLUG}.html"), self.read("findings/packet-all.html")
        for name, html in (("finding", finding), ("packet", packet)):
            with self.subTest(name):
                self.assertEqual(html.count(note), 3)
                self.assertEqual(html.count("<blockquote"), 3)
                self.assertIn(f"<p>{e(TRICKY)}</p></blockquote>", html)  # outer spaces kept, escaped exactly once
                for inert in ("<b>x</b>", "<strong>bold", "<a href=\"http://x.invalid\">"):
                    self.assertNotIn(inert, html)
                self.assertNotIn("&amp;amp;", html)
                blocks = [b for b in self.li_blocks(html) if "<blockquote" in b]
                self.assertEqual(len(blocks), 3)  # one per source, in source order, repeated sha kept apart
                self.assertEqual([("First passage as written." in b, e(TRICKY) in b, "Third passage." in b)
                                  for b in blocks], [(True, False, False), (False, True, False), (False, False, True)])
                self.assertEqual([("Page 1" in b) for b in blocks], [False, True, False])  # only the cited page
        first = next(b for b in self.li_blocks(finding) if "<blockquote" in b)
        self.assertLess(first.index("From a document we hold"), first.index("<blockquote"))  # under its source line
        self.assertEqual(finding.count('class="source-quote-note">' + note), 3)

    def test_optional_export_schema_and_privacy_guard(self):
        legacy = json.loads(self.build()["data/findings.json"])["findings"][0]
        self.assertTrue(all("quote" not in source for source in legacy["sources"]))
        self.set_sources(quoted_sources())
        exported = json.loads(self.build()["data/findings.json"])["findings"][0]["sources"]
        self.assertEqual([s["quote"] for s in exported], [q["quote"] for q in quoted_sources()])
        self.assertEqual([s.get("page") for s in exported], [None, 1, None])
        self.assertTrue(all(set(s) <= set(PUBLIC_SOURCE_FIELDS) for s in exported))
        self.assertNotIn("sha256", exported[0])
        properties = SCHEMA["$defs"]["source"]["properties"]
        self.assertEqual(properties["quote"], {"type": "string", "minLength": 1, "maxLength": 400})
        self.assertNotIn("quote", SCHEMA["$defs"]["source"]["required"])
        self.assertEqual(set(properties), set(PUBLIC_SOURCE_FIELDS))
        self.assertEqual(SCHEMA["properties"]["schema_version"]["const"], 1)
        self.assertIs(SCHEMA["$defs"]["source"]["additionalProperties"], False)
        self.assertNotIn("synthetic-author", self.read("data/findings.json"))
        # A quote holding a 40+ hex run refuses the build before anything changes (never redacted or truncated).
        before = self.tree()
        self.set_sources([dict(quoted_sources()[0], quote="Digest " + "ab" * 20 + " as printed.")])
        with self.assertRaisesRegex(ContentError, "full hash"):
            build_site(self.root, today=TODAY)
        self.assertEqual(self.tree(), before)
        self.assertEqual(sorted(p.name for p in self.root.iterdir() if p.name.startswith(".public")), [])

    def test_dependency_links_copy_caveats_and_preview(self):
        sources = quoted_sources()
        sources.append({"sha256": GONE, "locator": "page:9", "title": "Uncatalogued", "quote": "Uncatalogued passage."})
        self.set_sources(sources)
        files = self.build()
        finding = self.read(f"findings/{SLUG}.html")
        self.assertIn('<a href="/sources/cafecafecafe.html">Record no. <code>cafecafecafe</code></a>', finding)
        self.assertIn('<a href="/sources/beefbeefbeef.html">', finding)
        self.assertNotIn("/sources/d00dd00dd00d", finding)  # not catalogued: no record page link
        self.assertIn("Not yet listed in the source library.", finding)
        self.assertIn("Open the public copy (page 1)", finding)  # the page link survives beside the quote
        self.assertIn("The original is kept privately.", finding)
        self.assertIn("Uncatalogued passage.", finding)
        self.assertEqual([name for name, data in files.items() if name.startswith("sources/") and b"<blockquote" in data], [])
        self.assertNotIn(b"First passage", files["sources.html"])
        # Preview renders the same blockquotes and stays noindex.
        draft = dict(self.document(sources[:2]), state="draft", published_at=None, corrections=[])
        drafts = self.root / "private" / "drafts"
        drafts.mkdir(parents=True)
        (drafts / "d.json").write_text(json.dumps(draft), encoding="utf-8")
        out = self.base / "out"
        finding_preview(self.root, drafts / "d.json", out, now=datetime(2026, 3, 1, tzinfo=timezone.utc))
        page = (out / "findings" / f"{SLUG}.html").read_text(encoding="utf-8")
        self.assertIn('<meta name="robots" content="noindex">', page)
        self.assertEqual(page.count("<blockquote"), 2)
        self.assertIn(f"<p>{e(TRICKY)}</p></blockquote>", page)

    def test_absent_quote_whole_tree_byte_identical(self):
        plain_files = self.build()
        for token in (b"<blockquote", b"source-quote", b"Transcribed by the campaign", b'"quote"'):
            self.assertEqual([n for n, data in plain_files.items() if token in data], [])
        [source] = load_content(self.root, TODAY)["findings"][0]["sources"][:1]
        self.assertNotIn("quote", source)
        # Adding quotes changes only the finding page, the packet and the open-data file.
        self.set_sources([dict(s, quote="Passage.") for s in self.original["sources"]])
        before_inputs = {p.relative_to(self.root).as_posix(): p.read_bytes()
                         for p in self.root.rglob("*") if p.is_file() and "public" not in p.relative_to(self.root).parts}
        with mock.patch.object(socket.socket, "connect", side_effect=AssertionError("network")):
            quoted_files = self.build()
        changed = sorted(n for n in set(plain_files) | set(quoted_files) if plain_files.get(n) != quoted_files.get(n))
        self.assertEqual(changed, ["data/findings.json", f"findings/{SLUG}.html", "findings/packet-all.html"])
        after_inputs = {p.relative_to(self.root).as_posix(): p.read_bytes()
                        for p in self.root.rglob("*") if p.is_file() and "public" not in p.relative_to(self.root).parts}
        self.assertEqual(after_inputs, before_inputs)  # a build rewrites no legacy file and reads no private kit

    def test_label_overrides_and_no_source_matching(self):
        self.set_sources(quoted_sources())
        (self.content / "labels.json").write_text(json.dumps({"schema_version": 1, "labels": {
            "finding.quote_transcribed": "XNOTEX", "finding.quote_page": "XPAGEX {page}"}}), encoding="utf-8")
        self.build()
        for html in (self.read(f"findings/{SLUG}.html"), self.read("findings/packet-all.html")):
            self.assertEqual(html.count("XNOTEX"), 3)
            self.assertEqual(html.count("XPAGEX 1"), 1)
            self.assertNotIn("Transcribed by the campaign", html)
        spanish = json.loads((REPO_ROOT / "examples" / "labels" / "es.json").read_text(encoding="utf-8"))
        spanish = spanish.get("labels", spanish)
        for key in ("finding.quote_transcribed", "finding.quote_page"):
            self.assertEqual(set(re.findall(r"\{(\w+)\}", spanish[key])), set(re.findall(r"\{(\w+)\}", labels.DEFAULTS[key])))
            self.assertNotEqual(spanish[key], labels.DEFAULTS[key])
        self.assertIn("{page}", spanish["finding.quote_page"])


class QuoteDiffTests(unittest.TestCase):
    def test_diff_names_quote_without_echoing_excerpt(self):
        def doc(*sources):
            return {"sources": [dict({"sha256": "a" * 64, "locator": "page:1"}, **s) for s in sources]}
        def changed(old, new):
            [entry] = finding_diff.diff(old, new)
            self.assertEqual(entry["field"], "sources")
            self.assertEqual((entry["added"], entry["removed"]), ([], []))
            return entry["changed"]
        for old, new in ((doc({}), doc({"quote": EXCERPT_CANARY})), (doc({"quote": EXCERPT_CANARY}), doc({"quote": EXCERPT_CANARY + " "})),
                         (doc({"quote": EXCERPT_CANARY}), doc({}))):
            with self.subTest(old=old, new=new):
                rows = changed(old, new)
                self.assertEqual(rows, [{"sha256": "a" * 12, "keys": ["quote"]}])
                self.assertNotIn(EXCERPT_CANARY, json.dumps(finding_diff.diff(old, new)))
        self.assertEqual(finding_diff.diff(doc({}), doc({})), [])  # legacy source: no spurious diff
        self.assertEqual(finding_diff.diff(doc({"quote": EXCERPT_CANARY}), doc({"quote": EXCERPT_CANARY})), [])
        # Repeated sha: a quote on only the second source is named, and a count change without quotes is not.
        self.assertEqual(changed(doc({}, {"locator": "page:2"}), doc({}, {"locator": "page:2", "quote": EXCERPT_CANARY})),
                         [{"sha256": "a" * 12, "keys": ["quote"]}])
        self.assertEqual(changed(doc({}), doc({}, {"locator": "page:2"}))[0]["keys"], ["count", "locator", "title", "rule_id"])
        # Locator, title and rule edits still report alongside a quote edit.
        old, new = doc({"title": "T", "rule_id": "R"}), doc({"locator": "page:3", "title": "U", "rule_id": "S", "quote": "q"})
        self.assertEqual(changed(old, new)[0]["keys"], ["locator", "title", "rule_id", "quote"])


class QuoteReviewTests(unittest.TestCase):
    """Review binding: the quote belongs to the reviewed content, so any change needs fresh approvals."""
    _p = vars(publish_tests.FindingPublishTests)
    setUp_campaign, cli, register, write_draft, receipt, approve_all, log_lines = (
        _p["setUp"], _p["cli"], _p["register"], _p["write_draft"], _p["receipt"], _p["approve_all"], _p["log_lines"])
    LATER = datetime(2026, 3, 10, 12, 0, 0, tzinfo=timezone.utc)

    def setUp(self):
        self.setUp_campaign()
        self.approve_all()
        findings.publish(self.root, self.draft, "Pat Organizer", now=publish_tests.NOW)
        self.path = self.content / "findings" / "synthetic-title.json"

    def quoted(self, quote="Quoted passage.", **changes):
        document = publish_tests.draft(**changes)
        if quote is not None:
            document["sources"][0]["quote"] = quote
        return document

    def correct_from(self, document, approve):
        path = self.base / "corrected.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        if approve:
            self.approve_all(document=document)
        return findings.correct(self.root, "syn-pub-1", "2026-03-06", "Added a transcribed passage.", path,
                                now=self.LATER, approved_by="Pat Organizer")

    def test_hash_and_reviewed_correction(self):
        hashes = [findings.reviewed_hash(self.quoted(q)) for q in (None, "Quoted passage.", "Quoted passage. ", "Other passage.")]
        self.assertEqual(len(set(hashes)), 4)  # add, one trailing space, edit: each is a different reviewed content
        self.assertEqual(findings.reviewed_hash(self.quoted(None)), findings.reviewed_hash(publish_tests.draft()))  # remove
        published = dict(self.quoted(), state="published", published_at="2026-03-04T05:06:07Z",
                         updated_at="2026-03-05T00:00:00Z", corrections=[{"date": "2026-03-05", "note": "n"}])
        self.assertEqual(findings.reviewed_hash(published), findings.reviewed_hash(self.quoted()))  # metadata alone
        # Receipts given to the quote-free text no longer cover a quoted (or edited, or removed) text.
        for label, document in (("add", self.quoted()), ("space", self.quoted("Quoted passage. "))):
            with self.subTest(label):
                before, lines = self.path.read_bytes(), self.log_lines()
                with self.assertRaises(ValueError) as caught:
                    self.correct_from(document, approve=False)
                self.assertIn("review blockers", str(caught.exception))
                self.assertEqual((self.path.read_bytes(), self.log_lines()), (before, lines))  # failed correction writes nothing
        self.assertEqual(self.correct_from(self.quoted(), approve=True), {"corrected": "synthetic-title", "corrections": 1})
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["sources"][0]["quote"], "Quoted passage.")
        entry = json.loads(self.log_lines()[-1])
        self.assertEqual((entry["action"], entry["text_changed"], len(self.log_lines())), ("correct", True, 2))
        # Approvals bind the exact text: those given to "Quoted passage." cover neither a one-space edit nor a twin without it.
        twin = dict(self.quoted(summary="Another summary."))
        receipts = findings._load_receipts(findings._receipts_path(self.root, twin))
        blockers = lambda document: findings._bound_blockers(self.root, document, receipts)  # noqa: E731
        self.approve_all(document=self.quoted("Other passage.", summary="Another summary."))
        receipts = findings._load_receipts(findings._receipts_path(self.root, twin))
        self.assertEqual(blockers(self.quoted("Other passage.", summary="Another summary.")), [])
        for label, document in (("space", self.quoted("Other passage. ", summary="Another summary.")),
                                ("remove", self.quoted(None, summary="Another summary."))):
            with self.subTest(label, after="approval"):
                self.assertIn("missing_factual_review", blockers(document))


if __name__ == "__main__":
    unittest.main()
