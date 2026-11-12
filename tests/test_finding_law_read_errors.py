"""Finding-law check (RF1530-7): a regular campaign.json that cannot be read is refused with fixed, path-free text.

Synthetic data only: a copy of the fictional example under a temporary directory whose name carries a marker; the real
permission case chmods only that scratch copy and restores it in ``finally``. Injected cases replace only the shared
reader seam (``cli.read_config``) after the real type inspection has admitted the file. Run from the repository root:
python3 -B -m unittest tests.test_finding_law_read_errors -v
"""
import errno
import json
import os
import stat
import unittest
from unittest import mock

from campaign_tool import cli, content_check, finding_law, law
from campaign_tool.content import ContentError
from campaign_tool.content_check import content_problems
from tests import test_finding_law_config_nesting as nesting
from tests import test_finding_law_config_paths as paths

FIXED = "campaign.json: must resolve to a readable regular file"
MARKER = "SYNTHETIC-PRIVATE-PATH-MARKER"
CITED = [{"slug": "s", "event_date": "2021-04-01", "sources": [], "rule_ids": ["ca-made-up"]}]


def oserrors(where):
    """The read-stage OSError family, each carrying path/strerror markers that must never surface."""
    name = os.path.join(where, MARKER, "campaign.json")
    return [PermissionError(errno.EACCES, f"denied {MARKER}", name),
            OSError(errno.EIO, f"I/O {MARKER}\nline two", name, name + "-2"),
            FileNotFoundError(errno.ENOENT, f"gone {MARKER} \u00e9", name),
            IsADirectoryError(errno.EISDIR, f"dir {MARKER}", name)]


class ReadErrorCase(nesting.NestingCase):
    def assert_clean(self, *outputs):
        for output in outputs:
            for leak in (MARKER, str(self.tmp), "Errno", "Traceback", "Permission denied"):
                self.assertNotIn(leak, output)

    def refused(self, call, *args):
        with self.assertRaises(ContentError) as caught:
            call(*args)
        self.assertEqual(str(caught.exception), FIXED)
        self.assertTrue(caught.exception.__suppress_context__)
        self.assertIsNone(caught.exception.__cause__)
        self.assert_clean(str(caught.exception))
        return caught.exception

    def assert_cli_refuses(self):
        text = self.cli_run()
        self.assertEqual((text.returncode, text.stderr), (3, ""), text.stdout)
        self.assertEqual(text.stdout.splitlines(), ["content/: 1 problem(s)", FIXED])
        as_json = self.cli_run("--json")
        self.assertEqual((as_json.returncode, as_json.stderr), (3, ""), as_json.stdout)
        document = json.loads(as_json.stdout)
        self.assertEqual((document["count"], document["truncated"]), (1, False))
        self.assertIsNone(document["law_package"])
        self.assertEqual(document["problems"],
                         [{"file": "campaign.json", "message": "must resolve to a readable regular file"}])
        self.assert_clean(text.stdout, as_json.stdout)

    def unreadable(self):
        """Make the scratch campaign.json mode 000; skip only when this runtime can still read it (root/ACL)."""
        mode = stat.S_IMODE(self.config.stat().st_mode)
        os.chmod(self.config, 0)
        self.addCleanup(os.chmod, self.config, mode)
        try:
            self.config.read_bytes()
        except PermissionError:
            return
        self.skipTest("runtime can read a mode-000 file")


class PermissionTests(ReadErrorCase):
    def setUp(self):
        super().setUp()
        marked = self.tmp / MARKER
        self.root.rename(marked)
        self.root, self.config = marked, marked / "campaign.json"

    def test_real_permission_denial_is_path_free(self):
        self.put_overlay()
        self.unreadable()
        self.assertTrue(stat.S_ISREG(os.lstat(self.config).st_mode))
        self.refused(finding_law.jurisdiction, self.root)
        self.assertEqual(self.load_error(), FIXED)
        self.assertEqual(content_problems(self.root), [FIXED])
        self.assert_cli_refuses()

    def test_repeat_refusal_and_recovery_are_read_only(self):
        public = self.root / "public"
        public.mkdir()
        (public / "sentinel.txt").write_text("synthetic published sentinel", encoding="utf-8")
        self.cite_unknown()
        self.put_overlay()
        mode = stat.S_IMODE(self.config.stat().st_mode)
        self.unreadable()
        before = self.snapshot()
        for _ in range(2):
            self.assertEqual(self.load_error(), FIXED)
            self.assertEqual(content_problems(self.root), [FIXED])
        self.assert_cli_refuses()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(sorted(p.name for p in public.iterdir()), ["sentinel.txt"])
        os.chmod(self.config, mode)  # restoring the scratch copy's mode is the only change needed to recover
        self.assertEqual(finding_law.jurisdiction(self.root), "us-ca")
        self.assertIn(paths.UNKNOWN, content_problems(self.root))


class InjectedReadErrorTests(ReadErrorCase):
    def test_oserror_family_is_fixed_and_suppresses_context(self):
        self.cite_unknown()
        for error in oserrors(str(self.tmp)):
            with self.subTest(error=type(error).__name__):
                with mock.patch.object(cli, "read_config", side_effect=error) as reader, \
                        mock.patch.object(law, "load_package", side_effect=AssertionError("package read")):
                    self.refused(finding_law.jurisdiction, self.root)
                    self.refused(finding_law.check_findings, self.root, CITED)
                    self.assertEqual(self.load_error(), FIXED)
                    self.assertEqual(content_problems(self.root), [FIXED])
                self.assertTrue(reader.called)  # the real inspection admitted the file first

    def test_post_admission_disappearance_is_not_absence(self):
        for kind in ("cited", "overlay"):
            with self.subTest(kind=kind):
                self.setUp()
                self.cite_unknown() if kind == "cited" else self.put_overlay()
                gone = FileNotFoundError(errno.ENOENT, "No such file", str(self.config))
                with mock.patch.object(cli, "read_config", side_effect=gone):
                    if kind == "cited":
                        self.refused(finding_law.check_findings, self.root, CITED)
                    else:
                        self.refused(finding_law.check, self.root, [])
                    self.assertEqual(content_problems(self.root), [FIXED])
                self.replace_config("absent")  # initial absence keeps its content-only meaning
                self.assertIsNone(finding_law.jurisdiction(self.root))

    def test_required_consumers_have_identical_report_attribution(self):
        for kind in ("cited", "overlay"):
            with self.subTest(kind=kind):
                self.setUp()
                self.cite_unknown() if kind == "cited" else self.put_overlay()
                with mock.patch.object(cli, "read_config", side_effect=oserrors(str(self.tmp))[0]):
                    self.assertEqual(self.load_error(), FIXED)
                    self.assertEqual(content_problems(self.root), [FIXED])
                    self.assertEqual(self.check_cli(), (3, ["content/: 1 problem(s)", FIXED]))
                    self.assertIsNone(content_check._law_package(self.root))


class CompatibilityTests(ReadErrorCase):
    def test_existing_parser_and_nesting_diagnostics_unchanged(self):
        expected = {"{": None, json.dumps(dict(nesting.BASE, schema_version=2)): "Unsupported campaign schema version",
                    json.dumps({k: v for k, v in nesting.BASE.items() if k != "name"}): "Missing campaign field: name",
                    json.dumps(dict(nesting.BASE, state="California")):
                        "Use a two-letter state code; jurisdiction is not auto-verified"}
        for text, message in expected.items():
            with self.subTest(text=text[:20]):
                self.put(text)
                with self.assertRaises(ContentError) as caught:
                    finding_law.jurisdiction(self.root)
                got = str(caught.exception)
                self.assertTrue(got.startswith("campaign.json: "), got)
                self.assertNotEqual(got, FIXED)
                if message:
                    self.assertEqual(got, "campaign.json: " + message)
        self.config.write_bytes(b"\xff\xfe{")
        with self.assertRaises(ContentError) as caught:
            finding_law.jurisdiction(self.root)
        self.assertIn("codec", str(caught.exception))
        self.put(nesting.deep_text())
        with self.assertRaises(ContentError) as caught:
            finding_law.jurisdiction(self.root)
        self.assertEqual(str(caught.exception), nesting.FIXED)

    def test_boundary_and_fast_path_compatibility(self):
        self.assertEqual(finding_law.jurisdiction(self.root), "us-ca")
        self.replace_config("regular_link")
        self.assertEqual(finding_law.jurisdiction(self.root), "us-ca")
        with mock.patch.object(finding_law, "jurisdiction", side_effect=AssertionError("jurisdiction")), \
                mock.patch.object(cli, "read_config", side_effect=AssertionError("config read")):
            self.assertEqual(finding_law.check(self.root, []), ([], []))
        for error in (RuntimeError(MARKER), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__):
                with mock.patch.object(cli, "read_config", side_effect=error):
                    with self.assertRaises(type(error)):
                        finding_law.jurisdiction(self.root)
        with mock.patch.object(law, "load_package", side_effect=PermissionError(errno.EACCES, MARKER)):
            problems = finding_law.check_findings(self.root, CITED)  # a later package stage keeps its own handling
        self.assertEqual(problems, ["findings/s: cites rule ca-made-up but there is no usable us-ca law package"])
        self.setUp()
        self.replace_config("absent")
        self.assertIsNone(finding_law.jurisdiction(self.root))


if __name__ == "__main__":
    unittest.main()
