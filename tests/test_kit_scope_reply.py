"""kit-scope-reply writes a private, never-sent draft about recorded per-scope gaps. Offline, synthetic."""
import contextlib
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import cli, kit_track, law  # noqa: E402
from tests.flock.test_legal_claim_guard import privacy  # noqa: E402
from tests.test_kit_check import ready_drafts  # noqa: E402

PACKAGE = law.load_package("us-ca")
SCOPES = [s["scope_id"] for s in PACKAGE["request_scopes"]]
TITLES = {s["scope_id"]: s["title"] for s in PACKAGE["request_scopes"]}
A, B, C, D = SCOPES[:4]
DAY = date(2026, 4, 1)


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


def snapshot(root):
    return {str(p.relative_to(root)): (p.read_bytes(), stat.S_IMODE(p.lstat().st_mode))
            for p in Path(root).rglob("*") if p.is_file() and not p.is_symlink()}


class ScopeReplyTests(unittest.TestCase):
    def setUp(self):
        for target in ("socket.socket", "socket.create_connection", "urllib.request.urlopen"):
            offline = mock.patch(target, side_effect=AssertionError("network used"))
            offline.start()
            self.addCleanup(offline.stop)
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-kit-scope-reply-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        code, _, err = run("init", "--directory", self.root, "--name", "Synthetic", "--county", "San Luis Obispo",
                           "--state", "CA")
        self.assertEqual(code, 0, err)
        self.sent = self.root / "kit" / "sent.json"
        self.folder = self.root / "kit" / "followups"

    def sent_request(self, *scopes):
        args = [x for scope in scopes for x in ("--scope", scope)]
        code, _, err = run("kit", "--directory", self.root, *args)
        self.assertEqual(code, 0, err)
        self.agency = sorted(p.stem for p in (self.root / "kit" / "requests").glob("*.md"))[0]
        ready_drafts(self.root)
        code, _, err = run("kit-sent", "--directory", self.root, "--agency", self.agency, "--date", "2026-03-02",
                           "--channel", "email")
        self.assertEqual(code, 0, err)

    def step(self, kind, *extra, on="2026-03-10"):
        code, _, err = run("kit-response", "--directory", self.root, "--agency", self.agency, "--date", on,
                           "--kind", kind, *extra)
        self.assertEqual(code, 0, err)

    def answer(self, kind, disposition, *scopes, on="2026-03-10"):
        self.step(kind, *[x for s in scopes for x in ("--scope", s)], "--disposition", disposition, on=on)

    def reply(self, *extra):
        return run("kit-scope-reply", "--directory", self.root, "--agency", self.agency, *extra)

    def draft(self, today=DAY, **kwargs):
        result = kit_track.scope_reply(self.root, self.agency, today=today, **kwargs)
        return result, (self.root / result["written"]).read_text(encoding="utf-8")

    def refused(self, words, **kwargs):
        before = snapshot(self.root)
        folder_before = self.folder.exists()
        with self.assertRaisesRegex(ValueError, words):
            kit_track.scope_reply(self.root, self.agency, today=DAY, **kwargs)
        self.assertEqual(snapshot(self.root), before)
        self.assertEqual(self.folder.exists(), folder_before)

    def mixed(self):
        self.sent_request(A, B, C, D)
        self.answer("produced", "no_records_stated", A)
        self.answer("produced", "partly_produced", C, on="2026-03-12")
        self.answer("denied", "withheld", D, on="2026-03-13")

    def test_gap_a_gap_b_and_answered_in_asked_order(self):
        self.mixed()
        before = snapshot(self.root)
        result, text = self.draft()
        self.assertEqual(result["written"], f"kit/followups/{self.agency}-scopes-2026-04-01.md")
        self.assertEqual((result["gaps"], result["answered"]), (2, 2))
        self.assertIn(f"1. {TITLES[A]}: our record shows the agency stated on 2026-03-10 that it has no records", text)
        self.assertIn(f"2. {TITLES[B]}: we have no recorded answer for this part.", text)
        self.assertLess(text.index(f"1. {TITLES[A]}"), text.index(f"2. {TITLES[B]}"))
        for words in ("offices, systems and accounts were searched", "contractor or vendor", "existed earlier",
                      "retention schedule", "whether responsive records exist", "when they will be produced",
                      "DRAFT — NOT SENT", "Draft only. Nothing was sent.", "Not legal advice", "[EDIT ME: salutation]",
                      "Law package status:"):
            self.assertIn(words, text)
        answered = text[text.index("Parts recorded as answered"):]
        self.assertIn(f"- {TITLES[C]}: recorded as partly produced on 2026-03-12", answered)
        self.assertIn(f"- {TITLES[D]}: recorded as withheld on 2026-03-13", answered)
        self.assertNotIn(f"- {TITLES[A]}", answered)
        self.assertNotIn("3. ", text)
        self.assertEqual(privacy.legal_claims(text), [])
        for forbidden in ("inadequate", "violat", "unlawful", "illegal"):
            self.assertNotIn(forbidden, text.lower())
        after = snapshot(self.root)
        written = after.pop(f"kit/followups/{self.agency}-scopes-2026-04-01.md")
        self.assertEqual(written[1], 0o600)
        self.assertEqual(stat.S_IMODE(self.folder.stat().st_mode), 0o700)
        self.assertEqual(after, before)  # sent.json, drafts and everything else untouched

    def test_cli_prints_notice_and_writes_one_dated_file(self):
        self.mixed()
        code, out, err = self.reply()
        self.assertEqual(code, 0, err)
        self.assertTrue(out.endswith("Draft only. Nothing was sent.\n"))
        self.assertEqual(len(list(self.folder.glob(f"{self.agency}-scopes-*.md"))), 1)

    def test_latest_answer_wins_in_both_directions_and_unrelated_steps_keep_state(self):
        self.sent_request(A, B)
        self.answer("produced", "no_records_stated", A)
        self.answer("produced", "produced", B)
        self.step("denied", on="2026-03-20")
        _, text = self.draft()
        self.assertIn(f"1. {TITLES[A]}", text)
        self.assertIn(f"- {TITLES[B]}: recorded as produced", text)
        self.answer("produced", "produced", A, on="2026-03-25")  # gap removed -> nothing left
        self.refused("no scope gap")
        self.answer("produced", "no_records_stated", B, on="2026-03-26")  # a later no-records answer creates one
        _, text = self.draft(replace=True)
        self.assertIn(f"1. {TITLES[B]}: our record shows the agency stated on 2026-03-26", text)
        self.assertIn(f"- {TITLES[A]}: recorded as produced on 2026-03-25", text)

    def test_same_day_tie_later_recorded_entry_wins(self):
        self.sent_request(A)
        self.answer("produced", "produced", A)
        self.answer("determination", "no_records_stated", A)
        _, text = self.draft()
        self.assertIn(f"1. {TITLES[A]}", text)

    def test_gap_b_needs_produced_or_determination_not_other_steps(self):
        self.sent_request(A, B)
        for kind in ("acknowledged", "extension", "denied"):
            self.step(kind, on="2026-03-05" if kind == "acknowledged" else "2026-03-10")
            self.refused("no scope gap")
        self.step("determination", on="2026-03-15")
        result, text = self.draft()
        self.assertEqual(result["gaps"], 2)
        self.assertIn(f"1. {TITLES[A]}: we have no recorded answer", text)
        self.assertIn(f"2. {TITLES[B]}: we have no recorded answer", text)
        self.assertNotIn("Parts recorded as answered", text)

    def test_no_records_without_produced_step_is_a_gap(self):
        self.sent_request(A, B)
        self.answer("denied", "no_records_stated", A)
        result, text = self.draft()
        self.assertEqual(result["gaps"], 1)
        self.assertNotIn(TITLES[B], text)

    def test_all_answered_and_no_steps_refuse(self):
        self.sent_request(A, B)
        self.refused("no scope gap")
        self.answer("produced", "produced", A)
        self.answer("denied", "withheld", B)
        self.refused("no scope gap")

    def test_no_scopes_and_malformed_state_refuse_before_any_write(self):
        self.sent_request(A, B)
        original = self.sent.read_bytes()

        def edit(change):
            document = json.loads(original)
            change(document["requests"][self.agency])
            self.sent.write_text(json.dumps(document), encoding="utf-8")

        edit(lambda e: e.pop("scopes"))
        self.refused("no scopes recorded")
        for bad in ([], [A, A], [1]):
            edit(lambda e: e.update(scopes=bad))
            self.refused("malformed scopes")
        edit(lambda e: e.update(steps=[{"on": "2026-03-10", "kind": "produced", "scope_answers": {C: "produced"}}]))
        self.refused("malformed steps")
        edit(lambda e: e.update(steps=[{"on": "2026-03-10", "kind": "produced", "scope_answers": {A: "bogus"}}]))
        self.refused("malformed steps")
        edit(lambda e: e.update(steps=[{"on": "not-a-date", "kind": "produced"}]))
        self.refused("malformed steps")
        edit(lambda e: e.update(scopes=["not-in-package"]))
        self.refused("not in the law package")

    def test_missing_law_package_refuses(self):
        self.mixed()
        with mock.patch.object(kit_track.law, "load_package", side_effect=ValueError("no package")):
            self.refused("no law package")

    def test_named_requests_isolate_history_and_stems(self):
        self.mixed()
        drafts = self.root / "kit" / "requests"
        (drafts / f"{self.agency}--second.md").write_text((drafts / f"{self.agency}.md").read_text(encoding="utf-8"),
                                                          encoding="utf-8")
        code, _, err = run("kit-sent", "--directory", self.root, "--agency", self.agency, "--request", "second",
                           "--date", "2026-03-03", "--channel", "email", "--reference", "SYN-REF-2")
        self.assertEqual(code, 0, err)
        code, _, err = run("kit-response", "--directory", self.root, "--agency", self.agency, "--request", "second",
                           "--date", "2026-03-11", "--kind", "produced", "--scope", B, "--disposition",
                           "no_records_stated")
        self.assertEqual(code, 0, err)
        _, main_text = self.draft()
        result, named_text = self.draft(request="second")
        self.assertEqual(result["written"], f"kit/followups/{self.agency}--second-scopes-2026-04-01.md")
        self.assertIn(f"1. {TITLES[A]}", main_text)
        self.assertIn(f"{TITLES[B]}: our record shows the agency stated on 2026-03-11", named_text)
        self.assertIn(f"1. {TITLES[A]}: we have no recorded answer", named_text)
        self.assertNotIn(f"{TITLES[A]}: our record shows", named_text)
        self.assertNotIn(f"{TITLES[B]}: our record shows", main_text)
        self.assertIn("SYN-REF-2", named_text)
        self.assertNotIn("SYN-REF-2", main_text)
        self.assertEqual(len(list(self.folder.glob("*-scopes-*.md"))), 2)

    def test_bad_request_agency_and_flags_refuse(self):
        self.mixed()
        before = snapshot(self.root)
        for request in ("../x", "Bad Name", ""):
            self.assertEqual(self.reply("--request", request)[0], 1)
        self.assertEqual(self.reply("--request", "absent")[0], 1)
        self.assertEqual(run("kit-scope-reply", "--directory", self.root, "--agency", "nope-agency")[0], 1)
        self.assertEqual(run("kit-scope-reply", "--directory", self.root)[0], 2)
        self.assertEqual(self.reply("--bogus")[0], 2)
        self.assertEqual(snapshot(self.root), before)

    def test_existing_draft_kept_unless_replace_and_other_files_untouched(self):
        self.mixed()
        _, first = self.draft()
        path = self.folder / f"{self.agency}-scopes-2026-04-01.md"
        other = self.folder / "other.md"
        other.write_text("mine\n", encoding="utf-8")
        path.write_text("edited by hand\n", encoding="utf-8")
        before = snapshot(self.root)
        self.refused("already exists; use --replace")
        self.assertEqual(snapshot(self.root), before)
        kit_track.scope_reply(self.root, self.agency, today=DAY, replace=True)
        self.assertEqual(path.read_text(encoding="utf-8"), first)
        self.assertEqual(other.read_text(encoding="utf-8"), "mine\n")

    def test_next_day_changes_only_the_generation_date(self):
        self.mixed()
        _, one = self.draft()
        result, two = self.draft(today=date(2026, 4, 2))
        self.assertEqual(result["written"], f"kit/followups/{self.agency}-scopes-2026-04-02.md")
        self.assertEqual(one.replace("2026-04-01", "2026-04-02"), two)

    def test_symlinked_target_and_folder_refuse_with_and_without_replace(self):
        self.mixed()
        self.draft()
        path = self.folder / f"{self.agency}-scopes-2026-04-01.md"
        outside = self.root / "outside.md"
        outside.write_text("keep\n", encoding="utf-8")
        path.unlink()
        os.symlink(outside, path)
        for replace in (False, True):
            self.refused("must not be a symlink|already exists", replace=replace)
        self.assertEqual(outside.read_text(encoding="utf-8"), "keep\n")
        path.unlink()
        path.mkdir()  # not a regular file
        self.refused("already exists", replace=False)
        path.rmdir()
        self.folder.rmdir()
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        os.symlink(elsewhere, self.folder)
        self.refused("symlink|must not", replace=True)
        self.assertEqual(list(elsewhere.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
