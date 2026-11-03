#!/usr/bin/env python3
"""Records search: one private local index over the extracted text of every preserved record in a private store.

  records_search.py index --store STORE --out INDEXDIR
  records_search.py query --index INDEXDIR 'TERMS'
  records_search.py stat --index INDEXDIR

STORE is the private flock store: each agencies/<slug>/INVENTORY.csv (columns request, doc_id, sha256, as for flock_facts
coverage) lists the preserved documents, and agencies/<slug>/extracted/<sha256>/units.jsonl is that original's extraction
output (one JSON unit per line, page from locator.page). Nothing is re-extracted or OCR'd. Each document is recorded as
text, no-text (units.jsonl absent, or present with no text) or unreadable (not a regular file, too large, not UTF-8 JSON).

INDEXDIR is absolute, normalized, outside every repository and every public output folder, with a link-free existing
parent; it is created 0700 and holds one 0600 file whose name and suffix tools/check_public_tree.py refuses to publish.
A document is keyed by its extraction bytes' sha256: unchanged bytes are a no-op, changed bytes replace that document's
rows only. The index is rebuilt in a temporary sibling and swapped in with os.replace, so a crash keeps the old index.

TERMS: literal words and "quoted phrases" joined by AND / OR (adjacent terms mean AND); no regular expressions.
query prints `document_id | page | offset | request | agency | sha256` per hit (offset: UTF-8 byte offset of the first
matched term on that page) and never any document text: open the cited document to read the context. Every query and
stat output ends with the coverage line; extracted text stays unreviewed. Exit 0 done, 2 refused or usage error.
Nothing is copied, uploaded or published; there is no export path.
"""
import argparse, contextlib, csv, hashlib, json, os, re, sqlite3, stat, sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from campaign_tool.records.public_scan import DENIED_NAMES, DENIED_SUFFIXES  # noqa: E402  the one denial list

INDEX_NAME = "records-search.rsidx"
PUBLIC_PARTS = {"public", "site", "_site", "dist", "www", "publish", "published", "build"}
SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
DOC_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}")
REQUEST = re.compile(r"[A-Za-z0-9][A-Za-z0-9._#/-]{0,59}")
SHA = re.compile(r"[0-9a-f]{64}")
TERM = re.compile(r"[^\W_](?:[\w.@'-]*[^\W_])?")
MAX_UNITS_BYTES, MAX_QUERY, MAX_TERMS = 32 * 1024 * 1024, 256, 16
SCHEMA = ("CREATE TABLE docs(agency TEXT, doc_id TEXT, request TEXT, sha256 TEXT, text_sha256 TEXT, state TEXT, reason TEXT,"
          " PRIMARY KEY(agency, doc_id));"
          "CREATE VIRTUAL TABLE pages USING fts5(body, agency UNINDEXED, doc_id UNINDEXED, page UNINDEXED);")


class Refused(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message):  # argparse would echo the arguments, which may be query terms
        raise Refused("usage_invalid")


def _checkpoint():
    """Called after each document is written into the temporary index; tests inject a crash here."""


def _linked(path):
    return any(candidate.is_symlink() for candidate in (path, *path.parents))


def _absolute(value, name):
    path = Path(value)
    if not path.is_absolute() or Path(os.path.normpath(path)) != path:
        raise Refused(name + "_must_be_absolute_and_normalized")
    return path


def check_indexdir(out):
    """Mechanism (a): the index never lands under a repository or a public output path."""
    if Path(INDEX_NAME).suffix not in DENIED_SUFFIXES or INDEX_NAME not in DENIED_NAMES:
        raise Refused("index_name_not_denied_by_public_scan")
    real = Path(os.path.realpath(out))
    for candidate in (out, real):
        if candidate == REPO or REPO in candidate.parents \
                or any(os.path.lexists(folder / ".git") for folder in (candidate, *candidate.parents)):
            raise Refused("indexdir_inside_repository")
        if set(part.lower() for part in candidate.parts) & PUBLIC_PARTS:
            raise Refused("indexdir_inside_public_output")
    if _linked(out.parent) or not out.parent.is_dir():
        raise Refused("indexdir_parent_unavailable")
    if os.path.lexists(out):
        info = os.lstat(out)
        if not stat.S_ISDIR(info.st_mode):
            raise Refused("indexdir_not_a_directory")
        if set(os.listdir(out)) - {INDEX_NAME}:
            raise Refused("indexdir_not_empty")
        if info.st_mode & 0o077 or info.st_uid != os.getuid():
            raise Refused("indexdir_not_private")


def read_units(path):
    """(state, reason, sha256 of the extraction bytes, {page: text})."""
    if not os.path.lexists(path):
        return "no-text", "absent", "", {}
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return "unreadable", "open", "", {}
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_UNITS_BYTES:
            return "unreadable", "not_regular_or_too_large", "", {}
        data = handle.read(MAX_UNITS_BYTES + 1)
    digest, pages = hashlib.sha256(data).hexdigest(), {}
    try:
        for line in filter(str.strip, data.decode("utf-8").splitlines()):
            unit = json.loads(line)
            locator = unit.get("locator") or {}
            page, text = locator.get("page", 1) if isinstance(locator, dict) else None, unit.get("text", "")
            if not isinstance(text, str) or type(page) is not int or page < 1:
                raise ValueError("unit_shape")
            if text.strip():
                pages[page] = pages[page] + "\n" + text if page in pages else text
    except (UnicodeDecodeError, ValueError, AttributeError):
        return "unreadable", "not_utf8_json_units", digest, {}
    return ("text", "", digest, pages) if pages else ("no-text", "empty", digest, {})


def store_documents(store):
    """[(agency, doc_id, request, sha256, units path)] from every agencies/<slug>/INVENTORY.csv; refuses bad rows."""
    root = store / "agencies"
    if _linked(store) or not store.is_dir() or not root.is_dir() or root.is_symlink():
        raise Refused("store_unavailable")
    docs, seen = [], set()
    for folder in sorted(root.iterdir()):
        inventory = folder / "INVENTORY.csv"
        if folder.is_symlink() or not folder.is_dir() or not SLUG.fullmatch(folder.name) or not os.path.lexists(inventory):
            continue
        if inventory.is_symlink() or not inventory.is_file():
            raise Refused("inventory_unreadable:" + folder.name)
        try:
            with open(inventory, encoding="utf-8-sig", newline="") as handle:
                table = [row for row in csv.reader(handle) if row]
        except (OSError, UnicodeDecodeError, csv.Error):
            raise Refused("inventory_unreadable:" + folder.name) from None
        head = table[0] if table else []
        if not {"request", "doc_id", "sha256"} <= set(head):
            raise Refused("inventory_header_invalid:" + folder.name)
        for number, cells in enumerate(table[1:], 2):
            row = dict(zip(head, (cell.strip() for cell in cells)))
            request = row.get("request") or "-"
            if len(cells) != len(head) or not DOC_ID.fullmatch(row["doc_id"]) or not SHA.fullmatch(row["sha256"]) \
                    or request != "-" and not REQUEST.fullmatch(request):
                raise Refused("inventory_row_invalid:%s:%d" % (folder.name, number))
            if (folder.name, row["doc_id"]) in seen:
                raise Refused("inventory_duplicate_document:%s:%d" % (folder.name, number))
            seen.add((folder.name, row["doc_id"]))
            docs.append((folder.name, row["doc_id"], request, row["sha256"],
                         folder / "extracted" / row["sha256"] / "units.jsonl"))
    return docs


def _open_index(indexdir):
    path = indexdir / INDEX_NAME
    if _linked(path) or not path.is_file():
        raise Refused("index_unavailable")
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("SELECT count(*) FROM docs").fetchone()
    except sqlite3.DatabaseError:
        conn.close()
        raise Refused("index_unreadable") from None
    return conn


def coverage_lines(conn):
    count = dict(conn.execute("SELECT state, count(*) FROM docs GROUP BY state").fetchall())
    why = dict(conn.execute("SELECT reason, count(*) FROM docs WHERE state = 'no-text' GROUP BY reason").fetchall())
    return count, ["no-text: absent=%d empty=%d" % (why.get("absent", 0), why.get("empty", 0)),
                   "coverage: indexed=%d no-text=%d unreadable=%d; extracted text is unreviewed"
                   % (count.get("text", 0), count.get("no-text", 0), count.get("unreadable", 0))]


def build(store, out):
    check_indexdir(out)
    docs = store_documents(store)
    final, old = out / INDEX_NAME, {}
    if os.path.lexists(final):
        with contextlib.closing(_open_index(out)) as conn:
            old = {(a, d): rest for a, d, *rest in conn.execute(
                "SELECT agency, doc_id, request, sha256, text_sha256, state, reason FROM docs")}
    plan, unchanged = [], 0
    for agency, doc_id, request, sha, units in docs:
        state, reason, text_sha, pages = read_units(units)
        row = [request, sha, text_sha, state, reason]
        if old.get((agency, doc_id)) == row:
            unchanged += 1
        else:
            plan.append((agency, doc_id, row, pages))
    removed = sorted(set(old) - {(a, d) for a, d, *_ in docs})
    if not plan and not removed and os.path.lexists(final):
        return len(docs), 0, unchanged, 0
    if not os.path.lexists(out):
        os.mkdir(out, 0o700)
    tmp = out / (".tmp-%d-%s" % (os.getpid(), INDEX_NAME))
    os.close(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600))
    try:
        with contextlib.closing(sqlite3.connect(tmp)) as conn:
            conn.execute("PRAGMA journal_mode=MEMORY")
            if old or os.path.lexists(final):
                with contextlib.closing(_open_index(out)) as source:
                    source.backup(conn)
            else:
                conn.executescript(SCHEMA)
            for agency, doc_id in removed:
                conn.execute("DELETE FROM pages WHERE agency = ? AND doc_id = ?", (agency, doc_id))
                conn.execute("DELETE FROM docs WHERE agency = ? AND doc_id = ?", (agency, doc_id))
            for agency, doc_id, row, pages in plan:
                conn.execute("DELETE FROM pages WHERE agency = ? AND doc_id = ?", (agency, doc_id))
                conn.execute("INSERT OR REPLACE INTO docs VALUES (?, ?, ?, ?, ?, ?, ?)", (agency, doc_id, *row))
                conn.executemany("INSERT INTO pages(body, agency, doc_id, page) VALUES (?, ?, ?, ?)",
                                 [(text, agency, doc_id, page) for page, text in sorted(pages.items())])
                _checkpoint()
            conn.commit()
        fd = os.open(tmp, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, final)
    finally:
        if os.path.lexists(tmp):
            os.unlink(tmp)
    return len(docs), len(plan), unchanged, len(removed)


def parse_query(text):
    """(FTS5 expression of quoted literals, [terms]); anything else is a usage error."""
    if not text.strip() or len(text) > MAX_QUERY:
        raise Refused("query_invalid")
    parts, terms, want = [], [], True
    for token in re.findall(r'"[^"]*"|\S+', text):
        if token in ("AND", "OR"):
            if want:
                raise Refused("query_invalid")
            parts.append(token)
            want = True
            continue
        words = token[1:-1].split() if len(token) > 1 and token[0] == token[-1] == '"' else [token]
        if not words or any(not TERM.fullmatch(word) for word in words):
            raise Refused("query_invalid")
        if not want:
            parts.append("AND")
        parts.append('"%s"' % " ".join(words))
        terms.append(words)
        want = False
    if want or len(terms) > MAX_TERMS:
        raise Refused("query_invalid")
    return " ".join(parts), terms


def first_offset(body, terms):
    found = [match.start() for words in terms
             for match in [re.search(r"\W+".join(map(re.escape, words)), body, re.IGNORECASE)] if match]
    return str(len(body[:min(found)].encode("utf-8"))) if found else "-"


def query(indexdir, text):
    expression, terms = parse_query(text)
    with contextlib.closing(_open_index(indexdir)) as conn:
        try:
            rows = conn.execute(
                "SELECT p.doc_id, p.page, p.body, d.request, d.agency, d.sha256 FROM pages p JOIN docs d"
                " ON d.agency = p.agency AND d.doc_id = p.doc_id WHERE pages MATCH ?"
                " ORDER BY p.agency, p.doc_id, p.page", (expression,)).fetchall()
        except sqlite3.OperationalError:
            raise Refused("query_invalid") from None
        count, coverage = coverage_lines(conn)
    lines = []
    if rows:
        lines.append("document_id | page | offset | request | agency | sha256")
        lines += [" | ".join((doc_id, str(page), first_offset(body, terms), request, agency, sha))
                  for doc_id, page, body, request, agency, sha in rows]
        lines.append("hits=%d" % len(rows))
    elif count.get("no-text", 0):
        lines.append("no hits in indexed text; %d documents have no extracted text" % count["no-text"])
    else:
        lines.append("no hits in indexed text")
    return lines + coverage


def main(argv=None):
    parser = _Parser(prog="records_search.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", parser_class=_Parser)
    index_cmd = commands.add_parser("index", help="build or update the private index")
    index_cmd.add_argument("--store", required=True)
    index_cmd.add_argument("--out", required=True)
    query_cmd = commands.add_parser("query", help="print cited hits, never text")
    query_cmd.add_argument("--index", required=True)
    query_cmd.add_argument("terms")
    commands.add_parser("stat", help="print index size and coverage").add_argument("--index", required=True)
    try:
        args = parser.parse_args(sys.argv[1:] if argv is None else argv)
        if args.command == "index":
            total, changed, unchanged, removed = build(_absolute(args.store, "store"), _absolute(args.out, "indexdir"))
            with contextlib.closing(_open_index(Path(args.out))) as conn:
                lines = ["index: documents=%d changed=%d unchanged=%d removed=%d" % (total, changed, unchanged, removed)]
                lines += coverage_lines(conn)[1]
        elif args.command == "query":
            lines = query(_absolute(args.index, "index"), args.terms)
        elif args.command == "stat":
            with contextlib.closing(_open_index(_absolute(args.index, "index"))) as conn:
                docs, pages = (conn.execute(sql).fetchone()[0] for sql in
                               ("SELECT count(*) FROM docs", "SELECT count(*) FROM pages"))
                lines = ["index: documents=%d pages=%d" % (docs, pages)] + coverage_lines(conn)[1]
        else:
            raise Refused("usage_invalid")
    except Refused as error:
        print("records_search: refused: %s" % error, file=sys.stderr)
        return 2
    except (OSError, sqlite3.Error):
        print("records_search: refused: index_io_failed", file=sys.stderr)
        return 2
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
