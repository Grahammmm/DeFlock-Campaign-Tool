"""Rootless OCR swap driver: ordering, dry-run and rollback regressions.

Everything here is synthetic. A fake Docker/systemd records every command;
helper containers are emulated by running their command locally with the
container paths (/live, /private, /recipe) mapped onto temporary directories,
so the real tar and snapshot_evidence.py run against a synthetic WAL database.
No Docker daemon, systemd unit, network or real data is touched.
"""
import copy
import importlib.util
import io
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RECIPE = ROOT / "container" / "flock-ocr"


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, RECIPE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


swap = _load("flock_ocr_rootless_swap", "rootless_swap.py")
check_runbook = _load("flock_ocr_check_runbook_r", "check_runbook.py")
evidence = _load("flock_ocr_snapshot_evidence_r", "snapshot_evidence.py")
PLAN = json.loads((RECIPE / "runbook-rootless.json").read_text())
ORIGINAL = json.loads((RECIPE / "runbook.json").read_text())
DOC = (ROOT / "docs" / "RECORDS-OCR-CONTAINER-ROOTLESS.md").read_text()

OLD_IMAGE = "sha256:" + "a1" * 32
NEW_IMAGE = "sha256:" + "c2" * 32
OTHER_IMAGE = "sha256:" + "e3" * 32
TARGET, SIDECAR, UNRELATED = "flock-target", "flock-sidecar", "unrelated"
UNIT = "synthetic-mail"
WHEEL_SHA = "ab" * 32

_WAL_WRITER = (
    "import os, sqlite3, sys\n"
    "c = sqlite3.connect(sys.argv[1])\n"
    "c.execute('pragma journal_mode=wal')\n"
    "c.execute('create table synthetic(value)')\n"
    "c.executemany('insert into synthetic values (?)', [(n,) for n in range(42)])\n"
    "c.commit()\n"
    "os._exit(0)\n"
)


def _container(ident, name, image, running, mounts, binds, env, labels):
    return {"Id": ident, "Name": "/" + name, "Image": image, "Created": "2026-10-01T00:00:00Z",
            "State": {"Running": running, "Status": "running" if running else "exited"},
            "RestartCount": 0, "Path": "/bin/app", "Args": [],
            "Config": {"Image": image, "Hostname": ident[:12], "Env": env, "Labels": labels,
                       "Cmd": ["app"], "Entrypoint": None, "User": "1000"},
            "HostConfig": {"Binds": binds, "Mounts": None, "NetworkMode": "bridge",
                           "RestartPolicy": {"Name": "unless-stopped"}, "Privileged": False,
                           "Tmpfs": None, "CapAdd": None},
            "Mounts": mounts,
            "NetworkSettings": {"Networks": {"bridge": {"Aliases": None, "IPAddress": "10.0.0.2"}}},
            "LogPath": f"/logs/{ident}.log", "HostsPath": f"/hosts/{ident}"}


class FakeSystem:
    """Fake rootless Docker + systemd --user. Records every call."""

    def __init__(self, tmp):
        self.tmp = Path(tmp)
        self.calls, self.api_calls, self.helper_runs, self.events = [], [], [], []
        self.faults, self.hooks, self.fail_start = {}, {}, set()
        self.open_files, self.rootless, self.busy_service = [], True, False
        self.clock = 0
        self.volume = self.tmp / "volumes" / "flock-data" / "_data"
        self.bind = self.tmp / "binds" / "records"
        (self.volume / "state").mkdir(parents=True)
        self.bind.mkdir(parents=True)
        (self.volume / "state" / "notes.txt").write_text("synthetic\n")
        os.chmod(self.volume / "state" / "notes.txt", 0o640)
        (self.bind / "receipt.json").write_text("{}\n")
        subprocess.run([sys.executable, "-B", "-c", _WAL_WRITER, str(self.volume / "state" / "flock.db")],
                       check=True)
        self.other_volume = self.tmp / "volumes" / "other" / "_data"
        self.other_volume.mkdir(parents=True)
        self.volumes = {"flock-data": self.volume, "other": self.other_volume}
        self.images = {
            OLD_IMAGE: {"Id": OLD_IMAGE, "Config": {"Env": ["PATH=/usr/bin"], "Labels": {"base": "1"}}},
            OTHER_IMAGE: {"Id": OTHER_IMAGE, "Config": {"Env": [], "Labels": {}}}}
        self.tags = {}
        vol = {"Type": "volume", "Name": "flock-data", "Source": str(self.volume), "Destination": "/data",
               "RW": True, "Driver": "local", "Mode": "z", "Propagation": ""}
        bind = {"Type": "bind", "Source": str(self.bind), "Destination": "/records", "RW": True,
                "Mode": "", "Propagation": "rprivate"}
        tmpfs = {"Type": "tmpfs", "Source": "", "Destination": "/scratch", "RW": True}
        self.containers = {}
        self.add(_container("1" * 64, TARGET, OLD_IMAGE, True, [vol, bind, tmpfs],
                            ["flock-data:/data", f"{self.bind}:/records"],
                            ["PATH=/usr/bin", "APP=1"], {"base": "1", "app": "flock"}))
        side_mount = dict(bind, Destination="/r", RW=False)
        self.add(_container("2" * 64, SIDECAR, OTHER_IMAGE, True, [side_mount], [f"{self.bind}:/r:ro"], [], {}))
        other = dict(vol, Name="other", Source=str(self.other_volume))
        self.add(_container("3" * 64, UNRELATED, OTHER_IMAGE, True, [other], ["other:/data"], [], {}))
        self.units = {f"{UNIT}.timer": "active", f"{UNIT}.service": "inactive"}

    def record_event(self, argv):
        if argv[0] == "docker" and argv[1] in ("stop", "start", "rename", "build", "rm"):
            ref = argv[-1] if argv[1] != "rename" else argv[2]
            if argv[1] == "build":
                ref = argv[argv.index("-t") + 1]
            info = self.find(ref)
            label = {"1" * 64: "old", "2" * 64: "sidecar", "3" * 64: "unrelated", "9" * 64: "new"}.get(
                info["Id"] if info else "", ref)
            self.events.append(f"{argv[1]} {label}" + (f" -> {argv[3]}" if argv[1] == "rename" else ""))
        elif argv[0] == "systemctl" and argv[2] in ("stop", "start"):
            self.events.append(f"systemctl {argv[2]} {argv[3]}")

    def add(self, info):
        self.containers[info["Id"]] = info

    def find(self, ref):
        for info in self.containers.values():
            if ref in (info["Id"], info["Id"][:12], info["Name"].lstrip("/")):
                return info
        return None

    # -- System interface --

    def sleep(self, seconds):
        pass

    def now(self):
        self.clock += 1
        return f"2026-10-19T20:00:{self.clock:02d}Z"

    def disk_free(self, path):
        return 1 << 40

    def open_files_under(self, roots):
        return list(self.open_files)

    def docker_api(self, method, path, body):
        self.api_calls.append((method, path, copy.deepcopy(body)))
        name = path.split("name=", 1)[1]
        if self.find(name):
            return 409, {"message": "name in use"}
        old = self.find("1" * 64)
        image = self.images[body["Image"]]["Config"]
        env = dict(e.split("=", 1) for e in image.get("Env") or [])
        env.update(dict(e.split("=", 1) for e in body.get("Env") or []))
        labels = dict(image.get("Labels") or {}, **(body.get("Labels") or {}))
        ident = "9" * 64
        config = {k: v for k, v in body.items() if k not in ("HostConfig", "NetworkingConfig")}
        config.update(Env=[f"{k}={v}" for k, v in env.items()], Labels=labels)
        host = copy.deepcopy(body["HostConfig"])
        networks = {n: {"Aliases": e.get("Aliases"), "IPAddress": ""}
                    for n, e in body.get("NetworkingConfig", {}).get("EndpointsConfig", {}).items()}
        self.add({"Id": ident, "Name": "/" + name, "Image": body["Image"], "Created": "2026-10-19",
                  "State": {"Running": False, "Status": "created"}, "RestartCount": 0,
                  "Path": old["Path"], "Args": old["Args"], "Config": config, "HostConfig": host,
                  "Mounts": copy.deepcopy(old["Mounts"]), "NetworkSettings": {"Networks": networks},
                  "LogPath": f"/logs/{ident}.log", "HostsPath": f"/hosts/{ident}"})
        return 201, {"Id": ident}

    def run(self, argv, timeout):
        argv = list(argv)
        self.calls.append(argv)
        self.record_event(argv)
        joined = " ".join(argv)
        for key, hook in self.hooks.items():
            if key in joined:
                hook()
        for key, response in self.faults.items():
            if key in joined:
                return response
        if argv[0] == "systemctl":
            return self._systemctl(argv)
        if argv[0] == sys.executable:
            script = Path(argv[2]).name
            if script == "build_wheel.py":
                return 0, "{}\nWHEEL_SHA256=" + WHEEL_SHA + "\n", ""
            if script in ("preflight.py", "verify.py"):
                return 0, ("preflight: OK\n" if script == "preflight.py" else "PASS x\nverify: OK\n"), ""
        if argv[0] != "docker":
            return 127, "", "unknown command"
        return getattr(self, "_" + argv[1].replace("-", "_"))(argv)

    def _systemctl(self, argv):
        verb, unit = argv[2], argv[-1]
        if verb == "show":
            if unit not in self.units:
                return 0, "LoadState=not-found\nActiveState=inactive\n", ""
            state = self.units[unit]
            if unit.endswith(".service") and self.busy_service:
                state = "active"
            return 0, f"LoadState=loaded\nActiveState={state}\n", ""
        if verb in ("stop", "start"):
            self.units[unit] = "inactive" if verb == "stop" else "active"
            return 0, "", ""
        return 1, "", "bad verb"

    def _info(self, argv):
        options = ["name=seccomp,profile=builtin"] + (["name=rootless"] if self.rootless else [])
        return 0, json.dumps(options) + "\n", ""

    def _inspect(self, argv):
        refs = [a for a in argv[2:] if a not in ("--type", "container")]
        found = [self.find(r) for r in refs]
        if any(f is None for f in found):
            return 1, "[]\n", "Error: No such container"
        return 0, json.dumps(found), ""

    def _ps(self, argv):
        return 0, "".join(i + "\n" for i in self.containers), ""

    def _image(self, argv):
        ref = argv[-1]
        image = self.images.get(self.tags.get(ref, ref))
        if image is None:
            return 1, "[]\n", "Error: No such image"
        if "--format" in argv:
            return 0, image["Id"] + "\n", ""
        return 0, json.dumps([image]), ""

    def _stop(self, argv):
        info = self.find(argv[-1])
        info["State"]["Running"] = False
        return 0, argv[-1] + "\n", ""

    def _start(self, argv):
        info = self.find(argv[-1])
        if info is None or info["Id"] in self.fail_start:
            return 1, "", "Error: cannot start"
        info["State"]["Running"] = True
        hook = self.hooks.get("start:" + info["Id"])
        if hook:
            hook()
        return 0, argv[-1] + "\n", ""

    def _rename(self, argv):
        info, target = self.find(argv[2]), argv[3]
        if info is None or self.find(target):
            return 1, "", "Error: rename"
        info["Name"] = "/" + target
        return 0, "", ""

    def _top(self, argv):
        return 0, "UID PID CMD\nsynthetic 1 app-server\n", ""

    def _build(self, argv):
        tag = argv[argv.index("-t") + 1]
        self.images[NEW_IMAGE] = {"Id": NEW_IMAGE, "Config": {
            "Env": ["PATH=/usr/bin", "PIP_NO_CACHE_DIR=1"], "Labels": {"base": "1", "title": "ocr"}}}
        self.tags[tag] = NEW_IMAGE
        return 0, "", ""

    def _run(self, argv):
        if swap.HELPER_LABEL not in argv:
            wheel = json.loads((RECIPE / "pins.json").read_text())["engine"]["wheel_filename"]
            root = "/opt/campaign-tool"
            listing = [root, f"{root}/ENGINE_COMMIT", f"{root}/requirements.lock", f"{root}/venv",
                       f"{root}/venv/bin", f"{root}/dist", f"{root}/dist/{wheel}"]
            return 0, "\n".join(listing) + "\n", ""
        i = argv.index("--entrypoint")
        command = [argv[i + 1], *argv[i + 3:]]
        mapping, mounts = {}, []
        for j, value in enumerate(argv[:i]):
            if value != "--mount":
                continue
            parts = argv[j + 1].split(",")
            spec = dict(p.split("=", 1) for p in parts if "=" in p)
            source = self.volumes[spec["src"]] if spec["type"] == "volume" else spec["src"]
            mapping[spec["dst"]] = str(source)
            mounts.append((spec["dst"], "readonly" in parts))
        self.helper_runs.append({"command": command, "mounts": mounts})

        def rewrite(arg):
            for dst in sorted(mapping, key=len, reverse=True):
                if arg == dst or arg.startswith(dst + "/"):
                    return mapping[dst] + arg[len(dst):]
            return arg
        local = [rewrite(a) for a in command]
        if local[0] == "python3":
            local[0] = sys.executable
        result = subprocess.run(local, capture_output=True, text=True, check=False)
        return result.returncode, result.stdout, result.stderr


def lifecycle(fake):
    """Ordered state-changing events (no helpers, no reads), labelled at call time."""
    return list(fake.events)


class RunMixin:
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="flock-rootless-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.fake = FakeSystem(self.tmp)
        self.private = self.tmp / "private"
        self.live_before = {"vol": evidence.manifest(self.fake.volume), "bind": evidence.manifest(self.fake.bind)}

    def args(self, mode="--apply", **extra):
        argv = [mode, "--container", TARGET, "--pause-unit", UNIT, "--private", str(self.private),
                "--new-tag", "flock-ocr:synthetic", "--build-python", "/usr/bin/python3", "--mail-wait", "10"]
        for key, value in extra.items():
            argv += [f"--{key.replace('_', '-')}", value]
        return argv

    def run_swap(self, argv):
        out = io.StringIO()
        code = swap.main(argv, system=self.fake, out=out)
        return code, out.getvalue()

    def run_dir(self):
        runs = sorted(self.private.glob("run-*"))
        self.assertEqual(len(runs), 1)
        return runs[0]

    def state(self):
        return json.loads((self.run_dir() / "state.json").read_text())

    def assert_live_unchanged(self):
        self.assertEqual(evidence.compare(self.live_before["vol"], evidence.manifest(self.fake.volume)), [])
        self.assertEqual(evidence.compare(self.live_before["bind"], evidence.manifest(self.fake.bind)), [])

    def assert_safe_helpers(self, allow_restore=False):
        for run in self.fake.helper_runs:
            writable = [d for d, ro in run["mounts"] if d.startswith("/live/") and not ro]
            if writable:
                self.assertTrue(allow_restore, run["command"])
                cmd = run["command"]
                self.assertTrue(cmd[0] == "find" and "-delete" in cmd
                                or (cmd[0] == "tar" and "-xpf" in cmd and cmd[cmd.index("-xpf") + 1]
                                    .startswith("/private/s1/")), cmd)
            if run["command"][0] == "rm":
                self.assertTrue(run["command"][-1].startswith("/private/"), run["command"])
            if "sqlite-evidence" in run["command"]:
                self.assertTrue(run["command"][5].startswith("/private/"), run["command"])
        self.assertFalse(any(a[0] == "docker" and a[1] == "rm" for a in self.fake.calls), "docker rm used")


class RootlessRunbookOrderTests(unittest.TestCase):
    def test_rootless_runbook_passes_check_runbook(self):
        self.assertEqual(check_runbook.check(PLAN), [])
        out = io.StringIO()
        from contextlib import redirect_stdout
        with redirect_stdout(out):
            self.assertEqual(check_runbook.main([str(RECIPE / "runbook-rootless.json")]), 0)
        self.assertIn("runbook: OK", out.getvalue())

    def test_rootless_runbook_keeps_the_reviewed_step_ids_kinds_and_paths(self):
        strip = lambda plan: ([(s["id"], s["kind"], s.get("on_fail")) for s in plan["main"]],
                              {k: [(s["id"], s["kind"]) for s in v] for k, v in plan["rollback"].items()})
        self.assertEqual(strip(PLAN), strip(ORIGINAL))

    def test_doc_lists_the_same_steps_in_the_same_order(self):
        section = DOC[DOC.index("## Rootless owner runbook"):]
        found = re.findall(r"^\s*\d+\.\s+\[([A-Z0-9-]+)\]", section, re.M)
        self.assertEqual(found, check_runbook.ordered_ids(PLAN))

    def test_driver_implements_every_step_and_follows_runbook_order(self):
        args = swap.parse_args(["--dry-run", "--container", TARGET, "--pause-unit", UNIT, "--private", "/x",
                                "--new-tag", "a:b", "--build-python", "py"])
        driver = swap.Swap(args, system=object(), plan=PLAN)
        self.assertEqual(list(driver.main_impl), [s["id"] for s in PLAN["main"]])
        kinds = {s["kind"] for steps in PLAN["rollback"].values() for s in steps}
        self.assertEqual(kinds, set(driver.rollback_impl))

    def test_driver_refuses_a_runbook_that_breaks_ordering_rules(self):
        plan = copy.deepcopy(PLAN)
        snapshot = next(s for s in plan["main"] if s["kind"] == "snapshot")
        plan["main"].remove(snapshot)
        plan["main"].insert(1, snapshot)
        path = Path(tempfile.mkdtemp()) / "plan.json"
        self.addCleanup(shutil.rmtree, path.parent)
        path.write_text(json.dumps(plan))
        with self.assertRaisesRegex(swap.StepFailed, "ordering rules"):
            swap.load_plan(path)

    def test_doc_names_rootless_controls(self):
        for needle in ("docker run --rm", "--numeric-owner", "readonly", "systemctl --user stop",
                       "never `docker rm`", "65534", "--dry-run", "--apply", "chmod 700", "docker inspect"):
            self.assertIn(needle, DOC)


class PureFunctionTests(unittest.TestCase):
    def base(self):
        return _container("1" * 64, TARGET, OLD_IMAGE, False, [], ["v:/data"], ["PATH=/usr/bin", "APP=1"],
                          {"app": "flock", "base": "1"})

    def test_create_body_changes_only_the_image_and_drops_own_aliases(self):
        before = self.base()
        before["NetworkSettings"]["Networks"]["bridge"]["Aliases"] = ["1" * 12, "flock"]
        body = swap.create_body(before, NEW_IMAGE)
        self.assertEqual(body["Image"], NEW_IMAGE)
        self.assertEqual(body["HostConfig"], before["HostConfig"])
        self.assertEqual(body["NetworkingConfig"]["EndpointsConfig"]["bridge"], {"Aliases": ["flock"]})
        self.assertEqual({k: v for k, v in body.items() if k not in ("Image", "HostConfig", "NetworkingConfig")},
                         {k: v for k, v in before["Config"].items() if k != "Image"})

    def test_inspect_diff_allows_identity_state_and_new_image_additions(self):
        before = self.base()
        after = copy.deepcopy(before)
        after.update(Id="9" * 64, Image=NEW_IMAGE, Created="later", LogPath="/x", State={"Running": False})
        after["Config"].update(Image=NEW_IMAGE, Env=before["Config"]["Env"] + ["PIP_NO_CACHE_DIR=1"],
                               Labels=dict(before["Config"]["Labels"], title="ocr"))
        old_img = {"Env": ["PATH=/usr/bin"], "Labels": {"base": "1"}}
        new_img = {"Env": ["PATH=/usr/bin", "PIP_NO_CACHE_DIR=1"], "Labels": {"base": "1", "title": "ocr"}}
        self.assertEqual(swap.inspect_diff(before, after, old_img, new_img), [])

    def test_inspect_diff_rejects_setting_changes(self):
        before = self.base()
        old_img = new_img = {"Env": ["PATH=/usr/bin"], "Labels": {"base": "1"}}
        cases = {
            "HostConfig.Binds": lambda a: a["HostConfig"].update(Binds=["v:/other"]),
            "HostConfig.Privileged": lambda a: a["HostConfig"].update(Privileged=True),
            "Config.Env.APP": lambda a: a["Config"].update(Env=["PATH=/usr/bin", "APP=2"]),
            "Config.Env.EXTRA": lambda a: a["Config"].update(Env=before["Config"]["Env"] + ["EXTRA=1"]),
            "Config.User": lambda a: a["Config"].update(User="0"),
            "Mounts": lambda a: a.update(Mounts=[{"Type": "volume", "Name": "x", "Destination": "/d"}]),
            "Networks": lambda a: a["NetworkSettings"]["Networks"].update(host={}),
        }
        for expected, change in cases.items():
            with self.subTest(expected=expected):
                after = copy.deepcopy(before)
                change(after)
                self.assertIn(expected, swap.inspect_diff(before, after, old_img, new_img))

    def test_anonymous_volumes_and_overflow_owners_are_detected(self):
        info = self.base()
        info["Mounts"] = [{"Type": "volume", "Name": "abc", "Destination": "/anon", "Source": "/s"},
                          {"Type": "volume", "Name": "v", "Destination": "/data", "Source": "/t"}]
        self.assertEqual(swap.anonymous_volumes(info), ["/anon"])
        manifest = {"entries": {".": {"owner": "0:0"}, "a": {"owner": "65534:0"}, "b": {"owner": "5:65534"}}}
        self.assertEqual(swap.overflow_owners(manifest), ["a", "b"])


class RootlessDryRunTests(RunMixin, unittest.TestCase):
    def test_dry_run_executes_nothing_and_prints_every_step_in_order(self):
        code, out = self.run_swap(self.args("--dry-run"))
        self.assertEqual(code, 0, out)
        self.assertIn("dry-run: OK", out)
        for argv in self.fake.calls:
            if argv[0] == "docker":
                self.assertIn(argv[1], {"info", "inspect", "ps", "image", "top"}, argv)
            elif argv[0] == "systemctl":
                self.assertEqual(argv[2], "show", argv)
            else:
                self.fail(f"dry-run ran {argv}")
        self.assertEqual(self.fake.api_calls, [])
        self.assertEqual(self.fake.helper_runs, [])
        self.assertFalse(self.private.exists(), "dry-run must not create the private directory")
        positions = [out.index(f"[{s['id']}] ") for s in PLAN["main"]]
        self.assertEqual(positions, sorted(positions))
        for step in (s for steps in PLAN["rollback"].values() for s in steps):
            self.assertIn(f"[{step['id']}] ", out)
        self.assertRegex(out, r"would run: docker stop --time 120 1{64}")
        self.assertRegex(out, r"would run: docker stop --time 120 2{64}")
        self.assertNotRegex(out, r"docker stop --time 120 3{64}")
        self.assertIn("would run: systemctl --user stop synthetic-mail.timer", out)
        self.assertIn("not snapshotted: tmpfs:/scratch", out)
        self.assertIn("--numeric-owner", out)
        self.assert_live_unchanged()
        self.assertTrue(all(i["State"]["Running"] for i in self.fake.containers.values()))

    def test_r1_refusals_change_nothing(self):
        cases = {
            "not rootless": lambda f: setattr(f, "rootless", False),
            "name taken": lambda f: f.add(_container("4" * 64, TARGET + "-pre-ocr", OLD_IMAGE, False,
                                                      [], [], [], {})),
            "tag exists": lambda f: f.tags.update({"flock-ocr:synthetic": OLD_IMAGE}),
            "unit missing": lambda f: f.units.pop(f"{UNIT}.service"),
            "file bind": lambda f: f.find(TARGET)["Mounts"].append(
                {"Type": "bind", "Source": str(f.bind / "receipt.json"), "Destination": "/one", "RW": True}),
        }
        for name, change in cases.items():
            for mode in ("--dry-run", "--apply"):
                with self.subTest(case=name, mode=mode):
                    case_dir = self.tmp / f"{name}-{mode}".replace(" ", "_")
                    self.fake = FakeSystem(case_dir)
                    self.private = case_dir / "private"
                    change(self.fake)
                    code, out = self.run_swap(self.args(mode))
                    self.assertEqual(code, swap.EXIT_REFUSED, out)
                    self.assertIn("FAIL at [R1]", out)
                    self.assertEqual(lifecycle(self.fake), [])
                    self.assertEqual(self.fake.helper_runs, [])

    def test_private_dir_inside_git_checkout_is_refused(self):
        (self.tmp / "checkout" / ".git").mkdir(parents=True)
        argv = self.args("--apply")
        argv[argv.index("--private") + 1] = str(self.tmp / "checkout" / "private")
        code, out = self.run_swap(argv)
        self.assertEqual(code, swap.EXIT_REFUSED, out)
        self.assertIn("inside a Git checkout", out)
        self.assertFalse((self.tmp / "checkout" / "private").exists())


class RootlessApplyTests(RunMixin, unittest.TestCase):
    MAIN_EVENTS = ["systemctl stop synthetic-mail.timer", "systemctl stop synthetic-mail.service",
                   "stop old", "stop sidecar", "build flock-ocr:synthetic",
                   "rename old -> flock-target-pre-ocr", "start new", "stop new", "start new",
                   "start sidecar", "systemctl start synthetic-mail.timer"]

    def test_apply_runs_every_step_in_order_and_completes(self):
        code, out = self.run_swap(self.args())
        self.assertEqual(code, 0, out)
        self.assertEqual(lifecycle(self.fake), self.MAIN_EVENTS)
        state = self.state()
        self.assertEqual(state["done"], [s["id"] for s in PLAN["main"]])
        self.assertEqual(state["phase"], "complete")
        receipt = json.loads((self.run_dir() / "snapshot-receipt.json").read_text())
        self.assertIs(receipt["snapshot_ok"], True)
        self.assertEqual(set(receipt["archives_sha256"]), {"m00", "m01"})
        self.assertEqual(list(receipt["sqlite_backups_sha256"]), ["m00/state/flock.db"])
        self.assertEqual(stat.S_IMODE(os.stat(self.private).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(self.run_dir()).st_mode), 0o700)
        new, old = self.fake.find(TARGET), self.fake.find(TARGET + "-pre-ocr")
        self.assertEqual((new["Id"], new["Image"], new["State"]["Running"]), ("9" * 64, NEW_IMAGE, True))
        self.assertEqual((old["Id"], old["State"]["Running"]), ("1" * 64, False))
        self.assertTrue(self.fake.find(UNRELATED)["State"]["Running"])
        self.assertEqual(self.fake.units, {f"{UNIT}.timer": "active", f"{UNIT}.service": "inactive"})
        self.assertNotIn("systemctl start synthetic-mail.service", lifecycle(self.fake))
        self.assert_live_unchanged()
        self.assert_safe_helpers()
        # The backup in the private dir carries the committed WAL rows.
        backup = sqlite3.connect(self.run_dir() / "s2" / "sqlite" / "m00" / "state" / "flock.db.backup")
        try:
            self.assertEqual(backup.execute("select count(*) from synthetic").fetchone()[0], 42)
        finally:
            backup.close()
        body = self.fake.api_calls[0][2]
        self.assertEqual(body["Image"], NEW_IMAGE)
        self.assertEqual(body["HostConfig"], json.loads((self.run_dir() / "inspect-before.json")
                                                        .read_text())["HostConfig"])

    def test_gate_stops_build_when_sqlite_evidence_fails(self):
        self.fake.faults["sqlite-evidence"] = (2, "", "FAIL synthetic")
        code, out = self.run_swap(self.args())
        self.assertEqual(code, swap.EXIT_ROLLED_BACK, out)
        self.assertIn("FAIL at [S2]", out)
        self.assertIn("ROLLBACK RB-A", out)
        self.assertEqual(lifecycle(self.fake), [
            "systemctl stop synthetic-mail.timer", "systemctl stop synthetic-mail.service",
            "stop old", "stop sidecar", "start old", "start sidecar", "systemctl start synthetic-mail.timer"])
        self.assertFalse((self.run_dir() / "snapshot-receipt.json").exists())
        self.assertEqual(self.fake.api_calls, [])
        self.assertEqual(self.state()["phase"], "rolled_back")
        self.assert_live_unchanged()
        self.assert_safe_helpers()

    def test_gate_stops_build_when_restore_test_differs(self):
        self.fake.faults["-xpf /private/s1/m00.tar -C /private/s3/restore-test/m00"] = (0, "", "")
        code, out = self.run_swap(self.args())
        self.assertEqual(code, swap.EXIT_ROLLED_BACK, out)
        self.assertIn("FAIL at [S3]", out)
        self.assertNotIn("build flock-ocr:synthetic", lifecycle(self.fake))
        self.assertFalse((self.run_dir() / "snapshot-receipt.json").exists())
        self.assertEqual(lifecycle(self.fake)[-3:], ["start old", "start sidecar",
                                                     "systemctl start synthetic-mail.timer"])
        self.assert_live_unchanged()

    def test_live_change_before_the_gate_escalates_without_restore_or_start(self):
        def writer():
            (self.fake.volume / "state" / "notes.txt").write_text("changed by an unknown writer\n")
        self.fake.hooks["-cpf /private/s1/m00.tar"] = writer
        code, out = self.run_swap(self.args())
        self.assertEqual(code, swap.EXIT_ESCALATE, out)
        self.assertIn("FAIL at [S1]", out)
        self.assertIn("ESCALATE at [RB-A2]", out)
        events = lifecycle(self.fake)
        self.assertEqual(events, ["systemctl stop synthetic-mail.timer", "systemctl stop synthetic-mail.service",
                                  "stop old", "stop sidecar"])
        self.assertEqual(self.state()["phase"], "escalated")
        self.assert_safe_helpers()

    def test_service_still_running_is_not_interrupted(self):
        self.fake.busy_service = True
        code, out = self.run_swap(self.args())
        self.assertEqual(code, swap.EXIT_ROLLED_BACK, out)
        self.assertIn("FAIL at [Q1]", out)
        self.assertNotIn("systemctl stop synthetic-mail.service", lifecycle(self.fake))
        self.assertEqual(lifecycle(self.fake)[-1], "systemctl start synthetic-mail.timer")
        self.assertEqual(self.fake.helper_runs[0]["mounts"], [("/private", False), ("/recipe", True)])
        self.assertFalse(any("/live/" in " ".join(r["command"]) for r in self.fake.helper_runs))

    def test_open_file_under_a_mount_fails_quiescence(self):
        self.fake.open_files = ["pid 4242 (synthetic-queue)"]
        code, out = self.run_swap(self.args())
        self.assertEqual(code, swap.EXIT_ROLLED_BACK, out)
        self.assertIn("FAIL at [Q2]", out)
        self.assertIn("no data step started", out)
        self.assertEqual(lifecycle(self.fake), [
            "systemctl stop synthetic-mail.timer", "systemctl stop synthetic-mail.service",
            "stop old", "stop sidecar", "start old", "start sidecar", "systemctl start synthetic-mail.timer"])
        self.assertFalse(any("/live/" in " ".join(r["command"]) for r in self.fake.helper_runs))

    def test_create_diff_takes_rb_b_and_restores_old_name(self):
        original = self.fake.docker_api

        def drifting(method, path, body):
            status, response = original(method, path, body)
            self.fake.find("9" * 64)["HostConfig"]["Privileged"] = True
            return status, response
        self.fake.docker_api = drifting
        code, out = self.run_swap(self.args())
        self.assertEqual(code, swap.EXIT_ROLLED_BACK, out)
        self.assertIn("FAIL at [C2]", out)
        self.assertIn("HostConfig.Privileged", out)
        self.assertEqual(lifecycle(self.fake)[-6:], [
            "rename old -> flock-target-pre-ocr", "rename new -> flock-target-ocr-failed",
            "rename old -> flock-target", "start old", "start sidecar", "systemctl start synthetic-mail.timer"])
        self.assertNotIn("start new", lifecycle(self.fake))
        self.assert_live_unchanged()

    def test_start_failure_takes_rb_c_in_order(self):
        self.fake.fail_start.add("9" * 64)
        code, out = self.run_swap(self.args())
        self.assertEqual(code, swap.EXIT_ROLLED_BACK, out)
        self.assertIn("FAIL at [C3]", out)
        tail = lifecycle(self.fake)[lifecycle(self.fake).index("start new") + 1:]
        self.assertEqual(tail, ["systemctl stop synthetic-mail.timer", "systemctl stop synthetic-mail.service",
                                "rename new -> flock-target-ocr-failed", "rename old -> flock-target",
                                "start old", "start sidecar", "systemctl start synthetic-mail.timer"])
        self.assertEqual(self.fake.find(TARGET)["Id"], "1" * 64)
        self.assertIn("mounts differing from S1: none", out)
        self.assert_live_unchanged()
        self.assert_safe_helpers()

    def test_runtime_reverify_failure_takes_rb_c(self):
        self.fake.faults["verify.py --container"] = (1, "FAIL x\nverify: 1 check(s) FAILED\n", "")
        code, out = self.run_swap(self.args())
        self.assertEqual(code, swap.EXIT_ROLLED_BACK, out)
        self.assertIn("FAIL at [A4]", out)
        events = lifecycle(self.fake)
        last_start = len(events) - 1 - events[::-1].index("start new")
        self.assertEqual(events[last_start + 1:], [
            "systemctl stop synthetic-mail.timer", "systemctl stop synthetic-mail.service", "stop new",
            "rename new -> flock-target-ocr-failed", "rename old -> flock-target", "start old",
            "start sidecar", "systemctl start synthetic-mail.timer"])
        self.assertFalse(self.fake.find(TARGET + "-ocr-failed")["State"]["Running"])

    def replacement_writes(self):
        def write():
            (self.fake.volume / "state" / "notes.txt").write_text("written by the replacement\n")
            (self.fake.volume / "state" / "new.txt").write_text("new\n")
        self.fake.hooks["start:" + "9" * 64] = write

    def test_a2_difference_holds_with_everything_stopped(self):
        self.replacement_writes()
        code, out = self.run_swap(self.args())
        self.assertEqual(code, swap.EXIT_HELD, out)
        self.assertIn("HOLD at [A2]", out)
        diff = json.loads((self.run_dir() / "a2" / "diff.json").read_text())
        self.assertEqual(diff, {"m00": ["state/new.txt", "state/notes.txt"]})
        self.assertEqual(self.state()["phase"], "held_a2")
        self.assertFalse(any(self.fake.find(n)["State"]["Running"] for n in (TARGET, SIDECAR)))
        self.assertEqual(self.fake.units[f"{UNIT}.timer"], "inactive")
        self.assertEqual(lifecycle(self.fake).count("start new"), 1)

    def test_resume_requires_the_reviewed_diff_hash_then_completes(self):
        self.replacement_writes()
        self.assertEqual(self.run_swap(self.args())[0], swap.EXIT_HELD)
        run = str(self.run_dir())
        before = len(lifecycle(self.fake))
        code, out = self.run_swap(["--resume", run, "--accept-a2", "0" * 64])
        self.assertEqual(code, swap.EXIT_REFUSED, out)
        self.assertEqual(len(lifecycle(self.fake)), before)
        digest = swap.sha256_file(Path(run) / "a2" / "diff.json")
        self.fake.hooks.clear()
        code, out = self.run_swap(["--resume", run, "--accept-a2", digest])
        self.assertEqual(code, 0, out)
        self.assertEqual(lifecycle(self.fake)[before:], ["start new", "start sidecar",
                                                         "systemctl start synthetic-mail.timer"])
        self.assertEqual(self.state()["phase"], "complete")

    def test_rollback_from_a2_hold_restores_snapshot_and_keeps_current_state(self):
        self.replacement_writes()
        self.assertEqual(self.run_swap(self.args())[0], swap.EXIT_HELD)
        run = self.run_dir()
        code, out = self.run_swap(["--rollback", str(run)])
        self.assertEqual(code, swap.EXIT_ROLLED_BACK, out)
        self.assertIn("mounts differing from S1: ['m00']", out)
        self.assert_live_unchanged()
        self.assertTrue((run / "rollback" / "m00.current.tar").is_file())
        listing = subprocess.run(["tar", "-tf", str(run / "rollback" / "m00.current.tar")],
                                 capture_output=True, text=True, check=True).stdout
        self.assertIn("./state/new.txt", listing)
        old, failed = self.fake.find(TARGET), self.fake.find(TARGET + "-ocr-failed")
        self.assertEqual((old["Id"], old["State"]["Running"]), ("1" * 64, True))
        self.assertEqual((failed["Id"], failed["State"]["Running"]), ("9" * 64, False))
        self.assertEqual(lifecycle(self.fake)[-3:], ["start old", "start sidecar",
                                                     "systemctl start synthetic-mail.timer"])
        self.assert_safe_helpers(allow_restore=True)
        self.assertEqual(self.state()["phase"], "rolled_back")
        code, out = self.run_swap(["--rollback", str(run)])
        self.assertEqual(code, swap.EXIT_REFUSED, out)


if __name__ == "__main__":
    unittest.main()
