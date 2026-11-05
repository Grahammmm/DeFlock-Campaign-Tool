"""Arrival stamps: API, CLI, privacy and fixed-snapshot consume (REC-AUTO-1)."""
import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from campaign_tool.records import arrival, run_orchestrator, run_report  # noqa: E402

REF_A = hashlib.sha256(b"a").hexdigest()
REF_B = hashlib.sha256(b"b").hexdigest()
RUN_1 = "run_20261104T180000Z_0123abcd"
RUN_2 = "run_20261104T181000Z_4567abcd"


def cli(*args):
    return subprocess.run([sys.executable, "-B", "-m", "campaign_tool.records", "arrival", *args],
                          cwd=ROOT, capture_output=True, text=True, timeout=60)


class ArrivalCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.runtime = self.tmp / "runtime"
        self.runtime.mkdir(mode=0o700)

    def code(self, call, *args):
        with self.assertRaises(arrival.ArrivalError) as caught:
            call(*args)
        return caught.exception.code


class StampTests(ArrivalCase):
    def test_stamp_is_private_atomic_and_metadata_only(self):
        for source in arrival.SOURCES:
            name = arrival.stamp(self.runtime, source, REF_A)
            path = self.runtime / "arrival" / name
            self.assertEqual(set(json.loads(path.read_text())), {"source", "ref", "seen_at"})
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertTrue(name.endswith(f"-{source}.json"))
        for directory in (self.runtime / "arrival", self.runtime / "arrival" / "done"):
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        [first, second] = arrival.pending(self.runtime)
        self.assertEqual({first["source"], second["source"]}, set(arrival.SOURCES))
        self.assertTrue(first["seen_at"].endswith("Z"))
        self.assertEqual(sorted(p.name for p in (self.runtime / "arrival").iterdir() if p.is_file()),
                         sorted(p.name for p in (self.runtime / "arrival").glob("2*.json")))

    def test_concurrent_same_instant_stamps_never_overwrite(self):
        threads = [threading.Thread(target=arrival.stamp, args=(self.runtime, "drop-folder", REF_A))
                   for _ in range(24)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(arrival.pending(self.runtime)), 24)

    def test_invalid_inputs_are_refused_with_fixed_codes(self):
        self.assertEqual(self.code(arrival.stamp, self.runtime, "email", REF_A), "ARRIVAL_SOURCE_INVALID")
        for ref in ("", "abc", REF_A.upper(), REF_A + "0", "x" * 64, None):
            self.assertEqual(self.code(arrival.stamp, self.runtime, "inbox-watch", ref), "ARRIVAL_REF_INVALID")
        self.assertEqual(self.code(arrival.stamp, "relative", "inbox-watch", REF_A), "ARRIVAL_RUNTIME_INVALID")
        self.assertEqual(self.code(arrival.stamp, self.tmp / "missing", "inbox-watch", REF_A),
                         "ARRIVAL_RUNTIME_INVALID")
        self.assertFalse((self.runtime / "arrival").exists())

    def test_permissive_directory_and_symlink_are_refused(self):
        (self.runtime / "arrival").mkdir(mode=0o755)
        os.chmod(self.runtime / "arrival", 0o755)
        self.assertEqual(self.code(arrival.stamp, self.runtime, "inbox-watch", REF_A), "ARRIVAL_STATE_UNSAFE")
        os.rmdir(self.runtime / "arrival")
        real = self.tmp / "elsewhere"
        real.mkdir(mode=0o700)
        os.symlink(real, self.runtime / "arrival")
        self.assertEqual(self.code(arrival.stamp, self.runtime, "inbox-watch", REF_A), "ARRIVAL_STATE_UNSAFE")
        self.assertEqual(list(real.iterdir()), [])

    def test_publication_failure_is_not_acknowledged(self):
        arrival.stamp(self.runtime, "inbox-watch", REF_A)
        os.chmod(self.runtime / "arrival", 0o500)
        self.addCleanup(os.chmod, self.runtime / "arrival", 0o700)
        if os.access(self.runtime / "arrival", os.W_OK):
            self.skipTest("directory permissions are not enforced for this user")
        self.assertEqual(self.code(arrival.stamp, self.runtime, "inbox-watch", REF_B), "ARRIVAL_PUBLISH_FAILED")

    def test_cli_stamp_and_list_leak_nothing(self):
        done = cli("stamp", "--runtime-dir", str(self.runtime), "--source", "inbox-watch", "--ref", REF_A)
        self.assertEqual((done.returncode, done.stdout, done.stderr), (0, "", ""))
        listed = cli("list", "--runtime-dir", str(self.runtime))
        self.assertEqual(json.loads(listed.stdout), arrival.pending(self.runtime))
        bad_ref = "private-message-id@example.invalid"
        refused = cli("stamp", "--runtime-dir", str(self.runtime), "--source", "inbox-watch", "--ref", bad_ref)
        self.assertEqual(refused.returncode, 2)
        self.assertIn("ARRIVAL_REF_INVALID", refused.stderr)
        self.assertNotIn(bad_ref, refused.stdout + refused.stderr)
        self.assertEqual(len(arrival.pending(self.runtime)), 1)

    def test_list_does_not_create_directories(self):
        self.assertEqual(json.loads(cli("list", "--runtime-dir", str(self.runtime)).stdout), [])
        self.assertFalse((self.runtime / "arrival").exists())
        self.assertEqual(os.listdir(self.runtime), [])


class PendingConsumeTests(ArrivalCase):
    def stamps(self, count):
        return [arrival.stamp(self.runtime, "inbox-watch", hashlib.sha256(bytes([n])).hexdigest())
                for n in range(count)]

    def test_pending_is_ordered_and_ignores_coordination_files(self):
        names = self.stamps(3)
        arrival_dir = self.runtime / "arrival"
        (arrival_dir / ".tmp-1-abc").write_text("{")
        (arrival_dir / "notes.txt").write_text("x")
        self.assertEqual([s["ref"] for s in arrival.pending(self.runtime)],
                         [json.loads((arrival_dir / n).read_text())["ref"] for n in sorted(names)])
        before = sorted(os.listdir(arrival_dir))
        arrival.pending(self.runtime)
        self.assertEqual(sorted(os.listdir(arrival_dir)), before)

    def test_malformed_oversized_extra_field_and_hardlinked_stamps_are_refused(self):
        name = self.stamps(1)[0]
        path = self.runtime / "arrival" / name
        good = path.read_text()
        for body, code in (("{", "ARRIVAL_STAMP_INVALID"), ("x" * 5000, "ARRIVAL_STAMP_INVALID"),
                           (json.dumps({"source": "inbox-watch", "ref": REF_A,
                                        "seen_at": "2026-11-04T10:00:00Z", "subject": "hi"}),
                            "ARRIVAL_STAMP_INVALID"),
                           (json.dumps({"source": "inbox-watch", "ref": REF_A,
                                        "seen_at": "2026-02-31T10:00:00Z"}), "ARRIVAL_SEEN_INVALID")):
            path.write_text(body)
            self.assertEqual(self.code(arrival.pending, self.runtime), code)
        path.write_text(good)
        os.link(path, self.tmp / "alias")
        self.assertEqual(self.code(arrival.pending, self.runtime), "ARRIVAL_STATE_UNSAFE")
        os.unlink(self.tmp / "alias")
        os.chmod(path, 0o644)
        self.assertEqual(self.code(arrival.pending, self.runtime), "ARRIVAL_STATE_UNSAFE")
        os.chmod(path, 0o600)
        os.unlink(path)
        os.symlink(self.tmp, path)
        self.assertEqual(self.code(arrival.pending, self.runtime), "ARRIVAL_STATE_UNSAFE")

    def test_backlog_over_one_batch_drains_without_loss(self):
        with mock.patch.object(arrival, "MAX_STAMPS", 4):
            self.stamps(10)
            self.assertEqual(len(arrival.pending(self.runtime)), 4)
            self.assertTrue(arrival.has_work(self.runtime, stamps_only=True))
            taken = []
            for run in (RUN_1, RUN_2, "run_20261104T182000Z_89abcdef"):
                batch = arrival.consume(self.runtime, run)
                self.assertLessEqual(len(batch), 4)
                taken.append(len(batch))
            self.assertEqual(taken, [4, 4, 2])
            self.assertFalse(arrival.has_work(self.runtime, stamps_only=True))
            self.assertEqual(arrival.pending(self.runtime), [])

    def test_real_burst_over_max_stamps_drains_in_bounded_batches(self):
        total = arrival.MAX_STAMPS + 1
        for n in range(total):
            arrival.stamp(self.runtime, "inbox-watch", hashlib.sha256(n.to_bytes(4, "big")).hexdigest())
        self.assertEqual(len(arrival.pending(self.runtime)), arrival.MAX_STAMPS)
        first = arrival.consume(self.runtime, RUN_1)
        self.assertEqual(len(first), arrival.MAX_STAMPS)
        rest = arrival.consume(self.runtime, RUN_2)
        self.assertEqual(len(rest), 1)
        refs = {s["ref"] for s in first + rest}
        self.assertEqual(len(refs), total)
        self.assertEqual(arrival.pending(self.runtime), [])

    def test_consume_claims_a_fixed_snapshot_and_is_idempotent(self):
        self.stamps(2)
        expected = arrival.pending(self.runtime)
        self.assertEqual(arrival.consume(self.runtime, RUN_1), expected)
        self.assertEqual(arrival.pending(self.runtime), [])
        late = arrival.stamp(self.runtime, "drop-folder", REF_B)
        self.assertEqual(arrival.consume(self.runtime, RUN_1), expected)  # never takes the late stamp
        self.assertEqual([s["ref"] for s in arrival.pending(self.runtime)], [REF_B])
        self.assertTrue((self.runtime / "arrival" / late).exists())
        self.assertEqual([s["ref"] for s in arrival.consume(self.runtime, RUN_2)], [REF_B])
        self.assertEqual(arrival.consume(self.runtime, "run_20261104T182000Z_89abcdef"), [])

    def test_two_consumers_never_duplicate_or_lose_a_stamp(self):
        self.stamps(10)
        results = {}

        def worker(run_id):
            results[run_id] = arrival.consume(self.runtime, run_id)

        threads = [threading.Thread(target=worker, args=(r,)) for r in (RUN_1, RUN_2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        claimed = [s["ref"] for batch in results.values() for s in batch]
        self.assertEqual(len(claimed), 10)
        self.assertEqual(len(set(claimed)), 10)

    def test_crash_between_moves_is_recovered_without_loss(self):
        names = sorted(self.stamps(3))
        done = self.runtime / "arrival" / "done"
        batch = done / RUN_1
        batch.mkdir(mode=0o700)
        (done / f".{RUN_1}.claim").write_text(json.dumps(names))
        os.chmod(done / f".{RUN_1}.claim", 0o600)
        os.rename(self.runtime / "arrival" / names[0], batch / names[0])  # crashed after one move
        self.stamps(1)  # a new arrival must not join the claimed batch
        again = arrival.consume(self.runtime, RUN_1)
        self.assertEqual(len(again), 3)
        self.assertEqual(sorted(os.listdir(batch)), names)
        self.assertEqual(len(arrival.pending(self.runtime)), 1)

    def test_invalid_run_id_and_traversal_are_refused(self):
        for run_id in ("", "run_1", "../../etc", RUN_1 + "/x", None, "run_20261104T180000Z_0123ABCD"):
            self.assertEqual(self.code(arrival.consume, self.runtime, run_id), "ARRIVAL_RUN_ID_INVALID")

    def test_marker_set_take_and_claim_clear(self):
        self.assertFalse(arrival.take_pending(self.runtime))
        arrival.mark_pending(self.runtime)
        self.assertEqual(stat.S_IMODE((self.runtime / "arrival.pending").stat().st_mode), 0o600)
        self.assertTrue(arrival.has_work(self.runtime))
        self.assertFalse(arrival.has_work(self.runtime, stamps_only=True))
        arrival.claim(self.runtime, RUN_1)
        self.assertFalse((self.runtime / "arrival.pending").exists())
        arrival.mark_pending(self.runtime)
        self.assertTrue(arrival.take_pending(self.runtime))
        self.assertFalse(arrival.take_pending(self.runtime))


class DropScanTests(ArrivalCase):
    def setUp(self):
        super().setUp()
        self.drop = self.tmp / "drop"
        self.drop.mkdir()

    def put(self, name, data=b"%PDF-1.4 synthetic", age=60):
        path = self.drop / name
        path.write_bytes(data)
        stamp = time.time() - age
        os.utime(path, (stamp, stamp))
        return path

    def test_new_file_is_stamped_once_and_unchanged_folder_is_quiet(self):
        self.put("scan.pdf")
        self.assertEqual(arrival.scan_drop(self.runtime, self.drop), 1)
        [item] = arrival.pending(self.runtime)
        self.assertEqual(item["source"], "drop-folder")
        self.assertNotIn("scan", json.dumps(item))
        self.assertEqual(arrival.scan_drop(self.runtime, self.drop), 0)
        self.assertEqual(len(arrival.pending(self.runtime)), 1)
        self.assertEqual(stat.S_IMODE((self.runtime / "arrival-drop.json").stat().st_mode), 0o600)

    def test_changed_final_bytes_are_a_new_arrival(self):
        self.put("scan.pdf")
        arrival.scan_drop(self.runtime, self.drop)
        self.put("scan.pdf", b"%PDF-1.4 synthetic, replaced")
        self.assertEqual(arrival.scan_drop(self.runtime, self.drop), 1)
        self.assertEqual(len(arrival.pending(self.runtime)), 2)

    def test_staging_partial_unsafe_and_unsettled_files_are_not_acknowledged(self):
        self.put("a.pdf.tmp")
        self.put(".hidden")
        self.put("b.part")
        self.put("fresh.pdf", age=0)
        os.symlink(self.tmp, self.drop / "link.pdf")
        (self.drop / "subdir").mkdir()
        self.assertEqual(arrival.scan_drop(self.runtime, self.drop), 0)
        self.assertEqual(arrival.pending(self.runtime), [])
        stale = self.drop / "fresh.pdf"
        os.utime(stale, (time.time() - 60,) * 2)  # settled on a later wake: now acknowledged
        self.assertEqual(arrival.scan_drop(self.runtime, self.drop), 1)

    def test_unsettled_files_are_reported_so_the_caller_can_rescan(self):
        self.put("fresh.pdf", age=0)
        self.assertEqual(arrival.scan_drop_status(self.runtime, self.drop), (0, 1, False))
        os.utime(self.drop / "fresh.pdf", (time.time() - 60,) * 2)
        self.assertEqual(arrival.scan_drop_status(self.runtime, self.drop), (1, 0, False))

    def test_retained_full_prefix_does_not_starve_a_later_new_file(self):
        with mock.patch.object(arrival, "MAX_DROP_FILES", 5):
            for n in range(5):
                self.put(f"a{n}.pdf")
            self.put(".hidden-0")  # staging names never consume the cap
            self.put("x.part")
            self.assertEqual(arrival.scan_drop_status(self.runtime, self.drop), (5, 0, False))
            self.put("z-new.pdf")
            self.assertEqual(arrival.scan_drop_status(self.runtime, self.drop), (0, 0, True))
            self.assertEqual(arrival.scan_drop_status(self.runtime, self.drop), (1, 0, False))
            self.assertEqual(arrival.scan_drop_status(self.runtime, self.drop), (0, 0, True))  # next wake: new cycle
            self.assertEqual(arrival.scan_drop_status(self.runtime, self.drop), (0, 0, False))
            self.assertEqual(len(arrival.pending(self.runtime)), 6)

    def test_unsettled_file_on_an_earlier_page_survives_pagination(self):
        with mock.patch.object(arrival, "MAX_DROP_FILES", 5):
            for n in range(5):
                self.put(f"b{n}.pdf")
            self.assertEqual(arrival.scan_drop_status(self.runtime, self.drop), (5, 0, False))
            self.put("a-fresh.pdf", age=0)  # sorts before every retained file
            self.assertEqual(arrival.scan_drop_status(self.runtime, self.drop), (0, 1, True))
            self.assertEqual(arrival.scan_drop_status(self.runtime, self.drop), (0, 1, False))
            os.utime(self.drop / "a-fresh.pdf", (time.time() - 60,) * 2)
            self.assertEqual(arrival.scan_drop_status(self.runtime, self.drop), (1, 0, True))
            self.assertEqual(arrival.scan_drop_status(self.runtime, self.drop), (0, 0, False))

    def test_trigger_never_reads_record_bytes(self):
        path = self.put("scan.pdf")
        os.chmod(path, 0)
        self.addCleanup(os.chmod, path, 0o600)
        self.assertEqual(arrival.scan_drop(self.runtime, self.drop), 1)

    def test_missing_and_relative_drop_dirs(self):
        self.assertEqual(arrival.scan_drop(self.runtime, self.tmp / "nope"), 0)
        self.assertEqual(self.code(arrival.scan_drop, self.runtime, "relative"), "ARRIVAL_DROP_INVALID")
        self.assertEqual(os.listdir(self.runtime), [])


class ReportMetadataTests(unittest.TestCase):
    def report(self, **fields):
        report = run_orchestrator.abort_report("run_20261104T180000Z_0123abcd", "arrival", "2026-11-04T18:00:00Z",
                                               "DISK_SPACE_LOW", trigger="arrival",
                                               arrivals=[{"source": "inbox-watch", "ref": REF_A,
                                                          "seen_at": "2026-11-04T17:59:59.123456Z"}])
        report.update(fields)
        return report

    def test_arrival_report_and_legacy_report_validate(self):
        run_report.validate_run_report(self.report())
        legacy = self.report()
        for key in ("trigger", "arrivals"):
            del legacy[key]
        legacy["mode"] = "scheduled"
        run_report.validate_run_report(legacy)
        manual = run_orchestrator.abort_report("run_20261104T180000Z_0123abcd", "manual", "2026-11-04T18:00:00Z",
                                               "DISK_SPACE_LOW", trigger="manual", arrivals=[])
        self.assertEqual((manual["trigger"], manual["arrivals"]), ("manual", []))

    def test_privacy_and_strictness_failures(self):
        good = {"source": "inbox-watch", "ref": REF_A, "seen_at": "2026-11-04T17:59:59Z"}
        for bad in ({**good, "subject": "x"}, {**good, "ref": "/home/user/mail.eml"},
                    {**good, "source": "email"}, {**good, "seen_at": "2026-02-30T10:00:00Z"},
                    {**good, "seen_at": "yesterday"}, {"source": "inbox-watch", "ref": REF_A}):
            with self.assertRaises(ValueError, msg=str(bad)):
                run_report.validate_run_report(self.report(arrivals=[bad]))
        with self.assertRaises(ValueError):
            run_report.validate_run_report(self.report(trigger="manual"))
        with self.assertRaises(ValueError):
            run_report.validate_run_report(self.report(mode="test"))
        with self.assertRaises(ValueError):
            run_report.validate_run_report(self.report(trigger="other"))

    def test_arrival_mode_wins_over_test_plan_only_with_the_flag(self):
        plan = {"mode": "test"}
        self.assertEqual(run_orchestrator.report_mode(plan, False, True), "arrival")
        self.assertEqual(run_orchestrator.report_mode(plan, True), "test")
        self.assertEqual(run_orchestrator.report_mode({"mode": "live"}, True), "scheduled")
        self.assertEqual(run_orchestrator.report_mode({"mode": "live"}, False), "manual")


if __name__ == "__main__":
    unittest.main()
