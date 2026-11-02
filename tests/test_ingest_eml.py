"""DF2037-07: `ingest --eml MESSAGE --source-id ID` stores the message as is and each attachment by hash.

Each attachment (decoded payload of a part with a filename) gets a receipt `<message>/<sanitized name>` and a
ledger `contains` row; body text and inline parts without a filename stay inside the stored message. Every
message is synthetic (example.org). Run from the repository root:
python3 -B -m unittest tests.test_ingest_eml -v
"""

import contextlib
from email.message import EmailMessage
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
HEADER_VALUES = ("Synthetic production subject 7Q", "records-clerk-7q@example.org", "organizer-7q@example.org")
PDF, CSV = b"%PDF-1.4 synthetic\n\x00\xff binary\n", b"col_a,col_b\n1,2\n"


def message(*attachments, html=True):
    """A synthetic message: text body, optional HTML alternative, then (data, maintype, subtype, filename)."""
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = HEADER_VALUES
    msg.set_content("Synthetic body text.\n")
    if html:
        msg.add_alternative("<p>Synthetic body text.</p>\n", subtype="html")
    for data, maintype, subtype, filename in attachments:
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
    return msg


def raw(boundary_header, body):
    """Hand-written message bytes (CRLF) for encodings and defects EmailMessage would not produce."""
    head = [f"Subject: {HEADER_VALUES[0]}", f"From: {HEADER_VALUES[1]}", f"To: {HEADER_VALUES[2]}",
            "MIME-Version: 1.0", boundary_header, "", ""]
    return ("\r\n".join(head) + body).encode()


class IngestEmlTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-ingest-eml-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root, self.work, self.inbox = self.base / "campaign", self.base / "work", self.base / "inbox"
        self.work.mkdir()
        self.inbox.mkdir()
        self.cli("init", "--name", "Synthetic Campaign", "--county", "Synthetic County", "--state", "ca")

    def cli(self, command, *arguments, returncode=0):
        env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(REPO_ROOT)}
        result = subprocess.run(
            [sys.executable, "-B", "-m", "campaign_tool", command, "--directory", str(self.root), *map(str, arguments)],
            cwd=self.work, env=env, capture_output=True, text=True, timeout=300)
        self.assertEqual(result.returncode, returncode, f"stdout={result.stdout!r}; stderr={result.stderr!r}")
        self.assertNotIn("Traceback", result.stderr)
        for value in HEADER_VALUES:  # edge 8: no header value on any path, success or refusal
            self.assertNotIn(value, result.stdout + result.stderr)
        return result

    def ingest(self, path, source_id="synthetic-1", dry=False, returncode=0):
        result = self.cli("ingest", "--eml", path, "--source-id", source_id, *(["--dry-run"] if dry else []),
                          returncode=returncode)
        if returncode:
            return result
        if dry:
            body, last = result.stdout.rstrip("\n").rsplit("\n", 1)
            self.assertEqual(last, "Dry run: nothing was written.")
            return json.loads(body)
        return json.loads(result.stdout)

    def write(self, msg, name="m.eml"):
        path = self.inbox / name
        path.write_bytes(msg if isinstance(msg, bytes) else msg.as_bytes())
        return path

    def files(self):
        return {str(p.relative_to(self.base)): p.read_bytes() for p in self.base.rglob("*") if p.is_file()}

    def rows(self, query):
        db = sqlite3.connect(self.root / "private" / "ledger.sqlite")
        try:
            return db.execute(query).fetchall()
        finally:
            db.close()

    def names(self, report):
        return [item["name"] for item in report["attachments"]]

    def test_body_html_and_two_attachments_three_objects_receipts_two_contains_rows(self):
        path = self.write(message((PDF, "application", "pdf", "a.pdf"), (CSV, "text", "csv", "b.csv")))
        before = self.files()
        report = self.ingest(path)
        whole = hashlib.sha256(path.read_bytes()).hexdigest()
        sa, sb = hashlib.sha256(PDF).hexdigest(), hashlib.sha256(CSV).hexdigest()
        self.assertEqual((report["message"]["sha256"], report["message"]["outcome"]), (whole, "stored"))
        self.assertEqual(report["attachments"], [{"name": "a.pdf", "sha256": sa, "outcome": "stored"},
                                                 {"name": "b.csv", "sha256": sb, "outcome": "stored"}])
        self.assertEqual((report["skipped"], report["parts"]), ([], 6))
        created = {k: v for k, v in self.files().items() if k not in before}
        objects = "campaign/private/objects/"
        self.assertEqual(set(created), {"campaign/private/ledger.sqlite", objects + whole, objects + sa, objects + sb})
        self.assertEqual((created[objects + whole], created[objects + sa], created[objects + sb]),
                         (path.read_bytes(), PDF, CSV))
        self.assertEqual(sorted(r[0] for r in self.rows("SELECT original_name FROM receipts")),
                         ["m.eml", "m.eml/a.pdf", "m.eml/b.csv"])
        self.assertEqual(sorted(self.rows("SELECT * FROM contains")), sorted([(whole, sa, "a.pdf"), (whole, sb, "b.csv")]))
        self.cli("status", "--check")

    def test_base64_and_quoted_printable_decode_and_rfc2231_name_decodes(self):
        body = ("--B\r\nContent-Type: text/plain\r\n\r\nbody\r\n"
                "--B\r\nContent-Type: application/octet-stream\r\nContent-Transfer-Encoding: base64\r\n"
                "Content-Disposition: attachment; filename*=utf-8''r%C3%A9sum%C3%A9.bin\r\n\r\nAP8BAgM=\r\n"
                "--B\r\nContent-Type: text/plain\r\nContent-Transfer-Encoding: quoted-printable\r\n"
                "Content-Disposition: attachment; filename=\"q.txt\"\r\n\r\ncaf=C3=A9 =3D ok\r\n--B--\r\n")
        report = self.ingest(self.write(raw('Content-Type: multipart/mixed; boundary="B"', body)))
        self.assertEqual(self.names(report), ["r\u00e9sum\u00e9.bin", "q.txt"])
        stored = [(self.root / "private" / "objects" / item["sha256"]).read_bytes() for item in report["attachments"]]
        self.assertEqual(stored, [b"\x00\xff\x01\x02\x03", "caf\u00e9 = ok".encode()])

    def test_unsafe_empty_and_duplicate_names_sanitized_and_nothing_written_outside_objects(self):
        parts = "".join(f"--B\r\nContent-Type: application/octet-stream\r\nContent-Disposition: attachment; "
                        f"filename=\"{name}\"\r\n\r\npart {n}\r\n"
                        for n, name in enumerate(["../../x.pdf", "C:\\\\tmp\\\\y.csv", "", "..", "scan.pdf",
                                                  "scan.pdf", "tab\tname.txt"]))
        path = self.write(raw('Content-Type: multipart/mixed; boundary="B"', parts + "--B--\r\n"))
        before = self.files()
        report = self.ingest(path)
        self.assertEqual(self.names(report), ["x.pdf", "y.csv", "attachment-3", "attachment-4", "scan.pdf",
                                              "scan-2.pdf", "tabname.txt"])
        new = [k for k in self.files() if k not in before]
        self.assertEqual(sorted(new), sorted(["campaign/private/ledger.sqlite"] + [
            "campaign/private/objects/" + h for h in {report["message"]["sha256"]}
            | {item["sha256"] for item in report["attachments"]}]))
        self.assertEqual(sorted(r[0] for r in self.rows("SELECT member_path FROM contains")), sorted(self.names(report)))

    def test_inline_image_without_filename_is_not_stored_or_listed(self):
        msg = message(html=True)
        msg.get_payload()[1].add_related(b"\x89PNG synthetic", maintype="image", subtype="png", cid="<logo@example.org>")
        report = self.ingest(self.write(msg))
        self.assertEqual((report["attachments"], report["skipped"], report["message"]["outcome"]), ([], [], "stored"))
        self.assertEqual(len(os.listdir(self.root / "private" / "objects")), 1)
        self.assertEqual(self.rows("SELECT count(*) FROM contains"), [(0,)])

    def test_forwarded_message_is_one_member_and_its_attachments_are_not_extracted(self):
        inner = message((b"inner attachment bytes", "application", "pdf", "inner.pdf"), html=False)
        outer = message((PDF, "application", "pdf", "a.pdf"), html=False)
        outer.add_attachment(inner)
        report = self.ingest(self.write(outer))
        self.assertEqual(self.names(report), ["a.pdf", "attachment-2.eml"])
        hashes = set(os.listdir(self.root / "private" / "objects"))
        self.assertNotIn(hashlib.sha256(b"inner attachment bytes").hexdigest(), hashes)
        forwarded = (self.root / "private" / "objects" / report["attachments"][1]["sha256"]).read_bytes()
        self.assertIn(b'filename="inner.pdf"', forwarded)
        self.assertEqual(len(hashes), 3)

    def test_malformed_mime_and_over_200_parts_refused_with_nothing_written(self):
        cases = {"NoBoundaryInMultipartDefect": raw("Content-Type: multipart/mixed", "--B\r\nbody\r\n--B--\r\n"),
                 "StartBoundaryNotFoundDefect": raw('Content-Type: multipart/mixed; boundary="B"', "no parts here\r\n")}
        for label, data in cases.items():
            with self.subTest(label):
                path = self.write(data)
                before = self.files()
                result = self.ingest(path, returncode=1)
                self.assertIn(f"Stopped: malformed MIME ({label}); nothing was imported", result.stderr)
                self.assertEqual(self.files(), before)
        many = message(*[(b"p%d" % n, "application", "octet-stream", f"p{n}.bin") for n in range(197)])
        self.assertEqual(len(list(many.walk())), 201)
        path = self.write(many, "many.eml")
        before = self.files()
        result = self.ingest(path, returncode=1)
        self.assertIn("Stopped: many.eml: more than 200 MIME parts; nothing was imported", result.stderr)
        self.assertEqual(self.files(), before)
        fits = message(*[(b"p%d" % n, "application", "octet-stream", f"p{n}.bin") for n in range(196)])
        self.assertEqual(self.ingest(self.write(fits, "fits.eml"), dry=True)["parts"], 200)

    def test_oversized_attachment_skipped_too_large_empty_skipped_others_stored(self):
        from campaign_tool import ingest_eml
        path = self.write(message((b"x" * 64, "application", "pdf", "big.pdf"), (b"", "text", "csv", "e.csv"),
                                  (CSV, "text", "csv", "b.csv")))
        out = io.StringIO()
        args = types.SimpleNamespace(eml=str(path), source_id="synthetic-1", dry_run=False)
        with mock.patch.object(ingest_eml, "MAX_PART_BYTES", 63), contextlib.redirect_stdout(out):
            ingest_eml.ingest_eml(args, self.root)
        report = json.loads(out.getvalue())
        self.assertEqual(report["skipped"], [{"name": "big.pdf", "code": "too_large"}, {"name": "e.csv", "code": "empty"}])
        self.assertEqual(self.names(report), ["b.csv"])
        self.assertEqual(self.rows("SELECT count(*) FROM objects"), [(2,)])

    def test_dry_run_writes_nothing_and_reingest_is_all_present(self):
        path = self.write(message((PDF, "application", "pdf", "a.pdf"), (CSV, "text", "csv", "b.csv")))
        before = self.files()
        preview = self.ingest(path, dry=True)
        self.assertEqual(self.files(), before)
        self.assertEqual([preview["message"]["outcome"]] + [a["outcome"] for a in preview["attachments"]],
                         ["would_store"] * 3)
        self.assertTrue(preview["dry_run"])
        stored = self.ingest(path)
        counts = "SELECT (SELECT count(*) FROM objects), (SELECT count(*) FROM receipts), (SELECT count(*) FROM contains)"
        self.assertEqual(self.rows(counts), [(3, 3, 2)])
        after = self.files()
        again = self.ingest(path, dry=True)
        self.assertEqual(self.files(), after)
        self.assertEqual([again["message"]["outcome"]] + [a["outcome"] for a in again["attachments"]], ["present"] * 3)
        real = self.ingest(path)
        self.assertEqual([real["message"]["outcome"]] + [a["outcome"] for a in real["attachments"]], ["present"] * 3)
        self.assertEqual([a["sha256"] for a in real["attachments"]], [a["sha256"] for a in stored["attachments"]])
        self.assertEqual(self.rows(counts), [(3, 3, 2)])

    def test_symlink_message_refused(self):
        target = self.write(message())
        link = self.inbox / "link.eml"
        link.symlink_to(target)
        result = self.ingest(link, returncode=1)
        self.assertIn("Stopped: --eml must be a regular, non-symlink file", result.stderr)
        self.assertFalse((self.root / "private" / "ledger.sqlite").exists())


if __name__ == "__main__":
    unittest.main()
