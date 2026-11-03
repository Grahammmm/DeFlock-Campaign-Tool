"""Bounded synthetic first-party starter HTTP benchmark; loopback only."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from functools import partial
from http.client import HTTPConnection
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import math
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace

from campaign_tool.cli import initialize, build
from tools.synthetic_content import write_synthetic_content

SITE_SUFFIXES = (".html", ".xml", ".js", ".css")
MAX_SITE_PATHS = 64


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def site_paths(public):
    """Sorted URL paths of the built .html/.xml/.js/.css files, at most MAX_SITE_PATHS."""
    files = sorted(path.relative_to(public).as_posix() for path in public.rglob("*")
                   if path.is_file() and not path.is_symlink() and path.suffix in SITE_SUFFIXES)
    return ["/" + name for name in files][:MAX_SITE_PATHS]


def page_summaries(public, paths, samples):
    """Per-path bytes on disk, request/error counts and p50/p95 (None when never requested)."""
    pages = []
    for index, path in enumerate(paths):
        hits = samples[index::len(paths)]
        latency = summarize(hits, 1)["latency_ms"] if hits else {"p50": None, "p95": None}
        pages.append({"path": path, "bytes": (public / path.lstrip("/")).stat().st_size,
                      "requests": len(hits), "errors": sum(status != 200 for status, _ in hits),
                      "p50": latency["p50"], "p95": latency["p95"]})
    return pages


def summarize(samples, elapsed):
    latencies = sorted(milliseconds for status, milliseconds in samples)
    def percentile(percent):
        return latencies[max(0, math.ceil(len(latencies) * percent) - 1)]
    errors = sum(status != 200 for status, _ in samples)
    return {"requests": len(samples), "errors": errors,
            "error_rate": errors / len(samples), "elapsed_seconds": elapsed,
            "requests_per_second": len(samples) / elapsed,
            "latency_ms": {"p50": percentile(.50), "p95": percentile(.95),
                           "p99": percentile(.99)}}


def benchmark(requests=100, concurrency=2, site=False):
    if type(requests) is not int or not 1 <= requests <= 1000:
        raise ValueError("requests must be 1..1000")
    if type(concurrency) is not int or not 1 <= concurrency <= 8:
        raise ValueError("concurrency must be 1..8")
    with tempfile.TemporaryDirectory(prefix="synthetic-starter-capacity-") as folder:
        root = Path(folder) / "campaign"
        with redirect_stdout(io.StringIO()):
            initialize(SimpleNamespace(name="Synthetic capacity fixture", county="Alameda",
                                       state="CA", location=None), root)
            if site:
                write_synthetic_content(root, "capacity")
            build(root, check=True)
        paths = site_paths(root / "public") if site else ["/index.html"]
        server = ThreadingHTTPServer(("127.0.0.1", 0),
            partial(QuietHandler, directory=str(root / "public")))
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def request(number):
            path = paths[number % len(paths)]
            started = time.perf_counter()
            connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
            status = 0
            try:
                connection.request("GET", path)
                response = connection.getresponse()
                body = response.read(2 * 1024 * 1024 + 1)
                if len(body) <= 2 * 1024 * 1024:
                    status = response.status
            except (OSError, TimeoutError):
                pass
            finally:
                connection.close()
            return status, (time.perf_counter() - started) * 1000
        started = time.perf_counter()
        try:
            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                samples = list(pool.map(request, range(requests)))
            elapsed = time.perf_counter() - started
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
        extra = {}
        if site:
            pages = page_summaries(root / "public", paths, samples)
            extra = {"schema": "synthetic-site-capacity-v1", "target": "generated-synthetic-site-pages-on-loopback",
                     "pages": pages, "failed_paths": [page["path"] for page in pages if page["errors"]]}
    return {"schema": "synthetic-starter-capacity-v1", "synthetic_only": True,
            "target": "generated-starter-index-on-loopback", "concurrency": concurrency,
            "external_requests": 0, "production_capacity_validated": False,
            **summarize(samples, elapsed), **extra}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requests", type=int, default=100)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--site", action="store_true", help="load every built page of a synthetic content campaign")
    args = parser.parse_args(argv)
    report = benchmark(args.requests, args.concurrency, site=args.site)
    print(json.dumps(report, indent=2))
    return int(report["errors"] != 0)


if __name__ == "__main__":
    raise SystemExit(main())
