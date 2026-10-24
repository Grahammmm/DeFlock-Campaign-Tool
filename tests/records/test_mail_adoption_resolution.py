"""E25-37e: fixture-only real planner/backend integration; no owner acceptance."""
import contextlib
import copy
from email.message import EmailMessage
from email.policy import SMTP
import hashlib
import io
import json
import multiprocessing
import os
from pathlib import Path
import tempfile
import sqlite3
import shutil
import unittest
from unittest.mock import patch

from campaign_tool.records import mail_adoption as adoption, mail_resolution_census as census
from campaign_tool.records import mail_resolution_config as config
from campaign_tool.records.ledger import store
from campaign_tool.records.run import Pipeline, root_lock
from campaign_tool.records.runner.canonical_mail import CanonicalMailBackend
from campaign_tool.records.runner.core import hid
from tests.records import test_mail_resolution_census as fixtures
from tests.records import test_original_reconciliation as local_fixtures
from tests.records.test_mail_adoption import write_private

O1, O2, O3, X = (b"synthetic original one", b"synthetic original two", b"synthetic held three", b"synthetic absent attachment")


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


class World:
    def __init__(self, owner, baseline=False):
        if baseline:
            self.local = local_fixtures.OriginalReconciliationTests()
            self.local.setUp()
            owner.addCleanup(self.local.doCleanups)
            if baseline != "unresolved":
                self.local.enroll()
            self.root, self.base = self.local.base, self.local.base.parent
        else:
            tmp = tempfile.TemporaryDirectory()
            owner.addCleanup(tmp.cleanup)
            self.base = Path(tmp.name).resolve()
            self.root = self.base / "records"
            Pipeline(self.root)
        self.mail = self.base / "frozen-export"
        self.mail.mkdir(mode=0o700)
        (self.mail / "objects").mkdir(mode=0o700)
        (self.mail / "receipts").mkdir(mode=0o700)
        self.paths = {}
        self.cfg_path = self.base / "adoption.json"
        # The existing writer lock is part of the baseline, not a plan mutation.
        with root_lock(self.root):
            pass

    def export(self, uid, parts=(), nested=False):
        msg = EmailMessage()
        msg["Subject"] = "Synthetic fixture"
        msg.set_content(f"Synthetic message {uid}\n")
        for n, raw in enumerate(parts):
            msg.add_attachment(raw, maintype="application", subtype="octet-stream", filename=f"part-{n}.bin")
        if nested:
            inner = EmailMessage()
            inner.set_content("Synthetic nested message\n")
            msg.add_attachment(inner)
        if msg.is_multipart():
            msg.set_boundary(f"synthetic-fixture-boundary-{uid}")
        raw = msg.as_bytes(policy=SMTP)

        def blob(data):
            path = "objects/" + sha(data)
            write_private(self.mail / path, data)
            return {"path": path, "sha256": sha(data), "bytes": len(data)}

        attachments = []
        for index, part in enumerate(msg.walk()):
            if part.get_filename():
                entry = blob(part.get_payload(decode=True))
                entry.update(part=index, filename=part.get_filename(), original_filename=part.get_filename(),
                             content_type=part.get_content_type())
                attachments.append(entry)
        value = {"complete": True, "account_id": "fixture-account", "folder": "Records", "uidvalidity": 900,
                 "uid": uid, "bytes": len(raw), "original_eml": blob(raw), "attachments": attachments}
        path = self.mail / "receipts" / f"{uid}.json"
        write_private(path, store.canonical(value).encode())
        self.paths[uid] = path
        return value

    def declare(self, raw, form="txt"):
        fixtures.CensusTests.declare(self, sha(raw), len(raw), form)
        return sha(raw)

    def policy(self, held=()):
        inventory = census.build_inventory(self.root, self.mail)
        value = {"schema": "approved-mail-resolution-policy-v1", "inventory_sha256": sha(store.canonical(inventory).encode()),
                 "approval_sha256": "2" * 64, "held": sorted(held), "originals": {}}
        for subject, entry in inventory["originals"].items():
            if subject in held:
                continue
            proposal = entry["proposal"]
            value["originals"][subject] = {"bytes": entry["bytes"], "role": "original", "scope": "in_scope",
                "verification_receipt_sha256": proposal["verification_receipt_sha256"],
                "binding_occurrence_id": proposal["binding_occurrence_id"], "dependent_receipts": proposal["dependent_receipts"],
                "legacy_binding_sha256": entry["legacy_binding_sha256"]}
        return self.install(value)

    def install(self, value, historical=()):
        self.value = copy.deepcopy(value)
        self.current = fixtures.CensusTests.save_policy(self, value)
        cfg = {"schema": config.V2, "source": "mail-history", "adoption": "enabled",
               config.CURRENT: self.current.sha256, config.HISTORICAL: list(historical)}
        write_private(self.cfg_path, store.canonical(cfg).encode())
        return self.current

    def run(self, apply=False, **kwargs):
        return adoption.adopt(self.root, self.mail, config_path=self.cfg_path, apply=apply, **kwargs)

    def tree(self):
        return fixtures.CensusTests.tree(self)

    def rows(self, sql, args=()):
        with adoption._snapshot_ledger(self.root / "ledger.sqlite") as con:
            return [tuple(row) for row in con.execute(sql, args)]

    def receipt(self, digest):
        return next(uid for uid, path in self.paths.items() if sha(path.read_bytes()) == digest)

    def chain(self):
        self.export(1, (O1,))
        self.export(2, (O1, O2))
        self.export(3, (O2,))
        self.declare(O1)
        self.declare(O2)
        self.policy()
        value = self.value
        for raw, uid, dependents in ((O1, 1, [2]), (O2, 2, [3])):
            entry = value["originals"][sha(raw)]
            item = next(r for r in adoption.discover(self.mail, self.root / "intake")[0] if r["identity"][-1] == str(uid))
            records = census.receipt_occurrences(item, self.mail, self.root / "intake")
            entry.update(verification_receipt_sha256=sha(self.paths[uid].read_bytes()),
                         binding_occurrence_id=min(r[0] for r in records if r[1] == sha(raw)),
                         dependent_receipts=sorted(sha(self.paths[u].read_bytes()) for u in dependents))
        self.install(value)

    def backend(self, calls, fail=None):
        class Backend(CanonicalMailBackend):
            def preserve(inner, path, account, folder, uid):
                calls.append(uid)
                if uid == fail:
                    raise ValueError("synthetic_failure")
                return super().preserve(path, account, folder, uid)
        return Backend


def crash_after_commit(root, mail, cfg):
    def crash(*args, **kwargs):
        os._exit(73)
    with patch("campaign_tool.records.runner.canonical_mail.installed_preservation_runner", crash):
        adoption.adopt(root, mail, config_path=cfg, apply=True)


def leave_wal(root):
    con = sqlite3.connect(Path(root) / "ledger.sqlite")
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("UPDATE runs SET summary=? WHERE run_id='synthetic'", ('{"synthetic_wal":true}',))
    con.commit()
    os._exit(74)


class ResolutionTests(unittest.TestCase):
    def world(self, **kwargs):
        return World(self, **kwargs)

    def invariants(self, r):
        self.assertEqual(r["receipts"], r["already_adopted"] + r["pending_messages"] + r["coded_gap_messages"] + sum(r["identity_conflicts"].values()))
        self.assertEqual(r["new_messages"], r["eligible_new_messages"] + r["resolvable_messages"] + r["coded_gap_messages"])
        self.assertEqual(r["policy_originals"], r["policy_originals_resolved"] + r["resolvable_originals"] + r["policy_originals_blocked"])
        self.assertEqual(r["resolvable_messages"], r["resolvable_binding_messages"] + r["resolvable_dependent_messages"])
        if "after" in r:
            a = r["after"]
            self.assertEqual(a["policy_originals"], a["policy_originals_resolved"] + a["resolvable_originals"] + a["policy_originals_blocked"])

    def blocked(self, w, code=None):
        before = w.tree()
        r = w.run()
        self.invariants(r)
        self.assertTrue(adoption.blocking(r))
        self.assertEqual(r["resolution_plan"], {"state": "blocked"})
        if code:
            self.assertIn(code, {**r["resolution_drift"], **r["coded_gaps"]})
        with self.assertRaisesRegex(adoption.AdoptionError, "^resolution_plan_blocked$"):
            w.run(True)
        self.assertEqual(before, w.tree())
        return r

    def shared(self):
        w = self.world()
        w.export(1, (O1,))
        w.export(2, (O1,))
        w.declare(O1)
        w.policy()
        return w

    def test_e25_26_27_31_34_duplicate_prediction_purity_rerun(self):
        w = self.world()
        w.export(1, (O1, O1))
        w.declare(O1)
        w.policy()
        before = w.tree()
        dry = w.run()
        self.assertEqual(before, w.tree())
        self.assertEqual((dry["resolvable_messages"], dry["resolvable_originals"], dry["resolvable_attachments"]), (1, 1, 2))
        applied = w.run(True)
        self.assertFalse(adoption.blocking(applied), applied)
        for key in dry.keys() - {"mode", "resolution_after", "resolved_originals", "adopted_messages", "adopted_attachments"}:
            self.assertEqual(dry[key], applied[key], key)
        self.assertEqual(applied["resolved_originals"], dry["resolvable_originals"])
        self.assertEqual(applied["adopted_messages"], dry["resolvable_messages"])
        self.assertEqual(applied["resolution_after"]["rows"], 1)
        self.invariants(dry)
        self.invariants(applied)
        before = w.tree()
        for apply in (False, True):
            rerun = w.run(apply)
            self.assertEqual((rerun["adopted_messages"], rerun["resolved_originals"]), (0, 0))
            self.assertEqual(rerun["resolution_after"], applied["resolution_after"])
            self.assertFalse(adoption.blocking(rerun), rerun)
        self.assertEqual(before, w.tree())
        self.assertEqual(w.rows("SELECT count(*) FROM occurrences WHERE original_sha256=? AND acquisition_method='verified_mail_export'", (sha(O1),)), [(2,)])

    def test_e35_both_discovery_orders_and_filename_order(self):
        w = self.shared()
        receipts, gaps = adoption.discover(w.mail, w.root / "intake")
        dry = w.run()
        with patch.object(adoption, "discover", return_value=(list(reversed(receipts)), gaps)):
            self.assertEqual(w.run(), dry)
        binding = w.receipt(w.value["originals"][sha(O1)]["verification_receipt_sha256"])
        # File names force opposite discovery order; bytes and identities unchanged.
        for uid, path in list(w.paths.items()):
            renamed = path.with_name("a.json" if uid != binding else "z.json")
            path.rename(renamed)
            w.paths[uid] = renamed
        self.assertEqual(w.run(), dry)
        calls = []
        applied = w.run(True, backend_factory=w.backend(calls))
        self.assertEqual(calls, [binding, 3 - binding])
        self.assertEqual(applied["resolved_originals"], 1)
        self.assertEqual((dry["resolvable_binding_messages"], dry["resolvable_dependent_messages"]), (1, 1))

    def test_e28_rfc822_dependent_and_manual_binding(self):
        w = self.world()
        w.export(1, (O1,))
        w.export(2, (O1,), nested=True)
        w.declare(O1)
        w.policy()
        self.assertEqual(w.value["originals"][sha(O1)]["dependent_receipts"], [sha(w.paths[2].read_bytes())])
        dry = w.run()
        self.assertEqual(dry["coded_gaps"], {"unsupported_rfc822_part": 1})
        self.assertEqual(w.run(True)["resolved_originals"], 1)
        # Fresh fixture with MIME-ineligible binding: P blocks despite MIME-first code.
        other = self.world()
        other.export(1, (O1,), nested=True)
        other.declare(O1)
        inventory = census.build_inventory(other.root, other.mail)
        item = adoption.discover(other.mail, other.root / "intake")[0][0]
        oid = next(r[0] for r in census.receipt_occurrences(item, other.mail, other.root / "intake") if r[1] == sha(O1))
        value = copy.deepcopy(w.value)
        value["originals"][sha(O1)].update(verification_receipt_sha256=item["receipt_sha256"], binding_occurrence_id=oid, dependent_receipts=[],
            legacy_binding_sha256=inventory["originals"][sha(O1)]["legacy_binding_sha256"])
        other.install(value)
        self.assertEqual(self.blocked(other)["policy_originals_blocked"], 1)

    def test_e35_rebuilt_fixture_produces_identical_rows_and_reports(self):
        w = self.shared()
        initial = w.base / "initial-synthetic-root"
        shutil.copytree(w.root, initial)
        receipts, gaps = adoption.discover(w.mail, w.root / "intake")
        with patch("campaign_tool.records.runner.canonical_mail.datetime") as clock:
            clock.now.return_value.isoformat.return_value = "2026-01-02T03:04:05+00:00"
            first = w.run(True)
        resolution_rows = w.rows("SELECT * FROM original_resolutions ORDER BY original_sha256")
        occurrences = w.rows("SELECT * FROM occurrences ORDER BY id")
        # Preserve the whole first result, rebuilding at the SAME absolute root
        # so stored path evidence is comparable without normalizing any columns.
        w.root.rename(w.base / "first-synthetic-result")
        shutil.copytree(initial, w.root)
        with patch.object(adoption, "discover", return_value=(list(reversed(receipts)), gaps)), patch(
                "campaign_tool.records.runner.canonical_mail.datetime") as clock:
            clock.now.return_value.isoformat.return_value = "2026-01-02T03:04:05+00:00"
            second = w.run(True)
        self.assertEqual({k: v for k, v in first.items() if k != "run_id"},
                         {k: v for k, v in second.items() if k != "run_id"})
        self.assertEqual(w.rows("SELECT * FROM original_resolutions ORDER BY original_sha256"), resolution_rows)
        self.assertEqual(w.rows("SELECT * FROM occurrences ORDER BY id"), occurrences)

    def test_e29_missing_coverage_and_held_accepted(self):
        w = self.shared()
        value = copy.deepcopy(w.value)
        value["originals"] = {}
        w.install(value)
        self.blocked(w, "resolution_not_in_policy")
        value["held"] = [sha(O1)]
        w.install(value)
        r = w.run(True)
        self.assertEqual(r["coded_gaps"], {"original_scope_unresolved": 2})
        self.assertEqual(r["held_originals"], 1)
        self.assertFalse(adoption.blocking(r))

    def test_e30_v1_null_and_missing_policy(self):
        w = self.shared()
        for cfg in ({"schema": config.V1}, {"schema": config.V2, config.CURRENT: None}):
            cfg.update(source="mail-history", adoption="enabled")
            write_private(w.cfg_path, store.canonical(cfg).encode())
            self.assertEqual(w.run()["coded_gaps"], {"original_scope_unresolved": 2})
        cfg[config.CURRENT] = "f" * 64
        write_private(w.cfg_path, store.canonical(cfg).encode())
        before = w.tree()
        self.assertEqual(w.run()["resolution_policy"]["state"], "mismatch")
        with self.assertRaisesRegex(adoption.AdoptionError, "^resolution_policy_mismatch$"):
            w.run(True)
        self.assertEqual(before, w.tree())

    def test_e32_root_lock_refuses(self):
        w = self.shared()
        with root_lock(w.root):
            before = w.tree()
            with self.assertRaisesRegex(RuntimeError, "root_locked"):
                w.run(True)
            self.assertEqual(before, w.tree())

    def test_e36a_receipt_set_drift_includes_unrelated(self):
        w = self.shared()
        w.export(3, (O1,))
        w.export(4)
        r = self.blocked(w, "resolution_policy_receipt_set_mismatch")
        self.assertEqual(r["resolution_drift"], {"resolution_policy_receipt_set_mismatch": 1})
        self.assertEqual(r["eligible_new_messages"], 1)
        self.assertEqual(r["coded_gaps"]["resolution_policy_receipt_set_mismatch"], 3)

    def test_e36b_held_binding_blocks_dependents(self):
        w = self.world()
        w.chain()
        # O2 held in R2: R2 cannot bind an additional policy original (its EML).
        raw = (w.mail / json.loads(w.paths[2].read_bytes())["original_eml"]["path"]).read_bytes()
        w.declare(raw, "eml")
        w.policy(held=[sha(O2)])
        self.blocked(w, "resolution_binding_receipt_blocked")

    def test_e36c_cycle_and_unrelated_zero_mutation(self):
        w = self.world()
        w.export(1, (O1, O2))
        w.export(2, (O1, O2))
        w.export(4)
        w.declare(O1)
        w.declare(O2)
        w.policy()
        for raw, uid, dep in ((O1, 1, 2), (O2, 2, 1)):
            item = next(r for r in adoption.discover(w.mail, w.root / "intake")[0] if r["identity"][-1] == str(uid))
            oid = next(r[0] for r in census.receipt_occurrences(item, w.mail, w.root / "intake") if r[1] == sha(raw))
            w.value["originals"][sha(raw)].update(verification_receipt_sha256=item["receipt_sha256"], binding_occurrence_id=oid,
                dependent_receipts=[sha(w.paths[dep].read_bytes())])
        w.install(w.value)
        r = self.blocked(w, "resolution_dependency_cycle")
        self.assertEqual(r["resolution_drift"], {"resolution_dependency_cycle": 1})

    def test_e36d_failure_suppresses_dependent_unrelated_adopted(self):
        w = self.shared()
        w.export(4)
        binding = w.receipt(w.value["originals"][sha(O1)]["verification_receipt_sha256"])
        calls = []
        r = w.run(True, backend_factory=w.backend(calls, binding))
        self.assertEqual(calls, [4, binding])
        self.assertEqual(r["failures"].get("resolution_dependency_failed"), 1)
        self.assertEqual(r["adopted_messages"], 1)
        self.assertEqual(r["resolved_originals"], 0)
        self.assertEqual(w.run(True)["resolved_originals"], 1)

    def test_e37_chain_topological_and_transitive_suppression(self):
        w = self.world()
        w.chain()
        calls = []
        r = w.run(True, backend_factory=w.backend(calls, 1))
        self.assertEqual(calls, [1])
        self.assertEqual(r["failures"]["resolution_dependency_failed"], 2)
        calls = []
        dry = w.run()
        r = w.run(True, backend_factory=w.backend(calls))
        self.assertEqual(calls, [1, 2, 3])
        self.assertEqual(r["resolved_originals"], dry["resolvable_originals"])
        self.assertEqual(r["resolved_originals"], 2)

    def test_e37a_partial_chain_preserves_original_binding(self):
        w = self.world()
        w.chain()
        calls = []
        first = w.run(True, backend_factory=w.backend(calls, 2))
        self.assertEqual(calls, [1, 2])
        self.assertEqual(first["resolved_originals"], 1)
        prior = w.rows("SELECT * FROM original_resolutions")
        dry = w.run()
        self.assertEqual((dry["policy_originals_resolved"], dry["resolvable_originals"], dry["policy_originals_blocked"]), (1, 1, 0))
        self.assertFalse(adoption.blocking(dry), dry)
        calls = []
        second = w.run(True, backend_factory=w.backend(calls))
        self.assertEqual(calls, [2, 3])
        self.assertEqual(second["resolved_originals"], 1)
        self.assertIn(prior[0], w.rows("SELECT * FROM original_resolutions"))
        self.assertEqual(w.run(True)["resolved_originals"], 0)

    def test_e37c_resolved_drift_checks_all_complete_receipts(self):
        w = self.shared()
        w.run(True)
        row = w.rows("SELECT * FROM original_resolutions")
        w.export(3, (O1,))
        w.export(4)
        r = self.blocked(w, "resolution_policy_receipt_set_mismatch")
        self.assertEqual(r["already_adopted"], 2)
        self.assertEqual(r["policy_originals_resolved"], 1)
        self.assertEqual(row, w.rows("SELECT * FROM original_resolutions"))
        # Removing a complete dependent is also drift, not an empty new-work pass.
        w.paths[3].unlink()
        dep = w.value["originals"][sha(O1)]["dependent_receipts"][0]
        w.paths[w.receipt(dep)].unlink()
        self.blocked(w, "resolution_policy_receipt_set_mismatch")

    def test_e37b_postcommit_chain_recovers_dead_run_before_dependents(self):
        w = self.world()
        w.chain()
        process = multiprocessing.get_context("fork").Process(target=crash_after_commit, args=(w.root, w.mail, w.cfg_path))
        process.start()
        process.join(20)
        self.assertEqual(process.exitcode, 73)
        prior = w.rows("SELECT * FROM original_resolutions")
        self.assertEqual(len(prior), 1)
        dry = w.run()
        self.assertEqual((dry["incomplete_messages"], dry["unfinalized_runs"]), (1, 1))
        self.assertEqual((dry["policy_originals_resolved"], dry["resolvable_originals"], dry["policy_originals_blocked"]), (1, 1, 0))
        self.assertEqual(dry["resolution_plan"], {"state": "ok"})
        self.assertTrue(adoption.blocking(dry))
        calls = []
        report = w.run(True, backend_factory=w.backend(calls))
        self.assertEqual(calls, [1, 2, 3])
        self.assertEqual(report["recovered_runs"], {"finalized": 1})
        self.assertEqual((report["resumed_messages"], report["adopted_messages"], report["resolved_originals"]), (1, 2, 1))
        self.assertEqual(report["resolution_after"]["rows"], 2)
        self.assertIn(prior[0], w.rows("SELECT * FROM original_resolutions"))
        self.assertFalse(adoption.blocking(report), report)

    def test_e37e_v1_resolution_after_census_blocks_without_rewrite(self):
        w = self.world(baseline="unresolved")
        w.export(1, (b"synthetic original",))
        w.export(4)
        w.policy()
        w.local.enroll()
        prior = w.rows("SELECT * FROM original_resolutions")
        r = self.blocked(w, "resolution_existing_binding_mismatch")
        self.assertEqual(r["policy_originals_blocked"], 1)
        self.assertEqual(prior, w.rows("SELECT * FROM original_resolutions"))

    def test_blocked_plan_precedes_wal_settlement_and_dead_run_recovery(self):
        w = self.shared()
        w.export(3, (O1,))
        w.export(4)
        process = multiprocessing.get_context("fork").Process(target=leave_wal, args=(w.root,))
        process.start()
        process.join(20)
        self.assertEqual(process.exitcode, 74)
        self.assertTrue(adoption._wal_pending(w.root / "ledger.sqlite"))
        before = w.tree()
        with self.assertRaisesRegex(adoption.AdoptionError, "resolution_plan_blocked"):
            w.run(True)
        self.assertEqual(before, w.tree())

    def test_blocked_clean_plan_does_not_even_create_lock_file(self):
        w = self.shared()
        w.export(3, (O1,))
        (w.root / "run.lock").unlink()
        self.blocked(w, "resolution_policy_receipt_set_mismatch")

    def test_all_complete_coverage_and_held_drift_still_block(self):
        for mode, code in (("omitted", "resolution_not_in_policy"), ("held", "resolution_held_original_resolved")):
            with self.subTest(mode=mode):
                w = self.shared()
                old = w.current
                w.run(True)
                value = copy.deepcopy(w.value)
                value["originals"] = {}
                if mode == "held": value["held"] = [sha(O1)]
                pins = [{"policy_sha256": old.sha256, "introduced_at": "2026-01-01T00:00:00Z", "provenance_sha256": "4" * 64, "originals": [sha(O1)]}]
                w.install(value, pins)
                r = self.blocked(w, code)
                self.assertEqual(r["already_adopted"], 2)
                self.assertEqual(r["coded_gaps"], {})

    def test_e37d_successor_preserves_first_policy_and_negative_variants(self):
        w = self.shared()
        original = w.current
        w.run(True)
        row = w.rows("SELECT * FROM original_resolutions")
        w.export(3, (O1,))
        pins = [{"policy_sha256": original.sha256, "introduced_at": "2026-01-01T00:00:00Z", "provenance_sha256": "4" * 64, "originals": [sha(O1)]}]
        successor = copy.deepcopy(w.value)
        successor["originals"][sha(O1)]["dependent_receipts"].append(sha(w.paths[3].read_bytes()))
        successor["originals"][sha(O1)]["dependent_receipts"].sort()
        for mode, code in (("binding", "resolution_existing_binding_mismatch"), ("occurrence", "resolution_existing_binding_mismatch"),
                           ("omitted", "resolution_not_in_policy"), ("held", "resolution_held_original_resolved")):
            value = copy.deepcopy(successor)
            if mode == "binding":
                entry = value["originals"][sha(O1)]
                old = entry["verification_receipt_sha256"]
                entry["verification_receipt_sha256"] = entry["dependent_receipts"].pop()
                entry["dependent_receipts"] = sorted([old, *entry["dependent_receipts"]])
            elif mode == "occurrence":
                value["originals"][sha(O1)]["binding_occurrence_id"] = "f" * 64
            else:
                value["originals"] = {}
                if mode == "held": value["held"] = [sha(O1)]
            w.install(value, pins)
            self.blocked(w, code)
        w.install(successor, pins)
        dry = w.run()
        self.assertEqual(dry["eligible_new_messages"], 1)
        r = w.run(True)
        self.assertEqual((r["adopted_messages"], r["resolved_originals"]), (1, 0))
        self.assertEqual(w.rows("SELECT * FROM original_resolutions"), row)
        self.assertEqual(row[0][3], original.sha256)

    def test_d24_locked_policy_and_config_reload_no_mutation(self):
        w = self.shared()
        real_lock = root_lock
        before = w.tree()
        @contextlib.contextmanager
        def changed(root):
            with real_lock(root):
                artifact = w.root / "resolution-policies" / w.current.sha256
                raw = artifact.read_bytes()
                write_private(artifact, b"changed synthetic policy")
                try: yield
                finally: write_private(artifact, raw)
        with patch("campaign_tool.records.run.root_lock", changed):
            with self.assertRaisesRegex(adoption.AdoptionError, "resolution_policy_changed"):
                w.run(True)
        # The fixture changed policy itself; ledger/export/intake stayed identical.
        after = w.tree()
        for path in before:
            if "resolution-policies" not in path:
                self.assertEqual(before[path], after[path], path)

    def test_historical_pin_must_admit_subject(self):
        w = self.shared()
        pin = {"policy_sha256": w.current.sha256, "introduced_at": "2026-01-01T00:00:00Z", "provenance_sha256": "4" * 64, "originals": ["f" * 64]}
        w.install(w.value, [pin])
        with self.assertRaisesRegex(ValueError, "resolution_policy_mismatch"):
            config.load_settings(w.root, w.cfg_path)


if __name__ == "__main__":
    unittest.main()
