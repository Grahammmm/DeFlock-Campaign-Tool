"""DF2207-10: optional site.json.press builds /press.html for reporters; absent, nothing else changes.

Synthetic content only (a temp copy of the fictional example); no network.
"""
import json
import re
import shutil
import tempfile
import unittest
import zlib
from datetime import datetime, timedelta, timezone
from html import escape
from html.parser import HTMLParser
from pathlib import Path

from campaign_tool.content import LICENSES, ODBL, ContentError
from campaign_tool.content_check import content_problems
from campaign_tool.image_meta import PNG_SIGNATURE
from campaign_tool.labels import DEFAULTS
from campaign_tool.site import GENERATED_TOP_LEVEL, build_site
from campaign_tool.site_check import check_accessibility, check_links

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
PRESS = {"contact_role": "Press lead", "contact_email": "press@example.invalid"}
FOOTER_LINK = '<p><a href="/press.html">Press</a></p>'
PRESS_KEYS = ["footer.press"] + [key for key in DEFAULTS if key.startswith("press.")]


def _chunk(kind, body):
    return len(body).to_bytes(4, "big") + kind + body + zlib.crc32(kind + body).to_bytes(4, "big")


def png(width, height):
    ihdr = width.to_bytes(4, "big") + height.to_bytes(4, "big") + b"\x08\x02\x00\x00\x00"
    return PNG_SIGNATURE + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", b"\x78\x9c\x01\x02") + _chunk(b"IEND", b"")


class Page(HTMLParser):
    """Ids, hrefs, the decoded text of each .cite-text paragraph (in order) and tag counts of one page."""

    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.ids, self.hrefs, self.cites, self.tags, self._cite = [], [], [], {}, None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.tags[tag] = self.tags.get(tag, 0) + 1
        if "id" in attrs:
            self.ids.append(attrs["id"])
        if "href" in attrs:
            self.hrefs.append(attrs["href"])
        if tag == "p" and "cite-text" in (attrs.get("class") or "").split():
            self._cite = []

    def handle_data(self, data):
        if self._cite is not None:
            self._cite.append(data)

    def handle_endtag(self, tag):
        if tag == "p" and self._cite is not None:
            self.cites.append("".join(self._cite))
            self._cite = None


class PressBase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-press-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.public, self.content = self.root / "public", self.root / "content"
        self.base_finding = json.loads((self.content / "findings" / "cedar-policy-posted.json").read_text(encoding="utf-8"))

    def site(self, **changes):
        path = self.content / "site.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        for key, value in changes.items():
            if value is ABSENT:
                document.pop(key, None)
            else:
                document[key] = value
        path.write_text(json.dumps(document), encoding="utf-8")

    def build(self, **changes):
        self.site(**changes)
        build_site(self.root)

    def read(self, name):
        return (self.public / name).read_text(encoding="utf-8")

    def tree(self):
        return {p.relative_to(self.public).as_posix(): p.read_bytes() for p in sorted(self.public.rglob("*")) if p.is_file()}

    def finding(self, name, **changes):
        document = {**self.base_finding, "slug": name, "id": f"fnd_{name}", "title": f"Title {name}", "summary": f"Summary {name}.",
                    "corrections": [], **changes}
        (self.content / "findings" / f"{name}.json").write_text(json.dumps(document), encoding="utf-8")


ABSENT = object()


class EnabledAndDisabled(PressBase):
    def test_optional_press_absent_is_byte_identical_and_toggle_removes_page(self):
        self.build()
        off = self.tree()
        self.assertNotIn("press.html", off)
        self.assertFalse([name for name, data in off.items() if b"press.html" in data])
        self.build(press=PRESS)
        on = self.tree()
        self.assertEqual(sorted(set(on) - set(off)), ["press.html"])
        changed = sorted(name for name in off if off[name] != on[name])
        self.assertEqual(changed, sorted([name for name in off if name.endswith(".html")] + ["sitemap.xml"]))
        for name in changed:
            if name.endswith(".html"):  # the only difference on a content page is exactly one footer paragraph
                self.assertEqual(on[name].decode().count(FOOTER_LINK), 1, name)
                self.assertTrue(on[name].decode().replace(FOOTER_LINK, "") == off[name].decode(), name)
        self.assertIn("<loc>https://cedar-records.example.invalid/press.html</loc>", on["sitemap.xml"].decode())
        self.assertNotIn(b"press", on["feed.xml"])
        nav = re.search(r'<nav class="site-nav".*?</nav>', on["index.html"].decode()).group(0)
        self.assertNotIn("press.html", nav)
        self.build(press=ABSENT)  # disabling removes the stale page and every link
        self.assertTrue(self.tree() == off)

    def test_sitemap_follows_base_url_and_page_is_built_without_it(self):
        self.build(press=PRESS, base_url=None)
        self.assertFalse((self.public / "sitemap.xml").exists())
        html = self.read("press.html")
        self.assertNotIn("canonical", html)
        self.assertEqual(self.read("index.html").count(FOOTER_LINK), 1)
        self.build(press=PRESS, base_url="https://cedar-records.example.invalid")
        self.assertEqual(self.read("sitemap.xml").count("/press.html</loc>"), 1)

    def test_page_shows_description_contact_and_extra_copy_after_the_campaign_description(self):
        self.build(press={**PRESS, "about_md": "Reporters: **ask us** for a briefing & more."})
        html = self.read("press.html")
        self.assertIn("A fictional group of Cedar County residents.", html)  # site about_md first, never replaced
        self.assertLess(html.index("A fictional group"), html.index("<strong>ask us</strong> for a briefing &amp; more."))
        self.assertIn('Press lead · <a href="mailto:press@example.invalid">press@example.invalid</a>', html)
        self.assertEqual(Page(html).hrefs.count("mailto:press@example.invalid"), 1)
        # an address elsewhere on the site is never borrowed
        self.build(press={"contact_role": "Press lead", "contact_email": "other@example.invalid"},
                   contact_email="general@example.invalid", privacy_contact="privacy@example.invalid")
        main = re.search(r"<main.*?</main>", self.read("press.html"), re.S).group(0)  # the footer keeps its own contact line
        self.assertIn("mailto:other@example.invalid", main)
        self.assertNotIn("general@example.invalid", main)
        self.assertNotIn("privacy@example.invalid", main)

    def test_role_and_text_are_escaped_and_address_defaults_to_the_site_contact(self):
        self.build(press={"contact_role": 'Desk "A" & <Press>'}, contact_email="desk@example.invalid")
        html = self.read("press.html")
        self.assertIn("Desk &quot;A&quot; &amp; &lt;Press&gt; · <a href=\"mailto:desk@example.invalid\">", html)
        self.assertNotIn("<Press>", html)


class Validation(PressBase):
    def refusal(self, press, expected, **site):
        self.site(press=press, **site)
        before = self.tree()
        with self.assertRaises(ContentError) as caught:
            build_site(self.root)
        self.assertTrue(str(caught.exception).startswith(expected), str(caught.exception))
        self.assertTrue(self.tree() == before, "a refused press must write nothing")
        problems = content_problems(self.root)  # content-check names the same field with the same text
        self.assertIn(str(caught.exception), problems)
        self.assertTrue(self.tree() == before)

    def test_press_validation_boundaries_and_check_parity(self):
        role, about, email = "site.json.press.contact_role", "site.json.press.about_md", "site.json.press.contact_email"
        for text in ("x", "x" * 80, 'Q & "A" desk', "<b>", "Édito"):
            with self.subTest(accepted_role=text):
                self.build(press={**PRESS, "contact_role": text})
                self.assertEqual(content_problems(self.root), [])
        for text in ("", "x" * 81, "   ", "a\nb", "a\r\nb", "a\tb", "a\x00b", "a\x7fb", "a\u2028b", 5, None, ["x"]):
            with self.subTest(refused_role=text):
                self.refusal({**PRESS, "contact_role": text}, role)
        for text in ("", "x" * 1500, "Line one.\n\nLine two."):
            with self.subTest(accepted_about=len(text)):
                self.build(press={**PRESS, "about_md": text})
                self.assertEqual(content_problems(self.root), [])
        for text in ("x" * 1501, "<b>bold</b>", "a > b", 5, None, ["x"]):
            with self.subTest(refused_about=text):
                self.refusal({**PRESS, "about_md": text}, about)
        self.refusal({"contact_email": "press@example.invalid"}, "site.json.press: missing field(s) ['contact_role']")
        self.refusal({**PRESS, "phone": "x"}, "site.json.press: unknown field(s) ['phone']")
        for value in (None, False, True, [], "", "press", 0, {}):
            with self.subTest(press=value):
                self.refusal(value, "site.json.press: ")

    def test_inherited_and_explicit_email_rules(self):
        self.build(press={"contact_role": "Lead"}, contact_email="desk@example.invalid")  # omitted: inherits
        self.assertIn("mailto:desk@example.invalid", self.read("press.html"))
        self.refusal({"contact_role": "Lead"}, "site.json.press.contact_email: required when", contact_email=None)
        for value in (None, "", 5, ["a@b.co"], "not-an-address", "a@b.co\r\nBcc: x@y.co", "a@b.co\n", " a@b.co", "a@b"):
            with self.subTest(explicit=value):  # an explicit value never falls back to the site address
                self.refusal({"contact_role": "Lead", "contact_email": value}, "site.json.press.contact_email: ",
                             contact_email="desk@example.invalid")

    def test_press_address_or_phone_in_copy_is_caught_by_the_privacy_gate(self):
        self.site(press={**PRESS, "about_md": "Write to someone@example.invalid today."})
        with self.assertRaises(ContentError):
            build_site(self.root)
        self.assertFalse(self.public.exists())


class Findings(PressBase):
    def stamp(self, moment):
        return moment.isoformat()

    def twelve(self):
        base = datetime(2026, 3, 1, 0, 0, tzinfo=timezone.utc)
        for n in range(1, 13):  # n hours after the base; written with alternating offsets so string order differs
            zone = timezone(timedelta(hours=-8 if n % 2 else 9))
            self.finding(f"f{n:02d}", id=f"fnd_f{n:02d}", published_at=(base + timedelta(hours=n)).astimezone(zone).isoformat())
        (self.content / "findings" / "cedar-policy-posted.json").unlink()

    def order(self, html):
        return re.findall(r'<li id="press-finding-([a-z0-9-]+)"', html)

    def test_ten_newest_by_utc_publication_with_ties(self):
        self.twelve()
        self.build(press=PRESS)
        self.assertEqual(self.order(self.read("press.html")), [f"f{n:02d}" for n in range(12, 2, -1)])
        # equal instants written in different forms: the higher slug comes first; an old finding's newer correction,
        # update and event cannot promote it
        self.finding("aa-tie", id="fnd_aa", published_at="2026-03-01T13:00:00+00:00")
        self.finding("zz-tie", id="fnd_zz", published_at="2026-03-01T05:00:00-08:00")
        self.finding("f01", id="fnd_f01", published_at="2026-03-01T01:00:00+00:00", updated_at="2026-04-01T00:00:00+00:00",
                     event_date="2026-02-28", corrections=[{"date": "2026-04-02", "note": "Later fix."}])
        self.build()
        order = self.order(self.read("press.html"))
        self.assertEqual(order, ["zz-tie", "aa-tie"] + [f"f{n:02d}" for n in range(12, 4, -1)])
        self.assertNotIn("f01", order)
        self.assertEqual(len(order), 10)

    def test_zero_one_and_exactly_ten_findings(self):
        (self.content / "findings" / "cedar-policy-posted.json").unlink()
        self.build(press=PRESS)
        html = self.read("press.html")
        self.assertIn("<p>No findings have been published yet.</p>", html)
        self.assertEqual(self.order(html), [])
        self.assertNotIn("Cite this finding", html)
        self.finding("only-one", published_at="2026-03-01T00:00:00+00:00")
        self.build()
        self.assertEqual(self.order(self.read("press.html")), ["only-one"])
        for n in range(9):
            self.finding(f"more-{n}", id=f"fnd_more{n}", published_at=f"2026-03-02T0{n}:00:00+00:00")
        self.build()
        self.assertEqual(len(self.order(self.read("press.html"))), 10)

    def test_only_published_public_fields_are_shown(self):
        self.build(press=PRESS)
        html = self.read("press.html")
        sha = json.loads((self.content / "findings" / "cedar-policy-posted.json").read_text(encoding="utf-8"))["sources"][0]["sha256"]
        for private in (sha, "synthetic-author", "Synthetic fixture; the underlying document", "It names a retention period"):
            self.assertNotIn(private, html)
        self.assertNotIn("/private", html)

    def test_citations_and_corrections_match_finding_and_json(self):
        self.finding("quoted", published_at="2026-03-05T09:00:00-08:00", title='Tom & "Jerry" records',
                     corrections=[{"date": "2026-03-07", "note": "First."}, {"date": "2026-03-09", "note": "Newest."},
                                  {"date": "2026-03-08", "note": "Middle."}])
        for base in ("https://cedar-records.example.invalid", None):
            with self.subTest(base_url=base):
                self.build(press=PRESS, base_url=base)
                press = Page(self.read("press.html"))
                data = {f["slug"]: f for f in json.loads(self.read("data/findings.json"))["findings"]}
                self.assertEqual(len(press.cites), 2)
                self.assertEqual(len(set(press.ids)), len(press.ids), "ids must be unique")
                self.assertNotIn("cite", press.ids)
                for slug, cite in zip(self.order(self.read("press.html")), press.cites):
                    page = Page(self.read(f"findings/{slug}.html"))
                    self.assertEqual(page.cites, [cite], slug)  # decoded text equals the finding page
                    self.assertEqual(data[slug]["citation"], cite, slug)  # and data/findings.json
                    self.assertEqual(cite.count("http") == 1, base is not None)
                quoted = [c for c in press.cites if "Jerry" in c][0]
                self.assertIn('Tom & "Jerry" records', quoted)
                self.assertIn("corrected 2026-03-09", quoted)
                self.assertIn("Tom &amp; &quot;Jerry&quot; records", self.read("press.html"))  # escaped once in the markup
                self.assertEqual(press.tags.get("button", 0), 0)
                self.assertEqual(press.tags.get("script", 0), 0)
                self.assertNotIn("cite.js", self.read("press.html"))

    def test_correction_marker_links_to_the_findings_corrections_and_none_without(self):
        self.finding("fixed", published_at="2026-03-05T09:00:00+00:00",
                     corrections=[{"date": "2026-03-06", "note": "A."}, {"date": "2026-03-09", "note": "B."}])
        self.finding("clean", id="fnd_clean", published_at="2026-03-04T09:00:00+00:00")
        self.build(press=PRESS)
        html = self.read("press.html")
        cards = dict(re.findall(r'<li id="press-finding-([a-z0-9-]+)">(.*?)</li>', html, re.S))
        self.assertIn('<a href="/findings/fixed.html#corrections">Corrected <time datetime="2026-03-09">2026-03-09</time></a>',
                      cards["fixed"])
        self.assertNotIn("Corrected", cards["clean"])
        self.assertIn('id="corrections"', self.read("findings/fixed.html"))  # the link target exists
        self.assertIn('Corrected <time datetime="2026-02-03">', cards["cedar-policy-posted"])
        self.assertIn('<a href="/findings/clean.html">Title clean</a>', cards["clean"])
        self.assertIn("Summary clean.", cards["clean"])
        self.assertIn("Published 2026-03-04", cards["clean"])
        self.assertEqual(check_links(self.public), [])
        self.assertEqual(check_accessibility(self.public), [])


class LogoAndData(PressBase):
    def logo(self, name="img/logo.png", data=None):
        path = self.content / "public-assets" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data or png(120, 40))

    def data_links(self, html):
        return [h for h in Page(html).hrefs if h.startswith("/data/")]

    def test_logo_download_only_with_a_brand_logo_and_without_a_second_copy(self):
        self.build(press=PRESS)
        self.assertNotIn("download", self.read("press.html"))
        self.assertNotIn("Download the logo", self.read("press.html"))
        self.build(brand={"accent": "#8a1c0f"})  # colours only: still no logo section
        self.assertNotIn("Logo", self.read("press.html"))
        self.assertNotIn("<img", self.read("press.html"))
        self.logo()
        self.build(brand={"logo": "img/logo.png", "logo_alt": "Cedar mark"})
        html = self.read("press.html")
        self.assertIn('<a href="/img/logo.png" download>Download the logo</a>', html)
        self.assertIn('alt="Cedar mark"', html)
        self.assertTrue((self.public / "img" / "logo.png").read_bytes() == png(120, 40), "the actual logo bytes are served")
        self.assertEqual(len(list(self.public.rglob("logo.png"))), 1)
        self.assertNotIn("<svg", html)

    def test_logo_with_an_unsafe_path_inherits_the_brand_gate(self):
        for logo in ("../logo.png", "/logo.png", "img/../logo.png", "missing.png", "https://example.invalid/logo.png"):
            with self.subTest(logo=logo):
                self.site(press=PRESS, brand={"logo": logo, "logo_alt": "Mark"})
                with self.assertRaises(ContentError) as caught:
                    build_site(self.root)
                self.assertIn("site.json.brand.logo", str(caught.exception))
                self.assertFalse(self.public.exists())

    def test_logo_symlink_is_refused_and_public_is_preserved(self):
        self.build(press=PRESS)
        before = self.tree()
        real = self.root / "real.png"
        real.write_bytes(png(120, 40))
        link = self.content / "public-assets" / "logo.png"
        link.parent.mkdir(parents=True)
        link.symlink_to(real)
        self.site(brand={"logo": "logo.png", "logo_alt": "Mark"})
        with self.assertRaises(ContentError):
            build_site(self.root)
        self.assertTrue(self.tree() == before)

    def test_logo_and_exact_emitted_data_licences(self):
        emitted = lambda: sorted(f"/data/{p.name}" for p in (self.public / "data").iterdir()
                                 if p.suffix in (".json", ".csv", ".geojson"))
        for license_id, (name, url) in LICENSES.items():
            with self.subTest(license_id):
                self.build(press=PRESS, license={"id": license_id, "attribution": "Cedar group"})
                html = self.read("press.html")
                links = self.data_links(html)
                self.assertEqual(links, sorted(links))
                self.assertEqual(sorted(set(links)), emitted())
                self.assertEqual(len(links), len(set(links)))
                for link in links:
                    self.assertTrue((self.public / link[1:]).is_file(), link)
                    item = re.search(rf'<li><a href="{re.escape(link)}">.*?</li>', html).group(0)
                    if link == "/data/cameras.geojson":
                        self.assertIn(f'<a href="{ODBL["url"]}" rel="license">{escape(ODBL["name"])}</a>', item)
                        self.assertIn("\u00a9 OpenStreetMap contributors", item)
                        self.assertNotIn("creativecommons", item)
                    else:
                        self.assertIn(f'<a href="{url}" rel="license">{escape(name)}</a>, Cedar group', item)
                        self.assertNotIn("opendatacommons", item)
        links = self.data_links(self.read("press.html"))
        self.assertIn("/data/findings.csv", links)
        self.assertIn("/data/city-boundaries.geojson", links)

    def test_without_a_site_licence_files_say_so_and_never_choose_one(self):
        self.build(press=PRESS)
        html = self.read("press.html")
        items = re.findall(r'<li><a href="(/data/[^"]+)">[^<]*</a> · (.*?)</li>', html)
        self.assertGreaterEqual(len(items), 9)
        for link, text in items:
            if link == "/data/cameras.geojson":
                self.assertIn("Open Database License (ODbL) 1.0", text)  # a fixed fact, even with no site licence
            else:
                self.assertEqual(text, "License not specified", link)
        self.assertNotIn("creativecommons", html)

    def test_map_files_follow_the_build_plan_and_no_map_leaves_only_open_data(self):
        self.build(press=PRESS)  # the example has cameras and city boundaries, no county boundary
        planned = set(self.data_links(self.read("press.html")))
        self.assertTrue(planned >= {"/data/cameras.geojson", "/data/city-boundaries.geojson"}, planned)
        self.assertNotIn("/data/county.geojson", planned)
        (self.content / "map" / "county.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": []}),
                                                             encoding="utf-8")
        self.build()
        self.assertIn("/data/county.geojson", self.data_links(self.read("press.html")))
        self.assertTrue((self.public / "data" / "county.geojson").is_file())
        self.assertNotIn("/vendor/", self.read("press.html"))
        shutil.rmtree(self.content / "map")
        self.build()
        html = self.read("press.html")
        links = self.data_links(html)
        self.assertEqual(links, sorted([f"/data/{n}.{e}" for n in ("agencies", "findings", "meetings", "sources")
                                        for e in ("csv", "json")] + ["/data/datapackage.json"]))
        for link in links:
            self.assertTrue((self.public / link[1:]).is_file(), link)
        self.assertNotIn("cameras.geojson", html)
        self.assertEqual(check_links(self.public), [])

    def test_stale_public_files_are_never_linked(self):
        self.build(press=PRESS)
        (self.public / "data" / "stale-leak.json").write_text("{}", encoding="utf-8")
        (self.public / "data" / "county.geojson").write_text("{}", encoding="utf-8")
        (self.root / "notes.json").write_text("{}", encoding="utf-8")
        self.build()  # a rebuild replaces public/ whole, and the page is planned from the build, not from disk
        self.assertNotIn("stale-leak", self.read("press.html"))
        self.assertNotIn("county.geojson", self.read("press.html"))
        self.assertFalse((self.public / "data" / "stale-leak.json").exists())


class Collisions(PressBase):
    def test_press_path_collisions_and_refusals_preserve_public(self):
        self.assertIn("press.html", GENERATED_TOP_LEVEL)
        self.build(press=PRESS)
        before = self.tree()
        pages = self.content / "pages"
        pages.mkdir()
        (pages / "press.json").write_text(json.dumps({"title": "Mine", "body_md": "Text."}), encoding="utf-8")
        for enabled in (True, False):  # reserved with press enabled and disabled
            self.site(press=PRESS if enabled else ABSENT)
            with self.subTest(enabled=enabled):
                with self.assertRaisesRegex(ContentError, r"^pages/press\.json: slug press is reserved$"):
                    build_site(self.root)
                self.assertIn("pages/press.json: slug press is reserved", content_problems(self.root))
        self.assertTrue(self.tree() == before)  # the custom page was never written over the generated one
        (pages / "press.json").unlink()
        self.site(press=PRESS)
        self.assertTrue(self.public.is_dir())

    def test_redirect_from_press_html_is_refused_with_press_on_or_off(self):  # the name is reserved unconditionally
        self.build(press=PRESS)
        before = self.tree()
        (self.content / "redirects.json").write_text(json.dumps(
            {"schema_version": 1, "redirects": [{"from": "/press.html", "to": "/about.html"}]}), encoding="utf-8")
        for enabled in (True, False):
            self.site(press=PRESS if enabled else ABSENT)
            with self.subTest(enabled=enabled):
                with self.assertRaisesRegex(ContentError, r"redirects\[0\]: from /press\.html is a page this build writes"):
                    build_site(self.root)
                self.assertTrue(self.tree() == before)

    def test_bad_press_or_failed_plan_leaves_public_unchanged_and_checks_write_nothing(self):
        self.build(press=PRESS)
        before = self.tree()
        self.site(press={**PRESS, "contact_role": "x" * 81})
        with self.assertRaises(ContentError):
            build_site(self.root)
        self.assertTrue(self.tree() == before)
        self.site(press=PRESS, brand={"logo": "missing.png", "logo_alt": "Mark"})
        with self.assertRaises(ContentError):
            build_site(self.root)
        self.assertTrue(self.tree() == before)
        self.assertEqual(sorted(p.name for p in self.root.glob(".public.*")), [])
        self.site(press=PRESS, brand=ABSENT)
        listing = sorted(p.relative_to(self.root).as_posix() for p in self.root.rglob("*"))
        self.assertEqual(content_problems(self.root), [])
        self.assertEqual(sorted(p.relative_to(self.root).as_posix() for p in self.root.rglob("*")), listing)


class Labels(PressBase):
    def test_press_labels_render_and_spanish_keys_complete(self):
        self.assertEqual(PRESS_KEYS, ["footer.press", "press.title", "press.description", "press.contact", "press.findings",
                                      "press.logo", "press.data", "press.download_logo", "press.license_unspecified",
                                      "press.news"])
        markers = {key: f"X{key}X" for key in (*PRESS_KEYS, "finding.cite", "finding.corrected", "finding.published")}
        markers["finding.corrected"], markers["finding.published"] = "Xcorrected {date}X", "Xpublished {date}X"
        (self.content / "public-assets").mkdir()
        (self.content / "public-assets" / "logo.png").write_bytes(png(120, 40))
        (self.content / "labels.json").write_text(json.dumps({"schema_version": 1, "labels": markers}), encoding="utf-8")
        (self.content / "coverage.json").write_text(json.dumps({"schema_version": 1, "entries": []}), encoding="utf-8")  # DF0022-03: press.news shows only with coverage
        self.build(press=PRESS, brand={"logo": "logo.png", "logo_alt": "Mark"})
        html = self.read("press.html")
        for key in PRESS_KEYS:
            self.assertIn(f"X{key}X", html, key)
        for fixed in ("Cite this finding", "Download the logo", "License not specified", "Press contact", "Recent findings"):
            self.assertNotIn(fixed, html)
        self.assertIn("Xcorrected ", html)
        self.assertIn("Xpublished 2026-02-01X", html)
        self.assertEqual(self.read("index.html").count('<a href="/press.html">Xfooter.pressX</a>'), 1)
        # the Spanish draft carries every new key, translated
        draft = json.loads((REPO_ROOT / "examples" / "labels" / "es.json").read_text(encoding="utf-8"))["labels"]
        for key in PRESS_KEYS:
            self.assertIn(key, draft)
            self.assertNotEqual(draft[key], DEFAULTS[key], key)
        (self.content / "labels.json").write_text(json.dumps({"schema_version": 1, "labels": draft}), encoding="utf-8")
        build_site(self.root)
        self.assertIn("Contacto de prensa", self.read("press.html"))
        self.assertIn('<a href="/press.html">Prensa</a>', self.read("index.html"))


if __name__ == "__main__":
    unittest.main()
