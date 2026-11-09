"""First-request queue replays: a changed generated letter is refused, never silently kept as different text.

Synthetic policy data and a throwaway project only; real approval journal APIs, no mocks of persisted state.
"""
import csv
import io
import os
import tempfile
import unicodedata
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from campaign_tool.records import approval_queue as aq, first_request as fr, playbook as pb
from tests.approval_signing import Signer

BASIS = "the Example Public Records Act"
AGENCY = "example-agency"
SHA = "ab" * 32
HEADER = ["agency", "policy_id", "section", "quote", "doc_sha256", "page", "adopted_date"]
QUOTE = "Audits of law enforcement requests for data will be conducted each calendar year."
REFUSED = "records first-request: refused draft_content_conflict\n"


def policy_rows(sha=SHA):
    return [{"agency": AGENCY, "policy_id": "Policy 1", "section": "1.1", "quote": QUOTE, "doc_sha256": sha,
             "page": "2", "adopted_date": ""}]


def make(sha=SHA, **over):
    args = dict(agency=AGENCY, agency_name="Example Agency", start="2026-07-01", end="2026-09-30", basis=BASIS,
                requester="A. Requester")
    args.update(over)
    return fr.draft(pb.load(), policy_rows(sha), **args)


def swap(result, **body_change):
    """The same evidence with a different generated body."""
    return dict(result, body=body_change["body"])


def snapshot(root):
    """Every path under the project with its mode and bytes."""
    out = {}
    for path in sorted(Path(root).rglob("*")):
        info = path.lstat()
        out[str(path.relative_to(root))] = (info.st_mode, None if path.is_dir() else path.read_bytes())
    return out


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.project("project")

    def project(self, name):
        root = Path(self.tmp.name) / name
        root.mkdir(mode=0o700)
        return root

    def queue(self, result, root=None, run="r1"):
        return fr.queue(root or self.root, result, agency=AGENCY, request_id="first-1", run_id=run)

    def conflicts(self, result, root=None):
        before = snapshot(root or self.root)
        with self.assertRaises(fr.FirstRequestError) as caught:
            self.queue(result, root, run="r-conflict")
        self.assertEqual((caught.exception.code, caught.exception.detail), ("draft_content_conflict", ""))
        self.assertEqual(snapshot(root or self.root), before)


class ChangedGenerationTests(Base):
    def test_changed_generated_fields_refuse_without_journal_mutation(self):
        original = make()
        self.queue(original)
        for field, value in (("requester", "B. Requester"), ("agency_name", "Another Name"),
                             ("basis", "a different public records law")):
            changed = make(**{field: value})
            self.assertEqual(changed["evidence"], original["evidence"])
            self.assertNotEqual(changed["body"], original["body"])
            self.conflicts(changed)
            self.assertEqual(len(aq.load_queue(self.root)), 1)
        self.assertEqual(self.queue(original)["id"], aq.load_queue(self.root)[0]["id"])

    def test_conflict_is_per_project(self):
        other = self.project("other")
        original, changed = make(), make(requester="B. Requester")
        self.queue(original)
        self.queue(changed, other)  # first draft of the other project is its own original
        self.conflicts(changed)
        self.conflicts(original, other)
        self.assertEqual(self.queue(original)["id"], self.queue(changed, other)["id"])  # same identity, own bodies
        self.assertEqual(aq.history(self.root, aq.load_queue(self.root)[0]["id"])["original"]["body"], original["body"])
        self.assertEqual(aq.history(other, aq.load_queue(other)[0]["id"])["original"]["body"], changed["body"])

    def test_changed_evidence_still_refuses_as_source_conflict(self):
        self.queue(make())
        before = snapshot(self.root)
        with self.assertRaises(aq.ApprovalQueueError) as caught:
            self.queue(make(sha="cd" * 32))
        self.assertEqual(caught.exception.code, "source_conflict")
        self.assertEqual(snapshot(self.root), before)

    def test_damaged_history_is_not_downgraded_to_success(self):
        self.queue(make())
        journal = self.root / "approvals" / "queue.jsonl"
        damaged = journal.read_bytes() + b'{"schema"'
        journal.write_bytes(damaged)
        with self.assertRaises(aq.ApprovalQueueError) as caught:
            self.queue(make())
        self.assertEqual(caught.exception.code, "queue_invalid")
        self.assertEqual(journal.read_bytes(), damaged)


class ExactReplayTests(Base):
    def test_exact_replay_preserves_manual_edits_and_lifecycle(self):
        original = make()
        signer = Signer()
        self.addCleanup(signer.close)
        edited_root, rejected_root = self.project("edited"), self.project("rejected")
        for root in (edited_root, rejected_root):
            ident = self.queue(original, root)["id"]
        with signer.trust():
            aq.edit(edited_root, ident, "G", original["body"] + "Organizer note.\n")
            aq.approve(edited_root, ident, {"kind": "ssh", "signature": signer.sign(aq.approval_payload(edited_root, ident))})
        aq.reject(rejected_root, ident, "G", "not needed")
        for root, status in ((edited_root, "approved"), (rejected_root, "rejected")):
            with signer.trust():
                before_row, before = aq.load_queue(root)[0], snapshot(root)
                self.assertEqual(before_row["status"], status)
                for run in ("r2", "r3"):
                    self.assertEqual(self.queue(original, root, run=run), before_row)
                self.assertEqual(snapshot(root), before)
                self.assertEqual(aq.load_queue(root)[0], before_row)
                self.assertEqual(len(aq.load_queue(root)), 1)
                self.conflicts(make(requester="B. Requester"), root)
        self.assertEqual(aq.history(edited_root, ident)["current"]["body"], original["body"] + "Organizer note.\n")

    def test_current_body_equality_does_not_hide_changed_generation(self):
        original, changed = make(), make(requester="B. Requester")
        ident = self.queue(original)["id"]
        aq.edit(self.root, ident, "G", changed["body"])
        self.conflicts(changed)
        row = self.queue(original)  # the original generation is still accepted and keeps the edit
        self.assertEqual((row["id"], row["body"]), (ident, changed["body"]))
        self.assertEqual(aq.history(self.root, ident)["original"]["body"], original["body"])

    def test_body_comparison_is_exact(self):
        original = make()
        body = original["body"]
        self.queue(original)
        variants = {
            "trailing space": body.replace("A. Requester", "A. Requester "),
            "extra final newline": body + "\n",
            "no final newline": body.rstrip("\n"),
            "crlf": body.replace("\n", "\r\n"),
            "unicode": body.replace("A. Requester", "Á. Requester"),
            "doubled space": body.replace("Records requested:", "Records  requested:"),
        }
        for label, changed in variants.items():
            with self.subTest(label):
                self.assertNotEqual(changed, body)
                self.conflicts(swap(original, body=changed))
        self.assertEqual(self.queue(swap(original, body=body))["body"], body)

    def test_unicode_normalization_form_is_not_ignored(self):
        composed = make(requester=unicodedata.normalize("NFC", "Élan Requester"))
        decomposed = make(requester=unicodedata.normalize("NFD", "Élan Requester"))
        self.assertNotEqual(composed["body"], decomposed["body"])
        self.queue(composed)
        self.conflicts(decomposed)


class InsertionRaceTests(Base):
    def race(self, winner_body, result):
        real = aq.add_draft

        def add_with_winner(root, **kwargs):
            real(root, **dict(kwargs, body=winner_body, source_run_id="winner"))
            return real(root, **kwargs)

        with mock.patch.object(aq, "add_draft", add_with_winner):
            return self.queue(result)

    def test_competing_insertion_checks_returned_original(self):
        mine, theirs = make(), make(requester="B. Requester")
        with self.assertRaises(fr.FirstRequestError) as caught:
            self.race(theirs["body"], mine)
        self.assertEqual(caught.exception.code, "draft_content_conflict")
        rows = aq.load_queue(self.root)
        self.assertEqual(len(rows), 1)
        self.assertEqual(aq.history(self.root, rows[0]["id"])["original"]["body"], theirs["body"])
        journal = (self.root / "approvals" / "queue.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(journal), 1)  # the winner's draft is the only event; no conflict event, no rollback

    def test_competing_insertion_with_same_body_succeeds(self):
        mine = make()
        row = self.race(mine["body"], mine)
        self.assertEqual(row["id"], aq.load_queue(self.root)[0]["id"])
        self.assertEqual(len((self.root / "approvals" / "queue.jsonl").read_text(encoding="utf-8").splitlines()), 1)


class CliTests(Base):
    def setUp(self):
        super().setUp()
        self.policies = Path(self.tmp.name) / "policies.csv"
        with self.policies.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=HEADER)
            writer.writeheader()
            writer.writerows(policy_rows())

    def run_cli(self, requester, queue=True):
        argv = ["--agency", AGENCY, "--agency-name", "Example Agency", "--policies", str(self.policies),
                "--window-start", "2026-07-01", "--window-end", "2026-09-30", "--basis", BASIS,
                "--requester", requester]
        if queue:
            argv += ["--queue", str(self.root), "--run-id", "run-" + requester[0]]
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = fr.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_cli_conflict_is_content_free_and_prints_no_new_letter(self):
        code, out, err = self.run_cli("A. Requester")
        self.assertEqual((code, err), (0, "status: ready for approval\n"))
        self.assertIn("A. Requester", out)
        before = snapshot(self.root)
        code, out, err = self.run_cli("B. Requester")
        self.assertEqual((code, out, err), (2, "", REFUSED))
        self.assertEqual(snapshot(self.root), before)
        code, again, err = self.run_cli("A. Requester")  # exact replay keeps working
        self.assertEqual((code, err), (0, "status: ready for approval\n"))
        self.assertEqual(again, self.run_cli("A. Requester", queue=False)[1])
        self.assertEqual(snapshot(self.root), before)
        code, rendered, err = self.run_cli("B. Requester", queue=False)  # no --queue: changed inputs still render
        self.assertEqual((code, err), (0, "status: ready for approval\n"))
        self.assertIn("B. Requester", rendered)
        self.assertEqual(snapshot(self.root), before)


if __name__ == "__main__":
    unittest.main()
