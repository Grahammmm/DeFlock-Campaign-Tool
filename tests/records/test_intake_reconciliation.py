"""Synthetic regression coverage for PR10's reviewed intake fixes."""
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import zipfile

from campaign_tool.records.intake import folder


class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / "draft-host-name" / "originals"
        self.root.mkdir(parents=True)
        self.out = self.base / "out"
        self.config_path = self.base / "private.json"
        self.settings = {
            "roots": [str(self.root)], "output": str(self.out),
            "agency_patterns": {"synthetic-agency": "originals"},
            "excluded_path_fragments": [],
        }
        self.write_config()

    def write_config(self, **changes):
        self.settings.update(changes)
        self.config_path.write_text(json.dumps(self.settings))
        return self.config_path

    def cli(self, command="run", *args, module="folder"):
        result = subprocess.run(
            [sys.executable, "-B", "-m", "campaign_tool.records.intake." + module,
             *([command] if module == "folder" else []),
             "--config", str(self.config_path), *map(str, args)],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def db(self):
        db = sqlite3.connect(self.out / "intake.sqlite")
        db.row_factory = sqlite3.Row
        self.addCleanup(db.close)
        return db

    def queue(self):
        return [json.loads(line) for line in
                (self.out / "all-agency-document-queue.jsonl").read_text().splitlines()]

    def archive(self, name, members):
        with zipfile.ZipFile(self.root / name, "w") as z:
            for member, value in members.items():
                z.writestr(member, value)
        return hashlib.sha256((self.root / name).read_bytes()).hexdigest()

    def test_exclusions_apply_to_relative_paths_not_absolute_root(self):
        (self.root / "policy.txt").write_text("retained")
        (self.root / "draft").mkdir()
        (self.root / "draft" / "note.txt").write_text("excluded")
        self.write_config(excluded_path_fragments=["draft"])
        self.cli()
        self.assertEqual([row["sha256"] for row in self.queue()],
                         [hashlib.sha256(b"retained").hexdigest()])

    def test_changed_policy_retires_edges_preserves_provenance_and_is_idempotent(self):
        parent = self.archive("response.zip", {"omit/note.txt": "retire", "keep.txt": "keep"})
        self.cli()
        db = self.db()
        child = hashlib.sha256(b"retire").hexdigest()
        before = [tuple(row) for row in db.execute(
            "SELECT oid,sha,receipt,first_seen FROM occurrences ORDER BY oid,sha")]
        self.write_config(excluded_path_fragments=["omit"])
        result = self.cli("extract")
        self.assertEqual(json.loads(result.stdout.splitlines()[-1])["processed"], 1)
        self.assertNotIn(child, folder.active(db)[0])
        self.assertEqual(db.execute("SELECT count(*) FROM edge_history WHERE child=?", (child,)).fetchone()[0], 1)
        self.assertEqual(before, [tuple(row) for row in db.execute(
            "SELECT oid,sha,receipt,first_seen FROM occurrences ORDER BY oid,sha")])
        self.assertEqual((self.out / "blobs" / child).read_bytes(), b"retire")
        self.assertEqual(db.execute("SELECT text FROM units WHERE sha=?", (child,)).fetchone()[0], "retire")
        digest = json.loads(db.execute("SELECT digest FROM docs WHERE sha=?", (parent,)).fetchone()[0])
        self.assertEqual(digest["stage"], "partial")  # Exclusion is still disclosed.
        self.assertTrue(digest["children_inventory_complete"])
        self.assertEqual(json.loads(self.cli("extract").stdout.splitlines()[-1])["processed"], 0)
        attempts = db.execute("SELECT artifact_dir FROM extraction_attempts WHERE sha=?", (parent,)).fetchall()
        self.assertEqual(len(attempts), 2)
        self.assertTrue(all((Path(row[0]) / "digest.json").exists() for row in attempts))
        self.write_config(excluded_path_fragments=[])
        self.cli("extract")
        self.assertIn(child, folder.active(db)[0])
        self.assertEqual(db.execute("SELECT count(*) FROM extraction_attempts WHERE sha=?", (child,)).fetchone()[0], 1)

    def test_shared_child_survives_other_container_and_direct_original(self):
        first = self.archive("first.zip", {"omit/shared.txt": "shared", "omit/only.txt": "only"})
        second = self.archive("second.zip", {"retained.txt": "shared"})
        (self.root / "direct.txt").write_text("only")
        self.cli()
        db = self.db()
        self.write_config(excluded_path_fragments=["omit"])
        self.cli("extract")
        self.assertEqual(db.execute("SELECT count(*) FROM edges WHERE parent=?", (first,)).fetchone()[0], 0)
        self.assertEqual(db.execute("SELECT count(*) FROM edges WHERE parent=?", (second,)).fetchone()[0], 1)
        active, _ = folder.active(db)
        self.assertIn(hashlib.sha256(b"shared").hexdigest(), active)
        self.assertIn(hashlib.sha256(b"only").hexdigest(), active)
        self.assertEqual(db.execute("SELECT count(*) FROM edge_history").fetchone()[0], 2)

    def test_failed_retry_does_not_treat_missing_children_as_absent(self):
        parent = self.archive("response.zip", {"one.txt": "one", "two.txt": "two"})
        self.cli()
        db = self.db()
        edges = [tuple(row) for row in db.execute("SELECT * FROM edges")]
        self.cli("extract", "--sha", parent, "--retry", "--limits", '{"source_bytes":1}')
        self.assertEqual(edges, [tuple(row) for row in db.execute("SELECT * FROM edges")])
        digest = json.loads(db.execute("SELECT digest FROM docs WHERE sha=?", (parent,)).fetchone()[0])
        self.assertEqual(digest["stage"], "failed")
        self.assertFalse(digest["children_inventory_complete"])
        self.assertEqual(db.execute("SELECT count(*) FROM edge_history").fetchone()[0], 0)

    def test_partial_retry_preserves_unseen_children_but_retires_explicit_exclusions(self):
        parent = self.archive("response.zip", {"omit.txt": "omit", "keep.txt": "longer-content"})
        self.cli()
        db = self.db()
        self.write_config(excluded_path_fragments=["omit"])
        self.cli("extract", "--sha", parent, "--limits", '{"member_bytes":1}')
        self.assertEqual([row[0] for row in db.execute("SELECT name FROM edges")], ["keep.txt"])
        digest = json.loads(db.execute("SELECT digest FROM docs WHERE sha=?", (parent,)).fetchone()[0])
        self.assertEqual(digest["stage"], "partial")
        self.assertFalse(digest["children_inventory_complete"])
        self.assertEqual(db.execute("SELECT count(*) FROM edge_history").fetchone()[0], 1)

    def test_completed_inventory_removes_obsolete_link_and_retains_history(self):
        parent = self.archive("response.zip", {"keep.txt": "keep"})
        self.cli()
        db = self.db()
        child = hashlib.sha256(b"keep").hexdigest()
        db.execute("INSERT INTO edges VALUES(?,?,?,?)", (parent, '{"member_index":999}', child, "old.txt"))
        db.commit()
        self.cli("extract", "--sha", parent, "--retry")
        self.assertEqual(db.execute("SELECT count(*) FROM edges").fetchone()[0], 1)
        reason = db.execute("SELECT reason FROM edge_history").fetchone()[0]
        self.assertEqual(reason, "absent_from_completed_child_inventory")

    def test_routing_changes_do_not_reextract_finished_documents(self):
        self.archive("response.zip", {"keep.txt": "keep"})
        self.cli()
        self.write_config(agency_patterns={"rerouted": "originals"})
        result = self.cli("extract")
        self.assertEqual(json.loads(result.stdout.splitlines()[-1])["processed"], 0)
        self.assertTrue(all(row["agency_hints"] == ["rerouted"] for row in self.queue()))

    def test_legacy_text_retained_but_container_policy_and_docx_locators_refreshed(self):
        parent = self.archive("response.zip", {"keep.txt": "keep"})
        self.cli()
        db = self.db()
        for row in db.execute("SELECT sha,digest FROM docs").fetchall():
            digest = json.loads(row["digest"])
            digest.pop("extraction_policy_sha256")
            db.execute("UPDATE docs SET digest=? WHERE sha=?", (json.dumps(digest), row["sha"]))
        db.commit()
        result = self.cli("extract")
        self.assertEqual(json.loads(result.stdout.splitlines()[-1])["processed"], 1)
        self.assertEqual(db.execute("SELECT count(*) FROM extraction_attempts WHERE sha=?", (parent,)).fetchone()[0], 2)
        self.assertEqual(db.execute("SELECT count(*) FROM extraction_attempts WHERE sha!=?", (parent,)).fetchone()[0], 1)

    def test_docx_blank_and_table_paragraphs_keep_part_local_source_ordinals(self):
        ns = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
        body = ('<w:document ' + ns + '><w:body><w:p/>'
                '<w:p><w:r><w:t>Second</w:t></w:r></w:p>'
                '<w:tbl><w:tr><w:tc><w:p/><w:p><w:r><w:t>Fourth</w:t>'
                '</w:r></w:p></w:tc></w:tr></w:tbl></w:body></w:document>')
        header = ('<w:hdr ' + ns + '><w:p/><w:p><w:r><w:t>Header second</w:t>'
                  '</w:r></w:p></w:hdr>')
        self.archive("policy.docx", {"[Content_Types].xml": "<Types/>",
                                   "word/document.xml": body, "word/header1.xml": header})
        self.cli()
        db = self.db()
        locators = {row["text"]: json.loads(row["locator"])
                    for row in db.execute("SELECT text,locator FROM units WHERE kind='docx_paragraph'")}
        self.assertEqual(locators["Second"], {"part": "word/document.xml", "paragraph": 2})
        self.assertEqual(locators["Fourth"], {"part": "word/document.xml", "paragraph": 4})
        self.assertEqual(locators["Header second"], {"part": "word/header1.xml", "paragraph": 2})
        row = db.execute("SELECT sha,digest FROM docs").fetchone()
        digest = json.loads(row["digest"])
        digest.pop("extraction_policy_sha256")
        db.execute("UPDATE docs SET digest=? WHERE sha=?", (json.dumps(digest), row["sha"]))
        db.commit()
        self.assertEqual(json.loads(self.cli("extract").stdout.splitlines()[-1])["processed"], 1)

    def test_helpers_keep_agency_config_and_legacy_output_cli(self):
        (self.root / "keep.txt").write_text("retained")
        personal = self.root / "personal.txt"
        personal.write_text("synthetic personal placeholder")
        self.cli()
        self.cli(module="repair_intake")
        self.assertTrue(all(row["agency_hints"] == ["synthetic-agency"] for row in self.queue()))
        self.cli(None, personal, module="scope_quarantine")
        self.assertEqual(len(self.queue()), 1)
        self.assertEqual(self.queue()[0]["agency_hints"], ["synthetic-agency"])
        self.assertEqual(personal.read_text(), "synthetic personal placeholder")

    def test_invalid_helper_config_rejected_before_lock_or_database_creation(self):
        self.write_config(agency_patterns={"bad": "["})
        for module in ("repair_intake", "scope_quarantine"):
            args = ["unused.txt"] if module == "scope_quarantine" else []
            result = subprocess.run(
                [sys.executable, "-B", "-m", "campaign_tool.records.intake." + module,
                 "--config", str(self.config_path), *args],
                capture_output=True, text=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(self.out.exists())


if __name__ == "__main__":
    unittest.main()
