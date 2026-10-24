"""Synthetic swap hardening (rebuilt from PR #161): extract budget, isolated builds, conservative prune.

Helpers execute locally via FakeSystem; no daemon, timer or real run is touched.
"""
import json
import os
import unittest
from pathlib import Path
from unittest import mock

from tests.test_flock_ocr_rootless_swap import RunMixin, swap, evidence, OLD_IMAGE, TARGET


class BuildAndBudgetTests(RunMixin, unittest.TestCase):
    def test_a_failed_step_leaves_its_extract_next_to_the_rollback_verify_extract(self):
        # Why the budgets count the largest extract twice: [A2] sqlite-evidence
        # fails after extracting m00, RB-C then preserves the replacement's
        # writes and verifies that archive by extracting it, with the failed
        # step's extract still on disk.
        def write():
            (self.fake.volume / "state" / "new.txt").write_text("written by the replacement\n")
        self.fake.hooks["start:" + "9" * 64] = write
        self.fake.faults["sqlite-evidence /private/a2/inspect/m00"] = (1, "", "synthetic evidence failure")
        seen = []
        self.fake.hooks["-C /private/rollback/verify/m00-attempt-001"] = lambda: seen.append(
            sorted(p.name for p in (self.run_dir() / "a2" / "inspect" / "m00").iterdir()))
        code, out = self.run_swap(self.args())
        self.assertEqual(code, swap.EXIT_ROLLED_BACK, out)
        self.assertIn("FAIL at [A2]: m00: sqlite-evidence exit 1", out)
        self.assertEqual(len(seen), 1, out)
        self.assertIn("state", seen[0])  # the failed step's extract was still there
        self.assertTrue((self.run_dir() / "a2" / "inspect" / "m00" / "state" / "flock.db").is_file())
        self.assertTrue((self.run_dir() / "rollback" / "m00" / "attempt-001" / "receipt.json").is_file())
        self.assert_live_unchanged()

    def test_budget_counts_the_largest_extract_twice_at_r1_and_a2(self):
        full, _ = swap.space_budget(100, 7, 3)
        a2, _ = swap.a2_budget(100, 7, 3)
        for parts in (full, a2):
            self.assertEqual((parts["largest_extract"], parts["failed_step_extract"]), (7, 7))

    def test_failed_build_never_blocks_the_next_run(self):
        self.fake.faults["docker build"] = (1, "", "synthetic failed build")
        code, out = self.run_swap(self.args())
        self.assertEqual(code, swap.EXIT_ROLLED_BACK, out)
        self.assertIn("FAIL at [B2]", out)
        self.fake.faults.clear()
        code, out = self.run_swap(self.args())
        self.assertEqual(code, 0, out)
        contexts = [a[-1] for a in self.fake.calls if a[:2] == ["docker", "build"]]
        self.assertEqual(len(set(contexts)), 2)
        for context in contexts:
            self.assertEqual(Path(context).name, "build")
            self.assertEqual(Path(context).parent.parent, self.private)
        self.assertFalse(self.dist.exists())


class PruneTests(RunMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.private.mkdir(mode=0o700)

    def make_run(self, number, phase="rolled_back", reuse=None, name=None, private=None):
        private = private or self.private
        path = self.private / (name or f"run-20261019T2000{number:02d}Z")
        path.mkdir(mode=0o700)
        stored = Path(private) / path.name  # as the driver stores it: --private as given
        state = {"run_dir": str(stored), "private": str(private), "phase": phase, "helper_image": OLD_IMAGE}
        if reuse is not None:
            state["reuse"] = {"run_dir": str(reuse)}
        (path / "state.json").write_text(json.dumps(state))
        (path / "evidence").mkdir()
        (path / "evidence/archive.tar").write_bytes(b"synthetic archive")
        return path

    def add_rollback_archive(self, path):
        attempt = path / "rollback" / "m00" / "attempt-001"
        attempt.mkdir(parents=True)
        (attempt / "current.tar").write_bytes(b"only copy of what the replacement wrote")
        return attempt

    def prune(self, *args, private=None):
        return self.run_swap(["prune", "--private", str(private or self.private), *args])

    def make_orphan(self, number, step="s2", name=None):
        path = self.private / (name or f"run-20261019T2000{number:02d}Z")
        path.mkdir(mode=0o700)
        if step is not None:
            (path / step / "extract").mkdir(parents=True)
            (path / step / "extract/leftover").write_bytes(b"synthetic remnant")
        return path

    # -- rollback/ protection (review of PR #161) --

    def test_rolled_back_run_with_rollback_archives_is_kept_without_the_flag(self):
        old = self.make_run(1, "rolled_back")
        attempt = self.add_rollback_archive(old)
        self.make_run(2, "complete")
        code, out = self.prune("--apply")
        self.assertEqual(code, 0, out)
        self.assertIn(f"KEEP {old}: rollback/ holds preserved pre-restore state (possibly the only copy of what "
                      f"the replacement wrote); requires {swap.prune_runs.ROLLBACK_FLAG}", out)
        self.assertEqual((attempt / "current.tar").read_bytes(), b"only copy of what the replacement wrote")
        self.assertIn("prune: no eligible runs", out)
        self.assertEqual(self.fake.calls, [])

    def test_rollback_archives_are_pruned_only_with_the_explicit_flag(self):
        old = self.make_run(1, "rolled_back")
        self.add_rollback_archive(old)
        newest = self.make_run(2, "complete")
        code, out = self.prune("--include-rollback-archives")
        self.assertEqual(code, 0, out)
        self.assertIn(f"PRUNE {old}: superseded terminal run", out)
        self.assertTrue(old.exists())  # dry run
        code, out = self.prune("--apply", "--include-rollback-archives")
        self.assertEqual(code, 0, out)
        self.assertFalse(old.exists())
        self.assertTrue(newest.exists())

    def test_empty_rollback_dir_does_not_protect_and_any_entry_does(self):
        empty, other = self.make_run(1), self.make_run(2)
        (empty / "rollback").mkdir()
        (other / "rollback").mkdir()
        (other / "rollback" / "compare.json").write_text('{"differs": []}\n')
        self.make_run(3)
        code, out = self.prune()
        self.assertEqual(code, 0, out)
        self.assertIn(f"PRUNE {empty}: superseded terminal run", out)
        self.assertIn(f"KEEP {other}: rollback/ holds preserved pre-restore state", out)

    def test_orphan_with_rollback_archives_needs_the_flag_too(self):
        orphan = self.make_orphan(1, "rollback")
        self.make_run(2)
        args = ("--apply", "--include-orphans", "--helper-image", OLD_IMAGE)
        code, out = self.prune(*args)
        self.assertEqual(code, 0, out)
        self.assertIn(f"KEEP {orphan}: orphan remnant (missing state.json); rollback/ holds", out)
        self.assertTrue(orphan.exists())
        code, out = self.prune(*args, "--include-rollback-archives")
        self.assertEqual(code, 0, out)
        self.assertFalse(orphan.exists())

    # -- resolved path comparison (review of PR #161) --

    def aliased_private(self):
        """--private reached through a symlinked parent (the driver refuses a symlinked --private itself)."""
        link = self.tmp / "link"
        link.symlink_to(self.tmp, target_is_directory=True)
        return link / self.private.name

    def test_symlinked_parent_of_private_matches_the_stored_unresolved_paths(self):
        alias = self.aliased_private()
        old = self.make_run(1, private=alias)  # state.json stores alias/run-..., as the driver does
        newest = self.make_run(2, private=alias)
        self.assertNotEqual(json.loads((old / "state.json").read_text())["run_dir"], str(old.resolve()))
        for given in (alias, self.private):
            with self.subTest(private=given):
                code, out = self.prune(private=given)
                self.assertEqual(code, 0, out)
                self.assertIn(f"PRUNE {old.resolve()}: superseded terminal run", out)
        code, out = self.prune("--apply", private=alias)
        self.assertEqual(code, 0, out)
        self.assertFalse(old.exists())
        self.assertTrue(newest.exists())
        command = next(a for a in self.fake.calls if a[:2] == ["docker", "run"])
        self.assertIn(f"type=bind,src={old.resolve()},dst=/private", command)  # resolved mount source

    def test_reuse_reference_through_a_symlinked_parent_is_protected(self):
        alias = self.aliased_private()
        source = self.make_run(1, private=alias)
        self.make_run(2, reuse=alias / source.name, private=alias)
        self.make_run(3, private=alias)
        code, out = self.prune("--apply", "--reuse-image", str(alias / source.name), private=alias)
        self.assertEqual(code, 0, out)
        self.assertIn(f"KEEP {source.resolve()}: referenced by --reuse-image", out)
        self.assertTrue(source.exists())

    def test_state_naming_another_place_still_fails_closed(self):
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir(mode=0o700)
        old = self.make_run(1, private=elsewhere)  # resolves to elsewhere/run-..., not this run
        self.make_run(2)
        code, out = self.prune("--apply")
        self.assertEqual(code, 2, out)
        self.assertIn(f"state.json identity mismatch: {old.name}", out)
        self.assertTrue(old.exists())
        self.assertEqual(self.fake.calls, [])

    def test_prune_accepts_the_state_the_driver_writes_through_a_symlinked_parent(self):
        self.private.rmdir()
        alias = self.aliased_private()
        for n in range(2):  # two completed swaps on fresh fakes, the driver storing --private as given
            self.fake = type(self.fake)(self.tmp / f"host{n}")
            self.fake.clock = 30 * n  # a later run directory name
            argv = [str(alias) if a == str(self.private) else a for a in self.args()]
            code, out = self.run_swap(argv)
            self.assertEqual(code, 0, out)
        first, second = sorted(self.private.glob("run-*"))
        self.assertTrue(json.loads((first / "state.json").read_text())["run_dir"].startswith(str(alias)))
        code, out = self.prune("--apply", private=alias)
        self.assertEqual(code, 0, out)
        self.assertIn(f"PRUNE {first.resolve()}: superseded terminal run", out)
        self.assertIn(f"KEEP {second.resolve()}: newest run, latest completed swap rollback point", out)
        self.assertFalse(first.exists())
        self.assertTrue(second.exists())

    # -- ported from PR #161 --

    def test_attempt_one_two_orphan_layout_preview_and_opt_in_apply(self):
        first = self.make_orphan(1, "s2", "run-20261019T223755Z")
        second = self.make_orphan(2, "s3", "run-20261020T005344Z")
        third = self.make_orphan(3, "build", "run-20261020T005500Z")
        newest = self.make_run(4, name="run-20261020T010000Z")
        before = evidence.manifest(self.private)
        code, out = self.prune()
        self.assertEqual(code, 0, out)
        for path in (first, second, third):
            self.assertIn(f"KEEP {path}: orphan remnant (missing state.json)", out)
        self.assertIn("requires --include-orphans", out)
        self.assertIn("requires immutable --helper-image", out)
        self.assertEqual(evidence.manifest(self.private), before)
        code, out = self.prune("--include-orphans", "--helper-image", OLD_IMAGE)
        self.assertEqual(code, 0, out)
        for path in (first, second, third):
            self.assertIn(f"PRUNE {path}: orphan remnant (missing state.json)", out)
        self.assertEqual(evidence.manifest(self.private), before)
        self.assertEqual(self.fake.calls, [])
        code, out = self.prune("--apply", "--include-orphans", "--helper-image", OLD_IMAGE)
        self.assertEqual(code, 0, out)
        self.assertFalse(first.exists() or second.exists() or third.exists())
        self.assertTrue(newest.exists())
        self.assertEqual(len(self.fake.helper_runs), 3)
        for helper in self.fake.helper_runs:
            self.assertEqual(helper["mounts"], [("/private", False), ("/recipe", True)])

    def test_build_is_a_known_step_directory(self):
        self.assertIn("build", swap.prune_runs.STEP_DIRS)

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
        self.assertIn("not a real absolute directory", out)
        self.assertTrue(old.exists())
        self.assertTrue(newest.exists())
        self.assertEqual(self.fake.calls, [])

    def test_active_swap_lock_blocks_both_preview_and_apply(self):
        old = self.make_run(1)
        self.make_run(2)
        with swap._lock(self.private):
            for args in ((), ("--apply",)):
                with self.subTest(args=args):
                    code, out = self.prune(*args)
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

    def test_prune_is_not_a_swap_mode(self):
        with mock.patch.object(swap.Swap, "run_main") as run_main:
            code, out = self.prune()
        self.assertEqual(code, 0, out)
        run_main.assert_not_called()
        self.assertIn("prune: dry-run; 0 eligible run(s); nothing changed", out)


if __name__ == "__main__":
    unittest.main()
