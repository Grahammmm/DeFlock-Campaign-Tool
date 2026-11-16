"""DF0652-06: quiescence-audit --roots-user NAME (root auditor, named account's backup roots), fake /proc tree."""
import errno
import hashlib
import traceback
import io
import json
import os
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from campaign_tool.records import quiescence_audit as qa, records_backup
from campaign_tool.records.recovery import records_profile as rp
from tests.records import test_runtime_fds as transport
from tests.records.test_quiescence_audit import units_text

REAL_NAMED = rp.named_account


class RootsUser(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(os.path.realpath(tmp.name))
        self.project = self.base / "project"
        self.project.mkdir()
        self.home = self.base / "home-coder"
        self.roots = [str(self.project)]
        for rel in rp.EXTRA_ROOTS:
            (self.home / rel).mkdir(parents=True)
            self.roots.append(str(self.home / rel))
        self.runtime = self.base / "runtime"
        self.runtime.mkdir()
        (self.runtime / "run-records.lock").touch()
        self.proc = self.base / "proc"
        self.proc.mkdir()
        self.units = self.base / "units"
        self.units.write_text("Captured=%d\n\n" % time.time() + units_text())
        self.out = self.base / "audit.json"
        self.euid = 0
        accounts = {"coder": SimpleNamespace(pw_name="coder", pw_dir=str(self.home)),
                    "other": SimpleNamespace(pw_name="other", pw_dir=str(self.base / "home-other"))}

        def getpwnam(name):
            return accounts[name]
        for patch in (mock.patch.object(qa, "PROC_ROOT", str(self.proc)),
                      mock.patch.object(rp.pwd, "getpwnam", getpwnam),
                      mock.patch.object(rp, "named_account", lambda name: REAL_NAMED(name, euid=self.euid)),
                      mock.patch.object(rp, "_home", lambda: str(self.base / "home-caller"))):
            patch.start()
            self.addCleanup(patch.stop)

    def proc_pid(self, pid, cwd="/", fds=()):
        base = self.proc / str(pid)
        (base / "fd").mkdir(parents=True)
        (base / "cwd").symlink_to(cwd)
        for number, target in enumerate(fds):
            (base / "fd" / str(number)).symlink_to(target)

    def run_main(self, *extra, source=None):
        out = io.StringIO()
        with redirect_stdout(out):
            code = qa.main(["--project", str(self.project)] + (source or ["--runtime", str(self.runtime)])
                           + ["--units-file", str(self.units), "--out", str(self.out)] + list(extra))
        return code, out.getvalue()

    def test_root_selects_coder_roots_and_quiet_fake_proc_passes(self):
        self.proc_pid(1, "/")
        self.proc_pid(2, "/", [str(self.base / "elsewhere")])
        code, text = self.run_main("--roots-user", "coder")
        doc = json.loads(self.out.read_text())
        self.assertEqual((code, doc["all_writers_stopped"], doc["roots_user"], doc["roots"]),
                         (0, True, "coder", self.roots))
        self.assertEqual(doc["roots"], list(rp.backup_roots(str(self.project), roots_user="coder")))
        self.assertIn("evidence_sha256=", text)
        with transport.session(self.base) as (path, _host):  # live socket source
            code, _ = self.run_main("--roots-user", "coder", source=["--runtime-socket", str(path)])
        doc = json.loads(self.out.read_text())
        self.assertEqual((code, doc["runtime_source"], doc["roots_user"]), (0, "socket", "coder"))

    def test_open_fd_and_cwd_count_distinct_pids_only(self):
        canary = "SECRETNAME"
        self.proc_pid(11, self.roots[0], [self.roots[1] + "/" + canary, self.roots[2], self.roots[0] + "/x"])
        self.proc_pid(12, "/", [self.roots[3] + "/f"])
        self.proc_pid(13, "/", [self.roots[0] + "-sibling/f"])
        code, text = self.run_main("--roots-user", "coder")
        raw = self.out.read_text()
        doc = json.loads(raw)
        self.assertEqual((code, doc["all_writers_stopped"], doc["checks"]["open_files"],
                          doc["checks"]["codes"]), (1, False, {"count": 2, "proc_unreadable": 0}, ["OPEN_FILE"]))
        self.assertEqual([line.split("=")[0] for line in text.split()], ["OPEN_FILE", "evidence_sha256"])
        self.assertNotIn(canary, text)
        self.assertNotIn(canary, raw)
        self.assertEqual(qa._open_files_user(self.roots + [self.roots[0]]), {"count": 2, "proc_unreadable": 0})

    def test_unreadable_process_never_disappears_from_verdict(self):
        self.proc_pid(21, "/", [self.roots[1] + "/f"])
        self.proc_pid(22, "/", ["/elsewhere"])
        real = os.readlink
        for denied, expect in (("22/cwd", {"count": 0, "proc_unreadable": 1}),
                               ("22/fd/0", {"count": 1, "proc_unreadable": 1})):
            def readlink(path, denied=denied):
                if str(path).endswith(denied):
                    raise PermissionError(errno.EACCES, "denied")
                return real(path)
            with self.subTest(denied=denied), mock.patch.object(qa.os, "readlink", readlink):
                result = qa._open_files_user(self.roots)
            self.assertEqual(result["proc_unreadable"], 1)
        with mock.patch.object(qa.os, "readlink", lambda path: (_ for _ in ()).throw(PermissionError(errno.EPERM, "x"))):
            code, text = self.run_main("--roots-user", "coder")
        doc = json.loads(self.out.read_text())
        self.assertEqual((code, doc["all_writers_stopped"], doc["checks"]["open_files"]),
                         (1, False, {"count": 0, "proc_unreadable": 2}))
        self.assertIn("PROC_UNREADABLE", text)
        real_listdir = os.listdir

        def listdir(path):
            if str(path).endswith("21/fd"):
                raise PermissionError(errno.EACCES, "denied")
            return real_listdir(path)
        with mock.patch.object(qa.os, "listdir", listdir):
            self.assertEqual(qa._open_files_user(self.roots), {"count": 0, "proc_unreadable": 1})

    def test_disappearing_pid_keeps_race_rules_and_proc_listing_failure_is_unavailable(self):
        self.proc_pid(31, "/", [self.roots[0] + "/gone"])
        (self.proc / "notapid").mkdir()
        real = os.readlink

        def readlink(path):
            if str(path).endswith("31/fd/0"):
                raise FileNotFoundError(errno.ENOENT, "gone")
            return real(path)
        with mock.patch.object(qa.os, "readlink", readlink):
            self.assertEqual(qa._open_files_user(self.roots), {"count": 0, "proc_unreadable": 0})
        self.out.write_bytes(b"keep")
        with mock.patch.object(qa, "PROC_ROOT", str(self.base / "no-proc")):
            code, text = self.run_main("--roots-user", "coder")
        self.assertEqual((code, text.split()), (2, ["AUDIT_UNAVAILABLE"]))

    def test_nonroot_or_unknown_user_refuses_before_inputs_or_out(self):
        self.out.write_bytes(b"keep")
        with mock.patch.object(qa, "receive_runtime_fds") as receive, mock.patch.object(qa, "_units") as units, \
                mock.patch.object(qa, "_open_files_user") as scan:
            self.euid = 1000
            for name in ("coder", "other"):
                code, text = self.run_main("--roots-user", name)
                self.assertEqual((code, text.split()), (2, ["ROOTS_USER_REQUIRES_ROOT"]))
            self.euid = 0
            for name in ("nobody-here", ""):
                code, text = self.run_main("--roots-user", name)
                self.assertEqual((code, text.split()), (2, ["ROOTS_USER_UNKNOWN"]))
            self.unknown_via_api("a\0b")
            receive.assert_not_called()
            units.assert_not_called()
            scan.assert_not_called()
        self.assertEqual(self.out.read_bytes(), b"keep")

    def unknown_via_api(self, name):
        with self.assertRaises(rp.ProfileIncomplete) as caught:
            qa.audit(str(self.project), str(self.runtime), str(self.units), roots_user=name)
        self.assertEqual(caught.exception.reason, "roots_user_unknown")

    def test_no_flag_mode_keeps_legacy_counting_and_document(self):
        self.proc_pid(41, self.roots[0], [self.roots[0] + "/a", self.roots[0] + "/b"])
        mine = Path(rp._home()) / "x"
        doc = qa.audit(str(self.project), str(self.runtime), str(self.units))
        self.assertNotIn("roots_user", doc)
        self.assertEqual(doc["roots"], [str(self.project)])
        self.assertEqual(doc["checks"]["open_files"], {"count": 3, "proc_unreadable": 0})  # targets, not pids
        self.assertNotIn(str(mine), json.dumps(doc))
        self.assertEqual(qa._open_files_user([self.roots[0]])["count"], 1)  # selected mode counts pids

    def test_selected_roots_follow_named_home_and_refuse_unsafe(self):
        link = self.home / rp.EXTRA_ROOTS[0]
        link.rmdir()
        link.symlink_to(self.base)
        code, text = self.run_main("--roots-user", "coder")
        doc = json.loads(self.out.read_text())
        self.assertEqual((code, doc["all_writers_stopped"], "ROOTS_UNSAFE" in doc["checks"]["codes"]), (1, False, True))
        missing = qa.audit(str(self.base / "no-project"), str(self.runtime), str(self.units), roots_user="coder")
        self.assertFalse(missing["all_writers_stopped"])

    def test_optional_evidence_metadata_is_closed_and_bound(self):
        roots = tuple(self.roots)
        stamp = "2026-11-15T00:00:00+00:00"
        base = {"schema": "records-quiescence-v1", "basis": "independent_all_writer_audit", "roots": list(roots),
                "observed_utc": stamp, "all_writers_stopped": True, "checks": {}, "auditor": "records quiescence-audit"}
        path = self.base / "e.json"
        caller = SimpleNamespace(pw_name="coder")

        def load(**extra):
            path.write_text(json.dumps({**base, **extra}))
            return records_backup._evidence(str(path), None, roots)
        with mock.patch.object(records_backup.pwd, "getpwuid", lambda uid: caller):
            for extra in ({}, {"runtime_source": "socket"}, {"roots_user": "coder"},
                          {"roots_user": "coder", "runtime_source": "socket"}):
                with self.subTest(extra=extra):
                    self.assertEqual(load(**extra).roots, roots)
            for extra in ({"roots_user": "other"}, {"roots_user": ""}, {"roots_user": 5},
                          {"runtime_source": "file"}, {"extra": 1}, {"roots_user": "coder", "extra": 1}):
                with self.subTest(refused=extra), self.assertRaises(ValueError):
                    load(**extra)
        # the real audit's own evidence is accepted unchanged
        with mock.patch.object(records_backup.pwd, "getpwuid", lambda uid: caller):
            self.run_main("--roots-user", "coder")
            self.assertEqual(records_backup._evidence(str(self.out), None, roots).roots, roots)


@unittest.skipUnless(os.geteuid() == 0, "real cross-UID handoff needs root (a mapped user namespace suffices)")
class CrossUidHandoff(unittest.TestCase):
    def test_root_evidence_handoff_preserves_owner_only_access_and_digest(self):
        # Chroot only a forked child: synthetic paths remain traversable after dropping UID,
        # even when the host's TMPDIR has private ancestors. No host account/files are changed.
        with tempfile.TemporaryDirectory() as directory:
            read_fd, write_fd = os.pipe()
            pid = os.fork()
            if pid == 0:
                os.close(read_fd)
                try:
                    os.chroot(directory)
                    os.chdir("/")
                    self.exercise_handoff()
                except BaseException:
                    os.write(write_fd, traceback.format_exc().encode())
                    os._exit(1)
                os._exit(0)
            os.close(write_fd)
            with os.fdopen(read_fd) as result:
                detail = result.read()
            _, status = os.waitpid(pid, 0)
            self.assertEqual(status, 0, detail)

    def exercise_handoff(self):
        uid, gid = 1000, 1000
        Path("/").chmod(0o755)
        project, home = Path("/project"), Path("/home-coder")
        for path in (project, home, Path("/runtime"), Path("/proc"), Path("/handoff")):
            path.mkdir(mode=0o700)
        for rel in rp.EXTRA_ROOTS:
            (home / rel).mkdir(parents=True)
        os.chown("/handoff", uid, gid)
        # Only account lookup and /proc are synthetic; UID checks, DAC, audit output,
        # evidence parsing, digest validation and backup's quiescence boundary are real.
        account = SimpleNamespace(pw_name="coder", pw_dir=str(home))
        root = SimpleNamespace(pw_name="root", pw_dir="/root")
        Path("/units").write_text("Captured=%d\n\n" % time.time() + units_text())
        with mock.patch.object(qa, "PROC_ROOT", "/proc"), \
                mock.patch.object(rp.pwd, "getpwnam", return_value=account), \
                mock.patch.object(records_backup.pwd, "getpwuid", side_effect=lambda n: account if n == uid else root):
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = qa.main(["--project", str(project), "--runtime", "/runtime", "--units-file", "/units",
                                "--roots-user", "coder", "--out", "/handoff/e.json"])
            self.assertEqual(code, 0)
            evidence = Path("/handoff/e.json")
            raw = evidence.read_bytes()
            digest = stdout.getvalue().strip().split("evidence_sha256=")[-1]
            self.assertEqual(hashlib.sha256(raw).hexdigest(), digest)
            roots = tuple(json.loads(raw)["roots"])
            self.assertEqual((evidence.stat().st_uid, evidence.stat().st_mode & 0o777), (0, 0o600))
            with self.assertRaises(ValueError):  # running backup as root cannot bypass the named caller
                records_backup._evidence(str(evidence), digest, roots)

            for handed_off in (False, True):
                if handed_off:
                    # chown --no-dereference NAME E: owner-only, no permission widening.
                    os.chown(evidence, uid, -1, follow_symlinks=False)
                    self.assertEqual((evidence.stat().st_uid, evidence.stat().st_mode & 0o777), (uid, 0o600))
                os.setresgid(gid, gid, 0)
                os.setresuid(uid, uid, 0)
                try:
                    self.assertEqual((os.getuid(), os.geteuid()), (uid, uid))
                    if not handed_off:
                        with self.assertRaises(PermissionError):
                            records_backup._evidence(str(evidence), digest, roots)
                        continue
                    accepted = records_backup._evidence(str(evidence), digest, roots)
                    records_backup.v2._boundary(accepted, tuple(Path(r) for r in roots))
                    self.assertEqual(accepted.evidence_sha256, digest)
                    self.assertEqual(evidence.read_bytes(), raw)
                    with self.assertRaises(ValueError):
                        records_backup._evidence(str(evidence), "0" * 64, roots)
                finally:
                    os.seteuid(0)
                    os.setresuid(0, 0, 0)
                    os.setresgid(0, 0, 0)


if __name__ == "__main__":
    unittest.main()
