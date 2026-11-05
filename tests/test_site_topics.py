"""DF1952-06: content/topics/<slug>.json curated topic pages, finding backlinks and refusals by name.

Synthetic content only: a temporary copy of the fictional example; no network, no private records.
"""
import hashlib
import json
import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool import content as content_module
from campaign_tool.content import ContentError, load_content
from campaign_tool.content_check import content_problems, problem_record
from campaign_tool.labels import DEFAULTS, load_labels
from campaign_tool.site import build_site
from tests.test_site_pages import CLEAN_PNG

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
BASE = "https://cedar-records.example.invalid"
NEW_KEYS = ("nav.topics", "heading.topics", "title.topics", "title.topics_description", "finding.part_of")
EXAMPLE_ID = "fnd_0000000000000001"  # cedar-policy-posted: one correction, 2026-02-03


def sha(label):
    return hashlib.sha256(f"synthetic df1952-06 record {label}".encode()).hexdigest()


class TopicTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-topics-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.content = self.root / "content"
        self.public = self.root / "public"
        self.topics = self.content / "topics"
        self.base = json.loads((self.content / "findings" / "cedar-policy-posted.json").read_text())

    # -- helpers -------------------------------------------------------------
    def finding(self, slug, published_at, classification="documented_fact", event_date="2026-01-20", **fields):
        document = dict(self.base, id="fnd_" + slug.replace("-", "_"), slug=slug, title=f"Synthetic {slug}",
                        summary=f"Summary of {slug}.", published_at=published_at, classification=classification,
                        event_date=event_date, corrections=[], sources=[
                            {"sha256": sha(slug), "locator": "page:1", "title": "Synthetic record"}], **fields)
        (self.content / "findings" / f"{slug}.json").write_text(json.dumps(document), encoding="utf-8")
        return document["id"]

    def three(self):
        """Published oldest to newest: alpha, beta, gamma (the findings index lists gamma first)."""
        return [self.finding("alpha-find", "2026-02-10T12:00:00+00:00", "information_gap", "2025-05-04"),
                self.finding("beta-find", "2026-03-10T12:00:00+00:00", "redaction", "2025-06-05"),
                self.finding("gamma-find", "2026-04-10T12:00:00+00:00", "agency_assertion", "2025-07-06")]

    def topic(self, slug, finding_ids, title="Topic title", intro="Intro text.", **fields):
        self.topics.mkdir(exist_ok=True)
        document = {"title": title, "intro_md": intro, "finding_ids": finding_ids, **fields}
        path = self.topics / f"{slug}.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        return path

    def build(self):
        return {p.relative_to(self.public).as_posix() for p in build_site(self.root)}

    def tree(self):
        return {p.relative_to(self.public).as_posix(): p.read_bytes() for p in sorted(self.public.rglob("*")) if p.is_file()}

    def read(self, name):
        return (self.public / name).read_text(encoding="utf-8")

    def refused(self, pattern):
        with self.assertRaisesRegex(ContentError, pattern):
            build_site(self.root)

    def refused_keeps_public(self, pattern):
        """The build refuses by name and the public tree from the last good build is byte-for-byte unchanged."""
        before = self.tree()
        self.refused(pattern)
        self.assertEqual(before, self.tree())
        self.assertEqual([p.name for p in self.root.iterdir() if p.name.startswith(".public")], [])

    def nav(self, html):
        return re.search(r'<nav class="site-nav"[^>]*>(.*?)</nav>', html).group(1)

    def footer(self, html):
        return re.search(r'<footer class="site-footer">(.*?)</footer>', html, re.S).group(1)

    # -- 1. index, order and metadata ---------------------------------------
    def test_topic_index_and_ordered_metadata(self):
        alpha, beta, gamma = self.three()
        self.topic("zz-series", [alpha, EXAMPLE_ID, gamma], title='Series <b>& "more"</b>',
                   intro="First **bold** line.\n\n<script>alert(1)</script>")
        self.topic("aa-other", [beta], title="Other topic")
        written = self.build()
        self.assertTrue({"topics/index.html", "topics/zz-series.html", "topics/aa-other.html"} <= written)
        page = self.read("topics/zz-series.html")
        order = re.findall(r'<h3><a href="/findings/([^"]+)\.html">', page)
        self.assertEqual(order, ["alpha-find", "cedar-policy-posted", "gamma-find"])  # the topic's order, not newest first
        main = page.split('id="content"', 1)[1]
        self.assertIn("Series &lt;b&gt;&amp; &quot;more&quot;&lt;/b&gt;", main)
        self.assertNotIn("<script>alert", page)
        self.assertIn("<strong>bold</strong>", main)
        for needle in ("Information gap", "Documented fact", "Agency assertion", "Event date 2025-05-04",
                       "Corrected"):
            self.assertIn(needle, main)
        self.assertEqual(main.count("Corrected"), 1)  # only the example finding carries a correction
        self.assertIn('<time datetime="2026-02-03">', main)  # the newest correction marker, existing logic
        index = self.read("topics/index.html")
        self.assertEqual(re.findall(r'<li><a href="(/topics/[^"]+)">', index),
                         ["/topics/aa-other.html", "/topics/zz-series.html"])  # slug order, each once
        # nav: after Timeline; footer link once; every page of the site
        for name in sorted(n for n in written if n.endswith(".html")):
            html = self.read(name)
            self.assertTrue(self.nav(html).endswith('Timeline</a><a href="/topics/"'
                                                    + (' aria-current="page"' if name.startswith("topics/") else "")
                                                    + ">Topics</a>"), name)
            self.assertEqual(self.footer(html).count('href="/topics/"'), 1, name)
        # sitemap: absolute urls, the index only as /topics/, no invented lastmod for topics
        sitemap = self.read("sitemap.xml")
        self.assertIn(f"<loc>{BASE}/topics/</loc></url>", sitemap)
        self.assertIn(f"<loc>{BASE}/topics/zz-series.html</loc></url>", sitemap)
        self.assertNotIn("topics/index.html", sitemap)
        self.assertNotIn("topics/index.html", self.read("feed.xml"))
        self.assertNotIn("/topics/", self.read("feed.xml"))
        # every root-relative link on the new pages resolves inside the built tree
        for name in ("topics/index.html", "topics/zz-series.html", "topics/aa-other.html"):
            for href in re.findall(r'href="(/[^"#]*)', self.read(name)):
                target = href + "index.html" if href.endswith("/") else href
                self.assertTrue((self.public / target.lstrip("/")).exists(), f"{name}: {href}")
            self.assertEqual(self.read(name).count("<h2>"), 1, name)

    # -- 2. memberships and related ----------------------------------------
    def test_finding_multiple_memberships_preserve_related(self):
        alpha, beta, gamma = self.three()
        self.build()
        before = {slug: self.read(f"findings/{slug}.html") for slug in ("alpha-find", "beta-find", "gamma-find")}
        self.topic("m-second", [alpha, beta], title="Second & last")
        self.topic("b-first", [beta, alpha], title="First <topic>")
        self.build()
        page = self.read("findings/alpha-find.html")
        links = re.search(r"<p>Part of: (.*?)</p>", page).group(1)
        self.assertEqual(links, '<a href="/topics/b-first.html">First &lt;topic&gt;</a>, '
                                '<a href="/topics/m-second.html">Second &amp; last</a>')  # slug order, once each
        self.assertEqual(page.count("Part of:"), 1)
        # an unlisted finding differs from the earlier build only by the shared nav and footer link
        after = self.read("findings/gamma-find.html")
        self.assertNotIn("Part of:", after)
        shared = re.compile(r'<p><a href="/topics/">Topics</a></p>|<a href="/topics/"[^>]*>Topics</a>')
        self.assertEqual(shared.sub("", after), shared.sub("", before["gamma-find"]))
        # the related section (ranking, limit, output) is untouched by the new block
        def pick(html):
            return html.split('<h3 id="related">', 1)[1].split('<h3 id="corrections">', 1)[0]
        for slug in ("alpha-find", "beta-find"):
            self.assertEqual(pick(self.read(f"findings/{slug}.html")), pick(before[slug]), slug)
        self.topic("dup", [alpha, alpha])
        self.refused(r"topics/dup\.json\.finding_ids\[1\]: duplicate id 'fnd_alpha_find'")

    # -- 3. refusals by name -------------------------------------------------
    def test_unknown_unpublished_and_slug_collisions_refused_by_name(self):
        alpha, _, _ = self.three()
        self.topic("good", [alpha])
        self.build()
        (self.topics / "good.json").unlink()
        cases = []
        draft = self.root / "private" / "drafts"
        draft.mkdir(parents=True)
        (draft / "fnd_0123456789ab.json").write_text(json.dumps({"id": "fnd_0123456789ab"}), encoding="utf-8")
        withdrawn = self.root / "private" / "withdrawn"
        withdrawn.mkdir()
        (withdrawn / "gone-find.json").write_text(json.dumps({"id": "fnd_gone", "slug": "gone-find"}), encoding="utf-8")
        for slug, ids, needle in (
                ("t1", ["fnd_missing"], r"topics/t1\.json\.finding_ids\[0\]: 'fnd_missing' is not a published finding id"),
                ("t2", [alpha, "fnd_0123456789ab"], r"finding_ids\[1\]: 'fnd_0123456789ab' is not a published"),
                ("t3", ["fnd_gone"], r"finding_ids\[0\]: 'fnd_gone' is not a published"),
                ("t4", ["alpha-find"], r"'alpha-find' is not a published finding id \(use the finding id, not its slug\)"),
                ("t5", ["findings/alpha-find.json"], r"'findings/alpha-find.json' is not a published")):
            with self.subTest(slug):
                self.topic(slug, ids)
                self.refused_keeps_public(needle)
                (self.topics / f"{slug}.json").unlink()
        # a finding file that is itself a draft keeps the whole build from publishing it
        self.finding("draft-find", "2026-05-01T00:00:00+00:00", state="draft")
        self.topic("t6", ["fnd_draft_find"])
        self.refused_keeps_public(r"state")
        (self.content / "findings" / "draft-find.json").unlink()
        (self.topics / "t6.json").unlink()
        page = self.content / "pages"
        page.mkdir()
        (page / "explain.json").write_text(json.dumps({"title": "Explain", "body_md": "Text."}), encoding="utf-8")
        for slug, needle in (("explain", r"topics/explain\.json: slug explain clashes with the explainer page pages/explain\.json"),
                             ("alpha-find", r"topics/alpha-find\.json: slug alpha-find clashes with the published finding fnd_alpha_find"),
                             ("about", r"topics/about\.json: slug about is a reserved page name"),
                             ("index", r"topics/index\.json: slug index is a reserved page name"),
                             ("topics", r"topics/topics\.json: slug topics is a reserved page name"),
                             ("timeline", r"topics/timeline\.json: slug timeline is a reserved page name"),
                             ("cameras", r"topics/cameras\.json: slug cameras is a reserved page name"),
                             ("sitemap", r"topics/sitemap\.json: slug sitemap is a reserved page name"),
                             ("sharing", r"topics/sharing\.json: slug sharing is a reserved page name")):
            with self.subTest(slug):
                self.topic(slug, [alpha])
                self.refused_keeps_public(needle)
                (self.topics / f"{slug}.json").unlink()
        # the topics namespace is reserved only while topics exist
        self.topic("fine", [alpha])
        (page / "topics.json").write_text(json.dumps({"title": "Topics page", "body_md": "Text."}), encoding="utf-8")
        self.refused_keeps_public(r"topics/: the explainer page slug 'topics' clashes with the generated topics folder")
        (page / "topics.json").unlink()
        self.finding("topics", "2026-05-02T00:00:00+00:00")
        self.refused_keeps_public(r"topics/: the published finding slug 'topics' clashes")
        (self.content / "findings" / "topics.json").unlink()
        assets = self.content / "public-assets" / "topics"
        assets.mkdir(parents=True)
        (assets / "index.png").write_bytes(CLEAN_PNG)
        self.refused_keeps_public(r"public-assets/topics/index\.png: collides with a generated path")
        (self.topics / "fine.json").unlink()
        self.assertIn("topics/index.png", self.build())  # the same asset is fine once no topic exists

    def test_redirects_to_and_from_topic_paths(self):
        alpha, _, _ = self.three()
        self.topic("series", [alpha])
        redirects = self.content / "redirects.json"
        redirects.write_text(json.dumps({"schema_version": 1, "redirects": [{"from": "/old-series", "to": "/topics/series.html"}]}))
        self.build()
        self.assertIn("/old-series /topics/series.html 301", self.read("_redirects"))
        redirects.write_text(json.dumps({"schema_version": 1, "redirects": [{"from": "/topics/series.html", "to": "/"}]}))
        self.refused_keeps_public(r"/topics/series\.html is a page this build writes")

    # -- 4. limits, types, symlinks, checker parity ----------------------------
    def test_limits_types_symlinks_and_checker_parity(self):
        alpha, _, _ = self.three()
        for number in range(30):
            self.topic(f"t{number:02d}", [alpha])
        self.assertEqual(len(load_content(self.root)["topics"]), 30)
        self.assertEqual(content_problems(self.root), [])
        self.topic("t30", [alpha])
        self.refused(r"topics/: at most 30 topic files")
        self.assertEqual(content_problems(self.root), ["topics/: at most 30 topic files"])
        shutil.rmtree(self.topics)
        bad = {
            "empty.json": ({"title": "T", "intro_md": "", "finding_ids": []}, r"empty\.json\.finding_ids: must list 1 to 100"),
            "many.json": ({"title": "T", "intro_md": "", "finding_ids": [f"x{n}" for n in range(101)]},
                          r"many\.json\.finding_ids: must list 1 to 100"),
            "extra.json": ({"title": "T", "intro_md": "", "finding_ids": [alpha], "tags": []}, r"extra\.json: unknown field\(s\) \['tags'\]"),
            "short.json": ({"title": "T", "finding_ids": [alpha]}, r"short\.json: missing field\(s\) \['intro_md'\]"),
            "blank.json": ({"title": "  ", "intro_md": "", "finding_ids": [alpha]}, r"blank\.json\.title: must be a non-empty string"),
            "long.json": ({"title": "x" * 121, "intro_md": "", "finding_ids": [alpha]}, r"long\.json\.title: .*120"),
            "intro.json": ({"title": "T", "intro_md": ["a"], "finding_ids": [alpha]}, r"intro\.json\.intro_md: must be a string"),
            "big.json": ({"title": "T", "intro_md": "x" * (200 * 1024 + 1), "finding_ids": [alpha]}, r"big\.json\.intro_md"),
            "ids.json": ({"title": "T", "intro_md": "", "finding_ids": alpha}, r"ids\.json\.finding_ids: must list"),
            "type.json": ({"title": "T", "intro_md": "", "finding_ids": [7]}, r"type\.json\.finding_ids\[0\]: must be a finding id string"),
            "Bad_Name.json": ({"title": "T", "intro_md": "", "finding_ids": [alpha]}, r"Bad_Name\.json filename: must be a lowercase slug"),
        }
        self.topics.mkdir()
        for name, (document, needle) in bad.items():
            with self.subTest(name):
                path = self.topics / name
                path.write_text(json.dumps(document), encoding="utf-8")
                self.refused(needle)
                self.assertTrue(any(re.search(needle, line) for line in content_problems(self.root)), name)
                path.unlink()
        raw = {"utf8.json": (b'{"title": "\xff", "intro_md": "", "finding_ids": []}', r"utf8\.json: not valid UTF-8 JSON"),
               "json.json": (b"{not json", r"json\.json: not valid UTF-8 JSON"),
               "cap.json": (b" " * (2 * 1024 * 1024 + 1), r"cap\.json: file too large")}
        for name, (data, needle) in raw.items():
            with self.subTest(name):
                (self.topics / name).write_bytes(data)
                self.refused("topics/" + needle)
                self.assertTrue(any(line.startswith("topics/" + name) for line in content_problems(self.root)), name)
                (self.topics / name).unlink()
        # links and non-regular entries: refused by name, never followed or skipped
        real = self.root / "elsewhere.json"
        real.write_text(json.dumps({"title": "T", "intro_md": "", "finding_ids": [alpha]}), encoding="utf-8")
        os.symlink(real, self.topics / "link.json")
        os.symlink(self.root / "missing.json", self.topics / "dangling.json")
        (self.topics / "folder.json").mkdir()
        # two invalid files aggregate by file name in the checker, which stops nowhere
        problems = content_problems(self.root)
        self.assertEqual(sorted(problem_record(line)["file"] for line in problems),
                         ["topics/dangling.json", "topics/folder.json", "topics/link.json"])
        for name in ("link.json", "dangling.json", "folder.json"):
            path = self.topics / name
            path.rmdir() if path.is_dir() and not path.is_symlink() else path.unlink()
        for name, make in (("link.json", lambda p: os.symlink(real, p)),
                           ("dangling.json", lambda p: os.symlink(self.root / "missing.json", p)),
                           ("folder.json", lambda p: p.mkdir())):
            with self.subTest(name):
                make(self.topics / name)
                self.refused(rf"topics/{name}: must be a regular file, not a symlink or folder")
                path = self.topics / name
                path.rmdir() if path.is_dir() and not path.is_symlink() else path.unlink()
        # non-.json names are ignored; the folder itself may not be a link or a file
        (self.topics / "notes.txt").write_text("not a topic", encoding="utf-8")
        self.assertEqual(load_content(self.root)["topics"], [])
        shutil.rmtree(self.topics)
        os.symlink(self.root / "elsewhere-dir", self.topics)
        self.refused(r"topics/: the folder must not be a symlink")
        self.topics.unlink()
        self.topics.write_text("a file", encoding="utf-8")
        self.refused(r"topics/: must be a folder, not a file")
        self.assertEqual(content_problems(self.root), ["topics/: must be a folder, not a file"])
        self.topics.unlink()
        self.topic("one", [alpha])
        self.topic("two", ["nope"])
        self.topic("three", ["nada"])
        problems = content_problems(self.root)
        self.assertEqual([problem_record(line)["file"] for line in problems],
                         ["topics/three.json.finding_ids[0]", "topics/two.json.finding_ids[0]"])

    # -- 5. no topics: nothing changes ------------------------------------------
    def test_absent_empty_legacy_are_byte_identical(self):
        self.three()
        self.build()
        absent = self.tree()
        self.assertFalse([n for n, data in absent.items() if b"/topics/" in data or b"Part of:" in data or n.startswith("topics")])
        self.topics.mkdir()
        (self.topics / "notes.txt").write_text("ignored", encoding="utf-8")
        self.build()
        self.assertEqual(absent, self.tree())
        self.assertEqual(load_content(self.root)["topics"], [])
        # legacy inputs keep working without topics: a page slugged topics and a public-assets/topics folder
        (self.content / "pages").mkdir()
        (self.content / "pages" / "topics.json").write_text(json.dumps({"title": "About topics", "body_md": "Hi."}), encoding="utf-8")
        (self.content / "public-assets" / "topics").mkdir(parents=True)
        (self.content / "public-assets" / "topics" / "pic.png").write_bytes(CLEAN_PNG)
        written = self.build()
        self.assertTrue({"topics.html", "topics/pic.png"} <= written)
        self.assertNotIn("topics/index.html", written)

    def test_removal_clears_stale_pages(self):
        alpha, _, _ = self.three()
        self.topic("series", [alpha])
        self.assertIn("topics/series.html", self.build())
        self.assertIn("Part of:", self.read("findings/alpha-find.html"))
        (self.topics / "series.json").unlink()
        self.build()
        self.assertFalse((self.public / "topics").exists())
        self.assertNotIn("Part of:", self.read("findings/alpha-find.html"))
        self.assertNotIn("/topics/", self.read("sitemap.xml"))

    def test_no_base_url_keeps_relative_links(self):
        alpha, _, _ = self.three()
        site = json.loads((self.content / "site.json").read_text(encoding="utf-8"))
        site["base_url"] = None
        (self.content / "site.json").write_text(json.dumps(site), encoding="utf-8")
        self.topic("series", [alpha])
        written = self.build()
        self.assertNotIn("sitemap.xml", written)
        page = self.read("topics/series.html")
        self.assertIn('href="/findings/alpha-find.html"', page)
        self.assertIn('href="/topics/"', page)
        self.assertNotIn("https://cedar", page)
        self.assertIn('href="/topics/series.html"', self.read("findings/alpha-find.html"))

    # -- 6. corrections, withdrawn targets, private inputs, labels -----------------
    def test_corrections_and_missing_target_rebuild(self):
        alpha, beta, _ = self.three()
        self.topic("series", [alpha, beta])
        self.build()
        self.assertNotIn("Corrected", self.read("topics/series.html"))
        document = json.loads((self.content / "findings" / "beta-find.json").read_text(encoding="utf-8"))
        document["corrections"] = [{"date": "2026-04-01", "note": "Fixed."}, {"date": "2026-04-20", "note": "Fixed again."}]
        (self.content / "findings" / "beta-find.json").write_text(json.dumps(document), encoding="utf-8")
        self.build()
        page = self.read("topics/series.html")
        self.assertEqual(page.count("Corrected"), 1)
        self.assertIn('<time datetime="2026-04-20">', page)  # the newest correction, as in the findings list
        (self.content / "findings" / "beta-find.json").unlink()  # withdrawn: the topic must be edited first
        self.refused_keeps_public(r"topics/series\.json\.finding_ids\[1\]: 'fnd_beta_find' is not a published finding id")
        self.assertIn("beta-find", self.read("topics/series.html"))  # the previous public tree is still served

    def test_private_inputs_never_read_or_published(self):
        alpha, _, _ = self.three()
        private = self.root / "private" / "drafts"
        private.mkdir(parents=True)
        (private / "fnd_0123456789ab.json").write_text(json.dumps({"id": "fnd_0123456789ab", "title": "SECRETDRAFT"}), encoding="utf-8")
        self.topic("series", [alpha])
        seen = []
        original = content_module._read_json

        def spy(path, *args, **kwargs):
            seen.append(Path(path))
            return original(path, *args, **kwargs)
        with mock.patch.object(content_module, "_read_json", spy):
            self.build()
        self.assertTrue(any(p.name == "series.json" for p in seen))
        self.assertFalse([p for p in seen if "private" in p.parts])
        for name, data in self.tree().items():
            self.assertNotIn(b"SECRETDRAFT", data, name)
        self.assertFalse((self.public / "data" / "topics.json").exists())  # no JSON topics API

    def test_feature_labels_replaced(self):
        alpha, _, _ = self.three()
        self.topic("series", [alpha])
        (self.content / "labels.json").write_text(json.dumps({"schema_version": 1, "labels": {
            "nav.topics": "X1X", "heading.topics": "X2X", "title.topics": "X3X", "title.topics_description": "X4X",
            "finding.part_of": "X5X {topic}"}}), encoding="utf-8")
        self.build()
        index, finding = self.read("topics/index.html"), self.read("findings/alpha-find.html")
        self.assertIn(">X1X</a>", self.nav(index))
        self.assertIn("<h2>X2X</h2>", index)
        self.assertIn("<title>X3X |", index)
        self.assertIn('<meta name="description" content="X4X">', index)
        self.assertRegex(finding, r"<p>X5X <a href=\"/topics/series\.html\">Topic title</a></p>")
        for text in (DEFAULTS["nav.topics"], DEFAULTS["heading.topics"], DEFAULTS["title.topics_description"]):
            self.assertNotIn(f">{text}<", index)
        # the Spanish draft carries every new key with the same placeholders
        spanish = json.loads((REPO_ROOT / "examples" / "labels" / "es.json").read_text(encoding="utf-8"))
        merged = load_labels(spanish)
        for key in NEW_KEYS:
            self.assertIn(key, spanish["labels"])
            self.assertNotEqual(merged[key], DEFAULTS[key])
        self.assertEqual(spanish["labels"]["finding.part_of"], "Parte de: {topic}")
        (self.content / "labels.json").write_text(json.dumps(spanish), encoding="utf-8")
        self.build()
        self.assertIn("Parte de: ", self.read("findings/alpha-find.html"))
        self.assertIn(">Temas</a>", self.nav(self.read("topics/series.html")))


if __name__ == "__main__":
    unittest.main()
