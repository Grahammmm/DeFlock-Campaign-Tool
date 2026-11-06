"""Credential-free anonymous edge sweep of a hosted records console.

    python -m campaign_tool.records.console_edge_smoke https://<host> [--mode access|isolation]

Sends only anonymous requests (plus two junk-credential rounds that must be
refused) and reports headers/status only; bodies are never printed. There is
no option to supply a cookie, header or token. Run against the real host by
the verify card, never by CI.
"""
import argparse
import http.client
import re
import sys
from urllib.parse import urlsplit

CONSOLE_CSP = "base-uri 'none'; frame-ancestors 'none'"
FINGERPRINT_SERVER = "RecordsPanel"
FENCE_HEADER = "x-deflock-write-fence"
ACCESS_SUFFIX = ".cloudflareaccess.com"
MAX_BODY = 64 * 1024
URL_RE = re.compile(r"https://[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+")

GET_PATHS = ("/", "/board", "/board?filter=overdue", "/agency?agency=x", "/documents", "/next-steps",
             "/oversight", "/public-updates", "/manual-event", "/send?id=" + "0" * 64, "/healthz",
             "/does-not-exist", "/%2e%2e/etc/passwd")
POST_PATHS = ("/approvals/approve", "/approvals/reject", "/approvals/edit", "/manual-event",
              "/public-updates/off", "/public-updates/kill", "/send")
JUNK = {"Cookie": "CF_Authorization=x.y.z", "Cf-Access-Jwt-Assertion": "x.y.z"}


def _connect(scheme, host, timeout):
    """Connection factory; tests replace this, the CLI cannot."""
    if scheme == "https":
        return http.client.HTTPSConnection(host, timeout=timeout)
    return http.client.HTTPConnection(host, timeout=timeout)


def _probes():
    base = [("GET", p, None) for p in GET_PATHS] + [("HEAD", "/", None), ("OPTIONS", "/", None)]
    base += [("POST", p, "a=b") for p in POST_PATHS]
    plain = [("https", m, p, b, {}) for m, p, b in base]
    junk = [("https", m, p, b, dict(JUNK)) for m, p, b in base]
    insecure = [("http", "GET", p, None, {}) for p in ("/", "/board")]
    return plain + junk + insecure


def _send(scheme, host, method, path, body, extra, timeout):
    """Return (status, headers) or (None, error-name). Never follows redirects, never keeps a body."""
    headers = dict(extra)
    if body is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    try:
        conn = _connect(scheme, host, timeout)
        try:
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            try:
                resp.read(MAX_BODY)
            except (OSError, http.client.HTTPException):
                pass
            return resp.status, {k.lower(): v for k, v in resp.getheaders()}
        finally:
            conn.close()
    except (OSError, http.client.HTTPException) as error:
        return None, type(error).__name__


def _fingerprint(headers):
    found = []
    if CONSOLE_CSP in headers.get("content-security-policy", ""):
        found.append("console-csp")
    if FINGERPRINT_SERVER in headers.get("server", ""):
        found.append("server")
    if FENCE_HEADER in headers:
        found.append("write-fence")
    return found


def _location(headers):
    value = headers.get("location", "")
    if not value:
        return None, ""
    parts = urlsplit(value)
    return parts, (parts.hostname or "")


def _refused_by_access(status, headers):
    if status in (401, 403):
        return True
    if status in (302, 303):
        parts, host = _location(headers)
        return bool(parts) and parts.scheme == "https" and host.endswith(ACCESS_SUFFIX)
    return False


def judge(mode, host, scheme, path, status, headers):
    """Verdict word for one probe: ok, FAIL, or unreachable (isolation only)."""
    if status is None:
        return "unreachable" if mode == "isolation" else "FAIL"
    if _fingerprint(headers):
        return "FAIL"
    if mode == "isolation":
        if 200 <= status < 300:
            return "FAIL"
        return "unreachable" if status >= 500 else "ok"
    if _refused_by_access(status, headers):
        return "ok"
    if scheme == "http" and status in (301, 302, 307, 308):
        parts, loc_host = _location(headers)
        if parts and parts.scheme == "https" and loc_host == host and (parts.path or "/") + (
                "?" + parts.query if parts.query else "") == path:
            return "ok"
    return "FAIL"


def sweep(host, mode, timeout, out):
    failures = 0
    out.write("ISOLATION CHECK (not an Access check)\n" if mode == "isolation" else "ACCESS CHECK\n")
    for scheme, method, path, body, extra in _probes():
        status, headers = _send(scheme, host, method, path, body, extra, timeout)
        verdict = judge(mode, host, scheme, path, status, headers if status is not None else {})
        shown_path = path if scheme == "https" else "http://" + host + path
        loc_host = _location(headers)[1] if status is not None else ""
        if verdict == "FAIL":
            failures += 1
        out.write("%s %s %s %s %s\n" % (method, shown_path, status if status is not None else "ERR:" + headers,
                                        loc_host or "-", verdict))
    out.write("PASS\n" if not failures else "FAIL %d probes\n" % failures)
    return 1 if failures else 0


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise SystemExit(2)


def main(argv=None, out=None):
    out = out or sys.stdout
    parser = _Parser(prog="console_edge_smoke", allow_abbrev=False, add_help=False)
    parser.add_argument("url")
    parser.add_argument("--mode", choices=("access", "isolation"), default="access")
    parser.add_argument("--timeout", type=float, default=15)
    try:
        args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    except SystemExit:
        out.write("usage: console_edge_smoke https://<host> [--mode access|isolation] [--timeout N]\n")
        return 2
    if not URL_RE.fullmatch(args.url) or not 0 < args.timeout <= 120:
        out.write("usage: URL must be https://<lowercase dns name> with no path or port\n")
        return 2
    return sweep(args.url[len("https://"):], args.mode, args.timeout, out)


if __name__ == "__main__":
    sys.exit(main())
