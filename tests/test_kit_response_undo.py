"""kit-response-undo takes the latest recorded step off a hand-sent request and keeps it in a private archive.
Offline, synthetic: invented agencies, dates and hashes; no socket may be opened and nothing is sent."""
import contextlib
import copy
import errno
import hashlib
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
from campaign_tool import cli, kit, kit_track, kit_undo  # noqa: E402
from tests.test_kit_check import ready_drafts  # noqa: E402

SENT_ON = "2026-03-02"
TODAY = date(2026, 4, 20)
STAMP = "2026-04-01T09:30:00+00:00"
REASON = "Typed the wrong date for the letter"


def sha(name):
    return hashlib.sha256(name.encode()).hexdigest()


def step(on, kind, **extra):
    return {"on": on, "kind": kind, **extra, "recorded_at": STAMP}


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


class KitResponseUndoTests(unittest.TestCase):
    def setUp(self):
        for target, effect in (("socket.socket", AssertionError("socket opened")),
                               ("urllib.request.urlopen", AssertionError("network used"))):
            patcher = mock.patch(target, side_effect=effect)
            patcher.start()
            self.addCleanup(patcher.stop)
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-kit-undo-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.root = self.tmp / "campaign"
        self.assertEqual(run("init", "--directory", self.root, "--name", "Synthetic", "--county", "San Luis Obispo",
                             "--state", "CA")[0], 0)
        self.assertEqual(run("kit", "--directory", self.root)[0], 0)
        self.sent = self.root / "kit" / "sent.json"
        self.archive = self.root / "kit" / "sent-undone.json"
        self.agency = sorted((self.root / "kit" / "requests").glob("*.md"))[0].stem
        ready_drafts(self.root)
        self.assertEqual(run("kit-sent", "--directory", self.root, "--agency", self.agency, "--date", SENT_ON,
                             "--channel", "email")[0], 0)

    # helpers -------------------------------------------------------------------------------------------------
    def load(self):
        return json.loads(self.sent.read_text(encoding="utf-8"))

    def put(self, steps, key=None, **fields):  # replace one entry's steps (and fields) in sent.json
        document = self.load()
        entry = document["requests"][key or self.agency]
        entry["steps"] = steps
        entry.update(fields)
        self.sent.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")

    def entry(self, key=None):
        return self.load()["requests"][key or self.agency]

    def undo(self, *extra, agency=None, reason=REASON):
        return run("kit-response-undo", "--directory", self.root, "--agency", agency or self.agency,
                   "--reason", reason, *extra)

    def receipts(self):
        return json.loads(self.archive.read_text(encoding="utf-8"))["undos"]

    def snapshot(self):
        return {str(p.relative_to(self.root)): p.read_bytes() for p in sorted(self.root.rglob("*")) if p.is_file()}

    def refused(self, result, *words, code=1):
        got, out, err = result
        self.assertEqual(got, code, out + err)
        self.assertTrue(err.startswith("Stopped: "), err)
        self.assertNotIn("Traceback", err)
        for word in words:
            self.assertIn(word, err)

    def refused_untouched(self, *words, **kwargs):
        before = self.snapshot()
        self.refused(self.undo(**kwargs), *words)
        self.assertEqual(self.snapshot(), before)

    def status_row(self, request="main"):
        rows = [r for r in kit_track.status(self.root, today=TODAY)["requests"]
                if r["agency_id"] == self.agency and r["request"] == request]
        return rows[0]

    # acceptance ----------------------------------------------------------------------------------------------
    def test_latest_only_and_archive_bindings(self):
        first = step("2026-03-05", "acknowledged", correspondence=[sha("c1")])
        middle = step("2026-03-20", "produced", partial=True, expected_on="2026-04-10", note="First batch",
                      records=[sha("r1"), sha("r2")])
        tail = step("2026-03-20", "produced", records=[sha("r3")], correspondence=[sha("c2")],
                    withheld_basis=["Gov. Code 7923.600"], download_expires_on="2026-03-25", note="Final   batch")
        tail["note"] = "Final batch"
        self.put([first, middle, tail], reference="R-1", correspondence=[sha("entry-letter")])
        document = self.load()
        document["requests"][self.agency + "#second"] = {**copy.deepcopy(document["requests"][self.agency]),
                                                           "draft": f"requests/{self.agency}--second.md",
                                                           "steps": [step("2026-03-06", "acknowledged")]}
        del document["requests"][self.agency + "#second"]["correspondence"]
        self.sent.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        before = self.load()
        self.archive.write_text('{"schema_version": 1, "undos": []}\n')
        os.chmod(self.archive, 0o644)
        code, out, err = self.undo()
        self.assertEqual((code, err), (0, ""))
        self.assertIn("1 step(s) undone", out)
        self.assertIn("Recorded only. Nothing was sent.", out)
        after = self.load()
        main, second = after["requests"][self.agency], after["requests"][self.agency + "#second"]
        self.assertEqual(main["steps"], [first, middle])  # exactly the tail, not the greatest date
        self.assertEqual((main["reference"], main["correspondence"]), ("R-1", [sha("entry-letter")]))
        self.assertEqual(second, before["requests"][self.agency + "#second"])
        self.assertEqual(len(main["undo_ids"]), 1)
        (receipt,) = self.receipts()
        self.assertEqual(receipt["step"], tail)  # every full hash, note, expiry, basis and timestamp as stored
        self.assertEqual((receipt["agency_id"], receipt["request"], receipt["reason"], receipt["undo_id"]),
                         (self.agency, "main", REASON, main["undo_ids"][0]))
        self.assertEqual(receipt["entry_before_sha256"], kit_undo._digest(before["requests"][self.agency]))
        self.assertEqual(receipt["entry_after_sha256"], kit_undo._digest(main))
        self.assertEqual(stat.S_IMODE(self.archive.stat().st_mode), 0o600)
        self.assertEqual(self.status_row()["undone_steps"], 1)

    def test_undo_closed_then_correct(self):
        self.put([step("2026-03-05", "acknowledged"), step("2026-03-06", "closed", note="By mistake")])
        self.assertTrue(self.status_row()["closed"])
        self.assertEqual(run("kit-response", "--directory", self.root, "--agency", self.agency, "--date", "2026-03-07",
                             "--kind", "determination")[0], 1)  # a closed request locks today
        self.assertEqual(self.undo()[0], 0)
        self.assertFalse(self.status_row()["closed"])
        code, out, err = run("kit-response", "--directory", self.root, "--agency", self.agency, "--date", "2026-03-07",
                             "--kind", "determination")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual([s["kind"] for s in self.entry()["steps"]], ["acknowledged", "determination"])
        self.assertEqual(self.receipts()[0]["step"]["note"], "By mistake")

    def test_all_undone_then_replace_sent(self):
        self.put([step("2026-03-05", "acknowledged", correspondence=[sha("c1")]), step("2026-03-06", "extension")],
                 reference="R-9", correspondence=[sha("sent-letter")])
        self.assertEqual(self.undo()[0], 0)
        self.assertEqual(self.undo(reason="Second one")[0], 0)
        self.assertEqual(self.entry()["steps"], [])
        before = self.snapshot()
        self.refused(self.undo(), "no recorded step to undo")  # nothing left: refused, both files untouched
        self.assertEqual(self.snapshot(), before)
        code, out, err = run("kit-sent", "--directory", self.root, "--agency", self.agency, "--date", "2026-03-03",
                             "--channel", "email", "--replace")
        self.assertEqual((code, err), (0, ""))
        entry = self.entry()
        self.assertEqual((entry["sent_on"], entry["reference"], entry["correspondence"]),
                         ("2026-03-03", "R-9", [sha("sent-letter")]))
        self.assertEqual(len(entry["undo_ids"]), 2)  # the completed count survives the replacement
        self.assertEqual(self.status_row()["undone_steps"], 2)
        self.assertEqual(len(self.receipts()), 2)

    def test_prefix_validation_is_shared(self):
        wrapped = mock.patch.object(kit_track, "_check_step", wraps=kit_track._check_step)
        self.put([step("2026-03-05", "acknowledged"), step("2026-03-06", "extension"), step("2026-03-07", "closed")])
        with wrapped as check:
            kit_undo.undo_step(self.root, self.agency, reason=REASON, today=TODAY)
            self.assertEqual(check.call_count, 2)  # the same function record_step uses, once per retained step
            kit_track.record_step(self.root, self.agency, "2026-03-08", "determination", today=TODAY)
            self.assertEqual(check.call_count, 3)
        tail = step("2026-03-30", "determination")
        bad_prefixes = {
            "date order": ([step("2026-03-09", "acknowledged"), step("2026-03-08", "extension"), tail], "before the previous"),
            "before sent": ([step("2026-03-01", "acknowledged"), step("2026-03-08", "extension"), tail], "before the request"),
            "future": ([step("2999-01-01", "acknowledged"), step("2999-01-02", "extension"), tail], "in the future"),
            "ack not first": ([step("2026-03-05", "extension"), step("2026-03-06", "acknowledged"), tail], "only as the first"),
            "fee settled twice": ([step("2026-03-05", "fee_estimate", amount_cents=500), step("2026-03-06", "fee_paid"),
                                   step("2026-03-07", "fee_waived"), tail], "already paid"),
            "fee without estimate": ([step("2026-03-05", "fee_paid"), step("2026-03-06", "extension"), tail],
                                     "needs a fee_estimate"),
            "clarified alone": ([step("2026-03-05", "clarified"), step("2026-03-06", "extension"), tail],
                                "no open clarification"),
            "step after closed": ([step("2026-03-05", "closed"), step("2026-03-06", "extension"), tail], "closed"),
            "expected before determination": ([step("2026-03-05", "extension", expected_on="2026-04-01"),
                                               step("2026-03-06", "acknowledged"), tail], "unreadable"),
            "withheld on extension": ([step("2026-03-05", "extension", withheld_basis=["x"]), tail], "unreadable"),
            "download on extension": ([step("2026-03-05", "extension", download_expires_on="2026-03-09"), tail],
                                      "unreadable"),
            "unknown kind": ([step("2026-03-05", "bogus"), tail], "unreadable"),
            "unknown field": ([step("2026-03-05", "extension", mystery=1), tail], "unreadable"),
            "short record hash": ([step("2026-03-05", "produced", records=["abc123"]), tail], "unreadable"),
            "record linked twice": ([step("2026-03-05", "produced", partial=True, records=[sha("a")]),
                                     step("2026-03-06", "produced", records=[sha("a")]), tail], "unreadable"),
        }
        for name, (steps, words) in bad_prefixes.items():
            with self.subTest(name):
                self.put(steps)
                self.refused_untouched(words)
        for name, bad_tail in {"removed step with an unknown field": step("2026-03-30", "extension", mystery=1),
                               "removed step with a bad note": step("2026-03-30", "extension", note="<b>"),
                               "removed step with an unusual hash": step("2026-03-30", "produced", records=["zz"]),
                               "removed step with a bad stamp": {"on": "2026-03-30", "kind": "extension",
                                                                 "recorded_at": "yesterday"}}.items():
            with self.subTest(name):
                self.put([step("2026-03-05", "acknowledged"), bad_tail])
                self.refused_untouched("malformed")
        # retained evidence that is no longer in the ledger is checked as stored, never looked up again
        self.put([step("2026-03-05", "produced", records=[sha("gone")]), step("2026-03-06", "closed")])
        self.assertEqual(self.undo()[0], 0)
        self.assertEqual(self.entry()["steps"][0]["records"], [sha("gone")])

    def test_derived_state_after_undo(self):
        self.put([step("2026-03-05", "fee_estimate", amount_cents=5000), step("2026-03-06", "fee_paid", amount_cents=5000)])
        self.assertEqual(self.status_row()["fee_state"], "paid")
        self.assertEqual(self.undo()[0], 0)
        self.assertEqual(self.status_row()["fee_state"], "unpaid")
        self.put([step("2026-03-05", "clarification_requested"), step("2026-03-06", "clarified")])
        self.assertIsNone(self.status_row()["waiting_on_you_since"])
        self.assertEqual(self.undo(reason="again")[0], 0)
        self.assertEqual(self.status_row()["waiting_on_you_since"], "2026-03-05")
        due = kit_track.due(self.root, "2026-04-01", today=TODAY)
        self.assertEqual([r["state"] for r in due["open"]], ["waiting_on_you"])
        self.put([step("2026-03-05", "produced", partial=True, expected_on="2026-04-10"), step("2026-03-20", "produced")])
        self.assertFalse(self.status_row()["producing"])
        self.assertEqual(self.undo(reason="third")[0], 0)
        self.assertTrue(self.status_row()["producing"])
        self.put([step("2026-03-05", "produced", download_expires_on="2026-05-30"),
                  step("2026-03-10", "produced", records=[sha("linked")])])
        self.assertNotIn("downloads_pending", self.status_row())
        self.assertEqual(self.undo(reason="fourth")[0], 0)
        self.assertEqual(self.status_row()["downloads_pending"][0]["state"], "pending")
        self.assertEqual(self.status_row()["records"], 0)  # archived evidence no longer counts as linked
        self.assertEqual(self.receipts()[-1]["step"]["records"], [sha("linked")])
        self.assertEqual(len(self.receipts()), 4)
        self.assertEqual(self.status_row()["undone_steps"], 4)

    def test_refusals_unchanged(self):
        self.put([step("2026-03-05", "acknowledged")])
        self.refused_untouched("no sent request", agency="no-such-agency")
        self.refused(self.undo("--request", "second"), "no sent request")
        self.refused(self.undo("--request", "Bad Name!"), "--request must be")
        self.assertFalse(self.archive.exists())
        for reason, word in (("", "--reason"), ("   ", "--reason"), ("x" * 201, "--reason"), ("a\x00b", "--reason"),
                             ("a\tb", "--reason"), ("<script>", "--reason")):
            with self.subTest(reason=repr(reason)):
                result = self.undo(reason=reason)
                self.refused(result, word)
                if reason.strip():
                    self.assertNotIn(reason, result[2])
        self.assertEqual(self.undo(reason="  spaced \n".replace("\n", " ") + "out  ")[0], 0)
        self.assertEqual(self.receipts()[0]["reason"], "spaced out")
        self.assertEqual(self.undo()[0], 1)  # now empty
        # a MuckRock request is not hand-sent
        document = self.load()
        document["requests"][self.agency + "#mr"] = {
            **document["requests"][self.agency], "channel": "muckrock", "draft": f"requests/{self.agency}--mr.md",
            "request_url": "https://www.muckrock.com/foi/example-1/synthetic-request-123/",
            "steps": [step("2026-03-05", "acknowledged")]}
        self.sent.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        before = self.snapshot()
        self.refused(self.undo("--request", "mr"), "MuckRock")
        self.assertEqual(self.snapshot(), before)

    def test_refusals_for_bad_files(self):
        self.put([step("2026-03-05", "acknowledged")])
        outside = self.tmp / "outside.json"
        outside.write_text("{}")
        good_sent = self.sent.read_bytes()
        # absent sent file: refused, no file created
        self.sent.unlink()
        self.refused(self.undo(), "no sent request")
        self.assertFalse(self.sent.exists() or self.archive.exists())
        for label, make in (("corrupt sent", lambda: self.sent.write_text("{not json")),
                            ("symlink sent", lambda: self.sent.symlink_to(outside)),
                            ("directory sent", lambda: self.sent.mkdir())):
            with self.subTest(label):
                self.sent.unlink() if self.sent.is_symlink() or self.sent.is_file() else (
                    self.sent.rmdir() if self.sent.is_dir() else None)
                make()
                self.refused(self.undo(), "unreadable")
                self.assertFalse(self.archive.exists())
        if self.sent.is_dir():
            self.sent.rmdir()
        self.sent.unlink(missing_ok=True)
        self.sent.write_bytes(good_sent)
        for label, make in (("corrupt archive", lambda: self.archive.write_text("{not json")),
                            ("wrong schema", lambda: self.archive.write_text('{"schema_version": 2, "undos": []}')),
                            ("undos not a list", lambda: self.archive.write_text('{"schema_version": 1, "undos": {}}')),
                            ("receipt without id", lambda: self.archive.write_text('{"schema_version": 1, "undos": [{}]}')),
                            ("symlink archive", lambda: self.archive.symlink_to(outside)),
                            ("directory archive", lambda: self.archive.mkdir())):
            with self.subTest(label):
                if self.archive.is_dir() and not self.archive.is_symlink():
                    self.archive.rmdir()
                else:
                    self.archive.unlink(missing_ok=True)
                make()
                before = (self.sent.read_bytes(), outside.read_bytes())
                self.refused(self.undo(), "sent-undone.json")
                self.assertEqual((self.sent.read_bytes(), outside.read_bytes()), before)
        if self.archive.is_dir():
            self.archive.rmdir()
        self.archive.unlink(missing_ok=True)
        # a kit folder that is a symlink is refused before anything is written
        real = self.root / "kit-real"
        (self.root / "kit").rename(real)
        (self.root / "kit").symlink_to(real)
        self.refused(self.undo(), "symlink")
        self.assertFalse((real / "sent-undone.json").exists())
        self.assertEqual(outside.read_text(), "{}")

    def test_archive_first_failures(self):
        self.put([step("2026-03-05", "acknowledged"), step("2026-03-06", "extension")])
        self.assertEqual(self.undo(reason="first")[0], 0)  # one earlier receipt, applied
        (prior,) = self.receipts()
        before = self.snapshot()
        real = kit._write_json
        # archive write fails: sent.json untouched, no new receipt
        with mock.patch.object(kit, "_write_json", side_effect=OSError(errno.EIO, "injected")):
            self.refused(self.undo(reason="x"), "injected")
        self.assertEqual(self.snapshot(), before)
        # fsync fails after the archive replaced: still no change to sent.json, and the attempt is on record
        with mock.patch("os.fsync", side_effect=OSError(errno.EIO, "injected sync")):
            self.refused(self.undo(reason="synced"), "injected sync")
        self.assertEqual(self.load(), json.loads(before["kit/sent.json"]))
        self.assertEqual(len(self.receipts()), 2)
        self.assertNotIn(self.receipts()[1]["undo_id"], self.entry().get("undo_ids", []))
        # sent.json replacement fails: the attempt receipt stays, but it is not an applied undo
        self.put([step("2026-03-05", "acknowledged"), step("2026-03-06", "extension")], undo_ids=[prior["undo_id"]])
        self.sent_before = self.sent.read_bytes()

        def fail_sent(path, document):
            if Path(path).name == "sent.json":
                raise OSError(errno.EIO, "injected sent failure")
            real(path, document)
        with mock.patch.object(kit, "_write_json", side_effect=fail_sent):
            self.refused(self.undo(reason="third"), "injected sent failure")
        self.assertEqual(self.sent.read_bytes(), self.sent_before)
        self.assertEqual(len(self.receipts()), 3)
        self.assertEqual(self.status_row()["undone_steps"], 1)  # the abandoned attempt is not counted
        # a successful retry gets its own id and counts once
        self.assertEqual(self.undo(reason="third")[0], 0)
        receipts = self.receipts()
        self.assertEqual(len(receipts), 4)
        self.assertEqual(len({r["undo_id"] for r in receipts}), 4)
        self.assertEqual(self.entry()["undo_ids"], [prior["undo_id"], receipts[3]["undo_id"]])
        self.assertEqual(self.status_row()["undone_steps"], 2)
        self.assertEqual(receipts[0], prior)  # earlier receipts are never changed
        self.assertEqual(stat.S_IMODE(self.archive.stat().st_mode), 0o600)

    def test_private_output_and_legacy_status(self):
        secret = sha("secret-record")
        self.put([step("2026-03-05", "acknowledged"),
                  step("2026-03-20", "produced", records=[secret], correspondence=[sha("letter")],
                       note="Do not print this note")], reference="R-2")
        legacy = run("kit-status", "--directory", self.root)
        rows = json.loads(legacy[1])["requests"]
        self.assertNotIn("undone_steps", rows[0])
        self.assertNotIn("step(s) undone", legacy[2])
        csv_path = self.tmp / "before.csv"
        self.assertEqual(run("kit-status", "--directory", self.root, "--csv", csv_path)[0], 0)
        header_before = csv_path.read_text().splitlines()[0]
        code, out, err = self.undo(reason="Private reason text")
        self.assertEqual(code, 0, err)
        for hidden in (secret, secret[:12], sha("letter"), "Private reason text", "Do not print this note"):
            self.assertNotIn(hidden, out + err)
        self.assertEqual(len(out.strip().splitlines()), 2)  # one summary line, one reminder; no archived dump
        status_code, status_out, status_err = run("kit-status", "--directory", self.root)
        self.assertEqual(status_code, 0)
        self.assertIn(f"{self.agency}: 1 step(s) undone", status_err)
        row = json.loads(status_out)["requests"][0]
        self.assertEqual((row["undone_steps"], row["reference"], row["records"]), (1, "R-2", 0))
        self.assertNotIn("Private reason text", status_out + status_err)
        csv_after = self.tmp / "after.csv"
        self.assertEqual(run("kit-status", "--directory", self.root, "--csv", csv_after)[0], 0)
        self.assertEqual(csv_after.read_text().splitlines()[0], header_before)
        # the count belongs to the named request only
        document = self.load()
        document["requests"][self.agency + "#second"] = {
            **{k: v for k, v in document["requests"][self.agency].items() if k not in ("undo_ids", "reference")},
            "draft": f"requests/{self.agency}--second.md", "steps": [step("2026-03-06", "acknowledged")]}
        self.sent.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        self.assertNotIn("undone_steps", self.status_row("second"))
        self.assertEqual(self.status_row()["undone_steps"], 1)
        self.assertEqual(self.undo("--request", "second")[0], 0)
        self.assertEqual((self.status_row("second")["undone_steps"], self.status_row()["undone_steps"]), (1, 1))
        with self.assertRaises(SystemExit) as stopped, contextlib.redirect_stdout(io.StringIO()) as help_text:
            with mock.patch.object(sys, "argv", ["campaign_tool", "kit-response-undo", "--help"]):
                cli.main()
        self.assertEqual(stopped.exception.code, 0)
        self.assertIn("--reason", help_text.getvalue())
        self.assertEqual(run("kit-response-undo", "--directory", self.root, "--agency", self.agency)[0], 2)  # --reason required


if __name__ == "__main__":
    unittest.main()
