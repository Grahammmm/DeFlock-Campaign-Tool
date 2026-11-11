import hashlib
import io
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from campaign_tool.records import approval_queue, cli, console_export, public_packets

ROOT = Path(__file__).resolve().parents[2]
RUN = "run_20261110T150000Z_ab12cd34"


def tree_state(root):
    """Every path with mode, size, mtime and content hash: any change shows."""
    out = []
    for base, dirs, names in os.walk(root):
        for name in sorted(dirs + names):
            path = os.path.join(base, name)
            info = os.lstat(path)
            body = hashlib.sha256(Path(path).read_bytes()).hexdigest() if stat.S_ISREG(info.st_mode) else ""
            out.append((os.path.relpath(path, root), info.st_mode, info.st_size, info.st_mtime_ns, body))
    return sorted(out)


def unlock(root):
    for base, dirs, names in os.walk(root):
        os.chmod(base, 0o700)
        for name in names:
            os.chmod(os.path.join(base, name), 0o600)


class ConsoleExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(self.cleanup)
        self.project = self.tmp / "project"
        outcomes = self.project / "run-outputs" / RUN / "outcomes"
        outcomes.mkdir(parents=True)
        (outcomes / "received-items.json").write_text('{"items": []}')
        (self.project / "run-outputs" / RUN / "text-state.json").write_text("{}")
        (self.project / "processing-v1").mkdir()
        db = sqlite3.connect(self.project / "processing-v1" / "ledger.sqlite")
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("create table t (a)")
        db.execute("insert into t values (1)")
        db.commit()
        self.db = db  # left open: the live WAL stays in place during the export
        (self.project / "agency-retention.csv").write_text("agency,days\n")
        for journal in ("approvals/queue.jsonl", "public-packets.jsonl", "public-updates.jsonl", "mail/outbox.json"):
            path = self.project / journal
            path.parent.mkdir(exist_ok=True)
            path.write_text('{"entry": "journal"}\n')
        self.dest = self.tmp / "dest"
        self.dest.mkdir(mode=0o700)

    def cleanup(self):
        self.db.close()
        unlock(self.tmp)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def cli(self, *args, stdin=None):
        return subprocess.run([sys.executable, "-B", "-m", "campaign_tool.records", *args], cwd=ROOT,
                              input=stdin, capture_output=True)

    def tar(self, run=RUN):
        result = self.cli("console-export", "--project", str(self.project), "--run-id", run, "--stdout")
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def do_import(self, data, dest=None):
        return self.cli("console-import", "--out", str(dest or self.dest), "--from-tar", "-", stdin=data)

    def piped(self):
        export = subprocess.Popen([sys.executable, "-B", "-m", "campaign_tool.records", "console-export",
                                   "--project", str(self.project), "--latest", "--stdout"],
                                  cwd=ROOT, stdout=subprocess.PIPE)
        imp = subprocess.run([sys.executable, "-B", "-m", "campaign_tool.records", "console-import",
                              "--out", str(self.dest), "--from-tar", "-"], cwd=ROOT, stdin=export.stdout,
                             capture_output=True)
        export.stdout.close()
        self.assertEqual(export.wait(), 0)
        return imp

    def only_export(self):
        names = os.listdir(self.dest / "exports")
        self.assertEqual(len(names), 1, names)
        return self.dest / "exports" / names[0]

    def test_commands_registered(self):
        self.assertEqual(cli.COMMANDS["console-export"], ("console_export", "export_main"))
        self.assertEqual(cli.COMMANDS["console-import"], ("console_export", "import_main"))

    def test_piped_export_holds_exactly_the_plain_inputs_read_only(self):
        result = self.piped()
        self.assertEqual(result.returncode, 0, result.stderr)
        export = self.only_export()
        files = sorted(os.path.relpath(os.path.join(b, n), export) for b, _, ns in os.walk(export) for n in ns)
        self.assertEqual(files, sorted([
            "EXPORT.json", "agency-retention.csv", "processing-v1/ledger.sqlite",
            f"run-outputs/{RUN}/outcomes/received-items.json", f"run-outputs/{RUN}/text-state.json"]))
        for entry in ("approvals", "mail", "public-packets.jsonl", "public-updates.jsonl"):
            self.assertFalse((export / entry).exists())
        for base, dirs, names in os.walk(export):
            self.assertEqual(stat.S_IMODE(os.stat(base).st_mode), 0o500, base)
            for name in names:
                self.assertEqual(stat.S_IMODE(os.stat(os.path.join(base, name)).st_mode), 0o400, name)
        manifest = json.loads((export / "EXPORT.json").read_text())
        self.assertEqual(manifest["run_id"], RUN)
        self.assertEqual(manifest["source_project_id"], approval_queue._project_id(self.project))
        for rel, digest in manifest["files"].items():
            self.assertEqual(hashlib.sha256((export / rel).read_bytes()).hexdigest(), digest, rel)
        link = self.dest / "current"
        self.assertEqual(os.readlink(link), f"exports/{export.name}")
        self.assertEqual(os.listdir(export / "processing-v1"), ["ledger.sqlite"])
        copy = sqlite3.connect(f"file:{export / 'processing-v1' / 'ledger.sqlite'}?mode=ro", uri=True)
        self.assertEqual(copy.execute("select a from t").fetchall(), [(1,)])
        copy.close()

    def test_existing_writers_fail_closed_on_the_copy(self):
        self.assertEqual(self.piped().returncode, 0)
        export = self.only_export()
        before, source_before = tree_state(self.dest), tree_state(self.project)
        with self.assertRaises(Exception):
            approval_queue.reject(str(export), "x", {"label": "a"}, "no")
        with self.assertRaises(Exception):
            public_packets.decide(str(export), "x", "rejected", {"label": "a"})
        self.assertEqual(tree_state(self.dest), before)
        self.assertEqual(tree_state(self.project), source_before)

    def test_tampered_import_keeps_previous_current(self):
        self.assertEqual(self.do_import(self.tar()).returncode, 0)
        link = os.readlink(self.dest / "current")
        data = io.BytesIO(self.tar())
        out = io.BytesIO()
        with tarfile.open(fileobj=data) as src, tarfile.open(fileobj=out, mode="w") as dst:
            for member in src:
                body = src.extractfile(member).read()
                if member.name == "agency-retention.csv":
                    body = b"tampered\n"
                member.size = len(body)
                dst.addfile(member, io.BytesIO(body))
        result = self.do_import(out.getvalue())
        self.assertEqual(result.returncode, 1)
        self.assertIn(b"EXPORT_HASH_MISMATCH", result.stderr)
        truncated = self.do_import(self.tar()[:600])
        self.assertEqual(truncated.returncode, 1)
        self.assertEqual(os.readlink(self.dest / "current"), link)
        self.assertEqual(len(os.listdir(self.dest / "exports")), 1)
        self.assertEqual([n for n in os.listdir(self.dest) if n.startswith(".staging")], [])

    def test_unlisted_member_and_unsafe_name_refused(self):
        for name in ("extra.txt", "../escape.txt"):
            out = io.BytesIO()
            with tarfile.open(fileobj=io.BytesIO(self.tar())) as src, tarfile.open(fileobj=out, mode="w") as dst:
                for member in src:
                    dst.addfile(member, src.extractfile(member))
                info = tarfile.TarInfo(name)
                info.size = 1
                dst.addfile(info, io.BytesIO(b"x"))
            self.assertEqual(self.do_import(out.getvalue()).returncode, 1, name)
        self.assertFalse((self.dest / "current").exists())
        self.assertFalse((self.tmp / "escape.txt").exists())

    def test_two_exports_current_points_at_second_first_kept(self):
        self.assertEqual(self.do_import(self.tar()).returncode, 0)
        first = os.readlink(self.dest / "current")
        (self.project / "run-outputs" / "second_run").mkdir()
        (self.project / "run-outputs" / "second_run" / "x.json").write_text("{}")
        self.assertEqual(self.do_import(self.tar("second_run")).returncode, 0)
        second = os.readlink(self.dest / "current")
        self.assertNotEqual(first, second)
        self.assertTrue(second.endswith("-second_run"))
        self.assertTrue((self.dest / first / "EXPORT.json").is_file())

    def test_dir_through_symlink_or_loose_mode_refused(self):
        link = self.tmp / "link"
        link.symlink_to(self.dest)
        result = self.do_import(self.tar(), link)
        self.assertEqual(result.returncode, 1)
        self.assertIn(b"EXPORT_DIR_INVALID", result.stderr)
        loose = self.tmp / "loose"
        loose.mkdir(mode=0o755)
        os.chmod(loose, 0o755)
        self.assertIn(b"EXPORT_DIR_INVALID", self.do_import(self.tar(), loose).stderr)

    def test_export_refuses_symlink_in_run_outputs(self):
        os.symlink(self.project / "approvals" / "queue.jsonl", self.project / "run-outputs" / RUN / "link.json")
        result = self.cli("console-export", "--project", str(self.project), "--run-id", RUN, "--stdout")
        self.assertEqual(result.returncode, 1)
        self.assertIn(b"SYMLINK_REFUSED", result.stderr)
        self.assertEqual(result.stdout, b"")

    def test_export_refuses_symlinked_source_parents(self):
        outside = self.tmp / "outside"
        (outside / RUN).mkdir(parents=True)
        (outside / RUN / "f.json").write_text("{}")
        shutil.rmtree(self.project / "run-outputs")
        os.symlink(outside, self.project / "run-outputs")
        result = self.cli("console-export", "--project", str(self.project), "--run-id", RUN, "--stdout")
        self.assertEqual(result.returncode, 1)
        self.assertIn(b"SYMLINK_REFUSED", result.stderr)
        self.assertEqual(result.stdout, b"")

    def test_export_refuses_symlinked_ledger_parent(self):
        self.db.close()
        moved = self.tmp / "ledger-outside"
        os.rename(self.project / "processing-v1", moved)
        os.symlink(moved, self.project / "processing-v1")
        result = self.cli("console-export", "--project", str(self.project), "--run-id", RUN, "--stdout")
        self.assertEqual(result.returncode, 1)
        self.assertIn(b"SYMLINK_REFUSED", result.stderr)
        self.assertEqual(result.stdout, b"")

    def swap_to_outside(self, name, outside):
        """Replace project/<name> with a symlink to outside (the original is moved away)."""
        os.rename(self.project / name, self.tmp / (name + "-moved"))
        os.symlink(outside, self.project / name)

    def test_parent_swapped_after_check_still_reads_pinned_run_outputs(self):
        outside = self.tmp / "outside"
        (outside / RUN).mkdir(parents=True)
        (outside / RUN / "text-state.json").write_text('{"external": true}')
        real_copy, swapped = console_export._copy_file, []

        def copy_after_swap(dir_fd, name, dst):
            if not swapped:
                swapped.append(True)
                self.swap_to_outside("run-outputs", outside)
            return real_copy(dir_fd, name, dst)

        with mock.patch.object(console_export, "_copy_file", copy_after_swap):
            console_export.build(str(self.project), RUN, str(self.dest))
        self.assertTrue(swapped)
        self.assertEqual((self.dest / "run-outputs" / RUN / "text-state.json").read_text(), "{}")
        for path in self.dest.rglob("*"):
            self.assertNotIn("external", path.read_text(errors="ignore") if path.is_file() else "")

    def test_ledger_parent_swapped_after_check_still_reads_pinned_ledger(self):
        outside = self.tmp / "ledger-outside"
        outside.mkdir()
        other = sqlite3.connect(outside / "ledger.sqlite")
        other.execute("create table ext (a)")
        other.commit()
        other.close()
        real_backup, swapped = console_export._backup_ledger, []

        def backup_after_swap(dir_fd, name, dst):
            swapped.append(True)
            self.swap_to_outside("processing-v1", outside)
            return real_backup(dir_fd, name, dst)

        with mock.patch.object(console_export, "_backup_ledger", backup_after_swap):
            console_export.build(str(self.project), RUN, str(self.dest))
        self.assertTrue(swapped)
        copy = sqlite3.connect(self.dest / "processing-v1" / "ledger.sqlite")
        tables = [row[0] for row in copy.execute("select name from sqlite_master")]
        copy.close()
        self.assertEqual(tables, ["t"])

    def test_ledger_leaf_swapped_to_symlink_before_open_is_refused(self):
        outside = self.tmp / "ledger-leaf-outside.sqlite"
        other = sqlite3.connect(outside)
        other.execute("create table ext (a)")
        other.commit()
        other.close()
        real_copy, swapped = console_export._copy_file, []

        def copy_after_swap(dir_fd, name, dst):
            if name == "ledger.sqlite" and not swapped:
                swapped.append(True)
                leaf = self.project / "processing-v1" / "ledger.sqlite"
                leaf.unlink()
                leaf.symlink_to(outside)
            return real_copy(dir_fd, name, dst)

        with mock.patch.object(console_export, "_copy_file", copy_after_swap):
            with self.assertRaises(console_export.ExportError) as caught:
                console_export.build(str(self.project), RUN, str(self.dest))
        self.assertTrue(swapped)
        self.assertIn("SYMLINK_REFUSED", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
