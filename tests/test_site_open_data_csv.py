"""DF1207-14: data/findings.csv and data/sources.csv are spreadsheet copies of the open-data JSON.

Synthetic content only (a temp copy of the fictional example); no network.
"""
import csv
import hashlib
import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from campaign_tool import kit_export, site as site_module
from campaign_tool.content import ContentError
from campaign_tool.site import (PUBLIC_AGENCY_FIELDS, PUBLIC_COPY_FIELDS, PUBLIC_FINDING_FIELDS, PUBLIC_LIBRARY_FIELDS,
                                PUBLIC_MEETING_FIELDS, build_site, public_records_csv)
from tests.test_site_agencies_open_data import ASKED, CONTRACT, EMAIL, NEW, OLD, POLICY, PORTAL
from tests.test_site_meetings_open_data import CONTACT, OUTCOME, PARTICIPATION, VOTES

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
SLUG = "cedar-policy-posted"
PDF = "https://example.invalid/fixtures/copy"


def sha(label):
    return hashlib.sha256(f"synthetic-df1207-14-{label}".encode()).hexdigest()


A, B, C = sha("a"), sha("b"), sha("c")
LIBRARY = [
    {"sha256": A, "title": "=HYPERLINK(\"https://example.invalid\")", "agency_id": "ca-cedar-police",
     "received_at": "2026-01-20T16:30:00+00:00", "public_url": PDF, "fragment": "cedar-police-records", "pages": 2,
     "note": "Private synthetic custody note", "public_copy": {"matches": False, "checked_on": "2026-10-21", "reason": "redacted"}},
    {"sha256": B, "title": "Synthetic audit log", "agency_id": "ca-cedar-police", "received_at": "2026-01-22T09:00:00+00:00",
     "public_url": PDF + "-2", "fragment": "cedar-police-records", "public_copy": {"matches": True, "checked_on": "2026-10-22"}},
    {"sha256": C, "title": "Synthetic vendor letter", "agency_id": "ca-unconfigured-agency",
     "received_at": "2026-01-25T10:00:00Z", "public_url": None, "fragment": "unconfigured-records", "pages": 0},
]


def split(text):
    return list(csv.reader(io.StringIO(text, newline="")))


class OpenDataCsvTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="synthetic-open-data-csv-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.public = self.root / "public"
        self.content = self.root / "content"
        self.write("sources.json", LIBRARY)
        self.finding(SLUG, lambda d: d["sources"][0].update(sha256=A))

    def write(self, name, value):
        (self.content / name).write_text(json.dumps(value), encoding="utf-8")

    def finding(self, slug, change=None, **fields):
        base = json.loads((self.content / "findings" / f"{SLUG}.json").read_text(encoding="utf-8"))
        base.update(fields)
        if slug != SLUG:
            base.update(slug=slug, id=f"fnd_{hashlib.sha256(slug.encode()).hexdigest()[:16]}", corrections=[], updated_at=None)
        if change:
            change(base)
        (self.content / "findings" / f"{slug}.json").write_text(json.dumps(base), encoding="utf-8")

    def build(self):
        build_site(self.root)
        return {name: (self.public / "data" / name).read_bytes() for name in ("findings.csv", "sources.csv")}

    def read(self, name):
        return (self.public / name).read_text(encoding="utf-8")

    def rows(self, name):
        """The CSV as a header and one dict per row."""
        table = split(self.read(f"data/{name}.csv"))
        return table[0], [dict(zip(table[0], row)) for row in table[1:]]

    # 1. every CSV row matches its JSON record, in the JSON's order
    def test_every_row_matches_its_json_record(self):
        self.finding("zz-second", title="Second synthetic finding", agency_id="ca-cedar-police",
                     limitations=["first limit", "second; limit"], counterevidence=["counter one", "counter two"])
        self.build()
        findings = json.loads(self.read("data/findings.json"))["findings"]
        header, rows = self.rows("findings")
        self.assertEqual(header, list(PUBLIC_FINDING_FIELDS))
        self.assertEqual(len(rows), len(findings))
        self.assertGreaterEqual(len(findings), 2)
        for record, row in zip(findings, rows):
            for key in PUBLIC_FINDING_FIELDS:
                value = record.get(key) if key in ("also_agency_ids", "agency_response") else record[key]
                if key == "sources":
                    value = "; ".join(s["sha256_prefix"] for s in value)
                elif key == "corrections":
                    value = "; ".join(f'{c["date"]}: {c["note"]}' for c in value)
                elif isinstance(value, list):
                    value = "; ".join(value)
                self.assertEqual(row[key], "" if value is None else value, f'{record["slug"]}.{key}')
        by_slug = {f["slug"]: f for f in findings}
        self.assertEqual(rows[[r["slug"] for r in rows].index(SLUG)]["sources"], by_slug[SLUG]["sources"][0]["sha256_prefix"])
        self.assertIn("; ", {r["slug"]: r for r in rows}["zz-second"]["limitations"])

        sources = json.loads(self.read("data/sources.json"))["sources"]
        header, rows = self.rows("sources")
        expected = [c for key in PUBLIC_LIBRARY_FIELDS
                    for c in ([f"public_copy_{k}" for k in PUBLIC_COPY_FIELDS] if key == "public_copy" else [key])]
        self.assertEqual(header, expected)
        self.assertEqual(len(rows), len(sources))
        for record, row in zip(sources, rows):
            copy = record["public_copy"] or {}
            self.assertEqual(row["sha256_prefix"], record["sha256_prefix"])
            self.assertEqual(row["agency_name"], record["agency_name"] or "")
            self.assertEqual(row["public_url"], record["public_url"] or "")
            self.assertEqual(row["pages"], "" if record["pages"] is None else str(record["pages"]))
            self.assertEqual(row["cited_by"], "; ".join(record["cited_by"]))
            self.assertEqual(row["public_copy_matches"], "" if "matches" not in copy else str(copy["matches"]).lower())
            self.assertEqual(row["public_copy_checked_on"], copy.get("checked_on", ""))
            self.assertEqual(row["public_copy_reason"], copy.get("reason", ""))
        self.assertEqual([r["public_copy_matches"] for r in rows], ["false", "true", ""])
        self.assertEqual(rows[0]["public_copy_reason"], "redacted")
        self.assertEqual(rows[2]["pages"], "0")

    def test_optional_public_fields_match_json_and_pending_stays_private(self):
        self.build()
        _, rows = self.rows("findings")
        self.assertEqual(rows[0]["also_agency_ids"], "")
        self.assertEqual(rows[0]["agency_response"], "")
        pending = {"status": "pending", "prepared_on": "2026-02-02", "reply_by": "2026-02-20"}
        before = self.build()["findings.csv"]
        self.finding(SLUG, agency_response=pending)
        self.assertEqual(self.build()["findings.csv"], before)
        for status in ("received", "declined", "no_response"):
            with self.subTest(status=status):
                response = {**pending, "status": status, "asked_on": "2026-02-03", "on": "2026-02-20"}
                if status == "received":
                    response.update(text='=Réponse, "quoted"\nSecond line', record_sha256=A)
                self.finding(SLUG, also_agency_ids=["ca-cedar-county-sheriff", "ca-cedar-city-council"], agency_response=response)
                self.build()
                record = json.loads(self.read("data/findings.json"))["findings"][0]
                _, rows = self.rows("findings")
                self.assertEqual(rows[0]["also_agency_ids"], "; ".join(record["also_agency_ids"]))
                self.assertEqual(json.loads(rows[0]["agency_response"]), record["agency_response"])
                self.assertNotIn("prepared_on", rows[0]["agency_response"])
                self.assertNotIn(A, rows[0]["agency_response"])
                if status == "received":
                    self.assertEqual(json.loads(rows[0]["agency_response"])["record_sha256_prefix"], A[:12])

    # 2. formula guard: only a leading formula character is prefixed; the JSON is untouched
    def test_formula_cells_are_prefixed_and_plain_equals_is_not(self):
        titles = {"zz-eq": "=HYPERLINK(\"https://example.invalid\")", "zz-plus": "+1", "zz-minus": "-x", "zz-at": "@x",
                  "zz-tab": "\tx", "zz-mid": "a=b", "zz-quote": "'already"}
        for slug, title in titles.items():
            self.finding(slug, title=title)
        self.build()
        _, rows = self.rows("findings")
        by_slug = {r["slug"]: r["title"] for r in rows}
        for slug in ("zz-eq", "zz-plus", "zz-minus", "zz-at", "zz-tab"):
            self.assertEqual(by_slug[slug], "'" + titles[slug], slug)
        self.assertEqual((by_slug["zz-mid"], by_slug["zz-quote"]), ("a=b", "'already"))
        by_json = {f["slug"]: f["title"] for f in json.loads(self.read("data/findings.json"))["findings"]}
        self.assertEqual(by_json["zz-eq"], titles["zz-eq"])
        _, sources = self.rows("sources")
        self.assertEqual(sources[0]["title"], "'" + LIBRARY[0]["title"])  # the library CSV is guarded too

    def test_guard_is_the_kit_exports_own_helper(self):
        for text in ("=x", "+1", "-1", "@x", "\tx", "\rx", "plain", "a=b", ""):
            self.assertEqual(site_module._cell(text), kit_export._cell(text))
        self.assertEqual(site_module._cell(["=a", "b"]), "'=a; b")  # one guard on the joined cell, none inside it

    # 3. quotes, commas, newlines and non-ASCII survive; CRLF, UTF-8, no BOM, header first
    def test_round_trip_of_quotes_commas_newlines_and_non_ascii(self):
        summary = 'He said "go", then left,\nJosé Núñez replied: ¿sí?\nNext line; with semicolon'
        self.finding(SLUG, lambda d: d.update(summary=summary, corrections=[{"date": "2026-02-03", "note": 'fixed "date", yes'}]))
        raw = self.build()["findings.csv"]
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
        text = raw.decode("utf-8")
        self.assertTrue(text.startswith(",".join(PUBLIC_FINDING_FIELDS) + "\r\n"))
        self.assertEqual(text.count("\r\n"), 2)  # header and one record end in CRLF; embedded newlines stay LF
        _, rows = self.rows("findings")
        self.assertEqual(rows[0]["summary"], summary)
        self.assertEqual(rows[0]["corrections"], '2026-02-03: fixed "date", yes')
        self.assertIn("José", text)

    # 4. no published findings: header only, JSON has []
    def test_zero_findings_is_header_only(self):
        for path in (self.content / "findings").glob("*.json"):
            path.unlink()
        raw = self.build()
        self.assertEqual(json.loads(self.read("data/findings.json"))["findings"], [])
        self.assertEqual(raw["findings.csv"].decode("utf-8"), ",".join(PUBLIC_FINDING_FIELDS) + "\r\n")
        self.assertEqual(len(split(raw["sources.csv"].decode("utf-8"))), 1 + len(LIBRARY))

    def test_no_library_is_header_only_sources_csv(self):
        (self.content / "sources.json").unlink()
        self.finding(SLUG, lambda d: d["sources"][0].update(sha256="cafe" * 16))
        raw = self.build()
        self.assertEqual(json.loads(self.read("data/sources.json"))["sources"], [])
        self.assertEqual(len(split(raw["sources.csv"].decode("utf-8"))), 1)

    # 5. a model field outside the public allowlist reaches neither file
    def test_field_outside_the_allowlist_never_appears(self):
        real = site_module.Site._public_finding

        def leaky(self, finding):
            return {**real(self, finding), "private_note": "do not publish"}
        build_site(self.root)
        before = {p.name: p.read_bytes() for p in (self.public / "data").glob("*.csv")}
        with mock.patch.object(site_module.Site, "_public_finding", leaky):
            with self.assertRaisesRegex(ContentError, "data/findings.json: non-public key private_note"):
                build_site(self.root)
        self.assertEqual({p.name: p.read_bytes() for p in (self.public / "data").glob("*.csv")}, before)
        for text in (self.read("data/findings.csv"), self.read("data/sources.csv")):
            self.assertNotIn("private_note", text)
            self.assertNotIn("do not publish", text)
            self.assertNotIn("Private synthetic custody note", text)
            self.assertNotIn("cedar-police-records", text)  # fragment
        self.assertEqual(split(self.read("data/findings.csv"))[0], list(PUBLIC_FINDING_FIELDS))
        self.assertNotIn("sha256", split(self.read("data/sources.csv"))[0])

    # 6. no full sha256 in either CSV, and one in a title refuses the build
    def test_no_full_hash_in_the_csv_and_one_in_a_title_is_refused(self):
        self.build()
        for name in ("findings.csv", "sources.csv"):
            self.assertIsNone(re.search(r"[0-9a-fA-F]{40,}", self.read(f"data/{name}")), name)
        for value in (A, A[:40]):
            self.write("sources.json", [{**LIBRARY[0], "title": "x " + value}, *LIBRARY[1:]])
            with self.assertRaisesRegex(ContentError, "data/sources.json"):
                build_site(self.root)
        self.assertNotIn(A[:40], self.read("data/sources.csv"))

    # 7. links beside each JSON link; the label can be overridden
    def test_links_sit_next_to_each_json_link(self):
        self.build()
        findings_json = '<a href="/data/findings.json" type="application/json">'
        sources_json = '<a href="/data/sources.json" type="application/json">'
        findings_csv = '<a href="/data/findings.csv" type="text/csv">Spreadsheet (CSV)</a>'
        sources_csv = '<a href="/data/sources.csv" type="text/csv">Spreadsheet (CSV)</a>'
        index, sources, about = self.read("findings/index.html"), self.read("sources.html"), self.read("about.html")
        self.assertRegex(index, re.escape(findings_json) + r"Open data \(JSON\)</a> · " + re.escape(findings_csv))
        self.assertRegex(sources, re.escape(sources_json) + r"Open data \(JSON\)</a> · " + re.escape(sources_csv))
        self.assertRegex(about, re.escape(findings_json) + r"data/findings.json</a> · " + re.escape(findings_csv))
        self.assertRegex(about, re.escape(sources_json) + r"data/sources.json</a> · " + re.escape(sources_csv))
        self.assertRegex(about, r'<a href="/data/meetings.json" type="application/json">data/meetings.json</a> · '
                                r'<a href="/data/meetings.csv" type="text/csv">Spreadsheet \(CSV\)</a>')
        self.assertRegex(about, r'<a href="/data/agencies.json" type="application/json">data/agencies.json</a> · '
                                r'<a href="/data/agencies.csv" type="text/csv">Spreadsheet \(CSV\)</a>')
        self.assertNotIn("data/findings.csv", self.read("sitemap.xml"))  # JSON is not in the sitemap either
        self.assertNotIn("data/findings.json", self.read("sitemap.xml"))

    def test_link_label_comes_from_labels_json(self):
        self.write("labels.json", {"schema_version": 1, "labels": {"intro.open_data_csv": "Hoja & cálculo"}})
        self.build()
        for page in ("findings/index.html", "sources.html", "about.html"):
            html = self.read(page)
            self.assertIn('type="text/csv">Hoja &amp; cálculo</a>', html, page)
            self.assertNotIn("Spreadsheet (CSV)", html, page)

    # byte-stable
    def test_two_builds_are_byte_identical(self):
        self.assertEqual(self.build(), self.build())


AGENCY_COLUMNS = ["agency_id", "alpr_policy", "asked", "camera_count", "contract", "kind", "name", "transparency", "url"]
MEETING_COLUMNS = ["agency_id", "agenda_url", "body", "id", "outcome", "starts_at", "status", "url"]
DAY = date(2026, 11, 1)
SHERIFF = "ca-cedar-county-sheriff"


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def expected_cell(value):
    """The DF2207-11 flattening, written out again from the spec rather than imported."""
    if value is None:
        return ""
    if isinstance(value, list):
        return "; ".join(item if isinstance(item, str) else compact(item) for item in value)
    return value if isinstance(value, str) else compact(value)


class AgencyMeetingCsvTests(unittest.TestCase):
    """DF2207-11: data/agencies.csv and data/meetings.csv are spreadsheet copies of the two open-data JSON files."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="synthetic-records-csv-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.public = self.root / "public"
        self.content = self.root / "content"
        self.agencies_path = self.content / "agencies.json"
        self.meeting = self.content / "meetings" / "2026-10-cedar-council.json"
        (self.content / "officials.json").write_text(json.dumps({"schema_version": 1, "bodies": [{
            "agency_id": "ca-cedar-city-council", "officials": [{"name": v["name"], "role": "Council member",
                                                                  "source_url": "https://cedar.example/council",
                                                                  "checked_on": "2026-09-01"} for v in VOTES]}]}), encoding="utf-8")

    def agencies(self):
        return json.loads(self.agencies_path.read_text(encoding="utf-8"))

    def rich_agencies(self, sheriff_name=None):
        """Cedar PD with every nested object, plus a second listed agency (a library entry cites it)."""
        agencies = self.agencies()
        cedar = agencies[1]
        cedar.update(verified=True, verification={"source_url": "https://example.invalid/contact", "checked_on": "2026-01-15"},
                     flock_transparency_slug="cedar-pd-example", records_email=EMAIL, asked=ASKED, notes="organizer-only note",
                     portal={"vendor": "none", "url": "https://example.invalid/portal-link"})
        cedar["profile"].update(alpr_policy=POLICY, contract={**CONTRACT, "cost": {"amount_usd": "0.10", "period": "annual",
                                "as_stated_in": "Synthetic contract, page 2"}}, transparency=[NEW, OLD])
        if sheriff_name is not None:
            next(a for a in agencies if a["agency_id"] == SHERIFF)["name"] = sheriff_name
            sources = [{**LIBRARY[1], "agency_id": SHERIFF}]
            (self.content / "sources.json").write_text(json.dumps(sources), encoding="utf-8")
        self.agencies_path.write_text(json.dumps(agencies), encoding="utf-8")

    def edit_meeting(self, path=None, **changes):
        path = path or self.meeting
        data = json.loads(path.read_text(encoding="utf-8"))
        data.update(changes)
        path.write_text(json.dumps(data), encoding="utf-8")

    def add_meeting(self, meeting_id, starts_at, **extra):
        record = {"id": meeting_id, "body": f"Synthetic board {meeting_id} (fictional)", "starts_at": starts_at, **extra}
        (self.content / "meetings" / f"{meeting_id}.json").write_text(json.dumps(record), encoding="utf-8")

    def build(self):
        build_site(self.root, today=DAY)

    def raw(self, name):
        return (self.public / "data" / name).read_bytes()

    def table(self, name):
        header, *rows = split(self.raw(name).decode("utf-8"))
        return header, rows

    def check_against_json(self, kind, record_key, columns):
        """Every header, row and cell of data/<kind>.csv against data/<kind>.json; returns the JSON records."""
        records = json.loads(self.raw(f"{kind}.json"))[record_key]
        header, rows = self.table(f"{kind}.csv")
        self.assertEqual(header, columns)
        self.assertEqual(len(rows), len(records))
        for record, row in zip(records, rows):
            self.assertEqual(len(row), len(columns))  # no ragged rows
            for column, cell in zip(columns, row):
                want = expected_cell(record.get(column))
                self.assertEqual(cell, "'" + want if want[:1] in ("=", "+", "-", "@", "\t", "\r") else want, f"{record.get('id') or record['agency_id']}.{column}")
        return records

    # 1. every agency row matches its JSON record; private keys stay out
    def test_agency_csv_matches_public_json(self):
        self.rich_agencies(sheriff_name="Sheriff, Cedar County (fictional)")
        self.build()
        self.assertEqual(AGENCY_COLUMNS, sorted(PUBLIC_AGENCY_FIELDS))
        records = self.check_against_json("agencies", "agencies", AGENCY_COLUMNS)
        self.assertEqual(len(records), 2)
        _, rows = self.table("agencies.csv")
        cedar = next(dict(zip(AGENCY_COLUMNS, row)) for row in rows if row[0] == "ca-cedar-police")
        self.assertEqual(cedar["asked"], compact({"asked_on": "2026-10-01", "state": "awaiting_response", "topic": "Sharing list",
                                                  "updated_on": None}) + "; " + compact({
            "asked_on": "2026-08-01", "state": "answered", "topic": "Retention policy", "updated_on": "2026-09-01"}))
        self.assertEqual(json.loads(cedar["alpr_policy"]), {"checked_on": "2026-10-20", "url": POLICY["url"]})
        self.assertEqual(json.loads(cedar["contract"])["cost"], {"amount_usd": "0.10", "as_stated_in": "Synthetic contract, page 2",
                                                                 "period": "annual"})
        self.assertEqual(json.loads(cedar["transparency"])["cameras"], 42)
        self.assertNotIn("hotlist_alerts", cedar["transparency"])
        self.assertTrue(cedar["camera_count"])  # the card's sentence, unchanged
        self.assertEqual(cedar["url"], "https://cedar-records.example.invalid/agencies/cedar.html")
        sheriff = next(dict(zip(AGENCY_COLUMNS, row)) for row in rows if row[0] == SHERIFF)
        self.assertEqual((sheriff["asked"], sheriff["contract"], sheriff["transparency"]), ("", "", ""))  # absent values are blank
        self.assertEqual(set(json.loads(sheriff["alpr_policy"])), {"url", "checked_on"})
        self.assertEqual(sheriff["name"], "Sheriff, Cedar County (fictional)")
        text = self.raw("agencies.csv").decode("utf-8")
        for leaked in (EMAIL, "records_email", "organizer-only note", "renewal_note", "Renews quietly", "Internal organizer remark",
                       "portal-link", "verification", "2026-09-25", "mailto:", "notes"):
            self.assertNotIn(leaked, text)

    # 2. every meeting row matches its JSON record
    def test_meeting_csv_matches_public_json(self):
        self.edit_meeting(outcome=OUTCOME, participation=PARTICIPATION)  # held on 2026-10-14, build day 2026-10-20
        self.add_meeting("cancelled-one", "2026-10-02T18:00:00-07:00", status="cancelled")
        self.add_meeting("future-one", "2026-11-05T18:00:00-07:00", agenda_url="https://example.invalid/agenda?x=1&y=2")
        self.add_meeting("plain-one", "2026-10-01T18:00:00-07:00", outcome={"result": "continued", "on": "2026-10-01",
                         "source_url": "https://example.invalid/cedar-council/minutes", "minutes_url": "https://example.invalid/m",
                         "minutes_approved_on": "2026-10-10", "verified_by": "Synthetic Organizer", "verified_at": "2026-10-11"})
        self.build()
        self.assertEqual(MEETING_COLUMNS, sorted(PUBLIC_MEETING_FIELDS))
        records = self.check_against_json("meetings", "meetings", MEETING_COLUMNS)
        self.assertEqual([r["id"] for r in records], ["future-one", "cedar-council-2026-10", "cancelled-one", "plain-one"])  # newest first
        rows = {row[3]: dict(zip(MEETING_COLUMNS, row)) for row in self.table("meetings.csv")[1]}
        held = rows["cedar-council-2026-10"]
        self.assertEqual(json.loads(held["outcome"])["votes"], VOTES)  # per-member votes, stored order, one cell
        self.assertEqual(json.loads(held["outcome"])["vote"], "2-1")
        self.assertEqual(rows["future-one"]["outcome"], "")  # absent outcome
        self.assertEqual(rows["future-one"]["agency_id"], "")
        self.assertEqual(rows["future-one"]["agenda_url"], "https://example.invalid/agenda?x=1&y=2")
        self.assertEqual(rows["cancelled-one"]["status"], "cancelled")
        self.assertEqual(rows["cancelled-one"]["outcome"], "")
        self.assertEqual(json.loads(rows["plain-one"]["outcome"])["minutes_approved_on"], "2026-10-10")
        self.assertEqual(held["url"], "/meetings/cedar-council-2026-10.html")
        text = self.raw("meetings.csv").decode("utf-8")
        for leaked in ("verified_by", "verified_at", "Synthetic Organizer", CONTACT, "participation", "comment_kit",
                       "Two minutes, one ask", "Cedar Civic Center", "agenda_item", "Item 7"):
            self.assertNotIn(leaked, text)

    # 3. formula guard, quoting and Unicode through a real build, then every guarded prefix directly
    def test_new_csv_formula_and_unicode_roundtrip(self):
        self.rich_agencies(sheriff_name='=HYPERLINK("https://example.invalid","Sheriff, \u201cCedar\u201d")')
        body = 'Conseil \u201c\u00d1and\u00fa\u201d, "quoted"; ¿sí? José'
        self.edit_meeting(body="+" + body)
        self.build()
        for name in ("agencies.csv", "meetings.csv"):
            raw = self.raw(name)
            self.assertFalse(raw.startswith(b"\xef\xbb\xbf"), name)  # no byte-order mark
            self.assertTrue(raw.startswith(b"agency_id,") and b"\r\n" in raw, name)
            self.assertEqual(raw.count(b"\r\n"), len(split(raw.decode("utf-8"))), name)  # every record ends in CRLF
            self.assertNotIn(b"\r\r", raw)
        agencies = {r["agency_id"]: r for r in (dict(zip(AGENCY_COLUMNS, row)) for row in self.table("agencies.csv")[1])}
        self.assertEqual(agencies[SHERIFF]["name"], "'=HYPERLINK(\"https://example.invalid\",\"Sheriff, \u201cCedar\u201d\")")
        meeting = dict(zip(MEETING_COLUMNS, self.table("meetings.csv")[1][0]))
        self.assertEqual(meeting["body"], "'+" + body)
        self.assertEqual(json.loads(self.raw("meetings.json"))["meetings"][0]["body"], "+" + body)  # the JSON is untouched

        records = [{"id": "r", "body": prefix + "x"} for prefix in ("=", "+", "-", "@", "\t", "\r")]
        records += [{"id": "i", "body": "a=b+c-d@e"}, {"id": "q", "body": "'already"}, {"id": "n", "body": 'two\nlines, "q"'},
                    {"id": "s", "body": ["=first", "semi;colon", {"k": "=v"}, 3, True, None]}]
        text = public_records_csv(json.dumps({"rows": records}), "rows", ("id", "body"))
        header, *rows = split(text)
        self.assertEqual(header, ["body", "id"])
        cells = [row[0] for row in rows]
        self.assertEqual(cells[:6], ["'" + prefix + "x" for prefix in ("=", "+", "-", "@", "\t", "\r")])
        self.assertEqual(cells[6:9], ["a=b+c-d@e", "'already", 'two\nlines, "q"'])
        self.assertEqual(cells[9], "'=first; semi;colon; " + compact({"k": "=v"}) + "; 3; true; null")  # one guard, on the cell
        self.assertTrue(text.endswith("\r\n"))
        self.assertEqual(len(rows), len(records))

    # 4. empty arrays and optional columns keep a pinned, stable header
    def test_empty_arrays_have_stable_headers(self):
        self.assertEqual(public_records_csv(compact({"meetings": []}), "meetings", PUBLIC_MEETING_FIELDS),
                         ",".join(MEETING_COLUMNS) + "\r\n")
        self.assertEqual(public_records_csv(compact({"agencies": []}), "agencies", PUBLIC_AGENCY_FIELDS),
                         ",".join(AGENCY_COLUMNS) + "\r\n")
        (self.content / "sources.json").write_text("[]", encoding="utf-8")  # no finding or source: no agency has a page
        shutil.rmtree(self.content / "findings")
        self.meeting.unlink()
        self.build()
        self.assertEqual(json.loads(self.raw("agencies.json"))["agencies"], [])
        self.assertEqual(json.loads(self.raw("meetings.json"))["meetings"], [])
        self.assertEqual(self.raw("agencies.csv"), (",".join(AGENCY_COLUMNS) + "\r\n").encode())
        self.assertEqual(self.raw("meetings.csv"), (",".join(MEETING_COLUMNS) + "\r\n").encode())

    def test_optional_value_missing_from_the_first_record_keeps_its_column(self):
        self.edit_meeting(outcome=OUTCOME)
        self.add_meeting("future-one", "2026-11-05T18:00:00-07:00")  # newest, so first, and has no outcome
        self.build()
        header, rows = self.table("meetings.csv")
        self.assertEqual(header, MEETING_COLUMNS)
        self.assertEqual([r[3] for r in rows], ["future-one", "cedar-council-2026-10"])
        self.assertEqual((rows[0][4], bool(rows[1][4])), ("", True))
        self.assertEqual({len(r) for r in rows}, {len(MEETING_COLUMNS)})

    # 5. a CSV exists exactly when its JSON is in the plan, and is read back from that JSON text
    def test_no_json_no_csv_or_link(self):
        plan, _ = site_module._plan_site(self.root, today=DAY)
        for kind in ("agencies", "meetings"):
            self.assertIn(f"data/{kind}.json", plan)
            self.assertIn(f"data/{kind}.csv", plan)
        shown = {"schema_version": 1, "meetings": [{"id": "only-one", "body": "Only meeting", "status": "scheduled"}]}
        with mock.patch.object(site_module.Site, "meetings_json", return_value=json.dumps(shown)) as render:
            plan, _ = site_module._plan_site(self.root, today=DAY)
        render.assert_called_once()  # rendered once; the CSV is read back from that very text
        self.assertEqual(split(plan["data/meetings.csv"])[1:], [["", "", "Only meeting", "only-one", "", "", "scheduled", ""]])
        self.assertEqual(plan["data/meetings.json"], json.dumps(shown))
        self.assertEqual(public_records_csv(compact({"agencies": []}), "agencies", ()), "\r\n")  # nothing allowed, no columns
        self.assertEqual(public_records_csv(compact({"agencies": [{"agency_id": "a", "secret": "x"}]}), "agencies", ("agency_id",)),
                         "agency_id\r\na\r\n")  # a key outside the allowlist is never a column

    # 6. links beside every JSON link, label override, repeat build, build --check
    def test_download_links_and_repeat_build(self):
        self.rich_agencies()
        self.build()
        agencies_link = '<a href="/data/agencies.json" type="application/json">'
        meetings_link = '<a href="/data/meetings.json" type="application/json">'
        csv_link = '<a href="/data/%s.csv" type="text/csv">Spreadsheet (CSV)</a>'
        self.assertRegex(self.read("agencies.html"), re.escape(agencies_link) + r"Agencies as open data \(JSON\)</a> · "
                         + re.escape(csv_link % "agencies"))
        self.assertRegex(self.read("meetings.html"), re.escape(meetings_link) + r"Meetings and outcomes as open data \(JSON\)</a> · "
                         + re.escape(csv_link % "meetings"))
        about = self.read("about.html")
        self.assertRegex(about, re.escape(agencies_link) + r"data/agencies.json</a> · " + re.escape(csv_link % "agencies"))
        self.assertRegex(about, re.escape(meetings_link) + r"data/meetings.json</a> · " + re.escape(csv_link % "meetings"))
        for page in ("agencies.html", "meetings.html", "about.html"):  # a CSV link wherever its JSON is linked
            html = self.read(page)
            for kind in ("agencies", "meetings"):
                self.assertEqual(html.count(f'href="/data/{kind}.json"'), html.count(f'href="/data/{kind}.csv"'), (page, kind))
        for page in ("sitemap.xml",):
            if (self.public / page).exists():
                self.assertNotIn(".csv", self.read(page))
        first = {name: self.raw(name) for name in ("agencies.csv", "meetings.csv", "agencies.json", "meetings.json")}
        self.build()
        self.assertEqual({name: self.raw(name) for name in first}, first)
        result = subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "build", "--directory", str(self.root), "--check"],
                                cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Public tree check passed", result.stdout)
        self.assertIn("data/agencies.csv", result.stdout)
        self.assertEqual(self.raw("agencies.csv"), first["agencies.csv"])  # --check rebuilt the same bytes
        self.assertEqual(self.raw("meetings.csv"), first["meetings.csv"])

    def test_link_label_comes_from_labels_json(self):
        (self.content / "labels.json").write_text(json.dumps({"schema_version": 1, "labels": {"intro.open_data_csv": "Hoja"}}),
                                                  encoding="utf-8")
        self.build()
        for page, kinds in (("agencies.html", ("agencies",)), ("meetings.html", ("meetings",)),
                            ("about.html", ("agencies", "meetings"))):
            html = self.read(page)
            for kind in kinds:
                self.assertIn(f'href="/data/{kind}.csv" type="text/csv">Hoja</a>', html, (page, kind))
            self.assertNotIn("Spreadsheet (CSV)", html, page)

    def read(self, name):
        return (self.public / name).read_text(encoding="utf-8")

    # 7. a refused JSON build leaves the old public tree, CSVs included, and no key or full hash reaches a CSV
    def test_allowlist_refusal_keeps_old_public(self):
        self.rich_agencies()
        self.build()
        before = {p.relative_to(self.public).as_posix(): p.read_bytes() for p in self.public.rglob("*") if p.is_file()}
        real_agency, real_meeting = site_module.Site._public_agency, site_module.Site._public_meeting

        def leaky_agency(self_, agency, cities):
            value = real_agency(self_, agency, cities)
            value["contract"] = {**(value["contract"] or {}), "renewal_note": "Renews quietly in January."}
            return value

        def leaky_meeting(self_, meeting):
            return {**real_meeting(self_, meeting), "participation": {"in_person": True}}
        for target, leaky, pattern in (("_public_agency", leaky_agency, r"^data/agencies\.json: non-public key renewal_note$"),
                                       ("_public_meeting", leaky_meeting, r"^data/meetings\.json: non-public key participation$")):
            with self.subTest(target=target), mock.patch.object(site_module.Site, target, leaky):
                with self.assertRaisesRegex(ContentError, pattern):
                    self.build()
            after = {p.relative_to(self.public).as_posix(): p.read_bytes() for p in self.public.rglob("*") if p.is_file()}
            self.assertEqual(after, before)
            self.assertEqual(sorted(p.name for p in self.root.iterdir() if p.name.startswith(".public")), [])
        agencies = self.agencies()
        agencies[1]["name"] = "Cedar " + A  # a full hash in a name is refused by the JSON gate, so it reaches no CSV
        self.agencies_path.write_text(json.dumps(agencies), encoding="utf-8")
        with self.assertRaisesRegex(ContentError, r"data/agencies\.json: a string holds a full hash"):
            self.build()
        self.assertEqual({p.relative_to(self.public).as_posix(): p.read_bytes() for p in self.public.rglob("*") if p.is_file()}, before)
        for name in ("agencies.csv", "meetings.csv"):
            self.assertIsNone(re.search(r"[0-9a-fA-F]{40,}", self.raw(name).decode("utf-8")), name)


if __name__ == "__main__":
    unittest.main()
