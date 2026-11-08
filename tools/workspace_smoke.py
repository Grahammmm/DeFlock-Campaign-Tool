#!/usr/bin/env python3
"""Anonymous, credential-free smoke probe of the deployed admin console.

    python3 tools/workspace_smoke.py [--mode edge|app] URL

No credential flags exist: every request is sent with no cookie, token or
Authorization header, and redirects are never followed.

  edge (default; the deployed host behind Cloudflare Access)
      PASS only if every probed path, /healthz included, answers an Access
      redirect (302/303 to *.cloudflareaccess.com) or 401/403. Any 200 is a
      FAIL: it means Access is not in front of the host.
  app (local rehearsal, or the Worker before Access is switched on)
      PASS only if every path except /healthz answers 401/403/404/503, and
      /healthz answers 200 with a JSON object whose keys are a subset of
      HEALTH_KEYS and whose values are booleans, integers or "on"/"off".
      Any other key, string value or non-JSON body is a FAIL.

Exit status 0 on PASS, 1 on FAIL, 2 on a usage error.
"""
import argparse
import http.client
import json
import sys
from urllib.parse import urlsplit

PATHS = (
    "/",
    "/healthz",
    "/status",
    "/api/me",
    "/api/export.json",
    "/api/runner/export.json",
    "/dl/" + "0" * 64 + "?exp=1&sig=00",
)
HEALTH_KEYS = frozenset(
    {"ok", "campaign", "d1_ok", "migrations_applied", "access_configured", "outbound"}
)
DENY_EDGE = (401, 403)
DENY_APP = (401, 403, 404, 503)
REDIRECTS = (302, 303)
ACCESS_SUFFIX = ".cloudflareaccess.com"
TIMEOUT = 15
MAX_BODY = 65536


def fetch(base, path, timeout=TIMEOUT):
    """One anonymous GET. Returns (status, location, body_bytes); no redirects."""
    parts = urlsplit(base)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("URL must be http(s)://host[:port]")
    cls = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
    conn = cls(parts.hostname, parts.port, timeout=timeout)
    try:
        conn.request("GET", (parts.path.rstrip("/") + path), headers={
            "Accept": "*/*", "User-Agent": "workspace-smoke/1"})
        resp = conn.getresponse()
        return resp.status, resp.getheader("Location") or "", resp.read(MAX_BODY)
    finally:
        conn.close()


def is_access_redirect(status, location):
    if status not in REDIRECTS or not location:
        return False
    try:
        host = (urlsplit(location).hostname or "").lower()
    except ValueError:
        return False  # malformed Location is not an Access redirect
    return host.endswith(ACCESS_SUFFIX) and len(host) > len(ACCESS_SUFFIX)


def check_edge(path, status, location, body):
    """Return None when the response is acceptable, else a reason."""
    if is_access_redirect(status, location) or status in DENY_EDGE:
        return None
    return "answered %d, expected an Access redirect or 401/403" % status


def health_problem(body):
    try:
        data = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return "body is not JSON"
    if not isinstance(data, dict):
        return "body is not a JSON object"
    extra = sorted(set(data) - HEALTH_KEYS)
    if extra:
        return "unexpected key(s): " + ", ".join(extra)
    for key, val in sorted(data.items()):
        if isinstance(val, (bool, int)) or val in ("on", "off") and isinstance(val, str):
            continue
        return "value of %s is not a boolean, integer or on/off" % key
    return None


def check_app(path, status, location, body):
    if path == "/healthz":
        if status != 200:
            return "answered %d, expected 200" % status
        problem = health_problem(body)
        return "200 but " + problem if problem else None
    if status in DENY_APP:
        return None
    return "answered %d, expected 401/403/404/503" % status


def run(base, mode="edge", fetcher=fetch):
    """Probe every path. Returns (ok, [(path, ok, detail)])."""
    check = check_edge if mode == "edge" else check_app
    parts = urlsplit(base)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("URL must be http(s)://host[:port]")
    results = []
    for path in PATHS:
        label = path.split("?")[0] if path.startswith("/dl/") else path
        label = "/dl" if label.startswith("/dl/") else label
        try:
            status, location, body = fetcher(base, path)
        except (OSError, http.client.HTTPException, ValueError) as exc:
            results.append((label, False, "request failed: %s" % type(exc).__name__))
            continue
        reason = check(path, status, location, body)
        results.append((label, reason is None, "HTTP %d" % status if reason is None else reason))
    return all(ok for _, ok, _ in results), results


def main(argv=None, out=sys.stdout):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--mode", choices=("edge", "app"), default="edge")
    ap.add_argument("url", help="base URL, e.g. https://example.workers.dev")
    args = ap.parse_args(argv)
    try:
        ok, results = run(args.url, args.mode)
    except ValueError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    for path, good, detail in results:
        print("%s %-26s %s" % ("ok  " if good else "FAIL", path, detail), file=out)
    print("%s (%s mode)" % ("PASS" if ok else "FAIL", args.mode), file=out)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
