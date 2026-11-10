"""DF1109B-05: ``content agency-action list`` and ``content coverage list`` (read only).

They print the selectors the removal commands take: an agency action's 1-based position among that agency's actions in
the file, and a coverage entry's exact stored URL. Both read through the build's own loaders, print ``none recorded``
(``[]`` with --json) without a file, refuse a broken store or broken references with one fixed line (exit 2) and write
nothing. Synthetic data only; nothing is fetched or sent.

Run from the repository root: python3 -B -m unittest tests.test_content_lists -q
"""
import contextlib
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
import urllib.request
from datetime import date
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import cli  # noqa: E402
from campaign_tool.agency_actions import ACTION_KINDS, ActionError, list_actions  # noqa: E402
from campaign_tool.content import ContentError  # noqa: E402
from campaign_tool.coverage import list_coverage  # noqa: E402
from campaign_tool.site import build_site  # noqa: E402

EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
POLICE, SHERIFF, COUNCIL = "ca-cedar-police", "ca-cedar-county-sheriff", "ca-cedar-city-council"
FINDING = "fnd_0000000000000001"
TODAY = date(2026, 11, 1)
ACTION_REFUSAL = "Stopped: content/agency-actions.json: cannot list; fix the file and its agency references.\n"
COVERAGE_REFUSAL = "Stopped: content/coverage.json: cannot list; fix the file and its published finding references.\n"
SELECTOR_REFUSAL = "Stopped: --agency: unknown agency\n"
SECRET = "ZQ-SECRET-SENTINEL-71"
NO_NETWORK = AssertionError("no network in these tests")


def action(agency, on, kind="policy_adopted", text="Council adopted the policy", url="https://example.invalid/a",
           checked="2026-10-01"):
    return {"agency_id": agency, "on": on, "kind": kind, "as_stated": text, "source_url": url, "checked_on": checked}


def story(url, published="2026-10-01", outlet="Example News", headline="Published headline", **extra):
    return {"outlet": outlet, "headline": headline, "url": url, "published_on": published, **extra}


# B1, A1, B2: the sheriff appears first; its second row is dated before its first.
B1 = action(SHERIFF, "2026-09-20", "sharing_ended", "Sharing with outside agencies ended", "https://example.invalid/s1")
A1 = action(POLICE, "2026-09-10", "policy_posted", "Use policy posted | on the website", "https://example.invalid/p1")
B2 = action(SHERIFF, "2026-08-01", "contract_signed", "Contract signed for 24 months", "https://example.invalid/s2")
LINES = {"B1": f"{SHERIFF} | 1 | 2026-09-20 | Sharing ended | Sharing with outside agencies ended | "
               "https://example.invalid/s1 | checked 2026-10-01\n",
         "B2": f"{SHERIFF} | 2 | 2026-08-01 | Contract signed | Contract signed for 24 months | "
               "https://example.invalid/s2 | checked 2026-10-01\n",
         "A1": f"{POLICE} | 1 | 2026-09-10 | Policy posted | Use policy posted | on the website | "
               "https://example.invalid/p1 | checked 2026-10-01\n"}


def flat(row, index, label):
    return {"agency_id": row["agency_id"], "index": index, "on": row["on"], "kind": row["kind"], "kind_label": label,
            "as_stated": row["as_stated"], "source_url": row["source_url"], "checked_on": row["checked_on"]}


class Base(unittest.TestCase):
    def setUp(self):
        for target, name in ((socket, "socket"), (socket, "create_connection"), (socket, "getaddrinfo"),
                             (urllib.request, "urlopen")):
            patch = mock.patch.object(target, name, side_effect=NO_NETWORK)
            patch.start()
            self.addCleanup(patch.stop)
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-content-lists-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name).resolve()
        self.root = self.tmp / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.content = self.root / "content"
        self.actions = self.content / "agency-actions.json"
        self.coverage = self.content / "coverage.json"

    def put_actions(self, *rows):
        self.actions.write_text(json.dumps({"schema_version": 1, "actions": list(rows)}), encoding="utf-8")

    def put_coverage(self, *entries):
        self.coverage.write_text(json.dumps({"schema_version": 1, "entries": list(entries)}, ensure_ascii=False),
                                 encoding="utf-8")

    def run_cli(self, *arguments):
        """The real ``cli.main`` in process; (exit code, stdout, stderr)."""
        out, err, code = io.StringIO(), io.StringIO(), 0
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch.object(sys, "argv", ["campaign_tool", *map(str, arguments)]):
            try:
                cli.main()
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
        return code, out.getvalue(), err.getvalue()

    def actions_list(self, *extra, root=None):
        return self.run_cli("content", "agency-action", "list", "--directory", root or self.root, *extra)

    def coverage_list(self, *extra, root=None):
        return self.run_cli("content", "coverage", "list", "--directory", root or self.root, *extra)

    def assert_refused(self, result, line):
        self.assertEqual(result, (2, "", line))

    def snapshot(self, base):
        """{relative path: (kind, bytes or link target, mode)} for everything under ``base`` (access times ignored)."""
        found = {}
        if not os.path.lexists(base):
            return found
        for folder, names, files in os.walk(base):
            for name in names + files:
                path = Path(folder) / name
                info = os.lstat(path)
                if path.is_symlink():
                    found[str(path.relative_to(base))] = ("link", os.readlink(path), info.st_mode)
                elif path.is_dir():
                    found[str(path.relative_to(base))] = ("dir", None, info.st_mode)
                else:
                    found[str(path.relative_to(base))] = ("file", path.read_bytes(), info.st_mode)
        return found


class ActionListTests(Base):
    def test_action_indices_are_per_agency_stored_order(self):
        self.put_actions(B1, A1, B2)
        self.assertEqual(self.actions_list(), (0, LINES["B1"] + LINES["B2"] + LINES["A1"], ""))
        self.assertEqual(self.actions_list("--agency", SHERIFF), (0, LINES["B1"] + LINES["B2"], ""))
        self.assertEqual(self.actions_list("--agency", POLICE), (0, LINES["A1"], ""))
        code, out, err = self.actions_list("--json")
        self.assertEqual((code, err), (0, ""))
        self.assertTrue(out.endswith("]\n") and not out.endswith("\n\n"))
        self.assertEqual(json.loads(out), [flat(B1, 1, "Sharing ended"), flat(B2, 2, "Contract signed"),
                                           flat(A1, 1, "Policy posted")])
        self.assertEqual(json.loads(self.actions_list("--agency", SHERIFF, "--json")[1]),
                         [flat(B1, 1, "Sharing ended"), flat(B2, 2, "Contract signed")])
        labels = ["Policy adopted", "Policy amended", "Policy posted", "Sharing started", "Sharing ended",
                  "Retention changed", "Contract signed", "Contract renewed", "Contract ended", "Contract paused",
                  "Cameras added", "Cameras removed", "Other"]
        self.put_actions(*[action(POLICE, "2026-09-01", kind, url=f"https://example.invalid/k{n}")
                           for n, kind in enumerate(ACTION_KINDS)])
        listed = list_actions(self.root, today=TODAY)
        self.assertEqual([(row["index"], row["kind"], row["kind_label"]) for row in listed],
                         list(zip(range(1, 14), ACTION_KINDS, labels)))

    def test_list_selector_removes_the_intended_action(self):
        self.put_actions(B1, A1, B2)
        listed = json.loads(self.actions_list("--agency", SHERIFF, "--json")[1])
        first = listed[0]
        removed = self.run_cli("content", "agency-action", "remove", "--directory", self.root, "--agency",
                               first["agency_id"], "--index", first["index"])
        self.assertEqual(removed, (0, f"Agency action removed: {SHERIFF}, index 1.\n"
                                      "Content only; nothing was built, sent or deployed.\n", ""))
        self.assertEqual(json.loads(self.actions.read_text(encoding="utf-8"))["actions"], [A1, B2])
        # the remaining sheriff row is renumbered from 2 to 1 (and now appears after the police row); police is untouched
        self.assertEqual(self.actions_list(), (0, LINES["A1"] + LINES["B2"].replace(" | 2 | ", " | 1 | "), ""))
        # a refused remove keeps its own exit code and message
        self.assertEqual(self.run_cli("content", "agency-action", "remove", "--directory", self.root, "--agency", SHERIFF,
                                      "--index", "2")[0], 1)

    def test_missing_empty_and_known_agency_without_actions(self):
        for root in (self.root, self.tmp / "no-such-campaign", self.tmp / "bare"):
            if root.name == "bare":
                root.mkdir()
            before = self.snapshot(self.tmp)
            self.assertEqual(self.actions_list(root=root), (0, "none recorded\n", ""), root)
            self.assertEqual(self.actions_list("--json", root=root), (0, "[]\n", ""), root)
            self.assertEqual(self.coverage_list(root=root), (0, "none recorded\n", ""), root)
            self.assertEqual(self.coverage_list("--json", root=root), (0, "[]\n", ""), root)
            self.assertEqual(self.snapshot(self.tmp), before)
        self.assertFalse((self.tmp / "no-such-campaign").exists())
        self.assertFalse((self.tmp / "bare" / "content").exists())
        self.assertEqual(self.actions_list("--agency", COUNCIL), (0, "none recorded\n", ""))  # known, no file
        self.put_actions()
        self.put_coverage()
        self.assertEqual(self.actions_list(), (0, "none recorded\n", ""))
        self.assertEqual(self.coverage_list("--json"), (0, "[]\n", ""))
        self.put_actions(B1, A1)
        self.assertEqual(self.actions_list("--agency", COUNCIL), (0, "none recorded\n", ""))
        self.assertEqual(self.actions_list("--agency", COUNCIL, "--json"), (0, "[]\n", ""))
        self.actions.write_text("null", encoding="utf-8")
        self.coverage.write_text("null", encoding="utf-8")
        self.assert_refused(self.actions_list(), ACTION_REFUSAL)
        self.assert_refused(self.coverage_list(), COVERAGE_REFUSAL)

    def test_bad_or_unknown_selector_is_one_line_exit_two(self):
        self.put_actions(B1, A1)
        for selector in ("ca-unknown-pd", "../../" + SECRET, f"x; rm -rf / {SECRET}", "", SHERIFF.upper()):
            for extra in ((), ("--json",)):
                with self.subTest(selector=selector, extra=extra):
                    self.assert_refused(self.actions_list("--agency", selector, *extra), SELECTOR_REFUSAL)
        # the kit's selected agencies are the fallback without content/agencies.json, as for build and remove
        (self.root / "kit").mkdir()
        os.replace(self.content / "agencies.json", self.root / "kit" / "agencies.json")
        self.assertEqual(self.actions_list("--agency", POLICE), (0, LINES["A1"], ""))
        # argparse keeps its own usage refusals; no writer runs
        before = self.actions.read_bytes()
        for argv in (("content", "agency-action", "list"), ("content", "coverage", "list"),
                     ("content", "agency-action", "list", "--directory", self.root, "--index", "1"),
                     ("content", "coverage", "list", "--directory", self.root, "--url", "https://example.invalid/a")):
            code, out, err = self.run_cli(*argv)
            self.assertEqual((code, out), (2, ""), argv)
            self.assertIn("usage:", err)
        self.assertEqual(self.actions.read_bytes(), before)

    def test_whole_store_validation_precedes_action_filter(self):
        bad = {"date": action(SHERIFF, "2026-02-30"), "kind": action(SHERIFF, "2026-09-01", kind="banned"),
               "text": action(SHERIFF, "2026-09-01", text=f"<b>{SECRET}</b>"),
               "agency": action("ca-unknown-pd", "2026-09-01"), "duplicate": A1,
               "future": action(SHERIFF, "2026-09-01", checked="2999-01-01")}
        for name, row in bad.items():
            self.put_actions(A1, row)
            for extra in ((), ("--json",)):
                with self.subTest(name=name, extra=extra):
                    self.assert_refused(self.actions_list("--agency", POLICE, *extra), ACTION_REFUSAL)
        fifty = [action(POLICE, "2026-09-01", url=f"https://example.invalid/n{n}") for n in range(50)]
        self.put_actions(*fifty)
        self.assertEqual(list_actions(self.root, today=TODAY)[-1]["index"], 50)
        self.put_actions(*fifty, action(POLICE, "2026-09-01", url="https://example.invalid/n50"))
        self.assert_refused(self.actions_list(), ACTION_REFUSAL)
        # the build's date rules with an injected today: on <= checked_on <= today
        self.put_actions(action(POLICE, "2026-11-01", checked="2026-11-01"))
        self.assertEqual(len(list_actions(self.root, today=TODAY)), 1)
        with self.assertRaises(ActionError):
            list_actions(self.root, today=date(2026, 10, 31))
        self.put_actions(action(POLICE, "2026-10-02", checked="2026-10-01"))
        with self.assertRaises(ActionError):
            list_actions(self.root, today=TODAY)
        # broken agency inputs refuse rather than claim an empty healthy store
        self.actions.unlink()
        (self.content / "agencies.json").write_text("[{", encoding="utf-8")
        self.assert_refused(self.actions_list(), ACTION_REFUSAL)


class CoverageListTests(Base):
    def test_coverage_keeps_file_order_and_exact_url(self):
        long = "H" * 200
        entries = [story("https://news.example.test/story?id=1", "2026-10-05", "Café & Co's Daily", long,
                         findings=[FINDING], kind="opinion"),
                   story("https://news.example.test/story?id=1#top", "2026-09-01", "Ünïcode Times", "It's **not** Markdown"),
                   story("https://news.example.test/Story?id=1", "2026-10-05")]
        self.put_coverage(*entries)
        expected = ("2026-10-05 | Café & Co's Daily | " + long + " | https://news.example.test/story?id=1\n"
                    "2026-09-01 | Ünïcode Times | It's **not** Markdown | https://news.example.test/story?id=1#top\n"
                    "2026-10-05 | Example News | Published headline | https://news.example.test/Story?id=1\n")
        self.assertEqual(self.coverage_list(), (0, expected, ""))
        code, out, err = self.coverage_list("--json")
        self.assertEqual((code, err), (0, ""))
        listed = json.loads(out)
        self.assertEqual(listed, [
            {"published_on": "2026-10-05", "outlet": "Café & Co's Daily", "headline": long,
             "url": "https://news.example.test/story?id=1", "kind": "opinion", "findings": [FINDING]},
            {"published_on": "2026-09-01", "outlet": "Ünïcode Times", "headline": "It's **not** Markdown",
             "url": "https://news.example.test/story?id=1#top", "kind": "news", "findings": []},
            {"published_on": "2026-10-05", "outlet": "Example News", "headline": "Published headline",
             "url": "https://news.example.test/Story?id=1", "kind": "news", "findings": []}])
        self.assertIn("Café", out)  # not ASCII-escaped
        removed = self.run_cli("content", "coverage", "remove", "--directory", self.root, "--url", listed[1]["url"])
        self.assertEqual(removed, (0, json.dumps({"action": "removed", "url": listed[1]["url"]})
                                   + "\nNothing was built, published or sent.\n", ""))
        self.assertEqual([entry["url"] for entry in json.loads(self.coverage.read_text(encoding="utf-8"))["entries"]],
                         [listed[0]["url"], listed[2]["url"]])

    def test_coverage_reference_validation_matches_build(self):
        cited = story("https://news.example.test/a", findings=[FINDING])
        self.put_coverage(cited, story("https://news.example.test/b"))
        private = self.root / "private" / "findings"
        private.mkdir(parents=True)
        (private / "draft.json").write_text("{" + SECRET, encoding="utf-8")  # drafts are never opened
        (self.content / "meetings" / "2026-10-cedar-council.json").write_text("{", encoding="utf-8")
        (self.content / "map" / "cameras.geojson").write_text("{", encoding="utf-8")
        self.assertEqual(len(list_coverage(self.root, today=TODAY)), 2)  # unrelated broken stores do not block
        self.put_actions(A1)
        self.assertEqual(self.actions_list(), (0, LINES["A1"], ""))
        for name, entry in (("unknown", story("https://news.example.test/c", findings=["fnd_9999999999999999"])),
                            ("draft id", story("https://news.example.test/c", findings=["draft"]))):
            with self.subTest(name=name):
                self.put_coverage(cited, entry)
                self.assert_refused(self.coverage_list(), COVERAGE_REFUSAL)
                self.assert_refused(self.coverage_list("--json"), COVERAGE_REFUSAL)
        finding = self.content / "findings" / "cedar-policy-posted.json"
        original = finding.read_bytes()
        finding.write_text("{" + SECRET, encoding="utf-8")  # a malformed cited public finding
        self.put_coverage(cited)
        self.assert_refused(self.coverage_list(), COVERAGE_REFUSAL)
        finding.unlink()  # withdrawn: the reference is stale; listing refuses but the legacy repair still removes it
        self.assert_refused(self.coverage_list(), COVERAGE_REFUSAL)
        self.assertEqual(self.run_cli("content", "coverage", "remove", "--directory", self.root, "--url", cited["url"])[0], 0)
        self.assertEqual(self.coverage_list(), (0, "none recorded\n", ""))
        finding.write_bytes(original)


class RefusalAndReadOnlyTests(Base):
    def broken_cases(self, target, valid):
        big = json.dumps({"pad": "x" * (2 * 1024 * 1024 + 10)})
        return {"invalid utf-8": lambda: target.write_bytes(b"\xff\xfe" + SECRET.encode()),
                "invalid json": lambda: target.write_text("{" + SECRET, encoding="utf-8"),
                "empty bytes": lambda: target.write_bytes(b""),
                "null": lambda: target.write_text("null", encoding="utf-8"),
                "wrong envelope": lambda: target.write_text(json.dumps([SECRET]), encoding="utf-8"),
                "unknown field": lambda: target.write_text(json.dumps({**valid, SECRET: 1}), encoding="utf-8"),
                "oversize": lambda: target.write_text(big, encoding="utf-8"),
                "directory": lambda: target.mkdir(),
                "symlink": lambda: (self.tmp.joinpath("real.json").write_text(json.dumps(valid), encoding="utf-8"),
                                    target.symlink_to(self.tmp / "real.json")),
                "dangling link": lambda: target.symlink_to(self.tmp / "missing.json")}

    def reset(self, target):
        if target.is_symlink() or target.is_file():
            target.unlink()
        elif target.is_dir():
            target.rmdir()

    def test_broken_store_refusals_are_atomic_single_line(self):
        stores = ((self.actions, {"schema_version": 1, "actions": []}, self.actions_list, ACTION_REFUSAL),
                  (self.coverage, {"schema_version": 1, "entries": []}, self.coverage_list, COVERAGE_REFUSAL))
        for target, valid, listing, refusal in stores:
            for name, make in self.broken_cases(target, valid).items():
                self.reset(target)
                make()
                for extra in ((), ("--json",)):
                    with self.subTest(store=target.name, case=name, extra=extra):
                        result = listing(*extra)
                        self.assert_refused(result, refusal)
                        self.assertNotIn(SECRET, result[2])
            self.reset(target)
        self.actions.write_text('{"schema_version": 1, "actions": [], "actions": []}', encoding="utf-8")
        self.assert_refused(self.actions_list(), ACTION_REFUSAL)  # the action reader's duplicate-key rule
        self.put_actions(A1)
        os.link(self.actions, self.tmp / "second-link.json")
        self.assert_refused(self.actions_list(), ACTION_REFUSAL)  # hard links are refused as today
        (self.tmp / "second-link.json").unlink()
        self.put_coverage(story("https://news.example.test/a"))
        for error in (PermissionError(13, "denied", SECRET), RecursionError("deep")):
            with self.subTest(error=type(error).__name__):
                with mock.patch("campaign_tool.agency_actions._load", side_effect=error):
                    self.assert_refused(self.actions_list(), ACTION_REFUSAL)
                with mock.patch("campaign_tool.coverage.read_coverage_document", side_effect=error):
                    self.assert_refused(self.coverage_list("--json"), COVERAGE_REFUSAL)

    def test_all_listing_paths_are_read_only(self):
        sentinel = self.root / "private" / "sentinel.txt"
        sentinel.parent.mkdir()
        sentinel.write_text(SECRET, encoding="utf-8")
        refuse = AssertionError("a list command tried to write or build")
        spies = [mock.patch("tempfile.mkstemp", side_effect=refuse), mock.patch("os.replace", side_effect=refuse),
                 mock.patch("campaign_tool.site.build_site", side_effect=refuse),
                 mock.patch("campaign_tool.agency_actions._install", side_effect=refuse),
                 mock.patch("campaign_tool.coverage._write", side_effect=refuse)]
        states = {"absent": lambda: None,
                  "valid": lambda: (self.put_actions(B1, A1), self.put_coverage(story("https://news.example.test/a"))),
                  "empty": lambda: (self.put_actions(), self.put_coverage()),
                  "broken": lambda: (self.actions.write_text("{", encoding="utf-8"),
                                     self.coverage.write_text("{", encoding="utf-8"))}
        for name, make in states.items():
            make()
            before = self.snapshot(self.tmp)
            with contextlib.ExitStack() as stack:
                for spy in spies:
                    stack.enter_context(spy)
                for extra in ((), ("--json",), ("--agency", POLICE)):
                    self.actions_list(*extra)
                self.coverage_list()
                self.coverage_list("--json")
            self.assertEqual(self.snapshot(self.tmp), before, name)
        self.put_actions(B1, A1)
        self.put_coverage(story("https://news.example.test/a"))
        before = self.snapshot(self.tmp)
        for command in ("agency-action", "coverage"):
            result = subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "content", command, "list", "--directory",
                                     str(self.root)], cwd=REPO_ROOT, capture_output=True, text=True, timeout=120)
            self.assertEqual((result.returncode, result.stderr), (0, ""), command)
            self.assertEqual(len(result.stdout.splitlines()), 2 if command == "agency-action" else 1)
        self.assertEqual(self.snapshot(self.tmp), before)

    def test_legacy_writer_and_build_output_byte_identical(self):
        self.put_actions(B1, A1)
        self.put_coverage(story("https://news.example.test/a", findings=[FINDING]))
        build_site(self.root, today=TODAY)
        first = self.snapshot(self.root / "public")
        stores = (self.actions.read_bytes(), self.coverage.read_bytes())
        for extra in ((), ("--json",)):
            self.assertEqual(self.actions_list(*extra)[0], 0)
            self.assertEqual(self.coverage_list(*extra)[0], 0)
        self.assertEqual((self.actions.read_bytes(), self.coverage.read_bytes()), stores)
        build_site(self.root, today=TODAY)
        self.assertEqual(self.snapshot(self.root / "public"), first)
        url = "https://news.example.test/b"
        added = self.run_cli("content", "coverage", "add", "--directory", self.root, "--outlet", "Example News",
                             "--headline", "Another", "--url", url, "--published-on", "2026-10-02")
        self.assertEqual(added, (0, json.dumps({"action": "added", "url": url})
                                 + "\nNothing was built, published or sent.\n", ""))
        self.assertEqual(self.run_cli("content", "coverage", "remove", "--directory", self.root, "--url", url + "x"),
                         (2, "", "Stopped: coverage.json: URL not found\n"))


class DocTests(unittest.TestCase):
    def test_site_content_lists_before_remove(self):
        text = (REPO_ROOT / "docs" / "SITE-CONTENT.md").read_text(encoding="utf-8")
        actions = text.split("### Agency actions outside a meeting", 1)[1].split("\n### ", 1)[0]
        news = text.split("### In the news (`content/coverage.json`, optional)", 1)[1].split("\n### ", 1)[0]
        for section, command in ((actions, "agency-action"), (news, "coverage")):
            self.assertLess(section.index(f"content {command} list"), section.index(f"content {command} remove"))
            for phrase in ("none recorded", "exit 2", "--json", "writes nothing"):
                self.assertIn(phrase, section, (command, phrase))
        self.assertIn("exact stored URL", news)
        self.assertIn("not verification", news)


if __name__ == "__main__":
    unittest.main()
