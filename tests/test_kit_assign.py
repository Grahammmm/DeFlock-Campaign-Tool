"""kit-assign: private request owners and the --owner views (DF1103-05). Offline, synthetic, nothing is sent."""
import contextlib
import csv
import io
import json
import os
import shlex
import shutil
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import cli, kit_track  # noqa: E402
from tests.test_kit_check import ready_drafts  # noqa: E402

STAMP = "2026-03-01T12:00:00+00:00"
H1, H2 = "a" * 64, "b" * 64
DONE = "Recorded only. Nothing was sent.\n"
ASOF = ("--as-of", "2026-03-15")  # a request sent 03-02 is past due, 03-05 is due today, 03-10 is upcoming


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


def imported(sent_on, steps=()):  # a request recorded with kit-sent-import: no draft is needed
    return {"sent_on": sent_on, "channel": "email", "imported": True, "recorded_at": STAMP, "steps": list(steps)}


def without_owner(value):
    if isinstance(value, dict):
        return {k: without_owner(v) for k, v in value.items() if k != "owner"}
    return [without_owner(v) for v in value] if isinstance(value, list) else value


def tree(root):  # every file with its mtime and size: a view must change none of them
    return {str(p.relative_to(root)): (p.is_symlink(), p.lstat().st_mtime_ns, p.lstat().st_size)
            for p in Path(root).rglob("*") if not p.is_dir() or p.is_symlink()}


def read_csv(path):
    with open(path, encoding="utf-8", newline="") as handle:
        return list(csv.reader(handle))


class KitAssignTests(unittest.TestCase):
    def setUp(self):
        offline = mock.patch("urllib.request.urlopen", side_effect=AssertionError("network used"))
        offline.start()
        self.addCleanup(offline.stop)
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-kit-assign-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name).resolve()
        self.root = self.tmp / "campaign"
        self.ok("init", "--directory", self.root, "--name", "Synthetic", "--county", "San Luis Obispo", "--state", "CA")
        self.ok("kit", "--directory", self.root)
        self.kit, self.store, self.sent = self.root / "kit", self.root / "kit" / "assignments.json", \
            self.root / "kit" / "sent.json"
        self.a, self.b, self.c, self.d = (p.stem for p in sorted((self.kit / "requests").glob("*.md"))[:4])

    def ok(self, *arguments):
        code, out, err = run(*arguments)
        self.assertEqual(code, 0, err)
        return out

    def stopped(self, *arguments, words=()):
        code, out, err = run(*arguments)
        self.assertEqual((code, out), (1, ""), err)
        self.assertTrue(err.startswith("Stopped: "), err)
        for word in words:
            self.assertIn(word, err)
        return err

    def put_sent(self, requests):
        self.sent.write_text(json.dumps({"schema_version": 1, "requests": requests}), encoding="utf-8")

    def assign(self, agency, owner, *extra):
        return self.ok("kit-assign", "--directory", self.root, "--agency", agency, "--to", owner, *extra)

    def clear(self, agency, *extra):
        return self.ok("kit-assign", "--directory", self.root, "--agency", agency, "--clear", *extra)

    def stored(self):
        return json.loads(self.store.read_text(encoding="utf-8"))["assignments"]

    def write_store(self, assignments):
        self.store.write_text(json.dumps({"schema_version": 1, "assignments": assignments}), encoding="utf-8")

    def json_of(self, *arguments, expect=0):
        code, out, err = run(*arguments)
        self.assertEqual(code, expect, err)
        return json.loads(out)

    def status(self, *extra):
        return self.json_of("kit-status", "--directory", self.root, *extra)["requests"]

    def due(self, *extra, expect=0):
        return self.json_of("kit-due", "--directory", self.root, *ASOF, *extra, expect=expect)

    def today(self, *extra):
        return self.json_of("today", "--directory", self.root, *ASOF, "--json", *extra)

    def test_assign_reassign_clear_main_named_imported(self):
        a, b = self.a, self.b
        ready_drafts(self.root, (a,))
        self.ok("kit-sent", "--directory", self.root, "--agency", a, "--date", "2026-03-02", "--channel", "email")
        requests = json.loads(self.sent.read_text())["requests"]
        requests[f"{a}#press"] = imported("2026-03-03")  # no draft file exists for it
        requests[b] = imported("2026-03-04", [{"on": "2026-03-06", "kind": "closed"}])  # closed and imported
        self.put_sent(requests)
        out = self.assign(a, "Alex")
        self.assertTrue(out.endswith(DONE), out)
        self.assertEqual(json.loads(out.removesuffix(DONE)),
                         {"agency_id": a, "request": "main", "owner": "Alex", "changed": True})
        self.assertEqual(stat.S_IMODE(self.store.stat().st_mode), 0o600)
        self.assertEqual(json.loads(self.store.read_text()), {"schema_version": 1, "assignments": {a: "Alex"}})
        self.assign(a, "Sam", "--request", "press")  # a named request is its own identity
        self.assign(b, "Kim")
        self.assertEqual(self.stored(), {a: "Alex", f"{a}#press": "Sam", b: "Kim"})
        os.chmod(self.store, 0o644)
        self.assign(a, "  Alex   Q ")  # whitespace collapses; a broader old mode is tightened on write
        self.assertEqual(self.stored()[a], "Alex Q")
        self.assertEqual(stat.S_IMODE(self.store.stat().st_mode), 0o600)
        before = (self.store.read_bytes(), self.store.stat().st_mtime_ns)
        again = self.assign(a, "Alex Q")  # the same owner is a no-op
        self.assertFalse(json.loads(again.removesuffix(DONE))["changed"])
        self.assertEqual((self.store.read_bytes(), self.store.stat().st_mtime_ns), before)
        self.clear(a, "--request", "press")
        self.assertEqual(self.stored(), {a: "Alex Q", b: "Kim"})
        before = self.store.read_bytes()
        self.assertFalse(json.loads(self.clear(a, "--request", "press").removesuffix(DONE))["changed"])
        self.assertEqual(self.store.read_bytes(), before)
        self.store.unlink()
        self.clear(b)  # clearing a request that has no owner creates nothing
        self.assertFalse(self.store.exists())
        # the stores are separate: re-recording the request or undoing a step never touches an owner
        self.assign(a, "Alex")
        before = self.store.read_bytes()
        self.ok("kit-sent", "--directory", self.root, "--agency", a, "--date", "2026-03-05", "--channel", "email",
                "--replace")
        self.ok("kit-response", "--directory", self.root, "--agency", a, "--date", "2026-03-06", "--kind",
                "acknowledged")
        self.ok("kit-response-undo", "--directory", self.root, "--agency", a, "--reason", "Wrong date typed")
        self.assertEqual(self.store.read_bytes(), before)
        self.assertEqual({r["agency_id"]: r.get("owner") for r in self.status("--request", "main")}, {a: "Alex", b: None})

    def test_label_limits_and_private_refusals(self):
        a = self.a
        self.put_sent({a: imported("2026-03-02"), f"{a}#press": imported("2026-03-02")})
        for label in ("A", "B" * 40, "Team 123456", "Mary-Ann O'Neil", "Zoë"):  # 1 and 40 characters; six digits
            with self.subTest(accepted=label):
                self.assign(a, label)
                self.assertEqual(self.stored()[a], label)
        self.assign(a, "Alex Q")
        baseline = self.store.read_bytes()
        for label in ("", "   ", "C" * 41, "ann@example.org", "ann\uff20example.org", "(805) 555-0100", "805.555.0100",
                      "\u0660\u0661\u0662\u0663\u0664\u0665\u0666\u0667", "\uff11\uff12\uff13\uff14\uff15\uff16\uff17",
                      "tab\there", "nul\x00byte", "zero\u200bwidth", "<b>x</b>", "a>b"):
            with self.subTest(refused=label):
                err = self.stopped("kit-assign", "--directory", self.root, "--agency", a, "--to", label, words=("--to",))
                self.assertNotIn(label.strip() or "\0", err)
                self.assertEqual(self.store.read_bytes(), baseline)
        for command in (("kit-status",), ("kit-due",), ("today",), ("kit-status", "--csv", self.tmp / "o.csv")):
            for label in ("ann@example.org", "555-123-4567", "<b>"):
                err = self.stopped(*command, "--directory", self.root, "--owner", label, words=("--owner",))
                self.assertNotIn(label, err)
        self.assertFalse((self.tmp / "o.csv").exists())
        # labels compare exactly after whitespace collapse: case is kept, runs of spaces are not
        self.assertEqual([r["request"] for r in self.status("--owner", "  Alex   Q")], ["main"])
        self.assertEqual(self.status("--owner", "alex q"), [])
        self.assertEqual(self.status("--owner", "Alex"), [])
        self.assign(a, "Pat Lee", "--request", "press")
        quoted = [i["command"] for i in self.today("--owner", "Pat   Lee") if i["kind"].startswith("request_")]
        self.assertTrue(quoted and all(c.endswith("--owner " + shlex.quote("Pat Lee")) for c in quoted), quoted)

    def test_unknown_targets_and_bad_store_write_nothing(self):
        a = self.a
        self.put_sent({a: imported("2026-03-02"), "ca-ghost-pd": imported("2026-03-02")})
        (self.kit / "requests" / f"{a}--late.md").write_text("an unsent draft\n")
        before = tree(self.root)
        for arguments, word in (((self.d, "main"), "no recorded request"), (("ca-nowhere-pd", "main"), "--agency"),
                                (("ca-ghost-pd", "main"), "--agency"), ((a, "nope"), "no recorded request"),
                                ((a, "late"), "no recorded request"), ((a, "Bad Name"), "--request")):
            with self.subTest(arguments=arguments):
                self.stopped("kit-assign", "--directory", self.root, "--agency", arguments[0], "--request", arguments[1],
                             "--to", "Alex", words=(word,))
                self.stopped("kit-assign", "--directory", self.root, "--agency", arguments[0], "--request", arguments[1],
                             "--clear", words=(word,))
        self.assertEqual(tree(self.root), before)
        self.assertFalse(self.store.exists())
        outside = self.tmp / "outside.json"
        outside.write_text('{"schema_version":1,"assignments":{}}')
        outside_bytes = outside.read_bytes()
        bad = {"not json": b"{not json", "bad utf8": b"\xff\xfe", "oversized": b"[" + b" " * (1 << 20) + b"]",
               "repeated top key": b'{"schema_version":1,"schema_version":1,"assignments":{}}',
               "repeated agency": f'{{"schema_version":1,"assignments":{{"{a}":"A","{a}":"B"}}}}'.encode(),
               "wrong version": b'{"schema_version":2,"assignments":{}}',
               "bool version": b'{"schema_version":true,"assignments":{}}',
               "missing version": b'{"assignments":{}}', "extra key": b'{"schema_version":1,"assignments":{},"x":1}',
               "list": b'{"schema_version":1,"assignments":[]}',
               "number label": f'{{"schema_version":1,"assignments":{{"{a}":5}}}}'.encode(),
               "unnormalized label": f'{{"schema_version":1,"assignments":{{"{a}":"A  B"}}}}'.encode(),
               "email label": f'{{"schema_version":1,"assignments":{{"{a}":"x@y.example"}}}}'.encode(),
               "empty label": f'{{"schema_version":1,"assignments":{{"{a}":""}}}}'.encode(),
               "noncanonical main": f'{{"schema_version":1,"assignments":{{"{a}#main":"A"}}}}'.encode(),
               "bad key": b'{"schema_version":1,"assignments":{"BAD KEY":"A"}}'}
        for name, content in bad.items():
            with self.subTest(store=name):
                self.store.write_bytes(content)
                for command in (("kit-assign", "--agency", a, "--to", "Alex"), ("kit-assign", "--agency", a, "--clear"),
                                ("kit-status",), ("kit-due",), ("kit-coverage",)):
                    self.stopped(command[0], "--directory", self.root, *command[1:], words=("kit/assignments.json",))
                self.assertEqual(self.store.read_bytes(), content)
        self.store.unlink()
        self.store.mkdir()  # not a regular file
        self.stopped("kit-assign", "--directory", self.root, "--agency", a, "--to", "Alex", words=("regular file",))
        self.stopped("kit-status", "--directory", self.root, words=("regular file",))
        self.store.rmdir()
        self.store.symlink_to(outside)
        self.stopped("kit-assign", "--directory", self.root, "--agency", a, "--to", "Alex", words=("regular file",))
        self.stopped("kit-status", "--directory", self.root, words=("regular file",))
        self.assertEqual(outside.read_bytes(), outside_bytes)
        self.store.unlink()
        alternate = self.tmp / "alternate"  # a symlinked kit/ is refused before anything is read or written
        alternate.mkdir()
        shutil.copy(self.root / "campaign.json", alternate)
        (alternate / "kit").symlink_to(self.kit)
        self.stopped("kit-assign", "--directory", alternate, "--agency", a, "--to", "Alex", words=("symlink",))
        self.stopped("kit-status", "--directory", alternate, words=("symlink",))
        self.assertFalse(self.store.exists())
        # a valid assignment for a request that no longer exists is kept in the file and ignored by every view
        self.write_store({"ca-gone-pd": "Zed"})
        self.assign(a, "Alex")
        self.assertEqual(self.stored(), {"ca-gone-pd": "Zed", a: "Alex"})
        self.clear(a)
        self.assertEqual(self.stored(), {"ca-gone-pd": "Zed"})
        self.assertEqual(self.status("--owner", "Zed"), [])
        self.assertNotIn("Zed", json.dumps(self.status()))
        # argparse: exactly one action, and the help names both
        for extra in ((), ("--to", "Alex", "--clear")):
            self.assertEqual(run("kit-assign", "--directory", self.root, "--agency", a, *extra)[0], 2)
        code, out, _ = run("kit-assign", "--help")
        self.assertEqual(code, 0)
        self.assertTrue("--to" in out and "--clear" in out and "--request" in out)

    def due_fixture(self):
        a, b, c, d = self.a, self.b, self.c, self.d
        self.put_sent({
            a: imported("2026-03-10"),  # upcoming
            b: imported("2026-03-05"),  # due today
            c: imported("2026-03-02"),  # past due
            d: imported("2026-03-01", [{"on": "2026-03-02", "kind": "produced", "download_expires_on": "2026-03-10"}]),
            f"{a}#late": imported("2026-03-01", [{"on": "2026-03-02", "kind": "determination",
                                                  "expected_on": "2026-03-08"}]),  # the agency's date passed
            f"{a}#waiting": imported("2026-03-01", [{"on": "2026-03-02", "kind": "clarification_requested"}])})

    def test_owner_filters_intersect_before_counts_and_exit(self):
        a, b, c, d = self.a, self.b, self.c, self.d
        self.due_fixture()
        baseline, sent_bytes = self.due(expect=3), self.sent.read_bytes()
        self.assertNotIn("owner", json.dumps(baseline))  # legacy shape without a store
        for agency, request, owner in ((a, "main", "Alex"), (b, "main", "Sam"), (c, "main", "Sam"), (d, "main", "Sam"),
                                       (a, "late", "Sam"), (a, "waiting", "Sam")):
            self.assign(agency, owner, "--request", request)
        self.assertEqual(self.sent.read_bytes(), sent_bytes)
        self.assertEqual(without_owner(self.due(expect=3)), baseline)  # dates and state never change
        alex = self.due("--owner", "Alex")  # exit 0: another owner's past-due requests do not count
        self.assertEqual([(r["agency_id"], r["request"], r["state"], r["owner"]) for r in alex["open"]],
                         [(a, "main", "upcoming", "Alex")])
        self.assertEqual((alex["answered"], alex["stated_passed"]), ([], []))
        self.assertEqual((alex["past_due"], alex["stated_date_passed"], alex["waiting_on_you"]), (0, 0, 0))
        self.assertNotIn("Waiting on you", alex["note"])
        self.assertNotIn("download_expired", alex)
        sam = self.due("--owner", "Sam", expect=3)
        self.assertEqual((sam["past_due"], sam["stated_date_passed"], sam["waiting_on_you"], sam["download_expired"]),
                         (1, 1, 1, 1))
        self.assertIn("Waiting on you", sam["note"])
        collections = sam["open"] + sam["answered"] + sam["stated_passed"]
        self.assertEqual({r["owner"] for r in collections}, {"Sam"})  # every collection, downloads included
        self.assertIn("download", {r.get("kind") for r in sam["open"]})
        self.assertNotIn(a, {r["agency_id"] for r in sam["open"] if r["request"] == "main"})
        nobody = self.due("--owner", "Kim")  # a valid label nobody holds: empty, exit 0
        self.assertEqual((nobody["open"], nobody["answered"], nobody["stated_passed"], nobody["past_due"]), ([], [], [], 0))
        late = self.due("--request", "late", "--owner", "Sam", expect=3)  # --request and --owner intersect
        self.assertEqual({(r["agency_id"], r["request"]) for r in late["answered"] + late["stated_passed"]}, {(a, "late")})
        self.assertEqual(self.due("--request", "late", "--owner", "Alex")["answered"], [])
        self.stopped("kit-due", "--directory", self.root, *ASOF, "--request", "nope", "--owner", "Alex",
                     words=("no sent request",))  # an unknown request is refused whoever the owner is
        self.stopped("kit-due", "--directory", self.root, *ASOF, "--request", "nope", words=("no sent request",))
        self.assertEqual([r["owner"] for r in self.status("--owner", "Alex")], ["Alex"])

    def test_today_filters_only_requests(self):
        a, b, c, d = self.a, self.b, self.c, self.d
        self.put_sent({
            a: imported("2026-03-02"), f"{a}#press": imported("2026-03-02"), b: imported("2026-03-03"),
            c: imported("2026-03-02"),
            f"{c}#files": imported("2026-03-01", [{"on": "2026-03-02", "kind": "produced",
                                                   "download_expires_on": "2026-03-20"}])})
        path = self.kit / "agencies.json"  # a stale records contact: a source that is not request work
        agencies = json.loads(path.read_text())
        for entry in agencies:
            if entry["agency_id"] == d:
                entry.update(selected=True, verified=True, records_email="records@agency.synthetic.example",
                             verification={"source_url": "https://agency.synthetic.example/records",
                                           "fields": ["records_email"], "checked_on": "2025-01-01"})
        path.write_text(json.dumps(agencies))
        for agency, request, owner in ((a, "main", "Alex"), (a, "press", "Pat Lee"), (c, "main", "Pat Lee"),
                                       (c, "files", "Pat Lee")):
            self.assign(agency, owner, "--request", request)

        def requests(items):
            return [i for i in items if i["kind"].startswith("request_")]
        everything = self.today()
        self.assertEqual([i["rank"] for i in everything], list(range(1, len(everything) + 1)))
        self.assertEqual(len(requests(everything)), 5)
        texts = [i["what"] for i in requests(everything)]
        self.assertEqual(sum(t.endswith(" [owner: Alex]") for t in texts), 1)
        self.assertEqual(sum(t.endswith(" [owner: Pat Lee]") for t in texts), 3)
        self.assertEqual(sum("[owner:" not in t for t in texts), 1)  # b is unassigned: no suffix
        self.assertTrue(any(f"({a}/press)" in t for t in texts) and any(f"({a})" in t for t in texts))
        past = [t for t in texts if "passed its provisional determination" in t and f"({a}" in t]
        self.assertEqual([("/press)" in t) for t in past], [False, True])  # main, then the named one: stable tie order
        pat = self.today("--owner", "Pat Lee")
        self.assertEqual([i["rank"] for i in pat], list(range(1, len(pat) + 1)))
        self.assertEqual(len(requests(pat)), 3)
        self.assertTrue(all(t.endswith(" [owner: Pat Lee]") for t in (i["what"] for i in requests(pat))))
        self.assertIn("request_download_pending", {i["kind"] for i in requests(pat)})
        self.assertIn("stale_contact", {i["kind"] for i in pat})  # other sources stay
        alex = self.today("--owner", "Alex")
        self.assertEqual(len(requests(alex)), 1)
        self.assertIn("stale_contact", {i["kind"] for i in alex})
        kim = self.today("--owner", "Kim")
        self.assertEqual(requests(kim), [])
        self.assertIn("stale_contact", {i["kind"] for i in kim})
        # an unreadable store is one item naming it, never an unfiltered list, and nothing else is hidden
        self.store.write_text("{not json")
        for extra in ((), ("--owner", "Pat Lee")):
            items = self.today(*extra)
            self.assertEqual(requests(items), [])
            bad = [i for i in items if i["kind"] == "unreadable"]
            self.assertEqual(len(bad), 1, items)
            self.assertIn("kit/assignments.json", bad[0]["what"])
            self.assertNotIn("kit/sent.json", bad[0]["what"])
            self.assertIn("stale_contact", {i["kind"] for i in items})
        self.assertEqual(self.store.read_text(), "{not json")

    def test_coverage_unassigned_first_and_unique_agency_totals(self):
        a, b, c, d = self.a, self.b, self.c, self.d
        path = self.kit / "agencies.json"
        agencies = json.loads(path.read_text())
        for entry in agencies:
            entry["selected"] = entry["agency_id"] in (a, b, c)
        path.write_text(json.dumps(agencies))
        fee = {"on": "2026-03-04", "kind": "fee_estimate", "amount_cents": 10000}
        self.put_sent({
            a: imported("2026-03-02", [fee, {"on": "2026-03-05", "kind": "produced", "records": [H1]}]),
            f"{a}#press": imported("2026-03-03", [fee, {"on": "2026-03-06", "kind": "produced", "records": [H1, H2]}]),
            c: imported("2026-03-04"), f"{c}#late": imported("2026-03-06"),
            d: imported("2026-03-01"), f"{d}#x": imported("2026-03-01")})  # d is not selected
        self.write_store({a: "Alex", c: "=Sam", b: "Ghost", "ca-gone-pd": "Zed"})  # b has no request: ignored
        report = kit_track.coverage(self.root)
        self.assertEqual([(r["agency_id"], r["request"]) for r in report["rows"]],
                         [(b, "main"), (a, "press"), (c, "late"), (a, "main"), (c, "main")])
        self.assertEqual([r["owner"] for r in report["rows"]], [None, None, None, "Alex", "=Sam"])
        self.assertEqual([r["sent_on"] for r in report["rows"]],
                         [None, "2026-03-03", "2026-03-06", "2026-03-02", "2026-03-04"])
        by_row = {(r["agency_id"], r["request"]): r for r in report["rows"]}
        self.assertEqual((by_row[a, "main"]["records"], by_row[a, "press"]["records"]), (1, 2))
        self.assertEqual((by_row[a, "main"]["drafted"], by_row[a, "press"]["drafted"]), (True, False))
        self.assertEqual(by_row[a, "press"]["fee_state"], "unpaid")
        self.assertEqual(report["totals"], {
            "selected": 3, "drafted": 3, "sent": 2, "answered": 1, "with_records": 1, "with_findings": 0,
            "unselected_with_requests": 1, "fee_unpaid": 1, "requests": 5, "unassigned_requests": 3})
        self.assertIn("Unassigned requests come first", report["note"])
        table = self.ok("kit-coverage", "--directory", self.root).splitlines()
        self.assertEqual(table[0].split()[-2:], ["request", "owner"])
        self.assertTrue(table[1].startswith(b) and table[1].split()[-2:] == ["main", "-"])
        self.assertTrue(table[4].split()[-2:] == ["main", "Alex"] and table[5].split()[-2:] == ["main", "=Sam"])
        self.assertTrue(any(line.startswith("totals: selected 3,") and line.endswith("requests 5, unassigned_requests 3")
                            for line in table))
        rows = list(csv.reader(io.StringIO(self.ok("kit-coverage", "--directory", self.root, "--csv"))))
        self.assertEqual(rows[0][-2:], ["request", "owner"])
        self.assertEqual(len(rows), 6)
        self.assertEqual([(r[0], r[-2], r[-1]) for r in rows[1:]],
                         [(b, "main", ""), (a, "press", ""), (c, "late", ""), (a, "main", "Alex"),
                          (c, "main", "'=Sam")])  # a spreadsheet formula in a label is quoted
        self.clear(a)
        self.assertEqual(kit_track.coverage(self.root)["totals"]["unassigned_requests"], 4)

    def test_state_and_legacy_output_unchanged(self):
        a = self.a
        self.put_sent({a: imported("2026-03-02"), f"{a}#press": imported("2026-03-03")})
        legacy = self.status()
        self.assertTrue(all("owner" not in row for row in legacy))
        legacy_due = self.due(expect=3)
        self.assertNotIn("owner", json.dumps(legacy_due))
        first = self.tmp / "legacy.csv"
        self.ok("kit-status", "--directory", self.root, "--csv", first)
        table = read_csv(first)
        self.assertEqual(table[0][-2:], ["request_url", "owner"])
        self.assertEqual([row[-1] for row in table[1:]], ["", ""])  # blank when unassigned
        self.assign(a, "Owner Marker Zed")
        self.assign(a, "=Sam", "--request", "press")
        rows = self.status()
        self.assertEqual([r.get("owner") for r in rows], ["Owner Marker Zed", "=Sam"])
        self.assertEqual(without_owner(rows), legacy)  # nothing but the owner differs
        code, out, _ = run("kit-status", "--directory", self.root)
        self.assertEqual(len(json.loads(out)["requests"]), 2)  # stdout stays one JSON document
        second = self.tmp / "filtered.csv"
        self.ok("kit-status", "--directory", self.root, "--csv", second, "--owner", "=Sam")
        self.assertEqual(stat.S_IMODE(second.stat().st_mode), 0o600)
        table = read_csv(second)
        self.assertEqual([(r[0], r[-1]) for r in table[1:]], [(a, "'=Sam")])
        self.ok("build", "--directory", self.root)  # an owner is private: nothing public mentions it
        public = [p for p in (self.root / "public").rglob("*") if p.is_file()]
        self.assertTrue(public)
        for path in public:
            self.assertNotIn("Owner Marker Zed", path.read_text(encoding="utf-8", errors="replace"), path.name)

    def test_atomic_failure_and_read_only_views(self):
        a, b = self.a, self.b
        self.put_sent({a: imported("2026-03-02"), b: imported("2026-03-03")})
        before_tree = tree(self.root)
        with mock.patch("os.replace", side_effect=OSError("injected")):
            code, out, err = run("kit-assign", "--directory", self.root, "--agency", a, "--to", "Alex")
        self.assertEqual((code, out), (1, ""), err)
        self.assertEqual(tree(self.root), before_tree)  # no store and no temporary file
        self.assign(a, "Alex")
        stored = self.store.read_bytes()
        others = {k: v for k, v in tree(self.root).items() if k != "kit/assignments.json"}
        self.assertEqual(others, before_tree)  # the only change is the private store
        with mock.patch("os.replace", side_effect=OSError("injected")):
            self.assertEqual(run("kit-assign", "--directory", self.root, "--agency", b, "--to", "Sam")[0], 1)
            self.assertEqual(run("kit-assign", "--directory", self.root, "--agency", a, "--clear")[0], 1)
        self.assertEqual(self.store.read_bytes(), stored)
        self.assertEqual([p.name for p in self.kit.iterdir() if p.name.startswith(".assignments")], [])
        snapshot = tree(self.root)
        for arguments in (("kit-status",), ("kit-status", "--owner", "Alex"), ("kit-due", *ASOF), ("kit-due", "--owner", "Sam"),
                          ("kit-coverage",), ("kit-coverage", "--csv"), ("today", *ASOF), ("today", "--owner", "Alex")):
            code, _, err = run(*arguments[:1], "--directory", self.root, *arguments[1:])
            self.assertIn(code, (0, 3), err)
            self.assertEqual(tree(self.root), snapshot, arguments)
        # a refused store creates no CSV and leaves a file outside the campaign (a symlink target) alone
        outside = self.tmp / "outside.json"
        outside.write_text("{}")
        self.store.unlink()
        self.store.symlink_to(outside)
        csv_path = self.tmp / "none.csv"
        self.stopped("kit-status", "--directory", self.root, "--csv", csv_path, words=("kit/assignments.json",))
        self.assertFalse(csv_path.exists())
        self.assertEqual(outside.read_text(), "{}")


if __name__ == "__main__":
    unittest.main()
