"""NB-DF-07: digest keeps LEADS.csv triage state across digests; a flagged link is listed once; lead sets state."""
import contextlib
import csv
import fcntl
import importlib.util
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("_test_monitor_leads", ROOT / "tools" / "flock_monitor.py")
monitor = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(monitor)
D1, D2 = "2026-10-30", "2026-10-31"
A, B = "M-%012x" % 0xA1, "M-%012x" % 0xB2


def item(n, title, seen_at=D1, agency="Example City"):
    return dict(item_id="M-%012x" % n, agency=agency, watch_kind="agenda", url="https://www.city.example/i/%x" % n,
                title=title, page_sha256="0" * 64, seen_at=seen_at, kind="public", status="unreviewed")


DAY1 = [item(0xA1, "Flock camera contract"), item(0x01, "Budget hearing"), item(0xB2, "ALPR policy", agency="Zed Town"),
        item(0x02, "Parks update"), item(0x03, "Library hours")]


class LeadsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp, self.store = Path(tmp.name), Path(tmp.name) / "store"
        self.store.mkdir(mode=0o700)
        self.leads = self.store / "LEADS.csv"

    def monitor_rows(self, rows):
        with open(self.store / "MONITOR.csv", "w", encoding="utf-8", newline="") as out:
            writer = csv.DictWriter(out, monitor.MONITOR_HEADER, lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)

    def run_main(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = monitor.main(list(args))
        return code, out.getvalue().splitlines()

    def digest(self, date=D1, expect=0):
        code, out = self.run_main("digest", "--store", str(self.store), "--date", date)
        self.assertEqual(code, expect, out)
        return out if expect else (self.store / "digest" / (date + ".md")).read_text(encoding="utf-8").splitlines()

    def lead(self, item_id, state, by="Organizer A", date=D2):
        return self.run_main("lead", "--store", str(self.store), "--item", item_id, "--state", state, "--by", by,
                             "--date", date)

    def table(self):
        with open(self.leads, encoding="utf-8", newline="") as handle:
            return list(csv.reader(handle))

    def section(self, lines):
        return lines[lines.index("") + 1:]

    def hold_lock(self):
        fd = os.open(self.store / ".monitor.lock", os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_day1_two_relevant_become_new_leads_others_never(self):
        self.monitor_rows(DAY1)
        lines = self.digest()
        self.assertEqual(lines[0], "Flock monitor 2026-10-30: 5 new items (public, unreviewed), 2 relevant")
        self.assertEqual(self.section(lines), [
            "Leads (2 new)",
            "- Example City: Flock camera contract https://www.city.example/i/a1 [%s]" % A,
            "- Zed Town: ALPR policy https://www.city.example/i/b2 [%s]" % B])
        self.assertEqual(self.table(), [monitor.LEADS_HEADER, [A, "Example City", D1, "new", D1, "digest"],
                                        [B, "Zed Town", D1, "new", D1, "digest"]])
        self.assertEqual(self.leads.stat().st_mode & 0o777, 0o600)

    def test_day2_known_links_not_relisted_whatever_state(self):
        self.monitor_rows(DAY1)
        self.digest()
        self.assertEqual(self.lead(B, "ignored"), (0, ["lead | %s | new -> ignored" % B]))
        self.monitor_rows(DAY1 + [item(0xA1, "Flock camera contract", D2), item(0xB2, "ALPR policy", D2, "Zed Town"),
                                  item(0xC3, "Surveillance use policy", D2), item(0x04, "Other", D2)])
        lines = self.digest(D2)
        self.assertEqual(self.section(lines), ["Leads (1 new)",
                                               "- Example City: Surveillance use policy https://www.city.example/i/c3 "
                                               "[M-0000000000c3]"])
        self.assertEqual([r[0] for r in self.table()[1:]], [A, B, "M-0000000000c3"])
        self.assertEqual(self.table()[2], [B, "Zed Town", D1, "ignored", D2, "Organizer A"])  # state kept

    def test_same_day_rerun_is_byte_identical(self):
        self.monitor_rows(DAY1)
        first = self.digest()
        before = self.leads.read_bytes()
        self.assertEqual(self.digest(), first)
        self.assertEqual(self.leads.read_bytes(), before)

    def test_duplicate_relevant_rows_make_one_lead(self):
        self.monitor_rows(DAY1 + [item(0xA1, "Flock camera contract")])
        self.assertEqual(self.section(self.digest())[0], "Leads (2 new)")
        self.assertEqual(len(self.table()), 3)

    def test_no_relevant_items_no_section_no_file(self):
        self.monitor_rows([item(0x01, "Budget hearing")])
        self.assertEqual(self.digest(), ["Flock monitor 2026-10-30: 1 new items (public, unreviewed), 0 relevant",
                                         "- Example City agenda: Budget hearing https://www.city.example/i/1"])
        self.assertFalse(self.leads.exists())

    def test_leads_flag_is_gone(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            monitor.main(["digest", "--store", str(self.store), "--date", D1, "--leads"])

    def test_scan_committed_while_digest_waits_is_read_under_the_lock(self):
        self.monitor_rows(DAY1)
        real = monitor.store_lock

        @contextlib.contextmanager
        def late_scan(store, *args):  # a scan commits a relevant row for D1 just before digest gets the lock
            self.monitor_rows(DAY1 + [item(0xC3, "Surveillance use policy")])
            with real(store, *args):
                yield

        with mock.patch.object(monitor, "store_lock", late_scan):
            lines = self.digest()
        self.assertEqual(lines[0], "Flock monitor 2026-10-30: 6 new items (public, unreviewed), 3 relevant")
        self.assertEqual(self.section(lines)[0], "Leads (3 new)")
        self.assertEqual([r[0] for r in self.table()[1:]], [A, "M-0000000000c3", B])

    def test_lead_moves_allowed_and_refused(self):
        self.monitor_rows(DAY1)
        self.digest()
        self.assertEqual(self.lead(A, "ignored"), (0, ["lead | %s | new -> ignored" % A]))
        self.assertEqual(self.lead(A, "ignored"), (0, ["lead | %s | ignored -> ignored unchanged" % A]))
        self.assertEqual(self.lead(A, "requested", by="  Organizer   B ", date="2026-11-01"),
                         (0, ["lead | %s | ignored -> requested" % A]))
        self.assertEqual(self.table()[1], [A, "Example City", D1, "requested", "2026-11-01", "Organizer B"])
        before = self.leads.read_bytes()
        for args, why in (((A, "ignored"), "lead: %s cannot move requested -> ignored" % A),
                          ((B, "new"), "lead: --state is not requested or ignored"),
                          ((B, "Requested"), "lead: --state is not requested or ignored"),
                          (("M-%012x" % 0xEE, "ignored"), "lead: unknown item M-0000000000ee")):
            with self.subTest(args=args):
                self.assertEqual(self.lead(*args), (1, [why]))
        self.assertEqual(self.lead(B, "ignored")[0], 0)
        self.assertEqual(self.lead(B, "new"), (1, ["lead: --state is not requested or ignored"]))  # ignored -> new
        self.assertEqual(self.lead(A, "requested"), (0, ["lead | %s | requested -> requested unchanged" % A]))
        self.assertEqual(self.table()[1], [A, "Example City", D1, "requested", "2026-11-01", "Organizer B"])
        self.assertNotEqual(self.leads.read_bytes(), before)  # only B's move was written

    def test_lead_bad_item_and_by_refused_file_unchanged(self):
        self.monitor_rows(DAY1)
        self.digest()
        before = self.leads.read_bytes()
        for bad in ("M-0000000000A1", "M-00000000a1", "M-0000000000a1x", "m-0000000000a1", "0000000000a1", ""):
            with self.subTest(item=bad):
                self.assertEqual(self.lead(bad, "ignored"), (1, ["lead: --item is not M- and 12 lowercase hex"]))
        for by in ("", "   ", "\t\n"):
            with self.subTest(by=by):
                self.assertEqual(self.lead(A, "ignored", by=by), (1, ["lead: --by is empty"]))
        self.assertEqual(self.leads.read_bytes(), before)

    def test_lead_without_leads_file_creates_nothing(self):
        missing = self.tmp / "nostore"
        code, out = self.run_main("lead", "--store", str(missing), "--item", A, "--state", "ignored", "--by", "X")
        self.assertEqual((code, out, missing.exists()), (1, ["lead: unknown item " + A], False))
        self.assertEqual(self.lead(A, "ignored"), (1, ["lead: unknown item " + A]))
        self.assertFalse(self.leads.exists())

    def test_held_lock_refuses_digest_and_lead_nothing_written(self):
        self.monitor_rows(DAY1)
        self.digest()
        before = self.leads.read_bytes()
        self.monitor_rows(DAY1 + [item(0xC3, "Surveillance use policy", D2)])
        self.hold_lock()
        busy = ["store busy: another monitor command is running"]
        self.assertEqual(self.digest(D2, expect=1), busy)
        self.assertEqual(self.lead(A, "requested"), (1, busy))
        self.assertEqual((self.leads.read_bytes(), (self.store / "digest" / (D2 + ".md")).exists()), (before, False))

    def test_symlinked_or_folder_leads_refused_no_digest(self):
        self.monitor_rows(DAY1)
        target = self.tmp / "TARGET.csv"
        target.write_text(",".join(monitor.LEADS_HEADER) + "\n", encoding="utf-8")
        self.leads.symlink_to(target)
        self.assertEqual(self.digest(expect=1), ["LEADS.csv is a symlink"])
        self.assertEqual(self.lead(A, "ignored"), (1, ["LEADS.csv is a symlink"]))
        self.assertEqual(target.read_text(encoding="utf-8"), ",".join(monitor.LEADS_HEADER) + "\n")
        self.leads.unlink()
        self.leads.mkdir()
        self.assertEqual(self.digest(expect=1), ["LEADS.csv is not a file"])
        self.assertFalse((self.store / "digest" / (D1 + ".md")).exists())

    def test_ragged_or_wrong_header_leads_refused_nothing_written(self):
        self.monitor_rows(DAY1)
        for text, why in ((",".join(monitor.LEADS_HEADER) + "\n%s,Example City,%s,new\n" % (A, D1),
                           "LEADS.csv row 2: columns have the wrong count"),
                          ("item_id,agency,first_seen,state\n", "LEADS.csv row 1: header is not the accepted header")):
            with self.subTest(why=why):
                self.leads.write_text(text, encoding="utf-8")
                self.assertEqual(self.digest(expect=1), [why])
                self.assertEqual(self.lead(A, "ignored"), (1, [why]))
                self.assertEqual(self.leads.read_text(encoding="utf-8"), text)
                self.assertFalse((self.store / "digest" / (D1 + ".md")).exists())


if __name__ == "__main__":
    unittest.main()
