"""RF1045-3: `law impact --json` stays valid UTF-8 when a filesystem name holds undecodable bytes (synthetic only).

Run from the repository root: python3 -B -m unittest tests.test_law_impact_json_encoding -q
"""
import contextlib
import copy
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import law, law_impact  # noqa: E402
from tests import test_law_impact as fixtures  # noqa: E402  (alias only: its TestCase is not re-collected)

BAD_NAME = os.fsdecode(b"synthetic-\xff.json")
BAD_DIR = os.fsdecode(b"campaign-\xfe")


def _linux_can_hold_bad_names():
    with tempfile.TemporaryDirectory(prefix="synthetic-law-enc-probe-") as folder:
        try:
            (Path(folder) / BAD_NAME).write_text("{}", encoding="utf-8")
        except (OSError, UnicodeError):
            return False
    return True


NEEDS_BAD_NAMES = unittest.skipUnless(_linux_can_hold_bad_names(), "filesystem refuses undecodable names")


class ImpactJsonEncodingBase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-law-enc-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.root = self.tmp / "campaign"
        for sub in ("content/findings", "private/drafts", "private/withdrawn"):
            (self.root / sub).mkdir(parents=True)
        self.old, self.new = copy.deepcopy(fixtures.BASE), copy.deepcopy(fixtures.BASE)

    def package_paths(self, changed=True):
        if changed:
            fixtures.rule_of(self.new, "ca-alpha")["duty"] = "Synthetic duty, amended."
        old, new = self.tmp / "old.json", self.tmp / "new.json"
        old.write_text(json.dumps(self.old), encoding="utf-8")
        new.write_text(json.dumps(self.new), encoding="utf-8")
        return old, new

    def finding(self, ident, root=None, where="content/findings"):
        document = {"id": ident, "slug": "slug-" + ident.replace("_", "-"), "title": "Synthetic " + ident,
                    "event_date": "2024-05-01", "rule_ids": ["ca-alpha"], "sources": []}
        path = (root or self.root) / where / (ident + ".json")
        path.write_text(json.dumps(document), encoding="utf-8")
        return path

    def bad_draft(self, root=None):
        path = (root or self.root) / "private" / "drafts" / BAD_NAME
        path.write_text("{ not json", encoding="utf-8")
        return path

    def cli(self, old, new, directory, encoding="utf-8:strict", *extra, json_mode=True):
        env = dict(os.environ, PYTHONIOENCODING=encoding)
        command = [sys.executable, "-B", "-m", "campaign_tool.law", "impact", str(old), str(new),
                   "--directory", str(directory), *(["--json"] if json_mode else []), *extra]
        return subprocess.run(command, cwd=REPO_ROOT, env=env, capture_output=True)

    def parsed(self, run):
        text = run.stdout.decode("utf-8")  # strict: raises if the bytes are not valid UTF-8
        return json.loads(text)


@NEEDS_BAD_NAMES
class RealFilesystemTests(ImpactJsonEncodingBase):
    def test_real_unreadable_non_utf8_basename_emits_utf8_json(self):
        self.finding("fnd_ok")
        bad = self.bad_draft()
        run = self.cli(*self.package_paths(), self.root)
        self.assertEqual((run.returncode, run.stderr), (3, b""), run.stdout)
        data = self.parsed(run)
        self.assertEqual(data["unreadable"], [str(bad.resolve())])
        self.assertEqual(data["counts"]["unreadable"], 1)
        self.assertEqual([h["id"] for h in data["affected"]], ["fnd_ok"])
        self.assertIn("\\udcff", run.stdout.decode("ascii", "ignore"))

    def test_surrogateescape_stdout_does_not_emit_raw_filesystem_bytes(self):
        self.finding("fnd_ok")
        bad = self.bad_draft()
        strict = self.cli(*self.package_paths(), self.root, "utf-8:strict")
        loose = self.cli(*self.package_paths(), self.root, "utf-8:surrogateescape")
        self.assertEqual(loose.returncode, 3, loose.stderr)
        self.assertNotIn(b"\xff", loose.stdout)
        self.assertEqual(self.parsed(loose)["unreadable"], [str(bad.resolve())])
        self.assertEqual(loose.stdout, strict.stdout)

    def test_unreadable_only_report_keeps_exit_zero_and_membership(self):
        bad = self.bad_draft()
        self.finding("fnd_ok")
        for changed in (False, True):
            with self.subTest(changed=changed):
                if changed:  # a rule changes that no finding cites: still nothing affected
                    fixtures.rule_of(self.new, "ca-beta")["duty"] = "Synthetic duty, amended."
                old, new = self.package_paths(changed=False)
                run = self.cli(old, new, self.root)
                self.assertEqual((run.returncode, run.stderr), (0, b""))
                data = self.parsed(run)
                self.assertEqual(data["unreadable"], [str(bad.resolve())])
                self.assertEqual((data["counts"]["unreadable"], data["counts"]["affected"]), (1, 0))

    def test_directory_and_next_command_strings_are_escaped_without_mutation(self):
        root = self.tmp / BAD_DIR
        for sub in ("content/findings", "private/drafts", "private/withdrawn"):
            (root / sub).mkdir(parents=True)
        draft = self.finding("fnd_draft", root, "private/drafts")
        run = self.cli(*self.package_paths(), root)
        self.assertEqual((run.returncode, run.stderr), (3, b""))
        data = self.parsed(run)
        self.assertEqual([h["id"] for h in data["affected"]], ["fnd_draft"])
        hit = data["affected"][0]
        self.assertEqual(hit["path"], str(draft.resolve()))
        self.assertEqual(hit["next"][0], f"campaign_tool finding review-status {law_impact.shlex.quote(str(draft.resolve()))}"
                                         f" --directory {law_impact.shlex.quote(str(root))}")
        self.assertNotIn(b"\xfe", run.stdout)

    def test_reports_never_rewrite_inputs_or_create_outputs(self):
        self.finding("fnd_ok")
        self.bad_draft()
        paths = self.package_paths()

        def state():
            rows = {}
            for path in sorted(self.tmp.rglob("*")):
                info = path.lstat()
                rows[os.fsencode(path)] = (info.st_mode, info.st_mtime_ns, path.read_bytes() if path.is_file() else None)
            return rows

        before = state()
        for encoding in ("utf-8:strict", "utf-8:surrogateescape", "utf-8:strict"):
            self.assertEqual(self.cli(*paths, self.root, encoding).returncode, 3)
        self.assertTrue(before == state(), "inputs or outputs changed")


class PureHelperTests(unittest.TestCase):
    def dumps(self, value):
        return law_impact._json_text(value)

    def test_surrogate_range_and_literal_escape_matrix(self):
        cases = ["\udc80", "\udcff", "\ud800", "\udbff", "\udcff\udcff", "a\udcffb", "\ud83d\ude00", "\ude00\ud83d",
                 "literal \\udcff text", 'quote " and backslash \\', "\\\udcff"]
        for case in cases:
            with self.subTest(case=case):
                for value in (case, {case: case}, [case, {"k": [case]}]):
                    text = self.dumps(value)
                    text.encode("utf-8")  # strict: no lone surrogate survives
                    self.assertEqual(json.loads(text), json.loads(json.dumps(value, ensure_ascii=True)))
        self.assertEqual(json.loads(self.dumps("\udcff")), "\udcff")
        self.assertEqual(json.loads(self.dumps("literal \\udcff")), "literal \\udcff")
        self.assertEqual(self.dumps("literal \\udcff"), json.dumps("literal \\udcff", ensure_ascii=False))

    def test_healthy_non_ascii_text_is_not_escaped(self):
        value = {"é": ["naïve", "漢字", "😀", "\u2028", "tab\t"], "n": 1, "none": None}
        self.assertEqual(self.dumps(value), json.dumps(value, indent=2, ensure_ascii=False))


class ByteCompatibilityTests(ImpactJsonEncodingBase):
    def test_healthy_non_ascii_output_is_byte_identical(self):
        root = self.tmp / "campaign-é-漢字-😀"
        for sub in ("content/findings", "private/drafts", "private/withdrawn"):
            (root / sub).mkdir(parents=True)
        self.finding("fnd_pub", root)
        self.finding("fnd_draft", root, "private/drafts")
        undated = json.loads((root / "private/drafts/fnd_draft.json").read_text(encoding="utf-8"))
        undated.update(id="fnd_undated", event_date="unknown")
        (root / "private/drafts/fnd_undated.json").write_text(json.dumps(undated), encoding="utf-8")
        (root / "private/drafts/broken-é.json").write_text("{ not json", encoding="utf-8")
        old, new = self.package_paths()
        loaded_old, loaded_new = law_impact.load_file(old), law_impact.load_file(new)
        data, _ = law_impact.report(old, new, loaded_old, loaded_new, str(root))
        self.assertEqual(data["counts"]["unreadable"], 1)
        self.assertTrue(data["affected"] and data["undated"])
        expected = (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        for encoding in ("utf-8:strict", "utf-8:surrogateescape"):
            run = self.cli(old, new, root, encoding)
            self.assertEqual(run.returncode, 3, run.stderr)
            self.assertTrue(run.stdout == expected, "healthy JSON bytes changed")
        self.assertIn("é".encode("utf-8"), expected)

    def test_input_errors_and_text_branch_are_unchanged(self):
        old, new = self.package_paths()
        missing = self.tmp / "missing.json"
        bad = self.tmp / "bad.json"
        bad.write_text("{ not json", encoding="utf-8")
        deep = self.tmp / "deep.json"
        deep.write_text("[" * 10000 + "]" * 10000, encoding="utf-8")
        other = copy.deepcopy(self.new)
        other["jurisdiction"] = other["jurisdiction"] + "-other"
        mismatch = self.tmp / "mismatch.json"
        mismatch.write_text(json.dumps(other), encoding="utf-8")
        for name, args in (("missing", (old, missing)), ("invalid", (bad, new)), ("deep", (old, deep)),
                           ("mismatch", (old, mismatch))):
            with self.subTest(name=name):
                run = self.cli(*args, self.root)
                self.assertEqual((run.returncode, run.stdout), (2, b""), run.stderr)
                self.assertTrue(run.stderr.startswith(b"Stopped: "))
        self.finding("fnd_ok")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = law.main(["impact", str(old), str(new), "--directory", str(self.root)])
        data, rows = law_impact.report(old, new, law_impact.load_file(old), law_impact.load_file(new), str(self.root))
        self.assertEqual((code, out.getvalue(), err.getvalue()), (3, law_impact.render(data, rows) + "\n", ""))

    def test_unrelated_programming_errors_are_not_hidden(self):
        from unittest import mock
        old, new = self.package_paths()
        with mock.patch.object(law_impact, "_json_text", side_effect=RuntimeError("synthetic bug")):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(RuntimeError):
                    law_impact.run_cli(old, new, self.root, as_json=True)


if __name__ == "__main__":
    unittest.main()
