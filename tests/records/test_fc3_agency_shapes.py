"""FC3-AGENCY-SHAPES: malformed agency candidate labels are rejected up front.

Synthetic, offline contract tests for the pure ``reconcile`` ingress. Every
card's ``agency_status`` must be a string in the existing three-status
vocabulary and every card's ``role`` must be a nonblank string, checked
before membership tests and target selection and before any result is
emitted. Well-formed behaviour (target rule, evidence, conflicts, aliases,
candidate-only flags) is unchanged. The catalog-card and digest-record
vocabulary is reused from ``tests.records.test_agency_reconciliation``.

One TestCase class per frozen named case; the acceptance runner maps the
case names to these classes.
"""

import copy
import unittest

from campaign_tool.records.agency_reconciliation import reconcile
from tests.records.test_agency_reconciliation import card, record, sha

STATUS_MESSAGE = "card requires explicit agency_status"
ROLE_MESSAGE = "card requires explicit role"
STATUSES = ("unassigned", "hint_present", "scope_excluded")
TARGET_ROLES = ("agency_original", "container_child")
MISSING = object()  # Marker: remove the key instead of setting a value.

# Every malformed (non-string) label shape, including nested variants that
# carry an otherwise valid label inside a container.
NON_STRING_STATUSES = (
    ("missing", MISSING), ("null", None),
    ("bool_true", True), ("bool_false", False),
    ("int_zero", 0), ("int_one", 1), ("float", 1.5), ("float_inf", float("inf")),
    ("list_empty", []), ("list_valid_label", ["unassigned"]),
    ("list_nested", [["unassigned"]]), ("list_two_labels", ["hint_present", "scope_excluded"]),
    ("object_empty", {}), ("object_label_key", {"unassigned": True}),
    ("object_field", {"agency_status": "unassigned"}), ("object_nested", {"nested": ["unassigned"]}),
    ("tuple_valid_label", ("unassigned",)), ("bytes_valid_label", b"unassigned"),
)
NON_STRING_ROLES = (
    ("missing", MISSING), ("null", None),
    ("bool_true", True), ("bool_false", False),
    ("int_zero", 0), ("int_one", 1), ("int_negative", -1), ("float", 1.5), ("float_inf", float("inf")),
    ("list_empty", []), ("list_valid_label", ["agency_original"]),
    ("list_nested", [["container_child"]]), ("list_two_labels", ["agency_original", "container_child"]),
    ("object_empty", {}), ("object_label_key", {"agency_original": True}),
    ("object_field", {"role": "agency_original"}), ("object_nested", {"nested": ["container_child"]}),
    ("tuple_valid_label", ("agency_original",)), ("bytes_valid_label", b"agency_original"),
)
BLANK_ROLES = (
    ("empty", ""), ("space", " "), ("whitespace_mix", " \t\n "), ("unicode_space", "\u2003\u00a0"),
)
UNKNOWN_STATUS_STRINGS = (
    "", " ", "Unassigned", "UNASSIGNED", " unassigned", "unassigned ", "assigned", "unknown",
    "excluded", "hint present", "scope-excluded", "Hint_Present",
)
NONTARGET_STRING_ROLES = (
    "project_artifact", "excluded_unrelated_personal", "unresolved", "original",
    "Agency_Original", " agency_original", "container_child ", "container child", "x",
)


def with_field(item, field, value):
    item = dict(item)
    if value is MISSING:
        item.pop(field, None)
    else:
        item[field] = value
    return item


def attempt(cards, records=(), aliases=None):
    """Call the real API and report ('returned', result) or ('raised', error)."""
    try:
        return "returned", reconcile(cards, list(records), aliases)
    except Exception as error:  # The deciding assertions inspect the exact class.
        return "raised", error


class ShapeTestCase(unittest.TestCase):
    def assert_unpromoted(self, result):
        self.assertEqual(set(result), {"cards", "summary", "unmatched_digest_records", "excluded_digest_records"})
        for item in result["cards"]:
            self.assertIs(item["verified"], False)
            self.assertIs(item["publication_ready"], False)

    def assert_rejected(self, cards, message, records=(), aliases=None):
        before = copy.deepcopy((cards, list(records), aliases))
        kind, value = attempt(cards, records, aliases)
        self.assertEqual(kind, "raised", "malformed card produced a result instead of an error")
        self.assertIs(type(value), ValueError, "expected ValueError, got " + repr(value))
        self.assertEqual(str(value), message)
        self.assertEqual((cards, list(records), aliases), before)

    def targets(self, result):
        return {item["sha256"]: item for item in result["cards"]}


class BothTargetRolesTests(ShapeTestCase):
    def test_both_target_roles_are_targets_with_no_evidence(self):
        result = reconcile([card("original"), card("child", role="container_child")], [])
        targets = self.targets(result)
        self.assertEqual(set(targets), {sha("original"), sha("child")})
        self.assertEqual(targets[sha("original")]["role"], "agency_original")
        self.assertEqual(targets[sha("child")]["role"], "container_child")
        for item in targets.values():
            self.assertEqual(item["status"], "no_evidence")
            self.assertEqual(item["canonical_candidates"], [])
        self.assertEqual(result["summary"]["target_count"], 2)
        self.assertEqual(result["summary"]["no_evidence_items"], 2)
        self.assert_unpromoted(result)

    def test_both_target_roles_keep_exact_hash_locators(self):
        for role in TARGET_ROLES:
            with self.subTest(role=role):
                row = record("target", line=11)
                result = reconcile([card("target", role=role)], [row])
                item = result["cards"][0]
                self.assertEqual(item["role"], role)
                self.assertEqual(item["exact_hash_candidates"], [{
                    "agency": "Example Police", "canonical_agency": "example police",
                    "source": {"object_sha256": row["object_sha256"], "line": 11}}])
                self.assertEqual(item["status"], "candidate_only")
                self.assert_unpromoted(result)

    def test_both_target_roles_take_parent_hints_and_parent_digests(self):
        parent = card("parent", role="project_artifact", status="hint_present", hints=["Example Police"])
        for role in TARGET_ROLES:
            with self.subTest(role=role):
                target = card("target", role=role, parents=[parent["sha256"]])
                result = reconcile([parent, target], [record("parent", line=4)])
                item = result["cards"][0]
                self.assertEqual(item["parent_hints"], [{
                    "agency": "Example Police", "canonical_agency": "example police",
                    "parent_sha256": parent["sha256"]}])
                self.assertEqual(item["parent_digest_candidates"], [{
                    "agency": "Example Police", "canonical_agency": "example police",
                    "parent_sha256": parent["sha256"],
                    "source": {"object_sha256": sha("digest-object"), "line": 4}}])
                self.assertEqual(item["status"], "candidate_only")
                self.assert_unpromoted(result)

    def test_both_target_roles_use_explicit_aliases_only(self):
        for role in TARGET_ROLES:
            with self.subTest(role=role):
                rows = [record("target", "Example PD", line=1), record("target", "Example Police", line=2)]
                plain = reconcile([card("target", role=role)], rows)
                self.assertEqual(plain["cards"][0]["status"], "conflicting_candidates")
                aliased = reconcile([card("target", role=role)], rows, {"Example PD": "Example Police"})
                self.assertEqual(aliased["cards"][0]["canonical_candidates"], ["example police"])
                self.assertEqual(aliased["cards"][0]["status"], "candidate_only")
                self.assertEqual([c["agency"] for c in aliased["cards"][0]["exact_hash_candidates"]],
                                 ["Example PD", "Example Police"])
                self.assert_unpromoted(aliased)


class ExistingNontargetRolesTests(ShapeTestCase):
    def test_existing_nontarget_string_roles_are_skipped_not_rejected(self):
        for role in NONTARGET_STRING_ROLES:
            for status in STATUSES:
                with self.subTest(role=role, status=status):
                    cards = [card("other", role=role, status=status), card("original")]
                    result = reconcile(cards, [record("other", line=2)])
                    self.assertEqual(set(self.targets(result)), {sha("original")})
                    self.assertEqual(result["summary"]["target_count"], 1)
                    self.assertEqual(result["unmatched_digest_records"], [])
                    expected_excluded = 1 if status == "scope_excluded" else 0
                    self.assertEqual(result["summary"]["excluded_digest_records"], expected_excluded)
                    self.assert_unpromoted(result)

    def test_target_roles_with_non_unassigned_status_are_skipped(self):
        for role in TARGET_ROLES:
            for status in ("hint_present", "scope_excluded"):
                with self.subTest(role=role, status=status):
                    result = reconcile([card("other", role=role, status=status, hints=["Example Police"])], [])
                    self.assertEqual(result["cards"], [])
                    self.assertEqual(result["summary"]["target_count"], 0)

    def test_nontarget_parent_still_supplies_hints(self):
        parent = card("parent", role="project_artifact", status="hint_present", hints=["Example Sheriff"])
        child = card("child", role="container_child", parents=[parent["sha256"]])
        result = reconcile([parent, child], [])
        self.assertEqual(set(self.targets(result)), {sha("child")})
        self.assertEqual(result["cards"][0]["canonical_candidates"], ["example sheriff"])
        self.assert_unpromoted(result)


class MalformedStatusTypesTests(ShapeTestCase):
    def test_malformed_status_types_raise_fixed_value_error(self):
        for name, value in NON_STRING_STATUSES:
            for role in TARGET_ROLES + ("project_artifact",):
                with self.subTest(status=name, role=role):
                    self.assert_rejected([with_field(card("bad", role=role), "agency_status", value)], STATUS_MESSAGE)

    def test_malformed_status_after_valid_cards_and_with_matching_records(self):
        for name, value in NON_STRING_STATUSES:
            with self.subTest(status=name):
                cards = [card("original"), card("child", role="container_child"),
                         with_field(card("bad"), "agency_status", value)]
                self.assert_rejected(cards, STATUS_MESSAGE, records=[record("bad"), record("original")])


class MalformedRoleTypesTests(ShapeTestCase):
    def test_malformed_role_types_raise_fixed_value_error(self):
        for name, value in NON_STRING_ROLES + BLANK_ROLES:
            for status in STATUSES:
                with self.subTest(role=name, status=status):
                    self.assert_rejected([with_field(card("bad", status=status), "role", value)], ROLE_MESSAGE)

    def test_malformed_role_after_valid_cards_and_with_matching_records(self):
        for name, value in NON_STRING_ROLES + BLANK_ROLES:
            with self.subTest(role=name):
                cards = [card("original"), with_field(card("bad"), "role", value)]
                self.assert_rejected(cards, ROLE_MESSAGE, records=[record("bad"), record("original")])


class MalformedExcludedCardTests(ShapeTestCase):
    """Cards that would never become targets are still validated first."""

    def test_malformed_role_on_scope_excluded_card_with_digest_rows(self):
        for name, value in NON_STRING_ROLES + BLANK_ROLES:
            with self.subTest(role=name):
                excluded = with_field(card("excluded", status="scope_excluded"), "role", value)
                self.assert_rejected([card("original"), excluded], ROLE_MESSAGE,
                                     records=[record("excluded", line=6), record("original")])

    def test_malformed_role_on_hint_present_parent_only_card(self):
        for name, value in NON_STRING_ROLES + BLANK_ROLES:
            with self.subTest(role=name):
                parent = with_field(card("parent", status="hint_present", hints=["Example Police"]), "role", value)
                child = card("child", role="container_child", parents=[parent["sha256"]])
                self.assert_rejected([child, parent], ROLE_MESSAGE)

    def test_malformed_status_on_card_without_any_reference(self):
        for name, value in NON_STRING_STATUSES:
            with self.subTest(status=name):
                lone = with_field(card("lone", role="project_artifact"), "agency_status", value)
                self.assert_rejected([card("original"), lone], STATUS_MESSAGE, aliases={"Example PD": "Example Police"})


class UnknownStatusStringTests(ShapeTestCase):
    def test_unknown_status_strings_raise_fixed_value_error(self):
        for value in UNKNOWN_STATUS_STRINGS:
            for role in TARGET_ROLES + ("project_artifact",):
                with self.subTest(status=value, role=role):
                    self.assert_rejected([card("bad", role=role, status=value)], STATUS_MESSAGE)

    def test_every_existing_status_is_still_accepted(self):
        for status in STATUSES:
            with self.subTest(status=status):
                result = reconcile([card("item", status=status)], [])
                self.assertEqual(len(result["cards"]), 1 if status == "unassigned" else 0)


class EmptyInputsAndConflictingCandidatesTests(ShapeTestCase):
    def test_empty_inputs_have_zero_summary(self):
        for aliases in (None, {}):
            with self.subTest(aliases=aliases):
                result = reconcile([], [], aliases)
                self.assertEqual(result["cards"], [])
                self.assertEqual(result["unmatched_digest_records"], [])
                self.assertEqual(result["excluded_digest_records"], [])
                self.assertEqual(set(result["summary"].values()), {0})

    def test_exact_hash_conflict_is_reported(self):
        result = reconcile([card("original")], [record("original", "Example Police", line=1),
                                                record("original", "Example Sheriff", line=2)])
        self.assertEqual(result["cards"][0]["status"], "conflicting_candidates")
        self.assertEqual(result["cards"][0]["canonical_candidates"], ["example police", "example sheriff"])
        self.assertEqual(result["summary"]["conflicting_items"], 1)
        self.assert_unpromoted(result)

    def test_exact_and_parent_conflict_is_reported(self):
        parent = card("parent", role="project_artifact", status="hint_present", hints=["Example Sheriff"])
        child = card("child", role="container_child", parents=[parent["sha256"]])
        result = reconcile([parent, child], [record("child", "Example Police")])
        self.assertEqual(result["cards"][0]["status"], "conflicting_candidates")
        self.assertEqual(result["summary"]["conflicting_items"], 1)
        self.assertEqual(result["summary"]["candidate_only_items"], 0)
        self.assert_unpromoted(result)


class InputsUnchangedAndNoApprovalTests(ShapeTestCase):
    def test_successful_inputs_unchanged_and_never_promoted(self):
        parent = card("parent", role="project_artifact", status="hint_present", hints=["Example Police"])
        cards = [parent, card("child", role="container_child", parents=[parent["sha256"]]),
                 card("original"), card("excluded", status="scope_excluded")]
        rows = [record("child", "Example PD"), record("original", "Example Sheriff", line=2),
                record("excluded", line=3), record("missing", line=4)]
        aliases = {"Example PD": "Example Police"}
        before = copy.deepcopy((cards, rows, aliases))
        result = reconcile(cards, rows, aliases)
        self.assertEqual((cards, rows, aliases), before)
        self.assertEqual(result["summary"]["target_count"], 2)
        self.assert_unpromoted(result)

    def test_rejected_inputs_unchanged(self):
        cases = [with_field(card("bad"), "agency_status", value) for _, value in NON_STRING_STATUSES]
        cases += [with_field(card("bad"), "role", value) for _, value in NON_STRING_ROLES + BLANK_ROLES]
        for index, bad in enumerate(cases):
            with self.subTest(case=index):
                cards = [card("original"), bad]
                rows = [record("bad"), record("original")]
                aliases = {"Example PD": "Example Police"}
                before = copy.deepcopy((cards, rows, aliases))
                kind, _ = attempt(cards, rows, aliases)
                self.assertEqual(kind, "raised")
                self.assertEqual((cards, rows, aliases), before)


if __name__ == "__main__":
    unittest.main()
