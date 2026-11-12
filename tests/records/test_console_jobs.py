"""Jobs screen: fake systemctl runner and fake GitHub fetcher only; no real systemd and no network."""
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool.records import admin_panel as ap, console_jobs as cj
from tests.records import test_console_manual_events as http_fixture
from tests.records.test_admin_panel import Base

UNITS = ["deflock-backup.timer", "deflock-deadlines.timer", "deflock-reconcile.timer",
         "deflock-run-report-review.timer", "deflock-site-build.timer", "deflock-stale-check.timer",
         "records-analysis.timer", "records-arrival.path", "records-backup-check.timer",
         "records-health.timer", "records-run.timer"]


def fake(units):
    """units: name -> property dict, or absent for not-found. Records every call."""
    calls = []

    def runner(args):
        calls.append(list(args))
        assert args[0] == "show", args
        name = args[1]
        props = units.get(name)
        if props is None:
            return 0, "LoadState=not-found\n"
        return 0, "".join("%s=%s\n" % kv for kv in props.items())
    runner.calls = calls
    return runner


def timer(**kw):
    return dict({"LoadState": "loaded", "UnitFileState": "enabled", "ActiveState": "active",
                 "LastTriggerUSec": "Sat 2026-11-07 03:00:12 PST", "NextElapseUSecRealtime": "Sun 2026-11-08 03:00:00 PST"}, **kw)


def service(**kw):
    return dict({"LoadState": "loaded", "ActiveState": "inactive", "Result": "success", "ExecMainStatus": "0",
                 "ExecMainExitTimestamp": "Sat 2026-11-07 03:00:15 PST"}, **kw)


class JobsUnit(unittest.TestCase):
    def test_discovers_every_shipped_unit_from_deploy(self):
        self.assertEqual(cj.discover(), UNITS)

    def test_discover_is_generic(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "x.timer.in").write_text("")
            Path(d, "y.service").write_text("")
            self.assertEqual(cj.discover(d, extra=("z.timer", "bad name.timer")), ["x.timer", "z.timer"])
        self.assertEqual(cj.discover("/nonexistent-deploy-root"), [])

    def test_healthy(self):
        row = cj.inspect("deflock-backup.timer", fake({"deflock-backup.timer": timer(Unit="deflock-backup.service"),
                                                      "deflock-backup.service": service()}))
        self.assertEqual(row["state"], "ok")
        self.assertIn("03:00:12", row["last_trigger"])
        self.assertIn("Next", "Next " + row["next_run"])
        self.assertIn("2026-11-08", row["next_run"])

    def test_failed_exit(self):
        row = cj.inspect("a.timer", fake({"a.timer": timer(), "a.service": service(Result="exit-code", ExecMainStatus="3")}))
        self.assertEqual(row["state"], "failed")

    def test_lock_skip_is_not_a_pass(self):
        row = cj.inspect("a.timer", fake({"a.timer": timer(), "a.service": service(Result="exit-code", ExecMainStatus="73")}))
        self.assertEqual(row["state"], "skipped")

    def test_not_installed(self):
        self.assertEqual(cj.inspect("a.timer", fake({}))["state"], "not_installed")

    def test_disabled_never_and_path_unit(self):
        self.assertEqual(cj.inspect("a.timer", fake({"a.timer": timer(UnitFileState="disabled"), "a.service": service()}))["state"], "disabled")
        self.assertEqual(cj.inspect("a.timer", fake({"a.timer": timer(LastTriggerUSec="n/a"), "a.service": service()}))["state"], "never")
        path = {"LoadState": "loaded", "UnitFileState": "enabled", "ActiveState": "active", "Unit": "p.service"}
        row = cj.inspect("p.path", fake({"p.path": path, "p.service": service()}))
        self.assertEqual((row["state"], row["next_run"]), ("unknown", "n/a (event driven)"))

    def test_never_executed_service_is_not_ok(self):
        # systemd defaults Result=success, ExecMainStatus=0 for a service that never ran
        for stamp in ("", "n/a", "0"):
            row = cj.inspect("a.timer", fake({"a.timer": timer(), "a.service": service(ExecMainExitTimestamp=stamp)}))
            self.assertEqual(row["state"], "never", stamp)
        bare = service()
        del bare["ExecMainExitTimestamp"]
        self.assertEqual(cj.inspect("a.timer", fake({"a.timer": timer(), "a.service": bare}))["state"], "unknown")

    def test_held_arrival_exit_zero_is_not_ok(self):
        # run-records.sh --arrival exits 0 when the lock is held (arrival queued), so a path unit is never ok
        path = {"LoadState": "loaded", "UnitFileState": "enabled", "ActiveState": "active", "Unit": "p.service"}
        row = cj.inspect("p.path", fake({"p.path": path, "p.service": service()}))
        self.assertEqual(row["state"], "unknown")
        self.assertIn("queues", row["detail"])
        self.assertNotIn(">ok<", cj.render(["p.path"], fake({"p.path": path, "p.service": service()})))

    def test_systemctl_missing_and_garbage_are_unknown(self):
        def missing(args):
            raise FileNotFoundError("systemctl")
        self.assertEqual(cj.inspect("a.timer", missing)["state"], "unknown")
        for out in ("", "garbage\n", "Result=success\n"):
            self.assertEqual(cj.inspect("a.timer", lambda a, out=out: (0, out))["state"], "unknown")
        self.assertEqual(cj.inspect("a.timer", lambda a: (0, None))["state"], "unknown")
        # timer fine but its service unreadable
        self.assertEqual(cj.inspect("a.timer", fake({"a.timer": timer()}))["state"], "unknown")

    def test_render_never_green_for_missing_and_only_runs_show(self):
        runner = fake({})
        html = cj.render(UNITS, runner)
        self.assertEqual(html.count("NOT INSTALLED"), len(UNITS))
        self.assertNotIn(">ok<", html)
        self.assertTrue(all(c[0] == "show" for c in runner.calls))
        self.assertNotIn("<form", html)
        self.assertNotIn("<button", html)
        self.assertIn("job list unknown", cj.render([], runner))

    def test_github_not_configured_without_token(self):
        fetcher = mock.Mock()
        for env in ({}, {cj.REPO_ENV: "o/r"}, {cj.TOKEN_ENV: "t"}, {cj.TOKEN_ENV: "t", cj.REPO_ENV: "bad slug"}):
            self.assertEqual(cj.github_runs(env, fetcher)["state"], "not_configured")
        fetcher.assert_not_called()
        self.assertIn("not configured", cj.render([], fake({}), cj.github_runs({}, fetcher)))

    def test_github_present_with_fake_fetcher(self):
        seen = []

        def fetcher(repo, token):
            seen.append(repo)
            return {"workflow_runs": [
                {"name": "check", "conclusion": "success", "updated_at": "2026-11-07T01:00:00Z"},
                {"name": "production", "conclusion": "failure", "updated_at": "2026-11-06T01:00:00Z"},
                {"name": "check", "conclusion": "failure", "updated_at": "2026-11-01T01:00:00Z"}]}
        env = {cj.TOKEN_ENV: "SECRET-TOKEN-VALUE", cj.REPO_ENV: "example-org/example-site"}
        data = cj.github_runs(env, fetcher)
        self.assertEqual(seen, ["example-org/example-site"])
        states = {r["workflow"]: r["state"] for r in data["rows"]}
        self.assertEqual(states, {"check": "ok", "camera-source-review": "unknown", "production": "failed"})
        html = cj.render([], fake({}), data)
        self.assertNotIn("SECRET-TOKEN-VALUE", html)
        self.assertIn("no run found", html)

    def test_github_failure_and_token_file(self):
        def boom(repo, token):
            raise OSError("secret detail")
        data = cj.github_runs({cj.TOKEN_ENV: "t", cj.REPO_ENV: "o/r"}, boom)
        self.assertEqual(data["state"], "unavailable")
        self.assertNotIn("secret detail", data["reason"])
        with tempfile.TemporaryDirectory() as d:
            good, loose = Path(d, "good"), Path(d, "loose")
            good.write_text("tok\n"); good.chmod(0o600)
            loose.write_text("tok\n"); loose.chmod(0o644)
            self.assertEqual(cj.load_token({cj.TOKEN_FILE_ENV: str(good)}), ("tok", ""))
            self.assertIsNone(cj.load_token({cj.TOKEN_FILE_ENV: str(loose)})[0])
            self.assertIsNone(cj.load_token({cj.TOKEN_FILE_ENV: str(Path(d, "none"))})[0])


class JobsHttp(Base):
    request = http_fixture.ManualEvents.request
    close_server = http_fixture.ManualEvents.close_server

    def test_route_nav_and_no_network(self):
        server = ap.build_server(self.project, "run_1", actor="Operator", system=False)
        server.jobs_hooks = {"runner": fake({}), "environ": {}}
        self.thread = threading.Thread(target=server.serve_forever, daemon=True)
        self.thread.start()
        self.server = server
        self.addCleanup(self.close_server)
        self.origin = "http://127.0.0.1:%d" % server.server_port
        with mock.patch.object(cj.urllib.request, "urlopen", side_effect=AssertionError("no network")):
            status, page, _ = self.request("GET", "/jobs")
            home = self.request("GET", "/")[1]
        self.assertEqual(status, 200)
        self.assertIn("records-run.timer", page)
        self.assertIn("NOT INSTALLED", page)
        self.assertIn("GitHub Actions: not configured", page)
        self.assertIn('href="/jobs"', home)
        self.assertNotEqual(self.request("POST", "/jobs", {"csrf_token": server.csrf_token})[0], 200)
