"""Installed campaign resources remain byte-identical to reviewed repository inputs."""
import tempfile
import unittest
from pathlib import Path

from campaign_tool import discovery, law
from campaign_tool.cli import check_public_tree
from tools.sync_campaign_resources import RESOURCES, sync

ROOT = Path(__file__).resolve().parents[1]


class CampaignResourceTests(unittest.TestCase):
    def test_bundled_inputs_match_canonical_bytes(self):
        self.assertEqual(sync(ROOT), [])
        self.assertEqual(len(RESOURCES), 5)

    def test_runtime_paths_are_inside_package(self):
        package = ROOT / "campaign_tool"
        for resource in (discovery.SEED_SCHEMA, law.SCHEMA_PATH, law.TEMPLATE_PATH):
            self.assertTrue(resource.is_relative_to(package))
            self.assertTrue(resource.is_file())
        self.assertTrue(discovery.seed_path("us-ca").is_relative_to(package))
        self.assertTrue(law.package_path("us-ca").is_relative_to(package))

    def test_existing_unreviewed_status_is_not_promoted(self):
        package = law.load_package("us-ca")
        self.assertEqual(package["status"], "draft")
        self.assertEqual(package["reviewed_by"], [])
        seed = discovery.load_seed("us-ca")
        self.assertEqual(len(seed["counties"]), 58)

    def test_scanner_does_not_need_repository_tools_on_import_path(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "index.html").write_text("<p>Synthetic public page.</p>", encoding="utf-8")
            self.assertEqual(check_public_tree(root), [])
            (root / "private").mkdir()
            (root / "private" / "example.txt").write_text("Synthetic private fixture.", encoding="utf-8")
            self.assertTrue(check_public_tree(root))


if __name__ == "__main__":
    unittest.main()
