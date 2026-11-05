"""Run README "Start locally" literally from a clean copy of the tracked files, offline."""
import hashlib
import os
import posixpath
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
GUARD = "tests/fixtures/no_network_run.py"
PYTHON_M = ["python3", "-m", "campaign_tool"]


class ColdStartError(AssertionError):
    pass


def tracked_files(root):
    if shutil.which("git") is None:
        raise unittest.SkipTest("git is not available; cannot list tracked files")
    done = subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True, timeout=60)
    if done.returncode != 0:
        raise unittest.SkipTest("not a git checkout; cannot list tracked files")
    names = [n.decode() for n in done.stdout.split(b"\0") if n]
    return [n for n in names if (Path(root) / n).is_file()]


def copy_tracked(root, dest):
    for name in tracked_files(root):
        target = Path(dest) / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(Path(root) / name, target)


def snapshot(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in Path(root).rglob("*") if p.is_file()}


def readme_commands(text):
    """Commands of the first sh block under '## Start locally'; ColdStartError if it drifted."""
    match = re.search(r"^## Start locally\s*$(.*?)(?=^## |\Z)", text, re.M | re.S)
    if match is None:
        raise ColdStartError("README has no '## Start locally' heading")
    block = re.search(r"```sh\n(.*?)```", match.group(1), re.S)
    if block is None:
        raise ColdStartError("README 'Start locally' has no sh block")
    lines = [line.strip() for line in block.group(1).splitlines() if line.strip()]
    for needed in ("git clone ", "cd DeFlock-Campaign-Tool", "python3 -m http.server "):
        if not any(line.startswith(needed) for line in lines):
            raise ColdStartError("README block lost the line starting %r" % needed)
    kept = [line.replace('"Your County"', '"Mono"') for line in lines
            if not line.startswith(("git clone ", "cd ", "python3 -m http.server "))]
    if not kept:
        raise ColdStartError("README block has no campaign_tool commands")
    return kept


def run_block(readme_text, sandbox):
    """Run each README line from sandbox/DeFlock-Campaign-Tool; ColdStartError names a failing line."""
    clone, home = Path(sandbox) / "DeFlock-Campaign-Tool", Path(sandbox) / "home"
    home.mkdir()
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home), "PYTHONDONTWRITEBYTECODE": "1"}
    for line in readme_commands(readme_text):
        words = shlex.split(line, comments=True)
        if words[:3] != PYTHON_M:
            raise ColdStartError("README line is not a campaign_tool command: " + line)
        argv = [sys.executable, "-B", "-W", "error::ResourceWarning", GUARD, *words[3:]]
        done = subprocess.run(argv, cwd=clone, env=env, capture_output=True, text=True, timeout=120)
        if done.returncode != 0:
            raise ColdStartError("README line failed (exit %d): %s\n%s" % (
                done.returncode, line, done.stdout + done.stderr))
        if "NETWORK_FORBIDDEN" in done.stderr:
            raise ColdStartError("README line attempted a network connect: " + line)


def lint_doc(text, tracked, subcommands, doc="docs/FORK-AND-RUN.md"):
    problems = []
    for target in re.findall(r"\]\(([^)\s]+)\)", text):
        path = target.split("#")[0]
        if path and not re.match(r"[a-z]+:", path) and \
                posixpath.normpath(posixpath.join(posixpath.dirname(doc), path)) not in tracked:
            problems.append("link does not resolve to a tracked file: " + target)
    for sub in re.findall(r"python3 -m campaign_tool ([a-z][a-z-]*)", text):
        if sub not in subcommands:
            problems.append("unknown campaign_tool subcommand: " + sub)
    return problems


class ColdStartReadmeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="cold-start-")
        self.addCleanup(temporary.cleanup)
        self.sandbox = Path(temporary.name)
        self.clone = self.sandbox / "DeFlock-Campaign-Tool"
        copy_tracked(REPO, self.clone)
        self.readme = (self.clone / "README.md").read_text(encoding="utf-8")

    def test_readme_block_runs_offline_on_clean_copy_and_builds_site(self):
        before = snapshot(self.clone)
        run_block(self.readme, self.sandbox)
        campaign = self.sandbox / "my-campaign"
        self.assertTrue((campaign / "public" / "index.html").is_file())
        self.assertIn("Mono", (campaign / "public" / "index.html").read_text(encoding="utf-8"))
        self.assertTrue(snapshot(self.clone) == before, "the copied repo tree changed during the run")
        self.assertTrue(before and not any(n.startswith((".git/", "private/", "kit/")) for n in before))

    def test_kit_writes_drafts_only_under_campaign_kit_and_sends_nothing(self):
        run_block(self.readme, self.sandbox)
        campaign = self.sandbox / "my-campaign"
        self.assertEqual({p.name for p in self.sandbox.iterdir()},
                         {"DeFlock-Campaign-Tool", "home", "my-campaign"})
        self.assertEqual(list((self.sandbox / "home").iterdir()), [])
        self.assertTrue(any((campaign / "kit" / "requests").glob("*.md")))
        names = [p.name.lower() for tree in (campaign, self.sandbox / "home") for p in tree.rglob("*")
                 if p.is_file()]
        self.assertFalse([n for n in names if "outbox" in n or n.endswith((".sqlite", ".db"))])
        self.assertIn('"sent": false', (campaign / "kit" / "summary.json").read_text(encoding="utf-8"))

    def test_network_guard_is_active(self):
        done = subprocess.run([sys.executable, "-B", GUARD, "--guard-negative-control"],
                              cwd=self.clone, capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 97, done.stdout + done.stderr)
        self.assertIn("NETWORK_FORBIDDEN", done.stderr)

    def test_untracked_developer_files_are_not_copied(self):
        source, dest = self.sandbox / "dev", self.sandbox / "copy"
        source.mkdir()
        env = {"PATH": os.environ.get("PATH", ""), "HOME": str(self.sandbox)}
        subprocess.run(["git", "init", "-q"], cwd=source, env=env, check=True, timeout=60)
        (source / "tracked.txt").write_text("t", encoding="utf-8")
        (source / "private").mkdir()
        (source / "private" / "stray.txt").write_text("secret", encoding="utf-8")
        subprocess.run(["git", "add", "tracked.txt"], cwd=source, env=env, check=True, timeout=60)
        copy_tracked(source, dest)
        self.assertEqual(sorted(snapshot(dest)), ["tracked.txt"])

    def test_drifted_readme_fails_naming_the_line(self):
        cases = {
            "no heading": (self.readme.replace("## Start locally", "## Quick start"), "no '## Start locally'"),
            "no sh block": (self.readme.replace("```sh", "```text", 1), "no sh block"),
            "clone line lost": (self.readme.replace("git clone https", "git fetch https"), "git clone "),
            "serve line lost": (self.readme.replace("python3 -m http.server", "python3 -m serve"), "http.server"),
            "renamed command": (self.readme.replace("campaign_tool kit --directory", "campaign_tool kitt --directory"),
                                "campaign_tool kitt --directory"),
            "not our tool": (self.readme.replace("python3 -m campaign_tool doctor --directory ../my-campaign\n",
                                                 "echo hi\n", 1), "echo hi"),
        }
        for label, (text, expected) in cases.items():
            with self.subTest(label), tempfile.TemporaryDirectory(prefix="cold-start-bad-") as work:
                shutil.copytree(self.clone, Path(work) / "DeFlock-Campaign-Tool")
                with self.assertRaises(ColdStartError) as caught:
                    run_block(text, work)
                self.assertIn(expected, str(caught.exception))

    def test_fork_doc_links_and_commands_resolve(self):
        text = (REPO / "docs" / "FORK-AND-RUN.md").read_text(encoding="utf-8")
        self.assertLessEqual(len(text.splitlines()), 90)
        helped = subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "--help"], cwd=REPO,
                                capture_output=True, text=True, timeout=60).stdout
        found = re.search(r"\{([a-z,-]+)\}", helped)
        self.assertIsNotNone(found, helped)
        subcommands, tracked = set(found.group(1).split(",")), set(tracked_files(REPO))
        self.assertIn("kit-verify-agency", subcommands)
        self.assertEqual(lint_doc(text, tracked, subcommands), [])
        bad = text + "\n[x](nope.md) `python3 -m campaign_tool frobnicate`\n"
        self.assertEqual(len(lint_doc(bad, tracked, subcommands)), 2)
        self.assertIn("docs/FORK-AND-RUN.md", (REPO / "CONTRIBUTING.md").read_text(encoding="utf-8"))

    def test_readme_links_the_findings_guide_outside_start_locally(self):
        start = re.search(r"^## Start locally\s*$(.*?)(?=^## |\Z)", self.readme, re.M | re.S).group(1)
        self.assertNotIn("FINDINGS-GUIDE", start)
        self.assertEqual(self.readme.count("](docs/FINDINGS-GUIDE.md)"), 1)
        self.assertTrue((self.clone / "docs" / "FINDINGS-GUIDE.md").is_file())
        self.assertIn("](docs/REVIEW-GUIDE.md)", self.readme)  # the reviewer-side link stays
        self.assertEqual(lint_doc("[g](docs/FINDINGS-GUIDE.md)", set(tracked_files(REPO)), set(), doc="README.md"), [])
        self.assertEqual(lint_doc("[x](docs/NO-SUCH-GUIDE.md)", set(tracked_files(REPO)), set(), doc="README.md"),
                         ["link does not resolve to a tracked file: docs/NO-SUCH-GUIDE.md"])

    def test_readme_links_the_ordinance_checklist_once_outside_start_locally(self):
        start = re.search(r"^## Start locally\s*$(.*?)(?=^## |\Z)", self.readme, re.M | re.S).group(1)
        self.assertNotIn("ORDINANCE-CHECKLIST", start)
        self.assertEqual(self.readme.count("](docs/ORDINANCE-CHECKLIST.md)"), 1)
        self.assertIn("docs/ORDINANCE-CHECKLIST.md", tracked_files(REPO))
        self.assertEqual(lint_doc("[o](docs/ORDINANCE-CHECKLIST.md)", set(tracked_files(REPO)), set(), doc="README.md"), [])
        self.assertEqual(lint_doc("[o](docs/NO-SUCH-CHECKLIST.md)", set(tracked_files(REPO)), set(), doc="README.md"),
                         ["link does not resolve to a tracked file: docs/NO-SUCH-CHECKLIST.md"])


if __name__ == "__main__":
    unittest.main()
