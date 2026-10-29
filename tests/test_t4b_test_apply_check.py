"""Synthetic-only S5 checker controls, including real merged producer integration.

No command template is executed. The World fixture invokes the actual planner and
backend only on disposable synthetic roots redirected under this candidate's out/.
"""
import copy
import contextlib
import io
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch
import uuid

from tools import t4b_test_apply_check as check

REPO = Path(__file__).resolve().parents[1]
FIXTURE = REPO / "tests/fixtures/t4b_s5/valid.json"
RUN_ID = "synthetic-s5-f011"
DECISION = "2026-10-24T18:10:00Z"


def load_packet():
    """Materialize real digest relationships from low-entropy synthetic rows.

    valid.json is an inert template, not a CLI-ready receipt. True source pins
    stay exact; all dependent snapshot/report digests are generated at test time.
    No checker predicate is patched or relaxed to accept the synthetic packet.
    """
    p = json.loads(FIXTURE.read_text())
    p["source_head"] = check.SOURCE_HEAD
    p["f010_sha256"] = check.F010_SHA
    p["contract_pins"] = check.PINS.copy()
    before, after = (p["snapshots"][phase]["resolutions"] for phase in ("before", "after"))
    p["binding"]["snapshot_sha256"] = check.digest(p["snapshots"]["before"])
    for gate in p["gates"].values():
        gate["snapshot_sha256"] = p["binding"]["snapshot_sha256"]
    for phase, wrapper in p["reports"].items():
        wrapper["binding"] = p["binding"].copy()
        report = wrapper["report"]
        if phase != "preflight":
            for field, rows in (("resolution_baseline", after if phase == "rerun" else before),
                                ("resolution_after", after if phase in ("apply", "rerun") else before)):
                report[field]["sha256"] = check.resolution_summary(rows)["sha256"]
        wrapper["sha256"] = check.digest(report)
    return p


def make_packet(owner):
    """Author a fresh producer-shaped synthetic sample; never called on TEST."""
    # Imports run only after test tempdir redirection, not in the checker CLI.
    from tests.records import test_mail_adoption_resolution as fixture
    w = fixture.World(owner, baseline=True)
    ra = w.export(10)
    rb = w.export(11, (fixture.O1, fixture.O2, fixture.O2, fixture.X))
    rc = w.export(12, (fixture.O1,))
    w.export(13, (fixture.O3,))
    w.export(14, (fixture.O1,), nested=True)
    # Freeze a smaller binding receipt for both O1 and O2 (same G40 shape).
    raw = w.paths[11].read_bytes()
    for spaces in range(256):
        value = raw + b" " * spaces
        if fixture.sha(value) < fixture.sha(w.paths[12].read_bytes()):
            fixture.write_private(w.paths[11], value)
            break
    else:
        raise RuntimeError("synthetic binding fixture exhausted")
    for value in (fixture.O1, fixture.O2, fixture.O3):
        w.declare(value)
    fixture.write_private(w.cfg_path, json.dumps({"schema": "mail-history-adoption-v1", "source": "mail-history", "adoption": "enabled"}).encode())
    pre_config = check.sha(w.cfg_path.read_bytes())
    preflight = w.run()
    w.policy(held=[fixture.sha(fixture.O3)])

    def snapshot():
        original = {}
        for row in w.rows("SELECT * FROM originals ORDER BY sha256"):
            provenance = json.loads(row[9])
            receipt = provenance.get("receipt_sha256")
            occurrences = w.rows("SELECT evidence FROM occurrences WHERE original_sha256=? AND acquisition_method='verified_mail_export' ORDER BY id", (row[0],))
            # Export only canonical CAS identity, never a physical private path.
            cas = None
            if row[6] is not None:
                if row[6] != str(w.root / "intake/blobs" / row[0]):
                    raise RuntimeError("noncanonical synthetic CAS")
                cas = row[0]
            original[row[0]] = {"row_sha256": check.digest(row), "role": row[4], "scope": row[5],
                "cas_sha256": cas, "provenance": provenance.get("method"), "receipt_sha256": receipt,
                "occurrences": sorted({json.loads(r[0])["receipt_sha256"] for r in occurrences})}
        resolutions = {}
        for row in w.rows("SELECT * FROM original_resolutions ORDER BY original_sha256"):
            payload = json.loads(row[-1])
            resolutions[row[0]] = {"row_sha256": check.digest(row), "binding_sha256": row[1], "schema": payload["schema"],
                "policy_sha256": row[3], "capture_receipt_sha256": row[4], "capture_occurrence_id": row[5]}
        legacy = {name: check.digest(w.rows("SELECT * FROM " + name + " ORDER BY 1,2")) for name in ("legacy_rows", "occurrence_projection_rows")}
        legacy["captured_legacy_occurrences"] = check.digest(w.rows("SELECT * FROM occurrences WHERE acquisition_method LIKE 'captured_legacy_%' ORDER BY id"))
        stages = {key: dict(w.rows("SELECT stage,status FROM stage_state WHERE original_sha256=? AND stage IN ('preserve','extract') ORDER BY stage", (key,))) for key in original}
        return {"originals": original, "resolutions": resolutions, "legacy": legacy,
                "mail_messages": w.rows("SELECT count(*) FROM mail_messages")[0][0], "stages": stages,
                "quick_check": w.rows("PRAGMA quick_check")[0][0], "fk_violations": len(w.rows("PRAGMA foreign_key_check"))}

    before = snapshot()
    tree = w.tree()
    dry = w.run()
    owner.assertEqual(w.tree(), tree)
    applied = w.run(True)
    after = snapshot()
    tree = w.tree()
    rerun = w.run(True)
    owner.assertEqual(w.tree(), tree)
    final = snapshot()
    subjects = sorted(w.value["originals"])
    held = sorted(w.value["held"])
    binding = {"snapshot_sha256": check.digest(before), "inventory_sha256": w.value["inventory_sha256"],
        "policy_sha256": w.current.sha256, "approval_sha256": w.value["approval_sha256"],
        "config_sha256": check.sha(w.cfg_path.read_bytes()), "preflight_config_sha256": pre_config,
        "backup_manifest_sha256": check.digest("synthetic backup closure; not a real backup")}
    expected = {"receipts": 5, "eligible_new_messages": 1,
        "initial_gaps": {"original_scope_unresolved": 3, "unsupported_rfc822_part": 1},
        "final_gaps": {"original_scope_unresolved": 1, "unsupported_rfc822_part": 1},
        "binding_messages": 1, "dependent_messages": 1,
        "new_originals": sorted({ra["original_eml"]["sha256"], rb["original_eml"]["sha256"],
                                 rc["original_eml"]["sha256"], fixture.sha(fixture.X)}),
        "adopted_receipts": sorted(fixture.sha(w.paths[uid].read_bytes()) for uid in (10, 11, 12)),
        "preserve_promotions": subjects, "scope_counts": {"unresolved": 4}}
    p = {"schema": "s5-synthetic-evidence-v1", "kind": "SYNTHETIC", "run_id": RUN_ID,
        "source_head": check.SOURCE_HEAD, "contract_pins": check.PINS, "f010_sha256": check.F010_SHA,
        "binding": binding, "historical_tested_k": 0, "fixes400": False,
        "gates": {name: {"state": "SIMULATED_QUALIFIED", "evidence_sha256": check.digest(["synthetic", name]),
                           "run_id": RUN_ID, "snapshot_sha256": binding["snapshot_sha256"]} for name in check.GATES},
        "allowance": {"remaining_attempts": 2, "remaining_seconds": 600, "requested_attempts": 2, "requested_seconds": 600, "stop_clear": True},
        "partition": {"inventory": sorted(subjects + held), "policy": subjects, "held": held,
                      "already_resolved": [], "resolvable": subjects, "blocked": []},
        "policy": {key: {field: value[field] for field in ("verification_receipt_sha256", "binding_occurrence_id")} for key, value in w.value["originals"].items()},
        "expected": expected, "snapshots": {"before": before, "after": after, "rerun": final}, "reports": {}}
    for minute, (phase, report) in enumerate(zip(("preflight", "dry", "apply", "rerun"), (preflight, dry, applied, rerun))):
        p["reports"][phase] = {"run_id": RUN_ID, "binding": binding.copy(), "observed_at": f"2026-10-24T18:0{minute}:00Z",
                               "exit": 0, "sha256": check.digest(report), "report": report}
    return p


class CheckerTests(unittest.TestCase):
    def setUp(self):
        self.p = load_packet()

    @contextlib.contextmanager
    def runsheet_repo(self):
        """Copy only admitted source closure, then forbid every read outside it."""
        out = REPO.parent / "out"
        out.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="s5-hermetic-", dir=out) as temp:
            repo = Path(temp) / "repo"
            for name in (*check.PINS, "tools/t4b_test_apply_check.py", "docs/runbooks/T4B-TEST-APPLY.md"):
                target = repo / name
                target.parent.mkdir(parents=True, exist_ok=True)
                # This runsheet admits SOURCE_HEAD, not today's producer. Keep
                # its historical bytes inert and available in shallow CI clones;
                # production pins and the tampering checks remain unchanged.
                source = (REPO / "tests/fixtures/t4b_s5/mail_adoption.source.txt"
                          if name == "campaign_tool/records/mail_adoption.py"
                          else REPO / name)
                raw = source.read_bytes()
                if name in check.PINS:
                    self.assertEqual(check.sha(raw), check.PINS[name], name)
                target.write_bytes(raw)
            original_read = check.read_local
            reads = []

            def read(path, root):
                path, root = Path(path), Path(root)
                self.assertTrue(path.is_relative_to(repo), str(path))
                self.assertIn(root, (repo, repo.parent))
                reads.append(path)
                return original_read(path, root)

            with patch.object(check, "REPO", repo), patch.object(check, "read_local", read):
                yield repo, reads

    def validate(self, p=None):
        return check.validate(self.p if p is None else p, RUN_ID, DECISION)

    def fails(self, code):
        with self.assertRaisesRegex(check.CheckError, "^" + re.escape(code) + "$"):
            self.validate()

    def resign(self, phase):
        wrapper = self.p["reports"][phase]
        wrapper["sha256"] = check.digest(wrapper["report"])

    def test_valid_static_fixture_retains_boundaries(self):
        result = self.validate()
        self.assertEqual(result, {"status": "SYNTHETIC_VALID_REVIEW_REQUIRED", "activation_allowed": False,
            "semantic_acceptance": False, "s5_execution_verified": False, "historical_tested_k": 0, "synthetic_k": 2, "fixes400": False})

    def test_actual_producer_and_snapshot_adapter(self):
        out = REPO.parent / "out" / ("producer-" + uuid.uuid4().hex)
        out.mkdir(parents=True)
        with patch.object(tempfile, "tempdir", str(out)):
            p = make_packet(self)
            self.assertEqual(self.validate(p)["synthetic_k"], 2)
        # Stable source shape, independent real run; volatile engine/time/rows differ.
        self.assertEqual(p["expected"]["initial_gaps"], self.p["expected"]["initial_gaps"])
        self.assertEqual(p["reports"]["apply"]["report"]["adopted_messages"], 3)

    def test_each_gate_missing_or_unqualified(self):
        original = copy.deepcopy(self.p)
        for gate in check.GATES:
            for mode in ("missing", "unverified", "stale_snapshot"):
                with self.subTest(gate=gate, mode=mode):
                    self.p = copy.deepcopy(original)
                    if mode == "missing":
                        del self.p["gates"][gate]
                        self.fails("invalid_shape")
                    else:
                        self.p["gates"][gate]["state" if mode == "unverified" else "snapshot_sha256"] = "UNVERIFIED" if mode == "unverified" else "f" * 64
                        self.fails("unqualified_gate")

    def test_stop_and_remaining_allowance(self):
        for field, value in (("stop_clear", False), ("remaining_attempts", 0), ("remaining_seconds", 599)):
            with self.subTest(field=field):
                p = copy.deepcopy(self.p)
                p["allowance"][field] = value
                with self.assertRaisesRegex(check.CheckError, "^allowance_or_stop$"):
                    self.validate(p)

    def test_stale_future_and_reordered_reports(self):
        self.p["reports"]["dry"]["observed_at"] = "2026-10-24T17:00:00Z"
        self.fails("stale_report")
        self.p["reports"]["dry"]["observed_at"] = "2026-10-24T19:00:00Z"
        self.fails("stale_report")
        self.p["reports"]["dry"]["observed_at"] = "2026-10-24T18:03:00Z"
        self.fails("report_order_mismatch")

    def test_report_empty_hash_and_identity(self):
        original = copy.deepcopy(self.p)
        for mode, code in (("empty", "report_hash_mismatch"), ("hash", "report_hash_mismatch"), ("run", "report_identity_mismatch"), ("binding", "report_identity_mismatch")):
            with self.subTest(mode=mode):
                self.p = copy.deepcopy(original)
                w = self.p["reports"]["dry"]
                if mode == "empty": w["report"] = {}
                if mode == "hash": w["sha256"] = "e" * 64
                if mode == "run": w["run_id"] = "older-run"
                if mode == "binding": w["binding"]["policy_sha256"] = "f" * 64
                self.fails(code)

    def test_wrong_direction_gap_counts(self):
        self.p["expected"]["final_gaps"]["original_scope_unresolved"] = 4
        self.fails("gap_direction_mismatch")

    def test_wrong_direction_mail_count(self):
        for name in ("after", "rerun"):
            self.p["snapshots"][name]["mail_messages"] = 0
        self.fails("mail_delta_mismatch")

    def test_exact_resolution_set_not_count_only(self):
        for name in ("after", "rerun"):
            rows = self.p["snapshots"][name]["resolutions"]
            key = self.p["partition"]["resolvable"][0]
            rows["f" * 64] = rows.pop(key)
        self.fails("resolution_delta_mismatch")

    def test_binding_mismatch_isolated(self):
        key = self.p["partition"]["resolvable"][0]
        for name in ("after", "rerun"):
            self.p["snapshots"][name]["resolutions"][key]["capture_receipt_sha256"] = "e" * 64
        self.fails("resolution_binding_mismatch")

    def test_every_baseline_row_immutable(self):
        for table, code in (("originals", "k0_changed"), ("resolutions", "r0_changed")):
            p = copy.deepcopy(self.p)
            key = next(iter(p["snapshots"]["before"][table]))
            for phase in ("after", "rerun"):
                p["snapshots"][phase][table][key]["row_sha256"] = "e" * 64
            with self.assertRaisesRegex(check.CheckError, "^" + code + "$"):
                self.validate(p)

    def test_legacy_and_rerun_immutability(self):
        self.p["snapshots"]["rerun"]["legacy"]["legacy_rows"] = "f" * 64
        self.fails("rerun_snapshot_changed")
        self.p["snapshots"]["after"]["legacy"]["legacy_rows"] = "f" * 64
        self.fails("legacy_changed")

    def test_stage_direction_and_extraction_not_promoted(self):
        key = self.p["partition"]["resolvable"][0]
        for phase in ("after", "rerun"):
            self.p["snapshots"][phase]["stages"][key]["extract"] = "done"
        self.fails("stage_delta_mismatch")

    def test_new_original_requires_adopted_occurrence(self):
        key = self.p["expected"]["new_originals"][0]
        for phase in ("after", "rerun"):
            self.p["snapshots"][phase]["originals"][key]["occurrences"] = []
        self.fails("a_new_provenance_mismatch")

    def test_plan_after_and_counter_failures_with_valid_report_hash(self):
        original = copy.deepcopy(self.p)
        for field, value, code in (("adopted_messages", 4, "adopted_count_mismatch"),
                                   ("resolved_originals", 1, "resolved_count_mismatch"),
                                   ("failures", {"synthetic_failure": 1}, "report_blocked"),
                                   ("resolution_drift", {"synthetic_drift": 1}, "resolution_plan_blocked"),
                                   ("pending_messages", True, "invalid_count")):
            with self.subTest(field=field):
                self.p = copy.deepcopy(original)
                self.p["reports"]["apply"]["report"][field] = value
                self.resign("apply")
                self.fails(code)
        self.p = copy.deepcopy(original)
        self.p["reports"]["apply"]["report"]["after"]["resolution_plan"]["state"] = "blocked"
        self.resign("apply")
        self.fails("after_report_mismatch")

    def test_integrity_partition_scope_and_snapshot(self):
        for field, mutation, code in (("integrity", None, "integrity_failed"), ("partition", None, "partition_mismatch"),
                                       ("scope", None, "scope_count_mismatch"), ("snapshot", None, "snapshot_hash_mismatch")):
            with self.subTest(field=field):
                p = copy.deepcopy(self.p)
                if field == "integrity": p["snapshots"]["after"]["fk_violations"] = 1
                if field == "partition": p["partition"]["held"] = p["partition"]["policy"]
                if field == "scope": p["expected"]["scope_counts"] = {"unresolved": 5}
                if field == "snapshot": p["binding"]["snapshot_sha256"] = "f" * 64
                if field == "snapshot":
                    for gate in p["gates"].values(): gate["snapshot_sha256"] = "f" * 64
                with self.assertRaisesRegex(check.CheckError, "^" + code + "$"):
                    self.validate(p)

    def test_no_claim_to_fix400_or_real_acceptance(self):
        self.p["fixes400"] = True
        self.fails("false_coverage_claim")
        self.p["fixes400"] = False
        self.p["kind"] = "REAL"
        self.fails("not_synthetic")

    def test_exact_source_and_external_run_identity(self):
        self.p["source_head"] = "e" * 40
        self.fails("source_identity_mismatch")
        self.p["source_head"] = check.SOURCE_HEAD
        self.p["run_id"] = "older-run"
        self.fails("run_identity_mismatch")

    def test_attachment_and_resolution_digest_controls(self):
        for field, value, code in (("adopted_attachments", 4, "attachment_delta_mismatch"),
                                   ("attachments", -1, "invalid_count"),
                                   ("resolution_after", {"rows": 3, "sha256": "f" * 64}, "resolution_digest_mismatch")):
            with self.subTest(field=field):
                p = copy.deepcopy(self.p)
                wrapper = p["reports"]["apply"]
                wrapper["report"][field] = value
                wrapper["sha256"] = check.digest(wrapper["report"])
                with self.assertRaisesRegex(check.CheckError, "^" + code + "$"):
                    self.validate(p)

    def test_occurrence_continuity_and_preserve_direction(self):
        key = self.p["partition"]["resolvable"][0]
        for phase in ("after", "rerun"):
            self.p["snapshots"][phase]["originals"][key]["occurrences"] = ["f" * 64]
        self.fails("occurrence_continuity_mismatch")

    def test_clock_boundary_and_reordered_json_objects(self):
        # First observation exactly one hour old passes; one second beyond fails.
        self.assertEqual(check.validate(self.p, RUN_ID, "2026-10-24T19:00:00Z")["status"], "SYNTHETIC_VALID_REVIEW_REQUIRED")
        with self.assertRaisesRegex(check.CheckError, "^stale_report$"):
            check.validate(self.p, RUN_ID, "2026-10-24T19:00:01Z")
        self.p["reports"] = dict(reversed(list(self.p["reports"].items())))
        self.assertEqual(self.validate()["synthetic_k"], 2)

    def test_json_malformed_duplicate_nonfinite(self):
        for raw in ('{"a":1,"a":2}', '{"x":NaN}', '{} trailing'):
            with self.subTest(raw=raw), self.assertRaises(check.CheckError):
                check.parse(raw)

    def test_runsheet_positive_and_inert_negatives(self):
        with self.runsheet_repo() as (repo, reads):
            text = (repo / "docs/runbooks/T4B-TEST-APPLY.md").read_text()
            self.assertTrue(check.validate_runsheet(text, repo=repo))
            self.assertIn(repo / "tools/t4b_test_apply_check.py", reads)
            for candidate, code in (("", "empty_runsheet"), (text.replace('"--apply"', '"--invented-option"'), "unsupported_option"),
                                    (text.replace("<REPO_ROOT>/tools/t4b_test_apply_check.py", "/missing/check.py"), "missing_absolute_helper"),
                                    (re.sub(r"## backup\n.*?(?=## baseline)", "## backup\n\n", text, flags=re.S), "missing_runsheet_step:backup"),
                                    (text.replace("adopted=eligible_new+N", "adopted=eligible_new-N"), "runsheet_content_changed")):
                with self.subTest(code=code), self.assertRaisesRegex(check.CheckError, "^" + code + "$"):
                    check.validate_runsheet(candidate, repo=repo)
            for bad in ("tools/t4b_test_apply_check.py", "<REPO_ROOT>/../tools/t4b_test_apply_check.py"):
                candidate = text.replace("<REPO_ROOT>/tools/t4b_test_apply_check.py", bad)
                with self.subTest(helper=bad), self.assertRaisesRegex(check.CheckError, "^missing_absolute_helper$"):
                    check.validate_runsheet(candidate, repo=repo)
            with self.assertRaisesRegex(check.CheckError, "^missing_absolute_helper$"):
                check.validate_runsheet(text, repo=Path("relative-repo"))
            with self.assertRaisesRegex(check.CheckError, "^command_source_mismatch$"):
                check.validate_runsheet(text.replace("<SOURCE_HEAD>", "wrong-head"), repo=repo)

    def test_missing_helper_and_pinned_source_not_assumed_present(self):
        with self.runsheet_repo() as (repo, reads):
            text = (repo / "docs/runbooks/T4B-TEST-APPLY.md").read_text()
            original = check.read_local
            for missing in ("tools/t4b_test_apply_check.py", "campaign_tool/records/mail_adoption.py"):
                def read(path, root):
                    if Path(path) == repo / missing:
                        raise FileNotFoundError("synthetic missing tool")
                    return original(path, root)
                with self.subTest(missing=missing), patch.object(check, "read_local", read), self.assertRaises(FileNotFoundError):
                    check.validate_runsheet(text, repo=repo)
            with patch.object(check, "read_local", lambda path, root: b"" if Path(path) == repo / "tools/t4b_test_apply_check.py" else original(path, root)):
                with self.assertRaisesRegex(check.CheckError, "^missing_absolute_helper$"):
                    check.validate_runsheet(text, repo=repo)
            self.assertTrue(check.validate_runsheet(text, repo=repo))
            self.assertIn(repo / "tools/t4b_test_apply_check.py", reads)

    def test_cli_packet_hash_run_binding_and_no_writes(self):
        fixture_before = FIXTURE.stat()
        with self.runsheet_repo() as (repo, reads):
            packet = repo / "packet.json"
            packet.write_text(json.dumps(self.p))
            args = ["--packet", str(packet), "--packet-sha256", check.sha(packet.read_bytes()),
                    "--run-id", RUN_ID, "--decision-time", DECISION]
            before = packet.stat()
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(check.main(args), 0)
            self.assertIn("SYNTHETIC_VALID_REVIEW_REQUIRED", out.getvalue())
            self.assertIn(repo / "tools/t4b_test_apply_check.py", reads)
            args[3] = "f" * 64
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(check.main(args), 2)
            self.assertIn("packet_hash_mismatch", out.getvalue())
            self.assertEqual(packet.stat(), before)
        self.assertEqual(FIXTURE.stat(), fixture_before)

    def test_source_pins_and_tampered_source_fail_closed(self):
        with self.runsheet_repo() as (repo, reads):
            text = (repo / "docs/runbooks/T4B-TEST-APPLY.md").read_text()
            self.assertTrue(check.validate_runsheet(text, repo=repo))
            for name, expected in check.PINS.items():
                with self.subTest(source=name):
                    path = repo / name
                    raw = path.read_bytes()
                    self.assertEqual(check.sha(raw), expected)
                    path.write_bytes(raw + b"\n# synthetic tampering\n")
                    with self.assertRaisesRegex(check.CheckError, "^pinned_source_changed$"):
                        check.validate_runsheet(text, repo=repo)
                    path.write_bytes(raw)
            self.assertTrue(check.validate_runsheet(text, repo=repo))

    def test_symlink_and_fifo_refused_before_read(self):
        import os
        out = REPO.parent / "out" / ("unsafe-input-" + uuid.uuid4().hex)
        out.mkdir(parents=True)
        link = out / "link"
        link.symlink_to(FIXTURE)
        with self.assertRaisesRegex(check.CheckError, "^symlink_input$"):
            check.read_local(link, REPO.parent)
        fifo = out / "fifo"
        os.mkfifo(fifo)
        with self.assertRaisesRegex(check.CheckError, "^invalid_input_file$"):
            check.read_local(fifo, REPO.parent)

    def test_gate_negative_control_rejects_without_hash_shortcut(self):
        # Disabling only the deciding predicate makes the otherwise-valid bad
        # binding pass: this proves the assertion is not an unrelated hash failure.
        key = self.p["partition"]["resolvable"][0]
        for name in ("after", "rerun"):
            self.p["snapshots"][name]["resolutions"][key]["capture_receipt_sha256"] = "f" * 64
        self.fails("resolution_binding_mismatch")
        real_need = check.need
        def disabled(condition, code):
            if code != "resolution_binding_mismatch": real_need(condition, code)
        with patch.object(check, "need", disabled):
            self.assertEqual(self.validate()["status"], "SYNTHETIC_VALID_REVIEW_REQUIRED")


if __name__ == "__main__":
    unittest.main()
