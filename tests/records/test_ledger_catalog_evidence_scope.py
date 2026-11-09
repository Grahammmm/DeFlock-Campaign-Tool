"""Synthetic RF1345-3 tests: excluded originals are never catalog evidence sources."""
from contextlib import closing
import hashlib
import json
import sqlite3
import tempfile
import unittest
from unittest import mock

from tests.records import test_ledger_catalog as base

H1, H2, module, store = base.H1, base.H2, base.module, base.store

REFUSAL = "evidence_source_scope_excluded"
MARKER = "synthetic-private-marker"
# (declared scope, declared role) pairs that scope_excluded() must treat as excluded.
EXCLUDED_KINDS = (("excluded", "record"), ("out_of_scope", "record"),
                  ("in_scope", module.EXCLUDED_ROLE))


class EvidenceScopeTests(base.CanonicalCatalogTests):
    """Reuses the canonical fixture ledger (two in-scope originals, two agencies)."""

    def add_original(self, scope, role, name=None):
        sha = hashlib.sha256(("synthetic " + (name or scope + role)).encode()).hexdigest()
        with closing(sqlite3.connect(self.db)) as con, con:
            con.execute("INSERT INTO originals VALUES(?,?,?,?,?,?,?,?,?,?)", (sha, 12, "text/plain", "2026-01-02T00:00:00+00:00", role, scope, None, "captured", "text", "{}"))
            for stage in module.STAGES:
                con.execute("INSERT INTO stage_state VALUES(?,?,?,?,?,?,?,?)", (sha, stage, "pending", None, "unassigned", "2026-01-02T00:00:00+00:00", "synthetic", None))
            con.execute("INSERT INTO occurrences VALUES(?,?,?,?,?,?,?,?)", ("occ-" + sha[:8], sha, "local", "synthetic", None, "2026-01-02T00:00:00+00:00", "test", "{}"))
        return sha

    def evidence(self, source, locator="page:1"):
        return {"source_sha256": source, "locator": locator}

    def assert_refused_without_side_effects(self, **kw):
        before = self.db.read_bytes()
        exports = sorted(p.name for p in self.output.iterdir())
        with self.assertRaises(module.CatalogError) as caught:
            self.build(**kw)
        self.assertEqual(str(caught.exception), REFUSAL)
        self.assertNotIn(MARKER, str(caught.exception))
        self.assertEqual(self.db.read_bytes(), before)
        self.assertEqual(sorted(p.name for p in self.output.iterdir()), exports)

    def test_typed_join_rejects_excluded_evidence_source(self):
        for scope, role in EXCLUDED_KINDS:
            with self.subTest(scope=scope, role=role):
                source = self.add_original(scope, role)
                self.join("cross-" + source[:8], evidence=json.dumps(self.evidence(source)))
                self.assert_refused_without_side_effects()
                with closing(sqlite3.connect(self.db)) as con, con:
                    con.execute("DELETE FROM joins WHERE id=?", ("cross-" + source[:8],))

    def test_overlay_rejects_excluded_evidence_source(self):
        self.join("agency-link")
        for scope, role in EXCLUDED_KINDS:
            with self.subTest(scope=scope, role=role):
                source = self.add_original(scope, role)
                self.assert_refused_without_side_effects(
                    overlays={H1: {"title": MARKER, "evidence": [self.evidence(source)]}})

    def test_overlay_checks_all_evidence_entries(self):
        source = self.add_original("excluded", "record")
        good, bad = self.evidence(H2), self.evidence(source)
        for evidence in ([good, bad], [bad, good], [bad]):
            with self.subTest(order=[e["source_sha256"] == source for e in evidence]):
                self.assert_refused_without_side_effects(overlays={H1: {"evidence": evidence}})
        # A later draft for another original is checked as well.
        self.assert_refused_without_side_effects(overlays={
            H1: {"title": "Synthetic", "evidence": [good]}, H2: {"evidence": [good, bad]}})

    def test_source_eligibility_uses_effective_original_scope(self):
        # Resolution wins over the immutable declaration, in both directions.
        resolved_excluded = self.add_original("in_scope", "record", "declared in scope")
        resolved_included = self.add_original("excluded", "record", "declared excluded")
        real = store.effective_original

        def effective(connection, subject):
            original = real(connection, subject)
            if subject == resolved_excluded:
                original.update(scope="out_of_scope")
            if subject == resolved_included:
                original.update(scope="in_scope")
            return original

        with mock.patch.object(store, "effective_original", effective):
            self.assert_refused_without_side_effects(
                overlays={H1: {"title": "Synthetic", "evidence": [self.evidence(resolved_excluded)]}})
            self.join("typed-bad", evidence=json.dumps(self.evidence(resolved_excluded)))
            self.assert_refused_without_side_effects()
            with closing(sqlite3.connect(self.db)) as con, con:
                con.execute("DELETE FROM joins WHERE id='typed-bad'")
            result = self.build(overlays={H1: {"title": "Synthetic",
                                               "evidence": [self.evidence(resolved_included)]}})
        card = next(c for c in result["cards"] if c["sha256"] == H1)
        self.assertEqual(card["metadata"]["evidence"], [self.evidence(resolved_included)])

    def test_valid_cross_original_and_self_evidence_preserved(self):
        self.join("self-source")
        self.join("cross-source", "south", evidence=json.dumps(self.evidence(H2, "page:7")))
        overlays = {H1: {"title": "Synthetic", "evidence": [self.evidence(H1, "page:2"), self.evidence(H2, "page:3")]}}
        first, second = self.build(overlays=overlays), self.build(overlays=overlays)
        self.assertEqual(first["snapshot_id"], second["snapshot_id"])
        card = next(c for c in first["cards"] if c["sha256"] == H1)
        self.assertEqual({j["id"]: j["evidence"] for j in card["joins"]},
                         {"self-source": self.evidence(H1), "cross-source": self.evidence(H2, "page:7")})
        self.assertEqual(card["metadata"]["evidence"], overlays[H1]["evidence"])
        self.assertEqual(card["agency_ids"], ["agency:north", "agency:south"])
        self.assertEqual(card["metadata_status"], "candidate")
        self.assertIs(card["publication_ready"], False)
        self.assertIs(first["publication_ready"], False)
        self.assertEqual(module.artifact_payloads(first), module.artifact_payloads(second))

    def test_unknown_and_malformed_sources_keep_existing_errors(self):
        unknown = hashlib.sha256(b"synthetic unknown").hexdigest()
        cases = [
            (json.dumps(self.evidence(unknown)), "typed_join_evidence_required"),
            ("{}", "typed_join_evidence_required"),
            (json.dumps({"source_sha256": H1}), "typed_join_evidence_required"),
            ('{"source_sha256":"%s","source_sha256":"%s","locator":"p"}' % (H1, H1), "typed_join_evidence_required"),
            ('{"source_sha256":"%s","locator":NaN}' % H1, "typed_join_evidence_required"),
        ]
        for text, code in cases:
            with self.subTest(evidence=text[:40]):
                self.join("bad", evidence=text)
                with self.assertRaisesRegex(module.CatalogError, code):
                    self.build()
                with closing(sqlite3.connect(self.db)) as con, con:
                    con.execute("DELETE FROM joins WHERE id='bad'")
        for overlay in ({H1: {"evidence": [self.evidence(unknown)]}},
                        {H1: {"evidence": [{"source_sha256": H1}]}}):
            with self.assertRaisesRegex(module.CatalogError, "invalid_evidence"):
                self.build(overlays=overlay)

    def test_hinted_and_blocked_joins_do_not_become_evidence(self):
        source = self.add_original("excluded", "record")
        self.join("hint", status="hinted", evidence=json.dumps(self.evidence(source)))
        self.join("block", "south", status="blocked", evidence=json.dumps(self.evidence(source)))
        card = next(c for c in self.build()["cards"] if c["sha256"] == H1)
        self.assertEqual(card["agency_ids"], [])
        self.assertEqual({j["status"]: j["evidence"] for j in card["joins"]},
                         {"hinted": None, "blocked": None})

    def test_excluded_identity_cards_and_counts_remain(self):
        sources = {kind: self.add_original(*kind) for kind in EXCLUDED_KINDS}
        result = self.build(overlays={H1: {"title": "Synthetic", "evidence": [self.evidence(H2)]}})
        self.assertEqual(result["counts"]["originals"], 2 + len(sources))
        cards = {c["sha256"]: c for c in result["cards"]}
        for sha in sources.values():
            self.assertEqual(cards[sha]["agency_ids"], [])
            self.assertEqual(cards[sha]["metadata_status"], "unknown")
        # Excluded join targets keep their own, distinct refusal.
        self.join("target", evidence=json.dumps(self.evidence(H1)))
        with closing(sqlite3.connect(self.db)) as con, con:
            con.execute("UPDATE joins SET original_sha256=? WHERE id='target'", (next(iter(sources.values())),))
        with self.assertRaisesRegex(module.CatalogError, "source_scope_excluded"):
            self.build()

    def test_failed_build_preserves_ledger_and_prior_export(self):
        valid = self.build()
        exported = module.export(valid, self.output)
        tree = {p.relative_to(self.output): p.read_bytes()
                for p in sorted(self.output.rglob("*")) if p.is_file() and p.name != "catalog-export.lock"}
        source = self.add_original("excluded", "record")
        before = self.db.read_bytes()
        # The build fails before an export can be requested, so no result exists to export.
        result = None
        with self.assertRaises(module.CatalogError) as caught:
            result = self.build(overlays={H1: {"title": MARKER, "evidence": [self.evidence(source)]}})
        self.assertIsNone(result)
        self.assertEqual(str(caught.exception), REFUSAL)
        self.assertEqual(self.db.read_bytes(), before)
        self.assertEqual([p.name for p in self.output.iterdir() if p.is_dir()], [exported["snapshot_id"]])
        after = {p.relative_to(self.output): p.read_bytes()
                 for p in sorted(self.output.rglob("*")) if p.is_file() and p.name != "catalog-export.lock"}
        self.assertEqual(after, tree)


# The inherited canonical tests are covered by their own module; keep only this module's matrix.
for _name in [n for n in dir(base.CanonicalCatalogTests) if n.startswith("test_")]:
    if _name not in EvidenceScopeTests.__dict__:
        setattr(EvidenceScopeTests, _name, None)

if __name__ == "__main__":
    unittest.main()
