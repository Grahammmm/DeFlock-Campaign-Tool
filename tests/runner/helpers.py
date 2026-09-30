"""Shared helpers for runner tests: settings in a temp workdir, silent log, tiny synthetic files."""
import io
import tempfile
import zipfile
from pathlib import Path

from runner.client import FakeWorkspace
from runner.loop import Context, Log, Settings

REPO = Path(__file__).resolve().parents[2]


class Silent(Log):
    def __init__(self):
        super().__init__(io.StringIO())

    @property
    def lines(self):
        return self.stream.getvalue().splitlines()


def settings(tmp, **overrides):
    base = Settings(workdir=Path(tmp) / "work", privacy_tier="redacted_cloud", poll_interval=0.0)
    for key, value in overrides.items():
        setattr(base, key, value)
    return base


def context(tmp, job, workspace=None, **overrides):
    ws = workspace or FakeWorkspace()
    return Context(ws, settings(tmp, **overrides), job, Silent())


def job(kind, inputs=None, job_id="job_0000000000000001", tier="redacted_cloud"):
    return {"job_id": job_id, "campaign_id": "01test0000000000000000000a", "kind": kind, "idempotency_key": "0" * 64,
            "inputs": inputs or {}, "attempt": 1, "max_attempts": 3, "enqueued_at": "2026-09-30T00:00:00Z", "privacy_tier": tier}


def tiny_pdf(text="Synthetic ALPR policy. Retained for 30 days. Shared with federal agencies."):
    """A one-page PDF with a text stream pypdf can read, no third-party writer needed."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for n, obj in enumerate(objects, 1):
        offsets.append(out.tell())
        out.write(f"{n} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return out.getvalue()


def tiny_xlsx(rows=(("event_id", "purpose", "plate"), ("DEMO-1", "", "7ABC123"))):
    """Minimal workbook via openpyxl (already pinned for extraction tests)."""
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "log"
    for row in rows:
        ws.append(list(row))
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def tiny_zip(members):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for name, data in members.items():
            z.writestr(name, data)
    return out.getvalue()


def tempdir():
    return tempfile.TemporaryDirectory()
