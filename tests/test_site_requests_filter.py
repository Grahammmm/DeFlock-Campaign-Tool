"""DF1622-08: the requests-table agency/state filter (synthetic fixtures, no network, no private kit files)."""
import hashlib
import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path

from campaign_tool.content import ContentError
from campaign_tool.site import GENERATED_TOP_LEVEL, REQUESTS_FILTER_ASSET, REQUESTS_FILTER_CSS, build_site
from tests.test_site_requests import COUNCIL, EXAMPLE, MIXED, POLICE, SHERIFF, TODAY, item

# requests.html bytes captured on main before DF1622-08 (fixed day 2026-11-01); the filter must not change a disabled page.
MAIN_EMPTY_SHA = "fdae59bb65fb799a7615dda9a8886c322190de29c570aaa54345cc1da63c689f"  # pragma: allowlist secret - nonsecret page digest
MAIN_ONE_SHA = "87dfa84ef463c86c2792708cf385749334c6496d0d22312ad0f2650aaec624c6"  # pragma: allowlist secret - nonsecret page digest
ONE = {POLICE: [item("Plain", "2026-10-01")]}
FORM = re.compile(r'<form class="sources-filter" id="requests-filter"[^>]*>')


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class RequestsFilterTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-requests-filter-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.public = self.root / "public"

    def asked(self, by_agency):
        path = self.root / "content" / "agencies.json"
        agencies = json.loads(path.read_text(encoding="utf-8"))
        for agency in agencies:
            agency.pop("asked", None)
            if agency["agency_id"] in by_agency:
                agency["asked"] = by_agency[agency["agency_id"]]
        path.write_text(json.dumps(agencies), encoding="utf-8")

    def build(self, by_agency, labels=None):
        self.asked(by_agency)
        if labels is not None:
            (self.root / "content" / "labels.json").write_text(json.dumps({"schema_version": 1, "labels": labels}), encoding="utf-8")
        build_site(self.root, today=TODAY)
        return {p.relative_to(self.public).as_posix(): p.read_bytes() for p in self.public.rglob("*") if p.is_file()}

    def page(self, files):
        return files["requests.html"].decode("utf-8")

    def test_threshold_truth_table_and_main_parity(self):
        empty = self.build({})
        self.assertEqual(sha(self.page(empty)), MAIN_EMPTY_SHA)
        self.assertNotIn("requests/filter.js", empty)
        one = self.build(ONE)
        self.assertEqual(sha(self.page(one)), MAIN_ONE_SHA)
        self.assertNotIn("requests/filter.js", one)
        self.assertNotIn(b"requests-table", one["style.css"])
        self.assertNotIn("requests-filter", self.page(one))
        cases = {"two agencies one state": {SHERIFF: [item("A", "2026-10-01")], POLICE: [item("B", "2026-10-02")]},
                 "one agency two states": {POLICE: [item("A", "2026-10-01"), item("B", "2026-10-02", "withdrawn")]},
                 "both vary": MIXED}
        for name, data in cases.items():
            files = self.build(data)
            self.assertIn("requests/filter.js", files, name)
            self.assertRegex(self.page(files), FORM, name)
            self.assertIn('<script defer src="/requests/filter.js"></script>', self.page(files), name)

    def test_options_slug_state_order_and_full_nojs_table(self):
        plain = self.build(MIXED)
        page = self.page(plain)
        self.assertIn('<script defer src="/requests/filter.js"></script>', page)
        agencies = re.search(r'<select id="requests-filter-agency".*?</select>', page, re.S).group(0)
        states = re.search(r'<select id="requests-filter-state".*?</select>', page, re.S).group(0)
        self.assertEqual(re.findall(r'<option value="([^"]*)"', agencies)[0], "all")
        self.assertEqual(len(re.findall(r"<option", agencies)), 4)  # all + the 3 observed agencies
        slugs = set(re.findall(r'<tr data-agency="([^"]+)"', page))
        self.assertEqual(len(slugs), 3)
        self.assertEqual(set(re.findall(r'<option value="([^"]*)"', agencies)) - {"all"}, slugs)
        self.assertFalse(slugs & set(MIXED))  # values are the agency slug, not the agency_id
        self.assertEqual(re.findall(r'<option value="([^"]*)"', states),
                         ["all", "awaiting_response", "acknowledged", "partly_answered", "answered", "withdrawn"])
        self.assertTrue(FORM.search(page).group(0).endswith(" hidden>"))
        self.assertIn("Showing 6 of 6", page)
        self.assertRegex(page, r'<p class="sources-filter-empty" id="requests-filter-empty" hidden>No public request matches these filters\.</p>')
        body = page.split("<tbody>")[1].split("</tbody>")[0]
        self.assertEqual(body.count("<tr"), 6)  # the full table, duplicates included, no row hidden
        self.assertNotIn("hidden", body)
        self.assertEqual(len(re.findall(r'<tr data-agency="[^"]+" data-state="[^"]+">', body)), 6)
        self.assertEqual(page.count('id="requests-table"'), 1)
        # an agency with zero asked rows is not an option, and one varying dimension still shows both selects
        two = self.page(self.build({POLICE: [item("A", "2026-10-01"), item("B", "2026-10-02", "answered")]}))
        self.assertIn('id="requests-filter-agency"', two)
        self.assertEqual(len(re.findall(r"<option", re.search(r'<select id="requests-filter-agency".*?</select>', two, re.S).group(0))), 2)

    def test_asset_only_on_requests_and_stale_cleanup(self):
        first = self.build(MIXED)
        self.assertEqual(first["requests/filter.js"], REQUESTS_FILTER_ASSET.read_bytes())
        self.assertLess(len(first["requests/filter.js"]), 4096)
        for name, data in first.items():
            if name != "requests.html":
                self.assertNotIn(b"/requests/filter.js", data, name)
        self.assertTrue(first["style.css"].endswith(REQUESTS_FILTER_CSS.encode()))
        self.assertIn("display:table-row!important", REQUESTS_FILTER_CSS)
        self.assertRegex(first["style.css"].decode(), r"@media print\{[^}]*\.sources-filter")
        again = self.build(MIXED)
        self.assertTrue(first == again, "a fixed-day rebuild changed bytes")
        bare = self.build(ONE)  # a later below-threshold build drops the stale script through the normal swap
        self.assertNotIn("requests/filter.js", bare)
        self.assertFalse((self.public / "requests").exists())
        for name in ("_headers", "robots.txt", "feed.xml", "site-data.js", "app.js", "search.js"):
            self.assertTrue(bare[name] == first[name], name)
        self.assertEqual(set(first) - set(bare), {"requests/filter.js"})
        self.assertEqual(first["_headers"], bare["_headers"])  # CSP unchanged

    def test_reserved_path_is_refused(self):
        self.assertIn("requests", GENERATED_TOP_LEVEL)
        self.asked(MIXED)
        assets = self.root / "content" / "public-assets" / "requests"
        assets.mkdir(parents=True)
        (assets / "filter.png").write_bytes(b"x")
        with self.assertRaisesRegex(ContentError, "public-assets/requests/filter.png: collides with a generated path"):
            build_site(self.root, today=TODAY)
        self.assertFalse(self.public.exists())  # refused before any publication

    def test_labels_page_local_and_legacy_bytes(self):
        base = self.build(MIXED)
        labels = {"js.requests_label": 'Say "hi"', "js.requests_agency": "Org\\x", "js.requests_state": "Phase",
                  "js.requests_all_agencies": "Every org", "js.requests_all_states": "Any phase",
                  "js.requests_count": "{shown}/{total} rows", "js.requests_empty": "None here"}
        files = self.build(MIXED, labels)
        page = self.page(files)
        for text in ('aria-label="Say &quot;hi&quot;"', ">Org\\x<", ">Phase<", ">Every org<", ">Any phase<",
                     'data-count-template="{shown}/{total} rows"', ">6/6 rows<", ">None here<"):
            self.assertIn(text, page)
        for name in set(base) - {"requests.html"}:  # no other page, script or site-data.js sees these keys
            self.assertTrue(files[name] == base[name], name)
        disabled = self.build(ONE, labels)
        self.assertEqual(sha(self.page(disabled)), MAIN_ONE_SHA)
        self.assertNotIn("requests/filter.js", disabled)
        with self.assertRaises(ContentError):
            self.build(MIXED, {"js.requests_count": "{shown} of {nope}"})


if __name__ == "__main__":
    unittest.main()
