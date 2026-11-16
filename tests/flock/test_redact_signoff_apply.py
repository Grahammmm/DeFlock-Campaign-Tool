"""DF2007-08: ``flock_redact.py signoff-apply`` replays journaled console sign-offs through the one signoff function.

Synthetic store and values only; the verifier is patched in the test process, never a real key or trust file.
"""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock

from campaign_tool import approval
from campaign_tool.records import console_journal as cj
from tests.flock.test_redact import SIGNATURE, VALUES, redact, scan

PIN = types.SimpleNamespace(source_project_id="p" * 16, run_id="run", name="export")
OTHER_SIGNATURE = SIGNATURE.replace("synthetic", "other")


class Replay(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name).resolve()
        self.store, self.dir = base / "store", base / "decisions"
        self.store.mkdir(mode=0o700)
        self.dir.mkdir(mode=0o700)
        (self.store / "originals").mkdir(mode=0o700)
        self.source = self.store / "originals" / "original.txt"
        self.raw = ("\n".join(VALUES) + "\n").encode()
        self.source.write_bytes(self.raw)
        self.source.chmod(0o600)
        self.sha = redact.digest(self.raw)
        self.receipt = self.store / "scan.json"
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(scan.main([str(self.source), "--json-out", str(self.receipt)]), 3)
        self.folder = redact.record.record_dir(self.store, "synthetic", self.sha)
        self.verify = mock.patch.object(redact.approval, "verify", return_value="synthetic-reviewer").start()
        self.addCleanup(mock.patch.stopall)
        self.run_main("propose", "--scan-receipt", self.receipt)
        self.proposal = (self.folder / "PROPOSAL.json").read_bytes()
        self.sid = redact.store_id(self.store)

    def run_main(self, command, *extra, expected=0):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = redact.main([command, str(self.store), "synthetic", self.sha, str(self.source), *map(str, extra)])
        self.assertEqual(code, expected, out.getvalue())
        for value in VALUES:
            self.assertNotIn(value, out.getvalue())
        return out.getvalue()

    def replay(self, expected=0, directory=None):
        return self.run_main("signoff-apply", "--decisions", directory or self.dir, expected=expected)

    def journal(self, signature=SIGNATURE, store_id=None, revision=None, target=None, value=None, action="redaction_signoff"):
        value = value if value is not None else cj._canon({"store_id": store_id or self.sid, "signature": signature})
        return cj.append(str(self.dir), PIN, "operator", "/redactions/signoff", action, target or f"synthetic:{self.sha}",
                         revision or hashlib.sha256(self.proposal).hexdigest(), value)

    def receipts(self):
        path = self.dir / redact.RECEIPTS
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def snapshot(self):
        return {str(p.relative_to(self.store)): (p.read_bytes(), p.stat().st_mode) for p in self.store.rglob("*") if p.is_file()}

    def signoff_path(self):
        return self.folder / "SIGNOFF.json"


class ValidReplay(Replay):
    def test_valid_replay_once_and_crash_recovery(self):
        entry_id = self.journal()
        self.assertIn(f"{entry_id[:12]} APPLIED", self.replay())
        record = json.loads(self.signoff_path().read_bytes())
        self.assertEqual((record["scope"], record["signed_by"], record["signature"], record["proposal_sha256"]),
                         ("flock-redaction", "synthetic-reviewer", SIGNATURE, hashlib.sha256(self.proposal).hexdigest()))
        self.verify.assert_any_call(redact.payload(self.proposal, self.sha), SIGNATURE)
        self.assertEqual(oct(self.signoff_path().stat().st_mode & 0o777), "0o600")
        (receipt,) = self.receipts()
        self.assertEqual((receipt["schema"], receipt["entry_id"], receipt["outcome"], receipt["store_id"], receipt["target_id"]),
                         ("flock-redaction-signoff-receipt-v1", entry_id, "APPLIED", self.sid, f"synthetic:{self.sha}"))
        self.assertEqual(oct((self.dir / redact.RECEIPTS).stat().st_mode & 0o777), "0o600")
        before, calls = self.snapshot(), self.verify.call_count
        self.assertIn("ALREADY", self.replay())
        self.assertEqual((self.snapshot(), len(self.receipts()), self.verify.call_count), (before, 1, calls))
        # a restart after the install but before the final receipt: the same signature is recognised, nothing reinstalled
        (self.dir / redact.RECEIPTS).unlink()
        self.assertIn("APPLIED", self.replay())
        self.assertEqual((self.snapshot(), len(self.receipts())), (before, 1))
        # no derivative, eligibility or original change
        self.assertFalse((self.folder / "ELIGIBILITY.json").exists())
        self.assertFalse(self.source.with_name(self.sha + ".redacted.txt").exists())
        self.assertEqual(self.source.read_bytes(), self.raw)

    def test_a_different_signature_after_the_install_is_a_conflict_never_a_replacement(self):
        self.journal()
        self.replay()
        before = self.snapshot()
        self.journal(OTHER_SIGNATURE)
        self.assertIn("SIGNOFF_CONFLICT", self.replay(expected=1))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual([r["outcome"] for r in self.receipts()], ["APPLIED", "SIGNOFF_CONFLICT"])

    def test_a_failed_receipt_write_is_not_reported_as_receipted_and_a_rerun_recovers(self):
        self.journal()
        with mock.patch.object(redact, "append_receipt", side_effect=OSError("disk")):
            self.assertIn("receipt not written", self.replay(expected=1))
        self.assertEqual(self.receipts(), [])
        self.assertTrue(self.signoff_path().exists())
        before = self.snapshot()
        self.assertIn("APPLIED", self.replay())
        self.assertEqual((self.snapshot(), len(self.receipts())), (before, 1))

    def test_the_cli_signoff_still_installs_through_the_same_function(self):
        sig = Path(self.tmp.name) / "signature"
        sig.write_text(SIGNATURE)
        sig.chmod(0o600)
        with mock.patch.object(redact, "signoff", wraps=redact.signoff) as wrapped:
            self.run_main("signoff", "--signature", sig)
        self.assertEqual(wrapped.call_count, 1)
        self.assertEqual(json.loads(self.signoff_path().read_bytes())["signature"], SIGNATURE)


class RefusedReplay(Replay):
    def test_forgery_actor_and_conflicting_signoff(self):
        self.journal()
        self.verify.side_effect = approval.ApprovalError("synthetic forged")
        before = self.snapshot()
        self.assertIn("SIGNATURE_REFUSED", self.replay(expected=1))
        self.assertEqual(self.snapshot(), before)  # nothing in the store
        (receipt,) = self.receipts()
        self.assertEqual(receipt["outcome"], "SIGNATURE_REFUSED")
        self.assertIn("ALREADY", self.replay())  # final: not retried, no new receipt
        self.assertEqual(len(self.receipts()), 1)
        # an existing sign-off is immutable: a later valid-looking entry is a conflict
        self.verify.side_effect = None
        self.run_main("signoff", "--signature", self._sig())
        before = self.snapshot()
        self.journal(OTHER_SIGNATURE)
        self.assertIn("SIGNOFF_CONFLICT", self.replay(expected=1))
        self.assertEqual(self.snapshot(), before)

    def _sig(self):
        path = Path(self.tmp.name) / "signature"
        path.write_text(SIGNATURE)
        path.chmod(0o600)
        return path

    def test_proposal_changed_after_listing_or_journaling(self):
        self.journal(revision="a" * 64)  # what the person saw is not this proposal
        self.assertIn("STALE_PROPOSAL", self.replay(expected=1))
        self.assertFalse(self.signoff_path().exists())
        # after the append the proposal is rewritten (same canonical form, different bytes)
        self.journal(OTHER_SIGNATURE)
        value = json.loads(self.proposal)
        value["created_at"] = "2020-01-01T00:00:00Z"
        self.folder.joinpath("PROPOSAL.json").write_bytes(redact.encode(value))
        self.assertIn("STALE_PROPOSAL", self.replay(expected=1))
        self.assertFalse(self.signoff_path().exists())
        # the original changed separately: the existing source refusal, no receipt, no sign-off
        self.source.write_bytes(self.raw + b"x")
        self.journal(OTHER_SIGNATURE.replace("other", "third"))
        self.assertIn("refused source_mismatch", self.replay(expected=1))
        self.assertEqual(len(self.receipts()), 2)
        self.assertFalse(self.signoff_path().exists())

    def test_recovery_revalidates_inputs_changed_while_waiting_for_the_lock(self):
        import fcntl

        self.journal()
        self.replay()
        signoff = self.signoff_path().read_bytes()
        (self.dir / redact.RECEIPTS).unlink()  # interrupted after install, before receipt
        real_flock = fcntl.flock
        value = json.loads(self.proposal)
        value["created_at"] = "2020-01-01T00:00:00Z"
        changed_proposal = redact.encode(value)
        for path, changed, refusal in (
                (self.folder / "PROPOSAL.json", changed_proposal, "STALE_PROPOSAL"),
                (self.source, self.raw + b"x", "refused source_mismatch")):
            with self.subTest(path=path.name):
                self.source.write_bytes(self.raw)
                self.folder.joinpath("PROPOSAL.json").write_bytes(self.proposal)
                (self.dir / redact.RECEIPTS).unlink(missing_ok=True)
                after_change = []

                def acquire(fd, operation):
                    path.write_bytes(changed)  # inputs were loaded; the lock is not yet acquired
                    after_change.append(self.snapshot())
                    return real_flock(fd, operation)

                self.verify.reset_mock()
                with mock.patch.object(fcntl, "flock", side_effect=acquire) as locked:
                    self.assertIn(refusal, self.replay(expected=1))
                locked.assert_called_once()
                self.verify.assert_not_called()
                self.assertEqual(self.snapshot(), after_change[0])
                self.assertEqual(self.signoff_path().read_bytes(), signoff)
                self.assertNotIn("APPLIED", [r["outcome"] for r in self.receipts()])
                if path == self.source:
                    self.assertEqual(self.receipts(), [])
                else:
                    self.assertEqual([r["outcome"] for r in self.receipts()], ["STALE_PROPOSAL"])

    def test_an_install_time_change_rolls_back_only_our_signoff(self):
        self.journal()
        real = redact.record._bytes
        calls = []

        def changing(path):
            if Path(path).name == "PROPOSAL.json" and self.signoff_path().exists() and not calls:
                calls.append(1)
                return self.proposal + b" "
            return real(path)
        with mock.patch.object(redact.record, "_bytes", changing):
            self.assertIn("STALE_PROPOSAL", self.replay(expected=1))
        self.assertFalse(self.signoff_path().exists())

    def test_trust_unavailable_retry(self):
        self.journal()
        self.verify.side_effect = approval.TrustUnavailable("synthetic")
        before = self.snapshot()
        self.assertIn("DEFERRED", self.replay(expected=1))
        self.assertEqual((self.snapshot(), self.receipts()), (before, []))  # retryable: no final receipt
        self.verify.side_effect = None
        self.assertIn("APPLIED", self.replay())
        self.assertEqual([r["outcome"] for r in self.receipts()], ["APPLIED"])
        # trust lost during recovery of an interrupted success also defers
        (self.dir / redact.RECEIPTS).unlink()
        self.verify.side_effect = approval.TrustUnavailable("synthetic")
        self.assertIn("DEFERRED", self.replay(expected=1))
        self.assertEqual(self.receipts(), [])
        self.verify.side_effect = None
        self.assertIn("APPLIED", self.replay())
        self.assertEqual(len(self.receipts()), 1)

    def test_foreign_entries_are_untouched(self):
        self.journal(store_id="b" * 64)
        self.journal(target="other-agency:" + self.sha)
        self.journal(target="synthetic:" + "c" * 64)
        self.journal(action="reject")
        before = self.snapshot()
        self.assertNotIn("APPLIED", self.replay())
        self.assertEqual((self.snapshot(), self.receipts()), (before, []))

    def test_a_malformed_entry_for_this_target_is_a_final_invalid_entry(self):
        self.journal(value="not json")
        before = self.snapshot()
        self.assertIn("INVALID_ENTRY", self.replay(expected=1))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual([r["outcome"] for r in self.receipts()], ["INVALID_ENTRY"])

    def test_bad_inputs_fail_closed(self):
        self.journal()
        before = self.snapshot()
        journal = self.dir / cj.FILE
        good = journal.read_bytes()
        journal.write_bytes(good + b"{broken}\n")  # a malformed completed line refuses before any mutation
        self.assertIn("refused decisions_invalid", self.replay(expected=1))
        journal.write_bytes(good + b'{"partial')  # an unfinished tail is ignored, as everywhere else
        self.assertIn("APPLIED", self.replay())
        self.assertNotEqual(self.snapshot(), before)

    def test_unsafe_directories_and_receipts_are_refused_before_any_write(self):
        self.journal()
        before = self.snapshot()
        os.chmod(self.dir, 0o755)
        self.assertIn("refused decisions_invalid", self.replay(expected=1))
        os.chmod(self.dir, 0o700)
        inside = self.store / "inside"
        inside.mkdir(mode=0o700)
        self.assertIn("refused decisions_invalid", self.replay(expected=1, directory=inside))
        (self.dir / redact.RECEIPTS).write_text("not a receipt\n")
        os.chmod(self.dir / redact.RECEIPTS, 0o600)
        self.assertIn("refused decisions_invalid", self.replay(expected=1))
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.signoff_path().exists())


if __name__ == "__main__":
    unittest.main()
