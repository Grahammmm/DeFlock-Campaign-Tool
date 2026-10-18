"""Synthetic page transport/parser composition; no live source or socket."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from campaign_tool.records.export_bridge import enroll_export
from campaign_tool.records.portal import collection_pages as pages
from campaign_tool.records.portal.collections import CollectionInventory, Scope
from campaign_tool.records.portal.engine import (COLLECTION_REQUEST, Limits, PageBinding,
    Response, fetch_collection_page, fetch_queue)
from campaign_tool.records.portal.ledger_adapter import WP1LedgerAdapter
from campaign_tool.records.portal.policy import Approval, PortalError
from campaign_tool.records.portal.store import Queue
from campaign_tool.records.run import Pipeline, RunError, root_lock
from campaign_tool.records.runner.canonical_mail import CanonicalMailBackend
from tests.records import test_export_bridge as bridge_fixture

HOST = "archive.example.invalid"
BASE = "https://" + HOST
PROFILE = "synthetic-archive-v1"
FIXTURES = {'denominator-mismatch.html': b'<!doctype html><html><body><main data-fixture-provider="syntheti'
                              b'c-archive-v1" data-collection="mismatch" data-total="2" data-ter'
                              b'minal="true"><a data-record-id="B" href="/documents/B/download">'
                              b'Only one listed</a></main></body></html>\n',
 'explicit-empty.html': b'<!doctype html><html><body><main data-fixture-provider="synthetic-archiv'
                        b'e-v1" data-collection="empty" data-total="0" data-terminal="true"><p dat'
                        b'a-empty="true">No records in synthetic fixture.</p></main></body></html>'
                        b'\n',
 'login.html': b'<!doctype html><html><body><main data-fixture-provider="synthetic-archive-v1" da'
               b'ta-auth-required="true"><form action="/login"><input type="password" name="synth'
               b'etic-password"><button>Sign in</button></form></main></body></html>\n',
 'page-1.html': b'<!doctype html><html><body><main data-fixture-provider="synthetic-archive-v1" da'
                b'ta-collection="records" data-total="3" data-page="1"><a data-record-id="B" href='
                b'"/documents/B/download">Synthetic B</a><a data-record-id="C" href="/documents/C/'
                b'download">Synthetic C</a><a rel="next" href="/browse?id=records&amp;page=2">Next'
                b'</a></main></body></html>\n',
 'page-2.html': b'<!doctype html><html><body><main data-fixture-provider="synthetic-archive-v1" da'
                b'ta-collection="records" data-total="3" data-page="2" data-terminal="true"><a dat'
                b'a-record-id="C" href="/documents/C/download">Synthetic C repeated</a><a data-rec'
                b'ord-id="D" href="/documents/D/download">Synthetic D</a></main></body></html>\n',
 'pagination-loop.html': b'<!doctype html><html><body><main data-fixture-provider="synthetic-archiv'
                         b'e-v1" data-collection="loop" data-total="2"><a data-record-id="B" href="'
                         b'/documents/B/download">Synthetic B</a><a rel="next" href="/browse?id=loo'
                         b'p">Same page</a></main></body></html>\n',
 'unapproved-navigation.html': b'<!doctype html><html><body><main data-fixture-provider="syntheti'
                               b'c-archive-v1" data-collection="denied" data-total="1" data-termi'
                               b'nal="true"><a data-record-id="X" href="https://unapproved.exampl'
                               b'e.invalid/documents/X">Outside approved fixture origin</a></main'
                               b'></body></html>\n',
 'unknown-layout.html': b'<!doctype html><html><body><p>No recognized inventory markers. This must'
                        b' remain unsupported/unknown, never empty.</p></body></html>\n'}


class Transport:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []
        self.closed = 0

    def open(self, url, *, timeout):
        self.calls.append(url)
        value = self.responses[url]
        if callable(value):
            return value()
        raw = value if isinstance(value, bytes) else FIXTURES[value]
        def close():
            self.closed += 1
        return Response(200, {"content-type": "text/html", "content-length": str(len(raw))}, (raw,), close)


class CollectionPageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.pipeline = Pipeline(self.base / "canonical")
        self.root = self.pipeline.root
        self.queue = Queue(self.root.path / "portal")
        self.addCleanup(self.queue.close)
        self.inventory = CollectionInventory(self.queue, Scope(frozenset({HOST})))
        self.export = self.base / "export"
        self.export.mkdir(mode=0o700)
        self.backend = CanonicalMailBackend(self.export, self.root.intake, self.root.ledger)
        self.backend.start_run(self.pipeline._identity())
        self.approval = Approval(datetime.now(timezone.utc) + timedelta(hours=1),
                                 frozenset({HOST}), frozenset(), {HOST: ("/",)})

    def enroll(self, *names, host=HOST):
        urls = ["https://" + host + "/browse?id=" + name for name in names]
        path, _ = bridge_fixture.legacy_export(self.export, body="\n".join(urls))
        with root_lock(self.root.path):
            return enroll_export(path, export_root=self.export, backend=self.backend, inventory=self.inventory)

    def run_pages(self, value, transport, **kwargs):
        with root_lock(self.root.path):
            return pages.run_collection_pages(self.inventory, value.message_id, profile_id=PROFILE,
                apply=True, approval_loader=lambda: self.approval, egress=lambda url: True,
                transport=transport, **kwargs)

    def count(self, table):
        return self.queue.db.execute("SELECT count(*) FROM " + table).fetchone()[0]

    def test_two_pages_originals_denominator_dedup_and_canonical_provenance(self):
        value = self.enroll("records")
        first, second = BASE + "/browse?id=records", BASE + "/browse?id=records&page=2"
        transport = Transport({first: "page-1.html", second: "page-2.html"})
        result = self.run_pages(value, transport)
        self.assertEqual((result["attempted"], result["indexed"], result["blocked"]), (2, 2, 0))
        self.assertEqual(transport.calls, [first, second])
        self.assertEqual(transport.closed, 2)
        self.assertFalse(result["retrieval_complete"])
        self.assertEqual((result["document_downloads"], result["stage_promotions"]), (0, 0))
        self.assertEqual(self.count("export_collection_pages"), 2)
        self.assertEqual(self.count("export_collection_members"), 3)
        self.assertEqual(self.queue.db.execute("SELECT expected_total,state FROM export_collections").fetchone()[:], (3, "enumerated"))
        hashes = {hashlib.sha256(FIXTURES[name]).hexdigest() for name in ("page-1.html", "page-2.html")}
        for digest in hashes:
            self.queue.verify_object(digest, (self.queue.objects / digest).stat().st_size)
        raw_versions = self.queue.db.execute("SELECT ledger_state,provenance_json FROM portal_versions").fetchall()
        self.assertEqual({r["ledger_state"] for r in raw_versions}, {"collection_page"})
        self.assertTrue(all(json.loads(r["provenance_json"])["profile_id"] == PROFILE for r in raw_versions))

        # The existing document path must skip page items/outbox entries and
        # preserve B/C/D normally. The page HTML is not a canonical document.
        documents = Transport({BASE + "/documents/" + letter + "/download":
                               lambda letter=letter: Response(200, {"content-type": "application/pdf"},
                                                              (bridge_fixture.PDFS[letter],)) for letter in "BCD"})
        ledger = WP1LedgerAdapter(queue=self.queue, database=self.root.ledger, cas_root=self.root.intake / "blobs")
        fetched = fetch_queue(self.queue, apply=True, approval_loader=lambda: self.approval,
                               egress=lambda url: True, transport=documents, ledger=ledger)
        self.assertEqual((fetched["received"], fetched["ledger_delivered"]), (3, 3))
        self.assertEqual(len(documents.calls), 3)
        ready = self.inventory.readiness(value.message_id)
        self.assertTrue(ready["retrieval_complete"])
        self.assertEqual(ready["unique_preserved_documents"], 4)  # Attachment A and B/C/D.
        self.assertEqual(self.pipeline._query("SELECT count(*) AS n FROM originals")[0]["n"], 5)  # Plus EML, no HTML.
        c_item = self.queue.db.execute("SELECT target FROM export_collection_members WHERE member='C'").fetchone()[0]
        self.assertEqual({r[0] for r in self.queue.db.execute("SELECT source_sha256 FROM portal_notices WHERE item=?", (c_item,))}, hashes)
        version = self.queue.db.execute("SELECT id FROM portal_versions WHERE item=?", (c_item,)).fetchone()[0]
        evidence = self.pipeline._query("SELECT evidence FROM occurrences WHERE id=?", ("portal:" + version,))[0]["evidence"]
        self.assertEqual(set(json.loads(evidence)["receipt"]["provenance"]["source_sha256s"]), hashes)

    def test_explicit_empty_is_complete_but_unknown_layout_is_not_empty(self):
        value = self.enroll("empty", "unknown")
        transport = Transport({BASE + "/browse?id=empty": "explicit-empty.html",
                               BASE + "/browse?id=unknown": "unknown-layout.html"})
        result = self.run_pages(value, transport)
        self.assertEqual((result["indexed"], result["blocked"]), (1, 1))
        self.assertFalse(result["retrieval_complete"])
        nodes = {r["url_private"]: dict(r) for r in self.queue.db.execute("SELECT * FROM export_collections")}
        self.assertEqual((nodes[BASE + "/browse?id=empty"]["state"], nodes[BASE + "/browse?id=empty"]["expected_total"]), ("enumerated", 0))
        self.assertIsNone(nodes[BASE + "/browse?id=unknown"]["expected_total"])

    def test_login_html_stops_without_following_or_enqueuing_members(self):
        value = self.enroll("login")
        transport = Transport({BASE + "/browse?id=login": "login.html"})
        result = self.run_pages(value, transport)
        self.assertEqual(result["blocked"], 1)
        self.assertEqual(self.count("export_collection_members"), 0)
        self.assertEqual(self.queue.db.execute("SELECT reason FROM export_collections").fetchone()[0], "authorization_required")
        self.assertEqual(self.queue.db.execute("SELECT ledger_state FROM portal_versions").fetchone()[0], "collection_blocked")
        self.assertEqual(len(transport.calls), 1)

    def test_cookie_check_redirect_is_not_followed(self):
        value = self.enroll("records")
        transport = Transport({BASE + "/browse?id=records": lambda: Response(302,
            {"location": "/WebLink/CookieCheck.aspx?redirect=synthetic", "set-cookie": "synthetic-session=unused"})})
        result = self.run_pages(value, transport)
        self.assertEqual((result["attempted"], result["blocked"]), (1, 1))
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(self.queue.db.execute("SELECT reason FROM portal_items").fetchone()[0], "authorization_required")
        self.assertEqual(self.count("portal_versions"), 0)

    def test_http_authorization_and_expired_dispositions_are_terminal(self):
        value = self.enroll("denied401", "denied403", "denied410")
        transport = Transport({BASE + "/browse?id=denied" + str(status):
                               lambda status=status: Response(status, {}) for status in (401, 403, 410)})
        result = self.run_pages(value, transport)
        self.assertEqual(result["blocked"], 3)
        self.assertEqual(self.count("portal_versions"), 0)
        states = {r["url_private"]: r["reason"] for r in self.queue.db.execute("SELECT * FROM export_collections")}
        self.assertEqual(states[BASE + "/browse?id=denied401"], "authorization_required")
        self.assertEqual(states[BASE + "/browse?id=denied403"], "authorization_required")
        self.assertEqual(states[BASE + "/browse?id=denied410"], "expired_link")

    def test_unapproved_redirect_stops_before_target_transport(self):
        value = self.enroll("records")
        transport = Transport({BASE + "/browse?id=records": lambda: Response(302,
            {"location": "https://unapproved.example.invalid/browse"})})
        result = self.run_pages(value, transport)
        self.assertEqual(result["blocked"], 1)
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(self.queue.db.execute("SELECT reason FROM portal_items").fetchone()[0], "redirect_not_approved")

    def test_egress_denial_opens_no_transport(self):
        value = self.enroll("records")
        transport = Transport({})
        with root_lock(self.root.path):
            result = pages.run_collection_pages(self.inventory, value.message_id, profile_id=PROFILE, apply=True,
                approval_loader=lambda: self.approval, egress=lambda url: False, transport=transport)
        self.assertEqual(result["blocked"], 1)
        self.assertEqual(transport.calls, [])

    def test_redirect_budget_stops_before_fourth_follow(self):
        value = self.enroll("records")
        urls = [BASE + "/browse?id=records" + ("&page=" + str(i) if i else "") for i in range(5)]
        transport = Transport({url: lambda i=i: Response(302, {"location": urls[i + 1]})
                               for i, url in enumerate(urls[:-1])})
        result = self.run_pages(value, transport)
        self.assertEqual(result["blocked"], 1)
        self.assertEqual(transport.calls, urls[:4])
        self.assertEqual(self.queue.db.execute("SELECT reason FROM portal_items").fetchone()[0], "redirect_limit")
        self.assertEqual(self.count("portal_versions"), 0)

    def test_malformed_and_truncated_markup_cannot_claim_empty(self):
        value = self.enroll("empty", "duplicate")
        truncated = FIXTURES["explicit-empty.html"].split(b"</main>")[0]
        duplicate = FIXTURES["explicit-empty.html"].replace(b'data-collection="empty"',
            b'data-collection="duplicate" data-total="0"')
        result = self.run_pages(value, Transport({BASE + "/browse?id=empty": truncated,
                                                 BASE + "/browse?id=duplicate": duplicate}))
        self.assertEqual(result["blocked"], 2)
        self.assertFalse(result["retrieval_complete"])
        self.assertEqual(self.count("export_collection_pages"), 0)

    def test_reopened_broader_scope_stops_before_page_enrollment(self):
        value = self.enroll("empty")
        self.inventory = CollectionInventory(self.queue, Scope(frozenset({HOST, "other.example.invalid"})))
        transport = Transport({})
        with self.assertRaisesRegex(PortalError, "export_event_scope_mismatch"):
            self.run_pages(value, transport)
        self.assertEqual(transport.calls, [])
        self.assertEqual(self.count("portal_items"), 0)

    def test_partial_stream_never_promotes_page_or_members(self):
        value = self.enroll("records")
        transport = Transport({BASE + "/browse?id=records": lambda: Response(200,
            {"content-type": "text/html", "content-length": "10000"}, (b"<html>partial",))})
        result = self.run_pages(value, transport)
        self.assertEqual(result["blocked"], 1)
        self.assertEqual(self.count("portal_versions"), 0)
        self.assertEqual(self.count("export_collection_pages"), 0)
        self.assertEqual(self.count("export_collection_members"), 0)

    def test_encoded_collection_body_is_not_decoded(self):
        value = self.enroll("records")
        transport = Transport({BASE + "/browse?id=records": lambda: Response(200,
            {"content-type": "text/html", "content-encoding": "gzip"}, (b"not decoded",))})
        result = self.run_pages(value, transport)
        self.assertEqual(result["blocked"], 1)
        self.assertEqual(self.queue.db.execute("SELECT reason FROM portal_items").fetchone()[0], "encoded_body_unsupported")

    def test_page_byte_limit_is_enforced_before_inventory(self):
        value = self.enroll("records")
        result = self.run_pages(value, Transport({BASE + "/browse?id=records": "page-1.html"}), limits=Limits(max_bytes=8))
        self.assertEqual(result["blocked"], 1)
        self.assertEqual(self.count("export_collection_pages"), 0)
        self.assertEqual(self.queue.db.execute("SELECT reason FROM portal_items").fetchone()[0], "size_limit")

    def test_common_deadline_stops_body_before_promotion(self):
        value = self.enroll("records")
        ticks = iter((0, 0, 26, 26, 26))
        result = self.run_pages(value, Transport({BASE + "/browse?id=records": "page-1.html"}),
                                limits=Limits(backoff=0, max_bytes=pages.MAX_PAGE_BYTES), mono=lambda: next(ticks))
        self.assertEqual(result["pending"], 1)
        self.assertEqual(self.count("portal_versions"), 0)
        self.assertEqual(self.queue.db.execute("SELECT reason FROM portal_items").fetchone()[0], "timeout")

    def test_three_attempt_history_is_not_reset_by_reentry(self):
        value = self.enroll("records")
        transport = Transport({BASE + "/browse?id=records": lambda: Response(503, {})})
        for _ in range(3):
            self.run_pages(value, transport, limits=Limits(backoff=0, max_bytes=pages.MAX_PAGE_BYTES))
        before = self.queue.db.execute("SELECT state,tries FROM portal_items").fetchone()[:]
        self.assertEqual(before, ("blocked", 3))
        self.run_pages(value, transport, limits=Limits(backoff=0, max_bytes=pages.MAX_PAGE_BYTES))
        self.assertEqual(len(transport.calls), 3)
        self.assertEqual(self.queue.db.execute("SELECT state,tries FROM portal_items").fetchone()[:], before)

    def test_pagination_loop_and_terminal_mismatch_remain_exceptions(self):
        value = self.enroll("loop", "mismatch")
        transport = Transport({BASE + "/browse?id=loop": "pagination-loop.html",
                               BASE + "/browse?id=mismatch": "denominator-mismatch.html"})
        result = self.run_pages(value, transport)
        self.assertEqual(result["blocked"], 2)
        self.assertFalse(result["retrieval_complete"])
        self.assertEqual(self.count("export_collection_pages"), 0)

    def test_unapproved_member_is_not_enqueued(self):
        value = self.enroll("denied")
        result = self.run_pages(value, Transport({BASE + "/browse?id=denied": "unapproved-navigation.html"}))
        self.assertEqual(result["blocked"], 1)
        self.assertEqual(self.count("export_collection_members"), 0)
        self.assertEqual(self.queue.db.execute("SELECT count(*) FROM portal_items WHERE request_id!=?", (COLLECTION_REQUEST,)).fetchone()[0], 0)

    def test_bounded_worklist_resumes_next_page_without_refetching_first(self):
        value = self.enroll("records")
        first, second = BASE + "/browse?id=records", BASE + "/browse?id=records&page=2"
        transport = Transport({first: "page-1.html", second: "page-2.html"})
        self.assertEqual(self.run_pages(value, transport, limits=Limits(max_items=1, max_bytes=pages.MAX_PAGE_BYTES))["indexed"], 1)
        self.assertEqual(self.run_pages(value, transport, limits=Limits(max_items=1, max_bytes=pages.MAX_PAGE_BYTES))["indexed"], 1)
        self.assertEqual(transport.calls, [first, second])
        self.assertEqual(self.count("export_collection_pages"), 2)

    def test_preserved_page_resumes_after_parser_interruption_without_network(self):
        value = self.enroll("empty")
        transport = Transport({BASE + "/browse?id=empty": "explicit-empty.html"})
        with patch.object(pages.SyntheticProfile, "parse", side_effect=OSError("synthetic interruption")):
            with self.assertRaises(OSError):
                self.run_pages(value, transport)
        self.assertEqual(self.count("portal_versions"), 1)
        self.assertEqual(self.count("export_collection_pages"), 0)
        result = self.run_pages(value, Transport({}))
        self.assertEqual((result["attempted"], result["indexed"]), (0, 1))
        self.assertEqual(self.count("portal_attempts"), 1)
        self.assertEqual(self.count("export_collection_pages"), 1)

    def test_cached_page_parser_binding_cannot_silently_change(self):
        value = self.enroll("empty")
        transport = Transport({BASE + "/browse?id=empty": "explicit-empty.html"})
        self.run_pages(value, transport)
        item = self.queue.db.execute("SELECT id FROM portal_items WHERE request_id=?", (COLLECTION_REQUEST,)).fetchone()[0]
        with self.assertRaisesRegex(PortalError, "collection_parser_binding_conflict"):
            fetch_collection_page(self.queue, item, binding=PageBinding(PROFILE, "f" * 64), apply=True,
                approval_loader=lambda: self.approval, egress=lambda url: True, transport=Transport({}))
        self.assertEqual(self.count("portal_attempts"), 1)

    def test_plan_only_and_root_lock_do_not_grant_network(self):
        value = self.enroll("empty")
        result = pages.run_collection_pages(self.inventory, value.message_id, profile_id=PROFILE)
        self.assertEqual(result["network_calls"], 0)
        self.assertEqual(self.count("portal_items"), 0)
        with self.assertRaises(RunError):
            pages.run_collection_pages(self.inventory, value.message_id, profile_id=PROFILE, apply=True)
        self.assertEqual(self.count("portal_items"), 0)

    def test_real_host_and_unknown_profile_are_unsupported_before_transport(self):
        self.inventory = CollectionInventory(self.queue, Scope(frozenset({"public.example.org"})))
        value = self.enroll("records", host="public.example.org")
        with self.assertRaisesRegex(PortalError, "unsupported_provider"):
            self.run_pages(value, Transport({}))
        with self.assertRaisesRegex(PortalError, "unsupported_provider"):
            pages.run_collection_pages(self.inventory, value.message_id, profile_id="weblink", apply=True)
        self.assertEqual(self.count("portal_items"), 0)

    def test_document_html_and_sniff_guards_are_unchanged(self):
        source = hashlib.sha256(b"synthetic source").hexdigest()
        for key, mime in (("html", "text/html"), ("sniff", "text/plain")):
            url = BASE + "/documents/" + key
            self.queue.inventory(HOST, "synthetic-request", key, url, source)
            transport = Transport({url: lambda mime=mime: Response(200, {"content-type": mime}, (b"<html>not a document",))})
            result = fetch_queue(self.queue, apply=True, approval_loader=lambda: self.approval,
                                 egress=lambda url: True, transport=transport)
            self.assertEqual(result["blocked"], 1)
        self.assertEqual(self.count("portal_versions"), 0)

    def test_document_pdf_markup_still_preserves_as_before(self):
        source = hashlib.sha256(b"synthetic source").hexdigest()
        url = BASE + "/documents/pdf"
        self.queue.inventory(HOST, "synthetic-request", "pdf", url, source)
        raw = b"%PDF-1.4\n<script>synthetic inert PDF bytes</script>\n%%EOF"
        transport = Transport({url: lambda: Response(200, {"content-type": "application/pdf"}, (raw,))})
        result = fetch_queue(self.queue, apply=True, approval_loader=lambda: self.approval,
                             egress=lambda url: True, transport=transport)
        self.assertEqual(result["received"], 1)
        self.assertEqual(self.queue.db.execute("SELECT ledger_state FROM portal_versions").fetchone()[0], "pending")


if __name__ == "__main__":
    unittest.main()
