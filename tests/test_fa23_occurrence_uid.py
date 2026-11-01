"""FA23: captured mail identities use the IMAP 32-bit UID/UIDVALIDITY domain.

Synthetic data only. No original bytes, mailbox, network, model or acquisition
call is involved; every projected row remains a captured declaration.
"""
import copy
import hashlib
import json
from pathlib import Path
import socket
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from campaign_tool.records.ledger import occurrence_projection
from campaign_tool.records.ledger.occurrence_projection import project_occurrences
from campaign_tool.records.ledger.store import ledger
from tests.records import test_ledger_occurrences as occurrence_fixtures
from contextlib import closing

UID_MAX = 2 ** 32 - 1
FIELDS = ("uid", "uidvalidity")
BLOCKED = "invalid_or_unbound_mail_identity"
EXPLICIT = "explicit_mail_identity"
# Every out-of-domain mailbox number named by the frozen A2 contract.
INVALID_NUMBERS = (
    ("zero", 0), ("negative", -1), ("true", True), ("false", False),
    ("float", 1.0), ("string", "1"), ("null", None),
    ("upper_plus_one", UID_MAX + 1), ("far_above", 2 ** 64),
)
# A3 independent endpoints for each mailbox number.
ENDPOINTS = (1, UID_MAX, UID_MAX + 1)
OVERSIZED = ("upper_plus_one", "far_above")


def frozen_canonical(value):
    """Independent restatement of the ledger canonical JSON contract."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def expected_mail_id(identity):
    scoped = {"scheme": "mail_account_folder_uid_v1", **identity}
    return "occurrence:" + hashlib.sha256(frozen_canonical(["mail", scoped]).encode("utf-8")).hexdigest()


def mail_identity(uid, uidvalidity, account="synthetic-account", folder="INBOX"):
    return {"account": account, "folder": folder, "uidvalidity": uidvalidity, "uid": uid}


def mail_row(identity, label="synthetic message"):
    """Producer-shaped decoded legacy occurrences row plus its canonical original."""
    sha = hashlib.sha256(label.encode()).hexdigest()
    row = {"oid": "synthetic-" + label.replace(" ", "-"), "sha": sha, "path": "synthetic.eml",
           "root": "synthetic-root", "parent": None, "locator": json.dumps(None), "class": "hint-only",
           "receipt": json.dumps({"mail_identity": identity, "eml_sha256": sha}),
           "first_seen": "2000-01-01T00:00:00Z", "last_seen": "2000-01-01T00:00:00Z"}
    original = {"sha256": sha, "scope": "unresolved", "legacy_format": "eml"}
    return row, original


def domain_cases():
    """(label, uid, uidvalidity, expected_valid) for A2 and A3."""
    cases = []
    for uid in ENDPOINTS:
        for uidvalidity in ENDPOINTS:
            cases.append(("uid=%d,uidvalidity=%d" % (uid, uidvalidity), uid, uidvalidity,
                          uid <= UID_MAX and uidvalidity <= UID_MAX))
    for field in FIELDS:
        for other in (1, UID_MAX):
            for name, value in INVALID_NUMBERS:
                numbers = {field: value, ("uidvalidity" if field == "uid" else "uid"): other}
                cases.append(("%s=%s,other=%d" % (field, name, other), numbers["uid"], numbers["uidvalidity"], False))
    return cases


def domain_expectation_failures(base_plan):
    """Labels whose _base_plan result breaks the frozen A1-A3 expectation."""
    failures = []
    for label, uid, uidvalidity, valid in domain_cases():
        identity = mail_identity(uid, uidvalidity)
        row, original = mail_row(identity)
        row_before, original_before = copy.deepcopy(row), copy.deepcopy(original)
        candidate, reason, receipt, locator = base_plan(row, original)
        preserved = (row == row_before and original == original_before
                     and receipt == json.loads(row_before["receipt"]) and locator is None)
        if valid:
            ok = (reason == EXPLICIT and candidate is not None and candidate["kind"] == "mail"
                  and candidate["id"] == expected_mail_id(identity)
                  and candidate["source_ref"] == frozen_canonical({"scheme": "mail_account_folder_uid_v1", **identity}))
        else:
            ok = candidate is None and reason == BLOCKED
        if not (ok and preserved):
            failures.append(label)
    return failures


def endpoint_candidate_ids(base_plan):
    ids = {}
    for uid in (1, UID_MAX):
        for uidvalidity in (1, UID_MAX):
            candidate, reason, _, _ = base_plan(*mail_row(mail_identity(uid, uidvalidity)))
            ids[(uid, uidvalidity)] = (reason, candidate and candidate["id"], candidate and candidate["source_ref"])
    return ids


def oversized_labels():
    return sorted(label for label, uid, uidvalidity, valid in domain_cases()
                  if not valid and all(type(n) is int and not isinstance(n, bool) and 0 < n for n in (uid, uidvalidity)))


class MailUidDomainUnitTests(unittest.TestCase):
    base_plan = staticmethod(occurrence_projection._base_plan)

    def test_a1_valid_endpoints_keep_identity_bytes(self):
        for uid in (1, UID_MAX):
            for uidvalidity in (1, UID_MAX):
                with self.subTest(uid=uid, uidvalidity=uidvalidity):
                    identity = mail_identity(uid, uidvalidity)
                    row, original = mail_row(identity)
                    candidate, reason, receipt, _ = self.base_plan(row, original)
                    self.assertEqual(reason, EXPLICIT)
                    self.assertEqual(candidate["id"], expected_mail_id(identity))
                    self.assertEqual(candidate["source_ref"],
                                     frozen_canonical({"scheme": "mail_account_folder_uid_v1", **identity}))
                    self.assertEqual(candidate["acquisition_method"], "captured_legacy_mail")
                    self.assertIsNone(candidate["acquired_at"])
                    self.assertIsNone(candidate["parent_occurrence_id"])
                    self.assertEqual(receipt["mail_identity"], identity)

    def test_a1_filesystem_observation_unchanged(self):
        sha = hashlib.sha256(b"synthetic local").hexdigest()
        row = {"oid": "local", "sha": sha, "path": "synthetic.txt", "root": "synthetic-root", "parent": None,
               "locator": json.dumps(None), "receipt": json.dumps(
                   {"mtime_ns": 1, "ctime_ns": 1, "device": UID_MAX + 1, "inode": 2 ** 40,
                    "observed_at": "2000-01-01T00:00:00+00:00", "received_at": None})}
        candidate, reason, _, _ = self.base_plan(row, {"sha256": sha, "scope": "unresolved", "legacy_format": "txt"})
        self.assertEqual(reason, "filesystem_observation_only")
        self.assertEqual(candidate["kind"], "local")

    def test_a2_invalid_mailbox_numbers_block_each_field(self):
        for field in FIELDS:
            for name, value in INVALID_NUMBERS:
                with self.subTest(field=field, value=name):
                    identity = mail_identity(1, 1)
                    identity[field] = value
                    row, original = mail_row(identity)
                    raw = row["receipt"]
                    candidate, reason, receipt, _ = self.base_plan(row, original)
                    self.assertIsNone(candidate)
                    self.assertEqual(reason, BLOCKED)
                    self.assertEqual(row["receipt"], raw)
                    self.assertEqual(receipt, json.loads(raw))

    def test_a3_boundary_combinations_and_preservation(self):
        self.assertEqual(domain_expectation_failures(self.base_plan), [])
        # 5 oversized grid combinations + 2 fields x 2 partners x 2 oversized values.
        self.assertEqual(len(oversized_labels()), 13)

    def test_a3_account_and_folder_grammar_not_tightened(self):
        for folder in ("INBOX/Sub folder", "Synthetic \u00e9t\u00e9", " padded "):
            with self.subTest(folder=folder):
                candidate, reason, _, _ = self.base_plan(*mail_row(mail_identity(UID_MAX, 1, folder=folder)))
                self.assertEqual(reason, EXPLICIT)
        for folder in ("", "   ", "a\x00b", 7):
            with self.subTest(folder=folder):
                self.assertEqual(self.base_plan(*mail_row(mail_identity(1, 1, folder=folder)))[1], BLOCKED)

    def test_a3_other_binding_gates_still_apply_inside_domain(self):
        row, original = mail_row(mail_identity(UID_MAX, UID_MAX))
        self.assertEqual(self.base_plan(row, dict(original, legacy_format="txt"))[1], BLOCKED)
        receipt = json.loads(row["receipt"])
        receipt["eml_sha256"] = "0" * 64
        self.assertEqual(self.base_plan(dict(row, receipt=json.dumps(receipt)), original)[1], BLOCKED)
        receipt = json.loads(row["receipt"])
        receipt["mail_identity"]["extra"] = 1
        self.assertEqual(self.base_plan(dict(row, receipt=json.dumps(receipt)), original)[1], BLOCKED)


class MailUidProjectionIntegrationTests(unittest.TestCase):
    """A5/A6 through the real project_occurrences entry point on a fresh synthetic ledger."""

    VALID = ((1, UID_MAX), (UID_MAX, 1))
    INVALID = ((UID_MAX + 1, 1), (1, UID_MAX + 1), (UID_MAX + 1, UID_MAX + 1), (0, 1), (1, True), ("7", 1))

    def setUp(self):
        self.helper = occurrence_fixtures.OccurrenceProjectionTests()
        self.helper.setUp()
        self.addCleanup(self.helper.doCleanups)
        self.mails = []
        for index, (uid, uidvalidity) in enumerate(self.VALID + self.INVALID):
            label = "synthetic fa23 message %d" % index
            sha = self.helper.add_doc(label, "eml")
            oid = "fa23-message-%d" % index
            identity = mail_identity(uid, uidvalidity)
            self.helper.add_occurrence(oid, sha, receipt={"mail_identity": identity, "eml_sha256": sha})
            child = self.helper.add_doc("synthetic fa23 attachment %d" % index)
            self.helper.add_occurrence("fa23-attachment-%d" % index, child, parent=sha, locator={"mime": "1.2"},
                                       receipt={"relation": "decoded_mime_payload", "parent_sha256": sha,
                                                "parent_legacy_oid": oid})
            self.mails.append((identity, index < len(self.VALID)))

    def legacy_rows(self, con, import_id):
        return [tuple(r) for r in con.execute(
            "SELECT source_rowid,identity_json,payload_json,payload_sha256 FROM legacy_rows "
            "WHERE import_id=? AND source_table='occurrences' ORDER BY source_rowid", (import_id,))]

    def test_a5_real_projection_blocks_invalid_parents_and_restarts_idempotently(self):
        import_id = self.helper.capture()
        snapshot = (self.helper.snapshot / "intake.sqlite").read_bytes()
        with ledger(self.helper.database, readonly=True) as con:
            legacy_before = self.legacy_rows(con, import_id)
            events_before = con.execute("SELECT COUNT(*) FROM stage_events").fetchone()[0]
            stages_before = [tuple(r) for r in con.execute("SELECT * FROM stage_state ORDER BY 1,2")]
        first = project_occurrences(self.helper.database, import_id)
        invalid = len(self.INVALID)
        self.assertFalse(first["reused"])
        self.assertEqual(first["canonical_kinds"], {"local": 1, "mail": 2, "attachment": 2})
        self.assertEqual(first["unresolved_reasons"], {BLOCKED: invalid, "typed_mail_parent_not_established": invalid})
        self.assertEqual(first["blocked_source_rows"], 2 * invalid)
        self.assertEqual(first["stage_promotions"], 0)
        self.assertFalse(first["publication_ready"])
        self.assertIsNone(first["end_to_end_complete"])
        with ledger(self.helper.database, readonly=True) as con:
            refs = [json.loads(r[0]) for r in con.execute("SELECT source_ref FROM occurrences WHERE kind='mail'")]
            self.assertCountEqual([(r["uid"], r["uidvalidity"]) for r in refs], list(self.VALID))
            for identity, valid in self.mails:
                found = con.execute("SELECT COUNT(*) FROM occurrences WHERE id=?",
                                    (expected_mail_id(identity),)).fetchone()[0]
                self.assertEqual(found, 1 if valid else 0, identity)
            parents = {r[0] for r in con.execute("SELECT parent_occurrence_id FROM occurrences WHERE kind='attachment'")}
            self.assertEqual(parents, {expected_mail_id(i) for i, valid in self.mails if valid})
            self.assertEqual(con.execute("SELECT COUNT(*) FROM occurrences").fetchone()[0], 5)
            legacy_after = self.legacy_rows(con, import_id)
            self.assertEqual(legacy_after, legacy_before)
            saved = [tuple(r) for r in con.execute(
                "SELECT source_rowid,payload_json,payload_sha256,disposition,reason FROM occurrence_projection_rows "
                "ORDER BY source_rowid")]
            self.assertEqual([(r[0], r[2], r[3]) for r in legacy_before], [(r[0], r[1], r[2]) for r in saved])
            blocked = [r for r in saved if r[3] == "blocked"]
            self.assertEqual(sorted(r[4] for r in blocked),
                             sorted([BLOCKED] * invalid + ["typed_mail_parent_not_established"] * invalid))
            gaps = [json.loads(r[0])["code"] for r in con.execute(
                "SELECT reason FROM import_gaps WHERE category='occurrence_projection'")]
            self.assertEqual(sorted(gaps), sorted(r[4] for r in blocked))
            self.assertEqual(con.execute("SELECT COUNT(*) FROM stage_events").fetchone()[0], events_before)
            self.assertEqual([tuple(r) for r in con.execute("SELECT * FROM stage_state ORDER BY 1,2")], stages_before)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM joins WHERE status!='blocked'").fetchone()[0], 0)
            evidence = {json.loads(r[0])["verification"] for r in con.execute("SELECT evidence FROM occurrences")}
            self.assertEqual(evidence, {"captured_declaration_only"})
        second = project_occurrences(self.helper.database, import_id)
        self.assertTrue(second["reused"])
        self.assertEqual(second["projection_id"], first["projection_id"])
        self.assertEqual({k: v for k, v in second.items() if k != "reused"},
                         {k: v for k, v in first.items() if k != "reused"})
        with ledger(self.helper.database, readonly=True) as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM occurrences").fetchone()[0], 5)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM occurrence_projection_runs").fetchone()[0], 1)
            self.assertEqual(self.legacy_rows(con, import_id), legacy_before)
        self.assertEqual((self.helper.snapshot / "intake.sqlite").read_bytes(), snapshot)

    def test_a6_offline_and_confined_to_fixture_root(self):
        import_id = self.helper.capture()
        root = Path(tempfile.gettempdir()).resolve()
        self.assertTrue(Path(self.helper.database).resolve().is_relative_to(root))
        self.assertTrue(Path(self.helper.snapshot).resolve().is_relative_to(root))
        refused = RuntimeError("fa23 synthetic test refuses external effects")
        with patch.object(socket.socket, "connect", side_effect=refused) as connect, \
                patch.object(socket, "create_connection", side_effect=refused) as create, \
                patch.object(socket, "getaddrinfo", side_effect=refused) as resolve, \
                patch.object(subprocess, "Popen", side_effect=refused) as popen:
            result = project_occurrences(self.helper.database, import_id)
        for double in (connect, create, resolve, popen):
            self.assertFalse(double.called)
        self.assertEqual(result["stage_promotions"], 0)
        self.assertEqual(result["verified_agency_request_joins"], 0)
        with closing(sqlite3.connect(self.helper.database)) as con, con:
            listed = [r[2] for r in con.execute("PRAGMA database_list") if r[2]]
        self.assertTrue(listed)
        self.assertTrue(all(Path(p).resolve().is_relative_to(root) for p in listed))


if __name__ == "__main__":
    unittest.main()
