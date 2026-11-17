"""agency-next: phone-sized ranking from a synthetic agencies.tsv (read only)."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from campaign_tool.records import agency_next

HEADER = ["agency", "request_ref", "what_was_asked", "current_stage", "last_contact",
          "days_since_last_contact", "overdue_2026-11-05", "next_action", "owner",
          "docs_received"]
OVERDUE_TXT = "on our records, not a legal finding"


def row(agency, stage="awaiting determination", contact="2026-10-01", days="30",
        overdue="no", nxt="send follow-up", owner="G", **extra):
    d = dict(agency=agency, request_ref="REF-CANARY", what_was_asked="ASKED-CANARY",
             current_stage=stage, last_contact=contact, days_since_last_contact=days,
             **{"overdue_2026-11-05": overdue}, next_action=nxt, owner=owner,
             docs_received="DOCS-CANARY")
    d.update(extra)
    return d


def six():
    return [
        row("Pismo-like PD", overdue="YES (no reply)", days="30", contact="2026-10-06 (our follow-up)"),
        row("Closed Town", stage="closed", overdue="no", nxt="none unless new evidence", days="90"),
        row("Alpha PD", days="50"),
        row("Beta PD", days="9"),
        row("Gamma PD", days=""),
        row("Delta PD", overdue="YES (late)", days="12", nxt="none"),
    ]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def write(self, rows, header=HEADER, name="agencies.tsv"):
        p = Path(self.tmp.name) / name
        lines = ["\t".join(header)] + ["\t".join(r.get(h, "") for h in header) for r in rows]
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return p

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = agency_next.main([str(a) for a in args])
        return code, out.getvalue(), err.getvalue()


class AgencyNextTests(Base):
    def test_overdue_no_reply_ranks_above_closed(self):
        code, out, _ = self.run_cli("--agencies", self.write(six()))
        lines = out.splitlines()
        self.assertEqual(code, 0)
        self.assertTrue(lines[0].startswith("Pismo-like PD | "))
        self.assertNotIn("Closed Town", out)

    def test_ranking_overdue_then_days_then_name(self):
        _, out, _ = self.run_cli("--agencies", self.write(six()))
        names = [ln.split(" | ")[0] for ln in out.splitlines()]
        self.assertEqual(names, ["Pismo-like PD", "Delta PD", "Alpha PD", "Beta PD", "Gamma PD"])

    def test_overdue_wording_on_every_overdue_line(self):
        _, out, _ = self.run_cli("--agencies", self.write(six()))
        for ln in out.splitlines():
            if ln.split(" | ")[0] in ("Pismo-like PD", "Delta PD"):
                self.assertIn(OVERDUE_TXT, ln)
            else:
                self.assertNotIn("overdue", ln)

    def test_free_text_overdue_wording_is_neutralised(self):
        rows = [row("Odd PD", stage="Overdue", nxt="OVERDUE follow-up", owner="overdue G"),
                row("Flag PD", overdue="YES", nxt="overdue ask")]
        path = self.write(rows)
        for extra in ((), ("--json",)):
            _, out, _ = self.run_cli("--agencies", path, *extra)
            for chunk in out.replace('"overdue":', "").replace("}", "\n").splitlines():
                if "overdue" in chunk.lower():
                    self.assertIn(OVERDUE_TXT, chunk)
        _, out, _ = self.run_cli("--agencies", path)
        odd = [ln for ln in out.splitlines() if ln.startswith("Odd PD")][0]
        self.assertNotIn("overdue", odd.lower())
        self.assertLess(len(odd), 100)
        _, js, _ = self.run_cli("--agencies", path, "--json")
        self.assertNotIn("OVERDUE", js)

    def test_none_next_action_kept_when_overdue(self):
        _, out, _ = self.run_cli("--agencies", self.write(six()))
        delta = [ln for ln in out.splitlines() if ln.startswith("Delta PD")][0]
        self.assertIn("none recorded", delta)

    def test_missing_column_gives_named_code(self):
        for col in ("agency", "current_stage", "last_contact", "next_action", "overdue_2026-11-05"):
            header = [h for h in HEADER if h != col]
            code, out, err = self.run_cli("--agencies", self.write(six(), header))
            want = "overdue*" if col.startswith("overdue") else col
            self.assertEqual(code, 1)
            self.assertIn("column_missing:" + want, err)
            self.assertEqual(out, "")
        header = [h for h in HEADER if h != "days_since_last_contact"]
        self.assertEqual(self.run_cli("--agencies", self.write(six(), header))[0], 0)

    def test_every_line_under_100_characters(self):
        big = "x" * 120
        rows = [row("A" + big, stage=big, nxt=big, owner=big, overdue="YES")]
        rows += [row("Ag%02d" % i, days=str(i)) for i in range(12)]
        code, out, _ = self.run_cli("--agencies", self.write(rows))
        lines = out.splitlines()
        self.assertEqual(len(lines), 8)
        self.assertTrue(all(len(ln) <= 99 for ln in lines), [len(ln) for ln in lines])
        self.assertTrue(lines[0].endswith("…"))

    def test_no_request_text_names_or_addresses(self):
        rows = [row("Zed PD", nxt="write a@b.example see https://x.example/y now")]
        for args in ((), ("--json",)):
            _, out, _ = self.run_cli("--agencies", self.write(rows), *args)
            for bad in ("ASKED-CANARY", "REF-CANARY", "DOCS-CANARY", "a@b.example", "x.example"):
                self.assertNotIn(bad, out)
            self.assertIn("write", out)

    def test_today_overrides_days_column_and_bad_today_refuses(self):
        rows = [row("Old PD", contact="2026-09-01", days="1"), row("New PD", contact="2026-10-30", days="99")]
        p = self.write(rows)
        _, out, _ = self.run_cli("--agencies", p, "--today", "2026-11-05", "--json")
        got = {r["agency"]: r["days"] for r in json.loads(out)["rows"]}
        self.assertEqual(got, {"Old PD": 65, "New PD": 6})
        _, out, _ = self.run_cli("--agencies", self.write([row("P PD", contact="unknown", days="7")]),
                                 "--today", "2026-11-05", "--json")
        self.assertEqual(json.loads(out)["rows"][0]["days"], 7)
        code, _, err = self.run_cli("--agencies", p, "--today", "2026-13-40")
        self.assertEqual(code, 1)
        self.assertIn("today_invalid", err)

    def test_symlink_oversize_and_non_utf8_refuse(self):
        real = self.write(six())
        link = Path(self.tmp.name) / "link.tsv"
        os.symlink(real, link)
        big = Path(self.tmp.name) / "big.tsv"
        big.write_bytes(b"a" * (2 << 20))
        bad = Path(self.tmp.name) / "bad.tsv"
        bad.write_bytes(b"\xff\xfe\x00bad")
        for p in (link, big, bad):
            code, _, err = self.run_cli("--agencies", p)
            self.assertEqual(code, 1)
            self.assertIn("file_invalid", err)

    def test_input_bytes_unchanged_and_json_shape(self):
        p = self.write(six())
        before = (p.read_bytes(), p.stat().st_mtime_ns)
        _, text, _ = self.run_cli("--agencies", p)
        _, js, _ = self.run_cli("--agencies", p, "--json")
        data = json.loads(js)
        self.assertEqual(data["count"], len(text.splitlines()))
        self.assertEqual(data["total_ranked"], 5)
        self.assertEqual([r["rank"] for r in data["rows"]], [1, 2, 3, 4, 5])
        self.assertEqual(before, (p.read_bytes(), p.stat().st_mtime_ns))


if __name__ == "__main__":
    unittest.main()
