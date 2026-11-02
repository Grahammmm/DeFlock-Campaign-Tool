"""RF0310-8: `flock_approve approve --signature FILE` reads FILE safely before any verify or write.

Before, the file was read with Path.read_text: a link was followed, a FIFO blocked forever, and a device or huge file was read
whole before approval.verify looked at its length.
"""
import json, os, sys, threading, unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from campaign_tool import approval  # noqa: E402
import tests.flock.test_email_approval as base  # noqa: E402  (module import, so its tests are not collected twice)

BEGIN, END = "-----BEGIN SSH SIGNATURE-----", "-----END SSH SIGNATURE-----"
REASONS = ("signature file is a link", "signature file is not a regular file", "signature file is too large",
           "signature file is not UTF-8 text", "signature file is not an SSH signature")


class SignatureFileTests(unittest.TestCase):
    setUp, flock_approve, signed = (base.FlockApprovalFieldTests.setUp, base.FlockApprovalFieldTests.flock_approve,
                                    base.FlockApprovalFieldTests.signed)

    @classmethod
    def setUpClass(cls):
        cls.owner = base.Signer("owner@example.invalid")  # the same synthetic test key and patched trust file

    @classmethod
    def tearDownClass(cls):
        cls.owner.close()

    def refused(self, path, reason):
        """approve exits 1 with exactly `reason`, never calls verify (so never ssh-keygen), and writes nothing."""
        before = sorted(os.listdir(self.run_dir))
        with self.owner.trust(), mock.patch.object(approval, "verify", side_effect=AssertionError("verify called")) as verify:
            code, out, err = self.flock_approve("approve", self.run_dir, "--signature", path)
        verify.assert_not_called()
        self.assertEqual((code, out, err), (1, "", f"approve: refused: {reason}\n"))
        self.assertEqual(sorted(os.listdir(self.run_dir)), before)
        self.assertFalse((self.run_dir / "EMAIL-APPROVAL.json").exists())

    def reaches_verify(self, path):
        """The file passes every read rule: verify is called once with its exact text (stubbed to refuse)."""
        with self.owner.trust(), mock.patch.object(approval, "verify", side_effect=approval.ApprovalError("stub")) as verify:
            code, _, err = self.flock_approve("approve", self.run_dir, "--signature", path)
        self.assertEqual((code, err), (1, "approve: refused: stub\n"))
        verify.assert_called_once()
        return verify.call_args.args[1]

    def test_link_to_a_valid_signature_is_refused(self):
        link = self.tmp / "link.sig"
        link.symlink_to(self.signed(self.owner))
        self.refused(link, "signature file is a link")

    def test_fifo_is_refused_without_blocking(self):
        fifo = self.tmp / "payload.txt.sig"
        os.mkfifo(fifo)
        errors = []

        def attempt():
            try:
                self.refused(fifo, "signature file is not a regular file")
            except BaseException as exc:  # noqa: BLE001  reported in the main thread below
                errors.append(exc)
        worker = threading.Thread(target=attempt, daemon=True)
        worker.start()
        worker.join(10)
        self.assertFalse(worker.is_alive(), "approve blocked on a FIFO")
        self.assertEqual(errors, [])

    def test_folder_and_device_are_not_regular_files(self):
        folder = self.tmp / "sigdir"
        folder.mkdir()
        for path in (folder, Path("/dev/null")):
            with self.subTest(path=path.name):
                self.refused(path, "signature file is not a regular file")

    def test_size_limit_is_max_signature_bytes(self):
        sig = self.tmp / "big.sig"
        filler = approval.MAX_SIGNATURE - len(BEGIN) - len(END) - 3  # three newlines
        sig.write_bytes(f"{BEGIN}\n{'A' * filler}\n{END}\n".encode())
        self.assertEqual(sig.stat().st_size, approval.MAX_SIGNATURE)
        self.assertEqual(self.reaches_verify(sig), sig.read_text(encoding="utf-8"))
        sig.write_bytes(f"{BEGIN}\n{'A' * (filler + 1)}\n{END}\n".encode())
        self.assertEqual(sig.stat().st_size, approval.MAX_SIGNATURE + 1)
        self.refused(sig, "signature file is too large")

    def test_text_must_be_utf8_with_ssh_armor_lines(self):
        sig = self.tmp / "bad.sig"
        sig.write_bytes(f"{BEGIN}\n".encode() + "café".encode("latin-1") + f"\n{END}\n".encode())
        self.refused(sig, "signature file is not UTF-8 text")
        for name, text in {"begin only": f"{BEGIN}\nAAAA\n", "end only": f"AAAA\n{END}\n",
                           "end before begin": f"{END}\nAAAA\n{BEGIN}\n", "empty": "",
                           "armor inside a line": f"x{BEGIN}\nAAAA\n{END}x\n",
                           "one line": f"{BEGIN} AAAA {END}\n", "a typed name": "G APPROVED\n"}.items():
            with self.subTest(name):
                sig.write_text(text, encoding="utf-8")
                self.refused(sig, "signature file is not an SSH signature")

    def test_valid_signature_approves_exactly_as_before(self):
        sig = self.signed(self.owner)
        with self.owner.trust():
            code, out, err = self.flock_approve("approve", self.run_dir, "--signature", sig)
        self.assertEqual((code, out, err), (0, "approve: state=approved approved_by=owner@example.invalid\n", ""))
        rec = json.loads((self.run_dir / "EMAIL-APPROVAL.json").read_text(encoding="utf-8"))
        self.assertEqual(rec["signature"], sig.read_text(encoding="utf-8"))
        with self.owner.trust():
            self.assertEqual(self.flock_approve("verify", self.run_dir)[0], 0)

    def test_crlf_signature_is_stored_as_read_text_stored_it(self):
        sig = self.signed(self.owner)
        sig.write_bytes(sig.read_bytes().replace(b"\n", b"\r\n"))
        with self.owner.trust():
            self.assertEqual(self.flock_approve("approve", self.run_dir, "--signature", sig)[0], 0)
        rec = json.loads((self.run_dir / "EMAIL-APPROVAL.json").read_text(encoding="utf-8"))
        self.assertEqual(rec["signature"], sig.read_text(encoding="utf-8"))
        self.assertNotIn("\r", rec["signature"])

    def test_missing_file_keeps_the_os_error_refusal(self):
        missing = self.tmp / "nowhere.sig"
        with self.owner.trust(), mock.patch.object(approval, "verify", side_effect=AssertionError("verify called")) as verify:
            code, out, err = self.flock_approve("approve", self.run_dir, "--signature", missing)
        verify.assert_not_called()
        self.assertEqual((code, out), (1, ""))
        self.assertTrue(err.startswith("approve: refused: [Errno 2] No such file or directory"), err)
        self.assertFalse(any(reason in err for reason in REASONS))
        self.assertFalse((self.run_dir / "EMAIL-APPROVAL.json").exists())


if __name__ == "__main__":
    unittest.main()
