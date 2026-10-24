"""Synthetic canonical primary/support resolution, no semantic/date authority."""
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from campaign_tool.records import context_revision as cr
from campaign_tool.records.detectors import sources
from campaign_tool.records.detectors.pipeline import evaluate_structured
from campaign_tool.records.detectors.core import digest
from campaign_tool.records.ledger import store
from campaign_tool.records.run import Pipeline, sha
from tests.child_fixtures import run_fixture
from tests.records import test_structured_context_boundary as boundary
from tests.records.test_run_slice import synthetic_email


class SourceResolverTests(unittest.TestCase):
    setUp = boundary.StructuredContextBoundaryTests.setUp
    join = boundary.StructuredContextBoundaryTests.join

    def resolve(self, supports=(), *, subject=None):
        return sources.resolve_sources(database=self.pipeline.root.ledger,
            original_root=self.pipeline.root.intake / "blobs",
            derived_root=self.pipeline.root.sub("artifacts") / "evidence",
            subject_sha256=subject or self.subject, support_sha256=supports)

    def support(self):
        raw = b"Synthetic supporting policy text; no verified legal dates or applicability."
        (self.inbox / "support.eml").write_bytes(synthetic_email(attachment=raw))
        self.pipeline = Pipeline(self.pipeline.root.path)
        self.pipeline.run(self.inbox)
        return sha(raw)

    def test_complete_explicit_primary_support_inventory_and_no_mutation(self):
        support = self.support()
        self.join()
        before = store.file_hash(self.pipeline.root.ledger)
        snapshot = self.resolve([support])
        self.assertEqual([r["role"] for r in snapshot.originals], ["primary", "support"])
        self.assertEqual(len(snapshot.bindings), sum(r["unit_count"] for r in snapshot.originals))
        self.assertEqual(snapshot.joins.source_bindings_sha256,
                         digest(snapshot.bindings))
        self.assertEqual(store.file_hash(self.pipeline.root.ledger), before)
        self.assertIn("verified_date_provider_unavailable", snapshot.requirements)
        self.assertEqual(snapshot.support_scope, "explicit_originals_only")
        self.assertEqual(snapshot.revision_sha256, self.resolve([support]).revision_sha256)

    def test_missing_support_refuses_whole_snapshot(self):
        with self.assertRaises(sources.ResolutionError):
            self.resolve([sha(b"never preserved synthetic support")])

    def test_declared_support_and_fresh_join_change_revision(self):
        first = self.resolve()
        support = self.support()
        second = self.resolve([support])
        self.assertNotEqual(first.revision_sha256, second.revision_sha256)
        self.join()
        self.assertNotEqual(second.revision_sha256, self.resolve([support]).revision_sha256)

    def test_replaced_support_bytes_refuse_snapshot(self):
        support = self.support()
        with store.ledger(self.pipeline.root.ledger, readonly=True) as con:
            path = store.effective_original(con, support)["storage_path"]
        Path(path).chmod(0o600)
        Path(path).write_bytes(b"changed synthetic bytes")
        with self.assertRaisesRegex(sources.ResolutionError, "original_bytes_changed"):
            self.resolve([support])

    def test_missing_or_tampered_current_unit_is_never_silently_capped(self):
        for mutation in ("missing", "bytes"):
            with self.subTest(mutation=mutation):
                with store.ledger(self.pipeline.root.ledger, readonly=True) as con:
                    row = dict(con.execute("SELECT * FROM units WHERE id=?",
                                           (self.sources[0]["unit_id"],)).fetchone())
                path = Path(row["derived_path"])
                original = path.read_bytes()
                path.unlink()
                if mutation == "bytes":
                    path.write_bytes(b"{}")
                    path.chmod(0o600)
                with self.assertRaises(sources.ResolutionError):
                    self.resolve()
                path.write_bytes(original)
                path.chmod(0o600)

    def test_rebound_locator_refused_even_when_payload_hash_unchanged(self):
        with store.ledger(self.pipeline.root.ledger) as con:
            con.execute("UPDATE units SET locator=? WHERE id=?",
                        (json.dumps({"line": 999}), self.sources[0]["unit_id"]))
            con.commit()
        with self.assertRaisesRegex(sources.ResolutionError, "current_unit_binding_changed"):
            self.resolve()

    def test_capped_runtime_selection_does_not_cap_resolver(self):
        with patch.object(self.pipeline, "_unit_view", return_value=([], [], {})):
            snapshot = self.resolve()
        self.assertEqual(len(snapshot.bindings), len(self.sources))
        with patch.object(sources, "MAX_UNITS", 0):
            with self.assertRaisesRegex(sources.ResolutionError, "inventory"):
                self.resolve()

    def test_symlink_and_outside_root_unit_refused(self):
        with store.ledger(self.pipeline.root.ledger, readonly=True) as con:
            row = con.execute("SELECT derived_path FROM units WHERE id=?",
                              (self.sources[0]["unit_id"],)).fetchone()
        path = Path(row[0])
        moved = self.base / "synthetic-moved-unit"
        path.rename(moved)
        path.symlink_to(moved)
        with self.assertRaises(sources.ResolutionError):
            self.resolve()
        path.unlink()
        path.write_bytes(moved.read_bytes())
        path.chmod(0o600)
        with store.ledger(self.pipeline.root.ledger) as con:
            con.execute("UPDATE units SET derived_path=? WHERE id=?",
                        (str(moved), self.sources[0]["unit_id"]))
            con.commit()
        with self.assertRaisesRegex(sources.ResolutionError, "outside"):
            self.resolve()

    def test_changed_current_import_is_not_historical_fallback(self):
        with store.ledger(self.pipeline.root.ledger) as con:
            con.execute("DELETE FROM extraction_adapter_current WHERE original_sha256=?", (self.subject,))
            con.commit()
        with self.assertRaisesRegex(sources.ResolutionError, "current_extraction_missing"):
            self.resolve()

    def test_wrong_request_agency_refuses_fresh_join(self):
        self.join(wrong_request=True)
        with self.assertRaises(sources.ResolutionError):
            self.resolve()

    def test_duplicate_support_and_total_byte_bound_refused(self):
        with self.assertRaisesRegex(sources.ResolutionError, "duplicate_original"):
            self.resolve([self.subject])
        with patch.object(sources, "MAX_TOTAL_BYTES", 1):
            with self.assertRaisesRegex(sources.ResolutionError, "snapshot_byte_bound"):
                self.resolve()

    def test_snapshot_does_not_normalize_or_promote_any_detector(self):
        snapshot = self.resolve()
        changed = snapshot.bindings
        changed.clear()
        self.assertTrue(snapshot.bindings)
        output = evaluate_structured(subject_sha256=self.subject,
            source_bindings=snapshot.bindings, joins=snapshot.joins)
        self.assertEqual({v["reason"] for v in output["structured"].values()}, {"normalization_missing"})
        self.assertEqual(output["stage_promotions"], 0)

    def test_missing_and_ambiguous_source_dates_remain_unknown(self):
        # Even structurally source-bound synthetic assertions need a trusted
        # date/applicability provider. This resolver never supplies either.
        snapshot = self.resolve()
        evidence = [{"source_sha256": self.subject,
                     "locator": json.dumps(snapshot.bindings[0]["locator"])}]
        rule = {"rule_id": "synthetic-duty", "effective_from": "2025-01-01", "effective_to": None}
        for periods in ([], [{"event_kind": "search", "start": "2026-01-01",
                              "end": None, "evidence": evidence}] * 2):
            context = cr.event_context({"synthetic": True}, {"conduct": periods})
            basis = {"rule_revision_sha256": cr.rule_revision(rule),
                     "context_sha256": cr.fingerprint(context), "event_role": "conduct",
                     "event_kind": "search", "version_verified": True,
                     "version_evidence": evidence, "rationale": "Synthetic supplied assertion"}
            report = cr.resolve_applicability("synthetic-duty", [rule], context, [basis])
            self.assertEqual(report["applicability"], "unknown")
            self.assertIn("event_date_missing_or_ambiguous", report["reasons"])

    def test_join_reads_share_the_same_ledger_snapshot(self):
        self.join()
        first = self.resolve()
        real = sources.canonical_joins
        def concurrent_change(con, bindings):
            with store.ledger(self.pipeline.root.ledger) as writer:
                writer.execute("UPDATE agencies SET name='Changed synthetic agency' WHERE id='synthetic-a'")
                writer.commit()
            return real(con, bindings)
        with patch.object(sources, "canonical_joins", side_effect=concurrent_change):
            during = self.resolve()
        self.assertEqual(first.revision_sha256, during.revision_sha256)
        self.assertNotEqual(during.revision_sha256, self.resolve().revision_sha256)

    def test_ambiguous_agencies_are_retained_not_guessed(self):
        self.join()
        with store.ledger(self.pipeline.root.ledger) as con:
            con.execute("INSERT INTO joins SELECT 'synthetic-ambiguous',original_sha256,"
                "'synthetic-b',NULL,join_type,evidence,status,blocked_reason FROM joins "
                "WHERE id='synthetic-join'")
            con.commit()
        snapshot = self.resolve()
        self.assertEqual({r["agency_id"] for r in json.loads(snapshot.joins.rows_json)},
                         {"synthetic-a", "synthetic-b"})
        self.assertIn("verified_applicability_provider_unavailable", snapshot.requirements)

    def check_exclusion(self, *, primary):
        with store.ledger(self.pipeline.root.ledger, readonly=True) as con:
            original = dict(con.execute("SELECT * FROM originals WHERE sha256=?",
                                        (self.subject,)).fetchone())
        for field, value in (("scope", "excluded"), ("scope", "out_of_scope"),
                             ("role", "excluded_unrelated_personal")):
            with self.subTest(field=field, value=value):
                # Fresh immutable declarations, not rewritten accepted evidence.
                raw = ("Synthetic unaccepted exclusion fixture " + field + value).encode()
                identity = sha(raw)
                row = dict(original, sha256=identity, bytes=len(raw),
                           preservation_status="unresolved",
                           provenance_json=json.dumps({"synthetic_fixture": "excluded_unaccepted"}))
                path = self.pipeline.root.intake / "blobs" / identity
                path.write_bytes(raw)
                path.chmod(0o600)
                row.update(storage_path=str(path))
                row[field] = value
                with store.ledger(self.pipeline.root.ledger) as con:
                    columns = list(row)
                    con.execute("INSERT INTO originals(" + ",".join(columns) + ") VALUES(" +
                                ",".join("?" for _ in columns) + ")", [row[k] for k in columns])
                    con.commit()
                with patch.object(sources, "secure_open", wraps=sources.secure_open) as opened:
                    with self.assertRaisesRegex(sources.ResolutionError, "original_missing_or_excluded"):
                        self.resolve(() if primary else (identity,),
                                     subject=identity if primary else self.subject)
                self.assertNotIn(path, [call.args[0] for call in opened.call_args_list])

    def test_shared_scope_exclusions_refuse_primary_before_open(self):
        self.check_exclusion(primary=True)

    def test_shared_scope_exclusions_refuse_support_before_open(self):
        self.check_exclusion(primary=False)

    def test_substituted_original_and_unit_fifos_refuse_within_subprocess_deadline(self):
        with store.ledger(self.pipeline.root.ledger, readonly=True) as con:
            original = store.effective_original(con, self.subject)["storage_path"]
            unit = con.execute("SELECT derived_path FROM units WHERE id=?",
                               (self.sources[0]["unit_id"],)).fetchone()[0]
        for name in (original, unit):
            with self.subTest(path_kind="original" if name == original else "unit"):
                path = Path(name)
                raw = path.read_bytes()
                path.unlink()
                os.mkfifo(path, 0o600)
                try:
                    completed = run_fixture("source_resolver_fifo_probe",
                        str(self.pipeline.root.ledger), str(self.pipeline.root.intake / "blobs"),
                        str(self.pipeline.root.sub("artifacts") / "evidence"), self.subject,
                        timeout=3, capture_output=True, text=True)
                    self.assertEqual(completed.returncode, 0, completed.stderr)
                finally:
                    path.unlink()
                    path.write_bytes(raw)
                    path.chmod(0o600)
