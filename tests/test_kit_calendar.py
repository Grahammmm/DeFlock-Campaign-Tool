"""kit-calendar writes a private RFC 5545 calendar of request dates and comment deadlines. Offline, synthetic."""
import contextlib
import io
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import cli, kit_calendar, kit_track, law  # noqa: E402
from tests.test_kit_check import ready_drafts  # noqa: E402

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
HASH = "abcd" * 16  # low-entropy synthetic sha256 so the credential scan stays quiet
LINE = re.compile(r"[A-Z][A-Z0-9-]*(;[^:\r\n]*)?:[^\r\n]*")


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


def unfold(data):
    text = data.decode("utf-8")
    assert text.endswith("\r\n") and "\n" not in text.replace("\r\n", "")
    return text.replace("\r\n ", "").split("\r\n")[:-1]


def meeting(mid, deadline="2026-11-10T17:00:00-08:00", status="scheduled", participation=True):
    document = {"id": mid, "body": "Example City Council", "starts_at": "2026-11-10T18:00:00-08:00",
                "status": status}
    if participation:
        document["participation"] = {
            "in_person": True, "remote": None, "source_url": f"https://example.invalid/agenda/{mid}",
            "written_comment": {"how": "Email the clerk", "deadline": deadline} if deadline else None,
            "verified_by": "Synthetic Reviewer Name", "verified_at": "2026-10-20"}
    return document


class KitCalendarTests(unittest.TestCase):
    def setUp(self):
        offline = mock.patch("urllib.request.urlopen", side_effect=AssertionError("network used"))
        offline.start()
        self.addCleanup(offline.stop)
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-kit-calendar-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.root = self.tmp / "campaign"
        self.assertEqual(run("init", "--directory", self.root, "--name", "Synthetic", "--county", "San Luis Obispo",
                             "--state", "CA")[0], 0)
        self.assertEqual(run("kit", "--directory", self.root)[0], 0)
        drafts = sorted((self.root / "kit" / "requests").glob("*.md"))
        self.a, self.b, self.c = (path.stem for path in drafts[:3])
        ready_drafts(self.root)
        (self.root / "content" / "meetings").mkdir(parents=True)
        for name in ("site.json", "agencies.json"):  # content/ needs its own agency list beside kit/'s
            shutil.copy(REPO_ROOT / "examples" / "fictional-campaign" / "content" / name, self.root / "content")
        self.package = law.load_package("us-ca")

    def cli(self, *extra):
        return run(*extra[:1], "--directory", self.root, *extra[1:])

    def send(self, agency, on):
        self.assertEqual(self.cli("kit-sent", "--agency", agency, "--date", on, "--channel", "email")[0], 0)

    def step(self, agency, on, kind, *extra):
        code, _, err = self.cli("kit-response", "--agency", agency, "--date", on, "--kind", kind, *extra)
        self.assertEqual(code, 0, err)

    def stage(self, document):
        path = self.root / "content" / "meetings" / (document["id"] + ".json")
        path.write_text(json.dumps(document), encoding="utf-8")

    def calendar(self, name="dates.ics", as_of="2026-11-01", expect=0):
        out = self.tmp / name
        code, stdout, err = self.cli("kit-calendar", "--out", out, "--as-of", as_of)
        self.assertEqual(code, expect, err)
        return out, (json.loads(stdout) if code == 0 else err)

    def events(self, data):
        lines = unfold(data)
        depth, events = [], []
        for line in lines:
            self.assertRegex(line, LINE)
            if line.startswith("BEGIN:"):
                depth.append(line[6:])
                if line == "BEGIN:VEVENT":
                    events.append({})
            elif line.startswith("END:"):
                self.assertEqual(depth.pop(), line[4:])
            elif depth[-1] == "VEVENT":
                name, value = line.split(":", 1)
                events[-1][name] = value
        self.assertEqual(depth, [])
        self.assertEqual(lines[:4], ["BEGIN:VCALENDAR", "VERSION:2.0",
                                     "PRODID:-//DeFlock Campaign Tool//kit calendar//EN", "CALSCALE:GREGORIAN"])
        return events

    def due(self, sent_on):
        return law.deadline(self.package, sent_on).isoformat().replace("-", "")

    def test_open_requests_and_meeting_make_four_parseable_events_in_a_0600_file(self):
        self.send(self.a, "2026-03-02")
        self.send(self.b, "2026-03-05")
        self.send(self.c, "2026-03-06")
        self.step(self.c, "2026-03-12", "determination", "--expected-on", "2026-04-30")
        self.stage(meeting("council-1"))
        out, report = self.calendar()
        self.assertEqual(report, {"written": str(out), "events": 4})
        self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o600)
        events = self.events(out.read_bytes())
        self.assertEqual([e["UID"] for e in events], [
            f"determination-{self.a}@campaign.invalid", f"determination-{self.b}@campaign.invalid",
            f"stated-production-{self.c}@campaign.invalid", "meeting-comment-council-1@campaign.invalid"])
        self.assertEqual([e.get("DTSTART;VALUE=DATE") for e in events[:3]],
                         [self.due("2026-03-02"), self.due("2026-03-05"), "20260430"])
        self.assertTrue(events[2]["SUMMARY"].startswith("Records request: agency's stated production date — "))
        self.assertEqual(events[3]["DTSTART"], "20261111T010000Z")
        self.assertEqual(events[3]["DURATION"], "PT0M")
        self.assertEqual(events[3]["SUMMARY"], "Written comment deadline — Example City Council")
        self.assertEqual(events[3]["URL"], "https://example.invalid/agenda/council-1")
        self.assertTrue(all(re.fullmatch(r"\d{8}T\d{6}Z", e["DTSTAMP"]) for e in events))

    def test_closed_answered_cancelled_past_and_unverified_are_left_out(self):
        self.send(self.a, "2026-03-02")
        self.step(self.a, "2026-03-04", "acknowledged")
        self.step(self.a, "2026-03-09", "closed")
        self.send(self.b, "2026-03-05")
        self.step(self.b, "2026-03-10", "determination")
        self.send(self.c, "2026-03-06")
        self.step(self.c, "2026-03-12", "determination", "--expected-on", "2026-04-30")
        self.stage(meeting("cancelled-1", status="cancelled"))
        self.stage(meeting("past-1", deadline="2026-10-30T17:00:00-07:00"))
        self.stage(meeting("bare-1", participation=False))
        self.stage(meeting("no-written-1", deadline=None))
        out, report = self.calendar()
        self.assertEqual(report["events"], 1)
        self.assertEqual([e["UID"] for e in self.events(out.read_bytes())],
                         [f"stated-production-{self.c}@campaign.invalid"])

    def test_deadline_on_the_as_of_day_is_kept_and_an_empty_calendar_is_valid(self):
        self.stage(meeting("council-1"))
        out, report = self.calendar(as_of="2026-11-10")
        self.assertEqual(report["events"], 1)
        out, report = self.calendar("empty.ics", as_of="2026-11-11")
        self.assertEqual(report, {"written": str(out), "events": 0})
        self.assertEqual(self.events(out.read_bytes()), [])
        self.assertTrue(unfold(out.read_bytes())[-1] == "END:VCALENDAR")

    def test_agency_name_is_escaped(self):
        path = self.root / "kit" / "agencies.json"
        agencies = json.loads(path.read_text(encoding="utf-8"))
        for entry in agencies:
            if entry["agency_id"] == self.a:
                entry["name"] = "Police; Records, Unit\\Desk\nSecond line"
        path.write_text(json.dumps(agencies), encoding="utf-8")
        self.send(self.a, "2026-03-02")
        out, _ = self.calendar()
        summary = self.events(out.read_bytes())[0]["SUMMARY"]
        self.assertEqual(summary, "Records request: provisional determination date — "
                                  "Police\\; Records\\, Unit\\\\Desk\\nSecond line")

    def test_long_non_ascii_summary_is_folded_at_75_octets_without_splitting_utf8(self):
        text = "Plazo de comentario — Concejo Municipal de Año Nuevo ñandú 🦜 " * 4
        text = text[:200]
        self.assertEqual(len(text), 200)
        event = {"uid": "meeting-comment-x@campaign.invalid", "day": "2026-11-11", "summary": text}
        data = kit_calendar.render([event], NOW).encode("utf-8")
        physical = data.split(b"\r\n")[:-1]
        self.assertGreater(len(physical), 8)
        for line in physical:
            self.assertLessEqual(len(line), 75)
            line.decode("utf-8")  # each physical line is whole UTF-8 on its own
        summary = [line for line in unfold(data) if line.startswith("SUMMARY:")]
        self.assertEqual(summary, ["SUMMARY:" + kit_calendar.escape_text(text)])
        self.assertTrue(any(line.startswith(b" ") for line in physical))

    def assert_refused(self, out, words):
        before = sorted(str(p) for p in self.tmp.rglob("*"))
        code, stdout, err = self.cli("kit-calendar", "--out", out, "--as-of", "2026-11-01")
        self.assertEqual((code, stdout), (3, ""), err)
        self.assertTrue(err.startswith("Stopped: "), err)
        self.assertIn(words, err)
        self.assertEqual(sorted(str(p) for p in self.tmp.rglob("*")), before, "nothing may be written")

    def test_unsafe_targets_are_refused_and_nothing_is_written(self):
        (self.root / "public" / "sub").mkdir(parents=True)
        (self.root / "content" / "sub").mkdir()
        self.assert_refused(self.root / "public" / "dates.ics", "public/")
        self.assert_refused(self.root / "public" / "sub" / "dates.ics", "public/")
        self.assert_refused(self.root / "content" / "dates.ics", "content/")
        (self.tmp / "to-content").symlink_to(self.root / "content")
        self.assert_refused(self.tmp / "to-content" / "dates.ics", "content/")
        self.assert_refused(self.tmp / "to-content" / "sub" / "dates.ics", "content/")
        (self.tmp / "elsewhere").mkdir()
        (self.tmp / "linked").symlink_to(self.tmp / "elsewhere")
        self.assert_refused(self.tmp / "linked" / "dates.ics", "symbolic link")
        self.assert_refused(self.tmp / "missing" / "dates.ics", "existing directory")
        self.assert_refused(self.tmp / "dates.txt", ".ics")
        self.assert_refused(self.tmp / "dates.ics.bak", ".ics")
        existing = self.tmp / "dates.ics"
        existing.write_text("mine", encoding="utf-8")
        self.assert_refused(existing, "exists")
        self.assertEqual(existing.read_text(encoding="utf-8"), "mine")
        (self.tmp / "dangling.ics").symlink_to(self.tmp / "nowhere.ics")
        self.assert_refused(self.tmp / "dangling.ics", "exists")
        self.assertFalse((self.tmp / "nowhere.ics").exists())
        self.assertEqual(self.calendar(as_of="2026-13-01", name="x.ics", expect=3)[1].splitlines()[0],
                         "Stopped: --as-of must be a real YYYY-MM-DD date")

    def test_without_a_law_package_only_stated_dates_are_listed_and_the_output_says_so(self):
        self.send(self.a, "2026-03-02")
        self.send(self.c, "2026-03-06")
        self.step(self.c, "2026-03-12", "determination", "--expected-on", "2026-04-30")
        report = kit_calendar.calendar(self.root, self.tmp / "nolaw.ics", "2026-11-01", now=NOW,
                                       law_base=self.tmp / "no-law-packages")
        self.assertEqual(report["events"], 1)
        self.assertIn("No law package", report["note"])
        self.assertEqual([e["UID"] for e in self.events((self.tmp / "nolaw.ics").read_bytes())],
                         [f"stated-production-{self.c}@campaign.invalid"])
        self.assertNotIn("note", self.calendar()[1])

    def test_no_recipient_request_text_note_hash_or_reviewer_in_the_file(self):
        self.send(self.a, "2026-03-02")
        self.step(self.a, "2026-03-04", "acknowledged", "--note", "synthetic private note words")
        sent = self.root / "kit" / "sent.json"
        document = json.loads(sent.read_text(encoding="utf-8"))
        document["requests"][self.a]["steps"].append({"on": "2026-03-20", "kind": "produced", "records": [HASH]})
        sent.write_text(json.dumps(document), encoding="utf-8")
        self.stage(meeting("council-1"))
        out, report = self.calendar()
        self.assertEqual(report["events"], 1)
        text = out.read_bytes().decode("utf-8").replace("\r\n ", "")
        draft = (self.root / "kit" / "requests" / f"{self.a}.md").read_text(encoding="utf-8")
        for secret in ("records@example.org", "synthetic value", "synthetic private note", HASH[:12],
                       "Synthetic Reviewer Name", "Email the clerk", *[ln for ln in draft.splitlines() if len(ln) > 30]):
            self.assertNotIn(secret, text)

    def test_same_inputs_and_now_give_byte_identical_files(self):
        self.send(self.a, "2026-03-02")
        self.stage(meeting("council-1"))
        for name in ("one.ics", "two.ics"):
            kit_calendar.calendar(self.root, self.tmp / name, "2026-11-01", now=NOW)
        self.assertEqual((self.tmp / "one.ics").read_bytes(), (self.tmp / "two.ics").read_bytes())
        self.assertIn(b"DTSTAMP:20261101T120000Z\r\n", (self.tmp / "one.ics").read_bytes())

    def test_not_a_campaign_folder_is_stopped(self):
        os.mkdir(self.tmp / "plain")
        code, _, err = run("kit-calendar", "--directory", self.tmp / "plain", "--out", self.tmp / "p.ics")
        self.assertNotEqual(code, 0)
        self.assertFalse((self.tmp / "p.ics").exists())

    # --- request-event identity: agency and request components stay separate (RF1100-22) ---

    ACK = [{"kind": "acknowledged", "on": "2026-03-03"}]  # open request: a provisional determination event
    STATED = [{"kind": "determination", "on": "2026-03-03", "expected_on": "2026-11-10"}]  # a stated production event

    def write_sent(self, pairs, steps=None):
        """Synthetic kit/sent.json for (agency, request[, sent_on]) pairs; the real validators must accept every key."""
        requests = {}
        for agency, request, *rest in pairs:
            sent_on = rest[0] if rest else "2026-03-02"
            made = json.loads(json.dumps(steps or self.ACK))
            for step in made:
                step["on"] = max(step["on"], sent_on)  # a step is never dated before its request was sent
            requests[kit_track._key(agency, request)] = {
                "sent_on": sent_on, "channel": "email",
                "draft": kit_track._draft_path(agency, request), "draft_sha256": HASH,
                "recorded_at": "2026-03-02T12:00:00+00:00", "steps": made}
        path = self.root / "kit" / "sent.json"
        path.write_text(json.dumps({"schema_version": 1, "requests": requests}), encoding="utf-8")
        for key, entry in kit_track.read_sent(self.root).items():
            kit_track.validate_steps(entry, key)
        return path

    def export(self, name):
        out = self.tmp / name
        report = kit_calendar.calendar(self.root, out, "2026-11-01", now=NOW)
        return out, report, self.events(out.read_bytes())

    def test_uid_main_and_named_hyphen_collision(self):
        for steps, kind in ((self.ACK, "determination"), (self.STATED, "stated-production")):
            sent = self.write_sent([("ca-example-records", "main"), ("ca-example", "records")], steps)
            before = sent.read_bytes()
            out, report, events = self.export(kind + ".ics")
            self.assertEqual(report["events"], 2)
            self.assertEqual([e["UID"] for e in events], [f"{kind}-ca-example#records@campaign.invalid",
                                                          f"{kind}-ca-example-records@campaign.invalid"])
            self.assertEqual([e["SUMMARY"].rsplit(" — ", 1)[1] for e in events],
                             ["ca-example (records)", "ca-example-records"])
            self.assertEqual(sent.read_bytes(), before)

    def test_uid_two_named_hyphen_collision(self):
        self.write_sent([("ca-example", "records-extra", "2026-03-02"), ("ca-example-records", "extra", "2026-03-09")])
        out, report, events = self.export("two.ics")
        self.assertEqual(report["events"], 2)
        self.assertEqual([(e["UID"], e["DTSTART;VALUE=DATE"]) for e in events], [
            ("determination-ca-example#records-extra@campaign.invalid", self.due("2026-03-02")),
            ("determination-ca-example-records#extra@campaign.invalid", self.due("2026-03-09"))])
        self.assertEqual([e["SUMMARY"].rsplit(" — ", 1)[1] for e in events],
                         ["ca-example (records-extra)", "ca-example-records (extra)"])

    def test_uid_tuple_boundaries_and_kind_are_unique(self):
        agencies = ["ca-a", "ca-a-b", "ca-a-b-c", "ca-1", "ca-1-2", "ca-" + "x" * 36]
        requests = ["main", "b", "b-c", "c", "1", "1-2", "2", "a-b", "x" * 40, "0---9"]
        seen = {}
        for kind in ("determination", "stated-production"):
            for agency in agencies:
                for request in requests:
                    row = {"agency_id": agency, "request": request}
                    uid = kit_calendar._uid(kind, row)
                    self.assertEqual(uid, kit_calendar._uid(kind, dict(row)))
                    self.assertNotIn(uid, seen, f"{(kind, agency, request)} collides with {seen.get(uid)}")
                    seen[uid] = (kind, agency, request)
                    self.assertTrue(uid.endswith("@campaign.invalid"))
                    self.assertNotIn("%", uid)
        self.assertEqual(len(seen), 2 * len(agencies) * len(requests))
        self.assertEqual(kit_calendar._uid("determination", {"agency_id": "ca-a-b", "request": "main"}),
                         "determination-ca-a-b@campaign.invalid")
        self.assertEqual(kit_calendar._uid("stated-production", {"agency_id": "ca-a", "request": "1-2"}),
                         "stated-production-ca-a#1-2@campaign.invalid")

    def test_uid_stable_when_other_requests_and_display_values_change(self):
        self.write_sent([("ca-example", "records-extra", "2026-03-02"), ("ca-example-records", "extra", "2026-03-09")])
        _, _, first = self.export("first.ics")
        kept = "determination-ca-example#records-extra@campaign.invalid"
        self.assertIn(kept, [e["UID"] for e in first])
        # one request gone, another added, the survivor's sent date moved and its agency renamed
        self.write_sent([("ca-example", "records-extra", "2026-03-04"), ("ca-example-other", "main", "2026-03-05")])
        path = self.root / "kit" / "agencies.json"
        agencies = json.loads(path.read_text(encoding="utf-8"))
        agencies.append({"agency_id": "ca-example", "name": "Renamed Synthetic Agency"})
        path.write_text(json.dumps(agencies), encoding="utf-8")
        _, _, second = self.export("second.ics")
        self.assertEqual(sorted(e["UID"] for e in second),
                         sorted([kept, "determination-ca-example-other@campaign.invalid"]))
        moved = next(e for e in second if e["UID"] == kept)
        self.assertEqual(moved["DTSTART;VALUE=DATE"], self.due("2026-03-04"))
        self.assertIn("Renamed Synthetic Agency (records-extra)", moved["SUMMARY"])
        for name in ("third.ics", "fourth.ics"):
            kit_calendar.calendar(self.root, self.tmp / name, "2026-11-01", now=NOW)
        self.assertEqual((self.tmp / "third.ics").read_bytes(), (self.tmp / "fourth.ics").read_bytes())

    def test_named_uid_survives_folding_and_remains_private(self):
        agency = "ca-" + "long-agency-" * 6 + "end"
        request = "r" + "-segment" * 4 + "-" + "9" * 6
        self.assertEqual(len(request), 40)
        sent = self.write_sent([(agency, request)])
        before = sent.read_bytes()
        out, report, events = self.export("long.ics")
        self.assertEqual(report["events"], 1)
        uid = f"determination-{agency}#{request}@campaign.invalid"
        self.assertGreater(len("UID:" + uid), 75)
        self.assertEqual([e["UID"] for e in events], [uid])
        data = out.read_bytes()
        physical = data.split(b"\r\n")[:-1]
        self.assertTrue(all(len(line) <= 75 for line in physical))
        self.assertTrue(any(line.startswith(b" ") for line in physical))
        self.assertIn("UID:" + uid, unfold(data))
        self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o600)
        text = data.decode("utf-8").replace("\r\n ", "")
        for secret in (HASH, HASH[:12], "records@example.org", "sent.json", "requests/"):
            self.assertNotIn(secret, text)
        self.assertEqual(sent.read_bytes(), before)
        self.assert_refused(out, "exists")

    # --- agency-stated dates: pending download expiry, next installment, stated extension (DF1109-04) ---

    def write_requests(self, requests):
        """Synthetic kit/sent.json of {(agency, request): steps}, each sent 2026-03-02; the validators accept it."""
        path = self.write_sent(list(requests))
        document = json.loads(path.read_text(encoding="utf-8"))
        for (agency, request), steps in requests.items():
            document["requests"][kit_track._key(agency, request)]["steps"] = steps
        path.write_text(json.dumps(document), encoding="utf-8")
        for key, entry in kit_track.read_sent(self.root).items():
            kit_track.validate_steps(entry, key)
        return path

    def stated(self, steps, request=("ca-example", "main"), name="s.ics", law_base=None):
        """The new-kind events {uid: (day, summary)} of a one-request campaign, after the real export."""
        self.write_requests({request: steps})
        out = self.tmp / name
        kit_calendar.calendar(self.root, out, "2026-11-01", now=NOW, law_base=law_base)
        events = self.events(out.read_bytes())
        self.assertEqual(len({e["UID"] for e in events}), len(events), "no duplicate UID")
        return {e["UID"]: (e["DTSTART;VALUE=DATE"], e["SUMMARY"]) for e in events
                if e["UID"].startswith(NEW_KINDS)}

    def test_pending_download_events_follow_completion_stack(self):
        steps = [{"kind": "acknowledged", "on": "2026-03-03"},
                 {"kind": "produced", "on": "2026-03-06", "partial": True, "download_expires_on": "2026-03-25"},
                 {"kind": "produced", "on": "2026-03-06", "partial": True, "download_expires_on": "2026-03-25"}]
        uid = "download-expiry-ca-example-step{}@campaign.invalid".format
        summary = "Records request: download link expiry — ca-example (" + STATED_ESCAPED + ")"
        self.assertEqual(self.stated(steps, name="1.ics"),
                         {uid(1): ("20260325", summary), uid(2): ("20260325", summary)})  # same day, two events
        steps.append({"kind": "produced", "on": "2026-03-09", "partial": True, "records": [HASH]})  # clears step 2
        self.assertEqual(self.stated(steps, name="2.ics"), {uid(1): ("20260325", summary)})
        steps.append({"kind": "fee_estimate", "on": "2026-03-10", "amount_cents": 500})  # renumbers nothing
        self.assertEqual(self.stated(steps, name="3.ics"), {uid(1): ("20260325", summary)})
        expiring = {"kind": "produced", "on": "2026-03-12", "partial": True, "download_expires_on": "2026-03-30"}
        self.assertEqual(sorted(self.stated([*steps, {**expiring, "records": [OTHER]}], name="4.ics")), [],
                         "a linked step clears step 1 and never yields its own expiry")
        self.assertEqual(sorted(self.stated([*steps, expiring], name="5.ics")), [uid(1), uid(5)])
        status = kit_track.status(self.root)["requests"][0]["downloads_pending"]
        self.assertEqual([d["expires_on"] for d in status], ["2026-03-25", "2026-03-30"])  # same as kit-status

    def test_next_installment_uses_latest_partial_and_restated_date(self):
        uid = "next-installment-ca-example@campaign.invalid"
        summary = "Records request: next installment date — ca-example (" + STATED_ESCAPED + ")"
        steps = [{"kind": "produced", "on": "2026-03-05", "partial": True, "expected_on": "2026-04-01"}]
        self.assertEqual(self.stated(steps, name="1.ics"), {uid: ("20260401", summary)})  # a past date is kept
        steps.append({"kind": "produced", "on": "2026-03-10", "partial": True})
        self.assertEqual(self.stated(steps, name="2.ics"), {}, "a newer partial without a date ends the old one")
        steps.append({"kind": "produced", "on": "2026-03-12", "partial": True, "expected_on": "2026-11-15",
                      "download_expires_on": "2026-11-15"})
        self.assertEqual(self.stated(steps, name="3.ics"), {
            uid: ("20261115", summary),
            "download-expiry-ca-example-step2@campaign.invalid": (
                "20261115", "Records request: download link expiry — ca-example (" + STATED_ESCAPED + ")")})
        final = self.stated([*steps, {"kind": "produced", "on": "2026-03-20", "records": [HASH]}], name="4.ics")
        self.assertEqual(final, {}, "a final linked production ends the installment and clears the download")
        denied = self.stated([*steps, {"kind": "denied", "on": "2026-03-20"}], name="5.ics")
        self.assertEqual(sorted(denied), ["download-expiry-ca-example-step2@campaign.invalid"])

    def test_extension_stated_date_only_before_answer(self):
        uid = "stated-extension-ca-example@campaign.invalid"
        summary = "Records request: stated extension date — ca-example (" + STATED_ESCAPED + ")"
        notice = {"kind": "extension", "on": "2026-03-10", "extension_expected_on": "2026-04-20"}
        self.assertEqual(self.stated([notice], name="1.ics"), {uid: ("20260420", summary)})
        steps = [{"kind": "acknowledged", "on": "2026-03-09"},
                 {**notice, "extension_reason": "synthetic backlog reason"},
                 {"kind": "clarification_requested", "on": "2026-03-12"},
                 {"kind": "fee_estimate", "on": "2026-03-13", "amount_cents": 500}]
        self.assertEqual(self.stated(steps, name="2.ics"), {uid: ("20260420", summary)})
        self.assertNotIn("backlog", (self.tmp / "2.ics").read_text(encoding="utf-8"))
        nolaw = self.stated(steps, name="3.ics", law_base=self.tmp / "no-law-packages")
        self.assertEqual(nolaw, {uid: ("20260420", summary)}, "the stated date, never law arithmetic")
        for answer in ({"kind": "determination", "on": "2026-03-20"}, {"kind": "denied", "on": "2026-03-20"},
                       {"kind": "produced", "on": "2026-03-20", "partial": True}):
            self.assertEqual(self.stated([*steps, answer], name=answer["kind"] + ".ics"), {}, answer)
        reason_only = {"kind": "extension", "on": "2026-03-10", "extension_reason": "synthetic backlog reason"}
        self.assertEqual(self.stated([reason_only], name="4.ics"), {})

    def test_closed_and_withdrawn_suppress_every_new_kind(self):
        rolling = [{"kind": "produced", "on": "2026-03-05", "partial": True, "expected_on": "2026-04-01",
                    "download_expires_on": "2026-04-02"}]
        extended = [{"kind": "extension", "on": "2026-03-05", "extension_expected_on": "2026-04-03"}]
        requests = {("ca-example", "main"): rolling, ("ca-example", "r-2"): extended,
                    ("ca-other", "main"): rolling, ("ca-other", "r-2"): extended}
        self.stage(meeting("council-1"))
        self.write_requests(requests)
        _, report, events = self.export("open.ics")
        self.assertEqual(sum(e["UID"].startswith(NEW_KINDS) for e in events), 6)  # the control: all four would add
        ending = {"ca-example": {"kind": "closed", "on": "2026-03-09"},
                  "ca-other": {"kind": "withdrawn", "on": "2026-03-09", "note": "synthetic withdrawal reason"}}
        self.write_requests({key: [*steps, ending[key[0]]] for key, steps in requests.items()})
        _, report, events = self.export("ended.ics")
        self.assertEqual([e["UID"] for e in events], ["meeting-comment-council-1@campaign.invalid"])
        self.assertEqual(report["events"], 1)

    def test_new_uids_are_repeatable_and_noncolliding(self):
        rolling = [{"kind": "produced", "on": "2026-03-05", "partial": True, "expected_on": "2026-04-01",
                    "download_expires_on": "2026-04-01"}]
        self.write_requests({("ca-example-records", "main"): rolling, ("ca-example", "records"): rolling,
                             ("ca-example", "records-1"): rolling})
        _, report, events = self.export("one.ics")
        self.assertEqual(report["events"], 6)
        self.assertEqual([e["UID"] for e in events], [
            "download-expiry-ca-example#records-1-step0@campaign.invalid",
            "download-expiry-ca-example#records-step0@campaign.invalid",
            "download-expiry-ca-example-records-step0@campaign.invalid",
            "next-installment-ca-example#records-1@campaign.invalid",
            "next-installment-ca-example#records@campaign.invalid",
            "next-installment-ca-example-records@campaign.invalid"])
        self.assertEqual(len({e["UID"] for e in events}), 6)
        kit_calendar.calendar(self.root, self.tmp / "two.ics", "2026-11-01", now=NOW)
        self.assertEqual((self.tmp / "one.ics").read_bytes(), (self.tmp / "two.ics").read_bytes())

    def test_new_dates_without_law_and_no_private_fields(self):
        path = self.root / "kit" / "agencies.json"
        agencies = json.loads(path.read_text(encoding="utf-8"))
        agencies.append({"agency_id": "ca-example", "name": "Departamento de Policía de Año Nuevo, Ñandú 🦜"})
        path.write_text(json.dumps(agencies), encoding="utf-8")
        self.write_requests({
            ("ca-example", "main"): [{"kind": "produced", "on": "2026-03-05", "partial": True, "records": [HASH],
                                      "expected_on": "2026-04-01"}],
            ("ca-example", "dl"): [{"kind": "produced", "on": "2026-03-05", "download_expires_on": "2026-04-02",
                                    "note": "synthetic private note words"}],
            ("ca-example", "ext"): [{"kind": "extension", "on": "2026-03-05", "extension_expected_on": "2026-04-03",
                                     "extension_reason": "synthetic reason words"}]})
        report = kit_calendar.calendar(self.root, self.tmp / "nolaw.ics", "2026-11-01", now=NOW,
                                       law_base=self.tmp / "no-law-packages")
        self.assertEqual(report["note"], "No law package: provisional determination dates cannot be computed; "
                                         "only agency-stated dates are included.")
        data = (self.tmp / "nolaw.ics").read_bytes()
        self.assertTrue(all(len(line) <= 75 for line in data.split(b"\r\n")))
        events = self.events(data)
        label = "Departamento de Policía de Año Nuevo\\, Ñandú 🦜"
        self.assertEqual([(e["UID"], e["DTSTART;VALUE=DATE"], e["SUMMARY"]) for e in events], [
            ("next-installment-ca-example@campaign.invalid", "20260401",
             f"Records request: next installment date — {label} ({STATED_ESCAPED})"),
            ("download-expiry-ca-example#dl-step0@campaign.invalid", "20260402",
             f"Records request: download link expiry — {label} (dl) ({STATED_ESCAPED})"),
            ("stated-extension-ca-example#ext@campaign.invalid", "20260403",
             f"Records request: stated extension date — {label} (ext) ({STATED_ESCAPED})")])
        self.assertEqual(report["events"], 3)
        text = data.decode("utf-8").replace("\r\n ", "")
        for secret in (HASH, HASH[:12], "synthetic private note", "synthetic reason", "records@example.org", "URL:"):
            self.assertNotIn(secret, text)

    def test_no_new_dates_preserve_prechange_bytes(self):
        self.write_requests({
            ("ca-example", "main"): [{"kind": "acknowledged", "on": "2026-03-03"}],
            ("ca-example", "b-2"): [{"kind": "determination", "on": "2026-03-03", "expected_on": "2026-11-10"}],
            ("ca-other", "main"): [{"kind": "produced", "on": "2026-03-04", "records": [HASH],
                                    "download_expires_on": "2026-03-20"}],
            ("ca-third", "main"): [{"kind": "extension", "on": "2026-03-04", "extension_reason": "synthetic reason"}]})
        self.stage(meeting("council-1"))
        kit_calendar.calendar(self.root, self.tmp / "golden.ics", "2026-11-01", now=NOW)
        self.assertEqual((self.tmp / "golden.ics").read_bytes(), PRECHANGE)

    def test_bad_input_writes_nothing_and_export_writes_only_the_target(self):
        def snapshot():
            return {str(p): (p.read_bytes() if p.is_file() else None, stat.S_IMODE(p.lstat().st_mode))
                    for p in sorted(self.tmp.rglob("*"))}
        rolling = [{"kind": "produced", "on": "2026-03-05", "partial": True, "expected_on": "2026-04-01",
                    "download_expires_on": "2026-04-02"}]
        sent = self.write_requests({("ca-example", "main"): rolling})
        before = snapshot()
        out, _ = self.calendar("ok.ics")
        after = snapshot()
        self.assertEqual(stat.S_IMODE(after.pop(str(out))[1]), 0o600)
        self.assertEqual(after, before)
        for bad in ({"download_expires_on": "2026-03-01"}, {"download_expires_on": "2026-02-30"},
                    {"expected_on": "soon"}, {"partial": "yes"}):
            document = json.loads(sent.read_text(encoding="utf-8"))
            document["requests"]["ca-example"]["steps"] = [{**rolling[0], **bad}]
            sent.write_text(json.dumps(document), encoding="utf-8")
            self.assert_refused(self.tmp / "bad.ics", "malformed steps")
        extension = {"kind": "acknowledged", "on": "2026-03-05", "extension_expected_on": "2026-04-03"}
        document["requests"]["ca-example"]["steps"] = [extension]
        sent.write_text(json.dumps(document), encoding="utf-8")
        self.assert_refused(self.tmp / "bad.ics", "malformed steps")
        self.assert_refused(out, "exists")


NEW_KINDS = ("download-expiry-", "next-installment-", "stated-extension-")
STATED_ESCAPED = "agency-stated\\; provisional\\, not a legal deadline"
OTHER = "dcba" * 16
PRECHANGE = (  # main's export of test_no_new_dates_preserve_prechange_bytes before DF1109-04, captured unchanged
    b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//DeFlock Campaign Tool//kit calendar//EN\r\nCALSCALE:GREGORIAN\r\n"
    b"BEGIN:VEVENT\r\nUID:determination-ca-example@campaign.invalid\r\nDTSTAMP:20261101T120000Z\r\n"
    b"DTSTART;VALUE=DATE:20260312\r\n"
    b"SUMMARY:Records request: provisional determination date \xe2\x80\x94 ca-example\r\nEND:VEVENT\r\n"
    b"BEGIN:VEVENT\r\nUID:determination-ca-third@campaign.invalid\r\nDTSTAMP:20261101T120000Z\r\n"
    b"DTSTART;VALUE=DATE:20260326\r\n"
    b"SUMMARY:Records request: provisional determination date \xe2\x80\x94 ca-third\r\nEND:VEVENT\r\n"
    b"BEGIN:VEVENT\r\nUID:stated-production-ca-example#b-2@campaign.invalid\r\nDTSTAMP:20261101T120000Z\r\n"
    b"DTSTART;VALUE=DATE:20261110\r\n"
    b"SUMMARY:Records request: agency's stated production date \xe2\x80\x94 ca-example (b-\r\n 2)\r\nEND:VEVENT\r\n"
    b"BEGIN:VEVENT\r\nUID:meeting-comment-council-1@campaign.invalid\r\nDTSTAMP:20261101T120000Z\r\n"
    b"DTSTART:20261111T010000Z\r\nDURATION:PT0M\r\n"
    b"SUMMARY:Written comment deadline \xe2\x80\x94 Example City Council\r\n"
    b"URL:https://example.invalid/agenda/council-1\r\nEND:VEVENT\r\n"
    b"END:VCALENDAR\r\n")


if __name__ == "__main__":
    unittest.main()
