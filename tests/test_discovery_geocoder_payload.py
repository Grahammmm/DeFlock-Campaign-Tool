"""RF0240-4: locate_online refuses malformed geocoder response shapes (Location or ValueError).

Synthetic response bytes only; the transport seam is the only thing replaced; no network.
"""
import contextlib
import io
import json
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import cli, discovery  # noqa: E402
from tests import test_kit as _kit  # noqa: E402

BAD = "Census geocoder returned malformed location data"


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def good(counties=None, places=None, **extra):
    geographies = {"Counties": counties if counties is not None else
                   [{"GEOID": "06079", "NAME": "Synthetic County", "BASENAME": "Synthetic"}]}
    if places is not None:
        geographies["Incorporated Places"] = places
    geographies.update(extra)
    return {"result": {"addressMatches": [{"geographies": geographies}]}}


def locate(payload, raw=None):
    data = raw if raw is not None else json.dumps(payload).encode()
    response = Response(data)
    opener = mock.Mock(return_value=response)
    try:
        return discovery.locate_online("Synthetic", "CA", opener=opener), response, opener
    except ValueError as exc:
        return exc, response, opener


class MalformedShapes(unittest.TestCase):
    def assert_bad(self, payload):
        outcome, response, opener = locate(payload)
        self.assertIsInstance(outcome, ValueError, payload)
        self.assertEqual(str(outcome), BAD, payload)
        self.assertTrue(response.closed)
        opener.assert_called_once()

    def test_decoded_container_matrix_refuses_with_domain_error(self):
        place_ok = [{"GEOID": "0668000", "BASENAME": "Town"}]
        for payload in ([], [1], "text", 0, None, False,
                        {"result": None}, {"result": []}, {"result": "x"}, {"result": 0},
                        {"result": {"addressMatches": {}}},
                        {"result": {"addressMatches": "x"}},
                        {"result": {"addressMatches": 0}},
                        {"result": {"addressMatches": False}},
                        {"result": {"addressMatches": {"a": 1}}},
                        {"result": {"addressMatches": [None]}},
                        {"result": {"addressMatches": ["x"]}},
                        {"result": {"addressMatches": [{"geographies": []}]}},
                        {"result": {"addressMatches": [{"geographies": None}]}},
                        good(counties="x"), good(counties={}), good(counties=0),
                        good(counties=[None]), good(counties=["x"]), good(counties=[[]]),
                        good(places="x"), good(places={}), good(places=0), good(places=False),
                        good(places=[None]), good(places=["x"])):
            with self.subTest(payload=payload):
                self.assert_bad(payload)
        for bad_county in ([], "x"):
            with self.subTest(top=bad_county):
                self.assert_bad(good(counties=[bad_county], places=place_ok))

    def test_absent_and_empty_collections_keep_legacy_semantics(self):
        for payload in ({}, {"result": {}}, {"result": {"addressMatches": None}},
                        {"result": {"addressMatches": []}}):
            with self.subTest(payload=payload):
                outcome, *_ = locate(payload)
                self.assertEqual(str(outcome), "Census geocoder found no match for 'Synthetic'")
        for payload in ({"result": {"addressMatches": [{}]}},
                        {"result": {"addressMatches": [{"geographies": {}}]}},
                        {"result": {"addressMatches": [{"geographies": {"Counties": None}}]}},
                        good(counties=[])):
            with self.subTest(payload=payload):
                outcome, *_ = locate(payload)
                self.assertEqual(str(outcome), "Census geocoder returned no county for 'Synthetic'")
        for places in (None, []):
            outcome, *_ = locate(good(places=places))
            self.assertEqual((outcome.match_kind, outcome.place_fips, outcome.place_name),
                             ("county", None, None))

    def test_selected_identifiers_are_not_stringified(self):
        for geoid in (None, True, False, 6079, 0, ["06079"], {"a": 1}, "", "  "):
            with self.subTest(geoid=geoid):
                self.assert_bad(good(counties=[{"GEOID": geoid, "NAME": "Synthetic County"}]))
                self.assert_bad(good(places=[{"GEOID": geoid, "BASENAME": "Town"}]))
        self.assert_bad(good(counties=[{"NAME": "Synthetic County"}]))
        self.assert_bad(good(counties=[{}]))
        outcome, *_ = locate(good(counties=[{"GEOID": "06079", "NAME": "Synthetic County"}],
                                  places=[{"GEOID": "0668000", "BASENAME": "Town"}]))
        self.assertEqual((outcome.county_fips, outcome.place_fips), ("06079", "0668000"))

    def test_name_types_and_existing_fallbacks(self):
        for bad in (1, True, [], {}):
            with self.subTest(bad=bad):
                self.assert_bad(good(counties=[{"GEOID": "06079", "NAME": bad, "BASENAME": "S"}]))
                self.assert_bad(good(counties=[{"GEOID": "06079", "NAME": "S County", "BASENAME": bad}]))
                self.assert_bad(good(places=[{"GEOID": "1", "BASENAME": bad, "NAME": "T city"}]))
        self.assert_bad(good(counties=[{"GEOID": "06079"}]))
        self.assert_bad(good(counties=[{"GEOID": "06079", "NAME": " ", "BASENAME": None}]))
        self.assert_bad(good(counties=[{"GEOID": "06079", "NAME": " County", "BASENAME": "  "}]))
        self.assert_bad(good(places=[{"GEOID": "1", "BASENAME": "", "NAME": " city"}]))
        county = {"GEOID": "06079", "NAME": "Sân José County", "BASENAME": "x"}
        outcome, *_ = locate(good(counties=[county], places=[{"GEOID": "0000001", "BASENAME": "St. Mary's"}]))
        self.assertEqual((outcome.county_name, outcome.place_name), ("Sân José", "St. Mary's"))
        outcome, *_ = locate(good(counties=[{"GEOID": "06079", "NAME": "", "BASENAME": "Base"}],
                                  places=[{"GEOID": "2", "NAME": "Pismo Beach city"}]))
        self.assertEqual((outcome.county_name, outcome.place_name), ("Base", "Pismo Beach"))
        outcome, *_ = locate(good(places=[{"GEOID": "2", "BASENAME": None, "NAME": "Some town"}]))
        self.assertEqual(outcome.place_name, "Some")

    def test_bad_selected_row_is_not_skipped_for_later_good_data(self):
        healthy = {"GEOID": "06079", "NAME": "Synthetic County"}
        self.assert_bad(good(counties=[{"NAME": "No id"}, healthy]))
        self.assert_bad(good(places=[{"BASENAME": "No id"}, {"GEOID": "9", "BASENAME": "T"}]))
        payload = {"result": {"addressMatches": [
            {"geographies": []}, good(counties=[healthy])["result"]["addressMatches"][0]]}}
        self.assert_bad(payload)
        payload = good(counties=[healthy, None, "x"], places=[{"GEOID": "9", "BASENAME": "T"}, 5])
        payload["result"]["addressMatches"].append("junk")
        outcome, *_ = locate(payload)
        self.assertEqual((outcome.county_fips, outcome.place_fips), ("06079", "9"))

    def test_response_cleanup_and_transport_contract(self):
        outcome, response, opener = locate(None, raw=b"\xff")
        self.assertIn("lookup failed", str(outcome))
        self.assertTrue(response.closed)
        outcome, response, _ = locate(None, raw=b"not json")
        self.assertIn("lookup failed", str(outcome))
        outcome, response, opener = locate(good())
        self.assertEqual(outcome.county_fips, "06079")
        self.assertTrue(response.closed)
        self.assertEqual(opener.call_count, 1)
        self.assertEqual(opener.call_args.kwargs, {"timeout": 10})
        with self.assertRaises(RuntimeError):
            discovery.locate_online("S", "CA", opener=mock.Mock(side_effect=RuntimeError("x")))
        failing = mock.Mock(side_effect=OSError("down"))
        with self.assertRaisesRegex(ValueError, "lookup failed: down"):
            discovery.locate_online("S", "CA", opener=failing)


class KitCli(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="synthetic-geocoder-")
        self.addCleanup(self.temporary.cleanup)
        self.root = _kit.init(Path(self.temporary.name) / "campaign", county="San Louis Obispo")

    def snapshot(self):
        return {str(p.relative_to(self.root)): (p.stat().st_mode, p.read_bytes() if p.is_file() else None)
                for p in sorted(self.root.rglob("*"))}

    def run_kit(self, payload):
        data = json.dumps(payload).encode()
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", ["campaign_tool", "kit", "--directory", str(self.root), "--online"]), \
                mock.patch("urllib.request.urlopen", side_effect=lambda *a, **k: Response(data)) as urlopen, \
                mock.patch.object(socket.socket, "connect", side_effect=AssertionError("network")), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as caught:
                cli.main()
        return caught.exception.code, out.getvalue(), err.getvalue(), urlopen

    def test_actual_kit_cli_preserves_hint_and_refuses_before_writes(self):
        before = self.snapshot()
        self.assertNotIn("kit", {p.split("/")[0] for p in before})
        for payload in ([], good(counties=["x"]), good(counties=[{"NAME": "No id"}])):
            with self.subTest(payload=payload):
                code, out, err, urlopen = self.run_kit(payload)
                self.assertEqual(code, 1)
                self.assertEqual(out, "")
                text = err
                self.assertEqual(text.count(BAD), 1, text)
                self.assertIn("No county or city named", text)
                self.assertIn("Did you mean", text)
                self.assertIn("Online lookup also failed", text)
                self.assertNotIn("Traceback", text)
                urlopen.assert_called_once()
                self.assertEqual(self.snapshot(), before)

    def test_valid_response_and_offline_paths_unchanged(self):
        from campaign_tool import kit
        seed = discovery.load_seed("us-ca")
        cfg = {"state": "CA", "county": "San Louis Obispo"}
        slo = next(c for c in seed["counties"] if c["name"] == "San Luis Obispo")
        response = good(counties=[{"GEOID": slo["county_fips"], "NAME": "San Luis Obispo County"}])
        with mock.patch("urllib.request.urlopen", side_effect=lambda *a, **k: Response(
                json.dumps(response).encode())) as urlopen:
            found = kit._resolve(cfg, seed, True)
            self.assertEqual((found.county_fips, found.county_name, found.match_kind),
                             (slo["county_fips"], "San Luis Obispo", "county"))
            urlopen.assert_called_once()
            urlopen.reset_mock()
            offline = kit._resolve({"state": "CA", "county": "San Luis Obispo"}, seed, True)
            self.assertEqual(offline.county_fips, slo["county_fips"])
            urlopen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
