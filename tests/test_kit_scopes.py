"""kit --scope ID narrows drafts to chosen law-package request scopes. Offline, synthetic."""
import contextlib
import hashlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import kit, law  # noqa: E402

PACKAGE = law.load_package("us-ca")
TITLES = {scope["scope_id"]: scope["title"] for scope in PACKAGE["request_scopes"]}
RULES = {scope["scope_id"]: scope["rule_ids"] for scope in PACKAGE["request_scopes"]}


def cli(*arguments):
    return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", *map(str, arguments)],
                          cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)


def rule_lines(text):
    return [line for line in text.split("Rules the scopes relate to:\n", 1)[1].splitlines() if line.startswith("- ")]


class KitScopeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-kit-scopes-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.root = self.tmp / "campaign"
        result = cli("init", "--directory", self.root, "--name", "Synthetic", "--county", "San Luis Obispo",
                     "--state", "CA")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.kit = self.root / "kit"
        self.requests = self.kit / "requests"

    def build(self, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            summary = kit.build_kit(self.root, **kwargs)
        self.out = out.getvalue()
        return summary

    def drafts(self, folder=None):
        return {p.name: p.read_text(encoding="utf-8") for p in sorted((folder or self.requests).glob("*.md"))}

    def tree(self):
        digest = hashlib.sha256()
        for path in sorted(self.root.rglob("*")):
            if path.is_file():
                digest.update(str(path.relative_to(self.root)).encode() + b"\0" + path.read_bytes())
        return digest.hexdigest()

    def agencies(self):
        return {a["agency_id"] + ".md": a for a in json.loads((self.kit / "agencies.json").read_text("utf-8"))}

    def test_no_scope_drafts_match_all_scopes_render(self):
        summary = self.build()
        drafts, agencies = self.drafts(), self.agencies()
        self.assertGreater(len(drafts), 1)
        for name, text in drafts.items():
            self.assertEqual(text, kit.render_combined_request(PACKAGE, agencies[name]), name)
            for title in TITLES.values():
                self.assertIn(f"## {title}\n", text)
        self.assertEqual(summary["scopes"], "all")
        self.assertNotIn("--scope", (self.kit / "README.md").read_text("utf-8"))

    def test_chosen_scopes_in_package_order_with_limited_rules(self):
        summary = self.build(scopes=["policies", "agreements"])
        self.assertEqual(summary["scopes"], ["agreements", "policies"])
        drafts = self.drafts()
        self.assertGreater(len(drafts), 1)
        expected_rules = list(dict.fromkeys(RULES["agreements"] + RULES["policies"]))
        for name, text in drafts.items():
            first, second = text.index(f"## {TITLES['agreements']}\n"), text.index(f"## {TITLES['policies']}\n")
            self.assertLess(first, second, name)
            for other in ("sharing", "audit-logs", "compliance", "hearing-records"):
                self.assertNotIn(TITLES[other], text, name)
            self.assertEqual([line[2:].split(":", 1)[0] for line in rule_lines(text)], expected_rules, name)
        self.assertNotIn("ca-civ-1798.90.52-access-records", "".join(drafts.values()))
        self.assertIn("--scope`: agreements, policies.", (self.kit / "README.md").read_text("utf-8"))

    def test_duplicate_scope_gives_one_section(self):
        summary = self.build(scopes=["agreements", "agreements"])
        self.assertEqual(summary["scopes"], ["agreements"])
        for name, text in self.drafts().items():
            self.assertEqual(text.count(f"## {TITLES['agreements']}\n"), 1, name)
            self.assertEqual(text.count("\n## "), 2, name)  # the scope plus the statutory-context heading

    def test_unknown_scope_refused_before_any_write(self):
        before = self.tree()
        with self.assertRaises(ValueError) as caught:
            self.build(scopes=["agreements", "nope"])
        self.assertIn("'nope'", str(caught.exception))
        for scope_id in TITLES:
            self.assertIn(scope_id, str(caught.exception))
        self.assertFalse(self.kit.exists())
        self.assertEqual(self.tree(), before)

    def test_unknown_scope_leaves_existing_kit_unchanged(self):
        self.build(scopes=["agreements"])
        before = self.tree()
        with self.assertRaises(ValueError):
            self.build(scopes=["Agreements"])
        self.assertEqual(self.tree(), before)

    def test_cli_scope_and_stopped_message(self):
        result = cli("kit", "--directory", self.root, "--scope", "nope")
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(result.stderr.startswith("Stopped: Unknown request scope 'nope'"), result.stderr)
        self.assertIn("hearing-records", result.stderr)
        self.assertFalse(self.kit.exists())
        result = cli("kit", "--directory", self.root, "--scope", "policies", "--scope", "agreements")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"scopes": [\n    "agreements",\n    "policies"\n  ]', result.stdout)
        self.assertEqual(json.loads((self.kit / "summary.json").read_text("utf-8"))["scopes"],
                         ["agreements", "policies"])

    def test_scope_without_law_package_refused_before_writes(self):
        empty_base = self.tmp / "empty-law-base"
        empty_base.mkdir()
        before = self.tree()
        with self.assertRaises(ValueError) as caught:
            self.build(law_base=empty_base, scopes=["agreements"])
        self.assertEqual(str(caught.exception), "--scope needs a law package; none found for us-ca")
        self.assertFalse(self.kit.exists())
        self.assertEqual(self.tree(), before)
        self.assertEqual(self.build(law_base=empty_base)["scopes"], "all")  # no --scope: template drafts as before
        before = self.tree()
        with self.assertRaises(ValueError):
            self.build(law_base=empty_base, scopes=["agreements"])
        self.assertEqual(self.tree(), before)

    def test_switching_scope_keeps_edited_and_refreshes_unedited(self):
        self.build(scopes=["agreements"])
        names = sorted(self.drafts())
        self.assertGreater(len(names), 1)
        edited_name = names[0]
        edited = self.requests / edited_name
        edited_text = edited.read_text("utf-8") + "\nOrganizer note: synthetic edit.\n"
        edited.write_text(edited_text, encoding="utf-8")
        summary = self.build(scopes=["policies"])
        self.assertEqual(summary["drafts_kept_edited"], 1)
        self.assertEqual(edited.read_text("utf-8"), edited_text)
        self.assertIn(f"kept edited draft requests/{edited_name}", self.out)
        fresh = self.drafts(self.kit / "requests.suggested")
        self.assertEqual(sorted(fresh), [edited_name])
        self.assertIn(TITLES["policies"], fresh[edited_name])
        self.assertNotIn(TITLES["agreements"], fresh[edited_name])
        agencies = self.drafts()
        for name in names[1:]:
            self.assertEqual(agencies[name], kit.render_combined_request(
                PACKAGE, self.agencies()[name], scopes=["policies"]), name)
        summary = self.build()
        self.assertEqual(edited.read_text("utf-8"), edited_text)
        for name in names[1:]:
            self.assertEqual(self.drafts()[name], kit.render_combined_request(PACKAGE, self.agencies()[name]), name)


if __name__ == "__main__":
    unittest.main()
