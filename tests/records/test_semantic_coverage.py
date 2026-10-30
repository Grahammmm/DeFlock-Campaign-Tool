"""Synthetic ARCH02 coverage accounting; no parsers, records or model service."""

from copy import deepcopy
import hashlib
import json
import unittest

from campaign_tool.records import semantic_coverage as sc


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def unit(number, **metadata):
    return {"unit_id": digest("synthetic-unit-" + str(number)),
            "locator": {"page": number + 1}, **metadata}


class SemanticCoverageTests(unittest.TestCase):
    def manifest(self, inventory=None, texts=None, **options):
        if inventory is None:
            inventory = [unit(0)]
        if texts is None:
            texts = {inventory[0]["unit_id"]: "Synthetic text"}
        settings = dict(source_manifest_sha256=digest("synthetic extraction snapshot"),
                        extraction_version="synthetic-parser-v1",
                        context_sha256=digest("synthetic redaction/model/prompt/acceptance-v1"),
                        inventory_complete=True)
        settings.update(options)
        return sc.build_manifest(digest("synthetic original"), inventory, texts, **settings)

    def accept(self, manifest, checkpoint, chunk, receipt=None, **options):
        settings = dict(visible_ranges=chunk["ranges"], accepted_ranges=chunk["ranges"],
                        receipt_sha256=receipt or digest(chunk["chunk_id"] + " response"))
        settings.update(options)
        return sc.record_attempt(manifest, checkpoint, chunk["chunk_id"], **settings)

    def accept_all(self, manifest):
        checkpoint = sc.initial_checkpoint(manifest)
        for chunk in manifest["chunks"]:
            checkpoint = self.accept(manifest, checkpoint, chunk)
        return checkpoint

    def test_extraction_and_full_emission_accept_nothing(self):
        manifest = self.manifest()
        checkpoint = sc.initial_checkpoint(manifest)
        before = sc.coverage_summary(manifest, checkpoint)
        self.assertEqual((before["selected_units"], before["model_visible_units"], before["accepted_units"]), (1, 0, 0))
        chunk = manifest["chunks"][0]
        emitted = sc.render_chunk(manifest, chunk["chunk_id"], {unit(0)["unit_id"]: "Synthetic text"})
        checkpoint = self.accept(manifest, checkpoint, chunk, visible_ranges=emitted["visible_ranges"], accepted_ranges=[])
        summary = sc.coverage_summary(manifest, checkpoint)
        self.assertEqual((summary["model_visible_units"], summary["accepted_units"]), (1, 0))
        self.assertFalse(summary["all_units_accepted"])
        self.assertFalse(summary["semantic_understanding_established"])
        self.assertEqual(sc.pending_chunks(manifest, checkpoint), [chunk["chunk_id"]])

    def test_full_recorded_acceptance_never_claims_understanding_or_full_text(self):
        manifest = self.manifest()
        checkpoint = self.accept_all(manifest)
        summary = sc.coverage_summary(manifest, checkpoint)
        self.assertTrue(summary["all_units_accepted"])
        self.assertEqual(summary["status"], "accepted_scope_complete")
        self.assertFalse(summary["semantic_understanding_established"])
        self.assertEqual(summary["receipt_coverage"], {"denominator": {"kind": "items", "total": 1},
                                                      "covered": 1, "scope": "selected"})
        self.assertEqual(sc.pending_chunks(manifest, checkpoint), [])

    def test_2001_authoritative_units_do_not_shrink_to_2000_selected(self):
        inventory = [unit(i) for i in range(2001)]
        texts = {row["unit_id"]: "x" for row in inventory[:2000]}
        manifest = self.manifest(iter(inventory), texts)
        summary = sc.coverage_summary(manifest, self.accept_all(manifest))
        self.assertEqual(summary["inventory_total"], 2001)
        self.assertEqual(summary["selected_units"], 2000)
        self.assertEqual(summary["accepted_units"], 2000)
        self.assertEqual(summary["receipt_coverage"]["denominator"]["total"], 2001)
        self.assertFalse(summary["all_units_accepted"])
        self.assertEqual(summary["gaps"][0]["unit_id"], inventory[-1]["unit_id"])

    def test_hidden_and_empty_sheet_metadata_remain_in_denominator(self):
        inventory = [unit(0, unit_type="workbook_sheet", locator={"sheet": "Visible"}),
                     unit(1, unit_type="workbook_sheet", locator={"sheet": "Hidden", "state": "hidden"}, hidden=True),
                     unit(2, unit_type="workbook_row", locator={"sheet": "Hidden", "row": 1}, hidden=True),
                     unit(3, unit_type="workbook_sheet", locator={"sheet": "Secret", "state": "veryHidden"}, hidden=True)]
        texts = {inventory[0]["unit_id"]: "Visible text", inventory[1]["unit_id"]: "",
                 inventory[2]["unit_id"]: "Synthetic hidden row"}
        manifest = self.manifest(inventory, texts)
        summary = sc.coverage_summary(manifest, self.accept_all(manifest))
        self.assertEqual((summary["inventory_total"], summary["accepted_units"]), (4, 2))
        self.assertEqual([u["hidden"] for u in manifest["units"]], [False, True, True, True])
        reasons = {g["unit_id"]: g["reasons"] for g in summary["gaps"]}
        self.assertIn("empty_model_text", reasons[inventory[1]["unit_id"]])
        self.assertIn("unknown_model_text", reasons[inventory[3]["unit_id"]])
        self.assertFalse(summary["all_units_accepted"])

    def test_unreadable_source_unit_is_explicit_gap_not_omitted(self):
        inventory = [unit(0), unit(1, blocked_reason="synthetic malformed derivative")]
        manifest = self.manifest(inventory, {inventory[0]["unit_id"]: "x"})
        summary = sc.coverage_summary(manifest, self.accept_all(manifest))
        self.assertEqual((summary["inventory_total"], summary["accepted_units"]), (2, 1))
        self.assertIn("source_unit_blocked", summary["gaps"][0]["reasons"])
        self.assertEqual(summary["gaps"][0]["blocked_reason"], "synthetic malformed derivative")

    def test_missing_canonical_row_stays_in_denominator_without_invented_locator(self):
        missing = {"unit_id": digest("synthetic missing canonical row"),
                   "locator": None, "blocked_reason": "canonical row unavailable"}
        manifest = self.manifest([unit(0), missing])
        summary = sc.coverage_summary(manifest, self.accept_all(manifest))
        self.assertEqual(summary["inventory_total"], 2)
        self.assertEqual(summary["accepted_units"], 1)
        self.assertIsNone(manifest["units"][1]["locator"])
        self.assertIn("source_unit_blocked", summary["gaps"][0]["reasons"])
        with self.assertRaisesRegex(ValueError, "inventory_locator"):
            self.manifest([{**missing, "blocked_reason": None}], {})

    def test_blocked_metadata_is_not_cleared_by_accepting_available_text(self):
        manifest = self.manifest([unit(0, blocked_reason="source coverage missing")])
        summary = sc.coverage_summary(manifest, self.accept_all(manifest))
        self.assertEqual(summary["accepted_units"], 0)
        self.assertGreater(summary["accepted_chars"], 0)
        self.assertFalse(summary["all_units_accepted"])

    def test_oversized_unit_splits_contiguously_with_exact_serialized_budget(self):
        text = ('Synthetic "record"\n\t\\🙂é漢' * 600)
        texts = {unit(0)["unit_id"]: text}
        manifest = self.manifest(texts=texts, max_chunk_chars=256)
        self.assertGreater(len(manifest["chunks"]), 10)
        rendered_text, ranges = [], []
        for chunk in manifest["chunks"]:
            emitted = sc.render_chunk(manifest, chunk["chunk_id"], texts)
            self.assertLessEqual(len(emitted["payload"]), 256)
            self.assertTrue(emitted["payload"].isascii())
            self.assertEqual(len(emitted["payload"].encode()), chunk["payload_chars"])
            self.assertEqual(digest(emitted["payload"]), emitted["payload_sha256"])
            rows = json.loads(emitted["payload"])["ranges"]
            rendered_text.extend(row["text"] for row in rows)
            ranges.extend(emitted["visible_ranges"])
        self.assertEqual("".join(rendered_text), text)
        self.assertEqual(ranges[0]["start"], 0)
        self.assertEqual(ranges[-1]["end"], len(text))
        self.assertTrue(all(a["end"] == b["start"] for a, b in zip(ranges, ranges[1:])))

    def test_chunk_range_limit_and_order_are_deterministic(self):
        inventory = [unit(i) for i in range(7)]
        texts = {row["unit_id"]: str(i) for i, row in enumerate(inventory)}
        first = self.manifest(inventory, texts, max_chunk_ranges=2)
        second = self.manifest(iter(inventory), dict(reversed(list(texts.items()))), max_chunk_ranges=2)
        self.assertEqual(first, second)
        self.assertEqual([len(c["ranges"]) for c in first["chunks"]], [2, 2, 2, 1])
        self.assertEqual([r["unit_id"] for c in first["chunks"] for r in c["ranges"]], list(texts))

    def test_renderer_binds_full_redacted_view_and_omits_raw_locators(self):
        row = unit(0, locator={"sheet": "Synthetic Private Locator"})
        texts = {row["unit_id"]: "Synthetic [NAME-1]"}
        manifest = self.manifest([row], texts, max_chunk_chars=256)
        chunk = manifest["chunks"][0]
        emitted = sc.render_chunk(manifest, chunk["chunk_id"], texts)
        self.assertNotIn("Private Locator", emitted["payload"])
        self.assertNotIn(row["unit_id"], emitted["payload"])
        self.assertNotIn(manifest["source_sha256"], emitted["payload"])
        self.assertEqual(json.loads(emitted["payload"])["ranges"][0]["unit"], 0)
        self.assertEqual(emitted["unit_references"], [{"unit": 0, "unit_id": row["unit_id"]}])
        self.assertNotIn("text", manifest["units"][0])
        self.assertEqual(chunk["ranges"][0]["end"], len(texts[row["unit_id"]]))
        for wrong in ({}, {row["unit_id"]: "Synthetic [NAME-2]"}):
            with self.subTest(wrong=wrong), self.assertRaisesRegex(ValueError, "selected_text_view_mismatch"):
                sc.render_chunk(manifest, chunk["chunk_id"], wrong)

    def test_renderer_only_requires_views_needed_by_one_chunk(self):
        inventory = [unit(0), unit(1)]
        texts = {row["unit_id"]: "Synthetic" for row in inventory}
        manifest = self.manifest(inventory, texts, max_chunk_ranges=1)
        emitted = sc.render_chunk(manifest, manifest["chunks"][0]["chunk_id"],
                                  {inventory[0]["unit_id"]: "Synthetic"})
        self.assertEqual(len(emitted["visible_ranges"]), 1)

    def test_model_unit_references_are_stable_inventory_indexes_across_chunks(self):
        inventory = [unit(i) for i in range(4)]
        texts = {row["unit_id"]: "Synthetic" for row in inventory[1:]}
        manifest = self.manifest(inventory, texts, max_chunk_ranges=1)
        references = []
        for chunk in manifest["chunks"]:
            emitted = sc.render_chunk(manifest, chunk["chunk_id"], texts)
            references.extend(row["unit"] for row in json.loads(emitted["payload"])["ranges"])
            self.assertTrue(all(identity not in emitted["payload"] for identity in texts))
        self.assertEqual(references, [1, 2, 3])

    def test_omitted_and_truncated_visible_ranges_cannot_accept_whole_unit(self):
        inventory = [unit(i) for i in range(3)]
        texts = {row["unit_id"]: "synthetic" for row in inventory}
        manifest = self.manifest(inventory, texts)
        checkpoint = sc.initial_checkpoint(manifest)
        chunk = manifest["chunks"][0]
        first = chunk["ranges"][:1]
        with self.assertRaisesRegex(ValueError, "accepted_outside_visible"):
            self.accept(manifest, checkpoint, chunk, visible_ranges=first)
        checkpoint = self.accept(manifest, checkpoint, chunk, visible_ranges=first, accepted_ranges=first)
        summary = sc.coverage_summary(manifest, checkpoint)
        self.assertEqual((summary["model_visible_units"], summary["accepted_units"]), (1, 1))
        self.assertEqual(summary["inventory_total"], 3)
        self.assertFalse(summary["all_units_accepted"])
        truncated = [{**chunk["ranges"][1], "end": 3}]
        checkpoint = self.accept(manifest, checkpoint, chunk, digest("truncated response"),
                                 visible_ranges=truncated, accepted_ranges=truncated)
        summary = sc.coverage_summary(manifest, checkpoint)
        self.assertEqual(summary["accepted_units"], 1)
        self.assertEqual(summary["accepted_chars"], len("synthetic") + 3)

    def test_ranges_cannot_cross_other_chunk_or_unknown_unit(self):
        inventory = [unit(0), unit(1)]
        manifest = self.manifest(inventory, {row["unit_id"]: "synthetic" for row in inventory}, max_chunk_ranges=1)
        checkpoint = sc.initial_checkpoint(manifest)
        first, second = manifest["chunks"]
        with self.assertRaisesRegex(ValueError, "visible_outside_chunk"):
            self.accept(manifest, checkpoint, first, visible_ranges=second["ranges"], accepted_ranges=[])
        with self.assertRaisesRegex(ValueError, "range_unit_outside_inventory"):
            self.accept(manifest, checkpoint, first,
                        visible_ranges=[{"unit_id": digest("unknown"), "start": 0, "end": 1}], accepted_ranges=[])

    def test_duplicate_ranges_and_retries_never_inflate_counts(self):
        manifest = self.manifest()
        chunk = manifest["chunks"][0]
        checkpoint = self.accept(manifest, sc.initial_checkpoint(manifest), chunk,
                                 visible_ranges=chunk["ranges"] * 3, accepted_ranges=chunk["ranges"] * 3)
        same = self.accept(manifest, checkpoint, chunk)
        self.assertEqual(checkpoint, same)
        retried = self.accept(manifest, checkpoint, chunk, digest("new successful attempt"))
        summary = sc.coverage_summary(manifest, retried)
        self.assertEqual(summary["accepted_units"], 1)
        self.assertEqual(summary["accepted_chars"], len("Synthetic text"))
        self.assertEqual(len(retried["attempts"]), 2)

    def test_unequal_overlaps_in_one_attempt_are_rejected(self):
        manifest = self.manifest()
        chunk = manifest["chunks"][0]
        overlapping = [{**chunk["ranges"][0], "start": 0, "end": 5},
                       {**chunk["ranges"][0], "start": 4, "end": 8}]
        with self.assertRaisesRegex(ValueError, "overlapping_ranges"):
            self.accept(manifest, sc.initial_checkpoint(manifest), chunk, accepted_ranges=overlapping)

    def test_overlaps_between_valid_attempts_use_interval_union(self):
        manifest = self.manifest()
        chunk = manifest["chunks"][0]
        checkpoint = sc.initial_checkpoint(manifest)
        for index, (start, end) in enumerate(((0, 5), (3, 8), (8, 14))):
            accepted = [{**chunk["ranges"][0], "start": start, "end": end}]
            checkpoint = self.accept(manifest, checkpoint, chunk, digest("attempt" + str(index)), accepted_ranges=accepted)
        summary = sc.coverage_summary(manifest, checkpoint)
        self.assertEqual(summary["accepted_chars"], len("Synthetic text"))
        self.assertEqual(summary["accepted_units"], 1)

    def test_partial_acceptance_exposes_exact_remaining_ranges(self):
        manifest = self.manifest()
        chunk = manifest["chunks"][0]
        checkpoint = self.accept(manifest, sc.initial_checkpoint(manifest), chunk,
                                 accepted_ranges=[{**chunk["ranges"][0], "start": 3, "end": 8}])
        summary = sc.coverage_summary(manifest, checkpoint)
        self.assertEqual([(r["start"], r["end"]) for r in summary["gaps"][0]["unaccepted_ranges"]],
                         [(0, 3), (8, 14)])
        self.assertEqual(summary["accepted_units"], 0)
        self.assertEqual(sc.pending_chunks(manifest, checkpoint), [chunk["chunk_id"]])

    def test_interruption_resumes_only_unaccepted_chunks(self):
        inventory = [unit(i) for i in range(3)]
        manifest = self.manifest(inventory, {row["unit_id"]: "synthetic" for row in inventory}, max_chunk_ranges=1)
        checkpoint = self.accept(manifest, sc.initial_checkpoint(manifest), manifest["chunks"][0])
        checkpoint = self.accept(manifest, checkpoint, manifest["chunks"][1], digest("interrupted emission"),
                                 accepted_ranges=[], completed=False)
        restored = sc.validate_checkpoint(manifest, json.loads(json.dumps(checkpoint)))
        self.assertEqual(sc.pending_chunks(manifest, restored), [c["chunk_id"] for c in manifest["chunks"][1:]])
        self.assertEqual(sc.coverage_summary(manifest, restored)["accepted_units"], 1)
        for chunk in manifest["chunks"][1:]:
            restored = self.accept(manifest, restored, chunk)
        self.assertEqual(sc.pending_chunks(manifest, restored), [])
        self.assertEqual(sc.coverage_summary(manifest, restored)["accepted_units"], 3)

    def test_interrupted_attempt_cannot_accept(self):
        manifest = self.manifest()
        with self.assertRaisesRegex(ValueError, "interrupted_attempt_cannot_accept"):
            self.accept(manifest, sc.initial_checkpoint(manifest), manifest["chunks"][0], completed=False)

    def test_receipt_identity_cannot_be_rebound_to_changed_acceptance(self):
        manifest = self.manifest()
        chunk = manifest["chunks"][0]
        checkpoint = self.accept(manifest, sc.initial_checkpoint(manifest), chunk)
        with self.assertRaisesRegex(ValueError, "receipt_replay_conflict"):
            self.accept(manifest, checkpoint, chunk, accepted_ranges=[])

    def test_checkpoint_rejects_source_selection_version_and_context_drift(self):
        manifest = self.manifest()
        checkpoint = self.accept_all(manifest)
        changes = [dict(source_manifest_sha256=digest("changed extraction snapshot")),
                   dict(extraction_version="synthetic-parser-v2"),
                   dict(context_sha256=digest("changed prompt or model or redaction")),
                   dict(max_chunk_chars=256), dict(inventory_complete=False)]
        changed = [self.manifest(**options) for options in changes]
        changed.append(self.manifest(texts={unit(0)["unit_id"]: "changed redacted text"}))
        changed.append(sc.build_manifest(digest("other original"), [unit(0)],
                                        {unit(0)["unit_id"]: "Synthetic text"},
                                        source_manifest_sha256=manifest["source_manifest_sha256"],
                                        extraction_version=manifest["extraction_version"], context_sha256=manifest["context_sha256"]))
        for other in changed:
            with self.subTest(other=other["manifest_sha256"]), self.assertRaisesRegex(ValueError, "checkpoint_binding"):
                sc.validate_checkpoint(other, checkpoint)

    def test_changed_checkpoint_and_manifest_hashes_are_rejected(self):
        manifest = self.manifest()
        checkpoint = self.accept_all(manifest)
        damaged = deepcopy(checkpoint)
        damaged["attempts"][0]["accepted_ranges"] = []
        with self.assertRaisesRegex(ValueError, "checkpoint_sha256_mismatch"):
            sc.validate_checkpoint(manifest, damaged)
        damaged = deepcopy(manifest)
        damaged["units"][0]["hidden"] = True
        with self.assertRaisesRegex(ValueError, "manifest_sha256_mismatch"):
            sc.initial_checkpoint(damaged)

    def test_unknown_inventory_prevents_complete_even_when_all_known_text_accepted(self):
        manifest = self.manifest(inventory_complete=False)
        summary = sc.coverage_summary(manifest, self.accept_all(manifest))
        self.assertEqual(summary["accepted_units"], 1)
        self.assertFalse(summary["all_units_accepted"])
        self.assertEqual(summary["inventory_gaps"], ["authoritative_inventory_incomplete"])

    def test_inventory_complete_defaults_false(self):
        manifest = sc.build_manifest(digest("source"), [unit(0)], {unit(0)["unit_id"]: "x"},
                                     source_manifest_sha256=digest("snapshot"), extraction_version="synthetic-v1",
                                     context_sha256=digest("context"))
        self.assertFalse(manifest["inventory_complete"])

    def test_empty_inventory_is_not_vacuous_acceptance(self):
        manifest = self.manifest([], {})
        summary = sc.coverage_summary(manifest, sc.initial_checkpoint(manifest))
        self.assertEqual(summary["inventory_total"], 0)
        self.assertFalse(summary["all_units_accepted"])
        self.assertEqual(sc.pending_chunks(manifest, sc.initial_checkpoint(manifest)), [])

    def test_no_pending_chunks_does_not_hide_empty_unselected_or_blocked_units(self):
        inventory = [unit(0), unit(1), unit(2, blocked_reason="synthetic extraction hole")]
        manifest = self.manifest(inventory, {inventory[0]["unit_id"]: ""})
        checkpoint = sc.initial_checkpoint(manifest)
        self.assertEqual(sc.pending_chunks(manifest, checkpoint), [])
        summary = sc.coverage_summary(manifest, checkpoint)
        self.assertEqual(summary["inventory_total"], 3)
        self.assertEqual(len(summary["gaps"]), 3)
        self.assertFalse(summary["all_units_accepted"])

    def test_known_unselected_view_keeps_character_denominator(self):
        row = unit(0, view_chars=4, view_sha256=digest("text"))
        manifest = self.manifest([row], {})
        summary = sc.coverage_summary(manifest, sc.initial_checkpoint(manifest))
        self.assertEqual(summary["known_view_chars"], 4)
        self.assertEqual(summary["unknown_view_units"], 0)
        self.assertEqual(summary["gaps"][0]["unseen_ranges"], [{"unit_id": row["unit_id"], "start": 0, "end": 4}])

    def test_duplicate_inventory_ids_and_unknown_selection_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "duplicate_inventory_unit"):
            self.manifest([unit(0), unit(0)])
        with self.assertRaisesRegex(ValueError, "selected_unit_outside_inventory"):
            self.manifest(texts={digest("unknown unit"): "synthetic"})

    def test_inconsistent_view_metadata_is_rejected(self):
        for row in (unit(0, view_chars=1, view_sha256=digest("x")),
                    unit(0, view_chars=True, view_sha256=digest("Synthetic text")),
                    unit(0, view_chars=14), unit(0, view_sha256=digest("Synthetic text"))):
            with self.subTest(row=row), self.assertRaises(ValueError):
                self.manifest([row])

    def test_invalid_budget_and_range_values_are_rejected(self):
        for options in (dict(max_chunk_chars=255), dict(max_chunk_chars=True),
                        dict(max_chunk_ranges=0), dict(max_chunk_ranges=True)):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.manifest(**options)
        manifest = self.manifest()
        chunk = manifest["chunks"][0]
        for start, end in ((True, 1), (0, True), (-1, 1), (1, 1), (0, 15)):
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                self.accept(manifest, sc.initial_checkpoint(manifest), chunk,
                            accepted_ranges=[{**chunk["ranges"][0], "start": start, "end": end}])

    def test_outputs_do_not_alias_mutable_inputs(self):
        inventory = [unit(0)]
        manifest = self.manifest(inventory)
        inventory[0]["locator"]["page"] = 999
        self.assertEqual(manifest["units"][0]["locator"], {"page": 1})
        original = sc.initial_checkpoint(manifest)
        updated = self.accept(manifest, original, manifest["chunks"][0])
        self.assertEqual(original["attempts"], [])
        restored = sc.validate_checkpoint(manifest, updated)
        restored["attempts"].clear()
        self.assertEqual(len(updated["attempts"]), 1)

    def emit(self, manifest, checkpoint, chunk, receipt, completed=True, **options):
        return sc.record_emission(manifest, checkpoint, chunk["chunk_id"],
            payload_sha256=chunk["payload_sha256"], receipt_sha256=digest(receipt),
            completed=completed, **options)

    def test_mechanical_interrupt_resume_and_union_never_accept(self):
        manifest = self.manifest(texts={unit(0)["unit_id"]: "x" * 400}, max_chunk_chars=256)
        chunks = manifest["chunks"]
        self.assertEqual(len(chunks), 3)
        checkpoint = self.emit(manifest, sc.initial_checkpoint(manifest), chunks[0], "first")
        checkpoint = self.emit(manifest, checkpoint, chunks[1], "interrupted", False)
        checkpoint = sc.validate_checkpoint(manifest, json.loads(json.dumps(checkpoint)))
        self.assertEqual(sc.unemitted_chunks(manifest, checkpoint), [c["chunk_id"] for c in chunks[1:]])
        summary = sc.coverage_summary(manifest, checkpoint)
        self.assertEqual((summary["interrupted_chunks"], summary["emitted_units"]), (1, 0))
        self.assertEqual(summary["unemitted_units"], [unit(0)["unit_id"]])
        for i, chunk in enumerate(chunks[1:]):
            checkpoint = self.emit(manifest, checkpoint, chunk, "finished" + str(i))
        replay = self.emit(manifest, checkpoint, chunks[-1], "finished1")
        self.assertEqual(replay, checkpoint)
        retried = self.emit(manifest, checkpoint, chunks[0], "new receipt")
        self.assertEqual(len(retried["attempts"]), len(checkpoint["attempts"]) + 1)
        for progress in (checkpoint, replay, retried):
            summary = sc.coverage_summary(manifest, progress)
            self.assertEqual((summary["emitted_chars"], summary["planned_chars"], summary["model_visible_chars"]),
                             (400, 400, 400))
            self.assertTrue(summary["emission_complete"])
            self.assertEqual(summary["emitted_units"], 1)
            self.assertEqual((summary["accepted_units"], summary["status"]), (0, "partial"))
            self.assertFalse(summary["semantic_understanding_established"])
            self.assertEqual(sc.pending_chunks(manifest, progress), [c["chunk_id"] for c in chunks])

    def test_emission_refusals_and_bound_are_nonmutating(self):
        manifest = self.manifest()
        chunk = manifest["chunks"][0]
        checkpoint = sc.initial_checkpoint(manifest)
        options = dict(payload_sha256=chunk["payload_sha256"], receipt_sha256=digest("receipt"), completed=False)
        for identity, changes, error, code in [
                (chunk["chunk_id"], {"payload_sha256": digest("wrong")}, ValueError, "emission_payload_mismatch"),
                ("missing", {}, ValueError, "unknown_chunk"), ([], {}, ValueError, "unknown_chunk"),
                (chunk["chunk_id"], {"accepted_ranges": chunk["ranges"]}, TypeError, "accepted_ranges")]:
            before = deepcopy(checkpoint)
            with self.subTest(code=code), self.assertRaisesRegex(error, code):
                sc.record_emission(manifest, checkpoint, identity, **{**options, **changes})
            self.assertEqual(checkpoint, before)
        self.assertEqual(sc.MAX_EMISSIONS_PER_CHUNK, 8)
        for i in range(8):
            checkpoint = self.emit(manifest, checkpoint, chunk, str(i), False)
        before = deepcopy(checkpoint)
        self.assertEqual(self.emit(manifest, checkpoint, chunk, "7", False), checkpoint)
        with self.assertRaisesRegex(ValueError, "emission_attempts_exhausted"):
            self.emit(manifest, checkpoint, chunk, "ninth")
        with self.assertRaisesRegex(ValueError, "receipt_replay_conflict"):
            self.emit(manifest, checkpoint, chunk, "7", True)
        self.assertEqual(checkpoint, before)
        self.assertEqual(sc.unemitted_chunks(manifest, checkpoint), [chunk["chunk_id"]])
        # The bound does not change historical checkpoint validation or acceptance.
        old = self.accept(manifest, checkpoint, chunk, digest("legacy ninth"))
        self.assertEqual(sc.validate_checkpoint(manifest, old), old)

    def test_emission_keeps_inventory_gaps_and_legacy_summary(self):
        inventory = [unit(0), unit(1, view_chars=4, view_sha256=digest("text")),
                     unit(2, locator=None, blocked_reason="synthetic missing"),
                     unit(3), unit(4), unit(5, hidden=True)]
        manifest = self.manifest(inventory, {unit(0)["unit_id"]: "visible", unit(3)["unit_id"]: ""})
        checkpoint = self.emit(manifest, sc.initial_checkpoint(manifest), manifest["chunks"][0], "only")
        summary = sc.coverage_summary(manifest, checkpoint)
        self.assertTrue(summary["emission_complete"])
        self.assertEqual((summary["inventory_total"], summary["hidden_units"]), (6, 1))
        reasons = {r for gap in summary["gaps"] for r in gap["reasons"]}
        self.assertTrue({"unit_not_selected", "source_unit_blocked", "empty_model_text",
                         "unknown_model_text", "text_not_accepted"} <= reasons)
        legacy = self.accept(manifest, sc.initial_checkpoint(manifest), manifest["chunks"][0])
        summary = sc.coverage_summary(manifest, legacy)
        self.assertEqual((summary["status"], summary["accepted_units"], summary["accepted_chars"],
                          summary["model_visible_units"], summary["model_visible_chars"]), ("partial", 1, 7, 1, 7))
        self.assertEqual(summary["gaps"][0], {"unit_id": unit(1)["unit_id"],
            "reasons": ["unit_not_selected", "text_not_visible", "text_not_accepted"], "blocked_reason": None,
            "unseen_ranges": [{"unit_id": unit(1)["unit_id"], "start": 0, "end": 4}],
            "unaccepted_ranges": [{"unit_id": unit(1)["unit_id"], "start": 0, "end": 4}]})
        empty = self.manifest([], {})
        self.assertFalse(sc.coverage_summary(empty, sc.initial_checkpoint(empty))["emission_complete"])


if __name__ == "__main__":
    unittest.main()
