"""RF1300-3: an unknown place suggests the closest seed names; ``kit --online`` keeps the hint.

Offline only: ``discovery.locate_online`` is patched wherever the online path runs.
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import discovery, kit  # noqa: E402

NO_MATCH = "No county or city named"
ONLINE_FAILED = "Census geocoder lookup failed: timed out"


def county(fips, name, cities=()):
    agencies = [{"kind": "police", "jurisdiction_name": f"City of {city}", "place_fips": None}
                for city in cities]
    return {"county_fips": fips, "name": name, "agencies": agencies}


def tiny_seed():
    names = ["Abcdefghijk", "Abcdefghijkl", "Abcdefghix", "Xbcdefghij", "Abcdefghxx", "Cañada"]
    return {"jurisdiction": "us-ca",
            "counties": [county(f"06{n:03d}", name) for n, name in enumerate(names, 1)]}


def no_match_message(query):
    return (f"No county or city named {query!r} in the CA agency seed; check spelling, "
            "add it to tools/build_agency_seed.py, or use --online")


class SuggestionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.seed = discovery.load_seed("us-ca")
        slo = next(c for c in cls.seed["counties"] if c["name"] == "San Luis Obispo")
        cls.slo_cities = dict(discovery.cities_in(slo))

    def error(self, query, seed=None):
        with self.assertRaises(ValueError) as caught:
            discovery.locate(query, "CA", seed or self.seed)
        return str(caught.exception)

    def test_county_typo_suggests_counties_only(self):
        message = self.error("San Louis Obispo County")
        self.assertIn(NO_MATCH, message)
        self.assertTrue(message.endswith(" Did you mean: San Luis Obispo County?"), message)
        self.assertNotIn("City of", message)

    def test_city_typo_suggests_the_seed_city_label(self):
        self.assertIn("Paso Robles", self.slo_cities)
        message = self.error("Paso Robels")
        self.assertIn(NO_MATCH, message)
        self.assertIn("Did you mean: City of Paso Robles (San Luis Obispo County)", message)

    def test_city_hint_suggests_cities_only(self):
        message = self.error("City of San Luis Obispoo")
        self.assertIn("Did you mean: City of San Luis Obispo (San Luis Obispo County)?", message)
        self.assertNotIn("Did you mean: San Luis Obispo County", message)

    def test_bare_typo_suggests_both_kinds(self):
        message = self.error("San Luis Obispoo")
        self.assertIn(NO_MATCH, message)
        self.assertIn("Did you mean: City of San Luis Obispo (San Luis Obispo County); "
                      "San Luis Obispo County?", message)
        self.assertEqual(discovery.suggestions(self.seed, "San Luis Obispoo"),
                         ["City of San Luis Obispo (San Luis Obispo County)", "San Luis Obispo County"])

    def test_nothing_close_keeps_the_message_unchanged(self):
        for query in ("SLO", "Zzyzx Flats", "Zzyzx Flats County", "City of Zzyzx"):
            with self.subTest(query=query):
                self.assertEqual(self.error(query), no_match_message(query))
                self.assertNotIn("Did you mean", self.error(query))

    def test_limit_order_and_stability(self):
        seed = tiny_seed()
        expected = ["Abcdefghijk County", "Abcdefghijkl County", "Abcdefghix County"]
        first = discovery.suggestions(seed, "abcdefghij")
        self.assertEqual(first, expected)  # closest first; the 0.9 tie broken by label
        self.assertEqual(discovery.suggestions(seed, "abcdefghij"), first)
        self.assertEqual(len(discovery.suggestions(seed, "abcdefghij", limit=5)), 5)
        self.assertEqual(discovery.suggestions(seed, "abcdefghij", hint="city"), [])
        message = self.error("abcdefghij", seed)
        self.assertTrue(message.endswith(" Did you mean: " + "; ".join(expected) + "?"), message)
        self.assertEqual(self.error("abcdefghij", seed), message)

    def test_accented_county_matches_without_suggestions(self):
        with mock.patch.object(discovery, "suggestions", side_effect=AssertionError("consulted")):
            found = discovery.locate("Canada County", "CA", tiny_seed())
        self.assertEqual((found.county_name, found.match_kind), ("Cañada", "county"))

    def test_matches_never_consult_suggestions(self):
        with mock.patch.object(discovery, "suggestions", side_effect=AssertionError("consulted")):
            morro = discovery.locate("Morro Bay", "CA", self.seed)
            kern = discovery.locate("kern county", "CA", self.seed)
            with self.assertRaises(ValueError) as caught:
                discovery.locate("San Luis Obispo", "CA", self.seed)
        self.assertEqual((morro.match_kind, morro.place_name, morro.county_name),
                         ("city", "Morro Bay", "San Luis Obispo"))
        self.assertEqual((kern.match_kind, kern.county_name), ("county", "Kern"))
        message = str(caught.exception)
        self.assertIn("Ambiguous", message)
        self.assertIn("San Luis Obispo County", message)
        self.assertIn("City of San Luis Obispo (San Luis Obispo County)", message)
        self.assertNotIn("Did you mean", message)

    def test_other_errors_carry_no_suggestions(self):
        for query, state, text in (("", "CA", "non-empty"), ("San Louis Obispo", "OR", "Seed covers CA"),
                                   ("!!!", "CA", "Could not parse")):
            with self.subTest(query=query):
                with self.assertRaises(ValueError) as caught:
                    discovery.locate(query, state, self.seed)
                self.assertIn(text, str(caught.exception))
                self.assertNotIn("Did you mean", str(caught.exception))


class KitOnlineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.seed = discovery.load_seed("us-ca")

    def resolve_error(self, county_field, online, online_error=ONLINE_FAILED):
        cfg = {"state": "CA", "county": county_field}
        geocoder = mock.Mock(side_effect=ValueError(online_error))
        with mock.patch.object(discovery, "locate_online", geocoder):
            with self.assertRaises(ValueError) as caught:
                kit._resolve(cfg, self.seed, online)
        return str(caught.exception), geocoder

    def test_online_failure_keeps_offline_hint_and_online_reason(self):
        message, geocoder = self.resolve_error("San Louis Obispo", online=True)
        geocoder.assert_called_once_with("San Louis Obispo County", "CA")
        self.assertIn(NO_MATCH, message)
        self.assertIn("Did you mean: San Luis Obispo County?", message)
        self.assertTrue(message.endswith(" Online lookup also failed: " + ONLINE_FAILED), message)
        self.assertEqual(message.count(NO_MATCH), 1)

    def test_online_no_match_without_suggestion_still_keeps_both(self):
        message, _geocoder = self.resolve_error("Zzyzx Flats", True, "Census geocoder found no match for 'x'")
        self.assertTrue(message.startswith(no_match_message("Zzyzx Flats County")), message)
        self.assertNotIn("Did you mean", message)
        self.assertIn("Online lookup also failed: Census geocoder found no match for 'x'", message)

    def test_offline_only_message_without_online(self):
        message, geocoder = self.resolve_error("San Louis Obispo", online=False)
        geocoder.assert_not_called()
        self.assertTrue(message.endswith(" Did you mean: San Luis Obispo County?"), message)
        self.assertNotIn("Online lookup", message)

    def test_online_success_path_unchanged(self):
        found = discovery.Location("06079", "San Luis Obispo", "CA", None, None, "county", "x")
        with mock.patch.object(discovery, "locate_online", return_value=found):
            location = kit._resolve({"state": "CA", "county": "San Louis Obispo"}, self.seed, True)
        self.assertEqual((location.county_name, location.match_kind), ("San Luis Obispo", "county"))


if __name__ == "__main__":
    unittest.main()
