"""Contracts for ``records units-diff``: read-only, directive names only, links never followed."""
import contextlib
import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool.records import cli, units_diff

SERVICE = "[Service]\nExecStart=/usr/bin/records run\nEnvironment=TOKEN=sentinel-secret-value\n"
OTHER = "[Service]\nExecStart=/usr/bin/other\nEnvironment=TOKEN=changed-secret-value\n"
MAIL = "deflockslo-daily-mail.service.d/mail.conf"


class UnitsDiffTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.rendered, self.installed = self.base / "rendered", self.base / "installed"
        self.rendered.mkdir()
        self.installed.mkdir()
        self.calls = []

    def put(self, root, name, text):
        path = root / name
        path.parent.mkdir(exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def both(self, name, rendered=SERVICE, installed=SERVICE):
        self.put(self.rendered, name, rendered)
        self.put(self.installed, name, installed)

    def run_diff(self, installed=None, status="enabled"):
        def fake(unit):
            self.calls.append(unit)
            return status
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = units_diff.main(["--rendered", str(self.rendered), "--installed", str(installed or self.installed)],
                                   enabled=fake)
        shown = out.getvalue() + err.getvalue()
        for secret in ("sentinel-secret-value", "changed-secret-value", "/usr/bin", "TOKEN", str(self.base)):
            self.assertNotIn(secret, shown)  # no Environment value, ExecStart value or absolute path, ever
        return code, out.getvalue().splitlines()

    def snapshot(self):
        return sorted((str(p), p.lstat().st_mode, None if p.is_symlink() or not p.is_file() or not os.access(p, os.R_OK)
                       else p.read_bytes()) for p in self.base.rglob("*"))

    def test_units_diff_reports_only_directives_and_read_only_enabled_status(self):
        # Owner contract: one line per unit, directive names only, exit 0 only when every unit MATCHes, one
        # is-enabled query per parent unit. Bugs caught: an Environment value in the output; installed-only
        # records units missing from the report; unrelated filenames compared.
        self.both("records-run.service")
        self.both("deflock-console-panel.service")
        self.both(MAIL, "[Service]\nEnvironment=A=1\n", "[Service]\nEnvironment=A=1\n")
        self.put(self.rendered, "run-records.sh", "ignored")
        self.put(self.installed, "other.service", "ignored")
        self.put(self.installed, "deflock-console-old.service", "not an EXTRA: only records-* units are")
        code, lines = self.run_diff()
        self.assertEqual(code, 0)
        self.assertEqual(lines, ["MATCH deflock-console-panel.service enabled=enabled",
                                 f"MATCH {MAIL} enabled=enabled", "MATCH records-run.service enabled=enabled"])
        self.assertEqual(sorted(self.calls), ["deflock-console-panel.service", "deflockslo-daily-mail.service",
                                              "records-run.service"])
        self.put(self.installed, "records-run.service", OTHER)
        self.put(self.installed, "records-old.timer", "[Timer]\nOnCalendar=daily\n")
        self.put(self.rendered, "records-new.timer", "[Timer]\nOnCalendar=hourly\n")
        self.put(self.installed, MAIL, "[Service]\nEnvironment=A=2\n")
        code, lines = self.run_diff(status="disabled")
        self.assertEqual(code, 1)
        self.assertEqual(lines, ["MATCH deflock-console-panel.service enabled=disabled",
                                 f"DRIFT {MAIL} enabled=disabled changed: Environment",
                                 "MISSING records-new.timer enabled=disabled",
                                 "EXTRA records-old.timer enabled=disabled",
                                 "DRIFT records-run.service enabled=disabled changed: Environment, ExecStart"])

    def test_drift_in_execstart_alone_names_only_execstart(self):
        self.both("records-run.service", installed=SERVICE.replace("records run", "records old"))
        self.assertEqual(self.run_diff(), (1, ["DRIFT records-run.service enabled=enabled changed: ExecStart"]))
        self.both("records-run.service", installed=SERVICE + "# layout only\n")
        self.assertEqual(self.run_diff(), (1, ["DRIFT records-run.service enabled=enabled changed: non-directive"]))

    def test_units_diff_does_not_follow_symlinks_or_call_mutators(self):
        # Owner contract: a link is reported as drift without its target; nothing is created or changed; unusable
        # files and an unavailable systemctl never leak text. Bugs caught: link traversal, status/action conflation.
        self.both("records-run.service")
        self.put(self.rendered, "records-link.service", SERVICE)
        target = self.base / "elsewhere-target"
        target.write_text(SERVICE, encoding="utf-8")
        os.symlink(target, self.installed / "records-link.service")
        self.put(self.rendered, MAIL, "[Service]\n")
        os.symlink(self.base, self.installed / "deflockslo-daily-mail.service.d")
        self.put(self.rendered, "records-bad.service", SERVICE)
        os.chmod(self.rendered / "records-bad.service", 0)
        self.put(self.installed, "records-bad.service", SERVICE)
        before = self.snapshot()
        code, lines = self.run_diff(status="unknown")
        self.assertEqual(code, 1)
        self.assertEqual(lines, [f"DRIFT {MAIL} enabled=unknown symlink (installed, not followed)",
                                 "DRIFT records-bad.service enabled=unknown unreadable (rendered)",
                                 "DRIFT records-link.service enabled=unknown symlink (installed, not followed)",
                                 "MATCH records-run.service enabled=unknown"])
        self.assertEqual(self.snapshot(), before)

    def test_unusable_inputs_exit_2_and_unrecognised_status_is_unknown(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(units_diff.main(["--rendered", str(self.base / "absent")]), 2)
            self.assertEqual(units_diff.main(["--rendered", str(self.rendered), "--installed", str(self.installed)]), 2)
        self.put(self.rendered, "records-run.service", SERVICE)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = units_diff.main(["--rendered", str(self.rendered), "--installed", str(self.base / "none")],
                                   enabled=lambda unit: "unknown")
        self.assertEqual((code, out.getvalue()), (1, "MISSING records-run.service enabled=unknown\n"))
        self.assertEqual(cli.COMMANDS["units-diff"], ("units_diff", "main"))

    def test_is_enabled_runs_only_is_enabled_and_maps_output_to_fixed_words(self):
        # Contract: the single systemctl call is `--user is-enabled UNIT`; raw output never passes through.
        def fake(code, text):
            return mock.Mock(return_value=subprocess.CompletedProcess([], code, text, ""))
        with mock.patch.object(units_diff.subprocess, "run", fake(1, "disabled\n")) as run:
            self.assertEqual(units_diff.is_enabled("records-run.timer"), "disabled")
            self.assertEqual(run.call_args.args[0], ["systemctl", "--user", "is-enabled", "records-run.timer"])
        with mock.patch.object(units_diff.subprocess, "run", fake(0, "secret-ish text\n")):
            self.assertEqual(units_diff.is_enabled("records-run.timer"), "unknown")
        with mock.patch.object(units_diff.subprocess, "run", side_effect=FileNotFoundError("boom")):
            self.assertEqual(units_diff.is_enabled("records-run.timer"), "unknown")


if __name__ == "__main__":
    unittest.main()
