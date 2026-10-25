"""FA43: malformed collection members fail closed before any inventory mutation.

Synthetic only: a real Queue/CollectionInventory sidecar in a fresh temporary
root, the existing export-bridge collection-page factory and the real root
lock. No live mailbox, portal, credential configuration, model or socket.

Test names carry the acceptance row they cover (a1 positive, a2 negative,
a3 boundary/recovery, a4 deciding control, a5 integration).
"""
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import unittest

from campaign_tool.records.portal import collections as collections_module
from campaign_tool.records.portal.collections import CollectionInventory, Scope
from campaign_tool.records.portal.engine import Response, fetch_queue
from campaign_tool.records.portal.ledger_adapter import WP1LedgerAdapter
from campaign_tool.records.portal.policy import Approval, PortalError
from campaign_tool.records.run import root_lock
from tests.records import test_export_bridge as bridge

HOST, BASE, PARSER = bridge.HOST, bridge.BASE, bridge.PARSER
CODE = "invalid_collection_member"
SENTINEL_ID = "fa43SentinelId"
SENTINEL_URL = BASE + "/documents/fa43SentinelUrl"
REQUIRED = ("id", "kind", "url")
BAD_VALUES = (None, True, False, 0, 7, 1.5, [], ["document"], {}, {"value": "document"})


def collection_url(key):
    return BASE + "/WebLink/Browse.aspx?id=" + key


def document(letter, **extra):
    return {"id": letter, "kind": "document", "url": BASE + "/documents/" + letter, **extra}


def sentinel_member():
    return {"id": SENTINEL_ID, "kind": "document", "url": SENTINEL_URL}


class CollectionMemberShapeTests(unittest.TestCase):
    # Reuse the existing synthetic factory/fixture verbatim (not subclassed, so
    # the bridge suite is not collected twice under ordinary discovery).
    setUp = bridge.ExportBridgeTests.setUp
    enroll = bridge.ExportBridgeTests.enroll
    collection = bridge.ExportBridgeTests.collection
    page = bridge.ExportBridgeTests.page

    # -- helpers ---------------------------------------------------------------
    def event(self, *keys):
        path, _ = bridge.legacy_export(self.export, body="\n".join(collection_url(k) for k in keys) + "\n")
        return self.enroll(path)

    def staged(self, value, key, members, total, *, next_cursor=None, cursor=None, marker="page"):
        """Same page bytes/arguments as the factory, but CAS-staged before accept."""
        cursor = cursor or collection_url(key)
        raw = ("<html>SYNTHETIC " + marker + " " + cursor + "</html>").encode()
        digest = hashlib.sha256(raw).hexdigest()
        if not (self.queue.objects / digest).exists():
            temp = self.queue.objects / (".fixture-" + digest)
            temp.write_bytes(raw)
            os.chmod(temp, 0o600)
            self.queue.promote(temp, digest, len(raw))
            temp.unlink()
        return {"cursor": cursor, "source_sha256": digest, "source_bytes": len(raw), "parser_sha256": PARSER,
                "members": members, "expected_total": total, "next_cursor": next_cursor, "event": value.message_id}

    def accept(self, key, data):
        with root_lock(self.root.path):
            self.inventory.accept_page(self.collection(key), **data)

    def snapshot(self):
        db = self.queue.db
        tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        rows = {t: sorted(repr(tuple(r)) for r in db.execute('SELECT * FROM "' + t + '"')) for t in tables}
        files = {}
        for directory, _, names in os.walk(self.queue.root):
            for name in names:
                path = os.path.join(directory, name)
                if name.startswith("portal.sqlite"):
                    continue  # database content is compared row by row above
                with open(path, "rb") as handle:
                    files[os.path.relpath(path, self.queue.root)] = hashlib.sha256(handle.read()).hexdigest()
        return {"rows": rows, "files": files}

    def refused(self, key, data, code=CODE):
        before = self.snapshot()
        with root_lock(self.root.path), self.assertRaises(PortalError) as caught:
            self.inventory.accept_page(self.collection(key), **data)
        error = caught.exception
        self.assertEqual(error.args, (code,))
        self.assertEqual(str(error), code)
        for secret in (SENTINEL_ID, SENTINEL_URL, "fa43Sentinel"):
            self.assertNotIn(secret, repr(error))
        if code == CODE:
            self.assertIsNone(error.__cause__)
            self.assertIsNone(error.__context__)
        self.assertFalse(self.queue.db.in_transaction)
        self.assertEqual(self.snapshot(), before)
        return error

    def node(self, key):
        return dict(self.queue.db.execute("SELECT * FROM export_collections WHERE id=?", (self.collection(key),)).fetchone())

    def member_rows(self, key):
        return [tuple(r) for r in self.queue.db.execute(
            "SELECT member,kind,url_private FROM export_collection_members WHERE collection=? ORDER BY member",
            (self.collection(key),))]

    # -- A1 positive ----------------------------------------------------------
    def test_a1_valid_document_nested_collection_and_ignored_extras(self):
        value = self.event("F1")
        members = [document("B", title="Synthetic B", rank=3, meta={"nested": [1, None]}),
                   OrderedDict([("url", collection_url("F2")), ("kind", "collection"), ("id", "F2"), ("note", None)])]
        data = self.page(value, "F1", members, 2)
        self.assertEqual(self.member_rows("F1"), [("B", "document", BASE + "/documents/B"),
                                                  ("F2", "collection", collection_url("F2"))])
        child = self.queue.db.execute("SELECT target FROM export_collection_members WHERE member='F2'").fetchone()[0]
        self.assertEqual(child, self.collection("F2"))
        self.assertEqual(self.node("F1")["state"], "enumerated")
        self.assertEqual(self.node("F2")["state"], "pending")
        page = self.queue.db.execute("SELECT * FROM export_collection_pages").fetchone()
        self.assertEqual((page["source_sha256"], page["parser_sha256"]), (data["source_sha256"], PARSER))
        manifest = json.loads(page["manifest_json"])
        self.assertEqual(manifest["members"], [{"id": "B", "kind": "document", "url": BASE + "/documents/B"},
                                               {"id": "F2", "kind": "collection", "url": collection_url("F2")}])
        raw = ("<html>SYNTHETIC page " + collection_url("F1") + "</html>").encode()
        self.assertEqual((self.queue.objects / data["source_sha256"]).read_bytes(), raw)
        self.queue.verify_object(data["source_sha256"], data["source_bytes"])
        notices = {r[0] for r in self.queue.db.execute("SELECT source_sha256 FROM portal_notices")}
        self.assertEqual(notices, {data["source_sha256"]})
        self.assertEqual([p["collection"] for p in self.inventory.pending_pages(value.message_id)],
                         [self.collection("F2")])

    def test_a1_empty_explicitly_enumerated_collection(self):
        value = self.event("F1")
        self.page(value, "F1", [], 0)
        node = self.node("F1")
        self.assertEqual((node["state"], node["expected_total"]), ("enumerated", 0))
        self.assertEqual(self.member_rows("F1"), [])
        self.assertTrue(self.inventory.readiness(value.message_id)["retrieval_complete"])

    def test_a1_multi_page_continuation_and_exact_immutable_replay(self):
        value = self.event("F1")
        second = collection_url("F1") + "&page=2"
        first_page = self.page(value, "F1", [document("B")], 2, next_cursor=second)
        self.assertEqual((self.node("F1")["state"], self.node("F1")["cursor_private"]), ("partial", second))
        second_page = self.page(value, "F1", [document("C")], 2, cursor=second)
        self.assertEqual(self.node("F1")["state"], "enumerated")
        before = self.snapshot()
        for data in (first_page, second_page):
            self.accept("F1", data)  # immutable replay is a no-op
        self.assertEqual(self.snapshot(), before)
        bound = {(r["cursor_private"], r["source_sha256"], r["parser_sha256"]) for r in
                 self.queue.db.execute("SELECT * FROM export_collection_pages")}
        self.assertEqual(bound, {(collection_url("F1"), first_page["source_sha256"], PARSER),
                                 (second, second_page["source_sha256"], PARSER)})
        self.refused("F1", dict(first_page, parser_sha256="e" * 64), "collection_page_conflict")

    # -- A2 negative ----------------------------------------------------------
    def test_a2_null_scalar_and_array_members(self):
        value = self.event("F1")
        for member in (None, 0, 7, 1.5, True, "B", b"B", [], ["B", "document", SENTINEL_URL],
                       ("B", "document", SENTINEL_URL), {SENTINEL_ID, "document"}):
            with self.subTest(member=type(member).__name__):
                self.refused("F1", self.staged(value, "F1", [member], 1))
                self.refused("F1", self.staged(value, "F1", [sentinel_member(), member], 2))

    def test_a2_missing_required_fields(self):
        value = self.event("F1")
        for field in REQUIRED:
            member = sentinel_member()
            del member[field]
            with self.subTest(missing=field):
                self.refused("F1", self.staged(value, "F1", [member], 1))
        self.refused("F1", self.staged(value, "F1", [{}], 1))
        self.refused("F1", self.staged(value, "F1", [{"title": SENTINEL_ID}], 1))

    def test_a2_non_string_required_values(self):
        value = self.event("F1")
        for field in REQUIRED:
            for bad in BAD_VALUES:
                member = dict(sentinel_member(), **{field: bad})
                with self.subTest(field=field, value=repr(bad)):
                    self.refused("F1", self.staged(value, "F1", [member], 1))

    def test_a2_valid_member_then_invalid_keeps_existing_rows_and_bytes(self):
        value = self.event("F1", "F2")
        self.page(value, "F1", [document("B")], 1)
        existing = self.member_rows("F1")
        for bad in (None, {"id": "D", "kind": ["document"], "url": BASE + "/documents/D"}, {"id": "D", "url": SENTINEL_URL}):
            with self.subTest(bad=repr(bad)):
                self.refused("F2", self.staged(value, "F2", [document("C"), bad], 2))
        self.assertEqual(self.member_rows("F1"), existing)
        self.assertEqual(self.member_rows("F2"), [])
        node = self.node("F2")
        self.assertEqual((node["state"], node["cursor_private"], node["expected_total"]),
                         ("pending", collection_url("F2"), None))
        self.assertEqual(self.queue.db.execute("SELECT count(*) FROM portal_items WHERE item_id IS NOT NULL").fetchone()[0], 1)

    # -- A3 boundary and recovery --------------------------------------------
    def test_a3_member_limit_and_one_over(self):
        self.inventory = CollectionInventory(self.queue, Scope(frozenset({HOST}), frozenset({"social.example.invalid"}),
                                                               max_members=3))
        value = self.event("F1", "F2")
        self.page(value, "F1", [document(x) for x in "BCD"], 3)
        self.assertEqual(len(self.member_rows("F1")), 3)
        self.refused("F2", self.staged(value, "F2", [document(x) for x in "EFGH"], None), "collection_member_bound")
        # The pre-existing list bound still precedes the per-member shape gate.
        self.refused("F2", self.staged(value, "F2", [None] * 4, None), "collection_member_bound")
        self.refused("F2", self.staged(value, "F2", [document("E"), None], 2))

    def test_a3_existing_reason_codes_for_well_shaped_members(self):
        value = self.event("F1")
        cases = (
            ([document("B"), document("B")], "collection_member_invalid"),
            ([{"id": "B", "kind": "folder", "url": BASE + "/documents/B"}], "collection_member_invalid"),
            ([{"id": "B", "kind": "Document", "url": BASE + "/documents/B"}], "collection_member_invalid"),
            ([{"id": "!bad", "kind": "document", "url": BASE + "/documents/B"}], "invalid_identity"),
            ([{"id": "", "kind": "document", "url": BASE + "/documents/B"}], "invalid_identity"),
            ([{"id": "B", "kind": "document", "url": "http://" + HOST + "/documents/B"}], "invalid_url"),
            ([{"id": "B", "kind": "document", "url": "https://user:pw@" + HOST + "/documents/B"}], "invalid_url"),
            ([{"id": "B", "kind": "document", "url": BASE + "/documents/../B"}], "invalid_url"),
            ([{"id": "B", "kind": "document", "url": ""}], "invalid_url"),
            ([{"id": "B", "kind": "document", "url": "https://other.example.invalid/documents/B"}], "unapproved_origin"),
            ([{"id": "B", "kind": "document", "url": "https://social.example.invalid/documents/B"}], "unapproved_origin"),
        )
        for members, code in cases:
            with self.subTest(code=code, members=repr(members)):
                self.refused("F1", self.staged(value, "F1", members, len(members)), code)

    def test_a3_refusal_then_later_valid_pages_recover(self):
        value = self.event("F1")
        second = collection_url("F1") + "&page=2"
        self.refused("F1", self.staged(value, "F1", [document("B"), {"id": "C", "kind": None, "url": BASE + "/documents/C"}],
                                       2, next_cursor=second))
        self.assertEqual(len(self.inventory.pending_pages(value.message_id)), 1)
        self.page(value, "F1", [document("B")], 2, next_cursor=second, marker="retry")
        self.page(value, "F1", [document("C")], 2, cursor=second)
        self.assertEqual(self.node("F1")["state"], "enumerated")
        self.assertEqual([r[0] for r in self.member_rows("F1")], ["B", "C"])

    def test_a3_existing_page_replay_with_malformed_members_is_refused(self):
        value = self.event("F1")
        data = self.page(value, "F1", [document("B")], 1)
        self.refused("F1", dict(data, members=[None]))
        self.refused("F1", dict(data, members=[{"id": "B", "kind": "document"}]))
        self.accept("F1", data)  # the exact original replay remains valid

    def test_a3_pagination_loop_denominator_conflict_and_hold_retained(self):
        value = self.event("F1", "F2")
        second = collection_url("F1") + "&page=2"
        self.page(value, "F1", [document("B")], 2, next_cursor=second)
        with self.assertRaisesRegex(PortalError, "^pagination_loop_or_bound$"):
            self.page(value, "F1", [document("C")], 2, cursor=second, next_cursor=collection_url("F1"))
        self.assertEqual((self.node("F1")["state"], self.node("F1")["reason"]), ("blocked", "pagination_loop_or_bound"))
        # A held collection keeps its existing refusal ahead of member parsing.
        self.refused("F1", self.staged(value, "F1", [None], 1, cursor=second, marker="held"), "collection_missing_or_blocked")
        other = collection_url("F2") + "&page=2"
        self.page(value, "F2", [document("D")], 2, next_cursor=other)
        self.refused("F2", self.staged(value, "F2", [document("E")], 3, cursor=other), "collection_denominator_conflict")
        self.assertEqual(self.member_rows("F1"), [("B", "document", BASE + "/documents/B")])

    def test_a3_unknown_denominator_is_never_completed(self):
        value = self.event("F1")
        self.refused("F1", self.staged(value, "F1", [{"id": "B", "kind": "document", "url": None}], None))
        self.page(value, "F1", [document("B")], None)
        node = self.node("F1")
        self.assertEqual((node["state"], node["expected_total"]), ("partial", None))
        self.assertFalse(self.inventory.readiness(value.message_id)["retrieval_complete"])
        self.assertEqual(len(self.inventory.pending_pages(value.message_id)), 1)

    # -- A4 deciding control (negative-control case disables only the gate) ---
    def test_a4_null_member_is_portal_error(self):
        value = self.event("F1")
        self.refused("F1", self.staged(value, "F1", [None], 1))

    def test_a4_missing_kind_is_portal_error(self):
        value = self.event("F1")
        self.refused("F1", self.staged(value, "F1", [{"id": "B", "url": BASE + "/documents/B"}], 1))

    def test_a4_unhashable_kind_is_portal_error(self):
        value = self.event("F1")
        self.refused("F1", self.staged(value, "F1", [{"id": "B", "kind": ["document"], "url": BASE + "/documents/B"}], 1))

    def test_a4_valid_page_control(self):
        value = self.event("F1")
        self.page(value, "F1", [document("B")], 1)
        self.assertEqual(self.member_rows("F1"), [("B", "document", BASE + "/documents/B")])

    # -- A5 actual integration -----------------------------------------------
    def test_a5_refusal_then_accepted_members_ingest_through_portal_queue(self):
        value = self.event("F1", "F2")
        self.refused("F2", self.staged(value, "F2", [document("D"), {"id": "E", "kind": "document", "url": 5}], 2))
        self.page(value, "F1", [document("B"), document("C")], 2)
        delivered = []

        class Transport:
            def open(self, url, *, timeout):
                letter = url.rsplit("/", 1)[-1]
                delivered.append(letter)
                body = bridge.PDFS[letter]
                return Response(200, {"content-type": "application/pdf", "content-length": str(len(body))}, (body,))

        approval = Approval(datetime.now(timezone.utc) + timedelta(hours=1), frozenset({HOST}), frozenset(), {HOST: ("/",)})
        ledger = WP1LedgerAdapter(queue=self.queue, database=self.root.ledger, cas_root=self.root.intake / "blobs")

        def fetch():
            return fetch_queue(self.queue, apply=True, approval_loader=lambda: approval,
                               egress=lambda url: True, transport=Transport(), ledger=ledger)

        self.assertEqual((fetch()["received"], sorted(delivered)), (2, ["B", "C"]))
        ready = self.inventory.readiness(value.message_id)
        self.assertFalse(ready["retrieval_complete"])
        self.assertIn("collection_membership_unknown_or_exception", ready["gaps"])
        self.page(value, "F2", [document("D")], 1)
        self.assertEqual(fetch()["received"], 1)
        ready = self.inventory.readiness(value.message_id)
        self.assertTrue(ready["retrieval_complete"])
        self.assertEqual(ready["unique_preserved_documents"], 4)  # Attachment A and B/C/D.
        self.assertFalse(ready["publication_authorized"])

    def test_a4_shape_gate_is_a_module_level_callable(self):
        # The negative-control case patches exactly this callable in memory.
        self.assertTrue(callable(getattr(collections_module, "_require_member_shape", None)))


if __name__ == "__main__":
    unittest.main()
