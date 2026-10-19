"""Synthetic tests for scripts/verify_adoption_test.py (TEST adoption verification).

Everything is synthetic: generated receipts in a temporary ``*-TEST`` fixture,
with no private mail, no TEST copy, no network. The script is executed as a real
subprocess (as the local worker will run it) and, for apply/rerun failure
paths, imported with its engine call stubbed.
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from campaign_tool.records import mail_adoption
from campaign_tool.records.run import Pipeline

from tests.records.test_mail_adoption_regression import synthetic_receipt

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "verify_adoption_test.py"
SPEC = importlib.util.spec_from_file_location("verify_adoption_test", SCRIPT)
verify_script = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verify_script)

RECORD_TEXT = ("adopt.example.invalid", "campaign.example.invalid", "Synthetic", "synthetic-adopt-account",
               "Adoption", "attachment-0.pdf", "@", "/")


def write_private(path, data):
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
    os.chmod(path, 0o600)


def leaves(value, key=None):
    if isinstance(value, dict):
        for k, v in value.items():
            yield k, None
            yield from leaves(v, k)
    elif isinstance(value, list):
        for v in value:
            yield from leaves(v, key)
    else:
        yield key, value


class VerifyAdoptionTestScriptTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name).resolve()
        self.fixture = self.base / "records-fixture-TEST"
        self.fixture.mkdir(mode=0o700)
        self.root = Pipeline(self.fixture / "processing-v1").root.path
        self.export = self.fixture / "mail-history"
        self.export.mkdir(mode=0o700)
        for uid, attachments in ((21, 1), (22, 0), (23, 2), (24, 1)):
            synthetic_receipt(self.export, uid=uid, attachments=attachments)
        self.out = self.base / "out"
        self.out.mkdir(mode=0o700)
        self.config = self.write_config("enabled")

    def write_config(self, adoption, name="adoption-config.json"):
        path = self.base / name
        write_private(path, json.dumps({"schema": mail_adoption.CONFIG_SCHEMA, "source": "mail-history",
                                        "adoption": adoption, "disable_marker_sha256": None}).encode())
        return path

    def count(self, sql):
        con = sqlite3.connect("file:%s?mode=ro&immutable=1" % (self.root / "ledger.sqlite"), uri=True)
        try:
            return con.execute(sql).fetchone()[0]
        finally:
            con.close()

    def snapshot(self):
        return verify_script.tree_digest(self.fixture)

    def argv(self, receipt="receipt.json", *, messages=4, attachments=4, config=None, extra=()):
        return ["--test-root", str(self.fixture), "--config", str(config or self.config),
                "--receipt", str(self.out / receipt), "--expect-messages", str(messages),
                "--expect-attachments", str(attachments), *extra]

    def run_script(self, *args, **kw):
        done = subprocess.run([sys.executable, "-B", str(SCRIPT), *self.argv(*args, **kw)],
                              capture_output=True, timeout=600, check=False, cwd=str(self.base))
        return done

    def assert_numbers_only(self, text, receipt):
        for needle in RECORD_TEXT + (str(self.base), self.fixture.name):
            self.assertNotIn(needle, text)
        for key, value in leaves(receipt):
            if value is None or isinstance(value, (bool, int)):
                continue
            self.assertIsInstance(value, str, key)
            if key in ("mail_adoption_sha256", "script_sha256"):
                self.assertRegex(value, r"^[0-9a-f]{64}$")
            elif key == "schema":
                self.assertEqual(value, verify_script.SCHEMA)
            else:
                self.assertRegex(value, r"^[a-z][a-z0-9_:]*$", key)

    def test_full_pass_dry_run_apply_and_zero_new_rerun(self):
        before_export = verify_script.tree_digest(self.export)
        done = self.run_script()
        self.assertEqual(done.returncode, 0, done.stdout)
        self.assertEqual(done.stderr, b"")
        receipt = json.loads((self.out / "receipt.json").read_text())
        self.assertEqual(json.loads(done.stdout), receipt)
        self.assertEqual(receipt["status"], "pass")
        self.assertEqual(receipt["failed_checks"], [])
        self.assertEqual(receipt["baseline"]["mail_messages"], 0)
        self.assertEqual((receipt["dry_run"]["receipts"], receipt["dry_run"]["attachments"]), (4, 4))
        self.assertEqual(receipt["dry_run"]["new_messages"], 4)
        self.assertEqual(receipt["apply"]["adopted_messages"], 4)
        self.assertTrue(receipt["apply"]["started_run"])
        self.assertEqual(receipt["after_apply"]["mail_messages"], 4)
        self.assertEqual(receipt["after_apply"]["runs"], receipt["baseline"]["runs"] + 1)
        self.assertEqual(receipt["after_apply"]["mail_history_runs_by_status"], {"completed": 1})
        self.assertEqual(receipt["new_on_rerun"], 0)
        self.assertFalse(receipt["rerun"]["started_run"])
        self.assertEqual(receipt["after_rerun"], receipt["after_apply"])
        self.assertTrue(receipt["checks"]["rerun_tree_unchanged"])
        self.assertTrue(receipt["checks"]["export_unchanged"])
        self.assert_numbers_only((self.out / "receipt.json").read_text() + done.stdout.decode(), receipt)
        self.assertEqual(os.stat(self.out / "receipt.json").st_mode & 0o777, 0o600)
        # Ground truth from the ledger itself and the untouched export.
        self.assertEqual(self.count("SELECT count(*) FROM mail_messages"), 4)
        self.assertEqual(verify_script.tree_digest(self.export), before_export)

    def test_second_run_takes_nothing_new_branch_and_writes_nothing(self):
        self.assertEqual(self.run_script("first.json").returncode, 0)
        before = self.snapshot()
        done = self.run_script("second.json")
        self.assertEqual(done.returncode, 0, done.stdout)
        receipt = json.loads((self.out / "second.json").read_text())
        self.assertEqual(receipt["status"], "pass")
        self.assertEqual((receipt["dry_run"]["already_adopted"], receipt["dry_run"]["new_messages"]), (4, 0))
        self.assertEqual(receipt["apply"]["adopted_messages"], 0)
        self.assertFalse(receipt["apply"]["started_run"])
        self.assertTrue(receipt["checks"]["apply_tree_unchanged_when_nothing_new"])
        self.assertEqual(self.snapshot(), before)

    def test_count_mismatch_stops_before_apply_without_mutation(self):
        before = self.snapshot()
        done = self.run_script(messages=154, attachments=82)
        self.assertEqual(done.returncode, 1)
        receipt = json.loads((self.out / "receipt.json").read_text())
        self.assertEqual(receipt["status"], "fail_dry_run")
        self.assertEqual(receipt["stopped_at"], "dry_run")
        self.assertIn("dry_inventory_messages", receipt["failed_checks"])
        self.assertIn("dry_inventory_attachments", receipt["failed_checks"])
        self.assertNotIn("apply", receipt)
        self.assertTrue(receipt["checks"]["dry_tree_unchanged"])
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.count("SELECT count(*) FROM mail_messages"), 0)

    def test_unresolved_legacy_original_stops_before_apply(self):
        # Mirrors the known blocker class: an EML already declared as an
        # unresolved legacy original. The script must not apply around it.
        _, receipt = synthetic_receipt(self.export, uid=25, attachments=0)
        sha = receipt["original_eml"]["sha256"]
        from campaign_tool.records.ledger import store
        with store.ledger(self.root / "ledger.sqlite") as con:
            con.execute("INSERT INTO originals VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (sha, receipt["original_eml"]["bytes"], None, "2026-01-01T00:00:00+00:00",
                         "unresolved", "unresolved", None, "captured", "eml", "{}"))
            con.commit()
        before = self.snapshot()
        done = self.run_script(messages=5, attachments=4)
        self.assertEqual(done.returncode, 1, done.stdout)
        receipt = json.loads((self.out / "receipt.json").read_text())
        self.assertEqual(receipt["status"], "fail_dry_run")
        self.assertEqual(receipt["dry_run"]["predicted_binding_conflicts"], {"original_scope_unresolved": 1})
        self.assertIn("dry_no_predicted_binding_conflicts", receipt["failed_checks"])
        self.assertIn("dry_eligible_equals_new", receipt["failed_checks"])
        self.assertNotIn("apply", receipt)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.count("SELECT count(*) FROM mail_messages"), 0)

    def test_closed_gate_stops_before_apply(self):
        before = self.snapshot()
        done = self.run_script(config=self.write_config("disabled", "disabled.json"))
        self.assertEqual(done.returncode, 1)
        receipt = json.loads((self.out / "receipt.json").read_text())
        self.assertEqual(receipt["failed_checks"], ["dry_gate_open"])
        self.assertEqual(receipt["dry_run"]["gate_state"], "disabled_by_config")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.count("SELECT count(*) FROM runs WHERE run_id LIKE 'mail-history-%'"), 0)

    def test_dry_run_only_at_acceptance_scale(self):
        export = self.export
        for uid in range(101, 251):  # 4 existing + 150 = 154 messages; 4 + 78 = 82 attachments
            synthetic_receipt(export, uid=uid, attachments=1 if uid < 179 else 0)
        before = self.snapshot()
        done = self.run_script(messages=154, attachments=82, extra=("--dry-run-only",))
        self.assertEqual(done.returncode, 0, done.stdout)
        receipt = json.loads((self.out / "receipt.json").read_text())
        self.assertEqual(receipt["status"], "pass_dry_run_only")
        self.assertEqual((receipt["dry_run"]["receipts"], receipt["dry_run"]["attachments"]), (154, 82))
        self.assertNotIn("apply", receipt)
        self.assertEqual(self.snapshot(), before)

    def test_guards_refuse_before_anything_runs(self):
        before = self.snapshot()
        prod = self.base / "records-project"
        prod.mkdir()
        cases = [
            (["--test-root", str(prod)], "test_root_not_test"),
            (["--receipt", str(self.fixture / "r.json")], "receipt_inside_test_root"),
        ]
        for override, code in cases:
            argv = self.argv()
            index = argv.index(override[0])
            argv[index + 1] = override[1]
            done = subprocess.run([sys.executable, "-B", str(SCRIPT), *argv], capture_output=True,
                                  timeout=120, check=False)
            self.assertEqual(done.returncode, 2)
            self.assertEqual(json.loads(done.stdout), {"schema": verify_script.SCHEMA, "status": "refused",
                                                       "code": code})
        os.chmod(self.config, 0o644)
        done = self.run_script()
        self.assertEqual(json.loads(done.stdout)["code"], "config_not_owner_only")
        os.chmod(self.config, 0o600)
        (self.out / "receipt.json").write_text("{}")
        self.assertEqual(json.loads(self.run_script().stdout)["code"], "receipt_exists")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.count("SELECT count(*) FROM mail_messages"), 0)

    def test_guard_refuses_other_uid_and_symlinked_root(self):
        args = verify_script.parse_args(self.argv())
        with self.assertRaisesRegex(verify_script.Refused, "uid_not_ledger_owner"):
            verify_script.guard(args, euid=os.geteuid() + 1)
        link = self.base / "link-TEST"
        link.symlink_to(self.fixture)
        args = verify_script.parse_args(self.argv())
        args.test_root = str(link)
        with self.assertRaisesRegex(verify_script.Refused, "test_root_unsafe"):
            verify_script.guard(args)

    def stubbed_verify(self, results):
        args = verify_script.parse_args(self.argv())
        paths = verify_script.guard(args)
        real = verify_script.run_adopt
        calls = iter(results)

        def fake(engine, root, export, config, *, apply, timeout):
            override = next(calls)
            result = real(engine, root, export, config, apply=apply, timeout=timeout) if override is None \
                else override
            return result
        with mock.patch.object(verify_script, "run_adopt", fake):
            return verify_script.verify(paths, args)

    def test_apply_failure_stops_before_rerun(self):
        failed_apply = {"exit": 3, "timed_out": False, "stderr_empty": True,
                        "report": verify_script.sanitize_report({
                            "mode": "apply", "source": "mail-history", "receipts": 4, "attachments": 4,
                            "already_adopted": 0, "new_messages": 4, "adopted_messages": 1,
                            "resumed_messages": 0, "failures": {"canonical_original_binding_conflict": 3},
                            "gate": {"state": "enabled", "open": True}, "run_id": "mail-history-x",
                            "after": {"already_adopted": 1, "new_messages": 3, "incomplete_messages": 0,
                                      "pending_messages": 3, "identity_conflicts": {}, "unfinalized_runs": 0}})}
        receipt = self.stubbed_verify([None, failed_apply])
        self.assertEqual(receipt["status"], "fail_apply")
        self.assertNotIn("rerun", receipt)
        for name in ("apply_exit_0", "apply_no_failures", "apply_adopted_equals_new", "apply_after_clean"):
            self.assertIn(name, receipt["failed_checks"])
        self.assertEqual(receipt["apply"]["failures"], {"canonical_original_binding_conflict": 3})

    def test_rerun_that_adopts_again_fails(self):
        rerun = {"exit": 0, "timed_out": False, "stderr_empty": True,
                 "report": verify_script.sanitize_report({
                     "mode": "apply", "source": "mail-history", "receipts": 4, "attachments": 4,
                     "already_adopted": 4, "new_messages": 0, "pending_messages": 0, "incomplete_messages": 0,
                     "adopted_messages": 2, "resumed_messages": 0, "failures": {}, "gaps": {},
                     "identity_conflicts": {}, "gate": {"state": "enabled", "open": True},
                     "run_id": "mail-history-y",
                     "after": {"already_adopted": 4, "new_messages": 0, "incomplete_messages": 0,
                               "pending_messages": 0, "identity_conflicts": {}, "unfinalized_runs": 0}})}
        receipt = self.stubbed_verify([None, None, rerun])
        self.assertEqual(receipt["status"], "fail")
        self.assertEqual(sorted(receipt["failed_checks"]), ["rerun_adopted_zero", "rerun_no_run_started"])
        self.assertEqual(receipt["new_on_rerun"], 2)

    def test_sanitizer_drops_text_and_unsafe_codes(self):
        report = verify_script.sanitize_report({
            "mode": "dry-run", "source": "mail-history", "receipts": 1,
            "gaps": {"Subject: secret": 1, "receipt_unreadable": 2},
            "gate": {"state": "something else", "open": True, "marker_sha256": "a" * 64},
            "run_id": "mail-history-abc", "extra_text": "Dear clerk"})
        self.assertEqual(report["gaps"], {"receipt_unreadable": 2, "unrecognized_code": 1})
        self.assertEqual(report["gate_state"], "unrecognized_code")
        text = json.dumps(report)
        for needle in ("secret", "Dear clerk", "a" * 64, "mail-history-abc"):
            self.assertNotIn(needle, text)
        refused = verify_script.sanitize_report({"status": "refused", "code": "adoption_gate_closed:enabled"})
        self.assertEqual(refused["code"], "adoption_gate_closed:enabled")
        self.assertEqual(verify_script.sanitize_report("not json")["parsed"], False)


if __name__ == "__main__":
    unittest.main()
