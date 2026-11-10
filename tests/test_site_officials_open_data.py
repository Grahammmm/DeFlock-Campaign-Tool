"""DF1622-11: data/officials.json and data/officials.csv from content/officials.json (fictional names, .example hosts)."""
import csv
import io
import json
import unittest
from unittest import mock

from campaign_tool import law, site as site_module
from campaign_tool.content import ContentError
from campaign_tool.site import officials_csv, PRESS_DATA_FILES
from tests import test_site_officials as base
from tests.test_site_officials import COUNCIL, COUNCIL_MEETING, COUNCIL_PAGE, REPO_ROOT, official

SCHEMA = json.loads((REPO_ROOT / "schemas" / "officials-open-data.schema.json").read_text(encoding="utf-8"))
EXPORTS = {"data/officials.json", "data/officials.csv"}
PRESS = {"contact_role": "Press lead", "contact_email": "press@example.invalid"}
HEADER = ["agency_id", "agency_name", "name", "role", "profile_url", "source_url", "checked_on"]


class OfficialsOpenDataTests(unittest.TestCase):
    setUp = base.OfficialsTests.setUp  # the same fictional campaign copy, without re-running its tests
    officials, council, build, snapshot, read = (getattr(base.OfficialsTests, n) for n in
                                                  ("officials", "council", "build", "snapshot", "read"))

    def site_json(self, **changes):
        path = self.content / "site.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        for key, value in changes.items():
            if value is None:
                document.pop(key, None)
            else:
                document[key] = value
        path.write_text(json.dumps(document), encoding="utf-8")

    def rows(self):
        return list(csv.reader(io.StringIO(self.read("data/officials.csv"), newline="")))

    def doc(self):
        self.build()
        return json.loads(self.read("data/officials.json"))

    def test_absent_and_empty_roster_write_nothing_and_cleanup(self):
        for press in (None, PRESS):
            self.site_json(press=press)
            self.build()
            without = self.snapshot()
            self.assertFalse(EXPORTS & set(without))
            self.assertFalse(any(b"officials.json" in data or b"officials.csv" in data for data in without.values()))
            self.officials([])  # a valid, empty roster is the same as none
            self.build()
            self.assertEqual(self.snapshot(), without)
            self.council(official())
            self.build()
            self.assertTrue(EXPORTS <= set(self.snapshot()))
            (self.content / "officials.json").unlink()
            self.build()
            self.assertEqual(self.snapshot(), without)  # exports and links vanish, the original tree returns

    def test_exact_projection_order_and_body_without_page(self):
        other = "ca-cedar-county-sheriff"
        self.officials([
            {"agency_id": other, "officials": [official(name="Zed Example", profile_url=None), official(name="Zed Example")]},
            {"agency_id": COUNCIL, "officials": [official(name="Ann Example"), official(name="Bea Example")]}])
        self.site_json(base_url=None)
        doc = self.doc()
        law.validate(doc, SCHEMA)
        self.assertEqual(set(doc), {"schema_version", "campaign", "generated_from", "base_url", "url_kind", "bodies"})
        self.assertEqual((doc["schema_version"], doc["generated_from"], doc["url_kind"], doc["base_url"]),
                         (1, "content/officials.json", "relative", None))
        keys = [(b["name"], b["agency_id"]) for b in doc["bodies"]]
        self.assertEqual(keys, sorted(keys))
        self.assertEqual({b["agency_id"] for b in doc["bodies"]}, {COUNCIL, other})
        for body in doc["bodies"]:
            self.assertEqual(set(body), {"agency_id", "name", "officials"})
            for person in body["officials"]:
                self.assertEqual(set(person), {"name", "role", "profile_url", "source_url", "checked_on"})
        by_id = {b["agency_id"]: b for b in doc["bodies"]}
        self.assertEqual([p["name"] for p in by_id[COUNCIL]["officials"]], ["Ann Example", "Bea Example"])
        self.assertEqual([p["profile_url"] is None for p in by_id[other]["officials"]], [True, False])
        self.assertEqual(len(by_id[other]["officials"]), 2)  # duplicates stay
        self.assertNotIn("vote", json.dumps(doc))
        self.assertTrue(self.read("data/officials.json").endswith("}\n"))

    def test_license_and_absolute_base(self):
        self.site_json(license={"id": "CC0-1.0"})
        self.council(official())
        doc = self.doc()
        law.validate(doc, SCHEMA)
        self.assertEqual(doc["license"], "CC0-1.0")
        self.assertIn("license_url", doc)
        self.site_json(license=None, base_url="https://cedar-records.example.invalid")
        doc = self.doc()
        law.validate(doc, SCHEMA)
        self.assertNotIn("license", doc)
        self.assertEqual((doc["url_kind"], doc["base_url"]), ("absolute", "https://cedar-records.example.invalid"))
        self.assertEqual(doc["bodies"][0]["officials"][0]["profile_url"], "https://cedar.example/council/rowan")

    def test_csv_is_flattened_from_checked_json(self):
        self.council(official(name='Ann, "Q" Example', role="Mayor", profile_url=None), official(name="Bea Ünï"))
        self.build()
        rows = self.rows()
        self.assertEqual(rows[0], HEADER)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[1][:5], [COUNCIL, rows[1][1], 'Ann, "Q" Example', "Mayor", ""])
        self.assertEqual(rows[2][2], "Bea Ünï")
        raw = (self.public / "data/officials.csv").read_bytes()
        self.assertTrue(raw.startswith((",".join(HEADER) + "\r\n").encode()))
        alternate = json.dumps({"bodies": [{"agency_id": "x", "name": "Only JSON", "officials": [
            {"name": "Json Person", "role": "r", "profile_url": None, "source_url": "https://a.example", "checked_on": "2026-01-01"}]}]})
        with mock.patch.object(site_module.Site, "officials_json", return_value=alternate):
            self.build()
        self.assertEqual(self.rows()[1], ["x", "Only JSON", "Json Person", "r", "", "https://a.example", "2026-01-01"])

    def test_formula_guard_all_prefixes_once(self):
        body = lambda value: json.dumps({"bodies": [{"agency_id": "a", "name": value, "officials": [
            {"name": value, "role": "=r", "profile_url": None, "source_url": "https://a.example", "checked_on": "d"}]}]})
        for prefix in ("=", "+", "-", "@", "\t", "\r"):
            with self.subTest(repr(prefix)):
                row = list(csv.reader(io.StringIO(officials_csv(body(prefix + "x=y")), newline="")))[1]
                self.assertEqual((row[1], row[2], row[3]), ("'" + prefix + "x=y",) * 2 + ("'=r",))
        row = list(csv.reader(io.StringIO(officials_csv(body("a=b+c")), newline="")))[1]
        self.assertEqual(row[2], "a=b+c")
        self.council(official(name="=Ann Example", role="+Mayor"))
        self.build()
        self.assertEqual(self.rows()[1][2:4], ["'=Ann Example", "'+Mayor"])
        self.assertEqual(json.loads(self.read("data/officials.json"))["bodies"][0]["officials"][0]["name"], "=Ann Example")

    def test_nested_allowlists_refuse_before_write(self):
        self.council(official())
        self.build()
        before = self.snapshot()
        real = site_module.Site.officials_json

        def leaking(change):
            def build(site):
                original = site_module._assert_public
                def wrapped(doc, name, top, nested):
                    change(doc)
                    return original(doc, name, top, nested)
                with mock.patch.object(site_module, "_assert_public", wrapped):
                    return real(site)
            return build
        cases = {
            "top": lambda d: d.update(contact="x"), "body": lambda d: d["bodies"][0].update(notes="x"),
            "person": lambda d: d["bodies"][0]["officials"][0].update(votes=[]),
            "case": lambda d: d["bodies"][0]["officials"][0].update(Email="x")}
        for label, change in cases.items():
            with self.subTest(label), mock.patch.object(site_module.Site, "officials_json", leaking(change)):
                with self.assertRaises(ContentError) as caught:
                    self.build()
                self.assertIn("data/officials.json: non-public key", str(caught.exception))
                self.assertEqual(self.snapshot(), before)
        with mock.patch.object(site_module.Site, "officials_json", leaking(
                lambda d: d["bodies"][0]["officials"][0].update(role="a" * 64))):
            with self.assertRaises(ContentError) as caught:
                self.build()
            self.assertIn("full hash", str(caught.exception))
            self.assertEqual(self.snapshot(), before)

    def test_agencies_press_links_licences_and_labels(self):
        link_json = '<a href="/data/officials.json" type="application/json">Officials as open data (JSON)</a>'
        self.council(official())
        self.build()
        agencies = self.read("agencies.html")
        self.assertEqual(agencies.count(link_json), 1)
        self.assertIn('<a href="/data/officials.csv" type="text/csv">', agencies)
        self.assertNotIn("press.html", self.snapshot())
        self.site_json(press=PRESS, license={"id": "CC-BY-4.0"})
        self.build()
        press = self.read("press.html")
        for name in sorted(EXPORTS):
            self.assertEqual(press.count(f'href="/{name}"'), 1)
        self.assertIn("CC BY 4.0", self.read("agencies.html").split("data/officials.csv", 1)[1].split("</p>", 1)[0])
        self.assertEqual(sorted(n for n in PRESS_DATA_FILES if n.startswith("data/officials")), sorted(EXPORTS))
        (self.content / "labels.json").write_text(json.dumps({"schema_version": 1, "labels": {
            "intro.officials_open_data": "X&Y"}}), encoding="utf-8")
        self.build()
        self.assertIn('>X&amp;Y</a>', self.read("agencies.html"))
        es = json.loads((REPO_ROOT / "examples" / "labels" / "es.json").read_text(encoding="utf-8"))
        self.assertEqual(es["labels"]["intro.officials_open_data"] if "labels" in es else es["intro.officials_open_data"],
                         "Funcionarios como datos abiertos (JSON)")

    def test_disabled_label_override_changes_no_file(self):
        self.build()
        before = self.snapshot()
        (self.content / "labels.json").write_text(json.dumps({"schema_version": 1, "labels": {
            "intro.officials_open_data": "Other"}}), encoding="utf-8")
        self.build()
        self.assertEqual(self.snapshot(), before)

    def test_enabled_changes_only_expected_old_files_and_repeats(self):
        self.build()
        before = self.snapshot()
        self.council(official())
        self.build()
        first = self.snapshot()
        self.build()
        self.assertEqual(self.snapshot(), first)
        self.assertEqual(set(first) - set(before), EXPORTS)
        changed = {n for n in before if before[n] != first[n]}
        self.assertEqual(changed, {COUNCIL_PAGE, COUNCIL_MEETING, "agencies.html", "data.html", "data/datapackage.json"})  # DF1109-10
        self.assertEqual(before["data/meetings.json"], first["data/meetings.json"])
        self.assertEqual(before["data/meetings.csv"], first["data/meetings.csv"])

    def test_loader_refusals_leave_no_exports(self):
        self.council(official(profile_url="http://cedar.example/x"))
        with self.assertRaises(ContentError):
            self.build()
        self.assertFalse(EXPORTS & set(self.snapshot()))


if __name__ == "__main__":
    unittest.main()
