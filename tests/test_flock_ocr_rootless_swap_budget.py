"""SPACE-RULE-1: the smaller space rule for a same-image `--trust-mount add` recreate.

Table-driven, synthetic sizes only. The fake Docker/systemd of
tests/test_flock_ocr_rootless_swap.py stands in for every command; no Docker
daemon, swap or host measurement runs.
"""
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests import test_flock_ocr_rootless_swap as rs
from tests.test_approval_trust_mount import RELEASE, WORK, anchor_module, rendered_wrapper

swap = rs.swap
evidence = rs.evidence
GiB = 1 << 30
MARGIN = 512 << 20
SAME = swap.SPACE_RULE_SAME_IMAGE_TRUST_ADD
STANDARD = swap.SPACE_RULE_STANDARD
TRUST_BIND = {"Type": "bind", "Source": "/etc/deflock", "Destination": "/etc/deflock", "RW": False,
              "Mode": "", "Propagation": "rprivate"}


def bare_driver(state):
    driver = object.__new__(swap.Swap)
    driver.state = state
    driver.out = io.StringIO()
    driver.dry = False
    return driver


class SpaceRuleSelectionTests(unittest.TestCase):
    """Which runs get the smaller rule; the derived mounts come from the real derive_mounts."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.bind_a, self.bind_b = root / "archive", root / "work"
        self.bind_a.mkdir()
        self.bind_b.mkdir()

    def before(self, image=rs.OLD_IMAGE, extra=()):
        mounts = [{"Type": "volume", "Name": "synthetic-data", "Source": "/synthetic/volumes/data/_data",
                   "Destination": "/data", "RW": True},
                  {"Type": "bind", "Source": str(self.bind_a), "Destination": "/workspace/archive", "RW": True},
                  {"Type": "bind", "Source": str(self.bind_b), "Destination": "/workspace/work", "RW": True},
                  *extra]
        return rs._container("a" * 64, "synthetic", image, True, mounts, ["synthetic-data:/data"], [], {})

    def rule(self, before, trust="add", reuse_image=rs.OLD_IMAGE, mounts=None):
        derived, excluded = bare_driver({}).derive_mounts(before)
        reuse = {"run_dir": "/p/run-1", "new_image_id": reuse_image, "wheel_sha256": rs.WHEEL_SHA}
        return swap.space_rule(trust, reuse if reuse_image else None, before,
                               derived if mounts is None else mounts(derived), excluded)

    def test_table(self):
        tmpfs = {"Type": "tmpfs", "Source": "", "Destination": "/scratch", "RW": True}
        rows = [
            # name, before, trust, reuse image, mounts override, expected
            ("same image, add, mounts equal", self.before(), "add", rs.OLD_IMAGE, None, SAME),
            ("no --reuse-image", self.before(), "add", None, None, STANDARD),
            ("reuse of another image (a real swap)", self.before(), "add", rs.NEW_IMAGE, None, STANDARD),
            ("drop (the R.1 rollback)", self.before(extra=[TRUST_BIND]), "drop", rs.OLD_IMAGE, None, STANDARD),
            ("drop without the bind", self.before(), "drop", rs.OLD_IMAGE, None, STANDARD),
            ("no trust mount", self.before(), None, rs.OLD_IMAGE, None, STANDARD),
            ("a tmpfs is excluded", self.before(extra=[tmpfs]), "add", rs.OLD_IMAGE, None, STANDARD),
            ("derived mounts miss one", self.before(), "add", rs.OLD_IMAGE, lambda d: d[:-1], STANDARD),
            ("derived mounts have one more", self.before(), "add", rs.OLD_IMAGE,
             lambda d: d + [dict(d[0], label="m09", destination="/extra")], STANDARD),
            ("a derived source differs", self.before(), "add", rs.OLD_IMAGE,
             lambda d: [dict(d[0], source="/elsewhere")] + d[1:], STANDARD),
            ("a derived mount is read-only", self.before(), "add", rs.OLD_IMAGE,
             lambda d: [dict(d[0], rw=False)] + d[1:], STANDARD),
            ("no derived mounts", self.before(), "add", rs.OLD_IMAGE, lambda d: [], STANDARD),
        ]
        for name, before, trust, image, mounts, expected in rows:
            with self.subTest(case=name):
                self.assertEqual(self.rule(before, trust, image, mounts), expected)

    def test_only_the_read_only_anchor_is_left_out_of_the_comparison(self):
        # A writable /etc/deflock bind is data, not the anchor: derived mounts without it differ.
        before = self.before(extra=[dict(TRUST_BIND, RW=True)])
        derived, excluded = bare_driver({}).derive_mounts(self.before())
        reuse = {"new_image_id": rs.OLD_IMAGE}
        self.assertEqual(swap.space_rule("add", reuse, before, derived, excluded), STANDARD)
        anchored = self.before(extra=[TRUST_BIND])
        self.assertEqual(swap.space_rule("add", reuse, anchored, derived,
                                         [{"type": "bind", "destination": "/etc/deflock"}]), SAME)

    def test_missing_image_or_empty_inputs_keep_the_standard_rule(self):
        before = self.before()
        derived, excluded = bare_driver({}).derive_mounts(before)
        self.assertEqual(swap.space_rule("add", {"new_image_id": rs.OLD_IMAGE}, before, derived, excluded), SAME)
        no_image = dict(before, Image="")
        self.assertEqual(swap.space_rule("add", {"new_image_id": ""}, no_image, derived, excluded), STANDARD)
        self.assertEqual(swap.space_rule("add", {}, before, derived, excluded), STANDARD)


class PreflightBudgetTableTests(unittest.TestCase):
    """preflight_budget against synthetic surveys and a synthetic free-space number."""

    # Synthetic sizes shaped like the live five-mount target (no host was measured).
    SURVEYS = {
        "m00": {"tar_bytes": 18 * GiB, "extract_bytes": 19 * GiB, "sqlite_bytes": 2 * GiB,
                "estimated_copy_bytes": 19 * GiB},
        "m01": {"tar_bytes": 10240, "extract_bytes": 8192, "sqlite_bytes": 0, "estimated_copy_bytes": 8192},
        "m02": {"tar_bytes": 10240, "extract_bytes": 8192, "sqlite_bytes": 0, "estimated_copy_bytes": 8192},
        "m03": {"tar_bytes": 2 * GiB, "extract_bytes": 2 * GiB, "sqlite_bytes": 0,
                "estimated_copy_bytes": 2 * GiB},
        "m04": {"tar_bytes": 74 * GiB, "extract_bytes": 75 * GiB, "sqlite_bytes": 1 * GiB,
                "estimated_copy_bytes": 76 * GiB},
    }

    def totals(self):
        s = self.SURVEYS.values()
        return (sum(r["tar_bytes"] for r in s), max(r["extract_bytes"] for r in s),
                sum(r["sqlite_bytes"] for r in s), sum(r["estimated_copy_bytes"] for r in s))

    def same_need(self):
        tar, extract, sqlite, _ = self.totals()
        return tar + sqlite + extract + MARGIN

    def standard_need(self):
        tar, extract, sqlite, copies = self.totals()
        return max(7 * copies + MARGIN, 3 * tar + 2 * sqlite + extract + MARGIN)

    def check(self, rule, free):
        state = {"preflight": {}, "run_dir": "/synthetic/private/run-1"}
        if rule is not None:
            state["space_rule"] = rule
        driver = bare_driver(state)
        driver.system = SimpleNamespace(disk_free=lambda path: free)
        try:
            driver.preflight_budget(self.SURVEYS)
        except swap.StepFailed as error:
            return "refused", str(error), state
        return "ok", driver.out.getvalue(), state

    def test_needs_are_the_specified_formulas(self):
        tar, extract, sqlite, copies = self.totals()
        parts, need = swap.same_image_space_budget(tar, extract, sqlite)
        self.assertEqual(parts, {"s1_archives": tar, "s2_sqlite_backups": sqlite, "largest_extract": extract,
                                 "margin": MARGIN})
        self.assertEqual(need, self.same_need())
        self.assertGreater(self.standard_need(), 3 * self.same_need())  # the 7x rule is far larger here

    def test_boundaries(self):
        same, standard = self.same_need(), self.standard_need()
        rows = [
            # rule in state, free bytes, expected verdict, expected need in state
            (SAME, same, "ok", same),
            (SAME, same - 1, "refused", same),
            (SAME, standard, "ok", same),
            (STANDARD, standard, "ok", standard),
            (STANDARD, standard - 1, "refused", standard),
            (STANDARD, same, "refused", standard),      # every other swap keeps the 7x rule
            (None, same, "refused", standard),          # a state.json written before SPACE-RULE-1
            (None, standard, "ok", standard),
        ]
        for rule, free, verdict, need in rows:
            with self.subTest(rule=rule, free=free):
                got, text, state = self.check(rule, free)
                self.assertEqual(got, verdict, text)
                self.assertEqual(state["preflight"]["need"], need)
                self.assertEqual(state["preflight"]["space_rule"], rule or STANDARD)
                if verdict == "refused":
                    self.assertIn(f"private dir has {free} bytes free, the run needs {need}", text)
                    self.assertIn(f"free {need - free} bytes first", text)
                else:
                    self.assertIn(f"preflight: space {free} bytes free >= {need} needed", text)
                    self.assertIn(f"space rule {rule or STANDARD}", text)

    def test_standard_budget_parts_are_unchanged(self):
        _, _, state = self.check(STANDARD, 1 << 50)
        tar, extract, sqlite, _ = self.totals()
        self.assertEqual(state["preflight"]["budget"], swap.space_budget(tar, extract, sqlite)[0])
        _, _, state = self.check(SAME, 1 << 50)
        self.assertNotIn("a2_archives", state["preflight"]["budget"])
        self.assertNotIn("restore_preservation", state["preflight"]["budget"])

    def test_dry_run_names_the_rule_and_reads_no_space(self):
        for rule, needle in ((SAME, f"space rule {SAME}: one verified backup kept"),
                             (STANDARD, "S2/S3 extract + A2 + restore + 512 MiB")):
            with self.subTest(rule=rule):
                driver = bare_driver({"space_rule": rule, "preflight": {}})
                driver.dry = True
                driver.system = SimpleNamespace(disk_free=mock.Mock(side_effect=AssertionError("measured")))
                driver.preflight_budget({})
                self.assertIn(needle, driver.out.getvalue())


class SameImageTrustAddRunTests(rs.RunMixin, unittest.TestCase):
    """Whole runs on the fake runtime: a same-image add, a real swap and the R.1 drop."""

    TAG = "flock-ocr:synthetic"

    def setUp(self):
        super().setUp()
        self.work = self.tmp / "binds" / "work"
        (self.work / ("engine-" + RELEASE[:8])).mkdir(parents=True)
        (self.work / "records-project").mkdir()
        (self.work / "records-project" / "plan.json").write_text("{}\n")
        target = self.fake.find(rs.TARGET)
        target["Mounts"] = [m for m in target["Mounts"] if m["Type"] != "tmpfs"]
        target["Mounts"].append({"Type": "bind", "Source": str(self.work), "Destination": WORK, "RW": True,
                                 "Mode": "", "Propagation": "rprivate"})
        target["HostConfig"]["Binds"].append(f"{self.work}:{WORK}")
        self.wrapper_path = self.tmp / "wrapper" / "run-records.sh"
        rendered_wrapper(self.wrapper_path.parent)
        self.fake.faults["--anchor-status"] = (0, "anchor unavailable: trust file missing\n", "")
        # The image the target already runs, built and V1-verified by an earlier run.
        self.fake.tags[self.TAG] = rs.OLD_IMAGE
        self.prev = self.private / "run-20261021T000000Z"
        self.prev.mkdir(parents=True)
        os.chmod(self.private, 0o700)
        (self.prev / "state.json").write_text(json.dumps({
            "run_dir": str(self.prev), "new_tag": self.TAG, "done": ["V1"], "new_image_id": rs.OLD_IMAGE,
            "wheel_sha256": rs.WHEEL_SHA, "old_image": "sha256:" + "f9" * 32}))
        original = self.fake.docker_api

        def create(method, path, body):
            status, response = original(method, path, body)
            new = self.fake.find(response.get("Id", ""))
            if status == 201 and not path.endswith(swap.PROBE_SUFFIX):
                mounts = [m for m in new["Mounts"] if m.get("Destination") != "/etc/deflock"]
                new["Mounts"] = mounts + ([dict(TRUST_BIND)] if body["HostConfig"].get("Mounts") else [])
            return status, response
        self.fake.docker_api = create

    def surveys(self):
        return [evidence.survey(root) for root in (self.fake.volume, self.fake.bind, self.work)]

    def same_need(self):
        s = self.surveys()
        return (sum(r["tar_bytes"] for r in s) + max(r["extract_bytes"] for r in s)
                + sum(r["sqlite_bytes"] for r in s) + MARGIN)

    def standard_need(self):
        s = self.surveys()
        parts, fmt = swap.space_budget(sum(r["tar_bytes"] for r in s), max(r["extract_bytes"] for r in s),
                                       sum(r["sqlite_bytes"] for r in s))
        return max(7 * sum(r["estimated_copy_bytes"] for r in s) + MARGIN, fmt)

    def swap_argv(self, mode="--apply", trust="add"):
        argv = [mode, "--container", rs.TARGET, "--pause-unit", rs.UNIT, "--private", str(self.private),
                "--new-tag", self.TAG, "--mail-wait", "10", "--reuse-image", str(self.prev), "--trust-mount", trust]
        return argv + (["--records-wrapper", str(self.wrapper_path)] if trust == "add" else [])

    def go(self, argv):
        with mock.patch.object(swap, "_load", return_value=anchor_module()):
            return self.run_swap(argv)

    def new_run(self):
        runs = [p for p in sorted(self.private.glob("run-*")) if p != self.prev]
        self.assertEqual(len(runs), 1)
        return runs[0]

    def test_same_image_add_completes_at_exactly_the_smaller_need(self):
        need = self.same_need()
        self.assertLess(need, self.standard_need())
        self.fake.free = need
        code, out = self.go(self.swap_argv())
        self.assertEqual(code, swap.EXIT_OK, out)
        run = self.new_run()
        state = json.loads((run / "state.json").read_text())
        self.assertEqual(state["space_rule"], SAME)
        self.assertEqual(state["phase"], "complete")
        self.assertEqual(state["preflight"]["need"], need)
        self.assertIn(f"space rule: {SAME} (same image, only the read-only /etc/deflock bind added", out)
        s1 = json.loads((run / "s1" / "space-budget.json").read_text())
        self.assertEqual((s1["space_rule"], s1["required_free_bytes"]), (SAME, need))
        # One verified backup: S1 archives and S2 backups; A2 wrote manifests only.
        self.assertEqual(sorted(p.name for p in (run / "s1").glob("*.tar")), ["m00.tar", "m01.tar", "m02.tar"])
        self.assertTrue((run / "s2" / "sqlite" / "m00" / "state" / "flock.db.backup").is_file())
        self.assertEqual(sorted(p.name for p in (run / "a2").iterdir()),
                         ["diff.json", "m00.baseline.json", "m01.baseline.json", "m02.baseline.json"])
        self.assertIn("the S1 archive (S2 integrity, S3 fresh restore) stays the one verified backup", out)
        new = self.fake.find(rs.TARGET)
        self.assertEqual(new["Image"], rs.OLD_IMAGE)
        self.assertIn(TRUST_BIND, new["Mounts"])
        self.assert_live_unchanged()

    def test_same_image_add_one_byte_short_is_refused_before_any_change(self):
        self.fake.free = self.same_need() - 1
        code, out = self.go(self.swap_argv())
        self.assertEqual(code, swap.EXIT_REFUSED, out)
        self.assertIn(f"FAIL at [R1]: preflight: private dir has {self.fake.free} bytes free, "
                      f"the run needs {self.same_need()}", out)
        self.assertEqual(rs.lifecycle(self.fake), [])

    def test_a_tmpfs_on_the_target_keeps_the_7x_rule(self):
        target = self.fake.find(rs.TARGET)
        target["Mounts"].append({"Type": "tmpfs", "Source": "", "Destination": "/scratch", "RW": True})
        self.fake.free = self.same_need()
        code, out = self.go(self.swap_argv())
        self.assertEqual(code, swap.EXIT_REFUSED, out)
        self.assertIn(f"space rule: {STANDARD} (7x)", out)
        self.assertIn(f"the run needs {self.standard_need()}", out)
        self.assertEqual(rs.lifecycle(self.fake), [])

    def test_the_r1_drop_rollback_regates_on_the_7x_rule(self):
        target = self.fake.find(rs.TARGET)
        target["Mounts"].append(dict(TRUST_BIND))
        target["HostConfig"]["Mounts"] = [dict(swap.TRUST_MOUNT)]
        self.fake.free = self.same_need()
        code, out = self.go(self.swap_argv(trust="drop"))
        self.assertEqual(code, swap.EXIT_REFUSED, out)
        self.assertIn(f"space rule: {STANDARD} (7x)", out)
        self.assertIn(f"FAIL at [R1]: preflight: private dir has {self.fake.free} bytes free, "
                      f"the run needs {self.standard_need()}", out)
        self.assertEqual(rs.lifecycle(self.fake), [])
        self.fake.free = self.standard_need()
        code, out = self.go(self.swap_argv(mode="--dry-run", trust="drop"))
        self.assertEqual(code, swap.EXIT_OK, out)
        self.assertIn(f"space rule: {STANDARD} (7x)", out)

    def test_dry_run_names_the_smaller_rule_and_the_manifest_only_a2(self):
        code, out = self.go(self.swap_argv(mode="--dry-run"))
        self.assertEqual(code, swap.EXIT_OK, out)
        self.assertIn(f"space rule: {SAME}", out)
        self.assertIn(f"would write each a2 live manifest only (space rule {SAME})", out)
        self.assertEqual(rs.lifecycle(self.fake), [])

    def replacement_writes(self):
        def write():
            (self.fake.volume / "state" / "new.txt").write_text("new\n")
        self.fake.hooks["start:" + "9" * 64] = write

    def test_a2_difference_still_holds_and_rollback_preservation_is_gated(self):
        self.replacement_writes()
        self.fake.free = self.same_need()
        code, out = self.go(self.swap_argv())
        self.assertEqual(code, swap.EXIT_HELD, out)
        run = self.new_run()
        self.assertEqual(json.loads((run / "a2" / "diff.json").read_text()), {"m00": ["state/new.txt"]})
        current = evidence.manifest(self.fake.volume)
        estimate = evidence.space_estimate(current["entries"])
        preserve = estimate["tar_bytes"] + estimate["extract_bytes"] + MARGIN
        # Short by one byte: the rollback stops before anything is written or cleared.
        self.fake.free = preserve - 1
        code, out = self.go(["--rollback", str(run)])
        self.assertEqual(code, swap.EXIT_ESCALATE, out)
        self.assertIn(f"m00: preserving the current state needs {preserve} bytes free, the private dir has "
                      f"{preserve - 1} (space rule {SAME}); live data left untouched", out)
        self.assertEqual(evidence.compare(current, evidence.manifest(self.fake.volume)), [])
        self.assertFalse((run / "rollback" / "m00").exists())

    def test_rollback_from_the_hold_with_room_preserves_then_restores(self):
        self.replacement_writes()
        self.fake.free = self.same_need()
        self.assertEqual(self.go(self.swap_argv())[0], swap.EXIT_HELD)
        run = self.new_run()
        code, out = self.go(["--rollback", str(run)])
        self.assertEqual(code, swap.EXIT_ROLLED_BACK, out)
        self.assertTrue((run / "rollback" / "m00" / "attempt-001" / "receipt.json").is_file())
        self.assert_live_unchanged()


class TwoMountRollbackTests(unittest.TestCase):
    """Exercise RB-C across two changed synthetic mounts, not two isolated restores."""

    def setUp(self):
        self.fixture = SameImageTrustAddRunTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.replacement_writes()
        original = self.fixture.fake.hooks["start:" + "9" * 64]

        def write_both():
            original()
            (self.fixture.fake.bind / "replacement.txt").write_bytes(b"synthetic second mount work\n")

        self.fixture.fake.hooks["start:" + "9" * 64] = write_both
        code, out = self.fixture.go(self.fixture.swap_argv())
        self.assertEqual(code, swap.EXIT_HELD, out)
        self.fixture.fake.hooks.clear()
        self.run_path = self.fixture.new_run()
        self.mounts = {"m00": self.fixture.fake.volume, "m01": self.fixture.fake.bind}
        self.before = self.live_bytes()
        self.helpers_before = len(self.fixture.fake.helper_runs)
        self.estimates = {label: evidence.space_estimate(evidence.manifest(root)["entries"])
                          for label, root in self.mounts.items()}

    def live_bytes(self):
        return {label: {p.relative_to(root).as_posix(): p.read_bytes()
                        for p in root.rglob("*") if p.is_file()}
                for label, root in self.mounts.items()}

    def need(self, labels):
        estimates = [self.estimates[label] for label in labels]
        return sum(e["tar_bytes"] for e in estimates) + max(e["extract_bytes"] for e in estimates) + MARGIN

    def rollback(self):
        return self.fixture.go(["--rollback", str(self.run_path)])

    def assert_no_live_write(self):
        helpers = self.fixture.fake.helper_runs[self.helpers_before:]
        self.assertEqual([r for r in helpers if any(d.startswith("/live/") and not ro
                                                  for d, ro in r["mounts"])], [])
        self.assertEqual(self.live_bytes(), self.before)
        self.assertNotIn("restoring", json.loads((self.run_path / "state.json").read_text()))

    def test_aggregate_shortfall_stops_before_any_capture_or_live_write(self):
        # Each mount fits on its own; retaining both archives does not.
        free = max(self.need([label]) for label in self.mounts)
        self.assertLess(free, self.need(self.mounts))
        self.fixture.fake.free = free
        with mock.patch.object(self.fixture.fake, "disk_free", wraps=self.fixture.fake.disk_free) as disk_free:
            code, out = self.rollback()
        self.assertEqual(code, swap.EXIT_ESCALATE, out)
        self.assertIn(f"m00, m01: preserving the current state needs {self.need(self.mounts)} bytes free", out)
        disk_free.assert_called_once_with(self.run_path)
        self.assert_no_live_write()
        self.assertEqual(list((self.run_path / "rollback").glob("*/attempt-*")), [])

    def test_second_mount_verification_failure_leaves_both_live_trees_untouched(self):
        self.fixture.fake.free = self.need(self.mounts)
        self.fixture.fake.faults["manifest /private/rollback/verify/m01-attempt-001"] = (
            1, "", "synthetic second preservation verification failure")
        code, out = self.rollback()
        self.assertEqual(code, swap.EXIT_ESCALATE, out)
        self.assertIn("synthetic second preservation verification failure", out)
        self.assertTrue((self.run_path / "rollback/m00/attempt-001/receipt.json").is_file())
        self.assertFalse((self.run_path / "rollback/m01/attempt-001/receipt.json").exists())
        self.assert_no_live_write()

    def test_both_preservations_verify_before_first_clear_then_both_restore(self):
        self.fixture.fake.free = self.need(self.mounts)
        events = []
        preserve, helper = swap.Swap.preserve_current, swap.Swap.helper

        def preserved(driver, label, current):
            path = preserve(driver, label, current)
            self.assertTrue((path / "receipt.json").is_file())
            events.append("verified:" + label)
            return path

        def effect(driver, argv, *args, **kwargs):
            if argv[0] == "find":
                label = argv[1].rsplit("/", 1)[1]
                events.append("clear:" + label)
                state = json.loads((self.run_path / "state.json").read_text())
                self.assertTrue(state["restoring"][label]["clear_started"])
                self.assertEqual(set(state["preservations"]), set(self.mounts))
                for mount in self.mounts:
                    self.assertTrue((self.run_path / f"rollback/{mount}/attempt-001/receipt.json").is_file())
            elif "-xpf" in argv and argv[-1].startswith("/live/"):
                events.append("extract:" + argv[-1].rsplit("/", 1)[1])
            return helper(driver, argv, *args, **kwargs)

        with mock.patch.object(swap.Swap, "preserve_current", preserved), \
                mock.patch.object(swap.Swap, "helper", effect), \
                mock.patch.object(self.fixture.fake, "disk_free", wraps=self.fixture.fake.disk_free) as disk_free:
            code, out = self.rollback()
        self.assertEqual(code, swap.EXIT_ROLLED_BACK, out)
        disk_free.assert_called_once_with(self.run_path)
        self.assertEqual(events, ["verified:m00", "verified:m01", "clear:m00", "extract:m00",
                                  "clear:m01", "extract:m01"])
        self.fixture.assert_live_unchanged()
        state = json.loads((self.run_path / "state.json").read_text())
        self.assertTrue(all(state["restoring"][label]["extracted"] for label in self.mounts))

    def test_retry_reuses_both_receipts_without_capture_space(self):
        self.fixture.fake.faults["/live/m00 -mindepth 1 -delete"] = (1, "", "synthetic clear failure")
        self.assertEqual(self.rollback()[0], swap.EXIT_ESCALATE)
        self.fixture.fake.faults.clear()
        self.fixture.fake.free = 0
        with mock.patch.object(swap.Swap, "preserve_current", side_effect=AssertionError("recaptured")), \
                mock.patch.object(self.fixture.fake, "disk_free", side_effect=AssertionError("re-gated")):
            code, out = self.rollback()
        self.assertEqual(code, swap.EXIT_ROLLED_BACK, out)
        self.fixture.assert_live_unchanged()

    def test_retry_aggregate_budgets_only_mount_still_needing_capture(self):
        self.fixture.fake.faults["manifest /private/rollback/verify/m01-attempt-001"] = (
            1, "", "synthetic second preservation verification failure")
        self.assertEqual(self.rollback()[0], swap.EXIT_ESCALATE)
        self.fixture.fake.faults.clear()
        self.fixture.fake.free = self.need(["m01"])
        self.assertLess(self.fixture.fake.free, self.need(self.mounts))
        code, out = self.rollback()
        self.assertEqual(code, swap.EXIT_ROLLED_BACK, out)
        self.assertIn("m00: live already preserved", out)
        self.assertTrue((self.run_path / "rollback/m01/attempt-002/receipt.json").is_file())
        self.assertFalse((self.run_path / "rollback/m00/attempt-002").exists())
        self.fixture.assert_live_unchanged()

    def test_second_s1_hash_failure_precedes_all_captures_and_live_writes(self):
        archive = self.run_path / "s1/m01.tar"
        archive.write_bytes(archive.read_bytes() + b"synthetic tamper")
        code, out = self.rollback()
        self.assertEqual(code, swap.EXIT_ESCALATE, out)
        self.assertIn("m01: S1 archive hash differs from the receipt", out)
        self.assert_no_live_write()
        self.assertEqual(list((self.run_path / "rollback").glob("*/attempt-*")), [])


class AcceptedA2ResumeTableTests(unittest.TestCase):
    """Real preservation helpers over synthetic mounts; every runtime call is fake."""

    def test_resume_requires_verified_accepted_state(self):
        rows = [
            ("no space for evidence", "space", swap.EXIT_REFUSED, "private dir has 0 bytes free"),
            ("missing fresh restore evidence", "restore", swap.EXIT_REFUSED, "synthetic restore unavailable"),
            ("malformed accepted SQLite", "sqlite", swap.EXIT_REFUSED, "sqlite-evidence exit"),
            ("verified accepted state", "ok", swap.EXIT_OK, "fresh restore verified"),
        ]
        for name, fault, expected, message in rows:
            with self.subTest(case=name):
                fixture = SameImageTrustAddRunTests(methodName="runTest")
                fixture.setUp()
                try:
                    fixture.replacement_writes()
                    if fault == "sqlite":
                        original = fixture.fake.hooks["start:" + "9" * 64]

                        def write_malformed():
                            original()
                            rs.malformed_sqlite(fixture.fake.volume / "state" / "changed.sqlite")

                        fixture.fake.hooks["start:" + "9" * 64] = write_malformed
                    fixture.fake.free = fixture.same_need()
                    code, out = fixture.go(fixture.swap_argv())
                    self.assertEqual(code, swap.EXIT_HELD, out)
                    run = fixture.new_run()
                    reviewed = {p.name: p.read_bytes() for p in (run / "a2").glob("*.json")}
                    live = {p: evidence.manifest(p) for p in
                            (fixture.fake.volume, fixture.fake.bind, fixture.work)}
                    before = list(rs.lifecycle(fixture.fake))
                    fixture.fake.hooks.clear()
                    # No existing evidence: success must produce and check it now.
                    self.assertEqual(list((run / "a2").glob("accepted-*")), [])
                    fixture.fake.free = 0 if fault == "space" else 1 << 40
                    if fault == "restore":
                        fixture.fake.faults["manifest /private/a2/accepted-"] = (
                            1, "", "synthetic restore unavailable")

                    def verified_before_start():
                        state = json.loads((run / "state.json").read_text())
                        prefix = run / state["a2_accepted_evidence"]
                        for label in ("m00", "m01", "m02"):
                            self.assertTrue((prefix / f"{label}.tar").is_file())
                            report = json.loads((prefix / f"{label}.sqlite-evidence.json").read_text())
                            self.assertTrue(report["ok"])
                            baseline = json.loads((run / "a2" / f"{label}.baseline.json").read_text())
                            restored = json.loads((prefix / f"{label}.restored.json").read_text())
                            self.assertEqual(evidence.compare(baseline, restored), [])
                        self.assertTrue((prefix / "sqlite/m00/state/flock.db.backup").is_file())

                    fixture.fake.hooks["docker start"] = verified_before_start
                    digest = swap.sha256_file(run / "a2/diff.json")
                    code, out = fixture.go(["--resume", str(run), "--accept-a2", digest])
                    self.assertEqual(code, expected, out)
                    self.assertIn(message, out)
                    self.assertEqual(reviewed, {p.name: p.read_bytes() for p in (run / "a2").glob("*.json")})
                    state = json.loads((run / "state.json").read_text())
                    for path, baseline in live.items():
                        self.assertEqual(evidence.compare(baseline, evidence.manifest(path)), [])
                    if expected == swap.EXIT_OK:
                        self.assertEqual(state["phase"], "complete")
                        self.assertEqual(rs.lifecycle(fixture.fake)[len(before):], [
                            "start new", "start sidecar", "systemctl start synthetic-mail.timer"])
                    else:
                        self.assertEqual(state["phase"], "held_a2")
                        self.assertNotIn("a2_accepted_evidence", state)
                        self.assertNotIn("a2_accepted", state)
                        self.assertNotIn("A2", state["done"])
                        self.assertEqual(rs.lifecycle(fixture.fake), before)
                        self.assertFalse(any(fixture.fake.find(n)["State"]["Running"]
                                             for n in (rs.TARGET, rs.SIDECAR)))
                        self.assertEqual(fixture.fake.units[f"{rs.UNIT}.timer"], "inactive")
                    if fault == "restore":
                        # An interrupted attempt must neither be overwritten nor
                        # treated as verified on retry.
                        attempts = list((run / "a2").glob("accepted-*"))
                        self.assertEqual(len(attempts), 1)
                        old_archive = swap.sha256_file(attempts[0] / "m00.tar")
                        fixture.fake.faults.clear()
                        code, out = fixture.go(["--resume", str(run), "--accept-a2", digest])
                        self.assertEqual(code, swap.EXIT_OK, out)
                        self.assertEqual(len(list((run / "a2").glob("accepted-*"))), 2)
                        self.assertEqual(swap.sha256_file(attempts[0] / "m00.tar"), old_archive)
                finally:
                    fixture.doCleanups()


if __name__ == "__main__":
    unittest.main()
