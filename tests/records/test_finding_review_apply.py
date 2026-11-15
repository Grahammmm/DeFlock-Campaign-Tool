"""DF2007-06: the next ``records analysis-tick --from-journal`` applies journaled console finding decisions, receipted, before staging.

Real chain: console POST -> decision journal -> analysis-tick -> finding_review (the DF2007-05 writer) -> receipts -> packet outbox.
Synthetic only; the approver is a throwaway SSH key trusted in-process.
"""
import json
import os
import stat
import types
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool import approval
from campaign_tool.records import console_apply, console_journal as cj, finding_review as fr, finding_review_apply as fa
from tests.approval_signing import Signer
from tests.records import test_console_findings as cf
from tests.records.test_console_export import tree_state
from tests.records.test_console_model import RUN


class FindingApply(cf.FindingConsole):
    def start(self):
        self.up_findings()

    def tick(self, *more):
        code, out = self.o.t.tick(*self.tick_args(*more))
        return code, out

    def lines_of(self, out, prefix="finding-decision"):
        return [line.split(" ", 2)[2] for line in out.splitlines() if line.startswith(prefix)]

    def receipts(self):
        path = self.ar / ".analysis" / "finding-decisions-applied.jsonl"
        return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []

    def review_events(self):
        path = self.ar / ".analysis" / "finding-reviews.jsonl"
        return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []

    def packet_line(self, out):
        return next(x for x in out.splitlines() if x.startswith("%s PACKET_" % self.slug))

    def test_journal_signed_approve_then_next_tick_stages_packet(self):
        self.start()
        signature = self.sign()
        self.assertEqual(self.post("approve", signature=signature)[0], 303)
        (entry,) = self.lines()
        self.assertEqual(self.o.outbox(), [])  # the console wrote nothing: still unreviewed and unstaged
        code, out = self.tick()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.lines_of(out), ["finding_approve APPLIED"])
        self.assertLess(out.index("finding-decision"), out.index("PACKET_"))  # applied before any staging
        self.assertIn("PACKET_STAGED", self.packet_line(out))
        (path,) = self.o.outbox()
        self.assertEqual(json.loads(path.read_text())["packets"][0]["provenance"]["finding_id"], "N1")
        self.assertEqual([(e["decision"], e["entry_id"]) for e in self.review_events()], [("approve", entry["entry_id"])])
        (receipt,) = self.receipts()
        self.assertEqual((set(receipt), receipt["entry_id"], receipt["outcome"]), ({"entry_id", "at", "outcome"}, entry["entry_id"], "APPLIED"))
        self.assertEqual(stat.S_IMODE(os.stat(self.ar / ".analysis" / "finding-decisions-applied.jsonl").st_mode), 0o600)
        self.assertNotIn("SSH SIGNATURE", (self.ar / ".analysis" / "finding-decisions-applied.jsonl").read_text())
        code, out = self.tick()  # a repeat: no second review event, no second packet
        self.assertEqual((code, self.lines_of(out)), (0, ["finding_approve ALREADY"]))
        self.assertIn("PACKET_UNCHANGED", self.packet_line(out))
        self.assertEqual((len(self.review_events()), len(self.receipts()), self.o.outbox()), (1, 1, [path]))

    def test_reject_applies_private_reason_and_never_stages(self):
        self.start()
        self.post("reject", reason="Overstates what the log shows.")
        code, out = self.tick()
        self.assertEqual((code, self.lines_of(out)), (0, ["finding_reject APPLIED"]))
        self.assertIn("PACKET_ALL_HELD", self.packet_line(out))
        self.assertIn("FINDING_REJECTED=1", self.packet_line(out))
        self.assertEqual(self.o.outbox(), [])
        row = self.frow()
        self.assertEqual((row["review_state"], row["reason"], row["hold_code"]), ("rejected", "Overstates what the log shows.", "FINDING_REJECTED"))
        for name in ("finding-decisions-applied.jsonl", "analysis-ledger.jsonl"):  # the reason never reaches a receipt or the code-only ledger
            self.assertNotIn("Overstates", (self.ar / ".analysis" / name).read_text())

    def test_stale_claim_entry_is_receipted_stale_without_review(self):
        self.start()
        self.post("approve", signature=self.sign())
        self.edit_claim("A claim that changed after the page was loaded.")  # between journal and apply
        code, out = self.tick()
        self.assertEqual((code, self.lines_of(out)), (0, ["finding_approve STALE_FINDING"]))
        self.assertEqual(self.review_events(), [])  # a signature valid for the old finding appended nothing
        self.assertEqual(self.o.outbox(), [])
        self.assertEqual([r["outcome"] for r in self.receipts()], ["STALE_FINDING"])
        self.assertEqual(self.lines_of(self.tick()[1]), ["finding_approve ALREADY"])  # receipted once

    def test_one_bad_entry_does_not_block_a_healthy_one(self):
        self.start()
        pin = types.SimpleNamespace(source_project_id=self.pid, run_id=RUN, name="x")
        binding = fa.root_binding(self.ar)
        cj.append(str(self.j), pin, "operator", "/findings/review/reject", "finding_reject", self.slug + ":NOPE", "a" * 64,
                  cj._canon({"analysis_root_sha256": binding, "reason": "x"}), kind=cj.FINDING_KIND)
        self.post("reject", reason="Real reason.")
        code, out = self.tick()
        self.assertEqual((code, self.lines_of(out)), (0, ["finding_reject REFUSED finding_unknown", "finding_reject APPLIED"]))

    def test_root_mismatch_and_wrong_kind_are_not_consumed(self):
        self.start()
        pin = types.SimpleNamespace(source_project_id=self.pid, run_id=RUN, name="x")
        cj.append(str(self.j), pin, "operator", "/findings/review/reject", "finding_reject", self.slug + ":N1", self.frow()["finding_sha256"],
                  cj._canon({"analysis_root_sha256": "b" * 64, "reason": "other root"}), kind=cj.FINDING_KIND)
        code, out = self.tick()
        self.assertEqual((code, self.lines_of(out), self.receipts(), self.review_events()), (0, [], [], []))  # another root owns it
        # the project apply leaves a finding entry for the tick (no receipt, no permanent refusal) and still applies an old action
        self.decide("/approvals/reject", id=self.pending_id, reason="not ours")
        before = (self.live / "approvals" / console_apply.FILE).exists()
        self.assertEqual(self.apply(), ["ANALYSIS_TICK_REQUIRED", "APPLIED"])
        self.assertFalse(before)
        self.assertEqual(set(console_apply.receipts(self.live).values()), {"APPLIED"})
        self.assertEqual(self.row(self.pending_id)["status"], "rejected")

    def test_unavailable_trust_defers_and_a_forged_signature_is_refused_for_good(self):
        self.start()
        self.post("approve", signature=self.sign())
        with mock.patch.object(approval, "TRUSTED_SIGNERS", Path("/nonexistent/deflock-trust/approvers.allowed_signers")):
            code, out = self.tick()
        self.assertEqual((code, self.lines_of(out)), (0, ["finding_approve DEFERRED"]))
        self.assertEqual((self.receipts(), self.review_events(), self.o.outbox()), ([], [], []))  # not final: nothing receipted
        code, out = self.tick()  # trust restored
        self.assertEqual(self.lines_of(out), ["finding_approve APPLIED"])
        self.assertEqual(len(self.o.outbox()), 1)

    def test_a_malformed_or_untrusted_signature_is_a_final_refusal(self):
        self.start()
        stranger = Signer("stranger@example.invalid", trusted=False)
        self.addCleanup(stranger.close)
        self.post("approve", signature=stranger.sign(self.frow()["payload"].encode("utf-8")))
        code, out = self.tick()
        self.assertEqual((code, self.lines_of(out)), (0, ["finding_approve SIGNATURE_REFUSED"]))
        self.assertEqual((self.review_events(), self.o.outbox(), [r["outcome"] for r in self.receipts()]), ([], [], ["SIGNATURE_REFUSED"]))

    def test_crash_after_review_before_receipt_is_exactly_once(self):
        self.start()
        self.post("approve", signature=self.sign())
        (entry,) = self.lines()
        with mock.patch.object(fa, "_receipt", side_effect=OSError("disk")):
            code, out = self.tick()
        self.assertEqual(code, 1)
        self.assertIn("FINDING_APPLY_REFUSED finding_apply_io", out)
        self.assertEqual(([e["entry_id"] for e in self.review_events()], self.receipts(), self.o.outbox()), ([entry["entry_id"]], [], []))
        fr.reject(self.ar, self.slug, "N1", "Changed my mind.")  # a later, intervening decision stays effective
        code, out = self.tick()
        self.assertEqual((code, self.lines_of(out)), (0, ["finding_approve APPLIED"]))
        self.assertEqual([e["decision"] for e in self.review_events()], ["approve", "reject"])  # the approve was not replayed
        self.assertEqual(self.frow()["review_state"], "rejected")
        self.assertEqual(self.o.outbox(), [])

    def test_dry_run_writes_nothing(self):
        self.start()
        self.post("approve", signature=self.sign())
        before = tree_state(self.ar)
        code, out = self.o.t.tick(*self.tick_args("--dry-run"))
        self.assertEqual((code, self.lines_of(out)), (0, ["finding_approve WOULD_APPLY"]))
        self.assertEqual(tree_state(self.ar), before)

    def test_an_unreadable_receipt_file_refuses_the_pass_before_any_staging(self):
        self.start()
        self.post("approve", signature=self.sign())
        receipts = self.ar / ".analysis" / "finding-decisions-applied.jsonl"
        receipts.write_text("{broken\n")
        os.chmod(receipts, 0o600)
        code, out = self.tick()
        self.assertEqual(code, 1)
        self.assertIn("FINDING_APPLY_REFUSED finding_receipts_unreadable", out)
        self.assertNotIn("PACKET_", out)
        self.assertEqual((self.review_events(), self.o.outbox()), ([], []))

    def test_the_journal_kind_is_strict_and_old_entries_stay_byte_compatible(self):
        pin = types.SimpleNamespace(source_project_id=self.pid, run_id=RUN, name="x")
        cj.append(str(self.j), pin, "operator", "/approvals/reject", "reject", "x", 0, "no")
        old = (self.j / cj.FILE).read_bytes()
        self.assertNotIn(b'"kind"', old)
        self.assertEqual(len(cj.parse(old)), 1)
        for action, kind in (("finding_approve", cj.FINDING_KIND), ("reject", "other_kind")):  # one entry each, alone in its own journal
            journal = self.tmp / ("j-" + action)
            journal.mkdir(mode=0o700)
            cj.append(str(journal), pin, "operator", "/x", action, "a:b", "c" * 64, "{}", kind=kind)
            with self.assertRaises(cj.JournalError):  # a malformed finding envelope and an unknown kind are never parsed
                cj.parse((journal / cj.FILE).read_bytes())


if __name__ == "__main__":
    unittest.main()
