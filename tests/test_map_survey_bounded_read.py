"""RF0115-4: real-file survey read budgets and offline import preservation."""
import io
import os
import sys
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import date
from pathlib import Path
from unittest import mock

from campaign_tool import cli, content, map_survey
from tests import test_map_survey as fixtures


HEADER = map_survey.render_csv([])
LIMIT = map_survey.MAX_CSV_BYTES


def snapshot(root):
    """Include directory membership, modes and mtimes, but not read-induced atimes."""
    return {str(p.relative_to(root)): (p.stat().st_mode, p.stat().st_mtime_ns,
                                      p.read_bytes() if p.is_file() else None)
            for p in [root, *sorted(root.rglob("*"))]}


class ReadTrace:
    def __init__(self, handle, fail):
        self.handle, self.fail = handle, fail
        self.requests, self.returned = [], []

    def __enter__(self):
        self.handle.__enter__()
        return self

    def __exit__(self, *args):
        return self.handle.__exit__(*args)

    def read(self, size=-1):
        self.requests.append(size)
        if self.fail:
            raise OSError("synthetic read failure")
        data = self.handle.read(size)
        self.returned.append(len(data))
        return data


class SurveyBoundedReadTests(unittest.TestCase):
    def setUp(self):
        fixtures.SurveyTests.setUp(self)
        self.addCleanup(mock.patch.stopall)
        mock.patch("socket.socket", side_effect=AssertionError("network forbidden")).start()
        mock.patch("urllib.request.urlopen", side_effect=AssertionError("network forbidden")).start()

    write = staticmethod(fixtures.SurveyTests.write)

    @contextmanager
    def trace(self, *, grow=False, fail=False):
        original_fdopen, original_lstat = os.fdopen, os.lstat
        handles, admitted = [], []

        def opening(fd, *args, **kwargs):
            handle = original_fdopen(fd, *args, **kwargs)
            if args[:1] == ("rb",):
                handle = ReadTrace(handle, fail)
                handles.append(handle)
            return handle

        def inspecting(path, *args, **kwargs):
            result = original_lstat(path, *args, **kwargs)
            if Path(path) == self.csv and grow and not admitted:
                # Grow only after the real successful inspection, without timing races.
                with open(path, "ab") as handle:
                    handle.write(b"\n" * (LIMIT * 3))
                admitted.append(None)
                admitted[0] = snapshot(self.root.parent)
            return result

        with mock.patch.object(os, "fdopen", opening), mock.patch.object(os, "lstat", inspecting):
            yield handles, admitted

    def assert_read(self, handles, returned):
        self.assertEqual(len(handles), 1)
        self.assertEqual(handles[0].requests, [LIMIT + 1])
        self.assertEqual(handles[0].returned, returned)
        self.assertTrue(handles[0].handle.closed)

    def test_static_and_growing_oversize_reads_are_bounded_before_parser(self):
        self.assertEqual(LIMIT, 2 * 1024 * 1024)
        for grow in (False, True):
            with self.subTest(grow=grow):
                self.csv.write_bytes(HEADER if grow else HEADER + b"\n" * (LIMIT * 3))
                with self.trace(grow=grow) as (handles, admitted), \
                        mock.patch.object(map_survey.csv, "reader", side_effect=AssertionError("parser ran")):
                    with self.assertRaisesRegex(map_survey.SurveyError, "^--csv: file too large$"):
                        map_survey._read_sheet(self.csv)
                self.assert_read(handles, [LIMIT + 1])
                self.assertEqual(bool(admitted), grow)

    def test_overflow_precedes_decode_and_rejects_valid_truncated_prefix(self):
        for suffix in (b"\n", b'\xff"unterminated'):
            with self.subTest(suffix=suffix):
                self.csv.write_bytes(HEADER + b"\n" * (LIMIT - len(HEADER)) + suffix)
                with self.trace() as (handles, _), \
                        mock.patch.object(map_survey.csv, "reader", side_effect=AssertionError("parser ran")):
                    with self.assertRaisesRegex(map_survey.SurveyError, "^--csv: file too large$"):
                        map_survey._read_sheet(self.csv)
                self.assert_read(handles, [LIMIT + 1])

    def test_exact_byte_boundaries_bom_and_multibyte_metadata(self):
        row = {key: "" for key in map_survey.HEADER}
        row["manufacturer"] = "caf\u00e9 \u2713"
        for prefix in (HEADER, b"\xef\xbb\xbf" + map_survey.render_csv([row])):
            for offset in (-1, 0, 1):
                with self.subTest(bom=prefix.startswith(b"\xef"), offset=offset):
                    data = prefix + b"\n" * (LIMIT + offset - len(prefix))
                    self.csv.write_bytes(data)
                    with self.trace() as (handles, _):
                        if offset > 0:
                            with self.assertRaisesRegex(map_survey.SurveyError, "file too large"):
                                map_survey._read_sheet(self.csv)
                        else:
                            rows = map_survey._read_sheet(self.csv)
                            if prefix.startswith(b"\xef"):
                                self.assertEqual(rows[0][3], row["manufacturer"])
                    self.assert_read(handles, [len(data)])

    def test_handle_closes_on_success_and_read_error_without_retry(self):
        self.csv.write_bytes(HEADER)
        with self.trace() as (handles, _):
            self.assertEqual(map_survey._read_sheet(self.csv), [])
        self.assert_read(handles, [len(HEADER)])
        with self.trace(fail=True) as (handles, _):
            with self.assertRaisesRegex(OSError, "^synthetic read failure$"):
                map_survey._read_sheet(self.csv)
        self.assert_read(handles, [])

    def test_existing_decode_syntax_and_header_errors_close_handle(self):
        for data, error in ((b"\xff", "not UTF-8"), (HEADER + b'"unfinished', "not valid CSV"),
                            (b"", "the header row does not match the survey sheet")):
            with self.subTest(error=error):
                self.csv.write_bytes(data)
                with self.trace() as (handles, _):
                    with self.assertRaisesRegex(map_survey.SurveyError, error):
                        map_survey._read_sheet(self.csv)
                self.assert_read(handles, [len(data)])

    def test_preview_apply_overflow_preserves_absent_and_existing_checks(self):
        for existing in (False, True):
            if existing:
                self.write(self.path, {"schema_version": 1, "checks": []})
                self.path.chmod(0o640)
            for apply in (False, True):
                for grow in (False, True):
                    with self.subTest(existing=existing, apply=apply, grow=grow):
                        self.csv.write_bytes(HEADER if grow else HEADER + b"CANARY" + b"\n" * (LIMIT * 3))
                        before = snapshot(self.root.parent)
                        out, err = io.StringIO(), io.StringIO()
                        with self.trace(grow=grow) as (handles, admitted), \
                                mock.patch.object(map_survey.csv, "reader", side_effect=AssertionError("parser ran")), \
                                mock.patch.object(map_survey, "_write_checks") as writer, \
                                mock.patch.object(map_survey.tempfile, "TemporaryDirectory") as temporary, \
                                redirect_stdout(out), redirect_stderr(err):
                            args = ["campaign_tool", "map", "survey-import", "--csv", str(self.csv),
                                    "--directory", str(self.root), *(["--apply"] if apply else [])]
                            with mock.patch.object(sys, "argv", args), self.assertRaises(SystemExit) as stopped:
                                cli.main()
                        self.assertEqual(stopped.exception.code, 1)
                        self.assertEqual(out.getvalue(), "")
                        self.assertEqual(err.getvalue(), "Stopped: --csv: file too large\n")
                        self.assert_read(handles, [LIMIT + 1])
                        writer.assert_not_called()
                        temporary.assert_not_called()
                        self.assertEqual(snapshot(self.root.parent), admitted[0] if grow else before)

    def test_generated_blank_and_observation_round_trip(self):
        rows, _, day = map_survey.survey_rows(self.root, "Cedar", today=date(2026, 11, 10))
        self.assertEqual(rows[1]["manufacturer"], "=Evil,Co")
        blank = map_survey.render_csv(rows)
        self.assertIn(b"'=Evil,Co", blank)
        self.csv.write_bytes(blank)
        before = snapshot(self.root.parent)
        for apply in (False, True):
            report = map_survey.survey_import(self.root, self.csv, apply=apply, today=day)
            self.assertEqual(report["added"], 0)
        self.assertEqual(snapshot(self.root.parent), before)
        rows[0].update(status="seen_in_person", checked_on=day.isoformat(),
                       source="https://records.example.invalid/survey", note='gone, "ok"')
        filled = map_survey.render_csv(rows)
        self.csv.write_bytes(filled)
        before = snapshot(self.root.parent)
        dry = map_survey.survey_import(self.root, self.csv, today=day)
        self.assertEqual(snapshot(self.root.parent), before)
        with mock.patch.object(map_survey, "_write_checks", wraps=map_survey._write_checks) as writer:
            applied = map_survey.survey_import(self.root, self.csv, apply=True, today=day)
        writer.assert_called_once()
        expected = {"node": 10, "status": "seen_in_person", "checked_on": day.isoformat(),
                    "source": rows[0]["source"], "note": rows[0]["note"]}
        self.assertEqual(dry["checks"], [expected])
        self.assertEqual(applied["checks"], [expected])
        self.assertTrue(applied["applied"])
        self.assertFalse(dry["applied"])
        self.assertEqual(self.path.read_bytes(), map_survey._serialize({"schema_version": 1, "checks": [expected]}))
        loaded = {key: value for key, value in expected.items() if key != "node"}
        loaded.update(title=None, fragment=None)
        self.assertEqual(content.load_map(self.root / "content", today=day)["point_checks"][10], [loaded])
        self.assertEqual(self.csv.read_bytes(), filled)

    def test_unsafe_input_admission_prevents_payload_open(self):
        target = self.csv.with_suffix(".target")
        target.write_bytes(HEADER)
        for kind in ("missing", "directory", "symlink", "dangling"):
            with self.subTest(kind=kind):
                if kind == "directory":
                    self.csv.mkdir()
                elif kind in ("symlink", "dangling"):
                    self.csv.symlink_to(target if kind == "symlink" else target.with_suffix(".missing"))
                with self.trace() as (handles, _):
                    with self.assertRaisesRegex(map_survey.SurveyError, "^--csv: not a regular file$"):
                        map_survey._read_sheet(self.csv)
                self.assertEqual(handles, [])
                if self.csv.is_symlink():
                    self.csv.unlink()
                elif self.csv.is_dir():
                    self.csv.rmdir()


if __name__ == "__main__":
    unittest.main()
