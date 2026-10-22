"""T4B slice 4: resolution-policies/ joins the backup closure (synthetic only)."""
from contextlib import closing
from dataclasses import replace
import hashlib
import json
import sqlite3
import unittest

from campaign_tool.records.recovery import backup_v2 as v2
from campaign_tool.records.recovery import resolution_closure as closure
from tests import test_records_backup_v2 as fixture  # module import: its tests are not collected here

POLICY = b'{"synthetic":"policy bytes only; not a real approval"}'


class PolicyClosureTests(unittest.TestCase):
    """Reuses the synthetic backup fixture and adds a mail-export resolution row + policy file."""

    def setUp(self):
        fixture.RecoveryV2.setUp(self)  # builds self.profile / self.q and the synthetic stores
        self.policy_sha = hashlib.sha256(POLICY).hexdigest()
        folder = self.source / closure.POLICY_DIR
        folder.mkdir(mode=0o700)
        self.policy_path = folder / self.policy_sha
        self.policy_path.write_bytes(POLICY)
        self.policy_path.chmod(0o400)
        with closing(sqlite3.connect(self.canonical)) as con, con:
            con.execute("CREATE TABLE original_resolutions(original_sha256 TEXT PRIMARY KEY, policy_sha256 TEXT, "
                        "payload_json TEXT)")
            con.execute("INSERT INTO original_resolutions VALUES(?,?,?)",
                        (self.sha, self.policy_sha, json.dumps({"schema": "original_resolution_mail_export_v1"})))
            # A v1 local-capture row names its policy elsewhere and must not be demanded here.
            con.execute("INSERT INTO original_resolutions VALUES(?,?,?)",
                        ("f" * 64, "e" * 64, json.dumps({"schema": "original_resolution_v1"})))
            schema = fixture.schema_hash(con)
        stores = tuple(replace(s, schema_sha256=schema,
                               required_tables=s.required_tables + ("original_resolutions",))
                       if s.name == "canonical" else s for s in self.profile.stores)
        self.profile = replace(self.profile, stores=stores)

    def extended(self):
        return closure.add_resolution_policies(self.profile, str(self.source))

    def test_policy_is_backed_up_verified_and_restored(self):
        profile = self.extended()
        report = v2.backup_v2(str(self.base / "backup"), profile, quiescence=self.q)
        v2.verify_v2(str(self.base / "backup"), manifest_sha256=report["manifest_sha256"])
        v2.restore_v2(str(self.base / "backup"), str(self.base / "restored"), manifest_sha256=report["manifest_sha256"])
        payloads = [p.read_bytes() for p in (self.base / "restored").rglob("*") if p.is_file()]
        self.assertIn(POLICY, payloads)

    def test_a_referenced_policy_missing_from_the_backup_fails_closed(self):
        # The ledger names the policy, the profile asks for it, and the file is gone.
        self.policy_path.unlink()
        self.policy_path.parent.rmdir()
        stores = tuple(replace(s, reference_sql=s.reference_sql + closure.reference_sql(str(self.source)))
                       if s.name == "canonical" else s for s in self.profile.stores)
        with self.assertRaisesRegex(v2.RecoveryError, "database_reference_not_closed"):
            v2.backup_v2(str(self.base / "unclosed"), replace(self.profile, stores=stores), quiescence=self.q)

    def test_an_unclassified_policy_directory_fails_the_scope_check(self):
        with self.assertRaisesRegex(v2.RecoveryError, "unknown_or_missing_scope_store"):
            v2.backup_v2(str(self.base / "unclassified"), self.profile, quiescence=self.q)

    def test_no_policy_directory_leaves_the_profile_unchanged(self):
        self.policy_path.unlink()
        self.policy_path.parent.rmdir()
        self.assertIs(closure.add_resolution_policies(self.profile, str(self.source)), self.profile)

    def test_a_policy_file_that_does_not_hash_to_its_name_is_refused(self):
        self.policy_path.chmod(0o600)
        self.policy_path.write_bytes(POLICY + b" tampered")
        self.policy_path.chmod(0o400)
        with self.assertRaisesRegex(v2.RecoveryError, "policy_file_hash_mismatch"):
            self.extended()

    def test_the_query_is_one_bounded_select_and_rejects_odd_roots(self):
        (sql,) = closure.reference_sql("/srv/records")
        self.assertTrue(sql.startswith("SELECT") and ";" not in sql and len(sql) < 4096)
        for bad in ("relative/path", "/srv/it's", "/srv/../x", "/srv/a;b"):
            with self.assertRaises(v2.RecoveryError):
                closure.reference_sql(bad)

    def test_adding_twice_is_refused(self):
        with self.assertRaisesRegex(v2.RecoveryError, "policy_closure_profile_invalid"):
            closure.add_resolution_policies(self.extended(), str(self.source))


if __name__ == "__main__":
    unittest.main()
