"""Build-input refusals name every bad field and never echo a value (RF1240-1)."""
import copy
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from campaign_tool import agency_cards, map_controller, shell
from campaign_tool.content import load_map

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "fictional-campaign"
CONFIG = json.loads((EXAMPLE / "map-config.json").read_text())
CARDS = json.loads((EXAMPLE / "agency-cards.json").read_text())
AGENCY_PREFIX = "f2ab83435cea4374"  # pragma: allowlist secret -- SHA-256 prefix of public generated agency cards, not a credential


def payload():
    return {"schema_version": 1, "language": "en",
            "html": {"head": "", "header": "", "main": "", "footer": ""},
            "styles": {"font_family": "Georgia,serif", "font_faces": ""}}


def run(args, data):
    raw = data if isinstance(data, bytes) else json.dumps(data).encode()
    return subprocess.run([sys.executable, "-B", *args], input=raw, capture_output=True, cwd=ROOT, timeout=60)


def bad_cards():
    value = copy.deepcopy(CARDS)
    del value["county_profile"]["vendor"]
    value["county_profile"]["facts"].append("<b>x</b>")
    value["source_aliases"]["Bad"] = "x"
    return value


class MapControllerProblemsTests(unittest.TestCase):
    def test_valid_input_has_no_problems(self):
        self.assertEqual(map_controller.problems(CONFIG), [])
        self.assertEqual(map_controller.problems(CONFIG, "", ""), [])

    def test_several_problems_at_once_in_sorted_order(self):
        config = {key: value for key, value in CONFIG.items() if key != "initial_point_count"}
        config.update(private=True, group_slug="Bad Slug")
        expected = ["group_slug: invalid slug", "missing field: initial_point_count", "unknown field: private"]
        self.assertEqual(map_controller.problems(config), expected)
        with self.assertRaises(ValueError) as caught:
            map_controller.render(config, "")
        self.assertEqual(str(caught.exception), "; ".join(expected))

    def test_values_are_never_echoed(self):
        config = {**CONFIG, "source_map_url": "https://user:pw@example.invalid",  # pragma: allowlist secret -- synthetic rejected URL
                  "group_slug": 'x";alert(1)', "<script>": 1}
        found = map_controller.problems(config)
        self.assertEqual(found, ["group_slug: invalid slug", "source_map_url: not a public https URL",
                                 "unknown field: 8 chars"])
        for text in found:
            for value in ("pw", "alert", "<script", "user"):
                self.assertNotIn(value, text)

    def test_hostless_url_named_without_value(self):
        self.assertEqual(map_controller.problems({**CONFIG, "source_map_url": "https:///path"}),
                         ["source_map_url: credentials or no host in URL"])

    def test_long_or_odd_unknown_key_shown_by_length(self):
        found = map_controller.problems({**CONFIG, "k" * 65: 1, "ok_name": 2, "a b": 3})
        self.assertEqual(found, ["unknown field: 3 chars", "unknown field: 65 chars", "unknown field: ok_name"])

    def test_missing_field_reported_once(self):
        config = {key: value for key, value in CONFIG.items() if key != "source_map_url"}
        self.assertEqual(map_controller.problems(config), ["missing field: source_map_url"])

    def test_bool_is_not_int(self):
        self.assertEqual(map_controller.problems({**CONFIG, "schema_version": True, "initial_point_count": True}),
                         ["initial_point_count: out of range", "schema_version: unsupported schema version"])

    def test_every_field_check_named(self):
        config = {**CONFIG, "candidate_property": "1x", "bounding_box_fallbacks": ["cedar", "cedar"],
                  "initial_point_count": -1, "city_boundaries_url": "../secret.geojson", "candidate_count_suffix": ""}
        self.assertEqual(map_controller.problems(config, None, 7), [
            "bounding_box_fallbacks: invalid or duplicate slugs", "candidate_count_suffix: must be 1-200 characters",
            "candidate_property: invalid identifier", "city_boundaries_url: not a local public asset path",
            "initial_point_count: out of range", "point_context_renderer: invalid trusted renderer",
            "profile_renderer: invalid trusted renderer"])
        self.assertEqual(map_controller.problems({**CONFIG, "bounding_box_fallbacks": [{}]}),
                         ["bounding_box_fallbacks: invalid or duplicate slugs"])

    def test_non_dict_config(self):
        for value in (None, [], "config"):
            self.assertEqual(map_controller.problems(value), ["config: must be an object"])
            with self.assertRaises(ValueError):
                map_controller.render(value, "")

    def test_renderer_over_max_bytes(self):
        big = "x" * (map_controller.MAX_BYTES + 1)
        self.assertEqual(map_controller.problems(CONFIG, big), ["profile_renderer: invalid trusted renderer"])
        self.assertEqual(map_controller.problems(CONFIG, "", big), ["point_context_renderer: invalid trusted renderer"])

    def test_cli_names_both_fields(self):
        config = {**CONFIG, "group_slug": "Bad Slug", "initial_point_count": -1}
        result = run(["-m", "campaign_tool.map_controller"], {"config": config, "profile_renderer": ""})
        self.assertEqual((result.returncode, result.stdout), (1, b""))
        err = result.stderr.decode()
        self.assertTrue(err.startswith("Map build stopped: "), err)
        self.assertIn("group_slug: invalid slug; initial_point_count: out of range", err)
        self.assertNotIn("Bad Slug", err)
        self.assertEqual(run(["-m", "campaign_tool.map_controller"], b"{not json").returncode, 1)

    def test_existing_content_caller_names_both_fields(self):
        with tempfile.TemporaryDirectory(prefix="synthetic-map-") as temporary:
            root = Path(temporary) / "campaign"
            shutil.copytree(EXAMPLE, root, ignore=shutil.ignore_patterns("public"))
            path = root / "content" / "map" / "map-config.json"
            document = json.loads(path.read_text())
            document.update(group_slug="Bad Slug", initial_point_count=-1)
            path.write_text(json.dumps(document))
            with self.assertRaises(ValueError) as caught:
                load_map(root / "content")
        self.assertIn("group_slug: invalid slug", str(caught.exception))
        self.assertIn("initial_point_count: out of range", str(caught.exception))
        self.assertNotIn("Bad Slug", str(caught.exception))


class ShellProblemsTests(unittest.TestCase):
    def test_valid_payload(self):
        self.assertEqual(shell.problems(payload()), [])

    def test_nested_paths_sorted(self):
        data = payload()
        del data["html"]["main"]
        data["styles"]["extra"] = "x"
        data["html"]["head"] = []
        self.assertEqual(shell.problems(data), ["html.head: must be a string", "missing field: html.main",
                                                "unknown field: styles.extra"])
        with self.assertRaises(ValueError) as caught:
            shell.render(data)
        self.assertEqual(str(caught.exception),
                         "html.head: must be a string; missing field: html.main; unknown field: styles.extra")

    def test_top_level_problems(self):
        data = {**payload(), "schema_version": True, "language": "<script>", "<odd>": 1}
        del data["styles"]
        found = shell.problems(data)
        self.assertEqual(found, ["language: invalid tag", "missing field: styles", "schema_version: unsupported",
                                 "unknown field: 5 chars"])
        self.assertFalse(any("<" in text for text in found))
        self.assertEqual(shell.problems([]), ["payload: must be an object"])

    def test_non_dict_html_still_checks_styles(self):
        data = payload()
        data["html"] = []
        data["styles"]["extra"] = "x"
        del data["styles"]["font_faces"]
        self.assertEqual(shell.problems(data), ["html: must be an object", "missing field: styles.font_faces",
                                                "unknown field: styles.extra"])

    def test_slots_over_max_input(self):
        data = payload()
        data["html"]["main"] = "x" * shell.MAX_INPUT
        data["styles"]["font_faces"] = "y"
        self.assertEqual(shell.problems(data), ["input: too large"])

    def test_cli_names_both_fields(self):
        data = payload()
        data["language"] = "1"
        del data["html"]["footer"]
        result = run(["-m", "campaign_tool.shell"], data)
        self.assertEqual((result.returncode, result.stdout), (1, b""))
        err = result.stderr.decode()
        self.assertTrue(err.startswith("Shell build stopped: "), err)
        self.assertIn("language: invalid tag; missing field: html.footer", err)
        self.assertEqual(run(["-m", "campaign_tool.shell"], b"[").returncode, 1)


class AgencyCardProblemsTests(unittest.TestCase):
    def test_valid_example_unchanged(self):
        self.assertEqual(agency_cards.problems(CARDS), [])
        self.assertEqual(hashlib.sha256(agency_cards.render(CARDS).encode()).hexdigest()[:16], AGENCY_PREFIX)

    def test_three_problems_named_without_values(self):
        index = len(CARDS["county_profile"]["facts"])
        expected = [f"county_profile.facts: item {index} not plain text", "missing field: county_profile.vendor",
                    "source_aliases: invalid slug pair (count 1)"]
        found = agency_cards.problems(bad_cards())
        self.assertEqual(found, expected)
        self.assertFalse(any("<b>" in text or "Bad" in text for text in found))
        with self.assertRaises(ValueError) as caught:
            agency_cards.render(bad_cards())
        self.assertEqual(str(caught.exception), "; ".join(expected))

    def test_other_fields_named(self):
        value = {**copy.deepcopy(CARDS), "schema_version": True, "source_page": "../x.html", "default_fragment": "X",
                 "source_link_label": "  ", "<odd>": 1}
        value["county_profile"]["name"] = "<i>"
        value["county_profile"]["flags"] = "not a list"
        value["county_profile"]["extra"] = 1
        self.assertEqual(agency_cards.problems(value), [
            "county_profile.flags: must be a list of at most 100 items", "county_profile.name: not plain text",
            "default_fragment: invalid slug", "schema_version: unsupported schema version",
            "source_link_label: blank or not plain text", "source_page: not a local .html file name",
            "unknown field: 5 chars", "unknown field: county_profile.extra"])
        self.assertEqual(agency_cards.problems({**CARDS, "county_profile": [], "source_aliases": []}),
                         ["county_profile: must be an object", "source_aliases: must be an object of at most 100 pairs"])
        self.assertEqual(agency_cards.problems(None), ["config: must be an object"])
        missing = {key: value for key, value in CARDS.items() if key not in ("county_profile", "source_aliases")}
        self.assertEqual(agency_cards.problems(missing), ["missing field: county_profile", "missing field: source_aliases"])

    def test_cli_names_all_three(self):
        result = subprocess.run([sys.executable, "-B", "campaign_tool/agency_cards.py"], input=json.dumps(bad_cards()).encode(),
                                capture_output=True, cwd=ROOT, timeout=60)
        self.assertEqual((result.returncode, result.stdout), (1, b""))
        err = result.stderr.decode()
        self.assertTrue(err.startswith("Agency-card build stopped: "), err)
        for field in ("county_profile.facts", "county_profile.vendor", "source_aliases"):
            self.assertIn(field, err)
        self.assertNotIn("<b>", err)


if __name__ == "__main__":
    unittest.main()
