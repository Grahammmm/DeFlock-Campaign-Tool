"""Unreadable citations are extraction gaps, never documentary value classes (RF1405-3)."""
import unittest

from tests.flock import test_law_policy_compare as base
from tests.flock import test_law_policy_compare_json as machine
from tests.flock.test_law_policy_compare import A, B, C, SA, SB, SC, FACTS, PLANTED, doc, fact, policy


def gap(agency, label, kind="retention", value="UNREADABLE", status="unreviewed", page="1"):
    row = fact(agency, label, kind, value, status=status, page=page)
    row["verbatim_quote"] = ""
    return row


class UnreadableCompareTest(unittest.TestCase):
    setUp, write, agency, one, snapshot = (base.CompareTest.setUp, base.CompareTest.write, base.CompareTest.agency,
                                         base.CompareTest.one, base.CompareTest.snapshot)

    def both(self, *extra, skipped=0, disputed=0, code=0):
        for path in self.store.glob("agencies/*/FACTS.csv"):
            self.assertEqual(FACTS.check_file(path, path.parent.name), [])
        result, got, raw, text_result, lines = machine.CompareJsonTest.both(self, *extra)
        self.assertEqual((result, text_result), (code, code))
        for path in self.store.glob("agencies/*/FACTS.csv"):
            for _, row in FACTS.load(path, path.parent.name)[1]:
                if row["value"].strip() == "UNREADABLE":
                    self.assertNotIn(row["fact_id"], raw + "\n".join(lines), "gap used as documentary citation")
        self.assertEqual(got["counts"]["unreadable_skipped"], skipped)
        self.assertEqual(got["counts"]["disputed_skipped"], disputed)
        self.assertEqual(lines[-1], " ".join(f"{k}={got['counts'][k]}" for k in (
            "agencies", "governed", "ungoverned", "types", "differ", "agree", "disputed_skipped", "unreadable_skipped"))
            + " | ids only, research view, not a legal conclusion")
        self.assertEqual(got["schema"], "flock-law-policy-compare-v1")
        self.assertTrue(got["review_required"])
        self.assertEqual(got["note"], machine.NOTE)
        for text in (raw, "\n".join(lines)):
            for leak in (PLANTED, "CANARY-VALUE-41", doc("a1"), doc("b1"), "f" * 64, str(self.store), "UNREADABLE"):
                self.assertNotIn(leak, text)
        return got, lines

    def test_all_unreadable_is_undocumented_not_agree(self):
        hidden = [gap(A, "a1"), gap(B, "b1")]
        for name, label, row in zip((A, B), ("a1", "b1"), hidden):
            self.agency(name, [policy(name, label, "2025-01-01")], [row])
        got, lines = self.both(skipped=2)
        self.assertEqual(got, machine.doc_of([machine.row("retention", [], [SA, SB])], a=2, g=2, r=2))
        self.assertEqual(lines[0], f"compare | retention | undocumented: {SA}, {SB}")
        for row in hidden:
            self.assertNotIn(row["fact_id"], str(got) + str(lines))

    def test_one_unreadable_cannot_create_difference(self):
        hidden = gap(A, "a1")
        self.agency(A, [policy(A, "a1", "2025-01-01")], [hidden])
        keep = self.one(B, "b1", "retention", "CANARY-VALUE-41")
        got, lines = self.both(skipped=1)
        self.assertEqual(got["types"], [machine.row("retention", [[machine.member(SB, keep)]], [SA])])
        self.assertEqual(lines[0], f"compare | retention | class 1: {SB} ({keep}) | undocumented: {SA}")
        self.assertEqual((got["counts"]["differ"], got["counts"]["agree"]), (0, 0))
        self.assertNotIn(hidden["fact_id"], str(got) + str(lines))

    def test_mixed_readable_and_unreadable_preserves_readable_classes(self):
        readable = fact(A, "a1", "retention", "CANARY-VALUE-41")
        hidden = gap(A, "a1", page="2")
        self.agency(A, [policy(A, "a1", "2025-01-01")], [hidden, readable])
        for value, verdict, code in (("  canary-value-41 ", "agree", 0), ("different term", "differ", 3)):
            with self.subTest(verdict=verdict):
                other = self.one(B, "b1", "retention", value)
                got, lines = self.both(skipped=1, code=code)
                members = [machine.member(SA, readable["fact_id"]), machine.member(SB, other)]
                classes = [members] if verdict == "agree" else [[m] for m in members]
                self.assertEqual(got["types"], [machine.row("retention", classes, verdict=verdict)])
                self.assertTrue(lines[0].endswith(" | " + verdict))
                self.assertNotIn(hidden["fact_id"], str(got) + str(lines))

    def test_exact_sentinel_whitespace_and_status_precedence(self):
        rows = [gap(A, "a1", "checked_gap", " \tUNREADABLE ", "checked"), gap(A, "a1", "pending_gap"),
                gap(A, "a1", "disputed_only", status="disputed"),
                fact(A, "a1", "lowercase", "unreadable"), fact(A, "a1", "prose", "page UNREADABLE here"),
                fact(A, "a1", "other", "N/A")]
        self.agency(A, [policy(A, "a1", "2025-01-01")], rows)
        got, lines = self.both(skipped=2, disputed=1)
        expected = [machine.row(kind, [], [SA]) if kind.endswith("gap") else
                    machine.row(kind, [[machine.member(SA, next(r["fact_id"] for r in rows if r["fact_type"] == kind))]])
                    for kind in ("checked_gap", "lowercase", "other", "pending_gap", "prose")]
        self.assertEqual(got["types"], expected)
        for row in rows[:3]:
            self.assertNotIn(row["fact_id"], str(got) + str(lines))

    def test_only_governing_policy_facts_count_and_discover_types(self):
        versions = [policy(A, "old", "2020-01-01", "2024-12-31"), policy(A, "a1", "2025-01-01", "2025-12-31"),
                    policy(A, "future", "2026-01-01"), policy(A, "unverified", "", evidence="unverified")]
        hidden = [gap(A, label, label) for label in ("old", "future", "unverified")]
        record = gap(A, "a1", "record_gap")
        record["kind"] = "record"
        keep = gap(A, "a1", "retention")
        self.agency(A, versions, [*hidden, record, keep])
        self.agency(B, [policy(B, "b1", "2025-01-01")])
        (self.store / "agencies" / SB / "FACTS.csv").unlink()  # absent FACTS is still undocumented
        self.agency(C, [policy(C, "c1", "", evidence="unverified")], [gap(C, "c1", "not_governed")])
        got, _ = self.both(skipped=1)
        self.assertEqual(got, machine.doc_of([machine.row("retention", [], [SA, SB, SC])],
                                            ungoverned=[SC], g=2, u=1, r=1))

    def test_type_filter_preserves_global_gap_counts_and_unknown_types(self):
        self.agency(A, [policy(A, "a1", "2025-01-01")], [gap(A, "a1"), gap(A, "a1", "disputed", status="disputed")])
        keep = self.one(B, "b1", "sharing", "CANARY-VALUE-41")
        expected = {"retention": machine.row("retention", [], [SA, SB]),
                    "sharing": machine.row("sharing", [[machine.member(SB, keep)]], [SA]),
                    "unknown": machine.row("unknown", [], [SA, SB])}
        for types in (None, "retention", "unknown", "sharing", "sharing,retention,retention"):
            with self.subTest(types=types):
                names = sorted(set(types.split(","))) if types else ["retention", "sharing"]
                got, _ = self.both(*(["--types", types] if types else []), skipped=1, disputed=1)
                self.assertEqual(got["types"], [expected[name] for name in names])
                self.assertEqual(got["types_requested"], names if types else None)


if __name__ == "__main__":
    unittest.main()
