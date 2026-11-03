"""RF0910-8: tools/flock_store_init.py writes a new, empty private flock store, all or nothing, and the store check passes."""
import contextlib
import importlib.util
import io
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
FIX = ROOT / "tests" / "fixtures" / "flock"


def load(name):
    spec = importlib.util.spec_from_file_location("_test_store_init_" + name, ROOT / "tools" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


init, check, facts, law_policy = (load(n) for n in ("flock_store_init", "flock_store_check", "flock_facts",
                                                     "flock_law_policy"))


def run(fn, argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = fn(argv)
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


def tree(root):
    return sorted((p.relative_to(root).as_posix(), stat.S_IMODE(os.lstat(p).st_mode)) for p in Path(root).rglob("*"))


class StoreInitTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.store = self.tmp / "store"

    def init(self, *argv):
        return run(init.main, [*map(str, argv)])

    def assert_untouched(self, before):
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), before)

    def test_two_agencies_exact_tree_and_every_checker_accepts_it(self):
        code, out, err = self.init(self.store, "--agency", "a-b", "--agency", "c")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, "init | a-b | FACTS.csv POLICY.csv\ninit | c | FACTS.csv POLICY.csv\n"
                              "init | agencies=2 | LAW.csv | private store; keep it out of the repository and back it up "
                              "(flock_backup.py)\n")
        self.assertEqual(tree(self.store), [
            ("LAW.csv", 0o600), ("agencies", 0o700), ("agencies/a-b", 0o700), ("agencies/a-b/FACTS.csv", 0o600),
            ("agencies/a-b/POLICY.csv", 0o600), ("agencies/c", 0o700), ("agencies/c/FACTS.csv", 0o600),
            ("agencies/c/POLICY.csv", 0o600)])
        self.assertEqual(stat.S_IMODE(os.lstat(self.store).st_mode), 0o700)
        self.assertEqual((self.store / "LAW.csv").read_bytes(), (",".join(law_policy.LAW_HEADER) + "\n").encode())
        for slug in ("a-b", "c"):
            self.assertEqual((self.store / "agencies" / slug / "FACTS.csv").read_bytes(),
                             (",".join(facts.V2) + "\n").encode())
            self.assertEqual((self.store / "agencies" / slug / "POLICY.csv").read_bytes(),
                             (",".join(law_policy.POLICY_HEADER) + "\n").encode())
        self.assertEqual([p.name for p in self.tmp.iterdir()], ["store"])  # no temp tree left
        code, out, _ = run(check.main, [str(self.store)])
        self.assertEqual(code, 0, out)
        self.assertIn("contacts | none\n", out)
        self.assertIn("requests | none\n", out)
        self.assertEqual(facts.check_path(str(self.store)), [])
        self.assertEqual(law_policy.load_all(self.store)[2], [])
        for slug in ("a-b", "c"):
            self.assertEqual(facts.check_file(self.store / "agencies" / slug / "FACTS.csv", slug), [])

    def test_store_is_usable_facts_import_succeeds(self):
        self.assertEqual(self.init(self.store, "--agency", "testville-pd")[0], 0)
        lines = (FIX / "facts-sample.csv").read_text(encoding="utf-8").splitlines()
        src = self.tmp / "v1.csv"  # the v1 fixture's rows for one agency
        src.write_text("\n".join([lines[0]] + [ln for ln in lines[1:] if ln.startswith("Testville PD,")]) + "\n")
        code, out, _ = run(facts.main, ["import", str(src), "--store", str(self.store),
                                        "--added-by", "tester", "--date", "2026-11-02"])
        self.assertEqual(code, 0, out)
        self.assertEqual(out, "imported=3 duplicates=0 errors=0 agencies=1\n")
        self.assertEqual(len((self.store / "agencies" / "testville-pd" / "FACTS.csv").read_text().splitlines()), 4)
        self.assertEqual(facts.check_path(str(self.store)), [])
        self.assertEqual(run(check.main, [str(self.store)])[0], 0)

    def test_store_location_refusals_write_nothing(self):
        (self.tmp / "full").mkdir()
        (self.tmp / "full" / "x").write_text("x")
        (self.tmp / "empty").mkdir()
        (self.tmp / "link").symlink_to(self.tmp / "empty")
        (self.tmp / "parentlink").symlink_to(self.tmp / "empty")
        cases = {ROOT / "rf0910-8-store-probe": "STORE is inside the repository",
                 self.tmp / "full": "STORE exists and is not an empty folder",
                 self.tmp / "link": "STORE or its parent is a symlink",
                 self.tmp / "parentlink" / "store": "STORE or its parent is a symlink",
                 self.tmp / "missing" / "store": "STORE parent folder is missing"}
        before = sorted(p.name for p in self.tmp.iterdir())
        for store, why in cases.items():
            code, out, err = self.init(store, "--agency", "a")
            self.assertEqual((code, out), (1, ""), store)
            self.assertIn(f"init refused | {why}\n", err)
            self.assertTrue(err.endswith("init refused | nothing written\n"))
            self.assert_untouched(before)
            self.assertEqual(sorted(p.name for p in (self.tmp / "empty").iterdir()), [])
            self.assertEqual(list((self.tmp / "full").iterdir()), [self.tmp / "full" / "x"])
        self.assertFalse(os.path.lexists(ROOT / "rf0910-8-store-probe"))
        self.assertEqual([p for p in ROOT.iterdir() if p.name.startswith("rf0910-8-store-probe")], [])

    def test_bad_slugs_duplicates_and_no_agency_write_nothing(self):
        for slug in ("Bad", "a--b", "-a", "a-", "a/b", "..", "a" * 65, "", "a_b", "a b"):
            code, out, err = self.init(self.store, "--agency", "ok", "--agency=" + slug)
            self.assertEqual((code, out), (1, ""), slug)
            self.assertIn("init refused | agency slug is not [a-z0-9]+(-[a-z0-9]+)* of at most 64 characters\n", err)
            self.assertFalse(os.path.lexists(self.store))
        self.assertEqual(self.init(self.tmp / "s64", "--agency", "a" * 64)[0], 0)  # 64 is the limit, not over it
        code, _, err = self.init(self.store, "--agency", "x", "--agency", "x")
        self.assertEqual(code, 1)
        self.assertIn("init refused | agency x given twice\n", err)
        code, _, err = self.init(self.store)
        self.assertEqual(code, 1)
        self.assertIn("init refused | no --agency given\n", err)
        self.assertEqual(self.init()[0], 2)  # no STORE: usage
        self.assertEqual(self.init(self.store, "--agency")[0], 2)
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ["s64"])

    def test_all_refusals_are_named_together(self):
        code, _, err = self.init(ROOT / "rf0910-8-store-probe", "--agency", "x", "--agency", "x", "--agency", "Bad")
        self.assertEqual(code, 1)
        self.assertEqual(err.splitlines(), [
            "init refused | agency x given twice",
            "init refused | agency slug is not [a-z0-9]+(-[a-z0-9]+)* of at most 64 characters",
            "init refused | STORE is inside the repository",
            "init refused | nothing written"])

    def test_existing_empty_directory_is_filled_with_the_same_contents(self):
        self.store.mkdir(mode=0o755)
        code, out, _ = self.init(self.store, "--agency", "a-b", "--agency", "c")
        self.assertEqual(code, 0)
        other = self.tmp / "other"
        self.assertEqual(self.init(other, "--agency", "a-b", "--agency", "c")[1], out)
        self.assertEqual(tree(self.store), tree(other))
        self.assertEqual(stat.S_IMODE(os.lstat(self.store).st_mode), 0o700)  # renamed onto the empty folder
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ["other", "store"])
        self.assertEqual(run(check.main, [str(self.store)])[0], 0)

    def test_write_failing_on_second_agency_leaves_nothing(self):
        real, calls = open, []

        def failing(*args, **kwargs):
            calls.append(args)
            if len(calls) == 4:  # LAW.csv, a/FACTS.csv, a/POLICY.csv, then b/FACTS.csv fails
                raise OSError("disk full at /secret/path")
            return real(*args, **kwargs)
        with mock.patch.object(init, "open", failing, create=True):
            code, out, err = self.init(self.store, "--agency", "a", "--agency", "b")
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err, "init refused | io: OSError\ninit refused | nothing written\n")
        self.assertEqual(list(self.tmp.iterdir()), [])  # STORE absent, no STORE.tmp-<pid> left
        self.store.mkdir()  # an existing empty STORE stays, still empty
        calls.clear()
        with mock.patch.object(init, "open", failing, create=True):
            calls.extend([None] * 3)  # the very next open (LAW.csv) fails
            self.assertEqual(self.init(self.store, "--agency", "a")[0], 1)
        self.assertEqual((list(self.tmp.iterdir()), list(self.store.iterdir())), ([self.store], []))

    def test_leftover_temp_name_is_not_reused_or_removed(self):
        tmp = self.tmp / f"store.tmp-{os.getpid()}"
        tmp.mkdir()
        (tmp / "keep").write_text("k")
        code, _, err = self.init(self.store, "--agency", "a")
        self.assertEqual((code, err), (1, "init refused | io: FileExistsError\ninit refused | nothing written\n"))
        self.assertEqual(((tmp / "keep").read_text(), os.path.lexists(self.store)), ("k", False))

    def test_unlistable_existing_store_is_a_path_free_refusal(self):
        self.store.mkdir()
        secret = str(self.store.resolve())

        def unlistable(path):
            raise PermissionError(13, "Permission denied", secret)
        with mock.patch.object(init.Path, "iterdir", unlistable):
            code, out, err = self.init(self.store, "--agency", "a")
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err, "init refused | io: PermissionError\ninit refused | nothing written\n")
        self.assertNotIn(str(self.tmp), err)
        self.assertNotIn(secret, err)
        self.assertEqual((list(self.tmp.iterdir()), list(self.store.iterdir())), ([self.store], []))

    def test_stdout_and_stderr_never_contain_the_absolute_path(self):
        code, out, err = self.init(self.store, "--agency", "a")
        self.assertEqual(code, 0)
        code2, out2, err2 = self.init(self.store, "--agency", "a")  # now non-empty: refused
        self.assertEqual(code2, 1)
        for text in (out, err, out2, err2):
            self.assertNotIn(str(self.tmp), text)
            self.assertNotIn(str(self.tmp.resolve()), text)


class ContactsSectionTests(unittest.TestCase):
    """The one flock_store_check change: a missing CONTACT.csv is reported as none; a present one is still checked."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = Path(tmp.name) / "store"
        run(init.main, [str(self.store), "--agency", "a", "--agency", "b"])

    def test_missing_contact_files_are_none(self):
        self.assertEqual(check.contacts(self.store), ([], ["none"]))

    def test_header_only_or_linked_contact_file_still_fails(self):
        (self.store / "agencies" / "a" / "CONTACT.csv").write_text(",".join(init_cols()) + "\n")
        self.assertEqual(check.contacts(self.store), (["a: contact: no records row"], []))
        (self.store / "agencies" / "a" / "CONTACT.csv").unlink()
        (self.store / "agencies" / "b" / "CONTACT.csv").symlink_to(self.store / "nowhere.csv")
        self.assertEqual(check.contacts(self.store), (["b: contact: CONTACT.csv missing or unreadable"], []))
        self.assertEqual(run(check.main, [str(self.store)])[0], 1)


def init_cols():
    return load("flock_email").COLS


if __name__ == "__main__":
    unittest.main()
