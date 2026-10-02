"""Only generated synthetic messages; no corpus, ledger, network or filesystem I/O."""
from email import policy
from email.errors import MultipartInvariantViolationDefect
from email.parser import BytesHeaderParser, BytesParser
import unittest
from unittest.mock import patch

from campaign_tool.records.intake import wire_rfc822 as wire


def eml(eol=b"\r\n", body=b"synthetic body"):
    return eol.join((b"Subject: " + b"synthetic " * 12,
                     b"\t deliberately preserved fold " + b"example " * 10,
                     b"X-Synthetic:  two spaces", b"", body))


def rfc(payload, eol=b"\r\n", named=False, cte=None):
    headers = [b"Content-Type: message/rfc822"]
    if named:
        headers.extend((b"Content-Disposition: attachment; filename=synthetic.eml",
                        b"X-Synthetic: named"))
    if cte is not None:
        headers.append(b"Content-Transfer-Encoding: " + cte)
    return eol.join(headers) + eol + eol + payload


def multi(children, boundary=b"synthetic-outer", eol=b"\r\n", subtype=b"mixed",
          preamble=b"synthetic preamble", epilogue=b"synthetic epilogue"):
    head = b"Content-Type: multipart/" + subtype + b"; boundary=\"" + boundary + b"\""
    body = preamble + eol if preamble else b""
    for child in children:
        body += b"--" + boundary + eol + child + eol
    return head + eol + eol + body + b"--" + boundary + b"--" + eol + epilogue


class WireRFC822Tests(unittest.TestCase):
    def reject(self, raw, locator="1.1", code=None, **limits):
        with self.assertRaises(wire.WireRFC822Error) as caught:
            wire.capture_rfc822_payload(raw, locator, **limits)
        if code is not None:
            self.assertEqual(str(caught.exception), code)
        self.assertRegex(str(caught.exception), r"^[a-z0-9_]+$")

    def test_header_only_multipart_invariant_is_expected_not_source_corruption(self):
        header = b"Content-Type: multipart/mixed; boundary=synthetic-outer\r\n\r\n"
        parsed = BytesHeaderParser(policy=policy.default).parsebytes(header)
        self.assertTrue(any(isinstance(defect, MultipartInvariantViolationDefect)
                            for defect in parsed.defects))
        payload = eml()
        self.assertEqual(wire.capture_rfc822_payload(multi([rfc(payload)]), "1.1"), payload)
        self.reject(b"Content-Type: multipart/mixed\r\n\r\nsynthetic",
                    code="invalid_boundary")

    def test_exact_bytes_folds_line_endings_and_original_terminal_newlines(self):
        for outer_eol in (b"\n", b"\r\n"):
            for inner_eol in (b"\n", b"\r\n"):
                for terminal in (b"", inner_eol, inner_eol * 2):
                    for named in (False, True):
                        with self.subTest(outer=outer_eol, inner=inner_eol,
                                          terminal=terminal, named=named):
                            payload = eml(inner_eol, b"synthetic body" + terminal)
                            raw = multi([rfc(payload, outer_eol, named)], eol=outer_eol)
                            self.assertEqual(wire.capture_rfc822_payload(raw, "1.1"), payload)
                            parsed = BytesParser(policy=policy.default).parsebytes(payload)
                            serialized = parsed.as_bytes(policy=policy.default.clone(
                                refold_source="all", max_line_length=40))
                            self.assertNotEqual(serialized, payload)
                            self.assertIn(b"\t deliberately preserved fold", payload)

    def test_root_rfc822_and_identity_ctes(self):
        for cte in (None, b"7bit", b"8bit", b"binary", b"BiNaRy"):
            with self.subTest(cte=cte):
                payload = eml(body=b"synthetic\x00\xff\rbytes") if cte and cte.lower() == b"binary" else eml()
                self.assertEqual(wire.capture_rfc822_payload(rfc(payload, cte=cte), "1"), payload)

    def test_nested_multipart_roots_map_ordered_stdlib_children(self):
        first, second = eml(body=b"first"), eml(b"\n", b"second\n")
        raw = multi([b"Content-Type: text/plain\r\n\r\nsynthetic body",
                     multi([rfc(first), rfc(second, named=True)], boundary=b"synthetic-inner")])
        parsed = BytesParser(policy=policy.default).parsebytes(raw)
        self.assertEqual(len(list(parsed.iter_parts())), 2)
        self.assertEqual(len(list(list(parsed.iter_parts())[1].iter_parts())), 2)
        self.assertEqual(wire.capture_rfc822_payload(raw, "1.2.1"), first)
        self.assertEqual(wire.capture_rfc822_payload(raw, "1.2.2"), second)

    def test_encapsulated_multipart_root_preserved_then_explicitly_recaptured(self):
        inner = b"Subject: synthetic container\r\n" + multi(
            [rfc(eml()), b"Content-Type: application/octet-stream\r\n\r\nsynthetic file"],
            boundary=b"synthetic-contained")
        raw = multi([rfc(inner)])
        self.assertEqual(wire.capture_rfc822_payload(raw, "1.1"), inner)
        self.assertEqual(wire.capture_rfc822_payload(inner, "1.1"), eml())
        self.reject(raw, "1.1.1", "unlisted_locator")

    def test_opaque_body_not_parsed_or_claimed_complete(self):
        payload = b"Subject: synthetic opaque\r\nContent-Type: multipart/mixed; boundary=opaque\r\n\r\n"
        payload += (b"Content-Type: message/rfc822\r\n\r\n" * 2000) + b"unparsed"
        raw = rfc(payload)
        comparison_parser = BytesParser(policy=policy.default)
        with (
            patch.object(wire, "BytesParser", return_value=comparison_parser),
            patch.object(comparison_parser, "parsebytes",
                         wraps=comparison_parser.parsebytes) as parser,
        ):
            self.assertEqual(wire.capture_rfc822_payload(raw, "1", max_depth=1, max_parts=1), payload)
            self.assertNotIn(b"unparsed", parser.call_args.args[0])
        self.reject(raw, "1.1", "unlisted_locator")

    def test_digest_implicit_unnamed_rfc822(self):
        payload = eml()
        raw = multi([b"\r\n" + payload], subtype=b"digest")
        self.assertEqual(wire.capture_rfc822_payload(raw, "1.1"), payload)

    def test_headerless_plain_sibling_is_not_an_attachment_claim(self):
        raw = multi([b"\r\nsynthetic inline", rfc(eml())])
        self.assertEqual(wire.capture_rfc822_payload(raw, "1.2"), eml())
        self.reject(raw, "1.1", "not_rfc822")

    def test_valid_folded_content_type_quoted_boundary_and_padding(self):
        raw = multi([rfc(eml())], boundary=b"synthetic: boundary")
        raw = raw.replace(b"multipart/mixed; boundary=", b"multipart/mixed;\r\n\tboundary=")
        raw = raw.replace(b"--synthetic: boundary\r\n", b"--synthetic: boundary \t\r\n")
        raw = raw.replace(b"--synthetic: boundary--\r\n", b"--synthetic: boundary-- \t\r\n")
        self.assertEqual(wire.capture_rfc822_payload(raw, "1.1"), eml())

    def test_closing_boundary_at_eof(self):
        raw = multi([rfc(eml())], epilogue=b"").removesuffix(b"\r\n")
        self.assertEqual(wire.capture_rfc822_payload(raw, "1.1"), eml())

    def test_base64_and_qp_scalar_siblings_need_no_payload_decode(self):
        for cte in (b"base64", b"quoted-printable"):
            scalar = b"Content-Type: application/octet-stream\r\nContent-Transfer-Encoding: " + cte
            raw = multi([scalar + b"\r\n\r\nsynthetic encoded scalar", rfc(eml())])
            self.assertEqual(wire.capture_rfc822_payload(raw, "1.2"), eml())

    def test_unsupported_rfc822_ctes_fail_closed(self):
        for cte in (b"base64", b"quoted-printable", b"x-synthetic"):
            self.reject(multi([rfc(eml(), cte=cte)]), code="unsupported_transfer_encoding")

    def test_unsupported_container_encoding(self):
        raw = b"Content-Transfer-Encoding: base64\r\n" + multi([rfc(eml())])
        self.reject(raw, code="unsupported_transfer_encoding")

    def test_malformed_cte(self):
        for cte in (b"", b"7bit; extra", b"7bit garbage", b"(comment) 7bit", b"\"7bit\""):
            self.reject(multi([rfc(eml(), cte=cte)]), code="invalid_transfer_encoding")

    def test_duplicate_structural_headers(self):
        for name, value in ((b"Content-Type", b"message/rfc822"),
                            (b"Content-Transfer-Encoding", b"7bit"),
                            (b"Content-Disposition", b"attachment"), (b"MIME-Version", b"1.0")):
            part = name + b": " + value + b"\r\n" + name.lower() + b": " + value + b"\r\n\r\n" + eml()
            self.reject(multi([part]), code="duplicate_structural_header")

    def test_malformed_content_type(self):
        for value in (b"message", b"message /rfc822", b"/rfc822", b"message/rfc822 garbage",
                      b"message/rfc822;", b"message/rfc822; name", b"message/rfc822; name=",
                      b"message/rfc822; name=\"unterminated", b"message/rfc822(comment)"):
            self.reject(multi([b"Content-Type: " + value + b"\r\n\r\n" + eml()]),
                        code="invalid_content_type")

    def test_duplicate_content_type_parameters(self):
        raw = multi([rfc(eml())]).replace(b'boundary="synthetic-outer"',
                                             b'boundary="synthetic-outer"; BOUNDARY=other')
        self.reject(raw, code="duplicate_content_type_parameter")

    def test_extended_boundary_parameter_is_unsupported(self):
        raw = multi([rfc(eml())]).replace(b'boundary="synthetic-outer"', b"boundary*=us-ascii\x27\x27synthetic-outer")
        self.reject(raw, code="unsupported_boundary_parameter")

    def test_invalid_boundary_values(self):
        for value in (b"", b"synthetic ", b"synthetic[", b"x" * 71):
            raw = b'Content-Type: multipart/mixed; boundary="' + value + b'"\r\n\r\nbody'
            self.reject(raw, code="invalid_boundary")
        self.reject(b"Content-Type: multipart/mixed\r\n\r\nbody", code="invalid_boundary")

    def test_duplicate_and_prefix_boundaries_fail_closed(self):
        for boundary in (b"synthetic-outer", b"synthetic-outer-longer", b"synthetic"):
            self.reject(multi([multi([rfc(eml())], boundary=boundary)]))
        raw = multi([multi([rfc(eml())], boundary=b"reused"),
                     multi([rfc(eml())], boundary=b"reused")])
        self.reject(raw, "1.1.1", "duplicate_or_prefix_boundary")

    def test_missing_close_and_open_boundaries(self):
        raw = multi([rfc(eml())])
        self.reject(raw.replace(b"--synthetic-outer--", b"not-a-close"), code="missing_close_boundary")
        self.reject(b'Content-Type: multipart/mixed; boundary=synthetic\r\n\r\n--synthetic--\r\n',
                    code="missing_open_boundary")

    def test_consecutive_delimiters_empty_part(self):
        raw = multi([rfc(eml())]).replace(b"--synthetic-outer\r\n", b"--synthetic-outer\r\n--synthetic-outer\r\n", 1)
        self.reject(raw, code="invalid_boundary_framing")

    def test_delimiter_after_close(self):
        self.reject(multi([rfc(eml())]) + b"\r\n--synthetic-outer--\r\n", code="boundary_after_close")

    def test_boundary_lookalike_line(self):
        raw = multi([rfc(eml(body=b"synthetic\r\n--synthetic-outer-invalid"))])
        self.reject(raw, code="ambiguous_boundary_line")

    def test_malformed_headers_and_separator(self):
        for bad in (b" orphan fold\r\n\r\nbody", b"Bad Header: value\r\n\r\nbody",
                    b"Subject: synthetic\rbroken\r\n\r\nbody", b"Subject: \xff\r\n\r\nbody",
                    b"Subject: synthetic\r\nno separator", b"Subject: synthetic\r\n\r\n"[:20]):
            self.reject(multi([rfc(bad)]))
        self.reject(multi([rfc(b"X-Synthetic: only\r\n\r\nbody")]),
                    code="missing_encapsulated_message_header")

    def test_invalid_disposition_and_mime_version(self):
        self.reject(multi([b"Content-Disposition: attachment; filename=\"broken\r\n" + rfc(eml())]),
                    code="header_parse_defect")
        self.reject(multi([b"MIME-Version: 2.0\r\n" + rfc(eml())]), code="invalid_mime_version")

    def test_header_line_and_header_block_limits(self):
        self.reject(rfc(b"Subject: " + b"x" * 1000 + b"\r\n\r\nbody"), "1", "header_line_limit")
        payload = b"Subject: synthetic\r\n" + b"X-Synthetic: " + b"x" * 900 + b"\r\n"
        payload += (b"X-Synthetic: " + b"x" * 900 + b"\r\n") * 80 + b"\r\nbody"
        self.reject(rfc(payload), "1", "header_size_limit")

    def test_locator_validation_and_unlisted_paths(self):
        raw = multi([rfc(eml())])
        for locator in (None, 1, "", "0", "2", "01", "1.0", "1.01", "1/1", "../1",
                        "1..1", "1.1/../../secret", "1.1\x00", "1.9999999999", "1.1 " , "1." * 150):
            self.reject(raw, locator, "invalid_locator")
        for locator in ("1.2", "1.1.1", "1.999999999"):
            self.reject(raw, locator, "unlisted_locator")
        self.reject(raw, "1", "not_rfc822")

    def test_byte_limits_and_types(self):
        raw = multi([rfc(eml())])
        self.assertEqual(wire.capture_rfc822_payload(raw, "1.1", max_bytes=len(raw)), eml())
        self.reject(raw, code="byte_limit", max_bytes=len(raw) - 1)
        self.reject(b"", code="byte_limit")
        for bad in (None, "synthetic", bytearray(raw), memoryview(raw)):
            self.reject(bad, code="invalid_raw_bytes")

    def test_part_and_depth_bounds_before_stdlib_full_parse(self):
        raw = multi([rfc(eml()), rfc(eml())])
        self.assertEqual(wire.capture_rfc822_payload(raw, "1.1", max_parts=3), eml())
        with patch.object(wire, "BytesParser", side_effect=AssertionError("must not parse")):
            self.reject(raw, code="part_limit", max_parts=2)
            self.reject(raw, code="depth_limit", max_depth=1)
            deep = rfc(eml())
            for index in range(wire.MAX_DEPTH):
                deep = multi([deep], boundary=f"level-{index:03d}".encode())
            self.reject(deep, code="depth_limit")
            wide = multi([rfc(eml())] * wire.MAX_PARTS)
            self.reject(wide, code="part_limit")
        self.assertEqual(wire.capture_rfc822_payload(raw, "1.1", max_depth=2), eml())

    def test_invalid_or_raised_limits(self):
        for keyword, ceiling in (("max_bytes", wire.MAX_BYTES), ("max_depth", wire.MAX_DEPTH),
                                  ("max_parts", wire.MAX_PARTS)):
            for value in (0, -1, True, 1.5, "2", ceiling + 1):
                self.reject(rfc(eml()), "1", "invalid_limit", **{keyword: value})

    def test_unsupported_message_type_sibling(self):
        raw = multi([rfc(eml()), b"Content-Type: message/external-body\r\n\r\nsynthetic"])
        self.reject(raw, code="unsupported_message_type")

    def test_malformed_unselected_sibling_cannot_be_ignored(self):
        raw = multi([rfc(eml()), b"Content-Type: malformed\r\n\r\nsynthetic"])
        self.reject(raw, code="invalid_content_type")

    def test_stdlib_mapping_disagreement_fails_closed(self):
        parsed = BytesParser(policy=policy.default).parsebytes(multi([rfc(eml())]))
        parsed.set_payload([])
        with patch.object(wire, "BytesParser") as parser:
            parser.return_value.parsebytes.return_value = parsed
            self.reject(multi([rfc(eml())]), code="stdlib_mapping_mismatch")

    def test_bare_cr_boundary_interpretation_disagreement(self):
        raw = multi([rfc(eml()), b"Content-Type: text/plain\r\n\r\nsynthetic\r--synthetic-outer\rX-Synthetic: extra\r\rbody"])
        self.reject(raw, code="stdlib_mapping_mismatch")

    def test_inner_scalar_transfer_encoding_is_preserved_not_decoded(self):
        inner = b"Subject: synthetic\r\nContent-Transfer-Encoding: base64\r\n\r\nc3ludGhldGlj"
        self.assertEqual(wire.capture_rfc822_payload(rfc(inner), "1"), inner)


if __name__ == "__main__":
    unittest.main()
