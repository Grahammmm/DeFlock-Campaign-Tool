"""Synthetic owned-loopback benchmark acceptance; no provider traffic."""
import unittest
from tools.benchmark_starter import benchmark, summarize


class StarterCapacityTests(unittest.TestCase):
    def test_real_generated_starter_serves_bounded_concurrent_requests(self):
        report = benchmark(12, 2)
        self.assertEqual(report["requests"], 12)
        self.assertEqual(report["errors"], 0)
        self.assertEqual(report["external_requests"], 0)
        self.assertFalse(report["production_capacity_validated"])
        self.assertGreater(report["requests_per_second"], 0)
        self.assertLessEqual(report["latency_ms"]["p50"], report["latency_ms"]["p99"])

    def test_unsafe_load_parameters_refused_before_server_creation(self):
        for requests, concurrency in [(0, 1), (1001, 1), (1, 0), (1, 9), (True, 1)]:
            with self.subTest(requests=requests, concurrency=concurrency):
                with self.assertRaises(ValueError):
                    benchmark(requests, concurrency)

    def test_report_counts_http_and_transport_failures(self):
        report = summarize([(200, 1), (503, 10), (0, 100)], 2)
        self.assertEqual(report["errors"], 2)
        self.assertEqual(report["latency_ms"], {"p50": 10, "p95": 100, "p99": 100})
        self.assertEqual(report["requests_per_second"], 1.5)
