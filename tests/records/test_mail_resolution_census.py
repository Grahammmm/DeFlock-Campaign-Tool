"""S3a synthetic config/census tests. No live/private records or subprocesses."""
import contextlib
from email.message import EmailMessage
from email.policy import SMTP
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

from campaign_tool.records import mail_adoption as adoption, mail_resolution_census as census
from campaign_tool.records import mail_resolution_config as config
from campaign_tool.records.intake import eml_export
from campaign_tool.records.ledger import store
from campaign_tool.records.ledger.mail_resolution_policy import ApprovedMailResolutionPolicy
from campaign_tool.records.run import Pipeline, root_lock
from campaign_tool.records.runner.canonical_mail import CanonicalMailBackend
from campaign_tool.records.runner.contracts import Folder
from campaign_tool.records.runner.core import hid
from tests.records.test_canonical_mail_resolution import export
from tests.records.test_mail_adoption import write_private


class CensusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.root = self.base / "records"
        self.pipeline = Pipeline(self.root)
        self.mail = self.base / "frozen-export"
        self.mail.mkdir(mode=0o700)
        self.paths = [export(self.mail, uid, duplicate=uid == 41)[0] for uid in (41, 42, 43)]
        self.receipt = json.loads(self.paths[0].read_bytes())
        self.subject = self.receipt["attachments"][0]["sha256"]
        self.declare(self.subject, self.receipt["attachments"][0]["bytes"], "pdf")

    def declare(self, subject, size, form):
        with store.ledger(self.root / "ledger.sqlite") as con:
            rowid = con.execute("SELECT count(*) FROM legacy_rows").fetchone()[0] + 1
            con.execute("INSERT OR IGNORE INTO runs(run_id,kind,started_at,engine_version,status,summary) VALUES(?,?,?,?,?,?)",
                        ("synthetic", "legacy-import", "2026-01-01", "synthetic", "complete", "{}"))
            if not con.execute("SELECT 1 FROM legacy_imports WHERE id='synthetic'").fetchone():
                con.execute("INSERT INTO legacy_imports VALUES(?,?,?,?,?,?,?,?,?,?,?)", (
                    "synthetic", "a" * 64, "b" * 64, "synthetic", "c" * 64, "synthetic",
                    "complete", "2026-01-01", "2026-01-01", "{}", "d" * 64))
            payload = store.canonical({"sha": subject, "bytes": size, "format": form})
            con.execute("INSERT INTO legacy_rows VALUES(?,?,?,?,?,?)", ("synthetic", "docs", rowid,
                store.canonical({"sha": subject}), payload, hashlib.sha256(payload.encode()).hexdigest()))
            con.execute("INSERT INTO originals VALUES(?,?,?,?,?,?,?,?,?,?)", (subject, size, None,
                "2026-01-01T00:00:00+00:00", "unresolved", "unresolved", None, "captured", form,
                store.canonical({"table": "docs", "import_id": "synthetic", "source_rowid": rowid})))
            for stage in store.STAGES:
                con.execute("INSERT INTO stage_state(original_sha256,stage,status,receipt_sha256,owner,updated_at,run_id,reason) VALUES(?,?,?,?,?,?,?,?)",
                    (subject, stage, "pending", None, "receipt_reconciliation", "2026-01-01", "synthetic", "synthetic declaration"))
            con.commit()

    def cfg(self, **values):
        value = {"schema": config.V2, "source": "mail-history", "adoption": "enabled"}
        value.update(values)
        path = self.base / "config.json"
        write_private(path, store.canonical(value).encode())
        return path

    def tree(self):
        result = {}
        for root in (self.root, self.mail):
            for base, dirs, files in os.walk(root):
                for name in ["", *dirs, *files]:
                    path = Path(base) / name
                    st = path.lstat()
                    result[str(path)] = (st.st_dev, st.st_ino, st.st_mode, st.st_size, st.st_mtime_ns,
                        hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None)
        return result

    def build(self, **kwargs):
        return census.build_inventory(self.root, self.mail, **kwargs)

    def policy(self, inventory=None):
        entry = (inventory or self.build())["originals"][self.subject]
        p = entry["proposal"]
        value = {"schema": "approved-mail-resolution-policy-v1", "inventory_sha256": "1" * 64,
            "approval_sha256": "2" * 64, "held": [], "originals": {self.subject: {
                "bytes": entry["bytes"], "role": "original", "scope": "in_scope",
                "verification_receipt_sha256": p["verification_receipt_sha256"],
                "binding_occurrence_id": p["binding_occurrence_id"],
                "dependent_receipts": p["dependent_receipts"], "legacy_binding_sha256": entry["legacy_binding_sha256"]}}}
        return self.save_policy(value)

    def save_policy(self, value):
        raw = store.canonical(value).encode()
        sha = hashlib.sha256(raw).hexdigest()
        directory = self.root / "resolution-policies"
        directory.mkdir(mode=0o700, exist_ok=True)
        write_private(directory / sha, raw)
        return ApprovedMailResolutionPolicy.load(self.root, expected_sha256=sha)

    def test_union_shared_repeated_determinism_and_byte_identity(self):
        before = self.tree()
        first = self.build()
        receipts, gaps = adoption.discover(self.mail, self.root / "intake")
        with patch.object(adoption, "discover", return_value=(list(reversed(receipts)), gaps)):
            second = self.build()
        self.assertEqual(store.canonical(first), store.canonical(second))
        self.assertEqual(self.tree(), before)
        self.assertEqual(first["counts"]["originals"], 1)
        self.assertEqual(first["counts"]["shared_originals"], 1)
        self.assertEqual(first["counts"]["repeated_originals"], 1)
        entry = first["originals"][self.subject]
        self.assertIsNone(entry["eligibility_code"])
        members = entry["receipts"]
        self.assertEqual(len(members), 3)
        self.assertEqual(entry["proposal"]["verification_receipt_sha256"], min(m["receipt_sha256"] for m in members))
        binding = members[0]
        self.assertEqual(entry["proposal"]["binding_occurrence_id"], min(binding["occurrence_ids"]))
        repeated = next(m for m in members if len(m["occurrence_ids"]) == 2)
        mid = hid(["mail", "fixture-account", "Records", 900, 41])
        self.assertEqual(repeated["occurrence_ids"], sorted(hid(["attachment", mid, loc]) for loc in ("1.2", "1.3")))

    def test_all_discovery_states_are_included(self):
        receipts, gaps = adoption.discover(self.mail, self.root / "intake")
        for item, state in zip(receipts, ("complete", "incomplete", "identity_eml_changed")):
            item["state"] = state
        with patch.object(adoption, "discover", return_value=(receipts, gaps)):
            self.assertEqual(len(self.build()["originals"][self.subject]["receipts"]), 3)

    def test_rfc822_only_original_is_held_and_all_receipts_retained(self):
        msg = EmailMessage()
        msg.set_content("Synthetic outer\n")
        inner = EmailMessage()
        inner.set_content("Synthetic nested\n")
        msg.add_attachment(inner)
        raw = msg.as_bytes(policy=SMTP)
        sha = hashlib.sha256(raw).hexdigest()
        path = self.mail / "rfc822.eml"
        write_private(path, raw)
        value = {"complete": True, "account_id": "fixture-account", "folder": "Records", "uidvalidity": 900,
            "uid": 44, "bytes": len(raw), "original_eml": {"path": path.name, "sha256": sha, "bytes": len(raw)}, "attachments": []}
        write_private(self.mail / "receipts" / "rfc822.json", store.canonical(value).encode())
        self.declare(sha, len(raw), "eml")
        result = self.build()
        entry = result["originals"][sha]
        self.assertEqual(entry["receipts"][0]["mime_precheck"], "unsupported_rfc822_part")
        self.assertEqual(entry["receipts"][0]["occurrence_ids"], [adoption.message_id(("fixture-account", "Records", "900", "44"))])
        self.assertEqual(entry["proposal"], {"state": "held", "code": "resolution_no_eligible_binding_receipt"})
        self.assertEqual(result["counts"]["receipts"], 4)

    def test_census_private_output_counts_only_and_no_source_changes(self):
        target = self.base / "inventory.json"
        before = self.tree()
        out = io.StringIO()
        with patch.object(sys, "argv", ["adopt-mail", "--root", str(self.root), "--mail-history", str(self.mail),
                                       "--legacy-resolution-inventory", str(target)]), contextlib.redirect_stdout(out):
            self.assertEqual(adoption.main(), 0)
        self.assertEqual(self.tree(), before)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        value = json.loads(target.read_bytes())
        self.assertEqual(json.loads(out.getvalue()), value["counts"])
        for private in (self.subject, "fixture-account", "Records", str(self.base)):
            self.assertNotIn(private, out.getvalue())
        with self.assertRaisesRegex(adoption.AdoptionError, "output_exists"):
            census.inventory(self.root, self.mail, target)

    def test_apply_and_inventory_are_mutually_exclusive(self):
        with patch.object(sys, "argv", ["adopt-mail", "--root", str(self.root), "--mail-history", str(self.mail),
             "--legacy-resolution-inventory", str(self.base / "out"), "--apply"]), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                adoption.main()
        self.assertEqual(error.exception.code, 2)

    def test_missing_ledger_does_not_initialize(self):
        (self.root / "ledger.sqlite").unlink()
        before = self.tree()
        with self.assertRaisesRegex(ValueError, "required file unavailable"):
            self.build()
        self.assertEqual(before, self.tree())

    def test_ledger_and_intake_wal_fail_without_side_effects(self):
        for database in (self.root / "ledger.sqlite", self.root / "intake" / "intake.sqlite"):
            with self.subTest(database=database.name):
                if not database.exists():
                    write_private(database, b"not opened")
                wal = Path(str(database) + "-wal")
                write_private(wal, b"synthetic pending WAL")
                before = self.tree()
                with self.assertRaisesRegex(adoption.AdoptionError, "wal_not_checkpointed"):
                    self.build()
                self.assertEqual(self.tree(), before)
                wal.unlink()

    def test_symlink_source_ledger_config_and_output_refused(self):
        source = self.mail / self.receipt["original_eml"]["path"]
        moved = source.with_name("held-source")
        source.rename(moved)
        source.symlink_to(moved)
        with self.assertRaises((OSError, ValueError)):
            self.build()
        source.unlink()
        moved.rename(source)
        ledger = self.root / "ledger.sqlite"
        held = self.root / "held-ledger"
        ledger.rename(held)
        ledger.symlink_to(held)
        with self.assertRaises((OSError, ValueError)):
            self.build()
        ledger.unlink()
        held.rename(ledger)
        target = self.base / "link.json"
        target.symlink_to(self.base / "new.json")
        with self.assertRaisesRegex(adoption.AdoptionError, "output_unsafe"):
            census.inventory(self.root, self.mail, target)
        cfg = self.cfg()
        link = self.base / "config-link"
        link.symlink_to(cfg)
        with self.assertRaisesRegex(adoption.AdoptionError, "config_invalid"):
            adoption.load_config(link)

    def test_output_inside_sources_git_or_nonprivate_parent_refused(self):
        public = self.base / "public"
        public.mkdir(mode=0o755)
        public.chmod(0o755)
        repo = self.base / "repo"
        repo.mkdir(mode=0o700)
        (repo / ".git").write_text("synthetic git marker")
        for target in (self.root / "inventory.json", self.mail / "inventory.json", public / "out", repo / "out"):
            with self.subTest(target=target), self.assertRaisesRegex(adoption.AdoptionError, "output_unsafe"):
                census.inventory(self.root, self.mail, target)
            self.assertFalse(target.exists())

    def test_changed_source_and_wrong_declared_part_refused(self):
        source = self.mail / self.receipt["attachments"][0]["path"]
        write_private(source, b"tampered")
        with self.assertRaisesRegex((ValueError, adoption.AdoptionError), "source_mismatch"):
            self.build()

    def test_genuine_eml_binding_and_eligibility_codes(self):
        eml = self.receipt["original_eml"]
        self.declare(eml["sha256"], eml["bytes"], "eml")
        entry = self.build()["originals"][eml["sha256"]]
        self.assertEqual(entry["proposal"]["binding_occurrence_id"], hid(["mail", "fixture-account", "Records", 900, 41]))
        self.assertIsNone(entry["eligibility_code"])
        with store.ledger(self.root / "ledger.sqlite") as con:
            con.execute("UPDATE stage_state SET status='blocked' WHERE original_sha256=? AND stage='extract'", (eml["sha256"],))
            con.commit()
        self.assertEqual(self.build()["originals"][eml["sha256"]]["eligibility_code"],
                         "original_resolution_requires_unaccepted_original")

    def test_wrong_part_cannot_invent_occurrence(self):
        receipt = json.loads(self.paths[0].read_bytes())
        receipt["attachments"][0]["part"] = 999
        write_private(self.paths[0], store.canonical(receipt).encode())
        before = self.tree()
        with self.assertRaisesRegex(adoption.AdoptionError, "occurrence_mismatch"):
            self.build()
        self.assertEqual(self.tree(), before)

    def test_schema_receipt_genuine_original_and_occurrences(self):
        from campaign_tool.records.intake.native_receipt import export as wire_export
        raw = (self.mail / self.receipt["original_eml"]["path"]).read_bytes()
        path = wire_export(raw, mail_root=self.mail, account="fixture-account", mailbox="Records",
                           uidvalidity=900, uid=99, original_format="eml")
        write_private(self.mail / "receipts" / "wire.json", path.read_bytes())
        entry = self.build()["originals"][self.subject]
        self.assertEqual(len(entry["receipts"]), 4)
        mid = hid(["mail", "fixture-account", "Records", 900, 99])
        wanted = sorted(hid(["attachment", mid, e["part"]]) for e in json.loads(path.read_bytes())["attachments"])
        self.assertIn(wanted, [r["occurrence_ids"] for r in entry["receipts"]])

    def test_failure_output_not_created_and_config_permissions_refused(self):
        target = self.base / "no-output.json"
        cfg = self.cfg(**{config.CURRENT: "a" * 64})
        with self.assertRaises(ValueError):
            census.inventory(self.root, self.mail, target, config_path=cfg)
        self.assertFalse(target.exists())
        cfg = self.cfg()
        cfg.chmod(0o644)
        with self.assertRaisesRegex(adoption.AdoptionError, "config_invalid"):
            adoption.load_config(cfg)

    def test_config_v1_v2_null_absent_are_unchanged(self):
        baseline = adoption.adopt(self.root, self.mail)
        for values in ({"schema": config.V1}, {}, {config.CURRENT: None}):
            path = self.cfg(**values)
            value, sha = adoption.load_config(path)
            self.assertEqual(sha, hashlib.sha256(path.read_bytes()).hexdigest())
            report = adoption.adopt(self.root, self.mail, config_path=path)
            # A configured open gate is the sole pre-existing difference.
            report["gate"] = baseline["gate"]
            self.assertEqual(report, baseline)
            self.assertIsNone(config.load_settings(self.root, path).policy)

    def test_missing_policy_mismatch_exit3_and_zero_writes(self):
        path = self.cfg(**{config.CURRENT: "f" * 64})
        before = self.tree()
        out = io.StringIO()
        with patch.object(sys, "argv", ["adopt-mail", "--root", str(self.root), "--mail-history", str(self.mail), "--config", str(path)]), contextlib.redirect_stdout(out):
            self.assertEqual(adoption.main(), 3)
        self.assertEqual(json.loads(out.getvalue())["resolution_policy"]["state"], "mismatch")
        with self.assertRaisesRegex(adoption.AdoptionError, "integration_required"):
            adoption.adopt(self.root, self.mail, config_path=path, apply=True)
        self.assertEqual(before, self.tree())

    def test_config_rejects_duplicate_keys_nonfinite_unknown_external_pins(self):
        path = self.cfg()
        for raw in (b'{"schema":"mail-history-adoption-v2","schema":"mail-history-adoption-v1"}',
                    b'{"schema":NaN}', b'{"schema":"mail-history-adoption-v2","source":"mail-history","adoption":"enabled","legacy_resolution_historical_policy_pins":"external.json"}'):
            write_private(path, raw)
            with self.assertRaisesRegex(adoption.AdoptionError, "config_invalid"):
                adoption.load_config(path)
        path = self.cfg(**{config.CURRENT: "A" * 64})
        with self.assertRaisesRegex(adoption.AdoptionError, "config_invalid"):
            adoption.load_config(path)

    def test_successor_fixed_binding_requires_independent_owner_pins(self):
        original = self.policy()
        backend = CanonicalMailBackend(self.mail, self.root / "intake", self.root / "ledger.sqlite", resolution_policy=original)
        identity = self.pipeline._identity()
        backend.start_run(identity)
        pin = json.loads(original.raw)["originals"][self.subject]["verification_receipt_sha256"]
        binding = next(path for path in self.paths if hashlib.sha256(path.read_bytes()).hexdigest() == pin)
        uid = json.loads(binding.read_bytes())["uid"]
        with root_lock(self.root):
            backend.preserve(binding, "fixture-account", Folder("Records", 900), uid)
        backend.finish_run(identity, "slice_completed", {"synthetic": True})
        successor_value = json.loads(original.raw)
        successor_value["approval_sha256"] = "3" * 64
        successor = self.save_policy(successor_value)
        for path in (None, self.cfg(**{config.CURRENT: successor.sha256})):
            with self.assertRaisesRegex(ValueError, "resolution_policy_mismatch"):
                self.build(config_path=path)
        pin_entry = {"policy_sha256": original.sha256, "introduced_at": "2026-01-01T00:00:00Z",
                     "provenance_sha256": "4" * 64, "originals": [self.subject]}
        path = self.cfg(**{config.CURRENT: successor.sha256, config.HISTORICAL: [pin_entry]})
        before = self.tree()
        entry = self.build(config_path=path)["originals"][self.subject]
        self.assertEqual(entry["state"], "resolved")
        self.assertEqual(entry["proposal"]["state"], "fixed")
        self.assertEqual(entry["proposal"]["verification_receipt_sha256"], pin)
        self.assertEqual(entry["proposal"]["binding_occurrence_id"], json.loads(original.raw)["originals"][self.subject]["binding_occurrence_id"])
        self.assertEqual(self.tree(), before)
        # Unpinning current write policy does not untrust independently pinned history.
        self.cfg(**{config.CURRENT: None, config.HISTORICAL: [pin_entry]})
        self.assertEqual(self.build(config_path=path)["originals"][self.subject]["state"], "resolved")
        os.chmod(path, 0o644)
        with self.assertRaises(ValueError):
            self.build(config_path=path)

    def test_policy_secure_read_and_historical_pin_validation(self):
        original = self.policy()
        path = self.cfg(**{config.CURRENT: original.sha256})
        loaded = config.load_settings(self.root, path).policy
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded.sha256, original.sha256)
        artifact = self.root / "resolution-policies" / original.sha256
        os.chmod(artifact, 0o644)
        with self.assertRaises(ValueError):
            config.load_settings(self.root, path)
        os.chmod(artifact, 0o600)
        held = self.base / "held-policy"
        artifact.rename(held)
        artifact.symlink_to(held)
        with self.assertRaises(ValueError):
            config.load_settings(self.root, path)
        artifact.unlink()
        held.rename(artifact)
        good = {"policy_sha256": original.sha256, "introduced_at": "2026-01-01T00:00:00Z",
                "provenance_sha256": "4" * 64, "originals": [self.subject]}
        for mutation in ({"originals": [self.subject, self.subject]}, {"introduced_at": "yesterday"},
                         {"provenance_sha256": None}, {"path": "outside.json"}):
            bad = dict(good, **mutation)
            path = self.cfg(**{config.HISTORICAL: [bad]})
            with self.assertRaisesRegex(adoption.AdoptionError, "config_invalid"):
                adoption.load_config(path)


if __name__ == "__main__":
    unittest.main()
