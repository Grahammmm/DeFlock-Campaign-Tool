"""git exposure row and engine-local init refusal. Temporary synthetic repos only; nothing touches the network."""

import io
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

    def test_cli_checklist_expands_tilde_directory(self):
        root = self.tree([])
        from campaign_tool import cli
        init = ["init", "--directory", str(root / "campaign"), "--county", "X", "--state", "CA", "--name", "N"]
        with mock.patch.object(git_exposure, "engine_checkout_root", return_value=None), \
                mock.patch.object(sys, "argv", ["campaign_tool", *init]), mock.patch.object(sys, "stdout"):
            cli.main()
        argv = ["doctor", "--directory", "~/campaign", "--checklist"]
        out = io.StringIO()
        outside = self.base / "outside"  # cwd outside any work tree: only the expanded path reveals the repo
        outside.mkdir()
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(outside)
        with mock.patch.dict(os.environ, {"HOME": str(root)}), \
                mock.patch.object(git_exposure, "engine_checkout_root", return_value=None), \
                mock.patch.object(sys, "argv", ["campaign_tool", *argv]), \
                mock.patch.object(sys, "stdout", out), mock.patch.object(sys, "stderr"):
            try:
                cli.main()
            except SystemExit:
                pass
        self.assertIn("Campaign files may be included in git", out.getvalue())

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


OK_ROW = {"key": "git_exposure", "state": "ok", "label": "Campaign kit/, private/ and public/ are ignored by git",
          "next": None, "owner": None}
NOTE = ".gitignore does not untrack existing files; review tracked files manually before any push."
NESTED = "deep/camp [x]'s"


def tracked_row(affected, advice):
    return {"key": "git_exposure", "state": "todo", "label": "Campaign files may be included in git",
            "next": f"Affected: {affected}. {advice}, then rerun doctor --checklist. {NOTE}", "owner": "organizer"}


def one_line(name):
    return tracked_row(f"{name}/", f"Add /{name}/ as a line in the campaign directory's .gitignore")


class IndexedRootTests(Base):
    """A git index entry exactly at kit, private or public is tracked exposure, whatever the work tree holds."""

    def checked(self, root, campaign):
        """Row from two checks that leave the repository, its index and the campaign byte-identical."""
        before = snapshot(root)
        calls = []
        real = subprocess.run

        def spy(argv, **kw):
            calls.append(argv[3])
            return real(argv, **kw)

        with mock.patch.object(git_exposure.subprocess, "run", spy):
            row = git_exposure_row(campaign)
            self.assertEqual(git_exposure_row(campaign), row)
        self.assertEqual(snapshot(root), before)
        self.assertTrue(set(calls) <= {"rev-parse", "check-ignore", "ls-files"}, calls)
        return row

    def indexed_root(self, name, sub, label):
        root = self.tree(list(NAMES), name=label)  # unanchored: the root file itself is ignored
        campaign = root / sub if sub else root
        campaign.mkdir(parents=True, exist_ok=True)
        (campaign / name).write_text("synthetic\n")
        self.assertEqual(self.checked(root, campaign), OK_ROW)  # untracked and ignored: still fine
        git(root, "add", "-f", f"{sub}/{name}" if sub else name)
        self.assertEqual(git(root, "ls-files", "--cached", "-z").split("\0")[0], f"{sub}/{name}" if sub else name)
        return root, campaign

    def test_indexed_protected_root_files_are_exposed(self):
        for index, (name, sub) in enumerate(itertools.product(NAMES, ("", NESTED))):
            root, campaign = self.indexed_root(name, sub, f"r{index}")
            row = self.checked(root, campaign)
            self.assertEqual(row, one_line(name), (name, sub))
            self.assertNotIn("deep", json.dumps(row))
            self.assertNotIn("synthetic", json.dumps(row))

    def test_indexed_root_survives_worktree_deletion_or_directory_replacement(self):
        for index, (name, sub) in enumerate(itertools.product(NAMES, ("", NESTED))):
            root, campaign = self.indexed_root(name, sub, f"d{index}")
            (campaign / name).unlink()
            self.assertEqual(self.checked(root, campaign), one_line(name), ("absent", name, sub))
            (campaign / name).mkdir()
            (campaign / name / "child.txt").write_text("synthetic\n")
            self.assertEqual(self.checked(root, campaign), one_line(name), ("directory", name, sub))
            git(root, "rm", "-q", "--cached", f"{sub}/{name}" if sub else name)  # fixture setup, never the checker
            self.assertEqual(self.checked(root, campaign), OK_ROW, ("untracked", name, sub))

    def test_exact_and_descendant_index_entries_share_stable_order(self):
        root = self.tree(["kit", "private"])
        (root / "private").write_text("synthetic\n")
        (root / "kit").mkdir()
        (root / "kit" / "a.txt").write_text("synthetic\n")
        git(root, "add", "-f", "private", "kit/a.txt")
        (root / "private").unlink()
        advice = "Add /kit/, /private/ and /public/ as separate lines in the campaign directory's .gitignore"
        self.assertEqual(self.checked(root, root), tracked_row("kit/, private/, public/", advice))

    def test_index_root_boundary_does_not_match_lookalikes(self):
        root = self.tree(["/camp/kit/", "/camp/private/", "/camp/public/"])
        campaign = root / "camp"
        for rel in ("camp/private-old", "camp/private.txt", "camp/kitten", "camp/publicity", "camp-old/private",
                    "camp-old/kit/a.txt", "notes.txt"):
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text("synthetic\n")
        git(root, "add", ".")
        self.assertEqual(self.checked(root, campaign), OK_ROW)
        (campaign / "private").mkdir()
        (campaign / "private" / "child.txt").write_text("synthetic\n")
        git(root, "add", "-f", "camp/private/child.txt")  # positive control: a tracked descendant
        self.assertEqual(self.checked(root, campaign), one_line("private"))

    def test_exact_root_exposure_reaches_text_json_and_counts(self):
        root, campaign = self.indexed_root("public", NESTED, "render")
        (campaign / "public").unlink()
        report = {"config_readable": True}
        text = render(report, campaign)
        rows = json.loads(render(report, campaign, as_json=True))
        self.assertEqual([r for r in rows if r["key"] == "git_exposure"], [one_line("public")])
        self.assertIn("[todo] Campaign files may be included in git", text)
        self.assertIn("→ " + one_line("public")["next"] + " (organizer)", text)
        self.assertIn("1 to do", text)
        self.assertEqual(rows[-1]["label"],
                         "Production ready: not yet, 1 to do; ready only when every todo above is done")
        self.assertNotIn("deep", text + json.dumps(rows))
        git(root, "rm", "-q", "--cached", f"{NESTED}/public")
        healthy = json.loads(render(report, campaign, as_json=True))
        self.assertEqual([r for r in healthy if r["key"] == "git_exposure"], [OK_ROW])
        self.assertEqual(healthy[-1]["label"], "Production ready: no todo remains above; a person still decides")


NESTED_LABEL = "Git exposure: not checked"
NESTED_ADVICE = ("Campaign is inside nested git work trees; review tracked files in every enclosing work tree manually "
                 "before any push. Moving files does not untrack existing entries. Rerun doctor --checklist from a "
                 "campaign outside nested work trees.")
NESTED_ROW = {"key": "git_exposure", "state": "todo", "label": NESTED_LABEL, "next": NESTED_ADVICE,
              "owner": "organizer"}
GENERIC_ROW = {"key": "git_exposure", "state": "todo", "label": NESTED_LABEL, "owner": "organizer",
               "next": "Rerun doctor --checklist once git can read this work tree; "
                       "do not commit campaign files until checked."}


class NestedWorkTreeTests(Base):
    """An inner work tree must never vouch for protected paths when an enclosing git marker exists."""

    def inner(self, path, ignored=NAMES):
        path.mkdir(parents=True, exist_ok=True)
        git(path, "init", "-q")
        (path / ".gitignore").write_text("".join(f"/{n}/\n" for n in ignored))
        return path

    def no_git_calls(self):
        return mock.patch.object(git_exposure.subprocess, "run", side_effect=AssertionError("git was asked"))

    def test_nested_repository_cannot_hide_outer_tracked_private_files(self):
        for index, (name, sub, deleted) in enumerate(itertools.product(NAMES, ("campaign", f"a b/{NESTED}"),
                                                                       (False, True))):
            outer = self.tree([], name=f"outer{index}")
            camp = outer / sub
            (camp / name).mkdir(parents=True)
            (camp / name / "synthetic.txt").write_text("synthetic\n")
            git(outer, "add", "-f", f"{sub}/{name}/synthetic.txt")
            if deleted:
                (camp / name / "synthetic.txt").unlink()
            self.inner(camp)
            self.assertEqual(git(camp, "ls-files").strip(), "")  # inner index is empty and fully ignored
            self.assertEqual(git(outer, "ls-files", "--cached").strip(), f"{sub}/{name}/synthetic.txt")
            self.assertEqual(git_exposure_row(camp), NESTED_ROW, (name, sub, deleted))
            self.assertEqual(git_exposure_row(camp / "kit" / "deeper"), NESTED_ROW)

    def test_nested_marker_refusal_does_not_depend_on_exposure_or_git_installation(self):
        outer = self.tree([], name="outer")
        for ignored in (NAMES, ()):
            camp = self.inner(outer / f"camp{len(ignored)}", ignored)
            with self.no_git_calls():
                self.assertEqual(git_exposure_row(camp), NESTED_ROW)
            with self.no_git_calls(), mock.patch.object(git_exposure.shutil, "which", return_value=None):
                self.assertEqual(git_exposure_row(camp), NESTED_ROW)
        absent = outer / "camp3" / "not" / "created" / "yet"
        with self.no_git_calls():
            self.assertEqual(git_exposure_row(absent), NESTED_ROW)
        three = self.inner(outer / "mid" / "campaign")
        self.inner(outer / "mid")
        with self.no_git_calls():
            self.assertEqual(git_exposure_row(three), NESTED_ROW)
        single = self.tree(list(NAMES), name="single")
        with mock.patch.object(git_exposure.shutil, "which", return_value=None):
            self.assertEqual(git_exposure_row(single)["label"], "Git exposure: not checked (git is not installed)")

    def test_pointer_and_directory_marker_combinations_are_conservative(self):
        outer = self.tree(list(NAMES), name="outer")
        pointer = outer / "sub" / "camp"
        pointer.mkdir(parents=True)
        (pointer / ".git").write_text("gitdir: ../../.git/modules/camp\n")  # submodule-style, never followed
        with self.no_git_calls():
            self.assertEqual(git_exposure_row(pointer), NESTED_ROW)
        main = self.tree(list(NAMES), name="main")
        git(main, "add", ".gitignore")
        git(main, "commit", "-q", "-m", "init")
        inside = outer / "linked"
        git(main, "worktree", "add", "-q", str(inside), "-b", "inside")
        self.assertTrue((inside / ".git").is_file())
        self.assertEqual(git_exposure_row(inside), NESTED_ROW)
        beside = self.base / "beside"
        git(main, "worktree", "add", "-q", str(beside), "-b", "beside")
        self.assertEqual(git_exposure_row(beside)["state"], "ok")  # sibling checkout is not an ancestor
        sibling = self.inner(self.base / "sibling", NAMES)
        self.assertEqual(git_exposure_row(sibling)["state"], "ok")

    def test_outer_bad_marker_is_not_hidden_by_valid_inner_marker(self):
        def layout(label, make_bad):
            outer = self.base / label
            outer.mkdir()
            make_bad(outer / ".git")
            return self.inner(outer / "camp")

        def junk(marker):
            marker.write_text("not a gitdir pointer\n")

        def link(marker):
            (marker.parent / "real").mkdir()
            marker.symlink_to(marker.parent / "real")

        def fifo(marker):
            os.mkfifo(marker)

        for label, make in (("junk", junk), ("link", link), ("fifo", fifo)):
            camp = layout(label, make)
            with self.no_git_calls():
                self.assertEqual(git_exposure_row(camp), GENERIC_ROW, label)
        outer = self.base / "perm"
        outer.mkdir()
        (outer / ".git").write_text("gitdir: elsewhere\n")
        camp = self.inner(outer / "camp")
        real_open = os.open

        def deny(path, *a, **kw):
            if Path(path) == outer / ".git":
                raise PermissionError("synthetic denial")
            return real_open(path, *a, **kw)

        with mock.patch.object(git_exposure.os, "open", deny), self.no_git_calls():
            self.assertEqual(git_exposure_row(camp), GENERIC_ROW)
        stat = self.base / "stat"
        camp = self.inner(stat / "camp")
        real_lstat = os.lstat

        def lstat(path, *a, **kw):
            if Path(path) == stat / ".git":
                raise PermissionError("synthetic denial")
            return real_lstat(path, *a, **kw)

        with mock.patch.object(git_exposure.os, "lstat", lstat), self.no_git_calls():
            self.assertEqual(git_exposure_row(camp), GENERIC_ROW)

    def test_marker_read_stays_bounded(self):
        outer = self.tree([], name="outer")
        camp = self.base / "outer" / "pointer"
        camp.mkdir()
        (camp / ".git").write_bytes(b"gitdir:" + b"x" * 100000)
        reads = []
        real_read = os.read

        def spy(fd, size):
            reads.append(size)
            return real_read(fd, size)

        with mock.patch.object(git_exposure.os, "read", spy):
            self.assertEqual(git_exposure_row(camp), NESTED_ROW)
        self.assertEqual(reads, [512])
        self.assertTrue(outer.is_dir())

    def test_nested_refusal_reaches_text_json_counts_and_engine_precedence(self):
        outer = self.tree([], name="outer")
        camp = self.inner(outer / "camp")
        report = {"config_readable": True}
        text = render(report, camp)
        rows = json.loads(render(report, camp, as_json=True))
        self.assertEqual([r for r in rows if r["key"] == "git_exposure"], [NESTED_ROW])
        self.assertIn("[todo] Git exposure: not checked", text)
        self.assertIn("→ " + NESTED_ADVICE + " (organizer)", text)
        self.assertIn("1 to do", text)
        self.assertEqual(rows[-1]["label"],
                         "Production ready: not yet, 1 to do; ready only when every todo above is done")
        with mock.patch.object(git_exposure, "engine_checkout_root", return_value=camp), self.no_git_calls():
            row = git_exposure_row(camp / "kit")
        self.assertEqual(row["label"], "Campaign is inside the engine checkout")
        self.assertEqual(row["next"], "Move the campaign outside the engine checkout (docs/FORK-AND-RUN.md step 2). "
                         + NESTED_ADVICE)
        self.assertEqual(row["state"], "todo")

    def test_nested_warning_contains_no_paths_or_git_payload(self):
        outer = self.tree([], name="outer-zq-sentinel")
        camp = self.inner(outer / "private-zq-sentinel" / "camp")
        (camp / "private").mkdir()
        (camp / "private" / "zq-secret-name.txt").write_text("zq-secret-content")
        (camp / ".git").rename(camp / ".gitold")
        (camp / ".git").write_text("gitdir: /zq-pointer-target\n")
        report = {"config_readable": True}
        shown = json.dumps(git_exposure_row(camp)) + render(report, camp) + render(report, camp, as_json=True)
        self.assertIn(NESTED_ADVICE, shown)
        for sentinel in ("zq-sentinel", "zq-secret", "zq-pointer", "gitdir"):
            self.assertNotIn(sentinel, shown)

    def test_nested_checks_are_repeatable_and_read_only(self):
        outer = self.tree([], name="outer")
        (outer / "camp" / "private").mkdir(parents=True)
        (outer / "camp" / "private" / "a.txt").write_text("synthetic\n")
        git(outer, "add", "-f", "camp/private/a.txt")
        camp = self.inner(outer / "camp")
        before = snapshot(self.base)
        report = {"config_readable": True}
        with self.no_git_calls():
            for _ in range(2):
                self.assertEqual(git_exposure_row(camp), NESTED_ROW)
                render(report, camp)
                render(report, camp, as_json=True)
        self.assertEqual(snapshot(self.base), before)


if __name__ == "__main__":
    unittest.main()
