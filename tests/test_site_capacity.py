"""Synthetic content-campaign site benchmark acceptance; loopback only, small request counts."""
import contextlib
import hashlib
import io
import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import benchmark_starter
from tools.benchmark_starter import QuietHandler, benchmark
from tools.synthetic_content import write_synthetic_content

STARTER_KEYS = {"schema", "synthetic_only", "target", "concurrency", "external_requests",
                "production_capacity_validated", "requests", "errors", "error_rate",
                "elapsed_seconds", "requests_per_second", "latency_ms"}


def recording_handler(seen, status_for=None, big_for=None):
    class Double(QuietHandler):
        def do_GET(self):
            seen.append(self.path)
            if self.path == status_for:
                self.send_error(404)
            elif self.path == big_for:
                body = b"x" * (2 * 1024 * 1024 + 10)
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                super().do_GET()
    return Double


class SyntheticContentTests(unittest.TestCase):
    def test_writes_fixed_content_with_runtime_source_hashes(self):
        with tempfile.TemporaryDirectory() as folder:
            result = write_synthetic_content(folder, "capacity")
            self.assertEqual((result["findings"], result["meetings"]), (2, 1))
            content = Path(folder) / "content"
            text = "".join(path.read_text() for path in sorted(content.rglob("*.json")))
            for data in result["source_bytes"]:
                self.assertIn(hashlib.sha256(data).hexdigest(), text)
            self.assertIn("example.invalid", text)
            self.assertNotIn("base_url", text)
            with self.assertRaises(ValueError):
                write_synthetic_content(folder, "capacity")


class SiteCapacityTests(unittest.TestCase):
    def test_site_loads_every_built_page_without_errors(self):
        report = benchmark(24, 2, site=True)
        self.assertEqual(report["schema"], "synthetic-site-capacity-v1")
        self.assertEqual((report["requests"], report["errors"], report["failed_paths"]), (24, 0, []))
        pages = {page["path"]: page for page in report["pages"]}
        for path in ("/index.html", "/findings/synthetic-finding-1.html", "/findings/synthetic-finding-2.html",
                     "/meetings.html", "/sources.html", "/feed.xml", "/agency-cards.js", "/app.js", "/style.css"):
            self.assertIn(path, pages)
        self.assertEqual(list(pages), sorted(pages))
        for page in report["pages"]:
            self.assertGreaterEqual(page["requests"], 1, page["path"])
            self.assertGreater(page["bytes"], 0, page["path"])
            self.assertLessEqual(page["p50"], page["p95"])
        self.assertEqual(sum(page["requests"] for page in report["pages"]), 24)

    def test_fewer_requests_than_pages_requests_first_sorted_paths_only(self):
        seen = []
        with mock.patch.object(benchmark_starter, "QuietHandler", recording_handler(seen)):
            report = benchmark(3, 1, site=True)
        paths = [page["path"] for page in report["pages"]]
        self.assertEqual(sorted(seen), paths[:3])
        self.assertEqual([page["requests"] for page in report["pages"]], [1, 1, 1] + [0] * (len(paths) - 3))
        self.assertTrue(all(page["p50"] is None for page in report["pages"][3:]))

    def test_404_on_one_path_named_in_failed_paths_and_exit_1(self):
        double = recording_handler([], status_for="/feed.xml")
        with mock.patch.object(benchmark_starter, "QuietHandler", double):
            report = benchmark(30, 2, site=True)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = benchmark_starter.main(["--site", "--requests", "30"])
        self.assertEqual(report["failed_paths"], ["/feed.xml"])
        feed = next(page for page in report["pages"] if page["path"] == "/feed.xml")
        self.assertGreaterEqual(feed["errors"], 1)
        self.assertEqual(report["errors"], feed["errors"])
        self.assertEqual(code, 1)
        self.assertIn('"/feed.xml"', out.getvalue())

    def test_oversized_response_is_an_error_for_that_path_only(self):
        double = recording_handler([], big_for="/agency-cards.js")
        with mock.patch.object(benchmark_starter, "QuietHandler", double):
            report = benchmark(30, 2, site=True)
        self.assertEqual(report["failed_paths"], ["/agency-cards.js"])
        others = [page for page in report["pages"] if page["path"] != "/agency-cards.js"]
        self.assertEqual(sum(page["errors"] for page in others), 0)

    def test_bounds_refused_before_directory_or_server(self):
        with mock.patch.object(benchmark_starter.tempfile, "TemporaryDirectory") as folder, \
                mock.patch.object(benchmark_starter, "ThreadingHTTPServer") as server:
            for requests, concurrency in [(0, 1), (1001, 1), (1, 0), (1, 9), (True, 1)]:
                with self.subTest(requests=requests, concurrency=concurrency):
                    with self.assertRaises(ValueError):
                        benchmark(requests, concurrency, site=True)
        folder.assert_not_called()
        server.assert_not_called()

    def test_without_site_report_and_requests_unchanged(self):
        seen = []
        with mock.patch.object(benchmark_starter, "QuietHandler", recording_handler(seen)):
            report = benchmark(5, 2)
        self.assertEqual(set(report), STARTER_KEYS)
        self.assertEqual((report["schema"], report["target"]),
                         ("synthetic-starter-capacity-v1", "generated-starter-index-on-loopback"))
        self.assertEqual(seen, ["/index.html"] * 5)

    def test_temp_campaign_removed_after_success_and_exceptions(self):
        with tempfile.TemporaryDirectory() as temp_root:
            def leftovers():
                return [p for p in Path(temp_root).iterdir() if p.name.startswith("synthetic-starter-capacity-")]
            with mock.patch.object(tempfile, "tempdir", temp_root):
                benchmark(2, 1, site=True)
                self.assertEqual(leftovers(), [])
                with mock.patch.object(benchmark_starter, "build", side_effect=RuntimeError("build")):
                    with self.assertRaises(RuntimeError):
                        benchmark(2, 1, site=True)
                self.assertEqual(leftovers(), [])
                with mock.patch.object(benchmark_starter, "HTTPConnection", side_effect=RuntimeError("load")):
                    with self.assertRaises(RuntimeError):
                        benchmark(2, 1, site=True)
                self.assertEqual(leftovers(), [])

    def test_only_loopback_sockets_are_opened(self):
        hosts, real = [], socket.create_connection

        def guarded(address, *args, **kwargs):
            hosts.append(address[0])
            if address[0] != "127.0.0.1":
                raise AssertionError(f"non-loopback connection to {address[0]}")
            return real(address, *args, **kwargs)
        with mock.patch.object(socket, "create_connection", guarded):
            report = benchmark(6, 2, site=True)
        self.assertEqual(report["errors"], 0)
        self.assertEqual(hosts, ["127.0.0.1"] * 6)


if __name__ == "__main__":
    unittest.main()
