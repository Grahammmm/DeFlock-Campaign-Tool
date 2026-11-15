"""DF2007-13: optional public answer details on an asked item (fee quote, extension, withheld sections, no_records_stated).

Synthetic content only (temp copies of the fictional example); no network.
"""
import hashlib
import json
import os
import re
import shutil
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from campaign_tool import kit_track, law
from campaign_tool.content import (ASKED_AMOUNT, ASKED_STATES, ContentError, _asked, _asked_extension,
                                   _asked_fee_quoted, _asked_withheld_sections, load_content)
from campaign_tool.content_check import content_problems
from campaign_tool.labels import DEFAULTS, load_labels
from campaign_tool.site import PUBLIC_ASKED_FIELDS, Site, build_site

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
SCHEMA = json.loads((REPO_ROOT / "schemas" / "agencies-open-data.schema.json").read_text(encoding="utf-8"))
TODAY = date(2026, 11, 1)
SHERIFF, CEDAR = 0, 1  # index in the example's agencies.json: the sheriff has no individual page, Cedar PD has
BASE = {"topic": "Camera policy", "asked_on": "2026-09-01", "state": "answered", "updated_on": "2026-10-01"}
EXTENSION_SENTENCE = "On 2026-11-01 the agency said it would respond by 2026-11-15."
FEE_SENTENCE = "The agency quoted USD 12.50; recorded fee status: unpaid."
SECTIONS_SENTENCE = "The agency cited Government Code sections 7923.600, 7923.615 when withholding records."
NEW_LABELS = ("requests.state_no_records_stated", "requests.no_records_sentence", "requests.no_records_sentence_dated",
              "requests.fee_quoted", "requests.fee_status_unpaid", "requests.fee_status_paid",
              "requests.fee_status_waived", "requests.extension", "requests.withheld_sections")
# Whole-tree digests of builds on the day TODAY, taken on main before DF2007-13 (see LEGACY_ASKED below).
MAIN_EXAMPLE_DIGEST = "4982bb17cf806741c818d045af57cbc9a5cf623c84aea46cc4f9a6b1f23866cd"
MAIN_LEGACY_DIGEST = "77d898de7dfb50005cb00deac82211cd7929d91c909ea5a3a28eecbb163b8266"
LEGACY_ASKED = {CEDAR: [{"topic": "ALPR retention policy", "asked_on": "2026-08-01", "state": "answered", "updated_on": "2026-09-01"},
                        {"topic": "Data-sharing list", "asked_on": "2026-10-01", "state": "awaiting_response"},
                        {"topic": "Camera count", "asked_on": "2026-09-15", "state": "declined_in_writing",
                         "updated_on": "2026-10-20", "note": "Cited an exemption."},
                        {"topic": "Ack", "asked_on": "2026-10-05", "state": "acknowledged", "updated_on": "2026-10-06"},
                        {"topic": "Part", "asked_on": "2026-10-06", "state": "partly_answered"},
                        {"topic": "Gone", "asked_on": "2026-10-07", "state": "withdrawn", "updated_on": "2026-10-08"}],
                SHERIFF: [{"topic": "Sheriff log", "asked_on": "2026-09-01", "state": "answered", "updated_on": "2026-10-05"}]}


def tree(public):
    return {p.relative_to(public).as_posix(): (p.read_bytes(), p.stat().st_mode) for p in sorted(public.rglob("*")) if p.is_file()}


def digest(public):
    sha = hashlib.sha256()
    for name, (data, _) in tree(public).items():
        sha.update(name.encode() + b"\0" + data + b"\0")
    return sha.hexdigest()


class AnswerDetailTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-asked-outcomes-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.path = self.root / "content" / "agencies.json"
        self.public = self.root / "public"

    def asked(self, by_index):
        agencies = json.loads(self.path.read_text(encoding="utf-8"))
        for index, items in by_index.items():
            agencies[index]["asked"] = items
        self.path.write_text(json.dumps(agencies), encoding="utf-8")

    def build(self, today=TODAY):
        build_site(self.root, today=today)
        return tree(self.public)

    def text(self, name):
        return (self.public / name).read_text(encoding="utf-8")

    def problems(self, today=TODAY):
        return content_problems(self.root, today=today) or []

    def refused(self, **fields):
        """Load and content-check an item with ``fields`` added; return (loader message, content-check lines)."""
        self.asked({CEDAR: [{**BASE, **fields}]})
        with self.assertRaises(ContentError) as caught:
            load_content(self.root, TODAY)
        return str(caught.exception), self.problems()

    def assertRefused(self, field, **fields):
        message, lines = self.refused(**fields)
        where = f"content/agencies.json[{CEDAR}].asked[0].{field}"
        self.assertIn(where, message)
        self.assertTrue(any(where in line for line in lines), lines)
        with self.assertRaises(ContentError):
            build_site(self.root, today=TODAY)

    # --- fee_quoted -----------------------------------------------------------------------------------------------
    def test_fee_quoted_shape_amount_status(self):
        self.assertEqual(ASKED_AMOUNT.pattern, kit_track.AMOUNT.pattern)  # the documented grammar, not a drifted copy
        for amount in ("12.50", "0", "999999", "007", "0.5", "12.5"):
            for status in ("unpaid", "paid", "waived"):
                value = {"amount_usd": amount, "status": status}
                self.assertEqual(_asked_fee_quoted(value, "x.fee_quoted"), value)  # kept exactly as supplied
        for amount in (12.5, 12, True, None, "1000000", "-1", "+1", "1e3", "1,000", "$12", "12.505", "12.", ".5", "", " 1",
                       "1\n", "１２", "NaN"):
            with self.assertRaisesRegex(ContentError, r"x\.fee_quoted\.amount_usd"):
                _asked_fee_quoted({"amount_usd": amount, "status": "paid"}, "x.fee_quoted")
        for status in ("pending", "Paid", "", None, 1, ["paid"]):
            with self.assertRaisesRegex(ContentError, r"x\.fee_quoted\.status"):
                _asked_fee_quoted({"amount_usd": "1", "status": status}, "x.fee_quoted")
        for shape in (None, "12.50", ["12.50"], 12.5):
            with self.assertRaisesRegex(ContentError, r"x\.fee_quoted: must be an object"):
                _asked_fee_quoted(shape, "x.fee_quoted")
        with self.assertRaisesRegex(ContentError, r"x\.fee_quoted\.status: required"):
            _asked_fee_quoted({"amount_usd": "1"}, "x.fee_quoted")
        with self.assertRaisesRegex(ContentError, r"x\.fee_quoted\.amount_usd: required"):
            _asked_fee_quoted({"status": "paid"}, "x.fee_quoted")
        with self.assertRaisesRegex(ContentError, r"x\.fee_quoted: unknown field 'currency'"):
            _asked_fee_quoted({"amount_usd": "1", "status": "paid", "currency": "USD"}, "x.fee_quoted")
        self.assertRefused("fee_quoted.amount_usd", fee_quoted={"amount_usd": "-3", "status": "paid"})
        self.assertRefused("fee_quoted.status", fee_quoted={"amount_usd": "3", "status": "owed"})
        self.assertRefused("fee_quoted", fee_quoted=None)

    # --- extension ------------------------------------------------------------------------------------------------
    def test_extension_future_deadline_passes_check_and_build(self):
        self.asked({CEDAR: [{**BASE, "extension": {"noticed_on": "2026-11-01", "until": "2026-11-15"}}]})
        self.assertEqual(self.problems(), [])  # content-check
        loaded = load_content(self.root, TODAY)  # the loader
        extension = [a for a in loaded["agencies"] if a["slug"] == "cedar"][0]["asked"][0]["extension"]
        self.assertEqual(extension, {"noticed_on": "2026-11-01", "until": "2026-11-15"})  # never clipped or rewritten
        self.build()
        for page in ("requests.html", "agencies.html"):
            self.assertEqual(self.text(page).count(EXTENSION_SENTENCE), 1, page)
        self.assertNotIn(EXTENSION_SENTENCE, self.text("agencies/cedar.html"))  # the individual page lists no asked items
        row = re.search(r"<tr><th[^>]*><a[^>]*>Cedar.*?</tr>", self.text("requests.html"), re.S).group(0)
        self.assertIn("Answered 2026-10-01. " + EXTENSION_SENTENCE + "</td>", row)  # after the state sentence
        data = json.loads(self.text("data/agencies.json"))
        cedar = [a for a in data["agencies"] if a["agency_id"] == "ca-cedar-police"][0]
        self.assertEqual(cedar["asked"][0]["extension"], {"noticed_on": "2026-11-01", "until": "2026-11-15"})
        law.validate(data, SCHEMA)

    def test_extension_event_and_deadline_boundaries(self):
        ok = lambda notice, until: _asked_extension({"noticed_on": notice, "until": until}, "x.extension", TODAY)  # noqa: E731
        self.assertEqual(ok("2026-11-01", "2026-11-01"), {"noticed_on": "2026-11-01", "until": "2026-11-01"})  # equal is valid
        ok("2000-01-01", "2000-01-01")
        ok("2024-02-29", "2024-03-01")  # a leap day
        ok("2026-10-31", "2099-12-31")  # no limit on how far ahead the agency's deadline is
        tomorrow = (TODAY + timedelta(days=1)).isoformat()
        with self.assertRaisesRegex(ContentError, r"x\.extension\.noticed_on: must not be after the build date"):
            ok(tomorrow, "2026-12-01")
        with self.assertRaisesRegex(ContentError, r"x\.extension\.until: must not be before noticed_on"):
            ok("2026-10-20", "2026-10-19")
        for bad in ("1999-12-31", "2026-02-30", "2025-02-29", "2026-1-5", "20261101", "2026-11-01T00:00:00", " 2026-11-01",
                    "2026-11-01\n", "26-11-01", "", None, 20261101, True, ["2026-11-01"]):
            with self.assertRaisesRegex(ContentError, r"x\.extension\.noticed_on"):
                ok(bad, "2026-12-01")
            with self.assertRaisesRegex(ContentError, r"x\.extension\.until"):
                ok("2026-10-01", bad)
        for shape in (None, "2026-11-01", ["2026-11-01", "2026-11-15"]):
            with self.assertRaisesRegex(ContentError, r"x\.extension: must be an object"):
                _asked_extension(shape, "x.extension", TODAY)
        with self.assertRaisesRegex(ContentError, r"x\.extension\.until: required"):
            _asked_extension({"noticed_on": "2026-10-01"}, "x.extension", TODAY)
        with self.assertRaisesRegex(ContentError, r"x\.extension: unknown field 'reason'"):
            _asked_extension({"noticed_on": "2026-10-01", "until": "2026-10-02", "reason": "workload"}, "x.extension", TODAY)
        with self.assertRaisesRegex(ContentError, r"x\.extension\.noticed_on"):  # the default build day is today's
            _asked_extension({"noticed_on": (date.today() + timedelta(days=1)).isoformat(), "until": "2999-01-01"}, "x.extension")
        self.assertRefused("extension.noticed_on", extension={"noticed_on": tomorrow, "until": "2026-12-01"})
        self.assertRefused("extension.until", extension={"noticed_on": "2026-10-20", "until": "2026-10-19"})
        self.assertRefused("extension.until", extension={"noticed_on": "2026-10-20", "until": "2026-10-32"})

    def test_extension_refusal_does_not_echo_the_submitted_value(self):
        message, lines = self.refused(extension={"noticed_on": "SECRET-DATE", "until": "2026-11-15"})
        self.assertNotIn("SECRET-DATE", message + "".join(lines))

    # --- withheld_sections ----------------------------------------------------------------------------------------
    def test_sections_numbers_only_bounded_distinct(self):
        for good in (["7923.600"], ["7923"], ["7923.600", "7923.615", "7927.705", "7922.000", "7930.150"]):
            self.assertEqual(_asked_withheld_sections(good, "x.withheld_sections"), good)  # supplied order kept
        for bad in ([], ["7923"] * 2, [f"79{n:02}" for n in range(6)], "7923.600", None, {"a": "7923"}, 7923):
            with self.assertRaisesRegex(ContentError, r"x\.withheld_sections"):
                _asked_withheld_sections(bad, "x.withheld_sections")
        for element in (7923.6, 7923, True, None, "Gov. Code 7923.600", "7923.600(a)", "7923.", "792", "07923.600", "7923.6a",
                        "<b>7923</b>", "https://example.invalid/7923", " 7923", "7923\n", "7923.600 exempt", "٧٩٢٣"):
            with self.assertRaisesRegex(ContentError, r"x\.withheld_sections\[1\]"):
                _asked_withheld_sections(["7923.600", element], "x.withheld_sections")
        with self.assertRaisesRegex(ContentError, r"x\.withheld_sections\[2\]: duplicate"):
            _asked_withheld_sections(["7923.600", "7923.615", "7923.600"], "x.withheld_sections")
        self.assertRefused("withheld_sections[0]", withheld_sections=["Section 7923.600"])
        self.assertRefused("withheld_sections", withheld_sections=[])
        self.asked({CEDAR: [{**BASE, "withheld_sections": ["7923.615", "7923.600"]}]})
        self.assertEqual(self.problems(), [])
        self.build()
        self.assertIn("The agency cited Government Code sections 7923.615, 7923.600 when withholding records.", self.text("requests.html"))
        cedar = [a for a in json.loads(self.text("data/agencies.json"))["agencies"] if a["agency_id"] == "ca-cedar-police"][0]
        self.assertEqual(cedar["asked"][0]["withheld_sections"], ["7923.615", "7923.600"])  # supplied order

    # --- no_records_stated ----------------------------------------------------------------------------------------
    def test_no_records_is_an_agency_statement(self):
        self.assertEqual(ASKED_STATES[-1], "no_records_stated")
        self.assertEqual(ASKED_STATES[:6], ("awaiting_response", "acknowledged", "partly_answered", "answered",
                                            "declined_in_writing", "withdrawn"))
        self.asked({CEDAR: [{"topic": "Dated", "asked_on": "2026-09-01", "state": "no_records_stated", "updated_on": "2026-10-01"},
                            {"topic": "Undated", "asked_on": "2026-09-02", "state": "no_records_stated"},
                            {"topic": "Other", "asked_on": "2026-09-03", "state": "answered"}]})
        self.assertEqual(self.problems(), [])
        self.build()
        page = self.text("requests.html")
        self.assertIn("The agency stated that it had no responsive records on 2026-10-01.", page)
        self.assertIn("The agency stated that it had no responsive records.</td>", page)  # undated: no build day inferred
        self.assertNotIn("no responsive records as of", page)
        lead = re.search(r"<p>3 requests listed\. By state: (.*?)</p>", page).group(1)
        self.assertEqual(re.findall(r"([^·(]+?) \((\d+)\)", lead), [
            ("Awaiting response", "0"), (" Acknowledged", "0"), (" Partly answered", "0"), (" Answered", "1"),
            (" Declined in writing", "0"), (" Withdrawn", "0"), (" No responsive records stated", "2")])
        self.assertEqual(self.text("agencies.html").count("The agency stated that it had no responsive records"), 2)
        self.assertIn("no_records_stated", [a["asked"] for a in json.loads(self.text("data/agencies.json"))["agencies"]
                                            if a["asked"]][0][1]["state"])
        law.validate(json.loads(self.text("data/agencies.json")), SCHEMA)
        self.assertRefused("state", state="no_records")

    # --- every surface, labels and private exclusions -------------------------------------------------------------
    def rich_item(self, **more):
        return {**BASE, "note": "PRIVATE-NOTE", "request": "private-request-1",
                "fee_quoted": {"amount_usd": "12.50", "status": "unpaid"},
                "extension": {"noticed_on": "2026-11-01", "until": "2026-11-15"},
                "withheld_sections": ["7923.600", "7923.615"], **more}

    def test_all_surfaces_labels_and_private_exclusions(self):
        self.asked({CEDAR: [self.rich_item()], SHERIFF: [{**BASE, "topic": "Sheriff only", "fee_quoted": {"amount_usd": "3", "status": "paid"}}]})
        self.assertEqual(self.problems(), [])
        files = self.build()
        full = f"Answered 2026-10-01. {FEE_SENTENCE} {EXTENSION_SENTENCE} {SECTIONS_SENTENCE}"  # fee, extension, sections
        for page in ("requests.html", "agencies.html"):
            self.assertEqual(self.text(page).count(full), 1, page)
        self.assertNotIn(FEE_SENTENCE, self.text("agencies/cedar.html"))  # no page-membership or block expansion
        self.assertEqual(self.text("agencies.html").count("PRIVATE-NOTE"), 1)  # the card shows the note as before
        self.assertNotIn("PRIVATE-NOTE", self.text("requests.html"))  # and the request table still does not
        self.assertIn("The agency quoted USD 3; recorded fee status: paid.", self.text("requests.html"))
        self.assertTrue(files["requests.html"])
        data_text = self.text("data/agencies.json")
        for private in ("PRIVATE-NOTE", "private-request-1"):
            self.assertNotIn(private, data_text)
        self.assertNotIn('"note"', data_text)
        self.assertNotIn('"request"', data_text)
        data = json.loads(data_text)
        law.validate(data, SCHEMA)
        self.assertEqual([a["agency_id"] for a in data["agencies"]], ["ca-cedar-police"])  # the asked-only sheriff stays out
        self.assertEqual(sorted(data["agencies"][0]["asked"][0]), sorted(PUBLIC_ASKED_FIELDS + ("fee_quoted", "extension", "withheld_sections")))
        self.assertEqual(data["agencies"][0]["asked"][0]["fee_quoted"], {"amount_usd": "12.50", "status": "unpaid"})
        self.assertIn('href="/agencies.html#', self.text("requests.html"))  # asked-only agency: the existing card anchor
        self.assertFalse((self.public / "agencies" / "cedar-county-sheriff.html").exists())

    def test_every_new_label_is_replaced_and_escaped_once(self):
        probes = {"requests.state_no_records_stated": "S&1", "requests.no_records_sentence": "NR&1.",
                  "requests.no_records_sentence_dated": "ND&1 {date}.", "requests.fee_quoted": 'F&1 {amount_usd} "{status}"',
                  "requests.fee_status_unpaid": "u&1", "requests.fee_status_paid": "p&1", "requests.fee_status_waived": "w&1",
                  "requests.extension": "E&1 {noticed_on} / {until}", "requests.withheld_sections": "W&1 {sections}"}
        self.assertEqual(sorted(probes), sorted(NEW_LABELS))
        (self.root / "content" / "labels.json").write_text(json.dumps({"schema_version": 1, "labels": probes}), encoding="utf-8")
        self.asked({CEDAR: [self.rich_item(updated_on="2026-10-01"),
                            {"topic": "Dated none", "asked_on": "2026-09-02", "state": "no_records_stated", "updated_on": "2026-10-02"},
                            {"topic": "Bare none", "asked_on": "2026-09-03", "state": "no_records_stated"}]})
        self.build()
        for page in ("requests.html", "agencies.html"):
            text = self.text(page)
            for expected in ('F&amp;1 12.50 &quot;u&amp;1&quot;', "E&amp;1 2026-11-01 / 2026-11-15", "W&amp;1 7923.600, 7923.615",
                             "ND&amp;1 2026-10-02.", "NR&amp;1."):
                self.assertIn(expected, text, page)
            self.assertNotIn("&amp;amp;", text)
            for default in (FEE_SENTENCE, EXTENSION_SENTENCE, SECTIONS_SENTENCE):
                self.assertNotIn(default, text, page)
        self.assertIn("S&amp;1 (2)", self.text("requests.html"))
        for key in NEW_LABELS:  # the validator admits only each default's own placeholders
            with self.assertRaises(ContentError):
                load_labels({"schema_version": 1, "labels": {key: "{unknown}"}})
        for status in ("paid", "waived"):
            self.asked({CEDAR: [{**BASE, "fee_quoted": {"amount_usd": "1", "status": status}}]})
            self.build()
            self.assertIn(f"{status[0]}&amp;1", self.text("requests.html"))
        self.assertTrue(all(key in DEFAULTS for key in NEW_LABELS))

    def test_schema_is_closed_and_export_allowlist_fails_closed_on_nested_keys(self):
        self.asked({CEDAR: [self.rich_item()]})
        self.build()
        data = json.loads(self.text("data/agencies.json"))
        law.validate(data, SCHEMA)
        for path, value in ((("state",), "no_records"), (("fee_quoted", "status"), "owed"), (("fee_quoted", "amount_usd"), "-1"),
                            (("fee_quoted", "note"), "x"), (("extension", "reason"), "x"), (("extension", "until"), "soon"),
                            (("withheld_sections", 0), "7923 (a)"), (("note",), "private")):
            changed = json.loads(json.dumps(data))
            node = changed["agencies"][0]["asked"][0]
            for step in path[:-1]:
                node = node[step]
            node[path[-1]] = value
            with self.assertRaises(ValueError, msg=path):
                law.validate(changed, SCHEMA)
        for key in ("amount_usd", "status"):  # nested pairs are required
            changed = json.loads(json.dumps(data))
            del changed["agencies"][0]["asked"][0]["fee_quoted"][key]
            with self.assertRaises(ValueError):
                law.validate(changed, SCHEMA)
        real = Site._public_agency

        def leaky(site, agency, cities):
            out = real(site, agency, cities)
            out["asked"][0]["fee_quoted"]["payer"] = "PRIVATE"
            return out
        with mock.patch.object(Site, "_public_agency", leaky), self.assertRaisesRegex(ContentError, "data/agencies.json: non-public key payer"):
            build_site(self.root, today=TODAY)

    # --- refusals collect, inputs and the public tree are preserved -----------------------------------------------
    def test_refusals_collect_and_preserve_inputs_and_public(self):
        self.asked({CEDAR: [self.rich_item()]})
        self.build()
        before = tree(self.public)
        content_before = {p: p.read_bytes() for p in (self.root / "content").rglob("*") if p.is_file()}
        self.asked({SHERIFF: [{**BASE, "fee_quoted": {"amount_usd": "x", "status": "paid"}},
                              {**BASE, "extension": {"noticed_on": "2026-12-01", "until": "2026-12-02"}}],
                    CEDAR: [{**BASE, "withheld_sections": ["no"]}, {**BASE, "extension": {"noticed_on": "2026-10-02", "until": "2026-10-01"}}]})
        lines = self.problems()
        for where in (f"[{SHERIFF}].asked[0].fee_quoted.amount_usd", f"[{SHERIFF}].asked[1].extension.noticed_on",
                      f"[{CEDAR}].asked[0].withheld_sections[0]", f"[{CEDAR}].asked[1].extension.until"):
            self.assertEqual(sum(where in line for line in lines), 1, (where, lines))
        edited = {p: p.read_bytes() for p in (self.root / "content").rglob("*") if p.is_file()}
        with self.assertRaises(ContentError):
            build_site(self.root, today=TODAY)
        self.assertEqual(tree(self.public), before)  # bytes, modes and path set of the healthy public tree
        self.assertEqual({p: p.read_bytes() for p in (self.root / "content").rglob("*") if p.is_file()}, edited)
        self.assertEqual(set(edited), set(content_before))
        problems = []
        _asked({"asked": [{**BASE, "fee_quoted": {"amount_usd": "x", "status": "paid"}, "withheld_sections": []}]}, "t", TODAY, problems)
        self.assertEqual(len(problems), 2)

    # --- legacy campaigns ----------------------------------------------------------------------------------------
    def test_legacy_absence_whole_tree_identity_and_idempotence(self):
        self.build()
        self.assertEqual(digest(self.public), MAIN_EXAMPLE_DIGEST)
        self.asked(LEGACY_ASKED)
        self.build()
        first = tree(self.public)
        self.assertEqual(digest(self.public), MAIN_LEGACY_DIGEST)
        page = self.text("requests.html")
        self.assertIn("Withdrawn (1)</p>", page)  # six counts, nothing after: no seventh zero count
        self.assertNotIn("No responsive records stated", page)
        for key in ("fee_quoted", "extension", "withheld_sections", "no_records_stated"):
            self.assertNotIn(key, self.text("data/agencies.json"))
        self.assertEqual(self.build(), first)  # a second build is identical
        self.asked({CEDAR: [self.rich_item()]})
        one, two = self.build(), self.build()
        self.assertEqual(one, two)
        self.assertNotEqual(digest(self.public), MAIN_LEGACY_DIGEST)


if __name__ == "__main__":
    unittest.main()
