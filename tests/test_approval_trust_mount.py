"""Fixed trust mount delta and before-stop preconditions (fake Docker/systemd only)."""
import copy
import importlib.util
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from types import SimpleNamespace
from tests import test_flock_ocr_rootless_swap as rs
from campaign_tool import approval

swap = rs.swap
RUNTIME = rs.ROOT / "deploy" / "records-runtime"
_spec = importlib.util.spec_from_file_location("trust_mount_records_render", RUNTIME / "render.py")
render = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(render)

# Synthetic pin shapes as the live wrapper renders them (TRUST-MOUNT-PIN-1): the host ENGINE_DIR is a
# records-engine/source-<full sha> tree, the container engine a work/engine-<short sha> directory.
RELEASE = "2a23b3fa" + "5c" * 16
ENGINE = "/workspace/synthetic/work/engine-" + RELEASE[:8]
WORK = "/workspace/synthetic/work"
OLD_SYNTHETIC = "ENGINE_DIR=/synthetic/engines/engine-" + "a" * 40 + "\n"


def rendered_wrapper(out, release=RELEASE, engine=ENGINE, console=True, decisions=True):
    """run-records.sh rendered by render.py from run-records.sh.in with synthetic values."""
    values = {
        "RELEASE_COMMIT": release, "ENGINE_DIR": "/srv/records-engine/source-" + release,
        "DOCKER": "/usr/bin/docker", "TIMEOUT": "/usr/bin/timeout", "FLOCK": "/usr/bin/flock",
        "CONTAINER": "synthetic-flock", "EXPECTED_IMAGE": rs.OLD_IMAGE, "RUN_UID": "1000",
        "RUNTIME_DIR": "/srv/runtime", "HOST_PYTHON": "/usr/bin/python3", "NOTIFY_PY": "/srv/notify.py",
        "NOTIFY_ENV_FILE": "/srv/notify.env", "NOTIFY_LABEL": "Records", "DOCKER_WAIT_ATTEMPTS": "12",
        "DOCKER_WAIT_DELAY": "5", "DOCKER_WAIT_MAX_DELAY": "60", "DOCKER_CMD_TIMEOUT": "20",
        "DOCKER_WAIT_DEADLINE": "600", "NOTIFY_TIMEOUT": "60", "RUN_BUDGET_SECONDS": "2640",
        "DEFAULT_TARGET": "TEST", "SCHEDULED_TARGET": "TEST", "RUN_RECORDS_SH": "/srv/runtime/run-records.sh",
        "RECORDS_DROP_DIR": "/srv/records-drop", "STUB_FIXTURE": "/srv/fixture.json",
        "MIN_FREE_BYTES": "2147483648", "RECORDS_ROOT": "/srv/records-root"}
    if console:
        values.update(CONSOLE_EXPORT_DIR="/srv/console-export", CONTAINER_USER="coder", CONTAINER_ENGINE_DIR=engine,
                      CONTAINER_PYTHON="/opt/campaign-tool/venv/bin/python",
                      CONTAINER_PROJECT_DIR=WORK + "/records-project")
    if decisions:
        values["DECISIONS_DIR"] = "/srv/decisions"
    render.render(values, [("TEST", str(RUNTIME / "targets" / "stub.json.in"))], out)
    return (Path(out) / "run-records.sh").read_text()


def anchor_module():
    return SimpleNamespace(_require_root_host=mock.Mock(), TRUSTED_SIGNERS=approval.TRUSTED_SIGNERS,
                           ApprovalError=approval.ApprovalError)


class RenderedWrapperMixin:
    def setUp(self):
        super().setUp()
        self.render_root = Path(tempfile.mkdtemp(prefix="trust-wrapper-"))
        self.addCleanup(shutil.rmtree, self.render_root, ignore_errors=True)
        self.renders = 0

    def wrapper(self, **kw):
        self.renders += 1
        return rendered_wrapper(self.render_root / f"r{self.renders}", **kw)


class TrustMountTests(RenderedWrapperMixin, unittest.TestCase):
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
        module = anchor_module()
        wrapper = self.wrapper()
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
            argv = driver.sh.call_args.args[0]
            self.assertEqual(argv[argv.index("-w") + 1], ENGINE)  # the in-container engine, not the host ENGINE_DIR
            self.assertEqual(driver.state["engine_dir"], ENGINE)
            driver = self.driver("drop")
            driver.preflight_trust(self.after(self.before(), "add"))
            driver.sh.assert_not_called()

    def test_old_synthetic_engine_dir_pin_fails_closed_before_any_docker(self):
        # The pre-TRUST-MOUNT-PIN-1 fixture form: an ENGINE_DIR=.../engine-<sha> line names no container engine.
        for text in (OLD_SYNTHETIC, f"RELEASE_COMMIT={'a' * 40}\n" + OLD_SYNTHETIC):
            driver = self.driver("add")
            with mock.patch.object(swap, "_load", return_value=anchor_module()), \
                    mock.patch.object(Path, "read_text", return_value=text):
                with self.assertRaisesRegex(swap.StepFailed, "cannot identify the wrapper's single engine pin"):
                    driver.preflight_trust(self.before())
            driver.sh.assert_not_called()
            self.assertNotIn("engine_dir", driver.state)

    def test_unreadable_wrapper_refuses_before_any_docker(self):
        driver = self.driver("add")
        with mock.patch.object(swap, "_load", return_value=anchor_module()):
            with self.assertRaisesRegex(swap.StepFailed, "cannot read the records wrapper"):
                driver.preflight_trust(self.before())
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


class WrapperEnginePinTests(RenderedWrapperMixin, unittest.TestCase):
    """TRUST-MOUNT-PIN-1: the engine pin comes from the rendered wrapper's container PYTHONPATH values."""

    def refuses(self, text, reason):
        with self.assertRaisesRegex(ValueError, reason):
            swap.wrapper_engine(text)

    def test_the_real_rendered_wrapper_names_its_single_container_engine(self):
        for kw in ({}, {"decisions": False}):
            with self.subTest(**kw):
                text = self.wrapper(**kw)
                self.assertIn(f"ENGINE_DIR=/srv/records-engine/source-{RELEASE}\n", text)  # host tree, not the pin
                self.assertIn(f"PYTHONPATH={ENGINE} ", text)
                self.assertEqual(swap.wrapper_engine(text), ENGINE)
        full = "/workspace/synthetic/work/engine-" + RELEASE
        self.assertEqual(swap.wrapper_engine(self.wrapper(engine=full)), full)

    def test_two_distinct_engine_dirs_refuse(self):
        text = self.wrapper()
        other = "/workspace/synthetic/work/engine-" + RELEASE[:12]  # also a RELEASE_COMMIT prefix
        self.assertEqual(text.count(f"PYTHONPATH={ENGINE} "), 2)
        self.refuses(text.replace(f"PYTHONPATH={ENGINE} ", f"PYTHONPATH={other} ", 1),
                     "2 distinct in-container PYTHONPATH values")

    def test_release_commit_mismatch_refuses(self):
        self.refuses(self.wrapper(engine="/workspace/synthetic/work/engine-24a18b63"),
                     "is not RELEASE_COMMIT 2a23b3fa5c5c")
        text = self.wrapper()
        self.refuses(text.replace(f"RELEASE_COMMIT={RELEASE}\n", ""), "no single 40-hex RELEASE_COMMIT")
        self.refuses(text.replace(f"RELEASE_COMMIT={RELEASE}\n", f"RELEASE_COMMIT={RELEASE}\nRELEASE_COMMIT={'b' * 40}\n"),
                     "no single 40-hex RELEASE_COMMIT")

    def test_no_container_engine_refuses(self):
        text = self.wrapper(console=False, decisions=False)  # no docker exec post-step: host PYTHONPATH only
        self.assertIn('PYTHONPATH="$ENGINE_DIR"', text)
        self.refuses(text, "names no in-container engine")
        self.refuses(f"RELEASE_COMMIT={RELEASE}\n" + OLD_SYNTHETIC, "names no in-container engine")
        self.refuses(f"RELEASE_COMMIT={RELEASE}\n# env PYTHONPATH={ENGINE} python\n", "names no in-container engine")

    def test_other_engine_shapes_refuse(self):
        text = self.wrapper()
        for bad in ("/srv/synthetic/work/engine-2a23b3fa", "/workspace/synthetic/../engine-2a23b3fa",
                    "/workspace//engine-2a23b3fa", "/workspace/synthetic/work/engine-2a23b3",
                    f"{ENGINE}:/workspace/other", "/workspace/synthetic/work/source-2a23b3fa"):
            with self.subTest(bad=bad):
                self.refuses(text.replace(f"PYTHONPATH={ENGINE} ", f"PYTHONPATH={bad} "), "is not a /workspace/")


class EngineMountTests(unittest.TestCase):
    """Review F2: the dry-run refuses when the mount holding the wrapper's engine is not snapshotted."""

    def driver(self, engine=ENGINE):
        driver = object.__new__(swap.Swap)
        driver.state = {"engine_dir": engine} if engine else {}
        driver.out = io.StringIO()
        return driver

    @staticmethod
    def mount(label, destination, kind="bind"):
        return {"label": label, "type": kind, "name": None, "source": "/srv" + destination,
                "destination": destination, "rw": True}

    def test_engine_on_a_snapshotted_mount_passes_and_is_named(self):
        for dest in (WORK, "/workspace", ENGINE):
            with self.subTest(dest=dest):
                driver = self.driver()
                driver.check_engine_mount([self.mount("m00", "/data"), self.mount("m01", dest)],
                                          [{"type": "tmpfs", "destination": "/scratch", "reason": "not persistent"}])
                self.assertIn(f"engine {ENGINE} is on snapshotted mount m01={dest}", driver.out.getvalue())

    def test_excluded_or_missing_engine_mount_refuses(self):
        tmpfs = {"type": "tmpfs", "destination": WORK, "reason": "not persistent"}
        cases = {"excluded": ([self.mount("m00", "/data")], [tmpfs]),
                 "excluded under a snapshotted mount": ([self.mount("m00", "/workspace")], [tmpfs]),
                 "no mount": ([self.mount("m00", "/data"), self.mount("m01", "/workspace/synthetic/work2")], [])}
        for name, (mounts, excluded) in cases.items():
            with self.subTest(case=name):
                with self.assertRaisesRegex(swap.StepFailed, "a recreate would lose it"):
                    self.driver().check_engine_mount(mounts, excluded)

    def test_without_a_trust_add_engine_the_check_is_not_applied(self):
        driver = self.driver(engine=None)
        driver.check_engine_mount([self.mount("m00", "/data")], [])
        self.assertEqual(driver.out.getvalue(), "")


class TrustMountDryRunTests(RenderedWrapperMixin, rs.RunMixin, unittest.TestCase):
    """--dry-run --trust-mount add against a fake target with a rendered wrapper (no Docker, no host anchor)."""

    def setUp(self):
        super().setUp()
        self.work = self.tmp / "binds" / "work"
        (self.work / ("engine-" + RELEASE[:8])).mkdir(parents=True)
        target = self.fake.find(rs.TARGET)
        target["Mounts"].append({"Type": "bind", "Source": str(self.work), "Destination": WORK, "RW": True,
                                 "Mode": "", "Propagation": "rprivate"})
        target["HostConfig"]["Binds"].append(f"{self.work}:{WORK}")
        self.wrapper_path = self.render_root / "live-shape" / "run-records.sh"
        self.wrapper_text = rendered_wrapper(self.wrapper_path.parent)
        self.fake.faults["--anchor-status"] = (0, "anchor unavailable: trust file missing\n", "")

    def dry_run(self, mode="--dry-run"):
        with mock.patch.object(swap, "_load", return_value=anchor_module()):
            return self.run_swap(self.args(mode, trust_mount="add", records_wrapper=str(self.wrapper_path)))

    def anchor_calls(self):
        return [c for c in self.fake.calls if "--anchor-status" in c]

    def test_dry_run_with_the_rendered_wrapper_passes_and_names_the_engine_mount(self):
        code, out = self.dry_run()
        self.assertEqual(code, swap.EXIT_OK, out)
        self.assertIn(f"records wrapper in-container engine: {ENGINE}", out)
        self.assertRegex(out, rf"engine {ENGINE} is on snapshotted mount m\d\d={WORK}\n")
        self.assertIn("dry-run: OK", out)
        (call,) = self.anchor_calls()
        self.assertEqual(call[:6], ["docker", "exec", "-u", "coder", "-w", ENGINE])
        self.assertEqual(rs.lifecycle(self.fake), [])
        self.assert_live_unchanged()

    def test_dry_run_and_apply_refuse_when_the_engine_mount_is_not_snapshotted(self):
        target = self.fake.find(rs.TARGET)
        target["Mounts"][-1] = {"Type": "tmpfs", "Source": "", "Destination": WORK, "RW": True}
        target["HostConfig"]["Binds"].pop()
        for mode in ("--dry-run", "--apply"):
            with self.subTest(mode=mode):
                code, out = self.dry_run(mode)
                self.assertEqual(code, swap.EXIT_REFUSED, out)
                self.assertIn(f"FAIL at [R1]: the mount holding the wrapper's engine {ENGINE} (tmpfs:{WORK}) is not "
                              "snapshotted (not persistent)", out)
                self.assertEqual(rs.lifecycle(self.fake), [])
                self.assertEqual(self.fake.helper_runs, [])

    def test_wrapper_refusals_happen_before_any_docker_exec_or_stop(self):
        cases = {"old synthetic form": OLD_SYNTHETIC,
                 "no container engine": rendered_wrapper(self.render_root / "plain", console=False, decisions=False),
                 "release mismatch": self.wrapper_text.replace(f"RELEASE_COMMIT={RELEASE}", "RELEASE_COMMIT=" + "b" * 40)}
        for name, text in cases.items():
            with self.subTest(case=name):
                self.wrapper_path.write_text(text)
                code, out = self.dry_run()
                self.assertEqual(code, swap.EXIT_REFUSED, out)
                self.assertIn("FAIL at [R1]: cannot identify the wrapper's single engine pin", out)
                self.assertEqual(self.anchor_calls(), [])
                self.assertEqual(rs.lifecycle(self.fake), [])

    def test_other_r1_refusals_are_unchanged_with_the_trust_mount(self):
        cases = {"-pre-ocr": lambda f: f.add(rs._container("4" * 64, rs.TARGET + "-pre-ocr", rs.OLD_IMAGE, False,
                                                            [], [], [], {})),
                 "-ocr-failed": lambda f: f.add(rs._container("5" * 64, rs.TARGET + "-ocr-failed", rs.OLD_IMAGE,
                                                               False, [], [], [], {})),
                 "not rootless": lambda f: setattr(f, "rootless", False)}
        expected = {"-pre-ocr": f"a container named {rs.TARGET}-pre-ocr already exists",
                    "-ocr-failed": f"a container named {rs.TARGET}-ocr-failed already exists",
                    "not rootless": "the Docker daemon is not rootless"}
        for name, change in cases.items():
            for mode in ("--dry-run", "--apply"):
                with self.subTest(case=name, mode=mode):
                    self.fake.containers = {k: v for k, v in self.fake.containers.items()
                                            if v["Id"] not in ("4" * 64, "5" * 64)}
                    self.fake.rootless = True
                    change(self.fake)
                    code, out = self.dry_run(mode)
                    self.assertEqual(code, swap.EXIT_REFUSED, out)
                    self.assertIn(f"FAIL at [R1]: {expected[name]}", out)
                    self.assertEqual(rs.lifecycle(self.fake), [])
                    self.assertEqual(self.fake.helper_runs, [])


class TrustMountRehearsalTests(RenderedWrapperMixin, rs.RunMixin, unittest.TestCase):
    """--rehearse --trust-mount add with a rendered wrapper reaches `rehearsal: OK` on the fake runtime.

    As in RehearsalFaultModeTests the throwaway target is the fake's TARGET renamed, setup/cleanup are
    replaced and the swap is the real RehearsalSwap.run_main. The fake additionally reports the image's
    and the replacement's synthetic wheel hash and mounts the read-only trust bind it was asked to create.
    """

    def setUp(self):
        super().setUp()
        self.token = "0b1c2d3e"
        self.name = f"flock-swap-rehearsal-{rs.SECOND}-{self.token}"
        self.fake.find(rs.TARGET)["Name"] = "/" + self.name
        self.fake.units = {f"{self.name}-mail.timer": "active", f"{self.name}-mail.service": "inactive"}
        self.wrapper_path = self.render_root / "live-shape" / "run-records.sh"
        rendered_wrapper(self.wrapper_path.parent)
        run, api = self.fake.run, self.fake.docker_api
        wheel_path = f"/opt/flock-swap-rehearsal/dist/{swap.REHEARSAL_WHEEL}"

        def fake_run(argv, timeout):
            if argv and argv[-1] == wheel_path and "sha256sum" in argv:
                self.fake.calls.append(list(argv))
                (wheel,) = (self.tmp / "scratch").rglob(swap.REHEARSAL_WHEEL)
                return 0, f"{swap.sha256_file(wheel)}  {wheel_path}\n", ""
            return run(argv, timeout)

        def fake_api(method, path, body):
            code, out = api(method, path, body)
            if code == 201 and swap.TRUST_MOUNT in (body["HostConfig"].get("Mounts") or []):
                self.fake.find(out["Id"])["Mounts"].append({"Type": "bind", "Source": "/etc/deflock",
                                                            "Destination": "/etc/deflock", "RW": False,
                                                            "Mode": "", "Propagation": "rprivate"})
            return code, out

        self.fake.run, self.fake.docker_api = fake_run, fake_api

    def rehearse(self, wrapper=None):
        args = swap.parse_args(["--rehearse", "--private", str(self.tmp / "scratch"), "--trust-mount", "add",
                                "--records-wrapper", str(wrapper or self.wrapper_path)])
        out = io.StringIO()
        with mock.patch.object(swap.datetime, "datetime", rs.FrozenSecond), \
                mock.patch.object(swap.secrets, "token_hex", return_value=self.token), \
                mock.patch.object(swap.Rehearsal, "base_image", return_value=rs.OLD_IMAGE), \
                mock.patch.object(swap.Rehearsal, "setup", return_value={"pins": {}}), \
                mock.patch.object(swap.Rehearsal, "cleanup"), \
                mock.patch.object(swap, "_load", return_value=anchor_module()), \
                mock.patch.object(swap, "DIST", self.dist):
            code = swap.Rehearsal(args, system=self.fake, out=out).run()
        return code, out.getvalue()

    def test_rehearse_with_the_rendered_wrapper_reaches_rehearsal_ok(self):
        code, out = self.rehearse()
        self.assertEqual(code, swap.EXIT_OK, out)
        self.assertIn(f"records wrapper in-container engine: {ENGINE}", out)
        self.assertIn("rehearsal: anchor-status engine precondition is simulated", out)
        self.assertIn(f"rehearsal: engine-mount check for {ENGINE} is simulated", out)
        self.assertIn("rehearsal: OK, the R1 preflight through A5 passed against the throwaway target", out)
        new = self.fake.find(self.name)
        self.assertIn({"Type": "bind", "Source": "/etc/deflock", "Destination": "/etc/deflock", "RW": False,
                       "Mode": "", "Propagation": "rprivate"}, new["Mounts"])
        self.assertFalse(self.fake.find(self.name + "-pre-ocr")["State"]["Running"])

    def test_rehearse_with_the_old_synthetic_pin_fails_closed_in_r1(self):
        old = self.render_root / "old.sh"
        old.write_text(OLD_SYNTHETIC)
        code, out = self.rehearse(old)
        self.assertEqual(code, swap.EXIT_REFUSED, out)
        self.assertIn("FAIL at [R1]: cannot identify the wrapper's single engine pin", out)
        self.assertIn("rehearsal: FAILED, driver exit 2", out)
        self.assertNotIn("rehearsal: OK", out)
        self.assertEqual(rs.lifecycle(self.fake), [])
