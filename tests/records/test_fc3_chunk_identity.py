"""FC3-CHUNK-IDENTITY: typed refusals for malformed semantic-coverage chunk IDs.

Synthetic, redacted, offline fixtures only. Every identity is a SHA-256 of a
labelled fixture string; no record, mailbox, network or model is touched.

Named case coverage (each test method name starts with ``test_<case>``):
valid_render_and_record, direct_ids_wrong_types, restored_ids_wrong_types,
missing_and_unknown_ids, correct_self_hash_with_bad_id,
unchanged_inputs_on_refusal, exact_receipt_retry, bookkeeping_not_understanding.

Restored malformed rows recompute only the documented accidental-drift
checkpoint self-hash so the chunk-identity branch is actually reached. That
models accidental drift; it is not authentication and does not authorize
forged receipts.
"""

from copy import deepcopy
import hashlib
import json
import unittest

from campaign_tool.records import semantic_coverage as sc


CASES = ("valid_render_and_record", "direct_ids_wrong_types", "restored_ids_wrong_types",
         "missing_and_unknown_ids", "correct_self_hash_with_bad_id",
         "unchanged_inputs_on_refusal", "exact_receipt_retry", "bookkeeping_not_understanding")

LABEL = "fc3-chunk-identity:"


def label_digest(text):
    """SHA-256 of a labelled fixture string (no literal identities in source)."""
    return hashlib.sha256((LABEL + text).encode("utf-8")).hexdigest()


def canonical_bytes(value):
    """Documented canonical JSON form used by the accidental-drift self-hash."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("utf-8")


def resign_checkpoint(checkpoint):
    """Recompute only the accidental-drift self-hash of a restored checkpoint."""
    body = {k: v for k, v in checkpoint.items() if k != "checkpoint_sha256"}
    result = deepcopy(body)
    result["checkpoint_sha256"] = hashlib.sha256(canonical_bytes(body)).hexdigest()
    return result


ALPHA_TEXT = "FC3 synthetic redacted unit alpha sentence. " * 14
BETA_TEXT = "FC3 synthetic redacted unit beta."


def fixture():
    """Return (manifest, selected_text) for a multi-chunk synthetic inventory."""
    alpha, beta, gamma = (label_digest("unit-" + name) for name in ("alpha", "beta", "gamma"))
    inventory = [
        {"unit_id": alpha, "locator": {"page": 1}, "unit_type": "page"},
        {"unit_id": beta, "locator": {"page": 2}, "unit_type": "page"},
        {"unit_id": gamma, "locator": None, "blocked_reason": "synthetic unreadable unit"},
    ]
    texts = {alpha: ALPHA_TEXT, beta: BETA_TEXT}
    manifest = sc.build_manifest(
        label_digest("original"), inventory, texts,
        source_manifest_sha256=label_digest("extraction-snapshot"),
        extraction_version="fc3-synthetic-parser-v1",
        context_sha256=label_digest("redaction/model/prompt/acceptance-v1"),
        inventory_complete=True, max_chunk_chars=256, max_chunk_ranges=4)
    return manifest, texts


def receipt(name):
    return label_digest("receipt-" + name)


def accept(manifest, checkpoint, chunk, name, *, completed=True, accepted=None):
    ranges = chunk["ranges"]
    return sc.record_attempt(manifest, checkpoint, chunk["chunk_id"], visible_ranges=ranges,
                             accepted_ranges=ranges if accepted is None else accepted,
                             receipt_sha256=receipt(name), completed=completed)


def wrong_type_ids(real_id):
    """Every non-string identity class, with nested variants of the real ID.

    Unhashable entries are the ones that previously escaped as TypeError;
    hashable ones must keep (and are checked to keep) the same typed refusal.
    """
    return [
        ("none", None), ("bool_true", True), ("bool_false", False),
        ("int_zero", 0), ("int_ordinal_one", 1), ("float", 1.5),
        ("list_empty", []), ("list_of_real", [real_id]), ("list_nested", [[real_id]]),
        ("list_pair", [real_id, real_id]),
        ("dict_empty", {}), ("dict_chunk_id", {"chunk_id": real_id}),
        ("dict_nested", {"id": {"chunk_id": real_id}}), ("dict_key_real", {real_id: True}),
        ("tuple_of_real", (real_id,)), ("bytes_of_real", real_id.encode("ascii")),
        ("bytearray_of_real", bytearray(real_id.encode("ascii"))),
        ("set_of_real", {real_id}), ("frozenset_of_real", frozenset({real_id})),
    ]


def json_wrong_type_ids(real_id):
    """The subset representable in restored JSON checkpoints."""
    return [(name, value) for name, value in wrong_type_ids(real_id)
            if isinstance(value, (type(None), bool, int, float, list, dict))]


def unknown_string_ids(manifest):
    real = manifest["chunks"][0]["chunk_id"]
    other_manifest, _ = fixture_other()
    return [
        ("empty", ""), ("labelled_unknown", label_digest("unknown-chunk")),
        ("uppercase_real", real.upper()), ("real_with_space", real + " "),
        ("truncated_real", real[:-1]), ("ordinal_text", "0"),
        ("manifest_hash", manifest["manifest_sha256"]),
        ("other_manifest_chunk", other_manifest["chunks"][0]["chunk_id"]),
    ]


def fixture_other():
    other = label_digest("unit-other")
    texts = {other: "FC3 synthetic redacted other manifest unit."}
    manifest = sc.build_manifest(
        label_digest("other-original"), [{"unit_id": other, "locator": "other"}], texts,
        source_manifest_sha256=label_digest("other-snapshot"), extraction_version="fc3-other-v1",
        context_sha256=label_digest("other-context"), inventory_complete=True)
    return manifest, texts


def replay_bookkeeping():
    """Deterministic completed/interrupted replay bookkeeping snapshot.

    Used by tests and by the acceptance runner's negative control to show the
    identity-guard bypass does not alter unrelated replay bookkeeping.
    """
    manifest, texts = fixture()
    first, second = manifest["chunks"][0], manifest["chunks"][1]
    checkpoint = sc.initial_checkpoint(manifest)
    interrupted = accept(manifest, checkpoint, first, "interrupted", completed=False, accepted=[])
    interrupted_again = accept(manifest, interrupted, first, "interrupted", completed=False, accepted=[])
    completed = accept(manifest, interrupted_again, first, "completed")
    completed_again = accept(manifest, completed, first, "completed")
    partial = accept(manifest, completed_again, second, "second-visible-only", accepted=[])
    return {
        "manifest_sha256": manifest["manifest_sha256"],
        "interrupted_checkpoint": interrupted["checkpoint_sha256"],
        "interrupted_replay_identical": interrupted_again == interrupted,
        "completed_checkpoint": completed["checkpoint_sha256"],
        "completed_replay_identical": completed_again == completed,
        "attempts": len(partial["attempts"]),
        "pending": sc.pending_chunks(manifest, partial),
        "summary": sc.coverage_summary(manifest, partial),
        "render_payload_sha256": sc.render_chunk(manifest, first["chunk_id"], texts)["payload_sha256"],
    }


class ChunkIdentityTests(unittest.TestCase):
    def setUp(self):
        self.manifest, self.texts = fixture()
        self.assertGreaterEqual(len(self.manifest["chunks"]), 3, "fixture must span several chunks")
        self.first = self.manifest["chunks"][0]
        self.real_id = self.first["chunk_id"]

    def assertRefusal(self, reason, call, *args, **kwargs):
        """Require exactly ValueError(reason); any other exception is an error."""
        with self.assertRaises(ValueError) as caught:
            call(*args, **kwargs)
        self.assertEqual(caught.exception.args, (reason,))

    def record(self, checkpoint, chunk_id, name="direct"):
        return sc.record_attempt(self.manifest, checkpoint, chunk_id,
                                 visible_ranges=self.first["ranges"],
                                 accepted_ranges=self.first["ranges"],
                                 receipt_sha256=receipt(name))

    def restored_with_chunk_id(self, value, *, drop=False):
        checkpoint = accept(self.manifest, sc.initial_checkpoint(self.manifest), self.first, "restored")
        tampered = deepcopy(checkpoint)
        if drop:
            del tampered["attempts"][0]["chunk_id"]
        else:
            tampered["attempts"][0]["chunk_id"] = value
        return resign_checkpoint(tampered)

    # valid_render_and_record -------------------------------------------------
    def test_valid_render_and_record(self):
        rendered = sc.render_chunk(self.manifest, self.real_id, self.texts)
        self.assertEqual(rendered["payload_sha256"], self.first["payload_sha256"])
        self.assertEqual(len(rendered["payload"]), self.first["payload_chars"])
        self.assertLessEqual(len(rendered["payload"]), self.manifest["max_chunk_chars"])
        self.assertEqual(rendered["visible_ranges"], self.first["ranges"])
        checkpoint = sc.initial_checkpoint(self.manifest)
        for index, chunk in enumerate(self.manifest["chunks"]):
            shown = sc.render_chunk(self.manifest, chunk["chunk_id"], self.texts)
            checkpoint = sc.record_attempt(self.manifest, checkpoint, chunk["chunk_id"],
                                           visible_ranges=shown["visible_ranges"],
                                           accepted_ranges=shown["visible_ranges"],
                                           receipt_sha256=receipt("valid-" + str(index)))
        self.assertEqual(sc.validate_checkpoint(self.manifest, checkpoint), checkpoint)
        self.assertEqual([a["chunk_id"] for a in checkpoint["attempts"]],
                         [c["chunk_id"] for c in self.manifest["chunks"]])
        self.assertEqual(sc.pending_chunks(self.manifest, checkpoint), [])

    # direct_ids_wrong_types --------------------------------------------------
    def test_direct_ids_wrong_types(self):
        checkpoint = sc.initial_checkpoint(self.manifest)
        for name, value in wrong_type_ids(self.real_id):
            with self.subTest(path="render_chunk", variant=name):
                self.assertRefusal("unknown_chunk", sc.render_chunk, self.manifest, value, self.texts)
            with self.subTest(path="record_attempt", variant=name):
                self.assertRefusal("unknown_chunk", self.record, checkpoint, value)

    # restored_ids_wrong_types ------------------------------------------------
    def test_restored_ids_wrong_types(self):
        good = sc.initial_checkpoint(self.manifest)
        for name, value in json_wrong_type_ids(self.real_id):
            restored = self.restored_with_chunk_id(value)
            for path, call, args in (
                    ("validate_checkpoint", sc.validate_checkpoint, (self.manifest, restored)),
                    ("record_attempt", self.record, (restored, self.real_id, "after-restore")),
                    ("pending_chunks", sc.pending_chunks, (self.manifest, restored)),
                    ("coverage_summary", sc.coverage_summary, (self.manifest, restored))):
                with self.subTest(path=path, variant=name):
                    self.assertRefusal("checkpoint_chunk", call, *args)
            with self.subTest(path="valid_checkpoint_still_records", variant=name):
                self.assertEqual(len(self.record(good, self.real_id)["attempts"]), 1)

    # missing_and_unknown_ids -------------------------------------------------
    def test_missing_and_unknown_ids(self):
        checkpoint = sc.initial_checkpoint(self.manifest)
        for name, value in unknown_string_ids(self.manifest):
            with self.subTest(path="render_chunk", variant=name):
                self.assertRefusal("unknown_chunk", sc.render_chunk, self.manifest, value, self.texts)
            with self.subTest(path="record_attempt", variant=name):
                self.assertRefusal("unknown_chunk", self.record, checkpoint, value)
            with self.subTest(path="restored", variant=name):
                self.assertRefusal("checkpoint_chunk", sc.validate_checkpoint, self.manifest,
                                   self.restored_with_chunk_id(value))
        with self.subTest(path="restored", variant="missing_key"):
            restored = self.restored_with_chunk_id(None, drop=True)
            self.assertNotIn("chunk_id", restored["attempts"][0])
            self.assertRefusal("checkpoint_chunk", sc.validate_checkpoint, self.manifest, restored)
            self.assertRefusal("checkpoint_chunk", self.record, restored, self.real_id)

    # correct_self_hash_with_bad_id -------------------------------------------
    def test_correct_self_hash_with_bad_id(self):
        checkpoint = accept(self.manifest, sc.initial_checkpoint(self.manifest), self.first, "self-hash")
        # The documented recomputation reproduces a genuine checkpoint exactly.
        self.assertEqual(resign_checkpoint(checkpoint), checkpoint)
        for name, value in json_wrong_type_ids(self.real_id):
            tampered = deepcopy(checkpoint)
            tampered["attempts"][0]["chunk_id"] = value
            with self.subTest(stage="stale_self_hash", variant=name):
                self.assertRefusal("checkpoint_sha256_mismatch", sc.validate_checkpoint,
                                   self.manifest, tampered)
            resigned = resign_checkpoint(tampered)
            with self.subTest(stage="resigned_reaches_identity_branch", variant=name):
                self.assertNotEqual(resigned["checkpoint_sha256"], checkpoint["checkpoint_sha256"])
                self.assertRefusal("checkpoint_chunk", sc.validate_checkpoint, self.manifest, resigned)

    # unchanged_inputs_on_refusal ---------------------------------------------
    def test_unchanged_inputs_on_refusal(self):
        checkpoint = accept(self.manifest, sc.initial_checkpoint(self.manifest), self.first, "kept")
        manifest_bytes, checkpoint_bytes = canonical_bytes(self.manifest), canonical_bytes(checkpoint)
        texts_bytes = canonical_bytes(self.texts)
        for name, value in wrong_type_ids(self.real_id) + unknown_string_ids(self.manifest):
            value_before = repr(value)
            with self.subTest(path="render_chunk", variant=name):
                self.assertRefusal("unknown_chunk", sc.render_chunk, self.manifest, value, self.texts)
            with self.subTest(path="record_attempt", variant=name):
                ranges = deepcopy(self.first["ranges"])
                ranges_bytes = canonical_bytes(ranges)
                self.assertRefusal("unknown_chunk", sc.record_attempt, self.manifest, checkpoint, value,
                                   visible_ranges=ranges, accepted_ranges=ranges,
                                   receipt_sha256=receipt("refused"))
                self.assertEqual(canonical_bytes(ranges), ranges_bytes)
            with self.subTest(path="identity_value", variant=name):
                self.assertEqual(repr(value), value_before)
        for name, value in json_wrong_type_ids(self.real_id):
            restored = self.restored_with_chunk_id(value)
            restored_bytes = canonical_bytes(restored)
            with self.subTest(path="restored", variant=name):
                self.assertRefusal("checkpoint_chunk", sc.validate_checkpoint, self.manifest, restored)
                self.assertRefusal("checkpoint_chunk", self.record, restored, self.real_id)
                self.assertEqual(canonical_bytes(restored), restored_bytes)
        self.assertEqual(canonical_bytes(self.manifest), manifest_bytes)
        self.assertEqual(canonical_bytes(checkpoint), checkpoint_bytes)
        self.assertEqual(canonical_bytes(self.texts), texts_bytes)
        # The previously returned checkpoint still validates and is not reset.
        self.assertEqual(sc.validate_checkpoint(self.manifest, checkpoint), checkpoint)
        self.assertEqual(len(checkpoint["attempts"]), 1)

    # exact_receipt_retry -----------------------------------------------------
    def test_exact_receipt_retry(self):
        first, second = self.manifest["chunks"][0], self.manifest["chunks"][1]
        checkpoint = sc.initial_checkpoint(self.manifest)
        interrupted = accept(self.manifest, checkpoint, first, "retry-interrupted", completed=False, accepted=[])
        self.assertEqual(accept(self.manifest, interrupted, first, "retry-interrupted",
                                completed=False, accepted=[]), interrupted)
        completed = accept(self.manifest, interrupted, first, "retry-completed")
        # A malformed identity refusal between retries changes nothing.
        for name, value in wrong_type_ids(first["chunk_id"]):
            with self.subTest(path="refusal_between_retries", variant=name):
                self.assertRefusal("unknown_chunk", sc.record_attempt, self.manifest, completed, value,
                                   visible_ranges=first["ranges"], accepted_ranges=first["ranges"],
                                   receipt_sha256=receipt("retry-completed"))
        self.assertEqual(accept(self.manifest, completed, first, "retry-completed"), completed)
        self.assertEqual(len(completed["attempts"]), 2)
        self.assertRefusal("receipt_replay_conflict", accept, self.manifest, completed, second, "retry-completed")
        snapshot = replay_bookkeeping()
        self.assertTrue(snapshot["interrupted_replay_identical"])
        self.assertTrue(snapshot["completed_replay_identical"])
        self.assertEqual(snapshot["attempts"], 3)
        self.assertEqual(snapshot, replay_bookkeeping())

    # bookkeeping_not_understanding -------------------------------------------
    def test_bookkeeping_not_understanding(self):
        checkpoint = sc.initial_checkpoint(self.manifest)
        for index, chunk in enumerate(self.manifest["chunks"]):
            checkpoint = accept(self.manifest, checkpoint, chunk, "all-" + str(index))
        before = sc.coverage_summary(self.manifest, checkpoint)
        for name, value in wrong_type_ids(self.real_id):
            with self.subTest(variant=name):
                self.assertRefusal("unknown_chunk", self.record, checkpoint, value)
        after = sc.coverage_summary(self.manifest, checkpoint)
        self.assertEqual(after, before)
        self.assertFalse(after["semantic_understanding_established"])
        self.assertEqual(after["receipt_coverage"]["scope"], "selected")
        self.assertEqual(after["inventory_total"], 3)
        self.assertEqual(after["accepted_units"], 2)
        self.assertFalse(after["all_units_accepted"])
        self.assertEqual(after["status"], "partial")
        self.assertEqual([g["reasons"] for g in after["gaps"]],
                         [["source_unit_blocked", "unit_not_selected", "unknown_model_text"]])


if __name__ == "__main__":
    unittest.main()
