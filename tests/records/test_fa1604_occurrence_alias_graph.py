"""Fictional alias equivalence classes against the unchanged staged projector.

No application import or fixture side effect occurs during discovery. The oracle
is a hand-authored semantic graph, not a call to the production identity helper.
"""
from contextlib import contextmanager, ExitStack
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import unquote, urlsplit
import urllib.request

REPO = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = None  # Launcher-owned retained root; ordinary unittest owns cleanup.
STAMP = "2001-02-03T04:05:06+00:00"


def compact(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def ancestry(path):
    path = Path(os.path.abspath(path))
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("synthetic ancestry contains symlink")
    return path


@contextmanager
def synthetic_boundary(root):
    """Scoped accidental-use sentinels, not a hostile-code sandbox.

    Inspected import closure: package version, ledger store, two SQL migrations.
    All reachable data paths are required arguments; there are no bound data
    defaults or model clients. Compatibility adds only the inspected factories.
    """
    root = ancestry(root)
    connect, builtin_open = sqlite3.connect, io.open
    old_umask = os.umask(0o077)

    def denied(*args, **kwargs):
        raise RuntimeError("FA1604 offline network/model/process sentinel")

    def confined(path, *args, **kwargs):
        if isinstance(path, int):
            return builtin_open(path, *args, **kwargs)
        target = ancestry(path)
        mode = args[0] if args else kwargs.get("mode", "r")
        writing = any(c in mode for c in "wax+")
        if not target.is_relative_to(root) and (writing or not target.is_relative_to(REPO)):
            raise RuntimeError("FA1604 real-data file sentinel")
        return builtin_open(path, *args, **kwargs)

    def synthetic_sqlite(database, *args, **kwargs):
        text = os.fspath(database)
        # SQLite's exact private-memory target cannot access real data. Do not
        # exempt file: URIs, empty targets, or filesystem paths from confinement.
        if text == ":memory:":
            return connect(database, *args, **kwargs)
        target = unquote(urlsplit(text).path) if text.startswith("file:") else text
        if not ancestry(target).is_relative_to(root):
            raise RuntimeError("FA1604 real-data SQLite sentinel")
        return connect(database, *args, **kwargs)

    try:
        with ExitStack() as stack:
            stack.enter_context(patch("sqlite3.connect", synthetic_sqlite))
            stack.enter_context(patch("io.open", confined))
            stack.enter_context(patch("socket.create_connection", denied))
            stack.enter_context(patch.object(socket.socket, "connect", denied))
            stack.enter_context(patch.object(socket.socket, "connect_ex", denied))
            stack.enter_context(patch("urllib.request.urlopen", denied))
            stack.enter_context(patch("subprocess.Popen", denied))
            yield
    finally:
        os.umask(old_umask)


# Legacy producer-shaped schema, independently populated with fictional rows.
LEGACY_SQL = """
CREATE TABLE docs(sha TEXT PRIMARY KEY,bytes INTEGER,format TEXT,stage TEXT,first_seen TEXT,review_status TEXT,version TEXT,digest TEXT);
CREATE TABLE occurrences(oid TEXT,sha TEXT,path TEXT,root TEXT,parent TEXT,locator TEXT,class TEXT,receipt TEXT,first_seen TEXT,last_seen TEXT,PRIMARY KEY(oid,sha));
CREATE TABLE units(sha TEXT,ordinal INTEGER,kind TEXT,locator TEXT,text TEXT,data TEXT,PRIMARY KEY(sha,ordinal));
CREATE TABLE preservations(sha TEXT PRIMARY KEY,path TEXT,verified_at TEXT,bytes INTEGER);
CREATE TABLE extraction_attempts(id TEXT PRIMARY KEY,sha TEXT);
CREATE TABLE edges(parent TEXT,locator TEXT,child TEXT,name TEXT,PRIMARY KEY(parent,locator));
CREATE TABLE edge_history(parent TEXT,locator TEXT,child TEXT,name TEXT);
CREATE TABLE document_history(sha TEXT,record TEXT);
CREATE TABLE events(run TEXT,stage TEXT,code TEXT);
CREATE TABLE runs(id TEXT PRIMARY KEY,kind TEXT);
CREATE TABLE scope_exclusions(path TEXT,sha TEXT,reason TEXT,PRIMARY KEY(path,sha));
CREATE TABLE seen(run TEXT,oid TEXT,sha TEXT,PRIMARY KEY(run,oid));
CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT);
"""


class AliasGraphTests(unittest.TestCase):
    def setUp(self):
        if FIXTURE_ROOT is None:
            tmp = tempfile.TemporaryDirectory(prefix="fa1604-unittest-")
            self.addCleanup(tmp.cleanup)
            self.root = ancestry(tmp.name)
        else:
            self.root = Path(tempfile.mkdtemp(prefix="fixture-", dir=ancestry(FIXTURE_ROOT)))
        # enterContext registers the guard teardown even if an import fails.
        self.enterContext(synthetic_boundary(self.root))
        from campaign_tool.records.ledger import occurrence_projection, store
        self.projector, self.store = occurrence_projection, store
        # Exercise reference-schema construction under the guard every time;
        # correctness must not depend on another module warming this cache.
        store._base_schema.cache_clear()
        self.mail_sha = hashlib.sha256(b"FA1604 fictional identical message octets").hexdigest()
        self.part_sha = hashlib.sha256(b"FA1604 fictional identical attachment octets").hexdigest()

    def rows(self, missing=False):
        mail_receipt = {"mail_identity": {"account": "fictional-account", "folder": "INBOX",
                        "uidvalidity": 11, "uid": 7}, "eml_sha256": self.mail_sha, "received_at": STAMP}
        rows = []
        for oid, uid in (("mail-a", 7), ("mail-a-alias", 7), ("mail-b", 8)):
            receipt = json.loads(compact(mail_receipt))
            receipt["mail_identity"]["uid"] = uid
            rows.append(dict(oid=oid, sha=self.mail_sha, parent=None, locator=None, receipt=receipt))
        for oid, parent, mime in (("part-a", "mail-a", "1.2"),
                                  ("part-a-alias", "mail-a-alias", "1.2"),
                                  ("part-other-path", "mail-a-alias", "1.3"),
                                  ("part-b", "mail-b", "1.2")):
            rows.append(dict(oid=oid, sha=self.part_sha, parent=self.mail_sha, locator={"mime": mime},
                             receipt={"relation": "decoded_mime_payload", "parent_sha256": self.mail_sha,
                                      "parent_legacy_oid": "absent-mail" if missing and oid == "part-other-path" else parent}))
        return rows

    def capture(self, label, rows):
        root = self.root / label
        root.mkdir(mode=0o700)
        source = root / "source"
        source.mkdir(mode=0o700)
        database = root / "candidate" / "ledger.sqlite"
        with sqlite3.connect(source / "intake.sqlite") as con:
            con.executescript(LEGACY_SQL)
            for sha, form in ((self.mail_sha, "eml"), (self.part_sha, "txt")):
                con.execute("INSERT INTO docs VALUES(?,?,?,?,?,?,?,?)",
                            (sha, 42, form, "complete", STAMP, "full", "fictional", "{}"))
            for row in rows:
                con.execute("INSERT INTO occurrences VALUES(?,?,?,?,?,?,?,?,?,?)",
                            (row["oid"], row["sha"], "fictional-source", "fictional-root", row["parent"],
                             compact(row["locator"]), "hint-only", compact(row["receipt"]), STAMP, STAMP))
        raw = (source / "intake.sqlite").read_bytes()
        (source / "input-manifest.json").write_text(compact({"database_snapshot_sha256": hashlib.sha256(raw).hexdigest()}))
        captured = self.store.import_legacy(source, database)
        self.assertEqual(captured["status"], "captured")
        return database, captured["import_id"]

    def expected_graph(self, missing=False):
        mail_a = compact({"scheme": "mail_account_folder_uid_v1", "account": "fictional-account",
                          "folder": "INBOX", "uidvalidity": 11, "uid": 7})
        mail_b = compact({"scheme": "mail_account_folder_uid_v1", "account": "fictional-account",
                          "folder": "INBOX", "uidvalidity": 11, "uid": 8})
        # Parent references are semantic mail source_ref values, never IDs from
        # a second execution of the production identity algorithm.
        result: set[tuple[str, str, str, str | None]] = {("mail", mail_a, self.mail_sha, None), ("mail", mail_b, self.mail_sha, None)}
        for parent, mime in ((mail_a, "1.2"), (mail_b, "1.2"), (mail_a, "1.3")):
            if missing and mime == "1.3":
                continue
            result.add(("attachment", compact({"scheme": "mail_attachment_hierarchical_root_1_v1",
                                               "message_source_ref": json.loads(parent), "mime_part_path": mime}),
                        self.part_sha, parent))
        return result

    def inspect_graph(self, con):
        saved = {r["id"]: dict(r) for r in con.execute("SELECT * FROM occurrences")}
        graph, by_id = set(), {}
        for oid, row in saved.items():
            ref = json.loads(row["source_ref"])
            parent = row["parent_occurrence_id"]
            if row["kind"] == "attachment":
                self.assertIn(parent, saved)
                self.assertEqual(saved[parent]["kind"], "mail")
                self.assertEqual(ref.pop("message_occurrence_id"), parent)
                ref["message_source_ref"] = json.loads(saved[parent]["source_ref"])
            semantic = (row["kind"], compact(ref), row["original_sha256"], saved[parent]["source_ref"] if parent else None)
            graph.add(semantic)
            by_id[oid] = semantic
        return graph, by_id, saved

    def assert_alias_cardinality(self, result, missing=False):
        # This exact assertion is also the decisive oracle for the faulty variant.
        self.assertEqual(result["canonical_occurrences_in_projection"], 4 if missing else 5,
                         "alias-cardinality: legacy aliases must coalesce")
        self.assertEqual(result["canonical_kinds"], {"mail": 2, "attachment": 2 if missing else 3})

    def exercise(self, label, rows, missing=False):
        database, import_id = self.capture(label, rows)
        before = self.store.counts(database)
        self.assertEqual(before["originals"], 2)
        self.assertEqual(before["canonical_occurrences"], 0)
        self.assertEqual(before["acceptance"], "candidate_unreconciled")
        self.assertIsNone(before["end_to_end_complete"])
        self.assertEqual(before["stage_slots_observed"], 14)
        for stage in before["stages"].values():
            self.assertEqual(stage, {"pending": 2, "done": 0, "blocked": 0, "in_progress": 0, "inapplicable": 0})
        with self.store.ledger(database, readonly=True) as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM stage_state WHERE receipt_sha256 IS NOT NULL").fetchone()[0], 0)
            stage_before = [tuple(r) for r in con.execute("SELECT * FROM stage_state ORDER BY original_sha256,stage")]
            events_before = [tuple(r) for r in con.execute("SELECT * FROM stage_events ORDER BY rowid")]
            source_before = [tuple(r) for r in con.execute("SELECT * FROM legacy_rows ORDER BY source_table,source_rowid")]
        result = self.projector.project_occurrences(database, import_id)
        self.assert_alias_cardinality(result, missing)
        self.assertEqual(result["source_rows"], 7)
        self.assertEqual(result["projected_source_rows"], 6 if missing else 7)
        self.assertEqual(result["blocked_source_rows"], int(missing))
        self.assertEqual(result["scope_excluded_source_rows"], 0)
        self.assertEqual(result["unresolved_reasons"], {"typed_mail_parent_not_established": 1} if missing else {})
        self.assertEqual(result["stage_promotions"], 0)
        self.assertEqual(result["verified_agency_request_joins"], 0)
        self.assertIs(result["publication_ready"], False)
        self.assertIsNone(result["end_to_end_complete"])
        with self.store.ledger(database, readonly=True) as con:
            graph, by_id, saved = self.inspect_graph(con)
            self.assertEqual(graph, self.expected_graph(missing))
            self.assertEqual(len(saved), 4 if missing else 5)
            bindings = list(con.execute("SELECT * FROM occurrence_projection_rows ORDER BY source_rowid"))
            sources = list(con.execute("SELECT * FROM legacy_rows WHERE source_table='occurrences' ORDER BY source_rowid"))
            self.assertEqual(len(bindings), 7)
            self.assertEqual(len(sources), 7)
            classes = {"mail-a": (7, None), "mail-a-alias": (7, None), "mail-b": (8, None),
                       "part-a": (7, "1.2"), "part-a-alias": (7, "1.2"),
                       "part-other-path": (7, "1.3"), "part-b": (8, "1.2")}
            for binding, source, supplied in zip(bindings, sources, rows):
                payload = json.loads(source["payload_json"])
                self.assertEqual(payload["oid"], supplied["oid"])
                for field in ("import_id", "source_table", "source_rowid", "payload_sha256", "payload_json"):
                    self.assertEqual(binding[field], source[field])
                self.assertEqual(binding["projection_id"], result["projection_id"])
                blocked = missing and payload["oid"] == "part-other-path"
                self.assertEqual(binding["disposition"], "blocked" if blocked else "projected")
                if blocked:
                    self.assertIsNone(binding["occurrence_id"])
                    self.assertEqual(binding["reason"], "typed_mail_parent_not_established")
                else:
                    uid, mime = classes[payload["oid"]]
                    semantic = by_id[binding["occurrence_id"]]
                    ref = json.loads(semantic[1])
                    self.assertEqual(semantic[0], "mail" if mime is None else "attachment")
                    self.assertEqual(semantic[2], self.mail_sha if mime is None else self.part_sha)
                    self.assertEqual(ref["uid"] if mime is None else ref["message_source_ref"]["uid"], uid)
                    self.assertEqual(ref.get("mime_part_path"), mime)
                    self.assertEqual(binding["reason"], "explicit_mail_identity" if mime is None else "explicit_parent_and_hierarchical_mime")
            for oid, occurrence in saved.items():
                evidence = json.loads(occurrence["evidence"])
                contributors = [b for b in bindings if b["occurrence_id"] == oid]
                self.assertTrue(contributors)
                self.assertEqual(evidence, {"projection_id": result["projection_id"], "import_id": import_id,
                                           "source_table": "occurrences", "source_rowid": contributors[0]["source_rowid"],
                                           "payload_sha256": contributors[0]["payload_sha256"],
                                           "verification": "captured_declaration_only", "agency_request_join": "blocked"})
                self.assertEqual(occurrence["acquired_at"], STAMP if occurrence["kind"] == "mail" else None)
            self.assertEqual([tuple(r) for r in con.execute("SELECT * FROM legacy_rows ORDER BY source_table,source_rowid")], source_before)
            self.assertEqual([tuple(r) for r in con.execute("SELECT * FROM stage_state ORDER BY original_sha256,stage")], stage_before)
            self.assertEqual([tuple(r) for r in con.execute("SELECT * FROM stage_events ORDER BY rowid")], events_before)
            joins = [tuple(r) for r in con.execute("SELECT agency_id,request_id,status,blocked_reason FROM joins")]
            self.assertEqual(joins, [(None, None, "blocked", "agency_request_not_established_by_occurrence_projection")] * 7)
            gaps = list(con.execute("SELECT * FROM import_gaps WHERE category='occurrence_projection'"))
            self.assertEqual(len(gaps), int(missing))
            if missing:
                reason = json.loads(gaps[0]["reason"])
                self.assertEqual(reason["code"], "typed_mail_parent_not_established")
                bad = next(b for b in bindings if b["disposition"] == "blocked")
                self.assertEqual(reason["source"], {k: bad[k] for k in ("import_id", "source_table", "source_rowid", "payload_sha256")})
            tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
            snapshot = {t: [tuple(r) for r in con.execute('SELECT * FROM "' + t + '" ORDER BY rowid')] for t in tables}
        after = self.store.counts(database)
        self.assertEqual(before["stages"], after["stages"])
        self.assertEqual(after["acceptance"], "candidate_unreconciled")
        replay = self.projector.project_occurrences(database, import_id)
        self.assertEqual(replay, {**result, "reused": True})
        with self.store.ledger(database, readonly=True) as con:
            self.assertEqual({t: [tuple(r) for r in con.execute('SELECT * FROM "' + t + '" ORDER BY rowid')] for t in tables}, snapshot)
        return graph

    def test_alias_graph_order_and_replay(self):
        rows = self.rows()
        self.assertEqual(self.exercise("parent-first", rows), self.exercise("child-first", list(reversed(rows))))

    def test_only_missing_parent_child_is_blocked(self):
        ordinary, changed = self.rows(), self.rows(missing=True)
        differences = [(a["oid"], k) for a, b in zip(ordinary, changed) for k in a if a[k] != b[k]]
        self.assertEqual(differences, [("part-other-path", "receipt")])
        original_receipt = next(r["receipt"] for r in ordinary if r["oid"] == "part-other-path")
        changed_receipt = next(r["receipt"] for r in changed if r["oid"] == "part-other-path")
        self.assertEqual({k for k in original_receipt if original_receipt[k] != changed_receipt[k]}, {"parent_legacy_oid"})
        self.assertEqual(self.exercise("missing-parent-first", changed, True),
                         self.exercise("missing-child-first", list(reversed(changed)), True))


def faulty_mail_identity(original):
    """Test-local identity fault: bind the legacy OID into mail identity."""
    def altered(row, source_original):
        candidate, reason, receipt, locator = original(row, source_original)
        if candidate is not None and candidate["kind"] == "mail":
            ref = json.loads(candidate["source_ref"])
            ref["legacy_oid"] = row["oid"]
            candidate = dict(candidate, source_ref=compact(ref), id="occurrence:" + hashlib.sha256(compact(["mail", ref]).encode()).hexdigest())
        return candidate, reason, receipt, locator
    return altered
