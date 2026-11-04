"""DF1207-14: data/findings.csv and data/sources.csv are spreadsheet copies of the open-data JSON.

Synthetic content only (a temp copy of the fictional example); no network.
"""
import csv
import hashlib
import io
import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool import kit_export, site as site_module
from campaign_tool.content import ContentError
from campaign_tool.site import PUBLIC_COPY_FIELDS, PUBLIC_FINDING_FIELDS, PUBLIC_LIBRARY_FIELDS, build_site

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
        self.assertNotIn("/data/meetings.csv", about)
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


if __name__ == "__main__":
    unittest.main()
