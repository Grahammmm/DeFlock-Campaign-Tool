"""FC4-SOURCE-ORDINAL: a unit provenance ordinal must be an exact integer.

The stored ordinal must equal the unit's one-based position in the current
manifest inventory. Booleans, floats and every other JSON type refuse the whole
snapshot with the existing current_unit_binding_changed reason; stored
provenance is never coerced or repaired.

Fixture: a private synthetic SQLite ledger built here from labelled bytes. It
exercises only the installed resolver read contract. It is not a mailbox,
pipeline, canonical-store, extraction or stage qualification, and no real
record is read. Offline sentinels are installed before any application import.
"""
import contextlib
import hashlib
import http.client
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

SENTINEL_HITS = []
APPLICATION_PRELOADED = sorted(name for name in sys.modules
                               if name.split(".")[0] == "campaign_tool")


def _sentinel(name):
    def refuse(*args, **kwargs):
        SENTINEL_HITS.append(name)
        raise OSError("fc4_offline_sentinel:" + name)
    return refuse


SENTINELS = ((socket, "create_connection"), (socket, "getaddrinfo"),
             (socket.socket, "connect"), (socket.socket, "connect_ex"),
             (http.client.HTTPConnection, "connect"), (urllib.request, "urlopen"))
for _owner, _attribute in SENTINELS:
    setattr(_owner, _attribute, _sentinel(_owner.__name__ + "." + _attribute))

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from campaign_tool.records import extraction_ledger as extraction  # noqa: E402
from campaign_tool.records import extraction_validation  # noqa: E402
from campaign_tool.records.detectors import sources  # noqa: E402
from campaign_tool.records.ledger import store  # noqa: E402
from campaign_tool.records.ledger.migrations import stages_v001  # noqa: E402

ARTIFACT_ENV = "FC4_SOURCE_ORDINAL_ARTIFACT_ROOT"
SUMMARY_NAME = "valid-snapshot-summary.json"
LABEL = b"fc4-source-ordinal synthetic "
SPEC = (("subject", 3), ("support", 2))
MISSING = object()
_saved_popen = None


def artifact_root():
    configured = os.environ.get(ARTIFACT_ENV)
    root = Path(configured) if configured else Path(tempfile.mkdtemp(prefix="fc4-source-ordinal-"))
    root = root.resolve()
    if not root.is_dir() or root == REPO or REPO in root.parents:
        raise RuntimeError("fc4 artifact root must be an existing directory outside the repository")
    return root


def setUpModule():
    global _saved_popen
    _saved_popen = subprocess.Popen
    subprocess.Popen = _sentinel("subprocess.Popen")


def tearDownModule():
    subprocess.Popen = _saved_popen


def labelled(*parts):
    return LABEL + "/".join(parts).encode() + b"\n"


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def encoded(value):
    return extraction.canonical(value).encode()


def write_private(path, raw):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(raw)


class Fixture:
    def __init__(self, base):
        self.base = base
        self.database = base / "ledger" / "canonical.sqlite"
        self.original_root = base / "source-originals"
        self.derived_root = base / "derived"
        self.identity = {}
        self.expected_bindings, self.expected_units, self.expected_originals = [], [], []

    def resolve(self, *, supports=True):
        return sources.resolve_sources(
            database=self.database, original_root=self.original_root,
            derived_root=self.derived_root, subject_sha256=self.identity["subject"],
            support_sha256=(self.identity["support"],) if supports else ())

    def state(self):
        """Every file and directory entry, with bytes hash and inode identity."""
        entries = {}
        for directory, names, files in os.walk(self.base):
            for name in sorted(names + files):
                path = Path(directory, name)
                info = os.lstat(path)
                digest = sha(path.read_bytes()) if path.is_file() else None
                entries[str(path.relative_to(self.base))] = (
                    digest, info.st_mode, info.st_ino, info.st_size, info.st_mtime_ns)
        return entries

    def rows(self):
        """Table dump with the fixture base directory relativized."""
        uri = self.database.as_uri() + "?mode=ro"
        prefix = str(self.base)
        dump = {}
        with contextlib.closing(sqlite3.connect(uri, uri=True)) as con:
            tables = [row[0] for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
            for table in tables:
                rows = con.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
                dump[table] = [tuple(value.replace(prefix, "<base>") if type(value) is str else value
                                     for value in row) for row in rows]
        return dump

    def stored_ordinal(self, role, position):
        identity = self.identity[(role, position)]
        uri = self.database.as_uri() + "?mode=ro"
        with contextlib.closing(sqlite3.connect(uri, uri=True)) as con:
            raw = con.execute("SELECT provenance_json FROM units WHERE id=?", (identity,)).fetchone()[0]
        provenance = extraction.decode(raw)
        return provenance.get("ordinal", MISSING)


def build(overrides=None):
    """Build a private ledger; overrides maps (role, position) to a stored ordinal."""
    overrides = dict(overrides or {})
    fixture = Fixture(Path(tempfile.mkdtemp(prefix="ledger-", dir=artifact_root())))
    for directory in (fixture.original_root, fixture.derived_root):
        directory.mkdir(mode=0o700)
    store.initialize(fixture.database)
    stamp = "2026-01-01T00:00:00+00:00"
    with contextlib.closing(sqlite3.connect(fixture.database)) as con:
        # Synthetic writer only: the stage controller's append guard is a test double here.
        con.create_function("stage_append_allowed", -1, lambda *values: 1)
        con.executescript(stages_v001.SQL)
        extraction.ensure_schema(con)
        for role, count in SPEC:
            raw = labelled("original", role)
            original_path = fixture.original_root / (role + ".bin")
            write_private(original_path, raw)
            subject = sha(raw)
            fixture.identity[role] = subject
            con.execute("INSERT INTO originals(sha256,bytes,mime_detected,first_seen_at,role,scope,"
                        "storage_path,preservation_status,legacy_format,provenance_json) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (subject, len(raw), None, None, "unresolved", "in_scope", str(original_path),
                         "preserved", None, extraction.canonical({"fixture": role})))
            import_id = "fc4-import-" + role
            adapter_receipt = sha(labelled("adapter-receipt", role))
            inventory = []
            for position in range(1, count + 1):
                locator = {"page": 1, "unit": position}
                text = f"synthetic {role} unit {position}"
                payload = {"kind": "paragraph", "locator": locator, "text": text}
                payload_raw = encoded(payload)
                payload_path = fixture.derived_root / f"{role}-{position}.json"
                write_private(payload_path, payload_raw)
                identity = sha(extraction.canonical([import_id, position, sha(payload_raw)]).encode())
                provenance = {"extraction_import_id": import_id, "ordinal": position,
                              "payload_sha256": sha(payload_raw), "receipt_sha256": adapter_receipt}
                ordinal = overrides.pop((role, position), position)
                if ordinal is MISSING:
                    del provenance["ordinal"]
                else:
                    provenance["ordinal"] = ordinal
                con.execute("INSERT INTO units(id,original_sha256,parser,parser_version,locator,unit_type,"
                            "text_sha256,derived_path,status,legacy_ordinal,provenance_json) "
                            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                            (identity, subject, "fc4-synthetic", "1", extraction.canonical(locator),
                             "paragraph", sha(text.encode()), str(payload_path), "current", None,
                             extraction.canonical(provenance)))
                inventory.append(identity)
                fixture.identity[(role, position)] = identity
                fixture.expected_bindings.append({"unit_id": identity, "original_sha256": subject,
                                                  "artifact_sha256": sha(payload_raw), "locator": locator})
                fixture.expected_units.append({"unit_id": identity, "original_sha256": subject,
                                               "payload": payload})
            manifest_json = extraction.canonical({"original_bytes": len(raw), "original_sha256": subject,
                                                  "receipt_sha256": adapter_receipt, "unit_ids": inventory})
            manifest_sha = sha(manifest_json.encode())
            content_raw = encoded({"schema": "canonical-extraction-content-v1", "original_sha256": subject,
                                   "installed_code": extraction_validation.code_identity(),
                                   "import_id": import_id, "manifest_sha256": manifest_sha,
                                   "unit_count": count, "source_receipt_sha256": adapter_receipt})
            preserve_raw = encoded({"stage": "preserve", "subject_sha256": subject, "verdict": "pass"})
            extract_raw = encoded({"stage": "extract", "subject_sha256": subject, "verdict": "pass",
                                   "content_sha256": sha(content_raw),
                                   "input_hashes": {"preserve": sha(preserve_raw)}})
            for artifact in (content_raw, preserve_raw, extract_raw):
                con.execute("INSERT INTO stage_artifacts(sha256,payload) VALUES(?,?)", (sha(artifact), artifact))
            con.execute("INSERT INTO stage_content VALUES(?,?,?,?,?,?,?,?)",
                        (subject, "extract", 1, sha(content_raw), "fc4-synthetic", "A", "fc4-run", stamp))
            con.execute("INSERT INTO extraction_adapter_imports VALUES(?,?,?,?,?,?,?)",
                        (import_id, subject, adapter_receipt, manifest_sha, manifest_json, "fc4-run", stamp))
            con.execute("INSERT INTO extraction_adapter_current VALUES(?,?)", (subject, import_id))
            for stage, receipt in (("preserve", preserve_raw), ("extract", extract_raw)):
                con.execute("INSERT INTO stage_state(original_sha256,stage,status,receipt_sha256,owner,"
                            "updated_at,run_id,reason) VALUES(?,?,?,?,?,?,?,?)",
                            (subject, stage, "pending", None, "fc4-synthetic", stamp, "fc4-run", None))
                con.execute("UPDATE stage_state SET status='done', receipt_sha256=? "
                            "WHERE original_sha256=? AND stage=?", (sha(receipt), subject, stage))
            fixture.expected_originals.append({
                "original_sha256": subject, "role": "primary" if role == "subject" else "support",
                "bytes": len(raw), "resolution_sha256": None,
                "preserve_receipt_sha256": sha(preserve_raw), "extract_receipt_sha256": sha(extract_raw),
                "manifest_sha256": manifest_sha, "import_id": import_id, "unit_count": count})
        con.commit()
        # A quiet rollback-journal database: a read-only resolve must create no sidecar.
        con.execute("PRAGMA journal_mode=DELETE")
    if overrides:
        raise RuntimeError("fixture override names no synthetic unit")
    return fixture


# Declared malformed classes; each value replaces only the ordinal of one unit.
BOOLEANS = (True, False)
FLOATS = (1.0, 2.0, 3.0, 1.5, -1.0)
NON_NUMBERS = (MISSING, None, "1", "", [1], [], {"ordinal": 1}, {})
INTEGER_POSITIONS = (("subject", 1), ("subject", 3), ("support", 2))


class SourceOrdinalTest(unittest.TestCase):
    def tearDown(self):
        self.assertEqual(SENTINEL_HITS, [], "offline sentinel reached")

    def assertSnapshot(self, snapshot, fixture):
        self.assertEqual(snapshot.subject_sha256, fixture.identity["subject"])
        self.assertEqual(snapshot.bindings, fixture.expected_bindings)
        self.assertEqual(snapshot.units, fixture.expected_units)
        self.assertEqual(snapshot.originals, fixture.expected_originals)

    def assertRefused(self, fixture, *, supports=True):
        before = fixture.state()
        with self.assertRaises(sources.ResolutionError) as caught:
            fixture.resolve(supports=supports)
        self.assertEqual(caught.exception.args, ("current_unit_binding_changed",))
        self.assertEqual(fixture.state(), before)

    def check_malformed(self, values, positions):
        for role, position in positions:
            for value in values:
                stored = value(position) if callable(value) else value
                with self.subTest(role=role, position=position, value=repr(stored)):
                    fixture = build({(role, position): stored})
                    kept = fixture.stored_ordinal(role, position)
                    if stored is MISSING:
                        self.assertIs(kept, MISSING)
                    else:
                        self.assertEqual((type(kept), kept), (type(stored), stored))
                    self.assertRefused(fixture)

    def test_valid_integer_first_and_later_positions(self):
        fixture = build()
        for role, count in SPEC:
            for position in range(1, count + 1):
                kept = fixture.stored_ordinal(role, position)
                self.assertEqual((type(kept), kept), (int, position))
        snapshot = fixture.resolve()
        self.assertSnapshot(snapshot, fixture)
        self.assertEqual([row["unit_count"] for row in snapshot.originals], [3, 2])
        # An explicit exact integer at a first and a later position is the valid control.
        explicit = build({("subject", 1): 1, ("subject", 3): 3, ("support", 2): 2})
        self.assertEqual(explicit.resolve().revision_sha256, snapshot.revision_sha256)

    def test_bool_not_integer(self):
        self.check_malformed(BOOLEANS, INTEGER_POSITIONS)
        # True also equals the support inventory's first position.
        self.check_malformed((True,), (("support", 1),))

    def test_float_not_integer(self):
        self.check_malformed(FLOATS, INTEGER_POSITIONS)
        # A float equal to its own position at each first and later position.
        for role, count in SPEC:
            self.check_malformed((lambda position: float(position),),
                                 [(role, position) for position in range(1, count + 1)])

    def test_missing_null_string_container(self):
        self.check_malformed(NON_NUMBERS, INTEGER_POSITIONS)
        self.check_malformed((str, lambda position: [position], lambda position: {"value": position}),
                             INTEGER_POSITIONS)

    def test_zero_negative_wrong_position(self):
        self.check_malformed((0, -1, lambda position: -position, lambda position: position + 1,
                              lambda position: position - 1 or 2, 2 ** 63), INTEGER_POSITIONS)

    def test_otherwise_valid_binding_isolation(self):
        valid = build()
        valid_rows, valid_state = valid.rows(), valid.state()
        valid_revision = valid.resolve().revision_sha256
        for stored in (True, 1.0):
            with self.subTest(value=repr(stored)):
                variant = build({("subject", 1): stored})
                rows = variant.rows()
                self.assertEqual(set(rows), set(valid_rows))
                for table in valid_rows:
                    if table != "units":
                        self.assertEqual(rows[table], valid_rows[table], table)
                self.assertEqual(len(rows["units"]), len(valid_rows["units"]))
                changed = [(left, right) for left, right in zip(valid_rows["units"], rows["units"])
                           if left != right]
                self.assertEqual(len(changed), 1)
                left, right = changed[0]
                provenance_column = len(left) - 1
                self.assertEqual(left[:provenance_column], right[:provenance_column])
                left_provenance = extraction.decode(left[provenance_column])
                right_provenance = extraction.decode(right[provenance_column])
                self.assertEqual(right_provenance.pop("ordinal"), stored)
                self.assertEqual(left_provenance.pop("ordinal"), 1)
                self.assertEqual(left_provenance, right_provenance)
                # Original and derived bytes are identical; the ledger differs only as dumped above.
                state = variant.state()
                self.assertEqual({name: entry[0] for name, entry in state.items()
                                  if not name.startswith("ledger")},
                                 {name: entry[0] for name, entry in valid_state.items()
                                  if not name.startswith("ledger")})
                self.assertEqual(set(state), set(valid_state))
                self.assertRefused(variant)
                # Restoring only the ordinal to the exact integer restores the valid snapshot.
                restored = build({("subject", 1): 1})
                self.assertEqual(restored.rows(), valid_rows)
                self.assertEqual(restored.resolve().revision_sha256, valid_revision)

    def test_whole_snapshot_refusal(self):
        support_only = build({("support", 2): 2.0})
        self.assertRefused(support_only)
        # The same primary inventory remains valid when the bad support is not declared.
        primary = support_only.resolve(supports=False)
        self.assertEqual(primary.bindings, support_only.expected_bindings[:3])
        later = build({("subject", 3): 3.0})
        self.assertRefused(later)
        self.assertRefused(later, supports=False)
        first = build({("subject", 1): True})
        self.assertRefused(first)
        self.assertRefused(first, supports=False)

    def test_valid_revision_unchanged(self):
        fixture = build()
        first = fixture.resolve()
        self.assertSnapshot(first, fixture)
        summary = {"schema": "fc4-source-ordinal-valid-snapshot-v1",
                   "revision_sha256": first.revision_sha256,
                   "bindings_sha256": sha(first.bindings_json.encode()),
                   "units_sha256": sha(first.units_json.encode()),
                   "originals_sha256": sha(first.originals_json.encode()),
                   "join_revision_sha256": first.joins.revision_sha256,
                   "resolver_code_sha256": store.file_hash(sources.__file__),
                   "resolver_path": str(Path(sources.__file__).resolve())}
        with open(artifact_root() / SUMMARY_NAME, "x", encoding="utf-8") as stream:
            json.dump(summary, stream, indent=2, sort_keys=True)
        invalid = build({("subject", 1): True})
        self.assertRefused(invalid)
        again = fixture.resolve()
        independent = build().resolve()
        for snapshot in (again, independent):
            self.assertEqual((snapshot.bindings_json, snapshot.units_json, snapshot.originals_json,
                              snapshot.joins, snapshot.revision_sha256),
                             (first.bindings_json, first.units_json, first.originals_json,
                              first.joins, first.revision_sha256))

    def test_read_only_database_and_file_preservation(self):
        valid = build()
        for fixture, expect_valid in ((valid, True), (build({("subject", 1): True}), False),
                                      (build({("support", 2): 2.0}), False)):
            with self.subTest(valid=expect_valid):
                before = fixture.state()
                self.assertFalse([name for name in before if name.endswith(("-wal", "-shm", "-journal"))])
                for _ in range(2):
                    if expect_valid:
                        self.assertSnapshot(fixture.resolve(), fixture)
                    else:
                        with self.assertRaises(sources.ResolutionError):
                            fixture.resolve()
                    self.assertEqual(fixture.state(), before)


if __name__ == "__main__":
    unittest.main()
