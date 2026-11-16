"""Documentation-contract tests for the receipt manifest handoff policy (AUDINV-FIX-140)."""
import json
import re
import unittest
from pathlib import Path

README = Path(__file__).resolve().parents[2] / "deploy" / "records-runtime" / "README.md"
HEADING = "### Receipt manifest handoff privacy (AUDINV-FIX-140)"


def section():
    text = README.read_text(encoding="utf-8")
    start = text.index(HEADING)
    nxt = re.search(r"^#{1,3} ", text[start + len(HEADING):], re.M)
    end = start + len(HEADING) + nxt.start() if nxt else len(text)
    return text[start:end]


def flat():
    return " ".join(section().split())


class HandoffPolicyTests(unittest.TestCase):
    def test_handoff_section_prohibits_inline_manifest_metadata(self):
        t = flat()
        self.assertIn("Never paste manifest JSON", t)
        for term in ("mailbox UIDs", "UIDVALIDITY", "dates", "sender-domain classes",
                     "document rows", "copied stdout"):
            self.assertIn(term, t)

    def test_attachment_is_conditional_not_automatic_permission(self):
        t = flat()
        self.assertIn("separately authorized attachment", t)
        self.assertIn("`kanban_attach`", t)
        self.assertIn("not automatically private", t)
        self.assertIn("approved opaque locator plus its exact SHA-256", t)

    def test_locator_hash_example_has_only_two_keys(self):
        blocks = re.findall(r"```json\n(.*?)\n\s*```", section(), re.S)
        self.assertEqual(len(blocks), 1)
        data = json.loads(blocks[0])
        self.assertEqual(set(data), {"locator", "sha256"})
        self.assertEqual(data["locator"], "approved://synthetic/receipt-manifest.json")
        self.assertRegex(data["sha256"], r"^[0-9a-f]{64}$")

    def test_missing_approval_or_digest_mismatch_stays_hold(self):
        t = flat()
        for phrase in ("HOLD, no handoff", "missing or unapproved", "credential-bearing",
                       "unreadable or missing", "malformed or does not match",
                       "attachment audience is unauthorized", "approval is unavailable"):
            self.assertIn(phrase, t)

    def test_policy_preserves_private_manifest_and_live_holds(self):
        t = flat()
        self.assertIn("exact bytes", t)
        self.assertIn("without redaction, rewriting or schema change", t)
        self.assertIn("does not post or attach anything", t)
        self.assertIn("Historical cleanup needs a separately scoped owner decision", t)


if __name__ == "__main__":
    unittest.main()
