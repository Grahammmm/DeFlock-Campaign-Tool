"""FC2-RETAINED-SCOPE: malformed retained-unit scope IDs get a typed refusal.

Every identity below is derived from labelled synthetic bytes. No agency, record,
person or source exists behind any fixture. The tests never open a default or
live ledger: the scope tests use an in-memory recording SQL stub and the public
reader tests build a private synthetic ledger under a fresh temporary directory
(the acceptance runner points TMPDIR at its per-run output directory).
"""
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import unittest
from unittest import mock

from campaign_tool.records import retained_units

INVALID = "import_scope_invalid"


def hexid(label):
    """Deterministic 64-char lowercase identity from a labelled fixture string."""
    return hashlib.sha256(("fc2-retained-scope fixture:" + label).encode()).hexdigest()


def canon(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def sha_text(text):
    return hashlib.sha256(text.encode()).hexdigest()


class HexSubclass(str):
    """A str subclass carrying a valid-looking identity; exact type must be str."""


class ListSubclass(list):
    pass


# --------------------------------------------------------------- SQL stub


class _Rows:
    def __init__(self, rows):
        self._rows = list(rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class RecordingLedger:
    """Answers only the three scope queries and records every execute() call."""

    def __init__(self, specs=(), current_override=None):
        self.calls = []
        self.current = {}
        self.imports = {}
        self.units = {}
        for spec in specs:
            self.current[spec["subject"]] = spec["import_id"]
            self.imports[spec["import_id"]] = {"original_sha256": spec["subject"],
                                               "manifest_sha256": spec["manifest_sha256"],
                                               "manifest_json": spec["manifest_json"]}
            self.units[(spec["subject"], spec["import_id"])] = [{"id": u} for u in spec["stored_unit_ids"]]
        self.current.update(current_override or {})

    def execute(self, sql, params=()):
        params = tuple(params)
        self.calls.append((" ".join(sql.split()), params))
        text = sql.lower()
        if "from extraction_adapter_current" in text:
            imp = self.current.get(params[0])
            return _Rows([] if imp is None else [(imp,)])
        if "from extraction_adapter_imports" in text:
            row = self.imports.get(params[0])
            return _Rows([] if row is None else [row])
        if "from units" in text:
            subject, imp, limit = params
            return _Rows(self.units.get((subject, imp), [])[:limit])
        raise AssertionError("unexpected SQL in scope stub: " + sql)

    def params_seen(self):
        return [value for _sql, params in self.calls for value in params]


def stub_spec(label, count, manifest_ids=None, stored_ids=None):
    """One declared import plus the stub rows that make it fully consistent."""
    subject = hexid(label + ":original")
    imp = hexid(label + ":import")
    ids = [hexid("{}:unit:{}".format(label, i)) for i in range(count)]
    manifest = {"unit_ids": list(ids if manifest_ids is None else manifest_ids)}
    manifest_json = canon(manifest)
    spec = {"subject": subject, "import_id": imp, "manifest_json": manifest_json,
            "manifest_sha256": sha_text(manifest_json), "unit_ids": ids,
            "stored_unit_ids": list(ids if stored_ids is None else stored_ids)}
    declared = {"original_sha256": subject, "import_id": imp,
                "manifest_sha256": spec["manifest_sha256"], "unit_ids": list(ids)}
    return declared, spec


def declared_with(unit_ids, label="malformed"):
    declared, _spec = stub_spec(label, 1)
    declared["unit_ids"] = unit_ids
    return declared


# Members that would make set() raise TypeError on the unchanged source.
UNHASHABLE_MEMBERS = (
    ("empty_dict", lambda: {}),
    ("dict_with_valid_id", lambda: {"unit_id": hexid("u0")}),
    ("empty_list", lambda: []),
    ("list_with_valid_id", lambda: [hexid("u0")]),
    ("empty_set", lambda: set()),
    ("set_with_valid_id", lambda: {hexid("u0")}),
    ("bytearray_of_valid_id", lambda: bytearray(hexid("u0").encode())),
    ("tuple_holding_list", lambda: (hexid("u0"), [])),
    ("nested_dict_in_list", lambda: [{"unit_id": [{}]}]),
)
# Hashable but not exact str identities.
HASHABLE_NON_STRINGS = (
    ("none", lambda: None),
    ("true", lambda: True),
    ("false", lambda: False),
    ("zero", lambda: 0),
    ("one", lambda: 1),
    ("negative_int", lambda: -1),
    ("float", lambda: 1.5),
    ("nan", lambda: math.nan),
    ("infinity", lambda: math.inf),
    ("bytes_of_valid_id", lambda: hexid("u0").encode()),
    ("frozenset_of_valid_id", lambda: frozenset([hexid("u0")])),
    ("tuple_of_valid_id", lambda: (hexid("u0"),)),
    ("str_subclass_valid_id", lambda: HexSubclass(hexid("u0"))),
)
# Exact str but not a lowercase 64-hex identity.
WRONG_HASH_STRINGS = (
    ("empty", lambda: ""),
    ("uppercase", lambda: hexid("u0").upper()),
    ("short_63", lambda: hexid("u0")[:63]),
    ("long_65", lambda: hexid("u0") + "0"),
    ("trailing_newline", lambda: hexid("u0") + "\n"),
    ("leading_space", lambda: " " + hexid("u0")[1:]),
    ("non_hex_letter", lambda: "g" + hexid("u0")[1:]),
    ("fullwidth_digits", lambda: "\uff10" * 64),
)


class ScopeMemberTypeTests(unittest.TestCase):
    """Real retained_units._scope against the recording SQL stub."""

    def assert_refused_before_sql(self, unit_ids, code=INVALID):
        ledger = RecordingLedger()
        with self.assertRaises(retained_units.RetainedUnitError) as caught:
            retained_units._scope(ledger, [declared_with(unit_ids)])
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(str(caught.exception), code)
        self.assertEqual(ledger.calls, [])

    def test_dict_member_first_entry_rejected_before_sql(self):
        for unit_ids in ([{}], [{"unit_id": hexid("u0")}], [hexid("u0"), {}], [hexid("u0"), {"k": "v"}, hexid("u1")]):
            with self.subTest(unit_ids=repr(unit_ids)[:60]):
                self.assert_refused_before_sql(unit_ids)

    def test_list_member_first_entry_rejected_before_sql(self):
        for unit_ids in ([[]], [[hexid("u0")]], [hexid("u0"), []], [hexid("u0"), [hexid("u1")]]):
            with self.subTest(unit_ids=repr(unit_ids)[:60]):
                self.assert_refused_before_sql(unit_ids)

    def test_every_unhashable_member_rejected_in_any_position(self):
        for name, make in UNHASHABLE_MEMBERS:
            for position in ("only", "first", "last"):
                ids = {"only": [make()], "first": [make(), hexid("u1")],
                       "last": [hexid("u1"), hexid("u2"), make()]}[position]
                with self.subTest(member=name, position=position):
                    self.assert_refused_before_sql(ids)

    def test_hashable_non_string_members_rejected(self):
        for name, make in HASHABLE_NON_STRINGS:
            for ids in ([make()], [hexid("u1"), make()]):
                with self.subTest(member=name, length=len(ids)):
                    self.assert_refused_before_sql(ids)

    def test_wrong_hash_strings_rejected(self):
        for name, make in WRONG_HASH_STRINGS:
            for ids in ([make()], [hexid("u1"), make()]):
                with self.subTest(member=name, length=len(ids)):
                    self.assert_refused_before_sql(ids)

    def test_duplicates_rejected(self):
        a, b = hexid("dup:a"), hexid("dup:b")
        for ids in ([a, a], [a, b, a], [b, a, b, b]):
            with self.subTest(ids=len(ids)):
                self.assert_refused_before_sql(ids)

    def test_duplicate_alongside_malformed_member_is_typed(self):
        a = hexid("dup:a")
        for ids in ([a, a, {}], [{}, a, a], [a, [], a]):
            with self.subTest(ids=repr(ids)[:60]):
                self.assert_refused_before_sql(ids)

    def test_empty_list_and_wrong_containers_rejected(self):
        a = hexid("container:a")
        for value in ([], (), (a,), {a}, frozenset([a]), {a: 1}, a, None, 0, ListSubclass([a])):
            with self.subTest(container=type(value).__name__):
                self.assert_refused_before_sql(value)

    def test_per_import_bound_over_limit_rejected_before_sql(self):
        over = [hexid("bound:{}".format(i)) for i in range(retained_units.MAX_UNITS + 1)]
        self.assert_refused_before_sql(over)
        self.assert_refused_before_sql(over[:-1] + [{}])

    def test_per_import_bound_exact_limit_accepted(self):
        declared, spec = stub_spec("exact-bound", retained_units.MAX_UNITS)
        ledger = RecordingLedger([spec])
        result = retained_units._scope(ledger, [declared])
        self.assertEqual(len(result), retained_units.MAX_UNITS)
        self.assertEqual([r[3]["id"] for r in result], declared["unit_ids"])

    def test_global_bound_edges(self):
        first, first_spec = stub_spec("global:first", retained_units.MAX_UNITS - 1)
        at, at_spec = stub_spec("global:at", 1)
        result = retained_units._scope(RecordingLedger([first_spec, at_spec]), [first, at])
        self.assertEqual(len(result), retained_units.MAX_UNITS)
        over, over_spec = stub_spec("global:over", 2)
        ledger = RecordingLedger([first_spec, over_spec])
        with self.assertRaises(retained_units.RetainedUnitError) as caught:
            retained_units._scope(ledger, [first, over])
        self.assertEqual(caught.exception.code, "unit_count_limit")
        self.assertEqual(len(ledger.calls), 3)
        self.assertNotIn(over["original_sha256"], ledger.params_seen())

    def test_malformed_later_import_rejected_before_its_sql(self):
        good, good_spec = stub_spec("later:good", 2)
        bad, bad_spec = stub_spec("later:bad", 2)
        for member in ({}, [], None, 7):
            with self.subTest(member=type(member).__name__):
                ledger = RecordingLedger([good_spec, bad_spec])
                declared = dict(bad, unit_ids=[bad["unit_ids"][0], member])
                with self.assertRaises(retained_units.RetainedUnitError) as caught:
                    retained_units._scope(ledger, [good, declared])
                self.assertEqual(caught.exception.code, INVALID)
                self.assertEqual(len(ledger.calls), 3)
                self.assertNotIn(bad["original_sha256"], ledger.params_seen())
                self.assertNotIn(bad["import_id"], ledger.params_seen())

    def test_valid_scope_preserves_order_and_rows(self):
        one, one_spec = stub_spec("valid:one", 3)
        two, two_spec = stub_spec("valid:two", 2)
        ledger = RecordingLedger([one_spec, two_spec])
        snapshot = json.dumps([one, two], sort_keys=True)
        result = retained_units._scope(ledger, [one, two])
        self.assertEqual(json.dumps([one, two], sort_keys=True), snapshot)
        self.assertEqual([(r[0], r[1], r[3]["id"]) for r in result],
                         [(one["original_sha256"], one["import_id"], u) for u in one["unit_ids"]]
                         + [(two["original_sha256"], two["import_id"], u) for u in two["unit_ids"]])
        self.assertEqual([r[2]["unit_ids"] for r in result], [one["unit_ids"]] * 3 + [two["unit_ids"]] * 2)
        self.assertEqual([params[0] for _sql, params in ledger.calls],
                         [one["original_sha256"], one["import_id"], one["original_sha256"],
                          two["original_sha256"], two["import_id"], two["original_sha256"]])

    def test_existing_codes_preserved(self):
        declared, spec = stub_spec("codes", 3)
        stale = RecordingLedger([spec], current_override={declared["original_sha256"]: hexid("codes:newer-import")})
        cases = [
            ("current_import_mismatch", stale, [declared]),
            ("manifest_membership_mismatch", RecordingLedger([spec]),
             [dict(declared, unit_ids=list(reversed(declared["unit_ids"])))]),
            ("import_binding_mismatch", RecordingLedger([spec]),
             [dict(declared, manifest_sha256=hexid("codes:other-manifest"))]),
        ]
        _d, short_spec = stub_spec("codes", 3, stored_ids=declared["unit_ids"][:2])
        cases.append(("current_unit_membership_mismatch", RecordingLedger([short_spec]), [declared]))
        for code, ledger, imports in cases:
            with self.subTest(code=code):
                with self.assertRaises(retained_units.RetainedUnitError) as caught:
                    retained_units._scope(ledger, imports)
                self.assertEqual(caught.exception.code, code)


# ------------------------------------------------------ synthetic ledger


SCHEMA = """
CREATE TABLE extraction_adapter_imports(id TEXT PRIMARY KEY, original_sha256 TEXT NOT NULL,
  manifest_sha256 TEXT NOT NULL, manifest_json TEXT NOT NULL);
CREATE TABLE extraction_adapter_current(original_sha256 TEXT PRIMARY KEY, import_id TEXT NOT NULL);
CREATE TABLE units(id TEXT PRIMARY KEY, original_sha256 TEXT NOT NULL, legacy_ordinal INTEGER NOT NULL,
  unit_type TEXT NOT NULL, locator TEXT NOT NULL, text_sha256 TEXT NOT NULL, status TEXT NOT NULL,
  derived_path TEXT NOT NULL, provenance_json TEXT NOT NULL);
"""


def _private_dir(path):
    path.mkdir(mode=0o700)
    os.chmod(path, 0o700)
    return path


def build_ledger(base, layout, drop_last_unit_of=None):
    """Private synthetic ledger + CAS. ``layout`` is [(label, unit_count), ...]."""
    root = _private_dir(base / "evidence")
    cas = _private_dir(root / "cas")
    db = base / "ledger.sqlite"
    c = sqlite3.connect(str(db))
    c.executescript(SCHEMA)
    declared = []
    for label, count in layout:
        subject = hexid(label + ":original")
        imp = hexid(label + ":import")
        derived_sha = hexid(label + ":derived-units")
        unit_rows = []
        for ordinal in range(1, count + 1):
            locator = {"page": ordinal}
            text = "synthetic {} unit {}".format(label, ordinal)
            raw = canon({"data": {}, "locator": locator, "text": text}).encode()
            digest = hashlib.sha256(raw).hexdigest()
            shard = cas / digest[:2]
            if not shard.exists():
                _private_dir(shard)
            artifact = shard / digest
            artifact.write_bytes(raw)
            os.chmod(artifact, 0o600)
            uid = hashlib.sha256(canon([imp, ordinal, digest]).encode()).hexdigest()
            provenance = {"extraction_import_id": imp, "ordinal": ordinal, "payload_sha256": digest,
                          "source_units_sha256": derived_sha}
            unit_rows.append((uid, subject, ordinal, "text", canon(locator), sha_text(text),
                              "candidate_extracted", str(artifact), canon(provenance)))
        ids = [r[0] for r in unit_rows]
        manifest_json = canon({"artifacts": {"derived_units": {"sha256": derived_sha}}, "unit_ids": ids})
        msha = sha_text(manifest_json)
        c.execute("insert into extraction_adapter_imports values(?,?,?,?)", (imp, subject, msha, manifest_json))
        c.execute("insert into extraction_adapter_current values(?,?)", (subject, imp))
        stored = unit_rows[:-1] if label == drop_last_unit_of else unit_rows
        c.executemany("insert into units values(?,?,?,?,?,?,?,?,?)", stored)
        declared.append({"original_sha256": subject, "import_id": imp, "manifest_sha256": msha, "unit_ids": ids})
    c.commit()
    snapshot = retained_units.ledger_snapshot(c)
    c.close()
    os.chmod(db, 0o600)
    return db, root, snapshot, declared


def tree_state(base):
    state = {}
    for path in sorted(base.rglob("*")):
        info = path.lstat()
        state[str(path.relative_to(base))] = (stat.S_IFMT(info.st_mode), stat.S_IMODE(info.st_mode), info.st_ino,
                                              hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None)
    return state


class ReadCurrentUnitsTests(unittest.TestCase):
    """Public read_current_units over a private synthetic ledger (never a default path)."""

    def setUp(self):
        holder = tempfile.TemporaryDirectory(prefix="fc2-retained-scope-",
                                             dir=os.path.realpath(tempfile.gettempdir()))
        self.addCleanup(holder.cleanup)
        self.base = Path(holder.name)
        os.chmod(self.base, 0o700)
        self.layout = [("ledger:first", 3), ("ledger:second", 2)]
        self.db, self.root, self.snapshot, self.declared = build_ledger(self.base, self.layout)

    def read(self, imports, db=None, snapshot=None):
        return retained_units.read_current_units(str(db or self.db), str(self.root), snapshot or self.snapshot, imports)

    def observe(self, imports):
        """Run the public API while recording SQL issued after connect, payload reads and the connection."""
        seen = {"sql": [], "reads": [], "connections": []}
        real_connect, real_read = retained_units._connect, retained_units._read

        def connect(database, expected):
            c, p, did = real_connect(database, expected)
            c.set_trace_callback(seen["sql"].append)
            seen["connections"].append(c)
            return c, p, did

        def read(path, limit):
            seen["reads"].append(str(path))
            return real_read(path, limit)

        with mock.patch.object(retained_units, "_connect", connect), mock.patch.object(retained_units, "_read", read):
            try:
                outcome = self.read(imports)
            except retained_units.RetainedUnitError as error:
                outcome = error
        return outcome, seen

    def assert_closed(self, connection):
        with self.assertRaises(sqlite3.ProgrammingError):
            connection.execute("select 1")

    def assert_typed_refusal_before_scope(self, member):
        before = tree_state(self.base)
        first = dict(self.declared[0], unit_ids=[member])
        outcome, seen = self.observe([first, self.declared[1]])
        self.assertIsInstance(outcome, retained_units.RetainedUnitError)
        self.assertEqual(outcome.code, INVALID)
        self.assertEqual(len(seen["connections"]), 1)
        self.assert_closed(seen["connections"][0])
        self.assertEqual([s for s in seen["sql"] if "extraction_adapter" in s or "from units" in s.lower()], [])
        self.assertEqual(seen["reads"], [])
        self.assertEqual(tree_state(self.base), before)

    def test_valid_current_import_accepted(self):
        before = tree_state(self.base)
        outcome, seen = self.observe(self.declared)
        self.assertIsInstance(outcome, dict, repr(outcome))
        expected_ids = self.declared[0]["unit_ids"] + self.declared[1]["unit_ids"]
        self.assertEqual(outcome["snapshot_sha256"], self.snapshot)
        self.assertEqual(outcome["unit_count"], 5)
        self.assertEqual([u["unit_id"] for u in outcome["units"]], expected_ids)
        self.assertEqual([(u["import_id"], u["ordinal"]) for u in outcome["units"]],
                         [(self.declared[0]["import_id"], n) for n in (1, 2, 3)]
                         + [(self.declared[1]["import_id"], n) for n in (1, 2)])
        self.assertEqual(outcome["artifact_bytes"], sum(len(Path(p).read_bytes()) for p in seen["reads"]))
        self.assertEqual(len(seen["reads"]), 5)
        self.assert_closed(seen["connections"][0])
        self.assertEqual(tree_state(self.base), before)

    def test_dict_member_rejected_before_scope_sql_and_payload_read(self):
        self.assert_typed_refusal_before_scope({"unit_id": self.declared[0]["unit_ids"][0]})

    def test_list_member_rejected_before_scope_sql_and_payload_read(self):
        self.assert_typed_refusal_before_scope([self.declared[0]["unit_ids"][0]])

    def test_other_malformed_members_rejected_before_scope_sql(self):
        valid = self.declared[0]["unit_ids"][0]
        for member in (set(), None, True, 1, 1.5, valid.upper(), HexSubclass(valid)):
            with self.subTest(member=type(member).__name__):
                self.assert_typed_refusal_before_scope(member)

    def test_duplicate_ids_rejected_before_scope_sql(self):
        ids = self.declared[0]["unit_ids"]
        outcome, seen = self.observe([dict(self.declared[0], unit_ids=[ids[0], ids[1], ids[0]])])
        self.assertEqual(getattr(outcome, "code", None), INVALID)
        self.assertEqual([s for s in seen["sql"] if "extraction_adapter" in s], [])
        self.assertEqual(seen["reads"], [])
        self.assert_closed(seen["connections"][0])

    def test_stale_current_pointer_code_preserved(self):
        stale = dict(self.declared[0], import_id=hexid("ledger:first:superseded-import"))
        outcome, seen = self.observe([stale])
        self.assertEqual(getattr(outcome, "code", None), "current_import_mismatch")
        self.assertEqual(seen["reads"], [])
        self.assert_closed(seen["connections"][0])

    def test_manifest_order_mismatch_code_preserved(self):
        reordered = dict(self.declared[0], unit_ids=list(reversed(self.declared[0]["unit_ids"])))
        outcome, seen = self.observe([reordered])
        self.assertEqual(getattr(outcome, "code", None), "manifest_membership_mismatch")
        self.assertEqual(seen["reads"], [])

    def test_missing_unit_row_code_preserved(self):
        other = self.base / "short"
        _private_dir(other)
        db, root, snapshot, declared = build_ledger(other, self.layout, drop_last_unit_of="ledger:second")
        with self.assertRaises(retained_units.RetainedUnitError) as caught:
            retained_units.read_current_units(str(db), str(root), snapshot, declared)
        self.assertEqual(caught.exception.code, "current_unit_membership_mismatch")


if __name__ == "__main__":
    unittest.main()
