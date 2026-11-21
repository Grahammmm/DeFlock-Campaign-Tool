"""Offline MIME classification at the intake receipt boundary."""
import unittest
from email.message import EmailMessage

from campaign_tool.records.mail_poll import classify


NOTICE_BODY = "Your records are ready: https://portal.example/documents/1"


class NamelessAttachmentTests(unittest.TestCase):
    def test_unnamed_explicit_attachment_prevents_notice(self):
        # Counting only named parts drops this received file from the receipt.
        message = EmailMessage()
        message.set_content(NOTICE_BODY)
        message.add_attachment(b"Synthetic record", maintype="application", subtype="octet-stream")

        self.assertEqual(classify(message.as_bytes(), 7, 1)[1:], (False, 1, []))

    def test_unnamed_text_attachment_is_not_a_notice_body(self):
        # A link in an attached document is not a link in the message body.
        raw = b"Content-Disposition: attachment\nContent-Type: text/plain\n\n" + NOTICE_BODY.encode("ascii")

        self.assertEqual(classify(raw, 7, 1)[1:], (False, 1, []))

    def test_link_only_still_counts_as_notice(self):
        # Keep the notice path for the same body when there is no received file.
        message = EmailMessage()
        message.set_content(NOTICE_BODY)

        self.assertEqual(classify(message.as_bytes(), 7, 1)[1:], (True, 0, []))

    def test_named_unsafe_attachment_keeps_walk_index_and_reason(self):
        # Preserve the whole-message hold input for an unsafe name, including the part index.
        message = EmailMessage()
        message.set_content(NOTICE_BODY)
        message.add_attachment("Synthetic record", filename="folder/record.txt")

        self.assertEqual(
            classify(message.as_bytes(), 7, 1)[1:],
            (False, 1, [{"part": 2, "reason": "attachment_name_unsafe"}]),
        )
