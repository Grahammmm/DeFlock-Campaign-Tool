"""Offline derivative admission: unsafe bytes remain denied inventory gaps."""
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from campaign_tool.records import extraction_ledger as el, runner_coverage as rc
from campaign_tool.records.run import Pipeline
from tests.records import test_runner_coverage as coverage_fixture
from tests.records.test_runner_coverage import sha
from tests.records.test_run_slice import synthetic_email

GAP = "canonical_unit_unreadable_or_unbound"


def fixture(directory):
    return coverage_fixture.RunnerCoverageTests().fixture(directory)


def fifo_probe():
    # No writer is ever opened. A parent process bounds even the unfixed reader.
    with tempfile.TemporaryDirectory(prefix="coverage-fifo-") as directory:
        p, subject, rows, _, _ = fixture(directory)
        path = Path(rows[0]["derived_path"])
        path.unlink()
        os.mkfifo(path, 0o600)
        units, inventory, _ = rc.unit_view(p, subject, 3)
        assert [u["unit_id"] for u in units] == [r["id"] for r in rows[1:]]
        assert len(inventory) == 3 and inventory[0]["blocked_reason"] == GAP
        assert stat.S_ISFIFO(path.lstat().st_mode)
    case = AdmissionTests()
    case.real_substitution("fifo")
    print("writerless FIFO: direct and real pipeline gaps; normal completion")


class AdmissionTests(unittest.TestCase):
    def assert_denied(self, p, subject, rows, limit=3):
        units, inventory, bound = rc.unit_view(p, subject, limit)
        self.assertEqual([u["unit_id"] for u in units], [r["id"] for r in rows[1:limit]])
        self.assertEqual([i["unit_id"] for i in inventory], [r["id"] for r in rows])
        self.assertEqual(inventory[0]["blocked_reason"], GAP)
        progress = rc.processing_coverage(p, subject, units, inventory, bound, stage="review")
        summary = progress["summary"]
        self.assertEqual(summary["inventory_total"], len(rows))
        self.assertEqual((summary["accepted_units"], summary["accepted_chars"]), (0, 0))
        self.assertTrue(progress["review_required"])
        self.assertFalse(progress["semantic_review_established"])
        gap = next(g for g in summary["gaps"] if g["unit_id"] == rows[0]["id"])
        self.assertIn("source_unit_blocked", gap["reasons"])
        return progress

    def real_substitution(self, kind):
        with tempfile.TemporaryDirectory(prefix="coverage-real-") as directory:
            base = Path(directory)
            inbox = base / "inbox"
            inbox.mkdir(mode=0o700)
            (inbox / "synthetic.eml").write_bytes(synthetic_email(attachment=None))
            p = Pipeline(base / "root")
            p.run(inbox)
            row = p._query("SELECT * FROM units", ())[0]
            subject = row["original_sha256"]
            units, inventory, bound = p._unit_view(subject)
            self.assertIn(row["id"], [u["unit_id"] for u in units])
            path = Path(row["derived_path"])
            saved = path.with_name(path.name + ".saved")
            path.rename(saved)
            if kind == "symlink":
                path.symlink_to(saved)
            else:
                os.mkfifo(path, 0o600)

            def snapshot():
                result = {}
                for f in base.rglob("*"):
                    info = f.lstat()
                    # SQLite read-only connections touch shared-memory lock metadata.
                    # Preserve bytes/modes everywhere, and timestamps on evidence files.
                    value = (info.st_mode, info.st_size)
                    if not f.name.endswith("-shm"):
                        value += (info.st_mtime_ns, info.st_ctime_ns)
                    if stat.S_ISREG(info.st_mode):
                        value += (sha(f.read_bytes()),)
                    elif stat.S_ISLNK(info.st_mode):
                        value += (os.readlink(f),)
                    result[str(f.relative_to(base))] = value
                return result

            before = snapshot()
            units, after_inventory, after_bound = p._unit_view(subject)
            self.assertNotIn(row["id"], [u["unit_id"] for u in units])
            self.assertEqual([u["unit_id"] for u in after_inventory], [u["unit_id"] for u in inventory])
            item = next(u for u in after_inventory if u["unit_id"] == row["id"])
            self.assertEqual(item["blocked_reason"], GAP)
            self.assertEqual(after_bound, bound)
            progress = rc.processing_coverage(p, subject, units, after_inventory, bound, stage="review")
            self.assertEqual(progress["summary"]["inventory_total"], len(inventory))
            self.assertEqual(progress["summary"]["accepted_units"], 0)
            self.assertEqual(progress["summary"]["accepted_chars"], 0)
            self.assertTrue(progress["review_required"])
            self.assertFalse(progress["semantic_review_established"])
            self.assertEqual(snapshot(), before)

    def test_real_current_cas_symlink_becomes_gap_without_writes(self):
        self.real_substitution("symlink")

    def test_writerless_fifo_returns_gap_without_blocking(self):
        result = subprocess.run([sys.executable, "-B", "-m", __name__, "--fifo-probe"],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("normal completion", result.stdout)

    def test_unit_payload_byte_limit(self):
        for size in (el.MAX_UNIT, el.MAX_UNIT + 1):
            with self.subTest(size=size), tempfile.TemporaryDirectory() as directory:
                p, subject, rows, _, _ = fixture(directory)
                text = "é" + "x" * (size - len('{"text":"é"}'.encode()))
                raw = json.dumps({"text": text}, ensure_ascii=False, separators=(",", ":")).encode()
                self.assertEqual(len(raw), size)
                Path(rows[0]["derived_path"]).write_bytes(raw)
                rows[0]["text_sha256"] = sha(text.encode())
                provenance = json.loads(rows[0]["provenance_json"])
                provenance["payload_sha256"] = sha(raw)
                rows[0]["provenance_json"] = json.dumps(provenance)
                if size == el.MAX_UNIT:
                    units, inventory, _ = rc.unit_view(p, subject, 3)
                    self.assertEqual(units[0]["text"], text)
                    self.assertEqual(len(inventory), 3)
                else:
                    # Admission must fail before parsing the oversized payload.
                    decode = rc.json.loads
                    def bounded_decode(value, *args, **kwargs):
                        self.assertNotEqual(value, raw)
                        return decode(value, *args, **kwargs)
                    with patch.object(rc.json, "loads", side_effect=bounded_decode):
                        self.assert_denied(p, subject, rows)

    def test_unsafe_derivative_types_and_permissions(self):
        for kind in ("symlink", "ancestor", "directory", "missing", "0640", "0604", "0400", "0600", "owner"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                p, subject, rows, _, _ = fixture(directory)
                path = Path(rows[0]["derived_path"])
                if kind in ("symlink", "ancestor"):
                    if kind == "symlink":
                        target = path.with_name("saved")
                        path.rename(target)
                        path.symlink_to(target)
                    else:
                        link = Path(directory) / "link"
                        link.symlink_to(directory, target_is_directory=True)
                        rows[0]["derived_path"] = str(link / path.name)
                elif kind in ("directory", "missing"):
                    path.unlink()
                    if kind == "directory":
                        path.mkdir(mode=0o700)
                elif kind != "owner":
                    path.chmod(int(kind, 8))
                if kind in ("0400", "0600"):
                    units, _, _ = rc.unit_view(p, subject, 3)
                    self.assertEqual(len(units), 3)
                elif kind == "owner":
                    fstat = el.os.fstat
                    def wrong_owner(fd):
                        info = fstat(fd)
                        return SimpleNamespace(st_mode=info.st_mode, st_uid=info.st_uid + 1)
                    # Limit ownership substitution to the first file's admission.
                    with patch.object(el.os, "fstat", side_effect=wrong_owner):
                        units, inventory, _ = rc.unit_view(p, subject, 3)
                    self.assertEqual(units, [])
                    self.assertEqual([i["blocked_reason"] for i in inventory], [GAP] * 3)
                else:
                    self.assert_denied(p, subject, rows)

    def test_open_swap_and_growth_fail_closed(self):
        for kind in ("swap", "growth", "changed"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                p, subject, rows, _, _ = fixture(directory)
                path = Path(rows[0]["derived_path"])
                real_open, real_read, real_close = os.open, os.read, os.close
                descriptors, closed, consumed = [], [], []
                def opening(name, flags, *args, **kwargs):
                    self.assertTrue(flags & os.O_NOFOLLOW)
                    self.assertTrue(flags & os.O_NONBLOCK)
                    if Path(name) == path and kind == "swap":
                        saved = path.with_name("saved")
                        path.rename(saved)
                        path.symlink_to(saved)
                    fd = real_open(name, flags, *args, **kwargs)
                    descriptors.append(fd)
                    return fd
                changed = False
                def reading(fd, count):
                    nonlocal changed
                    if not changed:
                        changed = True
                        with path.open("ab") as stream:
                            stream.write(b"x" * (el.MAX_UNIT if kind == "growth" else 1))
                    chunk = real_read(fd, count)
                    consumed.append(len(chunk))
                    return chunk
                def closing(fd):
                    closed.append(fd)
                    return real_close(fd)
                with patch.object(el.os, "open", side_effect=opening), patch.object(el.os, "close", side_effect=closing):
                    if kind == "swap":
                        self.assert_denied(p, subject, rows)
                    else:
                        # One selected file isolates the changed/growing descriptor.
                        with patch.object(el.os, "read", side_effect=reading):
                            self.assert_denied(p, subject, rows, limit=1)
                self.assertEqual(closed, descriptors)
                self.assertLessEqual(sum(consumed), el.MAX_UNIT + 1)

    def test_capped_unsafe_unit_is_not_opened_and_checkpoint_remains_denied(self):
        with tempfile.TemporaryDirectory() as directory:
            p, subject, rows, _, _ = fixture(directory)
            units, inventory, bound = rc.unit_view(p, subject, 2)
            prior = rc.processing_coverage(p, subject, units, inventory, bound, stage="review")
            Path(rows[0]["derived_path"]).chmod(0o644)
            capped = Path(rows[2]["derived_path"])
            capped.unlink()
            os.mkfifo(capped, 0o600)
            read = el.read_file
            opened = []
            def tracked(path, limit):
                opened.append(str(path))
                return read(path, limit)
            with patch.object(el, "read_file", side_effect=tracked):
                progress = self.assert_denied(p, subject, rows, limit=2)
            self.assertEqual(opened, [r["derived_path"] for r in rows[:2]])
            self.assertEqual(progress["summary"]["selected_units"], 1)
            units, inventory, bound = rc.unit_view(p, subject, 2)
            rejected = rc.processing_coverage(p, subject, units, inventory, bound,
                                               stage="review", checkpoint=prior["checkpoint"])
            self.assertEqual(rejected["resume"], {"status": "rejected", "reason": "checkpoint_binding"})
            self.assertEqual(rejected["checkpoint"]["attempts"], [])


if __name__ == "__main__":
    if sys.argv[1:] == ["--fifo-probe"]:
        fifo_probe()
    else:
        unittest.main()
