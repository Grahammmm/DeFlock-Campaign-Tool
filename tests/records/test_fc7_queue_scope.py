"""FC7-QUEUE-SCOPE: catalog scope exclusions stay out of planned stage claims.

Synthetic, source-shaped rows only. No private records, network or model calls.
Expected outcomes are written as literals so they do not depend on the
predicate under test; the vocabulary itself is read from ledger_catalog and
pinned against literals as well.
"""
import copy
import hashlib
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from campaign_tool.records import ledger_catalog
from campaign_tool.records import queue
from campaign_tool.records.ledger import store
from campaign_tool.records.ledger.migrations import v001
from campaign_tool.records.ledger.stages import StageRunner
from contextlib import closing

NOW = "2026-01-01T12:00:00+00:00"
FUTURE = "2026-01-01T13:00:00+00:00"
EXCLUDED = "original_out_of_scope"
LEGACY_SCOPE = "excluded"
CURRENT_SCOPE = "out_of_scope"
PERSONAL_ROLE = "excluded_unrelated_personal"


def subject(label):
    return hashlib.sha256(("fc7-queue-scope:" + label).encode()).hexdigest()


def original(label, scope=None, role=None, *, keys=("scope", "role")):
    row = {"sha256": subject(label)}
    if "scope" in keys:
        row["scope"] = scope
    if "role" in keys:
        row["role"] = role
    return row


def slots(originals, status="pending"):
    rows = []
    for item in originals:
        for stage in queue.STAGES:
            rows.append({"original_sha256": item["sha256"], "stage": stage, "status": status,
                         "receipt_sha256": None, "owner": "synthetic",
                         "reason": "synthetic_reason" if status in ("blocked", "inapplicable") else None})
    return rows


def excluded_entries(*labels):
    return [{"original_sha256": s, "reason": EXCLUDED} for s in sorted(subject(x) for x in labels)]


def canonical_database(path, originals):
    """Tiny database built from the unchanged canonical v001 migration."""
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.executescript(v001.SQL)
        connection.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?)",
                           ("fc7-run", "synthetic", NOW, None, "synthetic", None, None, None,
                            "running", "{}"))
        for row in originals:
            connection.execute(
                "INSERT INTO originals(sha256,bytes,mime_detected,first_seen_at,role,scope,storage_path,"
                "preservation_status,legacy_format,provenance_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (row["sha256"], 1, "text/plain", NOW, row["role"], row["scope"], None,
                 "pending", None, "{}"))
        for row in slots(originals):
            connection.execute(
                "INSERT INTO stage_state(original_sha256,stage,status,receipt_sha256,owner,updated_at,"
                "run_id,reason) VALUES(?,?,?,?,?,?,?,?)",
                (row["original_sha256"], row["stage"], row["status"], None, row["owner"], NOW,
                 "fc7-run", None))
    connection.close()


def file_digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class RecordingRunner(StageRunner):
    """Test double: satisfies the isinstance gate and records requested claims only."""

    def __init__(self, database):  # pylint: disable=super-init-not-called
        self.database = database
        self.owner = "fc7-synthetic-owner"
        self.run_id = "fc7-synthetic-run"
        self.requested = []

    def claim(self, subject, stage, *, ttl_seconds=300, supersedes=None):
        self.requested.append((subject, stage, ttl_seconds))
        return {"claim_id": "synthetic-claim", "subject": subject, "stage": stage}


class FC7QueueScopeTests(unittest.TestCase):
    def run_plan(self, originals, rows, leases=(), **kwargs):
        return queue.plan(originals, rows, list(leases), now=NOW, **kwargs)

    def assert_only_excluded(self, result, *labels):
        hashes = {subject(x) for x in labels}
        self.assertEqual(result["excluded_from_claims"], excluded_entries(*labels))
        self.assertFalse(hashes & {x["original_sha256"] for x in result["top"]})
        self.assertFalse(hashes & {x["original_sha256"] for x in result["blocked"]})
        self.assertFalse(hashes & set(result["settled_candidate_originals"]))

    def test_source_vocabulary_is_pinned(self):
        self.assertEqual(ledger_catalog.EXCLUDED_SCOPES, frozenset({LEGACY_SCOPE, CURRENT_SCOPE}))
        self.assertEqual(ledger_catalog.EXCLUDED_ROLE, PERSONAL_ROLE)

    def test_current_and_legacy_scope_exclusions(self):
        for scope in sorted(ledger_catalog.EXCLUDED_SCOPES):
            for status in ("pending", "blocked", "inapplicable", "in_progress"):
                with self.subTest(scope=scope, status=status):
                    originals = [original("scoped", scope, "original")]
                    facts = {subject("scoped"): {"new_production_open_request": True,
                                                 "document_type": "policy", "severity3_hits": 2}}
                    result = self.run_plan(originals, slots(originals, status), facts=facts)
                    self.assertEqual(result["snapshot_originals"], 1)
                    self.assertEqual(result["eligible"], 0)
                    self.assertEqual(result["top"], [])
                    self.assertEqual(result["blocked"], [])
                    self.assertEqual(result["settled_candidate_originals"], [])
                    self.assert_only_excluded(result, "scoped")
        # Both labels together, plus a scope-only legacy caller row without role.
        originals = [original("legacy", LEGACY_SCOPE, keys=("scope",)),
                     original("current", CURRENT_SCOPE, keys=("scope",))]
        result = self.run_plan(originals, slots(originals))
        self.assertEqual(result["eligible"], 0)
        self.assert_only_excluded(result, "legacy", "current")

    def test_excluded_role_with_in_scope(self):
        for scope in ("in_scope", "unresolved", None):
            for status in ("pending", "blocked", "inapplicable"):
                with self.subTest(scope=scope, status=status):
                    originals = [original("personal", scope, ledger_catalog.EXCLUDED_ROLE)]
                    result = self.run_plan(originals, slots(originals, status))
                    self.assertEqual(result["eligible"], 0)
                    self.assertEqual(result["top"], [])
                    self.assertEqual(result["blocked"], [])
                    self.assertEqual(result["settled_candidate_originals"], [])
                    self.assert_only_excluded(result, "personal")

    def test_ordinary_and_unresolved_preserved(self):
        originals = [original("in-scope", "in_scope", "original"),
                     original("unresolved", "unresolved", "original"),
                     original("role-none", "in_scope", None),
                     original("minimal", keys=()),
                     original("scope-only", "in_scope", keys=("scope",)),
                     original("role-only", role="attachment", keys=("role",))]
        result = self.run_plan(originals, slots(originals))
        expected = sorted(x["sha256"] for x in originals)
        self.assertEqual(result["snapshot_originals"], 6)
        self.assertEqual(result["eligible"], 6)
        self.assertEqual(result["excluded_from_claims"], [])
        self.assertEqual(result["blocked"], [])
        self.assertEqual(result["settled_candidate_originals"], [])
        self.assertEqual([x["original_sha256"] for x in result["top"]], expected)
        for item in result["top"]:
            self.assertEqual(item["stage"], "preserve")
            self.assertEqual(item["score"], 0)
            self.assertEqual(item["expected_attempt"], 1)
            self.assertFalse(item["reclaim_expired_lease"])
            self.assertEqual(item["prior_receipts"], {})
            self.assertEqual(item["timebox_seconds"], 45 * 60)
        # Ordinary settled and held outcomes are unchanged.
        settled = [original("settled", "in_scope", "original")]
        self.assertEqual(self.run_plan(settled, slots(settled, "inapplicable"))["settled_candidate_originals"],
                         [subject("settled")])
        held = self.run_plan(settled, slots(settled, "blocked"))
        self.assertEqual([x["original_sha256"] for x in held["blocked"]], [subject("settled")])
        self.assertEqual(held["blocked"][0]["reasons"][0]["code"], "blocked_without_retry")

    def mixed_population(self):
        originals = [original("ordinary-high", "in_scope", "original"),
                     original("ordinary-low", "unresolved", "original"),
                     original("held", "in_scope", "original"),
                     original("settled", "in_scope", "original"),
                     original("legacy", LEGACY_SCOPE, "original"),
                     original("current", CURRENT_SCOPE, "original"),
                     original("personal", "in_scope", PERSONAL_ROLE)]
        rows = slots(originals)
        for row in rows:
            if row["original_sha256"] == subject("settled"):
                row.update(status="inapplicable", reason="synthetic_reason")
            if row["original_sha256"] == subject("held") and row["stage"] == "preserve":
                row["status"] = "in_progress"
        leases = [{"item_key": "stage:" + subject("held") + ":preserve", "stage": "preserve",
                   "owner": "other", "expires_at": FUTURE, "attempts": 1}]
        high = {"new_production_open_request": True, "document_type": "audit"}
        facts = {subject("ordinary-high"): high,
                 subject("legacy"): {**high, "severity3_hits": 5, "live_agency": True},
                 subject("current"): {**high, "severity3_hits": 5, "live_agency": True},
                 subject("personal"): {**high, "severity3_hits": 5, "live_agency": True}}
        return originals, rows, leases, facts

    def test_mixed_population_counts_and_order(self):
        originals, rows, leases, facts = self.mixed_population()
        result = self.run_plan(originals, rows, leases, facts=facts)
        self.assertEqual(result["snapshot_originals"], 7)
        self.assertEqual(result["eligible"], 2)
        self.assertEqual([(x["original_sha256"], x["stage"], x["score"]) for x in result["top"]],
                         [(subject("ordinary-high"), "preserve", 70), (subject("ordinary-low"), "preserve", 0)])
        self.assertEqual([x["original_sha256"] for x in result["blocked"]], [subject("held")])
        self.assertEqual(result["blocked"][0]["reasons"][0]["code"], "live_lease")
        self.assertEqual(result["settled_candidate_originals"], [subject("settled")])
        self.assert_only_excluded(result, "legacy", "current", "personal")
        limited = self.run_plan(originals, rows, leases, facts=facts, limit=1)
        self.assertEqual(limited["eligible"], 2)
        self.assertEqual([x["original_sha256"] for x in limited["top"]], [subject("ordinary-high")])
        self.assertEqual(limited["excluded_from_claims"], result["excluded_from_claims"])
        reordered = self.run_plan(list(reversed(originals)), list(reversed(rows)), leases, facts=facts)
        self.assertEqual(reordered, result)

    def test_from_ledger_projects_role(self):
        originals = [original("ordinary", "in_scope", "original"),
                     original("legacy", LEGACY_SCOPE, "original"),
                     original("personal", "in_scope", PERSONAL_ROLE)]
        with tempfile.TemporaryDirectory(prefix="fc7-queue-scope-") as temp:
            database = Path(temp) / "canonical.sqlite"
            canonical_database(database, originals)
            before = file_digest(database)
            entries = sorted(p.name for p in Path(temp).iterdir())
            seen = []
            real = store.effective_original_fields

            def spy(connection, row):
                seen.append(tuple(row.keys()))
                return real(connection, row)

            with mock.patch.object(store, "effective_original_fields", side_effect=spy):
                result = queue.from_ledger(database, now=NOW)
            self.assertEqual(file_digest(database), before)
            self.assertEqual(sorted(p.name for p in Path(temp).iterdir()), entries)
        self.assertEqual(seen, [("sha256", "scope", "role")] * 3)
        self.assertEqual(result["snapshot_originals"], 3)
        self.assertEqual(result["eligible"], 1)
        self.assertEqual([x["original_sha256"] for x in result["top"]], [subject("ordinary")])
        self.assert_only_excluded(result, "legacy", "personal")
        # Historical minimal snapshots without a role column keep working.
        minimal = [original("minimal-in", "in_scope", keys=("scope",)),
                   original("minimal-out", LEGACY_SCOPE, keys=("scope",))]
        with tempfile.TemporaryDirectory(prefix="fc7-queue-scope-") as temp:
            database = Path(temp) / "minimal.sqlite"
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute("CREATE TABLE originals(sha256 TEXT,scope TEXT)")
                connection.execute("CREATE TABLE stage_state(original_sha256 TEXT,stage TEXT,status TEXT,"
                                   "receipt_sha256 TEXT,owner TEXT,reason TEXT)")
                connection.execute("CREATE TABLE work_leases(item_key TEXT)")
                connection.executemany("INSERT INTO originals VALUES(?,?)",
                                       [(x["sha256"], x["scope"]) for x in minimal])
                connection.executemany("INSERT INTO stage_state VALUES(?,?,?,?,?,?)",
                                       [tuple(x.values()) for x in slots(minimal)])
            connection.close()
            result = queue.from_ledger(database, now=NOW)
        self.assertEqual([x["original_sha256"] for x in result["top"]], [subject("minimal-in")])
        self.assertEqual(result["excluded_from_claims"], excluded_entries("minimal-out"))

    def test_no_excluded_claim_candidate(self):
        originals = [original("ordinary", "in_scope", "original"),
                     original("legacy", LEGACY_SCOPE, "original"),
                     original("current", CURRENT_SCOPE, "original"),
                     original("personal", "in_scope", PERSONAL_ROLE)]
        excluded = {subject("legacy"), subject("current"), subject("personal")}
        facts = {s: {"new_production_open_request": True, "document_type": "policy"} for s in excluded}
        with tempfile.TemporaryDirectory(prefix="fc7-queue-scope-") as temp:
            database = Path(temp) / "canonical.sqlite"
            canonical_database(database, originals)
            for limit in (1, 2, 20):
                with self.subTest(limit=limit):
                    plan = queue.from_ledger(database, now=NOW, facts=facts, limit=limit)
                    self.assertFalse(excluded & {x["original_sha256"] for x in plan["top"]})
            runner = RecordingRunner(database)
            outcome = queue.claim_next(runner, now=NOW, facts=facts, limit=1)
            self.assertEqual(runner.requested, [(subject("ordinary"), "preserve", queue.LEASE_SECONDS)])
            self.assertEqual(outcome["claimed"]["original_sha256"], subject("ordinary"))
            self.assertEqual(outcome["skipped"], [])
            only_excluded = Path(temp) / "only-excluded.sqlite"
            canonical_database(only_excluded, originals[1:])
            runner = RecordingRunner(only_excluded)
            outcome = queue.claim_next(runner, now=NOW, facts=facts)
            self.assertEqual(runner.requested, [])
            self.assertIsNone(outcome["claimed"])
            self.assertEqual(outcome["skipped"], [])
            with self.assertRaises(queue.QueueError) as raised:
                queue.claim_next(object(), now=NOW)
            self.assertEqual(str(raised.exception), "trusted_stage_runner_required")

    def test_unchanged_input_and_publication_flags(self):
        originals, rows, leases, facts = self.mixed_population()
        before = copy.deepcopy((originals, rows, leases, facts))
        result = self.run_plan(originals, rows, leases, facts=facts)
        self.assertEqual((originals, rows, leases, facts), before)
        self.assertEqual(result["schema"], "records-queue-plan-v1")
        self.assertEqual(result["planner_version"], queue.VERSION)
        self.assertIs(result["publication_ready"], False)
        self.assertIs(result["corpus_coverage_claim"], False)
        unsigned = {k: v for k, v in result.items() if k != "plan_sha256"}
        self.assertEqual(result["plan_sha256"], queue.digest(unsigned))
        # Excluded originals still need complete inventories and valid identities.
        legacy = [original("legacy", LEGACY_SCOPE, "original")]
        for broken, reason in ((slots(legacy)[1:], "incomplete_stage_slots"),
                               (slots(legacy) + slots(legacy)[:1], "duplicate_stage_slot")):
            with self.assertRaises(queue.QueueError) as raised:
                self.run_plan(legacy, broken)
            self.assertEqual(str(raised.exception), reason)
        with self.assertRaises(queue.QueueError) as raised:
            self.run_plan(legacy * 2, slots(legacy))
        self.assertEqual(str(raised.exception), "duplicate_original")
        with self.assertRaises(queue.QueueError) as raised:
            self.run_plan(legacy, slots(legacy), facts={subject("absent"): {}})
        self.assertEqual(str(raised.exception), "priority_subject_outside_snapshot")
        ordinary = [original("ordinary", "in_scope", "original")]
        with self.assertRaises(queue.QueueError) as raised:
            self.run_plan(ordinary, slots(ordinary), facts={subject("ordinary"): {"severity3_hits": True}})
        self.assertEqual(str(raised.exception), "invalid_hit_count")


if __name__ == "__main__":
    unittest.main()
