"""FC7-CATALOG-LEASE-CLOCK: a malformed stored catalog lease expiry is a blocked gap.

CatalogStage.process reads work_leases.expires_at for the catalog item at its
existing lease decision. The stored value must be a timezone-aware RFC3339-style
timestamp: legal calendar fields, an explicit Z or signed HH:MM offset, and
optional fractional seconds. Anything else returns status blocked with reason
catalog_lease_clock_invalid before preserve_card, set_content, claim or promote.
Stored leases are never rewritten or repaired.

Fixture: the shipped synthetic catalog-stage fixture (setUp and helpers of
tests.records.test_catalog_stage.CatalogStageTests) with its test-only
StageRunner, one fresh SQLite ledger per scenario under the temporary
directory, and a fixed UTC decision clock. No real record, mailbox, network or
model is used. Offline and database-path sentinels are installed before any
application import and while this module's tests execute.
"""
import contextlib
from datetime import datetime, timedelta, timezone
import http.client
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import urllib.parse
import urllib.request

SENTINEL_HITS = []
DATABASE_HITS = []
DATABASE_PATHS = []
APPLICATION_PRELOADED = sorted(name for name in sys.modules if name.split(".")[0] == "campaign_tool")
REPO = Path(__file__).resolve().parents[2]
ARTIFACT_ENV = "FC7_LEASE_CLOCK_ARTIFACT_ROOT"
_ABSENT = object()
_REAL_CONNECT = sqlite3.connect


def _sentinel(name):
    def refuse(*args, **kwargs):
        SENTINEL_HITS.append(name)
        raise OSError("fc7_offline_sentinel:" + name)
    refuse.fc7_offline_sentinel = name
    return refuse


def allowed_database_roots():
    """Synthetic ledgers may live only under the temporary root (and declared artifact root)."""
    roots = [Path(tempfile.gettempdir()).resolve()]
    if os.environ.get(ARTIFACT_ENV):
        roots.append(Path(os.environ[ARTIFACT_ENV]).resolve())
    return [root for root in roots if root != REPO and REPO not in root.parents]


def _database_path(database, uri):
    if not isinstance(database, (str, os.PathLike)):
        return None
    text = os.fspath(database)
    if isinstance(text, bytes) or text == ":memory:":
        return text if text == ":memory:" else None
    if uri or text.startswith("file:"):
        return urllib.parse.unquote(urllib.parse.urlsplit(text).path) or None
    return text


def guarded_connect(database, *args, **kwargs):
    path = _database_path(database, kwargs.get("uri", False))
    if path != ":memory:":
        resolved = Path(os.path.abspath(path)).resolve() if path else None
        if resolved is None or not any(resolved == root or root in resolved.parents
                                       for root in allowed_database_roots()):
            DATABASE_HITS.append(str(database))
            raise PermissionError("fc7_database_sentinel:" + str(database))
        DATABASE_PATHS.append(str(resolved))
    return _REAL_CONNECT(database, *args, **kwargs)


guarded_connect.fc7_offline_sentinel = "sqlite3.connect"
NETWORK_TARGETS = ((socket, "create_connection"), (socket, "getaddrinfo"),
                   (socket.socket, "connect"), (socket.socket, "connect_ex"),
                   (http.client.HTTPConnection, "connect"), (urllib.request, "urlopen"))
EXECUTION_TARGETS = NETWORK_TARGETS + ((subprocess, "Popen"),)


def _install(saved, targets, database=True):
    for owner, attribute in targets:
        saved.append((owner, attribute, vars(owner).get(attribute, _ABSENT)))
        setattr(owner, attribute, _sentinel(owner.__name__ + "." + attribute))
    if database:
        saved.append((sqlite3, "connect", vars(sqlite3).get("connect", _ABSENT)))
        sqlite3.connect = guarded_connect


def _restore(saved):
    failed = []
    while saved:
        owner, attribute, value = saved.pop()
        try:
            if value is _ABSENT:
                if attribute in vars(owner):
                    delattr(owner, attribute)
            else:
                setattr(owner, attribute, value)
        except (AttributeError, TypeError) as caught:
            failed.append(owner.__name__ + "." + attribute + ": " + str(caught))
    if failed:
        raise RuntimeError("fc7 sentinel restore failed: " + "; ".join(failed))


def guards_active():
    names = [owner.__name__ + "." + attribute for owner, attribute in EXECUTION_TARGETS
             if hasattr(getattr(owner, attribute), "fc7_offline_sentinel")]
    if hasattr(sqlite3.connect, "fc7_offline_sentinel"):
        names.append("sqlite3.connect")
    return sorted(names)


_import_guard = []
_install(_import_guard, NETWORK_TARGETS)
try:
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    from campaign_tool.records import catalog_stage
    from tests.records import test_catalog_stage as shipped
    from campaign_tool.records.ledger import store, stages
finally:
    _restore(_import_guard)

BLOCKED = {"status": "blocked", "reason": "catalog_lease_clock_invalid", "promoted": False,
           "canonical_stage_changed": False}
FOREIGN = {"status": "blocked", "reason": "catalog_lease_owned_elsewhere", "promoted": False,
           "canonical_stage_changed": False}
MUTATIONS = (("adapter", "preserve_card"), ("runner", "set_content"), ("runner", "claim"),
             ("runner", "promote"), ("runner", "invalidate"))


def setUpModule():
    saved = []
    unittest.addModuleCleanup(_restore, saved)
    _install(saved, EXECUTION_TARGETS)


class _Rows:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _LeaseRowConnection:
    """Connection double: only the catalog lease read sees a substituted expires_at.

    SQLite TEXT NOT NULL affinity cannot hold NULL or numbers; this reaches the
    real process() path with those decoded values without touching the ledger.
    """

    def __init__(self, connection, value):
        self._connection, self._value = connection, value

    def execute(self, sql, *params):
        cursor = self._connection.execute(sql, *params)
        if sql.startswith("SELECT * FROM work_leases WHERE item_key=?"):
            row = cursor.fetchone()
            return _Rows(None if row is None else {**dict(row), "expires_at": self._value})
        return cursor

    def __getattr__(self, name):
        return getattr(self._connection, name)


class _LeaseRowStore:
    def __init__(self, real, value):
        self._real, self._value = real, value

    @contextlib.contextmanager
    def ledger(self, *args, **kwargs):
        with self._real.ledger(*args, **kwargs) as connection:
            yield _LeaseRowConnection(connection, self._value)

    def __getattr__(self, name):
        return getattr(self._real, name)


class LeaseClockTest(unittest.TestCase):
    # Borrow the shipped fixture's functions without binding its TestCase class
    # here, so loading this module never re-runs the shipped tests.
    write = shipped.CatalogStageTests.write
    save = shipped.CatalogStageTests.save
    prepare = shipped.CatalogStageTests.prepare
    advance_prerequisites = shipped.CatalogStageTests.advance_prerequisites

    def setUp(self):
        # Fixed UTC decision instant one day in the past: every real lease the
        # runner issues (now + ttl) is active against it, and every stored
        # expiry at or before it is also expired for stages.claim's own clock.
        self.instant = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(days=1)
        self.clock_calls = []

        def clock():
            self.clock_calls.append(self.instant)
            return self.instant
        patcher = mock.patch.object(catalog_stage, "_decision_instant", clock, create=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.fresh()

    def fresh(self):
        shipped.CatalogStageTests.setUp(self)
        self.key = "stage:" + self.subject + ":catalog"
        self.clock_calls.clear()

    # -- fixture helpers ------------------------------------------------------------
    def other_runner(self):
        return stages.testing_runner(self.database, run_id=self.run_id, owner="other-owner",
            engine_version="synthetic-engine", config_sha256=self.config,
            validators={"preserve": self.runner.validators["preserve"][1],
                        "extract": self.runner.validators["extract"][1], "catalog": self.adapter.validate},
            version="catalog-fixture-v1")

    def foreign_lease(self):
        """Prerequisites accepted, then a foreign runner holds a real catalog lease."""
        self.advance_prerequisites()
        prepared = self.prepare()
        other = self.other_runner()
        other.set_content(self.subject, "catalog", prepared["content"], author_id="synthetic-author", tier="A")
        claim = other.claim(self.subject, "catalog")
        return claim

    def own_lease(self):
        self.advance_prerequisites()
        prepared = self.prepare()
        self.runner.set_content(self.subject, "catalog", prepared["content"], author_id="synthetic-author", tier="A")
        return self.runner.claim(self.subject, "catalog")

    def store_expiry(self, value):
        with store.ledger(self.database) as con:
            changed = con.execute("UPDATE work_leases SET expires_at=? WHERE item_key=?", (value, self.key)).rowcount
            con.commit()
        self.assertEqual(changed, 1)

    def stored_lease(self):
        with store.ledger(self.database, readonly=True) as con:
            row = con.execute("SELECT *, typeof(expires_at) AS kind FROM work_leases WHERE item_key=?",
                              (self.key,)).fetchone()
        return None if row is None else dict(row)

    def snapshot(self):
        with store.ledger(self.database, readonly=True) as con:
            tables = [row[0] for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()]
            state = {table: sorted(tuple(row) for row in con.execute('SELECT * FROM "' + table + '"').fetchall())
                     for table in tables}
        files = sorted(str(path.relative_to(self.artifacts)) for path in self.artifacts.rglob("*"))
        return state, files

    def attempt(self, override=_ABSENT):
        """The actual process() result; an escaped exception becomes a comparable value."""
        if override is not _ABSENT:
            real = self.adapter.store
            self.adapter.store = _LeaseRowStore(real, override)
            self.addCleanup(setattr, self.adapter, "store", real)
        try:
            return self.catalog.process(self.path, self.save(), author_id="synthetic-author")
        except Exception as caught:  # recorded, then compared against the exact expected result
            return {"raised": type(caught).__name__, "message": str(caught)}
        finally:
            if override is not _ABSENT:
                self.adapter.store = real

    @contextlib.contextmanager
    def no_mutation(self):
        """Sentinel every post-lease mutation, then prove nothing in the ledger moved."""
        self.save()
        before = self.snapshot()
        lease = self.stored_lease()
        with contextlib.ExitStack() as stack:
            reached = {}
            for owner, attribute in MUTATIONS:
                target = self.adapter if owner == "adapter" else self.runner
                reached[attribute] = stack.enter_context(mock.patch.object(
                    target, attribute, side_effect=AssertionError("post-lease mutation reached: " + attribute)))
            yield reached
        self.assertEqual({name: double.call_count for name, double in reached.items()},
                         {attribute: 0 for _, attribute in MUTATIONS})
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.stored_lease(), lease)

    def assert_invalid(self, value, *, stored=True):
        """A foreign lease with `value` as expiry: blocked, nothing mutated, lease kept."""
        self.fresh()
        claim = self.foreign_lease()
        if stored:
            self.store_expiry(value)
        with self.no_mutation():
            result = self.attempt() if stored else self.attempt(override=value)
        self.assertEqual(result, BLOCKED, repr(value))
        lease = self.stored_lease()
        self.assertEqual((lease["owner"], lease["leased_at"]), ("other-owner", self.claim_leased_at(claim)))
        with self.assertRaisesRegex(catalog_stage.CatalogStageError, "^catalog_lease_clock_invalid$"):
            catalog_stage._lease_expiry(value)

    def claim_leased_at(self, claim):
        with store.ledger(self.database, readonly=True) as con:
            return con.execute("SELECT leased_at FROM stage_claims WHERE claim_id=?",
                               (claim["claim_id"],)).fetchone()[0]

    def catalog_status(self):
        with store.ledger(self.database, readonly=True) as con:
            return con.execute("SELECT status FROM stage_state WHERE stage='catalog'").fetchone()[0]

    def assert_done(self, result):
        self.assertEqual((result.get("status"), result.get("promoted"), result.get("reused")),
                         ("done", True, False), result)
        self.assertEqual(self.catalog_status(), "done")

    # -- frozen case IDs ----------------------------------------------------------
    def test_valid_active_foreign_and_same_owner(self):
        claim = self.foreign_lease()
        with self.no_mutation():
            self.assertEqual(self.attempt(), FOREIGN)
        self.assertEqual(self.clock_calls, [self.instant])
        lease = self.stored_lease()
        self.assertEqual((lease["owner"], lease["expires_at"]), ("other-owner", claim["expires_at"]))
        # Same instant written in other legal forms is still the same active lease.
        active = datetime.fromisoformat(claim["expires_at"])
        for text in (active.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                     active.astimezone(timezone(timedelta(hours=-7))).isoformat(),
                     (self.instant + timedelta(microseconds=1)).isoformat()):
            with self.subTest(expires_at=text):
                self.store_expiry(text)
                with self.no_mutation():
                    self.assertEqual(self.attempt(), FOREIGN)
        # Same owner and run: the existing active lease is reused and the stage completes.
        self.fresh()
        own = self.own_lease()
        result = self.attempt()
        self.assert_done(result)
        self.assertEqual(self.clock_calls, [self.instant])
        with store.ledger(self.database, readonly=True) as con:
            claims = con.execute("SELECT claim_id FROM stage_claims WHERE stage='catalog'").fetchall()
        self.assertEqual([row[0] for row in claims], [own["claim_id"]])

    def test_expired_exact_expiry_and_absent(self):
        # Absent lease: no lease decision, the stage completes.
        self.advance_prerequisites()
        self.assertIsNone(self.stored_lease())
        self.assert_done(self.attempt())
        for label, expiry in (("expired", self.instant - timedelta(seconds=1)),
                              ("expired_by_one_microsecond", self.instant - timedelta(microseconds=1)),
                              ("exact_expiry", self.instant)):
            with self.subTest(case=label):
                self.fresh()
                self.foreign_lease()
                self.store_expiry(expiry.isoformat())
                result = self.attempt()
                self.assert_done(result)
                self.assertEqual(self.clock_calls, [self.instant])
                self.assertEqual(self.stored_lease(), None)

    def test_null_nonstring_naive_and_garbage(self):
        naive = self.instant.replace(tzinfo=None)
        for value in (None, 0, 1792908673, 1.5, True, ["2026-10-25T00:00:00Z"]):
            with self.subTest(decoded=repr(value)):
                self.assert_invalid(value, stored=False)
        for value in (naive.isoformat(), (naive + timedelta(days=400)).isoformat(),
                      (naive + timedelta(days=400)).isoformat() + ".000001",
                      (self.instant + timedelta(days=400)).isoformat().encode(),
                      "", " ", "garbage", "now", "2026-10-25", "2026-10-25T00:00Z",
                      "2026-10-25 00:00:00+00:00", "2026-10-25t00:00:00Z", "2026-10-25T00:00:00z",
                      "20261025T000000Z", "2026-10-25T00:00:00+0000", "2026-10-25T00:00:00+00",
                      "2026-10-25T00:00:00UTC", " 2026-10-25T00:00:00Z", "2026-10-25T00:00:00Z\n",
                      "2026-10-25T00:00:00.Z", "2026-10-25T00:00:00,5Z", "2026-10-25T00:00:00.1234567Z",
                      "\uff12026-10-25T00:00:00Z", "2026-10-25T00:00:00\u00b100:00",
                      "+2026-10-25T00:00:00Z", "12026-10-25T00:00:00Z"):
            with self.subTest(stored=repr(value)):
                self.assert_invalid(value)
                if isinstance(value, bytes):
                    self.assertEqual(self.stored_lease()["kind"], "blob")

    def test_impossible_timezone_hours_minutes_and_calendar(self):
        for value in ("2099-10-25T00:00:00+24:00", "2099-10-25T00:00:00-24:00",
                      "2099-10-25T00:00:00+23:60", "2099-10-25T00:00:00-00:99", "2099-10-25T00:00:00+99:00",
                      "2099-02-30T00:00:00Z", "2099-02-29T00:00:00Z", "2100-02-29T00:00:00Z",
                      "2099-13-01T00:00:00Z", "2099-00-10T00:00:00Z", "2099-10-00T00:00:00Z",
                      "2099-10-32T00:00:00Z", "2099-04-31T00:00:00Z", "2099-10-25T24:00:00Z",
                      "2099-10-25T23:60:00Z", "2099-10-25T23:59:60Z", "0000-01-01T00:00:00Z"):
            with self.subTest(stored=value):
                self.assert_invalid(value)

    def test_fractional_seconds_and_legal_offsets(self):
        parse = catalog_stage._lease_expiry
        utc = timezone.utc
        self.assertEqual(parse("2024-02-29T23:59:59Z"), datetime(2024, 2, 29, 23, 59, 59, tzinfo=utc))
        self.assertEqual(parse("2026-10-25T00:00:00.5Z"), datetime(2026, 10, 25, 0, 0, 0, 500000, tzinfo=utc))
        self.assertEqual(parse("2026-10-25T00:00:00.000001+00:00"),
                         datetime(2026, 10, 25, 0, 0, 0, 1, tzinfo=utc))
        self.assertEqual(parse("2026-10-25T05:30:00.123456+05:30"),
                         datetime(2026, 10, 25, 0, 0, 0, 123456, tzinfo=utc))
        self.assertEqual(parse("2026-10-24T16:00:00-08:00"), datetime(2026, 10, 25, tzinfo=utc))
        self.assertEqual(parse("2026-10-25T23:59:00+23:59"), datetime(2026, 10, 25, 0, 0, tzinfo=utc))
        self.assertEqual(parse("2026-10-25T00:00:00-00:00"), datetime(2026, 10, 25, tzinfo=utc))
        self.assertEqual(parse("9999-12-31T23:59:59.999999Z"),
                         datetime(9999, 12, 31, 23, 59, 59, 999999, tzinfo=utc))
        self.assertEqual(parse(datetime(2026, 10, 25, 1, 2, 3, 456789, tzinfo=utc).isoformat()),
                         datetime(2026, 10, 25, 1, 2, 3, 456789, tzinfo=utc))
        for value in ("2026-10-25T00:00:00Z", "2026-10-25T00:00:00.5+05:30", "2026-10-24T16:00:00-08:00"):
            self.assertIsNotNone(parse(value).utcoffset())
        # Through process(): legal forms one microsecond after the instant stay active ...
        later = self.instant + timedelta(microseconds=1)
        forms = (later.astimezone(timezone(timedelta(hours=5, minutes=30))).isoformat(),
                 later.astimezone(timezone(-timedelta(hours=8))).isoformat(),
                 later.strftime("%Y-%m-%dT%H:%M:%S.%fZ"))
        self.foreign_lease()
        for text in forms:
            with self.subTest(active=text):
                self.store_expiry(text)
                with self.no_mutation():
                    self.assertEqual(self.attempt(), FOREIGN)
        # ... and the exact instant in any legal form is expired, so the stage completes.
        for text in (self.instant.astimezone(timezone(timedelta(hours=5, minutes=30))).isoformat(),
                     self.instant.astimezone(timezone(-timedelta(hours=8))).strftime("%Y-%m-%dT%H:%M:%S.000-08:00"),
                     self.instant.strftime("%Y-%m-%dT%H:%M:%SZ")):
            with self.subTest(exact=text):
                self.fresh()
                self.foreign_lease()
                self.store_expiry(text)
                self.assert_done(self.attempt())

    def test_no_stage_mutation_on_invalid_lease(self):
        for value in ("2026-10-25T00:00:00", "2026-10-25T00:00:00+24:00", "garbage"):
            with self.subTest(stored=value):
                self.fresh()
                self.foreign_lease()
                self.store_expiry(value)
                status = self.catalog_status()
                with self.no_mutation() as reached:
                    result = self.attempt()
                self.assertEqual(result, BLOCKED)
                self.assertEqual(sorted(reached), sorted(attribute for _, attribute in MUTATIONS))
                self.assertEqual(self.catalog_status(), status)
                self.assertFalse((self.artifacts / "catalog-card-artifacts").exists())
                self.assertEqual(self.stored_lease()["expires_at"], value)
        # A malformed lease on a stage this runner already holds is not rewritten either.
        self.fresh()
        self.own_lease()
        self.store_expiry("2026-10-25T00:00:00")
        with self.no_mutation():
            self.assertEqual(self.attempt(), BLOCKED)
        self.assertEqual(self.stored_lease()["owner"], "synthetic-cataloger")

    def test_unchanged_prior_drift_gate(self):
        malformed = "2026-10-25T00:00:00"
        # Prior artifact validation still answers first.
        self.foreign_lease()
        self.store_expiry(malformed)
        with self.no_mutation():
            self.assertEqual(self.catalog.process(self.path, catalog_stage._sha(b"wrong"), author_id="synthetic-author"),
                             {"status": "blocked", "reason": "card_artifact_hash_mismatch", "promoted": False,
                              "canonical_stage_changed": False})
            self.card["supports"][0]["quote"] = "invented phrase"
            self.assertEqual(self.attempt(), {"status": "blocked", "reason": "quote_not_in_source",
                                              "promoted": False, "canonical_stage_changed": False})
        # The extraction prerequisite gate is still ahead of the lease decision.
        self.fresh()
        with store.ledger(self.database) as con:
            con.execute("INSERT INTO work_leases VALUES(?,?,?,?,?,?,?,?)",
                        (self.key, "catalog", "other-owner", store.now(), malformed, 1, None, None))
            con.commit()
        with self.no_mutation():
            self.assertEqual(self.attempt(), {"status": "blocked", "reason": "accepted_extraction_receipt_required",
                                              "promoted": False, "canonical_stage_changed": False})
        # Accepted-evidence drift is still detected and reopened through the runner.
        self.fresh()
        self.advance_prerequisites()
        self.assert_done(self.attempt())
        with store.ledger(self.database) as con:
            con.execute("INSERT INTO work_leases VALUES(?,?,?,?,?,?,?,?)",
                        (self.key, "catalog", "other-owner", store.now(), malformed, 1, None, None))
            con.commit()
        self.write("unit.txt", b"changed bytes")
        drift = self.attempt()
        self.assertEqual((drift.get("status"), drift.get("reason"), drift.get("promoted"),
                          drift.get("canonical_stage_changed")),
                         ("blocked", "accepted_catalog_evidence_drift:support_artifact_hash_mismatch", False, True),
                         drift)
        self.assertNotEqual(self.catalog_status(), "done")
        # Without drift, an accepted stage with a malformed lease returns the lease gap.
        self.fresh()
        self.advance_prerequisites()
        self.assert_done(self.attempt())
        with store.ledger(self.database) as con:
            con.execute("INSERT INTO work_leases VALUES(?,?,?,?,?,?,?,?)",
                        (self.key, "catalog", "other-owner", store.now(), malformed, 1, None, None))
            con.commit()
        self.card["metadata"] = {"title": "Synthetic policy title.",
                                 "evidence": [{"source_sha256": self.subject, "locator": "page:1"}]}
        self.card["field_support"] = {"title": ["unit-one"]}
        with self.no_mutation():
            self.assertEqual(self.attempt(), BLOCKED)
        self.assertEqual(self.catalog_status(), "done")

    def test_offline_sentinels_held(self):
        self.assertEqual(guards_active(), sorted([owner.__name__ + "." + attribute
                                                  for owner, attribute in EXECUTION_TARGETS] +
                                                 ["sqlite3.connect"]))
        self.assertEqual(SENTINEL_HITS, [])
        self.assertEqual(DATABASE_HITS, [])
        self.assertTrue(DATABASE_PATHS)


if __name__ == "__main__":
    unittest.main()
