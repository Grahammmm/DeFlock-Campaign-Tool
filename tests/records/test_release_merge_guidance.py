"""Execute the release guide's shell blocks against local synthetic Git graphs."""
from pathlib import Path
import re
import subprocess
import tempfile
import unittest


GUIDE = Path(__file__).resolve().parents[2] / "docs" / "RECORDS-RELEASE.md"
IDENTITIES = "Resolve identities and require a single merge base"
MERGE = "Merge forward on the existing PR branch"


class ReleaseMergeGuidanceTests(unittest.TestCase):
    def setUp(self):
        self.guide = GUIDE.read_text(encoding="utf-8")
        self.section = self.guide.split("## Reviewed merge-forward integration\n", 1)[1].split(
            "\n## Acceptance and owner release action", 1
        )[0]
        # tempfile uses the runner's approved scratch root, outside source checkouts.
        self.temporary = tempfile.TemporaryDirectory(prefix="release-merge-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.origin = self.root / "origin"
        self.branch = self.root / "branch"
        self.origin.mkdir()
        self.git(self.origin, "init", "--initial-branch=main")
        self.identity(self.origin)
        self.initial = self.commit(self.origin, "common.txt", "common\n")
        self.git(self.root, "clone", str(self.origin), str(self.branch))
        self.identity(self.branch)
        self.git(self.branch, "switch", "-c", "existing-pr")

    def git(self, cwd, *args, input=None):
        return subprocess.run(
            ["git", *args], cwd=cwd, input=input, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
            timeout=20,
        ).stdout.strip()

    def identity(self, repo):
        # Only disposable fixture config is written, never host identity/config.
        for key, value in (("user.name", "Synthetic Reviewer"),
                           ("user.email", "fixture@example.invalid"),
                           ("commit.gpgsign", "false")):
            self.git(repo, "config", "--local", key, value)

    def commit(self, repo, name, text):
        (repo / name).write_text(text, encoding="utf-8")
        self.git(repo, "add", name)
        self.git(repo, "commit", "--signoff", "-m", name)
        return self.git(repo, "rev-parse", "HEAD")

    def graph_commit(self, *parents):
        tree = self.git(self.origin, "rev-parse", self.initial + "^{tree}")
        args = ["commit-tree", tree]
        for parent in parents:
            args.extend(("-p", parent))
        return self.git(self.origin, *args, input="Synthetic graph node\n")

    def block(self, label):
        match = re.search(r"```sh\n# " + re.escape(label) + r"\n(.*?)```", self.section, re.S)
        if match is None:
            self.fail("missing executable guide block: " + label)
        return match.group(1)

    def run_guide(self, suffix=""):
        script = self.root / "guide.sh"
        script.write_text(self.block(IDENTITIES) + suffix, encoding="utf-8")
        return subprocess.run(
            ["sh", str(script)], cwd=self.branch, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20,
        )

    def identities(self, result):
        self.assertEqual(result.returncode, 0, result.stderr)
        return dict(line.split("=", 1) for line in result.stdout.splitlines()
                    if line.startswith(("BASE=", "HEAD_SHA=", "MERGE_BASE=")))

    def diverge(self):
        head = self.commit(self.branch, "pr.txt", "reviewed PR change\n")
        base = self.commit(self.origin, "main.txt", "reviewed main change\n")
        return base, head

    def test_single_base_records_resolved_full_identities(self):
        base, head = self.diverge()
        self.assertEqual(self.identities(self.run_guide()), {
            "BASE": base, "HEAD_SHA": head, "MERGE_BASE": self.initial,
        })
        self.assertEqual(self.git(self.branch, "rev-parse", "HEAD"), head)

    def test_multiple_merge_bases_hold(self):
        a = self.graph_commit(self.initial)
        # Distinct sibling histories, followed by opposite-parent-order merges.
        # A distinct message prevents commit-tree from recreating the same object.
        tree = self.git(self.origin, "rev-parse", self.initial + "^{tree}")
        b = self.git(self.origin, "commit-tree", tree, "-p", self.initial,
                     input="Other synthetic sibling\n")
        left, right = self.graph_commit(a, b), self.graph_commit(b, a)
        self.git(self.origin, "update-ref", "refs/heads/reviewed-left", left)
        self.git(self.origin, "update-ref", "refs/heads/main", right)
        self.git(self.branch, "fetch", "origin")
        self.git(self.branch, "reset", "--hard", left)
        before = self.git(self.branch, "merge-base", "--all", right, left).splitlines()
        self.assertEqual(set(before), {a, b})
        result = self.run_guide()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("HOLD: expected exactly one merge base", result.stderr)
        self.assertEqual(self.git(self.branch, "rev-parse", "HEAD"), left)
        self.assertEqual(self.git(self.branch, "merge-base", "--all", right, left).splitlines(), before)

    def test_unrelated_histories_hold(self):
        unrelated = self.graph_commit()
        self.git(self.origin, "update-ref", "refs/heads/main", unrelated)
        result = self.run_guide()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("HOLD: merge-base failed or no base", result.stderr)
        self.assertEqual(self.git(self.branch, "rev-parse", "HEAD"), self.initial)

    def test_command_failure_holds(self):
        result = self.run_guide('\ncheck_single_base "$BASE" missing-commit\n')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("HOLD: merge-base failed or no base", result.stderr)
        self.assertEqual(self.git(self.branch, "rev-parse", "HEAD"), self.initial)

    def test_fetch_failure_holds_before_readiness(self):
        self.git(self.branch, "remote", "set-url", "origin", str(self.root / "missing"))
        result = self.run_guide()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("HOLD: fetch failed", result.stderr)
        self.assertNotIn("BASE=", result.stdout)

    def test_merge_forward_preserves_both_parents(self):
        base, head = self.diverge()
        result = self.run_guide(self.block(MERGE))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.git(self.branch, "show", "-s", "--format=%P", "HEAD").split(),
                         [head, base])
        for ancestor in (head, base):
            self.git(self.branch, "merge-base", "--is-ancestor", ancestor, "HEAD")
        self.assertEqual((self.branch / "pr.txt").read_text(), "reviewed PR change\n")
        self.assertEqual((self.branch / "main.txt").read_text(), "reviewed main change\n")
        self.assertIn("Signed-off-by: Synthetic Reviewer <fixture@example.invalid>",
                      self.git(self.branch, "show", "-s", "--format=%B", "HEAD"))
        self.assertEqual(self.git(self.branch, "branch", "--show-current"), "existing-pr")

    def test_new_merge_head_invalidates_old_receipt_identity(self):
        self.diverge()
        old_receipt = self.identities(self.run_guide())
        result = self.run_guide(self.block(MERGE))
        self.assertEqual(result.returncode, 0, result.stderr)
        new_receipt = self.identities(self.run_guide())
        self.assertNotEqual(old_receipt["HEAD_SHA"], new_receipt["HEAD_SHA"])
        self.assertEqual(new_receipt["MERGE_BASE"], new_receipt["BASE"])
        self.assertIn("new head requiring fresh exact-head evidence and fresh review", self.section)

    def test_moved_base_requires_fresh_single_base_check(self):
        base, head = self.diverge()
        old = self.identities(self.run_guide())
        moved = self.commit(self.origin, "later.txt", "later main change\n")
        # The old immutable base is still valid Git history, but no longer current.
        self.assertEqual(old["BASE"], base)
        current = self.identities(self.run_guide())
        self.assertEqual(current, {"BASE": moved, "HEAD_SHA": head, "MERGE_BASE": self.initial})
        self.assertNotEqual(current, old)
        self.assertIn("After every head/base movement", self.section)
        self.assertIn("rerun the identity/single-base block", self.section)

    def test_guidance_preserves_no_rebase_and_no_force_push(self):
        normalized = " ".join(self.section.split())
        for contract in (
            "Never rebase, squash away reviewed history, force-push, or open a replacement integration PR",
            "a clean local merge is not permission to bypass review",
            "an unresolved skip or missing evidence stays HOLD",
            "a merge conflict stays HOLD until resolved and checked",
            "incorporate the **reviewed upstream rework** using a normal merge",
            "name the incorporated commit",
            "Source integration and passing synthetic tests do not grant release authority",
            "all existing signing/trust-anchor, retire/delete, publication/launch and runtime holds remain unchanged",
        ):
            with self.subTest(contract=contract):
                self.assertIn(contract, normalized)
        # These are procedural refusals, not a new approval/receipt implementation.
        for invalid in ("cancelled or expired review is not approval",
                        "duplicate receipts cannot supply missing evidence",
                        "make a stale receipt current"):
            with self.subTest(invalid=invalid):
                self.assertIn(invalid, normalized)


if __name__ == "__main__":
    unittest.main()
