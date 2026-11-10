"""RF1415-1: a boundary's catalogue licence and credit must be what its own public bytes state.

Synthetic content only (temp copies of the fictional example); sockets are blocked, nothing is fetched.
Expected licence pairs and credits are written out here, never computed by the helper under test.
"""
import contextlib
import hashlib
import io
import json
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool import cli
from campaign_tool.content import ContentError
from campaign_tool.site import build_site, preview_site
from campaign_tool.site_check import check_data_catalogue
from campaign_tool.site_data_catalogue import verify_data_package

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
COUNTY, CITY = "data/county.geojson", "data/city-boundaries.geojson"
OSM = "\u00a9 OpenStreetMap contributors"
PAIRS = {
    "ODbL-1.0": {"name": "ODbL-1.0", "path": "https://opendatacommons.org/licenses/odbl/1-0/"},
    "CC-BY-4.0": {"name": "CC-BY-4.0", "path": "https://creativecommons.org/licenses/by/4.0/"},
    "CC-BY-SA-4.0": {"name": "CC-BY-SA-4.0", "path": "https://creativecommons.org/licenses/by-sa/4.0/"},
    "CC0-1.0": {"name": "CC0-1.0", "path": "https://creativecommons.org/publicdomain/zero/1.0/"},
}
LICENCE_REASON = "licenses differ from the licence the boundary file states"
CREDIT_REASON = "attribution differs from the credit the boundary file's licence requires"
PASSED = "Public tree check passed"


class Case(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="synthetic-boundary-licence-")
        self.addCleanup(self.temporary.cleanup)
        patcher = mock.patch("socket.socket", side_effect=AssertionError("network access attempted"))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.use_root("campaign")

    def use_root(self, name):
        self.root = Path(self.temporary.name) / name
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.public, self.content = self.root / "public", self.root / "content"

    def edit_json(self, path, change):
        document = json.loads(path.read_text(encoding="utf-8"))
        change(document)
        path.write_text(json.dumps(document), encoding="utf-8")

    def boundary(self, name, **members):
        """Write content/map/<name> from the example city boundary plus ``members`` (``license=None`` drops it)."""
        document = json.loads((EXAMPLE / "content" / "map" / "city-boundaries.geojson").read_text(encoding="utf-8"))
        document.pop("license", None)
        document.update({key: value for key, value in members.items() if value is not None})
        (self.content / "map" / name).write_text(json.dumps(document), encoding="utf-8")

    def build(self):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            build_site(self.root)

    def resources(self):
        document = json.loads((self.public / "data" / "datapackage.json").read_text(encoding="utf-8"))
        return {"data/" + r["path"]: r for r in document["resources"]}

    def tamper(self, path, change):
        target = self.public / "data" / "datapackage.json"
        document = json.loads(target.read_text(encoding="utf-8"))
        for resource in document["resources"]:
            if "data/" + resource["path"] == path:
                change(resource)
        target.write_text(json.dumps(document, indent=2), encoding="utf-8")

    def snapshot(self):
        """Every path under public/ with its bytes and mode bits."""
        found = {}
        for base, dirs, files in os.walk(self.public):
            for name in dirs + files:
                full = Path(base) / name
                mode = os.lstat(full).st_mode
                found[full.relative_to(self.public).as_posix()] = (stat.S_IMODE(mode), full.read_bytes() if stat.S_ISREG(mode) else None)
        return found

    def check_cli(self):
        """In-process ``build --check``: (refusal message or None, stdout)."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            try:
                cli.build(self.root, check=True)
            except ValueError as exc:
                return str(exc), out.getvalue()
        return None, out.getvalue()

    def assert_refused(self, path, reason, payload=None):
        before = self.snapshot()
        problems = verify_data_package(self.public)
        self.assertIn((path, reason), problems)
        self.assertEqual(problems, sorted(set(problems)))
        with self.assertRaises(ContentError) as caught:
            check_data_catalogue(self.public)
        self.assertIn(f"{path}: {reason}", str(caught.exception))
        if payload is not None:  # diagnostics name paths and fixed reasons, never the submitted values
            self.assertNotIn(payload, str(caught.exception))
        self.assertTrue(self.snapshot() == before, "a direct check wrote to public/")


class BoundaryLicenceTests(Case):
    def test_odbl_boundary_cannot_be_relabelled_cc0(self):
        self.boundary("county.geojson", license="ODbL-1.0")
        self.build()
        county = self.resources()[COUNTY]
        self.assertEqual((county["licenses"], county["attribution"]), ([PAIRS["ODbL-1.0"]], OSM))
        self.assertEqual(verify_data_package(self.public), [])
        digest = hashlib.sha256((self.public / COUNTY).read_bytes()).hexdigest()

        def relabel(resource):
            resource["licenses"] = [PAIRS["CC0-1.0"]]
            resource.pop("attribution")
        self.tamper(COUNTY, relabel)
        self.assert_refused(COUNTY, LICENCE_REASON)
        self.assertIn((COUNTY, CREDIT_REASON), verify_data_package(self.public))
        self.assertEqual(hashlib.sha256((self.public / COUNTY).read_bytes()).hexdigest(), digest)
        self.assertEqual(json.loads((self.public / COUNTY).read_bytes())["license"], "ODbL-1.0")

    def test_city_boundary_licence_comes_from_its_own_bytes(self):
        self.boundary("county.geojson", license="ODbL-1.0")
        self.boundary("city-boundaries.geojson", license="CC-BY-4.0")
        self.build()
        resources = self.resources()
        self.assertEqual(resources[CITY]["licenses"], [PAIRS["CC-BY-4.0"]])
        self.assertNotIn("attribution", resources[CITY])
        self.assertEqual(verify_data_package(self.public), [])
        self.tamper(CITY, lambda r: r.update(licenses=[PAIRS["ODbL-1.0"]], attribution=OSM))  # the county's rights
        self.assert_refused(CITY, LICENCE_REASON)
        self.assertIn((CITY, CREDIT_REASON), verify_data_package(self.public))
        self.assertNotIn(COUNTY, {path for path, _ in verify_data_package(self.public)})

        self.use_root("renamed")  # a configured, renamed .json copy is the same resource
        renamed = "data/renamed-city.json"
        self.boundary("county.geojson", license="CC-BY-SA-4.0")
        self.boundary("renamed-city.json", license="CC0-1.0")
        (self.content / "map" / "city-boundaries.geojson").unlink()
        self.edit_json(self.content / "map" / "map-config.json", lambda d: d.update(city_boundaries_url=renamed))
        self.build()
        resources = self.resources()
        self.assertEqual(resources[renamed]["licenses"], [PAIRS["CC0-1.0"]])
        self.assertEqual(resources[COUNTY]["licenses"], [PAIRS["CC-BY-SA-4.0"]])
        self.assertEqual(verify_data_package(self.public), [])
        self.tamper(renamed, lambda r: r.update(licenses=[]))
        self.assert_refused(renamed, LICENCE_REASON)

    def test_known_unknown_and_absent_boundary_licences(self):
        for stated, pair in PAIRS.items():
            with self.subTest(stated):
                self.boundary("county.geojson", license=stated)
                self.build()
                self.assertEqual(self.resources()[COUNTY]["licenses"], [pair])
                self.assertEqual(verify_data_package(self.public), [])
        for label, stated in (("absent", None), ("unknown", "made-up-licence"), ("non-string", 42)):
            with self.subTest(label):
                self.boundary("county.geojson", license=stated)
                self.build()
                self.assertEqual(self.resources()[COUNTY]["licenses"], [])
                self.assertEqual(verify_data_package(self.public), [])

        self.boundary("county.geojson", license="ODbL-1.0")  # county states ODbL; the city states nothing
        self.build()
        pristine = self.snapshot()
        wrong_url = "https://example.invalid/odbl-elsewhere"
        cases = [
            ("deleted known licence", COUNTY, lambda r: r.update(licenses=[]), None),
            ("different known id", COUNTY, lambda r: r.update(licenses=[PAIRS["CC-BY-4.0"]]), None),
            ("wrong url", COUNTY, lambda r: r.update(licenses=[{"name": "ODbL-1.0", "path": wrong_url}]), wrong_url),
            ("duplicate", COUNTY, lambda r: r.update(licenses=[PAIRS["ODbL-1.0"], PAIRS["ODbL-1.0"]]), None),
            ("extra", COUNTY, lambda r: r.update(licenses=[PAIRS["ODbL-1.0"], PAIRS["CC0-1.0"]]), None),
            ("licenses removed", COUNTY, lambda r: r.pop("licenses"), None),
            ("invented on unstated city", CITY, lambda r: r.update(licenses=[PAIRS["CC0-1.0"]]), None),
            ("unknown id invented", CITY, lambda r: r.update(licenses=[{"name": "made-up-licence", "path": wrong_url}]), wrong_url),
        ]
        for label, path, change, payload in cases:
            with self.subTest(label):
                self.restore(pristine)
                self.assertEqual(verify_data_package(self.public), [])
                self.tamper(path, change)
                self.assert_refused(path, LICENCE_REASON, payload)

    def test_boundary_attribution_matches_existing_generation_rule(self):
        self.boundary("county.geojson", license="ODbL-1.0")
        self.boundary("city-boundaries.geojson", license="CC-BY-4.0")
        self.edit_json(self.content / "site.json", lambda s: s.update(license={"id": "CC-BY-4.0", "attribution": "Cedar Records Project"}))
        self.build()
        self.assertEqual(self.resources()[COUNTY]["attribution"], OSM)
        self.assertNotIn("attribution", self.resources()[CITY])  # no campaign credit on a boundary
        self.assertEqual(verify_data_package(self.public), [])
        pristine = self.snapshot()
        cases = [
            ("odbl credit missing", COUNTY, lambda r: r.pop("attribution"), None),
            ("odbl credit changed", COUNTY, lambda r: r.update(attribution="Someone Else Entirely"), "Someone Else Entirely"),
            ("odbl credit empty", COUNTY, lambda r: r.update(attribution=""), None),
            ("campaign credit on non-odbl city", CITY, lambda r: r.update(attribution="Cedar Records Project"), "Cedar Records Project"),
            ("osm credit on non-odbl city", CITY, lambda r: r.update(attribution=OSM), None),
        ]
        for label, path, change, payload in cases:
            with self.subTest(label):
                self.restore(pristine)
                self.tamper(path, change)
                self.assert_refused(path, CREDIT_REASON, payload)
                self.assertNotIn((path, LICENCE_REASON), verify_data_package(self.public))

    def restore(self, pristine):
        shutil.rmtree(self.public)
        for name, (mode, data) in sorted(pristine.items()):
            target = self.public / name
            if data is None:
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            os.chmod(target, mode)


class CheckedBuildTests(Case):
    def test_checked_build_refuses_before_repairing_boundary_metadata(self):
        for name in ("first", "second"):  # separate synthetic roots: no shared state carries the result
            with self.subTest(name):
                self.use_root(name)
                self.boundary("county.geojson", license="ODbL-1.0")
                self.build()
                generated = (self.public / "data" / "datapackage.json").read_bytes()

                def relabel(resource):
                    resource["licenses"] = [PAIRS["CC0-1.0"]]
                    resource.pop("attribution")
                self.tamper(COUNTY, relabel)
                before = self.snapshot()
                message, out = self.check_cli()
                self.assertIsNotNone(message)
                self.assertIn("data catalogue check failed", message or "")
                self.assertIn(f"{COUNTY}: {LICENCE_REASON}", message or "")
                self.assertNotIn(PASSED, out)
                self.assertTrue(self.snapshot() == before, "a refused checked build changed public/")
                self.build()  # an ordinary build regenerates the metadata from unchanged content
                self.assertEqual((self.public / "data" / "datapackage.json").read_bytes(), generated)
                self.assertEqual(verify_data_package(self.public), [])
                message, out = self.check_cli()
                self.assertIsNone(message)
                self.assertIn(PASSED, out)

    def test_nonboundary_contracts_and_output_stay_unchanged(self):
        (self.content / "map" / "county.geojson").unlink(missing_ok=True)  # no county: nothing to compare
        for campaign in (None, {"id": "CC-BY-4.0", "attribution": "Cedar Records Project"}, {"id": "CC-BY-SA-4.0"}, {"id": "CC0-1.0"}):
            with self.subTest(campaign):
                def change(site):
                    site.pop("license", None)
                    if campaign:
                        site["license"] = campaign
                self.edit_json(self.content / "site.json", change)
                self.build()
                first = self.snapshot()
                self.assertNotIn(COUNTY, self.resources())
                self.assertEqual(self.resources()[CITY]["licenses"], [])
                self.assertEqual(self.resources()["data/cameras.geojson"]["licenses"], [PAIRS["ODbL-1.0"]])
                self.assertEqual(verify_data_package(self.public), [])
                self.build()
                self.assertTrue(self.snapshot() == first, "repeated healthy builds must be identical")
                report = preview_site(self.root)
                self.assertEqual((report["added"], report["removed"], report["changed"]), ([], [], []))
                message, out = self.check_cli()
                self.assertIsNone(message)
                self.assertIn(PASSED, out)
        self.tamper("data/cameras.geojson", lambda r: r.update(licenses=[]))  # the camera rule keeps its own reason
        problems = verify_data_package(self.public)
        self.assertIn(("data/cameras.geojson", "cameras.geojson must carry ODbL-1.0"), problems)
        self.assertNotIn(LICENCE_REASON, {reason for _, reason in problems})


if __name__ == "__main__":
    unittest.main()
