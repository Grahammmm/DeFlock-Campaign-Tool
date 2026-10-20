"""Synthetic swap hardening: isolated builds, peak disk budget, conservative prune.

Helpers execute locally via FakeSystem; no daemon, timer or real run is touched.
"""
import contextlib
import io
import json
import os
import shutil
import unittest
from pathlib import Path
from unittest import mock

from tests.test_flock_ocr_rootless_swap import (
    RunMixin, swap, evidence, RECIPE, OLD_IMAGE, TARGET,
)


class BuildAndBudgetTests(RunMixin, unittest.TestCase):
    def estimated_copy(self):
        entries = [e for root in (self.fake.volume, self.fake.bind)
                   for e in evidence.manifest(root)["entries"].values()]
        return sum(((e.get("size", 0) + 4095) // 4096) * 4096 + 4096 for e in entries)

    def test_failed_build_never_blocks_next_run(self):
        self.fake.faults["docker build"] = (1, "", "synthetic failed build")
        code, out = self.run_swap(self.args())
        self.assertEqual(code, swap.EXIT_ROLLED_BACK, out)
        first = self.run_dir()
        wheel = next((first / "b1/context/dist").iterdir())
        original = wheel.read_bytes()
        self.fake.faults.clear()
        code, out = self.run_swap(self.args())
        self.assertEqual(code, 0, out)
        contexts = [a[-1] for a in self.fake.calls if a[:2] == ["docker", "build"]]
        self.assertEqual(len(set(contexts)), 2)
        self.assertEqual(wheel.read_bytes(), original)
        for context in contexts:
            self.assertEqual(Path(context).parents[1].parent, self.private)

    def test_b1_never_reads_or_changes_recipe_dist(self):
        # Use a copy of recipe control files, with a deliberately unusable dist.
        recipe = self.tmp / "recipe"
        recipe.mkdir()
        for name in (".dockerignore", "requirements.lock", "pins.json"):
            shutil.copyfile(RECIPE / name, recipe / name)
        (recipe / "dist").symlink_to(self.tmp / "missing")
        args = swap.parse_args(self.args())
        driver = swap.Swap(args, system=self.fake, out=io.StringIO())
        run = self.tmp / "run"
        run.mkdir()
        (run / "snapshot-receipt.json").write_text('{"snapshot_ok": true}')
        driver.state = {"run_dir": str(run), "build_python": "/usr/bin/python3"}
        with mock.patch.object(swap, "RECIPE", recipe):
            driver.b1_wheel()
        self.assertTrue((recipe / "dist").is_symlink())
        self.assertFalse((self.tmp / "missing").exists())
        self.assertTrue((run / "b1/context/dist").is_dir())

    def test_old_three_copy_budget_refused_before_first_archive(self):
        total = self.estimated_copy()
        self.fake.disk_free = lambda path: 3 * total + (512 << 20)
        code, out = self.run_swap(self.args())
        self.assertEqual(code, swap.EXIT_ROLLED_BACK, out)
        self.assertIn("FAIL at [S1]", out)
        self.assertFalse(any("-cpf" in h["command"] for h in self.fake.helper_runs))
        budget = json.loads((self.run_dir() / "s1/space-budget.json").read_text())
        self.assertEqual(budget["required_free_bytes"], 7 * total + (512 << 20))
        self.assertEqual(budget["copies"], {"s1_evidence": 2, "a2_evidence": 2,
                                          "scratch_extract": 1, "rollback_preservation": 2})
        self.assertEqual(self.fake.api_calls, [])

    def test_exact_peak_budget_passes_and_a2_excludes_existing_s1(self):
        total = self.estimated_copy()
        started = []
        self.fake.hooks["start:" + "9" * 64] = lambda: started.append(True)
        self.fake.disk_free = lambda path: (5 if started else 7) * total + (512 << 20)
        code, out = self.run_swap(self.args())
        self.assertEqual(code, 0, out)
        budget = json.loads((self.run_dir() / "a2/space-budget.json").read_text())
        self.assertEqual(budget["required_free_bytes"], 5 * total + (512 << 20))

    def test_a2_rechecks_remaining_space_before_archive(self):
        total = self.estimated_copy()
        started = []
        self.fake.hooks["start:" + "9" * 64] = lambda: started.append(True)
        self.fake.disk_free = lambda path: ((5 * total + (512 << 20) - 1) if started else 1 << 40)
        code, out = self.run_swap(self.args())
        self.assertEqual(code, swap.EXIT_ROLLED_BACK, out)
        self.assertIn("FAIL at [A2]", out)
        self.assertFalse(any("-cpf" in h["command"] and any("/a2/" in a for a in h["command"])
                             for h in self.fake.helper_runs))
        target = self.fake.find(TARGET)
        assert target is not None
        self.assertEqual(target["Image"], OLD_IMAGE)

    def test_preflight_checks_isolated_context_and_preserves_default(self):
        context = self.tmp / "context"
        (context / "dist").mkdir(parents=True)
        for name in (".dockerignore", "requirements.lock"):
            shutil.copyfile(RECIPE / name, context / name)
        pins = json.loads((RECIPE / "pins.json").read_text())
        wheel = context / "dist" / pins["engine"]["wheel_filename"]
        wheel.write_bytes(b"synthetic wheel")
        argv = ["--base-image", OLD_IMAGE, "--engine-commit", pins["engine"]["commit"],
                "--wheel-sha256", swap.sha256_file(wheel), "--skip-docker", "--context", str(context)]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = swap.preflight.main(argv)
        self.assertEqual(code, 0, out.getvalue())
        self.assertTrue(out.getvalue().strip().endswith(str(context)))
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = swap.preflight.main(argv[:5] + ["0" * 64] + argv[6:])
        self.assertEqual(code, 1)


class PruneTests(RunMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.private.mkdir(mode=0o700)

    def make_run(self, number, phase="rolled_back", reuse=None, name=None):
        path = self.private / (name or f"run-20261019T2000{number:02d}Z")
        path.mkdir(mode=0o700)
        state: dict = {"run_dir": str(path), "private": str(self.private), "phase": phase,
                 "helper_image": OLD_IMAGE}
        if reuse is not None:
            state["reuse"] = {"run_dir": str(reuse)}
        (path / "state.json").write_text(json.dumps(state))
        (path / "evidence").mkdir()
        (path / "evidence/archive.tar").write_bytes(b"synthetic archive")
        return path

    def prune(self, *args):
        return self.run_swap(["prune", "--private", str(self.private), *args])

    def make_orphan(self, number, step: str | None = "s2", name=None):
        path = self.private / (name or f"run-20261019T2000{number:02d}Z")
        path.mkdir(mode=0o700)
        if step is not None:
            (path / step / "extract").mkdir(parents=True)
            (path / step / "extract/leftover").write_bytes(b"synthetic remnant")
        return path

    def test_attempt_one_two_orphan_layout_preview_and_opt_in_apply(self):
        first = self.make_orphan(1, "s2", "run-20261019T223755Z")
        second = self.make_orphan(2, "s3", "run-20261020T005344Z")
        newest = self.make_run(3, name="run-20261020T010000Z")
        before = evidence.manifest(self.private)
        code, out = self.prune()
        self.assertEqual(code, 0, out)
        for path in (first, second):
            self.assertIn(f"KEEP {path}: orphan remnant (missing state.json)", out)
        self.assertIn("requires --include-orphans", out)
        self.assertIn("requires immutable --helper-image", out)
        self.assertEqual(evidence.manifest(self.private), before)
        code, out = self.prune("--include-orphans", "--helper-image", OLD_IMAGE)
        self.assertEqual(code, 0, out)
        for path in (first, second):
            self.assertIn(f"PRUNE {path}: orphan remnant (missing state.json)", out)
        self.assertEqual(evidence.manifest(self.private), before)
        self.assertEqual(self.fake.calls, [])
        code, out = self.prune("--apply", "--include-orphans", "--helper-image", OLD_IMAGE)
        self.assertEqual(code, 0, out)
        self.assertFalse(first.exists())
        self.assertFalse(second.exists())
        self.assertTrue(newest.exists())
        self.assertEqual(len(self.fake.helper_runs), 2)
        for helper in self.fake.helper_runs:
            self.assertEqual(helper["mounts"], [("/private", False), ("/recipe", True)])

    def test_orphan_requires_both_opt_in_and_immutable_helper(self):
        orphan = self.make_orphan(1)
        self.make_run(2)
        for args in ((), ("--include-orphans",), ("--helper-image", OLD_IMAGE),
                     ("--include-orphans", "--helper-image", "mutable:latest")):
            with self.subTest(args=args):
                code, out = self.prune("--apply", *args)
                self.assertEqual(code, 0, out)
                self.assertIn(f"KEEP {orphan}", out)
                self.assertTrue((orphan / "s2/extract/leftover").exists())
        self.assertEqual(self.fake.calls, [])

    def test_orphan_newest_and_all_reuse_references_remain_protected(self):
        recorded = self.make_orphan(1)
        explicit = self.make_orphan(2)
        referrer = self.make_run(3, reuse=recorded)
        newest = self.make_orphan(4)
        code, out = self.prune("--apply", "--include-orphans", "--helper-image", OLD_IMAGE,
                               "--reuse-image", str(explicit))
        self.assertEqual(code, 0, out)
        for path in (recorded, explicit, newest):
            self.assertTrue(path.exists())
            self.assertIn(f"KEEP {path}", out)
        self.assertFalse(referrer.exists())
        self.assertIn("newest run", out)
        self.assertIn("referenced by --reuse-image", out)

    def test_orphan_unknown_entry_file_or_symlink_is_not_eligible(self):
        unknown = self.make_orphan(1, "unknown")
        file_entry = self.make_orphan(2, None)
        (file_entry / "s1").write_text("not a directory")
        link_entry = self.make_orphan(3, None)
        (link_entry / "s3").symlink_to(self.fake.volume, target_is_directory=True)
        self.make_run(4)
        code, out = self.prune("--apply", "--include-orphans", "--helper-image", OLD_IMAGE)
        self.assertEqual(code, 0, out)
        for path in (unknown, file_entry, link_entry):
            self.assertTrue(path.exists())
            self.assertIn(f"KEEP {path}", out)
        self.assertIn("top level must contain only known step directories", out)
        self.assertEqual(self.fake.calls, [])

    def test_empty_orphan_after_state_removal_can_be_pruned(self):
        orphan = self.make_orphan(1, None)
        self.make_run(2)
        code, out = self.prune("--apply", "--include-orphans", "--helper-image", OLD_IMAGE)
        self.assertEqual(code, 0, out)
        self.assertFalse(orphan.exists())

    def test_latest_complete_rollback_point_survives_later_refusal(self):
        superseded = self.make_run(1, "complete")
        rollback_point = self.make_run(2, "complete")
        refused = self.make_run(3, "refused")
        before = evidence.manifest(rollback_point)
        code, out = self.prune("--apply")
        self.assertEqual(code, 0, out)
        self.assertIn(f"KEEP {rollback_point}: latest completed swap rollback point", out)
        self.assertEqual(evidence.manifest(rollback_point), before)
        self.assertTrue(refused.exists())
        self.assertFalse(superseded.exists())

    def test_default_dry_run_is_byte_preserving_and_creates_no_lock(self):
        old, newest = self.make_run(1), self.make_run(2)
        before = evidence.manifest(self.private)
        code, out = self.prune()
        self.assertEqual(code, 0, out)
        self.assertIn(f"PRUNE {old}", out)
        self.assertIn(f"KEEP {newest}: newest run", out)
        self.assertEqual(evidence.manifest(self.private), before)
        self.assertEqual(self.fake.calls, [])
        self.assertFalse((self.private / ".flock-swap.lock").exists())

    def test_apply_only_mounts_candidate_and_readonly_recipe(self):
        old, newest = self.make_run(1), self.make_run(2)
        # A symlink outside the candidate is unlinked, never traversed.
        (old / "outside").symlink_to(self.fake.volume, target_is_directory=True)
        (old / "evidence/archive.tar").chmod(0o400)
        live = evidence.manifest(self.fake.volume)
        saved = evidence.manifest(newest)
        code, out = self.prune("--apply")
        self.assertEqual(code, 0, out)
        self.assertFalse(old.exists())
        self.assertEqual(evidence.manifest(newest), saved)
        self.assertEqual(evidence.manifest(self.fake.volume), live)
        self.assertEqual(len(self.fake.helper_runs), 1)
        helper = self.fake.helper_runs[0]
        self.assertEqual(helper["mounts"], [("/private", False), ("/recipe", True)])
        self.assertIn("/recipe/prune_runs.py", helper["command"])
        self.assertEqual(self.fake.events, [])
        command = next(a for a in self.fake.calls if a[:2] == ["docker", "run"])
        self.assertIn(f"type=bind,src={old},dst=/private", command)
        self.assertEqual(command[command.index("--user") + 1], "0:0")
        self.assertNotIn("--privileged", command)

    def test_reuse_references_and_future_explicit_reuse_are_kept(self):
        source = self.make_run(1)
        future = self.make_run(2)
        referrer = self.make_run(3, reuse=source)
        newest = self.make_run(4)
        code, out = self.prune("--apply", "--reuse-image", str(future))
        self.assertEqual(code, 0, out)
        self.assertTrue(source.exists())
        self.assertTrue(future.exists())
        self.assertTrue(newest.exists())
        self.assertFalse(referrer.exists())

    def test_unfinished_and_unknown_phases_are_never_pruned(self):
        paths = [self.make_run(i, phase) for i, phase in enumerate(
            ("running", "held_a2", "escalated", "rollback:RB-C", "unknown"), 1)]
        self.make_run(6)
        code, out = self.prune("--apply")
        self.assertEqual(code, 0, out)
        self.assertTrue(all(p.exists() for p in paths))
        self.assertEqual(self.fake.calls, [])

    def test_unknown_or_symlinked_state_fails_closed_before_deletion(self):
        old, newest = self.make_run(1), self.make_run(2)
        state = old / "state.json"
        state.unlink()
        state.symlink_to(newest / "state.json")
        code, out = self.prune("--apply")
        self.assertEqual(code, 2, out)
        self.assertIn("not a regular state.json", out)
        self.assertTrue((old / "evidence/archive.tar").exists())
        self.assertEqual(self.fake.calls, [])

    def test_malformed_state_refuses_all_candidates(self):
        old = self.make_run(1)
        newest = self.make_run(2)
        (newest / "state.json").write_text("{}")
        code, out = self.prune("--apply")
        self.assertEqual(code, 2, out)
        self.assertTrue(old.exists())
        self.assertEqual(self.fake.calls, [])

    def test_missing_recorded_reuse_reference_is_logged_and_ignored(self):
        old = self.make_run(1)
        absent = self.private / "run-20261019T200000Z"
        newest = self.make_run(2, reuse=absent)
        before = evidence.manifest(self.private)
        code, out = self.prune()
        self.assertEqual(code, 0, out)
        self.assertIn(f"reference target already absent: {absent}", out)
        self.assertIn(f"PRUNE {old}", out)
        self.assertEqual(evidence.manifest(self.private), before)
        code, out = self.prune("--apply")
        self.assertEqual(code, 0, out)
        self.assertIn(f"reference target already absent: {absent}", out)
        self.assertFalse(old.exists())
        self.assertTrue(newest.exists())

    def test_missing_explicit_reuse_reference_still_refuses(self):
        old = self.make_run(1)
        self.make_run(2)
        code, out = self.prune("--apply", "--reuse-image", str(self.private / "run-20261019T200000Z"))
        self.assertEqual(code, 2, out)
        self.assertIn("not a known direct run", out)
        self.assertTrue(old.exists())
        self.assertEqual(self.fake.calls, [])

    def test_missing_recorded_reference_outside_private_still_refuses(self):
        old = self.make_run(1)
        self.make_run(2, reuse=self.tmp / "run-20261019T200000Z")
        code, out = self.prune("--apply")
        self.assertEqual(code, 2, out)
        self.assertIn("not a known direct run", out)
        self.assertTrue(old.exists())
        self.assertEqual(self.fake.calls, [])

    def test_missing_recorded_reference_with_unknown_name_still_refuses(self):
        old = self.make_run(1)
        self.make_run(2, reuse=self.private / "unknown")
        code, out = self.prune("--apply")
        self.assertEqual(code, 2, out)
        self.assertTrue(old.exists())
        self.assertEqual(self.fake.calls, [])

    def test_rejects_outside_explicit_reference_and_run_symlink(self):
        old, newest = self.make_run(1), self.make_run(2)
        code, out = self.prune("--reuse-image", str(self.tmp))
        self.assertEqual(code, 2, out)
        (self.private / "run-20261019T200003Z").symlink_to(self.fake.volume)
        code, out = self.prune("--apply")
        self.assertEqual(code, 2, out)
        self.assertTrue(old.exists())
        self.assertTrue(newest.exists())
        self.assertEqual(self.fake.calls, [])

    def test_active_swap_lock_blocks_both_preview_and_apply(self):
        old = self.make_run(1)
        self.make_run(2)
        with swap._lock(self.private):
            code, out = self.prune()
            self.assertEqual(code, 2, out)
            code, out = self.prune("--apply")
            self.assertEqual(code, 2, out)
            self.assertIn("holds the lock", out)
        self.assertTrue(old.exists())
        self.assertEqual(self.fake.calls, [])

    def test_rootful_daemon_or_helper_failure_never_removes_run(self):
        old = self.make_run(1)
        self.make_run(2)
        self.fake.rootless = False
        code, out = self.prune("--apply")
        self.assertEqual(code, 2, out)
        self.assertTrue((old / "state.json").exists())
        self.fake.rootless = True
        self.fake.faults["clear-contents"] = (1, "", "helper failed")
        code, out = self.prune("--apply")
        self.assertEqual(code, 2, out)
        self.assertTrue((old / "state.json").exists())

    def test_mounted_candidate_refused_even_if_container_stopped(self):
        old = self.make_run(1)
        self.make_run(2)
        target = self.fake.find(TARGET)
        assert target is not None
        target["State"]["Running"] = False
        target["Mounts"].append({"Source": str(old / "evidence")})
        code, out = self.prune("--apply")
        self.assertEqual(code, 2, out)
        self.assertIn("overlapping", out)
        self.assertTrue(old.exists())
        self.assertEqual(self.fake.helper_runs, [])

    def test_missing_private_is_not_created(self):
        code, out = self.run_swap(["prune", "--private", str(self.tmp / "absent")])
        self.assertEqual(code, 2, out)
        self.assertFalse((self.tmp / "absent").exists())

    def test_nested_mount_is_refused_before_any_cleanup(self):
        old = self.make_run(1)
        with mock.patch.object(os.path, "ismount", side_effect=lambda p: Path(p).name == "evidence"):
            with self.assertRaisesRegex(ValueError, "nested mount"):
                swap.prune_runs.clear_contents(old)
        self.assertTrue((old / "state.json").exists())
        self.assertTrue((old / "evidence/archive.tar").exists())


    def test_same_filesystem_nested_bind_is_refused_from_mountinfo(self):
        old = self.make_run(1)
        mountinfo = f"1 2 0:3 / {old}/evidence rw - ext4 synthetic rw\n"
        with mock.patch.object(Path, "read_text", return_value=mountinfo):
            with self.assertRaisesRegex(ValueError, "nested mount"):
                swap.prune_runs.clear_contents(old)
        self.assertTrue((old / "state.json").exists())


if __name__ == "__main__":
    unittest.main()
