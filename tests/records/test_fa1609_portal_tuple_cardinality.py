"""FA1609: full portal tuple identity is independent of URL aliases."""
import copy
import hashlib
import socket
import unittest
from unittest import mock

from tools import portal_reconcile as pr


def url_hash(label):
    return hashlib.sha256(("FA1609 fictional URL " + label).encode("utf-8")).hexdigest()


def observation(label, host="records.example.test", request="req-alpha", item="item-alpha", path=None):
    row = {"host_class": "nextrequest", "host": host, "request_id": request,
           "item_id": item, "allowlisted": True, "url_sha256": url_hash(label)}
    if path is not None:
        row.pop("item_id")
        row["path"] = path
    return row


def queue_row(host, request, item):
    return {"host": host, "request_id": request, "item_id": item, "state": "received",
            "has_version": True}


def scenario():
    target = ("records.example.test", "req-alpha", "item-alpha")
    rows = [
        observation("target-explicit"),
        observation("target-document-path", path="/documents/item-alpha/download"),
        observation("decoy-host", host="archive.example.test"),
        observation("decoy-request", request="req-beta"),
        observation("decoy-item", item="item-beta"),
    ]
    queue = [
        queue_row(*target),
        # Valid received rows that each differ from target in exactly one tuple field.
        queue_row("archive2.example.test", "req-alpha", "item-alpha"),
        queue_row("records.example.test", "req-gamma", "item-alpha"),
        queue_row("records.example.test", "req-alpha", "item-gamma"),
        queue_row("records.example.test", "req-unused", "item-unused"),
    ]
    return rows, queue


def assert_identity_oracle(test, report):
    test.assertEqual(report["mail"]["observation_rows"], 5)
    test.assertEqual(report["mail"]["unique_urls"], 5)
    test.assertEqual(report["mail"]["classified_urls"], 5)
    test.assertEqual(report["classes"]["fetched"], 2)
    test.assertEqual(report["classes"]["unproven"], 3)
    test.assertEqual(report["detail"]["unproven"]["not_in_queue"], 3)
    test.assertEqual(report["detail"]["fetched_queue_items"], 1)
    test.assertEqual(report["queue"]["items"], 5)
    test.assertEqual(report["queue"]["referenced_by_mail"], 1)
    test.assertEqual(report["queue"]["not_referenced_by_mail"], 4)
    test.assertEqual(report["queue"]["with_version_receipt"], 5)
    test.assertEqual(report["mail"]["malformed_rows"], 0)
    test.assertEqual(report["queue"]["malformed_rows"], 0)


class PortalTupleCardinalityTests(unittest.TestCase):
    def setUp(self):
        guard = mock.patch.object(socket, "socket", side_effect=AssertionError("network_forbidden_in_FA1609"))
        guard.start()
        self.addCleanup(guard.stop)

    def test_identity_components_and_url_alias_cardinality(self):
        rows, queue = scenario()
        report = pr.reconcile(rows, queue)
        assert_identity_oracle(self, report)
        self.assertEqual(pr.render(report), pr.render(pr.reconcile(list(reversed(rows)), list(reversed(queue)))))
        # Also permute deterministically without changing input content.
        rotated_rows = rows[2:] + rows[:2]
        rotated_queue = queue[1:] + queue[:1]
        self.assertEqual(pr.render(report), pr.render(pr.reconcile(rotated_rows, rotated_queue)))

    def test_item_id_and_equivalent_document_paths_are_one_identity(self):
        q = [queue_row("records.example.test", "req-alpha", "item-alpha")]
        explicit = pr.reconcile([observation("explicit")], q)
        for path in ("/documents/item-alpha", "/documents/item-alpha/download"):
            with self.subTest(path=path):
                parsed = pr.reconcile([observation("path", path=path)], q)
                self.assertEqual(parsed["classes"]["fetched"], 1)
                self.assertEqual(parsed["detail"]["fetched_queue_items"], 1)
        self.assertEqual(explicit["classes"]["fetched"], 1)

    def test_each_valid_identity_boundary_is_independent(self):
        original = observation("boundary")
        queue = [queue_row("records.example.test", "req-alpha", "item-alpha")]
        variants = (
            ("host", "other.example.test"),
            ("request_id", "req-absent"),
            ("item_id", "item-absent"),
        )
        for field, absent in variants:
            with self.subTest(field=field):
                changed = copy.deepcopy(original)
                changed[field] = absent
                changed["url_sha256"] = url_hash("boundary-decoy-" + field)
                report = pr.reconcile([original, changed], queue)
                self.assertEqual(report["mail"]["malformed_rows"], 0)
                self.assertEqual(report["classes"]["fetched"], 1)
                self.assertEqual(report["classes"]["unproven"], 1)
                self.assertEqual(report["detail"]["unproven"]["not_in_queue"], 1)
                self.assertEqual(report["detail"]["fetched_queue_items"], 1)
                self.assertEqual(report["queue"]["referenced_by_mail"], 1)

    def test_item_only_lookup_fails_same_tuple_oracle(self):
        rows, queue = scenario()
        correct = pr.reconcile(rows, queue)
        assert_identity_oracle(self, correct)

        def item_only_classify(observation, items, ambiguous):
            _, _, _, _, item_id, _ = observation
            candidates = [entry for identity, entry in items.items() if identity[2] == item_id]
            if candidates:
                state, has_version, reason = candidates[0]
                if has_version:
                    return "fetched", ("item-only", item_id)
                return "queued", state
            return "unproven", "not_in_queue"

        with mock.patch.object(pr, "classify", side_effect=item_only_classify):
            faulty = pr.reconcile(rows, queue)
        with self.assertRaises(AssertionError) as caught:
            assert_identity_oracle(self, faulty)
        self.assertIn("4 != 2", str(caught.exception))
        print("NEGATIVE_CONTROL_CAUGHT: " + str(caught.exception))
        # The exact same oracle remains green against the unmodified API.
        assert_identity_oracle(self, pr.reconcile(rows, queue))
        self.assertEqual(correct["detail"]["fetched_queue_items"], 1)


if __name__ == "__main__":
    unittest.main()
