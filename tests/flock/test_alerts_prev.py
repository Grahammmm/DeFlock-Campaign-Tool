"""flock_alerts reminders --prev OLD.json: alerts marked new/still, cleared alerts listed, counts in summary and JSON (RF2330-6)."""
import contextlib
import io
import json
import os
from pathlib import Path
import unittest

from tests.flock import test_alerts as base
from tests.flock.test_alerts import BANNED, DATE, known, unknown

alerts = base.alerts
DAY_A = "2026-10-30"  # the previous run: one day before DATE
TAIL = " | law=draft | provisional; not a legal due date | law package is draft: dates unconfirmed"
SUMMARY_TAIL = " | recorded fields only; not a legal status; the canonical ledger wins"
MIB2 = 2 * 1024 * 1024


class PrevTest(unittest.TestCase):
    setUp = base.AlertsTest.setUp  # synthetic stores built with the test_alerts.py helpers
    write = base.AlertsTest.write

    def run_on(self, date, *extra):
        argv = ["reminders", "--store", str(self.store), "--date", date, "--receipt-days", "10"] + list(extra)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            try:
                code = alerts.main(argv)
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue().splitlines()

    def prev_from(self, rows, name="old.json"):  # a real run A on DAY_A writes the previous file
        self.write(rows)
        path = self.tmp / name
        self.assertIn(self.run_on(DAY_A, "--json-out", str(path))[0], (0, 3))
        return path

    def dump(self, doc, name):
        path = self.tmp / name
        path.write_text(json.dumps(doc), encoding="utf-8")
        return path

    def test_same_alerts_all_still_and_exit_unchanged(self):
        rows = [known("R-P", -5), known("R-5", 5), unknown("U-1", 30)]
        old = self.prev_from(rows)
        plain = self.run_on(DATE)
        code, lines = self.run_on(DATE, "--prev", str(old))
        self.assertEqual(code, plain[0])
        self.assertEqual(code, 3)
        self.assertEqual(lines[:-1], [ln + " | still" for ln in plain[1][:-1]])
        self.assertEqual(lines[-1], plain[1][-1].replace(SUMMARY_TAIL, " new=0 cleared=0 since=%s%s" % (DAY_A, SUMMARY_TAIL)))

    def test_closed_request_is_cleared_and_exit_zero(self):
        old = self.prev_from([known("R-P", -5), known("R-5", 5)])
        self.write([known("R-P", -5, status="determined"), known("R-5", 5)])
        code, lines = self.run_on(DATE, "--prev", str(old))
        self.assertEqual(code, 0)  # a cleared alert never makes the exit 3
        self.assertEqual(lines[1], "alert | cleared | remind:sample-alpha-pd:R-P:reminder-passed | reminder-passed")
        self.assertTrue(lines[0].endswith(TAIL + " | still"), lines[0])
        self.assertIn(" new=0 cleared=1 since=%s | " % DAY_A, lines[-1])
        self.assertEqual(len(lines), 3)

    def test_week_to_today_is_new_plus_cleared_and_json(self):
        old = self.prev_from([known("R-0", 0), known("Z-1", -1, status="determined")])
        before = json.loads(old.read_text(encoding="utf-8"))
        self.assertEqual([a["kind"] for a in before["alerts"]], ["reminder-week"])
        self.assertNotIn("since", before)
        self.assertNotIn("new", before["alerts"][0])
        out = self.tmp / "b.json"
        code, lines = self.run_on(DATE, "--prev", str(old), "--json-out", str(out))
        self.assertEqual(code, 3)
        self.assertTrue(lines[0].startswith("alert | remind:sample-alpha-pd:R-0:reminder-today | "), lines[0])
        self.assertTrue(lines[0].endswith(TAIL + " | new"))
        self.assertEqual(lines[1], "alert | cleared | remind:sample-alpha-pd:R-0:reminder-week | reminder-week")
        self.assertIn(" new=1 cleared=1 since=%s | " % DAY_A, lines[2])
        doc = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(doc["since"], DAY_A)
        self.assertEqual([(a["id"], a["new"]) for a in doc["alerts"]], [("remind:sample-alpha-pd:R-0:reminder-today", True)])
        self.assertEqual(doc["cleared"], [{"id": "remind:sample-alpha-pd:R-0:reminder-week", "kind": "reminder-week"}])
        self.assertEqual(out.stat().st_mode & 0o777, 0o600)

    def test_cleared_sorted_and_unsafe_labels_never_echoed(self):
        old = self.prev_from([known("R-3", 3)])
        doc = json.loads(old.read_text(encoding="utf-8"))
        doc["alerts"][0]["phrase"] = "write to clerk@example.org, overdue and unlawful"
        doc["alerts"] += [{"id": "remind:sample-alpha-pd:x@example.org:reminder-week", "kind": "reminder-week", "phrase": "y"},
                          {"id": "remind:aaa:R-9:receipt-unknown", "kind": "receipt-unknown"}]
        prev = self.dump(doc, "edited.json")
        self.write([known("R-3", 3)])
        out = self.tmp / "b.json"
        code, lines = self.run_on(DATE, "--prev", str(prev), "--json-out", str(out))
        self.assertEqual(code, 0)
        self.assertEqual(lines[1:3], ["alert | cleared | remind:aaa:R-9:receipt-unknown | receipt-unknown",
                                      "alert | cleared | (unsafe-label) | reminder-week"])
        self.assertIn(" new=0 cleared=2 since=%s | " % DAY_A, lines[-1])
        text = "\n".join(lines) + out.read_text(encoding="utf-8")
        for value in ("@", "example.org", "clerk", "write to"):
            self.assertNotIn(value, text)
        self.assertIsNone(BANNED.search(text))
        self.assertEqual(json.loads(out.read_text(encoding="utf-8"))["cleared"],
                         [{"id": "remind:aaa:R-9:receipt-unknown", "kind": "receipt-unknown"},
                          {"id": "(unsafe-label)", "kind": "reminder-week"}])

    def test_prev_refusals_exit_1_no_alert_lines_nothing_written(self):
        rows = [known("R-P", -5), unknown("U-1", 30)]
        good = self.prev_from(rows)
        raw = good.read_bytes()
        doc = json.loads(raw)
        (self.tmp / "real").mkdir()
        (self.tmp / "real" / "old.json").write_bytes(raw)
        (self.tmp / "dirlink").symlink_to(self.tmp / "real")
        (self.tmp / "filelink.json").symlink_to(good)
        (self.tmp / "big.json").write_bytes(raw + b" " * (MIB2 + 1 - len(raw)))
        (self.tmp / "cut.json").write_bytes(raw[:-20])

        def edited(name, **change):
            return str(self.dump(dict(doc, **change), name))

        def alert(i, **change):
            items = [dict(a) for a in doc["alerts"]]
            items[i].update(change)
            return items

        first = doc["alerts"][0]["id"]
        cases = {
            "relative": ("old.json", "prev must be an absolute path"),
            "missing": (str(self.tmp / "none.json"), "prev is not a regular file"),
            "folder": (str(self.tmp / "real"), "prev is not a regular file"),
            "symlink": (str(self.tmp / "filelink.json"), "prev is a symlink"),
            "parent link": (str(self.tmp / "dirlink" / "old.json"), "prev is a symlink"),
            "2 MiB + 1": (str(self.tmp / "big.json"), "prev larger than 2 MiB"),
            "truncated": (str(self.tmp / "cut.json"), "prev not UTF-8 JSON"),
            "v2": (edited("v2.json", schema="flock-alerts-v2"), "prev schema"),
            "review false": (edited("rr.json", review_required=False), "prev review_required"),
            "as_of equal": (edited("eq.json", as_of=DATE), "prev as_of"),
            "as_of after": (edited("after.json", as_of="2026-11-01"), "prev as_of"),
            "as_of not a date": (edited("bad.json", as_of="2026-02-30"), "prev as_of"),
            "alerts not a list": (edited("al.json", alerts={}), "prev alerts"),
            "pipe": (edited("pipe.json", alerts=alert(1, id="remind:a|b:U-1:receipt-unknown")), "prev alerts[1].id"),
            "newline": (edited("nl.json", alerts=alert(1, id="remind:a\nb:U-1:receipt-unknown")), "prev alerts[1].id"),
            "201 chars": (edited("long.json", alerts=alert(0, id="remind:" + "a" * 178 + ":reminder-passed")),
                          "prev alerts[0].id"),
            "no remind prefix": (edited("pre.json", alerts=alert(0, id="other:" + first[7:])), "prev alerts[0].id"),
            "unknown kind": (edited("uk.json", alerts=alert(0, id=first + "-x", kind="reminder-passed-x")), "prev alerts[0].id"),
            "duplicate": (edited("dup.json", alerts=alert(1, id=first, kind="reminder-passed")), "prev alerts[1].id duplicate"),
            "kind mismatch": (edited("km.json", alerts=alert(0, kind="reminder-week")), "prev alerts[0].kind"),
        }
        self.assertEqual(len("remind:" + "a" * 178 + ":reminder-passed"), 201)
        for name, (path, why) in cases.items():
            with self.subTest(name):
                out = self.tmp / "never.json"
                self.assertEqual(self.run_on(DATE, "--prev", path, "--json-out", str(out)), (1, ["alert | refused | " + why]))
                self.assertFalse(os.path.lexists(out))
        exact = self.tmp / "exact.json"  # exactly 2 MiB and 200 characters are accepted
        exact.write_bytes(raw + b" " * (MIB2 - len(raw)))
        self.assertEqual(self.run_on(DATE, "--prev", str(exact))[0], 3)
        ok = edited("ok200.json", alerts=alert(0, id="remind:" + "a" * 177 + ":reminder-passed"))
        code, lines = self.run_on(DATE, "--prev", ok)
        self.assertTrue(lines[0].endswith(" | new") and lines[2].endswith(" | (unsafe-label) | reminder-passed"), lines)

    def test_without_prev_bytes_equal_main_golden(self):
        self.write([known("R-3", 3), unknown("U-1", 30)])
        out = self.tmp / "g.json"
        code, lines = self.run_on(DATE, "--json-out", str(out))
        self.assertEqual((code, lines), (3, [  # golden captured from unmodified main on this store
            "alert | remind:sample-alpha-pd:R-3:reminder-week | reminder-week | info | reminder date 2026-11-03 | in 3 days" + TAIL,
            "alert | remind:sample-alpha-pd:U-1:receipt-unknown | receipt-unknown | action | reminder date - | "
            "receipt unknown, sent 30 days ago" + TAIL,
            "alert | open=2 alerts=2 passed=0 today=0 week=1 receipt_unknown=1 waiting=0 law=draft" + SUMMARY_TAIL]))
        doc = {"schema": "flock-alerts-v1", "review_required": True, "as_of": DATE, "law_status": "draft",
               "law_note": "law package is draft: dates unconfirmed", "alerts": [
                   {"id": "remind:sample-alpha-pd:R-3:reminder-week", "source": "request-reminders", "kind": "reminder-week",
                    "severity": "info", "agency": "sample-alpha-pd", "request_id": "R-3", "reminder_date": "2026-11-03",
                    "phrase": "in 3 days"},
                   {"id": "remind:sample-alpha-pd:U-1:receipt-unknown", "source": "request-reminders",
                    "kind": "receipt-unknown", "severity": "action", "agency": "sample-alpha-pd", "request_id": "U-1",
                    "reminder_date": None, "phrase": "receipt unknown, sent 30 days ago"}]}
        self.assertEqual(out.read_bytes(), (json.dumps(doc, indent=2, sort_keys=True) + "\n").encode("utf-8"))


if __name__ == "__main__":
    unittest.main()
