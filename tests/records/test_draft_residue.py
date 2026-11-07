"""Synthetic residue checks and send/placement refusals; never contacts a server."""
import unittest
from unittest import mock

from campaign_tool.records import approval_queue as aq, draft_residue as dr
from campaign_tool.records import draft_templates as dt, followup_drafter as fd
from campaign_tool.records import mail_drafts as md, mail_send as ms
from tests.records.test_mail_drafts import Base, CANARY, NOW
from tests.records.test_mail_send import SendBase


TRIGGERS = (
    ("# Draft (reminder): alpha-pd", ["draft_heading"]),
    (dt.DRAFT_BANNER, ["draft_banner"]),
    (fd.BANNER, ["draft_banner"]),
    ("[EDIT ME: name]", ["edit_placeholder"]),
    ("Notes for the reviewer", ["reviewer_notes"]),
    ("BEFORE SENDING", ["before_sending"]),
    (dt._LADDER, ["draft_footer", "reviewer_notes"]),
    (dt._FOLLOWUP_FOOTER, ["draft_footer"]),
)


class CodesTests(unittest.TestCase):
    def test_exact_triggers_and_sorted_unique_codes(self):
        for text, expected in TRIGGERS:
            with self.subTest(text=text):
                self.assertEqual(dr.codes("Hello,\n" + text + "\nThank you."), expected)
        body = "\n".join(text for text, _ in TRIGGERS)
        self.assertEqual(dr.codes(body + "\n" + body), sorted({c for _, cs in TRIGGERS for c in cs}))

    def test_clean_letter_and_near_matches_are_not_residue(self):
        for body in ("", "Hello,\n1. Please provide the policy.\n2. Please confirm receipt.\nThank you,\nOrganizer",
                     'The agency wrote: "This is a draft policy."',
                     " # Draft (indented)\n# Draft policy\n[edit me]\nNotes for a reviewer\nbefore sending",
                     dt.DRAFT_BANNER.lower(), fd.BANNER.lower(), dt._FOLLOWUP_FOOTER.rstrip()):
            with self.subTest(body=body):
                self.assertEqual(dr.codes(body), [])


class PlacementTests(Base):
    def test_mixed_batch_journals_only_stable_failure_code_and_retries_clean_revision(self):
        clean = self.draft()
        residue = self.draft(item="1" * 64, body=dt.DRAFT_BANNER + "\n" + CANARY)
        self.assertEqual(self.place(), {"placed": 1, "existing": 0, "failed": 1, "skipped": 0})
        self.assertEqual(self.fake.calls.count("append"), 1)
        self.assertEqual(self.parsed()["X-Records-Queue-Id"].strip(), clean["id"])
        rows = {r["queue_id"]: r for r in md.placements(self.project)}
        failed = rows[residue["id"]]
        self.assertEqual((failed["state"], failed["code"]), ("failed", "body_has_draft_residue"))
        self.assertEqual(set(failed), {"schema", "queue_id", "revision", "state", "at", "code"})
        for text in (CANARY, dt.DRAFT_BANNER, "draft_banner"):
            self.assertNotIn(text, self.journal_path().read_text())
        self.assertEqual(self.place(), {"placed": 0, "existing": 0, "failed": 1, "skipped": 1})
        aq.edit(self.project, residue["id"], "op", "Please confirm receipt.\n")
        self.assertEqual(self.place()["placed"], 0)  # edited text needs a new signature
        self.approve(residue["id"])
        self.assertEqual(self.place()["placed"], 1)
        self.assertEqual(len(self.fake.messages), 2)

    def test_residue_only_and_dry_run_never_connect(self):
        self.draft(body=dt.DRAFT_BANNER)
        factory = mock.Mock(side_effect=AssertionError("unexpected IMAP"))
        self.assertEqual(md.place(self.project, self.config, factory=factory, dry_run=True)["would_place"], 1)
        self.assertFalse(self.journal_path().exists())
        self.assertEqual(md.place(self.project, self.config, factory=factory)["failed"], 1)
        factory.assert_not_called()


class SendTests(SendBase):
    def test_preexisting_placement_with_each_residue_refuses_before_imap(self):
        self.on()
        for text, _ in TRIGGERS:
            with self.subTest(text=text):
                aq.edit(self.project, self.draft_row["id"], "op", text + "\n" + CANARY)
                row = self.approve(self.draft_row["id"])
                # Simulate an authentic legacy placement, made before the new gate existed.
                raw = md.build_message(row, None, self.config["from_addr"], now=NOW)
                with md._Journal(self.project, True) as journal:
                    journal.append([md._row(row, "placed", NOW, message_id="<legacy@example.test>",
                                            folder="Drafts", fingerprint=md.fingerprint(raw))])
                factory = mock.Mock(side_effect=AssertionError("unexpected IMAP"))
                info = ms.prepare(self.project, self.send_path, row["id"], imap_factory=factory, now=NOW)
                self.assertEqual(info["code"], "body_has_draft_residue")
                for fingerprint in (md.fingerprint(raw), "stale"):
                    result = ms.send_one(self.project, self.send_path, row["id"], actor="op",
                                         page_fingerprint=fingerprint, signature="synthetic-signature",
                                         imap_factory=factory, smtp_factory=self.smtp, now=NOW)
                    self.assertEqual(result, {"state": "refused", "code": "body_has_draft_residue"})
                factory.assert_not_called()
        self.assertEqual(self.smtp.connections, 0)
        self.assertFalse(self.outbox_path.exists())
        self.assertNotIn(CANARY, (self.project / "approvals" / ms.RESULTS).read_text())


if __name__ == "__main__":
    unittest.main()
