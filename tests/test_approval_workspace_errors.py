"""approval.verify: temporary-workspace I/O failures are fixed-message TrustUnavailable, never raw OSError."""
import contextlib
import errno
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool import approval, outbox as ob
from campaign_tool.records import approval_queue as aq
from tests.approval_signing import Signer
from tests.records.test_console_model import ModelBase

FIXED = "signature check could not run"
CANARY = "SYNTHETIC_PRIVATE_TEMP_CANARY"
DATA = b"synthetic approval payload\n"
real_write_text = Path.write_text
RealTD = tempfile.TemporaryDirectory


def cleanup_fails(error=None, run_real=True):
    """TemporaryDirectory whose single cleanup raises after (optionally) really cleaning up."""
    class TD(RealTD):
        calls = []

        def cleanup(self):
            TD.calls.append(self.name)
            if run_real:
                super().cleanup()
            raise error or OSError(errno.EIO, CANARY)
    return TD


def partial_write(error):
    def write(self, text, *a, **k):
        real_write_text(self, text[:10], *a, **k)
        raise error
    return write


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.signer = Signer()
        cls.sig = cls.signer.sign(DATA)

    @classmethod
    def tearDownClass(cls):
        cls.signer.close()

    def setUp(self):
        stack = self.signer.trust()
        stack.__enter__()
        self.addCleanup(stack.__exit__)

    def assertUnavailable(self, **kw):
        with self.assertRaises(approval.TrustUnavailable) as ctx:
            approval.verify(DATA, self.sig)
        self.assertEqual(str(ctx.exception), FIXED)
        return ctx.exception


class WorkspaceFailures(Base):
    def test_real_nondirectory_temp_root_is_unavailable(self):
        with tempfile.TemporaryDirectory() as parent:
            blocker = Path(parent) / "blocker"
            blocker.write_bytes(b"keep")
            with mock.patch.object(tempfile, "tempdir", str(blocker)), \
                    mock.patch.object(approval.subprocess, "run") as run:
                self.assertUnavailable()
            run.assert_not_called()
            self.assertEqual(blocker.read_bytes(), b"keep")
        for code in (errno.EACCES, errno.ENOSPC, errno.EMFILE):
            with self.subTest(code=code), mock.patch.object(approval.tempfile, "TemporaryDirectory",
                                                            side_effect=OSError(code, CANARY)):
                self.assertUnavailable()
        self.assertEqual(approval.verify(DATA, self.sig), self.signer.principal)

    def test_signature_partial_write_oserrors_are_unavailable(self):
        for code in (errno.EIO, errno.ENOSPC):
            with self.subTest(code=code), mock.patch.object(Path, "write_text", partial_write(OSError(code, CANARY))), \
                    mock.patch.object(approval.subprocess, "run") as run:
                error = self.assertUnavailable()
                run.assert_not_called()
                self.assertNotIn(CANARY, str(error))
        self.assertEqual(approval.verify(DATA, self.sig), self.signer.principal)

    def test_cleanup_only_oserror_never_returns_principal(self):
        td = cleanup_fails()
        with mock.patch.object(approval.tempfile, "TemporaryDirectory", td):
            self.assertUnavailable()
        self.assertEqual(len(td.calls), 1)
        self.assertFalse(Path(td.calls[0]).exists())
        # cleanup failure also beats an unclassified mismatch result
        with mock.patch.object(approval.tempfile, "TemporaryDirectory", cleanup_fails()):
            with self.assertRaises(approval.TrustUnavailable):
                approval.verify(DATA + b"x", self.sig)

    def test_retained_workspace_is_unavailable_and_sentinel_untouched(self):
        with tempfile.TemporaryDirectory() as parent:
            sentinel = Path(parent) / "sentinel"
            sentinel.write_text("keep")
            retained = []

            class TD(RealTD):
                def cleanup(self):
                    retained.append(self)
                    raise PermissionError(errno.EACCES, CANARY)
            with mock.patch.object(approval.tempfile, "TemporaryDirectory", TD):
                self.assertUnavailable()
            self.assertEqual(sentinel.read_text(), "keep")
            for workspace in retained:
                shutil.rmtree(workspace.name, ignore_errors=True)
                # Disarm the finalizer: otherwise garbage collection later emits an "Implicitly cleaning up"
                # ResourceWarning into whichever unrelated test is running, failing its empty-stderr assertion.
                workspace._finalizer.detach()


class PrimarySurvives(Base):
    def test_untrusted_key_refusal_wins_over_cleanup_oserror(self):
        other = Signer("other@example.invalid", trusted=False)
        self.addCleanup(other.close)
        td = cleanup_fails()
        with mock.patch.object(approval.tempfile, "TemporaryDirectory", td):
            with self.assertRaises(approval.ApprovalError) as ctx:
                approval.verify(DATA, other.sign(DATA))
        self.assertEqual(str(ctx.exception), "signature is not from a trusted approver")
        self.assertEqual(len(td.calls), 1)

    def test_subprocess_failure_with_cleanup_oserror_is_fixed_unavailable(self):
        for exc in (OSError(errno.EIO, "x"), approval.subprocess.TimeoutExpired("k", 30)):
            with self.subTest(exc=type(exc).__name__), \
                    mock.patch.object(approval.tempfile, "TemporaryDirectory", cleanup_fails()), \
                    mock.patch.object(approval.subprocess, "run", side_effect=exc):
                self.assertUnavailable()

    def test_secondary_cleanup_oserror_preserves_active_primary(self):
        for primary in (RuntimeError("p"), MemoryError(), KeyboardInterrupt(), SystemExit(3)):
            with self.subTest(primary=type(primary).__name__), \
                    mock.patch.object(approval.tempfile, "TemporaryDirectory", cleanup_fails()), \
                    mock.patch.object(approval.subprocess, "run", side_effect=primary):
                with self.assertRaises(type(primary)) as ctx:
                    approval.verify(DATA, self.sig)
                self.assertIs(ctx.exception, primary)

    def test_non_oserror_cleanup_failure_is_not_swallowed(self):
        with mock.patch.object(approval.tempfile, "TemporaryDirectory", cleanup_fails(RuntimeError("boom"))):
            with self.assertRaises(RuntimeError):
                approval.verify(DATA, self.sig)


class ContractsUnchanged(Base):
    def test_mismatch_and_preflight_contracts_unchanged(self):
        with self.assertRaises(approval.ApprovalError) as ctx:
            approval.verify(DATA + b"tamper", self.sig)
        self.assertEqual(str(ctx.exception), "signature does not match the approved text")
        with self.assertRaises(approval.ApprovalError):
            approval.verify(DATA, self.signer.sign(DATA, namespace="other"))
        with self.assertRaises(approval.ApprovalError):
            approval.verify(DATA, "-----BEGIN SSH SIGNATURE-----\nbogus\n")
        with mock.patch.object(approval.tempfile, "TemporaryDirectory") as td, \
                mock.patch.object(approval, "anchor_status") as anchor:
            for bad in ((DATA, ""), (DATA, "x" * (approval.MAX_SIGNATURE + 1)), (DATA, "name"), ("s", self.sig)):
                with self.assertRaises(approval.ApprovalError):
                    approval.verify(*bad)
            td.assert_not_called()
            anchor.assert_not_called()


class RecordsQueue(ModelBase):
    def test_records_approved_queue_materializes_unavailable_then_restores(self):
        signer = Signer()
        self.addCleanup(signer.close)
        stack = signer.trust()
        stack.__enter__()
        self.addCleanup(stack.__exit__)
        row = aq.add_draft(self.live, item_id="item", kind="reply-extension", agency_id="example", request_id="r1",
                           body="Synthetic draft.", evidence={}, source_run_id="synthetic")
        ident = row["id"]
        aq.approve(self.live, ident, {"kind": "ssh", "signature": signer.sign(aq.approval_payload(self.live, ident))})
        journal = self.live / "approvals/queue.jsonl"
        before = journal.read_bytes()
        self.assertEqual(aq.load_queue(self.live)[0]["status"], "approved")
        faults = {
            "create": mock.patch.object(approval.tempfile, "TemporaryDirectory", side_effect=OSError(errno.ENOSPC, CANARY)),
            "write": mock.patch.object(Path, "write_text", partial_write(OSError(errno.ENOSPC, CANARY))),
            "cleanup": mock.patch.object(approval.tempfile, "TemporaryDirectory", cleanup_fails()),
        }
        for name, fault in faults.items():
            with self.subTest(name), fault:
                pending = aq.load_queue(self.live)[0]
                self.assertEqual((pending["status"], pending["approval_unverified"]), ("pending", "unavailable"))
            self.assertEqual(journal.read_bytes(), before)
            self.assertEqual(aq.load_queue(self.live)[0]["status"], "approved")
        self.assertEqual(journal.read_bytes(), before)


class OutboxCli(unittest.TestCase):
    def test_outbox_approve_cli_returns_existing_refusal_on_workspace_io(self):
        signer = Signer()
        self.addCleanup(signer.close)
        stack = signer.trust()
        stack.__enter__()
        self.addCleanup(stack.__exit__)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        journal = Path(tmp.name) / "private" / "outbox.sqlite"
        box = ob.Outbox(journal)
        self.addCleanup(box.close)
        key = box.propose(ob.RequestDraft(request_id="R-1", agency_id="sample-city", scope_version=1, channel="email",
                                          subject="Public records request", body="Please provide the records.\n",
                                          to="records@city.example", from_addr="organizer@example.invalid"))["idempotency_key"]
        sig = Path(tmp.name) / "approval.sig"
        sig.write_text(signer.sign(box.approval_payload(key)), encoding="utf-8")
        before = dict(box.row(key))
        argv = ["--journal", str(journal), "approve", key, "--signature", str(sig)]
        faults = [mock.patch.object(approval.tempfile, "TemporaryDirectory", side_effect=OSError(errno.ENOSPC, CANARY)),
                  mock.patch.object(Path, "write_text", partial_write(OSError(errno.EIO, CANARY))),
                  mock.patch.object(approval.tempfile, "TemporaryDirectory", cleanup_fails())]
        for fault in faults:
            out, err = io.StringIO(), io.StringIO()
            with fault, contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                # Path.write_text is patched only in the verifier call below; argv reading uses read_text
                code = ob.main(argv)
            self.assertEqual(code, 2)
            result = json.loads(out.getvalue())
            self.assertEqual(result["state"], "refused")
            self.assertIn(FIXED, result["reason"])
            self.assertNotIn("approved_by", result)
            self.assertNotIn(CANARY, out.getvalue() + err.getvalue())
            self.assertEqual(box.row(key), before)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ob.main(argv), 0)
        self.assertEqual(box.row(key)["state"], "approved")


if __name__ == "__main__":
    unittest.main()
