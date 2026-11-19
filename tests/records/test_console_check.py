"""Console-check acceptance against a synthetic loopback server and export."""
import io
import json
import os
import socket
import stat
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

from campaign_tool.records import cli, pipeline_health


PAGES = ("/", "/board", "/documents", "/calendar", "/settings", "/next-steps")
REQUESTS = [("GET", path) for path in (*PAGES, "/send")] + [("POST", "/approvals/reject")]
CHECKS = [f"{method} {path}" for method, path in REQUESTS] + ["export"]
CSP = ("default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
       "base-uri 'none'; frame-ancestors 'none'")
NOW = datetime.now(timezone.utc).replace(microsecond=0)
CANARY = b"private synthetic response text"


class ConsoleCheckTests(unittest.TestCase):
    def setUp(self):
        previous = os.umask(0o077)
        self.addCleanup(os.umask, previous)
        tmp = tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve())
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "export"
        self.target = Path("exports/20260101T000000Z-check")
        (self.root / self.target).mkdir(parents=True, mode=0o700)
        (self.root / "current").symlink_to(self.target)
        self.manifest = self.root / self.target / "EXPORT.json"
        self.exported((NOW - timedelta(hours=1)).isoformat())
        self.alerts = self.root.with_name("alerts")
        self.overrides, self.requests = {}, []
        test = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def reply(self):
                key = (self.command, self.path)
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                test.requests.append((key, dict(self.headers), body))
                default = (403, None) if self.command == "POST" else (404, None) if self.path == "/send" else (200, CSP)
                status, csp = test.overrides.get(key, default)
                self.send_response(status)
                if csp is not None:
                    self.send_header("Content-Security-Policy", csp)
                if 300 <= status < 400:
                    self.send_header("Location", "/not-a-check")
                self.end_headers()
                self.wfile.write(CANARY)

            do_GET = do_POST = reply

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.addCleanup(self.server.server_close)
        thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(self.server.shutdown)

    def exported(self, exported_at):
        self.manifest.write_text(json.dumps({"schema": "records-console-export-v1", "run_id": "check",
                                             "source_project_id": "0" * 16, "files": {}, "exported_at": exported_at}))

    def invoke(self, *extra, port=None):
        self.requests.clear()
        argv = ["records", "console-check", "--port", str(self.server.server_port if port is None else port),
                "--export-root", str(self.root), *extra]
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", argv), redirect_stdout(out), redirect_stderr(err):
            try:
                code = cli.main()
            except SystemExit as exc:
                code = exc.code
        self.assertNotIn(CANARY.decode(), out.getvalue() + err.getvalue())
        self.assertNotIn(str(self.root), out.getvalue() + err.getvalue())
        return code, out.getvalue().splitlines(), err.getvalue()

    def assert_checks(self, problems=None, *extra, port=None):
        problems = problems or {}
        self.assertEqual(self.invoke(*extra, port=port),
                         (int(bool(problems)), [f"{key} {problems.get(key, 'PASS')}" for key in CHECKS], ""))

    def test_checks_report_only_codes_and_refuse_unsafe_targets(self):
        self.assert_checks()
        self.assertEqual([key for key, _, _ in self.requests], REQUESTS)
        _, headers, body = self.requests[-1]
        self.assertEqual(body, b"")
        self.assertEqual(set(k.lower() for k in headers), {"host", "content-length", "accept-encoding"})
        cases = [
            (("GET", "/board"), (404, CSP), "HTTP_404"),
            (("GET", "/documents"), (200, None), "FINGERPRINT_MISSING"),
            (("GET", "/calendar"), (200, "wrong policy"), "FINGERPRINT_MISSING"),
            (("GET", "/send"), (200, CSP), "SEND_EXPOSED"),
            (("GET", "/send"), (500, None), "HTTP_500"),
            (("POST", "/approvals/reject"), (200, None), "POST_ACCEPTED"),
            (("POST", "/approvals/reject"), (303, None), "POST_ACCEPTED"),
            (("POST", "/approvals/reject"), (503, None), "PASS"),
            (("GET", "/settings"), (302, None), "HTTP_302"),
        ]
        for key, response, code in cases:
            with self.subTest(key=key, response=response):
                self.overrides = {key: response}
                self.assert_checks({" ".join(key): code} if code != "PASS" else None)
                self.assertEqual([seen for seen, _, _ in self.requests], REQUESTS)
        with socket.socket() as offline:
            offline.bind(("127.0.0.1", 0))  # port reserved, not listening
            self.assert_checks({key: "HTTP_UNAVAILABLE" for key in CHECKS[:-1]}, port=offline.getsockname()[1])
        for extra, port in [(("--host", "192.0.2.10"), None), ((), 0), ((), 65536),
                            (("--max-age-hours", "nan"), None), (("--max-age-hours", "0"), None),
                            (("--export-root", "relative"), None), (("--alerts-dir", "relative"), None)]:
            with self.subTest(extra=extra, port=port):
                self.assertEqual(self.invoke(*extra, port=port)[0], 2)
                self.assertEqual(self.requests, [])

    def test_export_bad_age_boundary_and_owned_alert_clear(self):
        self.assert_checks()
        self.exported((NOW - timedelta(hours=36)).isoformat())
        with mock.patch("campaign_tool.records.console_check.datetime", wraps=datetime) as dt:
            dt.now.return_value = NOW
            self.assert_checks({"export": "EXPORT_STALE"})
        self.exported((NOW - timedelta(hours=37)).isoformat())
        self.assert_checks({"export": "EXPORT_STALE"})
        self.assert_checks(None, "--max-age-hours", "38")
        for invalid in ((NOW + timedelta(hours=1)).isoformat(), NOW.date().isoformat(), "not-a-time"):
            self.exported(invalid)
            self.assert_checks({"export": "EXPORT_BROKEN"})
        current = self.root / "current"
        current.unlink()
        current.symlink_to("exports/20260101T000000Z-missing")
        self.assert_checks({"export": "EXPORT_BROKEN"})
        current.unlink()
        self.exported((NOW - timedelta(hours=1)).isoformat())
        outside = self.root.parent / "outside" / self.target
        outside.parent.mkdir(parents=True)
        self.manifest.parent.rename(outside)
        current.symlink_to(Path("..", "outside") / self.target)
        self.assert_checks({"export": "EXPORT_BROKEN"})
        current.unlink()
        outside.rename(self.manifest.parent)
        current.symlink_to(self.target)
        self.assertFalse(self.alerts.exists())

        unrelated = self.alerts / "health-backup-missing.json"
        self.exported((NOW - timedelta(hours=37)).isoformat())
        self.overrides = {("GET", path): (404, None) for path in ("/board", "/settings")}
        failures = {"GET /board": "HTTP_404", "GET /settings": "HTTP_404", "export": "EXPORT_STALE"}
        http_alert = self.alerts / "health-console_check_http_four_zero_four-http_four_zero_four.json"
        stale_alert = self.alerts / "health-console_check_export_stale-export_stale.json"
        for count in (1, 2):
            self.assert_checks(failures, "--alerts-dir", str(self.alerts))
            if count == 1:
                self.assertEqual(stat.S_IMODE(self.alerts.stat().st_mode), 0o700)
                unrelated.write_text("{}")
            self.assertEqual(set(self.alerts.iterdir()), {unrelated, http_alert, stale_alert})
            for alert in (http_alert, stale_alert):
                payload = json.loads(alert.read_text())
                self.assertEqual((payload["schema"], payload["state"], payload["count"]),
                                 (pipeline_health.ALERT_SCHEMA, "warn", count))
                self.assertEqual(stat.S_IMODE(alert.stat().st_mode), 0o600)
        self.overrides.clear()
        self.exported(NOW.isoformat())
        self.assert_checks()  # no opt-in: neither file is cleared
        self.assertTrue(http_alert.exists() and stale_alert.exists())
        self.assert_checks(None, "--alerts-dir", str(self.alerts))
        for alert in (http_alert, stale_alert):
            self.assertFalse(alert.exists())
            self.assertEqual(json.loads(alert.with_suffix(".cleared.json").read_text())["state"], "cleared")
        self.assertEqual(unrelated.read_text(), "{}")

    def test_unsafe_alert_store_is_refused_without_pass(self):
        self.alerts.mkdir(mode=0o700)
        alias = self.alerts.with_name("alias")
        alias.symlink_to(self.alerts)
        wide = self.alerts.with_name("wide")
        wide.mkdir()
        wide.chmod(0o777)
        for path in (self.root / "alarms", self.root.parent, alias, alias / "nested", wide, wide / "nested"):
            with self.subTest(path=path.name):
                code, lines, err = self.invoke("--alerts-dir", str(path))
                self.assertEqual((code, lines), (2, []))
                self.assertIn("alerts_unsafe", err)
        self.assertEqual(list(self.alerts.iterdir()), [])
        self.assertEqual(list(wide.iterdir()), [])
        self.assertFalse((self.root / "alarms").exists())
        (self.alerts / "health-backup-missing.json").symlink_to(self.manifest)
        code, lines, err = self.invoke("--alerts-dir", str(self.alerts))
        self.assertEqual((code, lines), (2, []))
        self.assertIn("alerts_unsafe", err)
