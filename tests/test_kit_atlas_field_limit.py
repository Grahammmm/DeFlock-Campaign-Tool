"""Overlapping Atlas CSV reads keep the long-field limit and restore the caller's csv field limit. Synthetic CSVs only."""
import csv
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import kit_atlas  # noqa: E402
from campaign_tool.kit_atlas import AtlasError  # noqa: E402

WAIT = 20
HEADER = "Agency,City,County,State,Technology,Vendor,Notes\n"
ROW = 'Alpha PD,Somewhere,Fictional,CA,Automated License Plate Readers,Flock,"{}"\n'
SHORT = HEADER + ROW.format("x")
LONG = HEADER + ROW.format("x" * 300000)
BAD = HEADER + ROW.format("multi\nline") + '"unterminated'


class AcquireNotice:
    """Forwarding wrapper around the real module lock that announces each acquire attempt before blocking."""

    def __init__(self, real, attempted):
        self.real, self.attempted = real, attempted

    def __enter__(self):
        self.attempted.set()
        self.real.acquire()

    def __exit__(self, *exc):
        self.real.release()


class FieldLimitScopeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.outer = csv.field_size_limit()
        self.addCleanup(csv.field_size_limit, self.outer)
        self.release = threading.Event()
        self.addCleanup(self.release.set)

    def path(self, name, text):
        target = Path(self.tmp.name) / name
        target.write_text(text, encoding="utf-8", newline="")
        return str(target)

    def overlap(self, first_text, second_text, fail_first_with=None):
        """Run first (paused inside the real parser while holding the scope) then second; return both outcomes."""
        first_path, second_path = self.path("first.csv", first_text), self.path("second.csv", second_text)
        real_reader, parsing, attempted, results = csv.reader, threading.Event(), threading.Event(), {}

        class Gate:
            def __init__(self, reader):
                self.reader, self.paused = reader, False

            def __iter__(self):
                return self

            @property
            def line_num(self):
                return self.reader.line_num

            def __next__(self):
                item = next(self.reader)
                if threading.current_thread().name == "first" and not self.paused:
                    self.paused = True
                    parsing.set()
                    self_test.assertTrue(self_test.release.wait(WAIT))
                    if fail_first_with is not None:
                        raise fail_first_with
                return item

        self_test = self

        def run(key, target):
            try:
                results[key] = kit_atlas.read_atlas_csv(target)
            except BaseException as exc:  # noqa: BLE001 - recorded for the assertions
                results[key] = exc

        lock = AcquireNotice(kit_atlas._CSV_FIELD_LIMIT_LOCK, attempted)
        with mock.patch.object(csv, "reader", lambda *a, **k: Gate(real_reader(*a, **k))), \
                mock.patch.object(kit_atlas, "_CSV_FIELD_LIMIT_LOCK", lock):
            t1 = threading.Thread(target=run, args=("first", first_path), name="first")
            t2 = threading.Thread(target=run, args=("second", second_path), name="second")
            t1.start()
            self.assertTrue(parsing.wait(WAIT))
            attempted.clear()
            t2.start()
            self.assertTrue(attempted.wait(WAIT))  # the second call is contending while the first still parses
            self.assertEqual(csv.field_size_limit(), kit_atlas.ATLAS_MAX_BYTES)
            self.release.set()
            t1.join(WAIT)
            t2.join(WAIT)
            self.assertFalse(t1.is_alive() or t2.is_alive())
        return results

    def test_overlapping_atlas_reads_keep_long_field_admission_and_original_limit(self):
        for first, second in ((SHORT, LONG), (LONG, SHORT)):
            self.release.clear()
            csv.field_size_limit(131072)
            results = self.overlap(first, second)
            self.assertEqual({k: len(v) for k, v in results.items()}, {"first": 1, "second": 1})
            self.assertEqual(csv.field_size_limit(), 131072)

    def test_overlapping_success_and_csv_failure_release_and_restore(self):
        for first, second, bad_key in ((SHORT, BAD, "second"), (BAD, LONG, "first"), (LONG, BAD, "second")):
            self.release.clear()
            csv.field_size_limit(131072)
            results = self.overlap(first, second)
            good_key = "first" if bad_key == "second" else "second"
            self.assertEqual(len(results[good_key]), 1)
            self.assertIsInstance(results[bad_key], AtlasError)
            self.assertEqual(str(results[bad_key]), "Atlas CSV: line 4: malformed quoting or field too large")
            self.assertEqual(csv.field_size_limit(), 131072)
        self.assertEqual(len(kit_atlas.read_atlas_csv(self.path("later.csv", LONG))), 1)

    def test_unexpected_iteration_exception_restores_before_next_waiter(self):
        boom = RuntimeError("synthetic")
        csv.field_size_limit(131072)
        results = self.overlap(SHORT, LONG, fail_first_with=boom)
        self.assertIs(results["first"], boom)
        self.assertEqual(len(results["second"]), 1)
        self.assertEqual(csv.field_size_limit(), 131072)

    def test_nondefault_field_limits_are_captured_after_acquire(self):
        for initial in (1000, kit_atlas.ATLAS_MAX_BYTES, kit_atlas.ATLAS_MAX_BYTES * 2):
            self.release.clear()
            csv.field_size_limit(initial)
            results = self.overlap(SHORT, LONG)
            self.assertEqual(len(results["second"]), 1)
            self.assertEqual(csv.field_size_limit(), initial)
            with self.assertRaises(AtlasError):
                kit_atlas.read_atlas_csv(self.path("bad.csv", BAD))
            self.assertEqual(csv.field_size_limit(), initial)


if __name__ == "__main__":
    unittest.main()
