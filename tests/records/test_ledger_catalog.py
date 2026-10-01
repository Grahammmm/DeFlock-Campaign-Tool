"""Synthetic WP5 tests. Dependency overlays are explicit, never source copies."""
import hashlib
import html
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

import campaign_tool.records
for root in json.loads(os.environ.get("RECORDS_TEST_DEPENDENCIES", "[]")):
    campaign_tool.records.__path__.append(str(Path(root) / "campaign_tool" / "records"))
from campaign_tool.records import ledger_catalog as module
try:
    from campaign_tool.records.ledger import store
    from campaign_tool.records import catalog_links
except ImportError:
    store = catalog_links = None
H1 = hashlib.sha256(b"synthetic original one").hexdigest()
H2 = hashlib.sha256(b"synthetic original two").hexdigest()


class OverlayTests(unittest.TestCase):
    def test_empty_unknown_allowed(self):
        self.assertEqual(module.validate_overlay({}, {H1}), {})

    def test_candidate_evidence_required(self):
        with self.assertRaisesRegex(module.CatalogError, "draft_evidence_required"):
            module.validate_overlay({H1: {"doc_type": "policy"}}, {H1})

    def test_dates_and_ranges(self):
        for draft in ({"date_from": "2025-02-30"}, {"date_from": "2026-10-02", "date_to": "2026-09-02"}):
            with self.assertRaises(module.CatalogError):
                module.validate_overlay({H1: draft}, {H1})

    def test_score_boolean_and_unexplained_rejected(self):
        for score in (True, -1, 101, 1.5, "40"):
            with self.assertRaises(module.CatalogError):
                module.validate_overlay({H1: {"value_score": score, "value_reason": "Synthetic rule"}}, {H1})
        with self.assertRaisesRegex(module.CatalogError, "score_reason_required"):
            module.validate_overlay({H1: {"value_score": 40}}, {H1})

    def test_unknown_hash_and_field(self):
        for value in ({H2: {}}, {H1: {"publication_ready": True}}):
            with self.assertRaises(module.CatalogError):
                module.validate_overlay(value, {H1})

    def test_party_limits(self):
        with self.assertRaisesRegex(module.CatalogError, "invalid_parties"):
            module.validate_overlay({H1: {"parties": ["Synthetic"] * 101}}, {H1})

    def test_evidence_unknown_original(self):
        with self.assertRaisesRegex(module.CatalogError, "invalid_evidence"):
            module.validate_overlay({H1: {"evidence": [{"source_sha256": H2, "locator": "page:1"}]}}, {H1})

    def test_unknown_type(self):
        with self.assertRaisesRegex(module.CatalogError, "invalid_doc_type"):
            module.validate_overlay({H1: {"doc_type": "guessed"}}, {H1})


@unittest.skipUnless(store is not None and catalog_links is not None, "WP1/PR19 dependency overlays required")
class CanonicalCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="wp5-synthetic-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.root.chmod(0o700)
        self.db = self.root / "ledger.sqlite"
        store.initialize(self.db)
        with sqlite3.connect(self.db) as con:
            con.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?)", ("synthetic", "test", "2026-01-01T00:00:00+00:00", None, "synthetic", None, None, None, "running", "{}"))
            for index, sha in enumerate((H1, H2)):
                con.execute("INSERT INTO originals VALUES(?,?,?,?,?,?,?,?,?,?)", (sha, 12, "text/plain", "2026-01-02T00:00:00+00:00", "record", "in_scope", None, "captured", "text", "{}"))
                for stage in module.STAGES:
                    con.execute("INSERT INTO stage_state VALUES(?,?,?,?,?,?,?,?)", (sha, stage, "pending", None, "unassigned", "2026-01-02T00:00:00+00:00", "synthetic", None))
                con.execute("INSERT INTO occurrences VALUES(?,?,?,?,?,?,?,?)", ("occ" + str(index), sha, "local", "synthetic", None, "2026-01-02T00:00:00+00:00", "test", "{}"))
            for name in ("north", "south"):
                con.execute("INSERT INTO agencies VALUES(?,?,?,?)", ("agency:" + name, name.title(), "Synthetic county", None))
                con.execute("INSERT INTO requests VALUES(?,?,?,?,?,?,?,?)", ("request:" + name, "agency:" + name, "SYN-1", None, "2026-01-20", None, None, "open"))
        self.output = self.root / "exports"
        self.output.mkdir(mode=0o700)

    def build(self, **kw):
        return module.build(self.db, self.root, **kw)

    def join(self, identifier, agency="north", status="typed", evidence=None, request=None):
        ev = evidence if evidence is not None else json.dumps({"source_sha256": H1, "locator": "page:1"})
        with sqlite3.connect(self.db) as con:
            con.execute("INSERT INTO joins VALUES(?,?,?,?,?,?,?,?)", (identifier, H1, "agency:" + agency, request or "request:" + agency, "agreement_party", ev, status, None))

    def test_every_original_unknown_card(self):
        result = self.build()
        self.assertEqual(len(result["cards"]), 2)
        self.assertEqual(result["counts"]["originals"], 2)
        for card in result["cards"]:
            self.assertEqual(card["agency_status"], "unknown")
            self.assertIsNone(card["value_score"])
            self.assertEqual(card["metadata_status"], "unknown")
            self.assertFalse(card["publication_ready"])
            self.assertEqual(set(card["stages"]), set(module.STAGES))

    def test_cross_agency_many_to_many_preserved(self):
        self.join("join-1")
        self.join("join-2", "south")
        card = next(c for c in self.build()["cards"] if c["sha256"] == H1)
        self.assertEqual(card["agency_ids"], ["agency:north", "agency:south"])
        self.assertEqual(card["request_ids"], ["request:north", "request:south"])

    def test_hinted_filename_not_typed(self):
        self.join("hint", status="hinted", evidence="filename hint")
        card = next(c for c in self.build()["cards"] if c["sha256"] == H1)
        self.assertEqual(card["agency_ids"], [])
        self.assertEqual(card["joins"][0]["status"], "hinted")

    def test_request_agency_mismatch_rejected(self):
        self.join("mismatch", request="request:south")
        with self.assertRaisesRegex(catalog_links.LinkError, "request_agency_mismatch"):
            self.build()

    def test_typed_join_needs_exact_evidence(self):
        self.join("missing", evidence="{}")
        with self.assertRaisesRegex(module.CatalogError, "typed_join_evidence_required"):
            self.build()

    def test_no_source_database_or_stage_mutation(self):
        before = self.db.read_bytes()
        self.build()
        self.assertEqual(self.db.read_bytes(), before)
        with sqlite3.connect(self.db) as con:
            self.assertEqual(con.execute("SELECT count(*) FROM stage_state WHERE status='pending'").fetchone()[0], 14)
            self.assertIsNone(con.execute("SELECT name FROM sqlite_master WHERE name='stage_validation_authority'").fetchone())

    def test_counts_byte_identical_in_board_and_file(self):
        result = self.build()
        exported = module.export(result, self.output)
        directory = self.output / exported["snapshot_id"]
        counts = (directory / "counts.json").read_bytes()
        self.assertEqual(counts, module.encoded(result["counts"]))
        board = (directory / "index.html").read_text()
        embedded = board.split('<pre id="counts">', 1)[1].split("</pre>", 1)[0]
        self.assertEqual(html.unescape(embedded).encode(), counts)

    def test_idempotent_export_private_modes(self):
        result = self.build()
        first = module.export(result, self.output)
        self.assertFalse(first["reused"])
        self.assertTrue(module.export(result, self.output)["reused"])
        destination = self.output / first["snapshot_id"]
        self.assertEqual(destination.stat().st_mode & 0o777, 0o700)
        self.assertTrue(all(p.stat().st_mode & 0o777 == 0o600 for p in destination.iterdir()))
        self.assertFalse((self.output / "accepted.json").exists())

    def test_changed_snapshot_does_not_overwrite(self):
        old = self.build()
        module.export(old, self.output)
        self.join("new")
        new = self.build()
        self.assertNotEqual(new["snapshot_id"], old["snapshot_id"])
        module.export(new, self.output)
        self.assertTrue((self.output / old["snapshot_id"] / "catalog.json").is_file())

    def test_stale_and_tampered_export_rejected(self):
        result = self.build()
        result["counts"]["originals"] = 88
        with self.assertRaisesRegex(module.CatalogError, "stale_snapshot_binding"):
            module.export(result, self.output)
        result = self.build()
        module.export(result, self.output)
        (self.output / result["snapshot_id"] / "counts.json").write_bytes(b"{}")
        with self.assertRaisesRegex(module.CatalogError, "existing_export_mismatch"):
            module.export(result, self.output)

    def test_alias_and_symlink_rejected(self):
        alias = self.root / "alias"
        alias.symlink_to(self.db)
        with self.assertRaisesRegex(module.CatalogError, "symlink_path"):
            module.build(alias, self.root)
        with self.assertRaisesRegex(module.CatalogError, "noncanonical_path"):
            module.build(self.root / ".." / self.root.name / "ledger.sqlite", self.root)

    def test_world_readable_output_rejected(self):
        self.output.chmod(0o755)
        with self.assertRaisesRegex(module.CatalogError, "owner_only_path_required"):
            module.export(self.build(), self.output)

    def test_repository_output_rejected(self):
        (self.output / ".git").write_text("synthetic repository marker")
        with self.assertRaisesRegex(module.CatalogError, "private_output_inside_repository"):
            module.export(self.build(), self.output)

    def test_html_escaped_no_external_assets(self):
        text = '<script src="https://example.invalid/payload">alert(1)</script>'
        overlay = {H1: {"title": text, "summary": text, "evidence": [{"source_sha256": H1, "locator": "page:1"}]}}
        result = self.build(overlays=overlay)
        board = module.render_board(result).decode()
        self.assertNotIn("<script", board)
        self.assertNotIn("<img", board)
        self.assertNotIn("<link", board)
        self.assertIn("&lt;script", board)
        self.assertIn("Content-Security-Policy", board)
        self.assertNotIn('href="https://', board)
        self.assertNotIn('href="http://', board)

    def test_low_value_never_closed(self):
        result = self.build(overlays={H1: {"low_value_reason": "possible signature image", "value_score": 0, "value_reason": "synthetic scoring"}})
        card = next(c for c in result["cards"] if c["sha256"] == H1)
        self.assertEqual(card["low_value_status"], "proposed low-value")
        self.assertEqual(card["status"], "pending")

    def test_prior_review_preserves_declared_label(self):
        with sqlite3.connect(self.db) as con:
            con.execute("INSERT INTO digests VALUES(?,?,?,?,?,?,?,?,?)", ("digest-1", H1, "synthetic-author", "partial", "{}", "not-exported", H2, None, "unverified"))
        card = next(c for c in self.build()["cards"] if c["sha256"] == H1)
        self.assertEqual(card["prior_review"][0]["coverage_declared"], "partial")
        self.assertEqual(card["stages"]["review"]["status"], "pending")

    def test_arrivals_timezone_and_cutoff(self):
        self.assertEqual(len(self.build(since="2026-01-01T00:00:00+00:00")["arrivals"]["originals"]), 2)
        self.assertEqual(self.build(since="2026-01-03T00:00:00+00:00")["arrivals"]["originals"], [])
        with self.assertRaises(module.CatalogError):
            self.build(since="2026-01-01")

    def test_bound_fails_without_partial_catalog(self):
        with mock.patch.object(module, "MAX_ORIGINALS", 1):
            with self.assertRaisesRegex(module.CatalogError, "metadata_row_limit"):
                self.build()

    def test_blocked_owner_and_open_request_render(self):
        with sqlite3.connect(self.db) as con:
            con.execute("UPDATE stage_state SET status='blocked',owner='synthetic-owner',reason='No parser' WHERE original_sha256=? AND stage='extract'", (H1,))
        result = self.build()
        board = module.render_board(result).decode()
        self.assertIn("synthetic-owner", board)
        self.assertIn("2026-01-20", board)
        self.assertEqual(result["counts"]["stages"]["extract"]["blocked"], 1)

    def test_duplicate_deliveries_one_card(self):
        with sqlite3.connect(self.db) as con:
            con.execute("INSERT INTO occurrences VALUES(?,?,?,?,?,?,?,?)", ("occ-duplicate", H1, "local", "synthetic second delivery", None, None, "test", "{}"))
        result = self.build()
        self.assertEqual(len(result["cards"]), 2)
        self.assertEqual(len(next(c for c in result["cards"] if c["sha256"] == H1)["occurrences"]), 2)

    def test_multiple_evidence_rows_preserve_one_context_pair(self):
        self.join("evidence-1")
        self.join("evidence-2", evidence=json.dumps({"source_sha256": H1, "locator": "page:2"}))
        result = self.build()
        card = next(c for c in result["cards"] if c["sha256"] == H1)
        self.assertEqual(len(card["joins"]), 2)
        self.assertEqual(result["link_validation"]["candidate_links"], 1)

    def test_opaque_join_evidence_not_exported(self):
        self.join("evidence", evidence=json.dumps({"source_sha256": H1, "locator": "page:1", "opaque": "not for board"}))
        self.assertNotIn("not for board", module.encoded(self.build()).decode())

    def test_capture_size_and_deadline_bounds(self):
        with mock.patch.object(module, "MAX_DATABASE_BYTES", 1):
            with self.assertRaisesRegex(module.CatalogError, "database_capture_size_limit"):
                self.build()
        with mock.patch.object(module, "CAPTURE_SECONDS", -1):
            with self.assertRaisesRegex(module.CatalogError, "database_capture_timeout"):
                self.build()

    def test_export_broken_symlink_rejected(self):
        result = self.build()
        (self.output / result["snapshot_id"]).symlink_to(self.root / "missing", target_is_directory=True)
        with self.assertRaisesRegex(module.CatalogError, "symlink_path"):
            module.export(result, self.output)

    def test_count_mismatch_rejected(self):
        result = self.build()
        broken = dict(result["counts"], originals=3)
        with sqlite3.connect(self.db) as con:
            con.row_factory = sqlite3.Row
            with self.assertRaisesRegex(module.CatalogError, "count_denominator_mismatch"):
                module.project(con, broken)


if __name__ == "__main__":
    unittest.main()
