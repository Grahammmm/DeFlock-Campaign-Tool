"""DF1109-02: `today` lists finding reply windows (a private draft whose agency_response is pending with reply_by within
three days or already past) and correction requests open more than 14 days. Read-only; nothing is sent. Synthetic only.

Run from the repository root: python3 -B -m unittest tests.test_today_reply_work -v
"""
import json
import os
import shlex
import socket
import stat
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import correction_requests, today  # noqa: E402
from tests import test_today as base, test_today_request_work as work  # noqa: E402  (modules: not re-collected)
from tests.test_findings_publish import draft  # noqa: E402
from tests.test_findings_status import snapshot  # noqa: E402

AS_OF, ITEM_KEYS, FIXTURES = base.AS_OF, base.ITEM_KEYS, REPO_ROOT / "tests" / "fixtures" / "today"
NEW = ("finding_reply_due", "correction_request_open")
LOG = "private/correction-requests.json"
LOG_REASON = f"fix {LOG}: the correction request log could not be read (reason withheld: it can quote the file)"
SENTINEL = "SENTINEL-PRIVATE-7f3"
CEDAR = "fnd_0000000000000001"  # the published example finding build_tree copies in


def pending(by, prepared="2026-03-01"):
    return {"status": "pending", "prepared_on": prepared, "reply_by": by}


def answered(status):
    reply = {"status": status, "prepared_on": "2026-03-01", "reply_by": "2026-03-10", "asked_on": "2026-03-02",
             "on": "2026-03-10"}
    return {**reply, "text": "Synthetic reply."} if status == "received" else reply


def request(ident, received, state="open", finding=CEDAR):
    entry = {"id": ident, "finding_id": finding, "received_on": received, "summary": SENTINEL + " summary",
             "state": state}
    return entry if state == "open" else {**entry, "decided_on": "2026-03-14", "note": SENTINEL + " note"}


def write_log(root, requests):
    (root / LOG).write_text(json.dumps({"schema_version": 1, "requests": requests}), encoding="utf-8")


def reply_draft(root, name, response, filename=None, folder="drafts"):
    path = root / "private" / folder / (filename or f"{name}.json")
    path.parent.mkdir(mode=0o700, exist_ok=True)
    document = draft(id=name, slug=name.lower().replace("_", "-"), **({} if response is None else
                                                                       {"agency_response": response}))
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def control_tree(case):
    """The post-01/pre-02 control: every DF1109-01 request kind plus only absent, final and out-of-window reply and
    correction states (a pending window four days out, a final no_response, a request open exactly 14 days)."""
    case.build_tree()
    case.steps(case.c, [work.est("2026-03-03", 100), work.step("clarification_requested", "2026-03-04")])
    case.steps(case.a, [work.step("determination", "2026-03-11", expected_on="2026-03-14")])
    reply_draft(case.root, "td-later", pending("2026-03-19"))
    reply_draft(case.root, "td-answered", answered("no_response"))
    write_log(case.root, [request(1, "2026-02-01", "declined"), request(2, "2026-03-01")])


class ReplyWorkTests(unittest.TestCase):
    setUp, ok, build_tree = base.TodayTests.setUp, base.TodayTests.ok, base.TodayTests.build_tree
    sent, steps = work.RequestWorkTests.sent, work.RequestWorkTests.steps

    def new(self, as_of=AS_OF, **options):
        return [item for item in today.todo(self.root, as_of, **options) if item["kind"] in NEW]

    def windows(self, as_of):
        return {item["what"].split()[3]: item["what"] for item in self.new(as_of) if item["kind"] == NEW[0]}

    def test_pending_reply_window_strict_three_day_edge(self):
        paths = {offset: reply_draft(self.root, f"td-{offset + 1}", pending(str(AS_OF + timedelta(offset))))
                 for offset in (-1, 0, 1, 3, 4)}
        items = self.new()
        self.assertEqual([(i["kind"], i["what"], i["due"]) for i in items], [
            (NEW[0], "reply window for td-0 ended 2026-03-14: record the reply or no_response", "2026-03-14"),
            (NEW[0], "reply window for td-1 ends 2026-03-15", "2026-03-15"),
            (NEW[0], "reply window for td-2 ends 2026-03-16", "2026-03-16"),
            (NEW[0], "reply window for td-4 ends 2026-03-18", "2026-03-18")])
        for item, offset in zip(items, (-1, 0, 1, 3)):
            self.assertEqual(set(item), ITEM_KEYS)
            self.assertEqual(shlex.split(item["command"]), [
                "finding", "reply", str(paths[offset]), "--directory", str(self.root), "--status", "STATUS",
                "--on", "YYYY-MM-DD", "--asked-on", "YYYY-MM-DD"])
        said = json.dumps(items).lower()
        for word in ("deadline", "legal", "late", "overdue", "failed", "asked on"):
            self.assertNotIn(word, said)
        for name, by in (("td-leap-in", "2028-03-01"), ("td-leap-out", "2028-03-02"), ("td-year-in", "2027-01-02"),
                         ("td-year-out", "2027-01-03")):
            reply_draft(self.root, name, pending(by))
        self.assertEqual(sorted(name for name in self.windows(date(2026, 12, 30)) if "-year-" in name),
                         ["td-year-in"])
        leap = self.windows(date(2028, 2, 27))  # 2028-02-29 exists: 2028-03-01 is three days out, 03-02 four
        self.assertEqual(leap["td-leap-in"], "reply window for td-leap-in ends 2028-03-01")
        self.assertNotIn("td-leap-out", leap)
        self.assertEqual(leap["td-year-out"], "reply window for td-year-out ended 2027-01-03: record the reply or "
                                              "no_response")

    def test_only_pending_private_drafts_remind(self):
        self.build_tree()
        kinds = [i["kind"] for i in today.todo(self.root, AS_OF)]
        reply_draft(self.root, "td-none", None)
        for status in ("received", "declined", "no_response"):
            reply_draft(self.root, f"td-{status}", answered(status))
        reply_draft(self.root, "td-gone", pending("2026-03-15"))
        (self.root / "private" / "withdrawn").mkdir(mode=0o700)
        (self.root / "private" / "withdrawn" / "td-gone.json").write_text(json.dumps(draft(id="td-gone")),
                                                                          encoding="utf-8")
        published = json.loads((self.root / "content" / "findings" / "cedar-policy-posted.json").read_text("utf-8"))
        published.update(id="pub-only", slug="pub-only", agency_response=pending("2026-03-15"))
        (self.root / "content" / "findings" / "pub-only.json").write_text(json.dumps(published), encoding="utf-8")
        self.assertEqual([i["kind"] for i in today.todo(self.root, AS_OF)], kinds)  # none of these is listed
        correction = reply_draft(self.root, CEDAR, pending("2026-03-16"), "cedar fix.json")  # a pending correction
        twins = [reply_draft(self.root, "td-twin", pending("2026-03-17")),
                 reply_draft(self.root, "td-twin", pending("2026-03-17"), "o'brien twin.json")]
        items = self.new()
        self.assertEqual([i["what"] for i in items], [f"reply window for {CEDAR} ends 2026-03-16",
                                                      "reply window for td-twin ends 2026-03-17",
                                                      "reply window for td-twin ends 2026-03-17"])
        self.assertEqual([shlex.split(i["command"])[2] for i in items],
                         [str(correction), *sorted(map(str, twins))])  # the files actually read, in key order
        self.assertEqual([i["kind"] for i in today.todo(self.root, AS_OF)],
                         kinds[:2] + [NEW[0]] * 3 + kinds[2:])  # existing ready/blocked work unchanged

    def test_bad_response_draft_does_not_hide_healthy_work(self):
        self.build_tree()
        good = reply_draft(self.root, "td-ok", pending("2026-03-16"))
        write_log(self.root, [request(1, "2026-02-01")])
        folder = self.root / "private" / "drafts"
        bad = [reply_draft(self.root, f"td-bad{n}", response) for n, response in enumerate((
            "pending", pending("2026-02-30"), {**pending("2026-03-16"), "status": "maybe"},
            pending("2026-02-27"), {**pending("2026-03-16"), SENTINEL: "x"},
            {**pending("2026-03-16"), "asked_on": "2026-03-02"}))]
        (folder / "td-badjson.json").write_text("{nope " + SENTINEL, encoding="utf-8")
        (self.tmp / "outside.json").write_text(json.dumps(draft(id="td-link", agency_response=pending("2026-03-16"))),
                                               encoding="utf-8")
        (folder / "td-link.json").symlink_to(self.tmp / "outside.json")
        bad += [folder / "td-badjson.json", folder / "td-link.json"]
        items = today.todo(self.root, AS_OF)
        broken = [i for i in items if i["kind"] == "unreadable"]
        self.assertEqual(sorted(i["what"].split(": ")[0][4:] for i in broken), sorted(map(str, bad)), broken)
        self.assertIn(f"fix {bad[0]}: agency_response could not be read (reason withheld: it can quote the file)",
                      [i["what"] for i in broken])  # a response only today checks: named by the path actually read
        self.assertNotIn(SENTINEL, json.dumps(items))
        self.assertEqual([i["kind"] for i in items], ["comment_deadline", "request_past_due", *NEW, "finding_ready",
                                                      "finding_blocked", "stale_comment_kit", "stale_contact"]
                         + ["unreadable"] * len(bad))
        self.assertEqual(shlex.split(items[2]["command"])[2], str(good))
        drafts = self.root / "private" / "drafts"
        drafts.rename(self.tmp / "real-drafts")
        drafts.symlink_to(self.tmp / "real-drafts")  # a linked folder: one item, the log and meetings still listed
        kinds = [i["kind"] for i in today.todo(self.root, AS_OF)]
        self.assertEqual(kinds.count("unreadable"), 1)
        self.assertIn(NEW[1], kinds)
        self.assertIn("comment_deadline", kinds)

    def test_correction_request_strict_fourteen_day_edge(self):
        log = self.root / LOG
        self.assertEqual(self.new(), [])
        self.assertFalse(os.path.lexists(log))  # an absent log is never created
        write_log(self.root, [])
        self.assertEqual(self.new(), [])
        write_log(self.root, [request(1, "2026-03-02"), request(2, "2026-03-01"), request(3, "2026-02-28"),
                              request(4, "2026-03-20"), request(5, "2026-01-01", "accepted"),
                              request(6, "2026-01-01", "declined")])
        [item] = self.new()
        self.assertEqual(item, {"kind": NEW[1], "what": f"Correction request 3 for finding {CEDAR} has been open "
                                "15 days; record a decision", "due": "2026-02-28", "command": item["command"],
                                "rank": 1, "days_open": 15})
        self.assertIs(type(item["days_open"]), int)
        self.assertEqual([i["days_open"] for i in self.new(AS_OF + timedelta(5))], [20, 19, 18])  # by receipt date

    def test_correction_identity_command_and_privacy(self):
        self.root = self.tmp / "o'brien campaign"  # the printed command must survive a space and an apostrophe
        self.ok("init", "--directory", self.root, "--name", "Synthetic", "--county", "San Luis Obispo", "--state", "CA")
        write_log(self.root, [request(2, "2026-02-01"), request(10, "2026-02-01"),
                              request(11, "2026-02-02", finding="fnd-other")])
        items = self.new()
        self.assertEqual([(i["what"].split()[2], i["due"]) for i in items],
                         [("2", "2026-02-01"), ("10", "2026-02-01"), ("11", "2026-02-02")])  # numeric tie order
        self.assertEqual(shlex.split(items[1]["command"]), [
            "correction-request", "decide", "DECISION", "--directory", str(self.root), "--id", "10",
            "--on", "YYYY-MM-DD", "--note", "NOTE"])
        code, out, err = base.run("today", "--directory", self.root, "--as-of", "2026-03-15", "--json")
        self.assertEqual((code, err, json.loads(out)), (0, "", items))
        text = self.ok("today", "--directory", self.root, "--as-of", "2026-03-15")
        for shown in (out, text):
            self.assertNotIn(SENTINEL, shown)
            self.assertNotIn("accept", shown.lower())
        write_log(self.root, [request(2, "2026-02-01", "declined"), request(10, "2026-02-01")])
        with mock.patch.object(correction_requests, "open_overdue", return_value=[2, 10]):  # 2 decided in between
            self.assertEqual([i["what"].split()[2] for i in self.new()], ["10"])

    def test_broken_log_is_one_unreadable_and_other_sources_survive(self):
        self.build_tree()
        reply_draft(self.root, "td-ok", pending("2026-03-16"))
        log = self.root / LOG
        healthy = ["comment_deadline", "request_past_due", NEW[0], "finding_ready", "finding_blocked",
                   "stale_comment_kit", "stale_contact"]
        breaks = {"json": lambda: log.write_text("{nope " + SENTINEL, encoding="utf-8"),
                  "document": lambda: log.write_text("[]", encoding="utf-8"),
                  "duplicate": lambda: write_log(self.root, [request(1, "2026-02-01"), request(1, "2026-02-02")]),
                  "date": lambda: write_log(self.root, [request(1, "2026-02-30")]),
                  "unknown key": lambda: write_log(self.root, [{**request(1, "2026-02-01"), SENTINEL: 1}]),
                  "symlink": lambda: (write_log(self.root, [request(1, "2026-02-01")]),
                                      log.rename(self.tmp / "log.json"), log.symlink_to(self.tmp / "log.json"))}
        for name, broken in breaks.items():
            with self.subTest(case=name):
                broken()
                items = today.todo(self.root, AS_OF)
                self.assertEqual([i["kind"] for i in items], healthy + ["unreadable"])
                self.assertEqual((items[-1]["what"], items[-1]["command"]), (LOG_REASON, f"open {LOG} and fix it"))
                self.assertNotIn(SENTINEL, json.dumps(items))
                log.unlink()
        write_log(self.root, [request(1, "2026-02-01")])
        with mock.patch.object(correction_requests, "list_requests", side_effect=OSError(SENTINEL)):
            self.assertEqual([i["what"] for i in today.todo(self.root, AS_OF) if i["kind"] == "unreadable"],
                             [LOG_REASON])
        kinds = [i["kind"] for i in today.todo(self.root, AS_OF, owner="Pat Organizer")]
        self.assertEqual(kinds, ["comment_deadline", *NEW, "finding_ready", "finding_blocked", "stale_comment_kit",
                                 "stale_contact"])  # --owner filters request work only

    def test_no_reminders_preserves_both_generations_and_writes_nothing(self):
        self.assertEqual(today.KINDS[9:13], ("request_download_pending", "request_download_expired", *NEW))
        self.assertEqual(today.KINDS[13:15], ("finding_ready", "finding_blocked"))
        self.assertEqual(today.KINDS[-5:], ("review_waiting", "stale_body_schedule", "expected_regular_meeting",
                                            "backup_due", "unreadable"))

        def state(root):
            return snapshot(root), {p: stat.S_IMODE(os.lstat(p).st_mode) for p in root.rglob("*")}
        writes = [mock.patch.object(Path, name, side_effect=AssertionError(f"Path.{name} called"))
                  for name in ("write_text", "write_bytes", "mkdir", "touch", "chmod")]
        writes += [mock.patch.object(os, name, side_effect=AssertionError(f"os.{name} called"))
                   for name in ("replace", "rename", "chmod", "mkdir", "unlink")]
        writes += [mock.patch("fcntl.flock", side_effect=AssertionError("lock taken")),
                   mock.patch.object(socket, "socket", side_effect=AssertionError("socket opened"))]
        for generation, prepare in (("legacy01", self.build_tree), ("legacy02", lambda: control_tree(self))):
            with self.subTest(generation=generation):
                self.setUp()
                prepare()
                before = state(self.root)
                for as_json, name in ((False, "text"), (True, "json")):
                    fixture = (FIXTURES / f"{generation}.{name}.txt").read_bytes().decode("utf-8")
                    with work.NoWrites(writes):
                        self.assertEqual(today.run_cli(self.root, "2026-03-15", as_json=as_json)
                                         .replace(str(self.root), "<CAMPAIGN>"), fixture)
                    code, out, err = base.run("today", "--directory", self.root, "--as-of", "2026-03-15",
                                              *(["--json"] if as_json else []))
                    self.assertEqual((code, out.replace(str(self.root), "<CAMPAIGN>"), err), (0, fixture + "\n", ""))
                self.assertEqual(state(self.root), before)  # bytes, modes and the path set
        reply_draft(self.root, "td-soon", pending("2026-03-15"))
        write_log(self.root, [request(1, "2026-02-01")])
        before = state(self.root)
        with work.NoWrites(writes):
            self.assertEqual([i["kind"] for i in self.new()], list(NEW))
        self.assertEqual(state(self.root), before)


if __name__ == "__main__":
    unittest.main()
