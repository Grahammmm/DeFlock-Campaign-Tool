"""RF1105-9: flock_law_policy names I/O errors and never reads a POLICY.csv through a symlink (main() in-process)."""
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

from tests.flock.test_law_policy import AGENCY, FIXTURES, TOOL

SPEC = importlib.util.spec_from_file_location("flock_law_policy_io_under_test", TOOL)
tool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tool)
LINK = "a: POLICY.csv is a link"


def call(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = tool.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class LawPolicyIoTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.store = self.tmp / "store"
        self.outside = self.tmp / "outside"
        self.policy(self.store / "agencies" / AGENCY)  # one real agency: 2 valid rows
        self.policy(self.outside / "b")

    def policy(self, folder):
        folder.mkdir(parents=True, exist_ok=True)
        return Path(shutil.copy(FIXTURES / "policy-sample.csv", folder / "POLICY.csv"))

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root reads a chmod 000 file")
    def test_unreadable_file_named_not_traceback(self):
        path = self.policy(self.tmp / "solo")
        path.chmod(0)
        self.addCleanup(path.chmod, 0o600)
        self.assertEqual(call("--check", str(path)),
                         (1, "row 1: file unreadable (PermissionError)\nlaw=0 policy=0 errors=1\n", ""))
        self.assertEqual(call("versions", str(path), "--agency", AGENCY),
                         (1, "", "row 1: file unreadable (PermissionError)\n"))

    def test_law_package_oserror_named(self):
        with mock.patch.object(tool.law, "load_package", side_effect=PermissionError("x")):
            self.assertEqual(call("--check", str(FIXTURES / "law-sample.csv")),
                             (1, "row 1: law package unreadable\nlaw=0 policy=0 errors=1\n", ""))

    def test_any_other_oserror_is_one_io_line(self):
        with mock.patch.object(tool, "load_all", side_effect=PermissionError("/private/path")):
            self.assertEqual(call("--check", str(self.store)), (1, "", "io: PermissionError\n"))
            self.assertEqual(call("versions", str(self.store), "--agency", AGENCY), (1, "", "io: PermissionError\n"))

    def test_symlinked_agencies_folder_refused_and_not_counted(self):
        shutil.copytree(self.store / "agencies", self.outside / "agencies")
        shutil.rmtree(self.store / "agencies")
        (self.store / "agencies").symlink_to(self.outside / "agencies", target_is_directory=True)
        self.assertEqual(call("--check", str(self.store)), (1, "store: agencies is a link\nlaw=0 policy=0 errors=1\n", ""))
        shutil.copy(FIXTURES / "law-sample.csv", self.store / "LAW.csv")  # LAW.csv still checked beside it
        self.assertEqual(call("--check", str(self.store)), (1, "store: agencies is a link\nlaw=3 policy=0 errors=1\n", ""))

    def test_symlinked_agency_folder_refused_others_counted(self):
        (self.store / "agencies" / "a").symlink_to(self.outside / "b", target_is_directory=True)
        self.assertEqual(call("--check", str(self.store)), (1, f"{LINK}\nlaw=0 policy=2 errors=1\n", ""))
        self.assertEqual(call("versions", str(self.store), "--agency", "b"), (1, "", LINK + "\n"))
        self.assertEqual(call("versions", str(self.store), "--agency", AGENCY, "--on", "2026-10-20"), (1, "", LINK + "\n"))

    def test_symlinked_policy_csv_refused_not_read(self):
        folder = self.store / "agencies" / "a"
        folder.mkdir()
        (folder / "POLICY.csv").symlink_to(self.outside / "b" / "POLICY.csv")
        self.assertEqual(call("--check", str(self.store)), (1, f"{LINK}\nlaw=0 policy=2 errors=1\n", ""))
        (folder / "POLICY.csv").unlink()
        (folder / "POLICY.csv").symlink_to(self.outside / "missing.csv")  # dangling link: same error, no read attempt
        with mock.patch.object(tool, "load_policy", wraps=tool.load_policy) as read:
            self.assertEqual(call("--check", str(self.store)), (1, f"{LINK}\nlaw=0 policy=2 errors=1\n", ""))
        self.assertEqual([c.args[1] for c in read.call_args_list], [AGENCY])

    def test_valid_store_output_unchanged(self):
        shutil.copy(FIXTURES / "law-sample.csv", self.store / "LAW.csv")
        self.assertEqual(call("--check", str(self.store)), (0, "law=3 policy=2 errors=0\n", ""))
        code, out, err = call("versions", str(self.store), "--agency", AGENCY)
        self.assertEqual((code, len(out.splitlines()), err), (0, 2, ""))


if __name__ == "__main__":
    unittest.main()
