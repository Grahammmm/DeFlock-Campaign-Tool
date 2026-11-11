"""RF0330-2: add/decide refuse a prospective log the bounded reply/count readers could not read back.

Synthetic requests in a copy of the fictional example campaign; nothing is sent or published.
Run from the repository root: python3 -B -m unittest tests.test_correction_request_write_capacity -q
"""
import copy
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import correction_requests as cr  # noqa: E402

TODAY = date(2026, 3, 20)
FID = "fnd_0000000000000001"
LIMIT = 4194304
REFUSAL = f"private/correction-requests.json: update exceeds the {LIMIT}-byte reply/count reader limit; nothing was changed"
DECISION = dict(state="declined", decided_on="2026-02-04", note="no")
NEW_SUMMARY = "A new request."


def size(document):
    """Independent oracle: the pretty UTF-8 bytes the writer produces (POSIX newlines)."""
    return len((json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))


def row(number, summary):
    return {"id": number, "finding_id": FID, "received_on": "2026-02-02", "summary": summary, "state": "open"}


def rows_document(count, last):
    requests = [row(n, "a" * 500) for n in range(1, count)] + [row(count, last)]
    return {"schema_version": 1, "requests": requests}


def tuned(extra, after):
    """(before, after) documents whose prospective serialization is exactly ``extra`` bytes past LIMIT."""
    target = LIMIT + extra
    per = size(rows_document(2, "x")) - size(rows_document(1, "x"))
    count = max(2, (target - size(after(rows_document(1, "x")))) // per)
    while size(after(rows_document(count, "x"))) > target:
        count -= 1
    while size(after(rows_document(count + 1, "x"))) <= target:
        count += 1
    need = target - size(after(rows_document(count, "x"))) + 1  # bytes for the tuned summary (>= 1)
    wide = max(0, need - 500)  # two-byte characters only when 500 ASCII characters are not enough
    last = "é" * wide + "a" * (need - 2 * wide)
    assert 1 <= len(last) <= 500
    before = rows_document(count, last)
    result = after(copy.deepcopy(before))
    assert size(result) == target, (size(result), target)
    return before, result


def decided(document):
    document["requests"][0].update(DECISION)
    return document


def added(document):
    document["requests"].append(row(len(document["requests"]) + 1, NEW_SUMMARY))
    return document


class WriteCapacityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        self.root.mkdir()
        shutil.copytree(REPO_ROOT / "examples/fictional-campaign/content", self.root / "content")
        shutil.copy(REPO_ROOT / "examples/fictional-campaign/campaign.json", self.root / "campaign.json")
        (self.root / "private").mkdir()
        self.file = self.root / "private" / cr.NAME

    def seed(self, document, compact=False):
        text = json.dumps(document, separators=(",", ":"), ensure_ascii=False) if compact else \
            json.dumps(document, indent=2, ensure_ascii=False) + "\n"
        self.file.write_text(text, encoding="utf-8")
        os.chmod(self.file, 0o600)
        return self.file.read_bytes()

    def identity(self):
        info = self.file.stat()
        return self.file.read_bytes(), stat.S_IMODE(info.st_mode), info.st_mtime_ns, info.st_ino

    def readable(self):
        self.assertEqual(cr._reply_request(self.root, 1)["id"], 1)
        self.assertTrue(cr.quarterly_counts(self.root, TODAY))

    def mutate(self, which):
        if which == "decide":
            return cr.decide(self.root, 1, "declined", "2026-02-04", "no", today=TODAY)
        return cr.add(self.root, FID, "2026-02-02", NEW_SUMMARY, today=TODAY)

    def check_boundary(self, which, after):
        for extra, ok in ((-1, True), (0, True), (1, False)):
            with self.subTest(which=which, extra=extra):
                before, expected = tuned(extra, after)
                self.seed(before)
                self.readable()
                if ok:
                    self.mutate(which)
                    self.assertEqual(self.file.read_bytes(), (json.dumps(expected, indent=2, ensure_ascii=False)
                                                              + "\n").encode("utf-8"))
                    self.assertLessEqual(self.file.stat().st_size, LIMIT)
                    self.assertEqual(stat.S_IMODE(self.file.stat().st_mode), 0o600)
                    self.readable()
                else:
                    snapshot = self.identity()
                    siblings = sorted(p.name for p in self.file.parent.iterdir())
                    with self.assertRaises(ValueError) as caught:
                        self.mutate(which)
                    self.assertEqual(str(caught.exception), REFUSAL)
                    self.assertEqual(self.identity(), snapshot)
                    self.assertEqual({p.name for p in self.file.parent.iterdir()} - set(siblings), {".correction-requests.lock"}
                                     if ".correction-requests.lock" not in siblings else set())
                    self.readable()  # the retained log is still usable by the bounded readers

    def test_decide_boundary_is_inclusive_and_next_byte_refuses(self):
        self.check_boundary("decide", decided)

    def test_add_boundary_is_inclusive_and_next_byte_refuses(self):
        self.check_boundary("add", added)

    def test_compact_input_that_expands_on_rewrite_refuses_without_reformatting(self):
        before, _ = tuned(1, decided)
        compact = json.dumps(before, separators=(",", ":"), ensure_ascii=False)
        self.assertLess(len(compact.encode("utf-8")), LIMIT)
        for which in ("decide", "add"):
            with self.subTest(which=which):
                original = self.seed(before, compact=True)
                if which == "add":
                    before_add, _ = tuned(1, added)
                    original = self.seed(before_add, compact=True)
                with self.assertRaises(ValueError) as caught:
                    self.mutate(which)
                self.assertEqual(str(caught.exception), REFUSAL)
                self.assertEqual(self.file.read_bytes(), original)

    def test_refusal_message_carries_no_submitted_text_and_lock_is_released(self):
        before, _ = tuned(1, decided)
        self.seed(before)
        with self.assertRaises(ValueError) as caught:
            cr.decide(self.root, 1, "declined", "2026-02-04", "canary-note-xyz", today=TODAY)
        self.assertNotIn("canary", str(caught.exception))
        small, _ = tuned(-1000, decided)
        self.seed(small)  # a following valid transaction proceeds: the lock was released
        self.assertEqual(cr.decide(self.root, 1, "declined", "2026-02-04", "no", today=TODAY)["state"], "declined")

    def test_validation_precedes_capacity_and_small_logs_are_unchanged(self):
        before, _ = tuned(1, decided)
        self.seed(before)
        with self.assertRaises(ValueError) as caught:
            cr.decide(self.root, 1, "declined", "2026-02-01", "no", today=TODAY)  # before receipt
        self.assertIn("before the request was received", str(caught.exception))
        shutil.rmtree(self.root / "private")
        (self.root / "private").mkdir()
        request = cr.add(self.root, FID, "2026-02-02", "Small é request.", today=TODAY)
        self.assertEqual(request["id"], 1)
        self.assertEqual(self.file.read_bytes(), (json.dumps({"schema_version": 1, "requests": [request]}, indent=2,
                                                             ensure_ascii=False) + "\n").encode("utf-8"))

    def test_cli_stops_with_fixed_error_and_no_success_output(self):
        before, _ = tuned(1, decided)
        original = self.seed(before)
        for args in (("decide", "--id", "1", "declined", "--on", "2026-02-04", "--note", "canary-note"),
                     ("add", "--finding", FID, "--received", "2026-02-02", "--summary", "canary-summary")):
            with self.subTest(command=args[0]):
                result = subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "correction-request", args[0],
                                         "--directory", str(self.root), *args[1:]],
                                        cwd=REPO_ROOT, capture_output=True, text=True, timeout=120)
                self.assertEqual(result.returncode, 3, result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertIn("update exceeds the 4194304-byte reply/count reader limit", result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                self.assertNotIn("canary", result.stderr)
                self.assertEqual(self.file.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
