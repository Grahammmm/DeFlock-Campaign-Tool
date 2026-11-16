"""Ancestor .git preflight: finder, bounded probe driver and the rendered wrapper (FIX-136).

Synthetic only: temporary directories, a fake exec transport (a shell script standing in
for ``docker exec``) and the fake docker/Telegram harness. No real project is inspected.
"""
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
import time
import unittest
from unittest import mock

from campaign_tool.records import repository_preflight as rp
from campaign_tool.records import arrival
from campaign_tool.records.run_orchestrator import WORK_STAGES
from tests.test_records_runtime import ROOT
from tests.records.test_arrival_runtime import ArrivalRuntime

PROBE_SCRIPT = """#!/bin/sh
echo "$*" >>"{log}"
PYTHONPATH={root} exec {py} "$@"
"""


class TempCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="rp-synthetic-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def exec_script(self, body=None, name="fake-exec"):
        """A fake execution context; by default it really runs the probe module."""
        log = self.tmp / f"{name}.log"
        path = self.tmp / name
        path.write_text(body.replace("@LOG@", str(log)) if body is not None else
                        PROBE_SCRIPT.format(log=log, root=ROOT, py=sys.executable))
        path.chmod(stat.S_IRWXU)
        return path, log

    def config(self, script, projects, enabled=None):
        stages = []
        for index, name in enumerate(WORK_STAGES):
            project = projects[min(index, len(projects) - 1)]
            stages.append({"name": name, "enabled": True if enabled is None else enabled(name),
                           "argv": [str(script), "-B", "-m", rp.STAGE_MODULE, name,
                                    "--project", str(project)],
                           "timeout_seconds": 60, "on_fail": "stop"})
        return {"target_config_version": 1, "target": "TEST", "mode": "test", "stages": stages}

    def calls(self, log):
        return log.read_text().splitlines() if log.exists() else []


class FinderTests(TempCase):
    def test_marker_at_project_depth_zero(self):
        (self.tmp / ".git").mkdir()
        self.assertEqual(rp.find_marker(self.tmp), 0)

    def test_nearest_ancestor_marker_depth(self):
        (self.tmp / ".git").mkdir()
        (self.tmp / "a" / ".git").mkdir(parents=True)
        deep = self.tmp / "a" / "b" / "c"
        deep.mkdir(parents=True)
        self.assertEqual(rp.find_marker(deep), 2)

    def test_git_pointer_file_and_dangling_link_refuse(self):
        (self.tmp / "file" / "p").mkdir(parents=True)
        (self.tmp / "file" / ".git").write_text("gitdir: elsewhere\n")
        (self.tmp / "link" / "p").mkdir(parents=True)
        (self.tmp / "link" / ".git").symlink_to(self.tmp / "missing-target")
        self.assertEqual(rp.find_marker(self.tmp / "file" / "p"), 1)
        self.assertEqual(rp.find_marker(self.tmp / "link" / "p"), 1)

    def test_clean_tree_through_filesystem_root(self):
        (self.tmp / "p").mkdir()
        self.assertIsNone(rp.find_marker(self.tmp / "p"))

    def test_permission_error_is_unknown_not_clean(self):
        real = os.lstat

        def deny(path, *args, **kw):
            if str(path).endswith(os.sep + ".git") and Path(path).parent == self.tmp:
                raise PermissionError(13, "denied")
            return real(path, *args, **kw)

        with mock.patch.object(rp.os, "lstat", deny):
            with self.assertRaises(rp.PreflightError):
                rp.find_marker(self.tmp / "p")
            self.assertIsNone(rp.probe_result(self.tmp / "p"))

    def test_relative_or_dotdot_path_is_unknown(self):
        for bad in ("relative/p", str(self.tmp / ".." / "p")):
            with self.assertRaises(rp.PreflightError):
                rp.find_marker(bad)


class DriverTests(TempCase):
    def test_host_runtime_clean_container_project_dirty(self):
        host, dirty = self.tmp / "host" / "runtime", self.tmp / "container" / "proj"
        host.mkdir(parents=True)
        dirty.mkdir(parents=True)
        (self.tmp / "container" / ".git").mkdir()
        script, _ = self.exec_script()
        self.assertEqual(rp.check_target(self.config(script, [dirty]), 30), ("marker", 1))
        config_file = self.tmp / "TEST.json"
        config_file.write_text(json.dumps(self.config(script, [dirty])))
        with mock.patch("builtins.print") as shown:
            self.assertEqual(rp.main(["check", "--target-config", str(config_file),
                                      "--timeout", "30"]), rp.EXIT_MARKER)
        shown.assert_called_once_with("PROJECT_INSIDE_REPOSITORY depth=1 basename=.git")

    def test_clean_projects_pass_and_repeated_context_probed_once(self):
        project = self.tmp / "proj"
        project.mkdir()
        script, log = self.exec_script()
        self.assertEqual(rp.check_target(self.config(script, [project]), 30), ("clean", None))
        self.assertEqual(len(self.calls(log)), 1)

    def test_conflicting_projects_probe_each_and_nearest_marker_wins(self):
        clean, bad = self.tmp / "clean", self.tmp / "bad" / "x" / "y"
        clean.mkdir()
        bad.mkdir(parents=True)
        (self.tmp / "bad" / ".git").mkdir()
        script, log = self.exec_script()
        status = rp.check_target(self.config(script, [clean, bad]), 30)
        self.assertEqual(status, ("marker", 2))
        self.assertEqual(len(self.calls(log)), 2)

    def test_malformed_record_stage_refuses(self):
        script, log = self.exec_script()
        project = str(self.tmp)
        base = [str(script), "-B", "-m", rp.STAGE_MODULE, "sync", "--project", project]
        cases = {
            "no -m": [str(script), rp.STAGE_MODULE, "sync", "--project", project],
            "unknown stage": [*base[:4], "bogus", "--project", project],
            "two projects": [*base, "--project", project],
            "no project": base[:5],
            "dangling flag": base[:6],
            "relative project": [*base[:6], "relative"],
            "module twice": [*base, "-m", rp.STAGE_MODULE],
        }
        for label, argv in cases.items():
            with self.subTest(label):
                config = self.config(script, [project])
                config["stages"][0]["argv"] = argv
                with self.assertRaises(rp.PreflightError):
                    rp.check_target(config, 30)
        self.assertEqual(self.calls(log), [])

    def test_non_records_stub_plan_is_explicitly_not_applicable(self):
        script, log = self.exec_script()
        config = self.config(script, [self.tmp])
        for entry in config["stages"]:
            entry["argv"] = [str(script), "stub-stage", entry["name"]]
        self.assertEqual(rp.check_target(config, 30), ("not-applicable", None))
        self.assertEqual(self.calls(log), [])

    def test_disabled_records_entry_not_probed(self):
        dirty = self.tmp / "d" / "p"
        dirty.mkdir(parents=True)
        (self.tmp / "d" / ".git").mkdir()
        script, log = self.exec_script()
        config = self.config(script, [dirty], enabled=lambda name: False)
        self.assertEqual(rp.check_target(config, 30), ("not-applicable", None))
        self.assertEqual(self.calls(log), [])

    def test_probe_failures_refuse_with_no_fallback(self):
        valid = '{"depth":null,"marker":".git","schema":"%s","status":"clean"}' % rp.SCHEMA
        bodies = {
            "nonzero after valid json": f"#!/bin/sh\necho '{valid}'\nexit 1\n",
            "extra output": f"#!/bin/sh\necho '{valid}'\necho extra\n",
            "unknown key": "#!/bin/sh\necho '{\"schema\":\"%s\",\"status\":\"clean\",\"depth\":null,"
                           "\"marker\":\".git\",\"path\":\"/x\"}'\n" % rp.SCHEMA,
            "wrong marker": valid.replace('".git"', '"other"').join(["#!/bin/sh\necho '", "'\n"]),
            "garbage": "#!/bin/sh\necho clean\n",
            "empty": "#!/bin/sh\nexit 0\n",
        }
        for label, body in bodies.items():
            with self.subTest(label):
                script, _ = self.exec_script(body, name=label.replace(" ", "-"))
                with self.assertRaises(rp.PreflightError):
                    rp.check_target(self.config(script, [self.tmp]), 30)

    def test_probe_timeout_refuses_within_budget(self):
        script, _ = self.exec_script("#!/bin/sh\nexec sleep 30\n")
        started = time.monotonic()
        with self.assertRaises(rp.PreflightError):
            rp.check_target(self.config(script, [self.tmp]), 1)
        self.assertLess(time.monotonic() - started, 10)

    def test_missing_helper_refuses(self):
        config = self.config(self.tmp / "no-such-exec", [self.tmp])
        with self.assertRaises(rp.PreflightError):
            rp.check_target(config, 30)

    def test_cli_failure_reports_code_only_exit_two(self):
        config_file = self.tmp / "TEST.json"
        config_file.write_text("{}")
        with mock.patch("builtins.print") as shown:
            self.assertEqual(rp.main(["check", "--target-config", str(config_file),
                                      "--timeout", "30"]), rp.EXIT_FAILED)
        shown.assert_called_once_with(rp.CODE_FAILED)

    def test_probe_cli_outputs_bounded_machine_status_only(self):
        (self.tmp / ".git").mkdir()
        (self.tmp / "p").mkdir()
        with mock.patch("builtins.print") as shown:
            self.assertEqual(rp.main(["probe", "--project", str(self.tmp / "p")]), rp.EXIT_OK)
        line = shown.call_args.args[0]
        self.assertEqual(json.loads(line), {"schema": rp.SCHEMA, "status": "marker",
                                            "depth": 1, "marker": ".git"})
        self.assertNotIn(str(self.tmp), line)


class WrapperTests(ArrivalRuntime):
    def setUp(self):
        super().setUp()
        self.fake = TempCase()
        self.fake.setUp()
        self.addCleanup(self.fake.tearDown)
        self.exec, self.probe_log = self.fake.exec_script()
        self.dirty = self.tmp / "repo" / "proj"
        self.dirty.mkdir(parents=True)
        (self.tmp / "repo" / ".git").mkdir()
        self.install_plan(self.dirty)

    def install_plan(self, project, script=None):
        config = self.fake.config(script or self.exec, [project])
        (self.runtime / "targets" / "TEST.json").write_text(json.dumps(config))

    def assert_refused(self, result, code):
        self.assertEqual(result.returncode, 78, result.stderr)
        [report] = self.reports()
        sync = self.stage(report, "sync")
        self.assertEqual((sync["status"], sync["error_code"]), ("fail", code))
        self.assertTrue(all(self.stage(report, name)["status"] == "skipped"
                            for name in WORK_STAGES[1:]))
        self.assertNotIn(str(self.tmp / "repo"), result.stderr + self.log())
        # Only probes ever ran in the fake execution context: no stage, ever.
        self.assertTrue(all(rp.PROBE_MODULE in line for line in self.fake.calls(self.probe_log)))

    def test_wrapper_marker_refusal_precedes_work_and_arrival_claim(self):
        self.stamp()
        result = self.run_wrapper("--arrival")
        self.assert_refused(result, "PROJECT_INSIDE_REPOSITORY")
        self.assertIn("PROJECT_INSIDE_REPOSITORY depth=1 basename=.git", self.log())
        self.assertEqual(len(self.fake.calls(self.probe_log)), 1)
        self.assertEqual(len(arrival.pending(self.runtime)), 1)  # stamp not consumed
        self.assertEqual(list((self.target_dir / "work").glob("arrivals-*.json")), [])

    def test_unknown_inspection_is_check_failed_78(self):
        broken, _ = self.fake.exec_script("#!/bin/sh\nexit 3\n", name="broken")
        self.install_plan(self.dirty, broken)
        self.assert_refused(self.run_wrapper(), "PROJECT_REPOSITORY_CHECK_FAILED")
        self.assertIn("PROJECT_REPOSITORY_CHECK_FAILED", self.log())

    def test_wrapper_dry_run_never_probes_or_writes(self):
        result = self.run_wrapper("--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("preflight 5 repository", result.stdout)
        self.assertFalse(self.probe_log.exists())
        self.assertFalse(self.target_dir.exists())

    def test_existing_disk_and_image_refusals_preserved(self):
        self.render(MIN_FREE_BYTES=str(1024 ** 5))
        self.install_plan(self.dirty)
        self.assert_refused(self.run_wrapper(), "DISK_SPACE_LOW")
        self.assertFalse(self.probe_log.exists())

    def test_image_mismatch_preserved(self):
        (self.state / "image").write_text("other-image\n")
        self.assert_refused(self.run_wrapper(), "IMAGE_MISMATCH")
        self.assertFalse(self.probe_log.exists())

    def test_notify_failure_does_not_replace_ancestor_exit(self):
        self.fake_helper("import sys\nsys.exit(1)\n")
        self.install_plan(self.dirty)
        result = self.run_wrapper()
        self.assertEqual(result.returncode, 78, result.stderr)
        [report] = self.reports()
        self.assertTrue(report["notify_failed"])
        self.assertEqual(self.stage(report, "sync")["error_code"], "PROJECT_INSIDE_REPOSITORY")

    def test_stub_plan_logs_not_applicable_and_runs(self):
        self.render()  # restores the synthetic stub plan
        result = self.run_wrapper()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("repository preflight: not-applicable", self.log())


if __name__ == "__main__":
    unittest.main()
