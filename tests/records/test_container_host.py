"""Synthetic profiles, real private filesystem/client tests, injected Docker only."""
from contextlib import contextmanager
import copy
import hashlib
import io
import itertools
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

from campaign_tool.records import container_host as host


def sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


def worker(status="completed", quiescent=True):
    return host.encoded({"schema":1,"status":status,"quiescent":quiescent,
                         "worker_exit":0 if status == "completed" else -15,"escalated":False})


class FakeContainment:
    def __init__(self):
        self.populated = False
        self.denied = False
        self.captured = []
        self.checked = []

    def capture(self, cid, pid):
        if self.denied:
            raise host.Rejected()
        self.captured.append((cid,pid))
        return {"path":"/synthetic/docker-" + cid + ".scope","device":1,"inode":pid,"boot":"synthetic-boot"}

    def empty(self, cid, proof):
        self.checked.append((cid,proof))
        if self.denied:
            raise host.Rejected()
        assert proof["path"].endswith(cid + ".scope")
        return not self.populated


class FakeDocker:
    def __init__(self, profile_path, *, launch=None, report=None):
        self.path = profile_path
        self.value = json.loads(profile_path.read_text())
        self.launch = (0,worker()) if launch is None else launch
        self.report = host._output("completed",quiescent=True,healthy=True,cards=5) if report is None else report
        if isinstance(self.report,dict):
            self.report = dict(self.report,board="ok" if self.report["status"] == "completed" else "failed",
                               health="ok" if self.report["healthy"] else "failed")
        self.calls, self.containers = [], {}
        self.containment = FakeContainment()
        self.dirty = False
        self.commit = self.value["release_commit"]
        self.image = self.value["image_id"]
        self.wrong_label = False
        self.probe_bad = False
        self.stop_resistant = False
        self.kill_resistant = False
        self.daemon_down = False
        self.rootless = True
        self.volume_missing = False
        self.target_running = True
        self.target_wrong_id = False
        self.target_reads = 0
        self.target_fail_after = None
        self.policy_bad = False
        self.create_failure = False
        self.remove_failure = False
        self.wait_hook = None
        self.create_hook = None
        self.start_hook = None
        self.admit_hook = None

    def invoke(self, argv, timeout):
        self.calls.append((argv,timeout))
        if self.daemon_down:
            raise host.TransportError()
        receipts = Path(self.value["host_receipts"])
        active = json.loads((receipts / "active.json").read_text())
        current = json.loads((receipts / active["job_id"] / "receipt.json").read_text())
        for path in (receipts / "active.json", receipts / active["job_id"] / "receipt.json"):
            assert path.stat().st_mode & 0o777 == 0o600
        action = argv[1]
        if action == "info":
            return 0,host.encoded({"security":["name=rootless"] if self.rootless else [],"cgroup":"2"})
        if action == "volume":
            return (1,b"") if self.volume_missing else (0,argv[-1].encode())
        if action == "create":
            assert current["hold"]
            if self.create_failure:
                raise host.TransportError()
            role = argv[argv.index("--role") + 1]
            if role == "reporter":
                assert current["quiescent"] and current["worker_removed"]
            cid = sha(active["job_id"] + role)
            labels = dict(argv[index + 1].split("=",1) for index,x in enumerate(argv) if x == "--label")
            self.containers[cid] = dict(role=role,state="created",running=False,pid=0,exit=0,labels=labels,admitted=False)
            if self.create_hook:
                self.create_hook()
            return 0,(cid + "\n").encode()
        cid = argv[-1] if action in {"inspect","start","wait","logs","stop","kill","rm"} else argv[argv.index("--workdir") + 2]
        if action == "inspect" and cid == self.value["network"]["container_id"]:
            self.target_reads += 1
            running = self.target_running and (self.target_fail_after is None or self.target_reads <= self.target_fail_after)
            return 0,host.encoded({"id":sha("wrong-target") if self.target_wrong_id else cid,"running":running})
        item = self.containers[cid]
        if action == "inspect":
            labels = dict(item["labels"])
            if self.wrong_label:
                labels["campaign-tool.job"] = "stale-job"
            return 0,host.encoded({"id":cid,"image":self.image,"state":item["state"],"running":item["running"],
                                   "pid":item["pid"],"exit":item["exit"],"labels":labels,
                                   "network":"bridge" if self.policy_bad else ("container:" + self.value["network"]["container_id"]
                                       if item["role"] == "worker" and self.value["network"]["mode"] == "container" else "none"),
                                   "pid_mode":"","init":True,"restart":"no"})
        if action == "start":
            assert current["container_id" if item["role"] == "worker" else "reporter_id"] == cid
            item.update(state="running",running=True,pid=100 + len(self.containers))
            if self.start_hook:
                self.start_hook()
            return 0,(cid + "\n").encode()
        if action == "exec":
            assert cid in argv and item["running"]
            if "version" in argv:
                return 0,host.encoded({"source_dirty":self.dirty,"commit":self.commit,
                        "package_version":self.value["package_version"],"dependency_lock_sha256":self.value["dependency_lock_sha256"],
                        "ledger_schema_version":1,"installation_kind":"installed_wheel"})
            if "probe" in argv:
                return 0,host.encoded({"schema":1,"status":"runtime_ready","uid":0 if self.probe_bad else 1000})
            if "admit" in argv:
                assert current[item["role"] + "_cgroup"] is not None
                item["admitted"] = True
                if self.admit_hook:
                    self.admit_hook()
                return 0,host.encoded({"schema":1,"status":"admitted"})
            if "cancel" in argv:
                # Deliberately NO supervisor ACK. Runtime containment must suffice.
                return 30,worker("invalid_state",False)
        if action == "wait":
            if self.wait_hook and item["role"] == "worker":
                self.wait_hook()
            if item["role"] == "worker" and isinstance(self.launch,BaseException):
                raise self.launch
            if item["role"] == "reporter" and isinstance(self.report,BaseException):
                raise self.report
            exit_code = self.launch[0] if item["role"] == "worker" else 0
            item.update(state="exited",running=False,pid=0,exit=exit_code)
            return 0,str(exit_code).encode()
        if action == "logs":
            return 0,self.launch[1] if item["role"] == "worker" else host.encoded(self.report)
        if action == "stop":
            if self.stop_resistant:
                return 1,b""
            item.update(state="exited",running=False,pid=0,exit=143)
            return 0,b""
        if action == "kill":
            if not self.kill_resistant:
                item.update(state="exited",running=False,pid=0,exit=137)
            return 0,b""
        if action == "rm":
            assert not item["running"]
            assert not self.containment.populated
            if self.remove_failure:
                raise host.TransportError()
            del self.containers[cid]
            return 0,(cid + "\n").encode()
        raise AssertionError("Unexpected synthetic Docker action")


class HostFixture(unittest.TestCase):
    def setUp(self):
        parent = os.environ.get("TMPDIR",str(Path.home()))
        if parent in ("/tmp","/var/tmp"):
            parent = str(Path.home())
        self.temp = tempfile.TemporaryDirectory(prefix="container-host-synthetic-",dir=parent)
        self.root = Path(self.temp.name)
        self.receipts = self.root / "host-receipts"
        self.receipts.mkdir(mode=0o700)
        self.profile = self.root / "profile.json"
        security = self.root / "seccomp.json"
        security.write_bytes(b'{"synthetic":true}')
        security.chmod(0o600)
        value = {"schema":1,"name_prefix":"synthetic-worker","image_id":"sha256:" + sha("synthetic-image"),
                 "release_commit":hashlib.sha1(b"synthetic-release").hexdigest(),"package_version":"0.0.synthetic",
                 "dependency_lock_sha256":sha("synthetic-lock"),"docker_path":str(self.root / "fake-docker"),
                 "python":"/synthetic/python","release_root":"/synthetic/release","parser_path":"/synthetic/parser",
                 "tmp_dir":"/synthetic/tmp","root":"/synthetic/records","mail_config":"/synthetic/mail.json",
                 "worker_state_parent":"/synthetic/lifecycle","board_dir":"/synthetic/board",
                 "host_receipts":str(self.receipts),"wall_seconds":2700,"owner_launch_approved":True,
                 "seccomp_path":str(security),"seccomp_sha256":host.digest(security.read_bytes()),
                 "memory_bytes":268435456,"pids_limit":64,"cpus":1,"network":{"mode":"none","container_id":None},
                 "mounts":[{"kind":"volume","source":"synthetic-" + name,"target":"/synthetic/" + name,
                            "readonly":name in {"release","parser","mail.json"}}
                           for name in ("release","parser","records","tmp","lifecycle","board","mail.json")]}
        self.write_profile(value)

    def tearDown(self):
        self.temp.cleanup()

    def write_profile(self,value):
        self.profile.write_bytes(host.encoded(value))
        self.profile.chmod(0o600)

    def current(self):
        receipts = Path(json.loads(self.profile.read_text())["host_receipts"])
        active = json.loads((receipts / "active.json").read_text())
        return json.loads((receipts / active["job_id"] / "receipt.json").read_text())

    def fake(self,**kwargs):
        return FakeDocker(self.profile,**kwargs)

    def run_host(self,fake):
        return host.run(self.profile,invoke=fake.invoke,containment=fake.containment)

    def stop_host(self,fake):
        return host.stop(self.profile,invoke=fake.invoke,containment=fake.containment)

    def launch_hold(self):
        fake = self.fake(launch=host.TransportError())
        self.assertEqual(self.run_host(fake)["status"],"held")
        self.assertTrue(self.current()["hold"])
        return fake

    def mutate_journal(self,**changes):
        with host.Profile(self.profile) as profile:
            journal = host.Journal(profile)
            try:
                journal.change(self.current()["job_id"],**changes)
            finally:
                journal.close()


class ContainerHostTests(HostFixture):
    def test_atomic_replace_cleanup_and_fresh_no_overwrite(self):
        with host.PrivateDir(self.receipts) as directory:
            directory.write("synthetic.json",b"first",fresh=True)
            directory.write("synthetic.json",b"second")
            self.assertEqual(directory.read("synthetic.json")[0],b"second")
            with self.assertRaises(FileExistsError):
                directory.write("synthetic.json",b"third",fresh=True)
            self.assertEqual(directory.read("synthetic.json")[0],b"second")
        self.assertFalse(any(p.name.startswith(".atomic-") for p in self.receipts.iterdir()))

    def test_completed_reconciled_removed_and_private_receipts(self):
        fake = self.fake()
        self.assertEqual(self.run_host(fake),host._output("completed",quiescent=True,healthy=True,cards=5))
        current = self.current()
        self.assertEqual(current["ack"],"natural")
        self.assertTrue(current["worker_removed"] and current["reporter_removed"])
        self.assertFalse(current["hold"])
        self.assertEqual(fake.containers,{})
        self.assertEqual(len(fake.containment.checked),2)

    def test_no_persistent_exec_supervisor_and_security_flags(self):
        fake = self.fake()
        self.run_host(fake)
        creates = [argv for argv,_ in fake.calls if argv[1] == "create"]
        self.assertEqual(len(creates),2)
        for argv in creates:
            for flag in ("--init","--restart=no","--read-only","--no-healthcheck"):
                self.assertIn(flag,argv)
            for flag,value in (("--network","none"),("--cgroupns","private"),("--pull","never"),("--user","1000:1000"),
                               ("--cap-drop","ALL"),("--entrypoint","/usr/bin/env")):
                self.assertEqual(argv[argv.index(flag)+1],value)
            self.assertIn("no-new-privileges=true",argv)
            self.assertIn("MODEL_BASE_URL=",argv)
            self.assertIn("CHALLENGE_MODEL_BASE_URL=",argv)
            self.assertIn("-i",argv)
            self.assertNotIn("--rm",argv)
        self.assertFalse(any("supervise" in argv and argv[1] == "exec" for argv,_ in fake.calls))
        self.assertFalse(any("restart" == x or "--privileged" == x or "sh" == x for argv,_ in fake.calls for x in argv))
        self.assertTrue(all(timeout <= 2765 for _,timeout in fake.calls))

    def test_host_client_loss_independent_stop_without_supervisor_ack(self):
        fake = self.launch_hold()
        self.assertTrue(self.stop_host(fake)["quiescent"])
        self.assertEqual(self.current()["ack"],"contained")
        self.assertFalse(self.current()["hold"])
        self.assertTrue(any("cancel" in argv for argv,_ in fake.calls))
        self.assertEqual(fake.containers,{})

    def test_term_resistant_exact_cid_kill_escalation(self):
        fake = self.launch_hold()
        cid = self.current()["container_id"]
        fake.stop_resistant = True
        self.assertTrue(self.stop_host(fake)["quiescent"])
        self.assertEqual([argv[-1] for argv,_ in fake.calls if argv[1] == "kill"],[cid])

    def test_supervisor_crash_requires_runtime_containment(self):
        fake = self.fake(launch=(137,b"PRIVATE_WORKER_STDOUT"))
        self.assertEqual(self.run_host(fake)["status"],"held")
        self.assertFalse(self.current()["quiescent"])
        self.assertTrue(self.stop_host(fake)["quiescent"])
        self.assertFalse(self.current()["hold"])

    def test_completed_without_quiescence_rejected(self):
        fake = self.fake(launch=(0,worker("completed",False)))
        self.assertEqual(self.run_host(fake)["status"],"held")
        self.assertFalse(self.current()["quiescent"])
        self.assertFalse(any("reporter" in argv for argv,_ in fake.calls))

    def test_populated_cgroup_never_removed_or_admitted_next(self):
        fake = self.launch_hold()
        fake.containment.populated = True
        self.assertFalse(self.stop_host(fake)["quiescent"])
        self.assertTrue(self.current()["hold"])
        self.assertFalse(any(argv[1] == "rm" for argv,_ in fake.calls))
        with self.assertRaises(host.Rejected):
            self.run_host(fake)

    def test_unavailable_containment_blocks_worker_admission(self):
        fake = self.fake()
        fake.containment.denied = True
        self.assertEqual(self.run_host(fake)["status"],"held")
        self.assertFalse(any("admit" in argv for argv,_ in fake.calls))
        self.assertFalse(self.stop_host(fake)["quiescent"])

    def test_crash_before_capture_recovers_running_identity(self):
        fake = self.launch_hold()
        self.mutate_journal(worker_cgroup=None)
        self.assertTrue(self.stop_host(fake)["quiescent"])
        self.assertEqual(len(fake.containment.captured),3)

    def test_missing_terminal_cgroup_evidence_holds(self):
        fake = self.fake(launch=(137,b""))
        self.run_host(fake)
        self.mutate_journal(worker_cgroup=None)
        self.assertFalse(self.stop_host(fake)["quiescent"])
        self.assertTrue(self.current()["hold"])

    def test_daemon_unavailable_and_remove_ambiguity_holds(self):
        fake = self.launch_hold()
        fake.daemon_down = True
        self.assertEqual(self.stop_host(fake)["status"],"held")
        fake.daemon_down = False
        fake.remove_failure = True
        self.assertEqual(self.stop_host(fake)["status"],"held")
        self.assertTrue(self.current()["hold"])

    def test_ambiguous_create_no_cid_cannot_clear_hold(self):
        fake = self.fake()
        fake.create_failure = True
        self.assertEqual(self.run_host(fake)["status"],"held")
        self.assertIsNone(self.current()["container_id"])
        self.assertEqual(self.stop_host(fake)["status"],"held")
        self.assertFalse(any(argv[1] == "start" for argv,_ in fake.calls))

    def test_cancel_during_create_fences_start_and_removes_created_cid(self):
        fake = self.fake()
        fake.create_hook = lambda: self.mutate_journal(cancel_requested=True)
        self.assertEqual(self.run_host(fake)["status"],"held")
        self.assertFalse(any(argv[1] == "start" for argv,_ in fake.calls))
        fake.create_hook = None
        self.assertTrue(self.stop_host(fake)["quiescent"])
        self.assertFalse(self.current()["hold"])

    def test_cancel_during_start_fences_admit(self):
        fake = self.fake()
        fake.start_hook = lambda: self.mutate_journal(cancel_requested=True)
        self.assertEqual(self.run_host(fake)["status"],"held")
        self.assertFalse(any("admit" in argv for argv,_ in fake.calls))
        fake.start_hook = None
        self.assertTrue(self.stop_host(fake)["quiescent"])

    def test_concurrent_stop_finalization_not_overwritten_by_run(self):
        fake = self.fake()
        fake.wait_hook = lambda: self.stop_host(fake)
        self.run_host(fake)
        self.assertEqual(self.current()["phase"],"finished")
        self.assertFalse(self.current()["hold"])
        frozen = self.current()
        with host.Profile(self.profile) as profile:
            journal = host.Journal(profile)
            try:
                host._hold(journal,frozen["job_id"],"transport")
            finally:
                journal.close()
        self.assertEqual(self.current(),frozen)

    def test_stale_labels_image_and_dirty_version_block_admission(self):
        for field,value in (("wrong_label",True),("image","sha256:" + sha("wrong-image")),("dirty",True),("commit","0" * 40),("probe_bad",True)):
            receipts = self.root / ("receipts-" + field)
            receipts.mkdir(mode=0o700)
            profile = json.loads(self.profile.read_text())
            profile["host_receipts"] = str(receipts)
            self.write_profile(profile)
            fake = self.fake()
            setattr(fake,field,value)
            self.assertEqual(self.run_host(fake)["status"],"held")
            self.assertFalse(any("admit" in argv for argv,_ in fake.calls))

    def test_board_or_health_failure_is_not_healthy(self):
        fake = self.fake(report=host._output("postrun_failed",quiescent=True,cards=5))
        self.assertEqual(self.run_host(fake)["status"],"postrun_failed")
        self.assertFalse(self.current()["healthy"])
        self.assertFalse(self.current()["hold"])

    def test_reporter_controller_loss_independently_contained(self):
        fake = self.fake(report=host.TransportError())
        self.assertEqual(self.run_host(fake)["status"],"held")
        self.assertTrue(self.current()["worker_removed"])
        self.assertIsNotNone(self.current()["reporter_id"])
        self.assertTrue(self.stop_host(fake)["quiescent"])
        self.assertFalse(self.current()["healthy"])
        self.assertFalse(self.current()["hold"])
        self.assertEqual(fake.containers,{})

    def test_serialization_no_overwrite_and_no_stale_stop(self):
        fake = self.fake()
        with host.PrivateDir(self.receipts) as directory, host.locked(directory,"launch.lock"):
            with self.assertRaises(host.Rejected):
                self.run_host(fake)
        self.run_host(fake)
        first = self.current()
        raw = (self.receipts / first["job_id"] / "receipt.json").read_bytes()
        with self.assertRaises(host.Rejected):
            self.stop_host(fake)
        self.run_host(fake)
        self.assertNotEqual(self.current()["job_id"],first["job_id"])
        self.assertEqual((self.receipts / first["job_id"] / "receipt.json").read_bytes(),raw)

    def test_profile_drift_modes_symlink_hardlink_fifo(self):
        fake = self.launch_hold()
        original = json.loads(self.profile.read_text())
        self.write_profile(dict(original,package_version="changed"))
        with self.assertRaises(host.Rejected):
            self.stop_host(fake)
        self.write_profile(original)
        with host.Profile(self.profile) as profile:
            new = self.root / "replacement"
            new.write_bytes(self.profile.read_bytes())
            new.chmod(0o600)
            new.replace(self.profile)
            with self.assertRaises(host.Rejected):
                profile.check()
        for kind in ("symlink","hardlink","fifo"):
            path = self.root / kind
            if kind == "symlink": path.symlink_to(self.profile)
            elif kind == "hardlink": os.link(self.profile,path)
            else: os.mkfifo(path,0o600)
            with self.assertRaises((host.Rejected,OSError)): host.Profile(path)
            path.unlink()
        self.profile.chmod(0o644)
        with self.assertRaises(host.Rejected): host.Profile(self.profile)

    def test_profile_caps_mounts_and_seccomp_fail_closed(self):
        original = json.loads(self.profile.read_text())
        changes = ({"wall_seconds":2701},{"wall_seconds":True},{"wall_seconds":0},{"schema":1.0},{"cpus":True},
                   {"pids_limit":0},{"memory_bytes":1},{"owner_launch_approved":1},{"uid":0},{"root":"/a/../b"},
                   {"mounts":[]},{"mounts":[{"kind":"bind","source":str(self.root),"target":"/synthetic","readonly":False}]})
        for change in changes:
            self.write_profile(dict(original,**change))
            with self.assertRaises(host.Rejected): host.Profile(self.profile)
        self.write_profile(dict(original,wall_seconds=.5))
        with host.Profile(self.profile) as profile: self.assertEqual(profile.value["wall_seconds"],.5)
        self.write_profile(dict(original,seccomp_sha256=sha("wrong-security")))
        fake = self.fake()
        self.assertEqual(self.run_host(fake)["status"],"held")
        self.assertEqual(fake.calls,[])

    def test_no_launch_without_owner_approval(self):
        value = json.loads(self.profile.read_text())
        self.write_profile(dict(value,owner_launch_approved=False))
        fake = self.fake()
        with self.assertRaises(host.Rejected): self.run_host(fake)
        self.assertEqual(fake.calls,[])

    def test_trusted_parents_symlinks_and_malformed_pointer(self):
        writable = self.root / "writable"
        writable.mkdir(mode=0o700)
        writable.chmod(0o777)
        with self.assertRaises(host.Rejected): host.PrivateDir(writable)
        link = self.root / "link"
        link.symlink_to(self.receipts,target_is_directory=True)
        with self.assertRaises(OSError): host.PrivateDir(link)
        (self.receipts / "active.json").write_bytes(b'{"schema":1,"schema":1}')
        (self.receipts / "active.json").chmod(0o600)
        fake = self.fake()
        with self.assertRaises(host.Rejected): self.run_host(fake)
        self.assertEqual(fake.calls,[])

    def test_cli_never_prints_arguments_or_exception_text(self):
        with mock.patch("sys.stdout",new_callable=io.StringIO) as output:
            self.assertEqual(host.main(["--PRIVATE_ARGUMENT_SENTINEL"]),30)
        self.assertEqual(json.loads(output.getvalue()),host._output("invalid_state"))
        self.assertNotIn("PRIVATE_ARGUMENT_SENTINEL",output.getvalue())

    def test_real_owned_client_timeout_and_output_cap(self):
        with self.assertRaises(host.TransportError):
            host.call_argv([sys.executable,"-c","import time; time.sleep(10)"],.1)
        with self.assertRaises(host.TransportError):
            host.call_argv([sys.executable,"-c","import sys; sys.stdout.buffer.write(b'x'*1048576)"],2)
        code,raw = host.call_argv([sys.executable,"-c","print('synthetic')"],2)
        self.assertEqual((code,raw),(0,b"synthetic\n"))

    def test_terminal_runtime_without_empty_group_rejected(self):
        fake = self.fake()
        self.run_host(fake)
        self.assertEqual(len(fake.containment.captured),2)
        self.assertEqual(len(fake.containment.checked),2)
        for cid,proof in fake.containment.checked:
            self.assertIn(cid,proof["path"])

    def test_native_namespace_uid_access_is_required(self):
        with mock.patch.object(host.os,"geteuid",return_value=0), self.assertRaises(host.Rejected):
            host.probe({})

    def test_cgroup_proof_wrong_boot_cid_or_type_rejected(self):
        proof = {"path":"/synthetic/docker-" + sha("expected") + ".scope","device":1,"inode":1,"boot":"old-boot"}
        with self.assertRaises(host.Rejected): host.Containment().empty(sha("different"),proof)
        with self.assertRaises(host.Rejected): host.Containment().empty(sha("expected"),proof)
        proof["path"] = "/../../synthetic"
        with self.assertRaises(host.Rejected): host.Containment().empty(sha("expected"),proof)

    def test_entry_exec_replaces_init_child_and_fixed_worker_flags(self):
        job_id = sha("entry")[:32]
        identity = {"schema":1,"job_id":job_id,"role":"worker"}
        gate = mock.MagicMock()
        gate.__enter__.return_value = gate
        gate.read.return_value = (host.encoded(identity),None)
        parent = mock.MagicMock()
        parent.__enter__.return_value = parent
        parent.child.return_value = gate
        paths = dict(role="worker",job_id=job_id,wall_seconds=2700,python="/synthetic/python",
                     root="/synthetic/records",mail_config="/synthetic/mail.json",worker_state_parent="/synthetic/lifecycle")
        with mock.patch.object(host.os,"getuid",return_value=1000), mock.patch.object(host.os,"geteuid",return_value=1000), \
             mock.patch.object(host,"PrivateDir",return_value=parent), mock.patch.object(host.os,"execve") as execute:
            with self.assertRaises(host.Rejected): host.entry(paths)
        argv = execute.call_args.args[1]
        self.assertEqual(argv[3:5],[host.WORKER_MODULE,"supervise"])
        for flag in ("--unattended","--ocr","--json"): self.assertIn(flag,argv)
        self.assertEqual(argv[argv.index("--max-originals-per-run") + 1],"200")
        self.assertEqual(argv[argv.index("--state-dir") + 1],"/synthetic/lifecycle/" + job_id)

    def test_report_entry_health_failure_and_no_raw_output(self):
        paths = dict(root="/synthetic/root",board_dir="/synthetic/board",job_id=sha("report")[:32],python="/synthetic/python")
        parent = mock.MagicMock()
        parent.__enter__.return_value = parent
        parent.child.return_value.__enter__.return_value = parent
        with mock.patch.object(host,"PrivateDir",return_value=parent), \
             mock.patch.object(host,"build_board",return_value={"cards":5}), \
             mock.patch.object(host,"call_argv",return_value=(1,host.encoded({"status":"degraded","exit_code":1,"private":"RAW_BODY_SENTINEL"}))):
            result = host.postrun_entry(paths)
        self.assertEqual(result,dict(host._output("postrun_failed",quiescent=True,cards=5),board="ok",health="degraded"))
        health = json.loads(parent.write.call_args.args[1])
        self.assertEqual(health["status"],"degraded")
        self.assertFalse(health["healthy"])
        self.assertNotIn("RAW_BODY_SENTINEL",json.dumps(health))
        self.assertNotIn("RAW_BODY_SENTINEL",json.dumps(result))



    def test_rootful_daemon_and_missing_volume_refused(self):
        for field in ("rootless","volume_missing"):
            receipts = self.root / ("receipts-" + field)
            receipts.mkdir(mode=0o700)
            value = json.loads(self.profile.read_text())
            self.write_profile(dict(value,host_receipts=str(receipts)))
            fake = self.fake()
            setattr(fake,field,False if field == "rootless" else True)
            self.assertEqual(self.run_host(fake)["status"],"held")
            self.assertFalse(any(argv[1] == "create" for argv,_ in fake.calls))

    def test_volume_nocopy_and_no_implicit_volume_provision(self):
        fake = self.fake()
        self.run_host(fake)
        for argv,_ in fake.calls:
            if argv[1] == "create":
                mounts = [argv[i+1] for i,x in enumerate(argv) if x == "--mount"]
                self.assertTrue(all("volume-nocopy" in mount for mount in mounts))
        self.assertEqual(sum(argv[1] == "volume" for argv,_ in fake.calls),14)

    def test_stop_kill_resistant_runtime_retains_hold_bounded(self):
        fake = self.launch_hold()
        fake.stop_resistant = fake.kill_resistant = True
        ticks = itertools.count(step=10)
        with mock.patch.object(host.time,"monotonic",side_effect=lambda:next(ticks)), mock.patch.object(host.time,"sleep"):
            self.assertFalse(self.stop_host(fake)["quiescent"])
        self.assertTrue(self.current()["hold"])
        self.assertFalse(any(argv[1] == "rm" for argv,_ in fake.calls))

    def test_delayed_start_response_loss_can_recover_without_admission(self):
        fake = self.fake()
        def lost():
            fake.start_hook = None  # One delayed worker start, not a second reporter fault.
            raise host.TransportError()
        fake.start_hook = lost
        self.assertEqual(self.run_host(fake)["status"],"held")
        self.assertFalse(any("admit" in argv for argv,_ in fake.calls))
        self.assertTrue(self.stop_host(fake)["quiescent"])

    def test_cgroup_observation_populated_removed_and_inode_drift(self):
        cid = sha("observed-cgroup")
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        proof = {"path":"/synthetic/docker-"+cid+".scope","device":1,"inode":2,"boot":boot}
        st = mock.Mock(st_dev=1,st_ino=2)
        with mock.patch.object(host,"trusted_path",return_value=st), mock.patch.object(host.Containment,"events",return_value=0):
            self.assertTrue(host.Containment().empty(cid,proof))
        with mock.patch.object(host,"trusted_path",return_value=st), mock.patch.object(host.Containment,"events",return_value=1):
            self.assertFalse(host.Containment().empty(cid,proof))
        with mock.patch.object(host,"trusted_path",side_effect=[FileNotFoundError(),st]):
            self.assertTrue(host.Containment().empty(cid,proof))
        with mock.patch.object(host,"trusted_path",return_value=mock.Mock(st_dev=1,st_ino=3)), self.assertRaises(host.Rejected):
            host.Containment().empty(cid,proof)

    def test_real_cgroup_events_parser_caps_and_missing_populated(self):
        group = self.root / "cgroup"
        group.mkdir(mode=0o700)
        (group / "cgroup.events").write_text("populated 0\nfrozen 0\n")
        self.assertEqual(host.Containment.events(str(group)),0)
        for raw in ("populated 1\n","frozen 0\n","populated 2\n","x" * 4097):
            (group / "cgroup.events").write_text(raw)
            if raw.startswith("populated 1"):
                self.assertEqual(host.Containment.events(str(group)),1)
            else:
                with self.assertRaises(host.Rejected): host.Containment.events(str(group))

    def test_reporter_bad_schema_or_forged_healthy_holds(self):
        fake = self.fake()
        fake.report["schema"] = True
        self.assertEqual(self.run_host(fake)["status"],"held")
        self.assertTrue(self.current()["hold"])
        self.assertTrue(self.stop_host(fake)["quiescent"])

    def test_standalone_entry_report_exit_not_host_health_success(self):
        value = dict(host._output("postrun_failed",quiescent=True),board="failed",health="not_run")
        with mock.patch.object(host,"entry",return_value=value), mock.patch("sys.stdout",new_callable=io.StringIO) as output:
            code = host.main(["entry","--role","reporter","--job-id",sha("entry")[:32],"--worker-state-parent","/synthetic/state",
                              "--root","/synthetic/root","--mail-config","/synthetic/mail","--board-dir","/synthetic/board",
                              "--python","/synthetic/python","--wall-seconds","2700"])
        self.assertEqual(code,0)
        self.assertFalse(json.loads(output.getvalue())["healthy"])



    def set_container_network(self):
        value = json.loads(self.profile.read_text())
        value["network"] = {"mode":"container","container_id":sha("authorized-network-container")}
        self.write_profile(value)
        return value["network"]["container_id"]

    def test_pinned_existing_network_worker_only_no_bridge_fallback(self):
        target = self.set_container_network()
        fake = self.fake()
        self.assertEqual(self.run_host(fake)["status"],"completed")
        creates = [argv for argv,_ in fake.calls if argv[1] == "create"]
        self.assertEqual(creates[0][creates[0].index("--network")+1],"container:"+target)
        self.assertEqual(creates[1][creates[1].index("--network")+1],"none")
        self.assertEqual(fake.target_reads,3)
        self.assertFalse(any(argv[1] in {"stop","kill","rm","start"} and argv[-1] == target for argv,_ in fake.calls))

    def test_network_target_stopped_wrong_or_disappearing_fails_closed(self):
        self.set_container_network()
        for field,value in (("target_running",False),("target_wrong_id",True),("target_fail_after",1),("target_fail_after",2)):
            receipts = self.root / ("net-receipts-" + field + str(value))
            receipts.mkdir(mode=0o700)
            profile = json.loads(self.profile.read_text())
            self.write_profile(dict(profile,host_receipts=str(receipts)))
            fake = self.fake()
            setattr(fake,field,value)
            self.assertEqual(self.run_host(fake)["status"],"held")
            self.assertFalse(any("admit" in argv for argv,_ in fake.calls))
            self.assertTrue(self.current()["hold"])
            for argv,_ in fake.calls:
                if argv[1] == "create":
                    self.assertTrue(argv[argv.index("--network")+1].startswith("container:"))

    def test_network_profile_forbids_bridge_host_names_and_unpinned_ids(self):
        original = json.loads(self.profile.read_text())
        for network in ({"mode":"bridge","container_id":None},{"mode":"host","container_id":None},
                        {"mode":"container","container_id":"friendly-name"},{"mode":"container","container_id":None},
                        {"mode":"none","container_id":sha("extraneous")}):
            self.write_profile(dict(original,network=network))
            with self.assertRaises(host.Rejected): host.Profile(self.profile)

    def test_runtime_network_policy_drift_prevents_admission(self):
        fake = self.fake()
        fake.policy_bad = True
        self.assertEqual(self.run_host(fake)["status"],"held")
        self.assertFalse(any("admit" in argv for argv,_ in fake.calls))



    def test_minimal_volume_directory_and_file_subpaths_argv(self):
        value = json.loads(self.profile.read_text())
        for mount in value["mounts"]:
            mount["subpath"] = "synthetic/private-config.json" if mount["target"].endswith("mail.json") else "synthetic/" + mount["source"]
        self.write_profile(value)
        fake = self.fake()
        self.assertEqual(self.run_host(fake)["status"],"completed")
        for argv,_ in fake.calls:
            if argv[1] == "create":
                mounts = [argv[i+1] for i,x in enumerate(argv) if x == "--mount"]
                self.assertTrue(all("volume-subpath=synthetic/" in x and "volume-nocopy" in x for x in mounts))
                mail = next(x for x in mounts if "dst=/synthetic/mail.json" in x)
                self.assertIn("readonly",mail)
                self.assertIn("volume-subpath=synthetic/private-config.json",mail)

    def test_subpath_traversal_absolute_csv_and_control_rejected(self):
        original = json.loads(self.profile.read_text())
        for subpath in ("../escape","/absolute","a//b","a/./b","a,b","a\nsecret",""):
            value = copy.deepcopy(original)
            value["mounts"][0]["subpath"] = subpath
            self.write_profile(value)
            with self.assertRaises(host.Rejected): host.Profile(self.profile)

    def test_trusted_nonprivate_mail_parent_and_root_parent_supported(self):
        with host.PrivateDir("/",private=False) as directory:
            directory.check()
        parent = self.root / "nonprivate-parent"
        parent.mkdir(mode=0o755)
        parent.chmod(0o755)  # Defeat the deliberately private process umask for this negative fixture.
        file = parent / "single-file.json"
        file.write_bytes(b"SYNTHETIC_NOT_READ")
        file.chmod(0o600)
        with host.PrivateDir(parent,private=False) as directory:
            fd = directory.open_file(file.name)
            os.close(fd)
        with self.assertRaises(host.Rejected): host.PrivateDir(parent)



class BoardTests(HostFixture):
    def synthetic_ledger(self):
        root = self.root / "records"
        output = self.root / "board"
        root.mkdir(mode=0o700)
        output.mkdir(mode=0o700)
        path = root / "ledger.sqlite"
        con = sqlite3.connect(path)
        path.chmod(0o600)
        con.row_factory = sqlite3.Row
        con.executescript("""
CREATE TABLE originals(sha256 TEXT,bytes INT,mime_detected TEXT,first_seen_at TEXT,role TEXT,scope TEXT,preservation_status TEXT,legacy_format TEXT);
CREATE TABLE stage_state(original_sha256 TEXT,stage TEXT,status TEXT,receipt_sha256 TEXT);
CREATE TABLE stage_transitions(receipt_sha256 TEXT,subject_sha256 TEXT,stage TEXT,revision INT);
CREATE TABLE stage_content(subject_sha256 TEXT,stage TEXT,revision INT,content_sha256 TEXT);
CREATE TABLE stage_artifacts(sha256 TEXT,payload BLOB);
CREATE TABLE receipts(sha256 TEXT,role TEXT,model_or_tool TEXT);
CREATE TABLE agencies(id TEXT,name TEXT);
CREATE TABLE joins(original_sha256 TEXT,agency_id TEXT,status TEXT);
""")
        for number, status in enumerate(host.STATES[:-1]):
            identity = sha("synthetic-original-" + str(number))
            con.execute("INSERT INTO originals VALUES(?,?,?,?,?,?,?,?)", (identity, 10, "text/plain", "2026-01-01T00:00:00Z", "original", "out_of_scope" if number == 4 else "in_scope", "preserved", "txt"))
            for stage in host.STAGES:
                receipt = "receipt-" + str(number) + "-" + stage
                con.execute("INSERT INTO stage_state VALUES(?,?,?,?)", (identity, stage, status, receipt))
                if stage == "review":
                    con.execute("INSERT INTO receipts VALUES(?,?,?)", (receipt, "primary", "synthetic-model"))
            if number == 2:
                payload = host.encoded({"schema": "catalog-classification-v1", "subject_sha256": identity,
                                        "metadata": {"doc_type": "invoice", "value_score": 5, "date_from": "2026-01-02",
                                                     "date_to": "2026-02-03", "title": "RAW_MAIL_BODY_SENTINEL",
                                                     "summary": "RAW_MAIL_BODY_SENTINEL", "parties": ["PRIVATE_ACTOR_SENTINEL"],
                                                     "low_value_reason": "CLOSE_ME_SENTINEL"}})
                content = host.digest(payload)
                con.execute("INSERT INTO stage_artifacts VALUES(?,?)", (content, payload))
                con.execute("INSERT INTO stage_content VALUES(?,?,?,?)", (identity, "catalog", 1, content))
                con.execute("INSERT INTO stage_transitions VALUES(?,?,?,?)", ("receipt-2-catalog", identity, "catalog", 1))
                con.execute("INSERT INTO agencies VALUES(?,?)", ("synthetic-agency", "Fictional County"))
                con.execute("INSERT INTO joins VALUES(?,?,?)", (identity, "synthetic-agency", "typed"))
        con.commit()
        job_id = os.urandom(16).hex()
        state_dir = self.root / job_id
        state_dir.mkdir(mode=0o700)
        with open("/proc/sys/kernel/random/boot_id") as source:
            boot = source.read().strip()
        state = {"identity": {"uid": os.getuid(), "pid": 100, "start": 200}, "boot": boot, "run_id": os.urandom(16).hex()}
        receipt = dict(state, result=json.loads(worker()))
        for name, value in (("state.json", state), ("receipt.json", receipt)):
            target = state_dir / name
            target.write_bytes(host.encoded(value))
            target.chmod(0o600)
        return root, output, state_dir, job_id, con

    @contextmanager
    def ledger_api(self, con):
        @contextmanager
        def readonly(*args, **kwargs):
            self.assertTrue(kwargs["readonly"])
            yield con
        with mock.patch("campaign_tool.records.ledger.store.ledger", side_effect=readonly):
            yield

    def test_every_preserved_state_and_out_of_scope_card_reconciles(self):
        root, output, state, job_id, con = self.synthetic_ledger()
        before = con.total_changes
        with self.ledger_api(con):
            result = host.build_board(str(root), str(output), str(state), job_id)
        self.assertEqual(result["cards"], 5)
        catalog = json.loads((output / job_id / "catalog.json").read_text())
        receipt = json.loads((output / job_id / "receipt.json").read_text())
        self.assertEqual(len(catalog["cards"]), receipt["ledger_originals"])
        self.assertEqual(con.total_changes, before)
        self.assertEqual({card["stages"]["extract"] for card in catalog["cards"]}, set(host.STATES[:-1]))
        self.assertEqual(receipt["metadata_known"], 1)
        self.assertEqual(receipt["metadata_unknown"], 4)
        for counters in receipt["stage_counts"].values():
            self.assertEqual(sum(counters.values()), 5)
        html = (output / job_id / "index.html").read_text()
        self.assertEqual(html.count('<article class="card"'), 5)
        self.assertIn("model_or_tool_review", html)
        self.assertFalse(receipt["healthy"])
        con.close()

    def test_metadata_allowlist_no_raw_bodies_or_signed_urls(self):
        root, output, state, job_id, con = self.synthetic_ledger()
        signed = "https://synthetic.invalid/item?" + "access_token" + "=SIGNED_URL_SENTINEL"
        con.execute("UPDATE agencies SET name=?", (signed,))
        con.commit()
        with self.ledger_api(con):
            host.build_board(str(root), str(output), str(state), job_id)
        for name in ("index.html", "catalog.json", "receipt.json"):
            raw = (output / job_id / name).read_text()
            for sentinel in (signed, "SIGNED_URL_SENTINEL", "RAW_MAIL_BODY_SENTINEL", "PRIVATE_ACTOR_SENTINEL", "CLOSE_ME_SENTINEL"):
                self.assertNotIn(sentinel, raw)
        con.close()

    def test_metadata_no_overwrite_and_no_writer_race(self):
        root, output, state, job_id, con = self.synthetic_ledger()
        with self.ledger_api(con):
            host.build_board(str(root), str(output), str(state), job_id)
        catalog_path = output / job_id / "catalog.json"
        before = catalog_path.read_bytes()
        # End the synthetic read transaction, as the actual context manager does.
        con.rollback()
        with self.ledger_api(con), self.assertRaises(FileExistsError):
            host.build_board(str(root), str(output), str(state), job_id)
        self.assertEqual(catalog_path.read_bytes(), before)
        with host.PrivateDir(root) as directory, host.locked(directory, "run.lock"):
            with self.ledger_api(con), self.assertRaises(host.Rejected):
                host.build_board(str(root), str(output), str(state), os.urandom(16).hex())
        con.close()

    def test_metadata_missing_or_nonquiescent_proof_rejected(self):
        root, output, state, job_id, con = self.synthetic_ledger()
        receipt = json.loads((state / "receipt.json").read_text())
        receipt["result"]["quiescent"] = False
        (state / "receipt.json").write_bytes(host.encoded(receipt))
        with self.ledger_api(con), self.assertRaises(host.Rejected):
            host.build_board(str(root), str(output), str(state), job_id)
        self.assertEqual(list(output.iterdir()), [])
        con.close()

    def test_catalog_artifact_hash_drift_fails_not_unknown_success(self):
        root, output, state, job_id, con = self.synthetic_ledger()
        con.execute("UPDATE stage_artifacts SET payload=?", (b"CORRUPTED_CATALOG_SENTINEL",))
        con.commit()
        with self.ledger_api(con), self.assertRaises(host.Rejected):
            host.build_board(str(root), str(output), str(state), job_id)
        self.assertEqual(list(output.iterdir()), [])
        con.close()

    def test_real_public_ledger_readonly_smoke(self):
        from campaign_tool.records.ledger import store
        root, output, state, job_id, old = self.synthetic_ledger()
        old.close()
        (root / "ledger.sqlite").unlink()
        store.initialize(root / "ledger.sqlite")
        identity = sha("synthetic-public-ledger-original")
        with store.ledger(root / "ledger.sqlite") as con:
            con.execute("INSERT INTO originals VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (identity, 3, "text/plain", "2026-01-01", "original", "in_scope", None, "preserved", "txt", "{}"))
            con.commit()
        result = host.build_board(str(root), str(output), str(state), job_id)
        self.assertEqual(result["cards"], 1)
        with store.ledger(root / "ledger.sqlite", readonly=True) as con:
            self.assertEqual(con.execute("SELECT count(*) FROM originals").fetchone()[0], 1)


    def test_private_health_receipt_reconciles_and_redacts_real_board(self):
        root, output, state, job_id, con = self.synthetic_ledger()
        paths = dict(root=str(root),board_dir=str(output),job_id=job_id,python="/synthetic/python")
        raw = host.encoded({"status":"degraded","exit_code":1,"private":"RAW_MAIL_BODY_SENTINEL"})
        with self.ledger_api(con), mock.patch.object(host,"call_argv",return_value=(1,raw)):
            result = host.postrun_entry(paths)
        self.assertEqual(result["board"],"ok")
        self.assertEqual(result["health"],"degraded")
        self.assertFalse(result["healthy"])
        health_path = output / job_id / "health.json"
        health = json.loads(health_path.read_text())
        self.assertEqual(health["cards"],result["cards"])
        self.assertEqual(health["cards"],5)
        self.assertEqual(health["exit_code"],1)
        self.assertNotIn("RAW_MAIL_BODY_SENTINEL",health_path.read_text())
        self.assertEqual(health_path.stat().st_mode & 0o777,0o600)
        con.close()



if __name__ == "__main__":
    unittest.main()
