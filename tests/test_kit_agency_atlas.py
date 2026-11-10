"""kit-agency atlas: a private, read-only cross-check of a locally saved Atlas CSV. Synthetic CSVs and seed only."""
import contextlib
import copy
import csv
import io
import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import discovery, kit, kit_agencies, kit_atlas  # noqa: E402
from campaign_tool.kit_atlas import AtlasError, NOTICE  # noqa: E402

ALPR = "Automated License Plate Readers"
HEADER = ["Agency", "City", "County", "State", "Technology", "Vendor"]
SENTINEL = "PRIVATE-SENTINEL-4471"


def agency(agency_id, name, kind, jurisdiction_name, place_fips=None):
    return {"agency_id": agency_id, "name": name, "kind": kind, "jurisdiction_name": jurisdiction_name,
            "place_fips": place_fips}


DISTRICT = agency("ca-fict-harbor-district-police", "Fictional Harbor District Police", "district_police",
                  "Fictional Harbor District")
SEED = {"schema_version": 1, "jurisdiction": "us-ca", "counties": [
    {"county_fips": "06901", "name": "Fictional", "seat": "Fictionville", "agencies": [
        agency("ca-fict-county-sheriff", "Fictional County Sheriff's Office", "sheriff", "Fictional County"),
        agency("ca-fict-north-police", "North Fictional Police Department", "police", "City of North Fictional", "0690101"),
        agency("ca-fict-south-police", "South Fictional Police Dept.", "police", "City of South Fictional", "0690102"),
        agency("ca-fict-twin-police-a", "Twin Police Department", "police", "City of Twin", "0690103"),
        agency("ca-fict-twin-police-b", "Twin Police Office", "police", "Town of Twin", "0690104"),
        agency("ca-fict-east-police", "East Fictional Police", "police", "City of East Fictional", "0690105"),
        copy.deepcopy(DISTRICT)]},
    {"county_fips": "06902", "name": "Otherland", "seat": "Otherville", "agencies": [
        agency("ca-other-county-sheriff", "Otherland County Sheriff's Office", "sheriff", "Otherland County"),
        copy.deepcopy(DISTRICT)]}]}


def csv_text(rows, header=HEADER):
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    return out.getvalue()


def row(name, vendor="", county="Fictional", state="CA", tech=ALPR, city="Somewhere"):
    return [name, city, county, state, tech, vendor]


def snapshot(path):
    """Path, type, mode and bytes of everything under (or at) path."""
    path = Path(path)
    result = {}
    for item in ([path] + sorted(path.rglob("*")) if path.is_dir() else [path]):
        info = item.lstat()
        kind = "dir" if stat.S_ISDIR(info.st_mode) else "link" if stat.S_ISLNK(info.st_mode) else "file"
        result[str(item)] = (kind, stat.S_IMODE(info.st_mode), item.read_bytes() if kind == "file" else b"")
    return result


@contextlib.contextmanager
def forbidden():
    """Everything that could fetch or write fails the test when called."""
    def fail(*_args, **_kwargs):
        raise AssertionError("forbidden network or write call")
    with contextlib.ExitStack() as stack:
        for target in ("urllib.request.urlopen", "socket.socket.connect", "subprocess.run", "subprocess.Popen",
                       "campaign_tool.kit._write_json", "campaign_tool.kit_agencies._save",
                       "campaign_tool.kit_agencies.import_csv", "campaign_tool.kit_agencies.set_selected",
                       "campaign_tool.kit_agencies.add_agency", "campaign_tool.discovery.locate_online",
                       "campaign_tool.kit.build_kit", "os.mkdir", "os.rename", "os.replace", "os.chmod"):
            stack.enter_context(mock.patch(target, side_effect=fail))
        yield


class AtlasTestCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-atlas-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.root = self.tmp / "campaign"
        self.entries = [
            {"agency_id": "ca-fict-county-sheriff", "name": "Fictional County Sheriff", "kind": "sheriff"},  # legacy: no selected
            {"agency_id": "ca-fict-north-police", "name": "North Fictional PD", "kind": "police", "selected": False},
            {"agency_id": "ca-fict-harbor-district-police", "name": "Harbor", "kind": "district_police", "selected": True},
            {"agency_id": "ca-fict-east-police", "name": "East Fictional Police", "kind": "police", "selected": True},
            {"agency_id": "ca-fict-twin-police-a", "name": "Twin A", "kind": "police", "selected": True},
            {"agency_id": "ca-fict-added-board", "name": "Added Board", "kind": "other", "selected": True,
             "contact": SENTINEL + "@example.invalid"},
            {"agency_id": "ca-fict-added-off", "name": "Added Off", "kind": "other", "selected": False}]
        self.write_campaign()

    def write_campaign(self, county="Fictional", state="CA"):
        (self.root / "kit").mkdir(parents=True, exist_ok=True)
        (self.root / "campaign.json").write_text(json.dumps(
            {"schema_version": 1, "name": "Synthetic", "county": county, "state": state}), encoding="utf-8")
        (self.root / "kit" / "agencies.json").write_text(json.dumps(self.entries), encoding="utf-8")

    def atlas(self, rows, header=HEADER, name="atlas.csv"):
        path = self.tmp / name
        path.write_text(csv_text(rows, header), encoding="utf-8")
        return path

    def raw(self, data, name="raw.csv"):
        path = self.tmp / name
        path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
        return path

    def compare(self, path, **kwargs):
        return kit_atlas.compare_atlas(self.root, path, seed=kwargs.pop("seed", SEED), **kwargs)

    def refusal(self, path, fragment):
        with self.assertRaises(AtlasError) as caught:
            kit_atlas.read_atlas_csv(path)
        self.assertIn(fragment, str(caught.exception))
        self.assertNotIn(SENTINEL, str(caught.exception))
        return str(caught.exception)


class ReadTests(AtlasTestCase):
    def test_required_headers_width_and_encoding(self):
        for name in kit_atlas.REQUIRED_HEADERS:
            with self.subTest(missing=name):
                header = [h for h in HEADER if h != name]
                path = self.raw(csv_text([], header))
                self.assertEqual(self.refusal(path, "missing required column: "), f"Atlas CSV: missing required column: {name}")
        self.refusal(self.raw(csv_text([], ["Agency", "Vendor", "State", "Notes"])),
                     "missing required column: City, County, Technology")
        self.refusal(self.raw(csv_text([], [h.lower() for h in HEADER])), "missing required column: Agency, City")
        self.refusal(self.raw(csv_text([], HEADER + ["State"])), "duplicate column: State")
        self.refusal(self.raw(csv_text([], HEADER + [SENTINEL, SENTINEL])), "duplicate column: (an extra column)")
        self.refusal(self.raw(csv_text([], HEADER[:5] + ["Company", "Vendor"])), "use one vendor column: Company or Vendor")
        # any order and unknown extra columns are fine; extra values are never kept
        shuffled = ["Notes", "Technology", "State", "County", "City", "Agency"]
        rows = kit_atlas.read_atlas_csv(self.raw(csv_text([[SENTINEL, ALPR, "CA", "Fictional", "", "A One"]], shuffled)))
        self.assertEqual(rows, [{"line": 2, "agency": "A One", "city": "", "county": "Fictional", "state": "CA",
                                 "technology": ALPR, "vendor": ""}])
        # one BOM is accepted; blank rows are ignored; a header-only file is a valid empty comparison
        bom = kit_atlas.read_atlas_csv(self.raw(b"\xef\xbb\xbf" + csv_text([row("A One"), [], [" ", "", "", "", "", ""],
                                                                           row("A Two")]).encode()))
        self.assertEqual([r["line"] for r in bom], [2, 5])
        self.assertEqual(kit_atlas.read_atlas_csv(self.raw(csv_text([]))), [])
        for empty in (b"", b"\n\n \n", b"\xef\xbb\xbf"):
            self.refusal(self.raw(empty), "empty or has no header")
        self.refusal(self.raw(b"\xef\xbb\xbf\xef\xbb\xbf" + csv_text([]).encode()), "missing required column")
        self.refusal(self.raw(csv_text([row("Caf\xe9")]).encode("latin-1")), "not UTF-8")
        self.refusal(self.raw(csv_text([row("A\x00B")])), "NUL")
        # structural errors name the physical starting line, count cells only, and refuse the whole file
        self.refusal(self.raw(csv_text([row("A"), row("B")[:4]])), "line 3: expected 6 cells, found 4")
        self.refusal(self.raw(csv_text([row("A"), row("B") + [SENTINEL]])), "line 3: expected 6 cells, found 7")
        multiline = csv_text([row("A") + ["x\ny"], row("B")[:3]], HEADER + ["Notes"])
        self.refusal(self.raw(multiline), "line 4: expected 7 cells, found 3")
        self.refusal(self.raw(csv_text([row("A")]) + '"unterminated,' + SENTINEL + "\n"), "line 3: malformed")
        self.refusal(self.raw(csv_text([row("A")]) + '"ab"' + SENTINEL + ",c,d,e,f,g\n"), "line 3: malformed")
        # required text, controls and bidi characters in displayed fields
        for position, name in ((0, "Agency"), (2, "County"), (3, "State"), (4, "Technology")):
            with self.subTest(blank=name):
                cells = row("A")
                cells[position] = "  "
                self.refusal(self.raw(csv_text([cells])), f"line 2: {name} is required")
        self.assertEqual(len(kit_atlas.read_atlas_csv(self.raw(csv_text([row("A", city="")])))), 1)
        for bad in ("a\tb", "a\rb", "a\nb", "a\u202eb", "a\u2028b", "a\u2029b", "a\u200bb", "a\x1bb", "a\x7fb"):
            for position, name in ((0, "Agency"), (1, "City"), (5, "Vendor")):
                with self.subTest(bad=repr(bad), field=name):
                    cells = row("A")
                    cells[position] = bad
                    self.refusal(self.raw(csv_text([cells])), f"line 2: {name} contains a control or format character")
        accents = kit_atlas.read_atlas_csv(self.raw(csv_text([row("Ca\xf1on \u201cPD\u201d / Sheriff's, Inc.", vendor=" Flock  Safety ")])))
        self.assertEqual(accents[0]["vendor"], " Flock  Safety ")
        # a field above the stdlib default limit is fine; the process-wide limit is restored
        before = csv.field_size_limit()
        big = kit_atlas.read_atlas_csv(self.raw(csv_text([row("A") + ["x" * 300000]], HEADER + ["Notes"])))
        self.assertEqual(len(big), 1)
        self.assertEqual(csv.field_size_limit(), before)
        self.refusal(self.raw(csv_text([row("A") + ["x" * 300000]], HEADER + ["Notes"]) + '"' + "y" * 10),
                     "malformed")
        self.assertEqual(csv.field_size_limit(), before)

    def test_size_cap_boundary_and_permission_errors(self):
        header = csv_text([]).encode()
        exact = header + b"\n" * (kit_atlas.ATLAS_MAX_BYTES - len(header))
        self.assertEqual(len(exact), 10 * 1024 * 1024)
        self.assertEqual(kit_atlas.read_atlas_csv(self.raw(exact, "exact.csv")), [])
        self.refusal(self.raw(exact + b"\n", "over.csv"), "larger than 10 MiB")
        good = self.atlas([row("A")])
        for error in (PermissionError(13, "Permission denied"), OSError(5, "Input/output error")):
            with self.subTest(error=type(error).__name__), mock.patch("os.open", side_effect=error):
                self.refusal(good, "cannot be opened")
        with mock.patch("os.fstat", side_effect=PermissionError(13, "Permission denied")):
            self.refusal(good, "cannot be read")

    def test_local_only_safe_file_types(self):
        good = self.atlas([row("A")])
        self.refusal(self.tmp / "missing.csv", "cannot be opened")
        self.refusal(self.tmp, "must be a regular file")
        link = self.tmp / "link.csv"
        link.symlink_to(good)
        self.refusal(link, "symlinks are refused")
        dangling = self.tmp / "dangling.csv"
        dangling.symlink_to(self.tmp / "nowhere.csv")
        self.refusal(dangling, "cannot be opened")
        fifo = self.tmp / "fifo.csv"
        os.mkfifo(fifo)
        self.refusal(fifo, "must be a regular file")
        # a URL-looking name is only ever a local filename: nothing is fetched
        with forbidden():
            self.refusal(Path("https://example.invalid/atlas.csv"), "cannot be opened")
            with self.assertRaises(AtlasError):
                kit_atlas.compare_atlas(self.root, "file:///nonexistent/atlas.csv", seed=SEED)
            self.assertEqual(self.compare(good)["state"], "CA")


class CompareTests(AtlasTestCase):
    def listed(self, report):
        return {r["agency_id"]: r for r in report["listed_seed"]["rows"]}

    def test_filter_is_exact_state_county_technology(self):
        rows = [
            row("North Fictional Police Department", county=" fictional  COUNTY ", state=" ca ", tech="  automated  license plate   READERS "),
            row("East Fictional Police", county="Fictional County", city=""),
            row("Fictional County Sheriff's Office", county="Fictional"),
            row("Fictional County Sheriff's Office", county="Fictional County County"),  # only ONE suffix is optional
            row("Fictional County Sheriff's Office", county="Otherland"),
            row("Fictional County Sheriff's Office", state="NV"),
            row("Fictional County Sheriff's Office", state="California"),
            row("Fictional County Sheriff's Office", tech="Automated License Plate Reader"),
            row("Fictional County Sheriff's Office", tech="ALPR"),
            row("Fictional County Sheriff's Office", tech="Body-Worn Cameras"),
            row("Fictional County Sheriff's Office", tech="Automated License Plate Readers (ALPR)"),
            row("Fictional County Sheriff's Office", county="Fictionalton"),
        ]
        report = self.compare(self.atlas(rows))
        self.assertEqual([r["line"] for r in report["listed_seed"]["rows"][0]["atlas"]], [4])  # the sheriff: bare county name
        found = self.listed(report)
        self.assertEqual(sorted(found), ["ca-fict-county-sheriff", "ca-fict-east-police", "ca-fict-north-police"])
        self.assertEqual(found["ca-fict-east-police"]["atlas"][0]["city"], "")
        self.assertEqual(report["unmatched_atlas"]["rows"], [])  # filtered-out rows are not reported at all
        self.assertEqual((report["county"], report["state"]), ("Fictional", "CA"))
        # the multi-county opt-in district belongs only to the requested county
        district = [row("Fictional Harbor District Police", county="Otherland"), row("Fictional Harbor District Police")]
        report = self.compare(self.atlas(district))
        self.assertEqual([a["line"] for a in self.listed(report)["ca-fict-harbor-district-police"]["atlas"]], [3])
        # a row of another county that names a seed agency of ours is simply not in scope
        other = self.compare(self.atlas([row("Otherland County Sheriff's Office")]))
        self.assertEqual(other["unmatched_atlas"]["rows"][0]["reason"], "no_seed_match")
        # unknown county and unsupported state refuse, with no online fallback
        self.entries = []
        path = self.atlas([row("A")])
        for config in ({"county": "Nowhere"}, {"state": "NV"}, {"state": "CAL"}):
            self.write_campaign(**config)
            with forbidden(), self.assertRaises(AtlasError):
                self.compare(path, seed=SEED if config.get("state") != "NV" else None)

    def test_name_normalization_and_collision_trap(self):
        norm = kit_atlas.normalize_agency_name
        self.assertEqual(norm("Fictional County Sheriff's Office"), "fictional county sheriffs")
        self.assertEqual(norm("FICTIONAL COUNTY SHERIFF\u2019S DEPT."), "fictional county sheriffs")
        self.assertEqual(norm("North-Fictional  P.D. & Co. Department Office"), "north fictional p d co")
        self.assertEqual(norm("Police Department Dept Office"), "police")
        self.assertEqual(norm("Office of the Sheriff"), "office of the sheriff")  # internal words stay
        self.assertEqual(norm("Fictional Police"), "fictional police")  # role words stay
        self.assertEqual(norm("Caf\xe9 Police"), "caf\xe9 police")  # accents are not folded
        self.assertEqual(norm("Department"), "")
        self.assertEqual(norm(" . "), "")
        for name in ("North Fictional PD", "Dept. Dept", "A\u2019s Office", "X-Y Dept", "  ", "Z"):
            self.assertEqual(norm(norm(name)), norm(name))
        rows = [row("north fictional police department."), row("NORTH FICTIONAL POLICE OFFICE"),
                row("North Fictional PD"), row("North Fictional Police Dept. Dept"), row("N. Fictional Police Department"),
                row("Fictional Police Department North"), row("Caf\xe9 Police"), row("East Fictional"),
                row("Department"), row(" ... ")]
        report = self.compare(self.atlas(rows))
        self.assertEqual([a["line"] for a in self.listed(report)["ca-fict-north-police"]["atlas"]], [2, 3, 5])
        self.assertEqual([(r["line"], r["reason"]) for r in report["unmatched_atlas"]["rows"]],
                         [(4, "no_seed_match"), (6, "no_seed_match"), (7, "no_seed_match"), (8, "no_seed_match"),
                          (9, "no_seed_match"), (10, "no_seed_match"), (11, "no_seed_match")])
        # the seed: two ids with one key never pick one, even if one of them is selected in the kit
        twin = self.compare(self.atlas([row("Twin Police"), row("Twin Police Department")]))
        self.assertEqual(self.listed(twin), {})
        self.assertEqual([(r["line"], r["reason"], r["candidate_agency_ids"]) for r in twin["unmatched_atlas"]["rows"]],
                         [(2, "ambiguous_seed_name", ["ca-fict-twin-police-a", "ca-fict-twin-police-b"]),
                          (3, "ambiguous_seed_name", ["ca-fict-twin-police-a", "ca-fict-twin-police-b"])])
        self.assertIn("ca-fict-twin-police-a", [r["agency_id"] for r in twin["selected_not_listed"]["rows"]])
        # an organizer-added kit agency is never matched by name
        added = self.compare(self.atlas([row("Added Board")]))
        self.assertEqual(added["unmatched_atlas"]["rows"][0]["reason"], "no_seed_match")
        # an agency that normalises to nothing in the seed can never match an empty key
        odd = copy.deepcopy(SEED)
        odd["counties"][0]["agencies"].append(agency("ca-fict-odd", "Department", "other", "Fictional County"))
        self.assertEqual(self.compare(self.atlas([row("Office")]), seed=odd)["unmatched_atlas"]["rows"][0]["reason"],
                         "no_seed_match")

    def test_selection_ids_and_vendor_verbatim(self):
        rows = [row("Fictional County Sheriff's Office", vendor="Flock Safety"),
                row("Fictional County Sheriff's Office", vendor="flock safety "),
                row("Fictional County Sheriff's Office", vendor="Flock Safety"),  # duplicate listing is kept
                row("North Fictional Police Department", vendor="Vigilant"),
                row("South Fictional Police Dept.", vendor=""),
                row("Fictional Harbor District Police", vendor="Motorola  Solutions")]
        report = self.compare(self.atlas(rows))
        found = self.listed(report)
        self.assertEqual([r["agency_id"] for r in report["listed_seed"]["rows"]], sorted(found))
        self.assertEqual(found["ca-fict-county-sheriff"]["selected"], True)  # legacy entry without `selected`
        self.assertEqual(found["ca-fict-county-sheriff"]["name"], "Fictional County Sheriff's Office")  # seed name
        self.assertEqual([(a["line"], a["vendor"]) for a in found["ca-fict-county-sheriff"]["atlas"]],
                         [(2, "Flock Safety"), (3, "flock safety "), (4, "Flock Safety")])
        self.assertEqual(found["ca-fict-north-police"]["selected"], False)  # deselected in the kit
        self.assertEqual(found["ca-fict-south-police"]["selected"], False)  # not in the kit at all
        self.assertEqual(found["ca-fict-harbor-district-police"]["atlas"][0]["vendor"], "Motorola  Solutions")
        self.assertEqual(found["ca-fict-harbor-district-police"]["selected"], True)
        # selected, not uniquely listed: kit entries incl. organizer-added; deselected ones are ignored
        self.assertEqual(report["selected_not_listed"]["rows"], [
            {"agency_id": "ca-fict-added-board", "name": "Added Board"},
            {"agency_id": "ca-fict-east-police", "name": "East Fictional Police"},
            {"agency_id": "ca-fict-twin-police-a", "name": "Twin A"}])
        # selection follows the id, never the name: the kit calls the sheriff something else
        self.assertNotIn("Fictional County Sheriff\"", json.dumps(report))
        # vendor column spellings
        company = self.compare(self.atlas([[ "Fictional County Sheriff's Office", "", "Fictional", "CA", ALPR, "Axon"]],
                                          header=HEADER[:5] + ["Company"]))
        self.assertEqual(self.listed(company)["ca-fict-county-sheriff"]["atlas"][0]["vendor"], "Axon")
        none = self.compare(self.atlas([["Fictional County Sheriff's Office", "", "Fictional", "CA", ALPR]],
                                       header=HEADER[:5]))
        self.assertEqual(self.listed(none)["ca-fict-county-sheriff"]["atlas"][0]["vendor"], "")
        self.assertIn("vendor not supplied", kit_atlas.render_atlas(none))
        both = self.atlas([], header=HEADER[:5] + ["Company", "Vendor"])
        with self.assertRaises(AtlasError):
            self.compare(both)
        # no contacts, notes or kit-only fields travel into the report
        self.assertNotIn(SENTINEL, json.dumps(report))
        self.assertNotIn(SENTINEL, kit_atlas.render_atlas(report))
        # an empty selection and an empty list both work
        self.entries = []
        self.write_campaign()
        self.assertEqual(self.compare(self.atlas([row("East Fictional Police")]))["selected_not_listed"]["rows"], [])
        self.entries = [{"agency_id": "ca-fict-east-police", "name": "East", "kind": "police", "selected": "yes"}]
        self.write_campaign()
        with self.assertRaises(AtlasError):
            self.compare(self.atlas([row("East Fictional Police")]))

    def test_three_sections_not_evidence_and_json(self):
        sections = ("listed_seed", "unmatched_atlas", "selected_not_listed")
        empty = self.compare(self.atlas([]))
        self.assertEqual(list(empty), ["schema_version", "notice", "county", "state", *sections])
        self.assertEqual(empty["schema_version"], 1)
        self.assertEqual(empty["notice"], NOTICE)
        for key in sections:
            self.assertEqual(list(empty[key]), ["notice", "rows"])
            self.assertEqual(empty[key]["notice"], NOTICE)
        text = kit_atlas.render_atlas(empty)
        headings = ["Seed agencies listed in Atlas", "Atlas rows without a unique seed match",
                    "Selected agencies not listed in Atlas"]
        lines = text.splitlines()
        for heading in headings:
            at = lines.index(heading)
            self.assertEqual(lines[at + 1], NOTICE)
        self.assertEqual(text.count(NOTICE), 3)
        self.assertEqual(lines[-1], "Private, read-only: nothing was fetched, written, selected or published.")
        self.assertEqual(lines[lines.index(headings[0]) + 2], "(none)")
        self.assertEqual(lines[lines.index(headings[1]) + 2], "(none)")
        self.assertIn("- Added Board [ca-fict-added-board]", lines)
        # a full report; hostile-looking text is printed literally and nothing becomes a selection
        hostile = "=HYPERLINK(\"http://example.invalid\")<b>**bold**</b>[x](http://example.invalid)"
        rows = [row("Fictional County Sheriff's Office", vendor=hostile), row(hostile, vendor="V"),
                row("Twin Police Department"), row("East Fictional Police", city=SENTINEL)]
        report = self.compare(self.atlas(rows, header=HEADER))
        full = kit_atlas.render_atlas(report)
        self.assertEqual(full.count(NOTICE), 3)
        self.assertIn(f"CSV line 2: Fictional County Sheriff's Office; vendor: {hostile}", full)
        self.assertIn(f"- CSV line 3: {hostile}; vendor: V; reason: no_seed_match", full)
        self.assertIn("candidates: ca-fict-twin-police-a, ca-fict-twin-police-b", full)
        self.assertIn("- Fictional County Sheriff's Office [ca-fict-county-sheriff]: selected", full)
        self.assertFalse([c for c in full if ord(c) < 32 and c != "\n"])
        for word in ("operates", "compliance", "violation", "illegal", "no ALPR", "uses ALPR"):
            self.assertNotIn(word.lower(), full.lower())
        self.assertEqual(json.loads(json.dumps(report)), report)
        for entry in report["listed_seed"]["rows"]:
            self.assertEqual(list(entry), ["agency_id", "name", "selected", "atlas"])
            self.assertEqual(list(entry["atlas"][0]), ["line", "agency", "city", "vendor"])
        self.assertEqual(list(report["unmatched_atlas"]["rows"][0]),
                         ["line", "agency", "city", "vendor", "reason", "candidate_agency_ids"])
        self.assertEqual(report["unmatched_atlas"]["rows"][0]["candidate_agency_ids"], [])
        self.assertNotIn(str(self.tmp), json.dumps(report))
        # nothing was selected by the comparison
        self.assertEqual(json.loads((self.root / "kit" / "agencies.json").read_text()), self.entries)


class CityTextTests(AtlasTestCase):
    def test_text_city_for_each_listed_csv_row(self):
        path = self.atlas([
            row("North Fictional Police Department", vendor="V1", city=" Northville "),
            row("North Fictional Police Department", vendor="V2", city="Southville"),
            row("Fictional County Sheriff's Office", city="County Seat")])
        text = kit_atlas.render_atlas(self.compare(path))
        expected = [
            "    CSV line 2: North Fictional Police Department; vendor: V1; city:  Northville ",
            "    CSV line 3: North Fictional Police Department; vendor: V2; city: Southville"]
        for line in expected:
            self.assertIn(line, text.splitlines())
        self.assertLess(text.index(expected[0]), text.index(expected[1]))
        self.assertIn("- North Fictional Police Department [ca-fict-north-police]: not selected", text)
        self.assertIn("- Fictional County Sheriff's Office [ca-fict-county-sheriff]: selected", text)

    def test_text_city_for_unmatched_and_ambiguous_rows(self):
        text = kit_atlas.render_atlas(self.compare(self.atlas([
            row("Unknown Agency", vendor="V", city="Unknownville"),
            row("Twin Police", city="Twinville")])))
        self.assertIn("- CSV line 2: Unknown Agency; vendor: V; reason: no_seed_match; city: Unknownville",
                      text.splitlines())
        self.assertIn("- CSV line 3: Twin Police; vendor: vendor not supplied; reason: ambiguous_seed_name; city: Twinville",
                      text.splitlines())
        self.assertIn("    candidates: ca-fict-twin-police-a, ca-fict-twin-police-b", text.splitlines())
        for claim in ("recommended", "operates", "uses ALPR"):
            self.assertNotIn(claim, text)

    def test_empty_city_has_honest_text_fallback_only(self):
        for vendor in (None, "", " ", "V"):
            with self.subTest(vendor=vendor):
                rows = [row("North Fictional Police Department", city=""), row("Unknown", city="   ")]
                for cells in rows:
                    if vendor is None:
                        cells.pop()
                    else:
                        cells[-1] = vendor
                report = self.compare(self.atlas(rows, header=HEADER[:5] if vendor is None else HEADER))
                original = copy.deepcopy(report)
                text = kit_atlas.render_atlas(report)
                displayed_vendor = vendor or "vendor not supplied"
                self.assertIn(f"    CSV line 2: North Fictional Police Department; vendor: {displayed_vendor}; city: city not supplied",
                              text.splitlines())
                self.assertIn(f"- CSV line 3: Unknown; vendor: {displayed_vendor}; reason: no_seed_match; city: city not supplied",
                              text.splitlines())
                self.assertEqual(report["listed_seed"]["rows"][0]["atlas"][0]["city"], "")
                self.assertEqual(report["unmatched_atlas"]["rows"][0]["city"], "   ")
                self.assertEqual(report, original)
                self.assertNotIn("city:", text.split("Selected agencies not listed in Atlas", 1)[1])

    def test_city_is_literal_context_not_a_match_key(self):
        rows = [row("North Fictional Police Department"), row("Twin Police"), row("Unknown"),
                row("East Fictional Police", county="Otherland"),
                row("East Fictional Police", state="NV"), row("East Fictional Police", tech="Body-Worn Cameras")]
        baseline = self.compare(self.atlas(rows))
        for city in ("  Cañón d'Oro, Côte  ", '<b>**city**</b>[x](https://example.invalid)', '=HYPERLINK("x")'):
            with self.subTest(city=city):
                changed = copy.deepcopy(rows)
                for cells in changed:
                    cells[1] = city
                report = self.compare(self.atlas(changed))
                original = copy.deepcopy(report)
                text = kit_atlas.render_atlas(report)
                self.assertEqual(text.count("; city: " + city), 3)
                self.assertEqual(report, original)
                for listed in report["listed_seed"]["rows"]:
                    for atlas in listed["atlas"]:
                        atlas["city"] = "Somewhere"
                for atlas in report["unmatched_atlas"]["rows"]:
                    atlas["city"] = "Somewhere"
                self.assertEqual(report, baseline)

    def test_legacy_report_surfaces_unchanged_except_city_suffix(self):
        # A baseline renderer from main pins every surface except the new suffix.
        report = self.compare(self.atlas([row("North Fictional Police Department", city="CITY"),
                                         row("Twin Police", city="CITY")]))
        expected = ["Atlas cross-check for Fictional County, CA"]
        for key, heading in kit_atlas.SECTIONS:
            expected += ["", heading, NOTICE]
            if key == "listed_seed":
                expected += ["- North Fictional Police Department [ca-fict-north-police]: not selected",
                             "    CSV line 2: North Fictional Police Department; vendor: vendor not supplied; city: CITY"]
            elif key == "unmatched_atlas":
                expected += ["- CSV line 3: Twin Police; vendor: vendor not supplied; reason: ambiguous_seed_name; city: CITY",
                             "    candidates: ca-fict-twin-police-a, ca-fict-twin-police-b"]
            else:
                expected += [f"- {entry['name']} [{entry['agency_id']}]" for entry in report[key]["rows"]]
        self.assertEqual(kit_atlas.render_atlas(report), "\n".join(expected + ["", kit_atlas.TRAILER]))
        empty = kit_atlas.render_atlas(self.compare(self.atlas([])))
        self.assertEqual(empty.count("(none)"), 2)
        self.assertNotIn("city:", empty)
        self.assertNotIn(SENTINEL, kit_atlas.render_atlas(report))


def cli(*arguments, cwd=REPO_ROOT):
    return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", *map(str, arguments)],
                          cwd=cwd, capture_output=True, text=True, timeout=60)


class CliTests(AtlasTestCase):
    """The real shipped seed, a hand-built San Luis Obispo campaign and a synthetic CSV."""

    def setUp(self):
        super().setUp()
        county = next(c for c in discovery.load_seed("us-ca")["counties"] if c["name"] == "San Luis Obispo")
        self.sheriff = next(a for a in county["agencies"] if a["kind"] == "sheriff")
        self.entries = [{"agency_id": self.sheriff["agency_id"], "name": "Kit's own name", "kind": "sheriff",
                         "selected": True},
                        {"agency_id": "ca-slo-added", "name": "Added", "kind": "other", "selected": True}]
        self.write_campaign(county="San Luis Obispo")
        self.csv = self.atlas([row(self.sheriff["name"].replace("Office", "Department"), "Flock Safety",
                                   county="San Luis Obispo County"), row("Nowhere PD", county="San Luis Obispo")])

    def test_city_controls_refuse_entire_file_before_output(self):
        for control in ("\x1b", "\u202e", "\u2028", "\u2029"):
            for county in ("San Luis Obispo", "Otherland"):
                with self.subTest(control=repr(control), county=county):
                    path = self.atlas([row(self.sheriff["name"], county="San Luis Obispo", city="Good"),
                                       row("Unknown", county=county, city=SENTINEL + control)])
                    before = snapshot(self.tmp)
                    with forbidden(), self.assertRaisesRegex(AtlasError, "line 3: City contains a control or format character"):
                        kit_atlas.compare_atlas(self.root, path)
                    for extra in ([], ["--json"]):
                        result = cli("kit-agency", "atlas", "--csv", path, "--directory", self.root, *extra)
                        self.assertEqual((result.returncode, result.stdout), (1, ""))
                        self.assertIn("line 3: City contains a control or format character", result.stderr)
                        self.assertNotIn("Traceback", result.stderr)
                        self.assertNotIn(SENTINEL, result.stderr)
                    self.assertEqual(snapshot(self.tmp), before)

    def test_cli_city_text_json_parity_and_read_only_repeat(self):
        self.csv = self.atlas([row(self.sheriff["name"], "V", county="San Luis Obispo", city=" Cañón, East "),
                               row("Unknown", county="San Luis Obispo", city="Unmatched City")])
        before = snapshot(self.tmp)
        with forbidden():
            report = kit_atlas.compare_atlas(self.root, self.csv)
            text = kit_atlas.render_atlas(report)
        for _ in range(2):
            result = cli("kit-agency", "atlas", "--csv", self.csv, "--directory", self.root)
            self.assertEqual((result.returncode, result.stderr), (0, ""))
            self.assertEqual(result.stdout, text + "\n")
            self.assertIn(f"    CSV line 2: {self.sheriff['name']}; vendor: V; city:  Cañón, East ", result.stdout.splitlines())
            self.assertIn("- CSV line 3: Unknown; vendor: vendor not supplied; reason: no_seed_match; city: Unmatched City",
                          result.stdout.splitlines())
            as_json = cli("kit-agency", "atlas", "--csv", self.csv, "--directory", self.root, "--json")
            self.assertEqual((as_json.returncode, as_json.stderr), (0, ""))
            self.assertEqual(as_json.stdout, json.dumps(report, indent=2, ensure_ascii=False) + "\n")
        self.assertEqual(text.count(NOTICE), 3)
        self.assertTrue(text.endswith(kit_atlas.TRAILER))
        self.assertEqual(snapshot(self.tmp), before)

    def test_read_only_success_errors_and_cli_flags(self):
        before, source = snapshot(self.root), snapshot(self.csv)
        done = cli("kit-agency", "atlas", "--csv", self.csv, "--directory", self.root)
        self.assertEqual((done.returncode, done.stderr), (0, ""))
        self.assertIn("selected\n    CSV line 2:", done.stdout)
        self.assertIn("vendor: Flock Safety", done.stdout)
        self.assertIn("- CSV line 3: Nowhere PD; vendor: vendor not supplied; reason: no_seed_match", done.stdout)
        self.assertIn("- Added [ca-slo-added]", done.stdout)
        self.assertNotIn("Nothing was sent", done.stdout)
        as_json = cli("kit-agency", "atlas", "--csv", self.csv, "--directory", self.root, "--json")
        self.assertEqual(as_json.returncode, 0, as_json.stderr)
        report = json.loads(as_json.stdout)
        self.assertEqual(report["listed_seed"]["rows"][0]["agency_id"], self.sheriff["agency_id"])
        self.assertEqual(report["county"], "San Luis Obispo")
        self.assertEqual(snapshot(self.root), before)
        self.assertEqual(snapshot(self.csv), source)
        self.assertFalse((self.root / "public").exists() or (self.root / "private").exists())
        # refusals: exit 1, one Stopped line, no traceback, nothing changed
        bad = self.raw(csv_text([], HEADER[:4]))
        for arguments, fragment in (((bad, self.root), "missing required column: Technology"),
                                    ((self.tmp / "missing.csv", self.root), "cannot be opened"),
                                    ((self.csv, self.tmp / "no-such-campaign"), "")):
            with self.subTest(fragment=fragment):
                result = cli("kit-agency", "atlas", "--csv", arguments[0], "--directory", arguments[1])
                self.assertEqual((result.returncode, result.stdout), (1, ""))
                self.assertTrue(result.stderr.startswith("Stopped: "), result.stderr)
                self.assertIn(fragment, result.stderr)
                self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(snapshot(self.root), before)
        # argument misuse: exit 2 and none of the mutation flags exist
        for extra in (["--id", "x"], ["--name", "x"], ["--kind", "x"], ["--check"], ["--apply"], ["--replace"],
                      ["--online"], ["--out", "x"], ["--js"], ["--note", "x"]):
            with self.subTest(extra=extra):
                self.assertEqual(cli("kit-agency", "atlas", "--csv", self.csv, "--directory", self.root, *extra).returncode, 2)
        for missing in (["--csv", self.csv], ["--directory", self.root]):
            self.assertEqual(cli("kit-agency", "atlas", *missing).returncode, 2)
        self.assertEqual(cli("kit-agency", "atlas").returncode, 2)
        helped = cli("kit-agency", "atlas", "--help")
        self.assertIn("An Atlas listing is a lead, not evidence.", helped.stdout.replace("\n", " ").replace("  ", " "))
        # the existing actions still need their own flags
        self.assertEqual(cli("kit-agency", "select", "--directory", self.root).returncode, 2)

    def test_corrupt_inputs_and_read_only_directory(self):
        with self.subTest("corrupt kit"):
            (self.root / "kit" / "agencies.json").write_text("{not json", encoding="utf-8")
            with self.assertRaises(AtlasError):
                kit_atlas.compare_atlas(self.root, self.csv)
            result = cli("kit-agency", "atlas", "--csv", self.csv, "--directory", self.root)
            self.assertEqual(result.returncode, 1)
            self.assertNotIn("Traceback", result.stderr)
        (self.root / "kit" / "agencies.json").unlink()
        with self.assertRaises(AtlasError):  # no kit at all: the report would invent an empty selection
            kit_atlas.compare_atlas(self.root, self.csv)
        self.write_campaign(county="San Luis Obispo")
        for config in ([], None, "not an object", 1, True):
            with self.subTest(config=config):
                (self.root / "campaign.json").write_text(json.dumps(config), encoding="utf-8")
                before, source = snapshot(self.root), snapshot(self.csv)
                result = cli("kit-agency", "atlas", "--csv", self.csv, "--directory", self.root)
                self.assertEqual((result.returncode, result.stdout), (1, ""))
                self.assertTrue(result.stderr.startswith("Stopped: Unsupported campaign schema version\n"), result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                with self.assertRaisesRegex(AtlasError, "Unsupported campaign schema version"):
                    kit_atlas.compare_atlas(self.root, self.csv)
                self.assertEqual(snapshot(self.root), before)
                self.assertEqual(snapshot(self.csv), source)
        self.write_campaign(county="San Luis Obispo")
        with mock.patch.object(discovery, "load_seed", side_effect=ValueError("seed is invalid")):
            with self.assertRaises(AtlasError):
                kit_atlas.compare_atlas(self.root, self.csv)
        with mock.patch("os.open", side_effect=PermissionError(13, "Permission denied")):
            with self.assertRaises(AtlasError):
                kit_atlas.compare_atlas(self.root, self.csv)
        # a campaign directory nobody may write to still works, and gains nothing
        for path in sorted(self.root.rglob("*"), reverse=True):
            os.chmod(path, 0o500 if path.is_dir() else 0o400)
        os.chmod(self.root, 0o500)
        self.addCleanup(lambda: [os.chmod(p, 0o700) for p in [self.root, *self.root.rglob("*")]])
        before = snapshot(self.root)
        result = cli("kit-agency", "atlas", "--csv", self.csv, "--directory", self.root, "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(snapshot(self.root), before)
        self.assertFalse((self.root / "public").exists())
        with forbidden():
            self.assertEqual(kit_atlas.compare_atlas(self.root, self.csv)["state"], "CA")


if __name__ == "__main__":
    unittest.main()
