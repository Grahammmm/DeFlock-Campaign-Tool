"""RF2100-3: a Flate stream is well terminated only by its own complete ``endstream`` token after the zlib member."""
import unittest
import zlib
from unittest import mock

from campaign_tool import doc_assets
from campaign_tool.content import ContentError
from campaign_tool.doc_assets import PDF_UNREADABLE, check_asset, pdf_kinds
from campaign_tool.site import build_site
from tests import test_doc_assets as base

HIDDEN = b"<< /JS (x) >>"  # makes a healthy stream report "JavaScript"


def member_pdf(gap_and_tail, payload=b"q Q", *more):
    """A PDF whose first stream is a genuine zlib member followed by exactly ``gap_and_tail``."""
    packed = zlib.compress(payload)
    first = b"<< /Filter /FlateDecode >>\nstream\n" + packed + gap_and_tail
    return base.pdf(first, *more)


IMAGE = b"<< /Filter /DCTDecode >>\nstream\njpeg\nendstream"


class TerminatorTests(unittest.TestCase):
    def test_missing_first_terminator_cannot_borrow_later_stream(self):
        self.assertEqual(pdf_kinds(member_pdf(b"\nendstream")), [])  # healthy control
        for later in ((IMAGE,), (base.flate(HIDDEN, b""),)):
            data = member_pdf(b"\n", b"q Q", *later)
            self.assertEqual(pdf_kinds(data), [PDF_UNREADABLE])
        # unreadable wins over kinds an earlier stream already contributed
        data = base.pdf(base.flate(HIDDEN, b""), b"<< /Filter /FlateDecode >>\nstream\n" + zlib.compress(b"x") + b"\n",
                        IMAGE)
        self.assertEqual(pdf_kinds(data), [PDF_UNREADABLE])

    def test_endstream_requires_complete_keyword(self):
        for tail in (b"endstreamX", b"endstream7", b"endstream_", b"endstream\xc3\xa9", b"endstreamSYNTHETIC",
                     b"endstrea", b"endstrea m"):
            self.assertEqual(pdf_kinds(member_pdf(b"\n" + tail)), [PDF_UNREADABLE], tail)
        for tail in (b"endstream", b"endstream\n", b"endstream ", b"endstream/X", b"endstream%c", b"endstream>>"):
            self.assertEqual(pdf_kinds(member_pdf(b"\n" + tail + b"\nendobj\n")), [], tail)
        packed = zlib.compress(b"q Q")  # the keyword may end the input
        self.assertTrue(doc_assets._terminated(packed + b"endstream", len(packed)))
        self.assertFalse(doc_assets._terminated(packed + b"endstreamZ", len(packed)))

    def test_only_pdf_whitespace_can_separate_member_and_terminator(self):
        for gap in (b"", b"\n", b"\r", b"\r\n", b"\t", b"\x00", b"\x0c", b" ", b" \r\n\t\x00\x0c "):
            self.assertEqual(pdf_kinds(member_pdf(gap + b"endstream")), [], gap)
        packed = zlib.compress(b"x")
        for gap in (b"junk", b"\nJ", b"%c\n", b"(s)", b"\nendobj\n1 0 obj\n", b"\n" + packed, b"\n%\n"):
            self.assertEqual(pdf_kinds(member_pdf(gap + b"endstream")), [PDF_UNREADABLE], gap)

    def test_eof_offset_is_correct_with_same_chunk_and_cross_chunk_trailing_data(self):
        for chunk in (7, 16, 64):
            with mock.patch.object(doc_assets, "INFLATE_CHUNK", chunk):
                for size in range(1, 80):
                    packed = zlib.compress(bytes(range(size)) * 3)
                    for tail in (b"endstream", b"\nendstream"):
                        self.assertEqual(doc_assets._inflate(packed + tail + b" ", 0), zlib.decompress(packed))
                    for tail in (b"Xendstream", b"", b"endstreamX"):
                        with self.assertRaises(doc_assets.Unreadable):
                            doc_assets._inflate(packed + tail, 0)
        # the configured chunk size, with the member ending before, on and after the boundary
        for size in (doc_assets.INFLATE_CHUNK - 40, doc_assets.INFLATE_CHUNK - 11, doc_assets.INFLATE_CHUNK + 5):
            packed = zlib.compress(bytes(i * 7 % 251 for i in range(size)), 0)
            data = b"z" + packed
            self.assertEqual(len(doc_assets._inflate(data + b"\nendstream", 1)), size)
            with self.assertRaises(doc_assets.Unreadable):
                doc_assets._inflate(data + b"J\nendstream", 1)

    def test_embedded_marker_and_lying_length_do_not_choose_endpoint(self):
        payload = b"endstream endstream"
        for length in (1, 10 ** 6):
            packed = zlib.compress(payload)
            good = b"<< /Filter /FlateDecode /Length %d >>\nstream\n%s\nendstream" % (length, packed)
            bad = b"<< /Filter /FlateDecode /Length %d >>\nstream\n%s\n" % (length, packed)
            self.assertEqual(pdf_kinds(base.pdf(good)), [])
            self.assertEqual(pdf_kinds(base.pdf(bad, IMAGE)), [PDF_UNREADABLE])

    def test_caps_corruption_and_kind_precedence_remain_independent(self):
        with mock.patch.object(doc_assets, "MAX_INFLATE", 10):
            self.assertEqual(pdf_kinds(member_pdf(b"\nendstream", bytes(50))), [PDF_UNREADABLE])
        with mock.patch.object(doc_assets, "MAX_STREAMS", 1):
            self.assertEqual(pdf_kinds(base.pdf(base.flate(b"q", b""), base.flate(b"q", b""))), [PDF_UNREADABLE])
        packed = zlib.compress(b"q Q")
        self.assertEqual(pdf_kinds(base.pdf(b"<< /Filter /FlateDecode >>\nstream\n" + packed[:-3] + b"\nendstream")),
                         [PDF_UNREADABLE])
        for _ in range(2):  # repeated healthy / bad / healthy calls keep their results
            self.assertEqual(pdf_kinds(member_pdf(b"\nendstream")), [])
            self.assertEqual(pdf_kinds(member_pdf(b"\n")), [PDF_UNREADABLE])


class Consumers(base.Campaign):
    def malformed(self):
        return member_pdf(b"\n", b"q Q", IMAGE)

    def test_check_asset_and_content_check_agree_without_payload_or_write(self):
        with self.assertRaises(ContentError) as caught:
            check_asset(self._write(self.malformed()), "broken.pdf")
        self.assertEqual(str(caught.exception), "public-assets/broken.pdf: PDF (unreadable PDF)")
        before = self.snapshot()
        text = self.cli("content-check", "--directory", str(self.root))
        as_json = self.cli("content-check", "--directory", str(self.root), "--json")
        for result in (text, as_json):
            self.assertEqual(result.returncode, 3, result.stderr)
            self.assertIn(b"PDF (unreadable PDF)", result.stdout)
            self.assertIn(b"public-assets/broken.pdf", result.stdout)
            self.assertNotIn(b"Traceback", result.stdout + result.stderr)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.root / "public").exists())

    def _write(self, data):
        path = self.assets / "broken.pdf"
        path.write_bytes(data)
        return path

    def test_build_refusal_preserves_absent_and_existing_output(self):
        self._write(self.malformed())
        result = self.cli("build", "--directory", str(self.root))
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(b"public-assets/broken.pdf: PDF (unreadable PDF)", result.stderr)
        self.assertFalse((self.root / "public").exists())
        sentinel = self.root / "public" / "sentinel.txt"
        sentinel.parent.mkdir()
        sentinel.write_bytes(b"keep")
        before = self.snapshot()
        result = self.cli("build", "--directory", str(self.root))
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(self.snapshot(), before)
        healthy = member_pdf(b"\nendstream")  # the healthy counterpart builds and is copied byte-identical
        self._write(healthy)
        build_site(self.root)
        self.assertEqual((self.root / "public" / "broken.pdf").read_bytes(), healthy)
        with self.assertRaises(ContentError):
            self._write(self.malformed())
            build_site(self.root)


if __name__ == "__main__":
    unittest.main()
