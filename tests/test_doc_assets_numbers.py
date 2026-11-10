"""RF0645-2: an unconvertible direct integer in a PDF's /DecodeParms or /DP makes the PDF unreadable, not a crash.

The interpreter's integer-digit safeguard is set only in child interpreters (a positive 640 limit through
PYTHONINTMAXSTRDIGITS), never in this runner or in production. Every PDF is built here from bytes.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

from campaign_tool import doc_assets
from campaign_tool.doc_assets import PDF_UNREADABLE, pdf_kinds

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
LIMIT = 640
LONG = b"9" * 700  # over the child's 640-digit limit; the parent's own limit is not touched
JS_TEXT, AUTHOR = b"app.alert(41)", b"Jane Organizer"
CATALOG, PAGES = b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>"
PAGE = b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >>"
DEFAULTS = [1, 1, 8, 1]

# the child harness: reads a JSON list of operations, runs the real functions, prints one JSON document
HARNESS = '''\
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from campaign_tool import doc_assets
from campaign_tool.content import ContentError

out = {"limit": sys.get_int_max_str_digits(), "results": []}
for op in json.loads(Path(sys.argv[2]).read_text()):
    data = bytes.fromhex(op.get("hex", ""))
    if op["op"] == "kinds":
        out["results"].append(doc_assets.pdf_kinds(data))
    elif op["op"] == "params":
        try:
            out["results"].append(["ok", [list(p) for p in doc_assets._params_value(data, 0)[0]]])
        except doc_assets.Unreadable as error:
            out["results"].append(["unreadable", error.__cause__ is None and error.__suppress_context__])
    else:
        try:
            doc_assets.check_asset(Path(op["path"]), op["relative"])
            out["results"].append("clean")
        except ContentError as error:
            out["results"].append(str(error))
print(json.dumps(out))
'''


def pdf(*extra, catalog=CATALOG):
    objects = [catalog, PAGES, PAGE, *extra]
    out, offsets = bytearray(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n"), []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (number, body)
    xref, size = len(out), len(objects) + 1
    out += b"xref\n0 %d\n0000000000 65535 f \n" % size + b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (size, xref)
    return bytes(out)


def flate(payload, dictionary=b"/Type /ObjStm /N 1 /First 4"):
    packed = zlib.compress(payload)
    return b"<< %s /Filter /FlateDecode /Length %d >>\nstream\n%s\nendstream" % (dictionary, len(packed), packed)


def with_parms(parms, keyword=b"DecodeParms", payload=b"BT (x) Tj ET"):
    """A one-stream PDF whose Flate stream carries ``/<keyword> <parms>``."""
    return pdf(flate(payload, b"/%s %s" % (keyword, parms)))


def png_rows(payload, width):
    """``payload`` PNG-predicted with row type 0 on rows of ``width`` bytes."""
    return b"".join(b"\x00" + payload[i:i + width].ljust(width, b"\x00") for i in range(0, len(payload), width))


class Child(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-doc-numbers-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.harness = self.tmp / "harness.py"
        self.harness.write_text(HARNESS, encoding="utf-8")

    def env(self, limit=LIMIT):
        return {**os.environ, "PYTHONINTMAXSTRDIGITS": str(limit), "PYTHONDONTWRITEBYTECODE": "1"}

    def child(self, operations, limit=LIMIT):
        """Run ``operations`` in a dedicated interpreter with the digit limit set; (limit it saw, results)."""
        spec = self.tmp / "operations.json"
        spec.write_text(json.dumps([{**op, **({"hex": op["hex"].hex()} if "hex" in op else {})} for op in operations]),
                        encoding="utf-8")
        result = subprocess.run([sys.executable, "-B", str(self.harness), str(REPO_ROOT), str(spec)], env=self.env(limit),
                                capture_output=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
        document = json.loads(result.stdout)
        return document["limit"], document["results"]

    def kinds(self, *files, limit=LIMIT):
        seen, results = self.child([{"op": "kinds", "hex": data} for data in files], limit)
        self.assertEqual(seen, limit)
        return results

    def params(self, *parms, limit=LIMIT):
        return self.child([{"op": "params", "hex": data} for data in parms], limit)[1]


class ScannerTests(Child):
    # direct integers of every parameter key, signed and zero-padded, make the scanner return unreadable
    def test_direct_decode_parameter_integer_refusal_returns_unreadable(self):
        parent_limit = sys.get_int_max_str_digits()
        files = []
        for key in (b"Predictor", b"Colors", b"BitsPerComponent", b"Columns", b"K"):
            for token in (LONG, b"+" + LONG, b"-" + LONG, b"0" * 700):
                files.append(with_parms(b"<< /%s %s >>" % (key, token)))
        self.assertEqual(self.kinds(*files), [[PDF_UNREADABLE]] * len(files))
        self.assertEqual(sys.get_int_max_str_digits(), parent_limit)  # the runner's own setting is untouched
        self.assertEqual(self.kinds(with_parms(b"<< /Predictor 1 /Columns 5 /K -1 >>")), [[]])  # a control

    # /DecodeParms, /DP, escaped names, arrays and unknown or image keys share the one boundary
    def test_parameter_spellings_arrays_and_escaped_names_share_boundary(self):
        spellings = [
            (b"DecodeParms", b"<< /Columns %s >>" % LONG), (b"DP", b"<< /Columns %s >>" % LONG),
            (b"Decode#50arms", b"<< /Columns %s >>" % LONG), (b"DecodeParms", b"<< /Col#75mns %s >>" % LONG),
            (b"DecodeParms", b"[null << /Predictor %s >>]" % LONG), (b"DP", b"[ << /Foo %s >> ]" % LONG),
            (b"DecodeParms", b"[<< /Colors 1 >> null << /K %s >>]" % LONG), (b"DecodeParms", b"<< /Unknown %s >>" % LONG)]
        files = [with_parms(parms, keyword) for keyword, parms in spellings]
        image = b"<< /Subtype /Image /Filter /CCITTFaxDecode /DecodeParms << /K %s >> /Length 3 >>\nstream\nabc\nendstream"
        files.append(pdf(image % LONG))
        self.assertEqual(self.kinds(*files), [[PDF_UNREADABLE]] * len(files))
        helper = self.params(*(parms for _, parms in spellings))
        self.assertEqual([row[0] for row in helper], ["unreadable"] * len(spellings))
        self.assertTrue(all(row[1] for row in helper), helper)  # raised without an exception chain
        self.assertEqual(self.kinds(pdf(image % b"-1")), [[]])

    # 639 and 640 digits convert, 641 do not; an unknown key's value is not returned; a disabled limit adds no cap
    def test_controlled_digit_limit_boundary_and_parent_state(self):
        before = sys.get_int_max_str_digits()
        rows = self.params(*(b"<< /Foo %s >>" % (b"7" * n) for n in (639, 640, 641)), b"<< /Colors +3 /Columns -4 >>",
                           b"<< /Foo +%s >>" % (b"7" * 640))
        self.assertEqual(rows, [["ok", [DEFAULTS]], ["ok", [DEFAULTS]], ["unreadable", True], ["ok", [[1, 3, 8, -4]]],
                                ["ok", [DEFAULTS]]])
        self.assertEqual(self.params(b"<< /Foo %s >>" % (b"7" * 1000), limit=0), [["ok", [DEFAULTS]]])
        self.assertEqual(self.kinds(with_parms(b"<< /Foo %s >>" % (b"7" * 1000)), limit=0), [[]])
        self.assertEqual(sys.get_int_max_str_digits(), before)

    # unreadable wins over kinds already found; ordinary rules unchanged; an unrelated error is not swallowed
    def test_unreadable_precedence_and_existing_parameter_rules(self):
        marked = pdf(flate(b"<< /JS (%s) /Author (%s) >>" % (JS_TEXT, AUTHOR)), flate(b"x", b"/DP << /Colors %s >>" % LONG),
                     catalog=b"<< /Type /Catalog /Pages 2 0 R /OpenAction << /S /JavaScript /JS (x) >> >>")
        self.assertEqual(self.kinds(marked), [[PDF_UNREADABLE]])
        hidden = b"5 0 << /S /JavaScript /JS (%s) /Author (%s) >>" % (JS_TEXT, AUTHOR)
        predicted = pdf(flate(png_rows(hidden, 1), b"/Type /ObjStm /N 1 /First 4 /DecodeParms << /Predictor 10 /Columns 1 >>"))
        self.assertEqual(self.kinds(predicted, pdf(flate(hidden))), [["JavaScript", "author metadata"]] * 2)
        for parms in (b"<< /Columns 1 /Columns 2 >>", b"<< /Columns 5 0 R >>", b"<< /Predictor 99 >>", b"<< /Colors 1.5 >>",
                      b"5 0 R", b"<< /Columns >>", b"[ 7 ]"):
            self.assertEqual(self.kinds(with_parms(parms)), [[PDF_UNREADABLE]], parms)
        with mock.patch.object(doc_assets, "PDF_PARAM_INT", mock.Mock(match=mock.Mock(side_effect=RuntimeError("boom")))):
            with self.assertRaises(RuntimeError):
                pdf_kinds(with_parms(b"<< /Columns 5 >>"))

    # repeated calls: an unreadable PDF leaves no state behind
    def test_clean_assets_and_repeated_calls_are_unchanged(self):
        hidden = pdf(flate(b"5 0 << /S /JavaScript >>"))
        bad = with_parms(b"<< /Columns %s >>" % LONG)
        self.assertEqual(self.kinds(pdf(), bad, hidden, bad, pdf()), [[], [PDF_UNREADABLE], ["JavaScript"],
                                                                       [PDF_UNREADABLE], []])


class Campaign(Child):
    def setUp(self):
        super().setUp()
        self.root = self.tmp / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.assets = self.root / "content" / "public-assets"
        (self.assets / "docs").mkdir(parents=True, exist_ok=True)
        self.numeric = self.assets / "numeric.pdf"
        self.numeric.write_bytes(with_parms(b"<< /Columns %s >>" % LONG))

    def cli(self, *args):
        return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", *args, "--directory", str(self.root)],
                              cwd=REPO_ROOT, env=self.env(), capture_output=True, timeout=120)

    def snapshot(self):
        return {p.relative_to(self.root).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_mode)
                for p in sorted(self.root.rglob("*")) if p.is_file()}

    def assertSafe(self, result):
        shown = result.stdout + result.stderr
        for forbidden in (LONG, b"9999", b"digits", b"set_int_max_str_digits", b"Traceback", b"Exceeds", str(self.tmp).encode()):
            self.assertNotIn(forbidden, shown)


class ConsumerTests(Campaign):
    def test_check_asset_normalizes_conversion_failure_without_payload(self):
        before = (self.numeric.read_bytes(), self.numeric.stat().st_mtime_ns, self.numeric.stat().st_mode)
        clean = self.assets / "clean.pdf"
        clean.write_bytes(pdf())
        results = self.child([{"op": "check", "path": str(self.numeric), "relative": "numeric.pdf"},
                              {"op": "check", "path": str(clean), "relative": "clean.pdf"}])[1]
        self.assertEqual(results, ["public-assets/numeric.pdf: PDF (unreadable PDF)", "clean"])
        self.assertEqual((self.numeric.read_bytes(), self.numeric.stat().st_mtime_ns, self.numeric.stat().st_mode), before)

    def test_build_cli_numeric_refusal_preserves_fresh_and_existing_output(self):
        message = b"Stopped: public-assets/numeric.pdf: PDF (unreadable PDF)"
        result = self.cli("build")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(message, result.stderr)
        self.assertSafe(result)
        self.assertFalse((self.root / "public").exists())
        self.assertEqual([p.name for p in self.root.iterdir() if p.name.startswith(".public")], [])
        clean = pdf(flate(b"BT (Agenda) Tj ET", b""))
        self.numeric.write_bytes(clean)
        self.assertEqual(self.cli("build").returncode, 0)
        self.assertEqual((self.root / "public" / "numeric.pdf").read_bytes(), clean)  # copied byte-identical
        self.numeric.write_bytes(with_parms(b"<< /K -%s >>" % LONG))
        before = self.snapshot()
        result = self.cli("build")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(message, result.stderr)
        self.assertSafe(result)
        self.assertEqual(self.snapshot(), before)  # input, public/ bytes, modes and mtimes untouched
        self.assertEqual([p.name for p in self.root.iterdir() if p.name.startswith(".public")], [])

    def test_content_check_numeric_text_json_parity_and_aggregation(self):
        (self.assets / "clean.pdf").write_bytes(pdf())
        (self.root / "content" / "findings" / "broken.json").write_text("{}", encoding="utf-8")
        before = self.snapshot()
        text, as_json = self.cli("content-check"), self.cli("content-check", "--json")
        for result in (text, as_json):
            self.assertEqual(result.returncode, 3, result.stderr)
            self.assertSafe(result)
        self.assertIn(b"public-assets/numeric.pdf: PDF (unreadable PDF)", text.stdout)
        document = json.loads(as_json.stdout)
        self.assertEqual(sorted(document), ["count", "law_package", "problems", "schema", "truncated"])
        self.assertEqual(document["schema"], "content-check-v1")
        messages = {p["file"]: p["message"] for p in document["problems"] if p["file"]}
        self.assertEqual(messages["public-assets/numeric.pdf"], "PDF (unreadable PDF)")
        self.assertTrue(any(name.startswith("findings/broken.json") for name in messages), messages)
        self.assertNotIn("public-assets/clean.pdf", messages)
        self.assertEqual(self.snapshot(), before)  # read-only: nothing written, no public/
        self.assertFalse((self.root / "public").exists())


if __name__ == "__main__":
    unittest.main()
