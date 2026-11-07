"""DF2207-12: data.html and data/datapackage.json list every generated open-data file with its exact bytes and hash.

Synthetic content only (a temp copy of the fictional example); sockets are blocked, nothing is fetched.
"""
import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest import mock

from campaign_tool import cli, site as site_module
from campaign_tool.content import LICENSES, ODBL, ContentError
from campaign_tool.labels import DEFAULTS
from campaign_tool.site import build_site, preview_site
from campaign_tool.site_check import check_accessibility, check_data_catalogue, check_links
from campaign_tool.site_data_catalogue import (DATASETS, GEOJSON_SCHEMA, SCHEMA_BASE, csv_columns, resource_records,
                                               verify_data_package)

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
NO_SOCKETS = mock.patch("socket.socket", side_effect=AssertionError("network access attempted"))
OSM = "\u00a9 OpenStreetMap contributors"
JSON_PATHS = ("findings", "sources", "agencies", "meetings")
CITY = "data/city-boundaries.geojson"


class Rows(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows, self.row, self.cell = [], None, None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "tr" and "data-path" in attrs:
            self.row = {"path": attrs["data-path"], "bytes": attrs["data-bytes"], "sha256": attrs["data-sha256"], "text": ""}
            self.rows.append(self.row)
        if tag == "td" and self.row is not None:
            self.cell = True

    def handle_endtag(self, tag):
        if tag == "tr":
            self.row = None
        if tag == "td":
            self.cell = None

    def handle_data(self, data):
        if self.row is not None:
            self.row["text"] += data


class Case(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="synthetic-catalogue-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.public, self.content = self.root / "public", self.root / "content"
        patcher = NO_SOCKETS
        patcher.start()
        self.addCleanup(patcher.stop)

    def edit_json(self, path, change):
        document = json.loads(path.read_text(encoding="utf-8"))
        change(document)
        path.write_text(json.dumps(document), encoding="utf-8")

    def set_license(self, value):
        def change(site):
            site.pop("license", None)
            if value:
                site["license"] = value
        self.edit_json(self.content / "site.json", change)

    def add_county(self, **members):
        document = json.loads((self.content / "map" / "city-boundaries.geojson").read_text(encoding="utf-8"))
        (self.content / "map" / "county.geojson").write_text(json.dumps({**document, **members}), encoding="utf-8")

    def build(self):
        return build_site(self.root)

    def descriptor(self):
        return json.loads((self.public / "data" / "datapackage.json").read_text(encoding="utf-8"))

    def rows(self):
        parser = Rows()
        parser.feed((self.public / "data.html").read_text(encoding="utf-8"))
        return {row["path"]: row for row in parser.rows}

    def resources(self):
        return {"data/" + r["path"]: r for r in self.descriptor()["resources"]}

    def snapshot(self, base=None):
        base = base or self.public
        return {p.relative_to(base).as_posix(): p.read_bytes() for p in sorted(base.rglob("*")) if p.is_file()}

    def check_cli(self):
        """``build --check`` in-process: (exit/raise, stderr text); stdout is dropped."""
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            try:
                cli.build(self.root, check=True)
            except ValueError as exc:
                return str(exc), err.getvalue()
        return None, err.getvalue()


class CatalogueTests(Case):
    def test_catalogue_exact_bytes_and_membership(self):
        self.edit_json(self.root / "campaign.json", lambda c: c.update(name="Cedar R\u00e9cords \u00dcnic\u00f6de Project"))
        self.add_county()
        self.set_license({"id": "CC-BY-4.0"})
        self.build()
        on_disk = {p.relative_to(self.public).as_posix(): p.read_bytes() for p in (self.public / "data").iterdir()
                   if p.name != "datapackage.json"}
        self.assertEqual(sorted(on_disk), sorted([*(f"data/{k}.{x}" for k in JSON_PATHS for x in ("json", "csv")),
                                                  "data/cameras.csv", "data/cameras.geojson", "data/county.geojson", CITY]))
        for kind in JSON_PATHS:
            self.assertTrue(on_disk[f"data/{kind}.csv"].endswith(b"\r\n"), kind)  # CRLF kept in the hashed bytes
            self.assertTrue(on_disk[f"data/{kind}.json"].endswith(b"}\n"), kind)
        self.assertIn("R\u00e9cords".encode("utf-8"), on_disk["data/findings.json"])  # real UTF-8, not \u escapes
        document, resources, rows = self.descriptor(), self.resources(), self.rows()
        self.assertEqual(document["title"], "Cedar R\u00e9cords \u00dcnic\u00f6de Project")
        self.assertEqual(sorted(resources), sorted(on_disk))
        self.assertEqual(sorted(rows), sorted(on_disk))
        self.assertEqual([r["path"] for r in document["resources"]], sorted(r["path"] for r in document["resources"]))
        for path, data in on_disk.items():  # independent hashing of the bytes on disk, not the helper compared to itself
            digest = hashlib.sha256(data).hexdigest()
            resource = resources[path]
            self.assertEqual((resource["bytes"], resource["hash"]), (len(data), "sha256:" + digest), path)
            self.assertEqual((rows[path]["bytes"], rows[path]["sha256"]), (str(len(data)), digest), path)
            self.assertIn(digest, rows[path]["text"], path)  # all 64 characters are shown
            self.assertRegex(resource["hash"], r"^sha256:[0-9a-f]{64}$")
            self.assertFalse(resource["path"].startswith(("data/", "/")), resource["path"])
        self.assertEqual(len({r["name"] for r in document["resources"]}), len(document["resources"]))
        self.assertEqual((document["profile"], document["name"]), ("data-package", "campaign-open-data"))
        self.assertTrue(all(r["profile"] == "data-resource" for r in document["resources"]))
        self.assertEqual(resources["data/agencies.csv"]["mediatype"], "text/csv")
        self.assertEqual(resources["data/cameras.geojson"]["mediatype"], "application/geo+json")
        self.assertEqual(resources[CITY]["name"], "city-boundary-geojson")
        self.assertIn('href="/data/datapackage.json"', (self.public / "data.html").read_text(encoding="utf-8"))
        self.assertEqual(verify_data_package(self.public), [])

    def test_optional_and_empty_resources(self):
        # no map at all: no cameras, county or city entries
        shutil.rmtree(self.content / "map")
        self.build()
        self.assertEqual(sorted(self.resources()), sorted(f"data/{k}.{x}" for k in JSON_PATHS for x in ("json", "csv")))
        self.assertEqual(verify_data_package(self.public), [])
        # no findings: header-only CSV and an empty array are still listed
        for finding in (self.content / "findings").glob("*.json"):
            finding.unlink()
        self.build()
        resources = self.resources()
        self.assertIn("data/findings.csv", resources)
        csv_bytes = (self.public / "data" / "findings.csv").read_bytes()
        self.assertEqual(csv_bytes.count(b"\r\n"), 1)
        self.assertEqual([f["name"] for f in resources["data/findings.csv"]["schema"]["fields"]], csv_columns(csv_bytes))
        self.assertEqual(json.loads((self.public / "data" / "findings.json").read_text(encoding="utf-8"))["findings"], [])
        # unrelated public files never enter the package; neither does the catalogue itself
        (self.public / "data" / "stray.json").write_text("{}", encoding="utf-8")
        self.assertEqual(verify_data_package(self.public), [])
        self.assertNotIn("data/stray.json", self.resources())
        for name in ("data/datapackage.json", "data.html"):
            self.assertNotIn(name, self.resources())
            self.assertNotIn(name, self.rows())
        self.assertFalse(any(path.startswith("vendor/") for path in self.resources()))
        # a subset of payloads (absent JSON/CSV pairs) yields only its own records, no duplicate names
        records = resource_records({"data/meetings.json": b"{}\n", "data/meetings.csv": b"id\r\n"}, {})
        self.assertEqual([r["name"] for r in records], ["meetings-csv", "meetings-json"])
        with self.assertRaisesRegex(ContentError, "not a registered open-data file"):
            resource_records({"data/anything.json": b"{}"}, {})
        with self.assertRaisesRegex(ContentError, "collides"):
            resource_records({"data/findings.json": b"{}"}, {"city_boundary": "data/findings.json"})
        # the starter (no content/) builds no catalogue and --check carries on through the existing checks
        starter = Path(self.temporary.name) / "starter"
        shutil.copytree(EXAMPLE, starter, ignore=shutil.ignore_patterns("public", "content"))
        with contextlib.redirect_stdout(io.StringIO()):
            cli.build(starter, check=True)
        self.assertFalse((starter / "public" / "data.html").exists())
        self.assertFalse((starter / "public" / "data" / "datapackage.json").exists())

    def test_licenses_do_not_cross_resources(self):
        self.add_county()
        for license_id, (_, url) in LICENSES.items():
            with self.subTest(license_id):
                self.set_license({"id": license_id, "attribution": "Cedar Records Project"})
                self.build()
                resources = self.resources()
                for path, resource in resources.items():
                    if path.endswith((".json", ".csv")) and path != "data/cameras.csv":
                        self.assertEqual(resource["licenses"], [{"name": license_id, "path": url}], path)
                        self.assertEqual(resource["attribution"], "Cedar Records Project", path)
                cameras = resources["data/cameras.geojson"]
                self.assertEqual(cameras["licenses"], [{"name": ODBL["id"], "path": ODBL["url"]}])
                self.assertEqual(cameras["attribution"], OSM)
                self.assertEqual(resources["data/cameras.csv"]["licenses"], [{"name": ODBL["id"], "path": ODBL["url"]}])  # DF0237-12
                self.assertEqual(resources["data/cameras.csv"]["attribution"], OSM + ". ODbL 1.0; attribution and share-alike apply.")
                for path in ("data/county.geojson", CITY):  # GeoJSON placement is not a licence
                    self.assertEqual(resources[path]["licenses"], [], path)
                    self.assertNotIn("attribution", resources[path], path)
                self.assertNotIn("licenses", self.descriptor())
                self.assertNotIn("license", self.descriptor())
        self.set_license(None)  # no campaign licence: campaign files say so, cameras are still ODbL
        self.build()
        resources = self.resources()
        for path, resource in resources.items():
            if path not in ("data/cameras.geojson", "data/cameras.csv"):
                self.assertEqual(resource["licenses"], [], path)
        self.assertEqual(resources["data/cameras.geojson"]["licenses"][0]["name"], "ODbL-1.0")
        self.assertEqual(resources["data/cameras.csv"]["licenses"][0]["name"], "ODbL-1.0")
        page = (self.public / "data.html").read_text(encoding="utf-8")
        self.assertEqual(page.count("licence not stated"), len(resources) - 2)
        self.assertIn(OSM, page)
        self.assertIn(f'href="{ODBL["url"]}"', page)
        self.assertNotIn("creativecommons", page)

    def test_a_boundary_keeps_only_a_licence_its_own_file_states(self):
        self.add_county(license="ODbL-1.0")
        self.build()
        county = self.resources()["data/county.geojson"]
        self.assertEqual(county["licenses"], [{"name": "ODbL-1.0", "path": ODBL["url"]}])
        self.assertEqual(county["attribution"], OSM)
        self.assertEqual(self.resources()[CITY]["licenses"], [])  # the neighbour is not relicensed
        self.add_county(license="made-up-licence")
        self.build()
        self.assertEqual(self.resources()["data/county.geojson"]["licenses"], [])
        self.assertIn("licence not stated", self.rows()["data/county.geojson"]["text"])

    def test_descriptor_paths_and_schemas(self):
        self.add_county()
        self.build()
        document, data = self.descriptor(), self.public / "data"
        for resource in document["resources"]:
            target = data / resource["path"]
            self.assertTrue(target.is_file(), resource["path"])
            self.assertEqual(posix := resource["path"], os.path.normpath(posix))
            if resource["mediatype"] == "text/csv":
                fields = resource["schema"]["fields"]
                self.assertEqual([f["name"] for f in fields], csv_columns(target.read_bytes()))
                self.assertTrue(all(f["type"] == "string" for f in fields))
            elif resource["mediatype"] == "application/geo+json":
                self.assertEqual(resource["schema"], GEOJSON_SCHEMA)
            else:
                self.assertEqual(resource["schema"], SCHEMA_BASE + resource["name"].split("-")[0] + "-open-data.schema.json")
                self.assertTrue((REPO_ROOT / "schemas" / resource["schema"].rsplit("/", 1)[1]).is_file())
        self.assertTrue(SCHEMA_BASE.startswith("https://raw.githubusercontent.com/Grahammmm/DeFlock-Campaign-Tool/main/schemas/"))
        page = (self.public / "data.html").read_text(encoding="utf-8")
        self.assertIn("Source-record schema (JSON)", page)  # a CSV row names the JSON schema as the source-record schema
        # a full hash or a non-public key in an open-data document is still refused, not moved into a catalogue
        with self.assertRaisesRegex(ContentError, "full hash"):
            site_module._assert_public({"a": "0" * 64}, "data/x.json", ("a",), {})
        with self.assertRaisesRegex(ContentError, "non-public key"):
            site_module._assert_public({"b": 1}, "data/x.json", ("a",), {})

    def test_footer_sitemap_labels_and_collision(self):
        self.build()
        footer = re.search(r'<footer class="site-footer">.*</footer>', (self.public / "index.html").read_text(encoding="utf-8"), re.S).group()
        paragraphs = re.findall(r"<p>.*?</p>", footer)
        link = '<p><a href="/data.html">Open data</a></p>'
        self.assertIn(link, paragraphs)
        self.assertEqual(paragraphs[paragraphs.index(link) + 1][:30], '<p><a href="/feed.xml">Finding')  # before the links paragraph
        sitemap = (self.public / "sitemap.xml").read_text(encoding="utf-8")
        self.assertIn("/data.html</loc>", sitemap)
        self.assertNotIn(".csv", sitemap)
        self.assertNotIn("datapackage", sitemap)
        self.assertNotIn("geojson", sitemap)
        page = (self.public / "data.html").read_text(encoding="utf-8")
        self.assertNotIn("aria-current", page)  # no new top-nav item
        self.assertEqual(check_links(self.public), [])
        self.assertEqual(check_accessibility(self.public), [])
        self.edit_json(self.content / "site.json", lambda site: site.update(base_url=None))  # no base_url: link, no sitemap
        self.build()
        self.assertFalse((self.public / "sitemap.xml").exists())
        self.assertIn(link, (self.public / "about.html").read_text(encoding="utf-8"))
        # labels: an override reaches the footer and the page; the Spanish draft carries every new key
        (self.content / "labels.json").write_text(json.dumps({"schema_version": 1, "labels": {
            "footer.data": "Xdata", "data.title": "Xtitle", "data.license_unstated": "Xunstated"}}), encoding="utf-8")
        self.build()
        self.assertIn('<a href="/data.html">Xdata</a>', (self.public / "index.html").read_text(encoding="utf-8"))
        self.assertIn("Xtitle", (self.public / "data.html").read_text(encoding="utf-8"))
        self.assertIn("Xunstated", (self.public / "data.html").read_text(encoding="utf-8"))
        spanish = json.loads((REPO_ROOT / "examples" / "labels" / "es.json").read_text(encoding="utf-8"))["labels"]
        new = [key for key in DEFAULTS if key.startswith("data.") or key == "footer.data"]
        self.assertEqual(len(new), 13)
        self.assertEqual([key for key in new if key not in spanish], [])
        # data.html is reserved: a page cannot take the name, a public asset cannot collide with it
        self.assertIn("data.html", site_module.GENERATED_TOP_LEVEL)
        pages = self.content / "pages"
        pages.mkdir()
        (pages / "data.json").write_text(json.dumps({"title": "T", "description": "d", "body_md": "b", "nav": False}), encoding="utf-8")
        with self.assertRaisesRegex(ContentError, r"pages/data\.json: slug data is reserved"):
            self.build()


class IntegrityTests(Case):
    def setUp(self):
        super().setUp()
        self.add_county()
        self.build()
        self.pristine = self.snapshot()

    def reset(self):
        shutil.rmtree(self.public)
        for name, data in self.pristine.items():
            target = self.public / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)

    def save_descriptor(self, document):
        (self.public / "data" / "datapackage.json").write_text(json.dumps(document, indent=2), encoding="utf-8")

    def tamper_descriptor(self, change):
        document = self.descriptor()
        change(document)
        self.save_descriptor(document)

    def test_check_rejects_integrity_and_metadata_tampering(self):
        cameras = self.public / "data" / "cameras.geojson"
        findings = self.public / "data" / "findings.json"

        def same_size():
            data = findings.read_bytes()
            findings.write_bytes(data[:-2] + bytes([data[-2] ^ 1]) + data[-1:])

        def grow():
            findings.write_bytes(findings.read_bytes() + b" ")

        def bad_digest():
            self.tamper_descriptor(lambda d: d["resources"][0].update(hash="sha256:" + "0" * 64))

        def uppercase_digest():
            self.tamper_descriptor(lambda d: d["resources"][0].update(hash=d["resources"][0]["hash"].upper()))

        def duplicate_keys():
            text = (self.public / "data" / "datapackage.json").read_text(encoding="utf-8")
            (self.public / "data" / "datapackage.json").write_text(text.replace('"profile": "data-package"',
                                                                                  '"profile": "data-package", "profile": "data-package"', 1), encoding="utf-8")

        def duplicate_names():
            self.tamper_descriptor(lambda d: d["resources"][1].update(name=d["resources"][0]["name"]))

        def duplicate_paths():
            self.tamper_descriptor(lambda d: d["resources"].append(dict(d["resources"][0], name="again")))

        def omitted():
            self.tamper_descriptor(lambda d: d["resources"].pop(0))

        def extra():
            self.tamper_descriptor(lambda d: d["resources"].append(dict(d["resources"][0], name="extra", path="secret.json")))

        def parent_path():
            self.tamper_descriptor(lambda d: d["resources"][0].update(path="../index.html"))

        def absolute_path():
            self.tamper_descriptor(lambda d: d["resources"][0].update(path="/etc/passwd"))

        def url_path():
            self.tamper_descriptor(lambda d: d["resources"][0].update(path="https://example.invalid/findings.json"))

        def odbl_removed():
            self.tamper_descriptor(lambda d: [r.update(licenses=[]) for r in d["resources"] if r["name"] == "cameras-geojson"])

        def extra_key():
            self.tamper_descriptor(lambda d: d["resources"][0].update(license="CC0-1.0"))

        def csv_schema():
            self.tamper_descriptor(lambda d: [r["schema"]["fields"].pop() for r in d["resources"] if r["name"] == "agencies-csv"])

        def html_row():
            page = self.public / "data.html"
            page.write_text(page.read_text(encoding="utf-8").replace(f'data-bytes="{len(findings.read_bytes())}"', 'data-bytes="1"', 1), encoding="utf-8")

        def html_shown_digest():
            page = self.public / "data.html"
            digest = hashlib.sha256(findings.read_bytes()).hexdigest()
            page.write_text(page.read_text(encoding="utf-8").replace(f"<code>{digest}</code>", "<code>" + "f" * 64 + "</code>", 1), encoding="utf-8")

        def html_row_dropped():
            page = self.public / "data.html"
            page.write_text(re.sub(r'<tr data-path="data/county.geojson".*?</tr>', "", page.read_text(encoding="utf-8"), count=1, flags=re.S), encoding="utf-8")

        def html_substring_only():  # a path mentioned in text is not a row
            page = self.public / "data.html"
            page.write_text(page.read_text(encoding="utf-8").replace('data-path="data/county.geojson"', 'data-x="data/county.geojson"', 1), encoding="utf-8")

        def symlink_escape():
            outside = Path(self.temporary.name) / "outside.json"
            outside.write_bytes(findings.read_bytes())
            findings.unlink()
            findings.symlink_to(outside)

        def symlink_ancestor():
            moved = Path(self.temporary.name) / "moved-data"
            (self.public / "data").rename(moved)
            (self.public / "data").symlink_to(moved)

        def omitted_city():
            self.tamper_descriptor(lambda d: d.update(resources=[r for r in d["resources"] if "data/" + r["path"] != CITY]))
            page = self.public / "data.html"
            page.write_text(re.sub(r'<tr data-path="' + re.escape(CITY) + r'".*?</tr>', "", page.read_text(encoding="utf-8"), flags=re.S), encoding="utf-8")

        def malformed_license():
            self.tamper_descriptor(lambda d: d["resources"][0].update(licenses=[{"name": [], "path": "x"}]))

        cases = [
            ("null descriptor", lambda: self.save_descriptor(None), "data/datapackage.json"),
            ("omitted city and row", omitted_city, CITY),
            ("unhashable license name", malformed_license, "data/agencies.csv"),
            ("deleted dataset", lambda: cameras.unlink(), "data/cameras.geojson"),
            ("same-size edit", same_size, "data/findings.json"),
            ("size change", grow, "data/findings.json"),
            ("bad digest", bad_digest, "data/agencies.csv"),
            ("uppercase digest", uppercase_digest, "data/agencies.csv"),
            ("duplicate json keys", duplicate_keys, "data/datapackage.json"),
            ("duplicate names", duplicate_names, "data/"),
            ("duplicate paths", duplicate_paths, "data/agencies.csv"),
            ("omitted resource", omitted, "data/agencies.csv"),
            ("extra resource", extra, "data/secret.json"),
            ("parent segment", parent_path, "data/datapackage.json"),
            ("absolute path", absolute_path, "data/datapackage.json"),
            ("url path", url_path, "data/datapackage.json"),
            ("cameras without ODbL", odbl_removed, "data/cameras.geojson"),
            ("extra resource key", extra_key, "data/agencies.csv"),
            ("csv schema header", csv_schema, "data/agencies.csv"),
            ("altered html row", html_row, "data/findings.json"),
            ("altered shown digest", html_shown_digest, "data/findings.json"),
            ("dropped html row", html_row_dropped, "data/county.geojson"),
            ("row by substring only", html_substring_only, "data/county.geojson"),
            ("descriptor missing", lambda: (self.public / "data" / "datapackage.json").unlink(), "data/datapackage.json"),
            ("page missing", lambda: (self.public / "data.html").unlink(), "data.html"),
            ("symlink escape", symlink_escape, "data/findings.json"),
            ("symlinked ancestor", symlink_ancestor, "data/"),
        ]
        for label, tamper, expected in cases:
            with self.subTest(label):
                self.reset()
                tamper()
                problems = verify_data_package(self.public)
                self.assertTrue(problems, label)
                self.assertTrue(any(path.startswith(expected) for path, _ in problems), (label, problems))
                self.assertEqual(problems, sorted(problems))
                with self.assertRaises(ContentError) as caught:
                    check_data_catalogue(self.public)
                self.assertIn(problems[0][0], str(caught.exception))
                before = self.snapshot() if not self.public.is_symlink() and not (self.public / "data").is_symlink() else None
                message, _ = self.check_cli()  # the precheck refuses before the rebuild repairs anything
                self.assertIsNotNone(message, label)
                self.assertIn("data catalogue check failed", message)
                if before is not None:
                    self.assertTrue(self.snapshot() == before, f"{label}: a refused precheck changed bytes")

    def test_cli_check_fails_on_a_changed_file_and_a_plain_build_repairs_it(self):
        findings = self.public / "data" / "findings.json"
        findings.write_bytes(findings.read_bytes() + b" ")
        result = subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "build", "--directory", str(self.root), "--check"],
                                cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("data/findings.json", result.stderr + result.stdout)
        self.assertNotIn("Public tree check passed", result.stdout)
        self.assertEqual(findings.read_bytes(), self.pristine["data/findings.json"] + b" ")  # untouched by the refusal
        with contextlib.redirect_stdout(io.StringIO()):
            self.build()  # an ordinary build repairs old output
        self.assertEqual(verify_data_package(self.public), [])
        result = subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "build", "--directory", str(self.root), "--check"],
                                cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Public tree check passed", result.stdout)

    def test_renamed_json_city_copy_cannot_be_omitted_with_its_row(self):
        city = "data/renamed-city.json"
        (self.content / "map/city-boundaries.geojson").rename(self.content / "map/renamed-city.json")
        self.edit_json(self.content / "map/map-config.json", lambda d: d.update(city_boundaries_url=city))
        self.build()
        self.assertEqual(verify_data_package(self.public), [])
        self.tamper_descriptor(lambda d: d.update(resources=[r for r in d["resources"] if "data/" + r["path"] != city]))
        page = self.public / "data.html"
        page.write_text(re.sub(r'<tr data-path="' + re.escape(city) + r'".*?</tr>', "", page.read_text(encoding="utf-8"), flags=re.S), encoding="utf-8")
        self.assertIn((city, "present but not listed in the data package"), verify_data_package(self.public))
        before = self.snapshot()
        self.assertIn("data catalogue check failed", self.check_cli()[0])
        self.assertEqual(self.snapshot(), before)

    def test_legacy_tree_without_a_catalogue_skips_the_precheck(self):
        (self.public / "data.html").unlink()
        (self.public / "data" / "datapackage.json").unlink()
        self.assertEqual(self.check_cli()[0], None)  # the rebuild writes the catalogue, then the fresh output is checked
        self.assertTrue((self.public / "data.html").is_file())


class StagingTests(Case):
    def test_staging_and_preview_agree(self):
        self.add_county()
        self.build()
        first = self.snapshot()
        self.build()
        self.assertTrue(self.snapshot() == first, "repeated builds must be identical")
        # a clean preview reports nothing, writes nothing
        report = preview_site(self.root)
        self.assertEqual((report["added"], report["removed"], report["changed"], report["unreadable"]), ([], [], [], []))
        self.assertTrue(self.snapshot() == first)
        # a late change of a copied source after planning: nothing is swapped in, staging is cleaned up
        county = self.content / "map" / "county.geojson"
        original = site_module._copy

        def racing_copy(source, target, written, limit):
            if Path(source) == county:
                county.write_text(county.read_text(encoding="utf-8") + " ", encoding="utf-8")
            return original(source, target, written, limit)
        with mock.patch.object(site_module, "_copy", racing_copy):
            with self.assertRaisesRegex(ContentError, "changed while the site was built"):
                self.build()
        self.assertTrue(self.snapshot() == first)
        self.assertEqual(list(self.root.glob(".public.*")), [])
        # a read failure while the catalogue is rebuilt from the staged bytes: the old public stays
        calls = []
        real = site_module._catalogue_payloads

        def failing(*args):
            calls.append(1)
            if len(calls) > 1:
                raise OSError("staged file unreadable")
            return real(*args)
        with mock.patch.object(site_module, "_catalogue_payloads", failing):
            with self.assertRaises(OSError):
                self.build()
        self.assertTrue(self.snapshot() == first)
        self.assertEqual(list(self.root.glob(".public.*")), [])
        # a changed dataset updates both metadata outputs and the preview names them
        def change(finding):
            finding["summary"] = "A different synthetic summary."
        path = next((self.content / "findings").glob("*.json"))
        self.edit_json(path, change)
        report = preview_site(self.root)
        changed = {row["path"] for row in report["changed"]}
        self.assertTrue({"data.html", "data/datapackage.json", "data/findings.json", "data/findings.csv"} <= changed, changed)
        self.assertTrue(self.snapshot() == first, "preview must not write")
        self.build()
        self.assertNotEqual(self.snapshot()["data/datapackage.json"], first["data/datapackage.json"])
        self.assertEqual(verify_data_package(self.public), [])

    def test_preview_without_public_creates_nothing(self):
        report = preview_site(self.root)
        self.assertFalse(report["public_exists"])
        self.assertIn("data.html", {row["path"] for row in report["added"]})
        self.assertIn("data/datapackage.json", {row["path"] for row in report["added"]})
        self.assertFalse(self.public.exists())
        self.assertEqual(list(self.root.glob(".public.*")), [])

    def test_preview_hashes_agree_with_a_real_build(self):
        report = preview_site(self.root)
        planned = {row["path"]: row["bytes"] for row in report["added"]}
        self.build()
        for path, size in planned.items():
            self.assertEqual((self.public / path).stat().st_size, size, path)
        self.assertEqual(sorted(planned), sorted(p for p in self.snapshot()))


if __name__ == "__main__":
    unittest.main()
