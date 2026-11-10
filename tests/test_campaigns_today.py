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
from datetime import date
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import cli, registry, today  # noqa: E402
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
            self.assertEqual(after["command"], before["command"].replace(f"--directory {quoted}",
                                                                          "--campaign north.county-1"))
            self.assertEqual({k: v for k, v in after.items() if k not in ("command", "campaign")},
                             {k: v for k, v in before.items() if k != "command"})
        commands = {r["kind"]: r["command"] for r in rows}
        draft = root / "private" / "drafts" / "td-block.json"  # the positional draft path stays a real path
        self.assertEqual(commands["finding_blocked"], f"finding review-status {shlex.quote(str(draft))} "
                                                      "--campaign north.county-1")
        self.assertEqual(commands["outreach_not_checked"],
                         "python3 -m campaign_tool.newsletter --campaign north.county-1 --from-content")
        code, out, err = self.run_cli(*shlex.split(commands["finding_blocked"]))  # printed hints run as printed
        self.assertEqual(code, 0, err)
        self.assertEqual(self.run_cli(*shlex.split(commands["request_past_due"]))[0], 0)
        from campaign_tool.newsletter.__main__ import main as newsletter
        with contextlib.redirect_stdout(io.StringIO()) as out:
            try:  # the newsletter module takes --campaign too (read-only --check)
                self.assertEqual(newsletter(["--campaign", "north.county-1", "--check"]), 3)
            finally:
                registry.release()
        self.assertIn("not checked", out.getvalue())

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
