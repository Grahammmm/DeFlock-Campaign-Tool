"""kit-response --kind produced --record PREFIX links ingested ledger hashes to the step. Offline, synthetic."""
import contextlib
import hashlib
import io
import json
import re
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import cli, kit_track  # noqa: E402

SENT_ON = "2026-03-02"
FULL_HEX = re.compile(r"[0-9a-f]{64}")


def run(*arguments):
    """Run the CLI in-process; return (code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with mock.patch.object(sys, "argv", ["campaign_tool", *map(str, arguments)]), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            cli.main()
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


def synthetic(label):
    return hashlib.sha256(label.encode()).hexdigest()


class KitResponseRecordsTests(unittest.TestCase):
    def setUp(self):
        offline = mock.patch("urllib.request.urlopen", side_effect=AssertionError("network used"))
        offline.start()
        self.addCleanup(offline.stop)
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-kit-records-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.root = self.tmp / "campaign"
        for command in (("init", "--directory", self.root, "--name", "Synthetic", "--county", "San Luis Obispo",
                         "--state", "CA"), ("kit", "--directory", self.root)):
            code, _, err = run(*command)
            self.assertEqual(code, 0, err)
        self.sent = self.root / "kit" / "sent.json"
        self.agency = sorted((self.root / "kit" / "requests").glob("*.md"))[0].stem
        code, _, err = run("kit-sent", "--directory", self.root, "--agency", self.agency, "--date", SENT_ON,
                           "--channel", "email")
        self.assertEqual(code, 0, err)

    def ingest(self, *names):
        hashes = []
        for name in names:
            source = self.tmp / (name + ".txt")
            source.write_bytes(f"synthetic production page {name}\n".encode())
            code, _, err = run("ingest", "--directory", self.root, "--file", source, "--source-id", "synthetic-" + name)
            self.assertEqual(code, 0, err)
            hashes.append(hashlib.sha256(source.read_bytes()).hexdigest())
        return hashes

    def insert(self, *hashes):
        db = cli.database(self.root)
        with db:
            db.executemany("INSERT INTO objects (sha256, byte_count, stored_at) VALUES (?, 1, ?)",
                           [(h, "2026-03-01T00:00:00+00:00") for h in hashes])
        db.close()

    def step(self, on, kind, *prefixes):
        flags = [part for prefix in prefixes for part in ("--record", prefix)]
        return run("kit-response", "--directory", self.root, "--agency", self.agency, "--date", on, "--kind", kind,
                   *flags)

    def ok(self, on, kind, *prefixes):
        code, out, err = self.step(on, kind, *prefixes)
        self.assertEqual(code, 0, err)
        self.assertIsNone(FULL_HEX.search(out.replace(json.loads(self.sent.read_text())["requests"][self.agency]
                                                      ["draft_sha256"], "")), "full hash printed")
        return json.loads(out.split("Recorded only.")[0])

    def refused(self, on, kind, prefixes, *words):
        before = self.sent.read_bytes()
        code, out, err = self.step(on, kind, *prefixes)
        self.assertEqual(code, 1, out)
        self.assertTrue(err.startswith("Stopped: "), err)
        for word in words:
            self.assertIn(word, err)
        self.assertIsNone(FULL_HEX.search(err), "full hash in refusal")
        self.assertEqual(self.sent.read_bytes(), before)

    def stored(self):
        return json.loads(self.sent.read_text())["requests"][self.agency]["steps"]

    def row(self):
        code, out, err = run("kit-status", "--directory", self.root)
        self.assertEqual(code, 0, err)
        self.assertIsNone(FULL_HEX.search(out.replace(json.loads(self.sent.read_text())["requests"][self.agency]
                                                      ["draft_sha256"], "")), "full hash in status")
        return {row["agency_id"]: row for row in json.loads(out)["requests"]}[self.agency]

    def test_prefixes_bind_full_hashes_and_status_counts(self):
        first, second = self.ingest("a", "b")
        printed = self.ok("2026-03-20", "produced", first[:12], second[:20])
        self.assertEqual(self.stored()[-1]["records"], sorted([first, second]))
        self.assertEqual(printed["steps"][-1]["records"], sorted([first[:12], second[:12]]))
        self.assertEqual(stat.S_IMODE(self.sent.stat().st_mode), 0o600)
        row = self.row()
        self.assertEqual((row["records"], row["record_prefixes"]), (2, sorted([first[:12], second[:12]])))
        self.assertNotIn("more", row)

    def test_bad_prefixes_refused_by_position_untouched(self):
        (first,) = self.ingest("a")
        stem = synthetic("shared")[:12]
        twins = [stem + synthetic("x")[12:], stem + synthetic("y")[12:]]
        self.insert(*twins)
        cases = {"unknown": (synthetic("absent")[:12], "--record #2: not ingested: run ingest first"),
                 "11 chars": (first[:11], "--record #2: give at least 12 lowercase hex"),
                 "uppercase": (first[:12].upper(), "--record #2: give at least 12 lowercase hex"),
                 "ambiguous": (stem, "--record #2: ambiguous prefix (2 matches)")}
        for label, (bad, words) in cases.items():
            with self.subTest(label=label):
                self.refused("2026-03-20", "produced", [first[:12], bad], words)
        self.assertEqual(self.stored(), [])

    def test_record_only_on_produced(self):
        (first,) = self.ingest("a")
        for kind in ("acknowledged", "determination", "extension", "denied"):
            with self.subTest(kind=kind):
                self.refused("2026-03-05", kind, [first[:12]], "--record is only for produced steps")
        with self.assertRaisesRegex(ValueError, "only for produced"):
            kit_track.record_step(self.root, self.agency, "2026-03-05", "closed", records=[first[:12]])

    def test_duplicates_in_call_and_already_linked_refused(self):
        first, second, third = self.ingest("a", "b", "c")
        self.refused("2026-03-20", "produced", [first[:12], first[:16]], "--record #2: the same record is given twice")
        self.ok("2026-03-20", "produced", first[:12])
        self.refused("2026-03-27", "produced", [second[:12], first[:14]], "--record #2: already linked on 2026-03-20")
        printed = self.ok("2026-03-27", "produced", second[:12], third[:12])
        steps = self.stored()
        self.assertEqual([s["records"] for s in steps], [[first], sorted([second, third])])
        self.assertEqual(len(printed["steps"]), 2)
        row = self.row()
        self.assertEqual((row["records"], row["record_prefixes"]), (3, sorted(h[:12] for h in (first, second, third))))

    def test_no_ledger_refused_and_plain_produced_unchanged(self):
        self.refused("2026-03-20", "produced", [synthetic("a")[:12]], "--record #1: no ledger: run ingest first")
        printed = self.ok("2026-03-20", "produced")
        self.assertNotIn("records", printed["steps"][-1])
        self.assertNotIn("records", self.stored()[-1])
        self.assertEqual((self.row()["records"], self.row()["record_prefixes"]), (0, []))

    def test_limits_more_and_malformed_stored_records(self):
        many = sorted(synthetic(f"page-{n}") for n in range(60))
        self.insert(*many)
        self.refused("2026-03-20", "produced", [h[:16] for h in many] * 4, "at most 200 records per step")
        self.ok("2026-03-20", "produced", *[h[:16] for h in many])
        row = self.row()
        self.assertEqual((row["records"], row["record_prefixes"], row["more"]), (60, [h[:12] for h in many[:50]], 10))
        document = json.loads(self.sent.read_text())
        document["requests"][self.agency]["steps"][-1]["records"] = ["not-a-hash"]
        self.sent.write_text(json.dumps(document))
        self.refused("2026-03-21", "produced", [], "kit/sent.json unreadable: malformed steps")


if __name__ == "__main__":
    unittest.main()
