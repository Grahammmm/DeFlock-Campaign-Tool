"""Flock monitor: scan official agency index pages listed in WATCHLIST.csv into a private MONITOR.csv, and write a daily
digest. Index pages only: links (PDFs included) are recorded by URL and never fetched."""
import argparse, contextlib, csv, datetime, fcntl, hashlib, html.parser, os, re, sys, tempfile, urllib.error, urllib.request
from pathlib import Path
from urllib.parse import unquote, urldefrag, urljoin, urlsplit

REPO = Path(__file__).resolve().parents[1]
WATCH_HEADER = ["agency", "kind", "url", "query", "official_domain", "last_seen_hash"]
MONITOR_HEADER = ["item_id", "agency", "watch_kind", "url", "title", "page_sha256", "seen_at", "kind", "status"]
LEADS_HEADER = ["item_id", "agency", "first_seen", "state", "state_at", "state_by"]  # v1
LOG_HEADER = ["scan_date", "agency", "url", "outcome", "reason"]  # v1; outcome ok|unchanged|refused
LOG_KEEP = 60  # SCAN-LOG.csv rows kept per (agency, url); older rows are dropped when the log is rewritten
MOVES = {"new": ("requested", "ignored"), "ignored": ("requested",), "requested": ()}  # requested is final
KINDS = {"agenda", "minutes", "news", "social"}
CAP = 2_000_000
USER_AGENT = "DeFlock-Campaign-Tool flock-monitor/1.0 (index-page check; github.com/Grahammmm/DeFlock-Campaign-Tool)"
DOMAIN = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+")
COUNTS = ("scanned", "unchanged", "new_items", "refused", "offdomain_links", "skipped_social")

class Refused(Exception): """A fetch the scope guard will not make or accept. Recorded as data and never retried."""
class Fail(Exception): """Validation or IO error (exit 1)."""

def slug(agency):  # F1 rule
    return re.sub(r"[^a-z0-9]+", "-", agency.lower()).strip("-")

def absolute(href, base=""):  # fragment-free absolute URL, or "" when the URL parser rejects it (e.g. "https://[x")
    try:
        url = urldefrag(urljoin(base, href or ""))[0]
        return url if urlsplit(url) else ""  # urlsplit raises ValueError on a malformed URL
    except ValueError:
        return ""

def in_scope(url, domain):  # https only, host == official domain or a subdomain of it; malformed URLs are out of scope
    parts = urlsplit(absolute(url))
    host = (parts.hostname or "").rstrip(".")
    return parts.scheme == "https" and bool(domain) and (host == domain or host.endswith("." + domain))

def require(dom, final, ctype, body=b""):
    why = ("url outside official domain" if not in_scope(final, dom) else "body over 2000000 bytes" if len(body) > CAP
           else None if (ctype or "").lower().startswith("text/html") else "content-type not text/html")
    if why:
        raise Refused(why)

class RedirectGuard(urllib.request.HTTPRedirectHandler):
    def __init__(self, domain): self.domain = domain
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if in_scope(newurl, self.domain):
            return super().redirect_request(req, fp, code, msg, headers, newurl)
        fp.close() if fp is not None else None
        raise Refused("redirect outside official domain")

def fetch(url, official_domain):
    """Fetch one index page once: https, in-domain redirects only, text/html only, <= CAP bytes, honest UA, no retry."""
    require(official_domain, url, "text/html")
    opener = urllib.request.build_opener(RedirectGuard(official_domain))
    try:
        with opener.open(urllib.request.Request(url, headers={"User-Agent": USER_AGENT}), timeout=20) as resp:
            final, ctype = resp.geturl(), resp.headers.get("Content-Type", "")
            require(official_domain, final, ctype)  # aborts before the body is read
            body = resp.read(CAP + 1)
    except urllib.error.HTTPError as err:
        err.close()
        raise Refused("http %d" % err.code) from None
    except (urllib.error.URLError, OSError, ValueError) as err:  # ValueError: malformed redirect Location or URL
        raise Refused("malformed url" if isinstance(err, ValueError) else "network %s" % type(err).__name__) from None
    require(official_domain, final, ctype, body)
    return final, ctype, body

class Links(html.parser.HTMLParser):
    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.links, self.href, self.text = [], None, []
        self.feed(text)
        self.close()
        self.handle_endtag("a")  # flush an unclosed final anchor
    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.handle_endtag(tag)
            self.href = dict(attrs).get("href")
    def handle_data(self, data):
        self.text.append(data)
    def handle_endtag(self, tag):
        if tag == "a" and self.href:
            self.links.append((self.href.strip(), " ".join("".join(self.text).split())[:200] or "(no title)"))
        if tag == "a":
            self.href, self.text = None, []

def read_csv(path, header, missing_ok=False):
    if missing_ok and not Path(path).exists(): return []
    with open(path, encoding="utf-8-sig", newline="") as handle:
        records = list(csv.reader(handle))
    if not records or records[0] != header:
        raise Fail("row 1: header is not the accepted header")
    rows = [(n, rec) for n, rec in enumerate(records[1:], 2) if rec]
    if any(len(rec) != len(header) for _, rec in rows):
        raise Fail("row %d: columns have the wrong count" % next(n for n, rec in rows if len(rec) != len(header)))
    return [(n, dict(zip(header, rec))) for n, rec in rows]

def row_errors(row):
    social, url, dom = row["kind"] == "social", row["url"], row["official_domain"]
    checks = (("agency", row["agency"].strip(), "is empty"),
              ("kind", row["kind"] in KINDS, "is not agenda, minutes, news or social"),
              ("official_domain", DOMAIN.fullmatch(dom), "is not a bare lowercase hostname"),
              ("query", bool(row["query"].strip()) == social, "must be set for social rows only"),
              ("url", not url if social else url.startswith("https://"), "must be empty for social, else https://"),
              ("url", social or absolute(url), "is not a parseable URL"),
              ("url", social or not absolute(url) or in_scope(url, dom), "host is outside official_domain"),
              ("url", social or not urlsplit(absolute(url)).path.lower().endswith(".pdf"), "is a pdf"),
              ("last_seen_hash", re.fullmatch(r"(?:[0-9a-f]{64})?", row["last_seen_hash"]), "must be empty or 64 lowercase hex"))
    return ["%s %s" % (field, why) for field, ok, why in checks if not ok]

def write_atomic(path, text="", header=None, rows=()):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as out:
            out.write(text)
            if header: csv.writer(out, lineterminator="\n").writerows([header] + [[r[k] for k in header] for r in rows])
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError): os.unlink(tmp)
        raise

def private_store(store):
    store = Path(store).resolve()
    if store == REPO or REPO in store.parents: raise Fail("store inside repository")
    return store

def _real(path, what):  # a linked or wrong-kind watch list, MONITOR.csv or digest folder is refused, never followed
    folder = what == "digest folder"
    if os.path.islink(path): raise Fail(what + " is a symlink")
    if os.path.lexists(path) and not (os.path.isdir if folder else os.path.isfile)(path):
        raise Fail("%s is not a %s" % (what, "folder" if folder else "file"))

def validated(watchlist):  # rows of a valid watch list, else Fail with every row error
    rows = read_csv(watchlist, WATCH_HEADER)
    errors = ["row %d: %s" % (n, e) for n, row in rows for e in row_errors(row)]
    if errors:
        raise Fail("\n".join(errors))
    return rows

def check(watchlist):  # dry run: the pages scan would fetch; no network, no store, no write
    rows = validated(watchlist)
    lines = ["row %d: %s" % (n, "social, not fetched" if r["kind"] == "social" else "would fetch " + r["url"]) for n, r in rows]
    social = sum(r["kind"] == "social" for _, r in rows)
    return lines + ["rows=%d fetch=%d social=%d errors=0" % (len(rows), len(rows) - social, social)]

def scan(watchlist, store, date, fetch=fetch):
    rows = validated(watchlist)  # before any fetch
    _real(watchlist, "watchlist")
    _real((store := private_store(store)) / "MONITOR.csv", "MONITOR.csv")
    _real(store / "SCAN-LOG.csv", "SCAN-LOG.csv")
    with store_lock(store, "store busy: another scan is running"):
        return scan_locked(watchlist, rows, store, date, fetch)

@contextlib.contextmanager
def store_lock(store, busy="store busy: another monitor command is running"):
    store.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = os.open(store / ".monitor.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)  # a symlink raises OSError
    try:  # one writer per store at a time; the kernel drops the flock when the fd closes or the process dies
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: raise Fail(busy) from None
        yield
    finally:
        os.close(lock)  # the lock file itself is never deleted

def log_rows(path):  # rows of <store>/SCAN-LOG.csv (missing = none); a linked, wrong-kind or malformed file is refused
    _real(path, "SCAN-LOG.csv")
    try: return [r for _, r in read_csv(path, LOG_HEADER, missing_ok=True)]
    except Fail as err: raise Fail("SCAN-LOG.csv %s" % err) from None

def keep_last(rows, keep=LOG_KEEP):  # the last `keep` rows of each (agency, url) in file order, and how many were dropped
    seen, kept = {}, []
    for r in reversed(rows):
        key = (r["agency"], r["url"])
        seen[key] = seen.get(key, 0) + 1
        kept += [r] if seen[key] <= keep else []
    return kept[::-1], len(rows) - len(kept)

def scan_locked(watchlist, rows, store, date, fetch):  # call only while holding <store>/.monitor.lock
    counts, out, new, changed, logged = dict.fromkeys(COUNTS, 0), [], [], False, []
    monitor = [r for _, r in read_csv(store / "MONITOR.csv", MONITOR_HEADER, missing_ok=True)]
    log = log_rows(store / "SCAN-LOG.csv")  # before any fetch: a bad log fails the scan with nothing fetched or written
    note = lambda row, outcome, reason="": logged.append(dict(scan_date=date, agency=row["agency"], url=row["url"],
                                                              outcome=outcome, reason=reason))
    known = {r["item_id"] for r in monitor}
    for n, row in rows:
        dom, social = row["official_domain"], row["kind"] == "social"
        counts["skipped_social" if social else "scanned"] += 1
        try:
            if social:
                continue
            final, ctype, body = fetch(row["url"], dom)  # one fetch per row; link targets are never fetched
            require(dom, final, ctype, body)  # defence in depth against any fetcher
        except Refused as err:
            counts["refused"] += 1
            out.append("row %d: refused %s" % (n, err))
            note(row, "refused", str(err))
            continue
        page = hashlib.sha256(body).hexdigest()
        if page == row["last_seen_hash"]:
            counts["unchanged"] += 1
            note(row, "unchanged")
            continue
        note(row, "ok")
        for href, title in Links(body.decode("utf-8", "replace")).links:
            url = absolute(href, final)
            if not in_scope(url, dom):  # off-domain, non-https or malformed: dropped and counted
                counts["offdomain_links"] += 1
                continue
            item = "M-" + hashlib.sha256((slug(row["agency"]) + "\x1f" + url).encode()).hexdigest()[:12]
            if url != final and item not in known:
                known.add(item)
                new.append(dict(item_id=item, agency=row["agency"], watch_kind=row["kind"], url=url, title=title,
                                page_sha256=page, seen_at=date, kind="public", status="unreviewed"))
        row["last_seen_hash"], changed = page, True
    counts["new_items"] = len(new)
    if new:
        write_atomic(store / "MONITOR.csv", header=MONITOR_HEADER, rows=monitor + new)
    if changed:
        write_atomic(Path(watchlist), header=WATCH_HEADER, rows=[r for _, r in rows])
    trimmed = 0
    if logged:  # one atomic rewrite after MONITOR.csv and the watch list: old rows (trimmed) + this scan's rows
        kept, trimmed = keep_last(log + logged)
        write_atomic(store / "SCAN-LOG.csv", header=LOG_HEADER, rows=kept)
    return out + [" ".join("%s=%d" % (k, counts[k]) for k in COUNTS) + (" log_trimmed=%d" % trimmed if trimmed else "")]

def keywords():  # the shared meetings ALPR pattern, never a copy; a failed import fails the digest (no unranked digest)
    if str(REPO) not in sys.path: sys.path.insert(0, str(REPO))
    from campaign_tool.meetings import KEYWORDS
    return KEYWORDS

def alpr_terms(row, pattern):  # title plus percent-decoded URL path with -, _ and / read as spaces; lowercased, deduped
    path = re.sub(r"[-_/]", " ", unquote(urlsplit(absolute(row["url"])).path))
    return list(dict.fromkeys(m.group(0).lower() for m in pattern.finditer(row["title"] + " " + path)))

def leads_rows(path):  # rows of <store>/LEADS.csv (missing = none); a linked, wrong-kind or malformed file is refused
    _real(path, "LEADS.csv")
    try: return [r for _, r in read_csv(path, LEADS_HEADER, missing_ok=True)]
    except Fail as err: raise Fail("LEADS.csv %s" % err) from None

def new_leads(path, date, relevant):  # call only under the store lock: add unknown relevant items, list date's leads
    rows, ids, add = leads_rows(path), set(), []
    known = {r["item_id"] for r in rows}
    for r in relevant:
        if r["item_id"] not in known | ids:
            ids.add(r["item_id"])
            add.append(dict(item_id=r["item_id"], agency=r["agency"], first_seen=date, state="new", state_at=date,
                            state_by="digest"))
    if add:
        write_atomic(path, header=LEADS_HEADER, rows=rows + add)
    first, shown = {r["item_id"] for r in rows + add if r["first_seen"] == date}, {}
    for r in relevant:  # a lead first flagged on an earlier date is never listed again, whatever its state
        if r["item_id"] in first:
            shown.setdefault(r["item_id"], " ".join(("- %s: %s %s [%s]" % (r["agency"], r["title"], r["url"],
                                                                           r["item_id"])).split()))
    return ["", "Leads (%d new)" % len(shown)] + list(shown.values()) if shown else []  # no leads for D: no section

def lead(store, item, state, by, date):  # triage one lead: new -> requested|ignored, ignored -> requested
    if not re.fullmatch(r"M-[0-9a-f]{12}", item): raise Fail("lead: --item is not M- and 12 lowercase hex")
    if state not in ("requested", "ignored"): raise Fail("lead: --state is not requested or ignored")
    if not by.strip(): raise Fail("lead: --by is empty")
    path = private_store(store) / "LEADS.csv"
    if not leads_rows(path): raise Fail("lead: unknown item " + item)  # no store or no leads: nothing is created
    with store_lock(path.parent):
        rows = leads_rows(path)
        row = next((r for r in rows if r["item_id"] == item), None)
        if row is None: raise Fail("lead: unknown item " + item)
        if row["state"] == state: return "lead | %s | %s -> %s unchanged" % (item, state, state)
        old = row["state"]
        if state not in MOVES.get(old, ()): raise Fail("lead: %s cannot move %s -> %s" % (item, old, state))
        row.update(state=state, state_at=date, state_by=" ".join(by.split()))
        write_atomic(path, header=LEADS_HEADER, rows=rows)
    return "lead | %s | %s -> %s" % (item, old, state)

def unreachable(log, date, n):  # pages whose last n log rows with scan_date <= date are all refused (the log is the source)
    pages = {}
    for r in log:
        if r["scan_date"] <= date: pages.setdefault((r["agency"], r["url"]), []).append(r)
    lines = []
    for (agency, url), rs in sorted(pages.items(), key=lambda e: (slug(e[0][0]), e[0])):
        streak = next((i for i, r in enumerate(reversed(rs)) if r["outcome"] != "refused"), len(rs))
        if streak >= n:  # fewer than n rows, or an ok/unchanged among the last n: not listed
            lines.append("- %s: %s (%s, since %s)" % (agency, url, rs[-1]["reason"], rs[-streak]["scan_date"]))
    return ["", "Unreachable (%d pages, last %d scans refused)" % (len(lines), n)] + lines if lines else []

def digest(store, date, after=3):
    with store_lock(store := private_store(store)):  # date filter, relevance match and both writes: one locked pass
        return digest_locked(store, date, after)

def digest_locked(store, date, after=3):  # call only under the store lock; a scan committed before it is always included
    path = store / "digest" / (date + ".md")
    rows = [r for _, r in read_csv(store / "MONITOR.csv", MONITOR_HEADER, missing_ok=True) if r["seen_at"] == date]
    pattern = keywords()
    ranked = sorted(((r, alpr_terms(r, pattern)) for r in rows), key=lambda e: (slug(e[0]["agency"]), not e[1]))  # stable
    items = [(" ".join(("- %s %s: %s %s" % (r["agency"], r["watch_kind"], r["title"], r["url"])).split())
              + (" [ALPR: %s]" % ", ".join(terms) if terms else ""), bool(terms)) for r, terms in ranked]
    relevant = sum(hit for _, hit in items)
    lines = ["Flock monitor %s: %s" % (date, "%d new items (public, unreviewed), %d relevant" % (len(rows), relevant)
                                       if rows else "no new items")]
    if len(items) <= 39:
        lines += [line for line, _ in items]
    else:  # relevant items first into the 38 shown slots, then the rest; shown lines keep the ranked order
        keep = sorted(sorted(range(len(items)), key=lambda i: not items[i][1])[:38])
        lines += [items[i][0] for i in keep] + ["- … and %d more in MONITOR.csv (%d relevant)"
                                                 % (len(items) - 38, relevant - sum(items[i][1] for i in keep))]
    down = unreachable(log_rows(store / "SCAN-LOG.csv"), date, after)  # a bad log fails before any write
    _real(path.parent, "digest folder")  # checked before LEADS.csv is touched
    lines += new_leads(store / "LEADS.csv", date, [r for r, terms in ranked if terms]) + down
    write_atomic(path, "\n".join(lines) + "\n")
    return path

def scans(text):  # --unreachable-after: an integer 2..30, else argparse usage exit 2
    if not re.fullmatch(r"[0-9]{1,2}", text) or not 2 <= int(text) <= 30:
        raise argparse.ArgumentTypeError("must be an integer from 2 to 30")
    return int(text)

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    scan_p = sub.add_parser("scan", help="fetch each non-social WATCHLIST index page once and record new links")
    scan_p.add_argument("watchlist")
    sub.add_parser("check", help="validate WATCHLIST and print the pages scan would fetch (no network, no writes)").add_argument("watchlist")
    digest_p = sub.add_parser("digest", help="write <store>/digest/<date>.md; list new relevant items once (LEADS.csv)")
    lead_p = sub.add_parser("lead", help="set a LEADS.csv lead to requested or ignored (who and when recorded)")
    lead_p.add_argument("--item", required=True)
    lead_p.add_argument("--state", required=True, help="requested or ignored")
    lead_p.add_argument("--by", required=True)
    digest_p.add_argument("--unreachable-after", type=scans, default=3, metavar="N",
                          help="list pages whose last N logged scans (2..30, default 3) were all refused")
    for p, required in ((scan_p, False), (digest_p, True), (lead_p, False)):
        p.add_argument("--store", required=True)
        p.add_argument("--date", type=datetime.date.fromisoformat, required=required, default=datetime.date.today())
    args = parser.parse_args(argv)
    try:
        print("\n".join(check(args.watchlist)) if args.command == "check"
              else "\n".join(scan(args.watchlist, args.store, args.date.isoformat(), fetch=fetch)) if args.command == "scan"
              else lead(args.store, args.item, args.state, args.by, args.date.isoformat()) if args.command == "lead"
              else digest(args.store, args.date.isoformat(), args.unreachable_after))
    except (Fail, OSError, UnicodeDecodeError, csv.Error, ImportError) as err:  # csv.Error prints "io error: Error"
        print(err if isinstance(err, Fail) else "io error: %s" % ("ImportError" if isinstance(err, ImportError)
                                                                   else type(err).__name__))
        return 1
    return 0

if __name__ == "__main__":
    sys.exit(main())
