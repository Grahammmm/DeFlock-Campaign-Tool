"""Agency evidence pass: fetch the index-page links the seed already records and write one cited agency-evidence-v1 row
per agency - a verdict, its citations, and what was not checked. Never an uncited verdict: the verdict is a field
decided from the citations alone, never inferred from an agency's name, its county, or a neighbour's verdict.
  run --seed SEED --out NEWDIR [--agency SLUG ...] [--limit N]
      SEED is a schema_version 1 agency seed (data/agencies/us-ca.json). NEWDIR must be absolute, normalized,
      must not exist and its parent must be an existing, link-free directory; it is created 0700 only after every
      fetch, and the one row file is created exclusively (never overwritten). run writes nothing into the seed:
      promoting an evidenced row into the seed is a separate, attended decision.
  report NEWDIR
      Read a previous NEWDIR and print the table.
Verdicts (decided from citations alone): evidenced = at least one citation whose quote names an ALPR system or vendor
on a page under the agency's own domain; likely = a citation on a third-party or regional page; no-evidence = every
recorded link fetched, nothing found; unreachable = a recorded link could not be fetched; not-checked = no link
recorded. evidenced with zero citations is structurally impossible.
Fetching discipline: https only, one request per recorded link, no crawling, no following off-host redirects, a fixed
polite delay between requests, a per-run cap (--limit, default 25), a byte cap per response, a fixed timeout, a plain
identifying user agent. Nothing is logged in, nothing is submitted, no form, no search query, no robots-disallowed path.
Privacy: fetched bytes are not stored; only the hash, the status, and the quote. Quote context is checked against
shared email/phone/plate and conservative address/officer-name patterns; independent privacy review remains required.
Exit 0 clean, 1 any fetch failed (rows still written, failures named as unreachable), 2 usage. Output is PRIVATE and
REVIEW REQUIRED."""
import argparse, datetime, hashlib, importlib.util, json, os, re, sys, time, urllib.error, urllib.request
import urllib.robotparser
from pathlib import Path
from urllib.parse import urlsplit

REPO = Path(__file__).resolve().parents[1]
OUT_NAME = "agency-evidence.json"
SCHEMA = "agency-evidence-v1"
USER_AGENT = "DeFlock-Campaign-Tool agency-verify/1.0 (index-page evidence; github.com/Grahammmm/DeFlock-Campaign-Tool)"
TIMEOUT = 20  # fixed, seconds
BYTE_CAP = 2_000_000  # fixed, per response
DELAY = 1.0  # fixed polite delay between requests, seconds
LIMIT_DEFAULT = 25
QUOTE_MAX = 240  # a short span, never a page
# The fixed ALPR term list, longest-first on extraction; a citation names one of these.
# Generic words (matrix, cambridge, dax, speed camera) are left out: a bare common word must not become a citation.
ALPR_TERMS = ("flock safety", "automatic license plate recognition", "automatic license plate reader",
              "license plate reader", "license plate recognition", "alpr", "flock",
              "thruvue", "vigilant solutions", "motorola vigilant", "genetec autovu", "rekor")

class Refused(Exception):  # a usage or output-directory refusal: one line, nothing created, exit 2
    pass

def _load(name, path):  # a sibling loaded by path (like the other flock tools); never imported
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

def privacy():  # the one shared copy of the email/phone/plate patterns, beside the report and page lints
    return _load("_verify_flock_privacy", REPO / "tools" / "flock_privacy.py")

def host_of(url):  # the bare lowercase hostname of a URL, or ""
    try:
        return (urlsplit(url).hostname or "").rstrip(".").lower()
    except ValueError:
        return ""

def agency_domain(agency):
    # Ownership must be independently recorded, never inferred from the candidate link.
    dom = agency.get("official_domain")
    if isinstance(dom, str) and dom.strip():
        return dom.strip().lower()
    return ""

def alpr_terms_in(quote):  # the fixed term list, whole word, case-insensitive, longest-first
    text = quote.lower()
    return [term for term in sorted(ALPR_TERMS, key=len, reverse=True) if re.search(r"\b" + re.escape(term) + r"\b", text)]

TERM_RX = re.compile(r"\b(?:" + "|".join(re.escape(t) for t in sorted(ALPR_TERMS, key=len, reverse=True)) + r")\b", re.I)
PRIVATE_CONTEXT = (
    ("address", re.compile(r"\b\d{1,6}\s+(?:[\w.-]+\s+){1,5}(?:street|st|avenue|ave|road|rd|lane|ln|drive|dr|court|ct|way|boulevard|blvd)\b", re.I)),
    ("officer", re.compile(r"\b(?:officer|sergeant|detective|deputy|captain|chief|lieutenant|sgt|det|lt)\.?\s+[a-z]+", re.I)),
)

def quote_windows(text):
    for match in TERM_RX.finditer(text):
        start = max(0, match.start() - 24)
        end = min(len(text), match.end() + 96, start + QUOTE_MAX)
        yield text[start:end], len(text[:start].encode("utf-8")), match.group().lower(), text[max(0, start - QUOTE_MAX):end + QUOTE_MAX]

def extract_quote(text):
    found = next(quote_windows(text), None)
    return found[:3] if found else None

class Robots:
    """Cache path- and user-agent-aware plain-text policies per origin."""
    def __init__(self, fetcher):
        self._fetcher, self._cache = fetcher, {}
    def disallowed(self, url):
        origin = "https://" + urlsplit(url).netloc
        if origin not in self._cache:
            _, status, body, reason = self._fetcher(origin + "/robots.txt")
            parser = urllib.robotparser.RobotFileParser()
            # Missing policy permits; unreadable policy fails closed.
            text = body.decode("utf-8", "replace") if body is not None else "" if status == 404 else "User-agent: *\nDisallow: /"
            parser.parse(text.splitlines())
            self._cache[origin] = parser
            time.sleep(DELAY)
        return not self._cache[origin].can_fetch(USER_AGENT, url)

class RedirectRefused(Exception):
    pass

class RedirectGuard(urllib.request.HTTPRedirectHandler):
    def __init__(self, host, robots=None):
        self.host, self.robots = host, robots
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        reason = ("redirect off host" if host_of(newurl) != self.host else
                  "http scheme" if urlsplit(newurl).scheme != "https" else
                  "robots-disallowed" if self.robots and self.robots.disallowed(newurl) else None)
        if reason:
            if fp is not None:
                fp.close()
            raise RedirectRefused(reason)
        time.sleep(DELAY)
        return super().redirect_request(req, fp, code, msg, headers, newurl)

def fetch(url, robots=None, content_type="text/html"):
    """Fetch one recorded link once: https only, no off-host redirect, text/html only, <= BYTE_CAP bytes, honest UA, no retry.

    -> (final_url, http_status, body_bytes, reason) where exactly one of body/reason is set. reason names the failure."""
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        return url, None, None, "http scheme"
    if robots is not None and robots.disallowed(url):
        return url, None, None, "robots-disallowed"
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        opener = urllib.request.build_opener(RedirectGuard(host_of(url), robots))
        with opener.open(request, timeout=TIMEOUT) as response:
            final = response.geturl()
            start_host, end_host = host_of(url), host_of(final)
            if end_host != start_host:
                return final, response.status, None, "redirect off host"
            if urlsplit(final).scheme != "https":
                return final, response.status, None, "http scheme"
            ctype = (response.headers.get("Content-Type") or "").lower()
            if not ctype.startswith(content_type):
                return final, response.status, None, "content-type not text/html"
            body = response.read(BYTE_CAP + 1)
            if len(body) > BYTE_CAP:
                return final, response.status, None, "over byte cap"
            return final, response.status, body, None
    except RedirectRefused as err:
        return url, None, None, str(err)
    except urllib.error.HTTPError as err:
        err.close()
        return url, err.code, None, "http %d" % err.code
    except (urllib.error.URLError, OSError, ValueError) as err:
        return url, None, None, "network %s" % type(err).__name__

def clean_quote(quote, patterns):  # a quote matching the shared email/phone/plate patterns is rejected
    return [name for name, rx in patterns if rx.search(quote)]

def evidence_row(agency, patterns, now, fetcher=fetch):
    """One agency's evidence: fetch the recorded link once, collect the citations, decide the verdict later."""
    url = agency.get("records_url")
    if not isinstance(url, str) or not url.strip():
        return dict(agency=agency.get("agency_id"), name=agency.get("name"), verdict="not-checked",
                    citations=[], unreachable=[], not_checked=["no link recorded"], domain=agency_domain(agency))
    final, status, body, reason = fetcher(url)
    if reason is not None:
        return dict(agency=agency.get("agency_id"), name=agency.get("name"), verdict="unreachable",
                    citations=[], unreachable=[dict(url=url, reason=reason, http_status=status)], not_checked=[],
                    domain=agency_domain(agency))
    try:
        text = body.decode("utf-8", "strict")
    except UnicodeDecodeError:
        return dict(agency=agency.get("agency_id"), name=agency.get("name"), verdict="unreachable",
                    citations=[], unreachable=[dict(url=url, reason="invalid utf-8", http_status=status)],
                    not_checked=[], domain=agency_domain(agency))
    citations, rejected = [], []
    for quote, offset, term, context in quote_windows(text):
        cats = clean_quote(context, patterns) + clean_quote(context, PRIVATE_CONTEXT)
        if cats:
            rejected.append(dict(url=url, categories=cats))  # never the rejected text: it matched a personal-data pattern
        else:
            citations.append(dict(url=final, fetched_at=now, http_status=status,
                                  content_sha256=hashlib.sha256(body).hexdigest(),
                                  quote=quote, quote_offset=offset, alpr_terms=[term]))
            break
    return dict(agency=agency.get("agency_id"), name=agency.get("name"), verdict=None, citations=citations,
                unreachable=[], not_checked=[], rejected_quotes=rejected, domain=agency_domain(agency))

def decide(rows):  # the verdict is decided from the citations alone, never from the name
    for row in rows:
        cits, dom = row["citations"], row["domain"]
        if cits and dom and (host_of(cits[0]["url"]) == dom or host_of(cits[0]["url"]).endswith("." + dom)):
            row["verdict"] = "evidenced"
        elif cits:
            row["verdict"] = "likely"
        elif row["unreachable"]:
            row["verdict"] = "unreachable"
        elif row["not_checked"]:
            row["verdict"] = "not-checked"
        else:
            row["verdict"] = "no-evidence"
    return rows

class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Refused("usage: " + message)

def _parser():
    parser = _Parser(prog="agency_verify.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command")
    run = sub.add_parser("run", help="fetch and write one cited row per agency")
    run.add_argument("--seed", required=True)
    run.add_argument("--out", required=True)
    run.add_argument("--agency", action="append", default=[], metavar="SLUG")
    run.add_argument("--limit", type=int, default=LIMIT_DEFAULT, metavar="N")
    report = sub.add_parser("report", help="read a previous NEWDIR and print the table")
    report.add_argument("newdir")
    return parser

def _linked(path):
    return any(candidate.is_symlink() for candidate in (path, *path.parents))

def checked_out(out_path):  # OUT must be absolute, normalized, outside the repo, must not exist, parent link-free
    out = Path(out_path)
    if not out.is_absolute():
        raise Refused("out must be an absolute path")
    if Path(os.path.normpath(str(out))) != out:
        raise Refused("out must be a normalized path")
    if REPO == out or REPO in out.parents:
        raise Refused("out is inside the repository")
    if os.path.lexists(out):
        raise Refused("out directory already exists")
    if _linked(out.parent) or not out.parent.is_dir():
        raise Refused("out parent is not an existing link-free directory")
    return out

def run_seed(seed_path, out_path, agency_slugs, limit, fetcher=None):
    patterns = privacy().PATTERNS
    if limit < 1:
        raise Refused("usage: --limit must be at least 1")
    out = checked_out(out_path)  # OUT is checked before any fetch: a refusal creates nothing
    data = json.loads(Path(seed_path).read_text(encoding="utf-8"))
    if data.get("schema_version") != 1:
        raise Refused("seed schema_version is not 1")
    counties = data.get("counties")
    if not isinstance(counties, list):
        raise Refused("seed has no counties list")
    all_agencies = [a for county in counties for a in county.get("agencies", [])]
    if agency_slugs:
        slugs = set(agency_slugs)
        all_agencies = [a for a in all_agencies if a.get("agency_id") in slugs]
    selected = all_agencies[:limit]
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if fetcher is None:
        robots = Robots(lambda url: fetch(url, content_type="text/plain"))
        def checked_fetch(url):
            return fetch(url, robots=robots)
        fetcher = checked_fetch
    rows = []
    for i, agency in enumerate(selected):
        if i > 0:
            time.sleep(DELAY)
        rows.append(evidence_row(agency, patterns, now, fetcher))
    decide(rows)
    doc = dict(schema=SCHEMA, review_required=True, generated=now, limit=limit, rows=rows,
               counts=dict(agencies=len(rows),
                           evidenced=sum(r["verdict"] == "evidenced" for r in rows),
                           likely=sum(r["verdict"] == "likely" for r in rows),
                           no_evidence=sum(r["verdict"] == "no-evidence" for r in rows),
                           unreachable=sum(r["verdict"] == "unreachable" for r in rows),
                           not_checked=sum(r["verdict"] == "not-checked" for r in rows)))
    out.mkdir(mode=0o700)
    body = json.dumps(doc, sort_keys=True, indent=1, ensure_ascii=False) + "\n"
    fd = os.open(out / OUT_NAME, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(body)
    return doc

def report_dir(newdir):
    path = Path(newdir) / OUT_NAME
    if not path.is_file():
        raise Refused("no " + OUT_NAME + " in " + str(newdir))
    doc = json.loads(path.read_text(encoding="utf-8"))
    lines = ["agency | verdict | citations=N | unreachable=N | not-checked"]
    for row in doc.get("rows", []):
        nc = row.get("not_checked", [])
        nc_str = ", ".join(nc) if nc else "-"
        lines.append("%s | %s | citations=%d | unreachable=%d | not-checked %s" % (
            row.get("agency", "?"), row.get("verdict", "?"), len(row.get("citations", [])),
            len(row.get("unreachable", [])), nc_str))
    lines.append("evidence only; not a legal finding")
    return lines

def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        args = _parser().parse_args(argv)
    except Refused as err:
        print(str(err))
        return 2
    except SystemExit as err:  # --help prints and exits 0
        return err.code if isinstance(err.code, int) else 0
    if args.command == "run":
        try:
            doc = run_seed(args.seed, args.out, args.agency, args.limit)
        except Refused as err:
            print(str(err))
            return 2
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as err:
            print("io error: %s" % type(err).__name__)
            return 1
        counts = doc["counts"]
        print("rows=%d evidenced=%d likely=%d no_evidence=%d unreachable=%d not_checked=%d" % (
            counts["agencies"], counts["evidenced"], counts["likely"],
            counts["no_evidence"], counts["unreachable"], counts["not_checked"]))
        return 1 if counts["unreachable"] else 0  # rows are written either way; 1 names a failed fetch
    elif args.command == "report":
        try:
            lines = report_dir(args.newdir)
        except Refused as err:
            print(str(err))
            return 2
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as err:
            print("io error: %s" % type(err).__name__)
            return 1
        print("\n".join(lines))
        return 0
    else:
        print("usage: agency_verify.py run --seed SEED --out NEWDIR | report NEWDIR")
        return 2

if __name__ == "__main__":
    sys.exit(main())
