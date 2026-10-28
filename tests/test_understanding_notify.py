"""F-045: synthetic report metadata only; retained unique output roots, no send."""
import copy
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
# The same file is a file-backed child entrypoint for restart tests.
sys.path.insert(0, str(ROOT))
from campaign_tool.records import run_orchestrator as adapter
from campaign_tool.records import run_report

FIXTURES = ROOT / "tests/fixtures/run_report"
LEDGER = "understanding-alerts-v1.json"


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode()


def pin(value):
    # Independent of the adapter's fingerprint function.
    return hashlib.sha256(canonical(value)).hexdigest()


def report_at(minute):
    report = json.loads((FIXTURES / "pass.json").read_text())
    stamp = f"2000-01-01T00:{minute:02d}:00Z"
    report.update(run_id=f"run_20000101T00{minute:02d}00Z_{minute:08x}",
                  started_at=stamp, finished_at=stamp)
    return report


def fail(report, code="STAGE_EXIT_1", stage="outputs", count=1):
    entry = next(row for row in report["stages"] if row["name"] == stage)
    entry.update(status="fail", error_code=code,
                 counts={"input": count, "output": 0, "failed": count, "skipped": 0})
    if stage == "notify":
        report["notify_failed"] = True
    report["overall_status"] = "fail"


def call(root, report, baseline, prior, **kwargs):
    options = dict(baseline_sha256=pin(baseline), prior_report=prior,
                   prior_sha256=pin(prior) if prior is not None else None,
                   max_prior_age_seconds=60)
    options.update(kwargs)
    return adapter.record_understanding_alerts(report, baseline, root, **options)


class UnderstandingNotifyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # No fixed author receipt can be overwritten by a rerun or reviewer.
        cls.outputs = Path(tempfile.mkdtemp(prefix="notify-tests-", dir=ROOT.parent))
        print(f"F045 synthetic retained outputs: {cls.outputs}", file=sys.stderr)

    def setUp(self):
        self.root = self.outputs / self._testMethodName
        self.root.mkdir()
        self.baseline = json.loads((FIXTURES / "baseline-20261023.json").read_text())
        self.prior, self.current = report_at(0), report_at(1)

    def invoke(self, report=None, prior=None, root=None, **kwargs) -> dict:
        return call(root or self.root, report or self.current, self.baseline,
                    self.prior if prior is None else prior, **kwargs)

    def state(self):
        return json.loads((self.root / LEDGER).read_text())

    def test_source_shaped_inputs_and_identity(self):
        run_report.validate_run_report(self.prior)
        self.assertEqual(adapter.understanding_identity(self.prior), pin(self.prior))
        result = self.invoke()
        self.assertEqual(result["verification"], "VERIFIED")
        self.assertEqual(result["events"], [])
        self.assertTrue(result["review_required"])
        for flag in ("sent", "activation_allowed", "publication_ready"):
            self.assertIs(result[flag], False)
        self.assertEqual(len(self.state()["entries"]), 1)

    def test_identical_reports_no_duplicate_or_rewrite(self):
        self.current["needs_g"][4]["count"] += 1
        first = self.invoke()
        self.assertEqual(len(first["events"]), 1)
        path = self.root / LEDGER
        before = (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_ino)
        replay = self.invoke()
        self.assertEqual(replay["events"], [])
        self.assertEqual(replay["reason_code"], "ALREADY_RECORDED")
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_ino), before)
        self.assertEqual(len(self.state()["entries"]), 1)

    def test_exact_one_gap_event_with_bound_identities(self):
        self.current["needs_g"][4]["count"] = 2
        event, = self.invoke()["events"]
        expected = {"reason_code": "GAP_WIDENED", "stage": "extract", "code": "AWAITING_OCR",
                    "count": 2, "prior_count": 1, "baseline_count": 1,
                    "run_id": self.current["run_id"], "report_sha256": pin(self.current),
                    "prior_run_id": self.prior["run_id"], "prior_sha256": pin(self.prior),
                    "baseline_sha256": pin(self.baseline), "audience": "internal"}
        self.assertEqual(event, dict(expected, event_id=pin(expected)))

    def test_equality_decrease_and_prior_high_water(self):
        for index, count in enumerate((1, 2, 3)):
            root = self.root / str(index)
            root.mkdir()
            self.prior["needs_g"][4]["count"] = 3
            self.current["needs_g"][4]["count"] = count
            self.assertEqual(self.invoke(root=root)["events"], [])
        self.current["needs_g"][4]["count"] = 4
        self.assertEqual(len(self.invoke()["events"]), 1)

    def test_baseline_allowance_and_absent_identity_zero(self):
        self.baseline["needs_g"][4]["count"] = 3
        self.current["needs_g"][4]["count"] = 3
        self.assertEqual(self.invoke()["events"], [])
        newer = report_at(2)
        newer["needs_g"][4]["count"] = 4
        event, = self.invoke(report=newer, prior=self.current)["events"]
        self.assertEqual((event["count"], event["baseline_count"]), (4, 3))
        separate = self.root / "new-code"
        separate.mkdir()
        newer["needs_g"].append({"stage": "portal", "code": "NEW_GAP", "count": 1, "severity": "info"})
        self.assertEqual([e["code"] for e in self.invoke(report=newer, prior=self.current, root=separate)["events"]],
                         ["AWAITING_OCR", "NEW_GAP"])

    def test_stage_identity_no_offset_and_relocation(self):
        self.current["needs_g"][0]["count"] -= 1
        self.current["needs_g"][1]["count"] += 1
        event, = self.invoke()["events"]
        self.assertEqual((event["stage"], event["code"], event["count"]),
                         ("adoption", "ADOPTION_CODED_GAPS", 117))
        separate = self.root / "relocation"
        separate.mkdir()
        self.current = report_at(1)
        self.current["needs_g"][4]["stage"] = "delta"
        event, = self.invoke(root=separate)["events"]
        self.assertEqual((event["stage"], event["baseline_count"], event["prior_count"]), ("delta", 0, 0))

    def test_failure_timeout_and_deadline_are_distinct(self):
        for code, reason in (("STAGE_EXIT_1", "NEW_FAILURE"), ("STAGE_TIMEOUT", "NEW_TIMEOUT"),
                             ("RUN_DEADLINE_REACHED", "NEW_TIMEOUT"),
                             ("NOTIFY_HELPER_TIMEOUT", "NEW_TIMEOUT"),
                             ("NOTIFY_HELPER_FAILED", "NEW_FAILURE")):
            with self.subTest(code=code):
                root = self.root / code
                root.mkdir()
                report = report_at(1)
                stage = "notify" if code.startswith("NOTIFY_") else "outputs"
                fail(report, code, stage, count=0 if code == "STAGE_TIMEOUT" else 1)
                event, = self.invoke(root=root, report=report)["events"]
                self.assertEqual((event["reason_code"], event["stage"], event["code"]), (reason, stage, code))
                self.assertEqual(event["audience"], "internal")
                self.assertEqual(report["needs_g"], self.prior["needs_g"])

    def test_persistent_failure_increase_recovery_and_recurrence(self):
        fail(self.prior)
        fail(self.current)
        self.assertEqual(self.invoke()["events"], [])
        more = report_at(2)
        fail(more, count=2)
        event, = self.invoke(report=more, prior=self.current)["events"]
        self.assertEqual((event["reason_code"], event["count"]), ("NEW_FAILURE", 2))
        recovered = report_at(3)
        self.assertEqual(self.invoke(report=recovered, prior=more)["events"], [])
        again = report_at(4)
        fail(again)
        self.assertEqual(len(self.invoke(report=again, prior=recovered)["events"]), 1)

    def test_restart_new_process_preserves_dedup(self):
        fail(self.current)
        self.assertEqual(len(self.invoke()["events"]), 1)
        case = self.root / "restart-input.json"
        case.write_text(json.dumps({"report": self.current, "prior": self.prior, "baseline": self.baseline}))
        before = (self.root / LEDGER).read_bytes()
        completed = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--alert-replay", str(self.root)],
                                   cwd=ROOT, capture_output=True, text=True, timeout=20)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual((result["reason_code"], result["events"]), ("ALREADY_RECORDED", []))
        self.assertEqual((self.root / LEDGER).read_bytes(), before)

    def test_missing_unpinned_stale_overlap_mode_and_same_run_unverified(self):
        cases = [(None, {}), (self.prior, {"prior_sha256": None}),
                 (self.prior, {"prior_sha256": "0" * 64}),
                 (self.prior, {"max_prior_age_seconds": 59}),
                 (report_at(2), {}), (self.current, {})]
        other_mode = copy.deepcopy(self.prior)
        other_mode["mode"] = "manual"
        cases.append((other_mode, {}))
        for index, (prior, opts) in enumerate(cases):
            with self.subTest(index=index):
                root = self.root / str(index)
                root.mkdir()
                result = call(root, self.current, self.baseline, prior, **opts)
                self.assertEqual((result["verification"], result["reason_code"], result["events"]),
                                 ("UNVERIFIED", "PRIOR_UNVERIFIED", []))
                state = json.loads((root / LEDGER).read_text())
                self.assertEqual(len(state["entries"]), 1)
                self.assertEqual(state["entries"][0]["result"], result)
                self.assertEqual(result["audience"], "internal")
                recovered = call(root, report_at(2), self.baseline, self.current)
                self.assertEqual(recovered["verification"], "VERIFIED")

    def test_freshness_inclusive_boundary_and_zero(self):
        self.assertEqual(self.invoke(max_prior_age_seconds=60)["verification"], "VERIFIED")
        separate = self.root / "zero"
        separate.mkdir()
        self.prior["started_at"] = self.prior["finished_at"] = self.current["started_at"]
        self.assertEqual(self.invoke(root=separate, max_prior_age_seconds=0)["verification"], "VERIFIED")

    def test_wrong_predecessor_refused_and_history_replay_does_not_rewind(self):
        self.invoke()
        newer = report_at(2)
        path = self.root / LEDGER
        before = path.read_bytes()
        result = self.invoke(report=newer)
        self.assertEqual(result["verification"], "UNVERIFIED")
        self.assertEqual(path.read_bytes(), before)
        self.invoke(report=newer, prior=self.current)
        self.assertEqual(self.invoke()["events"], [])
        latest = report_at(3)
        self.assertEqual(self.invoke(report=latest, prior=newer)["verification"], "VERIFIED")
        self.assertEqual(len(self.state()["entries"]), 3)

    def test_stale_discontinuity_persisted_then_fresh_recovery(self):
        self.invoke()  # A -> B
        stale = report_at(10)
        stale["needs_g"][4]["count"] = 5
        original = canonical(stale["needs_g"])
        result = self.invoke(report=stale, prior=self.current)
        self.assertEqual((result["verification"], result.get("audience"), result["events"]),
                         ("UNVERIFIED", "internal", []))
        self.assertEqual(self.state()["entries"][-1]["result"], result)
        self.assertEqual(canonical(stale["needs_g"]), original)
        next_run = report_at(11)
        next_run["needs_g"][4]["count"] = 10
        event, = self.invoke(report=next_run, prior=stale)["events"]
        self.assertEqual((event["reason_code"], event["prior_run_id"], event["prior_sha256"],
                          event["count"], event["prior_count"]),
                         ("GAP_WIDENED", stale["run_id"], pin(stale), 10, 5))
        final = report_at(12)
        final["needs_g"][4]["count"] = 19
        self.assertEqual(self.invoke(report=final, prior=next_run)["verification"], "VERIFIED")
        self.assertEqual(len(self.state()["entries"]), 4)
        before = (self.root / LEDGER).read_bytes()
        # Both historical replay and an unseen older pair leave the tip intact.
        self.assertEqual(self.invoke(report=stale, prior=self.current)["events"], [])
        self.assertEqual(self.invoke(report=report_at(9), prior=report_at(8))["verification"], "UNVERIFIED")
        self.assertEqual((self.root / LEDGER).read_bytes(), before)

    def test_missed_run_records_gap_and_recovers_without_history_loss(self):
        self.invoke()  # B persisted, C not observed by the adapter
        missed, after, fresh = report_at(2), report_at(3), report_at(4)
        after["needs_g"][4]["count"] = 6
        result = self.invoke(report=after, prior=missed)
        self.assertEqual((result["verification"], result["events"]), ("UNVERIFIED", []))
        self.assertEqual(self.state()["entries"][-1]["prior_report"], missed)
        self.assertEqual(self.state()["entries"][-1]["result"], result)
        fresh["needs_g"][4]["count"] = 7
        fail(fresh, "STAGE_TIMEOUT")
        events = self.invoke(report=fresh, prior=after)["events"]
        self.assertEqual([e["reason_code"] for e in events], ["NEW_TIMEOUT", "GAP_WIDENED"])
        self.assertEqual(len(self.state()["entries"]), 3)
        before = (self.root / LEDGER).read_bytes()
        # Cannot insert the skipped report later or reuse it as an old prior.
        with self.assertRaisesRegex(ValueError, "seed identity conflict"):
            self.invoke(report=missed, prior=self.current)
        self.assertEqual(self.invoke(report=report_at(5), prior=missed)["verification"], "UNVERIFIED")
        self.assertEqual((self.root / LEDGER).read_bytes(), before)
        self.assertEqual(self.invoke(report=report_at(5), prior=fresh)["verification"], "VERIFIED")

    def test_absent_or_unpinned_predecessor_is_persistent_recoverable_gap(self):
        for index, options in enumerate(({"prior_report": None}, {"prior_sha256": "0" * 64})):
            root = self.root / str(index)
            root.mkdir()
            self.invoke(root=root)
            current = report_at(2)
            if "prior_report" in options:
                result = call(root, current, self.baseline, None)
            else:
                result = self.invoke(root=root, report=current, prior=self.current, **options)
            state = json.loads((root / LEDGER).read_text())
            self.assertEqual(result["verification"], "UNVERIFIED")
            self.assertEqual(state["entries"][-1]["prior_report"], None)
            self.assertEqual(state["entries"][-1]["result"], result)
            newer = report_at(3)
            fail(newer)
            event, = self.invoke(root=root, report=newer, prior=current)["events"]
            self.assertEqual(event["reason_code"], "NEW_FAILURE")

    def test_discontinuity_survives_process_restart_and_dedup(self):
        self.invoke()
        stale = report_at(10)
        self.invoke(report=stale, prior=self.current)
        newer = report_at(11)
        fail(newer)
        case = self.root / "restart-input.json"
        case.write_text(json.dumps({"report": newer, "prior": stale, "baseline": self.baseline}))
        command = [sys.executable, str(Path(__file__).resolve()), "--alert-replay", str(self.root)]
        completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=20)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(result["verification"], "VERIFIED")
        self.assertEqual([e["reason_code"] for e in result["events"]], ["NEW_FAILURE"])
        before = (self.root / LEDGER).read_bytes()
        completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=20)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["events"], [])
        self.assertEqual((self.root / LEDGER).read_bytes(), before)
        self.assertEqual(self.state()["entries"][1]["result"]["verification"], "UNVERIFIED")

    def test_discontinuity_cannot_adopt_older_or_changed_predecessor(self):
        self.invoke()
        stale = report_at(10)
        self.invoke(report=stale, prior=self.current)
        before = (self.root / LEDGER).read_bytes()
        altered = copy.deepcopy(stale)
        altered["review_required_count"] += 1
        with self.assertRaisesRegex(ValueError, "prior identity conflict"):
            self.invoke(report=report_at(11), prior=altered)
        for prior in (self.current, report_at(8)):
            result = self.invoke(report=report_at(11), prior=prior)
            self.assertEqual((result["verification"], result["events"]), ("UNVERIFIED", []))
        # An unknown pair with a current timestamp equal to the tip is no advance.
        same_time = report_at(10)
        same_time["run_id"] = "run_20000101T001000Z_ffffffff"
        self.assertEqual(self.invoke(report=same_time, prior=report_at(9))["verification"], "UNVERIFIED")
        self.assertEqual((self.root / LEDGER).read_bytes(), before)
        self.assertEqual(self.invoke(report=report_at(11), prior=stale)["verification"], "VERIFIED")

    def test_persisted_unverified_evidence_cannot_be_forged_as_verified(self):
        self.invoke()
        self.invoke(report=report_at(10), prior=self.current)
        valid = self.state()
        for field, value in (("verification", "VERIFIED"), ("audience", "external"),
                             ("events", [{"reason_code": "GAP_WIDENED"}]), ("sent", True)):
            state = copy.deepcopy(valid)
            state["entries"][-1]["result"][field] = value
            raw = canonical(state)
            (self.root / LEDGER).write_bytes(raw)
            with self.assertRaisesRegex(ValueError, "invalid alert evidence"):
                self.invoke(report=report_at(11), prior=report_at(10))
            self.assertEqual((self.root / LEDGER).read_bytes(), raw)

    def test_negative_control_detects_disabled_checkpoint_recovery(self):
        self.invoke()
        real = adapter._alert_transition

        def broken(*args):
            result, advance = real(*args)
            return result, advance and result["verification"] == "VERIFIED"

        with mock.patch.object(adapter, "_alert_transition", side_effect=broken):
            self.invoke(report=report_at(10), prior=self.current)
            result = self.invoke(report=report_at(11), prior=report_at(10))
            with self.assertRaises(AssertionError):
                self.assertEqual(result["verification"], "VERIFIED")
        self.invoke(report=report_at(10), prior=self.current)
        self.assertEqual(self.invoke(report=report_at(11), prior=report_at(10))["verification"], "VERIFIED")

    def test_same_run_changed_bytes_and_seed_reuse_refused(self):
        self.invoke()
        before = (self.root / LEDGER).read_bytes()
        self.current["review_required_count"] += 1
        with self.assertRaisesRegex(ValueError, "identity conflict"):
            self.invoke()
        with self.assertRaisesRegex(ValueError, "seed identity conflict"):
            self.invoke(report=self.prior)
        self.assertEqual((self.root / LEDGER).read_bytes(), before)

    def test_input_baseline_and_pin_negatives_before_write(self):
        for baseline in ({}, [], {"schema_version": True, "needs_g": []},
                         {"schema_version": 1, "needs_g": [{"stage": "extract", "code": "BAD", "count": True}]},
                         {"schema_version": 1, "needs_g": self.baseline["needs_g"] * 2}):
            with self.subTest(baseline=baseline):
                with self.assertRaises(ValueError):
                    call(self.root, self.current, baseline, self.prior)
        with self.assertRaisesRegex(ValueError, "baseline pin mismatch"):
            self.invoke(baseline_sha256="0" * 64)
        for age in (True, -1, 1.5, None):
            with self.assertRaisesRegex(ValueError, "prior age"):
                self.invoke(max_prior_age_seconds=age)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_invalid_reports_reject_text_and_wrong_types(self):
        mutations = [lambda r: r.update(extra_text="SYNTHETIC_FORBIDDEN_TEXT"),
                     lambda r: r.update(run_id=r["run_id"] + "\n"),
                     lambda r: r.update(schema_version=True),
                     lambda r: r.update(stages=r["stages"][:7]),
                     lambda r: r["needs_g"].append(copy.deepcopy(r["needs_g"][0])),
                     lambda r: r["needs_g"][0].update(code="AWAITING_OCR\n"),
                     lambda r: r["stages"][0]["counts"].update(input=True),
                     lambda r: r.update(duration_seconds=float("nan"))]
        for change in mutations:
            bad = copy.deepcopy(self.current)
            change(bad)
            with self.assertRaises(ValueError) as caught:
                self.invoke(report=bad)
            self.assertNotIn("SYNTHETIC_FORBIDDEN_TEXT", str(caught.exception))
        self.assertEqual(list(self.root.iterdir()), [])

    def test_corrupt_saved_state_never_reset_or_overwritten(self):
        self.invoke()
        valid = self.state()
        mutations = [lambda s: s.update(schema_version=True),
                     lambda s: s.update(unexpected="SYNTHETIC_TEXT"),
                     lambda s: s["entries"][0]["result"].update(sent=True),
                     lambda s: s["entries"][0]["result"].update(review_required=1),
                     lambda s: s["baseline"]["needs_g"][4].update(count=True),
                     lambda s: s["entries"].append(copy.deepcopy(s["entries"][0])),
                     lambda s: s["entries"][0].update(prior_report=None)]
        path = self.root / LEDGER
        for change in mutations:
            state = copy.deepcopy(valid)
            change(state)
            raw = json.dumps(state).encode()
            path.write_bytes(raw)
            with self.assertRaises(ValueError):
                self.invoke()
            self.assertEqual(path.read_bytes(), raw)
        for raw in (b'{"schema_version":1,"schema_version":1}', b'not json'):
            path.write_bytes(raw)
            with self.assertRaises(ValueError):
                self.invoke()
            self.assertEqual(path.read_bytes(), raw)

    def test_baseline_change_requires_separate_ledger(self):
        self.invoke()
        before = (self.root / LEDGER).read_bytes()
        self.baseline["needs_g"][4]["count"] = 2
        with self.assertRaisesRegex(ValueError, "invalid alert state"):
            self.invoke()
        self.assertEqual((self.root / LEDGER).read_bytes(), before)

    def test_symlink_root_and_ancestor_refused_before_target_read(self):
        target = self.root / "target"
        target.mkdir()
        (target / "sentinel").write_text("synthetic sentinel")
        link = self.root / "link"
        link.symlink_to(target, target_is_directory=True)
        child = target / "child"
        child.mkdir()
        for path in (link, link / "child", Path("relative"), target / ".." / "target"):
            with self.assertRaises((ValueError, OSError)):
                self.invoke(root=path)
        self.assertEqual(sorted(p.name for p in target.iterdir()), ["child", "sentinel"])
        self.assertEqual(list(child.iterdir()), [])

    def test_symlink_hardlink_fifo_and_directory_leaves_refused(self):
        for kind in ("symlink", "hardlink", "fifo", "directory"):
            for leaf in (LEDGER, "understanding-alerts.lock"):
                with self.subTest(kind=kind, leaf=leaf):
                    root = self.root / (kind + leaf)
                    root.mkdir()
                    sentinel = root / "sentinel"
                    sentinel.write_text("not a ledger")
                    path = root / leaf
                    if kind == "symlink":
                        path.symlink_to(sentinel)
                    elif kind == "hardlink":
                        os.link(sentinel, path)
                    elif kind == "fifo":
                        os.mkfifo(path)
                    else:
                        path.mkdir()
                    with self.assertRaises((ValueError, OSError)):
                        self.invoke(root=root)
                    self.assertEqual(sentinel.read_text(), "not a ledger")

    def test_replace_failure_preserves_previous_bytes_and_retry_dedup(self):
        self.invoke()
        path = self.root / LEDGER
        before = path.read_bytes()
        newer = report_at(2)
        fail(newer)
        with mock.patch.object(adapter.os, "replace", side_effect=OSError("synthetic write failure")):
            with self.assertRaises(OSError):
                self.invoke(report=newer, prior=self.current)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(len(self.invoke(report=newer, prior=self.current)["events"]), 1)
        self.assertEqual(self.invoke(report=newer, prior=self.current)["events"], [])
        self.assertTrue(list(self.root.glob(".understanding-alerts-*.tmp")))

    def test_concurrent_same_transition_exactly_once(self):
        fail(self.current)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda unused: self.invoke(), range(4)))
        self.assertEqual(sum(len(r["events"]) for r in results), 1)
        self.assertEqual(len(self.state()["entries"]), 1)

    def test_no_transport_credentials_environment_or_report_mutation(self):
        fail(self.current)
        before = canonical([self.current, self.prior, self.baseline])
        with mock.patch.object(socket, "socket", side_effect=AssertionError("network forbidden")), \
             mock.patch.object(subprocess, "Popen", side_effect=AssertionError("subprocess forbidden")), \
             mock.patch.object(os, "environ", object()), \
             mock.patch.object(os, "getenv", side_effect=AssertionError("environment forbidden")), \
             mock.patch.object(adapter, "write_notify_alert", side_effect=AssertionError("legacy writer forbidden")), \
             mock.patch.object(adapter, "record_notify_outcome", side_effect=AssertionError("notify mutation forbidden")):
            self.assertEqual(len(self.invoke()["events"]), 1)
        self.assertEqual(canonical([self.current, self.prior, self.baseline]), before)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["understanding-alerts-v1.json", "understanding-alerts.lock"])

    def test_pending_notify_not_mislabeled_as_sent(self):
        self.current["stages"][-1] = adapter.pending_notify()
        result = self.invoke()
        self.assertEqual(result["events"], [])
        self.assertFalse(result["sent"])
        self.assertTrue(adapter.notify_pending(self.current))
        updated = copy.deepcopy(self.current)
        adapter.record_notify_outcome(updated, False, "helper_timeout")
        self.assertTrue(updated["notify_failed"])
        self.assertEqual(updated["stages"][-1]["error_code"], "NOTIFY_HELPER_TIMEOUT")
        self.assertTrue(adapter.notify_pending(self.current))

    def test_negative_control_detects_disabled_event_comparator(self):
        self.current["needs_g"][4]["count"] = 2
        real = adapter._understanding_comparison

        def broken(*args):
            return dict(real(*args), events=[])

        with mock.patch.object(adapter, "_understanding_comparison", side_effect=broken):
            with self.assertRaises(AssertionError):
                self.assertEqual(len(self.invoke()["events"]), 1)
        positive = self.root / "positive"
        positive.mkdir()
        self.assertEqual(len(self.invoke(root=positive)["events"]), 1)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--alert-replay":
        root = Path(sys.argv[2])
        case = json.loads((root / "restart-input.json").read_text())
        print(json.dumps(call(root, case["report"], case["baseline"], case["prior"])))
    else:
        unittest.main()
