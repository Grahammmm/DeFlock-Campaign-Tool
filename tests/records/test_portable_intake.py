"""Portable intake safety checks using synthetic, temporary originals only."""
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[2]
MODULE = "campaign_tool.records.intake.folder"


class PortableIntakeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.source = self.base / "originals"
        self.source.mkdir()
        self.output = self.base / "out"

    def cli(self, *args):
        return subprocess.run([sys.executable, "-B", "-m", MODULE, *map(str, args)],
                              cwd=ROOT, capture_output=True, text=True, timeout=30)

    def config(self, **overrides):
        data = {"roots": [str(self.source)], "output": str(self.output)}
        data.update(overrides)
        path = self.base / "private-config.json"
        path.write_text(json.dumps(data))
        return path

    def connection(self):
        db = sqlite3.connect(self.output / "intake.sqlite")
        self.addCleanup(db.close)
        return db

    def test_inventory_requires_explicit_roots_before_output_creation(self):
        result = self.cli("inventory", "--output", self.output)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.output.exists())

    def test_output_is_required_without_a_config(self):
        result = self.cli("inventory", "--root", self.source)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.output.exists())

    def test_private_config_routes_agency_hints(self):
        (self.source / "synthetic-north-policy.txt").write_text("Synthetic public policy")
        config = self.config(agency_patterns={"synthetic-north": "synthetic-north"})
        result = self.cli("run", "--config", config)
        self.assertEqual(result.returncode, 0, result.stderr)
        queue = [json.loads(line) for line in (self.output / "all-agency-document-queue.jsonl").read_text().splitlines()]
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0]["agency_hints"], ["synthetic-north"])
        self.assertEqual(queue[0]["review_status"], "not_reviewed")

    def test_without_agency_overlay_routing_is_unassigned(self):
        (self.source / "policy.txt").write_text("Synthetic policy")
        result = self.cli("run", "--root", self.source, "--output", self.output)
        self.assertEqual(result.returncode, 0, result.stderr)
        queue = json.loads((self.output / "all-agency-document-queue.jsonl").read_text())
        self.assertEqual(queue["agency_hints"], ["UNASSIGNED"])

    def test_private_exclusions_reach_archive_worker(self):
        with zipfile.ZipFile(self.source / "production.zip", "w") as z:
            z.writestr("omit-this/note.txt", "Synthetic excluded content")
            z.writestr("keep.txt", "Synthetic retained content")
        config = self.config(excluded_path_fragments=["omit-this"])
        result = self.cli("run", "--config", config)
        self.assertEqual(result.returncode, 0, result.stderr)
        db = self.connection()
        self.assertEqual(db.execute("SELECT count(*) FROM docs WHERE format='txt'").fetchone()[0], 1)
        self.assertEqual(db.execute("SELECT name FROM edges").fetchone()[0], "keep.txt")

    def test_invalid_config_never_starts_intake(self):
        for overrides in ({"roots": "not-a-list"}, {"agency_patterns": {"test": "["}},
                          {"excluded_path_fragments": [7]}, {"unexpected": True}):
            with self.subTest(overrides=overrides):
                result = self.cli("run", "--config", self.config(**overrides))
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.output.exists())

    def test_report_does_not_require_roots_or_trigger_inventory(self):
        (self.source / "one.txt").write_text("Synthetic document")
        result = self.cli("run", "--root", self.source, "--output", self.output)
        self.assertEqual(result.returncode, 0, result.stderr)
        db = self.connection()
        before = db.execute("SELECT count(*) FROM runs").fetchone()[0]
        result = self.cli("report", "--output", self.output)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(db.execute("SELECT count(*) FROM runs").fetchone()[0], before)

    def test_valid_docx_has_part_and_paragraph_locators(self):
        xml = ('<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
               '<w:body><w:p><w:r><w:t>Synthetic policy text</w:t></w:r></w:p></w:body></w:document>')
        with zipfile.ZipFile(self.source / "policy.docx", "w") as z:
            z.writestr("[Content_Types].xml", "<Types/>")
            z.writestr("word/document.xml", xml)
        result = self.cli("run", "--root", self.source, "--output", self.output)
        self.assertEqual(result.returncode, 0, result.stderr)
        row = self.connection().execute("SELECT locator,text FROM units WHERE kind='docx_paragraph'").fetchone()
        self.assertEqual(json.loads(row[0]), {"part": "word/document.xml", "paragraph": 1})
        self.assertEqual(row[1], "Synthetic policy text")

    def test_docx_embedding_is_not_executed_or_silently_reviewed(self):
        with zipfile.ZipFile(self.source / "with-embedding.docx", "w") as z:
            z.writestr("[Content_Types].xml", "<Types/>")
            z.writestr("word/document.xml", "<document/>")
            z.writestr("word/embeddings/synthetic.bin", b"not executable")
        result = self.cli("run", "--root", self.source, "--output", self.output)
        self.assertEqual(result.returncode, 0, result.stderr)
        row = self.connection().execute("SELECT stage,digest,review_status FROM docs").fetchone()
        self.assertEqual((row[0], row[2]), ("partial", "not_reviewed"))
        self.assertIn("docx_media_or_embeddings_not_visually_reviewed", str(json.loads(row[1])["issues"]))


if __name__ == "__main__":
    unittest.main()
