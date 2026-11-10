"""RF0755-6: ``read_weights`` enforces its 2 MiB limit on the bytes actually returned by its bounded read.

A baseline that grows after the descriptor size check (a real file rewritten between ``fstat`` and the read) must be
refused with the existing size diagnostic, never accepted as a truncated prefix. Synthetic content only; no network.
"""
import contextlib
import io
import json
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool import cli, site_check
from tests import test_site_budget as budget_fixtures
from tests import test_site_weights_record as record_fixtures

CAP = site_check.WEIGHTS_MAX_BYTES
RECORD = {"schema": "page-weights-v1", "budget_kb": 1,
          "pages": [{"page": "index.html", "html_bytes": 1, "total": 1, "assets": []}]}


def padded(size, record=RECORD, tail=b""):
    """A valid ``page-weights-v1`` document padded with JSON whitespace to exactly ``size`` bytes (``tail`` last)."""
    head = json.dumps(record, ensure_ascii=False).encode("utf-8")
    return head + b" " * (size - len(head) - len(tail)) + tail


def size_error(path):
    return f"--weights-baseline {path}: must be a regular file of at most {CAP} bytes"


class Recorder:
    """Delegating file wrapper that records each read request and its returned length."""

    def __init__(self, inner, log):
        self.inner, self.log = inner, log

    def __enter__(self):
        self.inner.__enter__()
        return self

    def __exit__(self, *exc):
        return self.inner.__exit__(*exc)

    def fileno(self):
        return self.inner.fileno()

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def read(self, *args):
        data = self.inner.read(*args)
        self.log["reads"].append((args[0] if args else None, len(data)))
        return data


class Seam:
    """Wraps ``read_weights``' descriptor seam: a real ``fstat`` of the real file, then (optionally) the same inode is
    rewritten with ``grown`` before the captured result is returned; read sizes and handle closure are recorded."""

    def __init__(self, path, grown=None):
        self.path, self.grown = Path(path), grown
        self.log = {"reads": [], "handles": [], "after_growth": None}
        self.real_fstat, self.real_fdopen = os.fstat, os.fdopen

    def fstat(self, fd):
        result = self.real_fstat(fd)
        current = self.path.stat() if self.path.exists() else None
        if (self.grown is not None and self.log["after_growth"] is None and current is not None
                and (current.st_dev, current.st_ino) == (result.st_dev, result.st_ino)):
            with open(self.path, "r+b") as producer:  # the external producer rewrites the same inode
                producer.write(self.grown)
                producer.truncate(len(self.grown))
            after = self.path.stat()
            self.log["after_growth"] = (self.path.read_bytes(), after.st_mtime_ns)
        return result

    def fdopen(self, fd, *args, **kwargs):
        inner = self.real_fdopen(fd, *args, **kwargs)
        if args[:1] != ("rb",):  # only the baseline reader is observed, not the private-record writer
            return inner
        self.log["handles"].append(inner)
        return Recorder(inner, self.log)

    def __enter__(self):
        self.patches = [mock.patch.object(site_check.os, "fstat", self.fstat),
                        mock.patch.object(site_check.os, "fdopen", self.fdopen)]
        for patch in self.patches:
            patch.start()
        return self

    def __exit__(self, *exc):
        for patch in self.patches:
            patch.stop()

    @property
    def reads(self):
        return self.log["reads"]

    def closed(self):
        return bool(self.log["handles"]) and all(handle.closed for handle in self.log["handles"])


class Base(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-weights-bounds-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.baseline = self.tmp / "baseline.json"
        self.baseline.write_bytes(padded(len(json.dumps(RECORD))))


class ReaderTests(Base):
    def read(self, grown=None):
        with Seam(self.baseline, grown) as seam:
            try:
                return seam, site_check.read_weights(self.baseline), None
            except ValueError as exc:
                return seam, None, str(exc)

    def test_baseline_growth_after_real_fstat_refuses_actual_overflow(self):
        self.assertLess(self.baseline.stat().st_size, 200)
        for final in (CAP + 1, CAP + 64):
            with self.subTest(final=final):
                self.baseline.write_bytes(padded(len(json.dumps(RECORD))))
                seam, data, error = self.read(padded(final))
                self.assertIsNone(data)
                self.assertEqual(error, size_error(self.baseline))
                self.assertEqual(seam.reads, [(CAP + 1, CAP + 1)])
                self.assertTrue(seam.closed())

    def test_oversized_prefix_is_refused_before_decode_or_parse(self):
        cases = {"valid prefix, invalid tail beyond the read window": padded(CAP + 64, tail=b"!"),
                 "invalid UTF-8 among the returned bytes": b"\xff" + padded(CAP + 63),
                 "valid prefix, whitespace beyond cap": padded(CAP + 64)}
        for name, grown in cases.items():
            with self.subTest(name):
                self.baseline.write_bytes(padded(len(json.dumps(RECORD))))
                with mock.patch.object(site_check.json, "loads", wraps=json.loads) as parser:
                    seam, data, error = self.read(grown)
                self.assertEqual(error, size_error(self.baseline))
                parser.assert_not_called()
                self.assertTrue(seam.closed())

    def test_inclusive_limit_and_under_limit_controls(self):
        multibyte = dict(RECORD, pages=[dict(RECORD["pages"][0], page="é" * 40 + ".html")])
        small = len(json.dumps(RECORD))
        # exactly the cap is accepted, statically and after growth; a multibyte field is counted in bytes
        for name, initial, grown, record in (("static cap", CAP, None, RECORD),
                                             ("growth to cap", small, CAP, RECORD),
                                             ("multibyte cap", CAP, None, multibyte)):
            with self.subTest(name):
                self.baseline.write_bytes(padded(initial, record))
                seam, data, error = self.read(None if grown is None else padded(grown, record))
                self.assertIsNone(error)
                self.assertEqual(data, record)
                self.assertEqual(seam.reads, [(CAP + 1, CAP)])
                self.assertTrue(seam.closed())
        # one byte over, static: refused from the descriptor size before any content read
        self.baseline.write_bytes(padded(CAP + 1))
        seam, data, error = self.read()
        self.assertEqual(error, size_error(self.baseline))
        self.assertEqual(seam.reads, [])
        self.assertTrue(seam.closed())
        # multibyte payload whose character count is under the cap but whose byte count is over
        text = padded(CAP + 1, multibyte)
        self.assertLess(len(text.decode("utf-8")), len(text))
        self.baseline.write_bytes(padded(small))
        seam, data, error = self.read(text)
        self.assertEqual(error, size_error(self.baseline))

    def test_existing_baseline_admission_and_validation_remain_distinct(self):
        link, fifo, folder = self.tmp / "link.json", self.tmp / "fifo.json", self.tmp / "folder"
        link.symlink_to(self.baseline)
        os.mkfifo(fifo)
        folder.mkdir()
        good = {"schema": "page-weights-v1", "pages": [{"page": "a", "total": 1}]}
        files = {"badutf8.json": b"\xff\xfe", "notjson.json": b"not json {", "list.json": b"[]",
                 "v2.json": json.dumps({"schema": "page-weights-v2", "pages": []}).encode(),
                 "nopages.json": json.dumps({"schema": "page-weights-v1"}).encode(),
                 "pageslist.json": json.dumps({"schema": "page-weights-v1", "pages": {}}).encode(),
                 "dup.json": json.dumps(dict(good, pages=[{"page": "a", "total": 1}, {"page": "a", "total": 2}])).encode(),
                 "ctrl.json": json.dumps(dict(good, pages=[{"page": "a\x1b[2J", "total": 1}])).encode(),
                 "bool.json": json.dumps(dict(good, pages=[{"page": "a", "total": True}])).encode(),
                 "str.json": json.dumps(dict(good, pages=[{"page": "a", "total": "1"}])).encode(),
                 "neg.json": json.dumps(dict(good, pages=[{"page": "a", "total": -1}])).encode()}
        for name, body in files.items():
            (self.tmp / name).write_bytes(body)
        expected = {"badutf8.json": "not valid JSON", "notjson.json": "not valid JSON",
                    "list.json": "not a page-weights-v1 record", "v2.json": "not a page-weights-v1 record",
                    "nopages.json": "not a page-weights-v1 record", "pageslist.json": "not a page-weights-v1 record",
                    "dup.json": "malformed or duplicate page entry", "ctrl.json": "malformed or duplicate page entry",
                    "bool.json": "malformed or duplicate page entry", "str.json": "malformed or duplicate page entry",
                    "neg.json": "malformed or duplicate page entry"}
        for name, message in expected.items():
            with self.assertRaises(ValueError, msg=name) as caught:
                site_check.read_weights(self.tmp / name)
            self.assertEqual(str(caught.exception), f"--weights-baseline {self.tmp / name}: {message}", name)
        with self.assertRaises(ValueError) as caught:
            site_check.read_weights(fifo)
        self.assertEqual(str(caught.exception), size_error(fifo))
        with self.assertRaises(IsADirectoryError):  # unchanged: a directory is not normalised by this repair
            site_check.read_weights(folder)
        for path in (link, self.tmp / "absent.json"):
            with self.assertRaises(ValueError) as caught:
                site_check.read_weights(path)
            self.assertIn(f"--weights-baseline {path}: cannot be read as a regular file, not a symlink (", str(caught.exception))
        self.assertEqual(site_check.read_weights(self.baseline), RECORD)


class BudgetTests(Base):
    def setUp(self):
        super().setUp()
        self.public = self.tmp / "campaign" / "public"
        self.public.mkdir(parents=True)
        (self.public / "index.html").write_text("<!doctype html><html><body>aa</body></html>")
        self.out = self.tmp / "private.json"

    def snapshot(self):
        items = {}
        for path in [self.public, *sorted(self.public.rglob("*")), *([self.out] if self.out.exists() else [])]:
            info = path.stat()
            items[str(path)] = (stat.S_IMODE(info.st_mode), info.st_mtime_ns, None if path.is_dir() else path.read_bytes())
        return items

    def budget(self, grown, **kwargs):
        out = io.StringIO()
        with Seam(self.baseline, grown) as seam, contextlib.redirect_stdout(out), \
                budget_fixtures.NO_NETWORK:
            try:
                cli.weight_budget(self.public, 100, weights_out=self.out, weights_baseline=self.baseline, **kwargs)
            except ValueError as exc:
                return seam, out.getvalue(), str(exc)
        return seam, out.getvalue(), None

    def test_direct_budget_refusal_preserves_outputs_and_releases_handle(self):
        grown = padded(CAP + 64)
        for existing in (False, True):
            with self.subTest(existing_output=existing):
                self.out.unlink(missing_ok=True)
                self.baseline.write_bytes(padded(len(json.dumps(RECORD))))
                if existing:
                    self.out.write_bytes(b"previous private record\n")
                    self.out.chmod(0o600)
                    os.utime(self.out, ns=(10**18, 10**18))
                before = self.snapshot()
                with mock.patch.object(site_check, "write_weights", wraps=site_check.write_weights) as writer:
                    seam, text, error = self.budget(grown)
                self.assertEqual(error, size_error(self.baseline))
                self.assertEqual(text, "")
                writer.assert_not_called()
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(self.out.exists(), existing)
                self.assertTrue(seam.closed())
                grown_bytes, grown_mtime = seam.log["after_growth"]  # the loader never modified the input further
                self.assertEqual(grown_bytes, grown)
                self.assertEqual((self.baseline.read_bytes(), self.baseline.stat().st_mtime_ns), (grown, grown_mtime))


class CliTests(record_fixtures.Base):
    def setUp(self):
        super().setUp()
        self.baseline = self.private / "baseline.json"
        self.out = self.private / "new.json"

    def build(self, *extra, budget="100000"):
        return super().build("--weights-baseline", str(self.baseline), *extra, budget=budget)

    def test_growth_refusal_reaches_real_cli_and_does_not_write_record(self):
        for budget in ("100000", "1"):  # an invalid baseline is refused before the budget decision
            with self.subTest(budget=budget):
                shutil.rmtree(self.public, ignore_errors=True)
                self.baseline.write_bytes(padded(len(json.dumps(RECORD))))
                with Seam(self.baseline, padded(CAP + 64)) as seam:
                    code, text, err = self.build("--weights-out", str(self.out), budget=budget)
                self.assertEqual(code, 1)
                self.assertIn(size_error(self.baseline), err)
                self.assertNotIn("Traceback", err)
                for footer in ("Page weight", "since baseline", "new page", "over budget", "Page weights written to"):
                    self.assertNotIn(footer, text)
                self.assertFalse(self.out.exists())
                self.assertEqual(seam.reads, [(CAP + 1, CAP + 1)])
                self.assertTrue(seam.closed())

    def test_valid_comparisons_records_and_budget_codes_remain_compatible(self):
        first = self.private / "first.json"
        code, text, err = super().build("--weights-out", str(first))  # genuine writer-produced baseline
        self.assertEqual((code, err.count("Traceback")), (0, 0))
        self.assertIn("Page weight (first-party", text)
        record = json.loads(first.read_text(encoding="utf-8"))
        self.baseline.write_bytes(padded(CAP, record))  # exactly the cap, padded valid JSON
        self.assertEqual(self.baseline.stat().st_size, CAP)
        shutil.rmtree(self.public)
        code, text, err = self.build("--weights-out", str(self.out))
        self.assertEqual(code, 0, err)
        lines = [line for line in text.split("Page weight (first-party", 1)[1].splitlines()[1:] if line.startswith("  ")]
        self.assertTrue(lines and all(line.endswith(" bytes (±0 since baseline)") for line in lines), lines)
        self.assertEqual(stat.S_IMODE(self.out.stat().st_mode), 0o600)
        self.assertEqual(self.out.read_bytes(), first.read_bytes())
        # growth to exactly the cap after fstat is accepted unchanged
        self.baseline.write_bytes(padded(len(json.dumps(record)), record))
        self.out.unlink()
        shutil.rmtree(self.public)
        with Seam(self.baseline, padded(CAP, record)) as seam:
            code, text, err = self.build("--weights-out", str(self.out))
        self.assertEqual((code, seam.reads), (0, [(CAP + 1, CAP)]), err)
        self.assertEqual(self.out.read_bytes(), first.read_bytes())
        # over budget still exits 3 with a valid baseline; a no-baseline build prints the plain table
        self.out.unlink()
        shutil.rmtree(self.public)
        code, text, _ = self.build("--weights-out", str(self.out), budget="1")
        self.assertEqual(code, 3)
        self.assertIn("over budget: index.html", text)
        shutil.rmtree(self.public)
        code, text, _ = budget_fixtures.run_main("build", "--directory", str(self.root), "--budget", "100000")
        self.assertEqual(code, 0)
        self.assertNotIn("baseline", text)


if __name__ == "__main__":
    unittest.main()
