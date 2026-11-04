"""DF0022-13: a city agency's page shows the newest portal camera count beside the map's count for the same picker
city, each with its own observation date, and says they are counted differently. It never computes a gap, ratio,
percentage or ranking and never alleges missing cameras.

Offline, synthetic agencies, boundaries and points only (lon/lat 0..2); the network is patched to fail.
Run from the repository root: python3 -B -m unittest tests.test_agency_mapped_reported -v
"""
import html
import json
import re
import shutil
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from campaign_tool.cli import read_config
from campaign_tool.content import load_content
from campaign_tool.labels import DEFAULTS, PLACEHOLDER
from campaign_tool.legal_claims import legal_notes
from campaign_tool.site import Site, build_site

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
URL = "https://transparency.flocksafety.com/cedar-pd-example"
TODAY = date(2026, 3, 1)  # deliberately neither the portal day nor the map day
CHECKED, MAP_DAY = "2026-02-01", "2026-01-20"
SNAPSHOT = {"schema_version": 1, "osm_base": MAP_DAY + "T10:15:00Z", "source": "overpass"}
SECTION = '<section class="agency-mapped-reported">'
CAVEAT = ("These are counted differently: the portal counts the agency's own cameras, "
          "the map counts points inside a boundary whoever owns them.")
BANNED = re.compile(r"differen(?!tly)|discrepan|\bgap|percent|%|\bratio|\brank|missing|undercount|unaccounted|unreported|"
                    r"hidden|fewer|more than|less than|\bonly\b|\bshort\b|violat|illegal|unlawful|complete", re.I)


def sentences(reported, checked, mapped, map_date):
    return (f"The agency's transparency portal reported {reported} cameras (checked {checked}). "
            f"Community mappers have mapped {mapped} points inside the city limits (map data as of {map_date}).")


def square(west, south, east, north):
    return [[west, south], [east, south], [east, north], [west, north], [west, south]]


def boundary(name):
    return {"type": "Feature", "properties": {"NAME": name}, "geometry": {"type": "Polygon", "coordinates": [square(0.5, 0.5, 1.5, 1.5)]}}


def points(inside, outside=2):
    coords = [(0.51 + 0.9 * (n % 100) / 100, 0.51 + 0.9 * (n // 100) / 100) for n in range(inside)]
    coords += [(0.1 + 0.1 * n, 1.9) for n in range(outside)]
    return {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {}, "geometry": {"type": "Point", "coordinates": list(c)}} for c in coords]}


def refuse_network(*args, **kwargs):
    raise AssertionError("network access attempted")


def paragraph_texts(section):
    return [text_of(p) for p in re.findall(r"<p>(.*?)</p>", section)]


def text_of(fragment):
    return html.unescape(re.sub(r"<[^>]+>", "", fragment))


class MappedReportedTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-mapped-reported-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.public, self.map_dir = self.root / "public", self.root / "content" / "map"
        self.agencies_path = self.root / "content" / "agencies.json"
        self.agencies = json.loads(self.agencies_path.read_text())
        self.set_map(3)

    # -- fixtures -------------------------------------------------------------
    def set_map(self, inside, name="Cedar", snapshot=SNAPSHOT):
        (self.map_dir / "city-boundaries.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": [boundary(name)]}))
        (self.map_dir / "cameras.geojson").write_text(json.dumps(points(inside)))
        path = self.map_dir / "snapshot.json"
        if snapshot is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(json.dumps(snapshot))

    def portal(self, snapshots, index=1, slug="cedar-pd-example"):
        agency = self.agencies[index]
        agency.update(verified=True, verification={"source_url": "https://example.invalid/contact", "checked_on": "2026-01-15"},
                      flock_transparency_slug=slug)
        agency["profile"] = {**agency.get("profile", {}), "transparency": snapshots}

    def snap(self, cameras, checked=CHECKED):
        return {"checked_on": checked, "source_url": URL, "cameras": cameras}

    def write(self):
        self.agencies_path.write_text(json.dumps(self.agencies))

    def build(self):
        self.write()
        with mock.patch("urllib.request.urlopen", side_effect=refuse_network) as urlopen:
            build_site(self.root, today=TODAY)
        self.assertEqual(urlopen.call_count, 0)
        return {p.relative_to(self.public).as_posix(): p.read_bytes() for p in self.public.rglob("*") if p.is_file()}

    def section(self, files):
        page = files["agencies/cedar.html"].decode()
        if SECTION not in page:
            return ""
        return SECTION + page.split(SECTION, 1)[1].split("</section>", 1)[0] + "</section>"

    def site(self):
        self.write()
        return Site(self.root, read_config(self.root), load_content(self.root, TODAY), today=TODAY)

    def eligible_ids(self):
        site = self.site()
        return [a["agency_id"] for a in site.model["agencies"] if site.agency_mapped_reported_block(a)]

    # -- acceptance -----------------------------------------------------------
    def test_exact_two_counts_dates_and_caveat(self):
        self.portal([self.snap(4242)])
        files = self.build()
        section = self.section(files)
        paragraphs = re.findall(r"<p>(.*?)</p>", section)
        self.assertEqual(len(paragraphs), 2)
        self.assertEqual(text_of(paragraphs[0]), sentences(4242, CHECKED, 3, MAP_DAY))
        self.assertEqual(text_of(paragraphs[1]), CAVEAT)
        self.assertIn(f'<time datetime="{CHECKED}">{CHECKED}</time>', paragraphs[0])
        self.assertIn(f'<time datetime="{MAP_DAY}">{MAP_DAY}</time>', paragraphs[0])
        self.assertNotIn(TODAY.isoformat(), section)
        page = files["agencies/cedar.html"].decode()
        end = page.index("</section>", page.index('<section class="agency-transparency">')) + len("</section>")
        self.assertTrue(page[end:].startswith(SECTION))  # directly after the unchanged transparency section
        self.assertIn(f'<a href="{URL}" rel="noopener noreferrer">', page)  # the portal link stays
        self.assertNotIn("4239", section)  # 4242 - 3: no difference is ever computed
        for zero_reported, zero_mapped in ((0, 3), (4242, 0), (0, 0)):
            with self.subTest(reported=zero_reported, mapped=zero_mapped):
                self.portal([self.snap(zero_reported)])
                self.set_map(zero_mapped)
                self.assertEqual(paragraph_texts(self.section(self.build())),
                                 [sentences(zero_reported, CHECKED, zero_mapped, MAP_DAY), CAVEAT])

    def test_newest_only_and_missing_is_not_zero(self):
        older = self.snap(50, "2025-11-15")
        cases = {"newest null with an older count": [{**self.snap(None), "cameras": None}, older],
                 "no snapshot": None,
                 "snapshot without a camera key": [{"checked_on": CHECKED, "source_url": URL, "retention_days": 30}]}
        for label, snapshots in cases.items():
            with self.subTest(label):
                self.agencies = json.loads(self.agencies_path.read_text())
                if snapshots is not None:
                    self.portal(snapshots)
                self.assertEqual(self.section(self.build()), "")
        self.portal([self.snap(7)])
        self.set_map(3, name="Pine")  # a boundary exists, but none named for this city: missing is not zero
        self.assertEqual(self.section(self.build()), "")
        shutil.rmtree(self.map_dir)  # no map at all
        self.assertEqual(self.section(self.build()), "")

    def test_picker_identity_not_agency_name(self):
        self.portal([self.snap(10)])
        self.assertEqual(self.eligible_ids(), ["ca-cedar-police"])
        council = json.loads(json.dumps(self.agencies[2]))  # the city council shares the jurisdiction name, is a picker city
        self.assertEqual(council["jurisdiction_name"], "Cedar")
        extras = []
        for number, kind in enumerate(("sheriff", "chp", "campus_police", "county_board", "other", "police")):
            extra = json.loads(json.dumps(self.agencies[1]))
            extra.update(agency_id=f"ca-extra-{number}", slug=f"extra-{number}", kind=kind,
                         name=f"Cedar duplicate-sounding {kind}", jurisdiction_name="Cedar")
            extra["profile"]["transparency"] = [self.snap(10)]
            extra["flock_transparency_slug"] = "cedar-pd-example"
            extras.append(extra)
        self.agencies += extras  # none of them is in map/cities.json, so none is a picker city
        self.assertEqual(self.eligible_ids(), ["ca-cedar-police"])
        # a city agency outside the picker (map present, but no bounds for its slug) never qualifies by its name
        outside = next(a for a in self.agencies if a["agency_id"] == "ca-extra-5")
        self.assertEqual(outside["kind"], "police")
        self.assertNotIn(outside["slug"], json.loads((self.map_dir / "cities.json").read_text()))
        # a county agency whose own name is a boundary name does not qualify either
        self.set_map(3, name="Cedar County")
        self.assertEqual(self.eligible_ids(), [])

    def test_boundary_name_override_and_jurisdiction_fallback(self):
        self.portal([self.snap(10)])
        cities = self.map_dir / "cities.json"
        self.set_map(3, name="Cedar Override")
        cities.write_text(json.dumps({"cedar": {"name": "Cedar Override", "bounds": [[0.5, 0.5], [1.5, 1.5]]}}))
        self.assertIn("Community mappers have mapped 3 points", text_of(self.section(self.build())))
        cities.write_text(json.dumps({"cedar": {"bounds": [[0.5, 0.5], [1.5, 1.5]]}}))  # no override: jurisdiction_name
        self.assertEqual(self.section(self.build()), "")  # "Cedar Override" is not "Cedar": no fuzzy matching
        self.set_map(3, name="Cedar")
        self.assertIn("mapped 3 points", text_of(self.section(self.build())))

    def test_unknown_map_date_never_invents_freshness(self):
        self.portal([self.snap(10)])
        for label, snapshot in (("null osm_base", {**SNAPSHOT, "osm_base": None}), ("no snapshot file", None)):
            with self.subTest(label):
                self.set_map(3, snapshot=snapshot)
                section = self.section(self.build())
                self.assertEqual(text_of(section).split("(map data as of ")[1].split(")")[0], "unknown")
                self.assertNotIn("Map data date unknown", section)  # the token, not the full existing sentence
                self.assertNotIn(TODAY.isoformat(), section)
                self.assertEqual(section.count("<time"), 1)  # only the portal day is a date
        self.set_map(3, snapshot={**SNAPSHOT, "source": "bad"})  # malformed stays an upstream refusal
        before = {p.name for p in self.public.rglob("*")}
        with self.assertRaises(Exception) as caught:
            self.build()
        self.assertIn("snapshot.json", str(caught.exception))
        self.assertEqual({p.name for p in self.public.rglob("*")}, before)

    def test_cards_and_ineligible_bytes_unchanged(self):
        real = Site.agency_mapped_reported_block
        off = lambda self, agency: ""  # noqa: E731
        variants = {"no snapshot": None, "null cameras": [self.snap(None)], "newest null": [self.snap(None), self.snap(5, "2025-11-15")]}
        for label, snapshots in variants.items():
            with self.subTest(label):
                self.agencies = json.loads(self.agencies_path.read_text())
                if snapshots is not None:
                    self.portal(snapshots)
                with mock.patch.object(Site, "agency_mapped_reported_block", off):
                    disabled = self.build()
                with mock.patch.object(Site, "agency_mapped_reported_block", real):
                    enabled = self.build()
                self.assertEqual(sorted(disabled), sorted(enabled))
                self.assertEqual([p for p in enabled if enabled[p] != disabled[p]], [])
        self.agencies = json.loads(self.agencies_path.read_text())
        self.portal([self.snap(4242)])
        with mock.patch.object(Site, "agency_mapped_reported_block", off):
            disabled = self.build()
        enabled = self.build()
        self.assertEqual([p for p in enabled if enabled[p] != disabled[p]], ["agencies/cedar.html"])
        self.assertEqual(enabled["agencies/cedar.html"].decode().replace(self.section(enabled), ""), disabled["agencies/cedar.html"].decode())
        data = enabled["site-data.js"].decode()
        self.assertIn("3 community-mapped points inside city limits; ownership unconfirmed", data)  # DF2237-10 label stays
        self.assertNotIn("4242", data + enabled["agency-cards.js"].decode())
        self.assertNotIn(SECTION, enabled["agencies.html"].decode())  # the card page is untouched

    def test_comparison_wording_and_legal_guard(self):
        for reported, mapped in ((4242, 3), (3, 4242), (3, 3), (0, 0)):
            with self.subTest(reported=reported, mapped=mapped):
                self.portal([self.snap(reported)])
                self.set_map(mapped)
                text = " ".join(paragraph_texts(self.section(self.build())))
                self.assertEqual(text, sentences(reported, CHECKED, mapped, MAP_DAY) + " " + CAVEAT)
                self.assertIsNone(BANNED.search(text), text)
                self.assertEqual(legal_notes([("agencies/cedar.html#mapped-reported", text)]), [])
                tokens = re.findall(r"(?<![\d-])\d+(?![\d-])", text)  # only the two typed counts appear as numbers
                self.assertEqual(sorted(tokens), sorted([str(reported), str(mapped)]))
        # the wording guard is not a stub, and it would catch the bypasses the spec names
        for bad in ("The agency has 4239 missing cameras.", "That is a gap of 99 percent.", "Cedar ranks first by cameras."):
            self.assertIsNotNone(BANNED.search(bad), bad)
        self.assertNotEqual(legal_notes([("x", "The agency violated SB 34.")]), [])

    def test_labels_escape_and_spanish(self):
        self.portal([self.snap(12)])
        labels = self.root / "content" / "labels.json"
        markers = {"agency.mapped_reported_counts": "R&D {reported}/{checked}/{mapped}/{map_date} \"x\"",
                   "agency.mapped_reported_caveat": "Caveat & \"text\""}
        labels.write_text(json.dumps({"schema_version": 1, "labels": markers}))
        section = self.section(self.build())
        self.assertIn("R&amp;D 12/", section)
        self.assertIn("<p>Caveat &amp; &quot;text&quot;</p>", section)
        self.assertNotIn("transparency portal reported", section)  # English defaults absent under overrides
        self.assertNotIn("counted differently", section)
        labels.write_text(json.dumps({"schema_version": 1, "labels": {"agency.mapped_reported_unknown": "n/d"}}))
        self.set_map(3, snapshot=None)
        self.assertIn("(map data as of n/d)", text_of(self.section(self.build())))
        shutil.copy(REPO_ROOT / "examples" / "labels" / "es.json", labels)  # the unreviewed Spanish draft
        self.set_map(3)
        spanish = text_of(self.section(self.build()))
        self.assertIn("informó 12 cámaras (verificado el 2026-02-01)", spanish)
        self.assertIn("mapeado 3 puntos", spanish)
        self.assertIn("datos del mapa al 2026-01-20", spanish)
        self.set_map(3, snapshot=None)
        self.assertIn("(datos del mapa al desconocida)", text_of(self.section(self.build())))
        draft = json.loads(labels.read_text())["labels"]
        for key in ("agency.mapped_reported_counts", "agency.mapped_reported_caveat", "agency.mapped_reported_unknown"):
            self.assertEqual(sorted(PLACEHOLDER.findall(draft[key])), sorted(PLACEHOLDER.findall(DEFAULTS[key])), key)
            self.assertNotEqual(draft[key], DEFAULTS[key])


if __name__ == "__main__":
    unittest.main()
