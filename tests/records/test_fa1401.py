"""FA1401: a receipt-source ordinal must be an exact, in-range manifest position.

Python sequence indexing lets ordinal -1 alias the last manifest entry, so a
self-consistent source row (id derived from the same -1) used to satisfy the
rp_source_valid callback, the unchanged rp_source_consistency trigger and
verify(). These cases exercise the installed callback, trigger and verifier on
generated synthetic ledgers only. SQLite delivers ordinals after its INTEGER
column affinity; those cases are judged on the value actually delivered, and
nothing here claims SQL can tell a converted boolean or numeral apart.

No real record, mailbox, canonical store or live path is read. Network and
process sentinels and the scoped data-file/SQLite boundary from
tests/fixtures/fa1401/offline_boundary.py are installed before the application
import and while this module's tests run, and are restored afterwards. File
writes are admitted only under the generated fixture/temp roots; reads also
under this repository and the running interpreter. Any sentinel hit fails the
test that caused it.
"""
import hashlib
import http.client
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import urllib.request

REPO = Path(__file__).resolve().parents[2]
BOUNDARY_FILE = REPO / "tests" / "fixtures" / "fa1401" / "offline_boundary.py"
BOUNDARY_NAME = "fa1401_offline_boundary"


def _load_boundary():
    loaded = sys.modules.get(BOUNDARY_NAME)
    if loaded is not None:
        if Path(loaded.__file__).resolve() != BOUNDARY_FILE:
            raise RuntimeError("fa1401 boundary already loaded from another tree")
        return loaded
    spec = importlib.util.spec_from_file_location(BOUNDARY_NAME, BOUNDARY_FILE)
    loaded = importlib.util.module_from_spec(spec)
    sys.modules[BOUNDARY_NAME] = loaded
    spec.loader.exec_module(loaded)
    return loaded


boundary = _load_boundary()
ARTIFACT_ENV = "FA1401_ARTIFACT_ROOT"


def fixture_parent():
    """Explicit evidence root from the launcher, else the ordinary temp root."""
    configured = os.environ.get(ARTIFACT_ENV)
    parent = Path(configured).resolve() if configured else Path(tempfile.gettempdir()).resolve()
    if not parent.is_dir() or parent == REPO or REPO in parent.parents:
        raise RuntimeError("fa1401 fixture parent must be an existing directory outside the repository")
    return parent


def file_boundary():
    """A fresh scoped boundary admitting writes only to this run's fixture and temp roots."""
    return boundary.FileBoundary(repo=REPO, write_roots=(fixture_parent(), Path(tempfile.gettempdir()).resolve()),
                                 hits=SENTINEL_HITS)


SENTINEL_HITS = []
GUARDED_RUNS = []
APPLICATION_PRELOADED = sorted(name for name in sys.modules if name.split(".")[0] == "campaign_tool")
_ABSENT = object()
NETWORK_TARGETS = ((socket, "create_connection"), (socket, "getaddrinfo"), (socket.socket, "connect"),
                   (socket.socket, "connect_ex"), (http.client.HTTPConnection, "connect"),
                   (urllib.request, "urlopen"))
EXECUTION_TARGETS = NETWORK_TARGETS + ((subprocess, "Popen"),)
MARK = "fa1401_offline_sentinel"


def _sentinel(name):
    def refuse(*args, **kwargs):
        SENTINEL_HITS.append(name)
        raise OSError(MARK + ":" + name)
    setattr(refuse, MARK, name)
    return refuse


def _install(saved, targets):
    for owner, attribute in targets:
        saved.append((owner, attribute, vars(owner).get(attribute, _ABSENT)))
        setattr(owner, attribute, _sentinel(owner.__name__ + "." + attribute))


def _restore(saved):
    while saved:
        owner, attribute, value = saved.pop()
        if value is _ABSENT:
            if attribute in vars(owner):
                delattr(owner, attribute)
        else:
            setattr(owner, attribute, value)


def guards_active():
    return sorted([owner.__name__ + "." + attribute for owner, attribute in EXECUTION_TARGETS
                   if hasattr(getattr(owner, attribute), MARK)] + boundary.active())


EXPECTED_ACTIVE = sorted([owner.__name__ + "." + attribute for owner, attribute in EXECUTION_TARGETS]
                         + boundary.target_names())
IMPORT_BOUNDARY_ACTIVE = []

_import_guard = []
_import_files = file_boundary()
_install(_import_guard, NETWORK_TARGETS)
_import_files.install()
try:
    IMPORT_BOUNDARY_ACTIVE.extend(boundary.active())
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    from campaign_tool.records.ledger import store
    from campaign_tool.records.ledger import receipt_projection as rp
finally:
    _import_files.restore()
    _restore(_import_guard)

LABEL = b"fa1401 synthetic receipt index "
COUNT = 3
MODULE_BOUNDARY = []


def setUpModule():
    saved = []
    files = file_boundary()
    unittest.addModuleCleanup(_restore, saved)
    unittest.addModuleCleanup(files.restore)
    unittest.addModuleCleanup(MODULE_BOUNDARY.clear)
    _install(saved, EXECUTION_TARGETS)
    files.install()
    MODULE_BOUNDARY.append(files)


def index_bytes(position):
    original = hashlib.sha256(LABEL + b"original %d" % position).hexdigest()
    return json.dumps({"original": original, "position": position}).encode() + b"\n"


def manifest_spec(count=COUNT, kind="documents"):
    indexes = []
    for position in range(count):
        indexes.append({"path": "fa1401/index-%d.jsonl" % position,
                        "sha256": hashlib.sha256(index_bytes(position)).hexdigest(),
                        "kind": kind, "fields": {"original_hash": "original"},
                        "declarations": {}, "artifact_paths": {}})
    return {"version": 1, "indexes": indexes}


def mapping_of(entry):
    return rp.canonical({"kind": entry["kind"], "fields": entry["fields"],
                         "declarations": entry.get("declarations", {}),
                         "artifact_paths": entry.get("artifact_paths", {})})


def sid_for(mid, ordinal):
    try:
        return rp.identity(mid, ordinal)
    except (TypeError, ValueError):
        return rp.identity(mid, 0)


def call_args(mid, body, ordinal, entry_position, *, sid=None):
    """Callback arguments that are self-consistent for `ordinal` and point at one entry."""
    entry = json.loads(body)["indexes"][entry_position]
    return (sid_for(mid, ordinal) if sid is None else sid, mid, ordinal, entry["path"],
            mapping_of(entry), entry["sha256"], body)


class ReceiptOrdinalTests(unittest.TestCase):
    def setUp(self):
        # The offline boundary must be live for every case, and any hit is fatal to it.
        self.assertEqual(guards_active(), EXPECTED_ACTIVE)
        self.assertEqual(len(MODULE_BOUNDARY), 1)
        self.addCleanup(self.assert_no_new_sentinel_hits, len(SENTINEL_HITS))
        GUARDED_RUNS.append(self.id())
        self.tmp = tempfile.TemporaryDirectory(prefix="fa1401-", dir=fixture_parent())
        self.root = Path(self.tmp.name)
        self.root.chmod(0o700)
        self.db = self.root / "ledger.sqlite"
        store.initialize(self.db)
        self.body = rp.canonical(manifest_spec()).encode()
        self.mid = rp.digest(self.body)

    def tearDown(self):
        self.tmp.cleanup()

    def assert_no_new_sentinel_hits(self, seen):
        self.assertEqual(SENTINEL_HITS[seen:], [], "offline sentinel refused an access")

    # -- synthetic ledger helpers -------------------------------------------------
    def insert_manifest(self, con, body=None):
        body = self.body if body is None else body
        con.execute("INSERT INTO rp_manifests VALUES(?,?,?)", (rp.digest(body), str(self.root), body))
        con.commit()
        return rp.digest(body)

    def source_row(self, mid, body, ordinal, entry_position):
        entry = json.loads(body)["indexes"][entry_position]
        raw = index_bytes(entry_position)
        return (rp.identity(mid, ordinal), mid, ordinal, entry["path"], mapping_of(entry), entry["sha256"],
                rp.digest(raw), len(raw), raw, None)

    def insert_source(self, con, row):
        con.execute("INSERT INTO rp_sources VALUES(?,?,?,?,?,?,?,?,?,?)", row)

    def dump(self):
        with store.ledger(self.db, readonly=True) as con:
            tables = [r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'rp_%' ORDER BY name")]
            return {table: [tuple(r) for r in con.execute("SELECT * FROM " + table + " ORDER BY rowid")]
                    for table in tables}

    def historical_capture(self, ordinals):
        """Emulate sources admitted by an older callback; trigger DDL stays unchanged."""
        with store.ledger(self.db) as con:
            rp.install(con)
            mid = self.insert_manifest(con)
            con.create_function("rp_source_valid", 7, lambda *args: 1, deterministic=True)
            for ordinal in ordinals:
                self.insert_source(con, self.source_row(mid, self.body, ordinal, ordinal % COUNT))
            con.commit()
        return mid

    # -- offline boundary --------------------------------------------------------
    def test_offline_file_boundary_scoped(self):
        self.assertEqual(IMPORT_BOUNDARY_ACTIVE, boundary.target_names())
        # The hooks sit on the exact attributes the ledger code looks up at call time.
        reachable = {"store.sqlite3.connect": store.sqlite3.connect, "store.os.open": store.os.open,
                     "rp.os.open": rp.os.open, "tempfile mkstemp os.open": getattr(store.tempfile, "_os").open,
                     "Path.open via io.open": io.open, "builtins.open": open}
        self.assertEqual({name: hasattr(hook, boundary.MARK) for name, hook in reachable.items()},
                         {name: True for name in reachable})
        live = MODULE_BOUNDARY[0]
        source = REPO / "campaign_tool" / "records" / "ledger" / "receipt_projection.py"
        outside = fixture_parent().parent / "fa1401-not-admitted" / "data.sqlite"
        decisions = {
            "fixture_write": live.admitted(boundary.resolve(self.root / "new.json", write=True), True),
            "fixture_read": live.admitted(boundary.resolve(self.db, write=False), False),
            "repo_read": live.admitted(boundary.resolve(source, write=False), False),
            "runtime_read": live.admitted(boundary.resolve(os.__file__, write=False), False),
            "repo_write": live.admitted(boundary.resolve(source, write=True), True),
            "outside_read": live.admitted(boundary.resolve(outside, write=False), False),
            "outside_write": live.admitted(boundary.resolve(outside, write=True), True),
            "unresolvable": live.admitted(None, False),
        }
        self.assertEqual(decisions, {"fixture_write": True, "fixture_read": True, "repo_read": True,
                                     "runtime_read": True, "repo_write": False, "outside_read": False,
                                     "outside_write": False, "unresolvable": False})
        # Relative names under a dir_fd (rp.capture, rmtree) are judged at their real directory.
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            self.assertEqual(boundary.resolve("index.jsonl", fd, write=False),
                             os.path.join(os.path.realpath(self.root), "index.jsonl"))
        finally:
            os.close(fd)
        classify = boundary.FileBoundary
        self.assertEqual(classify._connect(":memory:"), [])
        self.assertEqual(classify._connect(self.db.as_uri() + "?mode=ro", uri=True), [(str(self.db), None, False)])
        self.assertEqual(classify._connect(self.db.as_uri() + "?mode=rw", uri=True), [(str(self.db), None, True)])
        self.assertEqual(classify._connect(self.db), [(str(self.db), None, True)])
        self.assertEqual(classify._os_open(self.db, os.O_RDONLY), [(self.db, None, False)])
        self.assertEqual(classify._os_open(self.db, os.O_WRONLY | os.O_CREAT), [(self.db, None, True)])
        self.assertEqual(classify._open(3, "rb"), [])
        # A hook refuses before its callee runs. The callee is an inert recorder, so the
        # refused case touches no file at all; an admitted fixture path reaches it.
        probe = boundary.FileBoundary(repo=REPO, write_roots=(self.root,), hits=[])
        reached = []
        for name, classifier, refused, admitted in (
                ("probe.open", probe._open, (str(outside), "wb"), (str(self.root / "a"), "wb")),
                ("probe.connect", probe._connect, (outside.as_uri(),), (self.db.as_uri() + "?mode=ro",))):
            hook = probe.wrap(name, classifier, lambda *args, **kwargs: reached.append(args))
            with self.subTest(hook=name):
                with self.assertRaisesRegex(PermissionError, boundary.MARK):
                    hook(*refused, uri=True) if name == "probe.connect" else hook(*refused)
                self.assertEqual(reached, [])
                hook(*admitted, uri=True) if name == "probe.connect" else hook(*admitted)
                self.assertEqual(len(reached), 1)
                del reached[:]
        self.assertEqual([hit["hook"] for hit in probe.hits], ["probe.open", "probe.connect"])
        self.assertEqual(SENTINEL_HITS, [])

    # -- A1 ----------------------------------------------------------------------
    def test_index_zero_selects_first_entry(self):
        self.assertEqual(rp.source_valid(*call_args(self.mid, self.body, 0, 0)), 1)
        with store.ledger(self.db) as con:
            rp.install(con)
            selected = con.execute("SELECT rp_source_valid(?,?,?,?,?,?,?)",
                                   call_args(self.mid, self.body, 0, 0)).fetchone()[0]
            self.assertEqual(selected, 1)
            mid = self.insert_manifest(con)
            self.insert_source(con, self.source_row(mid, self.body, 0, 0))
            con.commit()
            self.assertEqual([tuple(row) for row in con.execute("SELECT ordinal,typeof(ordinal) FROM rp_sources")],
                             [(0, "integer")])

    def test_registered_exact_ordinals_select_their_matching_entry(self):
        with store.ledger(self.db) as con:
            rp.install(con)
            for ordinal in range(COUNT):
                for position in range(COUNT):
                    with self.subTest(ordinal=ordinal, entry=position):
                        value = con.execute("SELECT rp_source_valid(?,?,?,?,?,?,?)",
                                            call_args(self.mid, self.body, ordinal, position)).fetchone()[0]
                        self.assertEqual(value, int(ordinal == position))

    def test_registered_negative_ordinals_return_zero(self):
        with store.ledger(self.db) as con:
            rp.install(con)
            for ordinal in range(-1, -COUNT - 1, -1):
                aliased = ordinal % COUNT
                with self.subTest(ordinal=ordinal, aliased=aliased):
                    args = call_args(self.mid, self.body, ordinal, aliased)
                    self.assertEqual(con.execute("SELECT rp_source_valid(?,?,?,?,?,?,?)", args).fetchone()[0], 0)
                    self.assertEqual(rp.source_valid(*args), 0)
                    # The same entry is still admitted at its real position.
                    self.assertEqual(rp.source_valid(*call_args(self.mid, self.body, aliased, aliased)), 1)

    def test_direct_negative_source_insert_raises_integrity_error(self):
        with store.ledger(self.db) as con:
            rp.install(con)
            mid = self.insert_manifest(con)
            for ordinal in (-1, -COUNT):
                with self.subTest(ordinal=ordinal):
                    try:
                        with self.assertRaises(sqlite3.IntegrityError):
                            self.insert_source(con, self.source_row(mid, self.body, ordinal, ordinal % COUNT))
                    finally:
                        con.rollback()
            self.assertEqual(con.execute("SELECT count(*) FROM rp_sources").fetchone()[0], 0)
            for ordinal in range(COUNT):
                self.insert_source(con, self.source_row(mid, self.body, ordinal, ordinal))
            con.commit()
            self.assertEqual([tuple(row) for row in con.execute("SELECT ordinal FROM rp_sources ORDER BY ordinal")],
                             [(n,) for n in range(COUNT)])
            self.assertEqual(rp.verify(con, mid), 0)

    # -- A2 ----------------------------------------------------------------------
    def test_non_exact_integer_ordinals_rejected(self):
        class Ordinal(int):
            pass
        values = (True, False, Ordinal(1), 0.0, 1.0, -1.0, "0", "1", "-1", None, [0], [1], {}, (1,), b"\x01",
                  complex(1, 0))
        for value in values:
            try:
                position = int(value) % COUNT
            except (TypeError, ValueError):
                position = 1
            with self.subTest(value=repr(value)):
                self.assertEqual(rp.source_valid(*call_args(self.mid, self.body, value, position)), 0)
                # Otherwise-identical exact integer is admitted: only the type decided.
                self.assertEqual(rp.source_valid(*call_args(self.mid, self.body, position, position)), 1)

    def test_out_of_range_ordinals_rejected(self):
        for value in (COUNT, COUNT + 1, 2 ** 63 - 1, 2 ** 70, -COUNT - 1, -(2 ** 63)):
            with self.subTest(value=value):
                self.assertEqual(rp.source_valid(*call_args(self.mid, self.body, value, 0)), 0)
                self.assertEqual(rp.source_valid(*call_args(self.mid, self.body, value, COUNT - 1)), 0)

    def test_complete_binding_comparison_preserved(self):
        good = call_args(self.mid, self.body, 1, 1)
        self.assertEqual(rp.source_valid(*good), 1)
        entry = json.loads(self.body)["indexes"][1]
        other = rp.digest(b"fa1401 other manifest")
        mutations = {
            "sid_from_other_ordinal": (rp.identity(self.mid, 0),) + good[1:],
            "sid_from_other_manifest": (rp.identity(other, 1),) + good[1:],
            "path": good[:3] + ("fa1401/elsewhere.jsonl",) + good[4:],
            "mapping_kind": good[:4] + (mapping_of(dict(entry, kind="reviews")),) + good[5:],
            "mapping_fields": good[:4] + (mapping_of(dict(entry, fields={})),) + good[5:],
            "mapping_declarations": good[:4] + (mapping_of(dict(entry, declarations={"role": "role"})),) + good[5:],
            "mapping_artifact_paths": good[:4] + (mapping_of(dict(entry, artifact_paths={"a": "b"})),) + good[5:],
            "expected_hash": good[:5] + (rp.digest(b"fa1401 changed"),) + good[6:],
            "malformed_body": good[:6] + (b"{not json",),
            "indexes_not_list": good[:6] + (rp.canonical({"version": 1, "indexes": {"1": entry}}).encode(),),
            "indexes_missing": good[:6] + (rp.canonical({"version": 1}).encode(),),
        }
        for name, args in mutations.items():
            with self.subTest(mutation=name):
                self.assertEqual(rp.source_valid(*args), 0)

    def test_sql_affinity_cases_judged_as_delivered(self):
        cases = ("1", 1.0, True, "-1", -1.0, " 1", 1.5, "x", "0x1")
        delivered = []

        def probe(*args):
            # Observation only: record what the trigger delivers and admit nothing.
            delivered.append(args[2])
            return 0

        with store.ledger(self.db) as con:
            rp.install(con)
            mid = self.insert_manifest(con)
            for value in cases:
                with self.subTest(value=repr(value)):
                    con.create_function("rp_source_valid", 7, probe, deterministic=True)
                    del delivered[:]
                    first = self.source_row(mid, self.body, 0, 0)
                    try:
                        with self.assertRaises(sqlite3.IntegrityError):
                            self.insert_source(con, first[:2] + (value,) + first[3:])
                    finally:
                        con.rollback()
                    self.assertEqual(len(delivered), 1, delivered)
                    seen = delivered[0]
                    rp.register(con)
                    # Self-consistent for the delivered value: id derived from it, entry it would alias.
                    position = seen % COUNT if type(seen) is int else 1
                    row = (sid_for(mid, seen),) + self.source_row(mid, self.body, position, position)[1:]
                    row = row[:2] + (value,) + row[3:]
                    admitted = type(seen) is int and 0 <= seen < COUNT
                    try:
                        if admitted:
                            self.insert_source(con, row)
                            stored = con.execute("SELECT ordinal,typeof(ordinal) FROM rp_sources WHERE id=?",
                                                 (row[0],)).fetchone()
                            self.assertEqual(tuple(stored), (seen, "integer"))
                        else:
                            with self.assertRaises(sqlite3.IntegrityError):
                                self.insert_source(con, row)
                            self.assertEqual(con.execute("SELECT count(*) FROM rp_sources").fetchone()[0], 0)
                    finally:
                        con.rollback()
                    if value in ("-1", -1.0, 1.5, "x", "0x1"):
                        # Whatever affinity delivers, these are never an admitted position.
                        self.assertFalse(admitted, (value, seen))

    # -- A3 ----------------------------------------------------------------------
    def test_historical_negative_source_refused_not_repaired(self):
        mid = self.historical_capture((0, 1, -1))
        before = self.dump()
        self.assertEqual([row[2] for row in before["rp_sources"]], [0, 1, -1])
        with store.ledger(self.db) as con:
            with self.assertRaisesRegex(ValueError, "source mutation"):
                rp.verify(con, mid)
        (self.root / "manifest.json").write_bytes(self.body)
        with self.assertRaisesRegex(ValueError, "source mutation"):
            rp.project(self.db, self.root, "manifest.json")
        self.assertEqual(self.dump(), before)

    def test_historical_valid_capture_still_verifies(self):
        mid = self.historical_capture((0, 1, 2))
        before = self.dump()
        with store.ledger(self.db) as con:
            self.assertEqual(rp.verify(con, mid), 0)
        self.assertEqual(self.dump(), before)

    def test_projected_manifest_captures_and_replays_identically(self):
        original = hashlib.sha256(b"fa1401 fictional original").hexdigest()
        with store.ledger(self.db) as con:
            con.execute("INSERT INTO originals VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (original, 18, "text/plain", None, "original", "in_scope", None, "captured", None, "{}"))
            con.commit()
        indexes = []
        for position in range(2):
            raw = json.dumps({"original": original, "position": position}).encode() + b"\n"
            (self.root / ("index-%d.jsonl" % position)).write_bytes(raw)
            indexes.append({"path": "index-%d.jsonl" % position, "sha256": rp.digest(raw), "kind": "documents",
                            "fields": {"original_hash": "original"}})
        (self.root / "manifest.json").write_text(json.dumps({"version": 1, "indexes": indexes}))
        counts = store.counts(self.db)
        first = rp.project(self.db, self.root, "manifest.json")
        captured = self.dump()
        second = rp.project(self.db, self.root, "manifest.json")
        self.assertEqual(self.dump(), captured)
        self.assertEqual(store.counts(self.db), counts)
        self.assertEqual([row[2] for row in captured["rp_sources"]], [0, 1])
        self.assertEqual((first["processed"], first["added"], first["status"]), (2, 2, "captured"))
        self.assertEqual((second["processed"], second["added"], second["remaining"]), (2, 0, 0))
        for result in (first, second):
            self.assertEqual((result["stage_promotions"], result["review_accepted"], result["publication_ready"]),
                             (0, False, False))
        self.assertEqual([row[-1] for row in captured["rp_items"]], [row[-1] for row in self.dump()["rp_items"]])


if __name__ == "__main__":
    unittest.main()
