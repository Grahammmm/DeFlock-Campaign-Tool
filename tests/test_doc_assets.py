"""DF2037-03: public-assets PDFs and SVGs with active content, hidden revisions or author fields are refused.

Every PDF and SVG is built here from bytes. A script body, URL or author name must never be printed.
"""
import re
import shutil
import subprocess
import sys
import tempfile
import tracemalloc
import unittest
import zlib
from pathlib import Path
from unittest import mock

from campaign_tool import doc_assets
from campaign_tool.content import ContentError
from campaign_tool.content_check import content_problems
from campaign_tool.doc_assets import PDF_UNREADABLE, SVG_UNREADABLE, pdf_kinds, svg_kinds
from campaign_tool.site import ASSET_MAX_BYTES, build_site

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
JS_TEXT, AUTHOR, URL = b"app.alert(41)", b"Jane Organizer", b"https://x.example/a.png"
CATALOG, PAGES = b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>"
PAGE = b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >>"


def pdf(*extra, catalog=CATALOG, first=None, trailer=b""):
    """A one-revision PDF: optional first object, catalog, pages, page, ``extra`` objects, xref, trailer, %%EOF."""
    objects = ([first] if first else []) + [catalog, PAGES, PAGE, *extra]
    out, offsets = bytearray(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n"), []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (number, body)
    xref, size = len(out), len(objects) + 1
    out += b"xref\n0 %d\n0000000000 65535 f \n" % size + b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R %s>>\nstartxref\n%d\n%%%%EOF\n" % (size, trailer, xref)
    return bytes(out)


def update(data, body=b"<< /Type /Catalog /Pages 2 0 R >>"):
    """An incremental update: a second body, xref and %%EOF appended to ``data``."""
    at = len(data)
    return data + (b"1 0 obj\n%s\nendobj\nxref\n1 1\n%010d 00000 n \ntrailer\n<< /Size 5 /Prev 0 >>\n"
                   b"startxref\n%d\n%%%%EOF\n" % (body, at, at + len(body) + 20))


def flate(payload, dictionary=b"/Type /ObjStm /N 1 /First 4"):
    packed = zlib.compress(payload)
    return b"<< %s /Filter /FlateDecode /Length %d >>\nstream\n%s\nendstream" % (dictionary, len(packed), packed)


def png_rows(payload, width, kind, pixel=1):
    """``payload`` PNG-predicted with row type ``kind`` (0-4) on rows of ``width`` bytes, ``pixel`` bytes a pixel."""
    out, prior = bytearray(), bytes(width)
    for start in range(0, len(payload), width):
        row = payload[start:start + width]
        coded = bytearray()
        for i, byte in enumerate(row):
            left, up = row[i - pixel] if i >= pixel else 0, prior[i]
            corner = prior[i - pixel] if i >= pixel else 0
            guess = left + up - corner
            paeth = min((abs(guess - left), 0, left), (abs(guess - up), 1, up), (abs(guess - corner), 2, corner))[2]
            coded.append((byte - (0, left, up, (left + up) // 2, paeth)[kind]) & 0xFF)
        out += bytes([kind]) + coded
        prior = row + bytes(width - len(row))
    return bytes(out)


def tiff_rows(payload, colors, columns):
    """``payload`` TIFF-predicted (8-bit samples): each sample less the same colour's sample to its left."""
    width, out = colors * columns, bytearray()
    for start in range(0, len(payload), width):
        row = payload[start:start + width]
        out += bytes((row[i] - (row[i - colors] if i >= colors else 0)) & 0xFF for i in range(len(row)))
    return bytes(out)


def svg(body, attrs=b""):
    return b'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink"%s>%s</svg>' % (
        attrs, body)


CLEAN_SVG = svg(b'<defs><linearGradient id="grad1"/></defs><rect fill="url(#grad1)" width="4" height="4"/>'
                b'<use href="#grad1"/><use xlink:href="#grad1"/><style>rect { fill: url(#grad1) }</style>')


class PdfKindsTests(unittest.TestCase):
    # 1. a minimal one-revision PDF has no kinds
    def test_clean_pdf(self):
        self.assertEqual(pdf_kinds(pdf()), [])
        self.assertEqual(pdf_kinds(pdf(flate(b"BT /F1 12 Tf (Hello) Tj ET", b""))), [])

    # 2. OpenAction JavaScript: both kinds, never the script text
    def test_open_action_javascript(self):
        catalog = b"<< /Type /Catalog /Pages 2 0 R /OpenAction << /S /JavaScript /JS (%s) >> >>" % JS_TEXT
        self.assertEqual(pdf_kinds(pdf(catalog=catalog)), ["JavaScript", "actions"])
        launch = b"<< /Type /Catalog /Pages 2 0 R /AA << /O << /S /Launch /F (calc.exe) >> >> >>"
        self.assertEqual(pdf_kinds(pdf(catalog=launch)), ["actions"])

    # 3. names hidden by #xx escapes or inside a Flate-compressed object stream are still found
    def test_hidden_names(self):
        escaped = b"<< /Type /Catalog /Pages 2 0 R /Open#41ction << /S /Java#53cript /J#53 (x) >> >>"
        self.assertEqual(pdf_kinds(pdf(catalog=escaped)), ["JavaScript", "actions"])
        hidden = flate(b"5 0 << /S /JavaScript /JS (%s) >>" % JS_TEXT)
        self.assertNotIn(b"JavaScript", hidden)
        self.assertEqual(pdf_kinds(pdf(hidden)), ["JavaScript"])
        escaped_filter = flate(b"<< /Author (x) >>").replace(b"/FlateDecode", b"/Flate#44ecode")
        self.assertEqual(pdf_kinds(pdf(escaped_filter)), ["author metadata"])
        array_filter = flate(b"<< /EmbeddedFiles 9 0 R >>").replace(b"/Filter /FlateDecode", b"/Filter [/FlateDecode]")
        self.assertEqual(pdf_kinds(pdf(array_filter)), ["embedded files"])
        for gap in (b" % comment\n", b"%\r", b" %% a % b\r\n %c\n", b"\n[ % x\n"):  # comments are whitespace
            commented = flate(b"<< /JS (x) /Author (a) >>").replace(b"/Filter /", b"/Filter" + gap + b"/")
            if gap.strip().startswith(b"["):
                commented = commented.replace(b"/FlateDecode", b"/FlateDecode %y\n]")
            self.assertEqual(pdf_kinds(pdf(commented)), ["JavaScript", "author metadata"], gap)
        # an ``obj``/``stream``/``endobj`` inside a comment or string is not a keyword: the real header still counts
        for decoy in (b"/Filter % object comment\n/FlateDecode", b"/Filter % 1 0 obj\r/FlateDecode",
                      b"/Filter /FlateDecode /T (obj) /U (a\\) 7 0 obj (\\(nested obj)) )",
                      b"/T (stream\n) /Filter /FlateDecode", b"/Filter %endobj stream\n/FlateDecode"):
            tricky = flate(b"<< /JS (x) /Author (a) >>").replace(b"/Filter /FlateDecode", decoy)
            self.assertEqual(pdf_kinds(pdf(tricky)), ["JavaScript", "author metadata"], decoy)
        # stream bounds come from neither /Length nor ``endstream``: an ``endstream\n(`` inside benign stream data,
        # closed by a ``)`` in a later benign stream, or a lying /Length, still leaves the hidden stream inflated
        opening = b"<< /Length 13 >>\nstream\nendstream\n(\nendstream"
        closing = b"<< /Subtype /Image /Filter /DCTDecode /Length 3 >>\nstream\n\xff)\xd8\nendstream"
        hidden = flate(b"<< /JS (x) /Author (a) >>")
        self.assertEqual(pdf_kinds(pdf(opening, hidden, closing)), ["JavaScript", "author metadata"])
        lying = re.sub(rb"/Length \d+", b"/Length 1", hidden)
        self.assertNotEqual(lying, hidden)
        self.assertEqual(pdf_kinds(pdf(lying)), ["JavaScript", "author metadata"])
        unfiltered = b"<< /Length 99 >>\nstream\n" + hidden + b"\nendstream"  # a stream keyword inside stream data
        self.assertEqual(pdf_kinds(pdf(unfiltered)), ["JavaScript", "author metadata"])

    # 3b. a stream encoded other than Flate-only or image-only cannot be read for names: unreadable
    def test_other_encodings(self):
        image = b"<< /Subtype /Image /Filter [/DCTDecode] /Length 3 >>\nstream\nabc\nendstream"
        self.assertEqual(pdf_kinds(pdf(image, image.replace(b"[/DCTDecode]", b"/JPXDecode"))), [])
        for value in (b"/ASCIIHexDecode", b"/LZWDecode", b"[/ASCII85Decode /FlateDecode]", b"[/Fl /DCT]",
                      b"5 0 R", b"/RunLengthDecode", b"[/FlateDecode", b"<< /FlateDecode 1 >>"):
            other = b"<< /Filter %s /Length 3 >>\nstream\nabc\nendstream" % value
            self.assertEqual(pdf_kinds(pdf(other)), [PDF_UNREADABLE], value)

    # 3c. a /DecodeParms predictor cannot hide names: PNG (every row type) and TIFF predictors are undone first
    def test_predictors(self):
        hidden = b"5 0 << /S /JavaScript /JS (%s) /Author (%s) >>" % (JS_TEXT, AUTHOR)
        found = ["JavaScript", "author metadata"]
        # the reported case: /Predictor 10 /Columns 1 puts a zero row-type byte before every character
        spaced = flate(png_rows(hidden, 1, 0), b"/Type /ObjStm /N 1 /First 4 /DecodeParms <</Predictor 10/Columns 1>>")
        self.assertNotIn(b"JS", zlib.decompress(spaced[spaced.index(b"stream\n") + 7:spaced.rindex(b"\nendstream")]))
        self.assertEqual(pdf_kinds(pdf(spaced)), found)
        for kind in range(5):
            for colors, columns in ((1, 7), (3, 4)):
                params = b"<< /Predictor %d /Colors %d /Columns %d /BitsPerComponent 8 >>" % (
                    11 + kind, colors, columns)
                for value in (params, b"[%s]" % params, b"[ null %% c\n %s ]" % params):
                    encoded = flate(png_rows(hidden, colors * columns, kind, colors), b"/DecodeParms " + value)
                    self.assertEqual(pdf_kinds(pdf(encoded)), found, (kind, colors, value))
        escaped = flate(png_rows(hidden, 3, 1), b"/Decode#50arms << /Pred#69ctor 15 /Columns 3 >>")
        self.assertEqual(pdf_kinds(pdf(escaped)), found)
        abbreviated = flate(png_rows(hidden, 3, 2), b"/DP << /Predictor 12 /Columns 3 >>")
        self.assertEqual(pdf_kinds(pdf(abbreviated)), found)
        tiff = flate(tiff_rows(hidden, 2, 5), b"/DecodeParms << /Predictor 2 /Colors 2 /Columns 5 >>")
        self.assertNotIn(b"JS", zlib.decompress(tiff[tiff.index(b"stream\n") + 7:tiff.rindex(b"\nendstream")]))
        self.assertEqual(pdf_kinds(pdf(tiff)), found)
        # every /DecodeParms before a stream counts, so an earlier null one does not stop the predictor being undone
        decoy = flate(png_rows(hidden, 2, 1), b"/T (x) /DecodeParms null /DecodeParms << /Predictor 12 /Columns 2 >>")
        self.assertEqual(pdf_kinds(pdf(decoy)), found)
        # a `stream` keyword in a string, comment or name after the predictor cannot take it from the real stream:
        # which stream it applies to is then unclear, so the PDF is unreadable
        for between in (b"/T (stream\n)", b"/T (>> stream\n)", b"%stream\n", b"/stream", b"/T <</X 1>> [stream\n]"):
            hiding = flate(png_rows(hidden, 1, 0), b"/Type /ObjStm /N 1 /First 4 /DecodeParms << /Predictor 10 "
                           b"/Columns 1 >> " + between)
            self.assertEqual(pdf_kinds(pdf(hiding)), [PDF_UNREADABLE], between)
        # clean predicted streams (an xref stream, images with image-filter parameters) have no kinds
        xref = flate(png_rows(bytes(range(40)), 5, 2), b"/Type /XRef /DecodeParms << /Columns 5 /Predictor 12 >> "
                     b"/W [1 3 1] /Index [0 8] /Size 8 /Root 1 0 R /ID [<0aF1><0A f1>] /Prev 1.5 /X true /Y null")
        ccitt = (b"<< /Subtype /Image /Filter /CCITTFaxDecode /DecodeParms << /K -1 /Columns 8 /BlackIs1 true >> "
                 b"/Length 3 >>\nstream\nabc\nendstream")
        jbig2 = (b"<< /Subtype /Image /Filter /JBIG2Decode /DecodeParms << /JBIG2Globals 9 0 R /F 1.5 /N /x >> "
                 b"/Length 3 >>\nstream\nabc\nendstream")
        identity = flate(b"<< /Type /Font >>", b"/DecodeParms << /Predictor 1 /Columns 3 >>")
        self.assertEqual(pdf_kinds(pdf(xref, ccitt, jbig2, identity)), [])
        # parameters that cannot be read, or a predictor no reader undoes this way: unreadable
        for value in (b"9 0 R", b"<< /Predictor 9 0 R /Columns 3 >>", b"<< /Predictor 12 /Columns 3 0 R >>",
                      b"<< /Predictor 3 >>", b"<< /Predictor 16 >>", b"<< /Predictor 12.0 >>", b"[9 0 R]",
                      b"<< /Predictor 12 /Predictor 1 >>", b"<< /Predictor 12 /BitsPerComponent 3 >>",
                      b"<< /Predictor 2 /BitsPerComponent 16 >>", b"<< /Predictor 12 /Columns 0 >>",
                      b"<< /Predictor 12 /Colors 33 >>", b"<< /Predictor 12 /Columns (3) >>",
                      b"<< /Predictor 12 /Columns [3] >>", b"(text)"):
            other = flate(png_rows(hidden, 3, 0), b"/DecodeParms " + value)
            self.assertEqual(pdf_kinds(pdf(other)), [PDF_UNREADABLE], value)
        bad_row = flate(b"\x05abc\x00def", b"/DecodeParms << /Predictor 12 /Columns 3 >>")
        self.assertEqual(pdf_kinds(pdf(bad_row)), [PDF_UNREADABLE])
        large = [flate(png_rows(bytes(1000), 10, 0), b"/DecodeParms << /Predictor 10 /Columns 10 >>")] * 3
        with mock.patch.object(doc_assets, "MAX_PREDICT_TOTAL", 3299):  # 3 x 1100 predicted bytes, bounded together
            self.assertEqual(pdf_kinds(pdf(*large)), [PDF_UNREADABLE])
        with mock.patch.object(doc_assets, "MAX_PREDICT_TOTAL", 3300):
            self.assertEqual(pdf_kinds(pdf(*large)), [])

    # 4. incremental updates are revisions; a linearized file may have exactly two %%EOF
    def test_revisions(self):
        self.assertEqual(pdf_kinds(update(pdf())), ["more than one revision"])
        self.assertEqual(pdf_kinds(update(update(pdf()))), ["more than one revision"])
        linear = pdf(first=b"<< /Linearized 1 /L 900 /O 4 /E 0 /N 1 /T 0 >>")
        self.assertEqual(pdf_kinds(linear), [])
        self.assertEqual(pdf_kinds(update(linear)), [])
        self.assertEqual(pdf_kinds(update(update(linear))), ["more than one revision"])
        late = pdf(b"<< /Linearized 1 >>")  # /Linearized outside the first object does not count
        self.assertEqual(pdf_kinds(update(late)), ["more than one revision"])

    # 5. embedded files and author metadata (Info and XMP), the name never returned
    def test_embedded_files_and_author(self):
        catalog = b"<< /Type /Catalog /Pages 2 0 R /Names << /EmbeddedFiles << /Names [(a.txt) 9 0 R] >> >> >>"
        self.assertEqual(pdf_kinds(pdf(catalog=catalog)), ["embedded files"])
        self.assertEqual(pdf_kinds(pdf(b"<< /RichMedia 1 >>")), ["embedded files"])
        info = pdf(b"<< /Author (%s) /Producer (x) >>" % AUTHOR, trailer=b"/Info 5 0 R ")
        self.assertEqual(pdf_kinds(info), ["author metadata"])
        for xmp in (b"<pdf:Author>%s</pdf:Author>" % AUTHOR, b"<dc:creator><rdf:Seq><rdf:li>%s</rdf:li>" % AUTHOR,
                    b'<rdf:Description pdf:Author="%s"/>' % AUTHOR):
            plain = b"<< /Type /Metadata /Subtype /XML >>\nstream\n%s\nendstream" % xmp
            self.assertEqual(pdf_kinds(pdf(plain)), ["author metadata"], xmp)
            self.assertEqual(pdf_kinds(pdf(flate(xmp, b"/Type /Metadata"))), ["author metadata"], xmp)
        self.assertEqual(pdf_kinds(pdf(b"<< /Producer (%s) /Title (Agenda) >>" % AUTHOR)), [])

    # 6. zip bomb, truncation, too many streams, bad Flate, non-PDF bytes: unreadable, no exception, bounded memory
    def test_unreadable(self):
        packer, block = zlib.compressobj(9), bytes(1024 * 1024)
        bomb = b"".join(packer.compress(block) for _ in range(64)) + packer.flush()  # inflates to 64 MiB
        bomb_pdf = pdf(b"<< /Filter /FlateDecode >>\nstream\n%s\nendstream" % bomb)
        tracemalloc.start()
        try:
            self.assertEqual(pdf_kinds(bomb_pdf), [PDF_UNREADABLE])
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 3 * doc_assets.MAX_INFLATE)
        many = [flate(b"q Q", b"") for _ in range(doc_assets.MAX_STREAMS)]
        self.assertEqual(pdf_kinds(pdf(*many)), [])
        self.assertEqual(pdf_kinds(pdf(*many, flate(b"q Q", b""))), [PDF_UNREADABLE])
        large = [flate(bytes(1000), b"") for _ in range(3)]
        with mock.patch.object(doc_assets, "MAX_INFLATE_TOTAL", 2999):  # all streams together are bounded too
            self.assertEqual(pdf_kinds(pdf(*large)), [PDF_UNREADABLE])
        with mock.patch.object(doc_assets, "MAX_INFLATE_TOTAL", 3000):
            self.assertEqual(pdf_kinds(pdf(*large)), [])
        bad_flate = b"<< /Filter /FlateDecode >>\nstream\nnot zlib\nendstream"
        cut = pdf(flate(b"q Q"))
        cut = cut[:cut.index(b"endstream")] + b"\n%%EOF\n"
        cases = {"zip bomb": bomb_pdf, "not a pdf": b"<html>%PDF-1.7", "empty": b"", "bad flate": pdf(bad_flate),
                 "no %%EOF": pdf()[:-6], "stream without endstream": cut,
                 "too large": b"%PDF-1.7\n" + bytes(ASSET_MAX_BYTES)}
        for name, data in cases.items():
            self.assertEqual(pdf_kinds(data), [PDF_UNREADABLE], name)
        dirty = pdf(flate(b"<< /JS (x) >>"), catalog=b"<< /OpenAction 3 0 R /Author (a) >>")
        for cut in range(0, len(dirty), 7):  # every truncation answers with a list, never an exception
            self.assertIsInstance(pdf_kinds(dirty[:cut]), list)


class SvgKindsTests(unittest.TestCase):
    # 7. each SVG kind; local #fragment references are clean
    def test_kinds(self):
        self.assertEqual(svg_kinds(CLEAN_SVG), [])
        cases = {b"<script>alert(1)</script>": ["script"], b"<SCRIPT/>": ["script"],
                 b'<rect onload="x()"/>': ["event handler"], b'<rect ONCLICK="x()"/>': ["event handler"],
                 b"<foreignObject><div/></foreignObject>": ["foreignObject"],
                 b'<image xlink:href="%s"/>' % URL: ["external reference"],
                 b'<image href="data:image/png;base64,AAAA"/>': ["external reference"],
                 b'<a href=""/>': ["external reference"],
                 b'<rect style="fill:url( %s)"/>' % URL: ["external reference"],
                 b"<rect fill=\"url('%s')\"/>" % URL: ["external reference"],
                 b'<rect style="fill:u\\72l(%s)"/>' % URL: ["external reference"],
                 b"<style>@import 'x.css';</style>": ["external reference"],
                 b"<style><![CDATA[rect { fill: url(%s) }]]></style>" % URL: ["external reference"],
                 b'<set attributeName="xlink:href" to="#a"/>': ["external reference"],
                 b'<script/><rect onload="x()"/><foreignObject/>': ["event handler", "foreignObject", "script"]}
        for body, kinds in cases.items():
            self.assertEqual(svg_kinds(svg(body)), kinds, body)
        self.assertEqual(svg_kinds(svg(b"", b' onload="x()"')), ["event handler"])
        for escape in (rb"\ffffff", rb"\110000", rb"\d800", rb"\0"):  # out of range: U+FFFD, never a crash
            self.assertEqual(svg_kinds(svg(b"<style>rect { fill: red }%s</style>" % escape)), [], escape)
            self.assertEqual(svg_kinds(svg(b'<rect style="fill:%s url(%s)"/>' % (escape, URL))),
                             ["external reference"], escape)
        self.assertEqual(svg_kinds(b'<?xml-stylesheet href="%s"?>' % URL + CLEAN_SVG), ["external reference"])

    # 8. DOCTYPE/ENTITY refused on the bytes before any parse; malformed or non-UTF-8 is unreadable
    def test_doctype_and_unreadable(self):
        laughs = (b'<?xml version="1.0"?><!DOCTYPE svg [<!ENTITY a "lol"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;">]>'
                  b"<svg>&b;</svg>")
        with mock.patch.object(doc_assets.ElementTree, "fromstring", side_effect=AssertionError("parsed")):
            self.assertEqual(svg_kinds(laughs), ["DOCTYPE"])
            self.assertEqual(svg_kinds(b"<!entity x>" + CLEAN_SVG), ["DOCTYPE"])
        for data in (b"<svg><rect></svg>", b"", b"not xml", svg(b"\xff\xfe"), CLEAN_SVG.decode().encode("utf-16"),
                     b"<svg/>" + bytes(ASSET_MAX_BYTES)):
            self.assertEqual(svg_kinds(data), [SVG_UNREADABLE], data[:40])

    def test_limit_matches_build(self):
        self.assertEqual(doc_assets.MAX_BYTES, ASSET_MAX_BYTES)

    # DF0137-16 2/3: width/height (unitless or px) else viewBox, rounded; anything else is None
    def test_svg_size(self):
        cases = {b' width="240" height="80"': (240, 80), b' width="240px" height="80PX"': (240, 80),
                 b' width=" 12.5 " height=".5"': (12, 0), b' viewBox="0 0 512 128"': (512, 128),
                 b' viewBox="-5,-5,40.6,10"': (41, 10), b' viewBox="0 0 512 128" width="100%"': (512, 128),
                 b' viewBox="0 0 512 128" width="10em" height="3em"': (512, 128),
                 b' viewBox="0 0 512 128" width="30" height="20"': (30, 20),
                 b' viewBox="0 0 512 128" width="30"': (512, 128),
                 b' viewBox="0 0 512" ': None, b' viewBox="0 0 -4 10"': None, b' viewBox="0 0 0 10"': None,
                 b' viewBox="0 0 4 0"': None, b' viewBox="0 0 a b"': None, b' viewBox="0 0 1e999 5"': None,
                 b' viewBox="0 0 nan 5"': None, b' viewBox="0 0 4 5 6"': None, b' viewBox=""': None,
                 b' width="100%" height="100%"': None, b' width="10em" height="4em"': None,
                 b' width="10mm" height="4mm"': None, b' width="-3" height="4"': None, b' width="0" height="4"': None,
                 b' width="10"': None, b"": None}
        for attrs, expected in cases.items():
            self.assertEqual(doc_assets.svg_size(svg(b"", attrs)), expected, attrs)
        self.assertIsNone(doc_assets.svg_size(b'<html width="4" height="4"/>'))
        self.assertIsNone(doc_assets.svg_size(b"<svg width='4' height='4'><rect></svg>"))
        self.assertEqual(doc_assets.svg_size(b'<s:svg xmlns:s="http://www.w3.org/2000/svg" width="4" height="5"/>'), (4, 5))


class Campaign(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-doc-assets-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.assets = self.root / "content" / "public-assets"
        (self.assets / "docs").mkdir(parents=True, exist_ok=True)

    def cli(self, *args):
        return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", *args], cwd=REPO_ROOT,
                              capture_output=True, timeout=120)

    def tree(self):
        return {p.relative_to(self.root).as_posix(): p.read_bytes() for p in sorted(self.root.rglob("*")) if p.is_file()}

    def dirty(self):
        catalog = b"<< /Type /Catalog /Pages 2 0 R /OpenAction << /S /JavaScript /JS (%s) >> >>" % JS_TEXT
        (self.assets / "docs" / "agenda.pdf").write_bytes(update(pdf(catalog=catalog)))
        (self.assets / "logo.svg").write_bytes(svg(b'<script>%s</script><image href="%s"/>' % (JS_TEXT, URL)))
        (self.assets / "docs" / "minutes.pdf").write_bytes(pdf(b"<< /Author (%s) >>" % AUTHOR))


class BuildGateTests(Campaign):
    # 1. clean PDF and SVG are copied byte-identical
    def test_clean_copied_byte_identical(self):
        files = {"docs/agenda.pdf": pdf(flate(b"BT (Agenda) Tj ET", b"")), "logo.svg": CLEAN_SVG,
                 "docs/linear.pdf": update(pdf(first=b"<< /Linearized 1 >>"))}
        for name, data in files.items():
            (self.assets / name).write_bytes(data)
        build_site(self.root)
        for name, data in files.items():
            self.assertEqual((self.root / "public" / name).read_bytes(), data, name)

    # 2. refused PDF and SVG: file and kinds named, script, URL and author never shown; public/ not written
    def test_refused_values_never_shown(self):
        self.dirty()
        (self.assets / "logo.svg").unlink()
        (self.assets / "docs" / "minutes.pdf").unlink()
        with self.assertRaises(ContentError) as caught:
            build_site(self.root)
        self.assertEqual(str(caught.exception), "public-assets/docs/agenda.pdf: PDF (JavaScript, actions, "
                                                "more than one revision)")
        self.assertFalse((self.root / "public").exists())
        (self.assets / "docs" / "agenda.pdf").unlink()
        (self.assets / "logo.svg").write_bytes(svg(b'<script>%s</script><image href="%s"/>' % (JS_TEXT, URL)))
        result = self.cli("build", "--directory", str(self.root))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"public-assets/logo.svg: SVG (external reference, script)", result.stderr)
        for secret in (JS_TEXT, URL):
            self.assertNotIn(secret, result.stdout + result.stderr)
        self.assertFalse((self.root / "public").exists())


class ContentCheckTests(Campaign):
    # 9. content-check lists the PDF/SVG problems with the others, exits 3, nothing written, no value shown
    def test_listed_with_other_problems_exit_3(self):
        self.dirty()
        (self.assets / "docs" / "ok.pdf").write_bytes(pdf())
        (self.assets / "ok.svg").write_bytes(CLEAN_SVG)
        (self.root / "content" / "site.json").write_text("{", encoding="utf-8")
        problems = content_problems(self.root)
        for line in ("public-assets/docs/agenda.pdf: PDF (JavaScript, actions, more than one revision)",
                     "public-assets/docs/minutes.pdf: PDF (author metadata)",
                     "public-assets/logo.svg: SVG (external reference, script)"):
            self.assertIn(line, problems)
        self.assertTrue(any(line.startswith("site.json") for line in problems), problems)
        self.assertFalse(any("ok.pdf" in line or "ok.svg" in line for line in problems), problems)
        before = self.tree()
        result = self.cli("content-check", "--directory", str(self.root))
        self.assertEqual(result.returncode, 3)
        self.assertIn(b"public-assets/docs/minutes.pdf: PDF (author metadata)", result.stdout)
        for secret in (JS_TEXT, URL, AUTHOR):
            self.assertNotIn(secret, result.stdout + result.stderr)
        self.assertEqual(self.tree(), before)

    def test_clean_campaign_has_no_problem(self):
        (self.assets / "docs" / "ok.pdf").write_bytes(pdf())
        (self.assets / "ok.svg").write_bytes(CLEAN_SVG)
        self.assertEqual(content_problems(self.root), [])


if __name__ == "__main__":
    unittest.main()
