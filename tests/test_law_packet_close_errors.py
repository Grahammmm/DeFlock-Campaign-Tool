"""Close-error handling of ``campaign_tool law review-packet`` (RF0440-1).

A close that reports an OSError must not stop the other descriptors of one packet invocation from being
released, must not strand a successor opened during the parent walk, and must not replace an error that is
already propagating. The hooks really close each descriptor first and then report EIO, so the descriptor
may already be free (and reused): no descriptor number is ever closed twice.
Run from the repository root: python3 -B -m unittest tests.test_law_packet_close_errors -v
"""

import argparse
import contextlib
import errno
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import law, law_packet  # noqa: E402

AS_OF = "2026-11-03"
MEMBERS = list(law_packet.NAMES)


class Faults:
    """Real-syscall forwarding hooks: label each descriptor, really close it, then report EIO for chosen roles.

    Roles: ``root``, ``walk:<component>``, ``parent`` (last walk component), ``out`` (the output folder
    candidate, later the verified descriptor) and the member names. ``reuse`` roles get a real sentinel
    allocated onto the number the real close just freed, before the error is reported.
    """

    def __init__(self, out, fail=(), reuse=(), before_open=None):
        self.out, self.fail, self.reuse, self.before_open = Path(out), set(fail), set(reuse), before_open
        self.roles, self.opened, self.attempts, self.sentinels, self.leaked = {}, [], [], {}, []
        self.real_open, self.real_close = os.open, os.close

    def role(self, path, flags, kwargs):
        if path == "/":
            return "root"
        if "dir_fd" not in kwargs:
            return None
        if path in law_packet.NAMES:
            return path
        if not flags & os.O_DIRECTORY:
            return None
        if path == self.out.name:
            return "out"
        return "parent" if path == self.out.parent.name else \
            "walk:" + path

    def _open(self, path, flags, *rest, **kwargs):
        if self.before_open:
            self.before_open(path, flags, kwargs)
        fd = self.real_open(path, flags, *rest, **kwargs)
        role = self.role(path, flags, kwargs)
        if role:
            self.roles[fd] = role
            self.opened.append((fd, role))
        return fd

    def _close(self, fd):
        role = self.roles.pop(fd, None)
        self.attempts.append((fd, role))
        self.real_close(fd)  # the descriptor really is released before any error is reported
        if role in self.reuse:
            new = self.real_open(__file__, os.O_RDONLY)
            assert new == fd, "the freed descriptor number was not reused"
            self.sentinels[fd] = new
        if role in self.fail:
            raise OSError(errno.EIO, "synthetic close failure " + role)

    def __enter__(self):
        self.patches = [mock.patch.object(os, "open", self._open), mock.patch.object(os, "close", self._close)]
        for patch in self.patches:
            patch.start()
        return self

    def __exit__(self, *exc):
        for patch in reversed(self.patches):
            patch.stop()
        for fd, role in self.opened:  # real fstat: anything still live was never released
            if fd in self.sentinels:
                continue
            try:
                os.fstat(fd)
            except OSError:
                continue
            self.leaked.append(role)
            self.real_close(fd)
        return False

    def released(self):
        """Roles that got a close attempt, in order; every attempt names a descriptor this run acquired."""
        return [role for _, role in self.attempts]


class CloseErrorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-law-close-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name).resolve()
        self.sentinel = os.open(__file__, os.O_RDONLY)  # an unrelated descriptor that must stay valid
        self.addCleanup(os.close, self.sentinel)

    def assert_sentinel(self):
        os.fstat(self.sentinel)

    def assert_every_release_attempted_once(self, faults, expected_roles):
        self.assertEqual(faults.leaked, [], "descriptors left open")
        self.assertEqual(sorted(faults.released(), key=str), sorted(expected_roles))
        self.assert_sentinel()

    def reference(self):
        ref = self.tmp / "reference"
        law_packet.build("us-ca", ref, AS_OF)
        return {name: (ref / name).read_bytes() for name in law_packet.NAMES}

    def files(self, out):
        return {name: (out / name).read_bytes() for name in sorted(os.listdir(out))}

    def walk_roles(self, out):
        return ["root"] + ["walk:" + part for part in out.parent.parts[1:-1]] + ["parent"]

    def test_close_error_after_success_attempts_every_remaining_release(self):
        reference = self.reference()
        for index, role in enumerate([*MEMBERS, "out", "parent"]):
            with self.subTest(failing=role):
                out = self.tmp / f"packet-{index}"
                with Faults(out, fail={role}) as faults, self.assertRaises(OSError) as caught:
                    law_packet.build("us-ca", out, AS_OF)
                self.assertEqual(caught.exception.errno, errno.EIO)
                self.assertIn(role, str(caught.exception))
                released = faults.released()
                self.assertEqual(released[-len(MEMBERS) - 2:], [*MEMBERS, "out", "parent"])
                self.assert_every_release_attempted_once(faults, [role for _, role in faults.opened])
                self.assertEqual(self.files(out), reference)  # completed packet left, not deleted
        with self.subTest(failing="several"):
            out = self.tmp / "packet-several"
            with Faults(out, fail={"RULES.md", "out", "parent"}) as faults, self.assertRaises(OSError) as caught:
                law_packet.build("us-ca", out, AS_OF)
            self.assertIn("RULES.md", str(caught.exception))  # first in release order wins
            self.assert_every_release_attempted_once(faults, [role for _, role in faults.opened])
            self.assertEqual(self.files(out), reference)

    def test_primary_write_failure_survives_all_cleanup_close_errors(self):
        primary = OSError(errno.ENOSPC, "Synthetic no space left on device")

        def fail_return(path, flags, kwargs):
            if path == "RETURN.md":
                raise primary

        for index, fail in enumerate(({"PACKET.md"}, {"WORKSHEET.csv", "out"}, {"parent"},
                                      {*MEMBERS[:3], "out", "parent"})):
            with self.subTest(failing=sorted(fail)):
                out = self.tmp / f"failed-{index}"
                with Faults(out, fail=fail, before_open=fail_return) as faults, self.assertRaises(OSError) as caught:
                    law_packet.build("us-ca", out, AS_OF)
                self.assertIs(caught.exception, primary)
                self.assertEqual(caught.exception.errno, errno.ENOSPC)
                self.assert_every_release_attempted_once(faults, [role for _, role in faults.opened])
                self.assertFalse(os.path.lexists(out))  # rollback ran while the descriptors were still held
                law_packet.build("us-ca", out, AS_OF)  # same path can be retried
                self.assertEqual({p.name for p in out.iterdir()}, set(MEMBERS))
        with self.subTest("replaced member preserved"):
            out = self.tmp / "replaced"
            marker = b"someone else's file"

            def replace_then_fail(path, flags, kwargs):
                if path == "RETURN.md":
                    (out / "WORKSHEET.csv").unlink()
                    (out / "WORKSHEET.csv").write_bytes(marker)
                    raise primary

            with Faults(out, fail={"out", "parent"}, before_open=replace_then_fail) as faults, \
                    self.assertRaises(OSError) as caught:
                law_packet.build("us-ca", out, AS_OF)
            self.assertIs(caught.exception, primary)
            self.assert_every_release_attempted_once(faults, [role for _, role in faults.opened])
            self.assertEqual(self.files(out), {"WORKSHEET.csv": marker})  # owned members removed, replacement kept

    def test_parent_successor_is_released_when_predecessor_close_reports_error(self):
        out = self.tmp / "packet"
        walk = self.walk_roles(out)
        for first in range(len(walk) - 1):  # root, then every component except the last handoff
            with self.subTest(predecessor=walk[first]):
                with Faults(out, fail={walk[first]}) as faults, self.assertRaises(OSError) as caught:
                    law_packet.build("us-ca", out, AS_OF)
                self.assertIn(walk[first], str(caught.exception))
                self.assertEqual(faults.leaked, [], "successor stranded")
                self.assertEqual(faults.released(), walk[:first + 2])  # nothing past the failed handoff opened
                self.assertFalse(os.path.lexists(out))
                self.assert_sentinel()
        with self.subTest("successor close also fails"):
            with Faults(out, fail={"root", "walk:" + out.parts[1]}) as faults, self.assertRaises(OSError) as caught:
                law_packet.build("us-ca", out, AS_OF)
            self.assertIn("root", str(caught.exception))  # the predecessor error, not the cleanup error
            self.assertNotIn("walk:", str(caught.exception))
            self.assertEqual(faults.released(), ["root", "walk:" + out.parts[1]])
            self.assertEqual(faults.leaked, [])
            self.assert_sentinel()

    def test_parent_open_error_wins_over_predecessor_close_error(self):
        out = self.tmp / "packet"
        denied = OSError(errno.EACCES, "Synthetic permission denied")
        target = out.parent.name

        def refuse(path, flags, kwargs):
            if path == target and "dir_fd" in kwargs:
                raise denied

        with Faults(out, fail={self.walk_roles(out)[-2]}, before_open=refuse) as faults, \
                self.assertRaises(OSError) as caught:
            law_packet.build("us-ca", out, AS_OF)
        self.assertIs(caught.exception, denied)
        self.assertEqual(faults.leaked, [])
        self.assertEqual(sorted(faults.released()), sorted(role for _, role in faults.opened))
        self.assertFalse(os.path.lexists(out))
        self.assert_sentinel()
        with self.subTest("root open fails"):
            broken = OSError(errno.EMFILE, "Synthetic too many open files")

            def refuse_root(path, flags, kwargs):
                if path == "/":
                    raise broken

            with Faults(out, before_open=refuse_root) as faults, self.assertRaises(OSError) as caught:
                law_packet.build("us-ca", out, AS_OF)
            self.assertIs(caught.exception, broken)
            self.assertEqual(faults.attempts, [])
        with self.subTest("root-only walk"):
            fd = law_packet._open_parent(Path("/"))
            try:
                self.assertTrue(stat.S_ISDIR(os.fstat(fd).st_mode))
            finally:
                os.close(fd)

    def test_admission_error_wins_over_candidate_close_error(self):
        out = self.tmp / "packet"
        real_fstat = os.fstat
        failure = OSError(errno.ENOSPC, "Synthetic fstat failure")
        faults = Faults(out, fail={"out"})

        def fstat(fd):
            if faults.roles.get(fd) == "out":
                raise failure
            return real_fstat(fd)

        with faults, mock.patch.object(os, "fstat", fstat), self.assertRaises(OSError) as caught:
            law_packet.build("us-ca", out, AS_OF)
        self.assertIs(caught.exception, failure)
        self.assertEqual(faults.leaked, [])
        self.assertEqual(faults.released().count("out"), 1)
        self.assertEqual(faults.released()[-2:], ["out", "parent"])  # parent is still released by the caller
        self.assertEqual(os.listdir(out), [])  # the unverified directory is neither filled nor removed
        self.assert_sentinel()
        with self.subTest("actual directory mismatch"):
            out = self.tmp / "swapped"
            parked = self.tmp / "parked"
            foreign = {}

            def swap(path, flags, kwargs):
                if path == "swapped" and flags & os.O_DIRECTORY and not foreign:
                    out.rename(parked)
                    out.mkdir(mode=0o750)
                    foreign["mode"] = stat.S_IMODE(out.stat().st_mode)

            with Faults(out, fail={"out"}, before_open=swap) as faults, self.assertRaises(ValueError) as caught:
                law_packet.build("us-ca", out, AS_OF)
            self.assertEqual(str(caught.exception), law_packet._CHANGED)
            self.assertEqual(faults.leaked, [])
            self.assertEqual(faults.released()[-2:], ["out", "parent"])
            self.assertEqual(os.listdir(out), [])
            self.assertEqual(os.listdir(parked), [])
            self.assertEqual(stat.S_IMODE(out.stat().st_mode), foreign["mode"])
            self.assert_sentinel()

    def test_close_error_never_retries_reused_descriptor_number(self):
        out = self.tmp / "packet"
        for role in ("PACKET.md", "out", "parent"):
            with self.subTest(reused=role):
                path = self.tmp / f"packet-{role}"
                with Faults(path, fail={role}, reuse={role}) as faults, self.assertRaises(OSError):
                    law_packet.build("us-ca", path, AS_OF)
                self.addCleanup(lambda f=faults: [os.close(fd) for fd in f.sentinels.values()])
                self.assertEqual(faults.leaked, [])
                (freed, _), = [(fd, r) for fd, r in faults.attempts if r == role]
                self.assertIn(freed, faults.sentinels)
                os.fstat(faults.sentinels[freed])  # the new owner of that number was never closed
                self.assertEqual([r for _, r in faults.attempts if r is None], [])  # no close of an unowned number
                self.assertEqual(sorted(faults.released()), sorted(r for _, r in faults.opened))
                self.assert_sentinel()
        self.assertFalse(os.path.lexists(out))

    def test_cli_reports_primary_failure_and_never_success_after_close_error(self):
        def run(out, *, fail=(), before_open=None):
            args = argparse.Namespace(jurisdiction="us-ca", out=str(out), as_of=AS_OF, base=None)
            stdout, stderr = io.StringIO(), io.StringIO()
            with Faults(out, fail=fail, before_open=before_open) as faults, \
                    contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = law_packet.main(args)
            self.assertEqual(faults.leaked, [])
            return code, stdout.getvalue(), stderr.getvalue()

        out = self.tmp / "close-only"
        code, stdout, stderr = run(out, fail={"RETURN.md"})
        self.assertEqual((code, stdout), (2, ""))
        self.assertEqual(stderr, "Stopped: [Errno 5] synthetic close failure RETURN.md\n")
        self.assertEqual({p.name for p in out.iterdir()}, set(MEMBERS))  # completed output is not deleted
        code, stdout, stderr = run(out)  # same path refuses
        self.assertEqual((code, stdout), (2, ""))
        self.assertEqual(stderr, "Stopped: --out already exists, not overwriting\n")
        code, stdout, stderr = run(self.tmp / "fresh")  # a fresh path succeeds
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(json.loads(stdout)["status"], "review_required")

        def full(path, flags, kwargs):
            if path == "RETURN.md":
                raise OSError(errno.ENOSPC, "Synthetic no space left on device")

        out = self.tmp / "primary"
        code, stdout, stderr = run(out, fail={"PACKET.md", "out", "parent"}, before_open=full)
        self.assertEqual((code, stdout), (2, ""))
        self.assertEqual(stderr, "Stopped: [Errno 28] Synthetic no space left on device\n")
        self.assertFalse(os.path.lexists(out))
        self.assert_sentinel()

    def test_no_fault_packet_bytes_modes_and_descriptor_lifetimes_are_unchanged(self):
        out = self.tmp / "packet"
        with Faults(out) as faults:
            report = law_packet.build("us-ca", out, AS_OF)
        self.assertEqual(faults.leaked, [])
        self.assertEqual(faults.released()[-len(MEMBERS) - 2:], [*MEMBERS, "out", "parent"])
        self.assertEqual(sorted(faults.released()), sorted(r for _, r in faults.opened))  # one close each
        package = law.load_package("us-ca", None)
        sheet = io.StringIO(newline="")
        law.write_sheet(sheet, law.review_sheet(package, law.parse_date(AS_OF, "--as-of"))[0])
        counts = law.summary(package)["review_counts"]
        packet, rules, back = law_packet.render(package, counts)
        expected = {"PACKET.md": packet, "RULES.md": rules, "RETURN.md": back, "WORKSHEET.csv": sheet.getvalue()}
        self.assertEqual({k: v.decode() for k, v in self.files(out).items()}, expected)
        self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o700)
        self.assertEqual({stat.S_IMODE((out / n).stat().st_mode) for n in MEMBERS}, {0o600})
        self.assertEqual(report["files"], MEMBERS)
        self.assertEqual(report["review_counts"], counts)
        self.assertEqual(report["out"], str(out))
        self.assertEqual(report["status"], "review_required")

    def test_process_control_and_programming_errors_are_not_treated_as_close_errors(self):
        stop = KeyboardInterrupt()

        def interrupt(path, flags, kwargs):
            if path == "RETURN.md":
                raise stop

        out = self.tmp / "interrupted"
        with Faults(out, fail={"PACKET.md", "parent"}, before_open=interrupt) as faults, \
                self.assertRaises(KeyboardInterrupt) as caught:
            law_packet.build("us-ca", out, AS_OF)
        self.assertIs(caught.exception, stop)
        self.assertEqual(faults.leaked, [])
        self.assertFalse(os.path.lexists(out))
        out = self.tmp / "bug"
        bug = RuntimeError("synthetic programming error")
        faults = Faults(out)
        real_close = faults._close

        def broken_close(fd):
            if faults.roles.get(fd) == "WORKSHEET.csv":
                real_close(fd)
                raise bug
            real_close(fd)

        faults._close = broken_close
        with faults, self.assertRaises(RuntimeError) as caught:
            law_packet.build("us-ca", out, AS_OF)
        self.assertIs(caught.exception, bug)  # not converted into an ordinary close refusal
        self.assertEqual(faults.leaked, [])  # every other release was still attempted
        self.assert_sentinel()


if __name__ == "__main__":
    unittest.main()
