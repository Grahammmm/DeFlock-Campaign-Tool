"""T4B slice 3: adopt-mail resolution plan, report, gate and census (synthetic only).

Covers test group E of docs/RECORDS-LEGACY-MAIL-RESOLUTION.md (see docs/reviews for the
numbering of what is covered). Fictional bytes, no mailbox, no network.
"""
import json
import os
import unittest
from unittest import mock

from campaign_tool.records import mail_adoption
from campaign_tool.records.intake import mail_resolution_plan
from campaign_tool.records.runner.canonical_mail import CanonicalMailBackend
from tests.records.mail_adoption_resolution_fixtures import ResolutionAdoptionCase, pdf
from tests.records.mail_resolution_fixtures import sha


class FailingFirst(CanonicalMailBackend):
    """Raise on chosen receipt paths, as an apply-time fault would."""
    fail = set()

    def preserve(self, receipt_path, *args):
        if str(receipt_path) in self.fail:
            raise RuntimeError("synthetic fault")
        return super().preserve(receipt_path, *args)


def faulty(paths):
    cls = type("Faulty", (FailingFirst,), {"fail": {str(p) for p in paths}})
    return lambda *args, **kw: cls(*args, **kw)


class DuplicatePartsEndToEnd(ResolutionAdoptionCase):
    def setUp(self):
        super().setUp()
        self.r = self.mk(11, ["dup", "dup"])  # two byte-identical attachment parts: one distinct original
        self.o = self.r.atts[0]
        self.declare_legacy(self.o)
        self.build()
        ids = self.occ(self.r, self.o["sha256"])
        self.assertEqual(len(ids), 2)
        self.pin({self.o["sha256"]: self.entry_for(self.o, self.r, ids[0])})

    def test_25_26_27_34_dry_run_predicts_apply_and_rerun_is_a_no_op(self):
        before = self.state()
        dry = self.adopt()
        self.assertEqual(self.state(), before)  # 26: a dry run writes nothing
        self.invariants(dry)
        self.assertEqual((dry["resolvable_messages"], dry["resolvable_originals"], dry["resolvable_attachments"],
                          dry["eligible_new_messages"], dry["coded_gap_messages"]), (1, 1, 2, 0, 0))
        self.assertEqual((dry["resolution_plan"], dry["resolution_drift"]), ({"state": "ok"}, {}))
        self.assertEqual(dry["resolution_policy"]["state"], "loaded")
        self.assertFalse(mail_adoption.blocking(dry))
        applied = self.adopt(apply=True)
        self.invariants(applied)
        self.assertEqual((applied["adopted_messages"], applied["resolved_originals"], applied["failures"]),
                         (dry["resolvable_messages"], dry["resolvable_originals"], {}))
        self.assertEqual(applied["resolution_after"]["rows"], applied["resolution_baseline"]["rows"] + 1)
        self.assertEqual(applied["after"]["resolvable_originals"], 0)
        self.assertEqual(applied["after"]["policy_originals_resolved"], 1)
        self.assertFalse(mail_adoption.blocking(applied))
        row = self.resolution_rows()
        self.assertEqual(len(row), 1)
        # Rerun, dry and apply: 0 adopted, 0 resolved, nothing written, same resolution digest.
        settled = self.state()
        for again in (self.adopt(), self.adopt(apply=True)):
            self.invariants(again)
            self.assertEqual((again["adopted_messages"], again["resolved_originals"], again["resolvable_originals"],
                              again["policy_originals_resolved"], again["policy_originals_blocked"],
                              again["resolution_drift"], again["resolution_plan"]),
                             (0, 0, 0, 1, 0, {}, {"state": "ok"}))
            self.assertEqual(again["resolution_baseline"], applied["resolution_after"])
            self.assertFalse(mail_adoption.blocking(again))
        self.assertEqual(self.resolution_rows(), row)

    def test_24_policy_changed_between_load_and_lock_is_refused(self):
        other = self.write_policy({self.o["sha256"]: self.entry_for(self.o, self.r, self.occ(self.r, self.o["sha256"])[1])})
        good = mail_adoption._load_policy(self.root, json.loads(self.cfg.read_bytes()))
        swapped = mail_adoption._load_policy(self.root, {"legacy_resolution_policy_sha256": other})
        before = self.state()
        with mock.patch.object(mail_adoption, "_load_policy", side_effect=[good, swapped]):
            with self.assertRaisesRegex(mail_adoption.AdoptionError, "^resolution_policy_changed$"):
                self.adopt(apply=True)
        self.assertEqual(self.state(), before)


class ConfigAndPolicyGate(ResolutionAdoptionCase):
    def test_30_v1_and_null_v2_mean_no_resolution_and_missing_policy_blocks(self):
        r = self.mk(12, ["one"])
        self.declare_legacy(r.atts[0])
        self.build()
        policy = self.pin({r.atts[0]["sha256"]: self.entry_for(r.atts[0], r, self.occ(r, r.atts[0]["sha256"])[0])})
        self.assertEqual(self.adopt()["resolvable_messages"], 1)
        for v2, pin in ((False, None), (True, None)):
            self.write_config(pin, v2=v2)
            report = self.adopt()
            self.assertEqual((report["resolvable_messages"], report["resolution_policy"]),
                             (0, {"state": "none", "sha256": None}))
            self.assertEqual(report["coded_gaps"], {"original_scope_unresolved": 1})
            self.assertEqual(report["unaccepted_coded_gaps"], {})
        self.write_config("e" * 64)  # pinned but absent
        before = self.state()
        report = self.adopt()
        self.assertEqual(report["resolution_policy"], {"state": "mismatch", "sha256": "e" * 64})
        self.assertTrue(mail_adoption.blocking(report))
        with self.assertRaisesRegex(mail_adoption.AdoptionError, "^resolution_policy_mismatch$"):
            self.adopt(apply=True)
        self.assertEqual(self.state(), before)
        self.assertIsNotNone(policy)

    def test_v1_config_refuses_the_policy_key(self):
        config = {"schema": mail_adoption.CONFIG_SCHEMA, "source": "mail-history", "adoption": "enabled",
                  "disable_marker_sha256": None, "legacy_resolution_policy_sha256": None}
        self.cfg.unlink()
        from tests.records.test_mail_adoption import write_private
        write_private(self.cfg, json.dumps(config).encode())
        with self.assertRaisesRegex(mail_adoption.AdoptionError, "^adoption_config_invalid$"):
            self.adopt()


class Recorder:
    """Backend factory that records the order of preserve calls."""

    def __init__(self, fail=()):
        self.calls, self.fail = [], {str(p) for p in fail}

    def __call__(self, *args, **kw):
        recorder = self

        class Recording(CanonicalMailBackend):
            def preserve(self, receipt_path, *rest):
                recorder.calls.append(str(receipt_path))
                if str(receipt_path) in recorder.fail:
                    raise RuntimeError("synthetic fault")
                return super().preserve(receipt_path, *rest)
        return Recording(*args, **kw)


class SharedOriginal(ResolutionAdoptionCase):
    """O is in R1 and R2; the policy binds it to the smaller receipt digest."""

    def setUp(self):
        super().setUp()
        self.a, self.b = self.mk(21, ["shared"]), self.mk(22, ["shared"])
        self.lo, self.hi = sorted((self.a, self.b), key=lambda r: r.digest)
        self.o = self.lo.atts[0]
        self.ra = self.mk(23, ["unrelated eligible"])
        self.declare_legacy(self.o)
        self.build()

    def shared_entry(self):
        return self.entry_for(self.o, self.lo, self.occ(self.lo, self.o["sha256"])[0], [self.hi])

    def pin_shared(self, **extra):
        return self.pin({self.o["sha256"]: self.shared_entry()}, **extra)

    def test_35_both_receipts_adopt_in_order_with_one_row_naming_the_binding_receipt(self):
        self.pin_shared()
        dry = self.adopt()
        self.invariants(dry)
        self.assertEqual((dry["resolvable_binding_messages"], dry["resolvable_dependent_messages"],
                          dry["resolvable_originals"], dry["eligible_new_messages"]), (1, 1, 1, 1))
        recorder = Recorder()
        applied = self.adopt(apply=True, backend_factory=recorder)
        self.invariants(applied)
        self.assertEqual((applied["adopted_messages"], applied["resolved_originals"], applied["failures"]), (3, 1, {}))
        order = [c for c in recorder.calls if c in (str(self.lo.path), str(self.hi.path))]
        self.assertEqual(order, [str(self.lo.path), str(self.hi.path)])
        rows = self.rows("SELECT original_sha256,capture_receipt_sha256 FROM original_resolutions")
        self.assertEqual(rows, [(self.o["sha256"], self.lo.digest)])
        rerun = self.adopt(apply=True)
        self.assertEqual((rerun["adopted_messages"], rerun["resolved_originals"], rerun["resolvable_originals"],
                          rerun["policy_originals_resolved"]), (0, 0, 0, 1))

    def test_35_result_does_not_depend_on_discovery_order(self):
        self.pin_shared()
        forward = self.adopt()
        real = mail_adoption.discover

        def reversed_discover(*args):
            found, gaps = real(*args)
            return list(reversed(found)), gaps
        with mock.patch.object(mail_adoption, "discover", reversed_discover):
            backward = self.adopt()
        self.assertEqual(forward, backward)

    def refused_with_nothing_written(self, code="resolution_plan_blocked"):
        before = self.state()
        with self.assertRaisesRegex(mail_adoption.AdoptionError, "^%s$" % code):
            self.adopt(apply=True)
        self.assertEqual(self.state(), before)  # Ra (unrelated eligible) is not adopted either

    def test_36a_receipt_not_named_in_the_policy_blocks_everything(self):
        r3 = self.mk(24, ["shared"])
        self.pin_shared()
        dry = self.adopt()
        self.assertEqual(dry["resolution_drift"], {"resolution_policy_receipt_set_mismatch": 1})
        self.assertEqual(dry["coded_gaps"], {"resolution_policy_receipt_set_mismatch": 3})
        self.assertEqual((dry["resolvable_messages"], dry["resolution_plan"]), (0, {"state": "blocked"}))
        self.assertTrue(mail_adoption.blocking(dry))
        self.refused_with_nothing_written()
        self.assertIsNotNone(r3)

    def test_36d_fault_in_the_binding_receipt_skips_its_dependent(self):
        self.pin_shared()
        recorder = Recorder(fail=[self.lo.path])
        applied = self.adopt(apply=True, backend_factory=recorder)
        self.assertEqual(applied["failures"], {"stage_or_input_rejected": 1, "resolution_dependency_failed": 1})
        self.assertEqual(applied["adopted_messages"], 1)  # only Ra
        self.assertNotIn(str(self.hi.path), recorder.calls)
        self.assertEqual(self.resolution_rows(), [])
        self.assertEqual(self.rows("SELECT count(*) FROM mail_messages"), [(1,)])
        again = self.adopt(apply=True)
        self.assertEqual((again["adopted_messages"], again["resolved_originals"], again["failures"]), (2, 1, {}))

    def test_37c_37d_receipt_added_after_resolution_and_successor_policy(self):
        self.pin_shared()
        self.adopt(apply=True)
        row = self.resolution_rows()
        r3 = self.mk(26, ["shared"])
        rb = self.mk(27, ["another unrelated eligible"])
        dry = self.adopt()
        self.assertEqual(dry["resolution_drift"], {"resolution_policy_receipt_set_mismatch": 1})
        self.assertEqual((dry["already_adopted"], dry["policy_originals_resolved"], dry["resolution_plan"]),
                         (3, 1, {"state": "blocked"}))
        self.assertEqual(dry["coded_gaps"], {"resolution_policy_receipt_set_mismatch": 1})
        self.assertTrue(mail_adoption.blocking(dry))
        before = self.state()
        with self.assertRaisesRegex(mail_adoption.AdoptionError, "^resolution_plan_blocked$"):
            self.adopt(apply=True)
        self.assertEqual(self.state(), before)
        self.assertEqual(self.resolution_rows(), row)
        # The backend copy of the check refuses a direct preserve of the unlisted receipt.
        policy, _ = mail_adoption._load_policy(self.root, json.loads(self.cfg.read_bytes()))
        backend = CanonicalMailBackend(self.export, self.root / "intake", self.database, resolution_policy=policy)
        backend.start_run(mail_adoption._identity(None))
        with self.assertRaisesRegex(ValueError, "^canonical_original_binding_conflict$"):
            self.preserve(backend, r3.path, r3.uid)
        # 37d: a successor policy carrying the same binding and naming R3 adopts it, with no new row.
        entry = self.entry_for(self.o, self.lo, self.occ(self.lo, self.o["sha256"])[0], [self.hi, r3])
        self.pin({self.o["sha256"]: entry})
        ok = self.adopt()
        self.assertEqual((ok["resolution_plan"], ok["eligible_new_messages"], ok["policy_originals_resolved"]),
                         ({"state": "ok"}, 2, 1))
        done = self.adopt(apply=True)
        self.assertEqual((done["adopted_messages"], done["resolved_originals"], done["failures"]), (2, 0, {}))
        self.assertEqual(self.resolution_rows(), row)
        self.assertIsNotNone(rb)
        # Variants that block the whole apply.
        wrong = self.entry_for(self.o, self.hi, self.occ(self.hi, self.o["sha256"])[0], [self.lo, r3])
        self.pin({self.o["sha256"]: wrong})
        report = self.adopt()
        self.assertEqual((report["policy_originals_blocked"], report["resolution_plan"]), (1, {"state": "blocked"}))
        self.refused_with_nothing_written()
        self.pin({}, held=[sha("unrelated")])  # omits O
        report = self.adopt()
        self.assertEqual(report["resolution_drift"].get("resolution_not_in_policy"), 1)
        self.assertEqual(report["resolution_plan"], {"state": "blocked"})
        self.pin({}, held=[self.o["sha256"]])
        report = self.adopt()
        self.assertEqual(report["resolution_drift"].get("resolution_held_original_resolved"), 1)
        self.assertEqual(self.resolution_rows(), row)


class BindingBlocked(ResolutionAdoptionCase):
    def test_36b_binding_receipt_with_a_held_original_blocks(self):
        a, b = self.mk(31, ["shared", "held original"]), self.mk(32, ["shared"])
        ra = self.mk(33, ["unrelated eligible"])
        o, held = a.atts[0], a.atts[1]
        self.assertEqual(o["sha256"], b.atts[0]["sha256"])
        self.declare_legacy(o, held)
        self.build()
        self.pin({o["sha256"]: self.entry_for(o, a, self.occ(a, o["sha256"])[0], [b])}, held=[held["sha256"]])
        dry = self.adopt()
        self.assertEqual(dry["coded_gaps"], {"resolution_binding_receipt_blocked": 1,
                                             "resolution_dependency_blocked": 1})
        self.assertEqual((dry["policy_originals_blocked"], dry["resolution_plan"], dry["held_originals"]),
                         (1, {"state": "blocked"}, 1))
        self.assertTrue(mail_adoption.blocking(dry))
        before = self.state()
        with self.assertRaisesRegex(mail_adoption.AdoptionError, "^resolution_plan_blocked$"):
            self.adopt(apply=True)
        self.assertEqual(self.state(), before)
        self.assertIsNotNone(ra)

    def test_36c_hand_edited_cycle_is_drift(self):
        r1, r2 = self.mk(34, ["cycle one", "cycle two"]), self.mk(35, ["cycle one", "cycle two"])
        o1, o2 = r1.atts
        self.declare_legacy(o1, o2)
        self.build()
        self.pin({o1["sha256"]: self.entry_for(o1, r1, self.occ(r1, o1["sha256"])[0], [r2]),
                  o2["sha256"]: self.entry_for(o2, r2, self.occ(r2, o2["sha256"])[0], [r1])})
        dry = self.adopt()
        self.assertEqual(dry["resolution_drift"], {"resolution_dependency_cycle": 1})
        self.assertEqual(dry["coded_gaps"], {"resolution_dependency_cycle": 2})
        self.assertEqual(dry["resolution_plan"], {"state": "blocked"})
        self.assertTrue(mail_adoption.blocking(dry))


class Chain(ResolutionAdoptionCase):
    """R2 depends on R1 for O1 and binds O2, which R3 depends on."""

    def setUp(self):
        super().setUp()
        self.r1, self.r2, self.r3 = self.mk(41, ["chain one"]), self.mk(42, ["chain one", "chain two"]), \
            self.mk(43, ["chain two"])
        self.o1, self.o2 = self.r2.atts
        self.declare_legacy(self.o1, self.o2)
        self.build()
        self.pin({self.o1["sha256"]: self.entry_for(self.o1, self.r1, self.occ(self.r1, self.o1["sha256"])[0], [self.r2]),
                  self.o2["sha256"]: self.entry_for(self.o2, self.r2, self.occ(self.r2, self.o2["sha256"])[0], [self.r3])})

    def test_37_apply_order_follows_the_dependency_edges(self):
        dry = self.adopt()
        self.invariants(dry)
        self.assertEqual((dry["resolvable_messages"], dry["resolvable_originals"], dry["policy_originals"]), (3, 2, 2))
        recorder = Recorder()
        applied = self.adopt(apply=True, backend_factory=recorder)
        self.assertEqual(recorder.calls, [str(r.path) for r in (self.r1, self.r2, self.r3)])
        self.assertEqual((applied["adopted_messages"], applied["resolved_originals"], applied["failures"]), (3, 2, {}))
        self.assertEqual(len(self.resolution_rows()), 2)
        self.assertEqual(applied["resolution_after"]["rows"], 2)

    def test_37a_fault_in_the_middle_then_recovery(self):
        recorder = Recorder(fail=[self.r2.path])
        first = self.adopt(apply=True, backend_factory=recorder)
        self.assertEqual(first["failures"], {"stage_or_input_rejected": 1, "resolution_dependency_failed": 1})
        self.assertEqual(first["adopted_messages"], 1)
        rows = self.resolution_rows()
        self.assertEqual([r[0] for r in rows], [self.o1["sha256"]])
        dry = self.adopt()
        self.invariants(dry)
        self.assertEqual((dry["policy_originals_resolved"], dry["resolvable_originals"], dry["policy_originals_blocked"],
                          dry["resolvable_binding_messages"], dry["resolvable_dependent_messages"]), (1, 1, 0, 1, 1))
        self.assertFalse(mail_adoption.blocking(dry))
        second = self.adopt(apply=True)
        self.assertEqual((second["adopted_messages"], second["resolved_originals"], second["failures"]), (2, 1, {}))
        self.assertIn(rows[0], self.resolution_rows())  # the O1 row is byte-identical
        self.assertEqual(len(self.resolution_rows()), 2)


class Rfc822AndHeld(ResolutionAdoptionCase):
    def setUp(self):
        super().setUp()
        self.r1 = self.mk(51, ["forwarded shared"])
        self.o = self.r1.atts[0]
        self.fwd = self.mk_rfc822(52, ["forwarded shared"])
        self.rd = self.mk(53, ["held only"])
        self.held = self.rd.atts[0]
        self.declare_legacy(self.o, self.held)
        self.build()

    def o_entry(self, binding, dependents, oid=None):
        return self.entry_for(self.o, binding, oid or self.occ(binding, self.o["sha256"])[0], dependents)

    def test_28_29_rfc822_dependent_keeps_its_code_and_held_only_gap_is_accepted(self):
        self.pin({self.o["sha256"]: self.o_entry(self.r1, [self.fwd])}, held=[self.held["sha256"]])
        dry = self.adopt()
        self.invariants(dry)
        self.assertEqual(dry["coded_gaps"], {"original_scope_unresolved": 1, "unsupported_rfc822_part": 1})
        self.assertEqual((dry["resolvable_messages"], dry["held_originals"], dry["unaccepted_coded_gaps"]),
                         (1, 1, {}))
        self.assertFalse(mail_adoption.blocking(dry))
        applied = self.adopt(apply=True)
        self.assertEqual((applied["adopted_messages"], applied["resolved_originals"]), (1, 1))
        self.assertFalse(mail_adoption.blocking(applied))
        self.assertEqual(applied["after"]["coded_gap_messages"], 2)

    def test_28_rfc822_binding_receipt_is_blocked(self):
        self.pin({self.o["sha256"]: self.o_entry(self.fwd, [self.r1], oid="a" * 64)}, held=[self.held["sha256"]])
        dry = self.adopt()
        self.assertEqual(dry["coded_gaps"].get("unsupported_rfc822_part"), 1)
        self.assertEqual(dry["coded_gaps"].get("resolution_dependency_blocked"), 1)
        self.assertEqual((dry["policy_originals_blocked"], dry["resolution_plan"]), (1, {"state": "blocked"}))
        self.assertTrue(mail_adoption.blocking(dry))

    def test_29_original_in_neither_originals_nor_held_is_drift(self):
        self.pin({self.o["sha256"]: self.o_entry(self.r1, [self.fwd])})  # held original omitted
        dry = self.adopt()
        self.assertEqual(dry["resolution_drift"], {"resolution_not_in_policy": 1})
        self.assertEqual(dry["resolution_plan"], {"state": "blocked"})
        self.assertTrue(mail_adoption.blocking(dry))
        before = self.state()
        with self.assertRaisesRegex(mail_adoption.AdoptionError, "^resolution_plan_blocked$"):
            self.adopt(apply=True)
        self.assertEqual(self.state(), before)

    def test_37e_unreadable_resolution_row_fails_closed(self):
        # A stand-in row that the reader rejects. The design's valid-v1-row case
        # (`resolution_existing_binding_mismatch`) is covered by the pure-plan test below.
        from campaign_tool.records.ledger import original_resolution, store
        self.pin({self.o["sha256"]: self.o_entry(self.r1, [self.fwd])}, held=[self.held["sha256"]])
        with store.ledger(self.database) as con:
            con.execute("PRAGMA foreign_keys=OFF")
            con.execute("BEGIN IMMEDIATE")
            original_resolution.ensure_schema(con)
            con.create_function("original_resolution_append_allowed", -1, lambda *row: 1)
            con.execute("INSERT INTO original_resolutions VALUES(?,?,?,?,?,?,?)",
                        (self.o["sha256"], sha("v1 binding"), sha("declaration"), sha("local policy"), sha("receipt"),
                         sha("occurrence"), json.dumps({"schema": "original_resolution_v1"})))
            con.commit()
        before = self.state()
        with self.assertRaisesRegex(ValueError, "^original_resolution_payload_mismatch$"):
            self.adopt()
        with self.assertRaisesRegex(ValueError, "^original_resolution_payload_mismatch$"):
            self.adopt(apply=True)
        self.assertEqual(self.state(), before)


class PurePlan(unittest.TestCase):
    """The plan is a pure function of digests, shas and ids (design 8.2)."""

    def policy(self, originals, held=()):
        from tests.records.mail_resolution_fixtures import build_policy
        return build_policy(originals, held)

    def entry(self, binding, oid, dependents=(), legacy="b" * 64):
        return {"bytes": 3, "role": "original", "scope": "in_scope", "verification_receipt_sha256": binding,
                "binding_occurrence_id": oid, "dependent_receipts": sorted(dependents), "legacy_binding_sha256": legacy}

    def receipts(self, *specs):
        return {digest: {"state": state, "gap": None, "foreign_gap": None, "mail_id": "m" * 64,
                         "originals": {sha_: [oid] for sha_, oid in originals.items()}}
                for digest, state, originals in specs}

    def test_v1_row_or_different_binding_blocks_the_plan(self):
        o, r1, oid = "1" * 64, "a" * 64, "c" * 64
        policy = self.policy({o: self.entry(r1, oid)})
        receipts = self.receipts((r1, "new", {o: oid}))
        for row in ({"schema": "original_resolution_v1", "receipt": r1, "occurrence": oid, "legacy_binding": "b" * 64},
                    {"schema": "original_resolution_mail_export_v1", "receipt": "d" * 64, "occurrence": oid,
                     "legacy_binding": "b" * 64}):
            plan = mail_resolution_plan.plan(policy, receipts, {o: {"scope": "unresolved", "format": "pdf",
                                                                    "resolution": row, "check": None}})
            self.assertEqual((plan["state"][o], plan["blocked_codes"][o], plan["plan_state"]),
                             ("blocked", "resolution_existing_binding_mismatch", "blocked"))

    def test_result_is_independent_of_insertion_order(self):
        o, r1, r2, oid = "1" * 64, "a" * 64, "b" * 64, "c" * 64
        policy = self.policy({o: self.entry(r1, oid, [r2])})
        specs = [(r1, "new", {o: oid}), (r2, "new", {o: "e" * 64})]
        facts = {o: {"scope": "unresolved", "format": "pdf", "resolution": None, "check": None}}
        forward = mail_resolution_plan.plan(policy, self.receipts(*specs), facts)
        backward = mail_resolution_plan.plan(policy, self.receipts(*reversed(specs)), facts)
        self.assertEqual(forward, backward)
        self.assertEqual(forward["resolvable"], [r1, r2])


class Census(ResolutionAdoptionCase):
    def setUp(self):
        super().setUp()
        self.a, self.b = self.mk(61, ["census shared", "census repeated", "census repeated"]), \
            self.mk(62, ["census shared"])
        self.fwd = self.mk_rfc822(63, ["census only in rfc822"])
        self.shared, self.repeated = self.a.atts[0], self.a.atts[1]
        self.only = self.fwd.atts[0]
        self.declare_legacy(self.shared, self.repeated, self.only)
        self.build()
        self.out = self.base / "inventory.json"

    def run_census(self):
        return mail_adoption.write_inventory(self.root, self.export, self.out)

    def test_33_census_lists_every_receipt_and_proposes_deterministically(self):
        before = self.state()
        counts = self.run_census()
        after = self.state()
        self.assertEqual({k: v for k, v in after.items() if not k.startswith("inventory")},
                         {k: v for k, v in before.items() if not k.startswith("inventory")})
        self.assertEqual((counts["receipts"], counts["inventory"], counts["shared_between_receipts"],
                          counts["repeated_in_receipt"], counts["proposed_bindings"], counts["proposed_held"]),
                         (3, 3, 1, 1, 2, {"resolution_no_eligible_binding_receipt": 1}))
        self.assertEqual(set(counts), {"receipts", "inventory", "shared_between_receipts", "repeated_in_receipt",
                                       "proposed_bindings", "proposed_held", "inventory_sha256"})
        self.assertEqual(oct(self.out.stat().st_mode & 0o777), "0o600")
        document = json.loads(self.out.read_bytes())
        by_sha = {row["sha256"]: row for row in document["originals"]}
        shared = by_sha[self.shared["sha256"]]
        self.assertEqual(sorted(r["receipt_sha256"] for r in shared["receipts"]), sorted([self.a.digest, self.b.digest]))
        lo = min(self.a.digest, self.b.digest)
        self.assertEqual(shared["proposal"]["binding_receipt"], lo)
        self.assertEqual(shared["proposal"]["dependent_receipts"], [max(self.a.digest, self.b.digest)])
        repeated = by_sha[self.repeated["sha256"]]
        ids = repeated["receipts"][0]["occurrence_ids"]
        self.assertEqual(len(ids), 2)
        self.assertEqual(repeated["proposal"]["binding_occurrence_id"], min(ids))
        self.assertEqual(by_sha[self.only["sha256"]]["proposal"], {"held": "resolution_no_eligible_binding_receipt"})
        self.assertTrue(by_sha[self.only["sha256"]]["receipts"][0]["mime_precheck"])
        self.assertEqual(len(counts["inventory_sha256"]), 64)

    def test_33_census_is_identical_when_discovery_order_is_reversed(self):
        self.run_census()
        forward = self.out.read_bytes()
        self.out.unlink()
        real = mail_adoption.discover

        def reversed_discover(*args):
            found, gaps = real(*args)
            return list(reversed(found)), gaps
        with mock.patch.object(mail_adoption, "discover", reversed_discover):
            self.run_census()
        self.assertEqual(self.out.read_bytes(), forward)

    def test_census_refuses_an_existing_output_and_apply_combination(self):
        self.run_census()
        with self.assertRaisesRegex(mail_adoption.AdoptionError, "^inventory_path_unsafe$"):
            self.run_census()

    def test_census_is_a_cli_option_and_apply_is_refused_with_it(self):
        import io
        from contextlib import redirect_stdout
        argv = ["adopt-mail", "--root", str(self.root), "--mail-history", str(self.export),
                "--legacy-resolution-inventory", str(self.base / "cli-inventory.json")]
        buffer = io.StringIO()
        with mock.patch("sys.argv", argv), redirect_stdout(buffer):
            self.assertEqual(mail_adoption.main(), 0)
        self.assertEqual(json.loads(buffer.getvalue())["inventory"], 3)
        with mock.patch("sys.argv", argv[:-1] + [str(self.base / "other.json"), "--apply"]), redirect_stdout(io.StringIO()):
            self.assertEqual(mail_adoption.main(), 2)
        self.assertFalse((self.base / "other.json").exists())


if __name__ == "__main__":
    unittest.main()
