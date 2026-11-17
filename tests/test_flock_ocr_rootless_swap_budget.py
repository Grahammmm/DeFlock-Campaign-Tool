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
        mounts = [{"Type": "volume", "Name": "home", "Source": "/var/lib/docker/volumes/home/_data",
                   "Destination": "/home/coder", "RW": True},
                  {"Type": "bind", "Source": str(self.bind_a), "Destination": "/workspace/archive", "RW": True},
                  {"Type": "bind", "Source": str(self.bind_b), "Destination": "/workspace/work", "RW": True},
                  *extra]
        return rs._container("a" * 64, "synthetic", image, True, mounts, ["home:/home/coder"], [], {})

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


if __name__ == "__main__":
    unittest.main()
