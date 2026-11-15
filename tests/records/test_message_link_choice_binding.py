"""DF2007-01: a signed message tie names the one request it ties to; a substituted choice never appends a link."""
import hashlib
import html
import io
import json
import re
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from campaign_tool import approval
from campaign_tool.records import approval_queue as aq, console_apply, console_export, draft_templates
from campaign_tool.records.outcomes import unmatched
from tests.records import test_outcome_received_items as _ri
from tests.records import test_outcome_unmatched as _u
from tests.records.test_console_apply import Apply
from tests.records.test_console_model import RUN

LINE_A, LINE_B = "choice: req-a (agency caltrans)", "choice: req-b (agency caltrans)"


def entry(ident, signature, choice, name="e1", revision=0):
    return {"entry_id": hashlib.sha256(name.encode()).hexdigest(), "action": "approve", "target_id": ident,
            "revision_seen": revision, "value": signature, "actor": "operator", "choice": choice}


def legacy_text(root, row):
    """The signed text of main before this change, written out independently of approval_queue._review_text."""
    return "\n".join(["project: " + aq._project_id(root), "id: " + row["id"], "agency: " + row["agency_id"],
                      "request: " + row["request_id"], "item: " + row["item_id"], "kind: " + row["kind"],
                      "body:", row["body"], "evidence:", json.dumps(row["evidence"], sort_keys=True, separators=(",", ":"),
                                                                    ensure_ascii=False)])


class Binding(unittest.TestCase):
    setUp0 = _ri.ReceivedItemsTests.setUp
    private, document, enroll, tree, stage = (_ri.ReceivedItemsTests.private, _ri.ReceivedItemsTests.document,
                                              _ri.ReceivedItemsTests.enroll, _ri.ReceivedItemsTests.tree,
                                              _ri.ReceivedItemsTests.stage)
    setUp = _u.UnmatchedTests.setUp
    outcomes, run_outputs, server, row, post = (_u.UnmatchedTests.outcomes, _u.UnmatchedTests.run_outputs,
                                                _u.UnmatchedTests.server, _u.UnmatchedTests.row, _u.UnmatchedTests.post)

    def tie(self):
        self.run_outputs()
        return self.row()["id"]

    def sign(self, ident, choice):
        return self.signer.sign(aq.approval_payload(self.project, ident, choice=choice))

    @property
    def links(self):
        return self.project / "message-links.jsonl"

    def test_the_payload_carries_one_choice_line_after_request(self):
        ident = self.tie()
        text = aq.approval_payload(self.project, ident, choice="req-a").decode()
        lines = text.split("\n")
        at = lines.index("request: unmatched")
        self.assertEqual((lines[at + 1], lines[at + 2][:6]), (LINE_A, "item: "))
        self.assertEqual(aq.message_link_choice_line(self.row(), "req-b"), LINE_B)
        self.assertNotEqual(aq.approval_payload(self.project, ident, choice="req-b"), aq.approval_payload(self.project, ident, choice="req-a"))
        for choice in (None, "req-x", "", "nope"):  # no generic signable tie text exists
            with self.assertRaises(aq.ApprovalQueueError) as caught:
                aq.approval_payload(self.project, ident, choice=choice)
            self.assertEqual(caught.exception.code, "message_link_choice")

    def test_signature_for_a_refuses_b_before_any_link(self):
        ident = self.tie()
        sig = self.sign(ident, "req-a")
        before = self.tree()
        self.assertEqual(console_apply.outcome(self.project, entry(ident, sig, "req-b")), "SIGNATURE_REFUSED")
        self.assertEqual(self.row()["status"], "pending")
        self.assertFalse(self.links.exists())
        self.assertEqual(self.tree(), before)
        self.assertEqual(console_apply.outcome(self.project, entry(ident, sig, "req-a")), "APPLIED")
        (line,) = self.links.read_text().splitlines()
        self.assertEqual(json.loads(line)["request_id"], "req-a")
        self.assertEqual(self.row()["approved_choice"], "req-a")
        link_bytes = self.links.read_bytes()
        self.assertEqual(console_apply.outcome(self.project, entry(ident, sig, "req-a")), "APPLIED")  # same signature, same choice: no-op
        self.assertEqual(console_apply.outcome(self.project, entry(ident, sig, "req-b")), "SIGNATURE_REFUSED")  # and still not B
        self.assertEqual(self.links.read_bytes(), link_bytes)

    def test_direct_console_uses_the_same_selected_payload(self):
        ident = self.tie()
        server, http = self.server()
        page = http.send("GET", "/review?id=%s&choice=req-a" % ident, headers={"Content-Length": None, "Content-Type": None})[2]
        self.assertIn(html.escape(LINE_A), page)
        shown = re.search(r'<pre id="approval-payload">(.*?)</pre>', page, re.S).group(1)
        self.assertEqual(html.unescape(shown).encode(), aq.approval_payload(self.project, ident, choice="req-a"))
        self.assertIn('<input type="hidden" name="choice" value="req-a">', page)
        for bad in ("req-x", "", "req-a&choice=req-b"):
            self.assertEqual(http.send("GET", "/review?id=%s&choice=%s" % (ident, bad),
                                       headers={"Content-Length": None, "Content-Type": None})[0], 400, bad)
        sig = self.sign(ident, "req-a")
        before = self.tree()
        self.assertEqual(self.post(http, server, "approve", ident, signature=sig, choice="req-b"), 403)
        self.assertEqual((self.tree(), self.links.exists(), self.row()["status"]), (before, False, "pending"))
        self.assertEqual(self.post(http, server, "approve", ident, signature=sig, choice="req-a"), 303)
        self.assertEqual(len(self.links.read_text().splitlines()), 1)
        self.assertEqual(self.post(http, server, "approve", ident, signature=sig, choice="req-b"), 403)
        self.assertEqual(len(self.links.read_text().splitlines()), 1)

    def test_interrupted_approve_cannot_retarget_missing_link(self):
        ident = self.tie()
        sig = self.sign(ident, "req-a")
        original = entry(ident, sig, "req-a", name="original")
        tampered = entry(ident, sig, "req-b", name="tampered")  # its own recomputed entry id: not corrupt transport
        with mock.patch.object(console_apply, "_append_link", side_effect=OSError("synthetic")):
            self.assertIsNone(console_apply.settle(self.project, original, {}))  # approval landed, link append failed
        self.assertEqual((self.row()["status"], self.links.exists()), ("approved", False))
        console_apply._receipt(self.project, original["entry_id"], console_apply.INTENT, 0)
        console_apply._receipt(self.project, tampered["entry_id"], console_apply.INTENT, 0)
        intents = console_apply.ledger(self.project)[1]
        for fresh in ({}, intents):  # with and without a write-ahead line for the changed entry
            self.assertEqual(console_apply.settle(self.project, tampered, fresh), "SIGNATURE_REFUSED")
            self.assertFalse(self.links.exists())
        self.assertEqual(console_apply.settle(self.project, original, intents), "APPLIED")
        self.assertEqual(console_apply.settle(self.project, original, intents), "APPLIED")
        (line,) = self.links.read_text().splitlines()
        self.assertEqual(json.loads(line)["request_id"], "req-a")

    def test_choice_validation_and_legacy_approval_are_fail_closed(self):
        ident = self.tie()
        sig = self.sign(ident, "req-a")
        for choice in (None, "", "req-x", 7):
            self.assertEqual(console_apply.outcome(self.project, entry(ident, sig, choice)), "REFUSED message_link_choice")
        # duplicate and malformed candidates cannot be chosen, so nothing can be signed for them
        sha = "cd" * 32
        options = [{"request_id": "dup", "agency_id": "caltrans"}, {"request_id": "dup", "agency_id": "caltrans"},
                   {"request_id": "bad\nid", "agency_id": "caltrans"}, {"request_id": "ok", "agency_id": "caltrans"}]
        aq.queue_message_links(self.project, "test-run", [unmatched._draft(
            {"message_sha256": sha, "received_date": None, "sender_domain": None, "attachment_count": 0,
             "candidates": options, "agency_id": "caltrans"})])
        other = aq.draft_id(sha, draft_templates.MESSAGE_LINK)
        for choice in ("dup", "bad\nid", "gone"):
            with self.assertRaises(aq.ApprovalQueueError):
                aq.approval_payload(self.project, other, choice=choice)
        self.assertTrue(aq.approval_payload(self.project, other, choice="ok"))
        # a historical tie approval signed over the old generic text has no bound choice: unverified, never approved
        row = next(r for r in aq.load_queue(self.project) if r["id"] == ident)
        legacy = self.signer.sign(approval.payload(aq.SCOPE, ident, legacy_text(self.project, row), round_=0))
        with aq._Store(self.project, True) as store:
            store.append([aq._event("approve", ident, 1, self.signer.principal, approval={
                "signed_revision": 0, "project": aq._project_id(self.project), "principal": self.signer.principal,
                "signature": legacy})])
        row = next(r for r in aq.load_queue(self.project) if r["id"] == ident)
        self.assertEqual((row["status"], row["approval_unverified"]), ("pending", "untrusted"))
        self.assertNotIn("approved_choice", row)
        for choice in ("req-a", "req-b"):
            self.assertEqual(console_apply.outcome(self.project, entry(ident, legacy, choice)), "INVALID_TRANSITION")
        self.assertFalse(self.links.exists())

    def test_edit_clears_the_bound_choice_and_non_ties_refuse_one(self):
        ident = self.tie()
        aq.approve(self.project, ident, {"kind": "ssh", "signature": self.sign(ident, "req-a")}, choice="req-a")
        self.assertEqual(self.row()["approved_choice"], "req-a")
        aq.edit(self.project, ident, "operator", "Which request?\nedited")
        row = self.row()
        self.assertEqual(row["status"], "pending")
        self.assertNotIn("approved_choice", row)
        plain = aq.add_draft(self.project, item_id="%064x" % 1, kind="reply-extension", agency_id="caltrans",
                             request_id="req-a", body="Please extend.", evidence={"source": "test"}, source_run_id="test-run")
        with self.assertRaises(aq.ApprovalQueueError) as caught:
            aq.approval_payload(self.project, plain["id"], choice="req-a")
        self.assertEqual(caught.exception.code, "invalid_choice")
        sig = self.signer.sign(aq.approval_payload(self.project, plain["id"]))
        with self.assertRaises(aq.ApprovalQueueError) as caught:
            aq.approve(self.project, plain["id"], {"kind": "ssh", "signature": sig}, choice="req-a")
        self.assertEqual(caught.exception.code, "invalid_choice")
        self.assertEqual(aq.approve(self.project, plain["id"], {"kind": "ssh", "signature": sig})["status"], "approved")
        with aq._Store(self.project, False) as store:
            events = [json.loads(line) for line in store.raw().splitlines()]
        self.assertTrue(all("choice" not in (e.get("approval") or {}) for e in events if e["id"] == plain["id"]))

    def test_empty_candidate_list_offers_rejection_not_signing(self):
        ident = self.tie()
        sha = "ef" * 32
        aq.queue_message_links(self.project, "test-run", [unmatched._draft(
            {"message_sha256": sha, "received_date": None, "sender_domain": None, "attachment_count": 0,
             "candidates": [], "agency_id": "caltrans"})])
        server, http = self.server()
        page = http.send("GET", "/review?id=" + aq.draft_id(sha, "message_link"),
                         headers={"Content-Length": None, "Content-Type": None})[2]
        self.assertIn("/approvals/reject", page)
        self.assertNotIn("/approvals/approve", page)
        self.assertNotIn("approval-payload", page)
        self.assertTrue(ident)

    def test_non_message_payloads_are_exact_main_bytes(self):
        for kind, body, evidence in (("reply-extension", "Please extend.", {"source": "test", "b": 1, "a": ["é", 2]}),
                                     ("appeal-denial", "Line one\r\nLine two\n", {"z": {"y": 1, "x": None}}),
                                     ("first-request", "Unicode \u2603 body", {"findings": [{"k": "v"}], "source": "x"})):
            made = aq.add_draft(self.project, item_id=hashlib.sha256(kind.encode()).hexdigest(), kind=kind, agency_id="caltrans",
                                request_id="req-9", body=body, evidence=evidence, source_run_id="test-run")
            row = next(r for r in aq.load_queue(self.project) if r["id"] == made["id"])
            expected = approval.payload(aq.SCOPE, row["id"], legacy_text(self.project, row), round_=0)
            self.assertEqual(aq.approval_payload(self.project, row["id"]), expected, kind)
            self.assertNotIn(b"choice:", expected)

    def test_unavailable_trust_defers_and_leaves_files_unchanged(self):
        ident = self.tie()
        sig = self.sign(ident, "req-a")
        before = self.tree()
        with mock.patch.object(approval, "verify", side_effect=approval.TrustUnavailable("anchor")):
            self.assertIsNone(console_apply.outcome(self.project, entry(ident, sig, "req-a")))
        self.assertEqual((self.tree(), self.links.exists()), (before, False))
        self.assertEqual(console_apply.outcome(self.project, entry(ident, sig, "req-a")), "APPLIED")

    def test_cli_payload_and_approve_take_the_choice(self):
        ident = self.tie()
        out = io.BytesIO()
        argv = ["records approvals", "payload", "--project", str(self.project), ident, "--choice", "req-b"]
        with mock.patch("sys.argv", argv), mock.patch("sys.stdout", mock.Mock(buffer=out)):
            self.assertEqual(aq.main(), 0)
        self.assertEqual(out.getvalue(), aq.approval_payload(self.project, ident, choice="req-b"))
        sigfile = self.project / "sig.txt"
        sigfile.write_text(self.sign(ident, "req-b"))
        sigfile.chmod(0o600)
        argv = ["records approvals", "approve", "--project", str(self.project), ident, "--signature", str(sigfile), "--choice", "req-a"]
        err = io.StringIO()
        with mock.patch("sys.argv", argv), redirect_stderr(err):
            self.assertEqual(aq.main(), 1)
        self.assertEqual(err.getvalue().strip(), "approval_required")
        argv[-1] = "req-b"
        with mock.patch("sys.argv", argv), redirect_stdout(io.StringIO()):
            self.assertEqual(aq.main(), 0)
        self.assertEqual(self.row()["approved_choice"], "req-b")
        with mock.patch("sys.argv", ["records approvals", "reject", "--project", str(self.project), ident, "--actor", "a",
                                     "--reason", "r", "--choice", "req-a"]), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            aq.main()


    def test_the_approvals_document_names_the_bound_field(self):
        text = (Path(__file__).resolve().parents[2] / "docs" / "RECORDS-CONSOLE-APPROVALS.md").read_text()
        self.assertIn("`choice: <request_id> (agency <agency_id>)`", text)
        self.assertIn("message_link_choice_line", text)


class ExportBinding(Apply):
    def tie(self):
        sha = "ab" * 32
        options = [{"request_id": "req-b", "agency_id": "caltrans"}, {"request_id": "req-a", "agency_id": "caltrans"}]
        aq.queue_message_links(self.live, RUN, [unmatched._draft(
            {"message_sha256": sha, "received_date": "2026-11-09", "sender_domain": "caltrans.example",
             "attachment_count": 1, "candidates": options, "agency_id": "caltrans"})])
        return aq.draft_id(sha, "message_link")

    def page(self, ident, choice):
        return html.unescape(self.get("/review?id=%s&choice=%s" % (ident, choice)))

    def test_export_selection_binds_original_project(self):
        ident = self.tie()
        self.make_export()
        model = json.loads((self.root / "current" / "console-model" / "queue.json").read_text())
        self.assertNotIn(ident, model["payloads"])
        self.assertIn(self.pending_id, model["payloads"])  # a non-tie payload entry is unchanged
        self.assertEqual(model["link_payloads"], {ident: {c: aq.approval_payload(self.live, ident, choice=c).decode()
                                                         for c in ("req-a", "req-b")}})
        self.up()
        chosen = {}
        for choice in ("req-a", "req-b"):  # the exported bytes are exactly the live ones for each candidate
            chosen[choice] = aq.approval_payload(self.live, ident, choice=choice).decode()
            self.assertIn(chosen[choice], self.page(ident, choice))
        self.assertNotIn("approval-payload", self.get("/review?id=" + ident))  # nothing signable before a selection
        sig = self.sign(chosen["req-a"])
        self.assertEqual(self.decide("/approvals/approve", id=ident, signature=sig, choice="req-a", **{"return": "review"})[0], 303)
        self.assertEqual(self.decide("/approvals/approve", id=ident, signature=sig, choice="req-b", **{"return": "review"})[0], 303)
        (a, b) = [e for e in self.lines() if e["action"] == "approve"]
        self.assertEqual((a["choice"], b["choice"]), ("req-a", "req-b"))
        self.assertEqual(console_apply.outcome(self.live, b), "SIGNATURE_REFUSED")
        self.assertFalse((self.live / "message-links.jsonl").exists())
        self.assertEqual(console_apply.outcome(self.live, a), "APPLIED")
        (line,) = (self.live / "message-links.jsonl").read_text().splitlines()
        self.assertEqual(json.loads(line)["request_id"], "req-a")

    def test_decisions_apply_refuses_the_substituted_choice(self):
        ident = self.tie()
        self.make_export()
        self.up()
        sig = self.sign(aq.approval_payload(self.live, ident, choice="req-a").decode())
        self.assertEqual(self.decide("/approvals/approve", id=ident, signature=sig, choice="req-b", **{"return": "review"})[0], 303)
        self.assertEqual(self.apply(), ["SIGNATURE_REFUSED"])
        self.assertFalse((self.live / "message-links.jsonl").exists())
        self.assertEqual(next(r for r in aq.load_queue(self.live) if r["id"] == ident)["status"], "pending")

    def test_old_snapshot_without_link_payloads_withholds_tie_approval_only(self):
        ident = self.tie()
        real = console_export._model_queue

        def old(project, now):
            model = real(project, now)
            del model["link_payloads"]
            return model
        with mock.patch.dict(console_export.MODEL, {"queue.json": old}):
            self.make_export()
        self.up()
        page = self.get("/review?id=%s&choice=req-a" % ident)
        self.assertIn("Refresh the export", page)
        self.assertNotIn("approval-payload", page)
        self.assertNotIn('name="signature"', page)
        self.assertIn("approval-payload", self.get("/review?id=" + self.pending_id))  # other pages keep working


if __name__ == "__main__":
    unittest.main()
