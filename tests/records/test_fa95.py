"""Portable, synthetic regressions for immutable collection traversal plans."""
from contextlib import contextmanager
from dataclasses import FrozenInstanceError
from pathlib import Path
import tempfile
import unittest

from campaign_tool.records.export_bridge import enroll_export
from campaign_tool.records.portal.collections import CollectionInventory, Scope
from campaign_tool.records.portal.policy import PortalError
from campaign_tool.records.portal.store import Queue
from campaign_tool.records.run import Pipeline, root_lock
from campaign_tool.records.runner.canonical_mail import CanonicalMailBackend
from tests.records.test_export_bridge import legacy_export

HOST = "archive.example.invalid"
OTHER = "social.example.invalid"
BASE = "https://" + HOST


@contextmanager
def fixture(scope):
    """Use the existing synthetic legacy producer and real enrollment path."""
    with tempfile.TemporaryDirectory() as directory:
        base = Path(directory)
        pipeline = Pipeline(base / "canonical")
        export = base / "export"
        export.mkdir(mode=0o700)
        queue = Queue(pipeline.root.path / "portal")
        try:
            inventory = CollectionInventory(queue, scope)
            backend = CanonicalMailBackend(export, pipeline.root.intake, pipeline.root.ledger)
            backend.start_run(pipeline._identity())
            path, _ = legacy_export(export, body=BASE + "/collection\nhttps://" + OTHER + "/profile\n")
            with root_lock(pipeline.root.path):
                value = enroll_export(path, export_root=export, backend=backend, inventory=inventory)
            yield queue, inventory, value.message_id
        finally:
            queue.close()


def state(queue):
    tables = ("export_events", "export_body_links", "export_collections",
              "export_collection_pages", "export_collection_members", "portal_items")
    return {table: [tuple(row) for row in queue.db.execute("SELECT * FROM " + table + " ORDER BY rowid")]
            for table in tables}


def assert_snapshot(test, scope_class):
    hosts, excluded = {HOST}, {OTHER}
    scope = scope_class(hosts, excluded)
    with fixture(scope) as (queue, inventory, event):
        before_hash = inventory._scope_hash()
        before_state = state(queue)
        before_ready = inventory.readiness(event)
        expected = ((BASE + "/collection", "collection", None),
                    ("https://" + OTHER + "/profile", "excluded", "explicit_scope_exclusion"),
                    ("https://outside.example.invalid/collection", "blocked", "unapproved_origin"))
        for url, kind, reason in expected:
            test.assertEqual(inventory._classify(url), (url, kind, reason))
        hosts.clear()
        hosts.add("new.example.invalid")
        excluded.clear()
        excluded.add("new-exclusion.example.invalid")
        test.assertEqual(inventory._scope_hash(), before_hash, "caller mutation changed scope hash")
        test.assertIs(type(scope.hosts), frozenset)
        test.assertIs(type(scope.excluded_hosts), frozenset)
        test.assertIsNot(scope.hosts, hosts)
        test.assertIsNot(scope.excluded_hosts, excluded)
        test.assertEqual(scope.hosts, frozenset({HOST}))
        test.assertEqual(scope.excluded_hosts, frozenset({OTHER}))
        for url, kind, reason in expected:
            test.assertEqual(inventory._classify(url), (url, kind, reason))
        for members in (scope.hosts, scope.excluded_hosts):
            with test.assertRaises(AttributeError):
                members.add("other.example.invalid")
            with test.assertRaises(AttributeError):
                members.clear()
        with test.assertRaises(FrozenInstanceError):
            scope.hosts = frozenset()
        test.assertEqual(state(queue), before_state)
        test.assertEqual(inventory.readiness(event), before_ready)
        root = queue.root
        queue.close()
        reopened = Queue(root)
        try:
            inventory = CollectionInventory(reopened, scope)
            test.assertEqual(inventory._scope_hash(), before_hash)
            test.assertEqual(inventory._event(event)["scope_sha256"], before_hash)
            test.assertEqual(inventory.readiness(event), before_ready)
            test.assertEqual(state(reopened), before_state)
            changed = CollectionInventory(reopened, scope_class({"new.example.invalid"}))
            with test.assertRaises(PortalError) as caught:
                changed._event(event)
            test.assertEqual(str(caught.exception), "export_event_scope_mismatch")
            test.assertEqual(state(reopened), before_state)
        finally:
            reopened.close()


class Behavior(unittest.TestCase):
    def test_set_membership_is_snapshotted_and_hash_stable(self):
        assert_snapshot(self, Scope)

    def test_frozenset_and_empty_memberships_are_accepted(self):
        for container in (set, frozenset):
            for hosts, excluded in ((container(), container()),
                                    (container({HOST}), container({OTHER}))):
                with self.subTest(container=container, hosts=hosts):
                    scope = Scope(hosts, excluded)
                    self.assertIs(type(scope.hosts), frozenset)
                    self.assertIs(type(scope.excluded_hosts), frozenset)
                    self.assertEqual(scope.hosts, hosts)
                    self.assertEqual(scope.excluded_hosts, excluded)
        # Preserve accepted hostname bytes exactly; never normalize or add hosts.
        self.assertEqual(Scope({HOST, "second.example.invalid"}).hosts,
                         frozenset({HOST, "second.example.invalid"}))

    def test_all_existing_bound_endpoints_are_stable(self):
        for field, ceiling in (("max_depth", 3), ("max_pages", 100),
                               ("max_members", 1000), ("max_links", 1000)):
            for value in (1, ceiling):
                with self.subTest(field=field, accepted=value):
                    self.assertEqual(getattr(Scope({HOST}, **{field: value}), field), value)
            for value in (0, ceiling + 1, True, False, 1.0, None, "1"):
                with self.subTest(field=field, refused=value):
                    with self.assertRaises(PortalError) as caught:
                        Scope({HOST}, **{field: value})
                    self.assertEqual(str(caught.exception), "invalid_inventory_bound")

    def test_conflict_refusal_remains_fixed(self):
        for container in (set, frozenset):
            with self.subTest(container=container):
                with self.assertRaises(PortalError) as caught:
                    Scope(container({HOST}), container({HOST}))
                self.assertEqual(str(caught.exception), "scope_conflict")

    def test_invalid_container_shapes_refuse_with_fixed_code(self):
        for value in (None, HOST, [HOST], (HOST,), {HOST: True}, 7, b"host"):
            for field in ("hosts", "excluded_hosts"):
                with self.subTest(value=value, field=field):
                    kwargs = {"hosts": {HOST}, field: value}
                    with self.assertRaises(PortalError) as caught:
                        Scope(**kwargs)
                    self.assertEqual(str(caught.exception), "invalid_inventory_scope")

    def test_invalid_hostname_and_overlap_refuse_without_normalization(self):
        for value in ("bad host", "https://archive.example.invalid", 7, None, HOST.upper(), ""):
            for field in ("hosts", "excluded_hosts"):
                for container in (set, frozenset):
                    with self.subTest(value=value, field=field, container=container):
                        kwargs = {"hosts": {HOST}, field: container({value})}
                        with self.assertRaises(PortalError) as caught:
                            Scope(**kwargs)
                        self.assertEqual(str(caught.exception), "invalid_host")
        for kwargs in ({"hosts": {HOST, 7}}, {"hosts": {HOST}, "excluded_hosts": {OTHER, 7}}):
            with self.subTest(mixed=kwargs):
                with self.assertRaises(PortalError) as caught:
                    Scope(**kwargs)
                self.assertEqual(str(caught.exception), "invalid_host")

    def test_scope_drift_still_refuses_existing_collection(self):
        hosts = {HOST}
        with fixture(Scope(hosts, {OTHER})) as (queue, inventory, event):
            key = inventory.pending_pages(event)[0]["collection"]
            before = state(queue)
            hosts.clear()
            self.assertEqual(inventory._node(key)["scope_sha256"], inventory._scope_hash())
            changed = CollectionInventory(queue, Scope({"new.example.invalid"}))
            with self.assertRaises(PortalError) as caught:
                changed._node(key)
            self.assertEqual(str(caught.exception), "collection_scope_mismatch")
            self.assertEqual(state(queue), before)


class GateControl(unittest.TestCase):
    # The launcher substitutes a file-backed module only in disposable workers.
    scope_class = Scope

    def test_mutation_hash_regression(self):
        assert_snapshot(self, self.scope_class)

    def test_frozenset_positive(self):
        scope = self.scope_class(frozenset({HOST}), frozenset({OTHER}))
        with fixture(scope) as (_, inventory, event):
            self.assertEqual(inventory._event(event)["scope_sha256"], inventory._scope_hash())
            self.assertEqual(inventory._classify(BASE + "/collection"),
                             (BASE + "/collection", "collection", None))


if __name__ == "__main__":
    unittest.main()
