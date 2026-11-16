"""DF1109-15: `campaigns today` — one private to-do list across every registered campaign (docs/MULTI-CAMPAIGN.md).

Synthetic campaigns in a temporary folder only; nothing is fetched, sent or published.
Run from the repository root: python3 -B -m unittest tests.test_campaigns_today -v
"""
import contextlib
import fcntl
import io
import json
import os
import shlex
import socket
import subprocess
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import cli, registry, today  # noqa: E402
from campaign_tool.records import campaign_summary, console_home, console_snapshot  # noqa: E402
from tests.records.test_campaign_summary import real_export  # noqa: E402
from tests import test_today  # noqa: E402  (its synthetic tree builder; its tests are not re-run here)

AS_OF = "2026-03-15"
TAIL = " | private; review-required"
WATCH, SEEN, NOW = [], [], {"campaign": None}  # a path under WATCH reached past the guard while another campaign is read
registry.confine("", ())  # the guard first: the recorder below only sees what the guard let through


def _record(event, args):
    if WATCH and NOW["campaign"] and event in registry._PATH_EVENTS:
        for arg in args[:registry._PATH_EVENTS[event]]:
            if isinstance(arg, (str, bytes, os.PathLike)):
                target = Path(os.path.realpath(os.path.abspath(os.fsdecode(arg))))
                SEEN.extend((event, str(target)) for w in WATCH
                            if registry._within(target, w) and not registry._within(target, NOW["campaign"]))


sys.addaudithook(_record)


def snapshot(base):
    """{relative path: (bytes or link target, mode)} of everything under base: the register and every campaign."""
    return {p.relative_to(base).as_posix(): (os.readlink(p) if p.is_symlink() else p.read_bytes() if p.is_file()
                                             else None, p.lstat().st_mode) for p in sorted(Path(base).rglob("*"))}


class CampaignsToday(unittest.TestCase):
    def setUp(self):
        for target in ("socket.socket.connect", "urllib.request.urlopen", "subprocess.Popen"):
            patcher = mock.patch(target, side_effect=AssertionError(f"{target} used"))
            patcher.start()
            self.addCleanup(patcher.stop)
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-campaigns-today-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(os.path.realpath(temporary.name))
        self.register = self.base / "config" / "campaigns.json"
        patch = mock.patch.dict(os.environ, {registry.ENV: str(self.register)})
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(registry.release)
        self.addCleanup(WATCH.clear)

    def run_cli(self, *arguments):
        out, err, code = io.StringIO(), io.StringIO(), 0
        with mock.patch.object(sys, "argv", ["campaign_tool", *map(str, arguments)]), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                cli.main()
            except SystemExit as exc:
                code = exc.code or 0
            finally:
                registry.release()
        self.assertNotIn("Traceback", err.getvalue())
        return code, out.getvalue(), err.getvalue()

    def ok(self, *arguments):  # test_today.TodayTests.build_tree calls this
        code, out, err = self.run_cli(*arguments)
        self.assertEqual(code, 0, err)
        return out

    def campaign(self, name, folder=None, full=False):
        """Register one synthetic campaign: just campaign.json, or test_today's tree with every kind plus a newsletter
        draft without provenance (DF1109-03's outreach_not_checked)."""
        self.root = self.base / (folder or name)
        if full:
            self.ok("init", "--directory", self.root, "--name", "Synthetic", "--county", "San Luis Obispo", "--state", "CA")
            test_today.TodayTests.build_tree(self)
            (self.root / "kit" / "newsletter-draft.json").write_text('{"subject": "Synthetic"}', encoding="utf-8")
        else:
            self.root.mkdir()
            (self.root / "campaign.json").write_text(json.dumps({"schema_version": 1, "name": name, "county":
                                                                 "Synthetic", "state": "CA"}), encoding="utf-8")
        registry.add(name, self.root)
        return self.root

    def today_all(self, *extra):
        code, out, err = self.run_cli("campaigns", "today", "--as-of", AS_OF, *extra)
        self.assertEqual(registry._denied, (), "the guard outlived the command")
        return code, (json.loads(out) if "--json" in extra else out.splitlines()), err

    def expected(self, name, root):  # an independent single-campaign read, outside any guard
        return [{"campaign": name, **item} for item in today.todo(root, date.fromisoformat(AS_OF), campaign_name=name)]

    def test_stable_campaign_order_retains_today_rank_and_metadata(self):
        beta = self.campaign("beta", "b campaign", full=True)  # registered first, listed last
        self.campaign("alpha")
        self.campaign("Alpha")  # case-sensitive: "Alpha" sorts before "alpha"
        code, rows, _ = self.today_all("--json")
        self.assertEqual(code, 0)
        self.assertEqual(rows, self.expected("beta", beta))  # the empty campaigns add no row
        self.assertEqual([r["kind"] for r in rows], ["comment_deadline", "request_past_due", "finding_ready",
                                                     "finding_blocked", "outreach_not_checked", "stale_comment_kit",
                                                     "stale_contact"])
        self.assertEqual([r["rank"] for r in rows], list(range(1, 8)))  # today's own rank, not renumbered
        gamma = self.campaign("gamma", full=True)
        code, rows, _ = self.today_all("--json")
        self.assertEqual(rows, self.expected("beta", beta) + self.expected("gamma", gamma))  # no global urgency sort
        code, lines, _ = self.today_all()
        self.assertEqual(lines[:2], [f"campaign | Alpha | Nothing due on {AS_OF}.{TAIL}",
                                     f"campaign | alpha | Nothing due on {AS_OF}.{TAIL}"])
        self.assertEqual(lines[2:], [f"campaign | {r['campaign']} | {r['rank']}. [{r['kind']}] {r['what']} — "
                                     f"{r['command']}{TAIL}" for r in rows])

    def test_named_selectors_do_not_rewrite_prose_or_operands(self):
        root = self.campaign("north.county-1", "O'Brien county", full=True)
        quoted = shlex.quote(str(root))
        meeting = root / "content" / "meetings" / "m-week.json"  # selector-looking text in an organizer's note
        document = json.loads(meeting.read_text(encoding="utf-8"))
        document["participation"]["written_comment"]["how"] = f"email the clerk; not --directory {quoted}"
        meeting.write_text(json.dumps(document), encoding="utf-8")
        old = today.todo(root, date.fromisoformat(AS_OF))
        _, rows, _ = self.today_all("--json")
        self.assertEqual(len(rows), len(old))
        for before, after in zip(old, rows):
            if before["kind"] == "comment_deadline":  # a human instruction: unchanged, the literal text included
                self.assertEqual(after["command"], before["command"])
                self.assertIn(f"--directory {quoted}", after["command"])
                continue
            self.assertIn(f"--directory {quoted}", before["command"])
            if "campaign_tool.newsletter" in before["command"]:  # that module has no --campaign flag: unchanged
                self.assertEqual(after["command"], before["command"])
            else:
                self.assertEqual(after["command"], before["command"].replace(f"--directory {quoted}",
                                                                              "--campaign north.county-1"))
            self.assertEqual({k: v for k, v in after.items() if k not in ("command", "campaign")},
                             {k: v for k, v in before.items() if k != "command"})
        commands = {r["kind"]: r["command"] for r in rows}
        draft = root / "private" / "drafts" / "td-block.json"  # the positional draft path stays a real path
        self.assertEqual(commands["finding_blocked"], f"finding review-status {shlex.quote(str(draft))} "
                                                      "--campaign north.county-1")
        self.assertEqual(commands["outreach_not_checked"],  # the newsletter module has no --campaign: path selector
                         f"python3 -m campaign_tool.newsletter --directory {quoted} --from-content")
        code, out, err = self.run_cli(*shlex.split(commands["finding_blocked"]))  # printed hints run as printed
        self.assertEqual(code, 0, err)
        self.assertEqual(self.run_cli(*shlex.split(commands["request_past_due"]))[0], 0)

    def legacy(self):
        root = self.campaign("beta", "b campaign", full=True)
        (self.base / "backups").mkdir()
        runs = {}
        for key, extra in (("text", ()), ("json", ("--json",)), ("owner", ("--owner", "Pat")),
                           ("limits", ("--contract-days", "30", "--review-days", "3", "--backups", self.base / "backups",
                                       "--json"))):
            for selector in (("--directory", root), ("--campaign", "beta")):
                code, out, _ = self.run_cli("today", *selector, "--as-of", AS_OF, *extra)
                runs.setdefault(key, set()).add((code, out.replace(str(self.base), "<BASE>")))
        (root / "content" / "body-schedules.json").write_text("{not json", encoding="utf-8")
        for selector in (("--directory", root), ("--campaign", "beta")):
            code, out, _ = self.run_cli("today", *selector, "--as-of", AS_OF)
            runs.setdefault("failing", set()).add((code, out.replace(str(self.base), "<BASE>")))
        return runs

    def test_single_campaign_legacy_bytes(self):
        runs = self.legacy()
        for key, outputs in runs.items():
            self.assertEqual(outputs, {LEGACY[key]}, key)  # --campaign and --directory: one output, the pinned one

    def test_one_bad_campaign_never_hides_neighbors(self):
        for name in ("a-first", "b-ok", "m-middle", "n-ok", "p-denied", "r-raises", "z-last"):
            self.campaign(name)
        (self.base / "a-first" / "campaign.json").rename(self.base / "a-first.json")  # missing campaign.json
        (self.base / "m-middle" / "campaign.json").write_text("{malformed", encoding="utf-8")
        os.rename(self.base / "z-last", self.base / "z-gone")  # missing directory
        real = today.todo

        def todo(root, as_of, **kw):
            if kw["campaign_name"] == "p-denied":
                raise PermissionError("SENTINEL p-denied " + str(root))
            if kw["campaign_name"] == "r-raises":
                raise RuntimeError("SENTINEL r-raises " + str(root))
            return real(root, as_of, **kw)
        with mock.patch.object(today, "todo", side_effect=todo):
            code, lines, err = self.today_all()
            self.assertEqual(code, 3)
            self.assertEqual(lines, [f"campaign | a-first | NOT CURRENT: ValueError{TAIL}",
                                     f"campaign | b-ok | Nothing due on {AS_OF}.{TAIL}",
                                     f"campaign | m-middle | NOT CURRENT: ValueError{TAIL}",
                                     f"campaign | n-ok | Nothing due on {AS_OF}.{TAIL}",
                                     f"campaign | p-denied | NOT CURRENT: PermissionError{TAIL}",
                                     f"campaign | r-raises | NOT CURRENT: RuntimeError{TAIL}",
                                     f"campaign | z-last | NOT CURRENT: ValueError{TAIL}"])
            code, rows, err2 = self.today_all("--json")
        self.assertEqual(code, 3)
        self.assertEqual(rows, [{"campaign": n, "rank": None, "kind": "not_current", "what": f"NOT CURRENT: {t}",
                                 "due": None, "command": None} for n, t in (
            ("a-first", "ValueError"), ("m-middle", "ValueError"), ("p-denied", "PermissionError"),
            ("r-raises", "RuntimeError"), ("z-last", "ValueError"))])
        for text in (err, err2, json.dumps(rows), "\n".join(lines)):
            for leak in ("SENTINEL", str(self.base), "Traceback", "campaign.json"):
                self.assertNotIn(leak, text)

    def test_swallowed_unreadable_becomes_not_current(self):
        broken = self.campaign("broken", full=True)  # healthy sources too, but kit/sent.json is malformed
        (broken / "kit" / "sent.json").write_text("not json", encoding="utf-8")
        ok = self.campaign("ok", full=True)
        other = self.campaign("other", full=True)  # one source is a link to ok's valid file: denied by the guard
        (other / "kit" / "agencies.json").unlink()
        (other / "kit" / "agencies.json").symlink_to(ok / "kit" / "agencies.json")
        self.assertFalse([i for i in today.todo(other, date.fromisoformat(AS_OF)) if i["kind"] == "unreadable"])
        code, rows, _ = self.today_all("--json")
        self.assertEqual(code, 3)
        not_current = {"rank": None, "kind": "not_current", "what": "NOT CURRENT: ValueError", "due": None,
                       "command": None}
        self.assertEqual(rows, [{"campaign": "broken", **not_current}, *self.expected("ok", ok),
                                {"campaign": "other", **not_current}])  # no partial items from either

    def test_each_read_confined_and_always_released(self):
        a, b = self.campaign("a-attacker", full=True), self.campaign("b-target", full=True)
        c = self.campaign("c-swapped")
        (a / "content" / "body-schedules.json").symlink_to(b / "kit" / "agencies.json")  # symlink into b
        (a / "kit" / "newsletter-draft.json").unlink()
        (a / "kit" / "newsletter-draft.json").symlink_to(f"../../{b.name}/kit/newsletter-draft.json")  # ../ into b
        config = json.loads((a / "campaign.json").read_text(encoding="utf-8"))
        config.update(directory=str(b), output=str(b / "public"), inbox=str(b / "kit" / "sent.json"))
        (a / "campaign.json").write_text(json.dumps(config), encoding="utf-8")  # foreign absolute paths
        (c / "campaign.json").unlink()
        (c / "campaign.json").symlink_to(b / "campaign.json")  # campaign.json itself, read before aggregation
        real, real_root, outcomes = registry.confine, registry._root, []

        def confine(root, registered):  # the real guard, plus which campaign it is now reading
            NOW["campaign"] = Path(os.path.realpath(root)) if str(root) else None
            outcomes.append(str(root))
            return real(root, registered)

        def root_of(path):  # a campaign's own campaign.json is read here, before today runs
            NOW["campaign"] = Path(os.path.realpath(path))
            return real_root(path)
        before = snapshot(self.base)
        WATCH[:] = [a, b, c]
        with mock.patch.object(registry, "confine", side_effect=confine), \
                mock.patch.object(registry, "_root", side_effect=root_of):
            code, rows, _ = self.today_all("--json")
        WATCH.clear()
        self.assertEqual(SEEN, [], "a campaign's read reached another registered campaign past the guard")
        self.assertEqual(code, 3)
        self.assertEqual([(r["campaign"], r["kind"]) for r in rows if r["kind"] == "not_current"],
                         [("a-attacker", "not_current"), ("c-swapped", "not_current")])
        self.assertEqual([r for r in rows if r["campaign"] == "b-target"], self.expected("b-target", b))
        self.assertEqual(outcomes, [str(a), str(a), "", str(b), str(b), "", str(c), "", ""])  # pre, post, release
        self.assertEqual(registry._denied, ())
        self.assertEqual(snapshot(self.base), before)

    def test_empty_missing_register_and_single_clock(self):
        self.assertEqual(self.today_all(), (0, [f"campaign | (none registered){TAIL}"], ""))
        self.assertEqual(self.today_all("--json")[:2], (0, []))
        self.assertFalse(self.register.parent.exists())  # nothing created for a missing register
        self.register.parent.mkdir()
        self.register.write_text("[]", encoding="utf-8")
        self.assertEqual(self.today_all()[:2], (0, [f"campaign | (none registered){TAIL}"]))
        self.campaign("one")
        self.campaign("two")
        days, real = iter([date(2026, 3, 15), date(2026, 3, 16)]), today.todo
        seen = []
        with mock.patch.object(registry, "date", **{"today.side_effect": lambda: next(days)}), \
                mock.patch.object(today, "todo", side_effect=lambda root, as_of, **kw: seen.append(as_of) or real(
                    root, as_of, **kw)):
            code, out, _ = self.run_cli("campaigns", "today")  # one default day across a midnight
        self.assertEqual((code, seen), (0, [date(2026, 3, 15)] * 2))
        self.assertEqual(out, f"campaign | one | Nothing due on 2026-03-15.{TAIL}\n"
                              f"campaign | two | Nothing due on 2026-03-15.{TAIL}\n")
        with mock.patch.object(registry, "_load", side_effect=AssertionError("register read")):
            for bad in ("2026-02-30", "2026-3-5", "15/03/2026", ""):
                self.assertEqual(self.run_cli("campaigns", "today", "--as-of", bad),
                                 (1, "", "Stopped: --as-of must be a real YYYY-MM-DD date\n"), bad)
            for flags in (("--directory", self.base), ("--campaign", "one"), ("--owner", "Pat"), ("--backups", "x"),
                          ("--contract-days", "3")):
                self.assertEqual(self.run_cli("campaigns", "today", *flags)[0], 2, flags)

    def test_read_only_no_lock(self):
        self.campaign("beta", full=True)
        self.campaign("plain")
        (self.base / "beta" / "kit" / "sent.json").write_text("not json", encoding="utf-8")  # a failing campaign too
        before = snapshot(self.base)
        refuse = AssertionError("written, locked or run")
        with mock.patch.object(fcntl, "flock", side_effect=refuse), \
                mock.patch.object(registry, "_transaction", side_effect=refuse), \
                mock.patch.object(registry, "_store", side_effect=refuse), \
                mock.patch.object(socket, "create_connection", side_effect=refuse), \
                mock.patch.object(subprocess, "run", side_effect=refuse):
            self.assertEqual(self.today_all()[0], 3)
            self.assertEqual(self.today_all("--json")[0], 3)
        self.assertEqual(snapshot(self.base), before)  # bytes, paths and modes, absent optional files included


    def bind(self, name, export_root, pid):
        entries = json.loads(self.register.read_text())
        next(e for e in entries if e["name"] == name)["records"] = {"export_root": str(export_root), "source_project_id": pid}
        self.register.write_text(json.dumps(entries))

    def two_counties(self):
        """Cedar (site-kit work plus a genuine export) and Pine (no export at all), both fictional."""
        made = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=3)
        exports = self.base / "exports-sandbox"
        exports.mkdir()
        self.addCleanup(self.unlock, exports)
        root, pin = real_export(exports, made)
        cedar, pine = self.campaign("cedar", full=True), self.campaign("pine")
        self.bind("cedar", root, pin.source_project_id)
        self.bind("pine", self.base / "no-such-export-root", "b" * 16)
        model = console_home.load(pin.path, pin.run_id, now=made, source=console_snapshot.SnapshotSource(pin))
        queue = [r for r in model["queue"]["rows"] if r["status"] == "pending"]
        decisions = console_home.decision_rows(model, made.date())
        want = {"drafts_waiting": sum(r["kind"] != "message_link" for r in queue),
                "messages_to_tie": sum(r["kind"] == "message_link" for r in queue),
                "requests_overdue": sum(bool(r["overdue"]) for r in model["waiting"]["rows"]),
                "holds_needing_decision": sum(r["source"] in ("public", "packets") for r in decisions)}
        return cedar, made, want

    @staticmethod
    def unlock(root):
        for base, dirs, names in os.walk(root):
            os.chmod(base, 0o700)

    def test_records_row_per_bound_campaign_additive_and_matching_home(self):
        cedar, made, want = self.two_counties()
        with mock.patch.object(registry, "_utcnow", return_value=made + timedelta(hours=5)):
            code, rows, _ = self.today_all("--json")
            _, lines, _ = self.today_all()
        records = {r["campaign"]: r for r in rows if r["kind"] == "records_summary"}
        self.assertEqual(code, 0)  # a readable summary and a normal "no records export" are both exit 0
        self.assertEqual([r for r in rows if r["kind"] != "records_summary"], self.expected("cedar", cedar))  # rank/order intact
        self.assertEqual([r["campaign"] for r in rows if r["kind"] == "records_summary"], ["cedar", "pine"])
        self.assertEqual(rows.index(records["cedar"]), len(self.expected("cedar", cedar)))  # right after its own site-kit rows
        cedar_row = records["cedar"]
        self.assertEqual((cedar_row["rank"], cedar_row["kind"], cedar_row["due"], cedar_row["command"]), (None, "records_summary", None, None))
        self.assertEqual({k: cedar_row["records"][k] for k in campaign_summary.COUNTS}, want)
        self.assertEqual((cedar_row["records"]["state"], cedar_row["records"]["age_hours"], cedar_row["records"]["stale"],
                          cedar_row["records"]["unavailable"]), ("ok", 5, False, []))
        pine = records["pine"]["records"]
        self.assertEqual((pine["state"], pine["drafts_waiting"], pine["age_hours"]), ("no_export", None, None))
        self.assertIn(f"campaign | pine | records: no records export{TAIL}", lines)
        text = next(l for l in lines if l.startswith("campaign | cedar | records:"))
        for label in ("drafts waiting for review %d" % want["drafts_waiting"], "unmatched messages to tie 0",
                      "requests overdue (on our records; not a legal finding) %d" % want["requests_overdue"],
                      "holds needing a decision %d" % want["holds_needing_decision"], "5 hours old"):
            self.assertIn(label, text)

    def test_stale_export_is_labelled_with_its_age_and_as_of_does_not_backdate(self):
        cedar, made, want = self.two_counties()
        with mock.patch.object(registry, "_utcnow", return_value=made + timedelta(hours=49)):
            code, rows, _ = self.today_all("--json")
            _, lines, _ = self.today_all()
        records = next(r["records"] for r in rows if r["campaign"] == "cedar" and r["kind"] == "records_summary")
        self.assertEqual((code, records["age_hours"], records["stale"]), (0, 49, True))
        self.assertEqual({k: records[k] for k in campaign_summary.COUNTS}, want)  # the clock never changes the counts
        self.assertTrue(any("stale (49 hours old)" in l for l in lines))

    def test_unbound_output_unchanged_and_a_bad_export_is_degraded_not_hiding(self):
        self.campaign("alpha")
        code, rows, _ = self.today_all("--json")
        self.assertEqual((code, rows), (0, []))  # unbound: byte-for-byte the legacy empty result
        cedar, made, _ = self.two_counties()
        top = next((self.base / "exports-sandbox" / "export-root" / "exports").iterdir())
        self.unlock(top.parent)
        target = top / "console-model" / "campaign-summary.json"
        os.chmod(target, 0o600)
        target.write_text("{}")  # tampered after the manifest sealed it
        code, rows, _ = self.today_all("--json")
        self.assertEqual(code, 3)
        records = {r["campaign"]: r["records"] for r in rows if r["kind"] == "records_summary"}
        self.assertEqual((records["cedar"]["code"], records["cedar"]["drafts_waiting"]), ("RECORDS_EXPORT_UNAVAILABLE", None))
        self.assertEqual(records["pine"]["state"], "no_export")  # the neighbour is still shown
        self.assertEqual([r for r in rows if r["kind"] != "records_summary"], self.expected("cedar", cedar))
        _, lines, _ = self.today_all()
        self.assertFalse(any("Nothing due" in l and "pine" in l for l in lines))  # no false all-clear for a bound campaign

    def test_records_read_changes_nothing_and_never_touches_another_campaign(self):
        cedar, made, _ = self.two_counties()
        before = snapshot(self.base)
        self.today_all("--json")
        self.assertEqual(snapshot(self.base), before)  # register, campaigns and export tree: bytes and modes unchanged
        self.assertEqual(registry._denied, ())  # the confinement guard is released


# Captured on the pre-change base (origin/main d7b74bdb, after DF1109-03) with the same synthetic tree; not regenerated.
LEGACY = {
    'text': (0, "1. [comment_deadline] Written comment due 2026-03-22T12:00:00-07:00 for Synthetic Council m-week (m-week) — submit written comment: email the synthetic clerk (agenda: https://agenda.synthetic.example/m-week)\n2. [request_past_due] Follow up with Grover Beach Police Department (ca-grover-beach-police): request sent 2026-03-02 passed its provisional determination date 2026-03-12 3 day(s) ago — kit-status --directory '<BASE>/b campaign'\n3. [finding_ready] Finding td-ready is reviewed and ready to publish — finding publish '<BASE>/b campaign/private/drafts/td-ready.json' --directory '<BASE>/b campaign' --approved-by NAME\n4. [finding_blocked] Finding td-block is blocked: missing_legal_review, missing_privacy_review, missing_summary, need_two_independent_reviewers — finding review-status '<BASE>/b campaign/private/drafts/td-block.json' --directory '<BASE>/b campaign'\n5. [outreach_not_checked] newsletter draft not checked; rebuild with --from-content — python3 -m campaign_tool.newsletter --directory '<BASE>/b campaign' --from-content\n6. [stale_comment_kit] Comment kit for meeting m-week cites cedar-policy-posted (corrected 2026-02-03) without the latest correction — meetings-stage --directory '<BASE>/b campaign' --id m-week --replace\n7. [stale_contact] Re-check the records contact for ca-arroyo-grande-police on its official site (last checked 2025-01-01, 438 days ago) — kit-verify-agency --directory '<BASE>/b campaign' --agency-id ca-arroyo-grande-police --source-url OFFICIAL_URL --checked-on YYYY-MM-DD --records-email ADDRESS\n"),
    'json': (0, '[\n  {\n    "rank": 1,\n    "kind": "comment_deadline",\n    "what": "Written comment due 2026-03-22T12:00:00-07:00 for Synthetic Council m-week (m-week)",\n    "due": "2026-03-22T12:00:00-07:00",\n    "command": "submit written comment: email the synthetic clerk (agenda: https://agenda.synthetic.example/m-week)"\n  },\n  {\n    "rank": 2,\n    "kind": "request_past_due",\n    "what": "Follow up with Grover Beach Police Department (ca-grover-beach-police): request sent 2026-03-02 passed its provisional determination date 2026-03-12 3 day(s) ago",\n    "due": "2026-03-12",\n    "command": "kit-status --directory \'<BASE>/b campaign\'"\n  },\n  {\n    "rank": 3,\n    "kind": "finding_ready",\n    "what": "Finding td-ready is reviewed and ready to publish",\n    "due": null,\n    "command": "finding publish \'<BASE>/b campaign/private/drafts/td-ready.json\' --directory \'<BASE>/b campaign\' --approved-by NAME"\n  },\n  {\n    "rank": 4,\n    "kind": "finding_blocked",\n    "what": "Finding td-block is blocked: missing_legal_review, missing_privacy_review, missing_summary, need_two_independent_reviewers",\n    "due": null,\n    "command": "finding review-status \'<BASE>/b campaign/private/drafts/td-block.json\' --directory \'<BASE>/b campaign\'"\n  },\n  {\n    "rank": 5,\n    "kind": "outreach_not_checked",\n    "what": "newsletter draft not checked; rebuild with --from-content",\n    "due": null,\n    "command": "python3 -m campaign_tool.newsletter --directory \'<BASE>/b campaign\' --from-content"\n  },\n  {\n    "rank": 6,\n    "kind": "stale_comment_kit",\n    "what": "Comment kit for meeting m-week cites cedar-policy-posted (corrected 2026-02-03) without the latest correction",\n    "due": null,\n    "command": "meetings-stage --directory \'<BASE>/b campaign\' --id m-week --replace"\n  },\n  {\n    "rank": 7,\n    "kind": "stale_contact",\n    "what": "Re-check the records contact for ca-arroyo-grande-police on its official site (last checked 2025-01-01, 438 days ago)",\n    "due": "2025-01-01",\n    "command": "kit-verify-agency --directory \'<BASE>/b campaign\' --agency-id ca-arroyo-grande-police --source-url OFFICIAL_URL --checked-on YYYY-MM-DD --records-email ADDRESS"\n  }\n]\n'),
    'owner': (0, "1. [comment_deadline] Written comment due 2026-03-22T12:00:00-07:00 for Synthetic Council m-week (m-week) — submit written comment: email the synthetic clerk (agenda: https://agenda.synthetic.example/m-week)\n2. [finding_ready] Finding td-ready is reviewed and ready to publish — finding publish '<BASE>/b campaign/private/drafts/td-ready.json' --directory '<BASE>/b campaign' --approved-by NAME\n3. [finding_blocked] Finding td-block is blocked: missing_legal_review, missing_privacy_review, missing_summary, need_two_independent_reviewers — finding review-status '<BASE>/b campaign/private/drafts/td-block.json' --directory '<BASE>/b campaign'\n4. [outreach_not_checked] newsletter draft not checked; rebuild with --from-content — python3 -m campaign_tool.newsletter --directory '<BASE>/b campaign' --from-content\n5. [stale_comment_kit] Comment kit for meeting m-week cites cedar-policy-posted (corrected 2026-02-03) without the latest correction — meetings-stage --directory '<BASE>/b campaign' --id m-week --replace\n6. [stale_contact] Re-check the records contact for ca-arroyo-grande-police on its official site (last checked 2025-01-01, 438 days ago) — kit-verify-agency --directory '<BASE>/b campaign' --agency-id ca-arroyo-grande-police --source-url OFFICIAL_URL --checked-on YYYY-MM-DD --records-email ADDRESS\n"),
    'limits': (0, '[\n  {\n    "rank": 1,\n    "kind": "comment_deadline",\n    "what": "Written comment due 2026-03-22T12:00:00-07:00 for Synthetic Council m-week (m-week)",\n    "due": "2026-03-22T12:00:00-07:00",\n    "command": "submit written comment: email the synthetic clerk (agenda: https://agenda.synthetic.example/m-week)"\n  },\n  {\n    "rank": 2,\n    "kind": "request_past_due",\n    "what": "Follow up with Grover Beach Police Department (ca-grover-beach-police): request sent 2026-03-02 passed its provisional determination date 2026-03-12 3 day(s) ago",\n    "due": "2026-03-12",\n    "command": "kit-status --directory \'<BASE>/b campaign\'"\n  },\n  {\n    "rank": 3,\n    "kind": "finding_ready",\n    "what": "Finding td-ready is reviewed and ready to publish",\n    "due": null,\n    "command": "finding publish \'<BASE>/b campaign/private/drafts/td-ready.json\' --directory \'<BASE>/b campaign\' --approved-by NAME"\n  },\n  {\n    "rank": 4,\n    "kind": "finding_blocked",\n    "what": "Finding td-block is blocked: missing_legal_review, missing_privacy_review, missing_summary, need_two_independent_reviewers",\n    "due": null,\n    "command": "finding review-status \'<BASE>/b campaign/private/drafts/td-block.json\' --directory \'<BASE>/b campaign\'"\n  },\n  {\n    "rank": 5,\n    "kind": "outreach_not_checked",\n    "what": "newsletter draft not checked; rebuild with --from-content",\n    "due": null,\n    "command": "python3 -m campaign_tool.newsletter --directory \'<BASE>/b campaign\' --from-content"\n  },\n  {\n    "rank": 6,\n    "kind": "stale_comment_kit",\n    "what": "Comment kit for meeting m-week cites cedar-policy-posted (corrected 2026-02-03) without the latest correction",\n    "due": null,\n    "command": "meetings-stage --directory \'<BASE>/b campaign\' --id m-week --replace"\n  },\n  {\n    "rank": 7,\n    "kind": "stale_contact",\n    "what": "Re-check the records contact for ca-arroyo-grande-police on its official site (last checked 2025-01-01, 438 days ago)",\n    "due": "2025-01-01",\n    "command": "kit-verify-agency --directory \'<BASE>/b campaign\' --agency-id ca-arroyo-grande-police --source-url OFFICIAL_URL --checked-on YYYY-MM-DD --records-email ADDRESS"\n  },\n  {\n    "rank": 8,\n    "kind": "backup_due",\n    "what": "No verified backup found in <BASE>/backups.",\n    "due": null,\n    "command": "backup --directory \'<BASE>/b campaign\' --into <BASE>/backups"\n  }\n]\n'),
    'failing': (0, "1. [comment_deadline] Written comment due 2026-03-22T12:00:00-07:00 for Synthetic Council m-week (m-week) — submit written comment: email the synthetic clerk (agenda: https://agenda.synthetic.example/m-week)\n2. [request_past_due] Follow up with Grover Beach Police Department (ca-grover-beach-police): request sent 2026-03-02 passed its provisional determination date 2026-03-12 3 day(s) ago — kit-status --directory '<BASE>/b campaign'\n3. [finding_ready] Finding td-ready is reviewed and ready to publish — finding publish '<BASE>/b campaign/private/drafts/td-ready.json' --directory '<BASE>/b campaign' --approved-by NAME\n4. [finding_blocked] Finding td-block is blocked: missing_legal_review, missing_privacy_review, missing_summary, need_two_independent_reviewers — finding review-status '<BASE>/b campaign/private/drafts/td-block.json' --directory '<BASE>/b campaign'\n5. [outreach_not_checked] newsletter draft not checked; rebuild with --from-content — python3 -m campaign_tool.newsletter --directory '<BASE>/b campaign' --from-content\n6. [stale_comment_kit] Comment kit for meeting m-week cites cedar-policy-posted (corrected 2026-02-03) without the latest correction — meetings-stage --directory '<BASE>/b campaign' --id m-week --replace\n7. [stale_contact] Re-check the records contact for ca-arroyo-grande-police on its official site (last checked 2025-01-01, 438 days ago) — kit-verify-agency --directory '<BASE>/b campaign' --agency-id ca-arroyo-grande-police --source-url OFFICIAL_URL --checked-on YYYY-MM-DD --records-email ADDRESS\n8. [unreadable] fix content/body-schedules.json: body-schedules.json: not valid UTF-8 JSON (Expecting property name enclosed in double quotes: line 1 column 2 (char 1)) — open content/body-schedules.json and fix it\n"),
}


if __name__ == "__main__":
    unittest.main()
