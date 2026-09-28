import copy
import json
import unittest
from pathlib import Path
from campaign_tool.agency_cards import render

CONFIG = json.loads((Path(__file__).resolve().parents[1] / "examples/fictional-campaign/agency-cards.json").read_text())


class AgencyCardContractTests(unittest.TestCase):
    def test_deterministic_output(self):
        self.assertEqual(render(CONFIG), render(CONFIG))

    def test_rejects_unknown_fields(self):
        with self.assertRaises(ValueError):
            render({**CONFIG, "private": "not allowed"})

    def test_rejects_wrong_version(self):
        for value in (True, "1", 2):
            with self.assertRaises(ValueError):
                render({**CONFIG, "schema_version": value})

    def test_rejects_html_in_new_profile_copy(self):
        for field in CONFIG["county_profile"]:
            value = copy.deepcopy(CONFIG)
            value["county_profile"][field] = ["<img onerror=alert(1)>"] if field in ("facts", "flags") else "<script>"
            with self.subTest(field=field), self.assertRaises(ValueError):
                render(value)

    def test_rejects_external_or_injected_source_destinations(self):
        for key, value in [("source_page", "https://example.invalid"),
                           ("source_page", "../sources.html"),
                           ("source_aliases", {"x": 'x";alert(1)'}),
                           ("default_fragment", 'x" onclick="x')]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                render({**CONFIG, key: value})

    def test_escaped_copy_and_single_pass_tokens(self):
        value = copy.deepcopy(CONFIG)
        value["county_profile"]["name"] = 'A "quoted" county {{SOURCE_ROUTE}}'
        rendered = render(value)
        self.assertIn('A \\"quoted\\" county {{SOURCE_ROUTE}}', rendered)

    def test_no_pilot_content(self):
        for value in ("San Luis Obispo", "sheriff", "templeton", "deflockslo.com"):
            self.assertNotIn(value, render(CONFIG))
