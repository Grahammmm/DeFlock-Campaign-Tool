"""kit-response-import replay-validates the selected request's existing history before importing or replaying.
Offline, synthetic: invented agencies and dates; no socket may be opened."""
import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import kit_import_steps, kit_track  # noqa: E402
from tests import test_kit_response_import as base  # noqa: E402

REFUSAL = "Stopped: kit/sent.json: selected request history is invalid; nothing was written\n"
NEW_TAIL = "2026-03-20,produced,,,,"
DAY = date(2026, 3, 25)


def steps_of(*pairs):
    return [{"on": on, "kind": kind} for on, kind in pairs]


class SelectedHistoryTests(unittest.TestCase):
    setUp = base.KitResponseImportTests.setUp  # the old synthetic fixture, without rerunning its tests
    write = base.KitResponseImportTests.write
    do = base.KitResponseImportTests.do

    def set_steps(self, steps, *, key=None):
        data = json.loads(self.sent.read_text(encoding="utf-8"))
        data["requests"][key or self.agency]["steps"] = steps
        self.sent.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    def assert_refused(self, path, *extra):
        before = self.sent.read_bytes()
        code, out, err = self.do(path, *extra)
        self.assertEqual((code, out, err), (2, "", REFUSAL))
        self.assertEqual(self.sent.read_bytes(), before)

    def test_non_object_stored_steps_are_typed_refusals_before_signatures(self):
        path = self.write(NEW_TAIL)
        for bad in (42, None, "text", [1]):
            self.set_steps([bad])
            kit_track.read_sent(self.root)  # the reader alone admits it
            for extra in ((), ("--apply",)):
                self.assert_refused(path, *extra)
            with self.assertRaises(kit_import_steps.ImportRefused) as raised:
                kit_import_steps.import_steps(self.root, self.agency, path, today=DAY)
            self.assertEqual(raised.exception.exit_code, 2)

    def test_descending_existing_history_cannot_receive_a_valid_new_tail(self):
        self.set_steps(steps_of(("2026-03-12", "acknowledged"), ("2026-03-04", "determination")))
        path = self.write(NEW_TAIL)
        self.assert_refused(path)
        self.assert_refused(path, "--apply")
        self.set_steps(steps_of(("2026-03-04", "acknowledged"), ("2026-03-12", "determination")))
        code, out, err = self.do(path, "--apply")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(self.steps()[-1]["kind"], "produced")

    def steps(self):
        return kit_track.read_sent(self.root)[self.agency]["steps"]

    def test_post_terminal_recorded_match_is_not_a_successful_replay(self):
        self.set_steps(steps_of(("2026-03-04", "acknowledged"), ("2026-03-05", "closed"), ("2026-03-06", "produced")))
        path = self.write("2026-03-06,produced,,,,")
        self.assert_refused(path)
        self.assert_refused(path, "--apply")

    def test_full_history_is_checked_not_only_last_or_matching_step(self):
        good = steps_of(("2026-03-04", "acknowledged"), ("2026-03-12", "determination"))
        bad = [dict(good[0], kind="mystery"), good[1]]
        self.set_steps(bad)
        self.assert_refused(self.write("2026-03-04,acknowledged,,,,"))
        self.assert_refused(self.write(NEW_TAIL))
        self.set_steps([dict(good[0], recorded_at="not a stamp"), good[1]])
        self.assert_refused(self.write("2026-03-04,acknowledged,,,,"))

    def test_selected_request_scope_and_existing_precedence_survive(self):
        drafts = self.root / "kit" / "requests"
        (drafts / f"{self.agency}--second.md").write_text((drafts / f"{self.agency}.md").read_text(encoding="utf-8"),
                                                          encoding="utf-8")
        self.assertEqual(base.run("kit-sent", "--directory", self.root, "--agency", self.agency, "--date",
                                  base.SENT_ON, "--channel", "email", "--request", "second")[0], 0)
        self.set_steps(steps_of(("2026-03-12", "acknowledged"), ("2026-03-04", "determination")),
                       key=self.agency + "#second")
        code, out, err = self.do(self.write(NEW_TAIL), "--apply")  # the healthy selected request still imports
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(self.do(self.write(NEW_TAIL), "--request", "second")[2], REFUSAL)
        # unknown request and malformed CSV keep their earlier diagnostics
        code, _, err = self.do(self.write(NEW_TAIL), agency="no-such-agency")
        self.assertEqual(code, 2)
        self.assertNotIn("history is invalid", err)
        self.set_steps(steps_of(("2026-03-12", "acknowledged"), ("2026-03-04", "determination")))
        code, _, err = self.do(self.write(NEW_TAIL, header="bad,header"))
        self.assertEqual(code, 2)
        self.assertNotIn("history is invalid", err)
        # an independently invalid row loses to the source-history gate
        self.assert_refused(self.write("not-a-date,produced,,,,"))

    def test_history_refusal_precedes_scratch_and_does_not_write(self):
        self.set_steps(steps_of(("2026-03-12", "acknowledged"), ("2026-03-04", "determination")))
        path = self.write(NEW_TAIL)
        csv_before = path.read_bytes()
        with mock.patch.object(tempfile, "TemporaryDirectory", side_effect=AssertionError("scratch")), \
                mock.patch.object(kit_import_steps, "_write", side_effect=AssertionError("write")), \
                mock.patch.object(kit_import_steps, "_step_signature", side_effect=AssertionError("signature")), \
                mock.patch.object(kit_track, "record_step", side_effect=AssertionError("record_step")):
            self.assert_refused(path, "--apply")
        self.assertEqual(path.read_bytes(), csv_before)

    def test_history_errors_are_fixed_private_and_narrow(self):
        secret = "canary-note-xyz"
        self.set_steps([{"on": "2026-03-12", "kind": "acknowledged", "note": secret},
                        {"on": "2026-03-04", "kind": "determination"}])
        path = self.write(NEW_TAIL)
        with self.assertRaises(kit_import_steps.ImportRefused) as raised:
            kit_import_steps.import_steps(self.root, self.agency, path, today=DAY)
        self.assertEqual(str(raised.exception), REFUSAL[len("Stopped: "):-1])
        self.assertTrue(raised.exception.__suppress_context__)
        self.assertNotIn(secret, str(raised.exception))
        with mock.patch.object(kit_track, "validate_steps", side_effect=RuntimeError("bug")):
            with self.assertRaises(RuntimeError):
                kit_import_steps.import_steps(self.root, self.agency, path, today=DAY)

    def test_single_effective_day_reaches_history_and_new_rows(self):
        self.set_steps(steps_of(("2026-03-04", "acknowledged")))
        path = self.write(NEW_TAIL)
        seen = []
        real_validate, real_record = kit_track.validate_steps, kit_track.record_step

        def validate(*args, **kwargs):
            seen.append(kwargs["today"])
            return real_validate(*args, **kwargs)

        def record(*args, **kwargs):
            seen.append(kwargs["today"])
            return real_record(*args, **kwargs)

        with mock.patch.object(kit_track, "validate_steps", validate), mock.patch.object(kit_track, "record_step", record):
            kit_import_steps.import_steps(self.root, self.agency, path, today=DAY)
            self.assertEqual(seen, [DAY, DAY, DAY][:len(seen)])
            self.assertGreaterEqual(len(seen), 2)
            seen.clear()
            fake = mock.Mock(wraps=date)
            fake.today.side_effect = [date(2026, 3, 25), date(2030, 1, 1)]
            with mock.patch.object(kit_import_steps, "date", fake):
                kit_import_steps.import_steps(self.root, self.agency, path)
            self.assertEqual(set(seen), {date(2026, 3, 25)})
        self.assertEqual(fake.today.call_count, 1)
        # a stored step after the effective day is a history refusal
        self.set_steps(steps_of(("2026-03-30", "acknowledged")))
        with self.assertRaises(kit_import_steps.ImportRefused):
            kit_import_steps.import_steps(self.root, self.agency, path, today=DAY)

    def test_valid_historical_replay_after_terminal_step_remains_noop(self):
        self.set_steps(steps_of(("2026-03-04", "acknowledged"), ("2026-03-12", "determination"),
                                ("2026-03-14", "closed")))
        path = self.write("2026-03-04,acknowledged,,,,", "2026-03-12,determination,,,,")
        before = self.sent.stat()
        raw = self.sent.read_bytes()
        code, out, err = self.do(path, "--apply")
        self.assertEqual((code, err), (0, ""))
        report = json.loads(out.split("Checked only")[0])
        self.assertEqual((report["added"], report["skipped"], report["applied"]), (0, 2, False))
        self.assertEqual(self.sent.read_bytes(), raw)
        self.assertEqual(self.sent.stat().st_mtime_ns, before.st_mtime_ns)

    def test_valid_preview_apply_and_mixed_replay_remain(self):
        path = self.write(*base.KitResponseImportTests.GOOD)
        code, out, err = self.do(path, "--apply")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(oct(self.sent.stat().st_mode & 0o777), "0o600")
        mixed = self.write(*base.KitResponseImportTests.GOOD, "2026-03-21,determination,,,,")
        report = json.loads(self.do(mixed, "--apply")[1].split("Recorded")[0])
        self.assertEqual((report["added"], report["skipped"]), (1, 3))
        dup = self.write("2026-03-22,closed,,,,", "2026-03-22,closed,,,,")
        before = self.sent.read_bytes()
        self.assertEqual(self.do(dup, "--apply")[0], 2)
        self.assertEqual(self.sent.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
