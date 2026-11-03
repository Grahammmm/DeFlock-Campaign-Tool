"""FA55: portal url_parts refuses URL strings that are not strict UTF-8.

A lone surrogate code point survives urlsplit and the existing character
checks, so before the fix url_parts/normalized accepted it and the later
collection/document identity hashing (``url.encode()``) raised
UnicodeEncodeError, possibly after a valid sibling had already been
inventoried. Every input here is synthetic: generated URLs, the existing
synthetic export fixture from tests.records.test_export_bridge and a fresh
temporary store. No network, mailbox, model, credential or live record.

Class map (used by tools/accept_fa55_url_unicode.py):
  A1PositiveTests        valid Unicode / percent bytes / signed queries kept
  A2MalformedTests       lone surrogates refused with exactly invalid_url
  A2PreservationTests    existing host/path/scheme negatives keep their codes
  A3InventoryTests       real accept_page refuses the whole page, no mutation
"""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from campaign_tool.records.export_bridge import enroll_export
from campaign_tool.records.portal import collections, policy, store
from campaign_tool.records.portal.collections import CollectionInventory, Scope, normalized
from campaign_tool.records.portal.policy import PortalError, strict_json, url_parts
from campaign_tool.records.portal.store import Queue
from campaign_tool.records.run import Pipeline, root_lock
from campaign_tool.records.runner.canonical_mail import CanonicalMailBackend
from tests.records.test_export_bridge import BASE, HOST, PARSER, legacy_export

# Name of the strict UTF-8 input gate in policy.py. The A4 negative control
# (tools/accept_fa55_url_unicode.py) bypasses only this function in memory.
GATE = "_utf8_encodable"

HIGH = "\ud83d"   # lone high surrogate
LOW = "\ude00"    # lone low surrogate
ASTRAL = "\U0001f600"
COLLECTION = BASE + "/WebLink/Browse.aspx?id=F1"

MALFORMED = {
    "high_in_path": BASE + "/documents/A" + HIGH,
    "low_in_path": BASE + "/documents/" + LOW + "B",
    "high_in_query": BASE + "/documents/A?page=" + HIGH,
    "low_in_query": COLLECTION + "&q=" + LOW,
    "reversed_pair_in_path": BASE + "/documents/x" + LOW + HIGH,
    "high_in_signed_query": BASE + "/documents/A/download?X-Amz-Date=20260102&sig=" + HIGH,
}
VALID = {
    "accented_path": BASE + "/documents/caf\u00e9",
    "accented_query": COLLECTION + "&q=\u00fcber",
    "astral_path": BASE + "/documents/x" + ASTRAL,
    "astral_query": COLLECTION + "&q=" + ASTRAL,
    "percent_path": BASE + "/documents/caf%C3%A9",
    "percent_query": BASE + "/documents/A?name=caf%C3%A9&raw=%FF&slash=%2F",
    "signed_query": BASE + "/documents/A/download?X-Amz-Date=20260102&sig=fixture-only&b=2&a=1",
}


def identity_encode(url):
    """The collection/document identity path: normalize, then strict UTF-8."""
    return normalized(url).encode("utf-8")


def bypass_utf8_gate():
    """In-memory control: disable only the strict UTF-8 gate (fails if absent)."""
    return mock.patch.object(policy, GATE, lambda url: True)


class InvalidUrlAssertions:
    def assert_invalid_url(self, exc, url):
        self.assertIs(type(exc), PortalError)
        self.assertEqual(exc.args, ("invalid_url",))
        self.assertIsNone(exc.__cause__)
        self.assertTrue(exc.__context__ is None or exc.__suppress_context__)
        for text in (str(exc), repr(exc)):
            self.assertNotIn(url, text)
            self.assertNotIn(HOST, text)
            self.assertFalse(any(0xD800 <= ord(c) <= 0xDFFF for c in text))


class A1PositiveTests(unittest.TestCase):
    def test_a1_valid_unicode_percent_and_signed_urls_preserved_exactly(self):
        for name, url in VALID.items():
            with self.subTest(name):
                parts = url_parts(url)
                self.assertEqual(parts.hostname, HOST)
                self.assertEqual(normalized(url), url)
                self.assertEqual(identity_encode(url), url.encode("utf-8"))

    def test_a1_signed_query_spelling_and_order_unchanged(self):
        url = VALID["signed_query"]
        self.assertEqual(url_parts(url).query, "X-Amz-Date=20260102&sig=fixture-only&b=2&a=1")
        self.assertEqual(normalized(url).split("?", 1)[1], "X-Amz-Date=20260102&sig=fixture-only&b=2&a=1")
        self.assertEqual(url_parts(VALID["percent_query"]).query, "name=caf%C3%A9&raw=%FF&slash=%2F")
        self.assertEqual(url_parts(VALID["percent_path"]).path, "/documents/caf%C3%A9")

    def test_a1_json_surrogate_pair_escape_decodes_to_astral_and_is_accepted(self):
        url = BASE + "/documents/x" + ASTRAL + "?q=" + ASTRAL
        raw = json.dumps({"url": url})
        self.assertIn("\\ud83d\\ude00", raw)  # ASCII JSON escape of a valid pair
        decoded = strict_json(raw)["url"]
        self.assertEqual(decoded, url)
        self.assertFalse(any(0xD800 <= ord(c) <= 0xDFFF for c in decoded))
        self.assertEqual(normalized(decoded), url)
        self.assertEqual(identity_encode(decoded), url.encode("utf-8"))


class A2MalformedTests(InvalidUrlAssertions, unittest.TestCase):
    def test_a2_lone_surrogates_refused_at_url_parts(self):
        for name, url in MALFORMED.items():
            with self.subTest(name):
                with self.assertRaises(PortalError) as caught:
                    url_parts(url)
                self.assert_invalid_url(caught.exception, url)

    def test_a2_lone_surrogates_refused_by_normalized(self):
        for name, url in MALFORMED.items():
            with self.subTest(name):
                with self.assertRaises(PortalError) as caught:
                    normalized(url)
                self.assert_invalid_url(caught.exception, url)

    def test_a2_lone_surrogate_json_escape_refused(self):
        for escape in ("\\ud83d", "\\ude00"):
            with self.subTest(escape):
                raw = '{"url": "' + BASE + '/documents/A?q=' + escape + '"}'
                decoded = strict_json(raw)["url"]  # JSON itself admits lone escapes
                with self.assertRaises(PortalError) as caught:
                    url_parts(decoded)
                self.assert_invalid_url(caught.exception, decoded)

    def test_a2_surrogate_in_host_is_invalid_url(self):
        url = "https://archive" + HIGH + ".example.invalid/documents/A"
        with self.assertRaises(PortalError) as caught:
            url_parts(url)
        self.assert_invalid_url(caught.exception, url)

    def test_a2_refusal_precedes_url_splitting_and_identity_hashing(self):
        refuse = mock.Mock(side_effect=AssertionError("parsed or hashed after refusal point"))
        with mock.patch.object(policy, "urlsplit", refuse), \
                mock.patch.object(collections, "digest_bytes", refuse), \
                mock.patch.object(store, "digest_bytes", refuse):
            for name, url in MALFORMED.items():
                with self.subTest(name):
                    with self.assertRaises(PortalError) as caught:
                        normalized(url)
                    self.assert_invalid_url(caught.exception, url)
        refuse.assert_not_called()


class A2PreservationTests(unittest.TestCase):
    CASES = (
        (123, "invalid_url"),
        (b"https://archive.example.invalid/documents/A", "invalid_url"),
        ("http://archive.example.invalid/documents/A", "invalid_url"),
        ("https://user@archive.example.invalid/documents/A", "invalid_url"),
        ("https://archive.example.invalid:8443/documents/A", "invalid_url"),
        ("https://archive.example.invalid/documents/A#frag", "invalid_url"),
        ("https://archive.example.invalid/documents/../A", "invalid_url"),
        ("https://archive.example.invalid/documents/%2e%2e/A", "invalid_url"),
        ("https://archive.example.invalid/documents/%25A", "invalid_url"),
        ("https://archive.example.invalid/documents/%5CA", "invalid_url"),
        ("https://archive.example.invalid/documents/A B", "invalid_url"),
        ("https://archive.example.invalid/documents/A\x7f", "invalid_url"),
        ("https://archive.example.invalid/" + "a" * 16384, "invalid_url"),
        ("https://archive_example.invalid/documents/A", "invalid_host"),
        ("https://localhost/documents/A", "invalid_host"),
        ("https://archive..example.invalid/documents/A", "invalid_host"),
        ("https://caf\u00e9.example.invalid/documents/A", "invalid_host"),
    )

    def test_a2_existing_negatives_keep_their_codes(self):
        for url, code in self.CASES:
            with self.subTest(repr(url)[:60]):
                with self.assertRaises(PortalError) as caught:
                    url_parts(url)
                self.assertEqual(caught.exception.args, (code,))

    def test_a2_ascii_baseline_and_port_443_still_accepted(self):
        self.assertEqual(normalized("https://ARCHIVE.example.invalid:443/documents/A?x=1"),
                         BASE + "/documents/A?x=1")
        self.assertEqual(normalized(BASE), BASE + "/")


class A3InventoryTests(InvalidUrlAssertions, unittest.TestCase):
    """Real accept_page on the existing synthetic export/collection fixture."""

    TABLES = ("portal_items", "portal_notices", "portal_notice_links", "portal_link_revisions",
              "export_collections", "export_collection_pages", "export_collection_members",
              "export_body_links", "export_events")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.pipeline = Pipeline(self.base / "canonical")
        self.root = self.pipeline.root
        self.export = self.base / "export"
        self.export.mkdir(mode=0o700)
        self.queue = Queue(self.root.path / "portal")
        self.addCleanup(self.queue.close)
        self.inventory = CollectionInventory(self.queue, Scope(frozenset({HOST})))
        self.backend = CanonicalMailBackend(self.export, self.root.intake, self.root.ledger)
        self.backend.start_run(self.pipeline._identity())
        path, _ = legacy_export(self.export, body=COLLECTION + "\n")
        with root_lock(self.root.path):
            self.event = enroll_export(path, export_root=self.export, backend=self.backend,
                                       inventory=self.inventory).message_id
        self.collection = self.queue.db.execute(
            "SELECT id FROM export_collections WHERE url_private=?", (COLLECTION,)).fetchone()[0]
        raw = b"<html>SYNTHETIC FA55 collection page</html>"
        self.source = hashlib.sha256(raw).hexdigest()
        temp = self.queue.objects / (".fixture-" + self.source)
        temp.write_bytes(raw)
        os.chmod(temp, 0o600)
        self.queue.promote(temp, self.source, len(raw))
        temp.unlink()
        self.source_bytes = len(raw)

    def snapshot(self):
        tables = {name: sorted(tuple(row) for row in self.queue.db.execute("SELECT * FROM " + name))
                  for name in self.TABLES}
        files = {}
        for directory in (self.queue.objects, self.export):
            for path in sorted(directory.rglob("*")):
                if path.is_file():
                    files[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
        return tables, files

    def accept(self, members, *, next_cursor=None, cursor=COLLECTION, total=None):
        with root_lock(self.root.path):
            return self.inventory.accept_page(
                self.collection, cursor=cursor, source_sha256=self.source,
                source_bytes=self.source_bytes, parser_sha256=PARSER, members=members,
                next_cursor=next_cursor, expected_total=total, event=self.event)

    def assert_page_refused_without_mutation(self, url, **page):
        before = self.snapshot()
        spies = [mock.patch.object(Queue, "inventory", autospec=True, side_effect=Queue.inventory),
                 mock.patch.object(CollectionInventory, "_collection", autospec=True,
                                   side_effect=CollectionInventory._collection),
                 mock.patch.object(CollectionInventory, "_document", autospec=True,
                                   side_effect=CollectionInventory._document)]
        with spies[0] as inventory, spies[1] as collection, spies[2] as document:
            with self.assertRaises(PortalError) as caught:
                self.accept(**page)
        self.assert_invalid_url(caught.exception, url)
        for spy in (inventory, collection, document):
            spy.assert_not_called()  # refused before any identity hashing/store write
        self.assertEqual(self.snapshot(), before)
        node = self.queue.db.execute("SELECT state,cursor_private,expected_total FROM export_collections "
                                     "WHERE id=?", (self.collection,)).fetchone()
        self.assertEqual(tuple(node), ("pending", COLLECTION, None))

    def assert_valid_sibling_page_still_accepted(self, sibling):
        self.accept([sibling], total=1)
        self.assertEqual(self.queue.db.execute("SELECT count(*) FROM export_collection_members").fetchone()[0], 1)
        replay = self.snapshot()
        self.accept([sibling], total=1)  # immutable replay
        self.assertEqual(self.snapshot(), replay)

    def test_a3_document_member_with_lone_surrogate_refuses_whole_page(self):
        sibling = {"id": "B", "kind": "document", "url": BASE + "/documents/B"}
        bad = BASE + "/documents/C?v=" + LOW
        self.assert_page_refused_without_mutation(
            bad, members=[sibling, {"id": "C", "kind": "document", "url": bad}], total=2)
        self.assert_valid_sibling_page_still_accepted(sibling)

    def test_a3_collection_member_with_lone_surrogate_refuses_whole_page(self):
        sibling = {"id": "B", "kind": "document", "url": BASE + "/documents/B"}
        bad = BASE + "/WebLink/Browse.aspx?id=F9" + HIGH
        self.assert_page_refused_without_mutation(
            bad, members=[sibling, {"id": "F9", "kind": "collection", "url": bad}], total=2)
        self.assert_valid_sibling_page_still_accepted(sibling)

    def test_a3_cursor_or_next_cursor_with_lone_surrogate_refuses_page(self):
        sibling = {"id": "B", "kind": "document", "url": BASE + "/documents/B"}
        bad_next = COLLECTION + "&page=2" + LOW
        self.assert_page_refused_without_mutation(bad_next, members=[sibling], next_cursor=bad_next, total=2)
        bad_cursor = COLLECTION + HIGH
        self.assert_page_refused_without_mutation(bad_cursor, members=[sibling], cursor=bad_cursor, total=1)
        self.assert_valid_sibling_page_still_accepted(sibling)


if __name__ == "__main__":
    unittest.main()
