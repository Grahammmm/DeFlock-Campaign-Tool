"""DF1207-16: /privacy.html is generated from the same settings as the CSP; the build refuses a claim the CSP contradicts."""
import json
import shutil
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from campaign_tool import site as site_module
from campaign_tool.cli import read_config
from campaign_tool.content import ContentError, load_content
from campaign_tool.site import Site, build_site

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "fictional-campaign"
SIGNUP = {"mode": "brevo_hosted", "form_html_allowlisted_url": "https://synthetic.sibforms.com/serve/x"}
TODAY = date(2026, 10, 1)


class PrivacyPageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="synthetic-site-privacy-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.content, self.public = self.root / "content", self.root / "public"

    def configure(self, map_config=True, **site_keys):
        if not map_config and (self.content / "map").exists():
            shutil.rmtree(self.content / "map")
        site = json.loads((self.content / "site.json").read_text())
        site.update(site_keys)
        (self.content / "site.json").write_text(json.dumps(site))

    def build(self):
        return build_site(self.root, today=TODAY)

    def read(self, name):
        return (self.public / name).read_text(encoding="utf-8")

    def refused(self, *needles):
        with self.assertRaises(ContentError) as caught:
            self.build()
        for needle in needles:
            self.assertIn(needle, str(caught.exception))
        return str(caught.exception)

    def facts(self):
        model = load_content(self.root, TODAY)
        return Site(self.root, read_config(self.root), model, today=TODAY).privacy_facts()

    # 1
    def test_no_map_no_signup_says_first_party_only_and_no_signup_paragraph(self):
        self.configure(map_config=False)
        self.build()
        page = self.read("privacy.html")
        self.assertIn("served from this site only", page)
        self.assertIn("sets no cookies and runs no analytics", page)
        self.assertNotIn("map tiles", page)
        self.assertNotIn("email address goes to", page)
        self.assertIn("This campaign has not published a privacy contact.", page)
        self.assertIn("not legal advice and not a reviewed privacy policy", page)
        self.assertEqual(self.facts()["map_hosts"], [])
        self.assertIsNone(self.facts()["signup"])

    # 2
    def test_map_with_two_tile_hosts_names_both_sorted_and_matches_img_src(self):
        config = self.content / "map" / "map-config.json"
        document = json.loads(config.read_text())
        document["tile_hosts"] = ["tiles.example.invalid", "cdn.example.invalid"]
        config.write_text(json.dumps(document))
        self.build()
        page = self.read("privacy.html")
        self.assertIn("cdn.example.invalid, tiles.example.invalid.", page)
        csp = self.read("_headers")
        self.assertIn("img-src 'self' data: blob: https://cdn.example.invalid https://tiles.example.invalid;", csp)
        self.assertEqual(self.facts()["map_hosts"], ["cdn.example.invalid", "tiles.example.invalid"])

    # 3
    def test_hosted_signup_names_the_host_the_form_action_allows(self):
        self.configure(map_config=False, signup=dict(SIGNUP, note="Zq note."))
        self.build()
        page = self.read("privacy.html")
        self.assertIn("Your email address goes to synthetic.sibforms.com when you submit the signup form; this site never receives it. Zq note.", page)
        self.assertIn("form-action https://synthetic.sibforms.com;", self.read("_headers"))
        self.assertNotIn("privacy policy</a>", page)  # no provider link without signup.privacy_url
        self.assertEqual(self.facts()["signup"]["origin"], "https://synthetic.sibforms.com")

    def test_signup_privacy_url_is_linked_only_with_hosted_signup(self):
        policy = "https://synthetic.sibforms.com/privacy"
        self.configure(map_config=False, signup=dict(SIGNUP, privacy_url=policy))
        self.build()
        self.assertIn(f'<a href="{policy}" rel="noopener noreferrer">Read the provider\'s privacy policy</a>', self.read("privacy.html"))
        self.configure(map_config=False, signup={"mode": "none", "privacy_url": policy})
        self.build()
        self.assertNotIn(policy, self.read("privacy.html"))

    # 4
    def test_forced_contradiction_is_refused_and_nothing_is_written(self):
        real = Site.csp
        cases = (("script-src 'self'", "script-src 'self' https://evil.example.invalid", "script-src 'self' https://evil.example.invalid"),
                 ("font-src 'self'", "font-src 'self' https://fonts.example.invalid", "font-src 'self' https://fonts.example.invalid"),
                 ("img-src 'self' data: blob:", "img-src 'self' data: blob: https://extra.example.invalid", "img-src/connect-src hosts"),
                 ("form-action 'none'", "form-action https://x.sibforms.com", "form-action https://x.sibforms.com"))
        for old, new, against in cases:
            with self.subTest(new=new), mock.patch.object(Site, "csp", lambda s, old=old, new=new: real(s).replace(old, new)):
                message = self.refused("privacy.html:", "contradicts", against)
                self.assertIn('"', message)
                self.assertFalse(self.public.exists())
                self.assertEqual([p.name for p in self.root.iterdir() if p.name.startswith(".public")], [])

    def test_contradiction_leaves_an_existing_public_untouched(self):
        self.build()
        before = self.read("privacy.html")
        real = Site.csp
        with mock.patch.object(Site, "csp", lambda s: real(s).replace("script-src 'self'", "script-src 'self' https://evil.example.invalid")):
            self.refused("scripts, stylesheets and fonts come from this site only")
        self.assertEqual(self.read("privacy.html"), before)

    # 5
    def test_built_analytics_script_is_refused(self):
        (self.content / "public-assets").mkdir()
        (self.content / "public-assets" / "analytics.js").write_text("// fixture\n")
        with mock.patch.object(site_module, "ASSET_EXTENSIONS", site_module.ASSET_EXTENSIONS | {".js"}):
            self.refused('"no analytics" contradicts the built script analytics.js')
        self.assertFalse(self.public.exists())

    def test_analytics_names_in_scripts_and_script_tags_are_refused(self):
        site = Site(self.root, read_config(self.root), load_content(self.root, TODAY), today=TODAY)
        for name in ("gtag.js", "vendor/Plausible.js", "umami/x.mjs", "matomo.js"):
            with self.subTest(name), self.assertRaisesRegex(ContentError, "no analytics"):
                site.check_privacy([name], {"_headers": site.headers()})
        with self.assertRaisesRegex(ContentError, "no analytics"):
            site.check_privacy([], {"_headers": site.headers(), "x.html": '<script defer src="/js/analytics-v2.js"></script>'})
        site.check_privacy(["analytics-report.pdf", "app.js"], {"_headers": site.headers()})  # only scripts count
        with self.assertRaisesRegex(ContentError, "no cookies"):
            site.check_privacy([], {"_headers": site.headers() + "  Set-Cookie: a=b\n"})

    # 6
    def test_privacy_contact_invalid_falls_back_then_fixed_sentence(self):
        self.configure(privacy_contact="not-an-email")
        self.refused("site.json.privacy_contact: invalid email address")
        self.configure(privacy_contact=None, contact_email="team@example.invalid")
        self.build()
        self.assertIn('Privacy contact: <a href="mailto:team@example.invalid">team@example.invalid</a>', self.read("privacy.html"))
        self.configure(privacy_contact="privacy@example.invalid")
        self.build()
        page = self.read("privacy.html")
        self.assertIn('mailto:privacy@example.invalid"', page)
        self.assertNotIn("mailto:team@example.invalid", page.split('<footer')[0])
        self.configure(privacy_contact=None, contact_email=None)
        self.build()
        self.assertIn("This campaign has not published a privacy contact.", self.read("privacy.html"))
        self.assertNotIn("mailto:", self.read("privacy.html"))

    # 7
    def test_signup_privacy_url_refused_by_field_name(self):
        for url in ("https://evil.example.invalid/privacy", "https://sibforms.com/privacy", "http://x.sibforms.com/privacy", "x.sibforms.com", 7):
            with self.subTest(url=url):
                self.configure(map_config=False, signup=dict(SIGNUP, privacy_url=url))
                self.refused("site.json.signup.privacy_url")
        self.configure(map_config=False, signup={"mode": "none", "privacy_url": "http://x.sibforms.com/p"})
        self.refused("site.json.signup.privacy_url")

    # 8
    def test_page_slug_privacy_is_reserved_and_footer_and_sitemap(self):
        pages = self.content / "pages"
        pages.mkdir()
        (pages / "privacy.json").write_text(json.dumps({"title": "Mine", "body_md": "x"}))
        self.refused("pages/privacy.json: slug privacy is reserved")
        shutil.rmtree(pages)
        written = {p.relative_to(self.public).as_posix() for p in self.build()}
        self.assertIn("privacy.html", written)
        for name in written:
            if name.endswith(".html"):
                footer = self.read(name).split("<footer", 1)[1]
                self.assertIn('<p><a href="/privacy.html">Privacy</a></p>', footer, name)
        self.assertIn("<loc>https://cedar-records.example.invalid/privacy.html</loc>", self.read("sitemap.xml"))
        self.configure(base_url=None)
        written = {p.relative_to(self.public).as_posix() for p in self.build()}
        self.assertNotIn("sitemap.xml", written)
        self.assertIn('<a href="/privacy.html">Privacy</a>', self.read("index.html"))

    def test_public_asset_folder_named_privacy_html_collides(self):
        (self.content / "public-assets" / "privacy.html").mkdir(parents=True)
        (self.content / "public-assets" / "privacy.html" / "a.png").write_bytes(b"x")
        self.refused("public-assets/privacy.html/a.png: collides with a generated path")

    # 9
    def test_label_override_changes_wording_not_facts(self):
        self.configure(signup=SIGNUP)
        before = self.facts()
        self.build()
        (self.content / "labels.json").write_text(json.dumps({"schema_version": 1, "labels": {
            "privacy.cookies": "Sin cookies ni analitica.", "footer.privacy": "Privacidad",
            "privacy.map": "Mapa desde: {hosts}.", "privacy.contact": "Contacto:"}}))
        self.build()
        page = self.read("privacy.html")
        self.assertIn("Sin cookies ni analitica.", page)
        self.assertIn("Mapa desde: tiles.example.invalid.", page)
        self.assertNotIn("sets no cookies", page)
        self.assertIn('<a href="/privacy.html">Privacidad</a>', self.read("index.html"))
        self.assertEqual(self.facts(), before)
        self.assertIn("synthetic.sibforms.com", page)

    def test_unknown_privacy_label_is_refused(self):
        (self.content / "labels.json").write_text(json.dumps({"schema_version": 1, "labels": {"privacy.bogus": "x"}}))
        self.refused("labels.json.labels.privacy.bogus: unknown label")


if __name__ == "__main__":
    unittest.main()
