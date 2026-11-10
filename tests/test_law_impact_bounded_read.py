"""RF0015-2: `law impact` enforces its package byte ceiling on the bytes actually read (synthetic packages only).

Run from the repository root: python3 -B -m unittest tests.test_law_impact_bounded_read -v
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import law, law_impact  # noqa: E402

CAP = law_impact.MAX_PACKAGE_BYTES
BASE_TEXT = (REPO_ROOT / "tests" / "fixtures" / "law_impact" / "base-package.json").read_text(encoding="utf-8")


def padded(size):
    """The synthetic package with JSON whitespace appended to exactly ``size`` bytes."""
    data = BASE_TEXT.encode("utf-8")
    return data + b" " * (size - len(data))


class BoundedReadTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-law-bounded-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.old, self.new = self.tmp / "old.json", self.tmp / "new.json"
        for path in (self.old, self.new):
            path.write_text(BASE_TEXT, encoding="utf-8")
        (self.tmp / "campaign" / "content" / "findings").mkdir(parents=True)

    def grow_after_fstat(self, target, size=CAP + 1):
        """Patch os.fstat so the file grows to ``size`` bytes right after its real fstat result is captured."""
        real = os.fstat

        state = {"done": False}

        def fstat(fd):
            result = real(fd)
            if not state["done"] and result.st_size == len(BASE_TEXT.encode("utf-8")):
                state["done"] = True
                with open(target, "ab") as handle:
                    handle.write(b" " * (size - result.st_size))
            return result
        return mock.patch.object(law_impact.os, "fstat", fstat)

    def test_package_growth_after_real_fstat_refuses_before_decode(self):
        for target in (self.old, self.new):
            with self.subTest(target=target.name), self.grow_after_fstat(target), \
                    mock.patch.object(law_impact.json, "loads") as loads, \
                    mock.patch.object(law, "validate") as validate:
                with self.assertRaisesRegex(law_impact.ImpactError, f"larger than {CAP} bytes"):
                    law_impact.load_file(target)
                loads.assert_not_called()
                validate.assert_not_called()
            target.write_text(BASE_TEXT, encoding="utf-8")

    def test_read_requests_fit_the_budget_and_stop_at_cap_plus_one(self):
        self.old.write_bytes(padded(CAP))
        requests, got, real = [], [], os.read

        def short_read(fd, count):
            requests.append(count)
            data = real(fd, min(count, 1_000_000))
            got.append(len(data))
            return data
        with mock.patch.object(law_impact.os, "read", short_read):
            self.assertEqual(law_impact.load_file(self.old)["jurisdiction"], "us-ca")
        self.assertGreater(len(requests), 1)
        for index, count in enumerate(requests):
            self.assertEqual(count, CAP + 1 - sum(got[:index]))
        returned = []

        def spy(fd, count):
            data = real(fd, min(count, 1_000_000))
            returned.append(len(data))
            return data
        with self.grow_after_fstat(self.new, 3 * CAP), mock.patch.object(law_impact.os, "read", spy):
            with self.assertRaises(law_impact.ImpactError):
                law_impact.load_file(self.new)
        self.assertEqual(sum(returned), CAP + 1)

    def test_exact_cap_loads_cap_plus_one_and_oversize_descriptor_refuse(self):
        self.old.write_bytes(padded(CAP))
        self.assertEqual(law_impact.load_file(self.old)["jurisdiction"], "us-ca")
        self.new.write_bytes(padded(CAP + 1))
        with mock.patch.object(law_impact.os, "read") as read:
            with self.assertRaisesRegex(law_impact.ImpactError, f"larger than {CAP} bytes"):
                law_impact.load_file(self.new)
            read.assert_not_called()

    def test_opened_descriptor_not_later_pathname_supplies_package(self):
        other = self.tmp / "other.json"
        other.write_text("{}", encoding="utf-8")
        real = os.fstat

        def swap(fd):
            result = real(fd)
            if other.exists():
                os.replace(other, self.old)
            return result
        with mock.patch.object(law_impact.os, "fstat", swap):
            self.assertEqual(law_impact.load_file(self.old)["jurisdiction"], "us-ca")
        link = self.tmp / "link.json"
        link.symlink_to(self.new)
        self.assertEqual(law_impact.load_file(link)["jurisdiction"], "us-ca")
        dangling = self.tmp / "dangling.json"
        dangling.symlink_to(self.tmp / "nowhere.json")
        with self.assertRaisesRegex(law_impact.ImpactError, "not a file"):
            law_impact.load_file(dangling)

    def test_nonregular_inputs_and_errors_close_the_descriptor(self):
        fifo = self.tmp / "fifo.json"
        os.mkfifo(fifo)
        opened, closed, real_open, real_close = [], [], os.open, os.close

        def track_open(*args, **kwargs):
            fd = real_open(*args, **kwargs)
            opened.append(fd)
            return fd

        def track_close(fd):
            closed.append(fd)
            real_close(fd)
        with mock.patch.object(law_impact.os, "open", track_open), mock.patch.object(law_impact.os, "close", track_close):
            for bad in (fifo, self.tmp, self.tmp / "missing.json"):
                with self.subTest(bad=bad.name), self.assertRaisesRegex(law_impact.ImpactError, "not a file"):
                    law_impact.load_file(bad)
            with mock.patch.object(law_impact.os, "read", side_effect=OSError("boom")):
                with self.assertRaisesRegex(law_impact.ImpactError, "boom"):
                    law_impact.load_file(self.old)
            self.old.write_bytes(b"\xff\xfe")
            with self.assertRaises(law_impact.ImpactError):
                law_impact.load_file(self.old)
            self.old.write_text("{not json", encoding="utf-8")
            with self.assertRaisesRegex(law_impact.ImpactError, "invalid JSON"):
                law_impact.load_file(self.old)
            self.old.write_text(BASE_TEXT, encoding="utf-8")
            law_impact.load_file(self.old)
        self.assertEqual(opened, closed)

    def run_cli(self, *extra):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = law.main(["impact", str(self.old), str(self.new), "--directory", str(self.tmp / "campaign"), *extra])
        return code, out.getvalue(), err.getvalue()

    def test_cli_growth_refusal_has_no_partial_report_in_both_modes(self):
        for extra in ((), ("--json",)):
            for target in (self.old, self.new):
                with self.subTest(extra=extra, target=target.name), self.grow_after_fstat(target), \
                        mock.patch.object(law_impact, "collect") as collect:
                    code, out, err = self.run_cli(*extra)
                self.assertEqual((code, out), (2, ""))
                self.assertIn(f"{target}: larger than {CAP} bytes", err)
                self.assertNotIn("Traceback", err)
                collect.assert_not_called()
                target.write_text(BASE_TEXT, encoding="utf-8")

    def test_ordinary_packages_keep_exit_and_read_only(self):
        before = {p.name: p.read_bytes() for p in self.tmp.iterdir() if p.is_file()}
        code, out, _ = self.run_cli()
        self.assertEqual(code, 0, out)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.tmp.iterdir() if p.is_file()})
        self.assertEqual(json.loads(BASE_TEXT)["jurisdiction"], "us-ca")


if __name__ == "__main__":
    unittest.main()
