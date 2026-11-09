"""Synthetic Unicode identities: lexical repair must not weaken publication gates."""
import copy
import unittest
from typing import Any

from campaign_tool import publication_gate as gate


HUMANS = ("É", "Ø", "Ł", "Μαρία", "Мария", "李明", "نور", "Élodie O'Neil",
          "李-Pat", "A", "Pat Organizer", "owner@example.invalid", "123")


def entry(who: Any = "李明") -> dict[str, Any]:
    record: dict[str, Any] = {"finding_id": "syn-names-1", "text": "Synthetic reviewed text."}
    record["review"] = {"status": "approved", "approved_by": who,
                        "content_hash": gate.content_digest(record)}
    return record


def approval(record):
    return dict(record["review"], artefact_id=record["finding_id"])


def digest(record):
    return gate.content_digest({k: v for k, v in record.items() if k != "review"})


class UnicodeApprovers(unittest.TestCase):
    def assert_refused(self, record, reason, rows=None, content_hash=None):
        rows = [approval(record)] if rows is None else rows
        before = copy.deepcopy((record, rows))
        with self.assertRaises(gate.PublicationRefused) as caught:
            gate.assert_publishable(record["finding_id"],
                                    digest(record) if content_hash is None else content_hash, rows)
        self.assertEqual(caught.exception.reason, reason)
        self.assertEqual((record, rows), before)

    def assert_entry_refused(self, record, fresh, reason):
        before = copy.deepcopy((record, fresh))
        self.assertEqual(gate.entry_refusals([record], {"findings": fresh}),
                         ["publication refused: artefact syn-names-1: " + reason])
        self.assertEqual((record, fresh), before)

    def assert_accepted(self, who):
        record = entry(who)
        rows, fresh = [approval(record)], {"findings": [copy.deepcopy(record)]}
        before = copy.deepcopy((record, rows, fresh))
        self.assertIs(gate.named(who), True)
        self.assertIsNone(gate.assert_publishable(record["finding_id"], digest(record), rows,
                                                 flags=["legal_claim"]))
        self.assertEqual(gate.entry_refusals([record], fresh), [])
        self.assertEqual((record, rows, fresh), before)
        self.assertEqual(record["review"]["approved_by"], who)

    def test_unicode_human_names_and_initials_pass_all_gate_views(self):
        for who in HUMANS:
            with self.subTest(who=who):
                self.assert_accepted(who)

    def test_canonical_and_compatibility_forms_classify_consistently(self):
        for who in ("É", "E\u0301", "  É  ", "Ｅ́", "Ｌｉ", "Ｐａｔ", "李\u200b明"):
            with self.subTest(who=who):
                self.assert_accepted(who)

    def test_machine_denials_survive_unicode_adjacency_and_obfuscation(self):
        machines: set[str] = set(gate.PLACEHOLDERS) | set(gate.MACHINE_WORDS)
        machines.update(prefix + "3" for prefix in gate.MODEL_PREFIXES)
        machines.update(("sol-reviewer", "sol-builder", "astra-high", "gpt-5-codex",
                         "ｓｙｓｔｅｍ", "s\u200bystem", "ＧＰＴ-５", "sol\u200d-reviewer"))
        # Placeholder denials apply to the whole string; machine/model/lane denials
        # must also survive Unicode adjacency without changing ASCII tokenization.
        adjacent = set(gate.MACHINE_WORDS) | {p + "3" for p in gate.MODEL_PREFIXES}
        adjacent.update(("sol-reviewer", "astra-high", "ｓｙｓｔｅｍ", "s\u200bystem"))
        machines.update(edge for name in adjacent for edge in ("李" + name, name + "نور"))
        for who in sorted(machines):
            with self.subTest(who=who):
                self.assertIs(gate.named(who), False)
                record = entry(who)
                self.assert_refused(record, "approver_unnamed")
                self.assert_entry_refused(record, [record], "approver_unnamed")

    def test_non_name_only_unicode_is_not_a_person(self):
        for who in (None, 7, [], {}, "", " \t\n", "\u200b\u200d", "\u0301\u0308",
                    "—'!?", "😀", "☀", "١٢٣", "१२३", "\u0301😀١"):
            with self.subTest(who=who):
                self.assertIs(gate.named(who), False)
                record = entry(who)
                self.assert_refused(record, "approver_unnamed")
                self.assert_entry_refused(record, [record], "approver_unnamed")

    def test_unicode_name_does_not_bypass_direct_approval_defects(self):
        record = entry()
        self.assert_refused(record, "no_approval", rows=[])
        self.assert_refused(record, "approval_for_other_content",
                            rows=[dict(approval(record), artefact_id="other")])
        for bad_hash in (None, "", "stale"):
            with self.subTest(content_hash=bad_hash):
                self.assert_refused(record, "content_changed_after_approval",
                                    rows=[dict(approval(record), content_hash=bad_hash)])
        missing = approval(record)
        del missing["content_hash"]
        self.assert_refused(record, "content_changed_after_approval", rows=[missing])
        for status in ("pending", "cancelled", "expired", "withdrawn", None):
            with self.subTest(status=status):
                self.assert_refused(record, "status_not_approved",
                                    rows=[dict(approval(record), status=status)])
        for fields, reason in (({"consumed": True}, "approval_consumed"),
                               ({"redaction_signed": False}, "redaction_unsigned")):
            self.assert_refused(record, reason, rows=[dict(approval(record), **fields)])

    def test_unicode_name_does_not_bypass_entry_content_binding(self):
        record = entry()
        for value in (None, "", "stale"):
            with self.subTest(recorded_hash=value):
                changed = copy.deepcopy(record)
                changed["review"]["content_hash"] = value
                self.assert_entry_refused(changed, [changed],
                                          "approval_unbound" if value in (None, "")
                                          else "content_changed_after_approval")
        missing = copy.deepcopy(record)
        del missing["review"]["content_hash"]
        self.assert_entry_refused(missing, [missing], "approval_unbound")
        edited = dict(record, text="Synthetic edited text.")
        for stored, fresh in ((record, [edited]), (edited, [record]), (edited, [edited]),
                              (record, []), (record, [dict(record, finding_id="other")])):
            with self.subTest(stored=stored, fresh=fresh):
                self.assert_entry_refused(stored, fresh, "content_changed_after_approval")
        for field, value, reason in (("consumed", True, "approval_consumed"),
                                     ("redaction_signed", False, "redaction_unsigned")):
            changed = copy.deepcopy(record)
            changed["review"][field] = value
            self.assert_entry_refused(changed, [changed], reason)
        # Non-approved entries are skipped, not promoted into publishable approvals.
        for status in ("pending", "cancelled", "expired"):
            changed = copy.deepcopy(record)
            changed["review"]["status"] = status
            before = copy.deepcopy(changed)
            self.assertEqual(gate.entry_refusals([changed], {"findings": [changed]}), [])
            self.assertEqual(changed, before)

    def test_matching_unicode_approval_counts_beside_stale_machine_and_duplicate_rows(self):
        record = entry()
        valid = approval(record)
        stale = dict(valid, content_hash="stale")
        machine = dict(valid, approved_by="system")
        for rows in ([stale, machine, valid], [valid, machine, stale], [valid, valid]):
            before = copy.deepcopy(rows)
            self.assertIsNone(gate.assert_publishable(record["finding_id"], digest(record), rows))
            self.assertEqual(rows, before)
        self.assert_refused(record, "approver_unnamed", rows=[machine, stale])
        self.assertEqual(gate.entry_refusals([record, record], {"findings": [record]}), [])

    def test_diagnostics_remain_bounded_and_inputs_unchanged(self):
        record = entry("李system")
        record["finding_id"] = "invalid/private 李明"
        record["review"]["content_hash"] = digest(record)
        rows = [approval(record)]
        before = copy.deepcopy((record, rows))
        with self.assertRaises(gate.PublicationRefused) as caught:
            gate.assert_publishable(record["finding_id"], digest(record), rows,
                                    flags=["legal_claim", "private 李明"])
        self.assertEqual(str(caught.exception),
                         "publication refused: artefact #?: approver_unnamed, flag:legal_claim")
        self.assertEqual(caught.exception.flags, ("legal_claim",))
        self.assertEqual((record, rows), before)
        self.assertEqual(gate.entry_refusals([record], {"findings": [record]}),
                         ["publication refused: artefact #?: approver_unnamed"])
        self.assertEqual((record, rows), before)


if __name__ == "__main__":
    unittest.main()
