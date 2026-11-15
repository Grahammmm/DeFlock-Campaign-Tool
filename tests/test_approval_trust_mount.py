"""Fixed trust mount delta and before-stop preconditions (fake Docker/systemd only)."""
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from types import SimpleNamespace
from tests import test_flock_ocr_rootless_swap as rs
from campaign_tool import approval

swap = rs.swap


class TrustMountTests(unittest.TestCase):
    def before(self):
        return rs._container("a" * 64, "synthetic", rs.OLD_IMAGE, False, [], [], [], {})

    def after(self, before, action):
        out = copy.deepcopy(before)
        out["HostConfig"]["Mounts"] = swap.trust_mounts(out["HostConfig"].get("Mounts"), action)
        out["Mounts"] = ([{"Type": "bind", "Source": "/etc/deflock", "Destination": "/etc/deflock",
                           "RW": False, "Mode": "", "Propagation": "rprivate"}] if action == "add" else [])
        return out

    def test_create_default_add_drop_and_no_input_mutation(self):
        before = self.before()
        original = copy.deepcopy(before)
        default = swap.create_body(before, rs.OLD_IMAGE)
        self.assertEqual(default["HostConfig"], original["HostConfig"])
        add = swap.create_body(before, rs.OLD_IMAGE, "add")
        self.assertEqual(add["HostConfig"]["Mounts"], [swap.TRUST_MOUNT])
        added = self.after(before, "add")
        self.assertEqual(swap.create_body(added, rs.OLD_IMAGE, "drop")["HostConfig"]["Mounts"], [])
        self.assertEqual(before, original)

    def test_inspect_only_exact_mount_delta_is_allowed(self):
        before = self.before()
        after = self.after(before, "add")
        self.assertEqual(swap.inspect_diff(before, after, {}, {}, "add"), [])
        self.assertNotEqual(swap.inspect_diff(before, after, {}, {}), [])
        dropped = self.after(after, "drop")
        self.assertEqual(swap.inspect_diff(after, dropped, {}, {}, "drop"), [])
        for key, value in (("Privileged", True), ("CapAdd", ["SYS_ADMIN"]), ("NetworkMode", "host")):
            changed = copy.deepcopy(after)
            changed["HostConfig"][key] = value
            self.assertIn("HostConfig." + key, swap.inspect_diff(before, changed, {}, {}, "add"))
        for field, value in (("Source", "/home/user/forged"), ("RW", True), ("Propagation", "shared")):
            changed = copy.deepcopy(after)
            changed["Mounts"][0][field] = value
            self.assertEqual(swap.inspect_diff(before, changed, {}, {}, "add"), ["trust_mount"])
        self.assertEqual(swap.inspect_diff(before, before, {}, {}, "add"), ["trust_mount"])
        changed = copy.deepcopy(after)
        changed["HostConfig"]["Mounts"].append({"Type": "bind", "Source": "/other", "Target": "/other"})
        self.assertIn("HostConfig.Mounts", swap.inspect_diff(before, changed, {}, {}, "add"))

    def driver(self, action):
        driver = object.__new__(swap.Swap)
        driver.state = {"trust_mount": action, "container": "synthetic", "records_wrapper": "/synthetic/wrapper.sh"}
        driver.out = io.StringIO()
        driver.sh = mock.Mock(return_value=(0, "anchor unavailable: mount missing\n", ""))
        return driver

    def test_host_unavailable_refuses_before_any_docker_or_stop(self):
        for error in (approval.TrustUnavailable("missing"), approval.TrustUnavailable("user-owned"),
                      approval.TrustUnavailable("group writable")):
            driver = self.driver("add")
            module = SimpleNamespace(_require_root_host=mock.Mock(side_effect=error),
                                     TRUSTED_SIGNERS=approval.TRUSTED_SIGNERS, ApprovalError=approval.ApprovalError)
            with mock.patch.object(swap, "_load", return_value=module):
                with self.assertRaises(swap.StepFailed):
                    driver.preflight_trust(self.before())
            driver.sh.assert_not_called()

    def test_engine_must_support_anchor_status_and_drop_never_needs_it(self):
        module = SimpleNamespace(_require_root_host=mock.Mock(), TRUSTED_SIGNERS=approval.TRUSTED_SIGNERS,
                                 ApprovalError=approval.ApprovalError)
        wrapper = 'ENGINE_DIR=/synthetic/engines/engine-' + 'a' * 40 + '\n'
        with mock.patch.object(swap, "_load", return_value=module), mock.patch.object(Path, "read_text", return_value=wrapper):
            for response in ((1, "anchor ok\n", ""), (0, "", ""), (0, "other\n", "")):
                driver = self.driver("add")
                driver.sh.return_value = response
                with self.assertRaises(swap.StepFailed):
                    driver.preflight_trust(self.before())
                self.assertEqual(driver.sh.call_count, 1)
                self.assertIn("--anchor-status", driver.sh.call_args.args[0])
                self.assertNotIn("stop", driver.sh.call_args.args[0])
            driver = self.driver("add")
            driver.preflight_trust(self.before())  # unavailable is valid: the new engine is active
            driver = self.driver("drop")
            driver.preflight_trust(self.after(self.before(), "add"))
            driver.sh.assert_not_called()

    def test_unchanged_image_reuse_needs_verified_receipt_and_opt_in(self):
        with tempfile.TemporaryDirectory() as tmp:
            prev = Path(tmp) / "prior"
            prev.mkdir()
            receipt = {"run_dir": str(prev), "new_tag": "synthetic:trusted", "done": ["V1"],
                       "new_image_id": rs.NEW_IMAGE, "wheel_sha256": rs.WHEEL_SHA, "old_image": rs.OLD_IMAGE}
            (prev / "state.json").write_text(json.dumps(receipt))
            driver = self.driver("add")
            driver.args = SimpleNamespace(reuse_image=str(prev), new_tag="synthetic:trusted", trust_mount="add")
            driver.state.update(private=tmp, run_dir=str(Path(tmp) / "next"))
            driver.image_inspect = mock.Mock(return_value={"RootFS": {"Layers": ["synthetic-layer"]}})
            before = dict(self.before(), Image=rs.NEW_IMAGE)
            self.assertEqual(driver.check_reuse(before, rs.NEW_IMAGE)["new_image_id"], rs.NEW_IMAGE)
            driver.args.trust_mount = None
            with self.assertRaises(swap.StepFailed):
                driver.check_reuse(before, rs.NEW_IMAGE)
            driver.args.trust_mount = "add"
            receipt["done"] = []
            (prev / "state.json").write_text(json.dumps(receipt))
            with self.assertRaises(swap.StepFailed):
                driver.check_reuse(before, rs.NEW_IMAGE)

    def test_conflicts_are_not_silently_overwritten(self):
        for mount in (dict(swap.TRUST_MOUNT, ReadOnly=False), dict(swap.TRUST_MOUNT, Source="/other")):
            before = self.before()
            before["HostConfig"]["Mounts"] = [mount]
            with self.assertRaises(swap.StepFailed):
                swap.create_body(before, rs.OLD_IMAGE, "add")

    def test_read_only_trust_mount_is_never_snapshotted_or_restored(self):
        driver = self.driver("drop")
        before = self.after(self.before(), "add")
        before["Mounts"].append({"Type": "volume", "Source": "/synthetic", "Destination": "/data", "RW": True})
        before["HostConfig"]["Mounts"].append({"Type": "volume", "Source": "synthetic", "Target": "/data"})
        mounts, excluded = driver.derive_mounts(before)
        self.assertEqual([m["destination"] for m in mounts], ["/data"])
        self.assertEqual(excluded[0]["destination"], "/etc/deflock")
