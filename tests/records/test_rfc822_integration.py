"""Synthetic wire inventory, shared budgets and receipt provenance only."""
import base64
from dataclasses import FrozenInstanceError, replace
from email import policy
from email.parser import BytesParser
import hashlib
import unittest
from unittest.mock import patch

from campaign_tool.records.intake import rfc822_inventory as inventory
from campaign_tool.records.intake import rfc822_adapter as adapter


def eml(body=b"synthetic body", eol=b"\r\n"):
    return eol.join((b"Subject: " + b"synthetic " * 12,
                     b"\t original fold " + b"example " * 10, b"", body))


def rfc(payload, named=False, eol=b"\r\n"):
    headers = b"Content-Type: message/rfc822" + eol
    if named:
        headers += b"Content-Disposition: attachment; filename=synthetic.eml" + eol
    return headers + eol + payload


def multi(children, boundary=b"synthetic-outer", eol=b"\r\n", subtype=b"mixed"):
    raw = b'Content-Type: multipart/' + subtype + b'; boundary="' + boundary + b'"' + eol + eol
    for child in children:
        raw += b"--" + boundary + eol + child + eol
    return raw + b"--" + boundary + b"--" + eol


def complete(raw, roles=None, **limits):
    plan = adapter.prepare_rfc822_intake(raw, limits=inventory.RFC822Limits(**limits))
    choices = {part.part: "body" for part in plan.inventory.scalar_parts} if roles is None else roles
    return adapter.classify_scalar_parts(plan, choices)


def digest(data):
    return hashlib.sha256(data).hexdigest()


class RFC822IntegrationTests(unittest.TestCase):
    def reject(self, action, code):
        with self.assertRaises((inventory.RFC822InventoryError, adapter.RFC822AdapterError)) as caught:
            action()
        self.assertEqual(str(caught.exception), code)
        self.assertIn(code, adapter.RFC822_FAILURE_CODES)

    def nested(self):
        grandchild = eml(b"synthetic final\n", b"\n")
        child = b"Subject: synthetic containing message\r\n" + multi(
            [rfc(grandchild, named=True),
             b"Content-Type: application/octet-stream\r\nContent-Disposition: attachment; filename=synthetic.bin\r\n\r\nsynthetic file"],
            boundary=b"synthetic-inner")
        return multi([rfc(child)]), child, grandchild

    def test_named_unnamed_nested_capture_exact_bytes_and_parent_chain(self):
        raw, child, grandchild = self.nested()
        plan = adapter.prepare_rfc822_intake(raw)
        first, second = plan.captures
        self.assertEqual((first.payload, second.payload), (child, grandchild))
        self.assertEqual(first.original_filename, None)
        self.assertEqual(second.original_filename, "synthetic.eml")
        self.assertEqual(first.mime_chain, ("1.1",))
        self.assertEqual(second.mime_chain, ("1.1", "1.1"))
        self.assertEqual((first.parent_part, first.parent_sha256), ("0", digest(raw)))
        self.assertEqual((second.parent_part, second.parent_sha256), (first.part, digest(child)))
        self.assertFalse(plan.complete)
        self.assertEqual(len(plan.inventory.sources), 3)
        self.assertEqual(len(plan.inventory.scalar_parts), 2)
        for capture in plan.captures:
            parent = next(source for source in plan.inventory.sources if source.part == capture.parent_part)
            self.assertEqual(parent.raw_bytes[capture.wire_start:capture.wire_end], capture.payload)
            self.assertEqual(capture.sha256, digest(capture.payload))
            self.assertNotEqual(BytesParser(policy=policy.default).parsebytes(capture.payload).as_bytes(
                policy=policy.default.clone(refold_source="all", max_line_length=40)), capture.payload)

    def test_nested_scalar_capture_uses_immediate_contained_message_parent(self):
        raw, child, grandchild = self.nested()
        initial = adapter.prepare_rfc822_intake(raw)
        roles = {part.part: ("attachment" if part.original_filename else "body")
                 for part in initial.inventory.scalar_parts}
        plan = adapter.classify_scalar_parts(initial, roles)
        self.assertTrue(plan.complete)
        scalar = next(capture for capture in plan.captures if capture.kind == "mime")
        self.assertEqual(scalar.payload, b"synthetic file")
        self.assertEqual(scalar.parent_part, plan.captures[0].part)
        self.assertEqual(scalar.parent_sha256, digest(child))
        self.assertEqual(scalar.mime_chain, ("1.1", "1.2"))
        self.assertEqual(plan.usage.total_bytes, len(raw) + len(child) + len(grandchild) + len(scalar.payload))
        self.assertEqual(adapter.bind_rfc822_receipts(plan, list(plan.receipts)), plan.captures)

    def test_crlf_lf_and_terminal_newlines_preserved(self):
        for outer in (b"\n", b"\r\n"):
            for inner in (b"\n", b"\r\n"):
                for suffix in (b"", inner, inner * 2):
                    with self.subTest(outer=outer, inner=inner, suffix=suffix):
                        payload = eml(b"synthetic" + suffix, inner)
                        plan = complete(multi([rfc(payload, eol=outer)], eol=outer))
                        self.assertEqual(plan.captures[0].payload, payload)

    def test_root_rfc822_transition_counts_root_and_child(self):
        payload = eml()
        plan = complete(rfc(payload), max_depth=2, max_parts=2)
        self.assertEqual(plan.captures[0].mime_chain, ("1",))
        self.assertEqual(plan.usage.mime_parts, 2)
        self.assertEqual(plan.usage.max_depth, 2)
        self.reject(lambda: complete(rfc(payload), max_depth=1), "rfc822_depth_limit")
        self.reject(lambda: complete(rfc(payload), max_parts=1), "rfc822_part_limit")

    def test_digest_unnamed_capture_inherits_rfc822(self):
        payload = eml()
        plan = complete(multi([b"\r\n" + payload], subtype=b"digest"))
        self.assertEqual(plan.captures[0].payload, payload)
        self.assertEqual(plan.captures[0].original_filename, None)
        self.assertEqual(plan.receipts[0]["format"], "eml")
        self.assertEqual(plan.receipts[0]["filename"], "attachment.bin")

    def test_duplicate_content_retains_distinct_occurrences(self):
        payload = eml()
        raw = multi([rfc(payload), rfc(payload)])
        plan = complete(raw)
        self.assertEqual(len(plan.captures), 2)
        self.assertEqual(plan.captures[0].sha256, plan.captures[1].sha256)
        self.assertNotEqual(plan.captures[0].part, plan.captures[1].part)
        self.assertEqual(plan.usage.total_bytes, len(raw) + 2 * len(payload))
        self.assertEqual(len({source.part for source in plan.inventory.sources}), 3)

    def test_duplicates_in_different_parents_do_not_collapse_edges(self):
        leaf = eml()
        child = b"Subject: synthetic child\r\n" + multi([rfc(leaf)], boundary=b"synthetic-inner")
        plan = complete(multi([rfc(child), rfc(child)]))
        self.assertEqual(len(plan.captures), 4)
        leaf_captures = [capture for capture in plan.captures if capture.payload == leaf]
        self.assertEqual(len(leaf_captures), 2)
        self.assertNotEqual(leaf_captures[0].parent_part, leaf_captures[1].parent_part)
        self.assertEqual(leaf_captures[0].parent_sha256, leaf_captures[1].parent_sha256)

    def test_shared_source_byte_budget_cannot_reset_inside_messages(self):
        raw, child, grandchild = self.nested()
        total = len(raw) + len(child) + len(grandchild)
        self.assertEqual(complete(raw, max_total_bytes=total).usage.total_bytes, total)
        self.reject(lambda: complete(raw, max_total_bytes=total - 1), "rfc822_total_byte_limit")
        self.reject(lambda: complete(raw, max_message_bytes=len(raw) - 1), "rfc822_message_byte_limit")

    def test_shared_depth_across_rfc822_and_multipart(self):
        raw = rfc(b"Subject: synthetic nested wrapper\r\n" + rfc(eml()))
        self.assertEqual(complete(raw, max_depth=3).usage.max_depth, 3)
        self.reject(lambda: complete(raw, max_depth=2), "rfc822_depth_limit")
        wrapped = multi([raw])
        self.assertEqual(complete(wrapped, max_depth=4).usage.max_depth, 4)
        self.reject(lambda: complete(wrapped, max_depth=3), "rfc822_depth_limit")

    def test_shared_parts_and_capture_count(self):
        raw = multi([rfc(eml()), rfc(eml())])
        self.assertEqual(complete(raw, max_parts=5, max_captures=2).usage.mime_parts, 5)
        self.reject(lambda: complete(raw, max_parts=4), "rfc822_part_limit")
        self.reject(lambda: complete(raw, max_captures=1), "rfc822_capture_limit")

    def test_opaque_capture_phase_rejects_malformed_contained_mime_on_inventory(self):
        invalid = b"Subject: synthetic\r\nContent-Type: multipart/mixed; boundary=missing\r\n\r\nsynthetic"
        self.reject(lambda: complete(rfc(invalid)), "rfc822_wire_rejected")

    def test_rfc822_base64_never_falls_back_to_reserialization(self):
        raw = b"Content-Transfer-Encoding: base64\r\n" + rfc(eml())
        self.reject(lambda: complete(raw), "rfc822_wire_rejected")

    def test_scalar_classification_is_exhaustive_and_rfc822_cannot_be_exempted(self):
        plan = adapter.prepare_rfc822_intake(multi([rfc(eml())]))
        self.assertFalse(plan.as_manifest()["complete"])
        self.reject(lambda: adapter.classify_scalar_parts(plan, {}), "rfc822_invalid_scalar_classification")
        roles = {part.part: "body" for part in plan.inventory.scalar_parts}
        roles[plan.captures[0].part] = "body"
        self.reject(lambda: adapter.classify_scalar_parts(plan, roles), "rfc822_invalid_scalar_classification")
        self.reject(lambda: adapter.bind_rfc822_receipts(plan, list(plan.receipts)), "rfc822_incomplete_inventory")

    def scalar(self, body, encoding=b"binary"):
        raw = b"Subject: synthetic scalar\r\nContent-Type: application/octet-stream\r\n"
        raw += b"Content-Transfer-Encoding: " + encoding + b"\r\nContent-Disposition: attachment; filename=synthetic.bin\r\n\r\n" + body
        plan = adapter.prepare_rfc822_intake(raw)
        return adapter.classify_scalar_parts(plan, {part.part: "attachment" for part in plan.inventory.scalar_parts})

    def test_scalar_identity_base64_and_quoted_printable_exact_decoding(self):
        payload = b"synthetic\x00\xff\r\nbytes"
        self.assertEqual(self.scalar(payload).captures[0].payload, payload)
        encoded = base64.b64encode(payload)
        self.assertEqual(self.scalar(encoded[:8] + b"\r\n" + encoded[8:], b"base64").captures[0].payload, payload)
        self.assertEqual(self.scalar(b"synthetic=00=FF\r\nby=\r\ntes", b"quoted-printable").captures[0].payload, payload)
        self.assertEqual(self.scalar(b"synthetic=\nbody\n", b"quoted-printable").captures[0].payload, b"syntheticbody\n")

    def test_strict_scalar_decoding_rejects_tolerant_or_noncanonical_inputs(self):
        for encoding, body in ((b"base64", b"Zh=="), (b"base64", b"Zg==junk"),
                               (b"base64", b"Zg==!"), (b"quoted-printable", b"bad=ZZ"),
                               (b"quoted-printable", b"bad="), (b"quoted-printable", b"bad \r\n"),
                               (b"quoted-printable", b"bare\rcr"), (b"quoted-printable", b"\xff")):
            with self.subTest(encoding=encoding, body=body):
                self.reject(lambda: self.scalar(body, encoding), "rfc822_scalar_payload_invalid")

    def test_scalar_outputs_share_rfc822_byte_and_capture_budgets(self):
        raw, child, grandchild = self.nested()
        initial = adapter.prepare_rfc822_intake(raw)
        roles = {part.part: ("attachment" if part.original_filename else "body")
                 for part in initial.inventory.scalar_parts}
        limited = adapter.prepare_rfc822_intake(raw, limits=inventory.RFC822Limits(
            max_total_bytes=initial.usage.total_bytes + len(b"synthetic file") - 1))
        self.reject(lambda: adapter.classify_scalar_parts(limited, roles), "rfc822_total_byte_limit")
        limited = adapter.prepare_rfc822_intake(raw, limits=inventory.RFC822Limits(max_captures=2))
        self.reject(lambda: adapter.classify_scalar_parts(limited, roles), "rfc822_capture_limit")

    def test_reclassification_does_not_double_charge_or_mutate_prior_plan(self):
        initial = adapter.prepare_rfc822_intake(rfc(eml()))
        roles = {part.part: "attachment" for part in initial.inventory.scalar_parts}
        first = adapter.classify_scalar_parts(initial, roles)
        second = adapter.classify_scalar_parts(first, roles)
        self.assertEqual(first, second)
        self.assertFalse(initial.complete)
        with self.assertRaises(FrozenInstanceError):
            first.complete = False

    def test_body_roles_leave_body_bytes_uncaptured_but_explicit(self):
        plan = complete(rfc(eml()))
        self.assertTrue(plan.complete)
        self.assertEqual(len(plan.captures), 1)
        self.assertTrue(all(role == "body" for _, role in plan.scalar_roles))
        self.assertEqual(len(plan.receipts), 1)

    def test_receipt_binding_missing_extra_duplicate_and_reordered(self):
        plan = complete(multi([rfc(eml()), rfc(eml())]))
        records = list(plan.receipts)
        self.assertEqual(adapter.bind_rfc822_receipts(plan, records[::-1]), plan.captures)
        self.reject(lambda: adapter.bind_rfc822_receipts(plan, records[:1]), "rfc822_missing_receipt")
        duplicate = [records[0], records[0]]
        self.reject(lambda: adapter.bind_rfc822_receipts(plan, duplicate), "rfc822_duplicate_receipt")
        extra = [dict(records[0], part="rfc822:1.99")]
        self.reject(lambda: adapter.bind_rfc822_receipts(plan, extra), "rfc822_unlisted_receipt")

    def test_receipt_binding_every_provenance_and_identity_field(self):
        raw, _, _ = self.nested()
        plan = complete(raw)
        expected = plan.receipts[1]
        for field, value in (("parent_part", "0"), ("parent_sha256", digest(b"synthetic different")),
                             ("sha256", digest(b"synthetic altered")), ("bytes", expected["bytes"] + 1),
                             ("wire_start", expected["wire_start"] + 1), ("wire_end", expected["wire_end"] - 1),
                             ("mime", "1.99"), ("mime_chain", ["1.1", "1.99"]),
                             ("original_filename", None), ("filename", "different.bin"),
                             ("kind", "mime"), ("format", None), ("content_type", "text/plain")):
            with self.subTest(field=field):
                records = list(plan.receipts)
                records[1] = dict(expected, **{field: value})
                self.reject(lambda: adapter.bind_rfc822_receipts(plan, records), "rfc822_receipt_mismatch")

    def test_receipt_shape_bool_numbers_path_like_locator_and_unknown_fields(self):
        plan = complete(rfc(eml()))
        for field, value in (("bytes", True), ("wire_start", True), ("mime_chain", ("1",))):
            records = [dict(plan.receipts[0], **{field: value})]
            self.reject(lambda: adapter.bind_rfc822_receipts(plan, records), "rfc822_invalid_receipt_shape")
        self.reject(lambda: adapter.bind_rfc822_receipts(plan, iter(plan.receipts)), "rfc822_invalid_receipt_shape")
        self.reject(lambda: adapter.bind_rfc822_receipts(plan, [dict(plan.receipts[0], unexpected="synthetic")]),
                    "rfc822_invalid_receipt_shape")
        self.reject(lambda: adapter.bind_rfc822_receipts(plan, [dict(plan.receipts[0], part="../synthetic")]),
                    "rfc822_unlisted_receipt")

    def test_private_path_not_certified_by_metadata_binding(self):
        plan = complete(rfc(eml()))
        records = [dict(plan.receipts[0], path="synthetic/staged.bin")]
        self.assertEqual(adapter.bind_rfc822_receipts(plan, records), plan.captures)
        self.assertNotIn("path", plan.receipts[0])

    def test_manifest_and_receipt_copy_mutation_cannot_change_plan(self):
        plan = complete(rfc(eml()))
        manifest = plan.as_manifest()
        manifest["attachments"][0]["mime_chain"].append("1.99")
        self.assertEqual(plan.receipts[0]["mime_chain"], ["1"])
        self.assertEqual(plan.inventory.sources[0].sha256, digest(rfc(eml())))

    def test_invalid_limits_types_and_safe_failures(self):
        for field, ceiling in (("max_message_bytes", inventory.wire.MAX_BYTES),
                               ("max_total_bytes", inventory.MAX_TOTAL_BYTES),
                               ("max_depth", inventory.wire.MAX_DEPTH),
                               ("max_parts", inventory.wire.MAX_PARTS),
                               ("max_captures", inventory.MAX_CAPTURES)):
            for value in (0, -1, True, ceiling + 1):
                self.reject(lambda: inventory.RFC822Limits(**{field: value}), "rfc822_invalid_limits")
        self.reject(lambda: inventory.inventory_rfc822(b"", limits=inventory.RFC822Limits()), "rfc822_invalid_raw_bytes")
        self.reject(lambda: inventory.inventory_rfc822("synthetic"), "rfc822_invalid_raw_bytes")
        self.reject(lambda: inventory.inventory_rfc822(eml(), limits={}), "rfc822_invalid_limits")

    def test_no_stdlib_serialization_call_in_nested_inventory(self):
        raw, child, grandchild = self.nested()
        with patch("email.message.EmailMessage.as_bytes", side_effect=AssertionError("no reserialization")):
            plan = complete(raw)
        self.assertEqual([capture.payload for capture in plan.captures], [child, grandchild])

    def test_outer_root_has_no_capture_and_still_requires_explicit_scalar_policy(self):
        plan = complete(eml())
        self.assertEqual(plan.captures, ())
        self.assertEqual(adapter.bind_rfc822_receipts(plan, []), ())
        self.assertEqual(plan.usage.mime_parts, 1)


if __name__ == "__main__":
    unittest.main()
