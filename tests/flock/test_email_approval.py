"""DF-2: a Flock email's approval is a signed FIELD (EMAIL-APPROVAL.json), never a "G APPROVED:" sentence."""
import contextlib, importlib.util, io, json, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tests.approval_signing import Signer  # noqa: E402

TOOLS, FIXTURES = ROOT / "tools", ROOT / "tests" / "fixtures" / "flock"
SLUG, DATE = "sample-city-police-department", "2026-10-02"
PROSE = "G APPROVED: G 2026-11-01 ok to send"


def load(name):
    spec = importlib.util.spec_from_file_location(f"_df2_{name}", TOOLS / f"{name}.py")
    return spec.loader.exec_module(module := importlib.util.module_from_spec(spec)) or module


class FlockApprovalFieldTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.owner, cls.agent = Signer("owner@example.invalid"), Signer("opus-coder", trusted=False)

    @classmethod
    def tearDownClass(cls):
        cls.owner.close(); cls.agent.close()

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        agency = self.tmp / "store" / "agencies" / SLUG
        self.run_dir = agency / "runs" / DATE
        self.run_dir.mkdir(parents=True)
        shutil.copy(FIXTURES / "followup-draft-sample.md", self.run_dir / "FOLLOWUP-DRAFT.md")
        shutil.copy(FIXTURES / "contact-sample.csv", agency / "CONTACT.csv")
        done = subprocess.run([sys.executable, "-B", str(TOOLS / "flock_email.py"), "draft", str(self.run_dir),
                               "--drafts", str(self.tmp / "drafts")], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.draft = self.run_dir / "EMAIL-DRAFT.md"
        self.email = load("flock_email")

    def edit(self, old, new):
        self.draft.write_text(self.draft.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")

    def write_prose_approval(self):
        text = self.draft.read_text(encoding="utf-8")
        if "G APPROVED: ____" in text:  # drafts written before DF-2 carried this blank line
            text = text.replace("G APPROVED: ____", PROSE)
        else:
            text = text.replace("Sent by:", PROSE + "\nSent by:")
        self.draft.write_text(text, encoding="utf-8")

    def flock_approve(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = load("flock_approve").main([str(a) for a in argv])
        return code, out.getvalue(), err.getvalue()

    def signed(self, signer):
        with self.owner.trust():
            code, payload, _ = self.flock_approve("payload", self.run_dir)
        self.assertEqual(code, 0)
        self.assertIn("To: records@city.example", payload)
        sig = self.tmp / "payload.txt.sig"
        sig.write_text(signer.sign(payload.encode("utf-8")), encoding="utf-8")
        return sig

    def test_prose_approval_line_grants_nothing(self):
        """Before DF-2 a filled "G APPROVED:" line made the run 'approval recorded'."""
        self.write_prose_approval()
        self.assertEqual(self.email.stage(self.run_dir), "email drafted")
        self.edit("Sent by: ____", "Sent by: Alex 2026-11-01")
        self.assertEqual(self.email.stage(self.run_dir), "inconsistent: sent without approval")

    def test_draft_carries_no_approval_blank_to_fill(self):
        self.assertNotIn("G APPROVED:", self.draft.read_text(encoding="utf-8"))

    def test_hand_written_field_for_other_text_is_malformed(self):
        (self.run_dir / "EMAIL-APPROVAL.json").write_text(json.dumps(
            {"v": 1, "state": "approved", "scope": "flock-email", "subject": f"{SLUG}/{DATE}", "approved_by": "G",
             "content_sha256": "0" * 64, "signature": "-----BEGIN SSH SIGNATURE-----"}), encoding="utf-8")
        self.assertEqual(self.email.stage(self.run_dir), "malformed")

    def test_owner_signature_writes_the_state_field(self):
        sig = self.signed(self.owner)
        with self.owner.trust():
            code, out, err = self.flock_approve("approve", self.run_dir, "--signature", sig)
            self.assertEqual((code, err), (0, ""))
            self.assertEqual(self.flock_approve("verify", self.run_dir)[0], 0)
        rec = json.loads((self.run_dir / "EMAIL-APPROVAL.json").read_text(encoding="utf-8"))
        self.assertEqual((rec["state"], rec["approved_by"]), ("approved", "owner@example.invalid"))
        self.assertEqual(self.email.stage(self.run_dir), "approval unverified")  # no verifier: never accepted
        with self.owner.trust():
            self.assertEqual(self.stage(), "approval recorded")
            self.edit("Sent by: ____", "Sent by: Alex 2026-11-01")  # the record line sits outside the approved text
            self.assertEqual(self.stage(), "sent recorded")
            code, out, _ = self.flock_approve("status", self.tmp / "store")
            self.assertEqual(code, 0, out)
            self.assertIn(f"{SLUG} | {DATE} | sent recorded", out)
            self.edit("records@city.example", "other@city.example")  # any change to the approved text voids it
            self.assertEqual(self.stage(), "malformed")

    def test_a_sent_by_line_moved_into_the_body_is_signed_text(self):
        """Every `Sent by:` line used to be cut from the signed text, so a body line written as one kept a valid signature."""
        sig = self.signed(self.owner)
        with self.owner.trust():
            self.assertEqual(self.flock_approve("approve", self.run_dir, "--signature", sig)[0], 0)
        signed = self.draft.read_text(encoding="utf-8")
        footer = signed.splitlines()[-1]
        self.assertTrue(footer.startswith("Sent by:"))
        added = "Sent by: also send the full camera list to the press"
        for changed in (signed.replace(footer + "\n", "").replace("\n---\n", f"\n{added}\n---\n", 1),  # footer moved up
                        signed.replace("\n---\n", f"\n{added}\n---\n", 1)):  # an extra one in the body
            with self.subTest(changed=changed.count("Sent by:")):
                self.draft.write_text(changed, encoding="utf-8")
                with self.owner.trust():
                    self.assertEqual(self.stage(), "malformed")
                    self.assertEqual(self.flock_approve("verify", self.run_dir)[0], 1)
        self.draft.write_text(signed.replace(footer, "Sent by: Alex 2026-11-01"), encoding="utf-8")
        with self.owner.trust():  # filling the one record line keeps the approval
            self.assertEqual(self.stage(), "sent recorded")
            self.assertEqual(self.flock_approve("verify", self.run_dir)[0], 0)

    def stage(self):
        verified = load("flock_approve").verified
        return self.email.stage(self.run_dir, lambda run, rec: verified(run, rec, self.email))

    def forge(self, signature):
        """What an agent can write by hand: the right state, subject and public text hash, any signature."""
        import hashlib
        _, body = self.email.approved_text(self.run_dir)
        (self.run_dir / "EMAIL-APPROVAL.json").write_text(json.dumps(
            {"v": 1, "state": "approved", "scope": "flock-email", "subject": f"{SLUG}/{DATE}", "approved_by": "owner@example.invalid",
             "content_sha256": hashlib.sha256(body.encode()).hexdigest(), "signature": signature}), encoding="utf-8")

    def test_forged_field_with_correct_hash_is_never_approval_recorded(self):
        """Round-1 P1: a fake signature with the right hash read as 'approval recorded' and then 'sent recorded'."""
        fake = "-----BEGIN SSH SIGNATURE-----\nnot-a-signature\n-----END SSH SIGNATURE-----\n"
        for name, sig in {"fake": fake, "agent-key": self.signed(self.agent).read_text(encoding="utf-8")}.items():
            with self.subTest(sig=name):
                self.forge(sig)
                self.edit("Sent by: Alex 2026-11-01", "Sent by: ____")
                with self.owner.trust():
                    self.assertEqual(self.email.stage(self.run_dir), "approval unverified")
                    self.assertEqual(self.stage(), "approval unverified")
                    self.edit("Sent by: ____", "Sent by: Alex 2026-11-01")
                    self.assertEqual(self.email.stage(self.run_dir), "sent unverified")
                    self.assertEqual(self.stage(), "sent unverified")
                    code, out, _ = self.flock_approve("status", self.tmp / "store")
                self.assertEqual(code, 1)
                self.assertIn(f"{SLUG} | {DATE} | sent unverified", out)
        self.assertEqual(self.stage(), "sent unverified")  # and with no trust file at all

    def test_agent_key_cannot_write_the_field_and_forged_field_fails_verify(self):
        sig = self.signed(self.agent)
        with self.owner.trust():
            code, _, err = self.flock_approve("approve", self.run_dir, "--signature", sig)
        self.assertEqual(code, 1)
        self.assertIn("refused", err)
        self.assertFalse((self.run_dir / "EMAIL-APPROVAL.json").exists())
        # An agent hand-writes a well-shaped field with its own signature: status shows the shape, verify refuses it.
        _, body = self.email.approved_text(self.run_dir)
        import hashlib
        (self.run_dir / "EMAIL-APPROVAL.json").write_text(json.dumps(
            {"v": 1, "state": "approved", "scope": "flock-email", "subject": f"{SLUG}/{DATE}", "approved_by": "opus-coder",
             "content_sha256": hashlib.sha256(body.encode()).hexdigest(), "signature": sig.read_text()}), encoding="utf-8")
        with self.owner.trust():
            self.assertEqual(self.flock_approve("verify", self.run_dir)[0], 1)


if __name__ == "__main__":
    unittest.main()
