"""Rootless OCR swap driver: ordering, dry-run and rollback regressions.

Everything here is synthetic. A fake Docker/systemd records every command;
helper containers are emulated by running their command locally with the
container paths (/live, /private, /recipe) mapped onto temporary directories,
so the real tar and snapshot_evidence.py run against a synthetic WAL database.
No Docker daemon, systemd unit, network or real data is touched.
"""
import contextlib
import copy
import importlib.util
import io
import json
import os
import re
import shutil
import socket
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
            OLD_IMAGE: {"Id": OLD_IMAGE, "Config": {"Env": ["PATH=/usr/bin"], "Labels": {"base": "1"}},
                        "RootFS": {"Type": "layers", "Layers": ["sha256:" + "0a" * 32]}},
            OTHER_IMAGE: {"Id": OTHER_IMAGE, "Config": {"Env": [], "Labels": {}},
                          "RootFS": {"Type": "layers", "Layers": ["sha256:" + "0e" * 32]}}}
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
            "Env": ["PATH=/usr/bin", "PIP_NO_CACHE_DIR=1"], "Labels": {"base": "1", "title": "ocr"}},
            "RootFS": {"Type": "layers", "Layers": ["sha256:" + "0a" * 32, "sha256:" + "0c" * 32]}}
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


def malformed_sqlite(path):
    """Write a synthetic SQLite file whose schema page is deliberately corrupt."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("create table synthetic(value)")
    conn.executemany("insert into synthetic values (?)", [(n,) for n in range(20)])
    conn.commit()
    conn.close()
    data = bytearray(path.read_bytes())
    data[100] = 0xFF  # page-1 b-tree type byte: "database disk image is malformed"
    path.write_bytes(bytes(data))
    return swap.sha256_file(path)


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

    @staticmethod
    def tar_listing(archive):
        return subprocess.run(["tar", "-tf", str(archive)], capture_output=True, text=True, check=True).stdout

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
                       "never `docker rm`", "65534", "--dry-run", "--apply", "chmod 700", "docker inspect",
                       "non_archivable", "socket ignored"):
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

    def test_inspect_diff_ignores_daemon_canonical_null_and_image_manifest_descriptor(self):
        # Swap attempt 3 (2026-10-19, dockerd 29.6 / API 1.55) failed at C2 on exactly these
        # two keys: the saved container (older daemon) stored OomKillDisable null, the
        # replacement created from the same body reported false; and the new API field
        # ImageManifestDescriptor carries the image ID, which changes by design.
        before = self.base()
        before["HostConfig"]["OomKillDisable"] = None
        before["ImageManifestDescriptor"] = {"mediaType": "application/vnd.oci.image.manifest.v1+json",
                                             "digest": OLD_IMAGE, "size": 4034}
        after = copy.deepcopy(before)
        after.update(Id="9" * 64, Image=NEW_IMAGE)
        after["Config"]["Image"] = NEW_IMAGE
        after["HostConfig"]["OomKillDisable"] = False
        after["ImageManifestDescriptor"] = {"mediaType": "application/vnd.oci.image.manifest.v1+json",
                                            "digest": NEW_IMAGE, "size": 4915}
        old_img = new_img = {"Env": ["PATH=/usr/bin"], "Labels": {"base": "1"}}
        self.assertEqual(swap.inspect_diff(before, after, old_img, new_img), [])
        # absent on the saved side is the same as null
        del before["HostConfig"]["OomKillDisable"]
        self.assertEqual(swap.inspect_diff(before, after, old_img, new_img), [])
        # a real change of the setting is still a difference
        after["HostConfig"]["OomKillDisable"] = True
        self.assertEqual(swap.inspect_diff(before, after, old_img, new_img), ["HostConfig.OomKillDisable"])

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

    # Create-only probe of rootless Docker 29.6.2 / API 1.55 (old image, no mounts,
    # network none, removed right after): HostConfig value sent -> value stored.
    PROBED_EQUAL = [("OomKillDisable", None, False), ("MemorySwappiness", -1, None),
                    ("PidsLimit", 0, None), ("PidsLimit", -1, None), ("ConsoleSize", None, [0, 0]),
                    ("Mounts", [], "<absent>"), ("Mounts", None, "<absent>"),
                    ("Links", [], None), ("Tmpfs", {}, "<absent>"), ("Sysctls", {}, "<absent>"),
                    ("StorageOpt", {}, "<absent>"), ("Annotations", {}, "<absent>"),
                    ("Init", None, "<absent>")]
    # Same probe: the daemon fills its own default for an unset value. The saved
    # inspect always carries the explicit value, so a difference here is a real
    # change (or a changed daemon default) and must still fail.
    PROBED_STRICT = [("MaskedPaths", ["/proc/kcore"], None), ("MaskedPaths", None, []),
                     ("ReadonlyPaths", ["/proc/sys"], None), ("IpcMode", "private", ""),
                     ("CgroupnsMode", "private", "host"), ("Runtime", "runc", ""),
                     ("ShmSize", 67108864, 0), ("LogConfig", {"Type": "json-file", "Config": {}}, {}),
                     ("RestartPolicy", {"Name": "unless-stopped", "MaximumRetryCount": 0},
                      {"Name": "no", "MaximumRetryCount": 0}),
                     ("OomKillDisable", None, True), ("OomKillDisable", False, True),
                     ("PidsLimit", 768, None), ("MemorySwappiness", 60, None), ("ConsoleSize", None, [24, 80]),
                     ("Tmpfs", {"/tmp": "size=2g"}, {}), ("Sysctls", {"net.ipv6.conf.all.disable_ipv6": "1"}, {}),
                     ("Mounts", [{"Type": "volume", "Source": "v", "Target": "/home"}], [])]

    @staticmethod
    def _set(host, key, value):
        if value == "<absent>":
            host.pop(key, None)
        else:
            host[key] = value

    def attempt3(self):
        """Synthetic before/after pair with the shape of the attempt-3 [C2] failure."""
        before = self.base()
        before["HostConfig"].update(OomKillDisable=None, PidsLimit=768, MemorySwappiness=None,
                                    CapAdd=["CAP_CHOWN"], CapDrop=["ALL"], SecurityOpt=["no-new-privileges:true"],
                                    MaskedPaths=["/proc/kcore"], ReadonlyPaths=["/proc/sys"], ReadonlyRootfs=True,
                                    Tmpfs={"/tmp": "rw,size=2g"}, ConsoleSize=[0, 0])
        before["Platform"] = "linux"
        before["ImageManifestDescriptor"] = {"digest": OLD_IMAGE, "size": 4034,
                                             "mediaType": "application/vnd.oci.image.manifest.v1+json",
                                             "platform": {"architecture": "amd64", "os": "linux"}}
        after = copy.deepcopy(before)
        after.update(Id="9" * 64, Image=NEW_IMAGE, Created="later", State={"Running": False})
        after["Config"].update(Image=NEW_IMAGE, Labels=dict(before["Config"]["Labels"], title="ocr"))
        after["HostConfig"]["OomKillDisable"] = False
        after["ImageManifestDescriptor"] = dict(before["ImageManifestDescriptor"], digest=NEW_IMAGE, size=4915)
        old_img = {"Env": ["PATH=/usr/bin"], "Labels": {"base": "1"}}
        new_img = {"Env": ["PATH=/usr/bin"], "Labels": {"base": "1", "title": "ocr"}}
        return before, after, old_img, new_img

    def test_attempt3_image_descriptor_and_oom_kill_default_are_not_differences(self):
        before, after, old_img, new_img = self.attempt3()
        self.assertEqual(swap.inspect_diff(before, after, old_img, new_img), [])
        self.assertEqual(swap.default_normalisations(before, after), ["HostConfig.OomKillDisable: null == false"])
        # An older daemon without the descriptor on the saved side is not a difference either.
        del before["ImageManifestDescriptor"]
        self.assertEqual(swap.inspect_diff(before, after, old_img, new_img), [])

    def test_inspect_diff_treats_probed_api_defaults_as_equal(self):
        for key, sent, stored in self.PROBED_EQUAL:
            for left, right in ((sent, stored), (stored, sent)):
                with self.subTest(key=key, left=left, right=right):
                    before, after, old_img, new_img = self.attempt3()
                    self._set(before["HostConfig"], key, left)
                    self._set(after["HostConfig"], key, right)
                    self.assertEqual(swap.inspect_diff(before, after, old_img, new_img), [])

    def test_inspect_diff_still_fails_on_real_changes_and_daemon_filled_defaults(self):
        for key, saved, created in self.PROBED_STRICT:
            with self.subTest(key=key, saved=saved, created=created):
                before, after, old_img, new_img = self.attempt3()
                self._set(before["HostConfig"], key, saved)
                self._set(after["HostConfig"], key, created)
                self.assertEqual(swap.inspect_diff(before, after, old_img, new_img), [f"HostConfig.{key}"])

    def test_inspect_diff_still_fails_on_mounts_env_network_user_caps_security(self):
        cases = {
            "HostConfig.CapAdd": lambda a: a["HostConfig"].update(CapAdd=["CAP_CHOWN", "CAP_SYS_ADMIN"]),
            "HostConfig.CapDrop": lambda a: a["HostConfig"].update(CapDrop=None),
            "HostConfig.SecurityOpt": lambda a: a["HostConfig"].update(SecurityOpt=[]),
            "HostConfig.ReadonlyRootfs": lambda a: a["HostConfig"].update(ReadonlyRootfs=False),
            "HostConfig.NetworkMode": lambda a: a["HostConfig"].update(NetworkMode="host"),
            "HostConfig.Binds": lambda a: a["HostConfig"].update(Binds=[]),
            "Mounts": lambda a: a.update(Mounts=[{"Type": "bind", "Source": "/x", "Destination": "/data"}]),
            "Networks": lambda a: a["NetworkSettings"]["Networks"].update(other={"Aliases": ["x"]}),
            "Config.User": lambda a: a["Config"].update(User="0"),
            "Config.Entrypoint": lambda a: a["Config"].update(Entrypoint=[]),
            "Config.Volumes": lambda a: a["Config"].update(Volumes={"/anon": {}}),
            "Config.Env.APP": lambda a: a["Config"].update(Env=["PATH=/usr/bin"]),
            "Config.Labels.app": lambda a: a["Config"]["Labels"].update(app="other"),
            "Platform": lambda a: a.update(Platform="windows"),
        }
        for expected, change in cases.items():
            with self.subTest(expected=expected):
                before, after, old_img, new_img = self.attempt3()
                change(after)
                self.assertEqual(swap.inspect_diff(before, after, old_img, new_img), [expected])

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
        attempt = run / "rollback" / "m00" / "attempt-001"
        self.assertTrue((attempt / "receipt.json").is_file())
        self.assertIn("./state/new.txt", self.tar_listing(attempt / "current.tar"))
        self.assertEqual([p.name for p in (run / "rollback" / "m00").iterdir()], ["attempt-001"])
        old, failed = self.fake.find(TARGET), self.fake.find(TARGET + "-ocr-failed")
        self.assertEqual((old["Id"], old["State"]["Running"]), ("1" * 64, True))
        self.assertEqual((failed["Id"], failed["State"]["Running"]), ("9" * 64, False))
        self.assertEqual(lifecycle(self.fake)[-3:], ["start old", "start sidecar",
                                                     "systemctl start synthetic-mail.timer"])
        self.assert_safe_helpers(allow_restore=True)
        self.assertEqual(self.state()["phase"], "rolled_back")
        code, out = self.run_swap(["--rollback", str(run)])
        self.assertEqual(code, swap.EXIT_REFUSED, out)


class AcceptMalformedSqliteTests(RunMixin, unittest.TestCase):
    """A hash-pinned, intentionally corrupt fixture is exempt from integrity only."""

    FIXTURE = "fixtures/regressions-v1/db-mutation/ledger.sqlite"
    COPY = "fixtures/regressions-v2/db-mutation/ledger.sqlite"

    def setUp(self):
        super().setUp()
        self.digest = malformed_sqlite(self.fake.bind / self.FIXTURE)
        self.live_before = {"vol": evidence.manifest(self.fake.volume), "bind": evidence.manifest(self.fake.bind)}

    def pinned(self, *pins, mode="--apply"):
        argv = self.args(mode)
        for pin in pins:
            argv += ["--accept-malformed-sqlite", pin]
        return argv

    def evidence_runs(self):
        return [r["command"] for r in self.fake.helper_runs if "sqlite-evidence" in r["command"]]

    def test_default_still_refuses_a_malformed_database(self):
        code, out = self.run_swap(self.args())
        self.assertEqual(code, swap.EXIT_ROLLED_BACK, out)
        self.assertIn("FAIL at [S2]", out)
        self.assertIn(f"SQLite evidence failed for {self.FIXTURE}: database disk image is malformed", out)
        failure = json.loads((self.run_dir() / "s2" / "m01.sqlite-evidence-failure.json").read_text())
        self.assertEqual(failure["exit"], 2)
        self.assertFalse((self.run_dir() / "snapshot-receipt.json").exists())
        self.assertEqual(self.fake.api_calls, [])
        self.assertFalse(any("--accept-malformed" in c for c in self.evidence_runs()))
        self.assert_live_unchanged()

    def test_pinned_fixture_is_archived_restore_tested_and_hash_matched(self):
        code, out = self.run_swap(self.pinned(f"{self.FIXTURE}={self.digest}"))
        self.assertEqual(code, 0, out)
        self.assertEqual(self.state()["done"], [s["id"] for s in PLAN["main"]])
        receipt = json.loads((self.run_dir() / "snapshot-receipt.json").read_text())
        self.assertEqual(receipt["sqlite_accepted_malformed_sha256"], {f"m01/{self.FIXTURE}": self.digest})
        self.assertEqual(list(receipt["sqlite_backups_sha256"]), ["m00/state/flock.db"])
        report = json.loads((self.run_dir() / "s2" / "m01.sqlite-evidence.json").read_text())
        self.assertEqual(report["databases"], {self.FIXTURE: {"accepted_malformed": True, "sha256": self.digest,
                                                              "integrity_check": None, "ok": True}})
        self.assertFalse((self.run_dir() / "s2" / "sqlite" / "m01" / (self.FIXTURE + ".backup")).exists())
        # Archived: the S1 tar holds it. Restore-tested and hash-matched: S3
        # and A2 compare full manifests, which include its sha256.
        self.assertIn(f"./{self.FIXTURE}", self.tar_listing(self.run_dir() / "s1" / "m01.tar"))
        baseline = json.loads((self.run_dir() / "s1" / "m01.baseline.json").read_text())
        self.assertEqual(baseline["entries"][self.FIXTURE]["sha256"], self.digest)
        restored = json.loads((self.run_dir() / "s3" / "m01.restored.json").read_text())
        self.assertEqual(restored["entries"][self.FIXTURE]["sha256"], self.digest)
        self.assertIn("1 hash-pinned malformed exempted", out)
        runs = self.evidence_runs()
        self.assertEqual(len(runs), 4)  # S2 and A2, two mounts each
        for cmd in runs:
            pinned = "--accept-malformed" in cmd
            self.assertEqual(pinned, "/private/s2/inspect/m01" in cmd or "/private/a2/inspect/m01" in cmd, cmd)
            if pinned:
                self.assertEqual(cmd[cmd.index("--accept-malformed") + 1], f"{self.FIXTURE}={self.digest}")
        self.assert_live_unchanged()
        self.assert_safe_helpers()

    def test_pin_with_another_hash_fails_at_s2(self):
        code, out = self.run_swap(self.pinned(f"{self.FIXTURE}={'0' * 64}"))
        self.assertEqual(code, swap.EXIT_ROLLED_BACK, out)
        self.assertIn("FAIL at [S2]", out)
        self.assertIn("no snapshotted mount holds that path with the pinned sha256", out)
        self.assertFalse((self.run_dir() / "snapshot-receipt.json").exists())
        self.assert_live_unchanged()

    def test_pin_for_a_path_that_does_not_exist_fails_at_s2(self):
        code, out = self.run_swap(self.pinned(f"fixtures/absent/ledger.sqlite={self.digest}"))
        self.assertEqual(code, swap.EXIT_ROLLED_BACK, out)
        self.assertIn("FAIL at [S2]", out)
        self.assertFalse((self.run_dir() / "snapshot-receipt.json").exists())

    def test_pin_exempts_only_its_own_path_and_every_failure_is_named(self):
        (self.fake.bind / self.COPY).parent.mkdir(parents=True)
        shutil.copyfile(self.fake.bind / self.FIXTURE, self.fake.bind / self.COPY)
        code, out = self.run_swap(self.args())
        self.assertEqual(code, swap.EXIT_ROLLED_BACK, out)
        failure = json.loads((self.run_dir() / "s2" / "m01.sqlite-evidence-failure.json").read_text())
        named = [line for line in failure["stderr"] if line.startswith("FAIL SQLite evidence failed")]
        self.assertEqual(len(named), 2, failure)
        self.assertTrue(any(self.FIXTURE in line for line in named))
        self.assertTrue(any(self.COPY in line for line in named))
        shutil.rmtree(self.private)
        code, out = self.run_swap(self.pinned(f"{self.FIXTURE}={self.digest}"))
        self.assertEqual(code, swap.EXIT_ROLLED_BACK, out)
        self.assertIn(f"SQLite evidence failed for {self.COPY}", out)
        self.assertNotIn(f"SQLite evidence failed for {self.FIXTURE}", out)
        shutil.rmtree(self.private)
        code, out = self.run_swap(self.pinned(f"{self.FIXTURE}={self.digest}", f"{self.COPY}={self.digest}"))
        self.assertEqual(code, 0, out)
        receipt = json.loads((self.run_dir() / "snapshot-receipt.json").read_text())
        self.assertEqual(receipt["sqlite_accepted_malformed_sha256"],
                         {f"m01/{self.FIXTURE}": self.digest, f"m01/{self.COPY}": self.digest})

    def test_dry_run_names_the_exemption_and_changes_nothing(self):
        code, out = self.run_swap(self.pinned(f"{self.FIXTURE}={self.digest}", mode="--dry-run"))
        self.assertEqual(code, 0, out)
        self.assertIn(f"would exempt from the SQLite integrity requirement only: {self.FIXTURE} "
                      f"(sha256 {self.digest})", out)
        self.assertFalse(self.private.exists())
        self.assert_live_unchanged()

    def test_malformed_pins_are_refused_before_anything_runs(self):
        good = f"{self.FIXTURE}={self.digest}"
        for pin in (self.FIXTURE, f"{self.FIXTURE}={'A' * 64}", f"{self.FIXTURE}={self.digest[:-1]}",
                    f"/{self.FIXTURE}={self.digest}", f"fixtures/../{self.FIXTURE}={self.digest}",
                    f"./{self.FIXTURE}={self.digest}", f"={self.digest}"):
            with self.subTest(pin=pin):
                with contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit) as raised:
                    swap.parse_args(self.pinned(pin))
                self.assertEqual(raised.exception.code, 2)
                self.assertIn("--accept-malformed-sqlite", err.getvalue())
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            swap.parse_args(self.pinned(good, good))
        with contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit):
            swap.parse_args(["--resume", str(self.tmp), "--accept-malformed-sqlite", good])
        self.assertIn("fixed at --apply", err.getvalue())
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(swap.parse_args(self.args()).accepted_malformed_sqlite, {})


class RollbackPreservationTests(RunMixin, unittest.TestCase):
    """Rollback retries never replace an earlier preserved current state."""

    EXTRACT_S1 = "-xpf /private/s1/m00.tar -C /live/m00"

    def held_run(self):
        def write():
            (self.fake.volume / "state" / "notes.txt").write_text("written by the replacement\n")
            (self.fake.volume / "state" / "new.txt").write_text("replacement-only work\n")
        self.fake.hooks["start:" + "9" * 64] = write
        self.assertEqual(self.run_swap(self.args())[0], swap.EXIT_HELD)
        self.fake.hooks.clear()
        return self.run_dir()

    def guard_starts(self):
        """Every start must find live data already equal to the S1 baseline."""
        self.starts_seen = []

        def check():
            self.starts_seen.append(evidence.compare(self.live_before["vol"], evidence.manifest(self.fake.volume)))
        self.fake.hooks["docker start"] = check
        self.fake.hooks["systemctl --user start"] = check

    @staticmethod
    def file_hashes(directory):
        return {p.name: swap.sha256_file(p) for p in sorted(directory.iterdir()) if p.is_file()}

    def extract(self, archive):
        dest = Path(tempfile.mkdtemp(dir=self.tmp))
        subprocess.run(["tar", "-xf", str(archive), "-C", str(dest)], check=True)
        return dest

    def destructive_helpers(self, since=0):
        return [r["command"] for r in self.fake.helper_runs[since:]
                if any(d.startswith("/live/") and not ro for d, ro in r["mounts"])]

    def test_retry_after_failed_extraction_keeps_the_original_preservation(self):
        run = self.held_run()
        self.guard_starts()
        self.fake.faults[self.EXTRACT_S1] = (1, "", "synthetic extraction failure")
        events = len(lifecycle(self.fake))
        code, out = self.run_swap(["--rollback", str(run)])
        self.assertEqual(code, swap.EXIT_ESCALATE, out)
        self.assertIn("ESCALATE at [RB-C4]", out)
        self.assertFalse(any(e.startswith(("start", "systemctl start")) for e in lifecycle(self.fake)[events:]))
        self.assertEqual(self.starts_seen, [])
        self.assertFalse((self.fake.volume / "state" / "new.txt").exists(), "live was cleared before the fault")
        first = run / "rollback" / "m00" / "attempt-001"
        receipt = json.loads((first / "receipt.json").read_text())
        hashes = self.file_hashes(first)
        self.assertEqual(hashes["current.tar"], receipt["archive_sha256"])
        self.assertEqual(self.state()["restoring"]["m00"], {"preserved_in": "attempt-001", "clear_started": True})

        self.fake.faults.clear()
        code, out = self.run_swap(["--rollback", str(run)])
        self.assertEqual(code, swap.EXIT_ROLLED_BACK, out)
        self.assertEqual(self.file_hashes(first), hashes, "attempt-001 was rewritten by the retry")
        kept = self.extract(first / "current.tar")
        self.assertEqual((kept / "state" / "new.txt").read_text(), "replacement-only work\n")
        self.assertEqual((kept / "state" / "notes.txt").read_text(), "written by the replacement\n")
        second = run / "rollback" / "m00" / "attempt-002"
        self.assertTrue((second / "receipt.json").is_file(), "the partial live state is preserved separately")
        self.assertIn("attempt-001, attempt-002", out)
        self.assert_live_unchanged()
        self.assertEqual(self.state()["phase"], "rolled_back")
        self.assertEqual([p["attempt"] for p in self.state()["preservations"]["m00"]],
                         ["attempt-001", "attempt-002"])
        self.assertTrue(self.starts_seen)
        self.assertTrue(all(diff == [] for diff in self.starts_seen), self.starts_seen)
        self.assertEqual(lifecycle(self.fake)[-3:], ["start old", "start sidecar",
                                                     "systemctl start synthetic-mail.timer"])
        self.assert_safe_helpers(allow_restore=True)

    def test_interrupted_preservation_is_kept_untouched_and_never_used(self):
        run = self.held_run()
        self.guard_starts()

        def interrupt():
            raise KeyboardInterrupt("synthetic interruption while verifying the capture")
        self.fake.hooks["-xpf /private/rollback/m00/attempt-001/current.tar"] = interrupt
        helpers = len(self.fake.helper_runs)
        with self.assertRaises(KeyboardInterrupt):
            self.run_swap(["--rollback", str(run)])
        self.assertEqual(self.destructive_helpers(helpers), [])
        self.assertEqual((self.fake.volume / "state" / "new.txt").read_text(), "replacement-only work\n")
        first = run / "rollback" / "m00" / "attempt-001"
        self.assertFalse((first / "receipt.json").exists())
        hashes = self.file_hashes(first)
        self.assertEqual(self.state()["phase"], "rollback:RB-C")
        self.assertNotIn("preservations", self.state())
        self.assertEqual(self.starts_seen, [])

        del self.fake.hooks["-xpf /private/rollback/m00/attempt-001/current.tar"]
        code, out = self.run_swap(["--rollback", str(run)])
        self.assertEqual(code, swap.EXIT_ROLLED_BACK, out)
        self.assertIn("attempt-001 has no receipt (interrupted capture); kept untouched, not used", out)
        self.assertEqual(self.file_hashes(first), hashes)
        second = run / "rollback" / "m00" / "attempt-002"
        self.assertEqual(self.state()["restoring"]["m00"]["preserved_in"], "attempt-002")
        kept = self.extract(second / "current.tar")
        self.assertEqual((kept / "state" / "new.txt").read_text(), "replacement-only work\n")
        self.assert_live_unchanged()
        self.assertTrue(self.starts_seen and all(diff == [] for diff in self.starts_seen), self.starts_seen)

    def test_failed_capture_leaves_live_untouched_and_retry_uses_a_new_attempt(self):
        run = self.held_run()
        first = run / "rollback" / "m00" / "attempt-001"

        def partial():
            (first / "current.tar").write_bytes(b"truncated")
        self.fake.hooks["-cpf /private/rollback/m00/attempt-001/current.tar"] = partial
        self.fake.faults["-cpf /private/rollback/m00/attempt-001/current.tar"] = (2, "", "synthetic tar failure")
        helpers = len(self.fake.helper_runs)
        code, out = self.run_swap(["--rollback", str(run)])
        self.assertEqual(code, swap.EXIT_ESCALATE, out)
        self.assertEqual(self.destructive_helpers(helpers), [])
        self.assertEqual((self.fake.volume / "state" / "new.txt").read_text(), "replacement-only work\n")
        self.fake.hooks.clear()
        self.fake.faults.clear()
        code, out = self.run_swap(["--rollback", str(run)])
        self.assertEqual(code, swap.EXIT_ROLLED_BACK, out)
        self.assertEqual((first / "current.tar").read_bytes(), b"truncated")
        self.assertFalse((first / "receipt.json").exists())
        self.assertTrue((run / "rollback" / "m00" / "attempt-002" / "receipt.json").is_file())
        self.assert_live_unchanged()

    def test_tampered_preservation_stops_before_live_is_touched(self):
        run = self.held_run()
        self.fake.faults[self.EXTRACT_S1] = (1, "", "synthetic extraction failure")
        self.assertEqual(self.run_swap(["--rollback", str(run)])[0], swap.EXIT_ESCALATE)
        self.fake.faults.clear()
        archive = run / "rollback" / "m00" / "attempt-001" / "current.tar"
        os.chmod(archive, 0o600)
        with open(archive, "ab") as handle:
            handle.write(b"\0")
        self.guard_starts()
        helpers, events = len(self.fake.helper_runs), len(lifecycle(self.fake))
        code, out = self.run_swap(["--rollback", str(run)])
        self.assertEqual(code, swap.EXIT_ESCALATE, out)
        self.assertIn("attempt-001 failed validation (current.tar differs from its receipt)", out)
        self.assertEqual(self.destructive_helpers(helpers), [])
        self.assertFalse(any(e.startswith(("start", "systemctl start")) for e in lifecycle(self.fake)[events:]))
        self.assertFalse((run / "rollback" / "m00" / "attempt-002").exists())

    def test_reused_preservation_when_live_still_matches(self):
        run = self.held_run()
        # Fail the clear after the capture: live is never changed, so the
        # retry reuses the verified attempt instead of capturing again.
        self.fake.faults["/live/m00 -mindepth 1 -delete"] = (1, "", "synthetic delete failure")
        self.assertEqual(self.run_swap(["--rollback", str(run)])[0], swap.EXIT_ESCALATE)
        self.fake.faults.clear()
        code, out = self.run_swap(["--rollback", str(run)])
        self.assertEqual(code, swap.EXIT_ROLLED_BACK, out)
        self.assertIn("live already preserved in $RUN/rollback/m00/attempt-001; not re-archived", out)
        self.assertEqual([p.name for p in (run / "rollback" / "m00").iterdir()], ["attempt-001"])
        self.assert_live_unchanged()


def make_socket(directory, name):
    """Bind a synthetic Unix socket file at directory/name (relative bind: no path-length limit)."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    cwd = os.getcwd()
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        os.chdir(directory)
        sock.bind(name)
    finally:
        os.chdir(cwd)
        sock.close()
    os.chmod(directory / name, 0o600)
    return directory / name


class NonArchivableSpecialFileTests(RunMixin, unittest.TestCase):
    """Sockets and other special files are recorded, named and excluded, never silently dropped."""

    CODEX = ".codex/app-server-control/app-server-control.sock"
    LIFECYCLE = "tmp/host-rehearsal/control-v1/lifecycle/" + "ab" * 8 + "/control.sock"
    FIFO = "tmp/host-rehearsal/control-v1/events.fifo"

    def setUp(self):
        super().setUp()
        make_socket(self.fake.volume / Path(self.CODEX).parent, Path(self.CODEX).name)
        make_socket(self.fake.bind / Path(self.LIFECYCLE).parent, Path(self.LIFECYCLE).name)
        os.mkfifo(self.fake.bind / self.FIFO, 0o600)
        self.live_before = {"vol": evidence.manifest(self.fake.volume), "bind": evidence.manifest(self.fake.bind)}
        self.expected = {"m00": {self.CODEX: "socket"},
                         "m01": {self.FIFO: "fifo", self.LIFECYCLE: "socket"}}

    def assert_live_archivable_unchanged(self):
        for key, root in (("vol", self.fake.volume), ("bind", self.fake.bind)):
            diff, _ = evidence.compare_archivable(self.live_before[key], evidence.manifest(root))
            self.assertEqual(diff, [], key)

    def test_manifest_records_special_files_as_non_archivable(self):
        vol, bind = self.live_before["vol"], self.live_before["bind"]
        self.assertEqual(vol["non_archivable"], self.expected["m00"])
        self.assertEqual(bind["non_archivable"], self.expected["m01"])
        self.assertEqual(vol["entries"][self.CODEX]["type"], "other")
        self.assertEqual(vol["entries"][self.CODEX]["special"], "socket")
        self.assertEqual(vol["entries"][self.CODEX]["mode"], "0600")
        self.assertEqual(evidence.non_archivable(bind), self.expected["m01"])
        empty = self.tmp / "empty"
        (empty / "d").mkdir(parents=True)
        (empty / "d" / "f.txt").write_text("x\n")
        self.assertEqual(evidence.manifest(empty)["non_archivable"], {})

    def test_compare_archivable_excludes_only_special_files(self):
        base = self.live_before["vol"]
        missing = copy.deepcopy(base)
        del missing["entries"][self.CODEX]
        missing["non_archivable"] = {}
        self.assertEqual(evidence.compare(base, missing), [self.CODEX])  # strict compare unchanged
        self.assertEqual(evidence.compare_archivable(base, missing), ([], [self.CODEX]))
        # A special file replaced by a regular file, directory or symlink is a difference.
        for replacement in ({"type": "f", "owner": "1000:1000", "mode": "0600", "sha256": "0" * 64, "size": 0},
                            {"type": "d", "owner": "1000:1000", "mode": "0700"},
                            {"type": "l", "owner": "1000:1000", "mode": "0777", "target": "elsewhere"}):
            with self.subTest(replacement=replacement["type"]):
                other = copy.deepcopy(base)
                other["entries"][self.CODEX] = replacement
                self.assertEqual(evidence.compare_archivable(base, other)[0], [self.CODEX])
                self.assertEqual(evidence.compare_archivable(other, base)[0], [self.CODEX])
        # Regular file, directory and symlink differences still count.
        edited = copy.deepcopy(base)
        edited["entries"]["state/notes.txt"]["sha256"] = "1" * 64
        edited["entries"]["state"]["mode"] = "0777"
        self.assertEqual(evidence.compare_archivable(base, edited)[0], ["state", "state/notes.txt"])
        # A manifest written before "special" existed (type "other" only) reads the same.
        legacy = copy.deepcopy(base)
        legacy["entries"][self.CODEX].pop("special")
        legacy.pop("non_archivable")
        self.assertEqual(evidence.non_archivable(legacy), {self.CODEX: "other"})
        self.assertEqual(evidence.compare_archivable(legacy, missing), ([], [self.CODEX]))
        # Devices are classified as special too (synthetic entries: no mknod in tests).
        for kind in ("char_device", "block_device", "unknown"):
            dev = copy.deepcopy(missing)
            dev["entries"]["dev/node"] = {"type": "other", "special": kind, "owner": "0:0", "mode": "0600"}
            self.assertEqual(evidence.compare_archivable(dev, missing), ([], ["dev/node"]))

    def test_apply_completes_and_names_every_excluded_path(self):
        code, out = self.run_swap(self.args())
        self.assertEqual(code, 0, out)
        self.assertEqual(self.state()["phase"], "complete")
        run = self.run_dir()
        # Recorded in every manifest the run wrote...
        for name in ("s1/m00.baseline.json", "s1/m00.after-tar.json", "s3/m00.live-after.json",
                     "a2/m00.baseline.json"):
            self.assertEqual(json.loads((run / name).read_text())["non_archivable"], self.expected["m00"], name)
        self.assertEqual(json.loads((run / "s1" / "m01.baseline.json").read_text())["non_archivable"],
                         self.expected["m01"])
        # ...absent from the archive and the fresh restore (the S3 failure of attempt 2)...
        self.assertNotIn(self.CODEX, self.tar_listing(run / "s1" / "m00.tar"))
        self.assertNotIn(self.LIFECYCLE, self.tar_listing(run / "s1" / "m01.tar"))
        restored = json.loads((run / "s3" / "m00.restored.json").read_text())
        self.assertNotIn(self.CODEX, restored["entries"])
        self.assertEqual(evidence.compare(json.loads((run / "s1" / "m00.baseline.json").read_text()), restored),
                         [self.CODEX])
        # ...and listed by path in the receipt and the run log.
        receipt = json.loads((run / "snapshot-receipt.json").read_text())
        self.assertEqual(receipt["non_archivable"], self.expected)
        self.assertEqual(self.state()["non_archivable"]["s1"], self.expected)
        self.assertIn(f"m00: 1 non-archivable special file(s), recorded in the manifest but not restored "
                      f"from the archive; excluded from every equality check: {self.CODEX} (socket)", out)
        self.assertIn(f"{self.FIFO} (fifo), {self.LIFECYCLE} (socket)", out)
        self.assertIn(f"m00 fresh restore vs baseline: 1 non-archivable special file(s) excluded from the "
                      f"comparison: {self.CODEX}", out)
        self.assertNotIn("difference(s)", out)
        self.assert_live_unchanged()  # strict: the live sockets were never touched
        self.assert_safe_helpers()

    def test_regular_file_difference_still_fails_s3(self):
        restore = self.private  # resolved after the run dir exists

        def extra():
            target = next(restore.glob("run-*")) / "s3" / "restore-test" / "m00" / "state" / "extra.txt"
            target.write_text("not in the baseline\n")
        self.fake.hooks["manifest /private/s3/restore-test/m00"] = extra
        code, out = self.run_swap(self.args())
        self.assertEqual(code, swap.EXIT_ROLLED_BACK, out)
        self.assertIn("FAIL at [S3]: m00 fresh restore vs baseline: 1 difference(s): state/extra.txt", out)
        self.assertFalse((self.run_dir() / "snapshot-receipt.json").exists())
        self.assertNotIn("build flock-ocr:synthetic", lifecycle(self.fake))
        self.assert_live_unchanged()

    def test_special_file_replaced_by_a_regular_file_still_fails(self):
        def swap_type():
            target = next(self.private.glob("run-*")) / "s3" / "restore-test" / "m00" / self.CODEX
            target.write_text("a regular file where the socket was\n")
        self.fake.hooks["manifest /private/s3/restore-test/m00"] = swap_type
        code, out = self.run_swap(self.args())
        self.assertEqual(code, swap.EXIT_ROLLED_BACK, out)
        self.assertIn(f"FAIL at [S3]: m00 fresh restore vs baseline: 1 difference(s): {self.CODEX}", out)
        self.assert_live_unchanged()

    def test_new_socket_from_the_replacement_does_not_hold_a2(self):
        def replacement():
            if not os.path.lexists(self.fake.volume / "state" / "replacement.sock"):
                make_socket(self.fake.volume / "state", "replacement.sock")
        self.fake.hooks["start:" + "9" * 64] = replacement
        code, out = self.run_swap(self.args())
        self.assertEqual(code, 0, out)
        self.assertEqual(json.loads((self.run_dir() / "a2" / "diff.json").read_text()), {})
        self.assertIn("m00: 2 non-archivable special file(s) excluded from the S1 comparison: "
                      f"{self.CODEX}, state/replacement.sock", out)

    def test_rollback_restores_archivable_data_and_names_cleared_sockets(self):
        def replacement():
            (self.fake.volume / "state" / "new.txt").write_text("replacement-only work\n")
        self.fake.hooks["start:" + "9" * 64] = replacement
        self.assertEqual(self.run_swap(self.args())[0], swap.EXIT_HELD)
        self.fake.hooks.clear()
        run = self.run_dir()
        code, out = self.run_swap(["--rollback", str(run)])
        self.assertEqual(code, swap.EXIT_ROLLED_BACK, out)
        self.assertIn("mounts differing from S1: ['m00']", out)
        self.assertIn(f"m00: 1 non-archivable special file(s) were cleared with the mount; S1 does not "
                      f"restore sockets: {self.CODEX} (socket)", out)
        self.assertIn("[RB-C5] validate_data", out)
        self.assertIn("m00: live equals the S1 baseline", out[out.index("[RB-C5]"):])
        receipt = json.loads((run / "rollback" / "m00" / "attempt-001" / "receipt.json").read_text())
        self.assertEqual(receipt["non_archivable"], {self.CODEX: "socket"})
        self.assertFalse((self.fake.volume / "state" / "new.txt").exists())
        self.assertFalse(os.path.lexists(self.fake.volume / self.CODEX))
        self.assert_live_archivable_unchanged()
        self.assertTrue(self.fake.find(TARGET)["State"]["Running"])
        self.assertEqual(self.state()["phase"], "rolled_back")


class C2DefaultsAndReuseImageTests(RunMixin, unittest.TestCase):
    """SWAP-FIX4: the attempt-3 [C2] shape passes; --reuse-image skips [B1]/[B2] only."""
    TAG = "flock-ocr:synthetic"
    DESCRIPTOR = {"mediaType": "application/vnd.oci.image.manifest.v1+json",
                  "platform": {"architecture": "amd64", "os": "linux"}}

    def daemon_like_create(self, extra=None):
        """Saved inspect as an older daemon stored it; create stores API defaults and a new descriptor."""
        target = self.fake.find(TARGET)
        target["HostConfig"]["OomKillDisable"] = None
        target["ImageManifestDescriptor"] = dict(self.DESCRIPTOR, digest=OLD_IMAGE, size=4034)
        original = self.fake.docker_api

        def create(method, path, body):
            status, response = original(method, path, body)
            new = self.fake.find("9" * 64)
            new["HostConfig"]["OomKillDisable"] = False
            new["ImageManifestDescriptor"] = dict(self.DESCRIPTOR, digest=body["Image"], size=4915)
            if extra:
                extra(new)
            return status, response
        self.fake.docker_api = create
        return original

    def test_c2_passes_with_the_oom_kill_default_and_a_new_manifest_descriptor(self):
        self.daemon_like_create()
        code, out = self.run_swap(self.args())
        self.assertEqual(code, 0, out)
        self.assertIn("API default, treated as equal: HostConfig.OomKillDisable: null == false", out)
        self.assertEqual(json.loads((self.run_dir() / "create-diff.json").read_text()), [])
        self.assertEqual(self.state()["phase"], "complete")

    def test_c2_still_rolls_back_on_a_real_change_next_to_the_defaults(self):
        self.daemon_like_create(extra=lambda new: new["HostConfig"].update(CapAdd=["CAP_SYS_ADMIN"]))
        code, out = self.run_swap(self.args())
        self.assertEqual(code, swap.EXIT_ROLLED_BACK, out)
        self.assertIn("FAIL at [C2]: replacement settings differ from the saved ones: ['HostConfig.CapAdd']", out)
        self.assertIn("ROLLBACK RB-B", out)
        self.assertNotIn("start new", lifecycle(self.fake))

    # -- --reuse-image --

    def failed_first_run(self):
        """An attempt that built and verified the tag, then failed at [C2] and rolled back."""
        original = self.daemon_like_create(extra=lambda new: new["HostConfig"].update(Privileged=True))
        code, out = self.run_swap(self.args())
        self.assertEqual(code, swap.EXIT_ROLLED_BACK, out)
        self.assertIn("FAIL at [C2]", out)
        self.fake.docker_api = original
        prev = self.run_dir()
        # Owner cleanup before the next attempt: the never-started replacement is removed.
        failed = self.fake.find(TARGET + "-ocr-failed")
        del self.fake.containers[failed["Id"]]
        return prev

    def new_run(self, prev):
        runs = [p for p in sorted(self.private.glob("run-*")) if p != prev]
        self.assertEqual(len(runs), 1)
        return runs[0]

    def test_reuse_skips_the_rebuild_reruns_v1_and_completes(self):
        prev = self.failed_first_run()
        self.daemon_like_create()
        calls, events = len(self.fake.calls), len(self.fake.events)
        argv = self.args(reuse_image=str(prev))
        del argv[argv.index("--build-python"):argv.index("--build-python") + 2]
        code, out = self.run_swap(argv)
        self.assertEqual(code, 0, out)
        new_calls = self.fake.calls[calls:]
        scripts = [Path(a[2]).name for a in new_calls if a[0] == sys.executable]
        self.assertNotIn("build_wheel.py", scripts)
        self.assertNotIn("preflight.py", scripts)
        self.assertIn("verify.py", scripts)
        self.assertIn([sys.executable, "-B", str(RECIPE / "verify.py"), "--image", NEW_IMAGE], new_calls)
        self.assertFalse(any(a[:2] == ["docker", "build"] for a in new_calls))
        self.assertEqual(self.fake.events[events:], [e for e in RootlessApplyTests.MAIN_EVENTS
                                                     if not e.startswith("build")])
        self.assertIn("not rebuilt: reusing wheel sha256 " + WHEEL_SHA, out)
        self.assertIn(f"not rebuilt: {self.TAG} = {NEW_IMAGE}", out)
        state = json.loads((self.new_run(prev) / "state.json").read_text())
        self.assertEqual(state["reuse"], {"run_dir": str(prev.resolve()), "new_image_id": NEW_IMAGE,
                                          "wheel_sha256": WHEEL_SHA})
        self.assertEqual((state["new_image_id"], state["wheel_sha256"], state["phase"]),
                         (NEW_IMAGE, WHEEL_SHA, "complete"))
        receipt = json.loads((self.new_run(prev) / "snapshot-receipt.json").read_text())
        self.assertEqual(receipt["reused_image"], state["reuse"])
        self.assertEqual(self.fake.find(TARGET)["Image"], NEW_IMAGE)

    def test_reuse_dry_run_checks_the_receipt_and_changes_nothing(self):
        prev = self.failed_first_run()
        calls = len(self.fake.calls)
        code, out = self.run_swap(self.args("--dry-run", reuse_image=str(prev)))
        self.assertEqual(code, 0, out)
        self.assertIn(f"reusing {self.TAG} = {NEW_IMAGE} built and V1-verified by", out)
        self.assertIn("not rebuilt", out)
        self.assertNotRegex(out, r"would run: .*(build_wheel\.py|preflight\.py|docker build)")
        for argv in self.fake.calls[calls:]:
            self.assertIn(argv[0], ("docker", "systemctl"), argv)
            if argv[0] == "docker":
                self.assertIn(argv[1], {"info", "inspect", "ps", "image", "top"}, argv)
            else:
                self.assertEqual(argv[2], "show", argv)
        self.assertEqual(sorted(self.private.glob("run-*")), [prev])

    def test_reuse_refusals_change_nothing(self):
        tag = self.TAG

        def rewrite(prev, **changes):
            state = json.loads((prev / "state.json").read_text())
            state.update(changes)
            (prev / "state.json").write_text(json.dumps(state))

        def outside(case):
            copy_dir = case.tmp / "elsewhere" / case.prev.name
            shutil.copytree(case.prev, copy_dir)
            rewrite(copy_dir, run_dir=str(copy_dir))
            return copy_dir
        cases = {
            "did not pass [V1]": lambda c: rewrite(c.prev, done=["R1", "Q1", "Q2", "S1", "S2", "S3", "G1", "B1"]),
            "not --new-tag": lambda c: rewrite(c.prev, new_tag="flock-ocr:other"),
            "another base image": lambda c: rewrite(c.prev, old_image=OTHER_IMAGE),
            "no valid image ID": lambda c: rewrite(c.prev, new_image_id="<NEW_IMAGE_ID>"),
            "belongs to another run": lambda c: rewrite(c.prev, run_dir=str(c.tmp / "x")),
            "resolves to": lambda c: c.fake.tags.update({tag: OTHER_IMAGE}),
            "do not extend": lambda c: c.fake.images[NEW_IMAGE]["RootFS"].update(Layers=["sha256:" + "0c" * 32]),
            "cannot read": lambda c: (c.prev / "state.json").unlink(),
            "directly under --private": outside,
        }
        for needle, change in cases.items():
            with self.subTest(case=needle):
                case_dir = self.tmp / re.sub(r"\W+", "_", needle)
                self.fake = FakeSystem(case_dir)
                self.private = case_dir / "private"
                prev = self.failed_first_run()
                reuse = change(type("Case", (), {"prev": prev, "fake": self.fake, "tmp": case_dir})) or prev
                events, helpers = len(self.fake.events), len(self.fake.helper_runs)
                code, out = self.run_swap(self.args(reuse_image=str(reuse)))
                self.assertEqual(code, swap.EXIT_REFUSED, out)
                self.assertIn("FAIL at [R1]", out)
                self.assertIn(needle, out)
                self.assertEqual(self.fake.events[events:], [])
                self.assertEqual(len(self.fake.helper_runs), helpers)

    def test_tag_moved_after_r1_fails_b2_before_any_container_change(self):
        prev = self.failed_first_run()
        api = len(self.fake.api_calls)
        self.fake.hooks["sqlite-evidence"] = lambda: self.fake.tags.update({self.TAG: OTHER_IMAGE})
        code, out = self.run_swap(self.args(reuse_image=str(prev)))
        self.assertEqual(code, swap.EXIT_ROLLED_BACK, out)
        self.assertIn(f"FAIL at [B2]: {self.TAG} now resolves to {OTHER_IMAGE}", out)
        self.assertIn("ROLLBACK RB-A", out)
        self.assertEqual(len(self.fake.api_calls), api)
        self.assertNotIn("verify.py", " ".join(" ".join(a) for a in self.fake.calls[-20:]))

    def test_reuse_arguments(self):
        base = ["--apply", "--container", TARGET, "--pause-unit", UNIT, "--private", "/p", "--new-tag", self.TAG]
        self.assertEqual(swap.parse_args(base + ["--reuse-image", "/p/run-1"]).reuse_image, "/p/run-1")
        with contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit):
            swap.parse_args(base)
        self.assertIn("--build-python", err.getvalue())
        with contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit):
            swap.parse_args(["--resume", "/p/run-1", "--reuse-image", "/p/run-0"])
        self.assertIn("--reuse-image is fixed at --apply", err.getvalue())


if __name__ == "__main__":
    unittest.main()
