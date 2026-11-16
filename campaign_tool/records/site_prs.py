"""CON-SITEPR-1 (DF2007-09): an operator-captured snapshot of the public site's open draft pull requests.

``records site-prs snapshot --from FILE --export-root DIR --repository OWNER/REPO`` validates the output of
``gh pr list --json number,title,isDraft,url,headRefName,updatedAt`` that the operator saved to FILE and writes
``DIR/site-prs.json`` (0600). The console shows it at ``/site-prs``. Nothing here runs gh, opens a network connection,
merges, approves or refreshes anything. The six captured fields cannot prove a pull request is still open or that the
capture is complete, so the page says "captured", never "live".
"""
import argparse
import json
import os
import re
import secrets
import stat
import sys
import time
import unicodedata
import urllib.parse
from datetime import datetime, timezone

from . import console_layout
from .outcomes.reading import esc

SCHEMA = "records-site-prs-v1"
NAME = "site-prs.json"
FIELDS = frozenset({"number", "title", "isDraft", "url", "headRefName", "updatedAt"})
MAX_ROWS, MAX_BYTES, MAX_TEXT = 50, 256 * 1024, 1024
STALE_SECONDS = 24 * 3600
_SEGMENT = re.compile(r"[A-Za-z0-9._-]+\Z")
_STAMP = re.compile(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.\d{1,9})?(?:Z|\+00:00)\Z")
_CANON = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ\Z")


class SitePrError(ValueError):
    """The code is the message; no input text is echoed."""


def _fail(code):
    raise SitePrError(code)


def repository(value):
    """OWNER/REPO as two GitHub-safe path segments, or SitePrError."""
    parts = value.split("/") if isinstance(value, str) else []
    if len(parts) != 2 or any(not _SEGMENT.match(p) or p in (".", "..") for p in parts):
        _fail("repository_invalid")
    return value


def _stamp(value):
    """A canonical UTC YYYY-MM-DDTHH:MM:SSZ, or SitePrError."""
    match = _STAMP.match(value) if isinstance(value, str) else None
    if not match:
        _fail("timestamp_invalid")
    try:
        return datetime.strptime(match.group(1), "%Y-%m-%dT%H:%M:%S").strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        _fail("timestamp_invalid")


def _parse_stamp(value):
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _control(text):
    return any(unicodedata.category(c) == "Cc" for c in text)


def _row(row, repo, strip):
    if not isinstance(row, dict) or set(row) != FIELDS:
        _fail("row_fields_invalid")
    number, title, draft, url, head = (row[k] for k in ("number", "title", "isDraft", "url", "headRefName"))
    if type(number) is not int or number < 1:
        _fail("number_invalid")
    if draft is not True:
        _fail("not_a_draft")
    if not isinstance(title, str) or len(title) > MAX_TEXT:
        _fail("title_invalid")
    if strip:
        title = "".join(c for c in title if unicodedata.category(c) != "Cc")
    elif _control(title):
        _fail("title_invalid")
    if not title.strip():
        _fail("title_invalid")
    if not isinstance(head, str) or not head or len(head) > MAX_TEXT or _control(head):
        _fail("head_invalid")
    if url != "https://github.com/%s/pull/%d" % (repo, number):  # exact: scheme, host, repository, number; no extras
        _fail("url_invalid")
    return {"number": number, "title": title, "isDraft": True, "url": url, "headRefName": head,
            "updatedAt": _stamp(row["updatedAt"])}


def validate(rows, repo, strip=True):
    """Validated rows sorted by number, or SitePrError. strip=False is the reader: stored rows must already be clean."""
    if not isinstance(rows, list) or len(rows) > MAX_ROWS:
        _fail("rows_invalid")
    out = [_row(r, repo, strip) for r in rows]
    if len({r["number"] for r in out}) != len(out):
        _fail("duplicate_number")
    return sorted(out, key=lambda r: r["number"])


def _pairs(pairs):
    keys = [k for k, _ in pairs]
    if len(set(keys)) != len(keys):
        _fail("duplicate_key")
    return dict(pairs)


def _refuse_constant(_name):
    _fail("not_finite")


def parse(data):
    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_refuse_constant)
    except SitePrError:
        raise
    except (UnicodeError, ValueError, RecursionError):
        _fail("json_invalid")


def read_regular(path, code):
    """At most MAX_BYTES of a regular, non-linked file (cap-plus-one read), or SitePrError(code)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        _fail(code)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            _fail(code)
        data = b""
        while len(data) <= MAX_BYTES:
            chunk = os.read(fd, MAX_BYTES + 1 - len(data))
            if not chunk:
                break
            data += chunk
    except OSError:
        _fail(code)
    finally:
        os.close(fd)
    if len(data) > MAX_BYTES:
        _fail("too_large")
    return data


def build(data, repo, now):
    """The exact bytes to store for captured input DATA, or SitePrError. Reads and writes nothing."""
    repo = repository(repo)
    rows = validate(parse(data), repo)
    doc = {"schema": SCHEMA, "repository": repo, "captured_at": now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "prs": rows}
    out = (json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8", errors="backslashreplace")
    if len(out) > MAX_BYTES:  # Never replace a snapshot with bytes that load() would refuse.
        _fail("too_large")
    return out


def _write(directory, out):
    """Exclusive 0600 sibling temp, fsync, atomic replace; the writer's own temp is removed on failure."""
    tmp = os.path.join(directory, ".%s.%s.tmp" % (NAME, secrets.token_hex(8)))
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        try:
            os.fchmod(fd, 0o600)
            view = memoryview(out)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, os.path.join(directory, NAME))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    try:
        dfd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass


def snapshot(source, export_root, repo, now=None):
    """Validate SOURCE completely, then replace EXPORT_ROOT/site-prs.json. Returns the stored document's row count."""
    now = now or datetime.now(timezone.utc)
    out = build(read_regular(source, "source_unreadable"), repo, now)
    root = os.path.abspath(export_root)
    try:
        info = os.lstat(root)
        ok = stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid() and os.path.realpath(root) == root
    except OSError:
        ok = False
    if not ok:
        _fail("export_root_invalid")
    dest = os.path.join(root, NAME)
    try:
        if not stat.S_ISREG(os.lstat(dest).st_mode):
            _fail("destination_invalid")
    except FileNotFoundError:
        pass
    except OSError:
        _fail("destination_invalid")
    try:
        _write(root, out)
    except OSError:
        _fail("write_failed")
    return len(json.loads(out)["prs"])


def main(argv=None):
    parser = argparse.ArgumentParser(prog="records site-prs", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    snap = sub.add_parser("snapshot", help="import an operator-captured gh pr list array (offline)")
    snap.add_argument("--from", dest="source", required=True, metavar="FILE", help="local file holding the captured JSON array")
    snap.add_argument("--export-root", required=True, metavar="DIR", help="existing console export directory; site-prs.json is written there")
    snap.add_argument("--repository", required=True, metavar="OWNER/REPO", help="the site repository the capture is for")
    args = parser.parse_args(argv)
    try:
        count = snapshot(args.source, args.export_root, args.repository)
    except SitePrError as exc:
        print("site-prs: refused (%s); nothing written" % exc, file=sys.stderr)
        return 2
    print("site-prs: stored %d captured open draft pull request(s) in %s" % (count, os.path.join(os.path.abspath(args.export_root), NAME)))
    return 0


def unavailable(code):
    return {"state": "unavailable", "code": code}


def load(path, now):
    """{"state": "ok", ...} or {"state": "unavailable", "code": ...}. Absent or invalid is never an empty list."""
    try:
        doc = parse(read_regular(path, "site_prs_missing"))
        if not isinstance(doc, dict) or set(doc) != {"schema", "repository", "captured_at", "prs"} or doc["schema"] != SCHEMA:
            _fail("site_prs_invalid")
        repo = repository(doc["repository"])
        captured = _stamp(doc["captured_at"])
        if not isinstance(doc["prs"], list) or validate(doc["prs"], repo, strip=False) != doc["prs"]:
            _fail("site_prs_invalid")
    except SitePrError as exc:
        return unavailable(str(exc) if str(exc) in ("site_prs_missing", "too_large") else "site_prs_invalid")
    seconds = (now - _parse_stamp(captured)).total_seconds()
    if seconds < 0:
        return unavailable("site_prs_future")
    return {"state": "ok", "repository": repo, "captured_at": captured, "prs": doc["prs"], "seconds": seconds,
            "hours": int(seconds // 3600), "stale": seconds > STALE_SECONDS}


def _now(handler):
    clock = getattr(handler.server, "access_clock", None) or time.time
    return datetime.fromtimestamp(clock(), timezone.utc)


def read(handler):
    """The request's one read of the sidecar beside the export root (or the live project)."""
    return load(os.path.join(handler.server.options["project"], NAME), _now(handler))


def age(updated, now):
    seconds = (now - _parse_stamp(updated)).total_seconds()
    if seconds < 0:
        return "update age unknown (clock ahead)"
    hours = int(seconds // 3600)
    return "%d hours" % hours if hours < 48 else "%d days" % (hours // 24)


def hours_ago(model):
    return "captured %d hour%s ago" % (model["hours"], "" if model["hours"] == 1 else "s")


def home_card(model):
    """One Home paragraph for a load() result."""
    if model["state"] != "ok":
        return '<p><a href="/site-prs">Site draft PR snapshot unavailable</a>.</p>'
    count, stale = len(model["prs"]), " Stale: older than 24 hours." if model["stale"] else ""
    if not count:
        return '<p><a href="/site-prs">Site draft PRs</a>: captured no open drafts, %s.%s</p>' % (hours_ago(model), esc(stale))
    return '<p><a href="/site-prs">Site draft PRs</a>: %d captured open draft%s, %s.%s</p>' % (
        count, "" if count == 1 else "s", hours_ago(model), esc(stale))


def render(model, now):
    out = ["<h1>Site draft pull requests</h1>",
           "<p>Read-only snapshot captured by the operator with <code>gh pr list</code>. This console never contacts "
           "GitHub, and cannot merge, approve or refresh anything. The list may be out of date or incomplete.</p>"]
    if model["state"] != "ok":
        out.append('<p role="status">Site draft PR snapshot unavailable (%s). This is not a count of zero.</p>'
                   % esc(model["code"]))
        return console_layout.shell("Site draft PRs", "\n".join(out), "Home")
    out.append("<p>%s: %s, %s.</p>" % (esc(model["repository"]), "%d captured open draft%s" % (
        len(model["prs"]), "" if len(model["prs"]) == 1 else "s") if model["prs"] else "no open drafts captured",
        esc(hours_ago(model))))
    if model["stale"]:
        out.append('<p role="alert" class="overdue">Stale: this snapshot was captured more than 24 hours ago. '
                   "Capture it again before relying on it.</p>")
    if model["prs"]:
        out.append("<p>Age is time since the captured update time, not proof of review inactivity.</p>")
        out.append('<table><thead><tr>' + "".join('<th scope="col">%s</th>' % t for t in (
            "PR", "Title", "Branch", "Last updated (UTC)", "Age")) + "</tr></thead><tbody>")
        for pr in model["prs"]:
            out.append('<tr><td><a href="%s" rel="noopener noreferrer">#%d</a></td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>' % (
                esc(pr["url"]), pr["number"], esc(pr["title"]), esc(pr["headRefName"]), esc(pr["updatedAt"]),
                esc(age(pr["updatedAt"], now))))
        out.append("</tbody></table>")
    return console_layout.shell("Site draft PRs", "\n".join(out), "Home")


def serve(handler):
    split = urllib.parse.urlsplit(handler.path)
    if split.path != "/site-prs":
        return False
    if split.query:
        handler._fail(400)
        return True
    handler._send(200, render(read(handler), _now(handler)))
    return True
