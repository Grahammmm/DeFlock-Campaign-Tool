"""Synthetic verified-header graph cases; no real mail is read or parsed."""
from copy import deepcopy
import hashlib
import itertools
import json
from pathlib import Path
import unittest

from campaign_tool.records import correspondence
from tests.records import test_correspondence as existing

REPO = Path(__file__).resolve().parents[2]
CASE_IDS = (
    "ordinary_chain_and_independent_chain", "two_and_three_message_cycles",
    "cycle_with_incoming_acyclic_edge", "reordered_population",
    "duplicate_delivery_occurrences", "cross_account_ids",
    "self_reference_collision_and_missing_parent", "report_integration_and_source_unchanged",
)


def message(identity, parent=None, *, account="synthetic-a", label=None, occurrence=None):
    """Exact producer projection shape, with an original digest computed at runtime."""
    mid = "<" + identity + "@synthetic.invalid>"
    fields = [["Message-ID", mid]]
    if parent is not None:
        fields.append(["In-Reply-To", "<" + parent + "@synthetic.invalid>"])
    raw = ("FICTIONAL ORIGINAL " + (label or identity) + "\n" + json.dumps(fields)).encode()
    digest = hashlib.sha256(raw).hexdigest()
    return {"id": occurrence or identity, "account": account, "eml_sha256": digest,
            "message_id": mid, "headers": json.dumps({"schema": "verified-eml-headers-v1",
            "source_original_sha256": digest, "parser_defects": [], "fields": fields})}


def candidate(identity, *parents):
    return {"mail_id": identity, "parent_mail_ids": sorted(parents),
            "basis": "declared_reply_header", "status": "unverified_candidate"}


def gap(identity, code="thread_reply_cycle"):
    return {"code": code, "mail_id": identity}


def project(population):
    gaps = []
    result = correspondence._threads(population, gaps)
    return (sorted(result, key=lambda row: row["mail_id"]),
            sorted(gaps, key=lambda row: (row["mail_id"], row["code"])))


class ThreadCycleTests(unittest.TestCase):
    def check_projection(self, population, candidates, gaps):
        before = deepcopy(population)
        self.assertEqual(project(population),
                         (sorted(candidates, key=lambda row: row["mail_id"]),
                          sorted(gaps, key=lambda row: (row["mail_id"], row["code"]))))
        self.assertEqual(population, before)

    def test_ordinary_chain_and_independent_chain(self):
        self.check_projection([message("a"), message("b", "a"), message("c", "b"),
                               message("x"), message("y", "x")],
                              [candidate("b", "a"), candidate("c", "b"), candidate("y", "x")], [])
        # Longer than Python's ordinary recursion limit: traversal must remain iterative.
        rows = [message("long-0")]
        rows.extend(message("long-" + str(i), "long-" + str(i - 1)) for i in range(1, 1200))
        self.check_projection(rows, [candidate("long-" + str(i), "long-" + str(i - 1))
                                     for i in range(1, 1200)], [])

    def test_two_and_three_message_cycles(self):
        # The two-edge assertion is also the deciding negative-control assertion.
        self.check_projection([message("a", "b"), message("b", "a")], [], [gap("a"), gap("b")])
        self.check_projection([message("x", "y"), message("y", "z"), message("z", "x")],
                              [], [gap("x"), gap("y"), gap("z")])
        # Independent reachability oracle over every three-node functional graph.
        names = ("a", "b", "c")
        for parents in itertools.product((None, *names), repeat=3):
            links = dict(zip(names, parents))
            expected_candidates, expected_gaps = [], []
            for name, parent in links.items():
                if parent is None:
                    continue
                if parent == name:
                    expected_gaps.append(gap(name, "self_reply_reference"))
                    continue
                seen, current = set(), parent
                while current is not None and current not in seen and current != name:
                    seen.add(current)
                    current = links[current]
                if current == name:
                    expected_gaps.append(gap(name))
                else:
                    expected_candidates.append(candidate(name, parent))
            with self.subTest(parents=parents):
                self.check_projection([message(name, links[name]) for name in names],
                                      expected_candidates, expected_gaps)

    def test_cycle_with_incoming_acyclic_edge(self):
        self.check_projection([message("a", "b"), message("b", "a"), message("c", "a"),
                               message("d", "c"), message("x"), message("y", "x")],
                              [candidate("c", "a"), candidate("d", "c"), candidate("y", "x")],
                              [gap("a"), gap("b")])

    def test_reordered_population(self):
        rows = [message("a", "b"), message("b", "a"), message("c", "a"),
                message("d"), message("e", "d")]
        expected = ([candidate("c", "a"), candidate("e", "d")], [gap("a"), gap("b")])
        for order in itertools.permutations(rows):
            self.assertEqual(project(list(order)), expected)

    def test_duplicate_delivery_occurrences(self):
        a, b = message("a", "b"), message("b", "a")
        a2, b2 = {**a, "id": "a-copy"}, {**b, "id": "b-copy"}
        self.check_projection([a, b, a2, b2, message("c", "a")],
                              [candidate("c", "a", "a-copy")],
                              [gap(i) for i in ("a", "a-copy", "b", "b-copy")])
        root = message("root")
        reply = message("reply", "root")
        self.check_projection([root, {**root, "id": "root-copy"}, reply,
                               {**reply, "id": "reply-copy"}],
                              [candidate("reply", "root", "root-copy"),
                               candidate("reply-copy", "root", "root-copy")], [])

    def test_cross_account_ids(self):
        a, b = message("a", "b"), message("b", "a")
        # Same bytes and same Message-IDs in the other account must not close a cycle.
        self.check_projection([a, {**b, "account": "synthetic-b"}], [],
                              [gap("a", "thread_parent_missing"), gap("b", "thread_parent_missing")])
        self.check_projection([a, b, message("a", account="synthetic-b", occurrence="other-a"),
                               message("b", "a", account="synthetic-b", occurrence="other-b")],
                              [candidate("other-b", "other-a")], [gap("a"), gap("b")])

    def test_self_reference_collision_and_missing_parent(self):
        rows = [message("self", "self"), message("p", label="first", occurrence="p1"),
                message("p", label="second", occurrence="p2"), message("reply", "p"),
                message("missing", "absent"), message("root"), message("ok", "root")]
        self.check_projection(rows, [candidate("ok", "root")],
                              [gap("self", "self_reply_reference"), gap("reply", "message_id_collision"),
                               gap("missing", "thread_parent_missing")])
        # Invalid headers must be rejected before graph construction, not relabeled cycles.
        a, b = message("a", "b"), message("b", "a")
        projection = json.loads(b["headers"])
        projection["parser_defects"] = ["synthetic-defect"]
        b["headers"] = json.dumps(projection)
        self.check_projection([a, b], [], [gap("a", "thread_parent_missing"),
                                          gap("b", "header_projection_invalid")])

    def test_report_integration_and_source_unchanged(self):
        fixture = existing.CorrespondenceTests(
            "test_reports_are_deterministic_and_leave_database_unchanged")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.seed_request_evidence()
        for ordinal, row in enumerate([message("a", "b"), message("b", "a"), message("c", "a")], 1):
            digest = fixture.original("cycle-" + row["id"])
            fixture.occurrence(row["id"], digest)
            projection = json.loads(row["headers"])
            projection["source_original_sha256"] = digest
            fixture.mail(row["id"], digest, uid=ordinal, message_id=row["message_id"],
                         headers=json.dumps(projection))
        before = fixture.database_state()
        result = fixture.report()
        self.assertEqual(fixture.database_state(), before)
        self.assertEqual(result["thread_candidates"], [candidate("c", "a")])
        self.assertEqual(sorted(result["gaps"], key=lambda row: row["code"]),
                         [{"code": "outstanding_items_and_follow_up_unreviewed", "request_id": "request-a"},
                          gap("a"), gap("b")])
        request = fixture.request_row(result)
        self.assertEqual({row["id"] for row in request["occurrences"]}, {"mail-a", "attachment-a"})
        self.assertEqual(result["unassigned_occurrence_ids"], ["a", "b", "c"])
        self.assertEqual(request["fulfillment"], "not_assessed")
        self.assertIsNone(request["review"])
        self.assertIsNone(request["outstanding_items"])
        self.assertEqual(result["reminder_proposals"], [])
        for row in result["mail"]:
            self.assertNotIn("headers", row)
            self.assertNotIn("message_id", row)
        reviewed = fixture.report(review=fixture.review())
        self.assertEqual(reviewed["thread_candidates"], [candidate("c", "a")])
        self.assertEqual(reviewed["gaps"], [gap("a"), gap("b")])
        self.assertEqual(fixture.request_row(reviewed)["review"]["status"], "proposal")
        self.assertTrue(reviewed["reminder_proposals"][0]["requires_current_owner_review"])
        stale = fixture.review()
        stale["snapshot_sha256"] = hashlib.sha256(b"synthetic stale snapshot").hexdigest()
        with self.assertRaisesRegex(correspondence.CorrespondenceError, "^review_snapshot_stale$"):
            fixture.report(review=stale)
        self.assertEqual(fixture.database_state(), before)


if __name__ == "__main__":
    unittest.main()
