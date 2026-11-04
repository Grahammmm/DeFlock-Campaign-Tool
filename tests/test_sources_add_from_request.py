"""`sources add-batch --from-request`: a request's production records into the library (synthetic campaign only)."""
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool import sources_cmd
from campaign_tool.cli import database
from campaign_tool.content import ContentError, load_content
from campaign_tool.sources_cmd import add_from_request

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
FULL_HASH = re.compile(r"[0-9a-f]{64}")
AGENCY = "ca-cedar-police"
EARLY, LATE = "2026-10-28", "2026-11-02"  # US daylight time ended on 2026-11-01
STAMP = "2026-10-20T00:00:00+00:00"


def digest(label):
    return hashlib.sha256(f"synthetic DF1422-06 record {label}; not a real document\n".encode()).hexdigest()


def cli(*args):
    return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", *args], cwd=REPO_ROOT,
                          capture_output=True, text=True, timeout=120)


def step(on, kind="produced", records=(), correspondence=()):
    return {"on": on, "kind": kind, **({"records": [digest(r) for r in records]} if records else {}),
            **({"correspondence": [digest(c) for c in correspondence]} if correspondence else {}),
            "recorded_at": STAMP}


class SourcesAddFromRequestTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-sources-request-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.sources = self.root / "content" / "sources.json"
        self.sent = self.root / "kit" / "sent.json"

    def request(self, steps, agency=AGENCY, request="main", correspondence=()):
        """Store one hand-sent request with the given steps in kit/sent.json (synthetic, written directly)."""
        key = agency if request == "main" else f"{agency}#{request}"
        stem = agency if request == "main" else f"{agency}--{request}"
        entry = {"sent_on": "2026-10-20", "channel": "email", "draft": f"requests/{stem}.md", "draft_sha256": "a" * 64,
                 "recorded_at": STAMP, "steps": steps}
        if correspondence:
            entry["correspondence"] = [digest(c) for c in correspondence]
        self.sent.parent.mkdir(exist_ok=True)
        try:
            requests = json.loads(self.sent.read_text(encoding="utf-8"))["requests"]
        except FileNotFoundError:
            requests = {}
        requests[key] = entry
        self.sent.write_text(json.dumps({"schema_version": 1, "requests": requests}, indent=2) + "\n", encoding="utf-8")

    def receive(self, rows):
        """rows: (label, original_name); synthetic ledger rows, no record bytes stored."""
        db = database(self.root)
        with db:
            for number, (label, name) in enumerate(rows):
                db.execute("INSERT OR IGNORE INTO objects VALUES (?, 1, ?)", (digest(label), STAMP))
                db.execute("INSERT INTO receipts VALUES (?, ?, ?, ?, ?)",
                           (f"r{number}", digest(label), "synthetic-production", f"2026-10-30T16:{number % 60:02d}:00+00:00",
                            name))
        db.close()

    def list_existing(self, *labels):
        document = json.loads(self.sources.read_text(encoding="utf-8"))
        document += [{**document[0], "sha256": digest(label)} for label in labels]
        self.sources.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")

    def entries(self):
        return json.loads(self.sources.read_text(encoding="utf-8"))

    def two_steps(self):
        self.request([step(EARLY, records=("a", "b")), step(LATE, records=("c",))])

    def assertRefused(self, message, agency=AGENCY, **arguments):
        before = self.sources.read_bytes()
        with self.assertRaises(ValueError) as caught:
            add_from_request(self.root, agency, **arguments)
        self.assertIn(message, str(caught.exception))
        self.assertEqual(self.sources.read_bytes(), before)
        self.assertEqual(list(self.sources.parent.glob(".sources-*")), [])

    def test_two_produced_steps_add_three_entries_with_each_step_date_in_one_write(self):
        self.two_steps()
        self.receive([("a", "Policy A.pdf"), ("b", "Contract B.pdf"), ("c", "Audit C.pdf")])
        with mock.patch.object(sources_cmd, "_write_atomic", wraps=sources_cmd._write_atomic) as write:
            result = add_from_request(self.root, AGENCY)
        self.assertEqual(write.call_count, 1)
        self.assertEqual(result, {"added": [digest(x)[:12] for x in "abc"], "already_listed": 0, "dry_run": False})
        new = self.entries()[2:]
        self.assertEqual([e["sha256"] for e in new], [digest(x) for x in "abc"])
        self.assertEqual([e["received_at"] for e in new],
                         ["2026-10-28T00:00:00-07:00", "2026-10-28T00:00:00-07:00", "2026-11-02T00:00:00-08:00"])
        self.assertEqual([e["title"] for e in new], ["Policy A [EDIT ME: title]", "Contract B [EDIT ME: title]",
                                                     "Audit C [EDIT ME: title]"])
        for entry in new:
            self.assertEqual((entry["agency_id"], entry["fragment"], entry["public_url"], entry["pages"], entry["note"]),
                             (AGENCY, "cedar-police-records", None, None,
                              f"added in batch from request {AGENCY}/main"))
        load_content(self.root / "content")  # the whole library still loads

    def test_record_without_a_ledger_name_gets_the_hash_title(self):
        self.two_steps()
        self.receive([("a", "Policy A.pdf"), ("b", "")])  # b has an empty name, c has no receipt at all
        add_from_request(self.root, AGENCY)
        self.assertEqual([e["title"] for e in self.entries()[2:]],
                         ["Policy A [EDIT ME: title]", f"Record {digest('b')[:12]} [EDIT ME: title]",
                          f"Record {digest('c')[:12]} [EDIT ME: title]"])

    def test_record_on_two_steps_is_one_entry_with_the_earlier_date(self):
        self.request([step(EARLY, records=("a",)), step(LATE, records=("a", "b"))])
        result = add_from_request(self.root, AGENCY)
        self.assertEqual(result["added"], [digest("a")[:12], digest("b")[:12]])
        self.assertEqual([(e["sha256"], e["received_at"]) for e in self.entries()[2:]],
                         [(digest("a"), "2026-10-28T00:00:00-07:00"), (digest("b"), "2026-11-02T00:00:00-08:00")])

    def test_correspondence_records_are_never_added(self):
        self.request([step(EARLY, records=("a",), correspondence=("letter-step",)),
                      step(LATE, "acknowledged", correspondence=("letter-ack",))], correspondence=("cover-letter",))
        result = add_from_request(self.root, AGENCY)
        self.assertEqual(result["added"], [digest("a")[:12]])
        self.assertEqual({e["sha256"] for e in self.entries()[2:]}, {digest("a")})
        self.request([step(EARLY, "acknowledged", correspondence=("letter-ack",))], correspondence=("cover-letter",))
        self.assertRefused("has no linked production records")

    def test_only_produced_steps_count(self):
        step_with_records = step(EARLY, "determination")
        step_with_records["records"] = [digest("a")]  # a hand-edited file: records on a step that is not produced
        self.request([step_with_records, step(LATE, records=("b",))])
        self.assertEqual(add_from_request(self.root, AGENCY)["added"], [digest("b")[:12]])

    def test_already_listed_are_counted_and_all_listed_means_nothing_to_add(self):
        self.two_steps()
        self.list_existing("b")
        result = add_from_request(self.root, AGENCY)
        self.assertEqual(result, {"added": [digest("a")[:12], digest("c")[:12]], "already_listed": 1, "dry_run": False})
        self.assertEqual([e["sha256"] for e in self.entries()].count(digest("b")), 1)
        before, mtime = self.sources.read_bytes(), self.sources.stat().st_mtime_ns
        done = cli("sources", "add-batch", "--directory", str(self.root), "--from-request", AGENCY)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("nothing to add", done.stdout)
        self.assertEqual(json.loads(done.stdout.splitlines()[0]),
                         {"added": [], "already_listed": 3, "dry_run": False})
        self.assertEqual((self.sources.read_bytes(), self.sources.stat().st_mtime_ns), (before, mtime))

    def test_dry_run_lists_prefixes_and_changes_nothing(self):
        self.two_steps()
        before, mtime = self.sources.read_bytes(), self.sources.stat().st_mtime_ns
        done = cli("sources", "add-batch", "--directory", str(self.root), "--from-request", AGENCY, "--dry-run")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout), {"added": [digest(x)[:12] for x in "abc"], "already_listed": 0,
                                                   "dry_run": True})
        self.assertIsNone(FULL_HASH.search(done.stdout), "a full 64-hex hash was printed")
        self.assertEqual((self.sources.read_bytes(), self.sources.stat().st_mtime_ns), (before, mtime))

    def test_cli_writes_and_prints_prefixes_only(self):
        self.two_steps()
        done = cli("sources", "add-batch", "--directory", str(self.root), "--from-request", AGENCY)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout)["added"], [digest(x)[:12] for x in "abc"])
        self.assertIsNone(FULL_HASH.search(done.stdout), "a full 64-hex hash was printed")
        self.assertEqual(len(self.entries()), 5)

    def test_named_request_reads_only_that_request(self):
        self.request([step(EARLY, records=("a",))])
        self.request([step(LATE, records=("b",))], request="audit-2")
        self.assertEqual(add_from_request(self.root, AGENCY, request="audit-2")["added"], [digest("b")[:12]])
        self.assertEqual(self.entries()[-1]["note"], f"added in batch from request {AGENCY}/audit-2")
        self.assertEqual(self.entries()[-1]["received_at"], "2026-11-02T00:00:00-08:00")

    def test_refusals_write_nothing(self):
        self.request([step(EARLY, "acknowledged"), step(LATE, "produced")])  # produced, but no linked records
        self.assertRefused(f"request {AGENCY} has no linked production records")
        self.assertRefused("no hand-sent request recorded for ca-cedar-city-council", "ca-cedar-city-council")
        self.assertRefused("no hand-sent request recorded for ca-unknown", "ca-unknown")
        self.assertRefused(f"no sent request {AGENCY}/audit; run kit-sent --request audit first", request="audit")
        self.assertRefused("--request must be", request="Bad Name")
        self.request([step(EARLY, records=("a",))], agency="ca-not-in-campaign")
        self.assertRefused("is not one of the campaign's agencies", "ca-not-in-campaign")
        no_request_file = cli("sources", "add-batch", "--directory", str(self.root), "--from-request", "ca-unknown")
        self.assertEqual(no_request_file.returncode, 1)
        self.assertIn("no hand-sent request recorded for ca-unknown", no_request_file.stderr)

    def test_no_sent_file_is_refused(self):
        self.assertRefused("no hand-sent request recorded for " + AGENCY)

    def test_tz_changes_the_offset_and_an_unknown_zone_is_refused(self):
        self.two_steps()
        add_from_request(self.root, AGENCY, tz="Pacific/Honolulu", dry_run=False)
        self.assertEqual({e["received_at"] for e in self.entries()[2:]},
                         {"2026-10-28T00:00:00-10:00", "2026-11-02T00:00:00-10:00"})
        self.assertRefused("unknown time zone Mars/Base", tz="Mars/Base")
        refused = cli("sources", "add-batch", "--directory", str(self.root), "--from-request", AGENCY, "--tz", "Mars/Base")
        self.assertEqual(refused.returncode, 1)
        self.assertIn("unknown time zone Mars/Base", refused.stderr)

    def test_unknown_zone_is_refused_before_anything_is_added(self):
        self.two_steps()
        self.assertRefused("unknown time zone", tz="")

    def test_argument_combinations_are_argparse_errors(self):
        self.two_steps()
        before = self.sources.read_bytes()
        base = ("sources", "add-batch", "--directory", str(self.root))
        for extra in (("--from-request", AGENCY, "--source-id", "prod-1"),
                      ("--from-request", AGENCY, "--received-at", "2026-10-20T09:00:00-07:00"),
                      ("--from-request", AGENCY, "--agency-id", AGENCY), (), ("--source-id", "prod-1"),
                      ("--source-id", "prod-1", "--agency-id", AGENCY, "--received-at", "2026-10-20T09:00:00-07:00",
                       "--request", "audit")):
            refused = cli(*base, *extra)
            self.assertEqual(refused.returncode, 2, (extra, refused.stderr))
            self.assertIn("error:", refused.stderr)
        self.assertEqual(self.sources.read_bytes(), before)

    def test_source_id_mode_rejects_explicit_request_options_even_at_defaults(self):
        self.receive([("a", "Policy A.pdf")])
        before = self.sources.read_bytes()
        base = ("sources", "add-batch", "--directory", str(self.root),
                "--source-id", "synthetic-production", "--agency-id", AGENCY,
                "--received-at", "2026-10-20T09:00:00-07:00")
        for flag, value in (("--request", "main"), ("--request", "audit"),
                            ("--tz", "America/Los_Angeles"), ("--tz", "Pacific/Honolulu")):
            for dry_run in ((), ("--dry-run",)):
                with self.subTest(flag=flag, value=value, dry_run=dry_run):
                    refused = cli(*base, flag, value, *dry_run)
                    self.assertEqual(refused.returncode, 2, refused.stderr)
                    self.assertIn("--request and --tz are only for --from-request", refused.stderr)
                    self.assertEqual(refused.stdout, "")
                    self.assertEqual(self.sources.read_bytes(), before)

    def test_more_than_the_batch_cap_is_refused(self):
        steps = [step(EARLY, records=[f"cap-{n}" for n in range(0, 200)]),
                 step(EARLY, records=[f"cap-{n}" for n in range(200, 400)]),
                 step(LATE, records=[f"cap-{n}" for n in range(400, 501)])]
        self.request(steps)
        self.assertRefused("501 new records; at most 500 per run, nothing written")
        steps[2]["records"].pop()
        self.request(steps)
        self.assertEqual(len(add_from_request(self.root, AGENCY)["added"]), 500)


class ExistingAddBatchUnchangedTests(unittest.TestCase):
    def test_source_id_form_still_needs_its_three_options(self):
        refused = cli("sources", "add-batch", "--directory", str(EXAMPLE), "--source-id", "x")
        self.assertEqual(refused.returncode, 2)
        self.assertIn("--agency-id", refused.stderr)


if __name__ == "__main__":
    unittest.main()
