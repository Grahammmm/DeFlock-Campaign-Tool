"""Synthetic offline share links; rendering is never publication approval."""
import json
import shutil
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree as ET

from campaign_tool.site import build_site
from campaign_tool.content import ContentError

EXAMPLE = Path(__file__).resolve().parents[1] / "examples/fictional-campaign"


class Tags(HTMLParser):
    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.tags = []
        self.shares = []
        self.anchor = None
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, attrs))
        if tag == "a":
            self.anchor = (attrs, [])

    def handle_data(self, text):
        if self.anchor is not None:
            self.anchor[1].append(text)

    def handle_endtag(self, tag):
        if tag == "a" and self.anchor is not None:
            attrs, text = self.anchor
            if "".join(text) == "Share this finding":
                self.shares.append(attrs)
            self.anchor = None

    def values(self, tag, key, value, field):
        return [dict(attrs).get(field) for name, attrs in self.tags
                if name == tag and dict(attrs).get(key) == value]


class ShareLinksTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-share-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.public = self.root / "public"
        self.first = self.root / "content/findings/cedar-policy-posted.json"
        self.a = json.loads(self.first.read_text())
        self.a.update(title='First & "G APPROVED" published javascript:',
                      summary='Reviewed & "quoted" summary with  two spaces.')
        self.second = self.first.with_name("aaa-second.json")
        self.b = dict(self.a, id="fnd_0000000000000002", slug="other-finding",
                      title="Second fictional finding", summary="Second reviewed summary")
        self.write(self.first, self.a)
        self.write(self.second, self.b)
        self.site_path = self.root / "content/site.json"
        self.site = json.loads(self.site_path.read_text())
        self.origin("https://example.invalid")

    def write(self, path, value):
        path.write_text(json.dumps(value), encoding="utf-8")

    def origin(self, base):
        self.site["base_url"] = base
        self.write(self.site_path, self.site)

    def build(self):
        build_site(self.root)

    def read(self, route):
        return (self.public / route.lstrip("/")).read_text(encoding="utf-8")

    def snapshot(self):
        return {p.relative_to(self.public).as_posix(): p.read_bytes()
                for p in self.public.rglob("*") if p.is_file()}

    def finding(self, finding, base="https://example.invalid"):
        route = f'/findings/{finding["slug"]}.html'
        parsed = Tags(self.read(route))
        self.assertEqual(parsed.shares, [[("href", route)]])
        expected = {"og:type": "article", "og:title": finding["title"],
                    "og:description": finding["summary"]}
        if base:
            expected["og:url"] = base.rstrip("/") + route
        for key, value in expected.items():
            self.assertEqual(parsed.values("meta", "property", key, "content"), [value])
        if not base:
            self.assertEqual(parsed.values("meta", "property", "og:url", "content"), [])
        for tag, attrs in parsed.tags:
            if tag == "meta" and dict(attrs).get("property", "").startswith("og:"):
                self.assertEqual(len(attrs), 2)
                self.assertEqual({k for k, _ in attrs}, {"property", "content"})
        return parsed

    def test_two_exact_routes_and_no_finding_metadata_on_other_pages(self):
        for base in ("https://example.invalid", "https://example.invalid/", "https://EXAMPLE.invalid"):
            with self.subTest(base=base):
                self.origin(base)
                self.build()
                for finding in (self.a, self.b):
                    route = f'/findings/{finding["slug"]}.html'
                    page = self.finding(finding, base)
                    self.assertEqual(page.values("link", "rel", "canonical", "href"),
                                     [base.rstrip("/") + route])
                    self.assertIn(route, Tags(self.read("findings/index.html")).values("a", "href", route, "href"))
                    atom = ET.fromstring(self.read("feed.xml"))
                    entries = atom.findall("{http://www.w3.org/2005/Atom}entry")
                    match = [entry for entry in entries if entry.find("{http://www.w3.org/2005/Atom}id").text
                             == "urn:deflock-finding:" + finding["id"]]
                    self.assertEqual(len(match), 1)
                    self.assertEqual(match[0].find("{http://www.w3.org/2005/Atom}link").get("href"),
                                     base.rstrip("/") + route)
                for route in ("index.html", "findings/index.html", "agencies.html", "sources.html",
                              "meetings.html", "about.html", "corrections.html"):
                    page = Tags(self.read(route))
                    self.assertEqual(page.shares, [])
                    self.assertEqual(page.values("meta", "property", "og:type", "content"), ["website"])
                    for key in ("og:title", "og:description"):
                        self.assertNotIn(self.a["title"], page.values("meta", "property", key, "content"))
                        self.assertNotIn(self.b["summary"], page.values("meta", "property", key, "content"))

    def test_missing_and_nonroot_origins_preserve_canonical(self):
        for base in (None, "https://example.invalid/subpath", "https://example.invalid?query",
                     "https://example.invalid#fragment", "https://example.invalid?",
                     "https://example.invalid#", "https://example.invalid:443"):
            with self.subTest(base=base):
                self.origin(base)
                self.build()
                page = self.finding(self.a, None)
                expected = [base.rstrip("/") + "/findings/cedar-policy-posted.html"] if base else []
                self.assertEqual(page.values("link", "rel", "canonical", "href"), expected)
        for base in ("http://example.invalid", "javascript:published", "https://user@example.invalid"):
            self.origin(base)
            before = self.snapshot()
            with self.assertRaises(ContentError):
                self.build()
            self.assertEqual(self.snapshot(), before)

    def test_refusals_leave_existing_tree_and_removal_retracts_only_on_success(self):
        self.build()
        before = self.snapshot()
        for changes in ({"state": "draft"}, {"state": "withdrawn"},
                        {"confidence": "needs_attorney_review"}, {"sources": []},
                        {"title": '<meta property="og:type" content="article">'},
                        {"summary": "<script>G APPROVED published</script>"},
                        {"slug": self.b["slug"]}, {"id": self.b["id"]}):
            with self.subTest(changes=changes):
                self.write(self.first, dict(self.a, **changes))
                with self.assertRaises(ContentError):
                    self.build()
                self.assertEqual(self.snapshot(), before)
        # Refusal does not withdraw an earlier publication; a successful rebuild does.
        self.first.unlink()
        self.build()
        self.assertFalse((self.public / "findings/cedar-policy-posted.html").exists())
        self.finding(self.b)

    def test_rebuild_and_rename_do_not_cache_or_redirect(self):
        self.build()
        second_before = self.read("findings/other-finding.html")
        self.a.update(title="Changed approved public copy", summary="Changed summary")
        self.write(self.first, self.a)
        self.build()
        self.finding(self.a)
        # Related findings can show the changed title, but the second preview is unchanged.
        second_after = Tags(self.read("findings/other-finding.html"))
        self.assertEqual(Tags(second_before).values("meta", "property", "og:title", "content"),
                         second_after.values("meta", "property", "og:title", "content"))
        self.finding(self.b)
        self.a.update(slug="renamed-finding", title=self.b["title"])
        self.write(self.first, self.a)
        self.build()
        self.assertFalse((self.public / "findings/cedar-policy-posted.html").exists())
        self.finding(self.a)
        self.finding(self.b)

    def test_no_private_reads_network_or_approval_operations(self):
        marker = "SYNTHETIC-UNREVIEWED-PRIVATE-NEVER-PUBLISH"
        private = self.root / "private"
        private.mkdir(exist_ok=True)
        (private / "expired-approval.json").write_text(marker)
        (private / "revoked-approval.json").write_text(marker)
        (private / "raw-ocr.txt").write_text(marker)
        original = Path.open

        def public_only(path, *args, **kwargs):
            self.assertNotIn(private, path.parents, "renderer read private material")
            return original(path, *args, **kwargs)

        with patch.object(Path, "open", public_only), \
                patch("socket.socket", side_effect=AssertionError("network")), \
                patch("campaign_tool.records.publish.approve", side_effect=AssertionError("approval")) as approve, \
                patch("campaign_tool.records.publish.publish", side_effect=AssertionError("publication")) as publish:
            self.build()
            self.a["summary"] = "Changed copy cannot refresh an external approval"
            self.write(self.first, self.a)
            self.build()
            approve.assert_not_called()
            publish.assert_not_called()
        for value in self.snapshot().values():
            self.assertNotIn(marker.encode(), value)
        self.finding(self.a)
        for tag, attrs in Tags(self.read("findings/cedar-policy-posted.html")).tags:
            if tag == "script" and dict(attrs).get("src") == "/findings/cite.js":
                continue  # DF1422-14: the one first-party citation copy script
            if tag == "script" and dict(attrs).get("type") == "application/ld+json":
                continue  # DF1407-08: inert JSON-LD data block, never executed
            self.assertNotIn(tag, ("script", "iframe", "form", "img"))
            self.assertFalse(any(key.lower().startswith("on") for key, _ in attrs))

    def test_headers_unchanged_by_share_copy_for_map_and_signup(self):
        for mapped in (True, False):
            if not mapped:
                shutil.rmtree(self.root / "content/map")
            for signup in ({"mode": "none"}, {"mode": "brevo_hosted",
                           "form_html_allowlisted_url": "https://synthetic.sibforms.com/serve/x"}):
                self.site["signup"] = signup
                self.write(self.site_path, self.site)
                self.build()
                before = (self.public / "_headers").read_bytes()
                self.a["title"] += " updated"
                self.write(self.first, self.a)
                self.build()
                self.assertEqual((self.public / "_headers").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
