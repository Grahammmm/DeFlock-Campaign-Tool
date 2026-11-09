"""DF1422-17: `recent --directory D --since DAY [--until DAY] [--md FILE] [--json]`, a private read-only report.

Synthetic campaign folders only; no network. Run from the repository root:
python3 -B -m unittest tests.test_recent -v
"""
import contextlib
import hashlib
import io
import json
import os
import shlex
import stat
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import cli, recent  # noqa: E402

TODAY = date(2026, 11, 4)
SYNTHETIC_NOTE, SYNTHETIC_ADDRESS = "Synthetic private note: call back Tuesday", "records-clerk@agency.synthetic.example"
REVIEWER, REASON, CORRECTION = "Synthetic Reviewer Pat", "Synthetic withdrawal reason", "Synthetic correction note"
HASH_A, HASH_B, HASH_C = (c * 64 for c in "abc")
PAYLOAD = "synthetic-payload-must-not-be-read"


def run(*arguments):
    out, err, code = io.StringIO(), io.StringIO(), 0
    with mock.patch.object(sys, "argv", ["campaign_tool", *map(str, arguments)]), \
            mock.patch.object(recent, "_today", return_value=TODAY), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            cli.main()
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


def tree(root):
    """(size, mtime_ns, sha256) of every path under root, symlinks not followed."""
    result = {}
    for folder, dirs, files in os.walk(root):
        for path in [folder] + [os.path.join(folder, name) for name in dirs + files]:
            info = os.lstat(path)
            digest = hashlib.sha256(Path(path).read_bytes()).hexdigest() if stat.S_ISREG(info.st_mode) else None
            result[os.path.relpath(path, root)] = (info.st_size, info.st_mtime_ns, digest)
    return result


class RecentTests(unittest.TestCase):
    def setUp(self):
        offline = mock.patch("urllib.request.urlopen", side_effect=AssertionError("network used"))
        offline.start()
        self.addCleanup(offline.stop)
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-recent-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name).resolve()
        self.root = self.tmp / "campaign"
        self.root.mkdir()
        self.put("campaign.json", {"schema_version": 1, "name": "Synthetic", "county": "San Luis Obispo", "state": "CA"})
        self.dir = shlex.quote(str(self.root))

    def put(self, relative, document):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(document if isinstance(document, str) else json.dumps(document), encoding="utf-8")
        return path

    def sent(self, *requests):
        """kit/sent.json from (agency, sent_on, steps) triples, with the agency list that names them."""
        self.put("kit/agencies.json", [{"agency_id": agency, "name": f"Synthetic {agency} Police",
                                        "records_email": SYNTHETIC_ADDRESS} for agency, _, _ in requests])
        self.put("kit/sent.json", {"schema_version": 1, "requests": {
            agency: {"sent_on": on, "channel": "email", "draft": f"requests/{agency}.md", "draft_sha256": HASH_C,
                     "recorded_at": f"{on}T10:00:00+00:00", "steps": steps} for agency, on, steps in requests}})

    def finding(self, folder, ident, **fields):
        return self.put(f"{folder}/{ident}.json", {"id": ident, "title": f"Title of {ident}", "slug": ident, **fields})

    def report(self, since, until=None, **kwargs):
        out, err = io.StringIO(), io.StringIO()
        code = recent.run_cli(self.root, since, until, today=TODAY, out=out, err=err, **kwargs)
        return code, out.getvalue(), err.getvalue()

    # 1
    def test_one_event_per_area_gives_four_groups_and_equal_json(self):
        self.sent(("ca-one", "2026-11-02", []))
        self.put("content/sources.json", [{"sha256": HASH_A, "title": "T", "agency_id": "ca-one",
                                           "received_at": "2026-11-02T08:00:00Z", "public_url": None, "fragment": "f"}])
        self.finding("content/findings", "fnd_pub", published_at="2026-11-03T09:00:00Z", corrections=[])
        self.put("content/meetings/council.json", {"id": "council", "body": "Synthetic Council",
                                                  "starts_at": "2026-11-03T18:00:00-08:00"})
        code, out, err = self.report("2026-11-02", "2026-11-03")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, "\n".join([
            "Recent changes, 2026-11-02 to 2026-11-03 (private; nothing was sent or published)", "",
            "Requests",
            f"2026-11-02  Request sent to Synthetic ca-one Police  → kit-status --directory {self.dir}", "",
            "Records",
            f"2026-11-02  1 record added to the library: {HASH_A[:12]}  → sources list --directory {self.dir}", "",
            "Findings",
            f"2026-11-03  Finding published: Title of fnd_pub  → finding status --directory {self.dir}", "",
            "Meetings",
            "2026-11-03  Meeting on 2026-11-03: Synthetic Council (council)  → open content/meetings/council.json",
            ""]))
        code, as_json, _ = self.report("2026-11-02", "2026-11-03", as_json=True)
        events = json.loads(as_json)
        self.assertEqual(code, 0)
        self.assertEqual(sorted((e["on"], e["area"], e["what"], e["command"]) for e in events),
                         sorted((e["on"], e["area"], e["what"], e["command"]) for e in
                                recent.report(self.root, "2026-11-02", "2026-11-03", TODAY)))
        self.assertEqual({e["area"] for e in events}, set(recent.AREAS))
        self.assertTrue(all(set(e) == {"on", "area", "what", "command"} for e in events))
        for event in events:  # every printed text line is one of the JSON events
            self.assertIn(f"{event['on']}  {event['what']}  → {event['command']}", out)

    def test_every_kind_of_event_is_listed_newest_first(self):
        self.sent(("ca-two", "2026-11-01", [{"on": "2026-11-02", "kind": "acknowledged", "recorded_at": "x"},
                                            {"on": "2026-11-03", "kind": "produced", "partial": True}]))
        self.put("content/sources.json", [
            {"sha256": h, "title": "T", "agency_id": "ca-two", "received_at": at, "public_url": None, "fragment": f}
            for h, at, f in ((HASH_A, "2026-11-03T08:00:00Z", "f1"), (HASH_B, "2026-11-03", "f2"),
                             (HASH_A, "2026-11-03", "f3"))])  # the same record in two fragments counts once
        self.finding("content/findings", "fnd_fix", published_at="2026-11-01T09:00:00Z",
                     corrections=[{"date": "2026-11-02", "note": CORRECTION}])
        self.finding("private/withdrawn", "fnd_gone", published_at="2026-10-01T09:00:00Z", corrections=[],
                     withdrawn={"at": "2026-11-03T12:00:00Z", "reason": REASON})
        self.put("private/reviews/fnd_fix.json", [
            {"content_sha256": HASH_A, "role": "factual", "decision": "approve", "reviewer": REVIEWER,
             "rationale": "ok", "reviewed_at": "2026-10-31T09:00:00Z", "campaign_id": "cmp_x"},
            {"content_sha256": HASH_A, "role": "legal", "decision": "changes_requested", "reviewer": REVIEWER,
             "rationale": "ok", "reviewed_at": "2026-11-02T09:00:00Z", "campaign_id": "cmp_x"}])
        self.put("content/meetings/council.json", {
            "id": "council", "body": "Synthetic Council", "starts_at": "2026-11-02T18:00:00-08:00",
            "outcome": {"result": "approved", "on": "2026-11-02", "source_url": "https://agenda.synthetic.example/m",
                        "verified_by": REVIEWER, "verified_at": "2026-11-03", "vote": "5–0"}})
        code, out, _ = self.report("2026-11-01")
        self.assertEqual(code, 0, out)
        shown = [line.split("  → ")[0] for line in out.splitlines() if line.startswith("2026-")]
        self.assertEqual(shown, [  # newest first inside each area: Requests, Records, Findings, Meetings
            "2026-11-03  Agency step recorded for Synthetic ca-two Police: produced (partial)",
            "2026-11-02  Agency step recorded for Synthetic ca-two Police: acknowledged",
            "2026-11-01  Request sent to Synthetic ca-two Police",
            f"2026-11-03  2 records added to the library: {HASH_A[:12]}, {HASH_B[:12]}",
            "2026-11-03  Finding withdrawn: Title of fnd_gone",
            "2026-11-02  Finding corrected: Title of fnd_fix",
            "2026-11-02  Finding reviewed: Title of fnd_fix (legal, changes_requested)",
            "2026-11-01  Finding published: Title of fnd_fix",
            "2026-11-03  Outcome recorded for Synthetic Council (council): approved",
            "2026-11-02  Meeting on 2026-11-02: Synthetic Council (council)"])
        for text in (HASH_B, SYNTHETIC_NOTE, REVIEWER, REASON, CORRECTION, "5–0"):
            self.assertNotIn(text, out)
        self.assertNotIn("2026-10-31", out)  # outside the window

    def test_withdrawn_finding_keeps_publication_and_correction_history(self):
        fields = {"published_at": "2026-11-01T09:00:00Z",
                  "corrections": [{"date": "2026-11-02", "note": CORRECTION}]}
        self.finding("private/withdrawn", "fnd_gone", **fields,
                     withdrawn={"at": "2026-11-03T12:00:00Z", "reason": REASON})
        expected = [("2026-11-03", "Finding withdrawn: Title of fnd_gone"),
                    ("2026-11-02", "Finding corrected: Title of fnd_gone"),
                    ("2026-11-01", "Finding published: Title of fnd_gone")]
        for leftover_public in (False, True):
            with self.subTest(leftover_public=leftover_public):
                if leftover_public:
                    self.finding("content/findings", "fnd_gone", **fields)
                before = tree(self.root)
                code, out, err = self.report("2026-11-01", "2026-11-03", as_json=True)
                self.assertEqual((code, err), (0, ""))
                self.assertEqual([(e["on"], e["what"]) for e in json.loads(out)], expected)
                _, limited, _ = self.report("2026-11-02", "2026-11-02", as_json=True)
                self.assertEqual([(e["on"], e["what"]) for e in json.loads(limited)], expected[1:2])
                for private_text in (CORRECTION, REASON):
                    self.assertNotIn(private_text, out)
                self.assertEqual(tree(self.root), before)

    # 2
    def test_window_is_inclusive_of_since_and_until(self):
        self.sent(("ca-before", "2026-10-31", []), ("ca-since", "2026-11-01", []), ("ca-until", "2026-11-03", []),
                  ("ca-after", "2026-11-04", []))
        _, out, _ = self.report("2026-11-01", "2026-11-03")
        self.assertIn("Request sent to Synthetic ca-since Police", out)
        self.assertIn("Request sent to Synthetic ca-until Police", out)
        self.assertNotIn("ca-before", out)
        self.assertNotIn("ca-after", out)
        _, out, _ = self.report("2026-11-04")  # one day, until defaults to today
        self.assertIn("ca-after", out)
        self.assertNotIn("ca-until", out)

    # 3
    def test_bad_windows_exit_2_naming_the_flag(self):
        for arguments, flag in ((("--since", "2026-11-03", "--until", "2026-11-01"), "--since"),
                                (("--since", "2026-11-01", "--until", "2026-11-05"), "--until"),  # tomorrow
                                (("--since", "2025-11-02", "--until", "2026-11-03"), "--since"),  # 367 days
                                (("--since", "2026-02-30"), "--since"), (("--since", "2026-11-01", "--until", "2026-02-30"),
                                                                       "--until"),
                                (("--since", "11/01/2026"), "--since"), (("--since", "2026-11-05"), "--since")):
            with self.subTest(arguments=arguments):
                before = tree(self.root)
                code, out, err = run("recent", "--directory", self.root, *arguments)
                self.assertEqual((code, out), (2, ""))
                self.assertIn(flag, err)
                self.assertNotIn("Traceback", err)
                self.assertEqual(tree(self.root), before)
        code, _, _ = run("recent", "--directory", self.root, "--since", "2025-11-04", "--until", "2026-11-04")
        self.assertEqual(code, 0)  # exactly 366 days is allowed
        code, _, err = run("recent", "--directory", self.root)
        self.assertEqual(code, 2)  # --since is required
        self.assertIn("--since", err)

    # 4
    def test_no_notes_addresses_reviewers_or_full_hashes(self):
        self.sent(("ca-priv", "2026-11-02", [{"on": "2026-11-03", "kind": "produced", "note": SYNTHETIC_NOTE,
                                              "records": [HASH_B], "correspondence": [HASH_C]}]))
        self.put("content/sources.json", [{"sha256": HASH_A, "title": "Secret title " + SYNTHETIC_NOTE,
                                           "agency_id": "ca-priv", "received_at": "2026-11-02", "public_url": None,
                                           "fragment": "f", "note": SYNTHETIC_NOTE}])
        self.put("private/reviews/fnd_x.json", [{"content_sha256": HASH_A, "role": "privacy", "decision": "approve",
                                                 "reviewer": REVIEWER, "rationale": SYNTHETIC_NOTE,
                                                 "reviewed_at": "2026-11-02T09:00:00Z"}])
        target = self.tmp / "report.md"
        code, out, err = self.report("2026-11-01", md=str(target))
        self.assertEqual(code, 0, err)
        written = target.read_text(encoding="utf-8")
        for text in (SYNTHETIC_NOTE, SYNTHETIC_ADDRESS, REVIEWER, HASH_A, HASH_B, HASH_C, "Secret title", "Body"):
            self.assertNotIn(text, out)
            self.assertNotIn(text, written)
        self.assertIn(HASH_A[:12], out)  # the prefix is the only trace of the record
        _, as_json, as_json_err = self.report("2026-11-01", as_json=True)
        for text in (SYNTHETIC_NOTE, SYNTHETIC_ADDRESS, REVIEWER, HASH_A, HASH_B, HASH_C):
            self.assertNotIn(text, as_json + as_json_err)

    # 5
    def test_a_draft_has_no_date_and_is_counted_not_listed(self):
        self.finding("private/drafts", "fnd_draft")
        code, out, _ = self.report("2026-01-01")
        self.assertEqual(code, 0)
        self.assertNotIn("fnd_draft", out)
        self.assertNotIn("Title of", out)
        self.assertTrue(out.endswith("\n1 undated item skipped\n"), out)

    def test_a_receipt_or_step_without_a_stored_date_is_counted_not_listed(self):
        self.put("private/reviews/fnd_x.json", [{"role": "factual", "decision": "approve"},
                                                {"reviewed_at": "not a date", "role": "legal"}])
        code, out, _ = self.report("2026-01-01")
        self.assertEqual(code, 0)
        self.assertNotIn("Finding reviewed", out)
        self.assertTrue(out.endswith("\n2 undated items skipped\n"), out)
        self.sent(("ca-one", "2026-11-02", [{"on": "bad", "kind": "produced"}]))
        _, out, _ = self.report("2026-01-01")
        self.assertIn("3 undated items skipped", out)
        self.assertNotIn("produced", out)
        _, as_json, as_json_err = self.report("2026-01-01", as_json=True)
        self.assertEqual([e["area"] for e in json.loads(as_json)], ["Requests"])
        self.assertIn("3 undated items skipped", as_json_err)  # JSON stdout stays a pure list

    def test_nothing_recorded_prints_for_every_empty_area(self):
        code, out, _ = self.report("2026-11-01")
        self.assertEqual(code, 0)
        self.assertEqual(out.count("nothing recorded"), 4)
        self.assertNotIn("undated", out)

    # 6
    def test_unreadable_files_are_named_and_the_rest_still_prints(self):
        self.put("kit/sent.json", "{not json")
        self.put("content/sources.json", "[")
        self.put("private/reviews/fnd_x.json", "{}")
        self.put("content/meetings/bad.json", "{}")
        self.put("content/meetings/good.json", {"id": "good", "body": "Synthetic Council",
                                               "starts_at": "2026-11-03T18:00:00-08:00"})
        self.finding("content/findings", "fnd_pub", published_at="2026-11-03T09:00:00Z", corrections=[])
        code, out, err = run("recent", "--directory", self.root, "--since", "2026-11-01")
        self.assertEqual(code, 3, err)
        self.assertNotIn("Traceback", err)
        for line in ("kit/sent.json unreadable", "content/sources.json unreadable",
                     "private/reviews/fnd_x.json unreadable", "content/meetings/bad.json unreadable"):
            self.assertIn(line, out)
        self.assertIn("Finding published: Title of fnd_pub", out)
        self.assertIn("Meeting on 2026-11-03: Synthetic Council (good)", out)
        self.assertNotIn("nothing recorded", out.split("Records")[0].split("Requests")[1])
        self.sent(("ca-one", "2026-11-02", []))
        self.put("kit/sent.json", self.root.joinpath("kit/sent.json").read_text().replace("email", "fax"))
        code, out, _ = run("recent", "--directory", self.root, "--since", "2026-11-01")  # malformed entry
        self.assertEqual(code, 3)
        self.assertIn("kit/sent.json unreadable", out)
        self.assertNotIn("ca-one", out)

    def test_an_unreadable_finding_is_named_by_path_only(self):
        self.put("content/findings/broken.json", '{"id": "bad id", "title": "Quoted ' + SYNTHETIC_NOTE + '"}')
        code, out, _ = self.report("2026-11-01")
        self.assertEqual(code, 3)
        self.assertIn("content/findings/broken.json unreadable", out)
        self.assertNotIn(SYNTHETIC_NOTE, out)

    # 7
    def test_md_is_refused_in_public_content_symlink_existing_or_missing_folder(self):
        (self.root / "public").mkdir()
        (self.root / "content").mkdir()
        (self.tmp / "link").symlink_to(self.root / "public")
        (self.tmp / "dangling.md").symlink_to(self.root / "public" / "new.md")
        existing = self.tmp / "existing.md"
        existing.write_text("keep me", encoding="utf-8")
        before, outside = tree(self.root), tree(self.tmp)
        for md in (self.root / "public" / "r.md", self.root / "content" / "r.md", self.root / "content" / "sub" / ".." / "r.md",
                   self.tmp / "link" / "r.md", self.tmp / "dangling.md", existing, self.tmp / "missing" / "r.md",
                   self.tmp / "link"):
            with self.subTest(md=str(md)):
                code, out, err = run("recent", "--directory", self.root, "--since", "2026-11-01", "--md", md)
                self.assertEqual((code, out), (2, ""))
                self.assertIn("--md", err)
                self.assertNotIn("Traceback", err)
        self.assertEqual(tree(self.root), before)
        self.assertEqual(tree(self.tmp), outside)
        self.assertEqual(existing.read_text(encoding="utf-8"), "keep me")

    def test_md_new_path_is_written_0600_with_the_markdown_report(self):
        self.sent(("ca-one", "2026-11-02", []))
        self.finding("content/findings", "fnd_pub", title="A *bold* [claim]", published_at="2026-11-03T09:00:00Z",
                     corrections=[])
        target = self.tmp / "report.md"
        code, out, err = run("recent", "--directory", self.root, "--since", "2026-11-01", "--md", target)
        self.assertEqual(code, 0, err)
        self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o600)
        events, undated, problems = recent.collect(self.root, "2026-11-01", None, TODAY)
        self.assertEqual(target.read_text(encoding="utf-8"),
                         recent.render(events, problems, undated, date(2026, 11, 1), TODAY, markdown=True))
        text = target.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("# Recent changes, 2026-11-01 to 2026-11-04\n"))
        self.assertIn("## Requests", text)
        self.assertIn("A \\*bold\\* \\[claim\\]", text)
        self.assertIn(f"`kit-status --directory {self.dir}`", text)
        self.assertEqual(out, recent.render(events, problems, undated, date(2026, 11, 1), TODAY))
        again, _, err = run("recent", "--directory", self.root, "--since", "2026-11-01", "--md", target)
        self.assertEqual(again, 2)  # never overwritten
        self.assertEqual(target.read_text(encoding="utf-8"), text)

    def test_md_is_written_even_when_a_file_was_unreadable(self):
        self.put("kit/sent.json", "nope")
        target = self.tmp / "report.md"
        code, _, _ = run("recent", "--directory", self.root, "--since", "2026-11-01", "--md", target)
        self.assertEqual(code, 3)
        self.assertIn("kit/sent.json unreadable", target.read_text(encoding="utf-8"))

    # 8
    def test_the_campaign_folder_is_unchanged(self):
        self.sent(("ca-one", "2026-11-02", [{"on": "2026-11-03", "kind": "acknowledged"}]))
        self.finding("private/drafts", "fnd_draft")
        self.finding("content/findings", "fnd_pub", published_at="2026-11-03T09:00:00Z", corrections=[])
        self.put("private/reviews/fnd_pub.json", [])
        self.put("content/meetings/council.json", {"id": "council", "body": "Synthetic Council",
                                                  "starts_at": "2026-11-03T18:00:00-08:00"})
        before = tree(self.root)
        for extra in ((), ("--json",)):
            code, _, _ = run("recent", "--directory", self.root, "--since", "2026-11-01", *extra)
            self.assertEqual(code, 0)
            self.assertEqual(tree(self.root), before)

    def test_not_a_campaign_folder_stops(self):
        code, out, err = run("recent", "--directory", self.tmp, "--since", "2026-11-01")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("Stopped", err)

    # RF0220-3: unsafe input directories
    def campaign(self, name):
        root = self.tmp / name
        root.mkdir(parents=True)
        (root / "campaign.json").write_text(json.dumps({"schema_version": 1, "name": "Synthetic", "county": "San Luis Obispo",
                                                         "state": "CA"}), encoding="utf-8")
        return root

    def unsafe(self, root, relative, variant):
        """Make root/relative a link (external, internal, dangling, self) or a regular file; its parent stays real."""
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if variant == "file":
            path.write_text(PAYLOAD, encoding="utf-8")
        elif variant == "self":
            path.symlink_to(path)
        elif variant == "dangling":
            path.symlink_to(self.tmp / "nowhere-synthetic")
        else:
            target = (self.tmp if variant == "external" else root) / f"{variant}-target-{relative.replace('/', '-')}"
            target.mkdir()
            path.symlink_to(target, target_is_directory=True)

    def no_readers(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        for name in ("_requests", "_records", "_findings", "_meetings"):
            stack.enter_context(mock.patch.object(recent, name, side_effect=AssertionError(f"{name} was called")))
        stack.enter_context(mock.patch.object(recent.findings, "status_rows", side_effect=AssertionError("status_rows")))

    @staticmethod
    def refusal(relative):
        return f"recent: {relative}/ must be a real directory, not a symlink or file"

    def test_input_directory_symlinks_refused_before_any_reader(self):
        self.no_readers()
        count = 0
        for relative in recent.INPUT_DIRECTORIES:
            for variant in ("external", "internal", "dangling", "self"):
                count += 1
                with self.subTest(relative=relative, variant=variant):
                    root = self.campaign(f"link-{count}")
                    self.unsafe(root, relative, variant)
                    with self.assertRaises(ValueError) as caught:
                        recent.collect(root, "2026-11-01", None, TODAY)
                    self.assertEqual(str(caught.exception), self.refusal(relative))
                    self.assertNotIn("Traceback", str(caught.exception))
                    code, out, err = run("recent", "--directory", root, "--since", "2026-11-01")
                    self.assertEqual((code, out, err), (1, "", f"Stopped: {self.refusal(relative)}\n"))
        self.assertEqual(count, 32)

    def test_input_guard_checks_parents_first_and_never_looks_below_a_refused_parent(self):
        root = self.campaign("order")
        self.unsafe(root, "content", "external")
        (self.tmp / "external-target-content" / "findings").mkdir()  # a real folder behind the link: never inspected
        self.unsafe(root, "private/reviews", "external")
        seen = []
        real_lstat = os.lstat

        def spy(path, *args, **kwargs):
            seen.append(os.fspath(path))
            return real_lstat(path, *args, **kwargs)
        self.no_readers()
        with mock.patch.object(os, "lstat", spy), self.assertRaises(ValueError) as caught:
            recent.collect(root, "2026-11-01", None, TODAY)
        self.assertEqual(str(caught.exception), self.refusal("content"))  # allowlist order: content before private/reviews
        self.assertEqual([p for p in seen if p.startswith(str(root))], [str(root / "kit"), str(root / "content")])
        both = self.campaign("both")
        self.unsafe(both, "private/reviews", "internal")
        self.unsafe(both, "kit", "dangling")
        with self.assertRaises(ValueError) as caught:
            recent.collect(both, "2026-11-01", None, TODAY)
        self.assertEqual(str(caught.exception), self.refusal("kit"))

    def test_external_kit_and_content_events_never_enter_recent(self):
        external = self.tmp / "external-history"
        external.mkdir()
        (external / "agencies.json").write_text(json.dumps([{"agency_id": "ca-sentinel", "name": "Sentinel Police",
                                                             "records_email": SYNTHETIC_ADDRESS}]), encoding="utf-8")
        (external / "sent.json").write_text(json.dumps({"schema_version": 1, "requests": {"ca-sentinel": {
            "sent_on": "2026-11-02", "channel": "email", "draft": "requests/x.md", "draft_sha256": HASH_C,
            "recorded_at": "2026-11-02T10:00:00+00:00", "steps": []}}}), encoding="utf-8")
        (external / "sources.json").write_text(json.dumps([{"sha256": HASH_B, "title": "T", "agency_id": "ca-sentinel",
                                                            "received_at": "2026-11-02T08:00:00Z", "public_url": None,
                                                            "fragment": "f"}]), encoding="utf-8")
        for relative, expected_area in (("kit", "kit"), ("content", "content")):
            for md in (None, self.tmp / f"{relative}.md"):
                with self.subTest(relative=relative, md=md):
                    root = self.campaign(f"external-{relative}-{md is not None}")
                    (root / relative).symlink_to(external, target_is_directory=True)
                    extra = () if md is None else ("--md", md)
                    code, out, err = run("recent", "--directory", root, "--since", "2026-11-01", *extra)
                    self.assertEqual((code, out), (1, ""))
                    self.assertEqual(err, f"Stopped: {self.refusal(expected_area)}\n")
                    for private in ("ca-sentinel", "Sentinel", HASH_B[:12], str(external), "external-history", "Traceback"):
                        self.assertNotIn(private, out + err)
                    if md is not None:
                        self.assertFalse(os.path.lexists(md))

    def test_linked_empty_collections_are_not_successful_empty_reports(self):
        for relative in ("private/reviews", "content/meetings", "content/findings", "private/drafts", "private/withdrawn"):
            for variant in ("external", "internal"):  # the targets are empty folders with no JSON files at all
                with self.subTest(relative=relative, variant=variant):
                    root = self.campaign(f"empty-{relative.replace('/', '-')}-{variant}")
                    self.unsafe(root, relative, variant)
                    for extra in ((), ("--json",)):
                        code, out, err = run("recent", "--directory", root, "--since", "2026-11-01", *extra)
                        self.assertEqual((code, out, err), (1, "", f"Stopped: {self.refusal(relative)}\n"))
                        self.assertNotIn("nothing recorded", out + err)
                        self.assertNotEqual(out, "[]\n")

    def test_input_directory_regular_files_and_fifos_refused_without_open(self):
        self.no_readers()
        real_open, real_os_open = open, os.open

        def watch(real):
            def inner(path, *args, **kwargs):
                if str(path).startswith(str(self.tmp)) and not str(path).endswith("campaign.json"):
                    raise AssertionError(f"opened {path}")
                return real(path, *args, **kwargs)
            return inner
        for index, relative in enumerate(recent.INPUT_DIRECTORIES):
            variants = ["file"] + (["fifo"] if hasattr(os, "mkfifo") else [])
            for variant in variants:
                with self.subTest(relative=relative, variant=variant):
                    root = self.campaign(f"nondir-{index}-{variant}")
                    if variant == "file":
                        self.unsafe(root, relative, "file")
                    else:
                        (root / relative).parent.mkdir(parents=True, exist_ok=True)
                        os.mkfifo(root / relative)
                    with mock.patch("builtins.open", watch(real_open)), mock.patch.object(os, "open", watch(real_os_open)):
                        with self.assertRaises(ValueError) as caught:
                            recent.collect(root, "2026-11-01", None, TODAY)
                    self.assertEqual(str(caught.exception), self.refusal(relative))
                    code, out, err = run("recent", "--directory", root, "--since", "2026-11-01")
                    self.assertEqual((code, out, err), (1, "", f"Stopped: {self.refusal(relative)}\n"))
                    self.assertNotIn(PAYLOAD, out + err)

    def test_input_directory_inspection_error_is_fixed_and_private(self):
        self.no_readers()
        secret = "/private/synthetic-secret-path denied"
        real_lstat = os.lstat
        for error in (PermissionError(13, secret), OSError(5, secret)):
            with self.subTest(error=type(error).__name__):
                root = self.campaign(f"inspect-{type(error).__name__}")
                (root / "private").mkdir()

                def lstat(path, *args, **kwargs):
                    if os.fspath(path) == str(root / "private" / "reviews"):
                        raise error
                    return real_lstat(path, *args, **kwargs)
                with mock.patch.object(os, "lstat", lstat):
                    with self.assertRaises(ValueError) as caught:
                        recent.collect(root, "2026-11-01", None, TODAY)
                    self.assertEqual(str(caught.exception), "recent: private/reviews/ cannot be inspected")
                    self.assertIsNone(caught.exception.__cause__)
                    self.assertTrue(caught.exception.__suppress_context__)
                    code, out, err = run("recent", "--directory", root, "--since", "2026-11-01")
                self.assertEqual((code, out, err), (1, "", "Stopped: recent: private/reviews/ cannot be inspected\n"))
                self.assertNotIn("synthetic-secret", err)

    def test_input_refusal_does_not_create_md_or_change_any_tree(self):
        self.sent(("ca-one", "2026-11-02", []))
        self.finding("content/findings", "fnd_pub", published_at="2026-11-03T09:00:00Z", corrections=[])
        self.put("private/drafts/fnd_draft.json", {"id": "fnd_draft", "title": "Draft", "slug": "fnd_draft"})
        external = self.tmp / "external-reviews"
        external.mkdir()
        (external / "fnd_pub.json").write_text("[]", encoding="utf-8")
        (self.root / "private" / "reviews").symlink_to(external, target_is_directory=True)
        out_dir = self.tmp / "out"
        out_dir.mkdir()
        before, outside = tree(self.root), tree(self.tmp / "external-reviews")
        beside = tree(out_dir)
        for extra in ((), ("--json",)):
            target = out_dir / "report.md"
            code, out, err = run("recent", "--directory", self.root, "--since", "2026-11-01", "--md", target, *extra)
            self.assertEqual((code, out, err), (1, "", f"Stopped: {self.refusal('private/reviews')}\n"))
            self.assertFalse(os.path.lexists(target))
            self.assertEqual(tree(out_dir), beside)
            self.assertEqual(tree(self.root), before)
            self.assertEqual(tree(external), outside)
        for call in (lambda: recent.collect(self.root, "2026-11-01", None, TODAY),
                     lambda: recent.report(self.root, "2026-11-01", None, TODAY),
                     lambda: self.report("2026-11-01", md=str(out_dir / "report.md"))):
            with self.assertRaises(ValueError) as caught:
                call()
            self.assertEqual(str(caught.exception), self.refusal("private/reviews"))
        self.assertEqual(tree(out_dir), beside)

    def populate(self, root):
        self.root, saved = root, self.root
        try:
            self.sent(("ca-one", "2026-11-02", []))
            self.finding("content/findings", "fnd_pub", published_at="2026-11-03T09:00:00Z", corrections=[])
        finally:
            self.root = saved

    def test_missing_input_directories_and_linked_campaign_root_still_work(self):
        bare = self.campaign("bare")  # every allowlisted folder absent
        code, out, err = run("recent", "--directory", bare, "--since", "2026-11-01")
        self.assertEqual((code, err, out.count("nothing recorded")), (0, "", 4))
        sparse = self.campaign("sparse")  # some real, empty folders
        for relative in ("kit", "content/meetings", "private/withdrawn"):
            (sparse / relative).mkdir(parents=True)
        code, out, err = run("recent", "--directory", sparse, "--since", "2026-11-01")
        self.assertEqual((code, err, out.count("nothing recorded")), (0, "", 4))
        real = self.campaign("Real Campaign ünï")  # spaces and Unicode
        self.populate(real)
        code, direct, err = run("recent", "--directory", real, "--since", "2026-11-01")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("Request sent to Synthetic ca-one Police", direct)
        self.assertIn(f"kit-status --directory {shlex.quote(str(real))}", direct)
        alias = self.tmp / "alias root"
        alias.symlink_to(real, target_is_directory=True)
        code, aliased, err = run("recent", "--directory", alias, "--since", "2026-11-01")
        self.assertEqual((code, err, aliased), (0, "", direct))  # the CLI resolves --directory itself (unchanged)
        events = recent.collect(alias, "2026-11-01", None, TODAY)[0]  # a direct call keeps the alias spelling
        self.assertEqual([e["what"] for e in events], [e["what"] for e in recent.collect(real, "2026-11-01", None, TODAY)[0]])
        self.assertTrue(events and all(shlex.quote(str(alias)) in e["command"] for e in events if "--directory" in e["command"]))
        parent = self.tmp / "ancestor-link"
        parent.symlink_to(self.tmp, target_is_directory=True)
        events = recent.collect(parent / "Real Campaign ünï", "2026-11-01", None, TODAY)[0]
        self.assertTrue(any(shlex.quote(str(parent / "Real Campaign ünï")) in e["command"] for e in events))
        code, via_ancestor, err = run("recent", "--directory", parent / "Real Campaign ünï", "--since", "2026-11-01")
        self.assertEqual((code, err, via_ancestor), (0, "", direct))

    def test_input_guard_precedence_and_existing_partial_report_contract(self):
        self.unsafe(self.root, "kit", "external")
        existing = self.tmp / "existing.md"
        existing.write_text("keep me", encoding="utf-8")
        for arguments in (("--since", "2026-11-09"), ("--since", "11/01/2026"),
                          ("--since", "2026-11-01", "--md", existing),
                          ("--since", "2026-11-01", "--md", self.root / "public" / "r.md"),
                          ("--since", "2026-11-01", "--md", self.tmp / "missing" / "r.md")):
            with self.subTest(arguments=arguments):
                code, out, err = run("recent", "--directory", self.root, *arguments)
                self.assertEqual((code, out), (2, ""))
                self.assertNotIn("must be a real directory", err)
        self.assertEqual(existing.read_text(encoding="utf-8"), "keep me")
        safe = self.campaign("partial")
        self.put_in(safe, "kit/sent.json", "{not json")
        self.put_in(safe, "private/reviews/fnd_x.json", "{}")
        target = self.tmp / "partial.md"
        code, out, err = run("recent", "--directory", safe, "--since", "2026-11-01", "--md", target)
        self.assertEqual(code, 3, err)
        self.assertIn("kit/sent.json unreadable", out)
        self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o600)
        self.assertIn("private/reviews/fnd\\_x.json unreadable", target.read_text(encoding="utf-8"))

    def put_in(self, root, relative, document):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(document, encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
