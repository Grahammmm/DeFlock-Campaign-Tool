"""RF2150-3: agency-action staging, replacement and cleanup stay in the admitted content directory (synthetic, offline)."""
import os
import unittest
from unittest import mock

from campaign_tool import agency_actions as actions
from campaign_tool.agency_actions import ActionError, add_action, remove_action
from tests.test_agency_actions import Base, document, row, POLICE, TODAY


class WriterPathTests(Base):
    def swap_during_fsync(self):
        """Hook: after the real fsync, plant a same-basename foreign file in an outside folder and repoint content/."""
        outside = self.tmp / "outside"
        outside.mkdir()
        sentinel = outside / "agency-actions.json"
        sentinel.write_bytes(b"OUTSIDE-STORE")
        held = self.tmp / "held-content"
        self.foreign = None
        real_fsync, state = os.fsync, {}

        def hook(fd):
            result = real_fsync(fd)
            if not state:
                state["done"] = True
                stage = [p for p in self.path.parent.iterdir() if p.name.startswith(".agency-actions-")][0]
                self.foreign = outside / stage.name
                self.foreign.write_bytes(b"FOREIGN")
                self.path.parent.rename(held)
                self.path.parent.symlink_to(outside, target_is_directory=True)
            return result

        self.sentinel, self.held, self.outside = sentinel, held, outside
        return mock.patch("campaign_tool.agency_actions.os.fsync", side_effect=hook)

    def assert_contained(self, before):
        self.assertEqual(self.foreign.read_bytes(), b"FOREIGN")
        self.assertEqual(self.sentinel.read_bytes(), b"OUTSIDE-STORE")
        self.assertEqual((self.held / "agency-actions.json").read_bytes(), before)
        self.assertEqual([p.name for p in self.held.iterdir() if p.name.startswith(".agency-actions-")], [])
        self.assertEqual(sorted(p.name for p in self.outside.iterdir() if p.name.startswith(".agency-actions-")),
                         [self.foreign.name])

    def test_add_parent_swap_refusal_keeps_foreign_collision_and_cleans_own_stage(self):
        self.put(document(row()))
        before = self.path.read_bytes()
        with self.swap_during_fsync():
            with self.assertRaises(ActionError):
                add_action(self.root, POLICE, on="2026-09-02", kind="policy_adopted", as_stated="Another",
                           source_url="https://example.invalid/x", checked_on="2026-10-01", today=TODAY)
        self.assert_contained(before)

    def test_remove_parent_swap_has_same_cleanup_ownership(self):
        self.put(document(row()))  # sole row: the empty envelope must not bypass safe cleanup
        before = self.path.read_bytes()
        with self.swap_during_fsync():
            with self.assertRaises(ActionError):
                remove_action(self.root, POLICE, 1, today=TODAY)
        self.assert_contained(before)

    def test_stage_collisions_are_bounded_and_never_touched(self):
        self.put(document(row()))
        before = self.path.read_bytes()
        folder = self.path.parent
        taken = folder / ".agency-actions-0000000000000000.tmp"
        taken.write_bytes(b"NOT-OURS")
        with mock.patch("campaign_tool.agency_actions.secrets.token_hex", return_value="0000000000000000"):
            with self.assertRaisesRegex(ActionError, "write failed"):
                remove_action(self.root, POLICE, 1, today=TODAY)
        self.assertEqual(taken.read_bytes(), b"NOT-OURS")
        self.assertEqual(self.path.read_bytes(), before)

    def test_replaced_stage_is_not_cleaned(self):
        self.put(document(row()))
        real_replace = os.replace

        def swap_stage(src, dst, **kwargs):
            stage = self.path.parent / src
            stage.unlink()
            stage.write_bytes(b"FOREIGN-STAGE")
            raise OSError("replace")

        with mock.patch("campaign_tool.agency_actions.os.replace", side_effect=swap_stage):
            with self.assertRaisesRegex(ActionError, "write failed"):
                remove_action(self.root, POLICE, 1, today=TODAY)
        left = [p for p in self.path.parent.iterdir() if p.name.startswith(".agency-actions-")]
        self.assertEqual([p.read_bytes() for p in left], [b"FOREIGN-STAGE"])
        self.assertIs(real_replace, os.replace)

    def test_healthy_add_and_remove_keep_bytes_and_mode(self):
        added = add_action(self.root, POLICE, on="2026-09-02", kind="policy_adopted", as_stated="Plain",
                           source_url="https://example.invalid/x", checked_on="2026-10-01", today=TODAY)
        self.assertEqual(added["index"], 1)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o644)
        remove_action(self.root, POLICE, 1, today=TODAY)
        self.assertEqual(self.path.read_bytes(), actions._dump([]))
        self.assertEqual([p.name for p in self.path.parent.iterdir() if p.name.startswith(".agency-actions-")], [])


if __name__ == "__main__":
    unittest.main()
