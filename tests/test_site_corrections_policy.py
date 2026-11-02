"""BF-df-corrections: corrections.html says in advance how to ask for a correction or takedown.

Synthetic content only (temp copies of the fictional example); no network.
"""
import json
import re
import shutil
import tempfile
import unittest
from datetime import date
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from campaign_tool.content import ContentError
from campaign_tool.corrections_policy import NO_ADDRESS, policy_html
from campaign_tool.site import build_site

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
BUILD_DAY = date(2026, 10, 20)
REQUEST_LINK = '<a href="/corrections.html#request">Ask for a correction</a>'
FORBIDDEN_WORDS = ("within", "days", "law", "guarantee")


class Anchors(HTMLParser):
    def __init__(self):
        super().__init__()
        self.anchors, self.ids, self.tags, self.attrs = [], [], [], []
        self._open = None

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        self.attrs += [name for name, _ in attrs]
        if "id" in dict(attrs):
            self.ids.append(dict(attrs)["id"])
        if tag == "a":
            self._open = {"href": dict(attrs).get("href"), "text": ""}
            self.anchors.append(self._open)

    def handle_data(self, data):
        if self._open is not None:
            self._open["text"] += data

    def handle_endtag(self, tag):
        if tag == "a":
            self._open = None


def parse(html):
    parser = Anchors()
    parser.feed(html)
    return parser


class CorrectionsPolicyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-corrections-policy-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.findings = self.root / "content" / "findings"
        self.public = self.root / "public"
        self.base = json.loads((self.findings / "cedar-policy-posted.json").read_text(encoding="utf-8"))

    def set_email(self, address):
        path = self.root / "content" / "site.json"
        site = json.loads(path.read_text(encoding="utf-8"))
        site["contact_email"] = address
        path.write_text(json.dumps(site), encoding="utf-8")

    def only(self, *findings):
        for path in self.findings.glob("*.json"):
            path.unlink()
        for slug, corrections in findings:
            record = dict(self.base, id="fnd_" + slug.replace("-", "_"), slug=slug, title=f"Synthetic {slug} (fictional)",
                          corrections=[{"date": d, "note": n} for d, n in corrections],
                          published_at="2026-02-01T18:00:00+00:00")
            (self.findings / f"{slug}.json").write_text(json.dumps(record), encoding="utf-8")

    def build(self):
        return build_site(self.root, today=BUILD_DAY)

    def read(self, name):
        return (self.public / name).read_text(encoding="utf-8")

    def section(self):
        page = self.read("corrections.html")
        start = page.index('<div class="notice" id="request">')
        return page[start:page.index("</div>", start) + len("</div>")]

    def mailtos(self, html):
        return [a for a in parse(html).anchors if (a["href"] or "").startswith("mailto:")]

    def test_with_contact_email_one_mailto_with_subject(self):
        self.set_email("organizer@example.invalid")
        self.only(("alpha", [("2026-03-01", "a1")]))
        self.build()
        links = self.mailtos(self.section())
        self.assertEqual(len(links), 1)
        split = urlsplit(links[0]["href"])
        self.assertEqual(unquote(split.path), "organizer@example.invalid")
        self.assertEqual(parse_qs(split.query), {"subject": ["Correction request"]})
        self.assertIn("subject=Correction%20request", links[0]["href"])
        self.assertIn("a correction or takedown", self.section())

    def test_without_contact_email_no_mailto_and_plain_sentence(self):
        self.only(("alpha", [("2026-03-01", "a1")]))
        self.build()
        page = self.read("corrections.html")
        self.assertEqual(self.mailtos(page), [])
        self.assertNotIn("mailto:", page)
        self.assertIn("This campaign has not published a contact address, so correction requests cannot be "
                      "made through this site yet.", self.section())
        self.assertEqual(NO_ADDRESS, "This campaign has not published a contact address, so correction requests "
                                     "cannot be made through this site yet.")
        self.assertNotIn(NO_ADDRESS, policy_html("organizer@example.invalid"))

    def test_plus_and_dot_address_is_escaped_without_breaking_attributes(self):
        self.set_email("fix+site@example.invalid")
        self.only(("alpha", []))
        self.build()
        links = self.mailtos(self.section())
        self.assertEqual(len(links), 1)
        self.assertEqual(unquote(urlsplit(links[0]["href"]).path), "fix+site@example.invalid")
        self.assertEqual(links[0]["text"], "fix+site@example.invalid")
        # hostile address handed straight to the renderer cannot break out of the attribute
        hostile = policy_html('x"><script>y</script>@example.invalid')
        self.assertNotIn("<script", hostile)
        self.assertEqual(len(self.mailtos(hostile)), 1)
        self.assertEqual(parse(hostile).tags.count("a"), 1)

    def test_zero_corrections_keeps_policy_then_empty_state(self):
        self.set_email("organizer@example.invalid")
        self.only(("alpha", []))
        self.build()
        page = self.read("corrections.html")
        self.assertEqual(page.count('id="request"'), 1)
        self.assertLess(page.index('id="request"'), page.index("<p>No corrections have been issued.</p>"))

    def test_each_finding_links_to_the_single_request_anchor(self):
        self.only(("alpha", [("2026-03-01", "a1")]), ("beta", [("2026-04-01", "b1")]))
        self.build()
        for slug in ("alpha", "beta"):
            page = self.read(f"findings/{slug}.html")
            self.assertEqual(page.count(REQUEST_LINK), 1, slug)
            self.assertIn('<p><a href="/corrections.html">All corrections</a></p>', page)
        self.assertEqual(parse(self.read("corrections.html")).ids.count("request"), 1)

    def test_refused_draft_finding_leaves_public_unchanged(self):
        self.only(("alpha", [("2026-03-01", "a1")]))
        self.build()
        before = {p.relative_to(self.public): p.read_bytes() for p in self.public.rglob("*") if p.is_file()}
        draft = json.loads((self.findings / "alpha.json").read_text(encoding="utf-8"))
        draft.update(id="fnd_draft", slug="draft", confidence="needs_attorney_review")
        (self.findings / "draft.json").write_text(json.dumps(draft), encoding="utf-8")
        with self.assertRaises(ContentError):
            self.build()
        after = {p.relative_to(self.public): p.read_bytes() for p in self.public.rglob("*") if p.is_file()}
        self.assertTrue(before == after, "public/ changed after a refused build")

    def test_headers_unchanged_and_section_adds_no_form_script_or_url(self):
        self.only(("alpha", [("2026-03-01", "a1")]))
        self.build()
        without_mail = self.read("_headers")
        self.assertTrue((self.public / "_headers").read_bytes().decode("utf-8") == without_mail)
        self.assertNotIn("mailto", without_mail)
        self.set_email("organizer@example.invalid")
        self.build()
        self.assertTrue(self.read("_headers") == without_mail, "_headers changed when a contact address was added")
        for html in (self.section(), policy_html(None)):
            parsed = parse(html)
            self.assertFalse({"form", "script", "input", "iframe", "img"} & set(parsed.tags))
            self.assertFalse([a for a in parsed.attrs if a.startswith("on")])
            self.assertNotIn("://", html)
            self.assertEqual([a["href"] for a in parsed.anchors if not a["href"].startswith("mailto:")], [])

    def test_no_legal_deadline_or_guarantee_wording(self):
        for html in (policy_html("organizer@example.invalid"), policy_html(None)):
            text = re.sub(r"<[^>]+>", " ", html).lower()
            for word in FORBIDDEN_WORDS:
                self.assertIsNone(re.search(rf"\b{word}\b", text), word)
        fixed = policy_html(None)
        for sentence in ("Include the link to the page, what is wrong, and a source if you have one. "
                         "Do not send personal information about anyone else.",
                         "If a page shows personal information about a private person, say so in the first line; "
                         "that page is reviewed first.",
                         "An organizer reviews every request against the cited records. A change is listed below "
                         "with its date. A request that is not accepted gets a reply saying why."):
            self.assertIn(sentence, fixed)


if __name__ == "__main__":
    unittest.main()
