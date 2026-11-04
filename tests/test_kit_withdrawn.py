"""DF1952-12: kit-response --kind withdrawn, the campaign's own terminal act (offline, synthetic only).

Run from the repository root: python3 -m unittest tests.test_kit_withdrawn
"""
import contextlib
import copy
import csv
import io
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import cli, kit_timing, kit_track, kit_undo, registry, today  # noqa: E402
from tests.test_kit_asked import A, B, EXAMPLE, POLICE, TITLE  # noqa: E402
from tests.test_kit_check import ready_drafts  # noqa: E402
from tests.test_today import meeting  # noqa: E402

SENT_ON = "2026-03-02"
AS_OF = "2026-06-01"
STAMP = "2026-04-01T09:30:00+00:00"
NOTE = "Sent twice; this request is superseded"
SYNTHETIC_PRIVATE_NOTE = "PRIVATE-WITHDRAWAL-NOTE-5519"


def run(*arguments):
    out, err, code = io.StringIO(), io.StringIO(), 0
    with mock.patch.object(sys, "argv", ["campaign_tool", *map(str, arguments)]), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            cli.main()
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


def step(on, kind, **extra):
    return {"on": on, "kind": kind, **extra, "recorded_at": STAMP}


def withdrawn(on="2026-05-30", note=NOTE):
    return step(on, "withdrawn", note=note)


class Helpers(unittest.TestCase):
    def offline(self):
        for target in ("socket.socket", "socket.create_connection", "urllib.request.urlopen"):
            patch = mock.patch(target, side_effect=AssertionError("network used"))
            patch.start()
            self.addCleanup(patch.stop)

    def load(self):
        return json.loads(self.sent.read_text(encoding="utf-8"))

    def save(self, document):
        self.sent.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")

    def put(self, steps, key=None, **fields):  # replace one entry's steps (and fields) in sent.json
        document = self.load()
        entry = document["requests"][key or self.a]
        entry["steps"] = steps
        entry.update(fields)
        self.save(document)

    def add(self, name, steps, agency=None, **fields):  # a further named request, cloned from the main entry
        agency = agency or self.a
        document = self.load()
        entry = copy.deepcopy(document["requests"][self.a])
        entry.update(draft=f"requests/{agency}--{name}.md", steps=steps, **fields)
        document["requests"][f"{agency}#{name}"] = entry
        self.save(document)

    def entry(self, key=None):
        return self.load()["requests"][key or self.a]

    def response(self, kind, on, *extra, agency=None):
        return run("kit-response", "--directory", self.root, "--agency", agency or self.a, "--date", on,
                   "--kind", kind, *extra)

    def snapshot(self):
        return {str(p.relative_to(self.root)): p.read_bytes() for p in sorted(self.root.rglob("*")) if p.is_file()}

    def refused(self, result, *words, code=1):
        got, out, err = result
        self.assertEqual(got, code, out + err)
        self.assertTrue(err.startswith("Stopped: "), err)
        self.assertNotIn("Traceback", err)
        for word in words:
            self.assertIn(word, err)

    def refused_untouched(self, result_fn, *words, code=1):
        before = self.snapshot()
        self.refused(result_fn(), *words, code=code)
        self.assertEqual(self.snapshot(), before)

    def status(self):
        code, out, err = run("kit-status", "--directory", self.root)
        self.assertEqual(code, 0, err)
        return {(r["agency_id"], r["request"]): r for r in json.loads(out)["requests"]}, out, err

    def due(self, *extra, expect=0):
        code, out, err = run("kit-due", "--directory", self.root, "--as-of", AS_OF, *extra)
        self.assertEqual(code, expect, err)
        return json.loads(out)


class Base(Helpers):  # a campaign made by `init` + `kit`, with agency A sent by hand on SENT_ON
    def setUp(self):
        self.offline()
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-kit-withdrawn-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.root = self.tmp / "campaign"
        self.assertEqual(run("init", "--directory", self.root, "--name", "Synthetic", "--county", "San Luis Obispo",
                             "--state", "CA")[0], 0)
        self.assertEqual(run("kit", "--directory", self.root)[0], 0)
        self.a, self.b, self.c = (p.stem for p in sorted((self.root / "kit" / "requests").glob("*.md"))[:3])
        ready_drafts(self.root)
        self.sent = self.root / "kit" / "sent.json"
        self.assertEqual(run("kit-sent", "--directory", self.root, "--agency", self.a, "--date", SENT_ON,
                             "--channel", "email")[0], 0)


class ExampleBase(Helpers):  # the fictional example campaign, whose public agencies.json lists POLICE
    def setUp(self):
        self.offline()
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-kit-withdrawn-asked-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.root = self.tmp / "campaign"
        shutil.copytree(EXAMPLE, self.root)
        self.a = POLICE
        self.path = self.root / "content" / "agencies.json"
        self.sent = self.root / "kit" / "sent.json"
        self.agencies = json.loads(self.path.read_text(encoding="utf-8"))

    def write_sent(self, steps, scopes=(A, B), extra=None):
        entry = {"sent_on": SENT_ON, "channel": "email", "draft": f"requests/{POLICE}.md", "draft_sha256": "a" * 64,
                 "recorded_at": "2026-03-02T10:00:00+00:00", "steps": steps,
                 **({"scopes": list(scopes)} if scopes else {}), **(extra or {})}
        self.sent.parent.mkdir(exist_ok=True)
        self.save({"schema_version": 1, "requests": {POLICE: entry}})

    def set_asked(self, items):
        agency = next(a for a in self.agencies if a["agency_id"] == POLICE)
        agency.pop("asked", None)
        if items is not None:
            agency["asked"] = items
        self.path.write_text(json.dumps(self.agencies, indent=2) + "\n", encoding="utf-8")

    def asked(self):
        document = json.loads(self.path.read_text(encoding="utf-8"))
        return next(a for a in document if a["agency_id"] == POLICE).get("asked", [])

    def propose(self, *flags):
        return run("kit-asked", "--directory", self.root, *flags)


class WithdrawnRecordingTests(Base):
    def test_withdraw_first_main_and_named(self):
        self.add("second", [])
        code, out, err = self.response("withdrawn", SENT_ON, "--note", "  Sent   twice,  superseded  ")
        self.assertEqual((code, err), (0, ""))
        self.assertTrue(out.endswith("Recorded only. Nothing was sent.\n"))
        main = self.entry()
        (stored,) = main["steps"]
        self.assertEqual({k: v for k, v in stored.items() if k != "recorded_at"},
                         {"on": SENT_ON, "kind": "withdrawn", "note": "Sent twice, superseded"})
        self.assertEqual(self.entry(f"{self.a}#second")["steps"], [])  # request isolation
        self.assertEqual(stat.S_IMODE(os.stat(self.sent).st_mode), 0o600)
        code, _, err = self.response("withdrawn", "2026-03-09", "--note", "Replaced by a narrower request",
                                     "--request", "second")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual([s["kind"] for s in self.entry(f"{self.a}#second")["steps"]], ["withdrawn"])
        self.assertEqual(len(self.entry()["steps"]), 1)

    def test_withdrawal_follows_any_nonterminal_history_and_keeps_a_correspondence_binding(self):
        self.put([step("2026-03-05", "clarification_requested"),
                  step("2026-03-10", "fee_estimate", amount_cents=24550),
                  step("2026-03-12", "produced", partial=True, expected_on="2026-04-10"),
                  step("2026-03-14", "denied")])
        before = self.sent.read_bytes()
        self.assertEqual(self.response("withdrawn", "2026-03-20", "--note", NOTE)[0], 0)
        self.assertEqual([s["kind"] for s in self.entry()["steps"]][-2:], ["denied", "withdrawn"])
        self.assertNotEqual(self.sent.read_bytes(), before)

    def test_terminal_refusals_write_nothing(self):
        self.put([withdrawn()])
        for kind in kit_track.KINDS:
            with self.subTest(kind=kind):
                self.refused_untouched(lambda: self.response(kind, "2026-05-31", "--note", NOTE),
                                       "withdrawn by us; no step can follow withdrawn")
        # reference-only mutation is a step too: it cannot slip past the terminal step
        self.refused_untouched(lambda: self.response("acknowledged", "2026-05-31", "--reference", "R-9"),
                               "no step can follow withdrawn")
        # withdrawn after closed keeps the closed refusal
        self.put([step("2026-03-05", "denied"), step("2026-03-06", "closed")])
        self.refused_untouched(lambda: self.response("withdrawn", "2026-03-07", "--note", NOTE),
                               "no step can follow closed")
        # a different stored reference: exit 3, nothing written
        self.put([], reference="R-1")
        self.refused_untouched(lambda: self.response("withdrawn", "2026-03-07", "--note", NOTE, "--reference", "R-2"),
                               "reference already recorded", code=3)
        self.put([], reference="R-1")
        self.refused_untouched(lambda: self.response("withdrawn", "2026-03-07", "--note", NOTE, agency=self.b),
                               "no hand-sent request recorded")
        self.refused_untouched(lambda: self.response("withdrawn", "2026-03-07", "--note", NOTE, "--request", "ghost"),
                               "no sent request")
        self.refused_untouched(lambda: self.response("withdrawn", "2099-01-01", "--note", NOTE), "in the future")
        self.refused_untouched(lambda: self.response("withdrawn", "2026-03-01", "--note", NOTE),
                               "before the request was sent")
        self.put([step("2026-03-09", "acknowledged")])
        self.refused_untouched(lambda: self.response("withdrawn", "2026-03-08", "--note", NOTE),
                               "before the previous step")

    def test_required_note_and_forbidden_fields(self):
        for note, words in ((None, "withdrawn needs --note"), ("", "--note must be 1-200"), ("   ", "--note must be 1-200"),
                            ("x" * 201, "--note must be 1-200"), ("bell\x07", "control characters"),
                            ("see <b>this</b>", "plain text")):
            with self.subTest(note=note):
                extra = () if note is None else ("--note", note)
                self.refused_untouched(lambda: self.response("withdrawn", SENT_ON, *extra), words)
        self.assertEqual(self.response("withdrawn", "2026-03-05", "--note", "x" * 200)[0], 0)  # the 200 limit itself
        self.put([])
        for flags, words in ((("--partial",), "--partial is only for produced"),
                             (("--record", "abcdef012345"), "--record is only for produced"),
                             (("--amount", "20"), "--amount is only for"),
                             (("--expected-on", "2026-04-01"), "--expected-on is for the determination"),
                             (("--download-expires-on", "2026-04-01"), "--download-expires-on is only for produced"),
                             (("--withheld-basis", "text"), "--withheld-basis is only for denied or produced"),
                             (("--scope", "any", "--disposition", "withheld"), "--scope is not allowed on a withdrawn")):
            with self.subTest(flags=flags):
                self.refused_untouched(lambda: self.response("withdrawn", SENT_ON, "--note", NOTE, *flags), words)
        # other kinds keep an optional note
        self.assertEqual(self.response("acknowledged", "2026-03-05")[0], 0)

    def test_stored_withdrawal_is_validated_by_the_shared_replay_and_never_repaired(self):
        label = self.a
        good = [step("2026-03-05", "acknowledged"), withdrawn("2026-03-10")]
        kit_track.validate_steps({**self.entry(), "steps": good}, label)  # a clean history passes
        bad = {
            "no note": [step("2026-03-10", "withdrawn")],
            "unclean note": [step("2026-03-10", "withdrawn", note="  two  spaces ")],
            "markup note": [step("2026-03-10", "withdrawn", note="<b>x</b>")],
            "amount": [step("2026-03-10", "withdrawn", note=NOTE, amount_cents=100)],
            "expected_on": [step("2026-03-10", "withdrawn", note=NOTE, expected_on="2026-04-01")],
            "partial": [step("2026-03-10", "withdrawn", note=NOTE, partial=True)],
            "records": [step("2026-03-10", "withdrawn", note=NOTE, records=["a" * 64])],
            "scope answers": [step("2026-03-10", "withdrawn", note=NOTE, scope_answers={"x": "withheld"})],
            "step after": [withdrawn("2026-03-10"), step("2026-03-11", "acknowledged")],
            "second withdrawal": [withdrawn("2026-03-10"), withdrawn("2026-03-11")],
            "after closed": [step("2026-03-08", "denied"), step("2026-03-09", "closed"), withdrawn("2026-03-10")],
        }
        entry = {**self.entry(), "scopes": ["x"]}
        for name, steps in bad.items():
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    kit_track.validate_steps({**entry, "steps": steps}, label)
        # undo replays the prefix that would remain: a corrupt withdrawal in it refuses, nothing is repaired
        self.put([withdrawn("2026-03-10"), step("2026-03-11", "acknowledged"), step("2026-03-12", "extension")])
        before = self.snapshot()
        self.refused(run("kit-response-undo", "--directory", self.root, "--agency", self.a, "--reason", "oops"),
                     "no step can follow withdrawn")
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.root / "kit" / "sent-undone.json").exists())


class WithdrawnReadersTests(Base):
    # (name, steps before the withdrawal, state kit-due shows for it before)
    SCENARIOS = (
        ("overdue", [], "past_due"),
        ("waiting", [step("2026-03-05", "clarification_requested")], "waiting_on_you"),
        ("fee", [step("2026-03-06", "fee_estimate", amount_cents=20000)], "past_due"),
        ("stated", [step("2026-03-06", "determination", expected_on="2026-04-01")], None),
        ("installment", [step("2026-03-20", "produced", partial=True, expected_on="2026-04-15")],
         "installment_passed"),
        ("pending", [step("2026-05-20", "produced", download_expires_on="2026-06-10")], "download_pending"),
        ("expired", [step("2026-05-20", "produced", download_expires_on="2026-05-25")], "download_expired"),
    )

    def scenarios(self, *, withdraw):
        document = self.load()
        document["requests"][self.a]["steps"] = [withdrawn()] if withdraw else []
        for name, steps, _ in self.SCENARIOS:
            entry = copy.deepcopy(document["requests"][self.a])
            entry.update(draft=f"requests/{self.a}--{name}.md", steps=[*steps, *([withdrawn()] if withdraw else [])])
            document["requests"][f"{self.a}#{name}"] = entry
        self.save(document)

    def names(self, report):
        found = {(r["agency_id"], r["request"]) for bucket in ("open", "answered", "stated_passed") for r in report[bucket]}
        return {request for agency, request in found if agency == self.a}

    def test_all_due_buckets_and_today_drop_withdrawn(self):
        self.scenarios(withdraw=False)
        report = self.due(expect=3)
        listed = self.names(report)
        self.assertTrue({name for name, _, _ in self.SCENARIOS} <= listed, listed)  # every bucket really is reached
        states = {(r["request"], r["state"]) for r in report["open"]}
        for name, _, state in self.SCENARIOS:
            if state:
                self.assertIn((name, state), states)
        self.assertEqual([r["request"] for r in report["stated_passed"]], ["stated"])
        # the same history ended by us: no bucket, no count, no exit 3
        self.scenarios(withdraw=True)
        report = self.due()
        self.assertEqual((report["open"], report["answered"], report["stated_passed"]), ([], [], []))
        self.assertEqual({k: report[k] for k in ("past_due", "stated_date_passed", "installment_passed", "waiting_on_you")},
                         dict(past_due=0, stated_date_passed=0, installment_passed=0, waiting_on_you=0))
        self.assertNotIn("download_expired", report)
        self.assertNotIn("Waiting on you", report["note"])
        # a known withdrawn request named by --request is empty work, not an unknown request
        for name, _, _ in self.SCENARIOS:
            report = self.due("--request", name)
            self.assertEqual((report["open"], report["answered"]), ([], []), name)
        self.refused(run("kit-due", "--directory", self.root, "--as-of", AS_OF, "--request", "nonesuch"),
                     "no sent request")
        # another live request keeps its row, rank and exit 3; so do non-request to-dos
        self.add("live", [])
        content = self.root / "content" / "meetings"
        content.mkdir(parents=True)
        document = meeting("m-soon", "2026-06-10T18:00:00-07:00", "2026-06-08T12:00:00-07:00")
        document["participation"]["verified_at"] = "2026-05-25"
        (content / "m-soon.json").write_text(json.dumps(document), encoding="utf-8")
        report = self.due(expect=3)
        self.assertEqual([(r["request"], r["state"]) for r in report["open"]], [("live", "past_due")])
        items = today.todo(self.root, date(2026, 6, 1))
        self.assertEqual([i["kind"] for i in items], ["comment_deadline", "request_past_due"], items)
        self.assertEqual(sum(f"({self.a})" in i["what"] for i in items), 1)  # only the live request
        self.add("live", [withdrawn()])
        self.assertEqual([i["kind"] for i in today.todo(self.root, date(2026, 6, 1))], ["comment_deadline"])

    def test_calendar_timing_and_registry_drop_withdrawn(self):
        self.put([withdrawn()])
        ics = self.tmp / "out.ics"
        self.assertEqual(run("kit-calendar", "--directory", self.root, "--out", ics, "--as-of", AS_OF)[0], 0)
        self.assertNotIn(self.a, ics.read_text(encoding="utf-8"))
        self.put([step("2026-03-05", "acknowledged")])
        ics2 = self.tmp / "out2.ics"
        self.assertEqual(run("kit-calendar", "--directory", self.root, "--out", ics2, "--as-of", AS_OF)[0], 0)
        self.assertIn(self.a, ics2.read_text(encoding="utf-8"))
        # timing: the withdrawal is ours, not the agency's first response, and the request is not open-ended
        self.put([step("2026-03-05", "clarification_requested"), withdrawn("2026-03-20")])
        (row,) = kit_timing.report(self.root, AS_OF)["requests"]
        self.assertEqual((row["first_step_days"], row["open_age_days"]), (3, None))
        self.assertEqual([(s["end_on"], s["open"]) for s in row["clarification_spans"]], [("2026-03-20", False)])
        self.put([withdrawn("2026-03-20")])
        (row,) = kit_timing.report(self.root, AS_OF)["requests"]
        self.assertEqual((row["first_step_days"], row["open_age_days"]), (None, None))
        # registry: a withdrawn request is not counted as open
        self.assertEqual(registry._state(self.root)[:2], (1, 0))
        self.put([])
        self.assertEqual(registry._state(self.root)[:2], (1, 1))

    def test_followup_refuses_before_writes(self):
        self.put([withdrawn("2026-03-20")])
        folder = self.root / "kit" / "followups"
        for as_of in (AS_OF, "2026-03-25"):
            self.refused_untouched(lambda: run("kit-followup", "--directory", self.root, "--agency", self.a,
                                               "--as-of", as_of), "was withdrawn by us on 2026-03-20")
        self.assertFalse(folder.exists())
        # waiting history: the withdrawal wins over the exit-3 "waiting on you" refusal
        self.put([step("2026-03-05", "clarification_requested"), withdrawn("2026-03-20")])
        self.refused_untouched(lambda: run("kit-followup", "--directory", self.root, "--agency", self.a,
                                           "--as-of", AS_OF), "was withdrawn by us", code=1)
        # no law package: still the withdrawal, not a law message
        config = json.loads((self.root / "campaign.json").read_text(encoding="utf-8"))
        (self.root / "campaign.json").write_text(json.dumps({**config, "state": "WY"}), encoding="utf-8")
        self.refused_untouched(lambda: run("kit-followup", "--directory", self.root, "--agency", self.a,
                                           "--as-of", AS_OF), "was withdrawn by us")
        (self.root / "campaign.json").write_text(json.dumps(config), encoding="utf-8")
        # an existing draft survives --replace byte for byte
        self.put([])
        self.assertEqual(run("kit-followup", "--directory", self.root, "--agency", self.a, "--as-of", AS_OF)[0], 0)
        (draft,) = list(folder.glob("*.md"))
        kept = draft.read_bytes()
        self.put([withdrawn("2026-03-20")])
        self.refused(run("kit-followup", "--directory", self.root, "--agency", self.a, "--as-of", AS_OF, "--replace"),
                     "was withdrawn by us")
        self.assertEqual(draft.read_bytes(), kept)
        self.assertEqual(list(folder.iterdir()), [draft])

    def test_status_csv_and_legacy_identity(self):
        legacy_keys = set(self.status()[0][(self.a, "main")])
        self.assertNotIn("withdrawn_on", legacy_keys)
        legacy_csv = self.tmp / "legacy.csv"
        self.assertEqual(run("kit-status", "--directory", self.root, "--csv", legacy_csv)[0], 0)
        (self.root / "kit" / "request-settings.json").write_text(json.dumps({"schema_version": 1, "fee_cap_usd": 100}),
                                                                 encoding="utf-8")
        self.put([step("2026-03-06", "fee_estimate", amount_cents=24550),
                  step("2026-05-20", "produced", download_expires_on="2026-06-30", note="kept"), withdrawn()])
        before = self.sent.read_bytes()
        rows, out, err = self.status()
        row = rows[(self.a, "main")]
        self.assertEqual(self.sent.read_bytes(), before)
        self.assertEqual(set(row), legacy_keys | {"withdrawn_on"})
        self.assertEqual((row["withdrawn_on"], row["closed"]), ("2026-05-30", False))
        self.assertEqual(row["last_step"], {"on": "2026-05-30", "kind": "withdrawn"})
        # one JSON document on stdout; the phrase and nothing actionable on stderr
        self.assertEqual(json.loads(out)["requests"][0]["agency_id"], self.a)
        self.assertEqual(err, f"{self.a}: withdrawn by us on 2026-05-30\n")
        # fee and evidence history stays, is not reported as paid, and downloads are not pending work
        self.assertEqual((row["fee_estimate"], row["fee_state"], row["fee_paid"], row["over_fee_cap"]),
                         ("$245.50", "unpaid", None, True))
        self.assertEqual(row["producing"], False)
        self.assertNotIn("downloads_pending", row)
        # named request label, and a request ended by the agency is unchanged
        self.add("second", [withdrawn("2026-03-09")])
        self.assertIn(f"{self.a}/second: withdrawn by us on 2026-03-09\n", self.status()[2])
        self.put([step("2026-03-05", "denied"), step("2026-03-06", "closed")])
        rows, _, err = self.status()
        self.assertEqual((rows[(self.a, "main")]["closed"], "withdrawn_on" in rows[(self.a, "main")]), (True, False))
        self.assertNotIn(f"{self.a}: withdrawn", err)
        # CSV: header unchanged, last_step_kind withdrawn, closed no
        self.put([withdrawn()])
        csv_path = self.tmp / "now.csv"
        self.assertEqual(run("kit-status", "--directory", self.root, "--csv", csv_path)[0], 0)
        lines = list(csv.reader(csv_path.read_text(encoding="utf-8").splitlines()))
        self.assertEqual(lines[0], list(csv.reader(legacy_csv.read_text(encoding="utf-8").splitlines()))[0])
        header = lines[0]
        self.assertEqual(len(lines), 3)  # the header, the main request and the named one
        for cells in lines[1:]:
            self.assertEqual((cells[header.index("closed")], cells[header.index("last_step_kind")]), ("no", "withdrawn"))

    def test_reminders_and_legacy_status_bytes_without_a_withdrawal(self):
        self.put([step("2026-03-06", "fee_estimate", amount_cents=24550)])
        _, _, err = self.status()
        self.assertIn(f"{self.a}: fee estimate $245.50 is unpaid", err)  # still actionable when not withdrawn
        self.assertNotIn("withdrawn", err)
        rows, out, _ = self.status()
        self.assertNotIn("withdrawn_on", rows[(self.a, "main")])
        self.assertNotIn("withdrawn_on", out)


class WithdrawnAskedTests(ExampleBase):
    def test_asked_withdrawn_precedes_produced(self):
        self.write_sent([step("2026-03-12", "produced", scope_answers={A: "produced", B: "produced"}),
                         step("2026-03-15", "produced", partial=True, correspondence=["c" * 64]),
                         step("2026-03-25", "withdrawn", note=SYNTHETIC_PRIVATE_NOTE, correspondence=["d" * 64])],
                        extra={"reference": "PRIVATE-REF-5519"})
        before = self.path.read_bytes()
        code, out, err = self.propose()
        self.assertEqual((code, err), (0, ""))
        self.assertIn('"state": "withdrawn"', out)
        self.assertEqual(self.path.read_bytes(), before)  # dry run writes nothing
        self.assertEqual(self.propose("--apply")[0], 0)
        (item,) = self.asked()
        self.assertEqual(item, {"topic": f"{TITLE[A]}; {TITLE[B]}", "asked_on": SENT_ON, "state": "withdrawn",
                                "updated_on": "2026-03-25", "request": "main"})
        content = self.path.read_text(encoding="utf-8")
        for private in (SYNTHETIC_PRIVATE_NOTE, "PRIVATE-REF-5519", "c" * 64, "d" * 64):
            self.assertNotIn(private, content)

    def test_apply_updates_only_linked_fields_and_keeps_organizer_items(self):
        organizer = {"topic": "Hand typed", "asked_on": "2026-02-01", "state": "answered", "note": "my own words"}
        linked = {"topic": "Old topic", "asked_on": SENT_ON, "state": "partly_answered", "updated_on": "2026-03-12",
                  "request": "main", "note": "linked note"}
        self.set_asked([organizer, linked])
        self.write_sent([step("2026-03-12", "produced", partial=True), step("2026-03-25", "withdrawn", note=SYNTHETIC_PRIVATE_NOTE)])
        self.assertEqual(self.propose("--apply")[0], 0)
        first, second = self.asked()
        self.assertEqual(first, organizer)
        self.assertEqual(second, {"topic": f"{TITLE[A]}; {TITLE[B]}", "asked_on": SENT_ON, "state": "withdrawn",
                                  "updated_on": "2026-03-25", "request": "main", "note": "linked note"})
        again = self.propose("--apply")[1]
        self.assertEqual(again.splitlines()[0], "nothing to propose: content/agencies.json already follows kit/sent.json")

    def test_overflow_and_closed_semantics_are_unchanged(self):
        self.set_asked([{"topic": f"T{i}", "asked_on": SENT_ON, "state": "awaiting_response"} for i in range(20)])
        self.write_sent([step("2026-03-25", "withdrawn", note=SYNTHETIC_PRIVATE_NOTE)])
        before = self.path.read_bytes()
        code, out, err = self.propose("--apply")
        self.assertEqual(code, 3)
        self.assertIn("at most 20", err)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertNotIn(SYNTHETIC_PRIVATE_NOTE, out + err)
        self.set_asked(None)
        self.write_sent([step("2026-03-04", "acknowledged"), step("2026-03-20", "closed")], scopes=(A,))
        code, out, _ = self.propose("--apply")
        self.assertEqual(code, 0)
        self.assertIn("closed without production; organizer to choose", out)
        self.assertEqual(self.asked(), [])


class WithdrawnUndoTests(ExampleBase):
    def test_undo_withdrawal_reopens_without_public_write(self):
        prior = [step("2026-03-05", "acknowledged"), step("2026-03-08", "clarification_requested"),
                 step("2026-03-09", "clarified"),
                 step("2026-03-12", "produced", partial=True, expected_on="2026-04-10", records=["e" * 64],
                      correspondence=["f" * 64])]
        self.write_sent(prior)
        code, _, err = self.response("withdrawn", "2026-03-20", "--note", SYNTHETIC_PRIVATE_NOTE)
        self.assertEqual((code, err), (0, ""))
        content_before = self.path.read_bytes()
        before = self.entry()
        code, out, err = run("kit-response-undo", "--directory", self.root, "--agency", POLICE,
                             "--reason", "Withdrew the wrong request")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("removed the withdrawn step of 2026-03-20", out)
        after = self.entry()
        self.assertEqual(after["steps"], prior)
        self.assertEqual(len(after["undo_ids"]), 1)
        self.assertEqual(self.path.read_bytes(), content_before)  # undo never writes public content
        (receipt,) = json.loads((self.root / "kit" / "sent-undone.json").read_text(encoding="utf-8"))["undos"]
        self.assertEqual(receipt["step"]["kind"], "withdrawn")
        self.assertEqual(receipt["step"]["note"], SYNTHETIC_PRIVATE_NOTE)
        self.assertEqual(receipt["step"]["on"], "2026-03-20")
        self.assertEqual((receipt["reason"], receipt["undo_id"]), ("Withdrew the wrong request", after["undo_ids"][0]))
        self.assertEqual(receipt["entry_before_sha256"], kit_undo._digest(before))
        self.assertEqual(receipt["entry_after_sha256"], kit_undo._digest(after))
        # the request is open again, the prior history is live, and the next valid step is allowed
        row = self.status()[0][(POLICE, "main")]
        self.assertNotIn("withdrawn_on", row)
        self.assertEqual((row["producing"], row["next_installment"], row["waiting_on_you_since"]),
                         (True, "2026-04-10", None))
        self.assertEqual([r["state"] for r in self.due(expect=3)["open"]], ["installment_passed"])
        self.assertEqual(self.response("produced", "2026-04-02")[0], 0)
        self.assertEqual(self.entry()["steps"][-1]["kind"], "produced")

    def test_proposal_after_undo_reflects_retained_state_only(self):
        self.write_sent([step("2026-03-12", "produced", partial=True)])
        self.assertEqual(self.response("withdrawn", "2026-03-20", "--note", NOTE)[0], 0)
        self.assertEqual(self.propose("--apply")[0], 0)
        self.assertEqual([i["state"] for i in self.asked()], ["withdrawn"])
        applied = self.path.read_bytes()
        self.assertEqual(run("kit-response-undo", "--directory", self.root, "--agency", POLICE,
                             "--reason", "Mistake")[0], 0)
        self.assertEqual(self.path.read_bytes(), applied)  # undo itself never edits public content
        code, out, _ = self.propose()
        self.assertIn('state: "withdrawn" -> "partly_answered"', out)
        self.assertEqual(self.path.read_bytes(), applied)  # and a dry run does not either
        self.assertEqual(self.propose("--apply")[0], 0)
        self.assertEqual([i["state"] for i in self.asked()], ["partly_answered"])


if __name__ == "__main__":
    unittest.main()
