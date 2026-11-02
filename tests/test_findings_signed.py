"""DF1507-04: SSH-signed finding review receipts against the campaign's private/reviewers.allowed_signers.

Throwaway keys from tests/approval_signing.py; synthetic names only. Never touches approval.py's trust file.
Run from the repository root: python3 -B -m unittest tests.test_findings_signed -v
"""
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from campaign_tool import findings
from tests.approval_signing import KEYGEN, Signer
from tests.test_findings_publish import draft as publish_draft
from tests.test_findings_review import draft

REPO_ROOT = Path(__file__).resolve().parents[1]
NS = "deflock-finding-review"


@unittest.skipUnless(shutil.which("ssh-keygen"), "ssh-keygen (OpenSSH 8.2+) is required")
class SignedReceiptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.alice, cls.bob, cls.mallory = Signer("alice"), Signer("bob"), Signer("mallory")

    @classmethod
    def tearDownClass(cls):
        for signer in (cls.alice, cls.bob, cls.mallory):
            signer.close()

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-finding-signed-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "campaign"
        self.cli("init", "--directory", self.root, "--name", "Synthetic", "--county", "Synthetic County",
                 "--state", "ca")
        self.draft = self.base / "draft.json"
        self.draft.write_text(json.dumps(draft()), encoding="utf-8")
        self.signers = self.root / "private" / "reviewers.allowed_signers"
        self.receipts = self.root / "private" / "reviews" / "syn-finding-1.json"

    def cli(self, *arguments, returncode=0, env=None):
        result = subprocess.run([sys.executable, "-B", "-m", "campaign_tool", *map(str, arguments)], cwd=REPO_ROOT,
                                capture_output=True, text=True, timeout=30, env=env)
        self.assertEqual(result.returncode, returncode, result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        if returncode:
            self.assertTrue(result.stderr.startswith("Stopped: "), result.stderr)
        return result

    def add(self, name, *roles, key=None, returncode=0):
        arguments = ["finding", "reviewers", "add", "--directory", self.root, "--name", name]
        for role in roles:
            arguments += ["--role", role]
        return self.cli(*arguments, *(["--ssh-key", key.dir / "key.pub"] if key else []), returncode=returncode)

    def flags(self, reviewer, rationale, role):
        return ["--directory", self.root, "--role", role, "--decision", "approve", "--reviewer", reviewer,
                "--rationale", rationale]

    def payload(self, reviewer="Alice Smith", rationale="Checked p. 1.", role="factual"):
        return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "finding", "review-payload", str(self.draft),
                               *map(str, self.flags(reviewer, rationale, role))], cwd=REPO_ROOT, capture_output=True,
                              timeout=30, check=True).stdout

    def sign(self, signer, data, namespace=NS):
        path = self.base / "review.sig"
        path.write_text(signer.sign(data, namespace), encoding="utf-8")
        return path

    def review(self, reviewer="Alice Smith", rationale="Checked p. 1.", role="factual", signature=None, returncode=0,
               env=None):
        result = self.cli("finding", "review", self.draft, *self.flags(reviewer, rationale, role),
                          *(["--signature", signature] if signature else []), returncode=returncode, env=env)
        return json.loads(result.stdout) if returncode == 0 else result.stderr

    def status(self, returncode=0):
        result = self.cli("finding", "review-status", self.draft, "--directory", self.root, returncode=returncode)
        return json.loads(result.stdout) if returncode == 0 else result.stderr

    def tree(self):
        return {str(p.relative_to(self.root)): p.read_bytes() if p.is_file() else None
                for p in sorted(self.root.rglob("*"))}

    def pub(self, signer):
        return " ".join((signer.dir / "key.pub").read_text(encoding="utf-8").split()[:2])

    def test_valid_signature_over_printed_payload_is_written_and_counted_signed(self):
        self.add("Alice Smith", "factual", key=self.alice)
        data = self.payload()
        self.assertEqual(json.loads(data), {"id": "syn-finding-1", "content_sha256": findings.reviewed_hash(draft()),
                                            "role": "factual", "decision": "approve", "reviewer": "Alice Smith",
                                            "rationale": "Checked p. 1.", "namespace": NS})
        self.assertEqual(data, findings.receipt_payload("syn-finding-1", findings.reviewed_hash(draft()), "factual",
                                                        "approve", "Alice Smith", "Checked p. 1."))
        signature = self.sign(self.alice, data)
        receipt = self.review(signature=signature)
        self.assertEqual((receipt["signature"], receipt["signed_by"]), (signature.read_text(), "Alice_Smith"))
        [stored] = json.loads(self.receipts.read_text())
        self.assertEqual(stored["signed_by"], "Alice_Smith")
        report = self.status()
        self.assertEqual((report["signed"], report["self_asserted"], report["bad_signatures"]), (1, 0, []))

    def test_signature_over_one_changed_rationale_byte_is_refused_without_write(self):
        self.add("Alice Smith", "factual", key=self.alice)
        signature = self.sign(self.alice, self.payload(rationale="Checked p. 1."))
        before = self.tree()
        self.assertIn("bad signature for Alice_Smith", self.review(rationale="Checked p. 2.", signature=signature,
                                                                   returncode=1))
        self.assertEqual(self.tree(), before)
        self.assertFalse(os.path.lexists(self.receipts))

    def test_unrostered_key_and_approval_namespace_are_refused(self):
        self.add("Alice Smith", "factual", key=self.alice)
        before = self.tree()
        for signature in (self.sign(self.mallory, self.payload()),
                          self.sign(self.alice, self.payload(), namespace="deflock-approval")):
            self.assertIn("bad signature", self.review(signature=signature, returncode=1))
        self.assertEqual(self.tree(), before)

    def test_reviewer_without_key_or_without_roster_is_refused_naming_the_key(self):
        signature = self.sign(self.bob, self.payload(reviewer="Bob Jones"))
        self.assertIn("Bob Jones has no ssh_public_key", self.review("Bob Jones", signature=signature, returncode=1))
        self.add("Bob Jones", "factual")
        before = self.tree()
        self.assertIn("Bob Jones has no ssh_public_key", self.review("Bob Jones", signature=signature, returncode=1))
        self.assertEqual(self.tree(), before)

    def test_edited_receipt_is_listed_under_bad_signatures_and_counted_self_asserted(self):
        self.add("Alice Smith", "factual", "legal", key=self.alice)
        self.review(signature=self.sign(self.alice, self.payload()))
        self.review(role="legal", signature=self.sign(self.alice, self.payload(role="legal")))
        receipts = json.loads(self.receipts.read_text())
        receipts[0]["rationale"] = "Checked p. 9."
        receipts[1]["signed_by"] = "alice"
        self.receipts.write_text(json.dumps(receipts), encoding="utf-8")
        report = self.status()
        self.assertEqual((report["signed"], report["self_asserted"]), (0, 2))
        self.assertEqual([(b["reviewer"], b["role"]) for b in report["bad_signatures"]],
                         [("Alice Smith", "factual"), ("Alice Smith", "legal")])

    def test_allowed_signers_regenerated_on_add_and_replace(self):
        self.add("Alice Smith", "factual", key=self.alice)
        line = 'Alice_Smith namespaces="{}" {}\n'
        self.assertEqual(self.signers.read_text(), line.format(NS, self.pub(self.alice)))
        self.assertEqual(stat.S_IMODE(self.signers.stat().st_mode), 0o600)
        old = self.sign(self.alice, self.payload())
        self.add("Bob Jones", "legal", key=self.bob)
        self.add("alice  smith", "privacy")  # roles only: the key is kept
        self.assertEqual(self.signers.read_text(), line.format(NS, self.pub(self.alice))
                         + 'Bob_Jones namespaces="{}" {}\n'.format(NS, self.pub(self.bob)))
        self.add("Alice Smith", "factual", key=self.mallory)  # replace Alice's key
        self.assertNotIn(self.pub(self.alice), self.signers.read_text())
        self.assertIn(line.format(NS, self.pub(self.mallory)), self.signers.read_text())
        self.assertIn("bad signature", self.review(signature=old, returncode=1))
        self.review(signature=self.sign(self.mallory, self.payload()))
        self.assertEqual(self.status()["signed"], 1)

    def test_symlinked_allowed_signers_is_refused(self):
        self.add("Alice Smith", "factual", key=self.alice)
        signature = self.sign(self.alice, self.payload())
        outside = self.base / "outside_signers"
        outside.write_text(self.signers.read_text(), encoding="utf-8")
        self.signers.unlink()
        self.signers.symlink_to(outside)
        roster = (self.root / "private" / "reviewers.json").read_bytes()
        for stderr in (self.review(signature=signature, returncode=1), self.status(returncode=1),
                       self.add("Bob Jones", "legal", key=self.bob, returncode=1).stderr):
            self.assertIn("allowed-signers file must be a regular file, not a symlink", stderr)
        self.assertEqual((self.root / "private" / "reviewers.json").read_bytes(), roster)
        self.assertFalse(os.path.lexists(self.receipts))

    def test_bad_key_files_and_principals_are_refused(self):
        rsa = self.base / "rsa"
        subprocess.run([KEYGEN, "-q", "-t", "rsa", "-b", "2048", "-N", "", "-f", str(rsa)], check=True)
        link = self.base / "link.pub"
        link.symlink_to(self.alice.dir / "key.pub")
        two = self.base / "two.pub"
        two.write_text(self.pub(self.alice) + "\n" + self.pub(self.bob) + "\n", encoding="utf-8")
        for path, message in ((self.base / "rsa.pub", "must be one ssh-ed25519"), (link, "symlink"),
                              (two, "must be one ssh-ed25519")):
            stderr = self.cli("finding", "reviewers", "add", "--directory", self.root, "--name", "Alice Smith",
                              "--role", "factual", "--ssh-key", path, returncode=1).stderr
            self.assertIn(message, stderr)
        self.assertIn("cannot be an SSH principal", self.add("O'Neil Q", "factual", key=self.alice, returncode=1).stderr)
        self.assertFalse(os.path.lexists(self.root / "private" / "reviewers.json"))  # every refusal wrote nothing
        self.add("Alice Smith", "factual", key=self.alice)
        self.assertIn("already has a key", self.add("Alice_Smith", "legal", key=self.bob, returncode=1).stderr)
        self.assertFalse(os.path.lexists(self.root / "private" / "reviews"))

    def test_unsigned_receipts_still_accepted_and_shown_self_asserted(self):
        self.add("Alice Smith", "factual", key=self.alice)
        self.add("Bob Jones", "legal")
        self.assertNotIn("signature", self.review())
        self.review("Bob Jones", role="legal")
        report = self.status()
        self.assertEqual((report["signed"], report["self_asserted"], report["bad_signatures"]), (0, 2, []))

    def test_without_ssh_keygen_signed_review_stops_and_unsigned_works(self):
        self.add("Alice Smith", "factual", key=self.alice)
        signature = self.sign(self.alice, self.payload())
        empty = self.base / "empty-bin"
        empty.mkdir()
        env = dict(os.environ, PATH=str(empty))
        stderr = self.review(signature=signature, returncode=1, env=env)
        self.assertTrue(stderr.startswith("Stopped: ssh-keygen not found; signed receipts need OpenSSH 8.2+"), stderr)
        self.assertFalse(os.path.lexists(self.receipts))
        self.assertNotIn("signature", self.review(env=env))

    def test_publish_reports_signed_and_self_asserted(self):
        document = publish_draft()
        self.draft.write_text(json.dumps(document), encoding="utf-8")
        self.add("Alice Smith", "factual", "privacy", key=self.alice)
        self.add("Bob Jones", "legal")
        self.review(signature=self.sign(self.alice, self.payload()))
        self.review(role="privacy")
        self.review("Bob Jones", role="legal")
        (self.root / "content" / "findings").mkdir(parents=True, exist_ok=True)
        with mock.patch("campaign_tool.findings.cited_problems", return_value=[]):
            result = findings.publish(self.root, self.draft, "Pat Organizer")
        self.assertEqual(result["receipt_trust"], {"signed": 1, "self_asserted": 2, "bad_signatures": []})
        self.assertEqual(findings.trust_line(result["receipt_trust"]), "Review receipts: 1 signed, 2 self-asserted.")
        self.assertEqual(findings.trust_line(dict(result["receipt_trust"], bad_signatures=[{}])),
                         "Review receipts: 1 signed, 2 self-asserted, 1 with a signature that does not verify.")


if __name__ == "__main__":
    unittest.main()
