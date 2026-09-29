"""Synthetic contract tests; no private corpus or external services are used."""

import copy
import hashlib
import unittest

from campaign_tool.records.agency_reconciliation import reconcile


def sha(label):
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def card(label, role="agency_original", status="unassigned", hints=None, parents=None):
    return {
        "sha256": sha(label),
        "role": role,
        "agency_status": status,
        "agency_hints": list(hints or []),
        "parents": list(parents or []),
    }


def record(label, agency="Example Police", source="digest-object", line=1):
    return {
        "sha256": sha(label),
        "agency": agency,
        "object_sha256": sha(source),
        "line": line,
    }


class AgencyReconciliationTests(unittest.TestCase):
    def assert_unpromoted(self, result):
        for item in result["cards"]:
            self.assertIs(item["verified"], False)
            self.assertIs(item["publication_ready"], False)

    def test_empty_input(self):
        result = reconcile([], [])
        self.assertEqual(result["cards"], [])
        self.assertEqual(result["unmatched_digest_records"], [])
        self.assertEqual(result["excluded_digest_records"], [])
        for key in (
            "target_count", "exact_hash_candidate_items", "parent_hint_items",
            "either_source_items", "no_evidence_items", "conflicting_items",
            "candidate_only_items", "unresolved_parent_items",
            "unmatched_digest_records", "excluded_digest_records",
        ):
            self.assertEqual(result["summary"][key], 0, key)

    def test_only_unassigned_originals_and_children_are_targets(self):
        cards = [
            card("original"), card("child", role="container_child"),
            card("artifact", role="project_artifact"),
            card("assigned", status="hint_present", hints=["Example Police"]),
            card("excluded", status="scope_excluded"),
        ]
        result = reconcile(cards, [])
        self.assertEqual(
            {item["sha256"] for item in result["cards"]},
            {sha("original"), sha("child")},
        )
        self.assertEqual(result["summary"]["target_count"], 2)
        self.assert_unpromoted(result)

    def test_no_evidence_is_explicit(self):
        result = reconcile([card("original")], [])
        item = result["cards"][0]
        self.assertEqual(item["role"], "agency_original")
        self.assertEqual(item["status"], "no_evidence")
        for key in ("exact_hash_candidates", "parent_hints", "canonical_candidates",
                    "unresolved_parent_hashes"):
            self.assertEqual(item[key], [])
        self.assert_unpromoted(result)

    def test_exact_hash_candidate_preserves_locator(self):
        row = record("original", line=17)
        result = reconcile([card("original")], [row])
        item = result["cards"][0]
        self.assertEqual(item["exact_hash_candidates"], [{
            "agency": "Example Police", "canonical_agency": "example police",
            "source": {"object_sha256": sha("digest-object"), "line": 17},
        }])
        self.assertEqual(item["canonical_candidates"], ["example police"])
        self.assertEqual(item["status"], "candidate_only")
        self.assert_unpromoted(result)

    def test_all_matching_rows_keep_distinct_source_locators(self):
        rows = [record("original", source="first", line=3),
                record("original", source="first", line=9),
                record("original", source="second", line=3)]
        result = reconcile([card("original")], rows)
        candidates = result["cards"][0]["exact_hash_candidates"]
        self.assertEqual(len(candidates), 3)
        self.assertCountEqual(
            [candidate["source"] for candidate in candidates],
            [{"object_sha256": row["object_sha256"], "line": row["line"]}
             for row in rows],
        )
        self.assertEqual(result["summary"]["exact_hash_candidate_items"], 1)

    def test_duplicate_parent_references_do_not_duplicate_hints(self):
        parent = card("parent", status="hint_present", hints=["Example Police"])
        child = card("child", role="container_child",
                     parents=[parent["sha256"], parent["sha256"]])
        result = reconcile([parent, child], [])
        self.assertEqual(result["cards"][0]["parent_hints"], [{
            "agency": "Example Police", "canonical_agency": "example police",
            "parent_sha256": parent["sha256"],
        }])
        self.assertEqual(result["summary"]["parent_hint_items"], 1)
        self.assert_unpromoted(result)

    def test_blank_and_unassigned_parent_hints_are_filtered(self):
        parent = card("parent", status="hint_present",
                      hints=["", "   ", "UNASSIGNED", " unAssigned ", " Unknown ", "Example Police"])
        child = card("child", role="container_child", parents=[parent["sha256"]])
        result = reconcile([parent, child], [])
        self.assertEqual(
            [hint["agency"] for hint in result["cards"][0]["parent_hints"]],
            ["Example Police"],
        )
        self.assertEqual(result["cards"][0]["status"], "candidate_only")

    def test_unresolved_parent_hashes_are_retained_and_deduplicated(self):
        child = card("child", role="container_child",
                     parents=[sha("absent"), sha("absent")])
        result = reconcile([child], [])
        self.assertEqual(result["cards"][0]["unresolved_parent_hashes"], [sha("absent")])
        self.assertEqual(result["summary"]["unresolved_parent_items"], 1)
        self.assertEqual(result["cards"][0]["status"], "no_evidence")

    def test_exact_hash_labels_can_conflict(self):
        result = reconcile([card("original")], [
            record("original", "Example Police", line=1),
            record("original", "Example Sheriff", line=2),
        ])
        self.assertEqual(result["cards"][0]["status"], "conflicting_candidates")
        self.assertEqual(set(result["cards"][0]["canonical_candidates"]),
                         {"example police", "example sheriff"})
        self.assertEqual(result["summary"]["conflicting_items"], 1)
        self.assert_unpromoted(result)

    def test_exact_and_parent_conflict_is_counted_together(self):
        parent = card("parent", status="hint_present", hints=["Example Sheriff"])
        child = card("child", role="container_child", parents=[parent["sha256"]])
        result = reconcile([parent, child], [record("child", "Example Police")])
        self.assertEqual(result["cards"][0]["status"], "conflicting_candidates")
        for key in ("exact_hash_candidate_items", "parent_hint_items",
                    "either_source_items", "conflicting_items"):
            self.assertEqual(result["summary"][key], 1)
        self.assertEqual(result["summary"]["candidate_only_items"], 0)
        self.assert_unpromoted(result)

    def test_parent_only_labels_can_conflict(self):
        parents = [card("parent-a", status="hint_present", hints=["Example Police"]),
                   card("parent-b", status="hint_present", hints=["Example Sheriff"])]
        child = card("child", role="container_child",
                     parents=[parent["sha256"] for parent in parents])
        result = reconcile(parents + [child], [])
        self.assertEqual(result["cards"][0]["status"], "conflicting_candidates")
        self.assertEqual(result["summary"]["conflicting_items"], 1)

    def test_no_automatic_alias_for_similar_labels(self):
        result = reconcile([card("original")], [
            record("original", "Example PD", line=1),
            record("original", "Example Police Department", line=2),
        ])
        self.assertEqual(result["cards"][0]["status"], "conflicting_candidates")

    def test_explicit_alias_keys_ignore_case_and_whitespace(self):
        parent = card("parent", status="hint_present", hints=["Example Police Department"])
        child = card("child", role="container_child", parents=[parent["sha256"]])
        row = record("child", "Example PD", line=8)
        result = reconcile([parent, child], [row],
                           aliases={"  eXaMpLe Pd  ": "Example Police Department"})
        item = result["cards"][0]
        self.assertEqual(item["status"], "candidate_only")
        self.assertEqual(item["canonical_candidates"], ["example police department"])
        self.assertEqual(item["exact_hash_candidates"][0]["agency"], "Example PD")
        self.assertEqual(item["exact_hash_candidates"][0]["source"],
                         {"object_sha256": row["object_sha256"], "line": 8})
        self.assert_unpromoted(result)

    def test_blank_or_unassigned_alias_endpoints_are_rejected(self):
        for endpoint in ("", "   ", "UNASSIGNED", " unAssigned ", "Unknown", " unknown "):
            for aliases in ({endpoint: "Example Police"}, {"Example PD": endpoint}):
                with self.subTest(aliases=aliases):
                    with self.assertRaises((ValueError, TypeError)):
                        reconcile([card("original")], [], aliases=aliases)

    def test_unmatched_and_excluded_digest_records_remain_separate(self):
        cards = [card("original"), card("excluded", status="scope_excluded")]
        unmatched = record("missing", line=5)
        excluded = record("excluded", line=6)
        result = reconcile(cards, [record("original"), unmatched, excluded])
        self.assertEqual(result["unmatched_digest_records"], [{
            "sha256": unmatched["sha256"], "agency": unmatched["agency"],
            "canonical_agency": "example police",
            "source": {"object_sha256": unmatched["object_sha256"], "line": 5},
        }])
        self.assertEqual(result["excluded_digest_records"], [{
            "sha256": excluded["sha256"],
            "source": {"object_sha256": excluded["object_sha256"], "line": 6},
        }])
        self.assertEqual(result["summary"]["unmatched_digest_records"], 1)
        self.assertEqual(result["summary"]["excluded_digest_records"], 1)
        self.assertEqual(result["summary"]["target_count"], 1)

    def test_summary_counts_reconcile_without_double_counting_sources(self):
        parent = card("parent", status="hint_present", hints=["Example Police"])
        cards = [parent, card("exact"),
                 card("parent-only", role="container_child", parents=[parent["sha256"]]),
                 card("both", role="container_child", parents=[parent["sha256"]]),
                 card("conflict", role="container_child", parents=[parent["sha256"]]),
                 card("none")]
        rows = [record("exact"), record("both"), record("conflict", "Example Sheriff")]
        result = reconcile(cards, rows)
        summary = result["summary"]
        expected = {"target_count": 5, "exact_hash_candidate_items": 3,
                    "parent_hint_items": 3, "either_source_items": 4,
                    "no_evidence_items": 1, "conflicting_items": 1,
                    "candidate_only_items": 3, "unresolved_parent_items": 0,
                    "unmatched_digest_records": 0, "excluded_digest_records": 0}
        for key, count in expected.items():
            self.assertEqual(summary[key], count, key)
        self.assertEqual(summary["target_count"], len(result["cards"]))
        self.assertEqual(summary["target_count"], summary["no_evidence_items"] +
                         summary["conflicting_items"] + summary["candidate_only_items"])
        self.assert_unpromoted(result)

    def test_inputs_are_not_mutated(self):
        parent = card("parent", status="hint_present", hints=["Example Police"])
        cards = [parent, card("child", role="container_child", parents=[parent["sha256"]])]
        rows = [record("child", "Example PD")]
        aliases = {"Example PD": "Example Police"}
        before = copy.deepcopy((cards, rows, aliases))
        reconcile(cards, rows, aliases=aliases)
        self.assertEqual((cards, rows, aliases), before)

    def test_duplicate_card_hash_is_rejected(self):
        original = card("original")
        with self.assertRaises((ValueError, TypeError)):
            reconcile([original, copy.deepcopy(original)], [])

    def test_malformed_hashes_are_rejected(self):
        invalid = "not-a-sha256"
        cases = []
        bad_card = card("original")
        bad_card["sha256"] = invalid
        cases.append(([bad_card], []))
        bad_parent = card("child", role="container_child", parents=[invalid])
        cases.append(([bad_parent], []))
        for field in ("sha256", "object_sha256"):
            bad_record = record("original")
            bad_record[field] = invalid
            cases.append(([card("original")], [bad_record]))
        for index, (cards, rows) in enumerate(cases):
            with self.subTest(case=index):
                with self.assertRaises((ValueError, TypeError)):
                    reconcile(cards, rows)

    def test_invalid_record_lines_are_rejected(self):
        for line in (0, -1, None, "invalid"):
            with self.subTest(line=line):
                with self.assertRaises((ValueError, TypeError)):
                    reconcile([card("original")], [record("original", line=line)])

    def test_unknown_digest_label_is_not_evidence(self):
        for placeholder in ("Unknown", " unknown ", "UNKNOWN", "UNASSIGNED", " "):
            with self.subTest(placeholder=placeholder):
                result = reconcile([card("original")], [record("original", placeholder)])
                self.assertEqual(result["cards"][0]["status"], "no_evidence")
                self.assertEqual(result["cards"][0]["canonical_candidates"], [])
                self.assertEqual(result["summary"]["exact_hash_candidate_items"], 0)


if __name__ == "__main__":
    unittest.main()
