"""Installed campaign resources remain byte-identical to reviewed repository inputs."""
import tempfile
import tomllib
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

    def test_packaging_declares_exact_resource_paths(self):
        config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        declared = config["tool"]["setuptools"]["package-data"]["campaign_tool"]
        resources = [name for name in declared if name.startswith("_resources/")]
        self.assertCountEqual(resources, ["_resources/" + name for name in RESOURCES])
        self.assertEqual(len(resources), 5)

    def make_synthetic_bundle(self, root):
        for name in RESOURCES:
            source = root / name
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(b"synthetic public fixture")
        self.assertEqual(sync(root, write=True), [])

    def test_unexpected_resources_block_check_and_write(self):
        for extra in ("data/agencies/extra.json", "templates/private.md", "unexpected.bin"):
            with self.subTest(extra=extra), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                self.make_synthetic_bundle(root)
                bundle = root / "campaign_tool" / "_resources"
                unknown = bundle / extra
                unknown.parent.mkdir(parents=True, exist_ok=True)
                unknown.write_bytes(b"synthetic unapproved fixture")
                (root / RESOURCES[0]).write_bytes(b"changed canonical fixture")
                self.assertIn("unexpected resource: " + extra, sync(root))
                self.assertIn("unexpected resource: " + extra, sync(root, write=True))
                self.assertEqual((bundle / RESOURCES[0]).read_bytes(), b"synthetic public fixture")
                self.assertEqual(unknown.read_bytes(), b"synthetic unapproved fixture")

    def test_unexpected_empty_directory_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.make_synthetic_bundle(root)
            (root / "campaign_tool" / "_resources" / "unapproved").mkdir()
            self.assertIn("unexpected resource: unapproved", sync(root))

    def test_unexpected_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.make_synthetic_bundle(root)
            (root / "campaign_tool" / "_resources" / "unapproved").symlink_to(root / RESOURCES[0])
            with self.assertRaisesRegex(ValueError, "symlink resource path"):
                sync(root, write=True)

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
