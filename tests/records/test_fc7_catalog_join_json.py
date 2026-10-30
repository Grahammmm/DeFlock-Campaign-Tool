"""FC7-CATALOG-JOIN-JSON: typed join evidence must decode unambiguously.

The canonical catalog projection selects a source citation from the typed join
``evidence`` JSON. Ordinary ``json.loads`` keeps the last of repeated keys, so a
row such as ``{"locator": "page:9", "locator": "page:1"}`` silently cites
whichever declaration came last. These cases call ``ledger_catalog.project`` on
a producer-shaped synthetic snapshot (seeded like tests.records.test_ledger_catalog,
evidence encoded with the ledger's canonical writer) and mutate only the typed
join evidence text. All data is synthetic; nothing touches a real ledger.
"""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from campaign_tool.records import ledger_catalog as module
from campaign_tool.records.ledger import store
from campaign_tool.records.ledger.stages import query_counts
from tests.records.test_ledger_catalog import H1, H2

CASES = ("valid_typed_join_projection", "duplicate_source_and_locator", "nested_and_identical_duplicate",
         "nonfinite_constants_and_overflow", "malformed_and_consumer_shape_errors",
         "reordering_whitespace_and_source_unchanged")
CASE_MAP = {case: "test_" + case for case in CASES}
REASON = "typed_join_evidence_required"
# Columns project() reads from joins via _rows (kept in step with ledger_catalog.project).
JOIN_COLUMNS = ("id", "original_sha256", "agency_id", "request_id", "join_type", "evidence", "status",
                "blocked_reason")
# Distinctive values placed only in the losing/ambiguous declarations; they must never be echoed.
SHADOW = "page:shadow-declaration"
STAMP = "2026-01-02T00:00:00+00:00"


def valid_evidence(locator="page:1", source=H1):
    """Producer-shaped canonical evidence text (sorted keys, compact separators)."""
    return store.canonical({"source_sha256": source, "locator": locator})


class CatalogJoinJsonTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="fc7-catalog-join-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.root.chmod(0o700)
        self.db = self.root / "ledger.sqlite"
        self.snapshots = 0
        store.initialize(self.db)
        with closing(sqlite3.connect(self.db)) as con, con:
            columns = {row[1] for row in con.execute("PRAGMA table_info(joins)")}
            self.assertTrue(set(JOIN_COLUMNS) <= columns, "synthetic snapshot must carry the projected join columns")
            con.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?)",
                        ("synthetic", "test", "2026-01-01T00:00:00+00:00", None, "synthetic", None, None, None,
                         "running", "{}"))
            for index, sha in enumerate((H1, H2)):
                con.execute("INSERT INTO originals VALUES(?,?,?,?,?,?,?,?,?,?)",
                            (sha, 12, "text/plain", STAMP, "record", "in_scope", None, "captured", "text", "{}"))
                for stage in module.STAGES:
                    con.execute("INSERT INTO stage_state VALUES(?,?,?,?,?,?,?,?)",
                                (sha, stage, "pending", None, "unassigned", STAMP, "synthetic", None))
                con.execute("INSERT INTO occurrences VALUES(?,?,?,?,?,?,?,?)",
                            ("occ" + str(index), sha, "local", "synthetic", None, STAMP, "test", "{}"))
            for name in ("north", "south"):
                con.execute("INSERT INTO agencies VALUES(?,?,?,?)",
                            ("agency:" + name, name.title(), "Synthetic county", None))
                con.execute("INSERT INTO requests VALUES(?,?,?,?,?,?,?,?)",
                            ("request:" + name, "agency:" + name, "SYN-1", None, "2026-01-20", None, None, "open"))

    # helpers ---------------------------------------------------------------
    def set_join(self, evidence, *, identifier="join-typed", sha=H1, status="typed"):
        with closing(sqlite3.connect(self.db)) as con, con:
            con.execute("DELETE FROM joins")
            con.execute("INSERT INTO joins VALUES(?,?,?,?,?,?,?,?)",
                        (identifier, sha, "agency:north", "request:north", "agreement_party", evidence, status, None))

    def project(self):
        """Capture a disposable snapshot copy, count it like build(), then call project()."""
        source_before = self.db.read_bytes()
        self.snapshots += 1
        copy = self.root / ("snapshot-%d.sqlite" % self.snapshots)
        with (closing(sqlite3.connect(self.db)) as source, source,
              closing(sqlite3.connect(copy)) as dest, dest):
            source.backup(dest)
        copy.chmod(0o600)
        counts = query_counts(copy)
        snapshot_before = copy.read_bytes()
        try:
            with closing(sqlite3.connect(copy)) as con, con:
                con.row_factory = sqlite3.Row
                con.execute("PRAGMA query_only=ON")
                return module.project(con, counts)
        finally:
            self.assertEqual(copy.read_bytes(), snapshot_before, "project must not change the snapshot")
            self.assertEqual(self.db.read_bytes(), source_before, "project must not change the source ledger")

    def assert_rejected(self, evidence, *, reason=REASON, hidden=(SHADOW,)):
        self.set_join(evidence)
        result = "unset"
        with self.assertRaises(module.CatalogError) as caught:
            result = self.project()
        self.assertEqual(result, "unset", "no partial projection may be returned")
        error = caught.exception
        self.assertIs(type(error), module.CatalogError)
        self.assertEqual(error.args, (reason,))
        chain, seen = [], error
        while seen is not None and len(chain) < 10:
            chain.append(str(seen) + " " + repr(getattr(seen, "args", ())))
            seen = seen.__cause__ or seen.__context__
        for marker in hidden:
            self.assertFalse(any(marker in text for text in chain), "source value echoed in error chain")
        return error

    def card(self, result, sha=H1):
        return next(c for c in result["cards"] if c["sha256"] == sha)

    def assert_valid_projection(self, result, locator="page:1"):
        card = self.card(result)
        self.assertEqual(card["agency_ids"], ["agency:north"])
        self.assertEqual(card["agency_status"], "typed")
        self.assertEqual(card["request_ids"], ["request:north"])
        self.assertEqual(len(card["joins"]), 1)
        self.assertEqual(card["joins"][0]["status"], "typed")
        self.assertEqual(card["joins"][0]["evidence"], {"source_sha256": H1, "locator": locator})
        self.assertEqual(result["link_validation"]["candidate_links"], 1)
        self.assertFalse(result["publication_ready"])
        other = self.card(result, H2)
        self.assertEqual((other["agency_ids"], other["agency_status"]), ([], "unknown"))

    # frozen cases ------------------------------------------------------------
    def test_valid_typed_join_projection(self):
        self.set_join(valid_evidence())
        result = self.project()
        self.assert_valid_projection(result)
        self.assertEqual(result["counts"]["originals"], 2)
        self.assertEqual(result["snapshot_id"], module.digest(module.encoded(
            {k: v for k, v in result.items() if k != "snapshot_id"})))
        # A second-source citation decodes and is cited exactly as declared.
        self.set_join(valid_evidence("page:4", H2))
        self.assertEqual(self.card(self.project())["joins"][0]["evidence"], {"source_sha256": H2, "locator": "page:4"})

    def test_duplicate_source_and_locator(self):
        both_known = '{"source_sha256": "%s", "source_sha256": "%s", "locator": "page:1"}'
        for first, last in ((H2, H1), (H1, H2)):
            with self.subTest(duplicate="source_sha256", first=first[:8]):
                self.assert_rejected(both_known % (first, last), hidden=())
        for text in ('{"source_sha256": "%s", "locator": "%s", "locator": "page:1"}' % (H1, SHADOW),
                     '{"source_sha256": "%s", "locator": "page:1", "locator": "%s"}' % (H1, SHADOW),
                     '{"locator": "%s", "source_sha256": "%s", "locator": "page:1"}' % (SHADOW, H1)):
            with self.subTest(duplicate="locator", text=text[:40]):
                self.assert_rejected(text)

    def test_nested_and_identical_duplicate(self):
        base = '"source_sha256": "%s", "locator": "page:1"' % H1
        cases = {
            "identical_source": '{"source_sha256": "%s", "source_sha256": "%s", "locator": "page:1"}' % (H1, H1),
            "identical_locator": '{"source_sha256": "%s", "locator": "page:1", "locator": "page:1"}' % H1,
            "nested_object": '{%s, "context": {"page": "%s", "page": 2}}' % (base, SHADOW),
            "nested_identical": '{%s, "context": {"page": 2, "page": 2}}' % base,
            "deep_in_array": '{%s, "context": [1, {"a": {"b": "%s", "b": "x"}}]}' % (base, SHADOW),
            "unrelated_top_level": '{%s, "opaque": "%s", "opaque": "x"}' % (base, SHADOW),
        }
        for name, text in cases.items():
            with self.subTest(case=name):
                self.assert_rejected(text)

    def test_nonfinite_constants_and_overflow(self):
        base = '"source_sha256": "%s", "locator": "page:1"' % H1
        for literal in ("NaN", "Infinity", "-Infinity", "1e400", "-1e400", "1.8e308", "2.5E+999"):
            for placement in ('{%s, "score": %s}', '{%s, "context": {"scores": [0, %s]}}'):
                with self.subTest(literal=literal, placement=placement[-20:]):
                    self.assert_rejected(placement % (base, literal))

    def test_malformed_and_consumer_shape_errors(self):
        long_locator = "p" * 501
        cases = {
            "truncated": '{"source_sha256": "%s", "locator": "page:1"' % H1,
            "empty_text": "",
            "not_json": "page:1 of the synthetic source",
            "trailing_data": valid_evidence() + " {}",
            "array": json.dumps([H1, "page:1"]),
            "null": "null",
            "string": json.dumps(valid_evidence()),
            "missing_locator": store.canonical({"source_sha256": H1}),
            "missing_source": store.canonical({"locator": "page:1"}),
            "unknown_source": valid_evidence(source=hashlib.sha256(b"synthetic unknown").hexdigest()),
            "integer_locator": store.canonical({"source_sha256": H1, "locator": 1}),
            "empty_locator": valid_evidence(""),
            "overlong_locator": valid_evidence(long_locator),
            "source_case_variant": valid_evidence(source=H1.upper()),
        }
        for name, text in cases.items():
            with self.subTest(case=name):
                self.assert_rejected(text, hidden=(long_locator,) if name == "overlong_locator" else ())
        # The boundary locator length is still accepted by the unchanged consumer check.
        self.set_join(valid_evidence("p" * 500))
        self.assertEqual(self.card(self.project())["joins"][0]["evidence"]["locator"], "p" * 500)
        # Non-typed rows never decode evidence: an ambiguous hinted row stays opaque and unexported.
        self.set_join('{"locator": "%s", "locator": "x"}' % SHADOW, status="hinted")
        hinted = self.project()
        self.assertEqual(self.card(hinted)["joins"][0]["evidence"], None)
        self.assertNotIn(SHADOW, module.encoded(hinted).decode())
        # Scope exclusion still wins before any evidence is decoded.
        excluded = hashlib.sha256(b"synthetic excluded original").hexdigest()
        with closing(sqlite3.connect(self.db)) as con, con:
            con.execute("INSERT INTO originals VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (excluded, 12, "text/plain", STAMP, "record", "out_of_scope", None, "captured", "text", "{}"))
            for stage in module.STAGES:
                con.execute("INSERT INTO stage_state VALUES(?,?,?,?,?,?,?,?)",
                            (excluded, stage, "pending", None, "unassigned", STAMP, "synthetic", None))
        for evidence in ('{"source_sha256": "%s", "source_sha256": "%s", "locator": "page:1"}' % (excluded, excluded),
                         "{"):
            with self.subTest(case="scope_excluded_first", evidence=evidence[:12]):
                self.set_join(evidence, sha=excluded)
                with self.assertRaises(module.CatalogError) as caught:
                    self.project()
                self.assertEqual(caught.exception.args, ("source_scope_excluded",))

    def test_reordering_whitespace_and_source_unchanged(self):
        self.set_join(valid_evidence())
        reference = self.project()
        variants = {
            "reordered": '{"locator":"page:1","source_sha256":"%s"}' % H1,
            "whitespace": '\n  {\t"source_sha256" :  "%s" ,\r\n "locator":"page:1"  }  \n' % H1,
            "unicode_escape": '{"source_sha256": "%s", "locator": "\\u0070age:1"}' % H1,
            "finite_extras": ('{"source_sha256": "%s", "locator": "page:1", "context": {"max": 1.7976931348623157e308,'
                              ' "tiny": 5e-324, "neg_zero": -0.0, "int": 12345678901234567890, "list": [1, 2.5, -3e2],'
                              ' "same_name_other_level": {"locator": "nested"}}}' % H1),
        }
        for name, text in variants.items():
            with self.subTest(variant=name):
                self.set_join(text)
                result = self.project()
                self.assert_valid_projection(result)
                # Only the validated citation is exported, so the whole projection is identical.
                self.assertEqual(result, reference)
        # Object member order inside the decoded evidence is preserved for the consumer.
        self.assertEqual(list(module._json('{"b": 1, "a": {"y": 2, "x": 3}}', REASON)), ["b", "a"])
        self.assertEqual(list(module._json('{"b": 1, "a": {"y": 2, "x": 3}}', REASON)["a"]), ["y", "x"])
        # A rejected projection leaves the source, a fresh valid projection and the reference intact.
        # (project() itself asserts byte-identical source and snapshot on every call, raised or not.)
        self.assert_rejected('{"source_sha256": "%s", "locator": "%s", "locator": "page:1"}' % (H1, SHADOW))
        self.set_join(valid_evidence())
        self.assertEqual(self.project(), reference)


if __name__ == "__main__":
    unittest.main()
