"""Share metadata (Open Graph + Twitter summary card) on every built page.

Builds the fictional example into a temporary copy; synthetic content only, no network.
"""
import json
import shutil
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path

from campaign_tool.cli import read_config
from campaign_tool.content import load_content
from campaign_tool.site import Site, build_site, preview

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
FINDING = "findings/cedar-policy-posted.html"
PAGES = {"index.html": "/", "agencies.html": "/agencies.html", "findings/index.html": "/findings/",
         FINDING: "/" + FINDING, "sources.html": "/sources.html", "meetings.html": "/meetings.html",
         "about.html": "/about.html", "corrections.html": "/corrections.html"}
REQUIRED = ("og:site_name", "og:title", "og:description", "og:type",
            "twitter:card", "twitter:title", "twitter:description")
SHARE_PREFIXES = ("og:", "twitter:", "article:")
BASE = "https://cedar-records.example.invalid"
# The starter's whole <head> block as built on main before this change (charset through stylesheet).
STARTER_HEAD = ('  <meta charset="utf-8">\n  <meta name="viewport" content="width=device-width, initial-scale=1">\n'
                '  <meta name="description" content="A local initiative for transparent ALPR policies and '
                'evidence-based public participation.">\n'
                '  <title>Cedar County Records Project | Local accountability</title>\n'
                '  <link rel="stylesheet" href="style.css">\n')


class MetaParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.meta = []  # (attribute used, key, content)

    def handle_starttag(self, tag, attrs):
        if tag != "meta":
            return
        attrs = dict(attrs)
        for attr in ("property", "name"):
            key = attrs.get(attr)
            if key and key.startswith(SHARE_PREFIXES):
                self.meta.append((attr, key, attrs.get("content")))


def share_tags(html):
    parser = MetaParser()
    parser.feed(html)
    return parser.meta


def values(tags, key):
    return [content for _, k, content in tags if k == key]


class ShareMetaTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-share-meta-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.public = self.root / "public"
        self.finding_path = self.root / "content" / "findings" / "cedar-policy-posted.json"
        self.finding = json.loads(self.finding_path.read_text(encoding="utf-8"))

    def edit_json(self, path, **changes):
        document = json.loads(path.read_text(encoding="utf-8"))
        document.update(changes)
        path.write_text(json.dumps(document), encoding="utf-8")

    def build(self):
        build_site(self.root)
        return {page: (self.public / page).read_text(encoding="utf-8") for page in PAGES}

    def tags(self):
        return {page: share_tags(html) for page, html in self.build().items()}

    def test_every_page_has_exactly_one_of_each_required_tag(self):
        for page, tags in self.tags().items():
            for key in REQUIRED:
                self.assertEqual(len(values(tags, key)), 1, f"{page} {key}")
            self.assertEqual(values(tags, "twitter:card"), ["summary"], page)
            self.assertEqual(values(tags, "og:site_name"), ["Cedar County Records Project"], page)
            for attr, key, _ in tags:
                self.assertEqual(attr, "name" if key.startswith("twitter:") else "property", f"{page} {key}")

    def test_finding_page_is_an_article_with_title_summary_and_published_time(self):
        tags = self.tags()[FINDING]
        self.assertEqual(values(tags, "og:type"), ["article"])
        self.assertEqual(values(tags, "og:title"), [self.finding["title"]])
        self.assertEqual(values(tags, "twitter:title"), [self.finding["title"]])
        self.assertEqual(values(tags, "og:description"), [self.finding["summary"]])
        self.assertEqual(values(tags, "twitter:description"), [self.finding["summary"]])
        self.assertEqual(values(tags, "article:published_time"), [self.finding["published_at"]])
        self.assertEqual(values(tags, "article:modified_time"), [], "updated_at is null in the example")

    def test_modified_time_present_only_when_updated_at_is_set(self):
        self.edit_json(self.finding_path, updated_at="2026-02-05T09:30:00+00:00")
        tags = self.tags()[FINDING]
        self.assertEqual(values(tags, "article:modified_time"), ["2026-02-05T09:30:00+00:00"])
        self.assertEqual(values(tags, "article:published_time"), [self.finding["published_at"]])
        self.edit_json(self.finding_path, updated_at=None)
        self.assertEqual(values(self.tags()[FINDING], "article:modified_time"), [])

    def test_non_finding_pages_are_websites_without_article_tags_or_name_suffix(self):
        tags = self.tags()
        for page, page_tags in tags.items():
            if page != FINDING:
                self.assertEqual(values(page_tags, "og:type"), ["website"], page)
                self.assertEqual([k for _, k, _ in page_tags if k.startswith("article:")], [], page)
        self.assertEqual(values(tags["index.html"], "og:title"), ["Home"])
        self.assertEqual(values(tags["index.html"], "og:description"), ["Local records. Public understanding."])
        self.assertEqual(values(tags["corrections.html"], "og:title"), ["Corrections"])

    def test_og_url_is_base_url_plus_path(self):
        for page, tags in self.tags().items():
            self.assertEqual(values(tags, "og:url"), [BASE + PAGES[page]], page)

    def test_trailing_slash_on_base_url_is_not_doubled(self):
        self.edit_json(self.root / "content" / "site.json", base_url=BASE + "/")
        for page, tags in self.tags().items():
            self.assertEqual(values(tags, "og:url"), [BASE + PAGES[page]], page)

    def test_no_og_url_without_base_url(self):
        site_json = self.root / "content" / "site.json"
        self.edit_json(site_json, base_url=None)
        for page, tags in self.tags().items():
            self.assertEqual(values(tags, "og:url"), [], f"{page} base_url=None")
            self.assertEqual(len(tags), len(REQUIRED) + (1 if page == FINDING else 0), page)
        document = json.loads(site_json.read_text(encoding="utf-8"))
        del document["base_url"]
        site_json.write_text(json.dumps(document), encoding="utf-8")
        for page, tags in self.tags().items():
            self.assertEqual(values(tags, "og:url"), [], f"{page} base_url missing")

    def assert_share_lines_safe(self, head):
        lines = [line for line in head.splitlines()
                 if 'property="og:' in line or 'name="twitter:' in line or 'property="article:' in line]
        self.assertTrue(lines)
        for line in lines:
            self.assertTrue(line.endswith('">'), line)
            content = line.split('content="', 1)[1][:-2]
            self.assertNotIn("<", content, line)
            self.assertNotIn('"', content, line)
            self.assertNotIn("\t", content, line)

    def test_quotes_ampersands_and_whitespace_are_escaped_and_collapsed_on_the_built_page(self):
        title = 'Cedar "policy" posted & filed\n\tagain'
        summary = 'A summary & "quoted"\n\n\t\tover  lines'
        self.edit_json(self.finding_path, title=title, summary=summary)
        html = self.build()[FINDING]
        tags = share_tags(html)
        for key in ("og:title", "twitter:title"):
            self.assertEqual(values(tags, key), [" ".join(title.split())], key)
        for key in ("og:description", "twitter:description"):
            self.assertEqual(values(tags, key), [" ".join(summary.split())], key)
        head = html.split("</head>", 1)[0]
        self.assert_share_lines_safe(head)
        self.assertIn('content="Cedar &quot;policy&quot; posted &amp; filed again"', head)

    def test_angle_brackets_are_escaped_in_share_values(self):
        # The content model refuses angle brackets in finding text; the renderer must still escape them.
        site = Site(self.root, read_config(self.root), load_content(self.root))
        title = 'T <b>"x"</b> & y\n\tz'
        summary = 'S <script>alert(1)</script>\n\t& "q"'
        head = site.head(title, summary, "/findings/x.html", kind="article", published="2026-02-01T18:00:00+00:00")
        tags = share_tags(head)
        self.assertEqual(values(tags, "og:title"), [" ".join(title.split())])
        self.assertEqual(values(tags, "twitter:description"), [" ".join(summary.split())])
        self.assert_share_lines_safe(head)
        self.assertNotIn("<script>alert", head)
        self.assertNotIn("<b>", head)

    def test_campaign_name_with_ampersand_is_escaped_once(self):
        self.edit_json(self.root / "campaign.json", name="Cedar Records & Rights <Project>")
        pages = self.build()
        for page, html in pages.items():
            tags = share_tags(html)
            self.assertEqual(values(tags, "og:site_name"), ["Cedar Records & Rights <Project>"], page)
            self.assertIn('content="Cedar Records &amp; Rights &lt;Project&gt;"', html, page)
            self.assertNotIn("&amp;amp;", html, page)

    def test_no_image_tags_and_no_remote_url_other_than_base_url(self):
        for unset_base in (False, True):
            if unset_base:
                self.edit_json(self.root / "content" / "site.json", base_url=None)
            for page, tags in self.tags().items():
                keys = [k for _, k, _ in tags]
                self.assertFalse([k for k in keys if "image" in k], page)
                for _, key, content in tags:
                    if "http://" in content or "https://" in content:
                        self.assertFalse(unset_base, f"{page} {key}")
                        self.assertEqual(key, "og:url", page)
                        self.assertTrue(content.startswith(BASE + "/"), page)

    def test_remote_url_in_text_is_only_plain_text_never_an_extra_tag(self):
        self.edit_json(self.finding_path, summary="See https://elsewhere.example.invalid/image.png for the policy.")
        tags = self.tags()[FINDING]
        self.assertEqual(values(tags, "og:description"), ["See https://elsewhere.example.invalid/image.png for the policy."])
        self.assertFalse([k for _, k, _ in tags if "image" in k])
        self.assertEqual(values(tags, "og:url"), [BASE + "/" + FINDING])

    def test_starter_preview_is_byte_identical_to_main(self):
        shutil.rmtree(self.root / "content")
        written = {p.name for p in build_site(self.root)}
        self.assertEqual(written, {"index.html", "style.css", "_headers"})
        page = (self.public / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("og:", page)
        self.assertNotIn("twitter:", page)
        self.assertEqual(share_tags(page), [])
        self.assertEqual(page, preview(read_config(self.root))["html"])
        self.assertIn(STARTER_HEAD, page.split("</head>", 1)[0])


if __name__ == "__main__":
    unittest.main()
