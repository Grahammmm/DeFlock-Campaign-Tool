"""BF-df-agency-verify: agency_verify.py fetches the recorded index-page links and writes one cited row per agency,
never an uncited verdict. A local HTTP double serves the pages; no real network is opened."""
import ast
import contextlib
import hashlib
import importlib.util
import io
import json
import re
import shutil
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools" / "agency_verify.py"
BANNED = re.compile(r"violat|unlawful|illegal|non-?complian|breach", re.I)

def load_tool():
    spec = importlib.util.spec_from_file_location("_agency_verify", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

MOD = load_tool()

# --- a local HTTP double: it serves fixed pages per path and records every request time --------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "Double/1.0"
    def log_message(self, *args):
        pass
    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
    def do_GET(self):
        srv = self.server
        srv.requests.append((self.path, time.monotonic()))
        page = srv.pages.get(self.path)
        if page is None:
            self._send(404, "text/html", b"<html><body>gone</body></html>")
            return
        code, ctype, body = page
        if not isinstance(body, bytes):
            body = body()
        self._send(code, ctype, body)

class Double:
    def __init__(self, pages):
        self.pages = pages
        self.requests = []
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.pages = pages
        self.httpd.requests = self.requests
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
    def start(self):
        self.thread.start()
    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

def make_double():
    double = Double({})
    double.start()
    return double

def seed_doc(agencies):
    return dict(schema_version=1, jurisdiction="us-test", generated="2026-11-03",
                counties=[dict(county_fips="09999", name="Test", seat="Test", agencies=agencies)])

def agency(agency_id, name, url=None, domain=None):
    a = dict(agency_id=agency_id, name=name, kind="police", jurisdiction_name="Test County",
             place_fips=None, records_url=url, records_email=None,
             portal=dict(vendor="unknown", url=None), flock_transparency_slug=None,
             muckrock_agency_id=None, verified=False,
             sources=[dict(title="t", url=None, accessed="2026-11-03")])
    if domain is not None:
        a["official_domain"] = domain
    return a

class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.seed = self.tmp / "seed.json"
        self.out = self.tmp / "out"
    def write_seed(self, agencies):
        self.seed.write_text(json.dumps(seed_doc(agencies)), encoding="utf-8")
        return self.seed
    def local_fetch(self, double):
        """A fetcher that hits the http double but reports the final URL as https, so the https-only logic passes."""
        base_host = "127.0.0.1:%d" % double.port
        def fetcher(url):
            path = urlsplit(url).path or "/"
            req = urllib.request.Request("http://%s%s" % (base_host, path),
                                         headers={"User-Agent": MOD.USER_AGENT})
            try:
                with urllib.request.urlopen(req, timeout=MOD.TIMEOUT) as resp:
                    tail = resp.geturl().split(base_host, 1)[1]
                    final = "https://%s%s" % (base_host, tail)
                    ctype = (resp.headers.get("Content-Type") or "").lower()
                    if not ctype.startswith("text/html"):
                        return final, resp.status, None, "content-type not text/html"
                    body = resp.read(MOD.BYTE_CAP + 1)
                    if len(body) > MOD.BYTE_CAP:
                        return final, resp.status, None, "over byte cap"
                    return final, resp.status, body, None
            except urllib.error.HTTPError as err:
                err.close()
                return url, err.code, None, "http %d" % err.code
            except (urllib.error.URLError, OSError, ValueError) as err:
                return url, None, None, "network %s" % type(err).__name__
        return fetcher
    def run_main(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = MOD.main(list(args))
        return code, out.getvalue(), err.getvalue()

class VerdictTests(Base):
    def test_five_verdicts_from_their_own_fixtures(self):
        double = make_double()
        self.addCleanup(double.stop)
        double.pages["/own"] = (200, "text/html", b"<html><body>we run <b>Flock Safety</b> cameras</body></html>")
        double.pages["/plain"] = (200, "text/html", b"<html><body>just a town</body></html>")
        # a-own: page on own host (double host) + domain = double host => evidenced
        # a-third: page on the double host but domain = a different host => likely
        # a-plain: no ALPR term => no-evidence
        # a-gone: 404 => unreachable
        # a-none: no link => not-checked
        agencies = [
            agency("a-own", "Own PD", "https://127.0.0.1:%d/own" % double.port, domain="127.0.0.1"),
            agency("a-third", "Third PD", "https://127.0.0.1:%d/own" % double.port, domain="else.example"),
            agency("a-plain", "Plain PD", "https://127.0.0.1:%d/plain" % double.port),
            agency("a-gone", "Gone PD", "https://127.0.0.1:%d/nope" % double.port),
            agency("a-none", "None PD", None),
        ]
        self.write_seed(agencies)
        fetcher = self.local_fetch(double)
        doc = MOD.run_seed(str(self.seed), str(self.out), [], 25, fetcher=fetcher)
        by_id = {r["agency"]: r for r in doc["rows"]}
        self.assertEqual(by_id["a-own"]["verdict"], "evidenced")
        self.assertEqual(by_id["a-third"]["verdict"], "likely")
        self.assertEqual(by_id["a-plain"]["verdict"], "no-evidence")
        self.assertEqual(by_id["a-gone"]["verdict"], "unreachable")
        self.assertEqual(by_id["a-none"]["verdict"], "not-checked")
        for aid in ("a-own", "a-third"):
            cit = by_id[aid]["citations"]
            self.assertGreaterEqual(len(cit), 1, aid)
            self.assertTrue(cit[0]["url"] and cit[0]["content_sha256"] and cit[0]["quote"], aid)
        self.assertEqual(self.seed.read_bytes(), json.dumps(seed_doc(agencies)).encode())

    def test_evidenced_requires_same_host_third_party_is_likely(self):
        # same host => evidenced; different host => likely, with the same page content
        own = agency("a-own", "Own", "https://own.example/own", domain="own.example")
        third = agency("a-third", "Third", "https://own.example/own", domain="third.example")
        self.write_seed([own, third])
        def fetcher(url):
            return url, 200, b"<html><body>Flock Safety</body></html>", None
        doc = MOD.run_seed(str(self.seed), str(self.out), [], 25, fetcher=fetcher)
        by_id = {r["agency"]: r for r in doc["rows"]}
        self.assertEqual(by_id["a-own"]["verdict"], "evidenced")
        self.assertEqual(by_id["a-third"]["verdict"], "likely")

    def test_staff_directory_quote_rejected_and_verdict_falls_back(self):
        # the only ALPR mention sits in a staff line with a phone number => quote rejected => no-evidence
        double = make_double()
        self.addCleanup(double.stop)
        page = b"<html><body><p>Officer Jane, Flock Safety contact, (805) 555-1234</p></body></html>"
        double.pages["/staff"] = (200, "text/html", page)
        self.write_seed([agency("a-staff", "Staff PD", "https://127.0.0.1:%d/staff" % double.port)])
        doc = MOD.run_seed(str(self.seed), str(self.out), [], 25, fetcher=self.local_fetch(double))
        row = doc["rows"][0]
        self.assertEqual(row["verdict"], "no-evidence")
        self.assertEqual(len(row["citations"]), 0)
        self.assertEqual(len(row["rejected_quotes"]), 1)
        self.assertIn("phone", row["rejected_quotes"][0]["categories"])
        self.assertNotIn("quote", row["rejected_quotes"][0])  # the refused text is never stored
        self.assertNotIn("555", (self.out / MOD.OUT_NAME).read_text(encoding="utf-8"))

    def test_name_bypass_flock_safety_police_department_without_citation(self):
        # an agency NAMED "Flock Safety Police Department" whose page names nothing stays no-evidence
        double = make_double()
        self.addCleanup(double.stop)
        double.pages["/named"] = (200, "text/html", b"<html><body>just the department</body></html>")
        self.write_seed([agency("a-named", "Flock Safety Police Department", "https://127.0.0.1:%d/named" % double.port)])
        doc = MOD.run_seed(str(self.seed), str(self.out), [], 25, fetcher=self.local_fetch(double))
        self.assertEqual(doc["rows"][0]["verdict"], "no-evidence")

    def test_evidenced_with_zero_citations_is_impossible(self):
        double = make_double()
        self.addCleanup(double.stop)
        double.pages["/own"] = (200, "text/html", b"<html><body>Flock Safety</body></html>")
        self.write_seed([agency("a-own", "Own", "https://127.0.0.1:%d/own" % double.port, domain="127.0.0.1")])
        doc = MOD.run_seed(str(self.seed), str(self.out), [], 25, fetcher=self.local_fetch(double))
        for row in doc["rows"]:
            if row["verdict"] == "evidenced":
                self.assertGreaterEqual(len(row["citations"]), 1)

class UnreachableTests(Base):
    def test_off_host_redirect_is_unreachable(self):
        def fetcher(url):
            return "https://elsewhere.example/x", 200, b"<html><body>Flock</body></html>", "redirect off host"
        self.write_seed([agency("a-redir", "R", "https://start.example/x")])
        doc = MOD.run_seed(str(self.seed), str(self.out), [], 25, fetcher=fetcher)
        self.assertEqual(doc["rows"][0]["verdict"], "unreachable")
        self.assertEqual(doc["rows"][0]["unreachable"][0]["reason"], "redirect off host")

    def test_http_scheme_link_is_unreachable(self):
        def fetcher(url):
            return url, None, None, "http scheme"
        self.write_seed([agency("a-http", "H", "http://plain.example/x")])
        doc = MOD.run_seed(str(self.seed), str(self.out), [], 25, fetcher=fetcher)
        self.assertEqual(doc["rows"][0]["unreachable"][0]["reason"], "http scheme")

    def test_robots_disallowed_is_unreachable(self):
        def fetcher(url):
            return url, None, None, "robots-disallowed"
        self.write_seed([agency("a-robot", "Ro", "https://r.example/x")])
        doc = MOD.run_seed(str(self.seed), str(self.out), [], 25, fetcher=fetcher)
        self.assertEqual(doc["rows"][0]["unreachable"][0]["reason"], "robots-disallowed")

    def test_oversized_response_is_unreachable_no_partial_quote(self):
        def fetcher(url):
            return url, 200, None, "over byte cap"
        self.write_seed([agency("a-big", "B", "https://b.example/x")])
        doc = MOD.run_seed(str(self.seed), str(self.out), [], 25, fetcher=fetcher)
        self.assertEqual(doc["rows"][0]["unreachable"][0]["reason"], "over byte cap")
        self.assertEqual(doc["rows"][0]["citations"], [])

    def test_timeout_is_unreachable(self):
        def fetcher(url):
            return url, None, None, "network TimeoutError"
        self.write_seed([agency("a-time", "T", "https://t.example/x")])
        doc = MOD.run_seed(str(self.seed), str(self.out), [], 25, fetcher=fetcher)
        self.assertIn("TimeoutError", doc["rows"][0]["unreachable"][0]["reason"])

class LimitDelayTests(Base):
    def test_limit_respected_exactly(self):
        double = make_double()
        self.addCleanup(double.stop)
        double.pages["/p"] = (200, "text/html", b"<html><body>plain</body></html>")
        agencies = [agency("a-%d" % i, "A%d" % i, "https://127.0.0.1:%d/p" % double.port) for i in range(5)]
        self.write_seed(agencies)
        doc = MOD.run_seed(str(self.seed), str(self.out), [], 3, fetcher=self.local_fetch(double))
        self.assertEqual(len(doc["rows"]), 3)
        self.assertEqual(doc["limit"], 3)
        times = [t for _, t in double.requests]
        if len(times) >= 2:
            gaps = [times[i+1] - times[i] for i in range(len(times)-1)]
            self.assertTrue(any(g >= 0.9 * MOD.DELAY for g in gaps), gaps)

    def test_agency_filter_selects_only_named(self):
        double = make_double()
        self.addCleanup(double.stop)
        double.pages["/p"] = (200, "text/html", b"<html><body>plain</body></html>")
        agencies = [agency("a-keep", "Keep", "https://127.0.0.1:%d/p" % double.port),
                    agency("a-drop", "Drop", None)]
        self.write_seed(agencies)
        doc = MOD.run_seed(str(self.seed), str(self.out), ["a-keep"], 25, fetcher=self.local_fetch(double))
        self.assertEqual([r["agency"] for r in doc["rows"]], ["a-keep"])

class RerunTests(Base):
    def test_rerun_unchanged_pages_identical_except_fetched_at(self):
        double = make_double()
        self.addCleanup(double.stop)
        body = b"<html><body>Flock Safety</body></html>"
        double.pages["/own"] = (200, "text/html", body)
        self.write_seed([agency("a-own", "Own", "https://127.0.0.1:%d/own" % double.port, domain="127.0.0.1")])
        fetcher = self.local_fetch(double)
        d1 = MOD.run_seed(str(self.seed), str(self.tmp / "o1"), [], 25, fetcher=fetcher)
        d2 = MOD.run_seed(str(self.seed), str(self.tmp / "o2"), [], 25, fetcher=fetcher)
        r1, r2 = d1["rows"][0], d2["rows"][0]
        self.assertEqual(r1["agency"], r2["agency"])
        self.assertEqual(r1["verdict"], r2["verdict"])
        self.assertEqual(r1["citations"][0]["content_sha256"], r2["citations"][0]["content_sha256"])
        self.assertEqual(r1["citations"][0]["quote"], r2["citations"][0]["quote"])
        self.assertEqual(hashlib.sha256(body).hexdigest(), r1["citations"][0]["content_sha256"])

    def test_changed_page_changes_content_sha256(self):
        double = make_double()
        self.addCleanup(double.stop)
        body1 = b"<html><body>Flock Safety one</body></html>"
        double.pages["/own"] = (200, "text/html", body1)
        self.write_seed([agency("a-own", "Own", "https://127.0.0.1:%d/own" % double.port, domain="127.0.0.1")])
        fetcher = self.local_fetch(double)
        d1 = MOD.run_seed(str(self.seed), str(self.tmp / "o1"), [], 25, fetcher=fetcher)
        sha1 = d1["rows"][0]["citations"][0]["content_sha256"]
        body2 = b"<html><body>Flock Safety two</body></html>"
        double.pages["/own"] = (200, "text/html", body2)
        d2 = MOD.run_seed(str(self.seed), str(self.tmp / "o2"), [], 25, fetcher=fetcher)
        sha2 = d2["rows"][0]["citations"][0]["content_sha256"]
        self.assertNotEqual(sha1, sha2)
        self.assertEqual(sha2, hashlib.sha256(body2).hexdigest())

class OutRefusalTests(Base):
    def test_out_exists_refused_nothing_created(self):
        double = make_double()
        self.addCleanup(double.stop)
        double.pages["/p"] = (200, "text/html", b"<html><body>plain</body></html>")
        self.write_seed([agency("a", "A", "https://127.0.0.1:%d/p" % double.port)])
        self.out.mkdir()
        fetcher = self.local_fetch(double)
        with self.assertRaises(MOD.Refused):
            MOD.run_seed(str(self.seed), str(self.out), [], 25, fetcher=fetcher)
        self.assertFalse((self.out / MOD.OUT_NAME).exists())

    def test_out_relative_refused(self):
        double = make_double()
        self.addCleanup(double.stop)
        double.pages["/p"] = (200, "text/html", b"<html><body>plain</body></html>")
        self.write_seed([agency("a", "A", "https://127.0.0.1:%d/p" % double.port)])
        with self.assertRaises(MOD.Refused):
            MOD.run_seed(str(self.seed), "relative-out", [], 25, fetcher=self.local_fetch(double))

    def test_parent_symlink_refused(self):
        double = make_double()
        self.addCleanup(double.stop)
        double.pages["/p"] = (200, "text/html", b"<html><body>plain</body></html>")
        self.write_seed([agency("a", "A", "https://127.0.0.1:%d/p" % double.port)])
        real = self.tmp / "real"
        real.mkdir()
        linkparent = self.tmp / "linkparent"
        linkparent.symlink_to(real, target_is_directory=True)
        out = linkparent / "out"
        fetcher = self.local_fetch(double)
        with self.assertRaises(MOD.Refused):
            MOD.run_seed(str(self.seed), str(out), [], 25, fetcher=fetcher)
        self.assertFalse(out.exists())

    def test_out_dir_and_file_modes(self):
        double = make_double()
        self.addCleanup(double.stop)
        double.pages["/p"] = (200, "text/html", b"<html><body>plain</body></html>")
        self.write_seed([agency("a", "A", "https://127.0.0.1:%d/p" % double.port)])
        MOD.run_seed(str(self.seed), str(self.out), [], 25, fetcher=self.local_fetch(double))
        self.assertEqual(self.out.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.out / MOD.OUT_NAME).stat().st_mode & 0o777, 0o600)

class ReportTests(Base):
    def test_report_prints_table_and_footer(self):
        double = make_double()
        self.addCleanup(double.stop)
        double.pages["/own"] = (200, "text/html", b"<html><body>Flock Safety</body></html>")
        double.pages["/plain"] = (200, "text/html", b"<html><body>plain</body></html>")
        self.write_seed([agency("a-own", "Own", "https://127.0.0.1:%d/own" % double.port, domain="127.0.0.1"),
                         agency("a-plain", "Plain", "https://127.0.0.1:%d/plain" % double.port)])
        MOD.run_seed(str(self.seed), str(self.out), [], 25, fetcher=self.local_fetch(double))
        code, out, err = self.run_main("report", str(self.out))
        self.assertEqual((code, err), (0, ""), out)
        lines = out.splitlines()
        self.assertEqual(lines[0], "agency | verdict | citations=N | unreachable=N | not-checked")
        self.assertIn("a-own | evidenced | citations=1", lines[1])
        self.assertIn("a-plain | no-evidence | citations=0", lines[2])
        self.assertEqual(lines[-1], "evidence only; not a legal finding")

    def test_report_refuses_missing_dir(self):
        code, out, err = self.run_main("report", str(self.tmp / "nope"))
        self.assertEqual(code, 2)
        self.assertIn("no agency-evidence.json", out)

class WordingTests(Base):
    def test_no_banned_word_in_generated_output(self):
        double = make_double()
        self.addCleanup(double.stop)
        double.pages["/own"] = (200, "text/html", b"<html><body>Flock Safety</body></html>")
        double.pages["/plain"] = (200, "text/html", b"<html><body>plain</body></html>")
        self.write_seed([agency("a-own", "Own", "https://127.0.0.1:%d/own" % double.port, domain="127.0.0.1"),
                         agency("a-plain", "Plain", "https://127.0.0.1:%d/plain" % double.port),
                         agency("a-none", "None", None)])
        MOD.run_seed(str(self.seed), str(self.out), [], 25, fetcher=self.local_fetch(double))
        code, out, err = self.run_main("report", str(self.out))
        self.assertEqual(code, 0)
        self.assertFalse(BANNED.search(out), out)
        self.assertTrue(out.rstrip("\n").endswith("evidence only; not a legal finding"))

class StaticTests(unittest.TestCase):
    def test_no_form_post_cookie_credential_subprocess(self):
        text = TOOL.read_text(encoding="utf-8")
        tree = ast.parse(text)
        found = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                found.add(node.module.split(".")[0])
        self.assertEqual(found & {"smtplib", "imaplib", "socket", "requests", "subprocess", "sqlite3"}, set())
        self.assertIn("urllib", found)
        self.assertNotRegex(text, r"\bPOST\b|urlopen.*data=|http\.cookiejar|CookieJar|api_key|access_token|client_secret")
        self.assertNotIn("subprocess", text)

    def test_main_help_and_usage_exit_codes(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(MOD.main(["--help"]), 0)
        self.assertIn("agency_verify.py", out.getvalue())
        for argv in ([], ["run"], ["run", "--seed", "s"], ["run", "--seed", "s", "--out", "o"],
                     ["report"], ["bogus"]):
            with self.subTest(argv=argv), contextlib.redirect_stdout(io.StringIO()) as o, \
                    contextlib.redirect_stderr(io.StringIO()) as e:
                self.assertEqual(MOD.main(argv), 2)

class ExitCodeTests(Base):
    def test_exit_1_when_a_fetch_failed_rows_still_written(self):
        # an http:// link is refused before any request is made, so no network is opened
        self.write_seed([agency("a-http", "Plain PD", "http://example.invalid/page"), agency("a-none", "No Link PD")])
        code, out, err = self.run_main("run", "--seed", str(self.seed), "--out", str(self.out))
        self.assertEqual(code, 1, out + err)
        doc = json.loads((self.out / MOD.OUT_NAME).read_text(encoding="utf-8"))
        self.assertEqual([r["verdict"] for r in doc["rows"]], ["unreachable", "not-checked"])
        self.assertEqual(doc["rows"][0]["unreachable"][0]["reason"], "http scheme")

    def test_exit_0_when_nothing_failed(self):
        self.write_seed([agency("a-none", "No Link PD")])
        code, out, err = self.run_main("run", "--seed", str(self.seed), "--out", str(self.out))
        self.assertEqual(code, 0, out + err)

    def test_limit_below_one_is_refused_and_creates_nothing(self):
        self.write_seed([agency("a-none", "No Link PD")])
        code, out, err = self.run_main("run", "--seed", str(self.seed), "--out", str(self.out), "--limit", "0")
        self.assertEqual(code, 2)
        self.assertFalse(self.out.exists())

class SeedUntouchedTests(Base):
    def test_seed_byte_identical_after_every_run(self):
        double = make_double()
        self.addCleanup(double.stop)
        double.pages["/own"] = (200, "text/html", b"<html><body>Flock Safety</body></html>")
        self.write_seed([agency("a-own", "Own", "https://127.0.0.1:%d/own" % double.port, domain="127.0.0.1")])
        before = self.seed.read_bytes()
        MOD.run_seed(str(self.seed), str(self.out), [], 25, fetcher=self.local_fetch(double))
        self.assertEqual(self.seed.read_bytes(), before)

if __name__ == "__main__":
    unittest.main()
