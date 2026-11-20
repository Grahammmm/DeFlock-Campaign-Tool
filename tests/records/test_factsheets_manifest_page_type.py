"""Fact-sheet citation membership must match an integer manifest page."""
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from campaign_tool.records.extraction_ledger import SCHEMA, canonical, sha
from campaign_tool.records.ledger.migrations import v001
from campaign_tool.records.outcomes import factsheets


class ManifestPageTypeTests(unittest.TestCase):
    def collect(self, page_no):
        text = "Retention is 7 days."
        locator = {"page": 1}
        payload = canonical({"kind": "pdf_page", "locator": locator, "text": text, "data": {}}).encode()
        digest = sha(payload)
        original = b"synthetic original"
        subject = sha(original)
        unit_id = sha(canonical(["fixture-import", 1, digest]).encode())
        manifest = canonical({"original_sha256": subject, "unit_ids": [unit_id],
                              "pages": [{"page_no": page_no}]})

        with tempfile.TemporaryDirectory() as root, closing(sqlite3.connect(":memory:")) as db:
            processing = Path(root).resolve()
            evidence = processing / "artifacts" / "evidence" / digest
            evidence.parent.mkdir(parents=True)
            evidence.write_bytes(payload)
            evidence.chmod(0o600)
            db.executescript("PRAGMA foreign_keys=ON;\n" + v001.SQL + SCHEMA)
            db.execute("INSERT INTO runs VALUES('fixture','fixture','2041-01-01',NULL,'fixture',NULL,NULL,NULL,'fixture','{}')")
            db.execute("INSERT INTO agencies VALUES('fixture','Fixture Agency',NULL,NULL)")
            db.execute("INSERT INTO originals VALUES(?,?,NULL,NULL,'agency_original','in_scope',NULL,'pending',NULL,'{}')",
                       (subject, len(original)))
            db.execute("INSERT INTO joins VALUES('fixture',?,'fixture',NULL,'synthetic','{}','typed',NULL)", (subject,))
            db.execute("INSERT INTO extraction_adapter_imports VALUES('fixture-import',?,?,?,?,'fixture','2041-01-01')",
                       (subject, sha(b"synthetic receipt"), sha(manifest.encode()), manifest))
            db.execute("INSERT INTO extraction_adapter_current VALUES(?,'fixture-import')", (subject,))
            db.execute("INSERT INTO units VALUES(?,?,'fixture','1',?,'pdf_page',?,?,'ok',1,?)",
                       (unit_id, subject, canonical(locator), sha(text.encode()), str(evidence),
                        canonical({"extraction_import_id": "fixture-import", "ordinal": 1, "payload_sha256": digest})))
            return factsheets.collect(db, processing, "fixture")

    def test_equal_noninteger_manifest_pages_are_refused(self):
        # Equality alone admits both JSON true and 1.0 for an integer page-1 locator.
        for page_no in (True, 1.0):
            with self.subTest(page_no=page_no):
                with self.assertRaisesRegex(ValueError, "^factsheet_page_not_in_manifest$"):
                    self.collect(page_no)

    def test_matching_integer_still_yields_review_required_citation(self):
        # Guard against refusing every manifest entry or admitting the citation as reviewed.
        report = self.collect(1)
        self.assertIs(report["review_required"], True)
        self.assertIs(report["semantic_acceptance"], False)
        self.assertIs(report["publication_ready"], False)
        field = report["agencies"][0]["fields"]["retention"]
        self.assertEqual(field["status"], "SOURCE STATEMENT — REVIEW REQUIRED")
        statement, = field["statements"]
        self.assertEqual(statement["quote"], "Retention is 7 days.")
        page = statement["citation"]["page"]
        self.assertIs(type(page), int)
        self.assertEqual(page, 1)
