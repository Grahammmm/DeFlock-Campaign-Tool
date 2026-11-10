"""Finding-law check (RF0415-13): only an absent campaign.json selects the content-only path; a present unusable one is refused.

Synthetic data only: a copy of the fictional example plus a synthetic law package under a temporary jurisdictions base
(``law.JURISDICTIONS`` patched). Special files are created in the temporary tree; the FIFO is never opened. Run from the
repository root: python3 -B -m unittest tests.test_finding_law_config_paths -v
"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool import cli, finding_law, law
from campaign_tool.content import ContentError
from campaign_tool.content_check import content_problems, run_cli
from tests import test_finding_law
from tests.test_finding_law import EXAMPLE, synthetic_package
from tests.test_local_law import LOCAL, rule

REFUSAL = "campaign.json: must resolve to a readable regular file"
UNKNOWN = "findings/cedar-policy-posted: rule ca-made-up is not in the us-ca law package"
NEEDS_CAMPAIGN = "law/local-rules.json: needs a campaign.json to name the campaign's jurisdiction"
REPO_ROOT = Path(__file__).resolve().parents[1]


class ConfigPathCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-finding-law-config-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.base = self.tmp / "jurisdictions"
        (self.base / "us-ca").mkdir(parents=True)
        (self.base / "us-ca" / "package.json").write_text(json.dumps(synthetic_package()), encoding="utf-8")
        self.root = self.tmp / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.config = self.root / "campaign.json"
        self.stash = self.tmp / "real-campaign.json"
        patcher = mock.patch.object(law, "JURISDICTIONS", self.base)
        patcher.start()
        self.addCleanup(patcher.stop)

    finding = test_finding_law.FindingLawTests.finding
    load_error = test_finding_law.FindingLawTests.load_error
    check_cli = test_finding_law.FindingLawTests.check_cli

    def replace_config(self, kind):
        """Move the real configuration out of the tree, then make ``campaign.json`` the given kind of entry."""
        self.config.rename(self.stash)
        if kind == "absent":
            return
        if kind == "directory":
            self.config.mkdir()
        elif kind == "dangling":
            self.config.symlink_to(self.tmp / "nowhere.json")
        elif kind == "loop":
            self.config.symlink_to("campaign.json")
        elif kind == "link_dir":
            (self.tmp / "target-dir").mkdir()
            self.config.symlink_to(self.tmp / "target-dir")
        elif kind == "regular_link":
            self.config.symlink_to(self.stash)
        else:
            if not hasattr(os, "mkfifo"):
                self.skipTest("os.mkfifo is not available")
            os.mkfifo(self.tmp / "target-fifo" if kind == "link_fifo" else self.config)
            if kind == "link_fifo":
                self.config.symlink_to(self.tmp / "target-fifo")

    def cite_unknown(self):
        self.finding(rule_ids=["ca-made-up"])

    def snapshot(self):
        result = {}
        for path in [self.root, *sorted(self.root.rglob("*")), self.stash, self.tmp / "target-dir"]:
            try:
                info = path.lstat()
            except OSError:
                continue
            result[str(path)] = (info.st_mode, info.st_size, info.st_mtime_ns,
                                 os.readlink(path) if path.is_symlink() else None)
        return result


class PresentUnusableConfigTests(ConfigPathCase):
    def test_present_nonregular_config_never_disables_citation_check(self):
        for kind in ("directory", "fifo"):
            with self.subTest(kind=kind):
                self.setUp()
                self.replace_config(kind)
                self.cite_unknown()
                with mock.patch.object(cli, "read_config", side_effect=AssertionError("config read")), \
                        mock.patch.object(law, "load_package", side_effect=AssertionError("package read")):
                    with self.assertRaises(ContentError) as caught:
                        finding_law.jurisdiction(self.root)
                    self.assertEqual(str(caught.exception), REFUSAL)
                    self.assertEqual(self.load_error(), REFUSAL)
                    self.assertEqual(content_problems(self.root), [REFUSAL])
                    self.assertEqual(self.check_cli(), (3, ["content/: 1 problem(s)", REFUSAL]))

    def test_fifo_config_refuses_in_a_bounded_subprocess(self):
        self.replace_config("fifo")
        self.cite_unknown()
        script = self.tmp / "fifo_probe.py"
        script.write_text(
            "import sys\n"
            "from campaign_tool.content import ContentError, load_content\n"
            "from campaign_tool.content_check import run_cli\n"
            "try:\n"
            "    load_content(sys.argv[1])\n"
            "except ContentError as exc:\n"
            "    print('load:', exc)\n"
            "run_cli(sys.argv[1])\n", encoding="utf-8")
        result = subprocess.run([sys.executable, "-B", str(script), str(self.root)], cwd=REPO_ROOT, timeout=60,
                                capture_output=True, text=True, env=dict(os.environ, PYTHONPATH=str(REPO_ROOT)))
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertEqual(result.stdout.splitlines(),
                         [f"load: {REFUSAL}", "content/: 1 problem(s)", REFUSAL])
        self.assertEqual(result.stderr, "")

    def test_dangling_loop_and_nonregular_target_links_are_not_absence(self):
        for kind in ("dangling", "loop", "link_dir", "link_fifo"):
            with self.subTest(kind=kind):
                self.setUp()
                self.replace_config(kind)
                self.cite_unknown()
                with mock.patch.object(cli, "read_config", side_effect=AssertionError("config read")):
                    with self.assertRaises(ContentError) as caught:
                        finding_law.jurisdiction(self.root)
                    self.assertEqual(str(caught.exception), REFUSAL)
                    self.assertTrue(caught.exception.__suppress_context__)
                    self.assertEqual(self.load_error(), REFUSAL)
                    self.assertEqual(content_problems(self.root), [REFUSAL])

    def test_path_inspection_error_is_controlled_not_absent(self):
        secret = "SYNTHETIC-SENTINEL-errno-text"
        real_stat = os.stat
        cases = {
            "lstat denied": (mock.patch.object(finding_law.os, "lstat", side_effect=PermissionError(secret)), True),
            "lstat not a directory": (mock.patch.object(finding_law.os, "lstat", side_effect=NotADirectoryError(secret)),
                                      True),
            "stat denied": (mock.patch.object(finding_law.os, "stat", side_effect=PermissionError(secret)), True),
            "target vanished after lstat": (mock.patch.object(finding_law.os, "stat",
                                                              side_effect=FileNotFoundError(secret)), True),
            "entry not found": (mock.patch.object(finding_law.os, "lstat", side_effect=FileNotFoundError(secret)), False),
        }
        for name, (patcher, refused) in cases.items():
            with self.subTest(name=name), patcher:
                if refused:
                    with self.assertRaises(ContentError) as caught:
                        finding_law.jurisdiction(self.root)
                    self.assertEqual(str(caught.exception), REFUSAL)
                    self.assertNotIn(secret, str(caught.exception))
                    self.assertNotIn(str(self.tmp), str(caught.exception))
                else:
                    self.assertIsNone(finding_law.jurisdiction(self.root))
        self.assertIs(finding_law.os.stat, real_stat)


class CompatibilityTests(ConfigPathCase):
    def test_absent_and_valid_regular_link_compatibility(self):
        self.replace_config("absent")
        self.cite_unknown()
        self.assertIsNone(finding_law.jurisdiction(self.root))
        self.assertEqual((self.load_error(), content_problems(self.root)), (None, []))
        self.assertEqual(self.check_cli(), (0, ["content/: 0 problem(s)"]))
        for kind in ("regular", "regular_link"):
            with self.subTest(kind=kind):
                self.setUp()
                if kind == "regular_link":
                    self.replace_config(kind)
                self.assertEqual(finding_law.jurisdiction(self.root), "us-ca")
                self.cite_unknown()
                self.assertEqual(self.load_error(), UNKNOWN)
                self.assertIn(UNKNOWN, content_problems(self.root))
                self.finding(rule_ids=["ca-open"], event_date="2021-04-01")
                self.assertEqual((self.load_error(), content_problems(self.root)), (None, []))

    def test_malformed_regular_config_keeps_its_wrapper(self):
        self.config.write_text("{", encoding="utf-8")
        with self.assertRaises(ContentError) as caught:
            finding_law.jurisdiction(self.root)
        self.assertTrue(str(caught.exception).startswith("campaign.json: "), caught.exception)
        self.assertNotEqual(str(caught.exception), REFUSAL)


class ParityTests(ConfigPathCase):
    def test_text_json_parity_and_no_duplicate_prefix(self):
        for kind in ("directory", "dangling"):
            with self.subTest(kind=kind):
                self.setUp()
                self.replace_config(kind)
                self.cite_unknown()
                err, out = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as code:
                    run_cli(self.root, as_json=True)
                self.assertEqual((code.exception.code, err.getvalue()), (3, ""))
                document = json.loads(out.getvalue())
                self.assertEqual(document["count"], 1)
                self.assertEqual(document["problems"],
                                 [{"file": "campaign.json", "message": "must resolve to a readable regular file"}])
                self.assertIsNone(document["law_package"])
                self.assertEqual(self.check_cli(), (3, ["content/: 1 problem(s)", REFUSAL]))
                self.assertEqual(self.load_error(), REFUSAL)

    def test_unused_overlay_still_requires_usable_config(self):
        (self.root / "law").mkdir(exist_ok=True)
        (self.root / "law" / "local-rules.json").write_text(
            json.dumps({"schema_version": 1, "jurisdiction": "us-ca", "rules": [rule()]}), encoding="utf-8")
        self.assertEqual(finding_law.check(self.root, [])[0], [])
        versions = finding_law.check(self.root, [])[1]
        self.assertEqual([version["rule_id"] for version in versions], [LOCAL])
        for kind in ("directory", "dangling"):
            with self.subTest(kind=kind):
                self.setUp()
                (self.root / "law").mkdir(exist_ok=True)
                (self.root / "law" / "local-rules.json").write_text(
                    json.dumps({"schema_version": 1, "jurisdiction": "us-ca", "rules": [rule()]}), encoding="utf-8")
                self.replace_config(kind)
                with self.assertRaises(ContentError) as caught:
                    finding_law.check(self.root, [])
                self.assertEqual(str(caught.exception), REFUSAL)
                self.assertIn(REFUSAL, content_problems(self.root))
        self.setUp()
        (self.root / "law").mkdir(exist_ok=True)
        (self.root / "law" / "local-rules.json").write_text(
            json.dumps({"schema_version": 1, "jurisdiction": "us-ca", "rules": [rule()]}), encoding="utf-8")
        self.replace_config("absent")
        self.assertEqual(finding_law.check(self.root, []), ([NEEDS_CAMPAIGN], []))

    def test_no_citations_no_overlay_keeps_no_read_fast_path(self):
        for kind in ("absent", "directory", "dangling"):
            with self.subTest(kind=kind):
                self.setUp()
                self.replace_config(kind)
                with mock.patch.object(finding_law, "jurisdiction", side_effect=AssertionError("jurisdiction")), \
                        mock.patch.object(cli, "read_config", side_effect=AssertionError("config read")), \
                        mock.patch.object(law, "load_package", side_effect=AssertionError("package read")):
                    self.assertEqual(finding_law.check(self.root, []), ([], []))
                    self.assertEqual(finding_law.check_findings(self.root, []), [])
                if kind != "absent":
                    with self.assertRaises(ContentError):
                        finding_law.jurisdiction(self.root)

    def test_success_and_refusal_are_read_only(self):
        for kind in ("regular_link", "directory", "dangling", "absent"):
            with self.subTest(kind=kind):
                self.setUp()
                self.cite_unknown()
                self.replace_config(kind)
                before = self.snapshot()
                with mock.patch.object(law, "load_package", wraps=law.load_package) as loader:
                    for _ in range(2):
                        self.load_error()
                        content_problems(self.root)
                    if kind in ("directory", "dangling"):
                        loader.assert_not_called()
                self.assertEqual(self.snapshot(), before)
                self.assertFalse((self.root / "public").exists())


if __name__ == "__main__":
    unittest.main()
