"""Synthetic per-page OCR fixtures shared by WP4 extraction tests; no real OCR tools run."""
import io
from pathlib import Path
import subprocess

from campaign_tool.records.extract import ocr

# Doctor-shaped tool map with fake absolute paths; FakeOCRRunner never executes them.
TOOLS = {name: {"available": True, "path": "/synthetic/bin/" + name, "version": name + " 0.0-test"}
         for name in ocr.OCR_TOOLS}
TOOLS["pypdf"] = {"available": True, "version": "0.0-test"}


class FakeOCRRunner:
    """Injected subprocess.run: pdftoppm writes a raster, one tesseract run writes txt+tsv."""

    def __init__(self, text="synthetic OCR text\n", confidence="42"):
        self.text = text
        self.confidence = confidence
        self.calls = []

    def __call__(self, command, **kwargs):
        name = Path(command[0]).name
        self.calls.append(name)
        if name == "pdftoppm":
            Path(command[-1] + ".png").write_bytes(b"synthetic raster")
        elif name == "tesseract":
            Path(command[2] + ".txt").write_text(self.text)
            Path(command[2] + ".tsv").write_text("text\tconf\nword\t" + self.confidence + "\n")
        return subprocess.CompletedProcess(command, 0, "", "")


def mixed_pdf(native_text="Synthetic native text"):
    """Two-page synthetic PDF: page 1 has native text, page 2 is image-only (blank)."""
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
    writer = PdfWriter()
    page = writer.add_blank_page(width=200, height=200)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                             NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
    content = DecodedStreamObject()
    content.set_data(("BT /F1 12 Tf 20 100 Td (" + native_text + ") Tj ET").encode("ascii"))
    page[NameObject("/Contents")] = writer._add_object(content)
    writer.add_blank_page(width=72, height=72)
    stream = io.BytesIO()
    writer.write(stream)
    return stream.getvalue()
