"""Synthetic checks for effective-budget invalidation and preserved provenance."""
import hashlib
import io
import json
from pathlib import Path
import unittest
import zipfile
from campaign_tool.records.intake import folder
from tests.records import test_intake_reconciliation as support


def archive_bytes(name, contents):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr(name, contents)
    return stream.getvalue()


class ExtractionBudgetTests(unittest.TestCase):
    def setUp(self):
        self.fixture = support.ReconciliationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.f = self.fixture

    def test_nested_container_arriving_directly_preserves_newly_reachable_leaf(self):
        leaf = b"synthetic formerly unreachable document"
        inner = archive_bytes("leaf.txt", leaf)
        nested = inner
        for level in range(5):
            nested = archive_bytes("level.zip", nested)
        (self.f.root / "outer.zip").write_bytes(nested)
        self.f.cli()
        db = self.f.db()
        sha = hashlib.sha256(inner).hexdigest()
        leaf_sha = hashlib.sha256(leaf).hexdigest()
        before = json.loads(db.execute("SELECT digest FROM docs WHERE sha=?", (sha,)).fetchone()[0])
        self.assertIn("nesting_limit", [issue["code"] for issue in before["issues"]])
        self.assertFalse((self.f.out / "blobs" / leaf_sha).exists())
        (self.f.root / "direct.zip").write_bytes(inner)
        self.f.cli()
        self.assertEqual((self.f.out / "blobs" / leaf_sha).read_bytes(), leaf)
        self.assertEqual(db.execute("SELECT count(*) FROM edges WHERE parent=? AND child=?", (sha, leaf_sha)).fetchone()[0], 1)
        attempts = db.execute("SELECT digest,artifact_dir FROM extraction_attempts WHERE sha=? ORDER BY rowid", (sha,)).fetchall()
        self.assertEqual(len(attempts), 2)
        self.assertEqual([json.loads(a[0])["invocation_depth"] for a in attempts], [5, 0])
        self.assertTrue(all((Path(a[1]) / "digest.json").exists() for a in attempts))
        self.assertEqual(json.loads(self.f.cli("extract").stdout.splitlines()[-1])["processed"], 0)

    def test_increased_member_limit_retries_without_explicit_retry(self):
        sha = self.f.archive("response.zip", {"leaf.txt": "synthetic leaf"})
        self.f.cli("run", "--limits", '{"member_bytes":1}')
        leaf_sha = hashlib.sha256(b"synthetic leaf").hexdigest()
        self.assertFalse((self.f.out / "blobs" / leaf_sha).exists())
        self.f.cli("extract", "--sha", sha)
        self.assertTrue((self.f.out / "blobs" / leaf_sha).exists())
        self.assertEqual(json.loads(self.f.cli("extract", "--sha", sha).stdout.splitlines()[-1])["processed"], 0)

    def test_equivalent_remaining_depth_has_same_budget(self):
        first = folder.extraction_budget("zip", {**folder.LIMITS, "depth": 5}, 2)
        second = folder.extraction_budget("zip", {**folder.LIMITS, "depth": 7}, 4)
        self.assertEqual(first, second)
        self.assertNotEqual(first, folder.extraction_budget("zip", folder.LIMITS, 0))
        self.assertEqual(folder.extraction_budget("txt", folder.LIMITS, 0),
                         folder.extraction_budget("txt", {**folder.LIMITS, "depth": 20}, 10))

    def test_legacy_container_refreshes_but_recorded_text_budget_is_reused(self):
        sha = self.f.archive("response.zip", {"leaf.txt": "synthetic leaf"})
        self.f.cli()
        db = self.f.db()
        for row in db.execute("SELECT sha,digest FROM docs").fetchall():
            digest = json.loads(row["digest"])
            for key in ("extraction_budget", "extraction_budget_sha256", "invocation_depth"):
                digest.pop(key, None)
            db.execute("UPDATE docs SET digest=? WHERE sha=?", (json.dumps(digest), row["sha"]))
        db.commit()
        self.assertEqual(json.loads(self.f.cli("extract").stdout.splitlines()[-1])["processed"], 1)
        self.assertEqual(db.execute("SELECT count(*) FROM extraction_attempts WHERE sha=?", (sha,)).fetchone()[0], 2)
        self.assertEqual(json.loads(self.f.cli("extract").stdout.splitlines()[-1])["processed"], 0)

    def test_legacy_unknown_text_budget_refreshes_once(self):
        (self.f.root / "leaf.txt").write_text("synthetic text")
        self.f.cli()
        db = self.f.db()
        row = db.execute("SELECT sha,digest FROM docs").fetchone()
        digest = json.loads(row["digest"])
        for key in ("extraction_budget", "extraction_budget_sha256", "effective_limits"):
            digest.pop(key, None)
        db.execute("UPDATE docs SET digest=? WHERE sha=?", (json.dumps(digest), row["sha"]))
        db.commit()
        self.assertEqual(json.loads(self.f.cli("extract").stdout.splitlines()[-1])["processed"], 1)
        self.assertEqual(json.loads(self.f.cli("extract").stdout.splitlines()[-1])["processed"], 0)

    def test_failed_attempt_records_budget_without_erasing_unseen_edges(self):
        sha = self.f.archive("response.zip", {"leaf.txt": "synthetic leaf"})
        self.f.cli()
        db = self.f.db()
        before = [tuple(r) for r in db.execute("SELECT * FROM edges")]
        self.f.cli("extract", "--sha", sha, "--limits", '{"source_bytes":1}')
        self.assertEqual(before, [tuple(r) for r in db.execute("SELECT * FROM edges")])
        digest = json.loads(db.execute("SELECT digest FROM docs WHERE sha=?", (sha,)).fetchone()[0])
        self.assertEqual(digest["stage"], "failed")
        self.assertEqual(digest["extraction_budget"]["limits"]["source_bytes"], 1)
        self.assertEqual(digest["extraction_budget_sha256"], folder.hid(folder.js(digest["extraction_budget"])))
        self.assertEqual(digest, json.loads(db.execute("SELECT digest FROM extraction_attempts WHERE sha=? ORDER BY rowid DESC LIMIT 1", (sha,)).fetchone()[0]))


if __name__ == "__main__":
    unittest.main()
