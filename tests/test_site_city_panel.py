"""Map city panel: agency page link and published-finding count (DF1507-16).

The label lives in the panel markup (data-label on #city-agency-link), never in site-data.js or
agency-cards.js. Built from a temp copy of the fictional example; synthetic content only; no network.
"""
import json
import re
import shutil
import tempfile
import unittest
from datetime import date
from pathlib import Path

from campaign_tool.site import build_site

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
BUILD_DAY = date(2026, 10, 20)
SHERIFF_SOURCE = {"sha256": "ab" * 32, "title": "Synthetic sheriff contract", "agency_id": "ca-cedar-county-sheriff",
                  "received_at": "2026-02-01T10:00:00+00:00", "public_url": None, "fragment": "sheriff-records"}


class CityPanelTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-city-panel-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.content = self.root / "content"
        self.public = self.root / "public"

    def edit(self, name, change):
        path = self.content / name
        data = json.loads(path.read_text(encoding="utf-8"))
        change(data)
        path.write_text(json.dumps(data), encoding="utf-8")

    def build(self):
        build_site(self.root, today=BUILD_DAY)
        return {name: (self.public / name).read_text(encoding="utf-8")
                for name in ("index.html", "site-data.js", "agency-cards.js")}

    def cities(self, data_js):
        match = re.search(r"^var CITIES=(.*);$", data_js, flags=re.M)
        assert match is not None
        return json.loads(match.group(1))

    def anchor(self, index):
        match = re.search(r'<a class="text-link" id="city-agency-link"[^>]*></a>', index)
        assert match is not None
        return match.group(0)

    def test_page_agencies_carry_link_and_count_others_neither(self):
        self.edit("sources.json", lambda d: d.append(SHERIFF_SOURCE))
        out = self.build()
        cities = self.cities(out["site-data.js"])
        by_page = {slug: (c.get("agencyPage"), c.get("findingCount")) for slug, c in cities.items() if "agencyPage" in c}
        self.assertEqual(by_page, {"cedar": ("/agencies/cedar.html", 1)})
        self.assertGreater(len(cities), 1)  # the group and the no-page agencies carry neither key
        for c in cities.values():
            self.assertEqual("findingCount" in c, "agencyPage" in c)
            self.assertNotIn("agencyPageLabel", c)
        self.assertEqual(self.anchor(out["index.html"]),
                         '<a class="text-link" id="city-agency-link" data-label="Agency page" hidden></a>')
        for name in ("site-data.js", "agency-cards.js"):
            self.assertNotIn("Agency page", out[name], name)

    def test_finding_count_counts_model_findings(self):
        finding = json.loads((self.content / "findings" / "cedar-policy-posted.json").read_text(encoding="utf-8"))
        finding.update(id="fnd_0000000000000002", slug="cedar-second", title="A second synthetic Cedar finding")
        (self.content / "findings" / "cedar-second.json").write_text(json.dumps(finding), encoding="utf-8")
        self.assertEqual(self.cities(self.build()["site-data.js"])["cedar"]["findingCount"], 2)
        for name in ("cedar-second.json", "cedar-policy-posted.json"):
            (self.content / "findings" / name).unlink()
        cedar = self.cities(self.build()["site-data.js"])["cedar"]  # sources alone still give a page
        self.assertEqual((cedar["agencyPage"], cedar["findingCount"]), ("/agencies/cedar.html", 0))

    def test_label_is_escaped_markup_from_labels(self):
        labels = {"schema_version": 1, "labels": {"agency.page": 'Zq "&" Zq'}}
        (self.content / "labels.json").write_text(json.dumps(labels), encoding="utf-8")
        out = self.build()
        self.assertIn('data-label="Zq &quot;&amp;&quot; Zq"', self.anchor(out["index.html"]))
        self.assertNotIn("Zq", out["site-data.js"])

    def test_no_agency_pages_no_attribute_no_keys(self):
        (self.content / "findings" / "cedar-policy-posted.json").unlink()
        (self.content / "sources.json").write_text("[]", encoding="utf-8")
        out = self.build()
        self.assertEqual(self.anchor(out["index.html"]), '<a class="text-link" id="city-agency-link" hidden></a>')
        self.assertNotIn("data-label", out["index.html"])
        for c in self.cities(out["site-data.js"]).values():
            self.assertNotIn("agencyPage", c)
            self.assertNotIn("findingCount", c)

    def test_no_fetch_in_agency_cards(self):
        tmpl = REPO_ROOT / "campaign_tool" / "assets" / "agency-cards.js.tmpl"
        self.assertEqual(tmpl.read_text(encoding="utf-8").count("fetch("), 0)


if __name__ == "__main__":
    unittest.main()
