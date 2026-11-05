"""DF1407-01: the day the agency received a request (kit-sent --received-on, kit-received) moves the provisional dates.

Offline and synthetic. Receipt-less output must stay byte-identical to the pre-change fixture; no date is a legal deadline.
"""
import json
import shutil
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "kit_received"
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(FIXTURES))
from campaign_tool import kit_calendar, kit_timing, kit_track, law, today  # noqa: E402
import legacy_capture as legacy  # noqa: E402

AS_OF = legacy.AS_OF
DAY = date.fromisoformat(AS_OF)
EVIDENCE = ("portal", "acknowledgement", "return_receipt", "other")


def note(day, kind):
    return f"counted from receipt {day} ({kind})"


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.offline = mock.patch("urllib.request.urlopen", side_effect=AssertionError("network used"))
        cls.offline.start()
        cls.tmp = tempfile.TemporaryDirectory(prefix="synthetic-kit-received-")
        cls.base = Path(cls.tmp.name) / "base" / "campaign"
        cls.base.parent.mkdir()
        cls.agencies = legacy.build(cls.base)
        cls.a, cls.b, cls.c = cls.agencies
        cls.fixture = json.loads((FIXTURES / "legacy-output.json").read_text(encoding="utf-8"))
        cls.package = law.load_package("us-ca")

    @classmethod
    def tearDownClass(cls):
        cls.offline.stop()
        cls.tmp.cleanup()

    def setUp(self):
        self.n = getattr(Base, "count", 0) + 1
        Base.count = self.n
        self.root = Path(self.tmp.name) / f"t{self.n}" / "campaign"
        self.root.parent.mkdir()
        shutil.copytree(self.base, self.root)
        self.sent = self.root / "kit" / "sent.json"

    def cli(self, *arguments, expect=0):
        code, out, err = legacy.run(*arguments)
        self.assertEqual(code, expect, err + out)
        return out, err

    def received(self, agency, on, evidence, *extra, expect=0):
        return self.cli("kit-received", "--directory", self.root, "--agency", agency, "--on", on, "--evidence", evidence,
                        *extra, expect=expect)

    def refused(self, *arguments, words):
        before = self.sent.read_bytes()
        code, out, err = legacy.run(*arguments)
        self.assertEqual(code, 1, out)
        self.assertTrue(err.startswith("Stopped: "), err)
        self.assertIn(words, err)
        self.assertEqual(self.sent.read_bytes(), before)  # a refusal changes nothing

    def entry(self, agency, request="main"):
        return json.loads(self.sent.read_text(encoding="utf-8"))["requests"][kit_track._key(agency, request)]

    def status_rows(self, **kw):
        return {(r["agency_id"], r["request"]): r for r in kit_track.status(self.root, today=DAY, **kw)["requests"]}

    def due(self):
        return kit_track.due(self.root, AS_OF, today=DAY)

    def deadline(self, day, extension=False):
        return law.deadline(self.package, day, extension=extension).isoformat()


class ReceiptFreeTests(Base):
    def test_every_surface_without_receipt_is_byte_identical_to_the_pre_change_fixture(self):
        now = legacy.collect(self.root, self.agencies)
        self.assertEqual(set(now), set(self.fixture))
        for key in self.fixture:
            self.assertTrue(now[key] == self.fixture[key], f"{key} differs from the pre-change output")

    def test_the_fixture_has_no_receipt_wording_or_keys(self):
        text = json.dumps(self.fixture)
        for word in ("received_on", "receipt_evidence", "counted from receipt"):
            self.assertNotIn(word, text)

    def test_csv_header_and_timing_are_unchanged_by_a_recorded_receipt(self):
        self.assertEqual(kit_track.CSV_HEADER, ("agency_id", "name", "drafted", "sent_on", "channel", "last_step_on",
                                                "last_step_kind", "records", "sources", "findings", "fee", "scopes",
                                                "request", "owner"))
        before = kit_timing.format_report(kit_timing.report(self.root, AS_OF))
        self.received(self.a, "2026-03-20", "portal")
        self.assertEqual(kit_timing.format_report(kit_timing.report(self.root, AS_OF)), before)
        self.assertEqual(before, self.fixture["timing"])

    def test_an_acknowledged_step_never_sets_a_receipt(self):
        self.cli("kit-response", "--directory", self.root, "--agency", self.a, "--date", "2026-03-12", "--kind",
                 "acknowledged")
        entry = self.entry(self.a)
        self.assertNotIn("received_on", entry)
        self.assertNotIn("receipt_evidence", entry)
        self.assertEqual(self.status_rows()[(self.a, "main")]["determination_due"], self.deadline("2026-03-10"))
        self.received(self.a, "2026-03-15", "acknowledgement")
        self.cli("kit-response", "--directory", self.root, "--agency", self.c, "--request", "dl", "--date", "2026-03-11",
                 "--kind", "closed")  # a later step on another request leaves this receipt alone
        self.assertEqual((self.entry(self.a)["received_on"], self.entry(self.a)["receipt_evidence"]),
                         ("2026-03-15", "acknowledgement"))


class RecordingTests(Base):
    def test_kit_received_stores_the_pair_on_the_sent_entry_for_each_evidence_kind(self):
        before = self.entry(self.a)
        for kind in EVIDENCE:
            self.received(self.a, "2026-03-12", kind, *(["--replace"] if kind != "portal" else []))
            entry = self.entry(self.a)
            self.assertEqual((entry["received_on"], entry["receipt_evidence"]), ("2026-03-12", kind))
            self.assertEqual({k: v for k, v in entry.items() if k not in ("received_on", "receipt_evidence")}, before)
        self.assertEqual(json.loads(self.sent.read_text(encoding="utf-8"))["schema_version"], 1)

    def test_kit_sent_records_the_pair_with_the_send_for_each_evidence_kind(self):
        for kind in EVIDENCE:
            root = Path(self.tmp.name) / f"fresh-{kind}" / "campaign"
            root.parent.mkdir()
            self.cli("init", "--directory", root, "--name", "Synthetic", "--county", "San Luis Obispo", "--state", "CA")
            self.cli("kit", "--directory", root)
            legacy.ready_drafts(root)
            agency = sorted(p.stem for p in (root / "kit" / "requests").glob("*.md"))[0]
            self.cli("kit-sent", "--directory", root, "--agency", agency, "--date", "2026-03-10", "--channel", "mail",
                     "--received-on", "2026-03-10", "--receipt-evidence", kind)  # same day as sent is valid
            entry = json.loads((root / "kit" / "sent.json").read_text(encoding="utf-8"))["requests"][agency]
            self.assertEqual((entry["received_on"], entry["receipt_evidence"]), ("2026-03-10", kind))

    def test_a_named_and_an_imported_request_can_be_recorded_and_siblings_are_untouched(self):
        others = {k: v for k, v in json.loads(self.sent.read_text(encoding="utf-8"))["requests"].items()
                  if k != kit_track._key(self.a, "ans")}
        self.received(self.a, "2026-03-04", "other", "--request", "ans")  # imported: origin stays imported
        entry = self.entry(self.a, "ans")
        self.assertIs(entry["imported"], True)
        self.assertEqual(len(entry["steps"]), 2)
        after = json.loads(self.sent.read_text(encoding="utf-8"))["requests"]
        self.assertEqual({k: v for k, v in after.items() if k != kit_track._key(self.a, "ans")}, others)

    def test_a_receipt_is_replaceable_after_response_steps_and_keeps_every_step(self):
        self.received(self.c, "2026-03-04", "portal")  # c main already has two steps
        steps = self.entry(self.c)["steps"]
        self.received(self.c, "2026-03-06", "return_receipt", "--replace")
        entry = self.entry(self.c)
        self.assertEqual((entry["received_on"], entry["receipt_evidence"]), ("2026-03-06", "return_receipt"))
        self.assertEqual(entry["steps"], steps)

    def test_refusals_change_nothing_and_create_no_files(self):
        base = ("--directory", self.root)
        bad = {
            "date only": (("kit-sent", *base, "--agency", self.a, "--date", "2026-03-10", "--channel", "email",
                           "--replace", "--received-on", "2026-03-11"), "go together"),
            "evidence only": (("kit-sent", *base, "--agency", self.a, "--date", "2026-03-10", "--channel", "email",
                               "--replace", "--receipt-evidence", "portal"), "go together"),
            "unknown enum": (("kit-received", *base, "--agency", self.a, "--on", "2026-03-11", "--evidence", "Portal"),
                             None),
            "malformed date": (("kit-received", *base, "--agency", self.a, "--on", "2026-02-30", "--evidence", "portal"),
                               "real YYYY-MM-DD"),
            "empty date": (("kit-received", *base, "--agency", self.a, "--on", "", "--evidence", "portal"),
                           "real YYYY-MM-DD"),
            "before sent": (("kit-received", *base, "--agency", self.a, "--on", "2026-03-09", "--evidence", "portal"),
                            "before the request was sent"),
            "future": (("kit-received", *base, "--agency", self.a, "--on", "2999-01-01", "--evidence", "portal"),
                       "between 2000-01-01 and today"),
            "absent request": (("kit-received", *base, "--agency", self.a, "--request", "nope", "--on", "2026-03-11",
                                "--evidence", "portal"), "no sent request"),
            "absent agency": (("kit-received", *base, "--agency", "no-such-agency", "--on", "2026-03-11",
                               "--evidence", "portal"), "no sent request"),
        }
        before_files = sorted(p.relative_to(self.root).as_posix() for p in self.root.rglob("*"))
        for name, (arguments, words) in bad.items():
            with self.subTest(name):
                if words is None:  # argparse rejects an enum value outside the choices (exit 2) before any read
                    before = self.sent.read_bytes()
                    code, _, _ = legacy.run(*arguments)
                    self.assertEqual(code, 2)
                    self.assertEqual(self.sent.read_bytes(), before)
                else:
                    self.refused(*arguments, words=words)
        self.assertEqual(sorted(p.relative_to(self.root).as_posix() for p in self.root.rglob("*")), before_files)

    def test_half_pair_to_the_api_and_a_nonstring_date_are_refused(self):
        for kwargs in ({"received_on": "2026-03-11"}, {"receipt_evidence": "portal"}):
            with self.subTest(kwargs), self.assertRaisesRegex(ValueError, "go together"):
                kit_track.record_sent(self.root, self.a, "2026-03-10", "email", replace=True, **kwargs)
        with self.assertRaisesRegex(ValueError, "real YYYY-MM-DD"):
            kit_track.record_received(self.root, self.a, 20260311, "portal")
        with self.assertRaisesRegex(ValueError, "must be one of"):
            kit_track.record_received(self.root, self.a, "2026-03-11", "")

    def test_a_second_value_needs_replace_even_when_identical(self):
        self.received(self.a, "2026-03-11", "portal")
        self.refused("kit-received", "--directory", self.root, "--agency", self.a, "--on", "2026-03-11",
                     "--evidence", "portal", words="--replace")
        self.received(self.a, "2026-03-11", "portal", "--replace")  # allowed with --replace

    def test_equal_to_today_is_valid_and_the_day_after_is_not(self):
        kit_track.record_received(self.root, self.a, "2026-03-12", "other", today=date(2026, 3, 12))
        with self.assertRaisesRegex(ValueError, "between 2000-01-01 and today"):
            kit_track.record_received(self.root, self.a, "2026-03-13", "other", today=date(2026, 3, 12), replace=True)

    def test_kit_sent_replace_keeps_the_receipt_and_refuses_a_later_sent_day_atomically(self):
        self.received(self.a, "2026-03-12", "portal")
        resend = ("kit-sent", "--directory", self.root, "--agency", self.a, "--channel", "email", "--replace")
        self.cli(*resend, "--date", "2026-03-11")  # no receipt flags: the stored receipt stays
        self.assertEqual((self.entry(self.a)["received_on"], self.entry(self.a)["receipt_evidence"]),
                         ("2026-03-12", "portal"))
        self.refused(*resend, "--date", "2026-03-13", words="receipt recorded on 2026-03-12")
        self.cli(*resend, "--date", "2026-03-13", "--received-on", "2026-03-14", "--receipt-evidence", "other")
        self.assertEqual((self.entry(self.a)["sent_on"], self.entry(self.a)["received_on"],
                          self.entry(self.a)["receipt_evidence"]), ("2026-03-13", "2026-03-14", "other"))
        self.refused(*resend, "--date", "2026-03-13", "--received-on", "2026-03-12", "--receipt-evidence", "other",
                     words="before --date")

    def test_malformed_stored_metadata_fails_closed_without_resetting_the_tracker(self):
        original = self.sent.read_text(encoding="utf-8")
        key = kit_track._key(self.a, "main")
        for name, change in {"half pair": {"received_on": "2026-03-12"},
                             "evidence only": {"receipt_evidence": "portal"},
                             "bad enum": {"received_on": "2026-03-12", "receipt_evidence": "fax"},
                             "pre-send": {"received_on": "2026-03-09", "receipt_evidence": "portal"},
                             "bad date": {"received_on": "2026-13-01", "receipt_evidence": "portal"},
                             "number": {"received_on": 5, "receipt_evidence": "portal"}}.items():
            with self.subTest(name):
                document = json.loads(original)
                document["requests"][key].update(change)
                self.sent.write_text(json.dumps(document), encoding="utf-8")
                before = self.sent.read_bytes()
                with self.assertRaisesRegex(ValueError, "sent.json unreadable"):
                    kit_track.status(self.root, today=DAY)
                self.refused("kit-received", "--directory", self.root, "--agency", self.a, "--on", "2026-03-12",
                             "--evidence", "portal", "--replace", words="unreadable")
                self.assertEqual(self.sent.read_bytes(), before)

    def test_a_symlinked_tracker_is_refused(self):
        real = self.sent.with_name("real-sent.json")
        self.sent.rename(real)
        self.sent.symlink_to(real)
        before = real.read_bytes()
        self.refused("kit-received", "--directory", self.root, "--agency", self.a, "--on", "2026-03-12",
                     "--evidence", "portal", words="unreadable")
        self.assertEqual(real.read_bytes(), before)


class ReceiptDatesTests(Base):
    def setUp(self):
        super().setUp()
        self.received(self.a, "2026-03-20", "portal")  # a main: sent 03-10, no steps
        self.received(self.a, "2026-03-04", "acknowledgement", "--request", "ans")  # answered, stated date passed
        self.received(self.b, "2026-03-04", "other", "--request", "roll")  # installment passed + download pending
        self.received(self.c, "2026-03-03", "return_receipt")  # waiting_on_you
        self.received(self.c, "2026-03-05", "portal", "--request", "dl")  # answered + download expired

    def test_ordinary_and_extended_dates_count_from_receipt_and_other_requests_do_not(self):
        rows = self.status_rows()
        a = rows[(self.a, "main")]
        self.assertEqual((a["determination_due"], a["with_extension"]),
                         (self.deadline("2026-03-20"), self.deadline("2026-03-20", extension=True)))
        self.assertNotEqual(a["determination_due"], self.deadline("2026-03-10"))
        self.assertEqual((a["received_on"], a["receipt_evidence"], a["note"]), ("2026-03-20", "portal",
                                                                                   note("2026-03-20", "portal")))
        b = rows[(self.b, "main")]  # no receipt: counted from sent_on, with no new key at all
        self.assertEqual(b["determination_due"], self.deadline("2026-03-05", extension=True))
        self.assertFalse({"received_on", "receipt_evidence", "note"} & set(b))
        legacy_b = next(r for r in self.fixture["status"]["requests"] if (r["agency_id"], r["request"]) == (self.b, "main"))
        self.assertEqual(b, legacy_b)

    def test_a_receipt_before_a_stated_date_never_moves_the_agencys_own_dates(self):
        rows = self.status_rows()
        self.assertEqual(rows[(self.a, "ans")]["stated_production_date"], "2026-03-20")
        self.assertEqual(rows[(self.b, "roll")]["next_installment"], "2026-03-20")
        downloads = rows[(self.b, "roll")]["downloads_pending"]
        self.assertEqual(downloads, next(r for r in self.fixture["status"]["requests"]
                                         if (r["agency_id"], r["request"]) == (self.b, "roll"))["downloads_pending"])

    def test_later_receipt_makes_the_request_not_past_due_and_follow_up_is_refused(self):
        row = next(r for r in self.due()["open"] if (r["agency_id"], r["request"]) == (self.a, "main"))
        self.assertEqual((row["state"], row["due"]), ("upcoming", self.deadline("2026-03-20")))
        with self.assertRaisesRegex(ValueError, "not past due"):
            kit_track.followup(self.root, self.a, AS_OF, today=DAY)
        self.assertEqual(self.due()["past_due"], 0)  # a main was the only past-due request in the fixture

    def test_due_rows_in_every_collection_carry_the_pair_and_the_note(self):
        document = self.due()
        for collection in ("open", "answered", "stated_passed"):
            for row in document[collection]:
                key = (row["agency_id"], row["request"])
                with self.subTest(collection=collection, key=key):
                    entry = self.entry(*key)
                    if "received_on" not in entry:  # no cross-entry leakage into a receipt-less request
                        self.assertFalse({"received_on", "receipt_evidence"} & set(row))
                        self.assertNotIn("receipt", row.get("note", ""))
                        continue
                    self.assertEqual((row["received_on"], row["receipt_evidence"]),
                                     (entry["received_on"], entry["receipt_evidence"]))
                    self.assertTrue(row["note"].endswith(note(entry["received_on"], entry["receipt_evidence"])))
        by_state = {(r["agency_id"], r["request"], r["state"]): r for r in document["open"]}
        waiting = by_state[(self.c, "main", "waiting_on_you")]  # composed with its own note, not replaced
        self.assertTrue(waiting["note"].startswith(kit_track.WAITING.format(waiting["since"])))
        self.assertTrue(waiting["note"].endswith(note("2026-03-03", "return_receipt")))
        download = by_state[(self.b, "roll", "download_pending")]
        self.assertIn(note("2026-03-04", "other"), download["note"])
        self.assertEqual(download["due"], "2026-03-30")
        self.assertIn("stated_date_passed", {r["state"] for r in document["stated_passed"]})
        self.assertEqual(document["stated_passed"][0]["stated"], "2026-03-20")

    def test_follow_up_draft_and_result_carry_the_note_when_the_counted_date_has_passed(self):
        self.received(self.a, "2026-03-10", "other", "--replace")  # back to a past-due date
        result = kit_track.followup(self.root, self.a, AS_OF, today=DAY)
        self.assertEqual((result["received_on"], result["receipt_evidence"]), ("2026-03-10", "other"))
        self.assertIn(note("2026-03-10", "other"), result["note"])
        draft = (self.root / result["written"]).read_text(encoding="utf-8")
        self.assertIn(note("2026-03-10", "other"), draft)
        self.assertIn("DRAFT — NOT SENT", draft)

    def test_follow_up_for_a_request_without_receipt_has_no_receipt_wording(self):
        result = kit_track.followup(self.root, self.a, "2026-04-20", today=date(2026, 4, 20), request="main")
        self.assertIn(note("2026-03-20", "portal"), result["note"])  # a main HAS a receipt here
        other = self.root.parent / "other" / "campaign"
        shutil.copytree(self.base, other)
        plain = kit_track.followup(other, self.a, AS_OF, today=DAY)
        self.assertEqual(set(plain), {"written", "due", "days_past", "note"})
        self.assertNotIn("receipt 20", (other / plain["written"]).read_text(encoding="utf-8"))

    def test_calendar_summaries_carry_the_note_unfolded_and_dates_are_the_counted_ones(self):
        out = self.root.parent / "dates.ics"
        kit_calendar.calendar(self.root, out, AS_OF, now=legacy.NOW, today=DAY)
        text = out.read_bytes().decode("utf-8")
        self.assertTrue(all(len(line.encode("utf-8")) <= 75 for line in text.split("\r\n")))
        unfolded = text.replace("\r\n ", "")
        events = {}
        for block in unfolded.split("BEGIN:VEVENT\r\n")[1:]:
            fields = dict(line.split(":", 1) for line in block.split("\r\n") if ":" in line)
            events[fields["UID"]] = fields
        determination = events[f"determination-{self.a}@campaign.invalid"]
        self.assertEqual(determination["DTSTART;VALUE=DATE"], self.deadline("2026-03-20").replace("-", ""))
        self.assertIn(note("2026-03-20", "portal"), determination["SUMMARY"])
        stated = events[f"stated-production-{self.a}#ans@campaign.invalid"]
        self.assertEqual(stated["DTSTART;VALUE=DATE"], "20260320")  # the agency's stated date, not recounted
        self.assertIn(note("2026-03-04", "acknowledgement"), stated["SUMMARY"])
        plain = events[f"determination-{self.b}@campaign.invalid"]
        self.assertNotIn("receipt", plain["SUMMARY"])  # no leakage into a receipt-less request

    def test_today_items_name_the_receipt_and_copy_the_keys_only_for_that_request(self):
        items = {(i["kind"], i["command"]): i for i in today.todo(self.root, DAY)}
        installment = next(i for i in today.todo(self.root, DAY) if i["kind"] == "request_installment_passed")
        self.assertIn(note("2026-03-04", "other"), installment["what"])
        self.assertEqual((installment["received_on"], installment["receipt_evidence"]), ("2026-03-04", "other"))
        text = today.run_cli(self.root, AS_OF)
        self.assertIn(note("2026-03-04", "other"), text)
        self.assertEqual(len(items), len(today.todo(self.root, DAY)))
        for item in today.todo(self.root, DAY):
            if "received_on" not in item:
                self.assertNotIn("receipt", item["what"])
        # the past-due a main became upcoming, so it is not emitted at all
        self.assertFalse([i for i in today.todo(self.root, DAY) if i["kind"] == "request_past_due"])

    def test_receipt_bearing_status_and_due_documents_validate_against_the_committed_schemas(self):
        schemas = REPO_ROOT / "schemas"
        law.validate(kit_track.status(self.root, today=DAY), json.loads((schemas / "kit-status.schema.json").read_text()))
        law.validate(self.due(), json.loads((schemas / "kit-due.schema.json").read_text()))

    def test_status_reminders_on_stderr_carry_the_note_and_json_stays_one_document(self):
        out, err = self.cli("kit-status", "--directory", self.root, "--request", "ans")
        self.assertEqual(json.loads(out)["requests"][0]["note"], note("2026-03-04", "acknowledgement"))
        self.assertIn("unpaid (" + note("2026-03-04", "acknowledgement") + ")", err)

    def test_without_a_law_package_dates_stay_unavailable_but_provenance_is_visible(self):
        empty = self.root.parent / "no-packages"
        empty.mkdir()
        rows = {(r["agency_id"], r["request"]): r for r in kit_track.status(self.root, today=DAY, law_base=empty)["requests"]}
        row = rows[(self.a, "main")]
        self.assertEqual((row["determination_due"], row["with_extension"]), (None, None))
        self.assertEqual(row["note"], note("2026-03-20", "portal"))
        due = kit_track.due(self.root, AS_OF, today=DAY, law_base=empty)
        self.assertIn(note("2026-03-20", "portal"), next(r for r in due["open"] if r["agency_id"] == self.a
                                                         and r["request"] == "main")["note"])


if __name__ == "__main__":
    unittest.main()
