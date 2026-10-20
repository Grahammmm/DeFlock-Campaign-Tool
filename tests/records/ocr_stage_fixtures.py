"""Synthetic scanned-PDF fixtures and fake OCR backends for the OCR stage tests.

Everything here is generated: no campaign records, no real model. The PDFs are
written byte by byte (no pypdf), so the tests also run on a plain python3.
"""
import hashlib
import json
from pathlib import Path
import subprocess
import zlib


def _pdf(objects):
    """Assemble a PDF from object bodies (object 1 must be the catalog)."""
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return bytes(out)


def _stream(header, data):
    return b"<< " + header + b" /Length %d >>\nstream\n" % len(data) + data + b"\nendstream"


def scanned_pdf(pages):
    """Image-only PDF: each page is ``(width, height, gray_bytes)`` drawn full page.

    There is no text layer, exactly like a scanner's output.
    """
    count = len(pages)
    kids = b" ".join(b"%d 0 R" % (3 + 3 * i) for i in range(count))
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>",
               b"<< /Type /Pages /Kids [" + kids + b"] /Count %d >>" % count]
    for index, (width, height, gray) in enumerate(pages):
        page_obj, content_obj, image_obj = 3 + 3 * index, 4 + 3 * index, 5 + 3 * index
        # 1 px = 1 pt at 72 dpi; scale up to 72/150 so 150 dpi renders read naturally.
        w_pt, h_pt = width * 72 / 150, height * 72 / 150
        objects.append(b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %.2f %.2f] "
                       b"/Resources << /XObject << /Im0 %d 0 R >> >> /Contents %d 0 R >>"
                       % (w_pt, h_pt, image_obj, content_obj))
        objects.append(_stream(b"", b"q %.2f 0 0 %.2f 0 0 cm /Im0 Do Q" % (w_pt, h_pt)))
        objects.append(_stream(b"/Type /XObject /Subtype /Image /Width %d /Height %d "
                               b"/ColorSpace /DeviceGray /BitsPerComponent 8 /Filter /FlateDecode"
                               % (width, height), zlib.compress(gray)))
    return _pdf(objects)


def noise_page(seed, width=40, height=50):
    """A small synthetic 'scan' whose pixels differ per seed (unique hashes)."""
    data = hashlib.sha256(b"scan-%d" % seed).digest()
    return width, height, (data * (width * height // len(data) + 1))[:width * height]


def blank_page(width=1275, height=1650):
    return width, height, b"\xff" * (width * height)


def native_text_pdf(lines):
    """Born-digital one-page PDF with Helvetica text (used to make a real scan)."""
    text = b"BT /F1 28 Tf 72 700 Td 36 TL " + b" ".join(
        b"(" + line.encode("ascii") + b") '" for line in lines) + b" ET"
    return _pdf([b"<< /Type /Catalog /Pages 2 0 R >>",
                 b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                 b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                 b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
                 _stream(b"", text),
                 b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"])


def rasterize_gray(pdftoppm, pdf_path, workdir, dpi=150):
    """Render page 1 to an 8-bit PGM with real pdftoppm and return (w, h, gray)."""
    base = Path(workdir) / "raster"
    subprocess.run([pdftoppm, "-r", str(dpi), "-gray", "-f", "1", "-l", "1", "-singlefile",
                    str(pdf_path), str(base)], check=True, timeout=60)
    raw = (Path(str(base) + ".pgm")).read_bytes()
    fields, position = [], 0
    while len(fields) < 4:  # P5 width height maxval, whitespace separated
        while raw[position:position + 1].isspace():
            position += 1
        start = position
        while not raw[position:position + 1].isspace():
            position += 1
        fields.append(raw[start:position])
    assert fields[0] == b"P5" and fields[3] == b"255"
    width, height = int(fields[1]), int(fields[2])
    return width, height, raw[position + 1:position + 1 + width * height]


PNG_STUB = b"\x89PNG\r\n\x1a\n" + b"synthetic-render"


class FakeTools:
    """Stands in for subprocess.run: fake ocrmypdf (sidecar) and pdftoppm (PNG).

    ``sidecars`` maps a source PDF's sha256 to its per-page texts. ``exit_code``
    or ``raise_timeout`` simulate OCRmyPDF failures.
    """

    def __init__(self, sidecars=None, exit_code=0, raise_timeout=False):
        self.sidecars = sidecars or {}
        self.exit_code = exit_code
        self.raise_timeout = raise_timeout
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        tool = Path(argv[0]).name
        if tool == "ocrmypdf":
            if self.raise_timeout:
                raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"))
            if self.exit_code:
                return subprocess.CompletedProcess(argv, self.exit_code)
            source, sidecar = Path(argv[-2]), Path(argv[argv.index("--sidecar") + 1])
            pages = self.sidecars[hashlib.sha256(source.read_bytes()).hexdigest()]
            sidecar.write_text("".join(text + "\f" for text in pages), encoding="utf-8")
            Path(argv[-1]).write_bytes(source.read_bytes())
            return subprocess.CompletedProcess(argv, 0)
        if tool == "pdftoppm":
            Path(argv[-1] + ".png").write_bytes(PNG_STUB)
            return subprocess.CompletedProcess(argv, 0)
        raise AssertionError("unexpected tool " + tool)

    def count(self, tool):
        return sum(1 for argv in self.calls if Path(argv[0]).name == tool)


class FakeVision:
    """Stands in for the llama.cpp transport: records requests, returns a reply."""

    def __init__(self, text="Handwritten note transcribed by vision", error=None, raw=None):
        self.text, self.error, self.raw = text, error, raw
        self.requests = []

    def __call__(self, endpoint, body, timeout):
        self.requests.append((endpoint, json.loads(body)))
        if self.error is not None:
            raise self.error
        if self.raw is not None:
            return self.raw
        return json.dumps({"choices": [{"message": {"role": "assistant", "content": self.text}}]}).encode()
