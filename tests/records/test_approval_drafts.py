"""Cited reply drafts: selection, exact citations, no-send guarantee, CLI and the outputs-stage seam."""
import contextlib
import errno
import io
import json
import os
import shutil
import socket
import sqlite3
import stat
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from campaign_tool.records import approval_queue as aq
from campaign_tool.records import draft_templates as dt
from campaign_tool.records import followup_engine, run_stages
from campaign_tool import kit_track
from tests.approval_signing import Signer
from tests.test_records_run_stages import StageTestCase, synthetic_ledger, run_main

SHA = "a" * 64


def sha(n):
    return f"{n:064x}"


def finding(n, rating="gap", page=3, quote="Item 4 (camera audit logs) is not enclosed."):
    return {"finding_id": sha(n), "rating": rating, "rule_ref": {"citation": "Gov. Code 7922.535"},
            "policy_ref": None, "page": page, "quote": quote, "comparison_ref": {"row": n},
            "review_required": True, "needs_attorney_review": True, "legal_conclusion": False,
            "law_package_status": "draft", "disclaimer": "x"}


def item(n=1, kind="response", findings=(), agency="agency-a", request="req-1", deadlines=(), quote=None):
    other = kind == "other"
    return {"item_id": sha(n), "original_sha256": sha(n + 1000), "occurrence_ids": [], "agency_id": agency,
            "request_id": request, "received_at": "2026-11-02T18:59:00Z", "kind": kind,
            "kind_basis": {"page": None, "quote": None} if other else
            {"page": 1, "quote": quote or f"We are writing about your {kind} \u201crequest\u201d.\nSecond line."},
            "findings": list(findings), "deadlines": list(deadlines), "review_required": True}


def received(items, run_id="run-1"):
    return {"schema": "records-received-item-v1", "run_id": run_id, "review_required": True,
            "legal_conclusion": False, "law_package_status": "draft", "disclaimer": "d", "comparison_sha256": None,
            "items": items, "diagnostics": []}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "project"
        self.root.mkdir(mode=0o700)
        self.addCleanup(self.tmp.cleanup)

    def write(self, doc, run_id="run-1"):
        path = self.root / f"received-{run_id}.json"
        path.write_text(json.dumps(doc), encoding="utf-8")
        return path


class RendererTests(Base):
    def test_all_four_kinds_exact_citations_and_skips(self):
        deadline = {"date_text": "November 30", "date": "2026-11-30", "page": 1,
                    "quote": "We will respond by November 30.", "legal_deadline_calculation": False}
        items = [item(1, "extension", deadlines=[deadline]), item(2, "denial", [finding(20, "meets")]),
                 item(3, "fee"), item(4, "response", [finding(40), finding(41, page=7, quote="Exhibit B is missing.")]),
                 item(5, "response", [finding(50, "meets"), finding(51, "unknown")]), item(6, "other"),
                 item(7, "extension", agency=None), item(8, "records", [finding(80, "unknown")])]
        out = {i["item_id"]: dt.render(i) for i in items}
        self.assertEqual([None if out[sha(n)] is None else out[sha(n)][0] for n in range(1, 9)],
                         ["reply-extension", "appeal-denial", "fee-reply", "followup-missing-records",
                          None, None, None, None])
        ext = out[sha(1)][1]
        self.assertIn("November 30", ext)
        self.assertIn('page 1: "We will respond by November 30."', ext)
        self.assertIn("    | Second line.", ext)  # newline kept inside the delimited block
        self.assertIn(dt.DRAFT_BANNER, ext)
        self.assertIn("State: pending-approval", ext)
        self.assertIn("Target agency: agency-a", ext)
        self.assertNotIn("unlawful", ext.lower())
        miss = out[sha(4)][1]
        self.assertIn('1. page 3, finding', miss)
        self.assertIn('2. page 7, finding', miss)
        self.assertIn('"Exhibit B is missing."', miss)
        self.assertIn("not proof", miss)
        self.assertIn("Gov. Code 7922.535", miss)
        self.assertEqual(len(out[sha(4)][2]["findings"]), 2)
        self.assertIn("authorizes any charge or payment", out[sha(3)][1])
        self.assertIn("[EDIT ME: date the request was sent]", out[sha(2)][1])  # nothing invented
        self.assertIn("request for reconsideration", out[sha(2)][1])

    def test_denial_reuses_existing_template_constants(self):
        body = dt.render(item(2, "denial"))[1]
        self.assertIn(kit_track.ASK_REST, body)
        self.assertIn("Send the parts of the requested records that are not exempt", body)
        self.assertIn("Draft only. Not legal advice.", body)

    def test_malformed_sources_are_refused(self):
        bad = item(1, "denial")
        bad["kind_basis"] = {"page": True, "quote": "x"}
        for broken in (bad, {**item(1, "denial"), "kind_basis": {"page": 1, "quote": ""}},
                       item(2, "response", [finding(3, quote="")]), item(3, "response", [finding(3, page=0)]),
                       {**item(4), "agency_id": "a\nb"}):
            with self.subTest(broken=broken.get("item_id")), self.assertRaises(dt.DraftSourceError):
                dt.render(broken)

    def test_nothing_hardcoded_to_one_campaign(self):
        other = dt.render(item(9, "extension", agency="other-city-pd", request="REQ-77"))[1]
        self.assertIn("other-city-pd / REQ-77", other)


class QueueTests(Base):
    def test_batch_is_idempotent_and_private(self):
        path = self.write(received([item(1, "extension"), item(4, "response", [finding(40)]), item(6, "other")]))
        self.assertEqual(aq.queue_received_items(self.root, "run-1", path), {"created": 2, "existing": 0, "skipped": 1})
        again = self.write(received([item(1, "extension"), item(4, "response", [finding(40)])], "run-2"), "run-2")
        self.assertEqual(aq.queue_received_items(self.root, "run-2", again), {"created": 0, "existing": 2, "skipped": 0})
        rows = aq.load_queue(self.root)
        self.assertEqual({r["status"] for r in rows}, {"pending"})
        self.assertEqual([r["source_run_id"] for r in rows], ["run-1", "run-1"])
        self.assertEqual(stat.S_IMODE((self.root / "approvals").stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.root / "approvals" / "queue.jsonl").stat().st_mode), 0o600)

    def test_edit_reject_and_reordered_findings_never_overwritten(self):
        first = item(4, "response", [finding(40), finding(41, quote="Other gap.")])
        aq.queue_received_items(self.root, "run-1", self.write(received([first])))
        row = aq.load_queue(self.root)[0]
        aq.edit(self.root, row["id"], "G", "My edited body")
        aq.reject(self.root, row["id"], "G", "not now")
        flipped = item(4, "response", [finding(41, quote="Other gap."), finding(40)])
        summary = aq.queue_received_items(self.root, "run-2", self.write(received([flipped], "run-2"), "run-2"))
        self.assertEqual(summary["created"], 0)
        now = aq.load_queue(self.root)[0]
        self.assertEqual((now["status"], now["body"], now["reason"]), ("rejected", "My edited body", "not now"))
        with self.assertRaises(aq.ApprovalQueueError) as caught:
            aq.reject(self.root, row["id"], "G", "different reason")
        self.assertEqual(caught.exception.code, "invalid_transition")

    def test_changed_evidence_for_same_identity_conflicts(self):
        aq.queue_received_items(self.root, "run-1", self.write(received([item(4, "response", [finding(40)])])))
        changed = item(4, "response", [finding(40, quote="Different quote.")])
        with self.assertRaises(aq.ApprovalQueueError) as caught:
            aq.queue_received_items(self.root, "run-2", self.write(received([changed], "run-2"), "run-2"))
        self.assertEqual(caught.exception.code, "source_conflict")

    def test_reattributed_item_conflicts_and_preserves_edits_and_approval(self):
        for kind in ("extension", "fee"):
            for field, value in (("agency", "agency-b"), ("request", "req-2")):
                with self.subTest(kind=kind, field=field):
                    root = self.root.parent / f"project-{kind}-{field}"
                    root.mkdir(mode=0o700)
                    n = 7 if kind == "extension" else 8
                    path = root / "received-run-1.json"
                    path.write_text(json.dumps(received([item(n, kind)])), encoding="utf-8")
                    aq.queue_received_items(root, "run-1", path)
                    row = aq.load_queue(root)[0]
                    aq.edit(root, row["id"], "G", "My edited body")
                    moved = path.parent / "received-run-2.json"
                    moved.write_text(json.dumps(received([item(n, kind, **{field: value})], "run-2")), encoding="utf-8")
                    with self.assertRaises(aq.ApprovalQueueError) as caught:
                        aq.queue_received_items(root, "run-2", moved)
                    self.assertEqual(caught.exception.code, "source_conflict")
                    with self.assertRaises(aq.ApprovalQueueError) as caught:
                        aq.add_draft(root, item_id=sha(n), kind=row["kind"], agency_id=value if field == "agency" else "agency-a",
                                     request_id=value if field == "request" else "req-1", body=row["body"],
                                     evidence=row["evidence"], source_run_id="run-2")
                    self.assertEqual(caught.exception.code, "source_conflict")
                    now = aq.load_queue(root)[0]
                    self.assertEqual((now["agency_id"], now["request_id"], now["body"], now["status"]),
                                     ("agency-a", "req-1", "My edited body", "pending"))
                    self.assertEqual(len(aq.load_queue(root)), 1)

    def test_run_provenance_in_comparison_ref_is_not_evidence(self):
        def real(n, run, index, **kw):
            f = finding(n, **kw)
            f["comparison_ref"] = {"report_sha256": f"{hash((run, n)) & 0xfffffff:064x}", "row_index": index,
                                   "field": "audit_logs"}
            return f
        first = item(4, "response", [real(40, "run-1", 0)])
        aq.queue_received_items(self.root, "run-1", self.write(received([first])))
        original = aq.load_queue(self.root)[0]
        aq.edit(self.root, original["id"], "G", "My edited body")
        again = item(4, "response", [real(40, "run-2", 7)])
        new = item(5, "response", [real(50, "run-2", 8, quote="Exhibit C is missing.")])
        summary = aq.queue_received_items(self.root, "run-2", self.write(received([again, new], "run-2"), "run-2"))
        self.assertEqual((summary["created"], summary["existing"]), (1, 1))
        rows = {r["id"]: r for r in aq.load_queue(self.root)}
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[original["id"]]["body"], "My edited body")
        self.assertEqual(rows[original["id"]]["evidence"]["findings"][0]["comparison_ref"]["row_index"], 0)
        changed = item(4, "response", [real(40, "run-3", 0, quote="Different quote.")])
        with self.assertRaises(aq.ApprovalQueueError) as caught:
            aq.queue_received_items(self.root, "run-3", self.write(received([changed], "run-3"), "run-3"))
        self.assertEqual(caught.exception.code, "source_conflict")
        other_field = item(4, "response", [real(40, "run-3", 0)])
        other_field["findings"][0]["comparison_ref"]["field"] = "other"
        with self.assertRaises(aq.ApprovalQueueError):
            aq.queue_received_items(self.root, "run-3", self.write(received([other_field], "run-3"), "run-3"))

    def test_invalid_source_appends_nothing(self):
        bad = item(4, "response", [finding(40, page=0)])
        with self.assertRaises(aq.ApprovalQueueError) as caught:
            aq.queue_received_items(self.root, "run-1", self.write(received([item(1, "extension"), bad])))
        self.assertEqual(caught.exception.code, "source_invalid")
        self.assertFalse((self.root / "approvals").exists())
        with self.assertRaises(aq.ApprovalQueueError):  # wrong run id
            aq.queue_received_items(self.root, "run-9", self.write(received([item(1, "extension")])))
        self.assertEqual(aq.load_queue(self.root), [])
        self.assertFalse((self.root / "approvals").exists())  # reading creates nothing

    def test_signature_approval_scope_and_edit_invalidation(self):
        aq.queue_received_items(self.root, "run-1", self.write(received([item(1, "extension")])))
        ident = aq.load_queue(self.root)[0]["id"]
        signer = Signer()
        self.addCleanup(signer.close)
        for typed in ("G", "approved", {"kind": "ssh", "signature": "approved"}):
            with self.assertRaises(aq.ApprovalQueueError) as caught:
                aq.approve(self.root, ident, typed)
            self.assertEqual(caught.exception.code, "approval_required")
        with signer.trust():
            old = signer.sign(aq.approval_payload(self.root, ident))
            row = aq.approve(self.root, ident, {"kind": "ssh", "signature": old})
            self.assertEqual((row["status"], row["approved_by"]), ("approved", "owner@example.invalid"))
            self.assertEqual(aq.approve(self.root, ident, {"kind": "ssh", "signature": old})["status"], "approved")
            edited = aq.edit(self.root, ident, "G", "Changed body text")
            self.assertEqual(edited["status"], "pending")
            self.assertNotIn("approved_by", edited)
            with self.assertRaises(aq.ApprovalQueueError) as caught:  # stale signature for the old text
                aq.approve(self.root, ident, {"kind": "ssh", "signature": old})
            self.assertEqual(caught.exception.code, "approval_required")
            wrong_ns = signer.sign(aq.approval_payload(self.root, ident), namespace="other")
            with self.assertRaises(aq.ApprovalQueueError):
                aq.approve(self.root, ident, {"kind": "ssh", "signature": wrong_ns})
        # without trust a recorded approval is not trusted: it does not materialize as approved
        with signer.trust():
            aq.approve(self.root, ident, {"kind": "ssh", "signature": signer.sign(aq.approval_payload(self.root, ident))})
        self.assertEqual(aq.load_queue(self.root)[0]["status"], "pending")

    def test_forged_approval_event_is_not_approved_and_corrupt_tail_refused(self):
        aq.queue_received_items(self.root, "run-1", self.write(received([item(1, "extension")])))
        journal = self.root / "approvals" / "queue.jsonl"
        ident = aq.load_queue(self.root)[0]["id"]
        forged = {"schema": aq.SCHEMA, "event": "approve", "id": ident, "revision": 1, "at": "2026-11-05T00:00:00Z",
                  "actor": "mallory", "approval": {"signed_revision": 0, "project": aq._project_id(self.root),
                                                   "principal": "mallory", "signature": "-----BEGIN SSH SIGNATURE-----"}}
        with journal.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(forged) + "\n")
        self.assertEqual(aq.load_queue(self.root)[0]["status"], "pending")
        before = journal.read_bytes()
        journal.write_bytes(before + b'{"schema"')
        with self.assertRaises(aq.ApprovalQueueError) as caught:
            aq.load_queue(self.root)
        self.assertEqual(caught.exception.code, "queue_invalid")
        self.assertEqual(journal.read_bytes(), before + b'{"schema"')  # preserved, not repaired
        with self.assertRaises(aq.ApprovalQueueError) as writer:  # a writer refuses it too, as a bad format, not a denial
            aq.queue_received_items(self.root, "run-2", self.write(received([item(2, "extension")], "run-2"), "run-2"))
        self.assertEqual(writer.exception.code, "queue_invalid")
        self.assertEqual(journal.read_bytes(), before + b'{"schema"')

    def test_unsafe_store_is_refused(self):
        aq.queue_received_items(self.root, "run-1", self.write(received([item(1, "extension")])))
        journal = self.root / "approvals" / "queue.jsonl"
        os.chmod(journal, 0o644)
        with self.assertRaises(aq.ApprovalQueueError):
            aq.load_queue(self.root)
        os.chmod(journal, 0o600)
        link = self.root / "alias"
        os.link(journal, link)
        with self.assertRaises(aq.ApprovalQueueError):
            aq.load_queue(self.root)
        link.unlink()
        symlinked = Path(self.tmp.name) / "linked"
        symlinked.symlink_to(self.root)
        with self.assertRaises(aq.ApprovalQueueError):
            aq.load_queue(symlinked)

    # Contract: a writer's permission or read-only denial is queue_unwritable (DF2222-02), other I/O stays queue_io, and a
    # reader's mapping is unchanged. Fails if a denial still reads queue_invalid, the lock-create denial is swallowed
    # as a lost race (it then surfaces as queue_io), or a reader starts reporting queue_unwritable.
    def inject(self, name, number, call="open", when=lambda flags: True):
        real = getattr(os, call)

        def fake(path, *args, **kwargs):
            if path == name and (call == "mkdir" or when(args[0])):
                raise OSError(number, os.strerror(number))
            return real(path, *args, **kwargs)
        return mock.patch.object(os, call, fake)

    def test_writer_denials_are_unwritable_and_other_io_is_not(self):
        source = self.write(received([item(2, "extension")], "run-2"), "run-2")
        first = self.write(received([item(1, "extension")]))
        creating, rdonly = (lambda f: bool(f & os.O_CREAT)), (lambda f: f & os.O_ACCMODE == os.O_RDONLY)
        cases = (("empty", "approvals", "mkdir", None), ("full", "approvals", "open", None),
                 ("dir", ".lock", "open", creating), ("full", ".lock", "open", lambda f: not f & os.O_CREAT),
                 ("full", "queue.jsonl", "open", rdonly), ("full", "queue.jsonl", "open", lambda f: f & os.O_WRONLY))
        for n, (state, name, call, when) in enumerate(cases):
            for number, code in ((errno.EACCES, "queue_unwritable"), (errno.EPERM, "queue_unwritable"),
                                 (errno.EROFS, "queue_unwritable"), (errno.EIO, "queue_io")):
                with self.subTest(case=n, errno=number):
                    project = Path(self.tmp.name) / f"case{n}-{number}"
                    project.mkdir(mode=0o700)
                    if state == "dir":
                        (project / "approvals").mkdir(mode=0o700)
                    elif state == "full":
                        aq.queue_received_items(project, "run-1", first)
                    with self.inject(name, number, call, when or (lambda flags: True)):
                        with self.assertRaises(aq.ApprovalQueueError) as caught:
                            aq.queue_received_items(project, "run-2", source)
                        self.assertEqual(caught.exception.code, code)
        for n in (1, 3, 4):  # a reader keeps the old mapping for the same denial
            project = Path(self.tmp.name) / f"case{n}-{errno.EACCES}"
            name, when = cases[n][1], cases[n][3] or (lambda flags: True)
            with self.subTest(reader=n), self.inject(name, errno.EACCES, "open", when):
                with self.assertRaises(aq.ApprovalQueueError) as caught:
                    aq.load_queue(project)
                self.assertEqual(caught.exception.code, "queue_invalid")

    def test_lost_lock_creation_race_is_still_recovered(self):
        real, source = os.open, self.write(received([item(1, "extension")]))
        (self.root / "approvals").mkdir(mode=0o700)

        def racing(path, flags, *args, **kwargs):
            if path == ".lock" and flags & os.O_CREAT:
                real(path, flags, *args, **kwargs)  # the other writer's lock lands first
                raise FileExistsError(errno.EEXIST, "exists")
            return real(path, flags, *args, **kwargs)
        with mock.patch.object(os, "open", racing):
            self.assertEqual(aq.queue_received_items(self.root, "run-1", source)["created"], 1)

    def test_unsafe_entry_stays_invalid_even_when_the_open_is_denied(self):
        source = self.write(received([item(1, "extension")]))
        second = self.write(received([item(2, "extension")], "run-2"), "run-2")
        target = Path(self.tmp.name) / "target"
        target.mkdir(mode=0o700)
        (self.root / "approvals").symlink_to(target)
        for denied in (False, True):
            with self.subTest(symlink=True, denied=denied):
                with self.inject("approvals", errno.EACCES) if denied else contextlib.nullcontext():
                    with self.assertRaises(aq.ApprovalQueueError) as caught:
                        aq.queue_received_items(self.root, "run-1", source)
                self.assertEqual(caught.exception.code, "queue_invalid")
                self.assertEqual(list(target.iterdir()), [])  # nothing was written through the link
        (self.root / "approvals").unlink()
        aq.queue_received_items(self.root, "run-1", source)
        os.chmod(self.root / "approvals", 0o755)
        for denied in (False, True):
            with self.subTest(mode=0o755, denied=denied):
                with self.inject("approvals", errno.EACCES) if denied else contextlib.nullcontext():
                    with self.assertRaises(aq.ApprovalQueueError) as caught:
                        aq.queue_received_items(self.root, "run-2", second)
                self.assertEqual(caught.exception.code, "queue_invalid")
        os.chmod(self.root / "approvals", 0o700)
        for denied in (False, True):
            with self.subTest(owner=True, denied=denied), mock.patch.object(os, "geteuid", lambda: os.getuid() + 1):
                with self.inject("approvals", errno.EACCES) if denied else contextlib.nullcontext():
                    with self.assertRaises(aq.ApprovalQueueError) as caught:
                        aq.queue_received_items(self.root, "run-2", second)
                self.assertEqual(caught.exception.code, "queue_invalid")

    def test_invalid_inputs_append_nothing(self):
        aq.queue_received_items(self.root, "run-1", self.write(received([item(1, "extension")])))
        ident = aq.load_queue(self.root)[0]["id"]
        journal = self.root / "approvals" / "queue.jsonl"
        before = journal.read_bytes()
        for call in (lambda: aq.edit(self.root, ident, "G", "  "), lambda: aq.reject(self.root, ident, "G", ""),
                     lambda: aq.edit(self.root, "nope", "G", "x"), lambda: aq.edit(self.root, ident, "", "x"),
                     lambda: aq.edit(self.root, ident, "G", "x" * (aq.MAX_BODY + 1))):
            with self.assertRaises(aq.ApprovalQueueError):
                call()
        self.assertEqual(journal.read_bytes(), before)

    def test_never_sends_or_promotes(self):
        aq.queue_received_items(self.root, "run-1", self.write(received([item(1, "extension")])))
        ident = aq.load_queue(self.root)[0]["id"]
        signer = Signer()
        self.addCleanup(signer.close)
        with mock.patch.object(socket.socket, "connect", side_effect=AssertionError("network")), \
                mock.patch("smtplib.SMTP", side_effect=AssertionError("smtp")), signer.trust():
            aq.edit(self.root, ident, "G", "body")
            aq.approve(self.root, ident, {"kind": "ssh", "signature": signer.sign(aq.approval_payload(self.root, ident))})
        self.assertEqual(sorted(p.name for p in self.root.iterdir() if p.name != "received-run-1.json"), ["approvals"])


def _race(args):
    root, n = args
    return aq.add_draft(root, item_id=sha(1), kind="reply-extension", agency_id="a", request_id="r",
                        body=f"body {n}", evidence={"x": 1}, source_run_id="run-1")["id"]


class RaceAndCliTests(Base):
    def test_parallel_add_same_item_one_draft(self):
        import multiprocessing
        with multiprocessing.get_context("fork").Pool(4) as pool:
            ids = pool.map(_race, [(str(self.root), n) for n in range(8)])
        self.assertEqual(len(set(ids)), 1)
        self.assertEqual(len(aq.load_queue(self.root)), 1)

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", ["approvals", *argv]), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            try:
                code = aq.main()
            except SystemExit as exit_:
                code = exit_.code
        return code, out.getvalue(), err.getvalue()

    def test_cli_list_payload_approve_reject_edit(self):
        aq.queue_received_items(self.root, "run-1", self.write(received([item(1, "extension"), item(2, "fee")])))
        code, out, _ = self.cli("list", "--project", str(self.root), "--status", "pending")
        self.assertEqual((code, len(json.loads(out))), (0, 2))
        first, second = (r["id"] for r in json.loads(out))
        body = Path(self.tmp.name) / "body.txt"
        body.write_text("edited via cli", encoding="utf-8")
        self.assertEqual(self.cli("edit", first, "--project", str(self.root), "--actor", "G", "--body-file", str(body))[0], 0)
        self.assertEqual(self.cli("reject", second, "--project", str(self.root), "--actor", "G", "--reason", "dup")[0], 0)
        self.assertEqual(self.cli("list", "--project", str(self.root), "--status", "rejected")[1].count('"id"'), 1)
        self.assertEqual(self.cli("approve", "zz", "--project", str(self.root), "--signature", str(body))[2].strip(), "unknown_id")
        code, _, err = self.cli("approve", first, "--project", str(self.root), "--signature", str(body))
        self.assertEqual((code, err.strip()), (1, "approval_required"))  # a typed name is not a signature
        self.assertEqual(self.cli("nonsense")[0], 2)
        signer = Signer()
        self.addCleanup(signer.close)
        sig = Path(self.tmp.name) / "sig.txt"
        with signer.trust():
            sig.write_text(signer.sign(aq.approval_payload(self.root, first)), encoding="utf-8")
            code, out, _ = self.cli("approve", first, "--project", str(self.root), "--signature", str(sig))
        self.assertEqual((code, json.loads(out)["status"]), (0, "approved"))

    def test_records_cli_dispatch_and_singular_approve_unchanged(self):
        from campaign_tool.records import cli
        self.assertEqual(cli.COMMANDS["approvals"], "approval_queue")
        self.assertEqual(cli.COMMANDS["approve"], ("publish", "approve_main"))


class OutputsSeamTests(StageTestCase):
    def seam(self, doc, fail=None):
        if not self.ledger.exists():
            synthetic_ledger(self.ledger)
            with closing(sqlite3.connect(self.portal / "portal.sqlite")) as pdb, pdb:
                pdb.execute("CREATE TABLE portal_items(id TEXT)")
            (self.processing / "runs").mkdir(mode=0o700)
            (self.processing / "runs" / "extract-run-1.json").write_text(json.dumps(
                {"schema": "records-extract-outcomes-v1", "run_id": "run-1", "documents": []}))
        from campaign_tool.records.outcomes import received_items, response_items

        def fake(project, run_id):
            target = Path(project) / "run-outputs" / run_id / "outcomes"
            target.mkdir(parents=True, mode=0o700, exist_ok=True)
            if doc is not None:
                (target / "received-items.json").write_text(json.dumps(doc), encoding="utf-8")
        # Response ratings are their own outputs writer with their own tests; keep this seam about drafts only.
        with mock.patch.object(received_items, "write_run_received_items", fake), \
                mock.patch.object(response_items, "write_run_response_items", lambda project, run_id: None), \
                mock.patch.object(followup_engine, "run", lambda project, run_id, apply=False: None):  # needs ratings
            return self.stage("outputs")

    def test_outputs_stage_queues_pending_drafts_once(self):
        doc = received([item(1, "extension"), item(4, "response", [finding(40)])], run_id="run-1")
        code, parsed, _ = self.seam(doc)
        self.assertEqual((code, parsed["status"]), (0, "ok"))
        self.assertEqual({r["status"] for r in aq.load_queue(self.project)}, {"pending"})
        self.assertEqual(len(aq.load_queue(self.project)), 2)
        self.seam(doc)
        self.assertEqual(len(aq.load_queue(self.project)), 2)
        names = {p.name for p in self.project.rglob("*")}
        self.assertFalse(names & {"outbox.sqlite", "EMAIL-APPROVAL.json"})

    def test_missing_current_artifact_cannot_draft_from_older_run(self):
        self.seam(received([item(1, "extension")], run_id="run-1"))
        before = len(aq.load_queue(self.project))
        other = self.project / "run-outputs" / "run-1" / "outcomes" / "received-items.json"
        other.unlink()
        code, parsed, _ = self.seam(None)
        self.assertEqual((code, parsed["status"], parsed["error_code"]), (0, "warn", "OUTPUTS_APPROVAL_QUEUE_UNAVAILABLE"))
        self.assertEqual(len(aq.load_queue(self.project)), before)

    def test_queue_failure_is_additive_warning_and_timeout_propagates(self):
        doc = received([item(1, "extension")], run_id="run-1")
        with mock.patch.object(aq, "queue_received_items", side_effect=aq.ApprovalQueueError("queue_busy")):
            code, parsed, raw = self.seam(doc)
        self.assertEqual((code, parsed["error_code"]), (0, "OUTPUTS_APPROVAL_QUEUE_UNAVAILABLE"))
        self.assertTrue((self.project / "run-outputs" / "run-1.json").is_file())  # snapshot kept
        self.assertNotIn("queue_busy", raw)
        with mock.patch.object(aq, "queue_received_items", side_effect=run_stages.StageTimeout()):
            code, parsed, _ = self.seam(doc)
        self.assertEqual((code, parsed["error_code"]), (1, "STAGE_CONTAINER_TIMEOUT"))  # not swallowed as a warning

    # Contract: only queue_unwritable gets the new stage code; every other failure keeps the old one. Fails if a
    # format fault or a busy queue is reported as unwritable, or a denial stays the generic code or leaks its text.
    def test_only_an_unwritable_queue_gets_the_unwritable_code(self):
        doc = received([item(1, "extension")], run_id="run-1")
        for queue_code, expected in (("queue_unwritable", "OUTPUTS_APPROVAL_QUEUE_UNWRITABLE"),
                                     ("queue_invalid", "OUTPUTS_APPROVAL_QUEUE_UNAVAILABLE"),
                                     ("queue_io", "OUTPUTS_APPROVAL_QUEUE_UNAVAILABLE")):
            with self.subTest(queue_code=queue_code), \
                    mock.patch.object(aq, "queue_received_items", side_effect=aq.ApprovalQueueError(queue_code)):
                code, parsed, raw = self.seam(doc)
            self.assertEqual((code, parsed["status"], parsed["error_code"]), (0, "warn", expected))
            self.assertEqual([i["code"] for i in parsed["needs_g"]].count(expected), 1)
            self.assertNotIn(queue_code, raw)

    # Contract: a project the runner cannot write (the live root-owned project) reads as unwritable through the real
    # queue and the real outputs stage, while the reader still sees an empty queue. Fails if the denial is reported
    # as a format fault. Needs real permission checks, so it cannot run as root.
    @unittest.skipIf(os.geteuid() == 0, "root bypasses directory permissions")
    def test_read_only_project_reports_unwritable_queue(self):
        doc = received([item(1, "extension")], run_id="run-1")
        self.seam(doc)  # fixture, output descendants and a current received-items file
        shutil.rmtree(self.project / "approvals")
        os.chmod(self.project, 0o500)
        try:
            with self.assertRaises(aq.ApprovalQueueError) as caught:
                aq.queue_received_items(self.project, "run-1",
                                        self.project / "run-outputs" / "run-1" / "outcomes" / "received-items.json")
            self.assertEqual(caught.exception.code, "queue_unwritable")
            self.assertEqual(aq.load_queue(self.project), [])
            code, parsed, raw = self.seam(doc)
        finally:
            os.chmod(self.project, 0o700)
        self.assertEqual((code, parsed["status"], parsed["error_code"]), (0, "warn", "OUTPUTS_APPROVAL_QUEUE_UNWRITABLE"))
        codes = [i["code"] for i in parsed["needs_g"]]
        self.assertEqual(codes.count("OUTPUTS_APPROVAL_QUEUE_UNWRITABLE"), 1)
        self.assertNotIn("OUTPUTS_APPROVAL_QUEUE_UNAVAILABLE", codes)
        self.assertFalse((self.project / "approvals").exists())


if __name__ == "__main__":
    unittest.main()
