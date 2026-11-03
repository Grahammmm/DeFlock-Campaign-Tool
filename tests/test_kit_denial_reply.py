"""kit-denial-reply writes a private, never-sent reply draft after a recorded denial. Offline, synthetic."""
import contextlib
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import cli, kit_track, law  # noqa: E402
from tests.test_kit_check import ready_drafts  # noqa: E402

DONE = "Draft only. Nothing was sent.\n"
ASKS = ("1. Tell us which exemption or other legal basis the agency relied on",
        "2. Send the parts of the requested records that are not exempt",
        "3. Explain the denial in writing.", "[EDIT ME: what you asked for]", "\n[name]\n",
        "Draft only. Not legal advice.", "DRAFT — NOT SENT.")
SHIPPED = "ca-gov-7922.535-determination-and-extension-2026"  # in force from 2026-01-01 in the shipped package


def run(*arguments):
    """Run the CLI in-process; return (code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with mock.patch.object(sys, "argv", ["campaign_tool", *map(str, arguments)]), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            cli.main()
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


def snapshot(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in Path(root).rglob("*") if p.is_file()}


class KitDenialReplyTests(unittest.TestCase):
    def setUp(self):
        for target in ("socket.socket", "socket.create_connection", "urllib.request.urlopen"):
            offline = mock.patch(target, side_effect=AssertionError("network used"))
            offline.start()
            self.addCleanup(offline.stop)
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-kit-denial-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.root = self.tmp / "campaign"
        for command in (("init", "--name", "Synthetic", "--county", "San Luis Obispo", "--state", "CA"), ("kit",)):
            code, _, err = run(command[0], "--directory", self.root, *command[1:])
            self.assertEqual(code, 0, err)
        self.a, self.b = sorted(p.stem for p in (self.root / "kit" / "requests").glob("*.md"))[:2]
        ready_drafts(self.root)  # kit-sent refuses unfilled placeholders and unverified recipients (DF1507-07)
        self.folder = self.root / "kit" / "followups"
        self.sent_path = self.root / "kit" / "sent.json"

    def cli(self, command, agency, *extra):
        return run(command, "--directory", self.root, "--agency", agency, *extra)

    def denied(self, agency, on="2026-03-20", kinds=("denied",)):
        self.assertEqual(self.cli("kit-sent", agency, "--date", "2026-03-02", "--channel", "email")[0], 0)
        for kind in kinds:
            code, _, err = self.cli("kit-response", agency, "--date", on, "--kind", kind)
            self.assertEqual(code, 0, err)

    def reply(self, agency, *extra):
        code, out, err = self.cli("kit-denial-reply", agency, *extra)
        self.assertEqual(code, 0, err)
        self.assertTrue(out.endswith(DONE), out)
        return json.loads(out[:-len(DONE)])

    def stopped(self, words, agency, *extra):
        before = snapshot(self.root)
        code, out, err = self.cli("kit-denial-reply", agency, *extra)
        self.assertEqual(code, 1, out)
        self.assertTrue(err.startswith("Stopped: "), err)
        self.assertIn(words, err)
        self.assertEqual(snapshot(self.root), before)

    def synthetic_law(self):
        """A copy of the shipped package with three synthetic rules; patched in as the jurisdictions folder."""
        package = json.loads(law.package_path("us-ca").read_text(encoding="utf-8"))
        template = package["rules"][0]
        for rule_id, start, end, citation in (("ca-syn-open", "2020-01-01", None, "Synthetic Code § 1"),
                                              ("ca-syn-future", "2026-03-21", None, "Synthetic Code § 2"),
                                              ("ca-syn-expired", "2020-01-01", "2026-03-19", "Synthetic Code § 3")):
            package["rules"].append({**template, "rule_id": rule_id, "effective_from": start, "effective_to": end,
                                     "citation": citation})
        base = self.tmp / "law"
        (base / "us-ca").mkdir(parents=True)
        (base / "us-ca" / "package.json").write_text(json.dumps(package), encoding="utf-8")
        law.load_package("us-ca", base)
        return mock.patch.object(law, "JURISDICTIONS", base)

    def test_shipped_rule_cited_with_package_citation_and_status_label(self):
        self.denied(self.a)
        package = law.load_package("us-ca")
        self.assertEqual(package["status"], "draft")
        citation = next(r["citation"] for r in package["rules"] if r["rule_id"] == SHIPPED)
        report = self.reply(self.a, "--rule", SHIPPED)
        name = f"{self.a}-denial-2026-03-20.md"
        self.assertEqual(report, {"written": f"kit/followups/{name}", "rules": [SHIPPED],
                                  "law_package_status": "draft"})
        text = (self.folder / name).read_text(encoding="utf-8")
        self.assertIn(f"- {SHIPPED} (law package status: draft): {citation}\n", text)
        self.assertIn("On 2026-03-02 we sent a records request by email", text)
        self.assertIn("On 2026-03-20 we recorded the agency's response\nas a denial.", text)
        self.assertIn("law package: draft", text)
        for ask in ASKS:
            self.assertIn(ask, text)

    def test_no_denied_step_is_refused_and_nothing_written(self):
        self.stopped(f"no recorded denial for {self.a}", self.a)  # never sent
        self.stopped("no recorded denial for ../escape", "../escape")
        self.denied(self.a, kinds=("acknowledged", "determination", "produced", "closed"))
        for extra in ((), ("--rule", SHIPPED), ("--replace",)):
            with self.subTest(extra=extra):
                self.stopped(f"no recorded denial for {self.a}", self.a, *extra)
        self.assertFalse(os.path.lexists(self.folder))

    def test_unknown_duplicate_and_out_of_force_rules_are_refused(self):
        self.denied(self.a)
        with self.synthetic_law():
            for words, rules in (
                    ("--rule ca-syn-missing is not in the law package", ("ca-syn-missing",)),
                    ("--rule 7920.000 is not in the law package", ("ca-syn-open", "7920.000")),
                    ("--rule ca-syn-future is not in force on the denial date 2026-03-20", ("ca-syn-future",)),
                    ("--rule ca-syn-expired is not in force on the denial date 2026-03-20", ("ca-syn-expired",)),
                    ("--rule ca-syn-open is given twice", ("ca-syn-open", "ca-syn-open"))):
                with self.subTest(rules=rules):
                    self.stopped(words, self.a, *[x for r in rules for x in ("--rule", r)])
            self.assertFalse(os.path.lexists(self.folder))
            report = self.reply(self.a, "--rule", "ca-syn-open", "--rule", SHIPPED)
        self.assertEqual(report["rules"], ["ca-syn-open", SHIPPED])
        text = (self.root / report["written"]).read_text(encoding="utf-8")
        self.assertIn("- ca-syn-open (law package status: draft): Synthetic Code § 1\n", text)
        self.assertNotIn("Synthetic Code § 2", text)

    def test_no_rule_has_no_citations_and_missing_package_only_refuses_rules(self):
        self.denied(self.a)
        report = self.reply(self.a)
        self.assertEqual(report["rules"], [])
        text = (self.root / report["written"]).read_text(encoding="utf-8")
        self.assertNotIn("law package status:", text)
        self.assertNotIn("Rules we noted", text)
        self.assertNotIn("§", text)
        for ask in ASKS:
            self.assertIn(ask, text)
        empty = self.tmp / "no-law"
        empty.mkdir()
        with mock.patch.object(law, "JURISDICTIONS", empty):
            self.stopped("no law package: --rule cannot be checked", self.a, "--rule", SHIPPED, "--replace")
            report = self.reply(self.a, "--replace")
        self.assertEqual(report["law_package_status"], "missing")
        text = (self.root / report["written"]).read_text(encoding="utf-8")
        self.assertIn("law package: missing", text)
        self.assertNotIn("law package status:", text)

    def test_existing_target_needs_replace_and_symlinks_are_refused(self):
        self.denied(self.a)
        target = self.folder / f"{self.a}-denial-2026-03-20.md"
        self.reply(self.a)
        target.write_text("organizer edits\n", encoding="utf-8")
        self.stopped("already exists; use --replace", self.a, "--rule", SHIPPED)
        self.assertEqual(target.read_bytes(), b"organizer edits\n")
        self.reply(self.a, "--rule", SHIPPED, "--replace")
        self.assertIn(SHIPPED, target.read_text(encoding="utf-8"))
        self.assertEqual([p.name for p in self.folder.iterdir()], [target.name])  # no temp file left behind
        outside = self.tmp / "outside.md"
        outside.write_text("keep\n", encoding="utf-8")
        target.unlink()
        target.symlink_to(outside)
        for extra in ((), ("--replace",)):
            with self.subTest(extra=extra):
                self.stopped("must not be a symlink", self.a, *extra)
        self.assertEqual(outside.read_text(encoding="utf-8"), "keep\n")
        target.unlink()
        self.folder.rmdir()
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        self.folder.symlink_to(elsewhere, target_is_directory=True)
        self.stopped("followups/ must not be a symlink", self.a)
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_two_denied_steps_use_the_later_date_in_any_order(self):
        self.denied(self.a)
        document = json.loads(self.sent_path.read_text(encoding="utf-8"))
        first = document["requests"][self.a]["steps"][0]
        document["requests"][self.a]["steps"] = [{**first, "on": "2026-04-15"}, first]  # later one listed first
        self.sent_path.write_text(json.dumps(document), encoding="utf-8")
        report = self.reply(self.a)
        self.assertEqual(report["written"], f"kit/followups/{self.a}-denial-2026-04-15.md")
        text = (self.root / report["written"]).read_text(encoding="utf-8")
        self.assertIn("On 2026-04-15 we recorded", text)
        self.assertNotIn("2026-03-20", text)

    def test_private_modes_sent_json_unchanged_and_only_the_draft_written(self):
        self.denied(self.a)
        self.denied(self.b)
        before = snapshot(self.root)
        report = kit_track.denial_reply(self.root, self.a, [SHIPPED])
        after = snapshot(self.root)
        self.assertEqual(set(after) - set(before), {report["written"]})
        self.assertEqual({k: v for k, v in after.items() if k in before}, before)
        self.assertIn("kit/sent.json", before)
        self.assertEqual(stat.S_IMODE((self.root / report["written"]).stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.folder.stat().st_mode), 0o700)


if __name__ == "__main__":
    unittest.main()
