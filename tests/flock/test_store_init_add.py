"""RF0910-9: flock_store_init.py add STORE --agency SLUG adds empty agency folders to an existing store, all or nothing;
every existing file keeps its bytes and mode."""
import contextlib
import importlib.util
import io
import os
import stat
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
FIX = ROOT / "tests" / "fixtures" / "flock"


def load(name):
    spec = importlib.util.spec_from_file_location("_test_store_add_" + name, ROOT / "tools" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


init, check, facts, law_policy = (load(n) for n in ("flock_store_init", "flock_store_check", "flock_facts",
                                                     "flock_law_policy"))


def run(fn, argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = fn([*map(str, argv)])
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


def state(root):  # every entry, links not followed: (mode, bytes or link target)
    out = {}
    for dirpath, dirs, files in os.walk(root):
        for name in dirs + files:
            p = Path(dirpath) / name
            st = os.lstat(p)
            body = os.readlink(p) if p.is_symlink() else (p.read_bytes() if stat.S_ISREG(st.st_mode) else None)
            out[p.relative_to(root).as_posix()] = (stat.S_IMODE(st.st_mode), body)
    return out


REFUSED = "add refused | nothing written\n"
A = "testville-pd"  # the agency the store starts with


class StoreAddTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.store = self.tmp / "store"
        self.assertEqual(run(init.main, ["init", self.store, "--agency", A])[0], 0)
        src = self.tmp / "v1.csv"  # one fixture fact for the existing agency
        lines = (FIX / "facts-sample.csv").read_text(encoding="utf-8").splitlines()
        row = next(ln for ln in lines[1:] if ln.startswith("Testville PD,"))
        src.write_text(lines[0] + "\n" + row + "\n")
        code, out, _ = run(facts.main, ["import", src, "--store", self.store,
                                        "--added-by", "tester", "--date", "2026-11-02"])
        self.assertEqual((code, out), (0, "imported=1 duplicates=0 errors=0 agencies=1\n"))

    def add(self, *argv):
        return run(init.main, ["add", self.store, *argv])

    def assert_refused(self, result, *why):
        code, out, err = result
        self.assertEqual((code, out), (1, ""))
        for w in why:
            self.assertIn(f"add refused | {w}\n", err)
        self.assertTrue(err.endswith(REFUSED), err)

    def test_adds_only_the_new_agency_old_bytes_and_modes_unchanged(self):
        before = state(self.store)
        self.assertEqual(len((self.store / "agencies" / A / "FACTS.csv").read_text().splitlines()), 2)
        code, out, err = self.add("--agency", "b-c", "--agency", "d")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, "add | b-c | FACTS.csv POLICY.csv\nadd | d | FACTS.csv POLICY.csv\n"
                              "add | agencies=2 added, 1 already there | store check before: errors=0\n")
        after = state(self.store)
        self.assertEqual({k: v for k, v in after.items() if k in before}, before)
        self.assertEqual(sorted(set(after) - set(before)), [
            "agencies/b-c", "agencies/b-c/FACTS.csv", "agencies/b-c/POLICY.csv",
            "agencies/d", "agencies/d/FACTS.csv", "agencies/d/POLICY.csv"])
        for slug in ("b-c", "d"):
            self.assertEqual(after[f"agencies/{slug}"], (0o700, None))
            self.assertEqual(after[f"agencies/{slug}/FACTS.csv"], (0o600, (",".join(facts.V2) + "\n").encode()))
            self.assertEqual(after[f"agencies/{slug}/POLICY.csv"],
                             (0o600, (",".join(law_policy.POLICY_HEADER) + "\n").encode()))
        self.assertEqual(run(check.main, [self.store])[0], 0)
        self.assertNotIn(str(self.tmp), out)

    def test_agency_already_there_refused_and_nothing_else_created(self):
        before = state(self.store)
        self.assert_refused(self.add("--agency", A), f"agency {A} already there")
        self.assert_refused(self.add("--agency", "b", "--agency", A), f"agency {A} already there")
        self.assertEqual(state(self.store), before)  # b NOT created
        self.assert_refused(self.add("--agency", "b", "--agency", "b"), "agency b given twice")
        self.assert_refused(self.add("--agency", "Bad"), "agency slug is not [a-z0-9]+(-[a-z0-9]+)* of at most 64 "
                                                         "characters")
        self.assert_refused(self.add(), "no --agency given")
        self.assertEqual(self.add("--agency")[0], 2)
        self.assertEqual(state(self.store), before)

    def test_link_or_file_in_the_slug_place_is_refused_and_not_followed(self):
        outside = self.tmp / "outside"
        outside.mkdir()
        (self.store / "agencies" / "c").symlink_to(outside)
        (self.store / "agencies" / "e").write_text("x")
        (self.store / "agencies" / "f").symlink_to(self.tmp / "nowhere")  # dangling link counts too
        before = state(self.store)
        for slug in ("c", "e", "f"):
            self.assert_refused(self.add("--agency", slug), f"agency {slug} already there")
        self.assertEqual((state(self.store), list(outside.iterdir())), (before, []))
        self.assertFalse(os.path.lexists(self.tmp / "nowhere"))

    def test_store_location_refusals_write_nothing(self):
        bare = self.tmp / "bare"
        bare.mkdir()
        linkagencies = self.tmp / "linkagencies"
        linkagencies.mkdir()
        (linkagencies / "agencies").symlink_to(self.store / "agencies")
        (self.tmp / "link").symlink_to(self.store)
        before = state(self.tmp)
        cases = {ROOT / "rf0910-9-store-probe": "STORE is inside the repository",
                 ROOT / "tests": "STORE is inside the repository",
                 self.tmp / "missing": "STORE is not an existing folder",
                 self.store / "LAW.csv": "STORE is not an existing folder",
                 self.tmp / "link": "STORE is a symlink",
                 bare: "STORE/agencies is missing or a symlink",
                 linkagencies: "STORE/agencies is missing or a symlink"}
        for store, why in cases.items():
            self.assert_refused(run(init.main, ["add", store, "--agency", "b"]), why)
            self.assertEqual(state(self.tmp), before, store)
        self.assertEqual([p for p in ROOT.iterdir() if p.name.startswith("rf0910-9-store-probe")], [])
        self.assertFalse(os.path.lexists(ROOT / "tests" / "agencies"))

    def test_store_check_errors_are_counted_but_exit_2_refuses(self):
        with open(self.store / "agencies" / A / "FACTS.csv", "a", encoding="utf-8") as fh:
            fh.write("not,a,valid,row\n")
        self.assertEqual(run(check.main, [self.store])[0], 1)
        code, out, _ = self.add("--agency", "b")
        self.assertEqual(code, 0)
        self.assertTrue(out.endswith("| store check before: errors=1\n"), out)
        self.assertTrue((self.store / "agencies" / "b" / "FACTS.csv").is_file())
        before = state(self.store)
        fake = types.SimpleNamespace(main=lambda argv: 2)
        with mock.patch.object(init, "_tool", lambda name: fake):
            self.assert_refused(self.add("--agency", "g"), "store check exits 2 (refusal or usage)")
        self.assertEqual(state(self.store), before)

    def test_fault_on_the_second_folder_removes_the_first_too(self):
        before = state(self.store)
        real_open, calls = open, []

        def failing(*args, **kwargs):
            calls.append(args)
            if len(calls) == 3:  # b/FACTS.csv, b/POLICY.csv, then g/FACTS.csv fails
                raise OSError("disk full at /secret/path")
            return real_open(*args, **kwargs)
        with mock.patch.object(init, "open", failing, create=True), \
                mock.patch.object(init, "snapshot", lambda root: {}):
            code, out, err = self.add("--agency", "b", "--agency", "g")
        self.assertEqual((code, out, err), (1, "", "add refused | io: OSError\n" + REFUSED))
        self.assertEqual(state(self.store), before)  # b removed too, no .tmp- left
        real_rename = os.rename

        def rename(src, dst):
            if Path(dst).name == "g":
                raise PermissionError(13, "denied", str(dst))
            return real_rename(src, dst)
        with mock.patch.object(init.os, "rename", rename):
            code, _, err = self.add("--agency", "b", "--agency", "g")
        self.assertEqual((code, err), (1, "add refused | io: PermissionError\n" + REFUSED))
        self.assertEqual(state(self.store), before)

    def test_existing_file_changed_during_add_is_exit_1_and_rolled_back(self):
        before, snaps = state(self.store), iter([{"LAW.csv": (1, "x")}, {"LAW.csv": (1, "y")}])
        with mock.patch.object(init, "snapshot", lambda root: next(snaps)):
            code, out, err = self.add("--agency", "b")
        self.assertEqual((code, out, err), (1, "", "add: existing file changed\n"))
        self.assertEqual(state(self.store), before)

    def test_bare_and_init_forms_still_create_a_store(self):
        for argv in ([self.tmp / "x1", "--agency", "x"], ["init", self.tmp / "x2", "--agency", "x"]):
            code, out, _ = run(init.main, argv)
            self.assertEqual(code, 0)
            self.assertTrue(out.startswith("init | x | FACTS.csv POLICY.csv\ninit | agencies=1 | LAW.csv"), out)
        self.assertEqual(state(self.tmp / "x1"), state(self.tmp / "x2"))


if __name__ == "__main__":
    unittest.main()
