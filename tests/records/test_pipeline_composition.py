"""Opt-in, pinned WP1/WP2/WP4/WP5 composition with synthetic mail only.

Set RECORDS_COMPOSITION_MANIFEST to an owner-private JSON manifest containing
``roots`` (wp1/wp2/wp4/wp5/pr19 -> absolute installed root), and ``pins``
(same labels -> relative Python path -> SHA-256). Set
RECORDS_COMPOSITION_OUTPUT to a new owner-private directory outside all Git
repositories. Run this module alone in a fresh Python process: installed domain
validator bindings are deliberately immutable. Missing configuration is skipped,
not acceptance. A rejected catalog handoff is a failing acceptance test, even
when preservation, extraction, fail-closed tamper checks and board export pass.

No test-only stage validators, source-unit edits, services or network calls.
The imported exporter fixture runs only its hash-pinned synthetic local script.
"""
from datetime import datetime, timezone
from email.message import EmailMessage
from email.policy import SMTP
import hashlib
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def encoded(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=True) + "\n").encode()


def private_directory(path):
    path = Path(path)
    if not path.is_absolute() or path.resolve() != path:
        raise ValueError("canonical_private_directory_required")
    if any((parent / ".git").exists() for parent in (path, *path.parents)):
        raise ValueError("private_output_inside_repository")
    info = path.stat()
    if not path.is_dir() or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ValueError("owner_private_directory_required")
    return path


class PipelineCompositionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        manifest_name = os.environ.get("RECORDS_COMPOSITION_MANIFEST")
        output_name = os.environ.get("RECORDS_COMPOSITION_OUTPUT")
        if not manifest_name or not output_name:
            raise unittest.SkipTest("explicit pinned overlays and private output required")
        manifest_path = Path(manifest_name)
        info = manifest_path.stat()
        if info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ValueError("owner_private_manifest_required")
        cls.manifest_raw = manifest_path.read_bytes()
        cls.manifest = json.loads(cls.manifest_raw)
        cls.roots = {key: Path(value) for key, value in cls.manifest["roots"].items()}
        if set(cls.roots) != {"wp1", "wp2", "wp4", "wp5", "pr19"}:
            raise ValueError("five_explicit_overlay_roots_required")
        for label, root in cls.roots.items():
            if not root.is_absolute() or root.resolve() != root:
                raise ValueError("canonical_overlay_root_required")
            files = sorted(root.joinpath("campaign_tool/records").rglob("*.py"))
            files += sorted(root.joinpath("tests/records").rglob("*.py"))
            expected = {str(path.relative_to(root)): digest(path.read_bytes()) for path in files}
            if expected != cls.manifest["pins"][label]:
                raise ValueError("overlay_pin_mismatch:" + label)
        cls.output = private_directory(output_name)
        # Keep runtime/parser temp files outside repositories as well.
        os.environ["TMPDIR"] = str(cls.output)
        tempfile.tempdir = str(cls.output)
        import campaign_tool.records
        campaign_tool.records.__path__[:] = [str(cls.roots[key] / "campaign_tool/records")
                                            for key in ("wp5", "wp2", "wp4", "wp1", "pr19")]
        import campaign_tool.records.intake
        campaign_tool.records.intake.__path__.insert(0, str(cls.roots["wp2"] / "campaign_tool/records/intake"))
        import tests.records
        tests.records.__path__.insert(0, str(cls.roots["wp2"] / "tests/records"))
        bindings = {
            "store": ("wp1", "campaign_tool.records.ledger.store"),
            "stages": ("wp1", "campaign_tool.records.ledger.stages"),
            "mail": ("wp2", "campaign_tool.records.runner.canonical_mail"),
            "fixture": ("wp2", "tests.records.test_export_proof"),
            "enrollment": ("wp4", "campaign_tool.records.extraction_ledger"),
            "validation": ("wp4", "campaign_tool.records.extraction_validation"),
            "catalog_stage": ("wp5", "campaign_tool.records.catalog_stage"),
            "board": ("wp5", "campaign_tool.records.ledger_catalog"),
            "links": ("pr19", "campaign_tool.records.catalog_links"),
        }
        for attribute, (label, module_name) in bindings.items():
            module = importlib.import_module(module_name)
            if not Path(module.__file__).resolve().is_relative_to(cls.roots[label]):
                raise ValueError("wrong_installed_module:" + module_name)
            setattr(cls, attribute, module)
        case = cls("test_preservation_extraction_and_private_board")
        cls.addClassCleanup(case.doCleanups)
        case.compose()
        cls.result = case.report

    def write(self, path, raw):
        path.write_bytes(raw)
        path.chmod(0o600)
        return path

    def new_run(self, name):
        run_id = "composition-" + name
        config = digest(encoded({"stage": name, "pins": self.manifest["pins"]}))
        with self.store.ledger(self.database) as connection:
            connection.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?)", (
                run_id, "stage-runner", self.store.now(), None, "synthetic-composition-v1",
                None, config, None, "running", "{}"))
            connection.commit()
        return run_id, config

    def unit_rows(self):
        with self.store.ledger(self.database, readonly=True) as connection:
            return [dict(row) for row in connection.execute("SELECT * FROM units ORDER BY id")]

    def compose(self):
        sample = self.fixture.ExportProofTests()
        sample.setUp()
        self.addCleanup(sample.doCleanups)
        text = b"Synthetic public-record fixture.\n\nNo agency or date attribution asserted.\n"
        message = EmailMessage()
        message["From"] = "synthetic@example.invalid"
        message["To"] = "records@example.invalid"
        message["Subject"] = "Synthetic composition fixture"
        message.set_content("Synthetic transmittal; not substantive review.")
        message.add_attachment(text, maintype="text", subtype="plain", filename="synthetic.txt")
        eml = message.as_bytes(policy=SMTP)
        receipt = sample.fixture.delta.receipt
        self.write(sample.root / "mail/message.eml", eml)
        self.write(sample.root / "mail/attachment.txt", text)
        receipt["bytes"] = len(eml)
        receipt["original_eml"] = {"path": "mail/message.eml", "bytes": len(eml), "sha256": digest(eml)}
        receipt["attachments"] = [{"part": "2", "path": "mail/attachment.txt", "bytes": len(text),
            "sha256": digest(text), "filename": "synthetic.txt", "original_filename": "synthetic.txt",
            "content_type": "text/plain"}]
        self.write(sample.root / "seed.json", encoded(receipt))
        database_root = sample.fixture.delta.base / "canonical"
        database_root.mkdir(mode=0o700)
        self.database = database_root / "ledger.sqlite"
        self.store.initialize(self.database)
        backend = self.mail.CanonicalMailBackend(sample.root, sample.fixture.delta.out, self.database)
        wrapper = sample.wrapper()
        preserved = sample.fixture.invoke(provider=wrapper, backend=backend, hooks={})
        self.assertEqual(preserved["messages_preserved"], 1)
        export_proof = json.loads((wrapper.destination / "manifest.json").read_bytes())
        self.assertTrue(export_proof["coverage_verified"])
        for name, identity in export_proof["entries"].items():
            raw = (wrapper.destination / name).read_bytes()
            self.assertEqual(digest(raw), identity["sha256"])
            self.assertEqual(len(raw), identity["bytes"])
        initial = self.stages.query_counts(self.database)
        self.assertEqual(initial["originals"], 2)
        self.assertEqual(initial["stages"]["preserve"]["done"], 2)
        self.assertEqual(initial["stages"]["catalog"]["done"], 0)
        with self.store.ledger(self.database, readonly=True) as connection:
            occurrence_count = connection.execute("SELECT count(*) FROM occurrences").fetchone()[0]
            originals = {row["sha256"]: dict(row) for row in connection.execute("SELECT * FROM originals")}
            self.assertEqual(connection.execute("SELECT count(*) FROM stage_validation_authority WHERE test_only=1").fetchone()[0], 0)
        duplicate = sample.fixture.invoke(provider=sample.wrapper(), backend=backend, hooks={})
        self.assertEqual(duplicate["messages_preserved"], 0)
        self.assertEqual(self.stages.query_counts(self.database)["originals"], 2)
        with self.store.ledger(self.database, readonly=True) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM occurrences").fetchone()[0], occurrence_count)
        artifacts = database_root / "artifacts"
        artifacts.mkdir(mode=0o700)
        extraction_root = artifacts / "wrapper"
        extraction_root.mkdir(mode=0o700)
        evidence = artifacts / "evidence"
        evidence.mkdir(mode=0o700)
        original_path = Path(originals[digest(text)]["storage_path"])
        # Execute the real wrapper from its installed root so its parser child
        # resolves the same pinned WP4 code; no parser output is fabricated.
        command = [sys.executable, "-B", "-c", (
            "import json,sys;from campaign_tool.records.extraction_routes import extract;"
            "print(json.dumps(extract(sys.argv[1],sys.argv[2],sys.argv[3],form='txt',timeout=20)))"
        ), str(original_path), digest(text), str(extraction_root)]
        parsed = subprocess.run(command, cwd=self.roots["wp4"], env=dict(os.environ),
                                capture_output=True, text=True, timeout=35, check=True)
        extraction = json.loads(parsed.stdout)
        self.assertEqual(extraction["status"], "complete")
        extraction_receipt = Path(extraction["run_path"]) / "extraction.json"
        enrollment = self.enrollment.ExtractionLedgerAdapter(database=self.database, evidence_root=evidence)
        arguments = {"original_path": original_path, "receipt_path": extraction_receipt,
                     "receipt_sha256": extraction["receipt_sha256"]}
        imported = enrollment.enroll(**arguments)
        units_before = self.unit_rows()
        self.assertEqual(imported["units"], 3)
        self.assertTrue(enrollment.enroll(**arguments)["reused"])
        self.assertEqual(self.unit_rows(), units_before)
        extract_run, extract_config = self.new_run("extract")
        adapter_id = self.validation.install_extraction_validator(database=self.database,
            evidence_root=evidence, original_root=original_path.parent)
        self.stages.configure_installed_profile("composition-extract", engine_version="synthetic-composition-v1",
            config_sha256=extract_config, validators={"extract": adapter_id})
        extractor = self.validation.ExtractionStageAdapter(database=self.database, evidence_root=evidence,
            original_root=original_path.parent, run_id=extract_run, owner="synthetic-extractor",
            profile_id="composition-extract")
        # A real canonical unit is not usable before installed acceptance.
        provisional = self.catalog_stage.CatalogAdapter(self.database, artifacts)
        sample_unit = next(row for row in units_before if json.loads(Path(row["derived_path"]).read_bytes())["text"])
        sample_raw = Path(sample_unit["derived_path"]).read_bytes()
        sample_proof = {"unit_id": sample_unit["id"], "source_sha256": digest(text),
            "locator": sample_unit["locator"], "artifact_sha256": digest(sample_raw),
            "quote": json.loads(sample_raw)["text"]}
        with self.store.ledger(self.database, readonly=True) as con:
            with self.assertRaisesRegex(self.catalog_stage.CatalogStageError, "wp4_accepted_extraction_required"):
                provisional._unit(con, sample_proof)
        accepted = extractor.accept(imported["import_id"])
        counts_before_replay = self.stages.query_counts(self.database)
        replayed = extractor.accept(imported["import_id"])
        self.assertEqual(self.stages.query_counts(self.database), counts_before_replay)
        self.assertEqual(self.unit_rows(), units_before)
        self.assertEqual(counts_before_replay["stages"]["extract"]["done"], 1)
        with self.store.ledger(self.database, readonly=True) as connection:
            self.assertEqual(connection.execute("SELECT status FROM stage_state WHERE original_sha256=? AND stage='extract'",
                                               (digest(eml),)).fetchone()[0], "pending")
        unit = next(row for row in units_before if json.loads(Path(row["derived_path"]).read_bytes())["text"])
        unit_raw = Path(unit["derived_path"]).read_bytes()
        payload = json.loads(unit_raw)
        card = {"schema": "catalog-card-evidence-v1", "subject_sha256": digest(text),
            "metadata": {}, "field_support": {}, "supports": [{"unit_id": unit["id"],
                "source_sha256": digest(text), "locator": unit["locator"],
                "artifact_sha256": digest(unit_raw), "quote": payload["text"]}]}
        card_path = self.write(artifacts / "catalog.json", encoded(card))
        catalog_run, catalog_config = self.new_run("catalog")
        catalog = self.catalog_stage.catalog_run_factory(self.database, artifacts,
            run_id=catalog_run, owner="synthetic-cataloger", profile_id="composition-catalog",
            engine_version="synthetic-composition-v1", config_sha256=catalog_config)
        # Negative fixture edits are confined to rollback-only synthetic SQL
        # transactions. No unit/status rewrite makes the positive path pass.
        regression_names = ["missing_acceptance"]
        from . import test_wp4_catalog_support as support_regressions
        regression_names += support_regressions.challenge(self, catalog.adapter, self.database,
                                                          imported["import_id"], unit, card["supports"][0])
        self.assertEqual(self.unit_rows(), units_before)
        catalog_result = catalog.process(card_path, digest(card_path.read_bytes()), author_id="synthetic-author")
        catalog_counts = self.stages.query_counts(self.database)
        catalog_replay = catalog.process(card_path, digest(card_path.read_bytes()), author_id="synthetic-author")
        self.assertEqual(self.stages.query_counts(self.database), catalog_counts)
        self.assertEqual(self.unit_rows(), units_before)
        promoted = catalog_counts["stages"]["catalog"]["done"] == 1
        if not promoted:
            self.assertEqual(catalog_result["status"], "blocked")
            self.assertFalse(catalog_result["canonical_stage_changed"])
            self.assertEqual(catalog_counts["stages"]["catalog"]["done"], 0)
        for stage in ("detect", "review", "compare", "privacy"):
            self.assertEqual(catalog_counts["stages"][stage]["done"], 0)
            self.assertEqual(catalog_counts["stages"][stage]["pending"], 2)
        board = self.board.build(self.database, artifacts)
        self.assertEqual(board["counts"], self.stages.query_counts(self.database))
        self.assertEqual(len(board["cards"]), 2)
        for item in board["cards"]:
            self.assertEqual(item["agency_status"], "unknown")
            self.assertIsNone(item["date_from"])
            self.assertFalse(item["publication_ready"])
        board_root = self.output / "board"
        board_root.mkdir(mode=0o700)
        board_export = self.board.export(board, board_root)
        self.assertTrue(self.board.export(board, board_root)["reused"])
        self.assertFalse(board_export["accepted"])
        self.assertFalse(board_export["publication_ready"])
        board_files = {str(path.relative_to(board_root)): digest(path.read_bytes())
                       for path in sorted((board_root / board_export["snapshot_id"]).iterdir())}
        for path in (board_root / board_export["snapshot_id"]).glob("*.html"):
            html = path.read_text().lower()
            self.assertNotIn("<script", html)
            self.assertNotIn('src="http', html)
        # Malformed exact bytes must fail before any additional stage mutation.
        invalid = self.write(artifacts / "invalid-card.json", encoded(card) + b"tampered")
        blocked_card = catalog.process(invalid, digest(encoded(card)), author_id="synthetic-author")
        self.assertEqual(blocked_card["status"], "blocked")
        self.assertFalse(blocked_card["canonical_stage_changed"])
        with self.assertRaises(self.enrollment.ExtractionBindingError):
            enrollment.enroll(original_path=original_path, receipt_path=extraction_receipt,
                              receipt_sha256=digest(b"not the receipt"))
        unit_path = Path(unit["derived_path"])
        mode = unit_path.stat().st_mode & 0o777
        unit_path.chmod(0o600)
        try:
            unit_path.write_bytes(unit_raw + b"tampered")
            with self.assertRaises(self.enrollment.ExtractionBindingError):
                extractor.accept(imported["import_id"])
        finally:
            unit_path.write_bytes(unit_raw)
            unit_path.chmod(mode)
        self.assertEqual(self.stages.query_counts(self.database), catalog_counts)
        self.assertEqual(self.unit_rows(), units_before)
        with self.store.ledger(self.database, readonly=True) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM stage_validation_authority WHERE test_only=1").fetchone()[0], 0)
        self.report = {"schema": "synthetic-pipeline-composition-v1", "created_at": datetime.now(timezone.utc).isoformat(),
            "status": "bounded_txt_composition_passed" if promoted else "partial_blocked",
            "pipeline_complete": False, "bounded_txt_composition_complete": promoted,
            "real_mailbox_checked": False, "synthetic_only": True,
            "overlay_manifest_sha256": digest(self.manifest_raw), "pins": self.manifest["pins"],
            "export_proof_sha256": preserved["export_proof_sha256"], "coverage": "synthetic export only",
            "originals": {"eml": digest(eml), "txt": digest(text)}, "occurrences": occurrence_count,
            "stages": catalog_counts["stages"], "runs": {"extract": extract_run, "catalog": catalog_run},
            "extraction": {"import_id": imported["import_id"], "receipt_sha256": extraction["receipt_sha256"],
                           "acceptance": accepted, "replay": replayed, "units": len(units_before)},
            "catalog": catalog_result, "catalog_replay": catalog_replay,
            "support_regressions": regression_names,
            "actual_unit_contract": {"status": unit["status"], "type": unit["unit_type"],
                                     "payload_fields": sorted(payload), "locator": unit["locator"]},
            "duplicate_preservation_idempotent": True, "enrollment_replay_idempotent": True,
            "extraction_replay_idempotent": True, "catalog_replay_idempotent": True,
            "tamper_rejected": ["catalog_input_hash", "extraction_receipt_hash", "accepted_unit_cas_bytes"],
            "board": board_export, "board_files": board_files,
            "eml_extraction": "pending: installed validator does not accept EML",
            "remaining": [] if promoted else ["WP5 needs an authorized adapter for actual WP4 accepted text_line JSONL units; do not relabel canonical units."],
            "private_test_database": "temporary synthetic fixture; removed after tests", "schedules_changed": False,
            "models_called": False, "published": False}
        # Reject a mixed-source receipt if another author changed a dependency
        # while the installed-validator composition was running.
        for label, root in self.roots.items():
            files = sorted((root / "campaign_tool/records").rglob("*.py"))
            files += sorted((root / "tests/records").rglob("*.py"))
            observed = {str(path.relative_to(root)): digest(path.read_bytes()) for path in files}
            self.assertEqual(observed, self.manifest["pins"][label], "dependency changed during composition: " + label)
        self.write(self.output / "integration-receipt.json", encoded(self.report))

    def test_preservation_extraction_and_private_board(self):
        self.assertEqual(self.result["stages"]["preserve"]["done"], 2)
        self.assertEqual(self.result["stages"]["extract"]["done"], 1)
        self.assertEqual(self.result["board"]["cards"], 2)

    def test_replay_and_tamper_guards(self):
        self.assertTrue(self.result["duplicate_preservation_idempotent"])
        self.assertEqual(len(self.result["tamper_rejected"]), 3)
        self.assertFalse(self.result["published"])

    def test_strict_wp4_support_regressions(self):
        self.assertEqual(set(self.result["support_regressions"]), {
            "missing_acceptance", "stale_current_import", "changed_line", "extra_units",
            "blank_locator", "changed_ordinal", "uninstalled_validator", "changed_parser"})

    def test_catalog_handoff_acceptance(self):
        self.assertEqual(self.result["stages"]["catalog"]["done"], 1,
                         "Actual WP4-to-WP5 handoff blocked: " + json.dumps(self.result["catalog"]))


if __name__ == "__main__":
    unittest.main()
