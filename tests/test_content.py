"""Content-model validation: synthetic fixtures only, no network."""
import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from campaign_tool.content import ContentError, load_content, load_finding, load_site, load_sources

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"


def example_finding():
    return json.loads((EXAMPLE / "content" / "findings" / "cedar-policy-posted.json").read_text())


def example_site():
    return json.loads((EXAMPLE / "content" / "site.json").read_text())


class FindingValidationTests(unittest.TestCase):
    def test_example_finding_loads(self):
        finding = load_finding(example_finding(), "f")
        self.assertEqual(finding["slug"], "cedar-policy-posted")
        self.assertEqual(finding["sources"][0]["sha256"][:12], "cafecafecafe")

    def test_needs_attorney_review_is_refused_with_clear_error(self):
        finding = {**example_finding(), "confidence": "needs_attorney_review"}
        with self.assertRaises(ContentError) as caught:
            load_finding(finding, "findings/x.json")
        self.assertIn("needs_attorney_review", str(caught.exception))
        self.assertIn("refuses", str(caught.exception))

    def test_unpublished_states_are_refused(self):
        for state in ("draft", "review", "retracted", "", None):
            with self.subTest(state=state), self.assertRaises(ContentError) as caught:
                load_finding({**example_finding(), "state": state}, "f")
            self.assertIn("published", str(caught.exception))

    def test_review_blocker_fields_are_enforced(self):
        for field in ("author", "limitations", "counterevidence", "sources", "summary"):
            finding = example_finding()
            del finding[field]
            with self.subTest(field=field), self.assertRaises(ContentError):
                load_finding(finding, "f")
        finding = example_finding()
        finding["sources"][0]["sha256"] = "A" * 64
        with self.assertRaises(ContentError):
            load_finding(finding, "f")
        finding = example_finding()
        finding["sources"] = []
        with self.assertRaises(ContentError):
            load_finding(finding, "f")

    def test_conflict_classifications_need_rule_fields(self):
        finding = {**example_finding(), "classification": "apparent_conflict"}
        with self.assertRaises(ContentError) as caught:
            load_finding(finding, "f")
        self.assertIn("missing_rule_version", str(caught.exception))
        finding.update({"rule_version": "synthetic-rule-v1", "duty": "Synthetic duty", "exceptions": ["none"]})
        self.assertEqual(load_finding(finding, "f")["classification"], "apparent_conflict")

    def test_unknown_fields_markup_and_reserved_slugs_are_refused(self):
        with self.assertRaises(ContentError):
            load_finding({**example_finding(), "private_note": "x"}, "f")
        for field in ("title", "summary", "body_md"):
            with self.subTest(field=field), self.assertRaises(ContentError):
                load_finding({**example_finding(), field: "<script>alert(1)</script>"}, "f")
        for slug in ("index", "sources", "Bad Slug", "../x"):
            with self.subTest(slug=slug), self.assertRaises(ContentError):
                load_finding({**example_finding(), "slug": slug}, "f")
        with self.assertRaises(ContentError):
            load_finding({**example_finding(), "event_date": "January 2026"}, "f")


class SiteAndSourcesTests(unittest.TestCase):
    def test_signup_allowlist(self):
        site = example_site()
        site["signup"] = {"mode": "brevo_hosted",
                          "form_html_allowlisted_url": "https://synthetic.sibforms.com/serve/synthetic-form"}
        self.assertEqual(load_site(site)["signup"]["url"], "https://synthetic.sibforms.com/serve/synthetic-form")
        for url in ("https://example.invalid/form", "http://synthetic.sibforms.com/x",
                    "https://sibforms.com/x", "https://evil.invalid/sibforms.com/x", None):
            site["signup"] = {"mode": "brevo_hosted", "form_html_allowlisted_url": url}
            with self.subTest(url=url), self.assertRaises(ContentError):
                load_site(site)
        site["signup"] = {"mode": "mailchimp"}
        with self.assertRaises(ContentError):
            load_site(site)

    def test_site_text_rules(self):
        for field, value in (("tagline", "<b>x</b>"), ("about_md", "<img src=x>"), ("language", 'en" onload="x'),
                             ("contact_email", "not-an-email"), ("base_url", "http://plain.invalid"),
                             ("font_family", "url(x)")):
            with self.subTest(field=field), self.assertRaises(ContentError):
                load_site({**example_site(), field: value})
        site = example_site()
        site["social"] = [{"label": "x", "url": "javascript:alert(1)"}]
        with self.assertRaises(ContentError):
            load_site(site)

    def test_sources_validation(self):
        entries = json.loads((EXAMPLE / "content" / "sources.json").read_text())
        self.assertEqual(len(load_sources(entries)), 2)
        bad = copy.deepcopy(entries)
        bad[0]["fragment"] = "library"
        with self.assertRaises(ContentError):
            load_sources(bad)
        bad = copy.deepcopy(entries)
        bad[1]["sha256"] = bad[0]["sha256"]
        with self.assertRaises(ContentError):
            load_sources(bad)
        bad = copy.deepcopy(entries)
        bad[0]["public_url"] = "http://example.invalid/x"
        with self.assertRaises(ContentError):
            load_sources(bad)


class DirectoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="synthetic-content-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))

    def test_missing_content_returns_none(self):
        shutil.rmtree(self.root / "content")
        self.assertIsNone(load_content(self.root))

    def test_example_model(self):
        model = load_content(self.root)
        self.assertEqual([f["slug"] for f in model["findings"]], ["cedar-policy-posted"])
        self.assertEqual(len(model["meetings"]), 1)
        self.assertEqual({a["slug"] for a in model["agencies"]}, {"cedar-county-sheriff", "cedar", "cedar-city-council"})
        self.assertEqual(model["map"]["camera_count"], 3)
        self.assertEqual(model["map"]["tile_hosts"], ["tiles.example.invalid"])
        self.assertEqual(model["vendor"], {})

    def test_map_requires_local_files_and_known_slugs(self):
        (self.root / "content" / "map" / "cameras.geojson").unlink()
        with self.assertRaises(ContentError) as caught:
            load_content(self.root)
        self.assertIn("cameras.geojson", str(caught.exception))

    def test_camera_properties_are_validated(self):
        path = self.root / "content" / "map" / "cameras.geojson"
        data = json.loads(path.read_text())
        data["features"][0]["properties"]["url"] = "https://example.invalid/not-osm"
        path.write_text(json.dumps(data))
        with self.assertRaises(ContentError):
            load_content(self.root)
        data["features"][0]["properties"]["url"] = "https://www.openstreetmap.org/node/1"
        data["features"][0]["properties"]["operator"] = "<img src=x>"
        path.write_text(json.dumps(data))
        with self.assertRaises(ContentError):
            load_content(self.root)

    def test_kit_agencies_are_the_default(self):
        (self.root / "content" / "agencies.json").unlink()
        with self.assertRaises(ContentError):
            load_content(self.root)  # the map fallback slug no longer names a known city agency
        shutil.rmtree(self.root / "content" / "map")
        self.assertEqual(load_content(self.root)["agencies"], [])
        kit = self.root / "kit"
        kit.mkdir()
        (kit / "agencies.json").write_text(json.dumps([
            {"agency_id": "ca-cedar-police", "name": "Cedar PD (fictional)", "kind": "police",
             "jurisdiction_name": "Cedar", "selected": True, "slug": "cedar"},
            {"agency_id": "ca-skip-police", "name": "Skipped", "kind": "police", "selected": False},
        ]))
        (kit / "governing-bodies.json").write_text(json.dumps([
            {"agency_id": "ca-cedar-city-council", "name": "Council (fictional)", "kind": "city_council"}]))
        agencies = load_content(self.root)["agencies"]
        self.assertEqual([a["agency_id"] for a in agencies], ["ca-cedar-police", "ca-cedar-city-council"])

    def test_site_json_required_when_content_exists(self):
        (self.root / "content" / "site.json").unlink()
        with self.assertRaises(ContentError):
            load_content(self.root)


if __name__ == "__main__":
    unittest.main()
