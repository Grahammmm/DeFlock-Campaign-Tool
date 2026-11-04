"""BF-df-multi-campaign: a named register of campaigns, `--campaign NAME`, one `campaigns status`, no shared state.

Synthetic fixtures only (two campaigns in a temporary folder; no records, no network).
Run from the repository root: python3 -B -m unittest tests.test_multi_campaign -v
"""
import ast
import contextlib
import fcntl
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool import findings, registry
from campaign_tool.cli import main

REPO = Path(__file__).resolve().parents[1]
BANNED = re.compile(r"violat|unlawful|illegal|non-?complian|breach", re.I)
MARKER = "SYNTHETIC-RECORD-TEXT"
WATCH, SEEN = [], []  # paths under WATCH that reached a file operation PAST the registry's guard are appended to SEEN
registry.confine("", ())  # installs the guard first, so the recorder below only sees what the guard let through


def _record(event, args):
    if WATCH and event in registry._PATH_EVENTS:
        for arg in args[:registry._PATH_EVENTS[event]]:
            if isinstance(arg, (str, bytes, os.PathLike)):
                target = Path(os.path.realpath(os.path.abspath(os.fsdecode(arg))))
                SEEN.extend((event, str(target)) for w in WATCH if registry._within(target, w))


sys.addaudithook(_record)


def tree_hash(root):
    digest = hashlib.sha256()
    for path in sorted(Path(root).rglob("*")):
        digest.update(str(path.relative_to(root)).encode() + (path.read_bytes() if path.is_file() and not path.is_symlink() else b"-"))
    return digest.hexdigest()


def draft():
    return {"id": "synthetic-finding", "author": "Synthetic Author", "classification": "documented_fact",
            "summary": MARKER, "sources": [{"locator": "Synthetic page 1", "sha256": "a" * 64}],
            "limitations": "Synthetic only", "counterevidence": "Synthetic only"}


class MultiCampaign(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-multi-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(os.path.realpath(temporary.name))
        self.a, self.b = self.base / "A", self.base / "B"
        for root in (self.a, self.b):
            root.mkdir()
            (root / "campaign.json").write_text(json.dumps(
                {"schema_version": 1, "name": root.name, "county": "Synthetic", "state": "CA"}), encoding="utf-8")
        (self.b / "secret.txt").write_text("synthetic campaign B record\n", encoding="utf-8")
        self.register = self.base / "config" / "campaigns.json"
        patch = mock.patch.dict(os.environ, {registry.ENV: str(self.register)})
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(registry.release)
        self.addCleanup(WATCH.clear)
        SEEN.clear()

    def cli(self, *args, check=None):  # the real command line, one process
        result = subprocess.run([sys.executable, "-B", "-m", "campaign_tool", *map(str, args)], cwd=REPO, text=True,
                                capture_output=True, timeout=60, env=dict(os.environ))
        self.assertNotIn("Traceback", result.stderr)
        if check is not None:
            self.assertEqual(result.returncode, check, result.stdout + result.stderr)
        return result

    def inproc(self, *args):  # the same main(), in this process, so file operations can be recorded
        out, err, code = io.StringIO(), io.StringIO(), 0
        with mock.patch.object(sys, "argv", ["campaign_tool", *map(str, args)]), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                main()
            except SystemExit as exc:
                code = exc.code or 0
            finally:
                registry.release()  # the guard main() installed for --campaign must not outlive the command
        return code, out.getvalue(), err.getvalue()

    def register_both(self):
        for name, root in (("A", self.a), ("B", self.b)):
            self.cli("campaigns", "add", name, "--directory", root, check=0)

    def test_list_names_both(self):
        self.register_both()
        listing = self.cli("campaigns", "list", check=0).stdout
        self.assertEqual([line.split(" | ")[1] for line in listing.splitlines()], ["A", "B"])

    def test_campaign_output_is_byte_identical_to_directory_output(self):
        self.register_both()
        for command in (("status",), ("status", "--check"), ("doctor",), ("finding", "status"), ("kit-status",)):
            by_name = self.cli(*command, "--campaign", "A")
            by_path = self.cli(*command, "--directory", self.a)
            self.assertEqual((by_name.returncode, by_name.stdout, by_name.stderr),
                             (by_path.returncode, by_path.stdout, by_path.stderr), command)
        self.assertIn("stored_objects", self.cli("status", "--campaign", "A", check=0).stdout)

    def test_campaign_and_directory_together_is_a_usage_error(self):
        self.register_both()
        result = self.cli("status", "--campaign", "A", "--directory", self.a, check=2)
        self.assertIn("not allowed with", result.stderr)

    def test_unknown_name_lists_registered_names(self):
        self.register_both()
        result = self.cli("status", "--campaign", "Z", check=2)
        self.assertIn("unknown campaign 'Z'; registered: A, B", result.stderr)

    def test_neither_flag_is_still_a_usage_error(self):
        self.cli("status", check=2)

    def test_status_prints_one_row_each_and_a_not_current_row_for_a_broken_source(self):
        self.register_both()
        (self.b / "kit").mkdir()
        (self.b / "kit" / "sent.json").write_text("not json", encoding="utf-8")
        result = self.cli("campaigns", "status", check=3)
        rows = result.stdout.splitlines()
        self.assertEqual(len(rows), 2)
        self.assertRegex(rows[0], r"^campaign \| A \| requests=0 open=0 waiting-send=0 waiting-publish=0 law=draft \| private; review-required$")
        self.assertEqual(rows[1], "campaign | B | NOT CURRENT: ValueError | private; review-required")

    def test_status_counts_gates_and_carries_no_record_text(self):
        self.register_both()
        for role, reviewer in (("factual", "Synthetic Reviewer One"), ("legal", "Synthetic Reviewer Two"),
                               ("privacy", "Synthetic Reviewer Two")):
            findings.add_receipt(self.a, draft(), role, "approve", reviewer, "Synthetic review")
        (self.a / "private" / "drafts").mkdir()
        (self.a / "private" / "drafts" / "synthetic-finding.json").write_text(json.dumps(draft()), encoding="utf-8")
        (self.a / "kit" / "requests").mkdir(parents=True)
        (self.a / "kit" / "requests" / "synthetic-agency.md").write_text("Synthetic request text " + MARKER, encoding="utf-8")
        text = self.cli("campaigns", "status", check=0).stdout
        self.assertIn("campaign | A | requests=0 open=0 waiting-send=1 waiting-publish=1 law=draft |", text)
        for forbidden in (MARKER, "Synthetic Reviewer", "synthetic-finding", "synthetic-agency", "@", "Synthetic Author"):
            self.assertNotIn(forbidden, text)
        self.assertIsNone(BANNED.search(text))

    def test_remove_unregisters_without_touching_the_directory_and_re_add_restores(self):
        self.register_both()
        self.cli("status", "--campaign", "A", check=0)
        before = tree_hash(self.a)
        removed = self.cli("campaigns", "remove", "A", check=0).stdout
        self.assertIn("were not touched", removed)
        self.assertEqual(tree_hash(self.a), before)
        self.assertTrue(self.a.is_dir())
        self.assertIn("unknown campaign 'A'; registered: B", self.cli("status", "--campaign", "A", check=2).stderr)
        self.cli("campaigns", "add", "A", "--directory", self.a, check=0)
        self.cli("status", "--campaign", "A", check=0)
        self.assertEqual(self.cli("campaigns", "remove", "Nope", check=1).returncode, 1)

    def test_add_refusals(self):
        self.cli("campaigns", "add", "A", "--directory", self.a, check=0)
        plain, link, empty, nested = self.base / "file.txt", self.base / "link", self.base / "empty", self.a / "inner"
        plain.write_text("x", encoding="utf-8")
        link.symlink_to(self.b, target_is_directory=True)
        empty.mkdir()
        nested.mkdir()
        (nested / "campaign.json").write_bytes((self.a / "campaign.json").read_bytes())
        cases = {"duplicate name": ("A", self.b), "missing path": ("M", self.base / "missing"),
                 "file not directory": ("F", plain), "symlinked path": ("L", link),
                 "inside the repository": ("R", REPO / "examples" / "fictional-campaign"),
                 "no campaign.json": ("E", empty), "nested under a registered path": ("N", nested),
                 "the same registered path": ("S", self.a), "containing a registered path": ("P", self.base)}
        (self.base / "campaign.json").write_bytes((self.a / "campaign.json").read_bytes())
        for why, (name, path) in cases.items():
            self.assertEqual(self.cli("campaigns", "add", name, "--directory", path).returncode, 1, why)
        self.assertEqual(len(self.cli("campaigns", "list", check=0).stdout.splitlines()), 1)

    def test_name_validation_and_case_sensitivity(self):
        for bad in ("a/b", "a\\b", "a\0b", " a", "a ", "", "a b", "..", "-x", "x" * 41):
            with self.assertRaises(ValueError, msg=repr(bad)):
                registry.add(bad, self.a)
        other = self.base / "other"
        other.mkdir()
        (other / "campaign.json").write_bytes((self.a / "campaign.json").read_bytes())
        registry.add("alpha", self.a)
        registry.add("Alpha", other)
        self.assertEqual(sorted(e["name"] for e in registry._load(self.register)), ["Alpha", "alpha"])

    def attack(self, *command):
        """Run a command for A that is steered at B; nothing may reach B."""
        if not self.register.exists():
            self.register_both()
        WATCH.clear()
        before = tree_hash(self.b)
        WATCH[:] = [self.b]
        SEEN.clear()
        code, out, err = self.inproc(*command, "--campaign", "A")
        reached, WATCH[:] = list(SEEN), []
        self.assertEqual(reached, [], "reached campaign B past the guard")
        self.assertEqual(tree_hash(self.b), before)
        return code, out, err

    def test_symlink_into_b_reaches_nothing_in_b(self):
        (self.a / "inbox.txt").symlink_to(self.b / "secret.txt")
        code, _, err = self.attack("ingest", "--file", self.a / "inbox.txt", "--source-id", "s1")
        self.assertNotEqual(code, 0, err)

    def test_dotdot_traversal_into_b_reaches_nothing_in_b(self):
        code, _, err = self.attack("ingest", "--file", f"{self.a}/../B/secret.txt", "--source-id", "s2")
        self.assertNotEqual(code, 0, err)
        self.assertIn("may not reach another registered campaign", err)

    def test_foreign_absolute_path_in_campaign_json_reaches_nothing_in_b(self):
        config = json.loads((self.a / "campaign.json").read_text(encoding="utf-8"))
        config.update(directory=str(self.b), output=str(self.b / "public"), inbox=str(self.b / "secret.txt"))
        (self.a / "campaign.json").write_text(json.dumps(config), encoding="utf-8")
        for command in (("build",), ("status", "--check"), ("doctor",)):
            self.attack(*command)
        registry.confine(self.a, [self.a, self.b])
        with self.assertRaises(PermissionError):
            (self.b / "secret.txt").read_text()
        with self.assertRaises(PermissionError):
            (self.b / "written.txt").write_text("x")
        self.assertTrue((self.a / "campaign.json").read_text())
        registry.release()
        self.assertFalse((self.b / "written.txt").exists())

    def test_the_isolation_attack_is_real_without_the_guard(self):  # control: --directory keeps today's behaviour
        self.register_both()
        WATCH.append(self.b)
        code, _, _ = self.inproc("ingest", "--file", f"{self.a}/../B/secret.txt", "--source-id", "s3", "--directory", self.a)
        self.assertEqual(code, 0)
        self.assertTrue(SEEN)

    def test_approval_recorded_in_a_is_refused_in_b(self):
        for role, reviewer in (("factual", "Synthetic Reviewer One"), ("legal", "Synthetic Reviewer Two"),
                               ("privacy", "Synthetic Reviewer Two")):
            findings.add_receipt(self.a, draft(), role, "approve", reviewer, "Synthetic review")
        path = self.a / "private" / "reviews" / "synthetic-finding.json"
        receipts = findings._load_receipts(path)
        self.assertEqual(findings._bound_blockers(self.a, draft(), receipts), [])
        copied = self.b / "private" / "reviews" / "synthetic-finding.json"
        copied.parent.mkdir(parents=True)
        copied.write_bytes(path.read_bytes())
        blockers = findings._bound_blockers(self.b, draft(), findings._load_receipts(copied))
        self.assertIn("foreign_campaign_receipt", blockers)
        self.assertIn("missing_legal_review", blockers)

    def test_register_is_private_outside_the_repository_and_holds_names_and_paths_only(self):
        self.register_both()
        findings.add_receipt(self.a, draft(), "factual", "approve", "Synthetic Reviewer One", "Synthetic review")
        for command in (("status",), ("doctor",), ("campaigns", "status")):
            self.cli(*command, *(() if command[0] == "campaigns" else ("--campaign", "A")), check=0)
        self.assertEqual(oct(self.register.stat().st_mode & 0o777), "0o600")
        self.assertFalse(registry._within(self.register, REPO))
        entries = json.loads(self.register.read_text(encoding="utf-8"))
        self.assertTrue(all(set(e) == {"name", "directory", "added_at"} for e in entries))
        text = self.register.read_text(encoding="utf-8")
        for forbidden in (MARKER, "synthetic-finding", "Synthetic Reviewer", "Synthetic Author", "a" * 64, "agency", "request"):
            self.assertNotIn(forbidden, text)
        with mock.patch.dict(os.environ, {registry.ENV: str(REPO / "register.json")}):
            with self.assertRaises(ValueError):
                registry.register_path()

    def test_concurrent_adds_are_serialized_and_nothing_is_lost(self):
        roots = []
        for n in range(8):
            roots.append(self.base / f"C{n}")
            roots[-1].mkdir()
            (roots[-1] / "campaign.json").write_bytes((self.a / "campaign.json").read_bytes())
        env = dict(os.environ)
        procs = [subprocess.Popen([sys.executable, "-B", "-m", "campaign_tool", "campaigns", "add", f"n{i}", "--directory", str(r)],
                                  cwd=REPO, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE) for i, r in enumerate(roots)]
        for p in procs:
            p.communicate(timeout=60)
        self.assertEqual([p.returncode for p in procs], [0] * 8)
        names = sorted(e["name"] for e in json.loads(self.register.read_text(encoding="utf-8")))
        self.assertEqual(names, [f"n{i}" for i in range(8)])

    def test_an_add_waits_for_the_register_lock_and_then_sees_the_other_addition(self):
        registry.add("first", self.a)
        path = registry.register_path()
        with registry._transaction(path):  # another add is mid-transaction: it has read the register and not yet written
            entries = registry._load(path)
            proc = subprocess.Popen([sys.executable, "-B", "-m", "campaign_tool", "campaigns", "add", "second",
                                     "--directory", str(self.b)], cwd=REPO, env=dict(os.environ),
                                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            time.sleep(1.0)
            self.assertIsNone(proc.poll(), "the second add did not wait for the lock")
            registry._store(path, entries + [{"name": "mid", "directory": str(self.base / "mid"), "added_at": "x"}])
        proc.communicate(timeout=60)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual([e["name"] for e in json.loads(path.read_text(encoding="utf-8"))], ["first", "mid", "second"])
        json.loads(path.read_text(encoding="utf-8"))  # never corrupted

    def test_the_only_lock_is_on_the_register_file_and_never_in_a_campaign(self):
        locked, real = [], fcntl.flock

        def spy(fd, how):
            locked.append(os.path.realpath(f"/proc/self/fd/{fd}"))
            return real(fd, how)
        with mock.patch.object(fcntl, "flock", spy):
            self.assertEqual(self.inproc("campaigns", "add", "A", "--directory", self.a)[0], 0)
            self.assertEqual(self.inproc("campaigns", "add", "B", "--directory", self.b)[0], 0)
            self.assertEqual(len(locked), 2)
            for command in (("campaigns", "list"), ("campaigns", "status"), ("status", "--campaign", "A"),
                            ("doctor", "--campaign", "B")):
                code, _, err = self.inproc(*command)
                self.assertEqual(code, 0, (command, err))
            self.assertEqual(len(locked), 2, "a campaign command or status read took a lock")
            self.assertEqual(self.inproc("campaigns", "remove", "B")[0], 0)
        self.assertEqual(set(locked), {str(self.register)})
        self.assertEqual([p for p in self.base.rglob("*") if "lock" in p.name.lower()], [])

    def test_static_registry_has_no_delete_copy_sync_network_or_campaign_lock(self):
        tree = ast.parse((REPO / "campaign_tool" / "registry.py").read_text(encoding="utf-8"))
        imported = {n.name.split(".")[0] for x in ast.walk(tree) if isinstance(x, ast.Import) for n in x.names}
        imported |= {x.module.split(".")[0] for x in ast.walk(tree) if isinstance(x, ast.ImportFrom) and x.module}
        self.assertFalse(imported & {"socket", "http", "urllib", "ssl", "shutil", "requests", "sqlite3", "tempfile", "threading"})
        calls = {x.func.attr for x in ast.walk(tree) if isinstance(x, ast.Call) and isinstance(x.func, ast.Attribute)}
        self.assertFalse(calls & {"remove", "unlink", "rmdir", "removedirs", "rmtree", "copy", "copyfile", "copytree", "move",
                                  "symlink", "link", "rename", "system", "Popen", "run", "urlopen", "connect"})
        callers = {f.name for f in ast.walk(tree) if isinstance(f, ast.FunctionDef)
                   for x in ast.walk(f) if isinstance(x, ast.Call) and getattr(x.func, "attr", "") == "flock"}
        self.assertEqual(callers, {"_transaction"})  # the register's own transaction is the only lock, taken on register_path()
        opened = [x.args[0] for f in ast.walk(tree) if isinstance(f, ast.FunctionDef) and f.name == "_transaction"
                  for x in ast.walk(f) if isinstance(x, ast.Call) and ast.unparse(x.func) == "os.open"]
        self.assertEqual([ast.unparse(a) for a in opened], ["path"])  # the lock is on the register file passed in, nothing else


if __name__ == "__main__":
    unittest.main()
