"""DF2037-12: private correction-request log (add / decide / list; doctor names requests open over 14 days).

Synthetic campaign published via findings.publish (DF2237-02 helpers). Nothing is sent or published.
Run from the repository root: python3 -B -m unittest tests.test_correction_requests -v
"""
import json
import os
import stat
import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import correction_requests as cr, findings  # noqa: E402
from campaign_tool.cli import doctor_report  # noqa: E402
from campaign_tool.site import build_site  # noqa: E402
from tests import test_findings_publish as publish_tests  # noqa: E402  (module import: not re-collected here)

NOW = publish_tests.NOW  # published 2026-03-04
TODAY = date(2026, 3, 20)
FID = "syn-pub-1"


class CorrectionRequestTests(unittest.TestCase):
    _p = vars(publish_tests.FindingPublishTests)
    setUp_campaign, cli, approve_all = _p["setUp"], _p["cli"], _p["approve_all"]
    register, write_draft, receipt = _p["register"], _p["write_draft"], _p["receipt"]  # used by setUp_campaign

    def setUp(self):
        self.setUp_campaign()
        self.approve_all()
        findings.publish(self.root, self.draft, "Pat Organizer", now=NOW)
        self.private = self.root / "private"
        self.file = self.private / "correction-requests.json"

    def add(self, received="2026-03-06", summary="Page 2 is misquoted.", finding_id=FID, today=TODAY):
        return cr.add(self.root, finding_id, received, summary, today=today)

    def decide(self, request_id=1, decision="declined", on="2026-03-08", note="Records support the text.", today=TODAY):
        return cr.decide(self.root, request_id, decision, on, note, today=today)

    def correct(self, day):
        findings.correct(self.root, FID, day, "Fixed the quote.", now=datetime(2026, 3, 19, tzinfo=timezone.utc),
                          approved_by="Pat Organizer")

    def snapshot(self):
        return {str(p.relative_to(self.private)): p.read_bytes() for p in sorted(self.private.rglob("*")) if p.is_file()}

    def refused(self, needle, call, *args, **kwargs):
        before = self.file.read_bytes() if self.file.exists() else None
        with self.assertRaises(ValueError) as caught:
            call(*args, **kwargs)
        self.assertIn(needle, str(caught.exception))
        self.assertEqual(self.file.read_bytes() if self.file.exists() else None, before)
        return str(caught.exception)

    def test_add_via_cli_writes_only_the_0600_log_and_list_shows_days_open(self):
        before = self.snapshot()
        result = self.cli("correction-request", "add", "--directory", self.root, "--finding", FID,
                          "--received", "2026-03-05", "--summary", "Page 2 is misquoted.")
        lines = result.stdout.splitlines()
        self.assertEqual(json.loads("\n".join(lines[:-1])), {"id": 1, "finding_id": FID, "received_on": "2026-03-05",
                                                              "summary": "Page 2 is misquoted.", "state": "open"})
        self.assertEqual(lines[-1], "Private log only; nothing was sent or published.")
        self.assertEqual(stat.S_IMODE(self.file.stat().st_mode), 0o600)
        after = self.snapshot()
        self.assertEqual(set(after) - set(before), {"correction-requests.json", ".correction-requests.lock"})
        marker = self.private / ".correction-requests.lock"
        self.assertEqual(stat.S_IMODE(marker.stat().st_mode), 0o600)
        self.assertEqual(marker.read_bytes(), b"")
        self.assertEqual({k: v for k, v in after.items() if k in before}, before)
        self.assertEqual(self.add()["id"], 2)
        self.decide(request_id=1)
        self.assertEqual([(r["id"], r["state"], r.get("days_open")) for r in cr.list_requests(self.root, TODAY)],
                         [(2, "open", 14), (1, "declined", None)])
        listed = self.cli("correction-request", "list", "--directory", self.root).stdout
        self.assertEqual([r["id"] for r in json.loads(listed.rsplit("\n", 2)[0])], [2, 1])
        self.assertEqual(json.loads(self.file.read_text())["schema_version"], 1)

    def test_add_refuses_unknown_draft_withdrawn_and_bad_dates(self):
        drafts = self.private / "drafts"
        drafts.mkdir(exist_ok=True)
        (drafts / "fnd_syn_draft_2.json").write_text(json.dumps(publish_tests.draft(id="syn-draft-2")), encoding="utf-8")
        self.refused("no published finding nope", self.add, finding_id="nope")
        self.refused("no published finding syn-draft-2", self.add, finding_id="syn-draft-2")
        self.refused("is in the future", self.add, received="2026-03-21")
        self.refused("before the finding's publication date 2026-03-04", self.add, received="2026-03-03")
        self.refused("not a real calendar date", self.add, received="2026-02-30")
        self.refused("not a real calendar date", self.add, received="2026-3-5")
        self.assertFalse(self.file.exists())
        result = self.cli("correction-request", "add", "--directory", self.root, "--finding", "nope",
                          "--received", "2026-03-05", "--summary", "x", returncode=3)
        self.assertIn("no published finding nope", result.stderr)
        self.add()
        findings.withdraw(self.root, FID, "Synthetic withdrawal.")
        self.refused("no published finding syn-pub-1", self.add)
        self.refused("no published finding syn-pub-1", self.decide, decision="accepted")

    def test_summary_rules(self):
        for bad, needle in (("Ask me at someone@example.org please", "requester contact stays out of this log"),
                            ("   \n\t ", "1 to 500 characters"), ("x" * 501, "1 to 500 characters")):
            self.refused(needle, self.add, summary=bad)
        self.assertFalse(self.file.exists())
        self.assertEqual(self.add(summary="Line one\n  line\ttwo ")["summary"], "Line one line two")
        self.assertEqual(len(self.add(summary="y" * 500)["summary"]), 500)
        self.refused("requester contact stays out of this log", self.decide, note="Replied to a@b.example.com")

    def test_accepted_needs_a_correction_dated_on_or_after_the_request(self):
        self.add(received="2026-03-06")
        message = self.refused("accept only after recording the correction: finding correct syn-pub-1",
                               self.decide, decision="accepted")
        self.assertIn("2026-03-06 or later", message)
        self.correct("2026-03-05")  # before the request: does not count
        self.refused("accept only after recording", self.decide, decision="accepted")
        self.correct("2026-03-06")  # same day as the request
        result = self.cli("correction-request", "decide", "accepted", "--directory", self.root, "--id", "1",
                          "--on", "2026-03-07", "--note", "Quote fixed.")
        self.assertEqual(json.loads(result.stdout.rsplit("\n", 2)[0])["state"], "accepted")
        self.assertEqual(cr.list_requests(self.root)[0], {
            "id": 1, "finding_id": FID, "received_on": "2026-03-06", "summary": "Page 2 is misquoted.",
            "state": "accepted", "decided_on": "2026-03-07", "note": "Quote fixed."})

    def test_decide_refusals_and_finality(self):
        self.refused("no correction request 1", self.decide)
        self.add(received="2026-03-06")
        self.refused("--note must be 1 to 500 characters", self.decide, note="  ")
        self.refused("--on 2026-03-05 is before the request was received (2026-03-06)", self.decide, on="2026-03-05")
        self.refused("is in the future", self.decide, on="2026-03-21")
        self.refused("no correction request 2", self.decide, request_id=2)
        self.refused("decision must be accepted or declined", self.decide, decision="open")
        self.assertEqual(self.decide(on="2026-03-06")["state"], "declined")
        self.refused("already declined; decisions are final", self.decide)
        self.correct("2026-03-07")
        self.refused("already declined; decisions are final", self.decide, decision="accepted")
        result = self.cli("correction-request", "decide", "accepted", "--directory", self.root, "--id", "1",
                          "--on", "2026-03-08", "--note", "Again.", returncode=3)
        self.assertIn("decisions are final", result.stderr)

    def test_doctor_names_only_requests_open_more_than_14_days(self):
        self.assertNotIn("correction_requests_open_over_14_days", doctor_report(self.root))
        today = date.today()
        for days, summary in ((15, "SECRET-SUMMARY-A"), (14, "SECRET-SUMMARY-B"), (30, "SECRET-SUMMARY-C")):
            self.add(received=(today - timedelta(days=days)).isoformat(), summary=summary, today=today)
        self.decide(request_id=3, on=today.isoformat(), today=today)
        report = doctor_report(self.root)
        self.assertEqual(report["correction_requests_open_over_14_days"], 1)
        self.assertEqual([a for a in report["next_actions"] if "correction" in a],
                         ["Decide correction requests open over 14 days: id 1"])
        self.assertNotIn("SECRET-SUMMARY", json.dumps(report))
        self.assertEqual(cr.open_overdue(self.root, today), [1])

    def test_symlinks_and_unreadable_files_are_refused_without_overwrite(self):
        elsewhere = self.base / "elsewhere.json"
        elsewhere.write_text('{"schema_version": 1, "requests": []}\n', encoding="utf-8")
        self.file.symlink_to(elsewhere)
        self.refused("must be a regular file, not a symlink", self.add)
        self.refused("must be a regular file, not a symlink", cr.list_requests, self.root)
        self.assertEqual(json.loads(elsewhere.read_text()), {"schema_version": 1, "requests": []})
        self.file.unlink()
        moved = self.base / "real-private"
        self.private.rename(moved)
        self.private.symlink_to(moved, target_is_directory=True)
        self.refused("private/ must be an existing directory, not a symlink", self.add)
        self.assertFalse((moved / "correction-requests.json").exists())
        self.private.unlink()
        moved.rename(self.private)
        good = {"id": 1, "finding_id": FID, "received_on": "2026-03-06", "summary": "S.", "state": "open"}
        for text, needle in (("{not json", "not valid JSON"),
                             (json.dumps({"schema_version": 1, "requests": [dict(good, requester_email="x")]}),
                              "unknown key 'requester_email'"),
                             (json.dumps({"schema_version": 1, "requests": [], "contacts": []}), "unknown key 'contacts'"),
                             (json.dumps({"schema_version": 1, "requests": [good, good]}), "ids must be unique"),
                             (json.dumps({"schema_version": 1, "requests": [dict(good, received_on="2026-02-30")]}),
                              "'received_on' in requests[0] is not a YYYY-MM-DD date")):
            self.file.write_text(text, encoding="utf-8")
            message = self.refused("correction-requests.json unreadable", self.add)
            self.assertIn(needle, message)
            self.refused(needle, self.decide)
            self.refused(needle, cr.list_requests, self.root)
            self.assertIn(needle, doctor_report(self.root)["correction_requests_error"])
        result = self.cli("correction-request", "list", "--directory", self.root, returncode=3)
        self.assertIn("unreadable", result.stderr)

    def test_os_error_reading_the_log_is_reported_not_raised(self):
        self.add()
        real_read = Path.read_text

        def read_text(path, *args, **kwargs):
            if path.name == cr.NAME:
                raise PermissionError(13, "Permission denied")
            return real_read(path, *args, **kwargs)
        with mock.patch.object(Path, "read_text", read_text):
            self.refused("unreadable: cannot read it (PermissionError)", cr.list_requests, self.root)
            self.refused("cannot read it (PermissionError)", self.add)
            self.assertIn("cannot read it (PermissionError)", doctor_report(self.root)["correction_requests_error"])

    def test_build_output_ignores_the_log(self):
        def built():
            build_site(self.root, today=TODAY)
            public = self.root / "public"
            return {str(p.relative_to(public)): p.read_bytes() for p in sorted(public.rglob("*")) if p.is_file()}
        without = built()
        self.add(summary="UNIQUE-REQUEST-TEXT")
        self.add(received="2026-03-07")
        self.decide(request_id=2, note="UNIQUE-DECISION-NOTE")
        self.assertEqual(built(), without)
        self.assertFalse(any(b"UNIQUE-" in data for data in without.values()))

    # RF1935-8: a repeated JSON object key makes the whole log ambiguous and is refused by every consumer.
    DUP_NEEDLE = "correction-requests.json unreadable: duplicate object key; nothing was changed"

    def dup_text(self, extra_member=None, **override):
        """Raw log text whose one request repeats a member (default: the reproduced declined-then-open state pair)."""
        request = {"id": 1, "finding_id": FID, "received_on": "2026-03-06", "summary": "S.", "state": "open"}
        request.update(override)
        body = json.dumps(request)[:-1] + (extra_member if extra_member is not None else ', "state": "declined"') + "}"
        return '{"schema_version": 1, "requests": [' + body + "]}"

    def test_duplicate_state_is_refused_before_last_value_wins(self):
        for extra in (', "state": "declined"', ', "state": "open"'):  # conflicting and identical repeats
            for text in (self.dup_text(extra), self.dup_text(extra).replace('"state": "open"', '"state": "declined"', 1)):
                self.file.write_text(text, encoding="utf-8")
                self.file.chmod(0o600)
                self.refused(self.DUP_NEEDLE, cr.list_requests, self.root)
                self.refused(self.DUP_NEEDLE, cr.open_overdue, self.root, TODAY)
        result = self.cli("correction-request", "list", "--directory", self.root, returncode=3)
        self.assertIn(self.DUP_NEEDLE, result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_duplicate_keys_at_document_and_request_levels(self):
        good = '{"id": 1, "finding_id": "%s", "received_on": "2026-03-06", "summary": "S.", "state": "open"}' % FID
        decided = ('{"id": 1, "finding_id": "%s", "received_on": "2026-03-06", "summary": "S.", "state": "declined", '
                   '"decided_on": "2026-03-08", "note": "N."}' % FID)
        cases = [('{"schema_version": 1, "schema_version": 1, "requests": []}'),
                 ('{"schema_version": 1, "requests": [], "requests": []}')]
        for member in ('"id": 2', '"finding_id": "x"', '"received_on": "2026-03-07"', '"summary": "T."', '"state": "open"'):
            cases.append('{"schema_version": 1, "requests": [' + good[:-1] + ", " + member + "}]}")
        cases.append('{"schema_version": 1, "requests": [' + decided[:-1] + ', "note": "M."}]}')
        for text in cases:
            with self.subTest(text=text[:70]):
                self.file.write_text(text, encoding="utf-8")
                self.refused(self.DUP_NEEDLE, cr.list_requests, self.root)
        # the same field names in different request objects are not duplicates
        second = good.replace('"id": 1', '"id": 2')
        self.file.write_text('{"schema_version": 1, "requests": [' + good + ", " + second + "]}", encoding="utf-8")
        self.assertEqual([r["id"] for r in cr.list_requests(self.root, TODAY)], [1, 2])

    def test_unicode_escaped_duplicate_member_is_the_same_key(self):
        self.file.write_text(self.dup_text(', "st\\u0061te": "declined"'), encoding="utf-8")
        self.refused(self.DUP_NEEDLE, cr.list_requests, self.root)
        # near-looking but distinct keys still get ordinary schema validation, not the duplicate refusal
        self.file.write_text(self.dup_text(', "State": "declined"'), encoding="utf-8")
        self.refused("unknown key 'State'", cr.list_requests, self.root)

    def test_duplicate_log_cannot_be_silently_rewritten_by_add_or_decide(self):
        for text in (self.dup_text(), self.dup_text(id=1).replace("]}", ', {"id": 2, "id": 2}]}')):
            self.file.write_text(text, encoding="utf-8")
            self.file.chmod(0o600)
            os.utime(self.file, (1_700_000_000, 1_700_000_000))
            before, mtime = self.file.read_bytes(), self.file.stat().st_mtime_ns
            with mock.patch.object(cr, "_write_private") as writer:
                self.refused(self.DUP_NEEDLE, self.add)
                self.refused(self.DUP_NEEDLE, self.decide)
            writer.assert_not_called()
            self.assertEqual((self.file.read_bytes(), self.file.stat().st_mtime_ns,
                              stat.S_IMODE(self.file.stat().st_mode)), (before, mtime, 0o600))
            self.assertEqual(sorted(p.name for p in self.private.iterdir() if p.name.startswith(".correction")),
                             [".correction-requests.lock"])
            result = self.cli("correction-request", "add", "--directory", self.root, "--finding", FID,
                              "--received", "2026-03-06", "--summary", "x", returncode=3)
            self.assertIn(self.DUP_NEEDLE, result.stderr)
            self.assertEqual(self.file.read_bytes(), before)

    def test_duplicate_error_is_fixed_and_private_marker_free(self):
        marker = "HOSTILE-MARKER-\\u0001-ZQMARK"
        text = ('{"schema_version": 1, "requests": [], "%s": 1, "%s": "%s"}' % (marker, marker, marker))
        self.file.write_text(text, encoding="utf-8")
        shown = []
        for call in (lambda: cr.list_requests(self.root), lambda: self.add(), lambda: self.decide()):
            with self.assertRaises(ValueError) as caught:
                call()
            shown.append(str(caught.exception))
        shown.append(doctor_report(self.root)["correction_requests_error"])
        shown.append(self.cli("correction-request", "list", "--directory", self.root, returncode=3).stderr)
        for message in shown:
            self.assertIn("duplicate object key", message)
            self.assertNotIn("HOSTILE", message)
            self.assertNotIn("ZQMARK", message)

    def test_duplicate_free_log_keeps_all_existing_contracts(self):
        self.assertEqual(cr.list_requests(self.root, TODAY), [])  # missing file
        self.file.write_text('{"schema_version": 1, "requests": []}', encoding="utf-8")
        self.assertEqual(cr.list_requests(self.root, TODAY), [])  # empty log
        self.add(received="2026-03-06")
        self.add(received="2026-03-07")
        self.decide(request_id=1)
        self.assertEqual([(r["id"], r["state"]) for r in cr.list_requests(self.root, TODAY)], [(2, "open"), (1, "declined")])
        self.refused("already declined; decisions are final", self.decide)
        self.assertEqual(self.add()["id"], 3)


if __name__ == "__main__":
    unittest.main()
