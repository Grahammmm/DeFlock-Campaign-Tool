"""flock_requests.py due PATH --agency SLUG: reminders and totals scoped to the chosen agencies (RF0620-10)."""
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from tests.flock.test_requests_as_of import AS_OF, PKG, SHA, known, law, requests, run, unknown

MORRO, PISMO, GROVER = "Morro Bay PD", "Pismo Beach Police", "Grover City Records"


def at(row, agency):
    return [row[0], agency] + row[2:]


def closed(rid, agency):
    return at(known(rid, -3)[:7] + ["closed", SHA], agency)


ROWS = [at(known("M-PLUS3", 3), MORRO), at(known("M-MINUS2", -2), MORRO), at(unknown("M-UNK", AS_OF), MORRO),
        closed("M-SHUT", MORRO), at(known("P-PLUS1", 1), PISMO), at(known("P-PLUS20", 20), PISMO),
        at(known("G-MINUS5", -5), GROVER), closed("C-ONLY", "Cayucos Records")]
TAIL = " | " + law.HOLIDAY_NOTE


class DueAgencyTests(unittest.TestCase):
    def write(self, rows, header=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "REQUESTS.csv"
        lines = [",".join(header or requests.HEADER)] + [",".join(r) for r in rows]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return str(path)

    def ok(self, *args):
        done = run("due", *args)
        self.assertEqual((done.returncode, done.stderr), (0, ""), done.stderr)
        return done.stdout.splitlines()

    def usage(self, *args):
        done = run("due", *args)
        self.assertEqual((done.returncode, done.stdout, done.stderr), (2, "", requests.USAGE + "\n"), args)

    def test_one_agency_lines_and_totals(self):
        path = self.write(ROWS)
        lines = self.ok(path, "--agency", "morro-bay-pd")
        self.assertEqual([x.split(" | ")[1] for x in lines[:-1]], ["M-MINUS2", "M-PLUS3", "M-UNK"])
        self.assertTrue(all(f" | {MORRO} | " in x for x in lines[:-1]))
        self.assertEqual(lines[-1], "provisional reminder | open=3 receipt_unknown=1 agencies=morro-bay-pd" + TAIL)
        dated = self.ok(path, "--agency", "morro-bay-pd", "--as-of", AS_OF.isoformat())
        self.assertEqual(dated[-1], "provisional reminder | open=3 receipt_unknown=1 upcoming_7d=1 passed=1 "
                                    "as_of=2026-10-31 agencies=morro-bay-pd" + TAIL)

    def test_two_agencies_any_order_and_as_of_position(self):
        path, d = self.write(ROWS), AS_OF.isoformat()
        m, p = ("--agency", "morro-bay-pd"), ("--agency", "pismo-beach-police")
        orders = [(*m, *p, "--as-of", d), (*p, *m, "--as-of", d), ("--as-of", d, *p, *m), (*m, "--as-of", d, *p),
                  (*p, "--as-of", d, *m), ("--as-of", d, *m, *p)]
        outs = {run("due", path, *args).stdout for args in orders}
        self.assertEqual(len(outs), 1)
        lines = outs.pop().splitlines()
        self.assertEqual([x.split(" | ")[1] for x in lines[:-1]], ["M-MINUS2", "P-PLUS1", "M-PLUS3", "P-PLUS20", "M-UNK"])
        self.assertEqual(lines[-1], "provisional reminder | open=5 receipt_unknown=1 upcoming_7d=2 passed=1 as_of=2026-10-31"
                                    " agencies=morro-bay-pd,pismo-beach-police" + TAIL)
        everyone = self.ok(path, "--as-of", d)[-1]  # unscoped: G-MINUS5 also passed
        self.assertIn("open=6 receipt_unknown=1 upcoming_7d=2 passed=2 as_of=2026-10-31 | ", everyone)

    def test_agency_with_only_closed_requests_exists(self):
        lines = self.ok(self.write(ROWS), "--agency", "cayucos-records")
        self.assertEqual(lines, ["provisional reminder | open=0 receipt_unknown=0 agencies=cayucos-records" + TAIL])

    def test_unknown_agency_refused_before_output(self):
        path = self.write(ROWS)
        for args, slug in (([("--agency", "nope")], "nope"), ([("--agency", "morro-bay-pd"), ("--agency", "zzz")], "zzz"),
                           ([("--agency", "zeta"), ("--agency", "alpha"), ("--as-of", "2026-10-31")], "alpha"),
                           ([("--agency", "morro")], "morro")):  # a prefix of a real slug is not that agency
            with self.subTest(args=args):
                done = run("due", path, *[x for pair in args for x in pair])
                self.assertEqual((done.returncode, done.stdout, done.stderr), (1, "", f"due: no request for agency {slug}\n"))

    def test_malformed_agency_flags_are_usage(self):
        path = self.write(ROWS)
        many = [x for n in range(21) for x in ("--agency", f"a{n}")]
        for args in (("--agency", "Morro_Bay"), ("--agency", "Morro-Bay-PD"), ("--agency", "morro-"), ("--agency", ""),
                     ("--agency", "morro--bay"), ("--agency", "morro-bay-pd", "--agency", "morro-bay-pd"), tuple(many),
                     ("--agency",), ("--agency", "morro-bay-pd", "--agency"), ("--agency", "--as-of", "2026-10-31"),
                     ("--as-of", "2026-10-31", "--agency"), ("--agency", "morro-bay-pd", "--as-of", "2026-10-31",
                                                             "--as-of", "2026-10-30"),
                     ("--agency", "morro-bay-pd", "--today", "2026-10-31")):
            with self.subTest(args=args):
                self.usage(path, *args)
        done = run("due", path, "--agency", "morro-bay-pd", "--as-of", "2026-13-01")
        self.assertEqual((done.returncode, done.stdout, done.stderr), (2, "", "due: --as-of must be YYYY-MM-DD\n"))
        self.assertIn("due PATH [--as-of YYYY-MM-DD] [--agency SLUG ...]", run("--help").stdout)

    def test_twenty_agencies_is_the_limit(self):
        rows = [at(known(f"R{n}", n), f"Agency {n}") for n in range(20)]
        lines = self.ok(self.write(rows), *[x for n in range(20) for x in ("--agency", f"agency-{n}")])
        self.assertEqual(len(lines), 21)
        self.assertIn("open=20 receipt_unknown=0 agencies=" + ",".join(sorted(f"agency-{n}" for n in range(20))), lines[-1])

    def test_agency_with_ics_is_usage_and_writes_nothing(self):
        path = self.write(ROWS)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        out = Path(tmp.name) / "due.ics"
        for args in (("--agency", "morro-bay-pd", "--ics", str(out)), ("--ics", str(out), "--agency", "morro-bay-pd"),
                     ("--as-of", "2026-10-31", "--ics", str(out), "--agency", "nope")):
            with self.subTest(args=args):
                self.usage(path, *args)
                self.assertFalse(out.exists())

    def test_without_agency_output_unchanged(self):
        rows = [at(known("M-PLUS3", 3), MORRO), at(unknown("M-UNK", AS_OF - timedelta(days=4)), MORRO),
                closed("C-ONLY", PISMO)]
        lw, receipt = PKG["records_law"], rows[0][4]
        head = "provisional reminder | M-PLUS3 | Morro Bay PD | determination 2026-11-03 | "
        known_line = (head + f"receipt {receipt} + {lw['determination_days']} {lw['day_type']} days | Gov. Code 7922.535"
                      " | not a legal due date")
        unk_line = "provisional reminder | M-UNK | Morro Bay PD | receipt unknown | no date computed"
        path = self.write(rows)
        plain = run("due", path)
        self.assertEqual((plain.returncode, plain.stderr), (0, ""))
        self.assertEqual(plain.stdout, "\n".join([known_line, unk_line, "provisional reminder | open=2 receipt_unknown=1"
                                                  + TAIL]) + "\n")
        dated = run("due", path, "--as-of", "2026-10-31")
        self.assertEqual(dated.stdout, "\n".join([
            known_line + " | as of 2026-10-31: in 3 days", unk_line + " | as of 2026-10-31: sent 4 days ago",
            "provisional reminder | open=2 receipt_unknown=1 upcoming_7d=1 passed=0 as_of=2026-10-31" + TAIL]) + "\n")
        self.assertNotIn("agencies=", plain.stdout + dated.stdout)

    def test_load_error_wins_over_agency_check(self):
        bad = self.write(ROWS, header=requests.HEADER[:-1] + ["sha"])
        done = run("due", bad, "--agency", "nope")
        self.assertEqual((done.returncode, done.stdout), (1, ""))
        self.assertIn("row 1: header does not match REQUESTS.csv v1", done.stderr)
        self.assertNotIn("no request for agency", done.stderr)


if __name__ == "__main__":
    unittest.main()
