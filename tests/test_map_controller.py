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

    def test_optional_context_default_preserves_accepted_bytes(self):
        import hashlib
        self.assertEqual(hashlib.sha256(render(CONFIG, "").encode()).hexdigest(), "598492ad72765421b09e77094b0772d97285073fd522fb0ace533441ae8aada2")  # pragma: allowlist secret -- SHA-256 of public generated controller, not a credential
        self.assertEqual(render(CONFIG, ""), render(CONFIG, "", ""))

    def test_context_is_a_bounded_trusted_single_pass_hook(self):
        hook = "function(p){return mapText(p.context||'{{GROUP_SLUG}}');}"
        self.assertIn("+("+hook+")(p)", render(CONFIG, "", hook))
        for invalid in (None, {}, "x" * (2 * 1024 * 1024 + 1)):
            with self.assertRaises(ValueError):
                render(CONFIG, "", invalid)

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
