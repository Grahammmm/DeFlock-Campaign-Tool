"""DF0022-01: optional content/faq.json -> /faq.html with finding, law-rule and glossary citations; refusals by position.

Builds temporary copies of the fictional example with synthetic questions; sockets are blocked, nothing is fetched.
"""
import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from campaign_tool import content as content_module
from campaign_tool import law
from campaign_tool.content import ContentError, load_content, load_faq, read_faq
from campaign_tool.content_check import content_problems, problem_record, run_cli
from campaign_tool.legal_claims import faq_notes, site_notes
from campaign_tool.site import build_site
from campaign_tool.site_check import check_accessibility, check_links

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
TODAY = date(2026, 10, 1)
FINDING_ID, FINDING_SLUG = "fnd_0000000000000001", "cedar-policy-posted"
RULE = "ca-civ-1798.90.5-definitions"
NO_SOCKETS = mock.patch("socket.socket", side_effect=AssertionError("network access attempted"))
FOOTER_FAQ = '<p><a href="/faq.html">FAQ</a></p>'

# sha256 of every file the fictional example renders (today=2026-10-01, no faq.json) on origin/main daa479ec, which
# already holds the glossary and source record pages: captured from an untouched detached worktree of that revision with
# render-and-hash, never from this candidate. A later change to the generated site re-captures this table.
# Camera GeoJSON digest refreshed from untouched origin/main 6daa8a2e4a7fe90d17b5f824170d1d7915c1ae7b;
# the complete no-FAQ rendered inventory matches that independent render byte for byte.
# Source-filter inventory and three digests refreshed from untouched origin/main
# 6123a134c44a2f60934c063e814dafd9fc68db51 at TODAY; the candidate's complete
# no-FAQ render is byte-identical to that independent main render.
# Source-filter assets also verified against untouched origin/main 9d4417c64fb35ad91b60f132bbe504ff06f0fe06;
# all no-FAQ rendered files match that independent fixed-date render, including sources/filter.js.
# Fixed-day (2026-10-01) baseline independently rendered from main 53cdb59e
# and requests head 1234c7d0: removing only the requests footer link and sitemap
# entry restores main byte-for-byte; requests.html matches the immutable PR.
# Source-filter assets and every other byte remain exact.
# DF0237-09: the fixture carries a correction, so the corrections feed link in every footer, the corrections.xml file and
# the rel=alternate link on corrections.html / the corrected finding page changed those pins (regenerated at TODAY).
BASELINE = {
    "404.html": "5eb8c84c7ecd10e8837b67d3b9b234fda5cd2d2704291cb37287fd6ecf468f19",  # pragma: allowlist secret -- public rendered-file SHA-256
    "_headers": "d8b67b1ea84d17609529363319efc124bc818dfe7a85ae14508268369223978e",  # pragma: allowlist secret -- public rendered-file SHA-256
    "about.html": "b377eacf453c9c363298c43f62dd465a833e6cc4d95e693e08fd578c8bfa2df6",  # pragma: allowlist secret -- public rendered-file SHA-256
    "agencies.html": "cb719448fe3eab2c74cbc928090ce5c1a332dd64263a155bdac15caf49484344",  # pragma: allowlist secret -- public rendered-file SHA-256
    "agencies/cedar.html": "ec9acef0d6d9ac1e16d5cf5db6b2daeb9b27e0bcb1fcdf41b4331efde0f860a4",  # pragma: allowlist secret -- public rendered-file SHA-256
    "agency-cards.js": "95d2218da220b6ec4ecd303342c0aaa090f5ea4999b0ffd05b3454c46719ca00",  # pragma: allowlist secret -- public rendered-file SHA-256
    "app.js": "ab63767de14fee8703a11531e24ebcfc4a8b845f986208772f242ef8d447db9e",  # pragma: allowlist secret -- public rendered-file SHA-256
    "cameras.html": "77aaa07572b28a1a0ad261f398c00e14167bf6adf47f759099eb1f2f9e16756e",  # pragma: allowlist secret -- public rendered-file SHA-256
    "corrections.html": "3d405e1b1c0fa06bccea3a73c8506db6bf4e29aa9101add090ebd6f11093158a",  # pragma: allowlist secret -- public rendered-file SHA-256
    "corrections.xml": "23f51eff261d70f0837a0dcb1c463558e1420602c32b91764b3b4abef963f0ac",  # pragma: allowlist secret -- public rendered-file SHA-256
    "data/agencies.csv": "de226f37600fdaac36bacba18473551b0a724fa89dcd3c8a743a7442cf1bb477",  # pragma: allowlist secret -- public rendered-file SHA-256
    "data/agencies.json": "9c3dd727cbd33c726007e50c84daab6bd9d38cffa53e4941aefdff67a707f7bb",  # pragma: allowlist secret -- public rendered-file SHA-256
    "data/cameras.geojson": "5f3c2ce066666761f6c940d1c2e1c33ccd8cc5275df5431084c1bfb7a4cdc146",  # pragma: allowlist secret -- public rendered-file SHA-256
    "data/city-boundaries.geojson": "68593cd8443c2ec42b01e4153cf28386727a9486503eccb5dc42ea1961511f19",  # pragma: allowlist secret -- public rendered-file SHA-256
    "data/findings.csv": "c0b05563d22729245783a2fc5953ddc60ed6defa42bc45556f4721f8d6d5880d",  # pragma: allowlist secret -- public rendered-file SHA-256
    "data/findings.json": "09d948c3ea4c0e3501f9f5b686f576c42c3f8c720dfb5ab8fcf61440396cc2b7",  # pragma: allowlist secret -- public rendered-file SHA-256
    "data/meetings.csv": "561e8d71686d8f37308263f983360308d381f2d39484997a99525a3cff5f49ae",  # pragma: allowlist secret -- public rendered-file SHA-256
    "data/meetings.json": "7a024cb7b2a582f5095553011fbc7062e6807a0c75cdb14336a58019959405ee",  # pragma: allowlist secret -- public rendered-file SHA-256
    "data/sources.csv": "74580b77eadf3e2b12b77074094208e73aeed7e67d3733dc6dad00d0f1cb0eb3",  # pragma: allowlist secret -- public rendered-file SHA-256
    "data/sources.json": "be46346e4e80de79575cc5250b27a921b329f22ffd8dd3a81b18a4a1bc603caf",  # pragma: allowlist secret -- public rendered-file SHA-256
    "feed.xml": "e6c4da912d1ea1bee41a34396d0dfb0187aeed960758830ad033f74afcbabf54",  # pragma: allowlist secret -- public rendered-file SHA-256
    "findings/agency/cedar.xml": "555f536dddc3d3c6134c4b69ce281cc387f75efafbdb1d2ea0dc1dc9fda45414",  # pragma: allowlist secret -- public rendered-file SHA-256
    "findings/cedar-policy-posted.html": "0a5f930151bdcd35fe43ef1ba7c7b206604f1c53d59832f42c8923707c94c8b5",  # pragma: allowlist secret -- public rendered-file SHA-256
    "findings/cite.js": "13f4d137f53aaa98bcc1cf1662769871f5629dda97e5700cf97727fca110cb47",  # pragma: allowlist secret -- public rendered-file SHA-256
    "findings/index.html": "05642a4a14a1a8d798b21348d854ceba05ef5436388f862b5e44f37930782a7e",  # pragma: allowlist secret -- public rendered-file SHA-256
    "findings/packet-all.html": "aefc032fb70a0ec4ca261613a994974344a973356cd216f4c9ec5d22822db5bd",  # pragma: allowlist secret -- public rendered-file SHA-256
    "index.html": "e6aaf6d5b016fd3a50d045729d17279b31f7519f75d9894edd37d77cbdbd88d3",  # pragma: allowlist secret -- public rendered-file SHA-256
    "meetings.html": "c2f3ef307735205c905069ca60e3b1f63a4aca94ce4d8af9c9b83bc2c8a007a9",  # pragma: allowlist secret -- public rendered-file SHA-256
    "meetings.ics": "dd19281997628992288aaccd1a377c0b0b41c72d2110b15a641cd7a813191ae5",  # pragma: allowlist secret -- public rendered-file SHA-256
    "meetings.xml": "2af1fa8b795edc83edc9c854e689411875b401b940c3cc99569311f182b41e5e",  # pragma: allowlist secret -- public rendered-file SHA-256
    "meetings/cedar-council-2026-10.html": "640d75cf3cec9f471078173a482381f804558c7a998ecdcdda87a72b2e6b399a",  # pragma: allowlist secret -- public rendered-file SHA-256
    "methods.html": "6ec5efb7e8e4d695c9060c6dcd36a759079e5af71c5172827bd17caa3e855361",  # pragma: allowlist secret -- public rendered-file SHA-256
    "privacy.html": "e412ab86bd77ca97bbb82e15e263283f488ebf603cca945a61f45868592169e5",  # pragma: allowlist secret -- public rendered-file SHA-256
    "requests.html": "fdae59bb65fb799a7615dda9a8886c322190de29c570aaa54345cc1da63c689f",  # pragma: allowlist secret -- public rendered-file SHA-256
    "robots.txt": "f520ce90da728c1ea72afb9c9f22a0976b5e2c16ac6aa7256e1218479b4f2b80",  # pragma: allowlist secret -- public rendered-file SHA-256
    "search.html": "3d060142f8d238965b563e82334e838055d07d51f3d3816427235621d82068fb",  # pragma: allowlist secret -- public rendered-file SHA-256
    "search.js": "0f6185d1995caa68bb3292cf15417996ee917bf5b3c48c81d05cbd057d0da1f6",  # pragma: allowlist secret -- public rendered-file SHA-256
    "site-data.js": "992aa92b3f544c4deaba28d2661f8b9ed995939a7da592540f269f0fb0a23e7e",  # pragma: allowlist secret -- public rendered-file SHA-256
    "sitemap.xml": "13968bbc2dd16a0d1295c61187ba2b7fd519a9687ab4503f94864e4627f28bb1",  # pragma: allowlist secret -- public rendered-file SHA-256
    "sources.html": "62c41a86e40437808f4146f1ff2bae3e2b92b17c2c957616c60984060062f31c",  # pragma: allowlist secret -- public rendered-file SHA-256
    "sources/beefbeefbeef.html": "53bd838920950ad4fcdc1631bfaa44b62547eb8150cd9ebafd363d93a1e10b44",  # pragma: allowlist secret -- public rendered-file SHA-256
    "sources/cafecafecafe.html": "bbce024741c706b7af2f865c046649ec9b41ed66c051b08cf591cd2a64982484",  # pragma: allowlist secret -- public rendered-file SHA-256
    "sources/filter.js": "4880474e6f4e552946692df7bbadbf9341d0c383f081b873d776d1cbc1070b2a",  # pragma: allowlist secret -- public rendered-file SHA-256
    "style.css": "2dc934dbebe30b0c6c11780789a53d1a4c853f7cbbd5ff5b27cb347bed933bf4",  # pragma: allowlist secret -- public rendered-file SHA-256
    "timeline.html": "f6ed73ff842363a2b47ea6c4164c64b727cd90e8758f55246da6393b71b062bc",  # pragma: allowlist secret -- public rendered-file SHA-256
}


def entry(question="What is a retention period?", answer="How long a record is kept.", **extra):
    return {"question": question, "answer_md": answer, **extra}


def document(*entries, **extra):
    return {"schema_version": 1, "entries": list(entries), **extra}


class FaqCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-faq-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.content = self.root / "content"
        self.public = self.root / "public"
        self.path = self.content / "faq.json"
        NO_SOCKETS.start()
        self.addCleanup(NO_SOCKETS.stop)

    def put(self, value, name="faq.json"):
        path = self.content / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode("utf-8"))
        return path

    def add_finding(self, number, **changes):
        base = json.loads((self.content / "findings" / f"{FINDING_SLUG}.json").read_text(encoding="utf-8"))
        finding = dict(base, id=f"fnd_000000000000000{number}", slug=f"zq-{number}", title=f"Zq title {number}",
                       summary=f"Zq summary {number}", body_md="", **changes)
        self.put(finding, f"findings/zq-{number}.json")
        return finding

    def glossary(self):
        self.put({"schema_version": 1, "terms": [
            {"term": "Retention period", "aliases": ["data retention"], "definition_md": "How long."},
            {"term": "ALPR", "aliases": ["license plate reader"], "definition_md": "A camera."}]}, "glossary.json")

    def build(self):
        build_site(self.root, today=TODAY)
        return self.tree()

    def tree(self):
        return {p.relative_to(self.public).as_posix(): p.read_bytes() for p in sorted(self.public.rglob("*")) if p.is_file()}

    def digests(self, files):
        return {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}

    def refused(self, value, pattern):
        """build refuses with ``pattern`` and public/ keeps its bytes; content-check lists the same problem."""
        before = self.tree() if self.public.exists() else self.build()
        self.put(value)
        with self.assertRaisesRegex(ContentError, pattern) as caught:
            build_site(self.root, today=TODAY)
        self.assertEqual(self.tree(), before, "a refused build changed public/")
        problems = content_problems(self.root)
        self.assertIn(str(caught.exception), problems, "content-check does not name build's own refusal")
        return problems

    def accepted(self, value):
        self.put(value)
        self.assertEqual(content_problems(self.root), [])
        return self.build()


class PageTests(FaqCase):
    def test_order_anchors_citations_terms_footer_sitemap(self):
        self.glossary()
        self.add_finding(2)
        self.add_finding(3)
        self.put(document(
            entry("¿Qué es la «retención» & más? ALPR", "Un **camino**: ver [ley](https://example.com/x).\n\n- uno\n- dos",
                  finding_ids=["fnd_0000000000000003", FINDING_ID, "fnd_0000000000000002"], rule_id=RULE),
            entry("Which data retention applies?", "It is set by policy."),
            # "retention" ends the question and "period" starts the answer: a phrase never spans the two fields
            entry("How long is the retention", "period of a record kept?"),
            entry("Plain one?", "The License Plate Reader answer.")))
        files = self.build()
        page = files["faq.html"].decode()
        self.assertEqual(re.findall(r'<h2 id="(q-\d+)">([^<]*)</h2>', page),
                         [("q-1", "¿Qué es la «retención» &amp; más? ALPR"), ("q-2", "Which data retention applies?"),
                          ("q-3", "How long is the retention"), ("q-4", "Plain one?")])
        self.assertEqual(len(re.findall(r"<h1", page)), 1)
        ids = re.findall(r'\bid="([^"]+)"', page)
        self.assertEqual(len(ids), len(set(ids)))
        self.assertIn("<strong>camino</strong>", page)
        self.assertIn('<a href="https://example.com/x" rel="noopener noreferrer">ley</a>', page)
        first = page.split('<h2 id="q-2">')[0]
        links = re.findall(r'<li><a href="(/findings/[^"]+)">([^<]+)</a></li>', first)
        self.assertEqual(links, [("/findings/zq-3.html", "Zq title 3"), (f"/findings/{FINDING_SLUG}.html",
                                                                          "Cedar Police produced a written ALPR usage policy (fictional)"),
                                 ("/findings/zq-2.html", "Zq title 2")])  # supplied order, not date or file order
        self.assertIn("<h3>Related findings</h3>", first)
        self.assertIn(f"Law reference: Civ. Code § 1798.90.5 (<code>{RULE}</code>)", first)
        self.assertIn("Law package status: draft, not legally reviewed", first)
        self.assertNotIn("reviewed on", page)
        self.assertEqual(page.count("Related findings"), 1)  # only the entry with ids lists any
        self.assertEqual(page.count("Law reference"), 1)
        sections = page.split('<h2 id="q-')[1:]
        used = [re.findall(r'href="(/glossary\.html#term-[a-z0-9-]+)"', s) for s in sections]
        self.assertEqual(used, [["/glossary.html#term-alpr"], ["/glossary.html#term-retention-period"], [],  # alias of the question
                                ["/glossary.html#term-alpr"]])  # canonical anchor for a case-insensitive alias in the answer
        self.assertEqual(page.count("<h3>Terms used in this answer</h3>"), 3)  # q-3: no cross-boundary match, no heading
        self.assertTrue(set(re.findall(r'href="/glossary\.html#([^"]+)"', page))
                        <= set(re.findall(r'\bid="([^"]+)"', files["glossary.html"].decode())))
        for name, data in files.items():
            if name.endswith(".html"):
                body = data.decode()
                footer = re.search(r'<footer class="site-footer">(.*?)</footer>', body, re.S).group(1)
                self.assertTrue(footer.endswith(FOOTER_FAQ), name)
                self.assertEqual(footer.count("/faq.html"), 1, name)
        self.assertEqual(re.findall(r"<loc>([^<]+faq[^<]*)</loc>", files["sitemap.xml"].decode()),
                         ["https://cedar-records.example.invalid/faq.html"])
        self.assertEqual(check_links(self.public), [])
        self.assertEqual(check_accessibility(self.public), [])
        self.assertFalse((self.public / "faq.json").exists())  # the input file is never copied
        self.assertNotIn(b"**camino**", b"".join(files.values()))  # nor its raw markdown
        self.assertEqual(self.build(), files)  # deterministic

    def test_one_and_forty_entries_no_glossary_no_base_url_and_label_overrides(self):
        site = json.loads((self.content / "site.json").read_text(encoding="utf-8"))
        site["base_url"] = None
        self.put(site, "site.json")
        files = self.accepted(document(entry("Only?")))
        self.assertNotIn("sitemap.xml", files)
        self.assertNotIn("canonical", files["faq.html"].decode())
        self.assertIn(FOOTER_FAQ, files["index.html"].decode())
        self.assertNotIn("Terms used in this answer", files["faq.html"].decode())  # no glossary: none
        many = self.accepted(document(*[entry(f"Question number {n}?", f"Answer {n}.") for n in range(1, 41)]))
        self.assertEqual(re.findall(r'<h2 id="(q-\d+)">', many["faq.html"].decode()), [f"q-{n}" for n in range(1, 41)])
        self.put({"schema_version": 1, "labels": {"faq.title": "Preguntas", "footer.faq": "Preguntas aquí",
                                                  "faq.findings": "Hallazgos", "faq.terms": "Términos", "faq.rule": "Ley",
                                                  "faq.law_status": "Estado: {status}"}}, "labels.json")
        self.glossary()
        self.put(document(entry("What is ALPR?", "retention period.", finding_ids=[FINDING_ID], rule_id=RULE)))
        page = self.build()["faq.html"].decode()
        for text in ("<title>Preguntas | ", "<h3>Hallazgos</h3>", "<h3>Términos</h3>", "Ley: Civ. Code", "Estado: draft, not legally reviewed"):
            self.assertIn(text, page)
        self.assertIn('<a href="/faq.html">Preguntas aquí</a>', self.tree()["index.html"].decode())

    def test_package_status_is_shown_as_the_package_says(self):
        package = {"status": "reviewed", "reviewed_at": "2026-09-30",
                   "rules": [{"rule_id": "r1", "citation": "Test Code § 1 <b>&</b>", "sources": []}]}
        self.put(document(entry(rule_id="r1")))
        with mock.patch.object(law, "load_package", return_value=package):
            page = self.build()["faq.html"].decode()
        self.assertIn("Law reference: Test Code § 1 &lt;b&gt;&amp;&lt;/b&gt; (<code>r1</code>)", page)
        self.assertIn("Law package status: reviewed on 2026-09-30", page)
        self.assertNotIn("not legally reviewed", page)

    def test_escaping_and_unsupported_markdown_stay_text(self):
        self.put(document(entry("Amp & 'quote' \"dq\"?", "Image: ![alt](/logo.png)\n\n[bad](http://example.com) **x**")))
        page = self.build()["faq.html"].decode()
        self.assertIn('<h2 id="q-1">Amp &amp; &#x27;quote&#x27; &quot;dq&quot;?</h2>', page)
        self.assertNotIn("<img", page.split('id="content"')[1])  # images are not resolved
        self.assertNotIn("http://example.com", page)  # a non-https link degrades to its text


class SchemaTests(FaqCase):
    def test_schema_boundaries_and_duplicate_positions(self):
        self.accepted(document(entry("q" * 160, "a" * 1500)))
        self.accepted(document(entry("q", "a")))
        cases = [
            (document(), r"faq\.json\.entries: must be a list of 1 to 40 entries"),
            (document(*[entry(f"Q {n}") for n in range(41)]), r"faq\.json\.entries: must be a list of 1 to 40 entries"),
            ({"schema_version": 1, "entries": None}, r"faq\.json\.entries: must be a list"),
            ({"schema_version": 1, "entries": {}}, r"faq\.json\.entries: must be a list"),
            ({"schema_version": 1}, r"faq\.json: missing field\(s\) \['entries'\]"),
            ({"entries": [entry()]}, r"faq\.json: missing field\(s\) \['schema_version'\]"),
            ({"schema_version": 2, "entries": [entry()]}, r"faq\.json\.schema_version: must be 1"),
            ({"schema_version": True, "entries": [entry()]}, r"faq\.json\.schema_version: must be 1"),
            ({"schema_version": 1.0, "entries": [entry()]}, r"faq\.json\.schema_version: must be 1"),
            ({"schema_version": 1, "entries": [entry()], "notice_md": "x"}, r"faq\.json: unknown field\(s\) \['notice_md'\]"),
            ([entry()], r"faq\.json: expected an object"),
            (None, r"faq\.json: expected an object"),
            (document("text"), r"entries\[0\]: expected an object"),
            (document(None), r"entries\[0\]: expected an object"),
            (document({"question": "Q?"}), r"entries\[0\]: missing field\(s\) \['answer_md'\]"),
            (document({"answer_md": "A"}), r"entries\[0\]: missing field\(s\) \['question'\]"),
            (document(entry(), entry(extra=1)), r"entries\[1\]: unknown field\(s\) \['extra'\]"),
            (document(entry(""), ), r"entries\[0\]\.question: must be trimmed plain single-line text of 1-160"),
            (document(entry("q" * 161)), r"entries\[0\]\.question: must be trimmed plain single-line text of 1-160"),
            (document(entry(" padded")), r"entries\[0\]\.question: must be trimmed"),
            (document(entry("padded ")), r"entries\[0\]\.question: must be trimmed"),
            (document(entry("   ")), r"entries\[0\]\.question: must be trimmed"),
            (document(entry(None)), r"entries\[0\]\.question: must be trimmed"),
            (document(entry(True)), r"entries\[0\]\.question: must be trimmed"),
            (document(entry(["Q?"])), r"entries\[0\]\.question: must be trimmed"),
            (document(entry("two\nlines")), r"entries\[0\]\.question: must be trimmed"),
            (document(entry("tab\there")), r"entries\[0\]\.question: must be trimmed"),
            (document(entry("bell\x07")), r"entries\[0\]\.question: must be trimmed"),
            (document(entry("zero\u200bwidth")), r"entries\[0\]\.question: must be trimmed"),
            (document(entry("bidi\u202eflip")), r"entries\[0\]\.question: must be trimmed"),
            (document(entry("sep\u2028arator")), r"entries\[0\]\.question: must be trimmed"),
            (document(entry("para\u2029graph")), r"entries\[0\]\.question: must be trimmed"),
            (document(entry("Is <b>this</b> html?")), r"entries\[0\]\.question: must be trimmed"),
            (document(entry("a > b?")), r"entries\[0\]\.question: must be trimmed"),
            (document(entry(answer="")), r"entries\[0\]\.answer_md: must be non-blank plain text of at most 1500"),
            (document(entry(answer=" \n\t ")), r"entries\[0\]\.answer_md: must be non-blank"),
            (document(entry(answer="a" * 1501)), r"entries\[0\]\.answer_md: must be non-blank plain text of at most 1500"),
            (document(entry(answer=None)), r"entries\[0\]\.answer_md: must be non-blank"),
            (document(entry(answer=5)), r"entries\[0\]\.answer_md: must be non-blank"),
            (document(entry(answer="a\x00b")), r"entries\[0\]\.answer_md: must be non-blank"),
            (document(entry(answer="<script>x</script>")), r"entries\[0\]\.answer_md: must be non-blank"),
            (document(entry("Same?"), entry("Other?"), entry("same?")), r"faq\.json\.entries\[2\]\.question: duplicates entries\[0\]\.question"),
            (document(entry("Caf\u00e9?"), entry("Cafe\u0301?")), r"faq\.json\.entries\[1\]\.question: duplicates entries\[0\]\.question"),
            (document(entry("Is   it  here?"), entry("x"), entry("is it\u00a0here?")),
             r"faq\.json\.entries\[2\]\.question: duplicates entries\[0\]\.question"),
            (document(entry("Straße?"), entry("STRASSE?")), r"faq\.json\.entries\[1\]\.question: duplicates entries\[0\]\.question"),
            (document(entry("A?", finding_ids=None)), r"entries\[0\]\.finding_ids: must be a list of at most 3"),
            (document(entry("A?", finding_ids=FINDING_ID)), r"entries\[0\]\.finding_ids: must be a list of at most 3"),
            (document(entry("A?", finding_ids={})), r"entries\[0\]\.finding_ids: must be a list of at most 3"),
            (document(entry("A?", finding_ids=[FINDING_ID] * 4)), r"entries\[0\]\.finding_ids: must be a list of at most 3"),
            (document(entry("A?", finding_ids=[5])), r"entries\[0\]\.finding_ids\[0\]: must be a finding id"),
            (document(entry("A?", finding_ids=[True])), r"entries\[0\]\.finding_ids\[0\]: must be a finding id"),
            (document(entry("A?", finding_ids=[None])), r"entries\[0\]\.finding_ids\[0\]: must be a finding id"),
            (document(entry("A?", finding_ids=["has space"])), r"entries\[0\]\.finding_ids\[0\]: must be a finding id"),
            (document(entry("A?", finding_ids=[""])), r"entries\[0\]\.finding_ids\[0\]: must be a finding id"),
            (document(entry("A?", finding_ids=["f" * 65])), r"entries\[0\]\.finding_ids\[0\]: must be a finding id"),
            (document(entry("A?", finding_ids=[FINDING_ID, FINDING_ID])), r"entries\[0\]\.finding_ids\[1\]: duplicate finding id"),
            (document(entry("A?"), entry("B?", finding_ids=[FINDING_SLUG])), r"entries\[1\]\.finding_ids\[0\]: not a published finding id"),
            (document(entry("A?", finding_ids=[FINDING_ID[:12]])), r"entries\[0\]\.finding_ids\[0\]: not a published finding id"),
            (document(entry("A?", finding_ids=[FINDING_ID, "fnd_0000000000000099"])),
             r"entries\[0\]\.finding_ids\[1\]: not a published finding id"),
            (document(entry("A?", finding_ids=[FINDING_ID.upper()])), r"entries\[0\]\.finding_ids\[0\]: not a published finding id"),
        ]
        for value, pattern in cases:
            with self.subTest(pattern, value=str(value)[:60]):
                problems = self.refused(value, pattern)
                for line in problems:
                    self.assertTrue(problem_record(line)["file"].startswith("faq.json"), line)
        self.assertIn("faq.html", self.accepted(document(entry("A?", finding_ids=[FINDING_ID]))))

    def test_unpublished_draft_and_withdrawn_findings_are_not_citable(self):
        draft = json.loads((self.content / "findings" / f"{FINDING_SLUG}.json").read_text(encoding="utf-8"))
        self.put(dict(draft, id="fnd_0000000000000007", slug="zq-draft", state="draft"), "findings/zq-draft.json")
        self.put(document(entry("A?", finding_ids=["fnd_0000000000000007"])))
        problems = content_problems(self.root)
        # the draft file is itself refused (only published findings are rendered); the FAQ id is not matched against it
        self.assertTrue(any(line.startswith("findings/zq-draft.json") for line in problems), problems)
        self.assertFalse(any("not a published finding id" in line for line in problems), problems)
        with self.assertRaises(ContentError):
            load_content(self.root)
        (self.content / "findings" / "zq-draft.json").unlink()  # a draft that is not in content/findings is simply unknown
        self.assertEqual(sorted(content_problems(self.root)),
                         ["faq.json.entries[0].finding_ids[0]: not a published finding id"])

    def test_loader_apis_return_ordered_entries_and_none_for_absence(self):
        self.assertIsNone(read_faq(self.root, []))
        self.put(document(entry("B?", "b"), entry("A?", "a", finding_ids=[FINDING_ID])))
        loaded = read_faq(self.root, load_content(self.root)["findings"])
        self.assertEqual([item["question"] for item in loaded], ["B?", "A?"])
        self.assertEqual(loaded[1]["finding_ids"], [FINDING_ID])
        self.assertEqual(loaded[0]["finding_ids"], [])
        self.assertNotIn("rule_id", loaded[0])
        collected = []
        self.assertIsNone(load_faq(document(entry(""), entry("same"), entry("SAME")), self.root, [], collected))
        self.assertEqual(len(collected), 2)
        self.assertEqual(load_content(self.root)["faq"], loaded)

    def test_rule_resolution_is_offline_and_exact(self):
        rule = lambda value: document(entry("A?", rule_id=value))  # noqa: E731
        self.accepted(rule(RULE))  # the shipped draft package is accepted; draft is shown as draft
        self.assertIn("draft, not legally reviewed", (self.public / "faq.html").read_text(encoding="utf-8"))
        for value, pattern in ((RULE[:-3], r"entries\[0\]\.rule_id: unknown law-package rule id"),  # no prefix matching
                               (RULE.upper(), r"unknown law-package rule id"), (RULE + " ", r"unknown law-package rule id"),
                               ("ca-no-such-rule", r"unknown law-package rule id"),
                               ("", r"entries\[0\]\.rule_id: must be a non-empty rule id of at most 120"),
                               (None, r"entries\[0\]\.rule_id: must be a non-empty rule id"),
                               (5, r"entries\[0\]\.rule_id: must be a non-empty rule id"),
                               (True, r"entries\[0\]\.rule_id: must be a non-empty rule id"),
                               ([RULE], r"entries\[0\]\.rule_id: must be a non-empty rule id"),
                               ("r" * 121, r"entries\[0\]\.rule_id: must be a non-empty rule id of at most 120")):
            with self.subTest(value=str(value)[:30]):
                self.refused(rule(value), pattern)
        self.refused(document(entry("A?"), entry("B?", rule_id="nope")), r"faq\.json\.entries\[1\]\.rule_id: unknown law-package rule id")
        config = json.loads((self.root / "campaign.json").read_text(encoding="utf-8"))
        gone = r"faq\.json\.entries\[0\]\.rule_id: no usable campaign law package"
        self.put(rule(RULE))
        (self.root / "campaign.json").write_text(json.dumps(dict(config, state="ZZ")), encoding="utf-8")  # no such package
        with self.assertRaisesRegex(ContentError, gone):
            load_content(self.root)
        self.assertEqual(content_problems(self.root), ["faq.json.entries[0].rule_id: no usable campaign law package"])
        (self.root / "campaign.json").unlink()  # no campaign.json: still a usable package is required
        with self.assertRaisesRegex(ContentError, gone):
            load_content(self.root)
        self.assertEqual(content_problems(self.root), ["faq.json.entries[0].rule_id: no usable campaign law package"])
        (self.root / "campaign.json").write_text("{not json", encoding="utf-8")  # unreadable campaign.json
        with self.assertRaisesRegex(ContentError, gone):
            load_content(self.root)
        (self.root / "campaign.json").write_text(json.dumps(config), encoding="utf-8")
        with mock.patch.object(law, "load_package", side_effect=ValueError("broken package")):  # unusable package
            with self.assertRaisesRegex(ContentError, gone):
                load_content(self.root)
        self.assertEqual(content_problems(self.root), [])

    def test_a_plain_faq_never_loads_a_law_package(self):
        self.put(document(entry("A?", finding_ids=[FINDING_ID])))
        with mock.patch.object(law, "load_package", side_effect=AssertionError("law package read")), \
                mock.patch("campaign_tool.finding_law.jurisdiction", side_effect=AssertionError("jurisdiction read")):
            self.assertEqual(content_problems(self.root), [])
            self.assertIn("faq.html", self.build())

    def test_checker_names_every_independent_problem_with_the_file(self):
        self.put(document(entry("A?", answer=""), entry("a?"), entry("B?", finding_ids=["nope"], rule_id="nope"), "text"))
        problems = content_problems(self.root)
        self.assertEqual(len(problems), 5, problems)
        self.assertEqual(problems, sorted(problems))
        for line in problems:
            record = problem_record(line)
            self.assertTrue(record["file"].startswith("faq.json"), line)
            self.assertEqual(record["file"] + ": " + record["message"], line)
        with self.assertRaises(ContentError) as caught:  # build stops at its first refusal, which the checker also lists
            load_content(self.root)
        self.assertIn(str(caught.exception), problems)
        collected = []
        self.assertIsNone(read_faq(self.root, [], collected))
        self.assertEqual(sorted(collected), problems)


class GuardTests(FaqCase):
    ACCUSE = "The agency violated SB 34 by sharing plate data."

    def cli(self, as_json):
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                run_cli(self.root, as_json=as_json)
            except SystemExit as exc:
                code = exc.code
        return out.getvalue(), err.getvalue(), code

    def test_answer_guard_is_advisory_and_json_stays_one_line(self):
        private = "ZQ-PRIVATE-SENTINEL"
        self.put(document(
            entry("A neutral question?", f"{self.ACCUSE} {private}", rule_id=RULE),  # a rule citation suppresses nothing
            entry("The agency violated SB 34. Is that so?", "The agency declined to answer. SB 34 requires a published policy."),
            entry("Quoted?", 'The article said "the agency violated SB 34" in 2024.'),
            entry("Third accusation?", "Cedar Police is out of compliance with SB 34.")))
        expected = ["review-required: faq.json.entries[0].answer_md: legal-claim wording (violation-verb)",
                    "review-required: faq.json.entries[3].answer_md: legal-claim wording (non-compliant)"]
        self.assertEqual(faq_notes(load_content(self.root)["faq"]), expected)
        self.assertEqual(site_notes(self.root), expected)  # the CLI build and the frozen runner share this scan
        files = self.build()  # flags never fail a build
        self.assertIn("faq.html", files)
        with_faq_stdout = {}
        for as_json in (False, True):
            out, err, code = self.cli(as_json)
            self.assertEqual(code, 0)
            self.assertEqual(err.splitlines(), expected)
            self.assertNotIn(private, out + err)
            self.assertNotIn("violated", out + err)
            with_faq_stdout[as_json] = out
        record = json.loads(with_faq_stdout[True])
        self.assertEqual(len(with_faq_stdout[True].splitlines()), 1)
        self.assertEqual((record["count"], record["problems"], record["truncated"]), (0, [], False))
        self.assertEqual(sorted(record), ["count", "law_package", "problems", "schema", "truncated"])
        self.assertEqual(with_faq_stdout[False].splitlines()[0], "content/: 0 problem(s)")
        self.path.unlink()  # no FAQ: no new stderr and the same stdout
        for as_json in (False, True):
            out, err, code = self.cli(as_json)
            self.assertEqual((err, code, out), ("", 0, with_faq_stdout[as_json]))
        self.assertEqual(site_notes(self.root), [])

    def test_question_wording_never_hides_or_creates_an_answer_flag(self):
        self.put(document(entry("Did the agency violate SB 34 by sharing plate data?", "It is a question for the records.")))
        self.assertEqual(site_notes(self.root), [])  # the question is not scanned as an assertion
        self.put(document(entry("Neutral?", "Records show the agency violated SB 34.")))
        self.assertEqual(len(site_notes(self.root)), 1)

    def test_invalid_faq_prints_no_notes_and_keeps_every_structural_problem(self):
        self.put(document(entry("A?", self.ACCUSE), entry("a?", self.ACCUSE)))  # duplicate: the whole file is refused
        out, err, code = self.cli(True)
        self.assertEqual((err, code), ("", 3))
        record = json.loads(out)
        self.assertEqual(record["count"], 1)
        self.assertEqual(record["problems"][0]["file"], "faq.json.entries[1].question")
        out, err, code = self.cli(False)
        self.assertEqual((err, code), ("", 3))
        self.assertEqual(out.splitlines()[0], "content/: 1 problem(s)")


class RefusalTests(FaqCase):
    def test_refusals_preserve_public_and_checker_parity(self):
        good = self.build()
        for payload, pattern in (
                (b"\xff\xfe not utf-8", r"faq\.json: not valid UTF-8 JSON"),
                (b'{"schema_version": 1,', r"faq\.json: not valid UTF-8 JSON"),
                (b"", r"faq\.json: not valid UTF-8 JSON"),
                (json.dumps({"schema_version": 1, "entries": [], "x": "x" * (2 * 1024 * 1024)}).encode(), r"faq\.json: file too large"),
                (b"[" * 200000, r"faq\.json: not valid UTF-8 JSON")):
            with self.subTest(pattern, size=len(payload)):
                self.refused(payload, pattern)
                self.assertEqual(self.tree(), good)
        self.path.unlink()
        self.put(document(entry()))
        real = content_module._read_json
        def failing(path, *args):  # noqa: E306
            if Path(path).name == "faq.json":
                raise PermissionError("denied")
            return real(path, *args)
        with mock.patch.object(content_module, "_read_json", side_effect=failing):
            with self.assertRaisesRegex(ContentError, r"^faq\.json: could not be read$"):
                load_content(self.root)
            self.assertEqual(content_problems(self.root), ["faq.json: could not be read"])
        self.path.unlink()
        elsewhere = self.content.parent / "elsewhere.json"
        elsewhere.write_text(json.dumps(document(entry())), encoding="utf-8")
        cases = ((lambda: os.symlink(elsewhere, self.path), r"^faq\.json: symlinks are not read$"),
                 (lambda: os.symlink(self.content.parent / "missing.json", self.path), r"^faq\.json: symlinks are not read$"),
                 (lambda: self.path.mkdir(), r"^faq\.json: must be a regular file, not a symlink or folder$"),
                 (lambda: os.mkfifo(self.path), r"^faq\.json: must be a regular file, not a symlink or folder$"))
        for make, pattern in cases:
            with self.subTest(pattern):
                make()
                with self.assertRaisesRegex(ContentError, pattern):
                    build_site(self.root, today=TODAY)
                self.assertEqual(len(content_problems(self.root)), 1)
                self.assertRegex(content_problems(self.root)[0], pattern)
                self.assertEqual(self.tree(), good)
                if self.path.is_dir() and not self.path.is_symlink():
                    self.path.rmdir()
                else:
                    self.path.unlink()
        self.assertNotIn("faq.html", self.build())

    def test_reserved_finding_page_and_asset_collisions(self):
        self.put(document(entry()))
        base = json.loads((self.content / "findings" / f"{FINDING_SLUG}.json").read_text(encoding="utf-8"))
        self.put(dict(base, id="fnd_0000000000000005", slug="faq"), "findings/zq-faq.json")
        with self.assertRaisesRegex(ContentError, r"findings/zq-faq\.json\.slug: 'faq' is reserved"):
            load_content(self.root)
        self.assertTrue(any("'faq' is reserved" in p for p in content_problems(self.root)))
        (self.content / "findings" / "zq-faq.json").unlink()
        self.path.unlink()  # reserved even without the file
        self.put(dict(base, id="fnd_0000000000000005", slug="faq"), "findings/zq-faq.json")
        with self.assertRaisesRegex(ContentError, r"'faq' is reserved"):
            load_content(self.root)
        (self.content / "findings" / "zq-faq.json").unlink()
        self.put({"title": "FAQ", "body_md": "Hi."}, "pages/faq.json")
        with self.assertRaisesRegex(ContentError, r"faq"):
            load_content(self.root)
        (self.content / "pages" / "faq.json").unlink()
        for name, pattern in (("faq.html", r"public-assets/faq\.html: extension not allowed"),
                              ("faq.html/x.png", r"public-assets/faq\.html/x\.png: collides with a generated path")):
            asset = self.content / "public-assets" / name
            asset.parent.mkdir(parents=True, exist_ok=True)
            asset.write_bytes(b"\x89PNG\r\n\x1a\n")
            with self.assertRaisesRegex(ContentError, pattern):
                build_site(self.root, today=TODAY)
            self.assertFalse(self.public.exists())
            shutil.rmtree(self.content / "public-assets")

    def test_multiple_independent_bad_files_are_reported_and_check_is_read_only(self):
        self.put(document(entry("A?", finding_ids=["nope"])))
        self.put({"schema_version": 1, "terms": "x"}, "glossary.json")
        self.put({"schema_version": 1, "labels": {"nav.home": 5}}, "labels.json")
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in sorted(self.content.rglob("*")) if p.is_file()}
        problems = content_problems(self.root)
        self.assertTrue(any(p.startswith("faq.json.entries[0].finding_ids[0]") for p in problems), problems)
        self.assertTrue(any(p.startswith("glossary.json") for p in problems), problems)
        self.assertTrue(any(p.startswith("labels.json") for p in problems), problems)
        self.assertEqual([run_cli_quiet(self.root, flag) for flag in (False, True)], [3, 3])
        after = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in sorted(self.content.rglob("*")) if p.is_file()}
        self.assertEqual(before, after)
        self.assertFalse(self.public.exists())


def run_cli_quiet(root, as_json):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            run_cli(root, as_json=as_json)
        except SystemExit as exc:
            return exc.code
    return 0


class AbsenceTests(FaqCase):
    def test_absent_bytes_removal_and_check_reproducibility(self):
        files = self.build()
        # DF2207-12 adds exactly two catalogue outputs, one footer paragraph and one sitemap URL.
        # Retain every pre-feature digest; normalize only those independently named additions.
        self.assertEqual(set(files) - set(BASELINE), {"data.html", "data/datapackage.json"})
        legacy = {}
        data_link = b'<p><a href="/data.html">Open data</a></p>'
        data_url = b'<url><loc>https://cedar-records.example.invalid/data.html</loc></url>'
        for name in BASELINE:
            data = files[name]
            self.assertEqual(data.count(data_link), 1 if name.endswith(".html") else 0, name)
            if name.endswith(".html"):
                data = data.replace(data_link, b"", 1)
            if name == "sitemap.xml":
                self.assertEqual(data.count(data_url), 1)
                data = data.replace(data_url, b"", 1)
            legacy[name] = data
        self.assertEqual(self.digests(legacy), BASELINE)  # original pinned bytes, not refreshed from candidate
        self.assertNotIn("faq.html", files)
        self.assertNotIn("/faq.html", b"".join(files.values()).decode("utf-8", "ignore"))
        self.assertNotIn("faq", files["sitemap.xml"].decode())
        self.put(document(entry("What is ALPR?", "A camera.", finding_ids=[FINDING_ID], rule_id=RULE)))
        with_faq = self.build()
        self.assertEqual(self.build(), with_faq)  # repeated builds: identical bytes
        self.assertEqual(set(with_faq) - set(files), {"faq.html"})
        changed = sorted(name for name in files if files[name] != with_faq[name])
        self.assertEqual(changed, sorted([n for n in files if n.endswith(".html")] + ["sitemap.xml"]))
        for name in changed:
            if name != "sitemap.xml":  # only the appended footer paragraph differs
                self.assertEqual(with_faq[name].decode().replace(FOOTER_FAQ, "", 1).encode(), files[name], name)
        self.assertEqual(with_faq["sitemap.xml"].decode().count("faq.html"), 1)
        self.assertEqual(check_links(self.public), [])
        self.assertEqual(check_accessibility(self.public), [])
        page = with_faq["faq.html"].decode()
        self.assertNotIn("<script", page)
        self.assertEqual(re.findall(r'<link rel="stylesheet"[^>]*>', page), re.findall(r'<link rel="stylesheet"[^>]*>', with_faq["about.html"].decode()))
        self.path.unlink()  # removing the file and rebuilding removes the page and every link
        self.assertEqual(self.digests(self.build()), self.digests(files))
        self.assertFalse((self.public / "faq.html").exists())

    def test_missing_content_starter_is_untouched(self):
        shutil.rmtree(self.content)  # no content/: the neutral starter, no model, no FAQ page or link, no error
        self.assertIsNone(read_faq(self.root, []))
        with contextlib.redirect_stderr(io.StringIO()):
            build_site(self.root, today=TODAY)
        starter = b"".join(p.read_bytes() for p in sorted(self.public.rglob("*")) if p.is_file())
        self.assertNotIn(b"faq", starter.lower())


if __name__ == "__main__":
    unittest.main()
