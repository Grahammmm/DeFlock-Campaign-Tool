"""kit-contact: the organizer's own calls, voicemails, counter visits and emails, kept apart from agency steps.
Offline, synthetic: invented agencies, job titles and dates; no socket may be opened and nothing is sent."""
import contextlib
import io
import json
import stat
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import cli, kit_track  # noqa: E402
from tests.test_kit_check import ready_drafts  # noqa: E402

NAME = "narrowed"
SENT_ON, NAMED_SENT_ON = "2026-03-02", "2026-03-05"
DONE = "Recorded only. Nothing was sent.\n"
PRIVATE_NOTE = "NOTE-TEXT-ONLY-IN-SENT-JSON"


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


def snapshot(root):  # every path with its kind, mode and bytes: any write, new file or symlink shows
    return {str(p.relative_to(root)): (p.is_symlink(), p.lstat().st_mode, p.read_bytes() if p.is_file() else None)
            for p in sorted(Path(root).rglob("*"))}


class KitContactTests(unittest.TestCase):
    def setUp(self):
        for target, effect in (("socket.socket", AssertionError("socket opened")),
                               ("urllib.request.urlopen", AssertionError("network used"))):
            patcher = mock.patch(target, side_effect=effect)
            patcher.start()
            self.addCleanup(patcher.stop)
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-kit-contact-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.root = self.tmp / "campaign"
        self.assertEqual(run("init", "--directory", self.root, "--name", "Synthetic", "--county", "San Luis Obispo",
                             "--state", "CA")[0], 0)
        self.assertEqual(run("kit", "--directory", self.root)[0], 0)
        self.kit, self.sent = self.root / "kit", self.root / "kit" / "sent.json"
        self.ids = [p.stem for p in sorted((self.kit / "requests").glob("*.md"))]
        self.assertGreaterEqual(len(self.ids), 4)
        self.agency = self.ids[0]
        ready_drafts(self.root)
        (self.kit / "requests" / f"{self.agency}--{NAME}.md").write_text("Narrowed request: the 2026 contract only.\n",
                                                                         encoding="utf-8")
        self.ok(self.send(self.agency, SENT_ON))

    # helpers -------------------------------------------------------------------------------------------------
    def send(self, agency, on, *extra):
        return run("kit-sent", "--directory", self.root, "--agency", agency, "--date", on, "--channel", "email", *extra)

    def contact(self, on, channel="phone", role="Records Clerk", note="Asked about the status", *extra, agency=None):
        return run("kit-contact", "--directory", self.root, "--agency", agency or self.agency, "--on", on,
                   "--channel", channel, "--with", role, "--note", note, *extra)

    def ok(self, result):
        code, out, err = result
        self.assertEqual(code, 0, err)
        return out

    def refused(self, result, *words):
        code, out, err = result
        self.assertEqual(code, 1, out)
        self.assertTrue(err.startswith("Stopped: "), err)
        for word in words:
            self.assertIn(word, err)
        return err

    def load(self):
        return json.loads(self.sent.read_text(encoding="utf-8"))

    def entry(self, key=None):
        return self.load()["requests"][key or self.agency]

    def rows(self, *extra):
        return json.loads(self.ok(run("kit-status", "--directory", self.root, *extra)))["requests"]

    def put(self, key=None, **fields):  # edit one stored entry directly (what a damaged or old file looks like)
        document = self.load()
        document["requests"][key or self.agency].update(fields)
        self.sent.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    def stored(self, **changes):
        return {"on": "2026-03-10", "channel": "phone", "role": "Records Clerk", "note": "Asked",
                "recorded_at": "2026-03-10T09:00:00+00:00", **changes}

    def unchanged(self, action):  # the action is refused with exit 1 and no byte of the campaign tree moves
        before = snapshot(self.root)
        err = self.refused(action())
        self.assertEqual(snapshot(self.root), before)
        return err

    # the ten acceptance behaviours ---------------------------------------------------------------------------
    def test_four_channels_main_and_named_isolated(self):
        self.ok(self.send(self.agency, NAMED_SENT_ON, "--request", NAME))
        self.ok(run("kit-response", "--directory", self.root, "--agency", self.agency, "--date", "2026-03-03",
                    "--kind", "acknowledged", "--reference", "R0123-110125"))
        key = f"{self.agency}#{NAME}"
        before_main, before_named = self.entry(), self.entry(key)
        for number, channel in enumerate(kit_track.CONTACT_CHANNELS):
            out = self.ok(self.contact(f"2026-03-1{number}", channel, f"Title {number}", f"Note {number}"))
            self.assertTrue(out.endswith(DONE), out)
            printed = json.loads(out[:-len(DONE)])
            self.assertEqual((printed["request"], printed["contacts"], printed["contact"]),
                             ("main", number + 1, {"on": f"2026-03-1{number}", "channel": channel,
                                                   "role": f"Title {number}"}))
        self.ok(self.contact("2026-03-20", "email", "Records Supervisor", "Named follow", "--request", NAME))
        main, named = self.entry(), self.entry(key)
        self.assertEqual([c["channel"] for c in main["contacts"]], ["phone", "voicemail", "in_person", "email"])
        self.assertEqual([c["note"] for c in main["contacts"]], [f"Note {n}" for n in range(4)])
        self.assertEqual([c["channel"] for c in named["contacts"]], ["email"])
        for contact in main["contacts"] + named["contacts"]:
            self.assertEqual(set(contact), {"on", "channel", "role", "note", "recorded_at"})
            self.assertRegex(contact["recorded_at"], r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00")
        # steps, reference, draft hash and every other key are exactly what they were; contacts are not steps
        self.assertEqual({k: v for k, v in main.items() if k != "contacts"}, before_main)
        self.assertEqual({k: v for k, v in named.items() if k != "contacts"}, before_named)
        self.assertEqual(main["steps"][0]["kind"], "acknowledged")
        self.assertEqual(len(main["steps"]), 1)
        self.assertEqual(stat.S_IMODE(self.sent.stat().st_mode), 0o600)

    def test_date_boundaries_and_backdated_latest(self):
        today = date(2026, 4, 1)

        def add(on, channel="phone"):
            return kit_track.record_contact(self.root, self.agency, on, channel, "Records Clerk", "Asked", today=today)
        add(SENT_ON)  # the sent day and today are both accepted
        add("2026-04-01")
        before = snapshot(self.root)
        for bad in ("2026-03-01", "2026-04-02", "2026-02-30", "2026-3-5", "2026-03-05 ", "tomorrow", "20260305", ""):
            with self.subTest(on=bad), self.assertRaises(ValueError):
                add(bad)
        self.assertEqual(snapshot(self.root), before)
        self.assertIn("in the future", self.refused(self.contact("2999-01-01")))
        self.assertIn("before the request was sent", self.refused(self.contact("2026-03-01")))
        # a backdated contact is allowed in any order; the latest is the greatest date, a tie going to the later one
        add("2026-03-20", "voicemail")
        add("2026-03-10", "in_person")
        add("2026-03-20", "email")
        [row] = [r for r in kit_track.status(self.root, today=today)["requests"] if r["agency_id"] == self.agency]
        self.assertEqual(row["contacts"], 5)
        self.assertEqual(row["last_contact"], {"on": "2026-04-01", "channel": "phone", "role": "Records Clerk"})
        stored = self.entry()
        stored["contacts"] = [c for c in stored["contacts"] if c["on"] != "2026-04-01"]
        self.put(contacts=stored["contacts"])
        [row] = [r for r in kit_track.status(self.root, today=today)["requests"] if r["agency_id"] == self.agency]
        self.assertEqual(row["last_contact"]["channel"], "email")

    def test_role_and_note_boundaries(self):
        def add(role="Records Clerk", note="Asked"):
            return kit_track.record_contact(self.root, self.agency, SENT_ON, "phone", role, note, today=date(2026, 4, 1))
        add("R", "N")
        add("R" * 60, "N" * 500)
        add("  Records    Clerk  ", "  two   words  ")
        stored = self.entry()["contacts"]
        self.assertEqual((len(stored[1]["role"]), len(stored[1]["note"])), (60, 500))
        self.assertEqual((stored[2]["role"], stored[2]["note"]), ("Records Clerk", "two words"))
        add("Records Clerk 123456")  # six digits are not a phone number
        before = snapshot(self.root)
        bad_roles = ("", "   ", "R" * 61, "<b>Clerk</b>", "Clerk\nSupervisor", "Clerk\tX", "Clerk\x07", "Clerk\u200bX", "Clerk\u202eX",
                     "clerk@example.org", "j@x", "555-123-4567", "(805) 555 0100", "+1 805.555.0100", "5551234",
                     "Clerk 55 5 12 34", "Clerk\u00a05551234")
        for role in bad_roles:
            with self.subTest(role=role), self.assertRaises(ValueError) as caught:
                add(role=role)
            self.assertNotIn("example", str(caught.exception))  # the refused text is never echoed
        for note in ("", "   ", "N" * 501, "<script>", "a > b", "line\nbreak", "bell\x07", "mark\u200bup", "rtl\u202e"):
            with self.subTest(note=note), self.assertRaises(ValueError):
                add(note=note)
        self.assertEqual(snapshot(self.root), before)
        err = self.refused(self.contact(SENT_ON, "phone", "clerk@example.org"), "--with", "job title")
        self.assertNotIn("clerk@example.org", err)
        self.refused(self.contact(SENT_ON, "phone", "Clerk", "<b>"), "--note")
        self.assertEqual(run("kit-contact", "--directory", self.root, "--agency", self.agency, "--on", SENT_ON,
                             "--channel", "fax", "--with", "Clerk", "--note", "x")[0], 2)  # argparse: exit 2
        self.assertIn("not a person's name", run("kit-contact", "--help")[1].replace("\n", " "))

    def test_missing_request_and_corrupt_store_write_nothing(self):
        other = self.ids[1]
        self.assertIn("kit-sent", self.unchanged(lambda: self.contact(SENT_ON, agency=other)))
        self.assertIn("--request nope", self.unchanged(lambda: self.contact(SENT_ON, "phone", "Clerk", "x",
                                                                           "--request", "nope")))
        self.unchanged(lambda: self.contact(SENT_ON, agency="Bad ID"))
        self.unchanged(lambda: self.contact(SENT_ON, agency=f"{self.agency}#{NAME}"))
        self.unchanged(lambda: self.contact(SENT_ON, "phone", "Clerk", "x", "--request", "Bad Name"))
        good = self.stored()
        for label, contacts in (("not a list", {"on": "2026-03-10"}), ("a string item", ["2026-03-10"]),
                                ("extra key", [{**good, "extra": 1}]), ("missing key", [{"on": "2026-03-10"}]),
                                ("bad channel", [{**good, "channel": "fax"}]),
                                ("bad date", [{**good, "on": "2026-02-30"}]), ("before sent", [{**good, "on": "2026-03-01"}]),
                                ("bad type", [{**good, "role": 5}]), ("email role", [{**good, "role": "a@b.org"}]),
                                ("blank note", [{**good, "note": ""}]), ("long note", [{**good, "note": "n" * 501}]),
                                ("bad stamp", [{**good, "recorded_at": "yesterday"}])):
            with self.subTest(label):
                self.put(contacts=contacts)
                self.assertIn("unreadable", self.unchanged(lambda: self.contact(SENT_ON)))
                self.assertIn("unreadable", self.unchanged(lambda: run("kit-status", "--directory", self.root)))
        self.put(contacts=[])  # an empty list is valid and reads as none
        self.assertNotIn("contacts", self.rows()[0])
        self.ok(self.contact(SENT_ON))
        self.assertEqual(len(self.entry()["contacts"]), 1)
        # a symlinked store and a store the new contact would push past 1 MiB are refused untouched
        real = self.kit / "sent-real.json"
        self.sent.rename(real)
        self.sent.symlink_to(real)
        self.assertIn("unreadable", self.unchanged(lambda: self.contact(SENT_ON)))
        self.sent.unlink()
        real.rename(self.sent)
        with mock.patch.object(kit_track, "MAX_BYTES", self.sent.stat().st_size + 40):
            self.assertIn("1 MiB", self.unchanged(lambda: self.contact(SENT_ON)))

    def test_contacts_do_not_advance_steps(self):
        self.ok(self.contact("2026-03-04", note=PRIVATE_NOTE))  # a contact before any step does not touch acknowledged-first
        self.ok(run("kit-response", "--directory", self.root, "--agency", self.agency, "--date", "2026-03-06",
                    "--kind", "acknowledged"))
        self.assertEqual([s["kind"] for s in self.entry()["steps"]], ["acknowledged"])
        due_args = ("kit-due", "--directory", self.root, "--as-of", "2026-06-01")
        before_due = run(*due_args)
        row = self.rows()[0]
        asked = run("kit-asked", "--directory", self.root)
        self.ok(self.contact("2026-03-07", "voicemail"))
        self.assertEqual(run(*due_args), before_due)
        self.assertEqual(run("kit-asked", "--directory", self.root), asked)
        after = self.rows()[0]
        self.assertEqual({k: v for k, v in after.items() if k not in ("contacts", "last_contact")},
                         {k: v for k, v in row.items() if k not in ("contacts", "last_contact")})
        self.ok(run("kit-response", "--directory", self.root, "--agency", self.agency, "--date", "2026-03-08",
                    "--kind", "closed"))
        self.ok(self.contact("2026-03-09", "in_person"))  # a closed request still takes a contact
        self.assertEqual([s["kind"] for s in self.entry()["steps"]], ["acknowledged", "closed"])
        self.refused(run("kit-response", "--directory", self.root, "--agency", self.agency, "--date", "2026-03-10",
                         "--kind", "determination"), "closed")
        self.assertTrue(self.rows()[0]["closed"])
        self.assertEqual(self.rows()[0]["contacts"], 3)

    def test_status_summary_private_and_csv_unchanged(self):
        third = self.ids[2]
        self.ok(self.send(third, "2026-03-03"))
        csv_before = self.tmp / "before.csv"
        self.ok(run("kit-status", "--directory", self.root, "--csv", csv_before))
        plain_before = run("kit-status", "--directory", self.root)
        self.assertEqual(plain_before[0], 0)
        self.ok(self.contact("2026-03-10", "voicemail", "Records Supervisor", PRIVATE_NOTE))
        self.ok(self.contact("2026-03-12", "in_person", "Front Counter Clerk", PRIVATE_NOTE))
        code, out, err = run("kit-status", "--directory", self.root)
        self.assertEqual(code, 0, err)
        report = json.loads(out)  # stdout stays one JSON document
        rows = {r["agency_id"]: r for r in report["requests"]}
        self.assertEqual(rows[self.agency]["contacts"], 2)
        self.assertEqual(rows[self.agency]["last_contact"],
                         {"on": "2026-03-12", "channel": "in_person", "role": "Front Counter Clerk"})
        self.assertNotIn("contacts", rows[third])  # no contacts: the row reads exactly as before
        self.assertNotIn("last_contact", rows[third])
        self.assertEqual({k: v for k, v in rows[third].items()},
                         {r["agency_id"]: r for r in json.loads(plain_before[1])["requests"]}[third])
        self.assertNotIn(PRIVATE_NOTE, out + err)
        csv_after = self.tmp / "after.csv"
        self.ok(run("kit-status", "--directory", self.root, "--csv", csv_after))
        self.assertEqual(csv_after.read_bytes(), csv_before.read_bytes())
        self.assertNotIn(PRIVATE_NOTE, csv_after.read_text(encoding="utf-8"))

    def test_citation_opt_in_as_of_and_named_filename(self):
        self.ok(self.contact("2026-03-10", "phone", "Records Supervisor", PRIVATE_NOTE))
        self.ok(self.contact("2026-03-10", "voicemail", "Records Supervisor", PRIVATE_NOTE))  # same day: the later one
        self.ok(self.contact("2026-04-20", "in_person", "Front Counter Clerk", PRIVATE_NOTE))
        followups = self.kit / "followups"

        def draft(as_of, *extra):
            code, out, err = run("kit-followup", "--directory", self.root, "--agency", self.agency, "--as-of", as_of,
                                 *extra)
            self.assertEqual(code, 0, err)
            self.assertTrue(out.endswith("Draft only. Nothing was sent.\n"), out)
            return (self.root / json.loads(out.rsplit("Draft only.", 1)[0])["written"]).read_text(encoding="utf-8")
        plain = draft("2026-04-01")
        self.assertNotIn("we contacted", plain)
        cited = draft("2026-04-01", "--cite-contact", "--replace")
        line = "On 2026-03-10 we contacted the records office by voicemail (Records Supervisor)."
        self.assertEqual(cited.count(line), 1)
        self.assertEqual(cited.replace(line + "\n\n", "", 1), plain)  # nothing else in the draft moves
        self.assertNotIn(PRIVATE_NOTE, cited)
        self.assertNotIn("2026-04-20", cited)
        later = draft("2026-05-01", "--cite-contact")
        self.assertEqual(later.count("On 2026-04-20 we contacted the records office by in person (Front Counter Clerk)."), 1)
        self.assertNotIn("2026-03-10 we contacted", later)
        # a named request writes its own draft and cites only its own contact
        self.ok(self.send(self.agency, NAMED_SENT_ON, "--request", NAME))
        self.ok(self.contact("2026-03-25", "email", "Records Manager", PRIVATE_NOTE, "--request", NAME))
        code, out, err = run("kit-followup", "--directory", self.root, "--agency", self.agency, "--request", NAME,
                             "--as-of", "2026-05-01", "--cite-contact")
        self.assertEqual(code, 0, err)
        named_name = f"{self.agency}--{NAME}-2026-05-01.md"
        self.assertEqual(json.loads(out.split("Draft only.")[0])["written"], f"kit/followups/{named_name}")
        named = (followups / named_name).read_text(encoding="utf-8")
        self.assertIn("On 2026-03-25 we contacted the records office by email (Records Manager).", named)
        self.assertNotIn("Front Counter Clerk", named)
        self.assertEqual((followups / f"{self.agency}-2026-05-01.md").read_text(encoding="utf-8"), later)
        self.assertEqual(sorted(p.name for p in followups.iterdir()),
                         sorted([f"{self.agency}-2026-04-01.md", f"{self.agency}-2026-05-01.md", named_name]))

    def test_citation_empty_and_followup_gates(self):
        followups = self.kit / "followups"
        args = ("kit-followup", "--directory", self.root, "--agency", self.agency, "--as-of", "2026-05-01")
        # no contact at all, and a contact only after the as-of day: refused, no followups folder is created
        self.assertIn("kit-contact", self.unchanged(lambda: run(*args, "--cite-contact")))
        self.ok(self.contact("2026-05-02"))
        self.unchanged(lambda: run(*args, "--cite-contact"))
        self.assertFalse(followups.exists())
        # an existing draft is left byte for byte when --replace --cite-contact finds nothing to cite
        self.ok(run(*args))
        [existing] = followups.iterdir()
        kept = existing.read_bytes()
        self.unchanged(lambda: run(*args, "--cite-contact", "--replace"))
        self.assertEqual(existing.read_bytes(), kept)
        # every other gate still holds with an eligible contact
        gates = (("waiting", "clarification_requested", 3, "waiting on you"), ("answered", "determination", 1, "answered"),
                 ("closed", None, 1, "closed"))
        for agency, (label, kind, code, words) in zip(self.ids[1:], gates):
            with self.subTest(label):
                self.ok(self.send(agency, SENT_ON))
                self.ok(self.contact("2026-03-10", agency=agency))
                if kind:
                    self.ok(run("kit-response", "--directory", self.root, "--agency", agency, "--date", "2026-03-11",
                                "--kind", kind))
                else:
                    self.ok(run("kit-response", "--directory", self.root, "--agency", agency, "--date", "2026-03-11",
                                "--kind", "acknowledged"))
                    self.ok(run("kit-response", "--directory", self.root, "--agency", agency, "--date", "2026-03-12",
                                "--kind", "closed"))
                before = snapshot(self.root)
                result = run("kit-followup", "--directory", self.root, "--agency", agency, "--as-of", "2026-05-01",
                             "--cite-contact")
                self.assertEqual(result[0], code, result)
                self.assertIn(words, result[2])
                self.assertEqual(snapshot(self.root), before)
        with self.subTest("not past due"):
            fresh = self.ids[4] if len(self.ids) > 4 else None
            if fresh:
                self.ok(self.send(fresh, "2026-03-03"))
                self.ok(self.contact("2026-03-04", agency=fresh))
                before = snapshot(self.root)
                result = run("kit-followup", "--directory", self.root, "--agency", fresh, "--as-of", "2026-03-05",
                             "--cite-contact")
                self.assertEqual(result[0], 1)
                self.assertIn("not past due", result[2])
                self.assertEqual(snapshot(self.root), before)

    def test_undo_and_replace_preserve_contacts(self):
        agency = self.ids[1]
        self.ok(self.send(agency, SENT_ON))
        self.put(agency, channel="portal", request_url="https://records.example.org/requests/12345")
        self.ok(run("kit-response", "--directory", self.root, "--agency", agency, "--date", "2026-03-03",
                    "--kind", "acknowledged", "--reference", "R0123-110125"))
        self.ok(self.contact("2026-03-10", "phone", agency=agency))
        self.ok(self.contact("2026-03-12", "email", agency=agency))
        before = self.entry(agency)
        self.ok(run("kit-response-undo", "--directory", self.root, "--agency", agency, "--reason", "Wrong date typed"))
        after = self.entry(agency)
        self.assertEqual(after["steps"], [])
        for key in ("contacts", "reference", "request_url", "channel", "draft_sha256"):
            self.assertEqual(after[key], before[key], key)
        self.assertEqual(len(after["undo_ids"]), 1)
        # zero steps: a corrected sent date keeps the contacts; one later than the earliest contact is refused
        swap = lambda day: run("kit-sent", "--directory", self.root, "--agency", agency, "--date", day,  # noqa: E731
                               "--channel", "portal", "--replace")
        self.assertIn("contact", self.unchanged(lambda: swap("2026-03-11")))
        self.ok(swap("2026-03-10"))
        self.ok(swap("2026-03-01"))
        kept = self.entry(agency)
        self.assertEqual((kept["sent_on"], kept["contacts"], kept["reference"], kept["undo_ids"]),
                         ("2026-03-01", before["contacts"], "R0123-110125", after["undo_ids"]))
        self.assertEqual(kept["request_url"], before["request_url"])
        # the contact is still before a step that is later recorded: contacts never count as steps
        self.assertEqual(self.rows()[1]["steps"], 0)

    def test_only_private_files_change(self):
        public = self.root / "public"
        before = snapshot(self.root)
        self.ok(self.contact("2026-03-10", "phone", "Records Clerk", PRIVATE_NOTE))
        after = snapshot(self.root)
        self.assertEqual({p for p in after if after[p] != before.get(p)}, {"kit/sent.json"})
        self.assertEqual(set(after), set(before))  # no new file, folder or temp file
        for private in ("sent-undone.json", "followups", "request-settings.json"):
            self.assertFalse((self.kit / private).exists(), private)
        self.assertFalse(public.exists() and any(PRIVATE_NOTE in p.read_text(errors="ignore")
                                                 for p in public.rglob("*") if p.is_file()))
        self.ok(run("kit-status", "--directory", self.root))
        self.assertEqual(snapshot(self.root), after)  # status only reads
        code, out, err = run("kit-followup", "--directory", self.root, "--agency", self.agency, "--as-of", "2026-05-01",
                             "--cite-contact")
        self.assertEqual(code, 0, err)
        final = snapshot(self.root)
        written = f"{self.agency}-2026-05-01.md"
        self.assertEqual({p for p in final if final[p] != after.get(p)}, {"kit/followups", f"kit/followups/{written}"})
        self.assertEqual(final["kit/sent.json"], after["kit/sent.json"])
        self.assertEqual(stat.S_IMODE((self.kit / "followups").stat().st_mode), 0o700)
        self.assertNotIn(PRIVATE_NOTE, (self.kit / "followups" / written).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
