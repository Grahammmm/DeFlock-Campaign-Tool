"""kit-sent / kit-response --correspondence PREFIX: ledger hashes of letters kept apart from production records."""
import contextlib
import hashlib
import io
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import cli, kit_track  # noqa: E402
from tests.test_kit_check import ready_drafts  # noqa: E402

SENT_ON = "2026-03-02"
FULL_HEX = re.compile(r"[0-9a-f]{64}")


def run(*arguments):
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


class KitCorrespondenceTests(unittest.TestCase):
    def setUp(self):
        offline = mock.patch("urllib.request.urlopen", side_effect=AssertionError("network used"))
        offline.start()
        self.addCleanup(offline.stop)
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-kit-correspondence-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.root = self.tmp / "campaign"
        for command in (("init", "--directory", self.root, "--name", "Synthetic", "--county", "San Luis Obispo",
                         "--state", "CA"), ("kit", "--directory", self.root)):
            code, _, err = run(*command)
            self.assertEqual(code, 0, err)
        self.sent = self.root / "kit" / "sent.json"
        self.agency = sorted((self.root / "kit" / "requests").glob("*.md"))[0].stem
        ready_drafts(self.root)
        agencies = self.root / "kit" / "agencies.json"
        listed = json.loads(agencies.read_text())
        for item in listed:
            item["selected"] = item["agency_id"] == self.agency
        agencies.write_text(json.dumps(listed))
        self.hashes = self.ingest("a", "b", "c", "d")

    def ingest(self, *names):
        hashes = []
        for name in names:
            source = self.tmp / (name + ".txt")
            source.write_bytes(f"synthetic letter {name}\n".encode())
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

    def sentcmd(self, *extra):
        return run("kit-sent", "--directory", self.root, "--agency", self.agency, "--date", SENT_ON,
                   "--channel", "email", *extra)

    def sends(self, *extra):
        code, out, err = self.sentcmd(*extra)
        self.assertEqual(code, 0, err)
        return json.loads(out.split("Recorded only.")[0])

    def step(self, on, kind, *flags):
        return run("kit-response", "--directory", self.root, "--agency", self.agency, "--date", on, "--kind", kind,
                   *flags)

    def ok(self, on, kind, *flags):
        code, out, err = self.step(on, kind, *flags)
        self.assertEqual(code, 0, err)
        return json.loads(out.split("Recorded only.")[0])

    def refused(self, command, words):
        before = self.sent.read_bytes()
        code, out, err = command
        self.assertEqual(code, 1, out)
        self.assertTrue(err.startswith("Stopped: "), err)
        for word in words:
            self.assertIn(word, err)
        self.assertIsNone(FULL_HEX.search(err), "full hash in refusal")
        self.assertEqual(self.sent.read_bytes(), before)

    def stored(self):
        return json.loads(self.sent.read_text())["requests"][self.agency]

    def row(self):
        code, out, err = run("kit-status", "--directory", self.root)
        self.assertEqual(code, 0, err)
        return {row["agency_id"]: row for row in json.loads(out)["requests"]}[self.agency]

    def production(self):  # every production number: status, coverage row and totals, CSV
        row, report = self.row(), kit_track.coverage(self.root)
        code, csv_out, err = run("kit-coverage", "--directory", self.root, "--csv")
        self.assertEqual(code, 0, err)
        return ([row["records"], row["record_prefixes"], row.get("more")], report["rows"][0]["records"],
                report["totals"]["with_records"], csv_out)

    def test_legacy_produced_step_records_read_unchanged(self):
        a, b = self.hashes[:2]
        self.sends()
        document = json.loads(self.sent.read_text())
        document["requests"][self.agency]["steps"] = [{"on": "2026-03-20", "kind": "produced", "records": [a, b],
                                                       "recorded_at": "2026-03-20T10:00:00+00:00"}]
        self.sent.write_text(json.dumps(document))
        before = self.sent.read_bytes()
        row = self.row()
        self.assertEqual((row["records"], row["record_prefixes"]), (2, sorted([a[:12], b[:12]])))
        self.assertEqual((row["correspondence"], row["correspondence_prefixes"]), (0, []))
        self.assertNotIn("correspondence_more", row)
        self.assertEqual(self.production()[1:3], (2, 1))
        self.assertEqual(self.sent.read_bytes(), before)

    def test_new_produced_record_then_correspondence_on_same_step(self):
        a, b = self.hashes[:2]
        self.sends()
        self.ok("2026-03-20", "produced", "--record", a[:12])
        base = self.production()
        self.assertEqual(base[0][:2], [1, [a[:12]]])
        self.ok("2026-03-27", "produced", "--correspondence", b[:14])
        self.assertEqual(self.stored()["steps"][-1]["correspondence"], [b])
        self.assertNotIn("records", self.stored()["steps"][-1])
        self.assertEqual(self.production()[:3], base[:3])
        row = self.row()
        self.assertEqual((row["correspondence"], row["correspondence_prefixes"]), (1, [b[:12]]))

    def test_same_step_record_and_correspondence_keep_apart(self):
        a, b = self.hashes[:2]
        self.sends()
        printed = self.ok("2026-03-20", "produced", "--record", a[:12], "--correspondence", b[:12])
        step = self.stored()["steps"][-1]
        self.assertEqual((step["records"], step["correspondence"]), ([a], [b]))
        self.assertEqual((printed["steps"][-1]["records"], printed["steps"][-1]["correspondence"]), ([a[:12]], [b[:12]]))
        self.assertEqual(self.production()[:3], ([1, [a[:12]], None], 1, 1))

    def test_correspondence_only_on_acknowledged_and_produced_leaves_production_zero(self):
        a, b = self.hashes[:2]
        self.sends()
        base = self.production()
        self.assertEqual(base[:3], ([0, [], None], 0, 0))
        self.ok("2026-03-05", "acknowledged", "--correspondence", a[:12])
        self.assertEqual(self.production()[:3], base[:3])
        self.ok("2026-03-20", "produced", "--correspondence", b[:12])
        self.assertEqual(self.production()[:3], base[:3])
        row = self.row()
        self.assertEqual((row["records"], row["correspondence"]), (0, 2))
        self.assertEqual(row["correspondence_prefixes"], sorted([a[:12], b[:12]]))

    def test_kit_sent_correspondence_and_replace_rules(self):
        a, b = self.hashes[:2]
        printed = self.sends("--correspondence", a[:12])
        self.assertEqual(printed["correspondence"], [a[:12]])
        self.assertEqual(self.stored()["correspondence"], [a])
        self.sends("--replace")
        self.assertEqual(self.stored()["correspondence"], [a])
        self.sends("--replace", "--correspondence", b[:12])
        self.assertEqual(self.stored()["correspondence"], [b])
        self.assertEqual(self.row()["correspondence_prefixes"], [b[:12]])
        self.assertEqual(self.production()[:3], ([0, [], None], 0, 0))

    def test_same_hash_twice_or_already_linked_refused_untouched(self):
        a, b, c = self.hashes[:3]
        self.sends("--correspondence", a[:12])
        self.refused(self.step("2026-03-20", "produced", "--record", b[:12], "--correspondence", b[:16]),
                     ["--correspondence #1: already linked on this call as record"])
        self.refused(self.step("2026-03-20", "acknowledged", "--correspondence", c[:12], "--correspondence", c[:16]),
                     ["--correspondence #2: the same record is given twice"])
        self.refused(self.step("2026-03-20", "acknowledged", "--correspondence", a[:12]),
                     ["already linked on 2026-03-02 as correspondence"])
        self.ok("2026-03-20", "produced", "--record", b[:12], "--correspondence", c[:12])
        self.refused(self.step("2026-03-27", "produced", "--record", c[:12]),
                     ["--record #1: already linked on 2026-03-20 as correspondence"])
        self.refused(self.step("2026-03-27", "produced", "--correspondence", b[:12]),
                     ["--correspondence #1: already linked on 2026-03-20 as record"])
        self.refused(self.step("2026-03-27", "produced", "--correspondence", c[:12]),
                     ["already linked on 2026-03-20 as correspondence"])

    def test_unknown_ambiguous_and_short_prefix_refused_before_any_write(self):
        a = self.hashes[0]
        stem = synthetic("shared")[:12]
        self.insert(stem + synthetic("x")[12:], stem + synthetic("y")[12:])
        code, _, err = self.sentcmd("--correspondence", synthetic("absent")[:12])
        self.assertEqual((code, self.sent.exists()), (1, False), err)
        self.assertIn("--correspondence #1: not ingested", err)
        self.sends()
        cases = {"unknown": (synthetic("absent")[:12], "not ingested"),
                 "ambiguous": (stem, "ambiguous prefix (2 matches)"),
                 "11 chars": (a[:11], "give at least 12 lowercase hex")}
        for label, (bad, words) in cases.items():
            with self.subTest(label=label):
                self.refused(self.step("2026-03-20", "acknowledged", "--correspondence", a[:12],
                                       "--correspondence", bad), ["--correspondence #2: " + words])
                self.refused(self.sentcmd("--replace", "--correspondence", bad), ["--correspondence #1: " + words])
        self.assertEqual(self.stored()["steps"], [])

    def test_record_still_refused_on_other_kinds(self):
        a = self.hashes[0]
        self.sends()
        for kind in ("acknowledged", "determination", "closed"):
            with self.subTest(kind=kind):
                self.refused(self.step("2026-03-05", kind, "--record", a[:12]), ["--record is only for produced steps"])

    def test_malformed_stored_correspondence_unreadable(self):
        a = self.hashes[0]
        self.sends("--correspondence", a[:12])
        good = json.loads(self.sent.read_text())
        for label, bad in {"empty": [], "not hex": ["zz" * 32], "duplicate": [a, a], "not a list": a}.items():
            with self.subTest(label=label):
                document = json.loads(json.dumps(good))
                document["requests"][self.agency]["correspondence"] = bad
                self.sent.write_text(json.dumps(document))
                with self.assertRaisesRegex(ValueError, "kit/sent.json unreadable: malformed entry"):
                    kit_track.read_sent(self.root)
        document = json.loads(json.dumps(good))
        document["requests"][self.agency]["steps"] = [{"on": "2026-03-05", "kind": "acknowledged",
                                                       "correspondence": []}]
        self.sent.write_text(json.dumps(document))
        self.refused(self.step("2026-03-20", "produced"), ["kit/sent.json unreadable: malformed steps"])
        with self.assertRaisesRegex(ValueError, "malformed steps"):
            kit_track.status(self.root)

    def test_prefix_list_capped_with_more(self):
        many = sorted(synthetic(f"letter-{n}") for n in range(60))
        self.insert(*many)
        self.sends()
        for start in range(0, 60, 30):
            self.ok(f"2026-03-{20 + start // 30:02d}", "acknowledged" if start == 0 else "closed",
                    *[p for h in many[start:start + 30] for p in ("--correspondence", h[:16])])
        row = self.row()
        self.assertEqual((row["correspondence"], row["correspondence_prefixes"], row["correspondence_more"]),
                         (60, [h[:12] for h in many[:50]], 10))
        self.assertEqual((row["records"], row["record_prefixes"]), (0, []))


if __name__ == "__main__":
    unittest.main()
