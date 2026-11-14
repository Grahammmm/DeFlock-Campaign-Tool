"""records decisions-apply: the console decision journal replayed on the live project inside the records run (DFGL-16C)."""
import errno
import io
import json
import os
import shutil
import subprocess
import sys
import types
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from campaign_tool.records import approval_queue as aq
from campaign_tool.records import cli, console_apply, console_export, console_journal
from campaign_tool.records import public_packets as pp
from campaign_tool.records import public_updates as pu
from tests.records.test_console_export import tree_state
from tests.records.test_console_journal import Journal
from tests.records.test_console_model import RUN
from tests.records.test_console_oversight import candidate

ROOT = Path(__file__).resolve().parents[2]
FOREIGN = "0" * 16


class Apply(Journal):
    def exported(self, name, ident):
        model = json.loads((self.root / "current" / "console-model" / name).read_text())
        return model["payloads"][ident]

    def sign(self, text):
        return self.signer.sign(text.encode())

    def journal_bytes(self):
        return (self.j / console_journal.FILE).read_bytes()

    def apply(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = console_apply.main(["--project", str(self.live), "--from-journal", str(self.j / console_journal.FILE)])
        self.assertEqual((code, err.getvalue()), (0, ""))
        return [line.split(" ", 3)[3] for line in out.getvalue().splitlines()]

    def row(self, ident):
        return next(r for r in aq.load_queue(self.live) if r["id"] == ident)

    def served(self, name):
        self.make_export(name)
        return self.get("/review?id=" + self.pending_id)


class FullCycle(Apply):
    def test_refresh_decide_apply_refresh_over_the_cli(self):
        self.make_export()
        self.up()
        self.assertEqual(self.decide("/approvals/reject", id=self.pending_id, reason="not ours", **{"return": "review"})[0], 303)
        (entry,) = self.lines()
        before = tree_state(self.live)
        result = subprocess.run([sys.executable, "-B", "-m", "campaign_tool.records", "decisions-apply", "--project",
                                 str(self.live), "--from-journal", "-"], input=self.journal_bytes(), cwd=ROOT,
                                capture_output=True)
        self.assertEqual((result.returncode, result.stderr), (0, b""))
        self.assertEqual(result.stdout.decode(), "decisions-apply: %s reject APPLIED\n" % entry["entry_id"][:12])
        self.assertEqual(self.row(self.pending_id)["status"], "rejected")
        self.assertNotEqual(tree_state(self.live), before)
        self.assertEqual(oct(os.stat(self.live / "approvals" / console_apply.FILE).st_mode & 0o777), "0o600")
        page = self.get("/review?id=" + self.pending_id)  # still export #1: the decision is pending apply
        self.assertIn("pending apply: reject", page)
        name = self.make_export(RUN + "-2")
        manifest = json.loads((self.root / "exports" / name / "EXPORT.json").read_text())
        self.assertEqual((manifest["applied_journal_ids"], manifest["apply_outcomes"]),
                         ([entry["entry_id"]], {entry["entry_id"]: {"outcome": "APPLIED"}}))
        model = json.loads((self.root / "current" / "console-model" / "queue.json").read_text())
        self.assertEqual({r["id"]: r["status"] for r in model["rows"]}[self.pending_id], "rejected")
        served = tree_state(self.root)
        page = self.get("/review?id=" + self.pending_id)
        self.assertIn("Rejected: not ours", page)
        self.assertNotIn("pending apply", page)
        self.assertEqual(tree_state(self.root), served)
        self.assertEqual(len(self.lines()), 1)  # the journal itself is never edited or truncated

    def test_reapplying_is_already_and_writes_nothing(self):
        self.make_export()
        self.up()
        self.decide("/approvals/reject", id=self.pending_id, reason="not ours")
        self.assertEqual(self.apply(), ["APPLIED"])
        queue, receipt = [(self.live / "approvals" / n).read_bytes() for n in ("queue.jsonl", console_apply.FILE)]
        self.assertEqual(self.apply(), ["ALREADY"])
        self.assertEqual([(self.live / "approvals" / n).read_bytes() for n in ("queue.jsonl", console_apply.FILE)],
                         [queue, receipt])


class Outcomes(Apply):
    def test_a_signature_over_the_exported_payload_approves_and_a_copy_id_payload_is_refused(self):
        self.make_export()
        self.up()
        payload = self.exported("queue.json", self.pending_id)
        self.assertIn("\nproject: " + self.pid + "\n", payload)
        copy_id = aq._project_id(self.tmp / "copy")
        refused = payload.replace("\nproject: " + self.pid + "\n", "\nproject: " + copy_id + "\n")
        self.assertNotEqual(refused, payload)
        self.assertEqual(self.decide("/approvals/approve", id=self.pending_id, signature=self.sign(refused))[0], 303)
        self.assertEqual(self.apply(), ["SIGNATURE_REFUSED"])
        self.assertEqual(self.row(self.pending_id)["status"], "pending")
        self.assertEqual(self.decide("/approvals/approve", id=self.pending_id, signature=self.sign(payload))[0], 303)
        self.assertEqual(self.apply(), ["ALREADY", "APPLIED"])  # a refused entry is not retried, the new decision is applied
        row = self.row(self.pending_id)
        self.assertEqual((row["status"], row["approval_evidence"]["project"]), ("approved", self.pid))
        name = self.make_export(RUN + "-3")
        manifest = json.loads((self.root / "exports" / name / "EXPORT.json").read_text())
        first, second = (e["entry_id"] for e in self.lines())
        self.assertEqual(manifest["applied_journal_ids"], [second])
        self.assertEqual(manifest["apply_outcomes"][first], {"outcome": "SIGNATURE_REFUSED", "refusal": "SIGNATURE_REFUSED"})
        page = self.get("/review?id=" + self.pending_id)
        self.assertIn("signature verified on the live project", page)
        self.assertNotIn("pending apply", page)

    def test_an_edit_after_another_edit_is_stale_and_changes_nothing(self):
        self.make_export()
        self.up()
        self.assertEqual(self.decide("/approvals/edit", id=self.pending_id, body="mine", expected_revision="0")[0], 303)
        aq.edit(self.live, self.pending_id, "other", "theirs", expected_revision=0)
        self.assertEqual(self.apply(), ["STALE_REVISION"])
        self.assertEqual((self.row(self.pending_id)["body"], self.row(self.pending_id)["revision"]), ("theirs", 1))
        name = self.make_export(RUN + "-2")
        manifest = json.loads((self.root / "exports" / name / "EXPORT.json").read_text())
        self.assertEqual((manifest["applied_journal_ids"], list(manifest["apply_outcomes"].values())),
                         ([], [{"outcome": "STALE_REVISION", "refusal": "STALE_REVISION"}]))
        self.assertIn("not applied: STALE_REVISION", self.get("/review?id=" + self.pending_id))

    def test_a_foreign_project_entry_is_refused_unreceipted_and_nothing_is_applied(self):
        self.make_export()
        pin = types.SimpleNamespace(source_project_id=FOREIGN, run_id=RUN, name="x")
        console_journal.append(str(self.j), pin, "operator", "/approvals/reject", "reject", self.pending_id, 0, "no")
        before = tree_state(self.live)
        self.assertEqual(self.apply(), ["PROJECT_MISMATCH"])
        self.assertEqual(tree_state(self.live), before)
        self.assertEqual(self.row(self.pending_id)["status"], "pending")
        self.assertFalse((self.live / "approvals" / console_apply.FILE).exists())

    def test_packet_and_oversight_decisions_go_through_the_existing_writers(self):
        self.make_export()
        self.up()
        revision = self.packet_revision(self.proposed)
        self.decide("/packets/approve", id=self.proposed, revision=revision,
                    signature=self.sign(self.exported("packets.json", self.proposed)))
        self.decide("/public-updates/off")
        self.assertEqual(self.apply(), ["APPLIED", "APPLIED"])
        self.assertEqual(next(r for r in pp.status(self.live)["packets"] if r["id"] == self.proposed)["status"], "approved")
        self.decide("/packets/reject", id=self.approved_packet, revision=self.packet_revision(self.approved_packet), reason="no")
        pp.decide(self.live, self.approved_packet, "rejected", "someone", reason="late")  # decided elsewhere first
        self.assertEqual(self.apply(), ["ALREADY", "ALREADY", "INVALID_TRANSITION"])

    def test_a_damaged_journal_or_receipt_file_refuses_before_anything_is_applied(self):
        (self.j / console_journal.FILE).write_text('{"schema": "x"}\n')
        err = io.StringIO()
        with redirect_stderr(err):
            code = console_apply.main(["--project", str(self.live), "--from-journal", str(self.j / console_journal.FILE)])
        self.assertEqual((code, err.getvalue()), (1, "decisions-apply: refused (journal_invalid)\n"))
        self.assertFalse((self.live / "approvals" / console_apply.FILE).exists())
        (self.j / console_journal.FILE).unlink()
        self.make_export()
        self.up()
        self.decide("/approvals/reject", id=self.pending_id, reason="no")
        receipts = self.live / "approvals" / console_apply.FILE
        receipts.write_text("not json\n")
        os.chmod(receipts, 0o600)
        with redirect_stderr(err):
            code = console_apply.main(["--project", str(self.live), "--from-journal", str(self.j / console_journal.FILE)])
        self.assertEqual(code, 1)
        self.assertIn("receipts_invalid", err.getvalue())
        self.assertEqual(self.row(self.pending_id)["status"], "pending")
        with self.assertRaises(console_export.ExportError):  # an export never claims an unreadable receipt file is empty
            console_export.build(str(self.live), RUN, str(self.tmp / "unused"))


class Interrupted(Apply):
    """A run that dies after the live mutation but before its outcome line is receipted must replay to the true outcome."""

    def lose_outcomes(self):
        real = console_apply._receipt

        def receipt(project, ident, outcome, base=None):
            if outcome != console_apply.INTENT:
                raise OSError(errno.ENOSPC, "no space")
            return real(project, ident, outcome, base)
        return mock.patch.object(console_apply, "_receipt", receipt)

    def interrupted_run(self):
        err = io.StringIO()
        with self.lose_outcomes(), redirect_stdout(io.StringIO()), redirect_stderr(err):
            code = console_apply.main(["--project", str(self.live), "--from-journal", str(self.j / console_journal.FILE)])
        self.assertEqual((code, err.getvalue()), (1, "decisions-apply: refused (OSError)\n"))

    def entry(self, route, action, target, revision, value, actor="operator"):
        pin = types.SimpleNamespace(source_project_id=self.pid, run_id=RUN, name="x")
        return console_journal.append(str(self.j), pin, actor, route, action, target, revision, value)

    def assert_replays_to_applied(self, ident):
        self.assertEqual(console_apply.ledger(self.live)[0], {})  # no outcome was receipted ...
        self.assertEqual(list(console_apply.ledger(self.live)[1]), [ident])  # ... only the write-ahead line
        self.assertEqual(self.apply(), ["APPLIED"])
        self.assertEqual(console_apply.receipts(self.live), {ident: "APPLIED"})
        self.assertEqual(self.apply(), ["ALREADY"])
        name = self.make_export(RUN + "-2")
        manifest = json.loads((self.root / "exports" / name / "EXPORT.json").read_text())
        self.assertEqual((manifest["applied_journal_ids"], manifest["apply_outcomes"]),
                         ([ident], {ident: {"outcome": "APPLIED"}}))

    def test_an_edit_whose_receipt_failed_is_applied_not_stale_on_replay(self):
        self.make_export()
        self.up()
        self.decide("/approvals/edit", id=self.pending_id, body="mine", expected_revision="0")
        (entry,) = self.lines()
        self.interrupted_run()
        self.assertEqual((self.row(self.pending_id)["body"], self.row(self.pending_id)["revision"]), ("mine", 1))
        self.assert_replays_to_applied(entry["entry_id"])
        self.assertEqual((self.row(self.pending_id)["body"], self.row(self.pending_id)["revision"]), ("mine", 1))  # once

    def test_a_reject_and_an_approve_whose_receipt_failed_replay_to_applied(self):
        self.make_export()
        self.up()
        self.decide("/approvals/reject", id=self.pending_id, reason="not ours")
        (entry,) = self.lines()
        self.interrupted_run()
        self.assert_replays_to_applied(entry["entry_id"])
        self.assertEqual(self.row(self.pending_id)["status"], "rejected")

    def test_a_signed_approve_whose_receipt_failed_replays_to_applied(self):
        self.make_export()
        self.up()
        self.decide("/approvals/approve", id=self.pending_id,
                    signature=self.sign(self.exported("queue.json", self.pending_id)))
        (entry,) = self.lines()
        self.interrupted_run()
        self.assertEqual(self.row(self.pending_id)["status"], "approved")
        self.assert_replays_to_applied(entry["entry_id"])

    def test_packet_approve_whose_receipt_failed_replays_to_applied(self):
        self.make_export()
        self.up()
        self.decide("/packets/approve", id=self.proposed, revision=self.packet_revision(self.proposed),
                    signature=self.sign(self.exported("packets.json", self.proposed)))
        (entry,) = self.lines()
        self.interrupted_run()
        self.assertEqual(next(r for r in pp.status(self.live)["packets"] if r["id"] == self.proposed)["status"], "approved")
        self.assert_replays_to_applied(entry["entry_id"])

    def test_packet_reject_whose_receipt_failed_replays_to_applied(self):
        self.make_export()
        ident = self.entry("/packets/reject", "packet_reject", self.approved_packet,
                           int(self.packet_revision(self.approved_packet)), "no")
        self.interrupted_run()
        self.assertEqual(next(r for r in pp.status(self.live)["packets"] if r["id"] == self.approved_packet)["status"],
                         "rejected")
        self.assert_replays_to_applied(ident)

    def test_an_oversight_retract_whose_receipt_failed_replays_to_applied(self):
        pu.init(self.live)
        cand = candidate()
        pu.propose(self.live, [cand])
        self.make_export()
        ident = self.entry("/public-updates/retract", "retract", cand["id"], 0, "undo")
        self.interrupted_run()
        self.assertEqual(pu.fold(console_apply._events(pu, self.live))[0][cand["id"]]["state"], "retracted")
        self.assert_replays_to_applied(ident)

    def test_a_lost_intent_applies_nothing_and_an_intent_without_a_landed_mutation_is_applied_once(self):
        self.make_export()
        self.up()
        self.decide("/approvals/reject", id=self.pending_id, reason="no")
        (entry,) = self.lines()
        real = console_apply._receipt

        def no_intent(project, ident, outcome, base=None):
            if outcome == console_apply.INTENT:
                raise OSError(errno.ENOSPC, "no space")
            return real(project, ident, outcome, base)
        before = tree_state(self.live)
        with mock.patch.object(console_apply, "_receipt", no_intent):
            self.assertEqual(self.apply(), ["DEFERRED"])  # no write-ahead line, so no mutation
        self.assertEqual((tree_state(self.live), self.row(self.pending_id)["status"]), (before, "pending"))
        base = console_apply._mark(self.live, console_journal.parse(self.journal_bytes())[0])
        console_apply._receipt(self.live, entry["entry_id"], console_apply.INTENT, base)  # died after intent, before mutation
        self.assertEqual(self.apply(), ["APPLIED"])
        self.assertEqual(self.row(self.pending_id)["status"], "rejected")

    def test_a_retry_after_a_deferred_attempt_and_an_intervening_edit_keeps_its_own_base(self):
        self.make_export()
        self.up()
        self.decide("/approvals/reject", id=self.pending_id, reason="no")
        (entry,) = self.lines()
        ident = entry["entry_id"]
        busy = aq.ApprovalQueueError("queue_busy")
        with mock.patch.object(aq, "reject", side_effect=busy):
            self.assertEqual(self.apply(), ["DEFERRED"])  # INTENT at base 0, the live writer failed before mutating
        self.assertEqual(console_apply.ledger(self.live), ({}, {ident: 0}))
        aq.edit(self.live, self.pending_id, "other", "someone else's edit", expected_revision=0)  # pending row -> revision 1
        self.interrupted_run()  # the retry rejects at revision 2 and loses its outcome line
        self.assertEqual((self.row(self.pending_id)["status"], self.row(self.pending_id)["revision"]), ("rejected", 2))
        self.assertEqual(console_apply.ledger(self.live), ({}, {ident: 1}))  # the retry's own, refreshed base
        self.assertEqual(self.apply(), ["APPLIED"])  # not INVALID_TRANSITION
        self.assertEqual(console_apply.receipts(self.live), {ident: "APPLIED"})
        self.assertEqual(self.row(self.pending_id)["revision"], 2)  # applied once

    def test_an_intent_is_not_an_outcome_and_a_damaged_intent_is_refused(self):
        self.make_export()
        self.up()
        self.decide("/approvals/reject", id=self.pending_id, reason="no")
        (entry,) = self.lines()
        console_apply._receipt(self.live, entry["entry_id"], console_apply.INTENT, 0)
        self.assertEqual(console_apply.ledger(self.live), ({}, {entry["entry_id"]: 0}))
        name = self.make_export(RUN + "-2")
        manifest = json.loads((self.root / "exports" / name / "EXPORT.json").read_text())
        self.assertEqual((manifest["applied_journal_ids"], manifest["apply_outcomes"]), ([], {}))
        receipts = self.live / "approvals" / console_apply.FILE
        receipts.write_text(json.dumps({"entry_id": "x", "outcome": "INTENT", "at": "t"}) + "\n")
        with self.assertRaises(aq.ApprovalQueueError):
            console_apply.ledger(self.live)


class StoreFaults(Apply):
    """An unreadable store says nothing about the decision: the entry is deferred (no receipt), never a final refusal."""

    def run_apply(self, project):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = console_apply.main(["--project", str(project), "--from-journal", str(self.j / console_journal.FILE)])
        return code, [line.split(" ", 3)[3] for line in out.getvalue().splitlines()], err.getvalue()

    def entry(self, pid, route, action, target, revision, value, actor="operator"):
        pin = types.SimpleNamespace(source_project_id=pid, run_id=RUN, name="x")
        return console_journal.append(str(self.j), pin, actor, route, action, target, revision, value)

    def test_a_moved_project_defers_its_queue_decisions_and_applies_them_once_readable(self):
        self.make_export()
        moved = self.tmp / "moved"
        shutil.copytree(self.live, moved, symlinks=True)  # its approvals still name the old path's project id
        new_pid = aq._project_id(moved)
        self.assertNotEqual(new_pid, self.pid)
        with self.assertRaises(aq.ApprovalQueueError) as raised:
            aq.load_queue(moved)
        self.assertEqual(raised.exception.code, "queue_invalid")
        ident = self.entry(new_pid, "/approvals/reject", "reject", self.pending_id, 0, "not ours")
        queue = moved / "approvals" / "queue.jsonl"
        before = tree_state(moved)
        for _ in range(2):  # not burned: the next pass is no ALREADY
            code, results, err = self.run_apply(moved)
            self.assertEqual((code, results), (0, ["DEFERRED"]))
            self.assertIn("deferred (store unreadable: queue_invalid)", err)
            self.assertEqual(tree_state(moved), before)  # no receipt, not even a write-ahead line
        self.assertFalse((moved / "approvals" / console_apply.FILE).exists())
        queue.write_bytes(queue.read_bytes().replace(self.pid.encode(), new_pid.encode()))  # the store is readable again
        self.assertEqual(self.run_apply(moved), (0, ["APPLIED"], ""))
        self.assertEqual(console_apply.receipts(moved), {ident: "APPLIED"})
        self.assertEqual(next(r for r in aq.load_queue(moved) if r["id"] == self.pending_id)["status"], "rejected")
        self.assertEqual(self.run_apply(moved), (0, ["ALREADY"], ""))

    def test_a_damaged_packet_or_public_updates_journal_defers_only_its_own_entries(self):
        pu.init(self.live)
        cand = candidate()
        pu.propose(self.live, [cand])
        self.make_export()
        self.entry(self.pid, "/approvals/reject", "reject", self.pending_id, 0, "no")
        packet = self.entry(self.pid, "/packets/reject", "packet_reject", self.approved_packet,
                            int(self.packet_revision(self.approved_packet)), "no")
        retract = self.entry(self.pid, "/public-updates/retract", "retract", cand["id"], 0, "undo")
        files = {name: self.live / name for name in (pp.JOURNAL, pu.JOURNAL)}
        good = {name: path.read_bytes() for name, path in files.items()}
        for path in files.values():
            path.write_bytes(path.read_bytes() + b"torn")
        code, results, err = self.run_apply(self.live)
        self.assertEqual((code, results), (0, ["APPLIED", "DEFERRED", "DEFERRED"]))  # the queue itself is readable
        self.assertEqual(err.count("deferred (store unreadable: journal_invalid)"), 2)
        self.assertEqual(set(console_apply.receipts(self.live).values()), {"APPLIED"})
        self.assertNotIn(packet, console_apply.receipts(self.live))
        self.assertNotIn(retract, console_apply.receipts(self.live))
        for name, path in files.items():
            path.write_bytes(good[name])
        self.assertEqual(self.run_apply(self.live), (0, ["ALREADY", "APPLIED", "APPLIED"], ""))

    def test_per_entry_refusals_stay_final(self):
        self.make_export()
        self.up()
        self.decide("/approvals/edit", id=self.pending_id, body="mine", expected_revision="0")
        aq.edit(self.live, self.pending_id, "other", "theirs", expected_revision=0)
        self.assertEqual(self.run_apply(self.live), (0, ["STALE_REVISION"], ""))
        self.assertEqual(self.run_apply(self.live), (0, ["ALREADY"], ""))
        self.assertEqual(set(console_apply.STORE_FAULTS) & set(console_apply.CODES), set())


class Registered(Apply):
    def test_the_command_is_registered(self):
        self.assertEqual(cli.COMMANDS["decisions-apply"], ("console_apply", "main"))
