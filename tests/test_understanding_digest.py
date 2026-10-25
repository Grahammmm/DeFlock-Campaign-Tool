"""F-043 frozen synthetic acceptance; all outputs retained under candidate/out.

Uses the REAL structured adapter and canonical_joins against synthetic SQLite.
The F-042 wrapper/F-044 result shapes are pinned to parent native sources; no
ported parent implementation is copied. No services, stores, network or jobs.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from campaign_tool.records import run_stages
from campaign_tool.records.detectors import core, pipeline
from campaign_tool.records.understanding import digest
from tests.records.test_structured_detector_pipeline import context, ledger

CANDIDATE = Path(__file__).resolve().parents[2]
OUT = CANDIDATE / "out"
RUN = "synthetic-run-043"


def fixture(root):
    args, join_rows = context("retention-over-policy")
    con = ledger(join_rows, args["source_bindings"])
    try:
        args["joins"] = pipeline.canonical_joins(con, args["source_bindings"])
    finally:
        con.close()
    structured = pipeline.evaluate_structured(**args)["structured"]
    result = {"schema": "detectors-run-v1", "run_id": RUN,
              "subject_sha256": args["subject_sha256"], "structured": structured,
              "inputs": {n: {"sha256": digest.digest(n)} for n in
                         ("inventory", "units", "rules", "config", "canonical_copy")}}
    message_hash = hashlib.sha256(b"synthetic request message").hexdigest()
    cite = {"message_sha256": message_hash, "message_id": "synthetic-message-id"}
    # Exact F-044 output field vocabulary, not a different tracker schema.
    requests = {"schema": "records-understanding-requests-v1", "as_of": "2020-01-20",
                "requests": [{"request_id": "request-043", "agency_id": "synthetic-agency",
                              "request_number": "synthetic-private-label", "state": "sent",
                              "unresolved_reasons": [], "scope_uncertainty": False,
                              "message_sha256s": [message_hash],
                              "transitions": [{"state": "sent", "citation": cite,
                                               "message_date": "2020-01-01", "direction": "outbound",
                                               "kind": "request"}],
                              "reminders": {"status": "candidate", "anchor": {"citation": cite},
                                            "ten_day_candidate": "2020-01-11",
                                            "twenty_four_day_candidate": "2020-01-25"}}],
                "holds": [], "send": False, "outbound_messages": 0,
                "qualified": False, "publication_ready": False, "review_required": True}
    return {"plan": {"digest": {"enabled": True}, "synthetic": True}, "run_id": RUN,
            "detector_results": [result], "requests": requests,
            "request_provenance": {"run_id": RUN, "sha256": digest.digest(requests)},
            "source_bindings": args["source_bindings"], "joins": args["joins"],
            "admitted_root": root, "output_root": root / "digest-1"}


class DigestTests(unittest.TestCase):
    def setUp(self):
        OUT.mkdir(exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix="digest-", dir=OUT))
        self.ctx = fixture(self.root)

    def report(self):
        return digest.prepare_digest(**self.ctx)[1]

    def refresh_requests(self):
        self.ctx["request_provenance"]["sha256"] = digest.digest(self.ctx["requests"])

    def hit(self):
        return self.ctx["detector_results"][0]["structured"]["retention-over-policy"]["hits"][0]

    def refused(self, reason):
        before = sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*"))
        with self.assertRaisesRegex(digest.DigestRefused, "^" + reason + "$"):
            digest.write_digest(**self.ctx)
        self.assertEqual(before, sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*")))

    def test_disabled_writes_nothing_even_invalid_unread_inputs(self):
        for plan in ({}, {"digest": {}}, {"digest": {"enabled": False}}):
            self.assertEqual(digest.write_digest(plan=plan, output_root="/inert/not-read"),
                             {"status": "disabled", "files_written": 0})
        self.assertEqual(list(self.root.iterdir()), [])

    def test_flag_types_and_synthetic_admission(self):
        for flag in (1, "false", None):
            self.ctx["plan"]["digest"]["enabled"] = flag
            self.refused("digest_enabled_not_boolean")
        self.ctx["plan"] = {"digest": {"enabled": True}}
        self.refused("synthetic_plan_required")

    def test_exact_real_adapter_aggregate_and_citations(self):
        report = self.report()
        source = self.ctx["source_bindings"][0]
        hit = self.hit()
        self.assertEqual(report["aggregates"], [{"agency_id": "synthetic-agency",
            "detector": "retention-over-policy", "cited_observations": 1,
            "observation_ids": [hit["dedupe_key"]], "citations": [{
                "original_sha256": source["original_sha256"], "locator_sha256": digest.digest({"row": 1})}]}])
        self.assertEqual(report["observations"][0]["rule_source_sha256"], hit["applicability"]["rule_source_sha256"])
        self.assertFalse(report["qualified"])
        self.assertFalse(report["publication_ready"])
        self.assertFalse(report["activation_allowed"])
        self.assertEqual(report["stage_promotions"], 0)
        self.assertTrue(report["review_required"])
        self.assertEqual(report["legal_conclusion"], "NOT_ASSESSED")

    def test_deterministic_repeat_bytes_and_private_modes(self):
        original = copy.deepcopy(self.ctx)
        digest.write_digest(**self.ctx)
        self.ctx["output_root"] = self.root / "digest-2"
        digest.write_digest(**self.ctx)
        for name in ("digest.json", "digest.md"):
            self.assertEqual((self.root / "digest-1" / name).read_bytes(),
                             (self.root / "digest-2" / name).read_bytes())
            self.assertEqual(stat.S_IMODE((self.root / "digest-2" / name).stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.root / "digest-2").stat().st_mode), 0o700)
        self.ctx["output_root"] = original["output_root"]
        self.assertEqual(self.ctx, original)

    def test_names_quotes_and_private_values_never_emitted(self):
        self.hit()["quote"] = "Officer Fictional Person emailed fictional@example.test"
        self.hit()["observed_values_private"] = {"person": "Bare Fictional Name"}
        self.ctx["requests"]["requests"][0]["quote"] = "Requester: Madeup Name"
        self.refresh_requests()
        digest.write_digest(**self.ctx)
        for name in ("digest.json", "digest.md"):
            raw = (self.ctx["output_root"] / name).read_text()
            for forbidden in ("Fictional", "fictional@example.test", "Madeup", "synthetic-private-label"):
                self.assertNotIn(forbidden, raw)
        headings = [l for l in (self.ctx["output_root"] / "digest.md").read_text().splitlines() if l.startswith("## Agency")]
        self.assertEqual(headings, ["## Agency ID: synthetic-agency"])

    def test_missing_or_unresolvable_citations_unresolved_not_claim(self):
        for mutation in ("missing", "hash", "locator", "rule", "agency"):
            self.ctx = fixture(self.root)
            hit = self.hit()
            if mutation == "missing":
                hit["evidence"] = []
            elif mutation == "hash":
                hit["evidence"][0]["original_sha256"] = "f" * 64
            elif mutation == "locator":
                hit["evidence"][0]["locator"] = {"row": 99}
            elif mutation == "rule":
                hit["applicability"]["rule_source_sha256"] = "f" * 64
            else:
                hit["applicability"]["agency_id"] = "unbound-agency"
            report = self.report()
            self.assertEqual(report["aggregates"], [])
            self.assertEqual(report["unresolved"][0]["reason"], "citation_or_agency_unresolved")

    def test_blocked_and_inapplicable_keep_exact_reason_no_observations(self):
        entry = self.ctx["detector_results"][0]["structured"]["retention-over-policy"]
        for status, reason in (("blocked", "required_context_unresolved"),
                               ("inapplicable", "rule_not_effective")):
            entry.update(status=status, reason=reason, suppressed_hit_count=1)
            report = self.report()
            self.assertEqual(report["aggregates"], [])
            row = next(r for r in report["detector_statuses"] if r["detector"] == "retention-over-policy")
            self.assertEqual((row["status"], row["reason"], row["suppressed_hit_count"]), (status, reason, 1))
            self.assertTrue(row["outcomes"][0]["citations"])

    def test_all_blocked_missing_inventory_and_refused_result_visible(self):
        result = self.ctx["detector_results"][0]
        result["inputs"]["inventory"] = None
        result["structured"] = {n: pipeline._entry("blocked", "required_context_unresolved") for n in core.DETECTORS}
        report = self.report()
        self.assertEqual(report["aggregates"], [])
        self.assertEqual(len(report["detector_statuses"]), 7)
        self.assertEqual({r["reason"] for r in report["detector_statuses"]}, {"required_context_unresolved"})
        result.update(structured=None, reason="canonical_copy_changed")
        self.assertEqual(self.report()["unresolved"], [{"subject_sha256": result["subject_sha256"],
                         "kind": "detector_result_unresolved", "reason": "canonical_copy_changed"}])

    def test_request_candidates_and_holds_preserved(self):
        self.ctx["requests"]["holds"] = [{"reason": "invalid_citation", "quote": "Madeup Name"}]
        self.refresh_requests()
        report = self.report()
        row = report["requests"][0]
        self.assertEqual(row["state"], "sent")
        self.assertEqual(row["reminders"]["ten_day_candidate"], "2020-01-11")
        self.assertEqual(row["reminders"]["twenty_four_day_candidate"], "2020-01-25")
        self.assertFalse(row["send"])
        self.assertFalse(row["reminders"]["legal_due_date"])
        self.assertEqual(row["no_response"], "NOT_ESTABLISHED")
        self.assertEqual(report["request_holds"], [{"hold_sha256": digest.digest(self.ctx["requests"]["holds"][0]),
                                                   "status": "UNRESOLVED"}])

    def test_request_missing_citation_unknown_agency_and_existing_unresolved(self):
        for mutation in ("missing", "wrong_hash", "unresolved", "agency", "anchor"):
            self.ctx = fixture(self.root)
            request = self.ctx["requests"]["requests"][0]
            if mutation == "missing":
                request["transitions"] = []
            elif mutation == "wrong_hash":
                request["transitions"][0]["citation"]["message_sha256"] = "f" * 64
            elif mutation == "unresolved":
                request["unresolved_reasons"] = ["missing_sent_date"]
            elif mutation == "agency":
                request["agency_id"] = "unknown"
            else:
                request["reminders"]["anchor"]["citation"] = {}
            self.refresh_requests()
            row = self.report()["requests"][0]
            self.assertEqual(row["state"], "UNRESOLVED")
            self.assertIsNone(row["reminders"]["ten_day_candidate"])
            self.assertTrue(row["unresolved_reasons"])

    def test_preserve_run_and_all_input_provenance(self):
        report = self.report()
        p = report["provenance"]
        self.assertEqual(p["requests"], self.ctx["request_provenance"])
        self.assertEqual(p["detectors"][0]["result_sha256"], digest.digest(self.ctx["detector_results"][0]))
        self.assertEqual(p["detectors"][0]["inputs"], self.ctx["detector_results"][0]["inputs"])
        self.assertEqual(p["source_bindings_sha256"], digest.digest(self.ctx["source_bindings"]))
        self.assertEqual(p["joins_revision_sha256"], self.ctx["joins"].revision_sha256)

    def test_identity_drift_and_unknown_population_fail_before_writes(self):
        for mutation, reason in (("run", "detector_run_mismatch"), ("request", "request_provenance_mismatch"),
                                 ("population", "detector_population_mismatch"), ("status", "detector_status_invalid"),
                                 ("duplicate", "detector_subject_conflict"), ("inputs", "detector_inputs_missing")):
            self.ctx = fixture(self.root)
            result = self.ctx["detector_results"][0]
            if mutation == "run":
                result["run_id"] = "wrong"
            elif mutation == "request":
                self.ctx["requests"]["as_of"] = "changed"
            elif mutation == "population":
                result["structured"].pop(core.DETECTORS[0])
            elif mutation == "status":
                result["structured"][core.DETECTORS[0]]["status"] = "clean"
            elif mutation == "duplicate":
                self.ctx["detector_results"].append(copy.deepcopy(result))
            else:
                result["inputs"]["name"] = {"sha256": "a" * 64}
            self.refused(reason)

    def test_unsafe_roots_rejected_before_any_write(self):
        for value in ("relative", str(self.root / ".." / "escape"), self.root.parent / "escape",
                      self.root / "missing" / "nested", self.root / "fixtures", self.root):
            self.ctx["output_root"] = value
            with self.assertRaises(digest.DigestRefused):
                digest.write_digest(**self.ctx)
            self.assertEqual(list(self.root.iterdir()), [])

    def test_symlink_root_ancestor_leaf_and_existing_output(self):
        (self.root / "link").symlink_to(self.root, target_is_directory=True)
        self.ctx["output_root"] = self.root / "link" / "escape"
        self.refused("output_root_symlink")
        self.ctx["output_root"] = self.root / "link"
        self.refused("output_root_symlink")
        self.ctx["admitted_root"] = self.root / "link"
        self.ctx["output_root"] = self.root / "link" / "escape"
        self.refused("output_root_symlink")
        self.ctx["admitted_root"] = self.root
        self.ctx["output_root"] = self.root / "existing"
        self.ctx["output_root"].mkdir()
        (self.ctx["output_root"] / "sentinel").write_bytes(b"unchanged")
        self.refused("output_root_not_exclusive")
        self.assertEqual((self.ctx["output_root"] / "sentinel").read_bytes(), b"unchanged")

    def test_mutated_gate_control_proves_citation_check(self):
        # Break an exact locator binding, then bypass ONLY core.reference in
        # an inert control. The same negative assertion now detects the leak.
        self.hit()["evidence"][0]["locator"] = {"row": 99}
        self.assertEqual(self.report()["aggregates"], [])
        reference = core.reference
        def broken_reference(value):
            result = reference(value)
            if result["locator"] == {"row": 99}:
                result["locator"] = {"row": 1}
            return result
        with mock.patch.object(core, "reference", side_effect=broken_reference):
            with self.assertRaises(AssertionError):
                self.assertEqual(self.report()["aggregates"], [])

    def test_input_and_whole_output_bounds(self):
        self.ctx["requests"]["quote"] = "x" * 100
        self.refresh_requests()
        with mock.patch.object(digest, "MAX_BYTES", 10):
            self.refused("input_byte_bound")
        # Output gate separately reached: increase serialization amplification.
        with mock.patch.object(digest, "markdown", return_value="x" * (digest.MAX_BYTES + 1)):
            self.refused("output_byte_bound")

    def test_replay_refuses_overwrite_and_preserves_bytes(self):
        digest.write_digest(**self.ctx)
        before = {p.name: p.read_bytes() for p in self.ctx["output_root"].iterdir()}
        self.refused("output_root_not_exclusive")
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.ctx["output_root"].iterdir()})

    def snapshots(self):
        processing = self.root / "processing-v1"
        portal = self.root / "portal-queue-v1"
        processing.mkdir()
        portal.mkdir()
        db = sqlite3.connect(processing / "ledger.sqlite")
        db.executescript("CREATE TABLE stage_state(stage TEXT,status TEXT); CREATE TABLE mail_messages(id TEXT); CREATE TABLE originals(id TEXT);")
        db.close()
        db = sqlite3.connect(portal / "portal.sqlite")
        db.execute("CREATE TABLE portal_items(id TEXT)")
        db.close()
        return [processing / "ledger.sqlite", portal / "portal.sqlite"]

    def test_stage_outputs_real_enabled_seam_and_unchanged_snapshot_inputs(self):
        stores = self.snapshots()
        before = {p: p.read_bytes() for p in stores}
        args = SimpleNamespace(project=str(self.root), run_id=RUN, digest_context=self.ctx)
        line, exit_code = run_stages.stage_outputs(args)
        self.assertEqual(exit_code, 0)
        self.assertEqual(line["status"], "ok")
        self.assertEqual(line["counts"]["output"], 3)
        self.assertTrue((self.ctx["output_root"] / "digest.json").is_file())
        self.assertTrue((self.root / "run-outputs" / (RUN + ".json")).is_file())
        self.assertEqual(before, {p: p.read_bytes() for p in stores})
        self.assertFalse(any(p.suffix in {".sqlite-wal", ".sqlite-shm"} for p in self.root.rglob("*")))

    def test_stage_disabled_preserves_exact_snapshot_behavior(self):
        self.snapshots()
        args = SimpleNamespace(project=str(self.root), run_id=RUN)
        baseline, exit_code = run_stages.stage_outputs(args)
        snapshot = (self.root / "run-outputs" / (RUN + ".json")).read_bytes()
        self.ctx["plan"]["digest"]["enabled"] = False
        args.digest_context = self.ctx
        line, code = run_stages.stage_outputs(args)
        self.assertEqual((line, code), (baseline, exit_code))
        self.assertEqual((self.root / "run-outputs" / (RUN + ".json")).read_bytes(), snapshot)
        self.assertFalse(self.ctx["output_root"].exists())

    def test_stage_invalid_root_before_snapshot_read_or_write(self):
        self.ctx["output_root"] = self.root.parent / "escape"
        args = SimpleNamespace(project=str(self.root), run_id=RUN, digest_context=self.ctx)
        with mock.patch.object(run_stages, "ledger_ro", side_effect=AssertionError("must not read")):
            line, code = run_stages.stage_outputs(args)
        self.assertEqual((line["error_code"], code), ("OUTPUTS_DIGEST_REFUSED", 1))
        self.assertEqual(list(self.root.iterdir()), [])

    def test_stage_unresolved_warn_and_write_failure_fail(self):
        self.snapshots()
        self.hit()["evidence"] = []
        args = SimpleNamespace(project=str(self.root), run_id=RUN, digest_context=self.ctx)
        line, code = run_stages.stage_outputs(args)
        self.assertEqual((line["status"], line["error_code"], code), ("warn", "OUTPUTS_DIGEST_UNRESOLVED", 0))
        self.ctx["output_root"] = self.root / "digest-failed"
        with mock.patch.object(digest, "commit_digest", side_effect=OSError("inert synthetic failure")):
            line, code = run_stages.stage_outputs(args)
        self.assertEqual((line["status"], line["error_code"], code), ("fail", "OUTPUTS_DIGEST_WRITE_FAILED", 1))

    def test_empty_results_and_duplicate_observation_identity(self):
        self.ctx["detector_results"] = []
        self.assertEqual(self.report()["unresolved"], [{"kind": "detector_result_unresolved",
                                                     "reason": "detector_results_missing"}])
        self.ctx = fixture(self.root)
        hit = self.hit()
        entry = self.ctx["detector_results"][0]["structured"]["retention-over-policy"]
        entry["hits"].append(copy.deepcopy(hit))
        self.assertEqual(self.report()["aggregates"][0]["cited_observations"], 1)
        conflict = copy.deepcopy(hit)
        conflict["evidence"].append({"original_sha256": self.ctx["source_bindings"][1]["original_sha256"],
                                   "locator": self.ctx["source_bindings"][1]["locator"]})
        # Independent valid citation under same typed agency with reused hit ID.
        extra = copy.deepcopy(self.ctx["source_bindings"][0])
        extra.update(unit_id="extra-synthetic-unit", locator={"row": 2})
        self.ctx["source_bindings"].append(extra)
        self.ctx["joins"] = pipeline.CanonicalJoins(core.digest(self.ctx["source_bindings"]),
            self.ctx["joins"].rows_json, self.ctx["joins"].revision_sha256)
        conflict["evidence"] = [{"original_sha256": extra["original_sha256"], "locator": extra["locator"]}]
        entry["hits"].append(conflict)
        self.refused("observation_identity_conflict")

    def test_joins_binding_and_json_shape_fail_closed(self):
        self.ctx["joins"] = pipeline.CanonicalJoins("f" * 64, "[]", "a" * 64)
        self.refused("joins_inventory_mismatch")
        self.ctx = fixture(self.root)
        self.ctx["requests"]["quote"] = float("nan")
        self.refused("input_json_invalid")

    def test_stage_wrong_project_and_run_rejected_without_reads(self):
        for mutation in ("project", "run"):
            args = SimpleNamespace(project=str(self.root), run_id=RUN, digest_context=fixture(self.root))
            if mutation == "project":
                args.digest_context["admitted_root"] = self.root.parent
            else:
                args.digest_context["run_id"] = "another-run"
            with mock.patch.object(run_stages, "ledger_ro", side_effect=AssertionError("must not read")):
                line, code = run_stages.stage_outputs(args)
            self.assertEqual((line["error_code"], code), ("OUTPUTS_DIGEST_REFUSED", 1))
        self.assertEqual(list(self.root.iterdir()), [])

    def test_enabled_snapshot_symlinks_fail_before_any_read(self):
        directory = self.root / "run-outputs"
        directory.mkdir()
        sentinel = self.root / "sentinel"
        sentinel.write_bytes(b"unchanged")
        (directory / ("." + RUN + ".json.tmp")).symlink_to(sentinel)
        args = SimpleNamespace(project=str(self.root), run_id=RUN, digest_context=self.ctx)
        with mock.patch.object(run_stages, "ledger_ro", side_effect=AssertionError("must not read")):
            line, code = run_stages.stage_outputs(args)
        self.assertEqual((line["error_code"], code), ("OUTPUTS_DIGEST_REFUSED", 1))
        self.assertEqual(sentinel.read_bytes(), b"unchanged")
        self.assertFalse(self.ctx["output_root"].exists())

    def test_name_shaped_and_malformed_reasons_are_not_emitted(self):
        result = self.ctx["detector_results"][0]
        entry = result["structured"]["retention-over-policy"]
        entry["reason"] = "officer_jane_doe_named"
        outcome_key = entry["manifest"]["outcomes"][0]["event_key"]
        entry["manifest"]["outcomes"][0]["reason"] = "officer_jane_doe_named"
        request = self.ctx["requests"]["requests"][0]
        request["unresolved_reasons"] = ["requester_john_smith", None, {}, 1]
        self.refresh_requests()
        digest.write_digest(**self.ctx)
        for name in ("digest.json", "digest.md"):
            text = (self.ctx["output_root"] / name).read_text()
            self.assertNotIn("officer_jane_doe_named", text)
            self.assertNotIn("requester_john_smith", text)
        report = json.loads((self.ctx["output_root"] / "digest.json").read_text())
        row = next(r for r in report["detector_statuses"] if r["detector"] == "retention-over-policy")
        self.assertEqual(row["reason"], "unresolved_reason")
        self.assertEqual(next(o for o in row["outcomes"] if o["event_key"] == outcome_key)["reason"],
                         "unresolved_reason")
        self.assertEqual(report["requests"][0]["unresolved_reasons"], ["unresolved_reason"])
        self.assertIsNone(report["requests"][0]["reminders"]["ten_day_candidate"])

    def test_known_parent_reason_vocabulary_preserved_unknowns_minimized(self):
        for reason in ("canonical_copy_changed", "normalization_incomplete",
                       "field_user_id_redacted", "rule_maximum_days_invalid",
                       "supplied_context_evaluated", "rule_not_effective",
                       "thread_order_conflict", "delivery_scope_unverified",
                       "sent_date_missing", "agency_receipt_date_invalid"):
            with self.subTest(reason=reason):
                self.assertEqual(digest.code(reason), reason)
        for reason in ("field_officer_jane_doe_redacted", "rule_requester_john_smith_invalid",
                       "officer_jane_doe_named", "requester_john_smith", None, {}, []):
            with self.subTest(reason=reason):
                self.assertEqual(digest.code(reason), "unresolved_reason")

    def test_request_hash_inventory_requires_list_of_exact_sha256_values(self):
        message_hash = self.ctx["requests"]["requests"][0]["message_sha256s"][0]
        for index, value in enumerate((message_hash, "prefix" + message_hash + "suffix", None,
                      {message_hash: True}, [message_hash, "not-a-sha"], [None], [message_hash.upper()])):
            with self.subTest(value=value):
                self.ctx["output_root"] = self.root / ("hash-shape-" + str(index))
                self.ctx["requests"]["requests"][0]["message_sha256s"] = value
                self.refresh_requests()
                self.refused("request_message_hashes_shape")
        self.ctx["requests"]["requests"][0]["message_sha256s"] = []
        self.refresh_requests()
        row = self.report()["requests"][0]
        self.assertEqual(row["state"], "UNRESOLVED")
        self.assertEqual(row["unresolved_reasons"], ["reminder_citation_unresolved", "request_citation_unresolved"])
        self.assertIsNone(row["reminders"]["ten_day_candidate"])
        self.ctx["requests"]["requests"][0]["message_sha256s"] = [message_hash]
        self.refresh_requests()
        self.assertEqual(self.report()["requests"][0]["state"], "sent")

    def test_request_reason_container_has_typed_refusal_before_writes(self):
        for index, value in enumerate((None, "thread_order_conflict", {}, 1, False)):
            with self.subTest(value=value):
                self.ctx["output_root"] = self.root / ("reason-shape-" + str(index))
                self.ctx["requests"]["requests"][0]["unresolved_reasons"] = value
                self.refresh_requests()
                self.refused("request_unresolved_reasons_shape")

    def test_stage_reserved_output_entries_refuse_before_reads_or_writes(self):
        for name in ("run-outputs", "processing-v1", "portal-queue-v1", "mail"):
            with self.subTest(name=name):
                self.ctx["output_root"] = self.root / name
                args = SimpleNamespace(project=str(self.root), run_id=RUN, digest_context=self.ctx)
                with mock.patch.object(run_stages, "ledger_ro", side_effect=AssertionError("must not read")), \
                     mock.patch.object(run_stages, "snapshot_ro", side_effect=AssertionError("must not read")):
                    line, code = run_stages.stage_outputs(args)
                self.assertEqual((line["error_code"], code), ("OUTPUTS_DIGEST_REFUSED", 1))
                self.assertEqual(list(self.root.iterdir()), [])

    def test_stage_non_dict_context_has_typed_refusal_before_reads_or_writes(self):
        for context_value in ([], "private text", 1, False):
            with self.subTest(context=context_value):
                args = SimpleNamespace(project=str(self.root), run_id=RUN, digest_context=context_value)
                with mock.patch.object(run_stages, "ledger_ro", side_effect=AssertionError("must not read")):
                    line, code = run_stages.stage_outputs(args)
                self.assertEqual((line["error_code"], code), ("OUTPUTS_DIGEST_REFUSED", 1))
                self.assertEqual(list(self.root.iterdir()), [])

    def test_stage_missing_snapshot_never_writes_digest(self):
        args = SimpleNamespace(project=str(self.root), run_id=RUN, digest_context=self.ctx)
        line, code = run_stages.stage_outputs(args)
        self.assertEqual((line["error_code"], code), ("OUTPUTS_LEDGER_UNREADABLE", 1))
        self.assertFalse(self.ctx["output_root"].exists())


if __name__ == "__main__":
    unittest.main()
