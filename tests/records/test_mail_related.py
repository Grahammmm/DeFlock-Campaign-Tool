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


    def duplicate_type(self, part, value="application/pdf"):
        # Deliberately compose invalid wire headers beyond EmailMessage's
        # singleton-header authoring guard; BytesParser must reject the input.
        part._headers.append(("Content-Type", value))

    def test_duplicate_selected_text_type_rejected_before_exemption(self):
        related = self.related()
        self.duplicate_type(related.get_payload()[0])
        self.assert_rejected(related, "invalid_mime_content_type")

    def test_identical_duplicate_selected_text_type_still_ambiguous(self):
        related = self.related()
        self.duplicate_type(related.get_payload()[0], "text/html")
        self.assert_rejected(related, "invalid_mime_content_type")

    def test_malformed_selected_text_type_without_parent_type(self):
        related = self.related()
        related.del_param("type")
        related.get_payload()[0].replace_header("Content-Type", "not-a-media-type")
        self.assert_rejected(related, "invalid_mime_content_type")

    def test_malformed_selected_text_parameter(self):
        related = self.related()
        related.get_payload()[0].replace_header("Content-Type",
                                                'text/html; charset="unterminated"')
        raw = related.as_bytes()
        valid = b'Content-Type: text/html; charset="unterminated"'
        self.assertEqual(raw.count(valid), 1)
        malformed = raw.replace(valid, valid[:-1], 1)
        with mock.patch.object(related, "as_bytes", return_value=malformed):
            self.assert_rejected(related, "invalid_mime_content_type")

    def test_duplicate_selected_alternative_type(self):
        related = EmailMessage()
        related.make_related()
        root = EmailMessage()
        root.set_content("Synthetic alternative body")
        root.add_alternative("<p>Synthetic body</p>", subtype="html")
        self.duplicate_type(root)
        related.attach(root)
        related.attach(self.image())
        self.assert_rejected(related, "invalid_mime_content_type")

    def test_duplicate_outer_alternative_type(self):
        outer = self.alternative(self.related())
        self.duplicate_type(outer)
        self.assert_rejected(outer, "invalid_mime_content_type")

    def test_duplicate_related_container_type(self):
        related = self.related()
        self.duplicate_type(related)
        self.assert_rejected(related, "invalid_mime_content_type")

    def test_duplicate_nested_body_type(self):
        related = self.related()
        self.duplicate_type(related.get_payload()[0])
        self.assert_rejected(self.alternative(related), "invalid_mime_content_type")

    def test_duplicate_nonroot_image_type_rejected(self):
        related = self.related()
        self.duplicate_type(related.get_payload()[1])
        self.assert_rejected(related, "invalid_mime_content_type")

    def test_missing_selected_type_keeps_explicit_plain_default_policy(self):
        related = self.related(subtype="plain")
        del related.get_payload()[0]["Content-Type"]
        self.assert_import(related)

    def test_missing_outer_leaf_type_keeps_plain_default(self):
        body = self.body(subtype="plain")
        del body["Content-Type"]
        self.assert_import(body)

    def test_selected_named_or_attached_alternative_root_rejected(self):
        for metadata in ("attachment", "attachment_named", "inline_named", "type_named"):
            with self.subTest(metadata=metadata):
                related = EmailMessage()
                related.make_related()
                root = EmailMessage()
                root.set_content("Synthetic attached record body")
                root.add_alternative("<p>Synthetic attached record</p>", subtype="html")
                root["Content-ID"] = "<root@synthetic.invalid>"
                if metadata == "type_named":
                    root.set_param("name", "synthetic-record.mime")
                elif metadata == "attachment":
                    root["Content-Disposition"] = "attachment"
                else:
                    root.add_header("Content-Disposition",
                                    "inline" if metadata == "inline_named" else "attachment",
                                    filename="synthetic-record.mime")
                related.set_param("start", "<root@synthetic.invalid>")
                related.set_param("type", "multipart/alternative")
                related.attach(root)
                related.attach(self.image())
                self.assert_rejected(related, "unsupported_attached_multipart_part")

    def test_nonfirst_selected_attached_multipart_root_rejected(self):
        related = EmailMessage()
        related.make_related()
        related.set_param("start", "<root@synthetic.invalid>")
        root = self.alternative(self.related())
        root["Content-ID"] = "<root@synthetic.invalid>"
        root["Content-Disposition"] = "attachment"
        related.attach(self.image("outer.png"))
        related.attach(root)
        self.assert_rejected(related, "unsupported_attached_multipart_part")

    def test_outer_attached_mixed_container_rejected(self):
        outer = EmailMessage()
        outer.make_mixed()
        outer.attach(self.related())
        outer["Content-Disposition"] = "attachment"
        self.assert_rejected(outer, "unsupported_attached_multipart_part")

    def test_outer_named_related_container_rejected(self):
        related = self.related()
        related.add_header("Content-Disposition", "inline", filename="bundle.mime")
        self.assert_rejected(related, "unsupported_attached_multipart_part")

    def test_nonroot_attached_multipart_container_rejected(self):
        related = self.related()
        attached = EmailMessage()
        attached.make_mixed()
        attached.attach(self.image("attached.png"))
        attached.add_header("Content-Disposition", "attachment", filename="record.mime")
        related.attach(attached)
        self.assert_rejected(related, "unsupported_attached_multipart_part")

    def test_duplicate_multipart_disposition_rejected(self):
        related = self.related()
        related["Content-Disposition"] = "inline"
        related._headers.append(("Content-Disposition", "attachment"))
        self.assert_rejected(related, "invalid_multipart_disposition")

    def test_malformed_multipart_disposition_rejected(self):
        related = self.related()
        related["Content-Disposition"] = 'inline; filename="unterminated"'
        raw = related.as_bytes()
        valid = b'Content-Disposition: inline; filename="unterminated"'
        self.assertEqual(raw.count(valid), 1)
        malformed = raw.replace(valid, valid[:-1], 1)
        with mock.patch.object(related, "as_bytes", return_value=malformed):
            self.assert_rejected(related, "invalid_multipart_disposition")

    def test_explicit_attachment_on_named_leaf_still_binds(self):
        related = self.related()
        root = related.get_payload()[0]
        root.add_header("Content-Disposition", "attachment", filename="body.html")
        self.assertEqual(len(self.assert_import(related)), 2)

    def test_inline_container_without_filename_remains_body(self):
        related = self.related()
        related["Content-Disposition"] = "inline"
        self.assert_import(related)

    def test_invalid_multipart_body_payload_rejected(self):
        related = self.related()
        root = related.get_payload()[0]
        root.replace_header("Content-Type", "multipart/alternative")
        related.del_param("type")
        self.assert_rejected(related, "invalid_mime_container")

    def test_declared_attached_multipart_with_missing_boundary_rejected(self):
        related = self.related()
        root = related.get_payload()[0]
        root.replace_header("Content-Type", "multipart/alternative")
        root["Content-Disposition"] = "attachment"
        related.del_param("type")
        self.assert_rejected(related, "unsupported_attached_multipart_part")



    def test_selected_leaf_inline_then_attachment_disposition_rejected(self):
        related = self.related()
        root = related.get_payload()[0]
        root["Content-Disposition"] = "inline"
        root._headers.append(("Content-Disposition",
                              'attachment; filename="synthetic-record.html"'))
        self.assert_rejected(related, "invalid_leaf_disposition")

    def test_selected_leaf_attachment_then_inline_disposition_rejected(self):
        related = self.related()
        root = related.get_payload()[0]
        root.add_header("Content-Disposition", "attachment",
                        filename="synthetic-record.html")
        root._headers.append(("Content-Disposition", "inline"))
        self.assert_rejected(related, "invalid_leaf_disposition")

    def test_selected_leaf_duplicate_identical_inline_disposition_rejected(self):
        related = self.related()
        root = related.get_payload()[0]
        root["Content-Disposition"] = "inline"
        root._headers.append(("Content-Disposition", "inline"))
        self.assert_rejected(related, "invalid_leaf_disposition")

    def test_nested_selected_leaf_duplicate_disposition_rejected(self):
        related = self.related()
        root = related.get_payload()[0]
        root["Content-Disposition"] = "inline"
        root._headers.append(("Content-Disposition", "attachment"))
        self.assert_rejected(self.alternative(related), "invalid_leaf_disposition")

    def test_selected_leaf_malformed_wire_disposition_rejected(self):
        related = self.related()
        root = related.get_payload()[0]
        root["Content-Disposition"] = 'inline; filename="synthetic-record.html"'
        raw = related.as_bytes()
        valid = b'Content-Disposition: inline; filename="synthetic-record.html"'
        self.assertEqual(raw.count(valid), 1)
        malformed = raw.replace(valid, valid[:-1], 1)
        with mock.patch.object(related, "as_bytes", return_value=malformed):
            self.assert_rejected(related, "invalid_leaf_disposition")

    def test_nonroot_image_duplicate_disposition_rejected(self):
        related = self.related()
        image = related.get_payload()[1]
        image._headers.append(("Content-Disposition", "attachment"))
        self.assert_rejected(related, "invalid_leaf_disposition")

    def test_named_nonroot_text_duplicate_disposition_rejected(self):
        related = self.related()
        text = EmailMessage()
        text.set_content("Synthetic named record")
        text.add_header("Content-Disposition", "inline", filename="record.txt")
        text._headers.append(("Content-Disposition", "attachment"))
        related.attach(text)
        self.assert_rejected(related, "invalid_leaf_disposition")

    def test_selected_leaf_single_inline_disposition_keeps_body(self):
        related = self.related()
        related.get_payload()[0]["Content-Disposition"] = "inline"
        self.assertEqual(self.assert_import(related), {"1.2": 2})

    def test_selected_leaf_unnamed_explicit_attachment_still_binds(self):
        related = self.related()
        related.get_payload()[0]["Content-Disposition"] = "attachment"
        self.assertEqual(self.assert_import(related), {"1.1": 1, "1.2": 2})


if __name__ == "__main__":
    unittest.main()
