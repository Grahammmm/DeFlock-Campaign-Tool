"""FA56: blank query declarations cannot hide synthetic collection identity.

Offline and synthetic only. The SyntheticHTML parser must keep blank query
values on both the base URL and a next cursor, so an extra blank id or an
unknown blank key is rejected with collection_identity_mismatch instead of
being silently discarded by parse_qs. Valid single-identity pagination,
encoded identity values, the optional page parameter and exact cursor/member
URL spelling keep their existing behavior.

Test names containing "_ambiguous_" are the blank-ambiguity regressions; the
acceptance runner's negative control expects exactly those to fail when the
old blank-dropping query parse is restored, and every other test to pass.
"""
import html
import unittest

from campaign_tool.records.portal import collection_pages as pages
from campaign_tool.records.portal.engine import COLLECTION_REQUEST
from campaign_tool.records.portal.policy import PortalError
from tests import test_records_collection_pages as existing

HOST = existing.HOST
ORIGIN = "https://" + HOST
BROWSE = ORIGIN + "/browse"
MISMATCH = "collection_identity_mismatch"

# Base-URL queries for collection "A": blank declarations that the old parser
# dropped (and therefore accepted) versus controls it already rejected.
BASE_AMBIGUOUS = {
    "id_then_blank_id": "id=A&id=",
    "blank_id_then_id": "id=&id=A",
    "unknown_blank_key": "id=A&extra=",
    "unknown_blank_key_with_page": "id=A&page=2&extra=",
}
BASE_ALREADY_INVALID = {
    "blank_id_alone": "id=",
    "nonblank_duplicate_id": "id=A&id=B",
    "missing_id": "page=2",
    "no_query": "",
}
# Next-cursor queries relative to a valid base ?id=A.
NEXT_AMBIGUOUS = {
    "id_then_blank_id": "id=A&page=2&id=",
    "blank_id_then_id": "id=&id=A&page=2",
    "unknown_blank_key": "id=A&page=2&extra=",
    "unknown_blank_key_without_page": "id=A&extra=",
}
NEXT_ALREADY_INVALID = {
    "blank_id_alone": "id=&page=2",
    "nonblank_duplicate_id": "id=A&page=2&id=B",
    "missing_id": "page=2",
}


def markup(collection, total, members=(), next_href=None, empty=False):
    """Otherwise-valid synthetic-archive-v1 page; identity is the only variable."""
    attrs = ['data-fixture-provider="synthetic-archive-v1"',
             'data-collection="' + html.escape(collection) + '"',
             'data-total="' + str(total) + '"']
    if next_href is None:
        attrs.append('data-terminal="true"')
    body = "".join('<a data-record-id="' + m + '" href="/documents/' + m + '/download">' + m + "</a>"
                   for m in members)
    if empty:
        body += '<p data-empty="true">No synthetic records.</p>'
    if next_href is not None:
        body += '<a rel="next" href="' + html.escape(next_href) + '">Next</a>'
    return ("<!doctype html><html><body><main " + " ".join(attrs) + ">" + body
            + "</main></body></html>\n").encode()


def url(query):
    return BROWSE + ("?" + query if query else "")


def parse(raw, address):
    return pages.SyntheticProfile().parse(raw, address)


class A1PositiveAndBoundaryTests(unittest.TestCase):
    def test_a1_terminal_empty_single_identity_parses(self):
        parsed = parse(markup("A", 0, empty=True), url("id=A"))
        self.assertEqual(parsed, pages.ParsedPage((), None, 0))

    def test_a1_ordinary_multipage_keeps_exact_cursor_and_member_urls(self):
        first = parse(markup("A", 3, ("B", "C"), next_href="/browse?id=A&page=2"), url("id=A"))
        self.assertEqual(first.next_cursor, BROWSE + "?id=A&page=2")
        self.assertEqual([(m.id, m.kind, m.url) for m in first.members],
                         [("B", "document", ORIGIN + "/documents/B/download"),
                          ("C", "document", ORIGIN + "/documents/C/download")])
        self.assertEqual(first.expected_total, 3)
        last = parse(markup("A", 3, ("C", "D")), first.next_cursor)
        self.assertIsNone(last.next_cursor)
        self.assertEqual([m.url for m in last.members],
                         [ORIGIN + "/documents/C/download", ORIGIN + "/documents/D/download"])

    def test_a1_page_first_cursor_spelling_is_preserved(self):
        parsed = parse(markup("A", 3, ("B",), next_href="/browse?page=2&id=A"), url("id=A"))
        self.assertEqual(parsed.next_cursor, BROWSE + "?page=2&id=A")

    def test_a1_encoded_identity_value_is_compared_decoded_but_kept_verbatim(self):
        base = url("id=rec.ords%3A1")
        parsed = parse(markup("rec.ords:1", 2, ("B",), next_href="/browse?id=rec.ords%3A1&page=2"), base)
        self.assertEqual(parsed.next_cursor, BROWSE + "?id=rec.ords%3A1&page=2")
        terminal = parse(markup("rec.ords:1", 2, ("C",)), parsed.next_cursor)
        self.assertEqual([m.id for m in terminal.members], ["C"])

    def test_a1_optional_page_parameter_behavior_is_retained(self):
        # No page-value grammar is added: a blank or ordinary page stays allowed.
        for query in ("id=A&page=", "id=A&page=2", "page=7&id=A"):
            with self.subTest(base=query):
                self.assertEqual(parse(markup("A", 0, empty=True), url(query)).expected_total, 0)
        parsed = parse(markup("A", 2, ("B",), next_href="/browse?id=A&page="), url("id=A"))
        self.assertEqual(parsed.next_cursor, BROWSE + "?id=A&page=")

    def test_a1_missing_or_blank_identity_remains_invalid(self):
        for query in ("", "page=2", "id="):
            with self.subTest(base=query):
                with self.assertRaises(PortalError) as caught:
                    parse(markup("A", 0, empty=True), url(query))
                self.assertEqual(str(caught.exception), MISMATCH)


class A2MalformedQueryTests(unittest.TestCase):
    def base_raw(self, query):
        # Pages under a page=2 base are still terminal one-member pages.
        return markup("A", 1, ("B",))

    def next_raw(self, query):
        return markup("A", 3, ("B", "C"), next_href="/browse?" + query)

    def assert_mismatch(self, raw, address):
        with self.assertRaises(PortalError) as caught:
            parse(raw, address)
        self.assertEqual(str(caught.exception), MISMATCH)

    def test_a2_controls_markup_is_otherwise_valid(self):
        # The deciding variable is only the query: the same markup parses with
        # a clean identity, so a rejection cannot come from an unrelated gate.
        for name, query in {**BASE_AMBIGUOUS, **BASE_ALREADY_INVALID}.items():
            with self.subTest(base=name):
                self.assertEqual(parse(self.base_raw(query), url("id=A")).expected_total, 1)
        for name, query in {**NEXT_AMBIGUOUS, **NEXT_ALREADY_INVALID}.items():
            with self.subTest(next=name):
                clean = parse(self.next_raw("id=A&page=2"), url("id=A"))
                self.assertEqual(clean.next_cursor, BROWSE + "?id=A&page=2")
                self.assertEqual(len(clean.members), 2)

    def test_a2_base_ambiguous_blank_declarations_are_identity_mismatch(self):
        for name, query in BASE_AMBIGUOUS.items():
            with self.subTest(base=name):
                self.assert_mismatch(self.base_raw(query), url(query))

    def test_a2_next_ambiguous_blank_declarations_are_identity_mismatch(self):
        for name, query in NEXT_AMBIGUOUS.items():
            with self.subTest(next=name):
                self.assert_mismatch(self.next_raw(query), url("id=A"))

    def test_a2_base_blank_alone_and_nonblank_duplicate_stay_rejected(self):
        for name, query in BASE_ALREADY_INVALID.items():
            with self.subTest(base=name):
                self.assert_mismatch(self.base_raw(query), url(query))

    def test_a2_next_blank_alone_and_nonblank_duplicate_stay_rejected(self):
        for name, query in NEXT_ALREADY_INVALID.items():
            with self.subTest(next=name):
                self.assert_mismatch(self.next_raw(query), url("id=A"))


class A3ConsumerTests(unittest.TestCase):
    """Real run_collection_pages with the existing offline fixture and transport."""
    setUp = existing.CollectionPageTests.setUp
    enroll = existing.CollectionPageTests.enroll
    run_pages = existing.CollectionPageTests.run_pages
    count = existing.CollectionPageTests.count

    def nodes(self):
        return {r["url_private"]: dict(r) for r in self.queue.db.execute("SELECT * FROM export_collections")}

    def page_item_reasons(self):
        return sorted(r[0] for r in self.queue.db.execute(
            "SELECT reason FROM portal_items WHERE request_id=?", (COLLECTION_REQUEST,)))

    def assert_nothing_published(self, result, bases, transport):
        self.assertEqual((result["indexed"], result["blocked"]), (0, len(bases)))
        self.assertFalse(result["retrieval_complete"])
        self.assertEqual(sorted(transport.calls), sorted(bases))
        self.assertEqual(self.count("export_collection_pages"), 0)
        self.assertEqual(self.count("export_collection_members"), 0)
        self.assertEqual(self.queue.db.execute("SELECT count(*) FROM portal_items WHERE request_id!=?",
                                               (COLLECTION_REQUEST,)).fetchone()[0], 0)
        self.assertEqual(self.page_item_reasons(), [MISMATCH] * len(bases))
        nodes = self.nodes()
        for base in bases:
            node = nodes[base]
            self.assertEqual((node["state"], node["reason"], node["expected_total"], node["cursor_private"]),
                             ("blocked", "unsupported_navigation", None, base))

    def test_a3_ambiguous_next_cursor_cannot_publish_or_advance(self):
        names = ("qa", "qb", "qc", "qd")
        variants = ("id=qa&page=2&id=", "id=&id=qb&page=2", "id=qc&page=2&extra=", "id=qd&extra=")
        value = self.enroll(*names)
        responses = {}
        for name, query in zip(names, variants):
            responses[BROWSE + "?id=" + name] = markup(name, 3, ("B", "C"), next_href="/browse?" + query)
            # Present only so the old parser's mistaken follow is observable.
            responses[BROWSE + "?" + query] = markup(name, 3, ("C", "D"))
        transport = existing.Transport(responses)
        result = self.run_pages(value, transport)
        self.assert_nothing_published(result, [BROWSE + "?id=" + n for n in names], transport)

    def test_a3_ambiguous_base_identity_cannot_publish(self):
        queries = ("id=A&id=", "id=&id=A", "id=A&extra=")
        value = self.enroll(*(q[len("id="):] for q in queries))
        bases = [url(q) for q in queries]
        transport = existing.Transport({base: markup("A", 1, ("B",)) for base in bases})
        result = self.run_pages(value, transport)
        self.assert_nothing_published(result, bases, transport)

    def test_a3_nonblank_duplicate_next_cursor_stays_rejected(self):
        value = self.enroll("dup")
        base = BROWSE + "?id=dup"
        transport = existing.Transport({base: markup("dup", 3, ("B",), next_href="/browse?id=dup&id=B")})
        result = self.run_pages(value, transport)
        self.assert_nothing_published(result, [base], transport)

    def test_a3_valid_pagination_scope_and_denominator_unchanged(self):
        value = self.enroll("A", "short")
        first, second = BROWSE + "?id=A", BROWSE + "?id=A&page=2"
        short = BROWSE + "?id=short"
        transport = existing.Transport({
            first: markup("A", 3, ("B", "C"), next_href="/browse?id=A&page=2"),
            second: markup("A", 3, ("C", "D")),
            short: markup("short", 2, ("E",)),  # Terminal page below its denominator.
        })
        result = self.run_pages(value, transport)
        self.assertEqual((result["indexed"], result["blocked"]), (2, 1))
        self.assertEqual(sorted(transport.calls), sorted([first, second, short]))
        cursors = sorted(r[0] for r in self.queue.db.execute("SELECT cursor_private FROM export_collection_pages"))
        self.assertEqual(cursors, [first, second])
        members = sorted(tuple(r) for r in self.queue.db.execute(
            "SELECT member,url_private FROM export_collection_members"))
        self.assertEqual(members, [(m, ORIGIN + "/documents/" + m + "/download") for m in "BCD"])
        nodes = self.nodes()
        self.assertEqual((nodes[first]["state"], nodes[first]["expected_total"]), ("enumerated", 3))
        self.assertEqual(nodes[short]["state"], "blocked")


if __name__ == "__main__":
    unittest.main()
