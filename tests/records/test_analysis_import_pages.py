"""DF2007-04: records analysis-packets-import refuses a packet whose citation names a page the received record does not have (synthetic roots)."""
import contextlib
import io
import json
import os
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch

from campaign_tool.records import analysis_packets as ap, public_packets as pp
from campaign_tool.records.ledger import store
from campaign_tool.records.outcomes import received_items as ri
from tests.records import test_analysis_bridge as tb, test_analysis_packet_outbox as to, test_analysis_packets as tap, test_outcome_received_items as tri

CODE = "PACKET_PAGE_OUT_OF_RANGE"
LEGAL = "Civ. Code \u00a7 1798.90.52"


def ref(sha, page, fact="F-0123456789ab"):
    return "record %s page %s (%s)" % (sha[:12], page, fact)


class ImportPageTests(unittest.TestCase):
    run_main, receipts = tap.AnalysisPacketImportTests.run_main, tap.AnalysisPacketImportTests.receipts

    def setUp(self):
        outbox = to.PacketOutboxTests("test_empty_received_at_holds_the_finding_as_date_unavailable")
        outbox.start()
        self.addCleanup(outbox.doCleanups)
        outbox.review("N1")
        outbox.tick("PACKET_STAGED")
        (path,) = outbox.outbox()  # the real builder's file: page 2 of the two-page record
        self.name, self.data = path.name, path.read_bytes()
        self.run = json.loads(self.data)["packets"][0]["provenance"]["documents"][0]["run_id"]
        received = tri.ReceivedItemsTests("test_stage_artifact_modes_replay_conflict_and_no_coordinator_input")
        received.setUp()
        self.addCleanup(received.doCleanups)
        with store.ledger(received.database) as con:
            con.execute("INSERT INTO agencies VALUES(?,?,NULL,NULL)", (tb.AGY, tb.AGY))
            con.commit()
        docs = []
        for pages, extra in ((tb.PAGES, {}), (["Only page text"], {}), (["P1", "P2", "P3"], {}), (["No page locator"], {"locator": {"note": 1}})):
            docs.append(received.document(pages, tb.AGY, **extra)[0])
            received.mail(docs[-1], "2026-11-02T18:00:00Z")
        self.digest, self.one, self.three, self.unpaged = docs
        self.assertEqual(self.digest, outbox.digest)
        (received.processing / "runs").mkdir(mode=0o700)
        (received.processing / "runs" / ("extract-%s.json" % self.run)).write_text(json.dumps(
            {"schema": "records-extract-outcomes-v1", "run_id": self.run, "documents": []}))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(tap.run_stages.main(["outputs", "--project", str(received.project), "--run-id", self.run]), 0)
        self.project, self.processing = received.project, received.processing
        self.journal = self.project / pp.JOURNAL

    def packet(self, cites, docs=None, source=None):
        """The real outbox file rebuilt (and rekeyed) with these citations over these received documents."""
        body = json.loads(self.data)
        body.pop("key")
        entry = body["packets"][0]
        doc, prov = entry["packet"]["documents"][0], entry["provenance"]["documents"][0]
        shas = sorted(set(docs or [self.digest]))
        entry["packet"]["documents"] = [dict(doc, sha256=s) for s in shas]
        entry["provenance"]["documents"] = [dict(prov, sha256=s) for s in shas]
        entry["packet"]["citations"] = [{"ref": r, "verified": True} for r in cites]
        if source:
            entry["packet"]["source"] = source
        return tap.rekey(body)

    def tree(self):
        skip = {self.project / ap.RECEIPT}
        return {str(p): p.read_bytes() for p in self.project.rglob("*") if p.is_file() and not p.is_symlink() and p not in skip}

    def refused(self, *cites, docs=None):
        """Import this packet; it is receipted PACKET_PAGE_OUT_OF_RANGE, exit 0, and nothing is proposed or written to the journal."""
        before, seen = self.tree(), len(self.receipts())
        with patch.object(pp, "propose", side_effect=AssertionError("propose")):
            code, out = self.run_main(self.packet(cites, docs, source="fr-r%d" % seen))
        self.assertEqual(code, 0, out)
        self.assertIn(CODE + "=1", out)
        self.assertEqual([r["result"] for r in self.receipts()][seen:], [CODE])
        self.assertEqual((self.tree(), pp.pending(self.project)), (before, []))
        self.assertFalse(self.journal.exists())
        return out

    def test_page_two_of_one_is_held_then_import_refused(self):
        out = self.refused(ref(self.one, 2), docs=[self.one])
        self.assertNotIn(str(self.project), out)
        (receipt,) = self.receipts()
        self.assertEqual(set(receipt), {"key", "source", "result"})
        self.assertFalse((self.project / pp.OUT).exists())

    def test_page_one_stages_and_valid_key_is_unchanged(self):
        code, out = self.run_main((self.name, self.data))  # the real builder's file: page 2 of two pages
        self.assertEqual((code, "proposed=1" in out), (0, True), out)
        self.assertEqual([r["result"] for r in self.receipts()], ["proposed r0"])
        name, data = self.packet([ref(self.one, 1)], docs=[self.one], source="fr-one")
        code, out = self.run_main((name, data))
        self.assertEqual((code, "proposed=1" in out), (0, True), out)
        self.assertEqual(len(pp.pending(self.project)), 2)
        code, out = self.run_main((name, data), (self.name, self.data))
        self.assertEqual((code, "ALREADY=2" in out, len(self.receipts())), (0, True, 2))

    def test_relative_project_path_counts_pages_like_an_absolute_one(self):
        self.assertEqual(ap._count(os.path.relpath(self.project), self.three, {}), 3)  # relative to the working directory
        self.assertEqual(ap._count(os.path.relpath(self.project), self.three, {}), ap._count(str(self.project), self.three, {}))
        packet = {"documents": [{"sha256": self.one}], "citations": [{"ref": ref(self.one, 1)}]}
        self.assertIsNone(ap._pages(os.path.relpath(self.project), packet, {}))  # a valid citation is not refused
        packet["citations"] = [{"ref": ref(self.one, 2)}]
        self.assertEqual(ap._pages(os.path.relpath(self.project), packet, {}), CODE)  # an out-of-range one still is

    def test_import_resolves_full_document_and_all_citations(self):
        for cites, docs in (([LEGAL, ref(self.digest, 2), ref(self.three, 3), ref(self.three, 4)], [self.digest, self.three]),  # last citation fails
                            ([ref(self.digest, 3)], [self.digest]),  # a larger count of another document is never borrowed
                            ([ref("f" * 64, 1)], [self.digest]),  # prefix names no document of this packet
                            ([ref(self.one, 1)], [self.digest]),  # a received document that is not this packet's
                            ([LEGAL, ref(self.digest, 1), "record %s page 1" % self.digest[:12]], [self.digest])):  # record-shaped but malformed
            self.refused(*cites, docs=docs)
        for token in ("0", "p2", "02", "1.5", "+1", "-1", " 1", "\u0662", "1e0", ""):
            self.refused(ref(self.digest, token), docs=[self.digest])
        self.refused("record %s page 1 (F-1) trailing" % self.digest[:12], docs=[self.digest])

    def test_two_documents_with_different_counts_pass_and_a_legal_citation_is_opaque(self):
        name, data = self.packet([LEGAL, "Gov. Code \u00a7 7922.530", ref(self.digest, 2), ref(self.three, 3), ref(self.three, 1)], docs=[self.digest, self.three])
        with patch.object(ri, "_current_units", wraps=ri._current_units) as spy:
            code, out = self.run_main((name, data))
        self.assertEqual((code, "proposed=1" in out), (0, True), out)
        self.assertEqual(spy.call_count, 2)  # one read-only lookup per full document hash, cached for the import
        self.assertEqual(len(pp.pending(self.project)), 1)

    def test_duplicate_prefix_is_ambiguous_and_refused_before_any_lookup(self):
        packet = {"documents": [{"sha256": "a" * 12 + "0" * 52}, {"sha256": "a" * 12 + "1" * 52}], "citations": [{"ref": ref("a" * 12, 1)}]}
        with patch.object(ap, "_count", side_effect=AssertionError("count")):
            self.assertEqual(ap._pages(str(self.project), packet, {}), CODE)

    def test_sparse_pages_never_make_a_bound(self):
        self.assertEqual(ap._count(str(self.project), self.three, {}), 3)
        pages = lambda *n: ([{"page": p, "ordinal": i, "text": "t"} for i, p in enumerate(n, 1)], [], None)  # noqa: E731
        for units in (pages(1, 3), pages(2, 3), pages(1, 1, 2, 4)):
            with patch.object(ri, "_current_units", return_value=units):
                self.assertIsNone(ap._count(str(self.project), self.three, {}))
        with patch.object(ri, "_current_units", return_value=pages(1, 2, 2, 3)):  # distinct pages are counted as the exporter does
            self.assertEqual(ap._count(str(self.project), self.three, {}), 3)

    def test_import_missing_or_incomplete_extraction_refuses_without_mutation(self):
        self.refused(ref(self.unpaged, 1), docs=[self.unpaged])  # page-locator diagnostics: no usable pages
        ledger = self.processing / "ledger.sqlite"
        evidence = self.project / "processing-v1" / "artifacts" / "evidence"
        with sqlite3.connect(ledger.as_uri() + "?mode=ro&immutable=1", uri=True) as con:
            (path,) = con.execute("SELECT derived_path FROM units WHERE original_sha256=?", (self.one,)).fetchone()
        unit = Path(path)
        self.assertEqual(unit.parent, evidence)
        original, mode = unit.read_bytes(), unit.stat().st_mode & 0o777

        def put(data):  # evidence files are read-only: replace the whole file
            spare = unit.with_name("spare")
            spare.write_bytes(data)
            spare.chmod(mode)
            os.replace(spare, unit)
        put(original + b" ")  # malformed evidence: the pinned hash no longer matches
        self.refused(ref(self.one, 1), docs=[self.one])
        put(original)
        moved = unit.with_name("moved")
        os.replace(unit, moved)
        os.symlink(moved, unit)  # a symlinked evidence file is never followed
        self.refused(ref(self.one, 1), docs=[self.one])
        unit.unlink()
        os.replace(moved, unit)
        os.rename(ledger, str(ledger) + ".away")  # no ledger: nothing to bound against, and no fallback to the claimed page
        out = self.refused(ref(self.one, 1), docs=[self.one])
        self.assertNotIn(str(self.project), out)
        os.rename(str(ledger) + ".away", ledger)
        code, out = self.run_main(self.packet([ref(self.one, 1)], [self.one]))  # everything restored: the same page now stages
        self.assertEqual((code, "proposed=1" in out), (0, True), out)

    def test_a_busy_ledger_is_retryable_and_never_a_proposal(self):
        (self.processing / "ledger.sqlite-journal").write_bytes(b"x")
        before = self.tree()
        with patch.object(pp, "propose", side_effect=AssertionError("propose")):
            code, out = self.run_main(self.packet([ref(self.one, 1)], [self.one]))
        self.assertEqual((code, out.strip(), self.receipts(), self.tree()), (75, "ledger_busy", [], before))
        (self.processing / "ledger.sqlite-journal").unlink()
        code, out = self.run_main(self.packet([ref(self.one, 1)], [self.one]))
        self.assertEqual((code, "proposed=1" in out), (0, True), out)

    def test_refusal_receipt_replay_and_independent_member(self):
        bad, good = self.packet([ref(self.one, 2)], [self.one], source="fr-bad"), self.packet([ref(self.one, 1)], [self.one], source="fr-good")
        code, out = self.run_main(bad, good)
        self.assertEqual((code, CODE + "=1" in out, "proposed=1" in out), (0, True, True), out)
        self.assertEqual(sorted(r["result"] for r in self.receipts()), [CODE, "proposed r0"])
        self.assertEqual(len(pp.pending(self.project)), 1)
        events, receipts = self.journal.read_bytes(), self.receipts()
        code, out = self.run_main(bad, good)  # a resend is a no-op
        self.assertEqual((code, "ALREADY=2" in out), (0, True), out)
        self.assertEqual((self.journal.read_bytes(), self.receipts()), (events, receipts))
        forbidden = ("approve", "decide", "write_change", "enable", "turn_off", "init")
        with contextlib.ExitStack() as stack:
            for name in forbidden:
                stack.enter_context(patch.object(pp, name, side_effect=AssertionError(name)))
            self.assertEqual(self.run_main(bad, good)[0], 0)
        self.assertFalse((self.project / pp.OUT).exists() or (self.project / pp.SWITCH).exists())


if __name__ == "__main__":
    unittest.main()
