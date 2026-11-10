"""DF2037-07: `ingest --eml MESSAGE --source-id ID` stores the message as is and each attachment by hash.

Each attachment (decoded payload of a part with a filename) gets a receipt `<message>/<sanitized name>` and a
ledger `contains` row; body text and inline parts without a filename stay inside the stored message. Every
message is synthetic (example.org). Run from the repository root:
python3 -B -m unittest tests.test_ingest_eml -v
"""

import contextlib
from email import policy
from email.generator import BytesGenerator
from email.message import EmailMessage
from email.parser import BytesParser
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
REFUSAL = "malformed MIME (InvalidAttachmentEncoding); nothing was imported"
FWD_INNER = "Subject: Synthetic inner 7Q\r\nContent-Type: text/plain\r\n\r\ninner body\r\n"


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


def mixed(*parts):
    """A multipart message whose attachments p1.bin, p2.bin, ... are (Content-Transfer-Encoding value, payload text)
    pairs; the value may be None (no header), one string or a list (one header line each)."""
    body = "--B\r\nContent-Type: text/plain\r\n\r\nbody\r\n"
    for number, (header, payload) in enumerate(parts, 1):
        lines = [] if header is None else [header] if isinstance(header, str) else header
        body += ("--B\r\nContent-Type: application/octet-stream\r\n"
                 + "".join(f"Content-Transfer-Encoding: {line}\r\n" for line in lines)
                 + f'Content-Disposition: attachment; filename="p{number}.bin"\r\n\r\n{payload}\r\n')
    return raw('Content-Type: multipart/mixed; boundary="B"', body + "--B--\r\n")


DEEP_REFUSAL = "malformed MIME (RecursionError); nothing was imported"


def deep(depth):
    """A complete message of `depth` nested multiparts around one text leaf (depth + 1 parts); each level has its
    own boundary `D<n>`, opened and closed."""
    part = "Content-Type: text/plain\r\n\r\nleaf\r\n"
    for n in range(depth):
        part = f'Content-Type: multipart/mixed; boundary="D{n}"\r\n\r\n--D{n}\r\n{part}\r\n--D{n}--\r\n'
    head = f"Subject: {HEADER_VALUES[0]}\r\nFrom: {HEADER_VALUES[1]}\r\nTo: {HEADER_VALUES[2]}\r\nMIME-Version: 1.0\r\n"
    return (head + part).encode()


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

    def test_long_duplicate_names_stay_unique_and_within_120_characters(self):
        long_name = "s" * 150 + ".pdf"
        parts = "".join(f"--B\r\nContent-Type: application/octet-stream\r\nContent-Disposition: attachment; "
                        f"filename=\"{long_name}\"\r\n\r\npart {n}\r\n" for n in range(11))
        report = self.ingest(self.write(raw('Content-Type: multipart/mixed; boundary="B"', parts + "--B--\r\n")))
        names = self.names(report)
        self.assertEqual(len(set(names)), 11)
        self.assertTrue(all(len(name) <= 120 and name.endswith(".pdf") for name in names), names)
        self.assertEqual((names[0], names[1], names[10]),
                         ("s" * 116 + ".pdf", "s" * 114 + "-2.pdf", "s" * 113 + "-11.pdf"))
        self.assertEqual(sorted(r[0] for r in self.rows("SELECT member_path FROM contains")), sorted(names))

    def test_single_part_message_with_filename_is_its_own_attachment(self):
        head = 'Content-Type: application/pdf\r\nContent-Transfer-Encoding: base64\r\n' \
               'Content-Disposition: attachment; filename="single.pdf"'
        path = self.write(raw(head, "AP8BAgM=\r\n"))
        report = self.ingest(path)
        whole, sa = hashlib.sha256(path.read_bytes()).hexdigest(), hashlib.sha256(b"\x00\xff\x01\x02\x03").hexdigest()
        self.assertEqual(report["attachments"], [{"name": "single.pdf", "sha256": sa, "outcome": "stored"}])
        self.assertEqual((self.root / "private" / "objects" / sa).read_bytes(), b"\x00\xff\x01\x02\x03")
        self.assertEqual(self.rows("SELECT * FROM contains"), [(whole, sa, "single.pdf")])
        self.assertEqual(sorted(r[0] for r in self.rows("SELECT original_name FROM receipts")),
                         ["m.eml", "m.eml/single.pdf"])

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

    # RF0200-1: strict attachment transfer decoding (docs/INGEST-MAIL.md)
    def snapshot(self):
        return {str(p.relative_to(self.base)): (p.stat().st_mode, p.stat().st_mtime_ns, p.read_bytes())
                for p in self.base.rglob("*") if p.is_file()}

    def refused(self, data, label="", text=REFUSAL):
        """Real and dry-run both exit 1 with the one fixed message and change no byte, mode or mtime (so a fresh
        campaign gets no ledger), fresh or populated."""
        path = self.write(data, "bad.eml")
        for populated in (False, True):
            if populated:
                self.ingest(self.write(message((CSV, "text", "csv", "good.csv")), "good.eml"), source_id="synthetic-0")
            before = self.snapshot()
            for dry in (False, True):
                result = self.ingest(path, dry=dry, returncode=1)
                self.assertEqual(result.stderr.strip(), f"Stopped: {text}", label)
                self.assertEqual(result.stdout, "", label)
                self.assertEqual(self.snapshot(), before, label)

    def test_strict_base64_refuses_repairs_before_any_write(self):
        cases = {"invalid-character": "AP8B*gM=", "missing-padding": "AP8BAgM", "excess-padding": "AP8BAgM==",
                 "interior-padding": "AP8B=AgM=", "invalid-length": "A", "noncanonical-pad-bits": "AP8BAgN=",
                 "non-ascii": "AP8B\u00e9AgM=", "vertical-tab": "AP8B\x0bAgM=", "form-feed": "AP8B\x0cAgM="}
        for label, payload in cases.items():
            with self.subTest(label):
                self.refused(mixed(("base64", payload)), label)
        with self.subTest("valid first then invalid second"):
            self.refused(mixed(("base64", "AP8BAgM="), ("base64", "AP8BAgM")), "second")

    def test_strict_base64_accepts_mime_whitespace_and_exact_bytes(self):
        folded = "AP8B\r\n AgM=" + "\t"
        data = mixed(("base64", folded), ("base64", "AP8B"), ("base64", "AP8BAgM="), ("base64", ""))
        path = self.write(data)
        report = self.ingest(path)
        self.assertEqual(report["skipped"], [{"name": "p4.bin", "code": "empty"}])
        want = [b"\x00\xff\x01\x02\x03", b"\x00\xff\x01", b"\x00\xff\x01\x02\x03"]
        self.assertEqual([item["sha256"] for item in report["attachments"]], [hashlib.sha256(b).hexdigest() for b in want])
        for item, blob in zip(report["attachments"], want):
            self.assertEqual((self.root / "private" / "objects" / item["sha256"]).read_bytes(), blob)
        counts = "SELECT (SELECT count(*) FROM objects), (SELECT count(*) FROM contains)"
        before = self.rows(counts)
        again = self.ingest(path)
        self.assertEqual([a["outcome"] for a in again["attachments"]], ["present"] * 3)
        self.assertEqual(self.rows(counts), before)

    def test_quoted_printable_validates_escapes_not_text(self):
        good = mixed(("quoted-printable", "caf=c3=A9 =3D=3d ok"), ("quoted-printable", "one=\r\ntwo=\nthree"),
                     ("quoted-printable", "line\r\n\u00e9 literal"))
        report = self.ingest(self.write(good))
        stored = [(self.root / "private" / "objects" / a["sha256"]).read_bytes() for a in report["attachments"]]
        self.assertEqual(stored, ["caf\u00e9 == ok".encode(), b"onetwothree", "line\r\n\u00e9 literal".encode()])
        bad = {"dangling": "abc=", "bad-first-digit": "caf=Z0", "bad-second-digit": "caf=0Z", "cr-without-lf": "a=\rb",
               "incomplete-hex": "x=C", "lone-equals-mid": "a= b"}
        for label, payload in bad.items():
            with self.subTest(label):
                self.refused(mixed(("quoted-printable", "fine"), ("quoted-printable", payload)), label)

    def test_attachment_encoding_header_is_unambiguous(self):
        for header in (None, "7BIT", "8bit", "Binary", "Base64", "base64 ", "base64\t",
                       " QUOTED-Printable ", "quoted-printable ", "quoted-printable\t"):
            with self.subTest(header):
                encoding = header.strip().lower() if header is not None else "7bit"
                payload, expected = {"base64": ("AP8BAgM=", b"\x00\xff\x01\x02\x03"),
                                     "quoted-printable": ("a=3Db", b"a=b")}.get(
                                         encoding, ("plain \u00e9 \x01", "plain \u00e9 \x01".encode()))
                path = self.write(mixed((header, payload)), "ok.eml")
                preview = self.ingest(path, dry=True)
                self.assertEqual(len(preview["attachments"]), 1)
                self.assertEqual(preview["attachments"][0]["sha256"], hashlib.sha256(expected).hexdigest())
                report = self.ingest(path)
                self.assertEqual(report["attachments"][0]["sha256"], preview["attachments"][0]["sha256"])
                blob = (self.root / "private" / "objects" / report["attachments"][0]["sha256"]).read_bytes()
                self.assertEqual(blob, expected)
        report = self.ingest(self.write(mixed(("8bit", "\u00e9\xff")), "id.eml"))
        blob = (self.root / "private" / "objects" / report["attachments"][0]["sha256"]).read_bytes()
        self.assertEqual(blob, "\u00e9\xff".encode())
        for label, header in {"unknown": "x-uuencode", "empty": "", "malformed": "base64 (comment)",
                              "duplicate-equal": ["base64", "base64"],
                              "duplicate-conflicting": ["base64", "7bit"]}.items():
            with self.subTest(label):
                self.refused(mixed((header, "AP8BAgM=")), label)

    def test_encoding_validation_scope_preserves_opaque_parts(self):
        body = ("--B\r\nContent-Type: text/plain\r\nContent-Transfer-Encoding: base64\r\n\r\nnot*base64\r\n"
                "--B\r\nContent-Type: application/octet-stream\r\nContent-Transfer-Encoding: quoted-printable\r\n"
                "\r\nbad=\r\n--B--\r\n")
        report = self.ingest(self.write(raw('Content-Type: multipart/mixed; boundary="B"', body)), dry=True)
        self.assertEqual((report["attachments"], report["skipped"]), ([], []))
        inner = raw('Content-Type: multipart/mixed; boundary="I"', "--I\r\nContent-Type: text/plain\r\n\r\nfwd\r\n--I\r\n"
                    "Content-Type: application/pdf\r\nContent-Transfer-Encoding: base64\r\n"
                    "Content-Disposition: attachment; filename=\"inner.pdf\"\r\n\r\nAP8BAgM\r\n--I--\r\n").decode()
        outer = raw('Content-Type: multipart/mixed; boundary="B"',
                    "--B\r\nContent-Type: message/rfc822\r\nContent-Disposition: attachment; filename=\"fwd.eml\"\r\n\r\n"
                    + inner + "\r\n--B--\r\n")
        report = self.ingest(self.write(outer, "fwd.eml"), dry=True)
        self.assertEqual(self.names(report), ["fwd.eml"])
        single = 'Content-Type: application/octet-stream\r\nContent-Transfer-Encoding: base64\r\n' \
                 'Content-Disposition: attachment; filename="r.bin"'
        self.assertEqual(self.names(self.ingest(self.write(raw(single, "AP8BAgM=\r\n")), dry=True)), ["r.bin"])
        self.refused(raw(single, "AP8BAgM\r\n"), "single-part root")

    def test_invalid_attachment_diagnostic_has_no_private_values(self):
        sentinels = ("sentinel-header-8k", "SENTINELBODY8K", "sentinel-name-8k")
        data = raw('Content-Type: multipart/mixed; boundary="B"',
                   "--B\r\nContent-Type: application/octet-stream\r\nContent-Transfer-Encoding: base64\r\n"
                   f"X-Private: {sentinels[0]}\r\nContent-Disposition: attachment; filename=\"{sentinels[2]}.bin\"\r\n\r\n"
                   f"{sentinels[1]}*\r\n--B--\r\n")
        path = self.write(data, "sentinel.eml")
        digest = hashlib.sha256(data).hexdigest()
        result = self.ingest(path, returncode=1)
        self.assertEqual(result.stderr.strip(), f"Stopped: {REFUSAL}")
        for value in sentinels:
            self.assertNotIn(value, result.stdout + result.stderr)
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)

    # RF0824-2: the forwarded message/rfc822 wrapper has its own identity-only transfer encoding rule
    def forward_eml(self, header=None, named=True, nested=False, first=None, inner=FWD_INNER):
        """Message bytes with one forwarded wrapper; `header` is None, one CTE value or a list of values (one header
        line each), `first` an optional valid ordinary attachment before it, `nested` wraps it in a multipart."""
        lines = [] if header is None else [header] if isinstance(header, str) else header
        part = ("Content-Type: message/rfc822\r\n" + "".join(f"Content-Transfer-Encoding: {v}\r\n" for v in lines)
                + ('Content-Disposition: attachment; filename="fwd.eml"\r\n' if named else "")
                + "\r\n" + inner + "\r\n")
        if nested:
            part = 'Content-Type: multipart/mixed; boundary="N"\r\n\r\n--N\r\n' + part + "--N--\r\n"
        body = "--B\r\nContent-Type: text/plain\r\n\r\nbody\r\n"
        if first:
            body += ("--B\r\nContent-Type: application/octet-stream\r\nContent-Transfer-Encoding: base64\r\n"
                     'Content-Disposition: attachment; filename="first.bin"\r\n\r\nAP8BAgM=\r\n')
        return raw('Content-Type: multipart/mixed; boundary="B"', body + "--B\r\n" + part + "--B--\r\n")

    def objects(self):
        return set(os.listdir(self.root / "private" / "objects"))

    def test_forwarded_wrapper_cte_identity_and_legacy_bytes(self):
        oracle = io.BytesIO()
        BytesGenerator(oracle, mangle_from_=False, policy=policy.default.clone(refold_source="none")).flatten(
            BytesParser(policy=policy.default).parsebytes(FWD_INNER.encode()))
        want = hashlib.sha256(oracle.getvalue()).hexdigest()
        self.assertEqual(oracle.getvalue(), b"Subject: Synthetic inner 7Q\nContent-Type: text/plain\n\ninner body\n")
        counts = "SELECT (SELECT count(*) FROM objects), (SELECT count(*) FROM receipts), (SELECT count(*) FROM contains)"
        for header in (None, "7bit", "8bit", "binary", " 7BIT ", "8Bit\t", "Binary"):
            for named, nested, first in ((True, False, False), (False, False, False), (True, True, True),
                                         (False, True, True)):
                label = (header, named, nested, first)
                with self.subTest(label):
                    path = self.write(self.forward_eml(header, named, nested, first), "fwd.eml")
                    preview = self.ingest(path, dry=True)
                    report = self.ingest(path)
                    forwarded = report["attachments"][-1]
                    self.assertEqual(self.names(report)[-1].endswith(".eml"), True)
                    self.assertEqual((len(report["attachments"]), report["skipped"]), (2 if first else 1, []))
                    self.assertEqual(forwarded["sha256"], want)
                    self.assertEqual(preview["attachments"][-1]["sha256"], want)
                    self.assertEqual([a["sha256"] for a in preview["attachments"]],
                                     [a["sha256"] for a in report["attachments"]])
                    self.assertEqual((self.root / "private" / "objects" / want).read_bytes(), oracle.getvalue())
                    stored = (self.rows(counts), self.objects())
                    again = self.ingest(path)
                    self.assertEqual({a["outcome"] for a in again["attachments"]}, {"present"})
                    self.assertEqual((self.rows(counts), self.objects()), stored)

    def test_forwarded_wrapper_cte_refused_before_any_write(self):
        cases = {"unknown": "x-uuencode", "empty": "", "malformed-comment": "7bit (comment)", "malformed-token": "7 bit",
                 "duplicate-equal": ["7bit", "7bit"], "duplicate-conflicting": ["7bit", "base64"],
                 "base64": "base64", "base64-mixed-case": " Base64 ", "quoted-printable": "quoted-printable"}
        for label, header in cases.items():
            for named, nested, first in ((True, False, False), (False, False, True), (True, True, True)):
                with self.subTest((label, named, nested, first)):
                    self.refused(self.forward_eml(header, named, nested, first), label)
        for label, inner in {"malformed-base64": "AP8B*gM=", "malformed-qp": "caf=Z0 abc="}.items():
            with self.subTest(label):  # content is never read: the header alone decides
                self.refused(self.forward_eml("base64" if "base64" in label else "quoted-printable", inner=inner), label)

    def test_forwarded_inner_encodings_remain_opaque(self):
        inner = ('Content-Type: multipart/mixed; boundary="I"\r\n\r\n--I\r\nContent-Type: text/plain\r\n\r\nfwd\r\n--I\r\n'
                 'Content-Type: application/pdf\r\nContent-Transfer-Encoding: base64\r\n'
                 'Content-Disposition: attachment; filename="inner1.pdf"\r\n\r\nAP8BAgM\r\n--I\r\n'
                 'Content-Type: application/pdf\r\nContent-Transfer-Encoding: base64\r\nContent-Transfer-Encoding: 7bit\r\n'
                 'Content-Disposition: attachment; filename="inner2.pdf"\r\n\r\nx\r\n--I--\r\n')
        data = self.forward_eml("7bit", first=True, inner=inner)
        report = self.ingest(self.write(data, "opaque.eml"))
        self.assertEqual(self.names(report), ["first.bin", "fwd.eml"])
        self.assertEqual(len(self.objects()), 3)  # message, first attachment, one forwarded member
        forwarded = (self.root / "private" / "objects" / report["attachments"][1]["sha256"]).read_bytes()
        self.assertIn(b'filename="inner1.pdf"', forwarded)
        self.assertEqual(self.rows("SELECT count(*) FROM contains"), [(2,)])
        # an invalid wrapper around the same valid-looking inner message still refuses the whole message
        self.refused(self.forward_eml("x-uuencode", first=True, inner=inner), "invalid wrapper")
        self.refused(self.forward_eml(["7bit", "7bit"], inner=FWD_INNER), "duplicate wrapper")
        # ordinary body and unnamed inline parts keep their exemption beside a valid forward
        exempt = self.forward_eml("8bit").replace(
            b"--B\r\nContent-Type: text/plain\r\n\r\nbody\r\n",
            b"--B\r\nContent-Type: text/plain\r\nContent-Transfer-Encoding: base64\r\n\r\nnot*base64\r\n"
            b"--B\r\nContent-Type: image/png\r\nContent-Transfer-Encoding: quoted-printable\r\n\r\nbad=\r\n", 1)
        self.assertEqual(self.names(self.ingest(self.write(exempt, "exempt.eml"), dry=True)), ["fwd.eml"])

    def test_forwarded_wrapper_diagnostic_has_no_private_values(self):
        sentinels = ("sentinel-header-8k", "SENTINELBODY8K", "sentinel-name-8k")
        data = raw('Content-Type: multipart/mixed; boundary="B"',
                   "--B\r\nContent-Type: message/rfc822\r\nContent-Transfer-Encoding: x-" + sentinels[0] + "\r\n"
                   f'Content-Disposition: attachment; filename="{sentinels[2]}.eml"\r\n\r\n'
                   f"Subject: {sentinels[1]}\r\n\r\n{sentinels[1]}\r\n--B--\r\n")
        path = self.write(data, "sentinel.eml")
        digest = hashlib.sha256(data).hexdigest()
        result = self.ingest(path, returncode=1)
        self.assertEqual(result.stderr.strip(), f"Stopped: {REFUSAL}")
        for value in sentinels:
            self.assertNotIn(value, result.stdout + result.stderr)
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)

    # RF1355-1: a MIME tree nested deeper than the stdlib parser can follow is one malformed message
    def test_deep_multipart_eml_refused_before_any_write(self):
        data = deep(1200)
        self.assertLess(len(data), 100_000)  # a small, complete tree: every level opens and closes its own boundary
        self.assertEqual((data.count(b'boundary="D'), data.count(b"--\r\n")), (1200, 1200))
        self.refused(data, "deep", text=DEEP_REFUSAL)

    def test_recursion_normalized_during_parse_walk_and_forward_serialization(self):
        from email.message import Message
        from campaign_tool import ingest_eml
        sentinel = RecursionError("sentinel-recursion-3x")
        forward = message(html=False)
        forward.add_attachment(message((PDF, "application", "pdf", "inner.pdf"), html=False))
        sites = {"parse": (mock.patch.object(ingest_eml.BytesParser, "parsebytes", side_effect=sentinel), message()),
                 "walk": (mock.patch.object(Message, "walk", side_effect=sentinel), message()),
                 "attachments": (mock.patch.object(ingest_eml, "_attachments", side_effect=sentinel), message()),
                 "forward": (mock.patch.object(ingest_eml.BytesGenerator, "flatten", side_effect=sentinel), forward)}
        for site, (patch, msg) in sites.items():
            data = msg.as_bytes()  # serialized before the patch: only _extract meets the injected error
            with self.subTest(site), patch, self.assertRaises(ValueError) as caught:
                ingest_eml._extract(data, "m.eml")
            self.assertEqual(str(caught.exception), DEEP_REFUSAL)
            self.assertTrue(caught.exception.__suppress_context__)
            self.assertIsNone(caught.exception.__cause__)
            self.assertNotIn("sentinel", repr(caught.exception.args))

    def test_nested_part_limit_boundary_preserved(self):
        from campaign_tool import ingest_eml
        self.assertEqual(ingest_eml._extract(deep(199), "fits.eml"), ([], 200))
        with self.assertRaises(ValueError) as caught:
            ingest_eml._extract(deep(200), "over.eml")
        self.assertEqual(str(caught.exception), "over.eml: more than 200 MIME parts; nothing was imported")
        shallow = message((PDF, "application", "pdf", "a.pdf"), (CSV, "text", "csv", "b.csv"))
        self.assertEqual(ingest_eml._extract(shallow.as_bytes(), "m.eml"), ([("a.pdf", PDF), ("b.csv", CSV)], 6))
        report = self.ingest(self.write(deep(199), "fits.eml"), dry=True)
        self.assertEqual((report["parts"], report["attachments"]), (200, []))
        self.assertEqual(report["message"]["outcome"], "would_store")

    # RF0825-5: the saved file's own basename must be strict UTF-8 before anything is extracted or preserved
    NAME_REFUSAL = "Message filename cannot be represented as UTF-8; nothing was imported"
    RAW_LEAVES = (b"canary-name-9r-\xff.eml", b"canary-name-9r-\xc3(.eml")

    def raw_file(self, leaf, data, parent=None):
        """A real file whose exact POSIX name is the given bytes (optionally below a raw-byte directory)."""
        directory = os.fsencode(self.inbox) + (b"/" + parent if parent else b"")
        os.makedirs(directory, exist_ok=True)
        fd = os.open(directory + b"/" + leaf, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        return os.fsdecode(directory + b"/" + leaf)

    def assert_name_refused(self, path, dry):
        result = self.ingest(path, dry=dry, returncode=1)
        self.assertEqual((result.stdout, result.stderr), ("", f"Stopped: {self.NAME_REFUSAL}\n"))
        for value in ("canary-name-9r", "codec", "utf-8", "surrogate", "Dry run"):
            self.assertNotIn(value, result.stdout + result.stderr)

    def test_raw_eml_basename_refuses_before_preview_and_store(self):
        from campaign_tool import ingest_eml
        data = message((PDF, "application", "pdf", "a.pdf")).as_bytes()
        paths = [self.raw_file(leaf, data) for leaf in self.RAW_LEAVES]
        for populated in (False, True):
            if populated:
                self.ingest(self.write(message((CSV, "text", "csv", "good.csv")), "good.eml"), source_id="synthetic-0")
            before = self.snapshot()
            for path in paths:
                for dry in (False, True):
                    with self.subTest((populated, path, dry)):
                        self.assert_name_refused(path, dry)
                        self.assertEqual(self.snapshot(), before)
            self.assertEqual((self.root / "private" / "ledger.sqlite").exists(), populated)
        # the fixed refusal comes from the single guard, before any extract/lookup/database/preview/store seam
        seams = {name: mock.patch.object(ingest_eml, name, side_effect=AssertionError(name))
                 for name in ("_extract", "_ledger_lookup", "database", "_previewer", "_store_bytes")}
        for dry in (False, True):
            args = types.SimpleNamespace(eml=paths[0], source_id="synthetic-1", dry_run=dry)
            with self.subTest(("seams", dry)), contextlib.ExitStack() as stack:
                for patch in seams.values():
                    stack.enter_context(patch)
                with self.assertRaises(ValueError) as caught:
                    ingest_eml.ingest_eml(args, self.root)
            self.assertEqual(str(caught.exception), self.NAME_REFUSAL)
            self.assertTrue(caught.exception.__suppress_context__)
        # the apply defect: no message object may exist without a ledger row
        self.cli("status", "--check")

    def test_eml_filename_refusal_precedes_mime_processing(self):
        from campaign_tool import ingest_eml
        bad_encoding = mixed(("base64", "AP8B*gM="))
        no_boundary = raw('Content-Type: multipart/mixed', "--B\r\nContent-Type: text/plain\r\n\r\nbody\r\n")
        over_limit = deep(200)
        for number, (label, data, text) in enumerate((("encoding", bad_encoding, REFUSAL), ("boundary", no_boundary, None),
                                                      ("parts", over_limit, None))):
            for dry in (False, True):
                with self.subTest((label, dry)):
                    self.assert_name_refused(self.raw_file(self.RAW_LEAVES[0] + b"-%d-%d" % (number, dry), data), dry)
            # representable leaf: the existing MIME refusal is unchanged
            with self.subTest((label, "valid leaf")):
                result = self.ingest(self.write(data, f"{label}.eml"), returncode=1)
                self.assertTrue(result.stderr.startswith("Stopped: ") and "nothing was imported" in result.stderr)
                self.assertNotIn(self.NAME_REFUSAL, result.stderr)
                if text:
                    self.assertEqual(result.stderr.strip(), f"Stopped: {text}")
        # reader-first: a missing, symlinked, directory, FIFO or oversized input keeps its existing refusal
        leaf = self.RAW_LEAVES[0]
        missing = os.fsdecode(os.fsencode(self.inbox) + b"/" + leaf)
        link = os.fsdecode(os.fsencode(self.inbox) + b"/link-" + leaf)
        os.symlink(self.write(message(), "target.eml"), link)
        folder = os.fsdecode(os.fsencode(self.inbox) + b"/dir-" + leaf)
        os.mkdir(folder)
        fifo = os.fsdecode(os.fsencode(self.inbox) + b"/fifo-" + leaf)
        os.mkfifo(fifo)
        args = lambda path: types.SimpleNamespace(eml=path, source_id="synthetic-1", dry_run=True)
        for label, path in (("missing", missing), ("symlink", link), ("fifo", fifo)):
            with self.subTest(label), self.assertRaises(ValueError) as caught:
                ingest_eml.ingest_eml(args(path), self.root)
            self.assertEqual(str(caught.exception), "--eml must be a regular, non-symlink file")
        with self.assertRaises(OSError):  # a directory keeps the reader's own failure, never the filename refusal
            ingest_eml.ingest_eml(args(folder), self.root)
        big = self.raw_file(b"big-\xff.eml", b"x" * 100)
        with mock.patch.object(ingest_eml, "MAX_BYTES", 10), self.assertRaises(ValueError) as caught:
            ingest_eml.ingest_eml(args(big), self.root)
        self.assertEqual(str(caught.exception), "Alpha limit: 50 MiB per message; nothing was imported")

    def test_eml_unicode_leaf_and_raw_ancestor_are_supported(self):
        base = message((PDF, "application", "pdf", "a.pdf"), (CSV, "text", "csv", "a.pdf")).as_bytes()
        leaves = ["café-😀.eml", "cafe\u0301.eml", "記録.eml"]
        stored, number = {}, 0
        for leaf in leaves:
            for parent in (None, b"anc-\xff/inner-\xc3("):
                number += 1
                data = base + f"\nsynthetic case {number}\n".encode()  # a distinct message per case
                with self.subTest((leaf, parent)):
                    path = self.raw_file(leaf.encode(), data, parent) if parent else self.write(data, leaf + "-plain")
                    expected = leaf if parent else leaf + "-plain"
                    preview = self.ingest(path, source_id=f"synthetic-{number}", dry=True)
                    self.assertEqual(preview["message"]["outcome"], "would_store")
                    report = self.ingest(path, source_id=f"synthetic-{number}")
                    self.assertEqual(report["message"]["sha256"], hashlib.sha256(data).hexdigest())
                    self.assertEqual(self.names(report), ["a.pdf", "a-2.pdf"])
                    self.assertEqual(self.ingest(path, source_id=f"synthetic-{number}")["message"]["outcome"], "present")
                    self.assertEqual((self.root / "private" / "objects" / report["message"]["sha256"]).read_bytes(), data)
                    stored[expected] = report["message"]["sha256"]
                    members = self.rows(f"SELECT member_path FROM contains WHERE container_sha256 = '{stored[expected]}'")
                    self.assertEqual(sorted(r[0] for r in members), ["a-2.pdf", "a.pdf"])
        names = {r[0] for r in self.rows("SELECT original_name FROM receipts")}
        for expected in stored:  # the receipt is the exact leaf: no ancestor text, no normalization
            self.assertEqual({n for n in names if n.split("/")[0] == expected},
                             {expected, f"{expected}/a.pdf", f"{expected}/a-2.pdf"})
        self.assertEqual(len(names), 3 * len(stored))
        self.assertTrue(all("anc-" not in n and "inner-" not in n for n in names), names)
        self.cli("status", "--check")

    def test_bad_eml_retry_preserves_existing_same_digest_object_and_rows(self):
        data = message((PDF, "application", "pdf", "a.pdf")).as_bytes()
        digest = hashlib.sha256(data).hexdigest()
        self.ingest(self.write(data, "valid.eml"))
        bad = self.raw_file(b"raw-\xff.eml", data)
        before = self.snapshot()
        for dry in (False, True):
            self.assert_name_refused(bad, dry)
            self.assertEqual(self.snapshot(), before)
        # a deliberate organizer rename uses ordinary intake: the existing object is simply present
        renamed = os.fsdecode(os.fsencode(self.inbox) + b"/renamed.eml")
        os.rename(bad, renamed)
        self.assertEqual(self.ingest(renamed, source_id="synthetic-2")["message"]["outcome"], "present")
        # a historical orphan (object without a ledger row) is neither adopted nor removed
        orphan = b"synthetic orphan message\r\n"
        orphan_digest = hashlib.sha256(orphan).hexdigest()
        object_path = self.root / "private" / "objects" / orphan_digest
        object_path.write_bytes(orphan)
        bad2 = self.raw_file(b"orphan-\xff.eml", orphan)
        before = self.snapshot()
        for dry in (False, True):
            self.assert_name_refused(bad2, dry)
            self.assertEqual(self.snapshot(), before)
        self.assertEqual(object_path.read_bytes(), orphan)
        self.assertEqual(self.rows(f"SELECT count(*) FROM objects WHERE sha256 = '{orphan_digest}'"), [(0,)])
        self.assertEqual(self.rows(f"SELECT count(*) FROM objects WHERE sha256 = '{digest}'"), [(1,)])

    def test_eml_basename_error_is_fixed_and_value_free(self):
        from campaign_tool import ingest_eml
        for name in ("canary-9r-\udcff.eml", "x\udc80", "\ud800"):
            with self.subTest(name), self.assertRaises(ValueError) as caught:
                ingest_eml._require_receipt_name(name)
            self.assertEqual(str(caught.exception), self.NAME_REFUSAL)
            self.assertIsNone(caught.exception.__cause__)
            self.assertTrue(caught.exception.__suppress_context__)
        for name in ("m.eml", "café-😀.eml", "cafe\u0301.eml", "記録.eml"):
            ingest_eml._require_receipt_name(name)
        # unrelated failures are not masked as a filename problem
        with self.assertRaises(AttributeError):
            ingest_eml._require_receipt_name(None)
        distinct = self.raw_file(b"basename-canary-9r-\xff.eml", message(html=False).as_bytes(),
                                 b"ancestor-canary-9r")
        for dry in (False, True):
            result = self.ingest(distinct, dry=dry, returncode=1)
            for value in ("basename-canary", "ancestor-canary", "Synthetic body"):
                self.assertNotIn(value, result.stdout + result.stderr)

    def test_representable_eml_retains_existing_reports_and_mail_contracts(self):
        data = self.forward_eml("8bit", first=True)
        path = self.write(data, "fwd-ok.eml")
        preview = self.ingest(path, dry=True)
        report = self.ingest(path)
        self.assertEqual(self.names(report), ["first.bin", "fwd.eml"])
        self.assertEqual(preview["attachments"], [{**item, "outcome": "would_store"} for item in report["attachments"]])
        self.assertEqual((self.root / "private" / "objects" / report["message"]["sha256"]).read_bytes(), data)
        self.assertEqual(self.ingest(path)["message"]["outcome"], "present")
        # the documentation names the refusal and keeps the existing encoding and deep-tree wording
        doc = (REPO_ROOT / "docs" / "INGEST-MAIL.md").read_text(encoding="utf-8")
        self.assertIn(f"Stopped: {self.NAME_REFUSAL}", doc)
        self.assertIn("rename the file to a\nvalid UTF-8 name", doc)
        self.assertIn("Stopped: malformed MIME (InvalidAttachmentEncoding); nothing was imported", doc)
        self.assertIn("Stopped: malformed MIME (RecursionError); nothing was imported", doc)


if __name__ == "__main__":
    unittest.main()
