"""RF0211-5: an explicit empty --manifest operand is a usage refusal, never "no --manifest". Synthetic; nothing is sent."""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool.newsletter import __main__ as cli  # noqa: E402

DIAGNOSTIC = "argument --manifest: expected a non-empty path"
DRAFT_FILES = ("newsletter-draft.html", "newsletter-draft.txt", "newsletter-draft.json")
BOUNDARIES = ("read_config", "load_manifest", "manifest_from_content", "last_sent", "check_draft",
              "build_draft", "write_draft", "record_sent", "publish_issue")
EMPTY_FORMS = (["--manifest", ""], ["--manifest="], ["--man="])


def manifest(name="Synthetic"):
    return {"schema_version": 1, "campaign": {"name": name, "base_url": None, "county_name": "Synthetic"},
            "since": None, "findings": [], "meetings": []}


class ManifestOperandTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-newsletter-operand-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.root = self.tmp / "campaign"
        subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "init", "--directory", str(self.root), "--name",
                        "Synthetic", "--county", "Synthetic", "--state", "CA"], cwd=REPO_ROOT, check=True,
                       capture_output=True, timeout=30)
        self.kit = self.root / "kit"
        self.kit.mkdir(exist_ok=True)
        for index, name in enumerate(DRAFT_FILES):
            (self.kit / name).write_bytes(b"synthetic canary %d\n" % index)

    def tree(self):
        out = {}
        for path in sorted(self.root.rglob("*")):
            info = path.lstat()
            out[str(path.relative_to(self.root))] = (info.st_mode, info.st_ino, info.st_mtime_ns,
                                                     path.read_bytes() if path.is_file() else None)
        return out

    def run_main(self, *argv):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                code = cli.main(list(argv))
            except SystemExit as exc:
                code = exc.code
        return code, stdout.getvalue(), stderr.getvalue()

    def assert_usage_refusal(self, code, out, err):
        self.assertEqual((code, out), (2, ""), err)
        self.assertIn(DIAGNOSTIC, err)
        self.assertIn("usage:", err)
        self.assertNotIn("Stopped:", err)
        self.assertNotIn("Traceback", err)

    def test_empty_manifest_never_replaces_existing_draft(self):
        before = self.tree()
        for form in EMPTY_FORMS[:2]:
            proc = subprocess.run([sys.executable, "-B", "-m", "campaign_tool.newsletter", "--directory",
                                   str(self.root), *form], cwd=REPO_ROOT, capture_output=True, text=True, timeout=30)
            self.assert_usage_refusal(proc.returncode, proc.stdout, proc.stderr)
            self.assertEqual(self.tree(), before)
        bare = self.tmp / "no-kit"
        bare.mkdir()
        gone = self.tmp / "missing-root"
        for root in (bare, gone):
            proc = subprocess.run([sys.executable, "-B", "-m", "campaign_tool.newsletter", "--directory", str(root),
                                   "--manifest", ""], cwd=REPO_ROOT, capture_output=True, text=True, timeout=30)
            self.assert_usage_refusal(proc.returncode, proc.stdout, proc.stderr)
        self.assertEqual(list(bare.iterdir()), [])
        self.assertFalse(gone.exists())

    def test_empty_manifest_is_usage_error_before_any_campaign_access(self):
        for form in EMPTY_FORMS:
            with contextlib.ExitStack() as stack:
                hooks = [stack.enter_context(mock.patch.object(cli, name, side_effect=AssertionError(name)))
                         for name in BOUNDARIES]
                expand = stack.enter_context(mock.patch.object(Path, "expanduser", side_effect=AssertionError("expanduser")))
                resolve = stack.enter_context(mock.patch.object(Path, "resolve", side_effect=AssertionError("resolve")))
                result = self.run_main("--directory", str(self.tmp / "missing-root"), *form)
            self.assert_usage_refusal(*result)
            self.assertFalse(any(h.called for h in hooks) or expand.called or resolve.called)

    def test_empty_manifest_cannot_hide_in_other_modes(self):
        modes = (["--check"], ["--check", "--json"], ["sent", "--date", "2026-01-02"],
                 ["archive", "--date", "2026-01-02"], ["--record-sent", "2026-01-02"], ["--site", str(self.tmp)],
                 ["--from-content"], [])
        before = self.tree()
        for mode in modes:
            for form in (["--manifest", ""], ["--manifest="]):
                for argv in (mode + form, form + mode):
                    with self.subTest(argv=argv), contextlib.ExitStack() as stack:
                        hooks = [stack.enter_context(mock.patch.object(cli, name, side_effect=AssertionError(name)))
                                 for name in BOUNDARIES]
                        code, out, err = self.run_main("--directory", str(self.root), *argv)
                        self.assertEqual((code, out), (2, ""), err)
                        self.assertNotIn("Stopped:", err)
                        self.assertNotIn("Traceback", err)
                        self.assertFalse(any(h.called for h in hooks))
        self.assertEqual(self.tree(), before)

    def test_each_repeated_manifest_operand_is_validated(self):
        good = self.tmp / "good.json"
        good.write_text(json.dumps(manifest()))
        before = self.tree()
        for argv in (["--manifest", "", "--manifest", str(good)], ["--manifest", str(good), "--manifest", ""]):
            with self.subTest(argv=argv):
                self.assert_usage_refusal(*self.run_main("--directory", str(self.root), *argv))
        self.assertEqual(self.tree(), before)
        code, out, err = self.run_main("--directory", str(self.root), "--manifest", str(self.tmp / "missing.json"),
                                       "--manifest", str(good))
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["source"], "manifest")
        code, out, err = self.run_main("--directory", str(self.root), "--manifest")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("expected one argument", err)

    def test_literal_nonempty_paths_keep_exact_meaning(self):
        names = ("with space.json", " leading.json", "trailing.json ", "café-文書.json", "-dash.json", " ")
        for name in names:
            with self.subTest(name=name):
                self.assertEqual(cli._manifest_path(name), name)
                path = self.tmp / name
                path.write_text(json.dumps(manifest("Literal " + name.strip())))
                shown = os.fspath(path) if not name.startswith("-") else name
                with mock.patch.object(cli, "load_manifest", wraps=cli.load_manifest) as loader:
                    args = ["--manifest=" + shown]
                    cwd = os.getcwd()
                    os.chdir(self.tmp)
                    try:
                        code, out, err = self.run_main("--directory", str(self.root), *args)
                    finally:
                        os.chdir(cwd)
                self.assertEqual(code, 0, err)
                self.assertEqual(json.loads(out)["source"], "manifest")
                loader.assert_called_once_with(shown)
        before = self.tree()
        code, out, err = self.run_main("--directory", str(self.root), "--manifest", str(self.tmp / "  "))
        self.assertEqual((code, out), (1, ""))
        self.assertIn("Stopped:", err)
        self.assertEqual(self.tree(), before)

    def test_absent_source_and_valid_sources_are_compatible(self):
        code, out, err = self.run_main("--directory", str(self.root))
        self.assertEqual(code, 0, err)
        report = json.loads(out)
        self.assertEqual((report["source"], report["since_from"], report["sent"]), ("template", None, False))
        good = self.tmp / "good.json"
        good.write_text(json.dumps(manifest()))
        code, out, err = self.run_main("--directory", str(self.root), "--manifest", str(good))
        self.assertEqual(code, 0, err)
        report = json.loads(out)
        self.assertEqual((report["source"], report["since_from"], report["sent"]), ("manifest", None, False))
        self.assertEqual(report["counts"]["findings"], 0)

    def test_nonempty_errors_and_read_only_check_contract_remain(self):
        before = self.tree()
        missing = str(self.tmp / "missing.json")
        code, out, err = self.run_main("--directory", str(self.root), "--manifest", missing)
        self.assertEqual((code, out), (1, ""))
        self.assertIn("Stopped:", err)
        bad = self.tmp / "bad.json"
        bad.write_text("{not json")
        code, out, err = self.run_main("--directory", str(self.root), "--manifest", str(bad))
        self.assertEqual((code, out), (1, ""))
        self.assertIn("Stopped:", err)
        code, out, err = self.run_main("--directory", str(self.root), "--check", "--manifest", missing)
        self.assertEqual((code, out), (2, ""))
        self.assertIn("--check takes no other flag or action (found --manifest)", err)
        for argv in (["sent", "--date", "2026-01-02", "--manifest", ""],
                     ["archive", "--date", "2026-01-02", "--manifest", ""]):
            self.assert_usage_refusal(*self.run_main("--directory", str(self.root), *argv))
        self.assertEqual(self.tree(), before)
        self.assertFalse((self.kit / "newsletter-sent.json").exists())


if __name__ == "__main__":
    unittest.main()
