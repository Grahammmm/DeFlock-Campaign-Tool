"""Offline NextRequest contract fixtures, not captured or agency-derived pages.

No fixture proves live-layout qualification. Network calls fail the tests.
"""
import hashlib
import html
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from campaign_tool.records.export_bridge import enroll_export
from campaign_tool.records.portal import collection_pages as pages
from campaign_tool.records.portal.collections import CollectionInventory, Scope
from campaign_tool.records.portal.engine import Response, Limits, COLLECTION_REQUEST
from campaign_tool.records.portal.nextrequest import NextRequestProfile
from campaign_tool.records.portal.policy import Approval, PortalError
from campaign_tool.records.run import root_lock
from tests import test_records_collection_pages as existing
from tests.records import test_export_bridge as bridge

HOST = "fictional-fixture.nextrequest.com"
BASE = "https://" + HOST
URL = BASE + "/requests/26-001/documents"
PROFILE = "nextrequest-v1"


def row(key, filename=None, released="2026-01-02", token="fixture-token"):
    filename = filename or key + ".pdf"
    stamp = '<time datetime="' + released + '">Released</time>' if released else ""
    return ('<tr><td><a href="/documents/' + key + '/download?token=' + token + '">'
            + html.escape(filename) + '</a></td><td>' + stamp + '</td></tr>')


def page(total, rows="", next_href=None, extra="", empty=False):
    nav = '<a rel="next" href="' + html.escape(next_href) + '">Next</a>' if next_href else ""
    return ('<!doctype html><html><head><title>Public request</title>'
            '<script src="/assets/application.js"></script></head><body>' + extra
            + '<section id="documents" data-total="' + str(total) + '"><h2>Documents ('
            + str(total) + ')</h2><table><tbody>' + rows + '</tbody></table>'
            + ('<p>No released documents</p>' if empty else "")
            + nav + '</section></body></html>').encode()


SINGLE = page(1, row("D1", "Synthetic & sample.pdf"))
FIRST = page(3, row("D1") + row("D2"), "/requests/26-001/documents?page=2")
LAST = page(3, row("D2") + row("D3"))
LOGIN = page(1, row("D1"), extra='<form action="/users/sign_in"><input type="password"></form>')
EXPIRED = page(1, row("D1"), extra='<h1>This link has expired</h1>')
EMPTY = page(0, empty=True)


class Offline(unittest.TestCase):
    def setUp(self):
        for target in ("socket.create_connection", "socket.socket.connect", "socket.getaddrinfo"):
            guard = patch(target, side_effect=AssertionError("network forbidden"))
            guard.start()
            self.addCleanup(guard.stop)


class NextRequestParserTests(Offline):
    def parse(self, raw=SINGLE, url=URL):
        return pages.PROFILES[PROFILE].parse(raw, url)

    def reject(self, raw, reason, url=URL):
        with self.assertRaises(PortalError) as caught:
            self.parse(raw, url)
        self.assertEqual(str(caught.exception), reason)
        self.assertNotIn("fixture-token", repr(caught.exception))

    def test_single_page_metadata_and_query_removal(self):
        parsed = self.parse()
        self.assertEqual(parsed.expected_total, 1)
        self.assertIsNone(parsed.next_cursor)
        member, = parsed.members
        self.assertEqual((member.id, member.kind, member.filename, member.released_date),
                         ("D1", "document", "Synthetic & sample.pdf", "2026-01-02"))
        self.assertEqual(member.url, BASE + "/documents/D1/download")
        self.assertNotIn("fixture-token", repr(parsed))

    def test_multi_page_cursor(self):
        first = self.parse(FIRST)
        self.assertEqual(first.next_cursor, URL + "?page=2")
        last = self.parse(LAST, first.next_cursor)
        self.assertIsNone(last.next_cursor)
        self.assertEqual({m.id for m in first.members + last.members}, {"D1", "D2", "D3"})

    def test_request_page_and_missing_date(self):
        member, = self.parse(page(1, row("D1", released=None)), BASE + "/requests/26-001").members
        self.assertIsNone(member.released_date)

    def test_login_bot_check_expired_error_never_return_members(self):
        for raw, reason in ((LOGIN, "authorization_required"), (EXPIRED, "expired_link"),
                            (page(1, row("D1"), extra='<h1>Sign in</h1>'), "authorization_required"),
                            (page(1, row("D1"), extra='<div class="g-recaptcha"></div>'), "authorization_required"),
                            (page(1, row("D1"), extra='<h1>Verify you are human</h1>'), "authorization_required"),
                            (page(1, row("D1"), extra='<h1>404 Not found</h1>'), "inaccessible")):
            with self.subTest(reason=reason, raw=raw[:30]):
                self.reject(raw, reason)

    def test_empty_requires_explicit_evidence(self):
        self.assertEqual(self.parse(EMPTY), pages.ParsedPage((), None, 0))
        self.reject(page(0), "collection_denominator_unknown")
        self.reject(b"<html><body>Loading...</body></html>", "unsupported_navigation")

    def test_unknown_total_truncation_duplicate_attributes_and_bad_dates(self):
        self.reject(SINGLE.replace(b'data-total="1"', b'data-total="2"'), "collection_denominator_unknown")
        self.reject(SINGLE.split(b"</section>")[0], "unsupported_navigation")
        self.reject(SINGLE.replace(b'id="documents"', b'id="documents" id="documents"'), "unsupported_navigation")
        self.reject(page(1, row("D1", released="2026-02-30")), "collection_member_invalid")

    def test_allowlist_and_other_providers(self):
        for host in ("nextrequest.com", "evilnextrequest.com", "fixture.nextrequest.com.attacker.invalid",
                     "fixture.mycusthelp.com", "fixture.civicplus.com"):
            with self.subTest(host=host):
                self.reject(SINGLE, "unsupported_provider", "https://" + host + "/requests/26-001")
        self.reject(SINGLE.replace(b'/documents/D1/download?token=fixture-token',
                                  b'https://other.nextrequest.com/documents/D1'), "unapproved_origin")

    def test_pagination_cannot_change_request_origin_or_carry_tokens(self):
        for cursor, reason in (("/requests/OTHER/documents?page=2", "collection_identity_mismatch"),
                               (URL + "?page=2&token=fixture-token", "collection_identity_mismatch"),
                               (URL + "?page=2&page=3", "collection_identity_mismatch"),
                               (URL + "?page=1", "pagination_loop_or_bound"),
                               (URL + "?page=3", "pagination_loop_or_bound"),
                               ("https://other.nextrequest.com/requests/26-001/documents?page=2",
                                "collection_identity_mismatch")):
            with self.subTest(cursor=cursor):
                self.reject(page(2, row("D1"), cursor), reason)

    def test_inert_content_is_not_membership(self):
        raw = page(1, row("D1"), extra='<template><section id="documents">'
                   '<a href="/documents/FAKE">Fake</a></section></template>')
        self.assertEqual([m.id for m in self.parse(raw).members], ["D1"])

    def test_size_and_encoding_bounds(self):
        self.reject(b"", "collection_page_size_bound")
        self.reject(b"x" * (pages.MAX_PAGE_BYTES + 1), "collection_page_size_bound")
        self.reject(b"\xff", "inaccessible")

    def test_binding_covers_sibling_parser_bytes(self):
        binding = pages._binding(NextRequestProfile())
        expected = hashlib.sha256(Path(pages.__file__).read_bytes() + b"\x00nextrequest.py\x00"
                                  + Path(pages.__file__).with_name("nextrequest.py").read_bytes()).hexdigest()
        self.assertEqual(binding.parser_sha256, expected)

    def test_distinct_ids_keep_same_filename_and_conflicting_identity_fails(self):
        parsed = self.parse(page(2, row("D1", "same.pdf") + row("D2", "same.pdf")))
        self.assertEqual([m.id for m in parsed.members], ["D1", "D2"])
        self.assertEqual([m.filename for m in parsed.members], ["same.pdf", "same.pdf"])
        self.reject(page(1, row("D1") + row("D1", "different.pdf")), "collection_member_conflict")


class NextRequestInventoryTests(Offline):
    def setUp(self):
        super().setUp()
        existing.CollectionPageTests.setUp(self)
        self.inventory = CollectionInventory(self.queue, Scope(frozenset({HOST})))
        self.approval = Approval(self.approval.expires, frozenset({HOST}), frozenset(), {HOST: ("/",)})

    def enroll(self, url=URL):
        path, _ = bridge.legacy_export(self.export, body=url)
        with root_lock(self.root.path):
            return enroll_export(path, export_root=self.export, backend=self.backend, inventory=self.inventory)

    def run_pages(self, value, transport, **kwargs):
        with root_lock(self.root.path):
            return pages.run_collection_pages(self.inventory, value.message_id, profile_id=PROFILE,
                apply=True, approval_loader=lambda: self.approval, egress=lambda url: True,
                transport=transport, **kwargs)

    def count(self, table):
        return self.queue.db.execute("SELECT count(*) FROM " + table).fetchone()[0]

    def node(self):
        return self.queue.db.execute("SELECT * FROM export_collections").fetchone()

    def test_two_pages_reconcile_deduplicate_persist_metadata_and_resume(self):
        value = self.enroll()
        transport = existing.Transport({URL: FIRST, URL + "?page=2": LAST})
        result = self.run_pages(value, transport)
        self.assertEqual((result["indexed"], result["blocked"]), (2, 0))
        self.assertEqual(transport.calls, [URL, URL + "?page=2"])
        self.assertEqual((self.node()["state"], self.node()["expected_total"]), ("enumerated", 3))
        self.assertEqual(self.count("export_collection_members"), 3)
        manifests = " ".join(r[0] for r in self.queue.db.execute("SELECT manifest_json FROM export_collection_pages"))
        self.assertNotIn("fixture-token", manifests)
        self.assertEqual(json.loads(self.queue.db.execute("SELECT manifest_json FROM export_collection_pages LIMIT 1").fetchone()[0])
                         ["members"][0]["released_date"], "2026-01-02")
        for row_ in self.queue.db.execute("SELECT url_private FROM export_collection_members"):
            self.assertNotIn("?", row_[0])
        for row_ in self.queue.db.execute("SELECT last_url_private FROM portal_items WHERE request_id!=?", (COLLECTION_REQUEST,)):
            self.assertNotIn("?", row_[0])
        self.assertFalse(result["retrieval_complete"])  # enumeration is not document download
        self.assertEqual(result["document_downloads"], 0)
        self.assertFalse(result["publication_authorized"])
        again = self.run_pages(value, existing.Transport({}))
        self.assertEqual(again["attempted"], 0)

    def test_count_mismatch_blocks_instead_of_claiming_complete(self):
        value = self.enroll()
        result = self.run_pages(value, existing.Transport({URL: page(2, row("D1"))}))
        self.assertEqual(result["blocked"], 1)
        self.assertFalse(result["retrieval_complete"])
        self.assertEqual(self.count("export_collection_members"), 0)
        self.assertEqual(self.node()["state"], "blocked")
        self.assertEqual(self.node()["reason"], "missing_or_conflicting_members")

    def test_auth_expired_error_and_http_dispositions(self):
        cases = ((LOGIN, "authorization_required"), (EXPIRED, "expired_link"),
                 (page(1, row("D1"), extra='<h1>Server error</h1>'), "inaccessible"))
        # One sidecar per case; no automatic retry/replacement of blocked work.
        for index, (raw, reason) in enumerate(cases):
            address = BASE + "/requests/26-00" + str(index + 2)
            path, _ = bridge.legacy_export(self.export, body=address, uid=50 + index)
            with root_lock(self.root.path):
                value = enroll_export(path, export_root=self.export, backend=self.backend, inventory=self.inventory)
            result = self.run_pages(value, existing.Transport({address: raw}))
            self.assertEqual(result["blocked"], 1)
            node = self.queue.db.execute("SELECT * FROM export_collections WHERE url_private=?", (address,)).fetchone()
            self.assertEqual(node["reason"], reason)
            self.assertFalse(result["retrieval_complete"])
        self.assertEqual(self.count("export_collection_members"), 0)

    def test_http_410_maps_to_expired(self):
        value = self.enroll()
        result = self.run_pages(value, existing.Transport({URL: lambda: Response(410, {})}))
        self.assertEqual(result["blocked"], 1)
        self.assertEqual(self.node()["reason"], "expired_link")

    def test_empty_release_is_enumerated(self):
        value = self.enroll()
        result = self.run_pages(value, existing.Transport({URL: EMPTY}))
        self.assertEqual((result["indexed"], self.node()["expected_total"]), (1, 0))
        self.assertTrue(result["retrieval_complete"])
        self.assertEqual(self.count("export_collection_members"), 0)

    def test_page_bound_preserved_and_resume(self):
        self.inventory = CollectionInventory(self.queue, Scope(frozenset({HOST}), max_pages=1))
        value = self.enroll()
        transport = existing.Transport({URL: FIRST, URL + "?page=2": LAST})
        self.assertEqual(self.run_pages(value, transport, limits=Limits(max_items=1, max_bytes=pages.MAX_PAGE_BYTES))["indexed"], 1)
        result = self.run_pages(value, transport)
        self.assertEqual(result["blocked"], 1)
        self.assertEqual(self.node()["reason"], "pagination_loop_or_bound")
        self.assertFalse(result["retrieval_complete"])

    def test_govqa_civicplus_are_explicitly_unsupported_and_incomplete(self):
        hosts = {"fixture.mycusthelp.com", "fixture.civicplus.com"}
        self.inventory = CollectionInventory(self.queue, Scope(frozenset(hosts)))
        path, _ = bridge.legacy_export(self.export, body="\n".join("https://" + h + "/browse" for h in sorted(hosts)))
        with root_lock(self.root.path):
            value = enroll_export(path, export_root=self.export, backend=self.backend, inventory=self.inventory)
        self.assertEqual([tuple(r) for r in self.queue.db.execute("SELECT kind,reason FROM export_body_links")],
                         [("blocked", "unsupported_provider")] * 2)
        self.assertFalse(self.inventory.readiness(value.message_id)["retrieval_complete"])
        self.assertEqual(self.count("export_collections"), 0)


if __name__ == "__main__":
    unittest.main()
