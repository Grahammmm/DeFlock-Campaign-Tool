"""DF0137-06: `today` lists organizer-checked agency contracts ending within --contract-days (default 90) or ended in
the last 30 days, with vendor, end date, source link and the next step. Synthetic agencies only, offline.

Run from the repository root: python3 -B -m unittest tests.test_today_contracts -v
"""
import json
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import today  # noqa: E402
from tests import test_today  # noqa: E402
from tests.test_findings_status import snapshot  # noqa: E402

AS_OF = test_today.AS_OF  # 2026-03-15
CHECKED = "2026-01-15"  # content._contract refuses a checked_on after the wall-clock date, so keep it in the past
SOURCE = "https://council.synthetic.example/agenda?item=7&page=2"
LONG_URL = "https://council.synthetic.example/" + "staff-report/" * 8 + "renewal.pdf"


def day(offset):
    return (AS_OF + timedelta(days=offset)).isoformat()


def agency(name, end=None, start="2024-07-01", note="", **fields):
    entry = {"agency_id": f"ca-synthetic-{name}", "name": f"Synthetic {name} PD", "kind": "police",
             "jurisdiction_name": "Synthetic"}
    if end is not None:
        entry["profile"] = {"contract": {"vendor": f"Vendor {name} (fictional)", "start": start, "end": end,
                                         "renewal_note": note, "source_url": SOURCE, "checked_on": CHECKED}}
    entry.update(fields)
    return entry


class TodayContractTests(unittest.TestCase):
    setUp, ok, build_tree = test_today.TodayTests.setUp, test_today.TodayTests.ok, test_today.TodayTests.build_tree

    def write_agencies(self, *entries):
        path = self.root / "content" / "agencies.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(list(entries)), encoding="utf-8")
        return path

    def contracts(self, items):
        return [item for item in items if item["kind"] == "contract_ending"]

    def test_window_edges_ending_and_ended(self):
        offsets = (0, 90, 91, -1, -30, -31)
        self.write_agencies(*(agency(f"d{index}", day(offset)) for index, offset in enumerate(offsets)),
                            agency("none"), agency("unverified", day(5), verified=False))
        items = today.todo(self.root, AS_OF)
        self.assertEqual(self.kinds(items), ["contract_ending"] * 5, items)
        self.assertEqual([item["due"] for item in items], [day(-30), day(-1), day(0), day(5), day(90)])
        ended, _, on_day, unverified, last = items
        self.assertEqual(on_day["what"], f"Contract with Vendor d0 (fictional) for Synthetic d0 PD (ca-synthetic-d0) "
                         f"ends {day(0)} (in 0 days): ask for the renewal staff report and watch the agenda. Source: "
                         f"{SOURCE} (checked {CHECKED})")
        self.assertIn(f"ends {day(90)} (in 90 days): ask for the renewal staff report", last["what"])
        self.assertEqual(ended["what"], f"Contract with Vendor d4 (fictional) for Synthetic d4 PD (ca-synthetic-d4) "
                         f"ended {day(-30)} (30 days ago): ask whether it was renewed and for the staff report. "
                         f"Source: {SOURCE} (checked {CHECKED})")
        self.assertIn("(ca-synthetic-unverified)", unverified["what"])  # design: the verified flag is not consulted
        self.assertEqual({item["command"] for item in items},
                         {f"meetings --directory {self.root} --client LEGISTAR_CLIENT --online"})
        listed = json.dumps(items)
        for absent in ("ca-synthetic-d2", "ca-synthetic-d5", "ca-synthetic-none"):  # +91, -31 and no contract
            self.assertNotIn(absent, listed)

    def kinds(self, items):
        return [item["kind"] for item in items]

    def test_contract_days_narrows_and_bad_values_stop(self):
        self.write_agencies(agency("a", day(0)), agency("b", day(30)), agency("c", day(31)), agency("d", day(90)),
                            agency("e", day(-3)))
        base = ("today", "--directory", self.root, "--as-of", AS_OF.isoformat(), "--json")
        default = json.loads(self.ok(*base))
        self.assertEqual([item["due"] for item in default], [day(-3), day(0), day(30), day(31), day(90)])
        narrow = json.loads(self.ok(*base, "--contract-days", "30"))
        self.assertEqual([item["due"] for item in narrow], [day(-3), day(0), day(30)])  # ended still listed
        self.assertEqual({item["kind"] for item in narrow}, {"contract_ending"})
        for bad in ("0", "366", "x", "-5", "30.0", ""):
            code, out, err = test_today.run(*base, "--contract-days", bad)
            self.assertEqual((code, out), (2, ""), bad)
            self.assertIn("argument --contract-days:", err, bad)

    def test_ranked_after_finding_blocked_and_by_end_then_agency(self):
        self.build_tree()
        self.write_agencies(agency("zeta", day(20)))
        self.assertEqual(self.kinds(today.todo(self.root, AS_OF)),
                         ["comment_deadline", "request_past_due", "finding_ready", "finding_blocked", "contract_ending",
                          "stale_comment_kit", "stale_contact"])
        self.write_agencies(agency("zeta", day(20)), agency("beta", day(20)), agency("omega", day(10)))
        found = self.contracts(today.todo(self.root, AS_OF))
        self.assertEqual([item["due"] for item in found], [day(10), day(20), day(20)])
        self.assertEqual([item["what"].split("(ca-synthetic-")[1].split(")")[0] for item in found],
                         ["omega", "beta", "zeta"])

    def test_missing_list_no_item_malformed_contract_one_fix_item(self):
        self.build_tree()
        path = self.root / "content" / "agencies.json"
        if path.exists():
            path.unlink()
        self.assertFalse(path.exists())
        self.assertEqual(self.kinds(today.todo(self.root, AS_OF)),
                         ["comment_deadline", "request_past_due", "finding_ready", "finding_blocked",
                          "stale_comment_kit", "stale_contact"])  # no contract item and no fix item
        self.write_agencies(agency("ok", day(10)), agency("bad", day(10), start=day(11)))  # end before start
        items = today.todo(self.root, AS_OF)
        self.assertEqual(self.kinds(items), ["comment_deadline", "request_past_due", "finding_ready", "finding_blocked",
                                             "stale_comment_kit", "stale_contact", "unreadable"], items)
        self.assertEqual(items[-1]["what"], "fix content/agencies.json: content/agencies.json[1].profile.contract.end: "
                                            "must not be before start")
        self.assertEqual(items[-1]["command"], "open content/agencies.json and fix it")
        path.write_text("[{not json", encoding="utf-8")  # unreadable JSON: still one fix item
        broken = [item for item in today.todo(self.root, AS_OF) if item["kind"] == "unreadable"]
        self.assertEqual([item["command"] for item in broken], ["open content/agencies.json and fix it"])

    def test_renewal_note_verbatim_in_text_and_json(self):
        note = f'Vote "Q3" & staff {LONG_URL}'
        self.write_agencies(agency("n", day(45), note=note))
        base = ("today", "--directory", self.root, "--as-of", AS_OF.isoformat())
        item, = json.loads(self.ok(*base, "--json"))
        self.assertEqual(item["kind"], "contract_ending")
        self.assertEqual(set(item), test_today.ITEM_KEYS)
        self.assertTrue(item["what"].endswith(f"(checked {CHECKED}) Note: {note}"), item["what"])
        self.assertEqual(item["what"].count(note), 1)
        text = self.ok(*base)
        self.assertEqual(text, f"1. [contract_ending] {item['what']} — {item['command']}\n")
        for escaped in ("&quot;", "&amp;", "&lt;"):  # CLI output is never HTML-escaped
            self.assertNotIn(escaped, text + json.dumps(item))
        # `<` is refused by the same content validation the build uses: one fix item, never an escaped note.
        self.write_agencies(agency("n", day(45), note='Vote "Q3" <staff>'))
        item, = json.loads(self.ok(*base, "--json"))
        self.assertEqual((item["kind"], item["what"]), ("unreadable", "fix content/agencies.json: content/agencies."
                         "json[0].profile.contract.renewal_note: must be plain text without angle brackets"))

    def test_read_only(self):
        self.build_tree()
        self.write_agencies(agency("r", day(1)), agency("s", day(-2)))
        before = snapshot(self.root)
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("network used")):
            items = json.loads(self.ok("today", "--directory", self.root, "--as-of", AS_OF.isoformat(), "--json"))
        self.assertEqual(len(self.contracts(items)), 2)
        self.assertEqual(snapshot(self.root), before)


if __name__ == "__main__":
    unittest.main()
