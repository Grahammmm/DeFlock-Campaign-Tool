"""DF2007-04: the packet step never cites a record page the synced record does not have (synthetic roots)."""
import contextlib
import csv
import json
import unittest
from unittest.mock import patch

from campaign_tool.records import analysis_bridge as bridge
from tests.records import test_analysis_packet_outbox as to
from tests.records import test_analysis_tick as tt

OUT, UNKNOWN = "EVIDENCE_PAGE_OUT_OF_RANGE", "EVIDENCE_PAGE_COUNT_UNKNOWN"


@contextlib.contextmanager
def fact_page(page):
    """Every reviewed fact the packet step reads cites this page (the real FACTS.csv and its review log stay untouched)."""
    real = bridge._tool

    def tool(name):
        module = real(name)
        if name == "flock_facts":
            read = module.read_reviewed
            module.read_reviewed = lambda *a, **k: (lambda r: ([dict(x, page=page) for x in r[0]],) + tuple(r[1:]))(read(*a, **k))
        return module
    with patch.object(bridge, "_tool", tool):
        yield


class PacketPageTests(unittest.TestCase):
    def staged(self, at="2026-11-02T18:00:00Z"):
        self.fixture = to.PacketOutboxTests("test_empty_received_at_holds_the_finding_as_date_unavailable")
        self.fixture.start(at=at)
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.review("N1")
        return self.fixture

    def sources(self, pages):
        """What sync writes for a record whose TEXTS.csv row says `pages`."""
        path = self.fixture.out / "inputs" / "SOURCES.csv"
        with open(path, newline="") as handle:
            rows = list(csv.DictReader(handle))
        with open(path, "w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]), lineterminator="\n")
            writer.writeheader()
            writer.writerows(dict(r, pages=pages) for r in rows)

    def packets(self):
        fx = self.fixture
        return bridge._packets(fx.root, tt.SLUG, json.loads(fx.t.config().read_text()))

    def test_page_two_of_one_is_held_then_import_refused(self):
        fx = self.staged()
        self.sources("1")  # the synced record has one page; the reviewed fact cites page 2
        fx.tick("PACKET_ALL_HELD n=0 held=4 EVIDENCE_FACT_UNREVIEWED=3 %s=1" % OUT)
        self.assertEqual(fx.outbox(), [])  # the whole finding is held: no outbox file
        outcome, _, _, summary, write = self.packets()
        self.assertEqual((outcome, write), ("PACKET_ALL_HELD", None))
        self.assertNotIn("page 2", summary + json.dumps(fx.ledger()))  # codes and counts only
        # the import half of this test is test_analysis_import_pages.test_page_two_of_one_is_held_then_import_refused

    def test_page_one_stages_and_valid_key_is_unchanged(self):
        fx = self.staged()
        self.sources("1")
        with fact_page("1"):
            outcome, key, name, summary, write = self.packets()
            with patch.object(bridge, "_page_hold", lambda *a: None):  # the behaviour before this unit
                before = self.packets()
        self.assertEqual(outcome, "PACKET_STAGED")
        self.assertEqual((key, name, write[2]), (before[1], before[2], before[4][2]))  # same key, file name and bytes
        self.assertIn("record %s page 1 (" % fx.digest[:12], write[2].decode())
        self.assertNotIn('"pages"', write[2].decode())  # no page count enters the packet, provenance or inputs
        self.sources("2")  # the unchanged fixture: a two-page record cited at page 2
        fx.tick("PACKET_STAGED")
        fx.tick("PACKET_UNCHANGED")
        self.assertEqual(len(fx.outbox()), 1)

    def test_page_zero_and_p2_and_beyond_the_count_are_held_by_the_tick(self):
        fx = self.staged()
        for page in ("0", "p2", "", " 1", "+1", "01", "1.5", "\u0661", "3"):
            with fact_page(page):
                outcome, _, _, summary, write = self.packets()
            self.assertEqual((outcome, write), ("PACKET_ALL_HELD", None), page)
            self.assertIn(" %s=1" % OUT, summary, page)
        self.assertEqual(fx.outbox(), [])

    def test_page_token_admission_never_coerces(self):
        for value in (0, -1, True, False, None, "", "p2", 1.5, "+1", "-1", " 1", "1 ", "01", "1e0", "1.0", "\u0661", "1-2", 1.0, [1], "1\n"):
            self.assertIsNone(bridge._whole(value), repr(value))
        self.assertEqual((bridge._whole(1), bridge._whole("1"), bridge._whole("12"), bridge._whole(7)), (1, 1, 12, 7))
        facts, sources = {"F1": {"page": "3", "source_sha256": "a"}}, {"a": [{"pages": "3", "received_at": "x"}]}
        self.assertIsNone(bridge._page_hold(["F1"], facts, sources))  # boundary N accepted
        sources["a"][0]["pages"] = "2"
        self.assertEqual(bridge._page_hold(["F1"], facts, sources), OUT)  # N + 1 refused
        facts["F1"]["page"] = 2  # a genuine int is accepted
        self.assertIsNone(bridge._page_hold(["F1"], facts, sources))

    def test_unknown_count_has_its_own_producer_code(self):
        fx = self.staged()
        for pages in ("", "0", "x", "-1", "1.5", " 2", "\u0662"):
            self.sources(pages)
            outcome, _, _, summary, write = self.packets()
            self.assertEqual((outcome, write), ("PACKET_ALL_HELD", None), pages)
            self.assertIn(" %s=1" % UNKNOWN, summary, pages)
            self.assertNotIn(OUT, summary)
        fx.tick("PACKET_ALL_HELD n=0 held=4 EVIDENCE_FACT_UNREVIEWED=3 %s=1" % UNKNOWN)
        self.assertEqual(fx.outbox(), [])
        with fact_page("p2"):  # a malformed page with a missing count is the page failure
            self.assertIn(" %s=1" % OUT, self.packets()[3])

    def test_existing_date_hold_keeps_priority_over_the_page_check(self):
        fx = self.staged(at="")
        self.sources("")
        fx.tick("PACKET_ALL_HELD n=0 held=4 EVIDENCE_DATE_UNAVAILABLE=1 EVIDENCE_FACT_UNREVIEWED=3")

    def test_several_facts_page_syntax_then_unknown_count_then_bounds(self):
        facts = {"F1": {"page": "5", "source_sha256": "a"}, "F2": {"page": "1", "source_sha256": "b"}, "F3": {"page": "p1", "source_sha256": "b"}}
        sources = {"a": [{"pages": "2", "received_at": "1"}], "b": [{"pages": "", "received_at": "1"}]}
        self.assertEqual(bridge._page_hold(["F1", "F2", "F3"], facts, sources), OUT)  # F3 syntax outranks F2's unknown count
        self.assertEqual(bridge._page_hold(["F1", "F2"], facts, sources), UNKNOWN)  # unknown count outranks F1's bounds failure
        sources["b"][0]["pages"] = "9"
        self.assertEqual(bridge._page_hold(["F1", "F2"], facts, sources), OUT)
        self.assertIsNone(bridge._page_hold(["F2"], facts, sources))
        sources["b"].insert(0, {"pages": "1", "received_at": "0"})  # the earliest-received row bounds it, not a larger later one
        self.assertIsNone(bridge._page_hold(["F2"], facts, sources))
        facts["F2"]["page"] = "2"
        self.assertEqual(bridge._page_hold(["F2"], facts, sources), OUT)


if __name__ == "__main__":
    unittest.main()
