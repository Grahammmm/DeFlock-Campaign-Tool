"""Synthetic related-root selection, exact binding, and fail-closed regressions."""
from email.message import EmailMessage
import json
from unittest import mock
import unittest

from campaign_tool.records.intake import mail_delta
from tests.records import test_mail_delta as fixtures


class RelatedMimeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.MailDeltaTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def body(self, subtype="html"):
        part = EmailMessage()
        part.set_content("<p>Synthetic body</p>" if subtype == "html" else "Synthetic body",
                         subtype=subtype)
        part["Content-ID"] = "<root@synthetic.invalid>"
        return part

    def image(self, name="inline.png"):
        part = EmailMessage()
        part.set_content(b"\x89PNG\r\n\x1a\nsynthetic inline image",
                         maintype="image", subtype="png")
        part.add_header("Content-Disposition", "inline", filename=name)
        part["Content-ID"] = "<image@synthetic.invalid>"
        return part

    def related(self, second=False, start=None, subtype="html"):
        part = EmailMessage()
        part.make_related()
        part.set_param("type", "text/" + subtype)
        body, image = self.body(subtype), self.image()
        for child in ((image, body) if second else (body, image)):
            part.attach(child)
        if start is not None:
            part.set_param("start", start)
        return part

    def alternative(self, related):
        part = EmailMessage()
        part.set_content("Synthetic plain alternative")
        part.make_alternative()
        part.attach(related)
        return part

    def prepare(self, message, string_parts=False):
        """Write only generated fixture bytes; do not derive bindings via the parser."""
        f = self.fixture
        raw = message.as_bytes()
        (f.root / "mail" / "message.eml").write_bytes(raw)
        f.receipt["original_eml"].update(bytes=len(raw), sha256=fixtures.sha(raw))
        f.receipt["bytes"] = len(raw)
        attachments = []
        locators = {}

        def locate(part, locator):
            if part.is_multipart():
                for n, child in enumerate(part.iter_parts(), 1):
                    locate(child, locator + "." + str(n))
            else:
                locators[id(part)] = locator

        locate(message, "1")
        expected = {}
        for index, part in enumerate(message.walk()):
            if part.is_multipart() or not (
                    part.get_filename() or part.get_content_disposition() == "attachment" or
                    not part.get_content_type().startswith("text/")):
                continue
            data = part.get_payload(decode=True) or b""
            original_name = part.get_filename()
            filename = mail_delta.safe_filename(original_name)
            path = "mail/generated-" + str(index)
            (f.root / path).write_bytes(data)
            locator = locators[id(part)]
            receipt_part = locator if string_parts else index
            attachments.append(dict(part=receipt_part, path=path, bytes=len(data),
                                    sha256=fixtures.sha(data), filename=filename,
                                    original_filename=original_name,
                                    content_type=part.get_content_type()))
            expected[locator] = receipt_part
        f.receipt["attachments"] = attachments
        f._write_receipt()
        return raw, expected

    def assert_import(self, message, string_parts=False):
        raw, expected = self.prepare(message, string_parts)
        f = self.fixture
        result = f._import()
        self.assertEqual(result["added_occurrences"], 1 + len(expected))
        self.assertEqual(result["added_edges"], len(expected))
        actual = {json.loads(row[0])["mime"]: json.loads(row[0])["mail_receipt_part"]
                  for row in f.db.execute("SELECT locator FROM occurrences WHERE parent=?",
                                          (fixtures.sha(raw),))}
        self.assertEqual(actual, expected)
        self.assertEqual(f.db.execute("SELECT count(*) FROM edges WHERE parent=?",
                                      (fixtures.sha(raw),)).fetchone()[0], len(expected))
        self.assertEqual(f._import()["status"], "replay")
        return actual

    def assert_rejected(self, message, code):
        self.prepare(message)
        f = self.fixture
        counts = [f.db.execute("SELECT count(*) FROM " + table).fetchone()[0]
                  for table in ("docs", "occurrences", "edges", "runs", "preservations")]
        blobs = set((f.out / "blobs").iterdir())
        with self.assertRaisesRegex(mail_delta.Rejected, "^" + code + "$"):
            f._import()
        self.assertEqual(f._active(), "baseline")
        self.assertEqual(counts, [
            f.db.execute("SELECT count(*) FROM " + table).fetchone()[0]
            for table in ("docs", "occurrences", "edges", "runs", "preservations")])
        self.assertEqual(set((f.out / "blobs").iterdir()), blobs)

    def test_alternative_related_html_named_inline_image_walk_indices(self):
        actual = self.assert_import(self.alternative(self.related()))
        self.assertEqual(actual, {"1.2.2": 4})

    def test_first_root_without_start_or_type_compatibility(self):
        related = self.related()
        related.del_param("type")
        self.assertEqual(self.assert_import(related), {"1.2": 2})

    def test_start_selects_nonfirst_root_with_exact_string_locator(self):
        related = self.related(second=True, start="<root@synthetic.invalid>")
        actual = self.assert_import(self.alternative(related), string_parts=True)
        self.assertEqual(actual, {"1.2.1": "1.2.1"})

    def test_related_root_can_be_alternative(self):
        related = EmailMessage()
        related.make_related()
        root = EmailMessage()
        root.set_content("Synthetic plain body")
        root.add_alternative("<p>Synthetic HTML body</p>", subtype="html")
        root["Content-ID"] = "<root@synthetic.invalid>"
        related.set_param("start", "<root@synthetic.invalid>")
        related.set_param("type", "multipart/alternative")
        related.attach(root)
        related.attach(self.image())
        self.assertEqual(self.assert_import(related), {"1.2": 4})

    def test_nested_related_roots(self):
        inner = self.related()
        outer = EmailMessage()
        outer.make_related()
        outer.set_param("type", "multipart/related")
        outer.attach(inner)
        other = self.image("outer.png")
        other.replace_header("Content-ID", "<outer@synthetic.invalid>")
        outer.attach(other)
        self.assertEqual(self.assert_import(outer), {"1.1.2": 3, "1.2": 4})

    def test_plain_related_root(self):
        self.assert_import(self.related(subtype="plain"))

    def test_missing_start_target_does_not_fall_back(self):
        self.assert_rejected(self.related(start="<absent@synthetic.invalid>"),
                             "missing_related_root")

    def test_start_is_case_sensitive(self):
        self.assert_rejected(self.related(start="<ROOT@synthetic.invalid>"),
                             "missing_related_root")

    def test_start_cannot_select_descendant(self):
        related = EmailMessage()
        related.make_related()
        related.set_param("start", "<root@synthetic.invalid>")
        related.attach(self.alternative(self.related()))
        self.assert_rejected(related, "missing_related_root")

    def test_malformed_start(self):
        self.assert_rejected(self.related(start="root@synthetic.invalid"),
                             "invalid_related_start")

    def test_duplicate_content_id_even_without_start(self):
        related = self.related()
        related.get_payload()[1].replace_header("Content-ID", "<root@synthetic.invalid>")
        self.assert_rejected(related, "ambiguous_related_content_id")

    def test_multiple_content_id_headers(self):
        related = self.related()
        related.get_payload()[0]["Content-ID"] = "<other@synthetic.invalid>"
        self.assert_rejected(related, "ambiguous_related_content_id")

    def test_invalid_content_id(self):
        related = self.related()
        related.get_payload()[1].replace_header("Content-ID", "image@synthetic.invalid")
        self.assert_rejected(related, "invalid_related_content_id")

    def test_declared_type_mismatch(self):
        related = self.related()
        related.set_param("type", "text/plain")
        self.assert_rejected(related, "related_root_type_mismatch")

    def test_type_is_case_insensitive(self):
        related = self.related()
        related.set_param("type", "TEXT/HTML")
        self.assert_import(related)

    def test_extra_nonroot_unnamed_html_rejected(self):
        related = self.related()
        extra = self.body()
        extra.replace_header("Content-ID", "<extra@synthetic.invalid>")
        related.attach(extra)
        self.assert_rejected(related, "ambiguous_inline_body_part")

    def test_nonroot_alternative_cannot_launder_text(self):
        related = self.related()
        extra = EmailMessage()
        extra.set_content("Synthetic extra record")
        extra.add_alternative("<p>Synthetic extra record</p>", subtype="html")
        related.attach(extra)
        self.assert_rejected(related, "ambiguous_inline_body_part")

    def test_related_in_nonfirst_mixed_slot_rejected(self):
        mixed = EmailMessage()
        mixed.set_content("Synthetic primary body")
        mixed.make_mixed()
        mixed.attach(self.related())
        self.assert_rejected(mixed, "ambiguous_inline_body_part")

    def test_related_in_first_mixed_slot_with_pdf(self):
        mixed = EmailMessage()
        mixed.make_mixed()
        mixed.attach(self.alternative(self.related()))
        mixed.add_attachment(self.fixture.attachment, maintype="application",
                             subtype="pdf", filename="synthetic.pdf")
        self.assertEqual(self.assert_import(mixed), {"1.1.2.2": 5, "1.2": 6})

    def test_structured_text_root_still_rejected(self):
        self.assert_rejected(self.related(subtype="csv"), "ambiguous_inline_text_part")

    def test_named_nonroot_text_still_requires_attachment_binding(self):
        related = self.related()
        text = EmailMessage()
        text.set_content("Synthetic named record")
        text.add_header("Content-Disposition", "inline", filename="record.txt")
        related.attach(text)
        self.assertEqual(len(self.assert_import(related)), 2)

    def test_omitted_image_is_not_exempted(self):
        self.prepare(self.related())
        f = self.fixture
        f.receipt["attachments"] = []
        f._write_receipt()
        with self.assertRaisesRegex(mail_delta.Rejected, "unlisted_mime_attachment"):
            f._import()
        self.assertEqual(f._active(), "baseline")

    def test_wrong_walk_index_cannot_bind_by_hash(self):
        self.prepare(self.alternative(self.related()))
        f = self.fixture
        f.receipt["attachments"][0]["part"] = 3
        f._write_receipt()
        with self.assertRaisesRegex(mail_delta.Rejected, "missing_or_mismatched_mime_part"):
            f._import()
        self.assertEqual(f._active(), "baseline")

    def test_duplicate_bytes_keep_distinct_related_locators(self):
        related = self.related()
        other = self.image("second.png")
        other.replace_header("Content-ID", "<second@synthetic.invalid>")
        related.attach(other)
        self.assertEqual(self.assert_import(related), {"1.2": 2, "1.3": 3})

    def test_part_budget_still_enforced(self):
        with mock.patch.object(mail_delta, "MAX_MIME_PARTS", 2):
            self.assert_rejected(self.related(), "mime_part_limit")

    def test_unnamed_nested_rfc822_stays_unsupported(self):
        related = self.related()
        embedded = EmailMessage()
        embedded.set_content("Synthetic embedded mail")
        forwarded = EmailMessage()
        forwarded.set_type("message/rfc822")
        forwarded.set_payload([embedded])
        related.attach(forwarded)
        self.assert_rejected(related, "unsupported_rfc822_part")


if __name__ == "__main__":
    unittest.main()
