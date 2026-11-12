"""DFGL-07j: every way review state can go wrong holds or re-reviews as the frozen transition table says (same real path as 07i).

Each case starts from 07i's state after its step (3): the real DAG at DONE, N1's fact checked by the real flock_facts review CLI, one
staged file imported as one proposed, unsigned packet. Evidence changes come only from the fact-drafts card rewriting its draft (the
stub facts-build re-reads the scratch fixture) and the DAG replayed to DONE through the real tick/mark CLIs; reviews only by the real CLI.
"""
import csv
import json
import re
import unittest

from campaign_tool.records import public_packets as pp
from tests.flock import test_review as tr
from tests.records import test_analysis_packet_outbox as to, test_analysis_review_persistence as trp, test_analysis_tick as tt

QUOTE, NEWER = "The search log lists date, user and plate only.", "The search log lists date, user, plate and purpose only."
VENDOR = "The ALPR vendor is Vigilant Solutions."
NEW_ROW = "Sample City Police Department,REQ-101,vehicle_count,40,%s,7,Forty vehicles carry readers.\n"


class ReviewNegativeTests(unittest.TestCase):
    setUp, imported = trp.ReviewPersistenceTests.setUp, trp.ReviewPersistenceTests.imported  # real export, sync and DAG to DONE

    def proposed(self):
        o = self.o
        o.review("N1")
        o.tick("PACKET_STAGED")
        (path,) = o.outbox()
        self.assertEqual(self.imported(path)[0], 0)
        (row,) = pp.pending(self.project)
        self.n1 = o.cited["N1"]
        return path, row

    def facts(self):
        with open(self.o.facts, newline="") as handle:
            return {r["fact_id"]: r for r in csv.DictReader(handle)}

    def log(self):
        with open(self.o.log, newline="") as handle:
            return list(csv.DictReader(handle))

    def fp(self, row):
        return to.facts_module().evidence_fp(row)

    def edit(self, old, new, name="compare-facts.csv"):
        path = tr.FIX / name
        self.assertIn(old, path.read_text())
        path.write_text(path.read_text().replace(old, new))

    def replay(self, redraft=True):
        """Ticks, playing each model card the real DAG waits for, until the first line that is not a wait. -> (exit code, stdout)."""
        o, self.waits = self.o, []
        if redraft:
            (o.out / "facts" / "drafts" / "a.yaml").write_text("fact-drafts rewritten\n")
        for step in range(14):
            code, text = o.t.tick(process=True)
            wait = re.search(r"WAITING (\S+)", text)
            if not wait:
                return code, text
            stage, out = wait.group(1), o.out
            self.waits.append(stage)
            run = out / "compare" / "store" / "agencies" / tt.SLUG / "runs" / json.loads(o.t.config().read_text())["review"]["run_date"]
            if stage == "verify":
                plan = json.loads((out / "compare" / "VERIFY-PLAN.json").read_text())
                (out / "compare" / "VERIFY.csv").write_text("row_id,result,reason,page_cite\n" + "".join(
                    "%s,agree,checked,p.2\n" % i for i in plan["priority_ids"] + plan["sample_ids"]))
                (out / "compare" / "VERIFY.md").write_text("checked\n")
            elif stage == "f4":
                (run / "VERIFY.csv").write_bytes((tr.FIX / "verify-sample.csv").read_bytes())
            else:
                (out / {"fact-drafts": "facts/drafts/a.yaml", "assess": "compare/ASSESSMENTS.yaml", "f3-findings": "compare/F3-FINDINGS.yaml"}[stage]
                 ).write_text("%s input %d\n" % (stage, step))
            self.assertEqual(o.t.mark(stage, "openai" if stage in ("verify", "f4") else "anthropic"), 0)
        self.fail(text)

    def one_packet(self, row):
        """pending(P) never gains a second packet for the finding and no approval or site change exists."""
        (now,) = pp.pending(self.project)
        self.assertEqual((now["id"], now["revision"], now["status"], now["signed_by"]), (row["id"], 0, "proposed", None))
        self.assertFalse((self.project / pp.OUT).exists() or (self.project / pp.SWITCH).exists())

    def import_new(self, old):
        (again,) = [p for p in self.o.outbox() if p not in old]
        code, out = self.imported(again)
        self.assertEqual(code, 0, out)
        self.assertIn("duplicate=1", out)  # propose: identical content is a duplicate, never a second packet or revision

    def test_changed_quote_under_the_same_fact_id_is_unreviewed_with_unbound_history(self):
        o = self.o
        path, row = self.proposed()
        old, history = self.fp(self.facts()[self.n1]), self.log()
        self.edit(QUOTE, NEWER)
        code, text = self.replay()
        self.assertEqual(code, 0, text)
        self.assertEqual(self.waits[0], "fact-drafts")
        self.assertIn(" PACKET_ALL_HELD n=0 held=4 EVIDENCE_FACT_UNREVIEWED=4", text)
        new = self.facts()[self.n1]
        self.assertEqual((new["status"], new["verbatim_quote"]), ("unreviewed", NEWER))
        self.assertNotEqual(self.fp(new), old)
        self.assertEqual(self.log(), history)  # the old decision stays as history bound to bytes that no longer exist
        self.assertEqual(o.outbox(), [path])  # no new outbox file
        o.review("N1")  # a real review of the new evidence
        o.tick("PACKET_STAGED")
        self.import_new([path])
        self.one_packet(row)
        self.assertEqual([(r["to_status"], r["evidence_fp"]) for r in self.log()], [("checked", old), ("checked", self.fp(self.facts()[self.n1]))])

    def test_added_evidence_is_unreviewed_and_the_checked_fact_stays_checked(self):
        o = self.o
        path, row = self.proposed()
        checked, history = self.facts()[self.n1], self.log()
        with open(tr.FIX / "compare-facts.csv", "a") as handle:
            handle.write(NEW_ROW % o.digest)
        code, text = self.replay()
        self.assertEqual(code, 0, text)
        facts = self.facts()
        self.assertEqual((facts[self.n1], self.log()), (checked, history))
        self.assertEqual(sorted(r["status"] for r in facts.values()), ["checked"] + ["unreviewed"] * 6)
        self.assertEqual([r["status"] for r in facts.values() if r["fact_type"] == "vehicle_count"], ["unreviewed"])
        self.import_new([path])
        self.one_packet(row)

    def test_removed_evidence_leaves_no_status_and_its_history_stays_unbound(self):
        o = self.o
        path, row = self.proposed()
        history, other = self.log(), o.cited["O1"]
        # f4-start refuses a finding that cites a removed fact, so the F3 card re-points N1 and N2 (and the compare stub's rows) first
        self.edit("%s,2," % self.n1, "%s,3," % other, "verify-sample.csv")
        for name in ("findings-sample.csv", "review-findings.csv"):
            self.edit(self.n1, other, name)
        self.edit("".join(l for l in (tr.FIX / "compare-facts.csv").read_text().splitlines(True) if QUOTE in l), "")
        code, text = self.replay()
        self.assertEqual(code, 0, text)
        self.assertNotIn(self.n1, self.facts())
        self.assertEqual(self.log(), history)  # history kept, bound to nothing
        self.assertIn(" PACKET_ALL_HELD n=0 held=4 EVIDENCE_FACT_UNREVIEWED=4", text)
        self.assertEqual(o.outbox(), [path])
        self.one_packet(row)

    def test_dispute_holds_and_its_reversal_re_stages_the_same_packet_never_a_second(self):
        o = self.o
        path, row = self.proposed()
        o.review("N1", "disputed")
        o.tick("PACKET_ALL_HELD n=0 held=4 EVIDENCE_FACT_DISPUTED=1 EVIDENCE_FACT_UNREVIEWED=3")
        self.assertEqual((o.outbox(), self.facts()[self.n1]["status"]), ([path], "disputed"))
        o.review("N1", "checked")  # the reversal, by the real reviewer
        o.tick("PACKET_STAGED")
        self.import_new([path])
        self.one_packet(row)
        self.assertEqual([r["to_status"] for r in self.log()], ["checked", "disputed", "checked"])  # the full chain is kept

    def test_inconsistent_history_holds_and_the_next_evidence_change_fails_facts_import_untouched(self):
        o = self.o
        path, row = self.proposed()
        o.log.write_text(o.log.read_text().replace("unreviewed,checked", "checked,checked"))  # a row with a wrong from_status
        o.tick("PACKET_ALL_HELD n=0 held=4 EVIDENCE_REVIEW_STATE_INVALID=4")
        pair = (o.facts.read_bytes(), o.log.read_bytes())
        self.edit(QUOTE, NEWER)
        code, text = self.replay()
        self.assertEqual(code, 1, text)
        self.assertIn(" ERROR FAILED facts-import", text)
        self.assertEqual((o.facts.read_bytes(), o.log.read_bytes()), pair)  # the active pair is untouched
        self.assertEqual(o.outbox(), [path])
        self.one_packet(row)

    def test_a_genuine_upstream_reopen_keeps_the_checked_fact_and_new_facts_are_unreviewed(self):
        o = self.o
        path, row = self.proposed()
        checked, history = self.facts()[self.n1], self.log()
        o.t.sync_agency(tt.tb.R2, ["Other first page", "Other second page"], "rq2", "2026-11-05T09:00:00Z")  # a new text for the agency
        with open(tr.FIX / "compare-facts.csv", "a") as handle:
            for n in (1, 2):
                handle.write("Sample City Police Department,REQ-102,new_fact_%d,%d,%s,1,New text %d.\n" % (n, n, "c" * 64, n))
        code, text = self.replay(redraft=False)
        self.assertEqual(code, 0, text)
        self.assertEqual(self.waits[0], "fact-drafts")  # re-opened by the upstream change alone
        facts = self.facts()
        self.assertEqual((facts[self.n1], self.log()), (checked, history))
        self.assertEqual(sorted(r["status"] for r in facts.values()), ["checked"] + ["unreviewed"] * 7)
        self.import_new([path])
        self.one_packet(row)

    def test_a_raw_status_edit_is_not_a_review_and_the_next_reconcile_restores_the_log_status(self):
        o = self.o
        path, row = self.proposed()
        o.facts.write_text("".join(l.replace(",unreviewed", ",checked") if o.cited["O1"] in l else l for l in o.facts.read_text().splitlines(True)))
        edited = o.facts.read_bytes()
        o.tick("PACKET_ALL_HELD n=0 held=4 EVIDENCE_REVIEW_STATE_INVALID=4")
        self.assertEqual(o.outbox(), [path])
        self.edit(VENDOR, VENDOR[:-1] + " Inc.")  # the next evidence change reconciles
        code, text = self.replay()
        self.assertEqual(code, 0, text)
        self.assertEqual(sorted(r["status"] for r in self.facts().values()), ["checked"] + ["unreviewed"] * 5)
        self.assertEqual(self.facts()[self.n1]["status"], "checked")  # the log-derived status
        (kept,) = (o.out / ".review" / "superseded" / "fact-reviews").iterdir()
        self.assertEqual((kept / "FACTS.csv").read_bytes(), edited)  # the pre-reconcile pair is kept
        self.import_new([path])
        self.one_packet(row)


if __name__ == "__main__":
    unittest.main()
