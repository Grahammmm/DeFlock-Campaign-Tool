"""RF0211-3: survey-import reads only the regular file it inspected."""
import io
import os
import stat
import sys
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import date
from unittest import mock

from campaign_tool import cli, map_survey
from tests import test_map_survey as fixtures

HEADER = map_survey.render_csv([])
REFUSED = "--csv: cannot safely read the inspected regular file"


class SurveyReadSafetyTests(unittest.TestCase):
    def setUp(self):
        fixtures.SurveyTests.setUp(self)
        self.addCleanup(mock.patch.stopall)
        mock.patch("socket.socket", side_effect=AssertionError("network forbidden")).start()
        self.alt = self.csv.with_suffix(".alt")
        self.parked = self.csv.with_suffix(".parked")

    write = staticmethod(fixtures.SurveyTests.write)

    @contextmanager
    def swap_at_open(self, how):
        """Substitute the CSV immediately before the real payload open, then delegate to the real os.open."""
        real_open, state = os.open, {"flags": None, "swapped": False}

        def opening(path, flags, *args, **kwargs):
            if os.fspath(path) == os.fspath(self.csv) and not state["swapped"]:
                state["swapped"] = True
                state["flags"] = flags
                if how == "fifo":
                    self.assertTrue(flags & os.O_NONBLOCK, "FIFO would block")
                os.replace(self.csv, self.parked)
                if how == "symlink":
                    self.csv.symlink_to(self.alt)
                elif how == "inode":
                    os.replace(self.alt, self.csv)
                elif how == "fifo":
                    os.mkfifo(self.csv)
            return real_open(path, flags, *args, **kwargs)

        with mock.patch.object(os, "open", opening):
            yield state

    def make_alt(self, data=HEADER):
        self.alt.write_bytes(data)

    def test_late_substitutions_are_refused_before_payload_read(self):
        for how in ("symlink", "inode", "fifo"):
            for same_bytes in (False, True):
                with self.subTest(how=how, same=same_bytes):
                    self.csv.write_bytes(HEADER)
                    self.make_alt(HEADER if same_bytes else HEADER + b"CANARY")
                    with self.swap_at_open(how) as state, \
                            mock.patch.object(map_survey.csv, "reader", side_effect=AssertionError("parser ran")):
                        with self.assertRaisesRegex(map_survey.SurveyError, "^" + REFUSED + "$") as caught:
                            map_survey._read_sheet(self.csv)
                    self.assertTrue(state["flags"] & os.O_NOFOLLOW)
                    self.assertNotIn("CANARY", str(caught.exception))
                    self.assertEqual(self.parked.read_bytes(), HEADER)
                    self.assertEqual(stat.S_ISFIFO(os.lstat(self.csv).st_mode), how == "fifo")
                    for leftover in (self.csv, self.alt, self.parked):
                        if os.path.lexists(leftover):
                            os.unlink(leftover)

    def test_missing_safety_flags_refuse_without_opening(self):
        self.csv.write_bytes(HEADER)
        for name in ("O_NOFOLLOW", "O_NONBLOCK"):
            with self.subTest(flag=name), mock.patch.object(os, name, 0, create=True), \
                    mock.patch.object(map_survey.os, "open", side_effect=AssertionError("opened")):
                with self.assertRaisesRegex(map_survey.SurveyError, "^" + REFUSED + "$"):
                    map_survey._read_sheet(self.csv)

    def test_descriptor_closed_on_fstat_refusal_constructor_failure_and_read_error(self):
        self.csv.write_bytes(HEADER)
        real_open, real_close, opened, closed = os.open, os.close, [], []

        def opening(*args, **kwargs):
            fd = real_open(*args, **kwargs)
            opened.append(fd)
            return fd

        def closing(fd):
            closed.append(fd)
            real_close(fd)

        cases = {
            "fstat": mock.patch.object(os, "fstat", side_effect=OSError("canary-os-text")),
            "fdopen": mock.patch.object(os, "fdopen", side_effect=OSError("canary-os-text")),
        }
        for name, patch in cases.items():
            opened.clear()
            closed.clear()
            with self.subTest(case=name), mock.patch.object(os, "open", opening), \
                    mock.patch.object(os, "close", closing), patch:
                with self.assertRaisesRegex(map_survey.SurveyError, "^" + REFUSED + "$") as caught:
                    map_survey._read_sheet(self.csv)
            self.assertNotIn("canary", str(caught.exception))
            self.assertEqual(opened, closed)
            self.assertEqual(len(opened), 1)
        with mock.patch.object(os, "fdopen", side_effect=RuntimeError("boom")), \
                mock.patch.object(os, "open", opening), mock.patch.object(os, "close", closing):
            opened.clear()
            closed.clear()
            with self.assertRaises(RuntimeError):
                map_survey._read_sheet(self.csv)
        self.assertEqual(opened, closed)
        self.assertEqual(len(opened), 1)

    def test_admitted_original_is_read_even_if_path_is_replaced_afterwards(self):
        self.csv.write_bytes(HEADER)
        self.make_alt(HEADER + b"CANARY")
        real_fdopen = os.fdopen

        def replacing(fd, *args, **kwargs):
            os.replace(self.alt, self.csv)  # after descriptor admission
            return real_fdopen(fd, *args, **kwargs)

        with mock.patch.object(os, "fdopen", replacing):
            self.assertEqual(map_survey._read_sheet(self.csv), [])

    def test_static_non_regular_inputs_keep_original_refusal(self):
        self.make_alt()
        self.csv.symlink_to(self.alt)
        with self.assertRaisesRegex(map_survey.SurveyError, "^--csv: not a regular file$"):
            map_survey._read_sheet(self.csv)

    def test_preview_apply_and_cli_refuse_substitution_without_writing(self):
        rows, _, day = map_survey.survey_rows(self.root, "Cedar", today=date(2026, 11, 10))
        rows[0].update(status="seen_in_person", checked_on=day.isoformat(),
                       source="https://records.example.invalid/survey", note="ok")
        filled = map_survey.render_csv(rows)
        for how in ("symlink", "inode"):
            for apply in (False, True):
                with self.subTest(how=how, apply=apply):
                    self.csv.write_bytes(HEADER)
                    self.make_alt(filled)
                    out, err = io.StringIO(), io.StringIO()
                    args = ["campaign_tool", "map", "survey-import", "--csv", str(self.csv),
                            "--directory", str(self.root), *(["--apply"] if apply else [])]
                    with self.swap_at_open(how), mock.patch.object(map_survey, "_write_checks") as writer, \
                            mock.patch.object(sys, "argv", args), redirect_stdout(out), redirect_stderr(err), \
                            self.assertRaises(SystemExit) as stopped:
                        cli.main()
                    self.assertEqual(stopped.exception.code, 1)
                    self.assertEqual(out.getvalue(), "")
                    self.assertEqual(err.getvalue(), f"Stopped: {REFUSED}\n")
                    writer.assert_not_called()
                    self.assertFalse(self.path.exists())
                    for leftover in (self.csv, self.alt, self.parked):
                        if os.path.lexists(leftover):
                            os.unlink(leftover)

    def test_healthy_apply_still_installs_observation(self):
        rows, _, day = map_survey.survey_rows(self.root, "Cedar", today=date(2026, 11, 10))
        rows[0].update(status="seen_in_person", checked_on=day.isoformat(),
                       source="https://records.example.invalid/survey", note="ok")
        self.csv.write_bytes(map_survey.render_csv(rows))
        report = map_survey.survey_import(self.root, self.csv, apply=True, today=day)
        self.assertEqual(report["added"], 1)
        self.assertTrue(self.path.exists())


if __name__ == "__main__":
    unittest.main()
