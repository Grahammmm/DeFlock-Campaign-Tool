"""git exposure row and engine-local init refusal. Temporary synthetic repos only; nothing touches the network."""

import itertools
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import git_exposure  # noqa: E402
from campaign_tool.doctor_checklist import checklist, render  # noqa: E402
from campaign_tool.git_exposure import GitExposureError, git_exposure_row, refuse_engine_directory  # noqa: E402

GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
NAMES = ("kit", "private", "public")


def git(cwd, *args):
    return subprocess.run([*GIT, "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout


def snapshot(root):
    out = {}
    for path in sorted(Path(root).rglob("*")):
        st = path.lstat()
        out[str(path)] = (st.st_mode, None if path.is_dir() or path.is_symlink() else path.read_bytes())
    return out


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()

    def tree(self, ignore_lines=(), name="work"):
        root = self.base / name
        root.mkdir()
        git(root, "init", "-q")
        (root / ".gitignore").write_text("".join(line + "\n" for line in ignore_lines))
        return root


class InitRefusalTests(Base):
    def engine(self):
        engine = self.base / "engine"
        engine.mkdir()
        return engine

    def test_init_refuses_engine_root_descendant_and_alias_before_write(self):
        engine = self.engine()
        alias = self.base / "alias"
        alias.symlink_to(engine)
        before = snapshot(self.base)
        with mock.patch.object(git_exposure, "engine_checkout_root", return_value=engine), \
                mock.patch.object(git_exposure.shutil, "which", return_value=None):
            for target in (engine, engine / "a" / "b" / "camp", alias / "camp"):
                with self.assertRaises(GitExposureError) as caught:
                    refuse_engine_directory(target)
                self.assertIn("outside the engine checkout", str(caught.exception))
                self.assertIsInstance(caught.exception, ValueError)
            refuse_engine_directory(self.base / "engine-sibling")  # prefix lookalike is fine
        self.assertEqual(snapshot(self.base), before)

    def test_cli_init_inside_engine_exits_1_and_writes_nothing(self):
        engine = self.engine()
        before = snapshot(self.base)
        sys.path.insert(0, str(REPO_ROOT))
        from campaign_tool import cli
        argv = ["init", "--directory", str(engine / "camp"), "--county", "X", "--state", "CA", "--name", "N"]
        with mock.patch.object(git_exposure, "engine_checkout_root", return_value=engine), \
                mock.patch.object(sys, "argv", ["campaign_tool", *argv]), \
                mock.patch.object(sys, "stderr"), self.assertRaises(SystemExit) as caught:
            cli.main()
        self.assertEqual(caught.exception.code, 1)
        self.assertEqual(snapshot(self.base), before)

    def test_installed_package_has_no_source_checkout_root(self):
        fake = self.base / "site" / "campaign_tool"
        fake.mkdir(parents=True)
        (fake / "git_exposure.py").write_text("")
        with mock.patch.object(git_exposure, "__file__", str(fake / "git_exposure.py")):
            self.assertIsNone(git_exposure.engine_checkout_root())
        self.assertEqual(git_exposure.engine_checkout_root(), REPO_ROOT)

    def test_resolution_error_refuses_init(self):
        with mock.patch.object(git_exposure, "engine_checkout_root", return_value=self.base), \
                mock.patch.object(Path, "resolve", side_effect=OSError("denied")):
            with self.assertRaises(GitExposureError):
                refuse_engine_directory(self.base / "x")


class RowTests(Base):
    def test_outside_worktree_has_no_row(self):
        for target in (self.base, self.base / "no" / "such" / "dir"):
            with mock.patch.object(git_exposure, "engine_checkout_root", return_value=None):
                self.assertIsNone(git_exposure_row(target))
        report = {"config_readable": True}
        self.assertEqual(checklist(report, "d"), checklist(report, "d", git_row=None))

    def test_each_unignored_path_and_manual_fix(self):
        counter = itertools.count()
        for count in range(4):
            for ignored in itertools.combinations(NAMES, count):
                for sub in ("", "deep/er/camp x'y"):
                    lead = f"/{sub}/" if sub else "/"
                    root = self.tree([lead + n + "/" for n in ignored], name=f"w{next(counter)}")
                    row = git_exposure_row(root / sub)
                    missing = [n for n in NAMES if n not in ignored]
                    if not missing:
                        self.assertEqual((row["state"], row["owner"]), ("ok", None))
                        continue
                    self.assertEqual((row["state"], row["owner"]), ("todo", "organizer"))
                    self.assertEqual(row["label"], "Campaign files may be included in git")
                    self.assertTrue(row["next"].startswith("Affected: " + ", ".join(f"{n}/" for n in missing)))
                    for n in NAMES:
                        self.assertEqual(f"/{n}/" in row["next"], n in missing, (ignored, row["next"]))
                    self.assertIn("separate lines" if len(missing) > 1 else "as a line", row["next"])

    def test_text_json_and_counts_agree(self):
        root = self.tree(["/kit/"])
        report = {"config_readable": True}
        text = render(report, root)
        rows = json.loads(render(report, root, as_json=True))
        self.assertEqual([r["key"] for r in rows if r["key"] == "git_exposure"], ["git_exposure"])
        self.assertEqual(rows[-1]["key"], "production_ready")
        self.assertIn("[todo] Campaign files may be included in git", text)
        self.assertIn("1 to do", text)
        self.assertIn("Production ready: not yet, 1 to do", rows[-1]["label"])

    def test_effective_nested_rules_and_tracked_files(self):
        root = self.tree(["/kit/", "/private/", "/public/"])
        self.assertEqual(git_exposure_row(root)["state"], "ok")
        (root / "private").mkdir()
        (root / "private" / "secret-name.txt").write_text("x")
        (root / ".gitignore").write_text("/kit/\n/public/\n/private/*\n!/private/secret-name.txt\n")
        row = git_exposure_row(root)
        self.assertEqual(row["state"], "todo")
        self.assertIn("Affected: private/.", row["next"])
        self.assertNotIn("secret-name", json.dumps(row))
        (root / ".gitignore").write_text("/kit/\n/private/\n/public/\n")
        self.assertEqual(git_exposure_row(root)["state"], "ok")
        git(root, "add", "-f", "private/secret-name.txt")  # tracked-then-ignored
        row = git_exposure_row(root)
        self.assertEqual(row["state"], "todo")
        self.assertIn("does not untrack existing files", row["next"])
        self.assertNotIn("secret-name", json.dumps(row))

    def test_linked_worktree_git_file_and_engine_precedence(self):
        main = self.tree(["/kit/", "/private/", "/public/"], name="main")
        git(main, "add", ".gitignore")
        git(main, "commit", "-q", "-m", "init")
        linked = self.base / "linked"
        git(main, "worktree", "add", "-q", str(linked), "-b", "side")
        self.assertTrue((linked / ".git").is_file())
        self.assertEqual(git_exposure_row(linked)["state"], "ok")
        with mock.patch.object(git_exposure, "engine_checkout_root", return_value=linked):
            row = git_exposure_row(linked / "camp")
            self.assertEqual(row["label"], "Campaign is inside the engine checkout")
            self.assertTrue(row["next"].startswith("Move the campaign outside the engine checkout "
                                                   "(docs/FORK-AND-RUN.md step 2)."))
            self.assertEqual(row["state"], "todo")

    def test_missing_git_bad_marker_timeout_and_symlink_never_pass(self):
        root = self.tree(["/kit/", "/private/", "/public/"])
        with mock.patch.object(git_exposure.shutil, "which", return_value=None):
            row = git_exposure_row(root)
        self.assertEqual((row["state"], row["label"]),
                         ("todo", "Git exposure: not checked (git is not installed)"))
        self.assertTrue(row["next"].startswith("Install git and rerun doctor --checklist"))
        with mock.patch.object(git_exposure.subprocess, "run", side_effect=subprocess.TimeoutExpired("git", 5)):
            self.assertEqual(git_exposure_row(root)["label"], "Git exposure: not checked")
        with mock.patch.object(git_exposure.subprocess, "run", side_effect=PermissionError("secret path")):
            row = git_exposure_row(root)
            self.assertEqual(row["state"], "todo")
            self.assertNotIn("secret path", json.dumps(row))
        outside = self.base / "outside"
        outside.mkdir()
        (root / "private").symlink_to(outside)
        self.assertEqual(git_exposure_row(root)["label"], "Git exposure: not checked")
        (root / "private").unlink()
        (root / "public").mkdir()
        (root / "public" / "link").symlink_to(outside)
        self.assertEqual(git_exposure_row(root)["label"], "Git exposure: not checked")
        bad = self.base / "bad"
        (bad / ".git").mkdir(parents=True)
        (bad / ".git" / "junk").write_text("x")
        self.assertEqual(git_exposure_row(bad)["state"], "todo")  # invalid repository
        filemark = self.base / "filemark"
        filemark.mkdir()
        (filemark / ".git").write_text("not a gitdir pointer\n")
        self.assertEqual(git_exposure_row(filemark)["label"], "Git exposure: not checked")

    def test_check_is_read_only_and_argv_safe(self):
        root = self.tree(["/kit/", "/public/"])
        (root / "private").mkdir()
        (root / "private" / "a b'$(x).txt").write_text("x")
        before = snapshot(root)
        calls = []
        real = subprocess.run

        def spy(argv, **kw):
            calls.append((argv, kw))
            return real(argv, **kw)

        hostile = {"GIT_DIR": str(self.base / "elsewhere"), "GIT_WORK_TREE": str(self.base),
                   "GIT_INDEX_FILE": str(self.base / "idx"), "GIT_COMMON_DIR": str(self.base)}
        with mock.patch.dict(os.environ, hostile), mock.patch.object(git_exposure.subprocess, "run", spy):
            row = git_exposure_row(root)
        self.assertEqual(row["state"], "todo")
        self.assertEqual(snapshot(root), before)
        self.assertTrue(calls)
        for argv, kw in calls:
            self.assertIsInstance(argv, list)
            self.assertFalse(kw["shell"])
            self.assertEqual(kw["timeout"], 5)
            self.assertFalse([k for k in kw["env"] if k in hostile])
            self.assertEqual(kw["env"]["GIT_OPTIONAL_LOCKS"], "0")
            self.assertTrue(set(argv[3:4]) <= {"rev-parse", "check-ignore", "ls-files"})


if __name__ == "__main__":
    unittest.main()
