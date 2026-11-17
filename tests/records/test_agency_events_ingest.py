"""Synthetic-only tests for records agency-events; no live mail, network or send."""
import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import tempfile
import unittest

from campaign_tool.records.agency_run import events_ingest
from campaign_tool.records.outcomes import timeline

ALIASES = {"agency": "Pismo Test PD", "agency_id": "pismo-test", "event_prefix": "PTP",
           "aliases": ["Pismo Test PD", "pismotest.example"], "requests": {"26-901": "req-901"}}
CANARY = "CANARY-BODY-TEXT"


def receipt(uid, folder="Sent", when="25-Aug-2026 04:11:00 +0000", subject="Records request 26-901",
            frm="clerk@pismotest.example", date=None):
    headers = {"From": [frm], "To": ["records@pismotest.example"], "Subject": [subject + " " + CANARY],
               "Message-ID": [f"<m{uid}@x.example>"]}
    if date:
        headers["Date"] = [date]
    data = {"folder": folder, "uid": uid, "headers": headers}
    if when:
        data["internaldate"] = when
    return data


class Case(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.store = self.root / "mail"
        (self.store / "receipts").mkdir(parents=True)
        (self.store / "SYNC-STATUS.json").write_text(json.dumps({"failures": []}))
        self.aliases = self.root / "aliases.json"
        self.aliases.write_text(json.dumps(ALIASES))
        self.out = self.root / "out"
        self.n = 0

    def add(self, **kw):
        self.n += 1
        (self.store / "receipts" / f"{self.n}.json").write_text(json.dumps(receipt(self.n, **kw)))

    def run_cmd(self, out=None, aliases=None):
        o, e = io.StringIO(), io.StringIO()
        with redirect_stdout(o), redirect_stderr(e):
            code = events_ingest.main(["--receipts", str(self.store), "--agency-aliases", str(aliases or self.aliases),
                                       "--out", str(out or self.out)])
        return code, o.getvalue(), e.getvalue()

    def rows(self, name="events.tsv"):
        return [l.split("\t") for l in (self.out / name).read_text().splitlines()]


class Tests(Case):
    def test_golden_events_for_sheriff_shaped_run(self):
        self.add()
        self.add(when="06-Sep-2026 17:00:00 +0000")
        self.add(folder="INBOX", when="26-Aug-2026 17:00:00 +0000", frm="clerk@pismotest.example")
        code, out, _ = self.run_cmd()
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), {"ok": True, "events": 4, "manual_events": 2, "unclassified": 1,
                                           "bad_receipts": 0, "unmapped_refs": []})
        note = events_ingest.CLOCK_NOTE
        cols = "\t".join(events_ingest.COLUMNS)
        sent = "Message sent from the DeFlock SLO mailbox to the agency"
        fup = "Follow-up message sent from the DeFlock SLO mailbox to the agency"
        golden = "\n".join([
            cols,
            f"PTP-001\t2026-08-24\t21:11 PDT\tPismo Test PD\t26-901\trequest_sent\t{sent}\tSent:1 Message-ID m1@x.example\tDeFlock SLO\tDATED\treceipt basis: recorded",
            f"PTP-002\t2026-08-25\t\tPismo Test PD\t26-901\tclock_start\tSection 7922.535 clock start\tcomputed\tcomputed\tINFERRED\t{note}",
            f"PTP-003\t2026-09-04\t\tPismo Test PD\t26-901\tclock_due\tSection 7922.535 clock due date\tcomputed\tcomputed\tINFERRED\t{note}",
            f"PTP-004\t2026-09-06\t10:00 PDT\tPismo Test PD\t26-901\tfollow_up_sent\t{fup}\tSent:2 Message-ID m2@x.example\tDeFlock SLO\tDATED\treceipt basis: recorded",
        ]) + "\n"
        self.assertEqual((self.out / "events.tsv").read_text(), golden)
        self.assertEqual(self.rows("unclassified.tsv")[1], ["INBOX", "3", "2026-08-26", "inbound_needs_review"])

    def test_message_at_0020z_dates_previous_pacific_day(self):
        self.add(when="02-Nov-2026 00:20:00 +0000")
        self.run_cmd()
        row = self.rows()[1]
        self.assertEqual((row[1], row[2]), ("2026-11-01", "16:20 PST"))

    def test_after_hours_rule_boundary(self):
        for when in ("25-Aug-2026 23:59:00 +0000", "26-Aug-2026 00:00:00 +0000"):
            self.n = 0
            self.out = self.root / when[:2]
            self.add(when=when)
            self.run_cmd()
            self.assertEqual([r[1] for r in self.rows() if r[5] == "clock_start"],
                             ["2026-08-25" if when.startswith("25") else "2026-08-26"])

    def test_recorded_vs_proxy_basis(self):
        self.add(when=None, date="Tue, 25 Aug 2026 04:11:00 +0000")
        self.add(when=None, subject="Records request 26-901 again")
        self.run_cmd()
        self.assertTrue(self.rows()[1][-1].endswith("proxy"))
        self.assertEqual(self.rows("unclassified.tsv")[1][3], "no_time")

    def test_unmatched_messages_listed_not_dropped(self):
        self.add()
        self.n += 1
        (self.store / "receipts" / "2.json").write_text(json.dumps({
            "folder": "Sent", "uid": 2, "internaldate": "25-Aug-2026 04:11:00 +0000",
            "headers": {"From": ["a@b.example"], "To": ["c@d.example"], "Subject": ["Other 26-901"]}}))
        self.add(subject="Records request")
        self.add(subject="Records request 26-777")
        code, out, _ = self.run_cmd()
        self.assertEqual([r[3] for r in self.rows("unclassified.tsv")[1:]],
                         ["no_agency_match", "no_request_ref", "request_unmapped"])
        report = json.loads(out)
        self.assertEqual(report["unmapped_refs"], ["26-777"])
        self.assertEqual(report["unclassified"], 4 - 1)  # receipts minus the one request_sent event

    def test_manual_events_pass_real_timeline_reader(self):
        self.add()
        self.add(when="06-Sep-2026 17:00:00 +0000")
        self.run_cmd()
        events = timeline.read_manual_events(self.out / "manual-events.jsonl")
        self.assertEqual([e["type"] for e in events], ["sent", "our follow-up sent"])
        self.assertEqual(len({e["event_id"] for e in events}), 2)
        self.assertEqual(events[0]["request_id"], "req-901")

    def test_events_tsv_schema_rules(self):
        self.add()
        self.add(when="06-Sep-2026 17:00:00 +0000")
        self.run_cmd()
        text = (self.out / "events.tsv").read_text()
        rows = self.rows()
        self.assertEqual(rows[0], list(events_ingest.COLUMNS))
        self.assertNotIn("<", text)
        self.assertNotIn(">", text)
        self.assertEqual({r[5] for r in rows[1:]}, {"request_sent", "follow_up_sent", "clock_start", "clock_due"})
        self.assertEqual([r[0] for r in rows[1:]], [f"PTP-{n:03d}" for n in range(1, 5)])
        self.assertTrue(all(len(r) == 11 for r in rows))

    def test_refusals(self):
        self.add()
        (self.store / "receipts" / "9.json").symlink_to(self.store / "receipts" / "1.json")
        code, out, _ = self.run_cmd()
        self.assertEqual((code, json.loads(out)["bad_receipts"]), (0, 1))
        code, _, err = self.run_cmd()
        self.assertEqual((code, err.strip()), (1, "agency-events: out_not_empty"))
        self.assertEqual(self.run_cmd(out=self.store / "receipts" / "o")[2].strip(), "agency-events: out_inside_receipts")
        (self.store / "SYNC-STATUS.json").write_text(json.dumps({"failures": ["x"]}))
        self.assertEqual(self.run_cmd(out=self.root / "o2")[2].strip(), "agency-events: sync_status_unhealthy")
        for text in ('{"agency": "A", "agency": "B"}', json.dumps(ALIASES | {"aliases": ["ab"]})):
            self.aliases.write_text(text)
            self.assertEqual(self.run_cmd(out=self.root / "o3")[2].strip(), "agency-events: aliases_invalid")

    def test_store_bytes_unchanged_and_no_body_text(self):
        self.add()
        self.add(folder="INBOX")
        snap = lambda: {str(p): p.read_bytes() for p in sorted(self.store.rglob("*")) if p.is_file()}
        before = snap()
        self.run_cmd()
        self.assertEqual(snap(), before)
        for p in self.out.iterdir():
            self.assertNotIn(CANARY, p.read_text())
            self.assertEqual(p.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
