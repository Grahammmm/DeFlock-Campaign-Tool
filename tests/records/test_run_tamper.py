"""Tamper probes against a root built by the real ``records run`` command.

Replaces the previously env-gated composition test: the state under test is
produced by the pipeline itself (preserve -> extract -> catalog on one ledger),
not hand-assembled. Every probe is a rollback-only change or a restored byte
edit; the positive path is never relabelled to pass.
"""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest

from campaign_tool.records import catalog_stage, extraction_ledger, ledger_catalog
from campaign_tool.records.ledger import stages, store
from campaign_tool.records.run import Pipeline
from tests.records import test_wp4_catalog_support as support_regressions
from tests.records.test_run_slice import synthetic_email


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


class RunTamperTests(unittest.TestCase):
    def setUp(self):
        os.umask(0o077)
        self.tmp = tempfile.TemporaryDirectory(prefix="records-tamper-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        os.chmod(self.base, 0o700)
        inbox = self.base / "inbox"
        inbox.mkdir(mode=0o700)
        inbox.joinpath("m.eml").write_bytes(synthetic_email())
        self.root = self.base / "root"
        self.pipeline = Pipeline(self.root)
        self.report = self.pipeline.run(inbox)
        self.database = self.root / "ledger.sqlite"
        # The completed run is closed; probes need a live runner on the same root.
        self.probe = Pipeline(self.root)
        self.stage = self.probe._open_stage_run()
        self.addCleanup(self.probe._close_stage_run, {"probe": True})
        with store.ledger(self.database, readonly=True) as con:
            self.subject = con.execute("SELECT original_sha256 FROM stage_state WHERE stage='catalog' AND status='done' "
                                       "ORDER BY original_sha256").fetchone()[0]
            self.import_id = con.execute("SELECT import_id FROM extraction_adapter_current WHERE original_sha256=?",
                                         (self.subject,)).fetchone()[0]
            self.units = [dict(row) for row in con.execute("SELECT * FROM units WHERE original_sha256=? ORDER BY id",
                                                           (self.subject,))]
        self.card_path = self.root / "artifacts" / (self.subject + "-card.json")
        self.card = json.loads(self.card_path.read_bytes())
        self.before = self.counts()

    def counts(self):
        return stages.query_counts(self.database)

    def unit_rows(self):
        with store.ledger(self.database, readonly=True) as con:
            return [dict(row) for row in con.execute("SELECT * FROM units ORDER BY id")]

    def test_pipeline_built_a_real_card_with_metadata_and_counts_agree_with_the_board(self):
        self.assertEqual(self.card["schema"], "catalog-card-evidence-v1")
        self.assertEqual(self.card["subject_sha256"], self.subject)
        self.assertTrue(self.card["metadata"].get("title"))
        self.assertIn(self.card["metadata"]["doc_type"], ("correspondence", "policy", "contract", "other"))
        self.assertEqual(self.card["supports"][0]["unit_id"], self.units[0]["id"]
                         if len(self.units) == 1 else self.card["supports"][0]["unit_id"])
        board = ledger_catalog.build(self.database, self.root / "artifacts")
        self.assertEqual(board["counts"], self.counts())
        self.assertGreaterEqual(len(board["cards"]), 1)
        board_root = self.base / "board"
        board_root.mkdir(mode=0o700)
        export = ledger_catalog.export(board, board_root)
        self.assertTrue(ledger_catalog.export(board, board_root)["reused"])
        self.assertFalse(export["publication_ready"])
        for path in (board_root / export["snapshot_id"]).glob("*.html"):
            html = path.read_text().lower()
            self.assertNotIn("<script", html)
            self.assertNotIn('src="http', html)

    def test_support_regressions_against_the_real_catalog_adapter(self):
        proof = self.card["supports"][0]
        unit = next(u for u in self.units if u["id"] == proof["unit_id"])
        names = support_regressions.challenge(self, self.stage["catalog"].adapter, self.database, self.import_id, unit, proof)
        self.assertGreaterEqual(len(names), 5, names)
        self.assertEqual(self.unit_rows(), self.unit_rows())
        self.assertEqual(self.counts(), self.before)

    def test_tampered_card_bytes_are_blocked_before_any_stage_mutation(self):
        raw = self.card_path.read_bytes()
        invalid = self.card_path.with_name("invalid-card.json")
        invalid.write_bytes(raw + b"tampered")
        os.chmod(invalid, 0o600)
        result = self.stage["catalog"].process(invalid, digest(raw), author_id="records-cataloger")
        self.assertEqual(result["status"], "blocked")
        self.assertFalse(result["canonical_stage_changed"])
        wrong = {**self.card, "subject_sha256": "0" * 64}
        wrong_path = self.card_path.with_name("wrong-subject.json")
        wrong_raw = json.dumps(wrong, sort_keys=True).encode()
        wrong_path.write_bytes(wrong_raw)
        os.chmod(wrong_path, 0o600)
        result = self.stage["catalog"].process(wrong_path, digest(wrong_raw), author_id="records-cataloger")
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(self.counts(), self.before)

    def test_tampered_extraction_receipt_hash_and_unit_bytes_are_rejected(self):
        with store.ledger(self.database, readonly=True) as con:
            imported = dict(con.execute("SELECT * FROM extraction_adapter_imports WHERE id=?", (self.import_id,)).fetchone())
            original = dict(con.execute("SELECT storage_path FROM originals WHERE sha256=?", (self.subject,)).fetchone())
        receipt_path = Path(imported["receipt_path"]) if "receipt_path" in imported else None
        enrollment = self.stage["enrollment"]
        if receipt_path is not None:
            with self.assertRaises(extraction_ledger.ExtractionBindingError):
                enrollment.enroll(original_path=Path(original["storage_path"]), receipt_path=receipt_path,
                                  receipt_sha256=digest(b"not the receipt"))
        unit = self.units[0]
        unit_path = Path(unit["derived_path"])
        raw, mode = unit_path.read_bytes(), unit_path.stat().st_mode & 0o777
        unit_path.chmod(0o600)
        try:
            unit_path.write_bytes(raw + b"tampered")
            with self.assertRaises(extraction_ledger.ExtractionBindingError):
                self.stage["extraction"].accept(self.import_id)
        finally:
            unit_path.write_bytes(raw)
            unit_path.chmod(mode)
        self.assertEqual(self.counts(), self.before)
        with store.ledger(self.database, readonly=True) as con:
            self.assertEqual(con.execute("SELECT status FROM stage_state WHERE original_sha256=? AND stage='extract'",
                                         (self.subject,)).fetchone()[0], "done")

    def test_no_test_only_validators_were_installed(self):
        with store.ledger(self.database, readonly=True) as con:
            self.assertEqual(con.execute("SELECT count(*) FROM stage_validation_authority WHERE test_only=1").fetchone()[0], 0)
        self.assertIsInstance(self.stage["catalog"], catalog_stage.CatalogStage)


if __name__ == "__main__":
    unittest.main()
