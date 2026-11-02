"""RF0310-9: `flock_approve payload|approve` refuse a run already recorded as sent, or whose email draft is not ready.

Before, `approve` never read the `Sent by:` line (it sits outside the signed text), so a run that `flock_email status`
reported as `inconsistent: sent without approval` could be signed afterwards and then read as `sent recorded`.
"""
import os, shutil, subprocess, sys, unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from campaign_tool import approval  # noqa: E402
import tests.flock.test_email_approval as base  # noqa: E402  (module import, so its tests are not collected twice)

SENT, NOT_READY = "email already recorded as sent", "email draft is not ready for approval"
BLANK = "Sent by: ____ (operator, by hand, after G's reply)"


class ApproveAfterSendTests(unittest.TestCase):
    setUp, flock_approve, signed, edit = (base.FlockApprovalFieldTests.setUp, base.FlockApprovalFieldTests.flock_approve,
                                          base.FlockApprovalFieldTests.signed, base.FlockApprovalFieldTests.edit)

    @classmethod
    def setUpClass(cls):
        cls.owner = base.Signer("owner@example.invalid")  # the same synthetic test key and patched trust file

    @classmethod
    def tearDownClass(cls):
        cls.owner.close()

    def refused(self, reason, sig=None):
        """payload and approve both exit 1 with `reason`, never verify a signature, and write nothing."""
        sig = sig or self.tmp / "unused.sig"
        before = sorted(os.listdir(self.run_dir))
        with self.owner.trust(), mock.patch.object(approval, "verify", side_effect=AssertionError("verify called")) as verify:
            for argv in (("payload", self.run_dir), ("approve", self.run_dir, "--signature", sig)):
                with self.subTest(verb=argv[0]):
                    code, out, err = self.flock_approve(*argv)
                    self.assertEqual((code, out, err), (1, "", f"{argv[0]}: refused: {reason}\n"))
        verify.assert_not_called()
        self.assertEqual(sorted(os.listdir(self.run_dir)), before)
        self.assertFalse(os.path.lexists(self.run_dir / "EMAIL-APPROVAL.json"))

    def email_status(self):
        return subprocess.run([sys.executable, "-B", str(base.TOOLS / "flock_email.py"), "status", str(self.tmp / "store")],
                              capture_output=True, text=True)

    def test_sent_without_approval_is_refused_and_stays_inconsistent(self):
        sig = self.signed(self.owner)  # a valid owner signature made before the send: still refused afterwards
        self.edit(BLANK, "Sent by: Pat 2026-10-30")
        self.refused(SENT, sig)
        done = self.email_status()
        self.assertEqual(done.returncode, 1, done.stdout)
        self.assertIn(f"{base.SLUG} | {base.DATE} | inconsistent: sent without approval", done.stdout)

    def test_blank_sent_by_prints_payload_and_a_valid_signature_approves(self):
        sig = self.signed(self.owner)
        with self.owner.trust():
            self.assertEqual(self.flock_approve("approve", self.run_dir, "--signature", sig)[:2],
                             (0, "approve: state=approved approved_by=owner@example.invalid\n"))
        self.assertTrue((self.run_dir / "EMAIL-APPROVAL.json").exists())

    def test_sent_by_with_only_spaces_is_not_a_send(self):
        self.edit(BLANK, "Sent by:    ")
        sig = self.signed(self.owner)
        with self.owner.trust():
            self.assertEqual(self.flock_approve("approve", self.run_dir, "--signature", sig)[0], 0)
            self.assertEqual(self.flock_approve("verify", self.run_dir)[0], 0)

    def test_draft_without_head_line_is_not_ready(self):
        self.edit(base.load("flock_email").HEAD + "\n", "")
        self.refused(NOT_READY)

    def test_linked_draft_is_not_ready(self):
        target = self.tmp / "elsewhere.md"
        shutil.copy(self.draft, target)
        self.draft.unlink()
        self.draft.symlink_to(target)
        self.refused(NOT_READY)

    def test_linked_approval_file_is_not_ready(self):
        (self.run_dir / "EMAIL-APPROVAL.json").symlink_to(self.tmp / "nowhere.json")
        before = sorted(os.listdir(self.run_dir))
        with self.owner.trust():
            for argv in (("payload", self.run_dir), ("approve", self.run_dir, "--signature", self.tmp / "unused.sig")):
                self.assertEqual(self.flock_approve(*argv), (1, "", f"{argv[0]}: refused: {NOT_READY}\n"))
        self.assertEqual(sorted(os.listdir(self.run_dir)), before)

    def test_followup_only_is_not_ready(self):
        self.draft.unlink()
        self.assertTrue((self.run_dir / "FOLLOWUP-DRAFT.md").exists())
        self.refused(NOT_READY)

    def test_sent_with_malformed_approval_is_not_ready(self):
        (self.run_dir / "EMAIL-APPROVAL.json").write_text("{}", encoding="utf-8")
        self.edit(BLANK, "Sent by: Pat 2026-10-30")
        before = sorted(os.listdir(self.run_dir))
        with self.owner.trust():
            for argv in (("payload", self.run_dir), ("approve", self.run_dir, "--signature", self.tmp / "unused.sig")):
                self.assertEqual(self.flock_approve(*argv), (1, "", f"{argv[0]}: refused: {NOT_READY}\n"))
        self.assertEqual(sorted(os.listdir(self.run_dir)), before)

    def test_existing_approval_then_sent_keeps_already_exists_and_verify(self):
        sig = self.signed(self.owner)
        with self.owner.trust():
            self.assertEqual(self.flock_approve("approve", self.run_dir, "--signature", sig)[0], 0)
        record = (self.run_dir / "EMAIL-APPROVAL.json").read_bytes()
        self.edit(BLANK, "Sent by: Pat 2026-10-30")
        with self.owner.trust():
            code, payload, _ = self.flock_approve("payload", self.run_dir)  # G can still re-read the approved text
            self.assertEqual(code, 0)
            self.assertIn("To: records@city.example", payload)
            self.assertEqual(self.flock_approve("approve", self.run_dir, "--signature", sig),
                             (1, "", "approve: EMAIL-APPROVAL.json already exists\n"))
            self.assertEqual(self.flock_approve("verify", self.run_dir)[:2], (0, "verify: ok approved_by=owner@example.invalid\n"))
        self.assertEqual((self.run_dir / "EMAIL-APPROVAL.json").read_bytes(), record)


if __name__ == "__main__":
    unittest.main()
