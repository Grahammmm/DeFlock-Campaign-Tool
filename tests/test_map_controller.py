import json
import unittest
from pathlib import Path
from campaign_tool.map_controller import render

CONFIG = json.loads((Path(__file__).resolve().parents[1] / "examples/fictional-campaign/map-config.json").read_text())


class MapContractTests(unittest.TestCase):
    def test_deterministic_controller(self):
        self.assertEqual(render(CONFIG, ""), render(CONFIG, ""))

    def test_single_pass_trusted_hook(self):
        self.assertIn("{{GROUP_SLUG}}", render(CONFIG, "// {{GROUP_SLUG}}\n"))

    def test_unknown_fields_rejected(self):
        with self.assertRaises(ValueError):
            render({**CONFIG, "private": "not allowed"}, "")

    def test_bad_urls_identifiers_and_counts_rejected(self):
        for key, value in [
            ("group_slug", 'x";alert(1)'),
            ("candidate_property", "__proto__"),
            ("bounding_box_fallbacks", ["x", "x"]),
            ("source_map_url", "javascript:alert(1)"),
            ("source_map_url", "https://user:password@example.invalid"),  # pragma: allowlist secret -- synthetic rejected URL
            ("city_boundaries_url", "../private/records.json"),
            ("city_boundaries_url", "https://example.invalid/data.json"),
            ("initial_point_count", True), ("initial_point_count", -1),
            ("schema_version", True)
        ]:
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                render({**CONFIG, key: value}, "")

    def test_no_pilot_values(self):
        code = render(CONFIG, "")
        for forbidden in ("San Luis Obispo", "slo-", "Sheriff", "sheriff", "paso-robles", "templeton"):
            self.assertNotIn(forbidden, code)
