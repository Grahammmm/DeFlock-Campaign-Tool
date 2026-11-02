"""FA24 regression: an anchor with more than one href attribute is ambiguous.

Synthetic-only: every host is under the reserved .invalid TLD, no socket,
model, fetch or acquisition call is made, and the real-queue case writes only
below tempfile.gettempdir() (the acceptance helper points that at a fresh
per-run directory outside the repository).

Requirement map (card t_09b2b889):
  A1 positive path      LinksParserTests.test_a1_*, InventoryIntegrationTests.test_a1_*
  A2 negative path      AmbiguousAnchorTests.test_a2_*, InventoryIntegrationTests.test_a2_*
  A3 boundary/recovery  LinksParserTests.test_a3_*
  A4 broken control     BaselineDictLinks + ambiguous_href_urls (used by the helper's
                        negative-control case), InventoryIntegrationTests.test_a4_*
  A5 integration        InventoryIntegrationTests.test_a5_*
  A6 security           InventoryIntegrationTests.test_a6_*, WriteRootTests.test_a6_*
                        (helper write guard: in-root positive, inert escape-write
                        refusal), module teardown full-tree content-hash check
"""
import hashlib
import os
from html.parser import HTMLParser
from pathlib import Path
import re
import socket
import subprocess
import tempfile
import unittest
from unittest import mock
import urllib.request

from campaign_tool.records.portal import Queue
from campaign_tool.records.portal import notice

REPO = Path(__file__).resolve().parents[1]
HOST = "portal.records.invalid"
OTHER_HOST = "elsewhere.records.invalid"
REQUEST = "26-024"
SOURCE = hashlib.sha256(b"fa24 synthetic notice").hexdigest()
FIRST_URL = f"https://{HOST}/documents/first-1/download?token=synthetic-a&part=1"
SECOND_URL = f"https://{HOST}/documents/second-2/download?token=synthetic-b"
VALID_URL = f"https://{HOST}/documents/valid-3/download?token=synthetic-c"
TEXT_URL = f"https://{HOST}/documents/text-4"


def esc(url):
    return url.replace("&", "&amp;")


AMBIGUOUS_BODIES = {
    "distinct": f'<a href="{esc(FIRST_URL)}" href="{SECOND_URL}">x</a>',
    "identical": f'<a href="{SECOND_URL}" href="{SECOND_URL}">x</a>',
    "mixed_case": f'<A HREF="{esc(FIRST_URL)}" hReF="{SECOND_URL}">x</A>',
    "empty_then_value": f'<a href="" href="{SECOND_URL}">x</a>',
    "value_then_empty": f'<a href="{SECOND_URL}" href="">x</a>',
    "valueless_then_value": f'<a href href="{SECOND_URL}">x</a>',
    "self_closing": f'<a href="{esc(FIRST_URL)}" href="{SECOND_URL}"/>',
    "unquoted": f"<a href={SECOND_URL} href={SECOND_URL}>x</a>",
    "with_other_attrs": f'<a class="c" href="{esc(FIRST_URL)}" title="t" href="{SECOND_URL}" rel="r">x</a>',
}

# Bodies without ambiguous anchors: the candidate must extract exactly what the
# preserved dict-based baseline extracted (A1/A3 relational preservation).
UNAMBIGUOUS_BODIES = (
    "",
    "no links here",
    f'<a href="{esc(FIRST_URL)}">x</a>',
    f'<A HREF="{SECOND_URL}">x</A>',
    f'<a name="anchor">x</a>',
    "<a href>x</a>",
    '<a href="">x</a>',
    f'<a id="i" class="c" data-x="y" href="{VALID_URL}" title="t" rel="noopener" target="_blank">x</a>',
    f"see {TEXT_URL} and {VALID_URL}",
    f'<p>{TEXT_URL}</p><a href="{VALID_URL}">{VALID_URL}</a><a href="{esc(FIRST_URL)}">f</a>',
    f'<link href="{SECOND_URL}"><img src="{SECOND_URL}"><area href="{SECOND_URL}">',
    f'<a href="{VALID_URL}"/>',
)


class BaselineDictLinks(HTMLParser):
    """Preserved pinned-main (9c398829) Links behavior: dict(attrs) keeps the LAST href.

    Inert test double for the A4 broken control only; never used in product code.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.urls = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "a":
            value = dict(attrs).get("href")
            if value:
                self.urls.append(value)

    def handle_data(self, data):
        self.urls.extend(re.findall(r'https://[^\s<>"\']+', data))


def parse(body, parser_cls=None):
    parser = (parser_cls or notice.Links)()
    parser.feed(body)
    parser.close()
    return parser.urls


def ambiguous_href_urls(parser_cls):
    """URLs a parser derives from the single distinct-duplicate anchor body."""
    return parse(AMBIGUOUS_BODIES["distinct"], parser_cls)


class QueueSpy:
    """Synthetic queue double recording exact inventory() calls."""

    def __init__(self):
        self.calls = []

    def inventory(self, host, request_id, item_id, url, source_sha256):
        self.calls.append((host, request_id, item_id, url, source_sha256))
        return (host, request_id, item_id)


def run_inventory(queue, body, known_hosts=frozenset({HOST})):
    return notice.inventory_notice(queue, body=body, request_id=REQUEST,
                                   source_sha256=SOURCE, known_hosts=set(known_hosts))


# Top-level directories .gitignore marks as scratch/build output. Parallel CI chunks
# create and delete files there (e.g. build/outcome-factsheets sqlite WAL files)
# while this module's snapshot walks the tree, so they are not retained content.
SCRATCH_TOP_LEVEL = frozenset({"build", "dist", ".venv"})


def repository_snapshot(root=REPO):
    """Content hash of every retained repository file (fixtures included).

    Skips .git, bytecode and the top-level ignored scratch dirs. Any other file
    that is listed but unreadable still raises: tracked content is never skipped.
    """
    hashes = {}
    for directory, dirs, files in os.walk(root):
        at_root = Path(directory) == Path(root)
        dirs[:] = [d for d in dirs
                   if d not in (".git", "__pycache__") and not (at_root and d in SCRATCH_TOP_LEVEL)]
        for name in files:
            path = Path(directory) / name
            hashes[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


def run_root():
    """Per-run root published by the acceptance helper's write guard, or None standalone."""
    value = os.environ.get("FA24_ACCEPT_RUN_ROOT")
    return Path(value) if value else None


_SNAPSHOT = None


def setUpModule():
    global _SNAPSHOT
    _SNAPSHOT = repository_snapshot()


def tearDownModule():
    after = repository_snapshot()
    if after != _SNAPSHOT:
        changed = sorted(k for k in set(after) | set(_SNAPSHOT) if after.get(k) != _SNAPSHOT.get(k))
        raise AssertionError(f"A6: retained repository bytes changed during the regression run: {changed}")


class LinksParserTests(unittest.TestCase):
    def test_a1_single_href_kept(self):
        self.assertEqual(parse(f'<a href="{VALID_URL}">x</a>'), [VALID_URL])

    def test_a1_entity_escaped_query_is_decoded_once(self):
        self.assertEqual(parse(f'<a href="{esc(FIRST_URL)}">x</a>'), [FIRST_URL])

    def test_a1_mixed_case_tag_and_attribute(self):
        self.assertEqual(parse(f'<A HrEf="{VALID_URL}">x</A>'), [VALID_URL])

    def test_a1_text_node_https_links_kept(self):
        self.assertEqual(parse(f"<p>see {TEXT_URL} now</p>"), [TEXT_URL])

    def test_a1_independent_valid_anchors_kept_in_order(self):
        body = f'<a href="{VALID_URL}">v</a><a href="{esc(FIRST_URL)}">f</a>'
        self.assertEqual(parse(body), [VALID_URL, FIRST_URL])

    def test_a3_zero_href_anchor_contributes_nothing(self):
        for body in ('<a name="n">x</a>', "<a>x</a>", "<a href>x</a>", '<a href="">x</a>'):
            with self.subTest(body=body):
                self.assertEqual(parse(body), [])

    def test_a3_unrelated_attributes_do_not_block_single_href(self):
        body = (f'<a id="i" class="c" data-href="{SECOND_URL}" title="t" href="{VALID_URL}" '
                'rel="noopener" target="_blank">x</a>')
        self.assertEqual(parse(body), [VALID_URL])

    def test_a3_non_anchor_href_attributes_still_ignored(self):
        self.assertEqual(parse(f'<link href="{SECOND_URL}"><area href="{SECOND_URL}">'), [])

    def test_a3_unambiguous_bodies_match_preserved_baseline(self):
        for body in UNAMBIGUOUS_BODIES:
            with self.subTest(body=body):
                self.assertEqual(parse(body), parse(body, BaselineDictLinks))

    def test_a3_parser_recovers_after_ambiguous_anchor(self):
        parser = notice.Links()
        parser.feed(AMBIGUOUS_BODIES["distinct"])
        parser.feed(f'<a href="{VALID_URL}">v</a>')
        parser.close()
        self.assertEqual(parser.urls, [VALID_URL])


class AmbiguousAnchorTests(unittest.TestCase):
    def test_a2_distinct_duplicate_href_contributes_no_url(self):
        self.assertEqual(ambiguous_href_urls(notice.Links), [])

    def test_a2_every_duplicate_href_shape_contributes_no_url(self):
        for name, body in AMBIGUOUS_BODIES.items():
            with self.subTest(shape=name):
                self.assertEqual(parse(body), [])

    def test_a2_neither_first_nor_last_href_is_selected(self):
        urls = parse(AMBIGUOUS_BODIES["distinct"] + AMBIGUOUS_BODIES["mixed_case"])
        self.assertNotIn(FIRST_URL, urls)
        self.assertNotIn(SECOND_URL, urls)

    def test_a2_ambiguous_anchor_does_not_suppress_valid_sibling(self):
        for name, body in AMBIGUOUS_BODIES.items():
            with self.subTest(shape=name):
                before = parse(f'<a href="{VALID_URL}">v</a>' + body)
                after = parse(body + f'<a href="{VALID_URL}">v</a>')
                self.assertEqual(before, [VALID_URL])
                self.assertEqual(after, [VALID_URL])

    def test_a2_text_node_inside_ambiguous_anchor_is_unchanged(self):
        body = f'<a href="{esc(FIRST_URL)}" href="{SECOND_URL}">{TEXT_URL}</a>'
        self.assertEqual(parse(body), [TEXT_URL])
        self.assertEqual(parse(body, BaselineDictLinks), [SECOND_URL, TEXT_URL])


class InventoryIntegrationTests(unittest.TestCase):
    def mixed_notice(self):
        return ("<html><body><p>Synthetic notice.</p>"
                + "".join(AMBIGUOUS_BODIES.values())
                + f'<a class="doc" href="{VALID_URL}">valid</a></body></html>')

    def test_a5_mixed_notice_inventories_only_the_valid_anchor(self):
        spy = QueueSpy()
        self.assertEqual(run_inventory(spy, self.mixed_notice()), {"items": 1})
        self.assertEqual(spy.calls, [(HOST, REQUEST, "valid-3", VALID_URL, SOURCE)])

    def test_a2_ambiguous_only_notice_makes_no_inventory_call(self):
        for name, body in AMBIGUOUS_BODIES.items():
            with self.subTest(shape=name):
                spy = QueueSpy()
                self.assertEqual(run_inventory(spy, body), {"items": 0})
                self.assertEqual(spy.calls, [])

    def test_a4_single_href_inventories_once(self):
        spy = QueueSpy()
        self.assertEqual(run_inventory(spy, f'<a href="{VALID_URL}">v</a>'), {"items": 1})
        self.assertEqual(spy.calls, [(HOST, REQUEST, "valid-3", VALID_URL, SOURCE)])

    def test_a1_anchor_and_text_duplicates_deduplicate(self):
        spy = QueueSpy()
        body = f'<a href="{esc(FIRST_URL)}">{FIRST_URL}</a> {FIRST_URL}'
        self.assertEqual(run_inventory(spy, body), {"items": 1})
        self.assertEqual(spy.calls, [(HOST, REQUEST, "first-1", FIRST_URL, SOURCE)])

    def test_a2_unknown_host_and_malformed_ids_still_skip(self):
        spy = QueueSpy()
        body = (f'<a href="https://{OTHER_HOST}/documents/9/download">o</a>'
                f'<a href="https://{HOST}/documents/bad!id/download">b</a>'
                f'<a href="https://{HOST}/documents/-lead">l</a>'
                f'<a href="https://{HOST}/other/9">p</a>'
                f'<a href="http://{HOST}/documents/9">h</a>'
                f'<a href="{VALID_URL}">v</a>')
        self.assertEqual(run_inventory(spy, body), {"items": 1})
        self.assertEqual(spy.calls, [(HOST, REQUEST, "valid-3", VALID_URL, SOURCE)])

    def test_a5_real_queue_receipt_invariants(self):
        root = Path(tempfile.mkdtemp(prefix="fa24-queue-"))
        self.assertNotIn(REPO, root.resolve().parents)
        if run_root() is not None:
            self.assertIn(run_root(), root.resolve().parents)
        queue = Queue(root / "queue")
        try:
            self.assertEqual(run_inventory(queue, self.mixed_notice()), {"items": 1})
            items = queue.status()["items"]
            self.assertEqual(len(items), 1)
            row = queue.db.execute("SELECT item_id,last_url_private,generation FROM portal_items").fetchall()
            self.assertEqual([tuple(r) for r in row], [("valid-3", VALID_URL, 1)])
            dump = "\n".join(queue.db.iterdump())
            for absent in ("first-1", "second-2", "synthetic-a", "synthetic-b"):
                self.assertNotIn(absent, dump)
            # Replaying the same notice is idempotent: no new item or generation.
            self.assertEqual(run_inventory(queue, self.mixed_notice()), {"items": 1})
            row = queue.db.execute("SELECT item_id,generation FROM portal_items").fetchall()
            self.assertEqual([tuple(r) for r in row], [("valid-3", 1)])
        finally:
            queue.close()

    def test_a6_no_socket_fetch_or_subprocess_call(self):
        hits = []

        def record(name):
            def guard(*args, **kwargs):
                hits.append(name)
                raise AssertionError(f"A6: unexpected {name}")
            return guard
        with mock.patch.object(socket.socket, "connect", record("connect")), \
                mock.patch.object(socket, "create_connection", record("create_connection")), \
                mock.patch.object(socket, "getaddrinfo", record("getaddrinfo")), \
                mock.patch.object(urllib.request, "urlopen", record("urlopen")), \
                mock.patch.object(subprocess, "Popen", record("Popen")):
            spy = QueueSpy()
            run_inventory(spy, self.mixed_notice())
        self.assertEqual(hits, [])
        self.assertTrue(spy.calls)

    def test_a6_rejects_oversize_and_bad_identity_before_parsing(self):
        spy = QueueSpy()
        with mock.patch.object(notice, "Links", side_effect=AssertionError("parsed")):
            for kwargs in ({"body": "x" * (2 * 1024 * 1024 + 1)}, {"request_id": "../x"},
                           {"source_sha256": "not-a-hash"}):
                with self.subTest(field=next(iter(kwargs))), self.assertRaises(notice.PortalError):
                    args = {"body": "", "request_id": REQUEST, "source_sha256": SOURCE, "known_hosts": {HOST}}
                    args.update(kwargs)
                    notice.inventory_notice(spy, **args)
        self.assertEqual(spy.calls, [])


class RepositorySnapshotTests(unittest.TestCase):
    """The A6 snapshot ignores parallel-chunk scratch output but still catches tracked writes."""

    def make_tree(self):
        root = Path(tempfile.mkdtemp(prefix="fa24-snapshot-"))
        (root / "tests").mkdir()
        (root / "tests" / "tracked.txt").write_bytes(b"original")
        (root / "build" / "outcome-factsheets").mkdir(parents=True)
        return root

    def test_a6_build_scratch_churn_is_not_part_of_the_snapshot(self):
        root = self.make_tree()
        before = repository_snapshot(root)
        wal = root / "build" / "outcome-factsheets" / "ledger.sqlite-wal"
        wal.write_bytes(b"temporary")
        self.assertEqual(repository_snapshot(root), before)
        wal.unlink()
        self.assertEqual(repository_snapshot(root), before)

    def test_a6_vanishing_scratch_file_does_not_break_the_walk(self):
        root = self.make_tree()
        wal = root / "build" / "outcome-factsheets" / "ledger.sqlite-wal"
        wal.write_bytes(b"temporary")
        real_read_bytes = Path.read_bytes

        def racing_read_bytes(path):
            if path.name == "ledger.sqlite-wal":
                raise FileNotFoundError(path)
            return real_read_bytes(path)

        with mock.patch.object(Path, "read_bytes", racing_read_bytes):
            self.assertEqual(sorted(repository_snapshot(root)), [os.path.join("tests", "tracked.txt")])

    def test_a6_tracked_write_is_still_detected(self):
        root = self.make_tree()
        before = repository_snapshot(root)
        (root / "tests" / "tracked.txt").write_bytes(b"modified")
        self.assertNotEqual(repository_snapshot(root), before)
        (root / "tests" / "tracked.txt").write_bytes(b"original")
        (root / "tests" / "new_file.txt").write_bytes(b"added")
        self.assertNotEqual(repository_snapshot(root), before)

    def test_a6_missing_non_scratch_file_is_not_swallowed(self):
        root = self.make_tree()
        real_read_bytes = Path.read_bytes

        def racing_read_bytes(path):
            if path.name == "tracked.txt":
                raise FileNotFoundError(path)
            return real_read_bytes(path)

        with mock.patch.object(Path, "read_bytes", racing_read_bytes):
            with self.assertRaises(FileNotFoundError):
                repository_snapshot(root)

    def test_a6_nested_build_directory_is_still_snapshotted(self):
        root = self.make_tree()
        (root / "tests" / "build").mkdir()
        (root / "tests" / "build" / "fixture.txt").write_bytes(b"retained")
        self.assertIn(os.path.join("tests", "build", "fixture.txt"), repository_snapshot(root))


class WriteRootTests(unittest.TestCase):
    """A6: writes stay under the helper's fresh per-run root (requires the acceptance helper)."""

    def setUp(self):
        if run_root() is None:
            self.skipTest("write guard is installed only by tools/accept_fa24_notice_href.py")

    def test_a6_temp_root_is_inside_per_run_root(self):
        self.assertIn(run_root(), Path(tempfile.gettempdir()).resolve().parents)

    def test_a6_in_root_write_is_allowed(self):
        target = Path(tempfile.mkdtemp(prefix="fa24-write-")) / "positive.txt"
        target.write_bytes(b"synthetic")
        self.assertEqual(target.read_bytes(), b"synthetic")
        self.assertIn(run_root(), target.resolve().parents)

    def test_a6_escape_write_is_refused_before_the_syscall(self):
        # Inert: every target sits below a nonexistent directory and the rename
        # source does not exist, so unguarded calls would raise FileNotFoundError
        # and write nothing; only the guard raises PermissionError("...write guard...").
        escape = Path(os.sep) / f"fa24-escape-{os.getpid()}-{SOURCE[:12]}" / "probe.txt"
        missing_source = Path(tempfile.gettempdir()) / "fa24-missing-source"
        self.assertFalse(escape.parent.exists())
        self.assertFalse(missing_source.exists())
        for attempt in (lambda: open(escape, "w"), lambda: os.open(escape, os.O_WRONLY | os.O_CREAT),
                        lambda: os.mkdir(escape), lambda: os.replace(missing_source, escape)):
            with self.subTest(attempt=attempt), self.assertRaisesRegex(PermissionError, "write guard"):
                attempt()
        self.assertFalse(escape.parent.exists())


if __name__ == "__main__":
    unittest.main()
