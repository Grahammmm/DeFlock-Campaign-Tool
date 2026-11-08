"""ADM-SEED-1: tools/workspace_seed.py. Synthetic fixtures only (tests/fixtures/workspace_seed); no network.

Run from the repository root: python3 -m unittest tests.test_workspace_seed
"""
import contextlib
import hashlib
import importlib.util
import io
import shutil
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures" / "workspace_seed"
MIGRATIONS = sorted((ROOT / "workers" / "workspace" / "migrations").glob("000[1-5]_*.sql"))
_spec = importlib.util.spec_from_file_location("workspace_seed", ROOT / "tools" / "workspace_seed.py")
seed = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(seed)

CAMPAIGN = "synthetic-campaign"
EMPTY_TABLES = ("original", "receipt_occurrence", "extraction", "digest", "finding", "review_receipt", "publication", "meeting",
                "subscriber_event", "job", "external_action")


def run(out, agencies=None, events=None, tracker=True):
    argv = ["--agencies", str(agencies or FIX / "agencies.tsv"), "--events", *map(str, events or [FIX / "events"]),
            "--campaign-id", CAMPAIGN, "--snapshot-date", "2026-11-08", "--out-dir", str(out)]
    if tracker:
        argv += ["--tracker", str(FIX / "tracker.csv")]
    err = io.StringIO()
    with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
        code = seed.main(argv)
    return code, err.getvalue()


def database(out):
    db = sqlite3.connect(":memory:")
    db.execute("PRAGMA foreign_keys = ON")
    for path in MIGRATIONS:
        db.executescript(path.read_text())
    db.executescript((Path(out) / "seed.sql").read_text())
    return db


class SeedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.out = self.tmp / "out"

    def mutated(self, agencies=None, alpha_row=None):
        """Copy the fixtures and append one row (or replace agencies.tsv) to test a refusal."""
        copy = Path(tempfile.mkdtemp(dir=self.tmp)) / "fx"
        shutil.copytree(FIX, copy)
        if agencies is not None:
            (copy / "agencies.tsv").write_text(agencies)
        if alpha_row is not None:
            with open(copy / "events" / "alpha" / "events.tsv", "a") as handle:
                handle.write(alpha_row)
        return copy

    def test_migrations_apply_and_seed_loads(self):
        self.assertEqual(len(MIGRATIONS), 5)
        self.assertEqual(run(self.out)[0], 0)
        db = database(self.out)
        count = lambda sql: db.execute(sql).fetchone()[0]  # noqa: E731
        self.assertEqual(count("SELECT COUNT(*) FROM agency"), 4)  # duplicate agency row merged
        self.assertEqual(count("SELECT COUNT(*) FROM request"), 2)
        self.assertEqual(count("SELECT COUNT(*) FROM correspondence"), 6)
        self.assertEqual(db.execute("SELECT external_sends FROM campaign").fetchone()[0], "disabled")
        self.assertIn("2026-11-08", db.execute("SELECT value_json FROM setting WHERE key='seed_snapshot'").fetchone()[0])
        for table in EMPTY_TABLES:
            self.assertEqual(count("SELECT COUNT(*) FROM %s" % table), 0, table)

    def test_no_source_text_reaches_any_output(self):
        self.assertEqual(run(self.out)[0], 0)
        for name in ("seed.sql", "seed-summary.txt", "seed-review.txt"):
            text = (self.out / name).read_text()
            for canary in ("CANARY", "Zed Quill", "canary-role", "canary-contact", "@", "example.invalid"):
                self.assertNotIn(canary, text, (name, canary))

    def test_email_and_header_columns_are_null(self):
        run(self.out)
        db = database(self.out)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM agency WHERE records_email IS NOT NULL").fetchone()[0], 0)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM correspondence WHERE subject IS NOT NULL OR from_addr IS NOT NULL "
                                    "OR to_addr IS NOT NULL OR provider_message_id IS NOT NULL OR raw_sha256 IS NOT NULL").fetchone()[0], 0)

    def test_templates_only_and_third_party_dropped(self):
        run(self.out)
        db = database(self.out)
        summaries = {v[1][0] for v in seed.EVENTS.items()}
        actions = {v[4] for v in seed.EVENTS.values()}
        for (summary,) in db.execute("SELECT summary FROM correspondence"):
            self.assertIn(summary, summaries)
        for (action,) in db.execute("SELECT next_action FROM request"):
            self.assertIn(action, actions)
        refs = [r[0] for r in db.execute("SELECT request_id FROM request")]
        self.assertEqual(refs, ["req-alpha-pd-26-001", "req-bravo-county-sheriff-pl-pra"])
        self.assertFalse([r for r in db.execute("SELECT 1 FROM correspondence WHERE correspondence_id LIKE '%al-005' "
                                                "OR correspondence_id LIKE '%al-006'")])
        row = db.execute("SELECT subject, body_md, state, sent_at, determination_due, last_activity_at FROM request "
                         "WHERE request_id='req-alpha-pd-26-001'").fetchone()
        self.assertEqual(row, ("Alpha PD records request 26-001", seed.BODY, "partial", "2026-07-30", "2026-08-10", "2026-08-12"))
        denied = db.execute("SELECT state, sent_at FROM request WHERE request_id LIKE 'req-bravo%'").fetchone()
        self.assertEqual(denied, ("denied", "2026-08-30"))  # sent_at from the tracker's submitted date only

    def test_review_file_starts_with_hash_and_lists_every_text_value(self):
        run(self.out)
        sql = (self.out / "seed.sql").read_bytes()
        review = (self.out / "seed-review.txt").read_text().splitlines()
        self.assertEqual(review[0], "seed.sql sha256: " + hashlib.sha256(sql).hexdigest())
        body = "\n".join(review)
        for text in ("Agency produced records.", "Alpha PD records request 26-001", seed.BODY, "Agency denied the request."):
            self.assertIn(text, body)
        self.assertIn("agency.name [alpha-pd] = Alpha PD", body)

    def test_output_files_are_mode_0600(self):
        run(self.out)
        for name in ("seed.sql", "seed-summary.txt", "seed-review.txt"):
            self.assertEqual(stat.S_IMODE((self.out / name).stat().st_mode), 0o600, name)

    def test_unknown_event_type_refuses_and_writes_nothing(self):
        copy = self.mutated(alpha_row="AL-099\t2026-08-20\t\tAlpha PD\t26-001\tsurprise_type\tx\tx\tx\tDATED\t\n")
        code, err = run(self.out, agencies=copy / "agencies.tsv", events=[copy / "events"])
        self.assertEqual(code, 2)
        self.assertIn("unknown event type", err)
        self.assertFalse(self.out.exists())

    def test_email_or_phone_in_name_or_ref_refuses_without_echo(self):
        cases = (
            ("agency\nAlpha PD\nsecret.person@example.invalid\n", None, "agencies row 3 column agency"),
            ("agency\nAlpha PD\nCall (805) 555-0142 PD\n", None, "agencies row 3 column agency"),
            (None, "AL-050\t2026-08-20\t\tAlpha PD\t26 (805) 555-0142\tclosed\tx\tx\tx\tDATED\t\n", "request_ref"),
            (None, "AL-051\t2026-08-20\t\tAlpha PD\ta@b.invalid\tclosed\tx\tx\tx\tDATED\t\n", "request_ref"),
        )
        for agencies, row, locator in cases:
            with self.subTest(locator=locator, row=row):
                base = self.mutated(agencies, row)
                code, err = run(self.out, agencies=base / "agencies.tsv", events=[base / "events"])
                self.assertEqual(code, 2)
                self.assertIn(locator, err)
                for leaked in ("secret.person", "555-0142", "example.invalid", "805"):
                    self.assertNotIn(leaked, err)
                self.assertFalse(self.out.exists())

    def test_backstop_refuses_a_pattern_hit_by_row_and_column(self):
        rows = [("agency", "x", {"name": "reach me at jdoe [at] slo [dot] gov"})]
        with self.assertRaises(seed.Refused) as caught:
            seed.backstop(rows)
        message = str(caught.exception)
        self.assertIn("agency row x column name", message)
        self.assertNotIn("jdoe", message)

    def test_backstop_does_not_echo_a_phone_shaped_row_key(self):
        rows = [("request", "AL 805 555 0142", {"request_id": "AL 805 555 0142"})]
        with self.assertRaises(seed.Refused) as caught:
            seed.backstop(rows)
        message = str(caught.exception)
        self.assertIn("request row <redacted> column request_id", message)
        for leaked in ("555", "0142", "805"):
            self.assertNotIn(leaked, message)

    def test_third_party_ref_is_dropped_before_the_ref_shape_check(self):
        copy = self.mutated(alpha_row="AL-060\t2026-08-20\t\tAlpha PD\t26-83 (third party)\tproduction\tx\tx\tx\tDATED\t\n")
        self.assertEqual(run(self.out, agencies=copy / "agencies.tsv", events=[copy / "events"])[0], 0)

    def test_unknown_agency_in_events_refuses(self):
        copy = self.mutated(alpha_row="AL-070\t2026-08-20\t\tNobody PD\t26-001\tclosed\tx\tx\tx\tDATED\t\n")
        code, err = run(self.out, agencies=copy / "agencies.tsv", events=[copy / "events"])
        self.assertEqual(code, 2)
        self.assertIn("column agency", err)


if __name__ == "__main__":
    unittest.main()
