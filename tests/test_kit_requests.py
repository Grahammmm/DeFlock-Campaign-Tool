"""kit-sent --request: several hand-sent requests per agency, each with its own draft and hash. Offline, synthetic."""
import contextlib
import hashlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import cli, law  # noqa: E402
from tests.test_kit_check import ready_drafts  # noqa: E402

NAME = "contract-2026"


def run(*arguments):
    """Run the CLI in-process (so the urlopen patch applies); return (code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with mock.patch.object(sys, "argv", ["campaign_tool", *map(str, arguments)]), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            cli.main()
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


class KitRequestsTests(unittest.TestCase):
    def setUp(self):
        offline = mock.patch("urllib.request.urlopen", side_effect=AssertionError("network used"))
        offline.start()
        self.addCleanup(offline.stop)
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-kit-requests-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        code, _, err = run("init", "--directory", self.root, "--name", "Synthetic", "--county", "San Luis Obispo",
                           "--state", "CA")
        self.assertEqual(code, 0, err)
        code, _, err = run("kit", "--directory", self.root)
        self.assertEqual(code, 0, err)
        self.kit = self.root / "kit"
        self.sent = self.kit / "sent.json"
        drafts = sorted((self.kit / "requests").glob("*.md"))
        self.agency, self.other = drafts[0].stem, drafts[1].stem
        ready_drafts(self.root)
        self.main_draft = drafts[0]
        self.named_draft = self.kit / "requests" / f"{self.agency}--{NAME}.md"
        self.named_draft.write_text("Narrowed request: the 2026 contract only.\n", encoding="utf-8")
        self.package = law.load_package("us-ca")

    def send(self, sent_on="2026-03-02", *extra, agency=None):
        return run("kit-sent", "--directory", self.root, "--agency", agency or self.agency, "--date", sent_on,
                   "--channel", "email", *extra)

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

    def rows(self, *extra):
        return json.loads(self.ok(run("kit-status", "--directory", self.root, *extra)))["requests"]

    def due(self, *extra):
        code, out, err = run("kit-due", "--directory", self.root, "--as-of", "2026-03-04", *extra)
        self.assertIn(code, (0, 3), err)
        return json.loads(out)

    def respond(self, on, kind, *extra):
        return run("kit-response", "--directory", self.root, "--agency", self.agency, "--date", on, "--kind", kind,
                   *extra)

    def old_file(self):
        """A one-per-agency kit/sent.json exactly as main's kit-sent wrote it (indent 2, sorted keys, newline)."""
        entries = {}
        for agency_id, sent_on in ((self.agency, "2026-03-02"), (self.other, "2026-02-20")):
            draft = self.kit / "requests" / f"{agency_id}.md"
            entries[agency_id] = {"sent_on": sent_on, "channel": "portal", "draft": f"requests/{agency_id}.md",
                                  "draft_sha256": hashlib.sha256(draft.read_bytes()).hexdigest(),
                                  "recorded_at": "2026-03-02T10:00:00+00:00", "steps": []}
        document = {"schema_version": 1, "requests": dict(sorted(entries.items()))}
        self.sent.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return document

    def test_old_one_per_agency_file_reads_as_main_and_steps_leave_other_entries_identical(self):
        document = self.old_file()
        before = self.sent.read_bytes()
        rows = self.rows()
        self.assertEqual(self.sent.read_bytes(), before)
        self.assertEqual([(r["agency_id"], r["request"]) for r in rows], [(self.other, "main"), (self.agency, "main")])
        for row in rows:
            entry = document["requests"][row["agency_id"]]
            self.assertEqual((row["sent_on"], row["channel"], row["steps"], row["draft_changed_since_sent"]),
                             (entry["sent_on"], "portal", 0, False))
            self.assertEqual(row["determination_due"], law.deadline(self.package, entry["sent_on"]).isoformat())
        self.assertEqual(set(rows[0]), {"agency_id", "request", "name", "sent_on", "channel", "draft_changed_since_sent",
                                        "determination_due", "with_extension", "steps", "extended", "closed",
                                        "last_step", "records", "record_prefixes", "fee_estimate", "over_fee_cap",
                                        "stated_production_date", "waiting_on_you_since", "producing",
                                        "next_installment", "reference", "fee_state", "fee_paid", "withheld_basis", "scopes"})
        self.assertEqual([r["withheld_basis"] for r in rows], [[], []])
        self.assertEqual([(r["producing"], r["next_installment"]) for r in rows], [(False, None), (False, None)])
        printed = json.loads(self.ok(self.respond("2026-03-03", "acknowledged")).split("Recorded only.")[0])
        self.assertEqual(printed["request"], "main")
        after = json.loads(self.sent.read_text())
        [step] = after["requests"][self.agency]["steps"]
        document["requests"][self.agency]["steps"] = [step]
        self.assertEqual(self.sent.read_bytes(),
                         (json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))

    def test_second_request_has_own_row_draft_hash_and_change_flag(self):
        self.ok(self.send("2026-03-02"))
        printed = json.loads(self.ok(self.send("2026-03-03", "--request", NAME)).split("Recorded only.")[0])
        self.assertEqual((printed["request"], printed["draft"]), (NAME, f"requests/{self.agency}--{NAME}.md"))
        requests = json.loads(self.sent.read_text())["requests"]
        self.assertEqual(sorted(requests), [self.agency, f"{self.agency}#{NAME}"])
        self.assertEqual(requests[f"{self.agency}#{NAME}"]["draft_sha256"],
                         hashlib.sha256(self.named_draft.read_bytes()).hexdigest())
        self.assertEqual(requests[self.agency]["draft_sha256"],
                         hashlib.sha256(self.main_draft.read_bytes()).hexdigest())
        rows = self.rows()
        self.assertEqual([(r["agency_id"], r["request"]) for r in rows], [(self.agency, "main"), (self.agency, NAME)])
        self.named_draft.write_text("Edited after sending.\n", encoding="utf-8")
        self.assertEqual([r["draft_changed_since_sent"] for r in self.rows()], [False, True])
        self.main_draft.write_text("Edited too.\n", encoding="utf-8")
        self.assertEqual([r["draft_changed_since_sent"] for r in self.rows()], [True, True])
        self.assertEqual([r["request"] for r in self.rows("--request", NAME)], [NAME])
        self.assertEqual(self.rows("--request", "other-name"), [])

    def test_named_request_sorts_by_date_then_agency_then_name(self):
        self.ok(self.send("2026-03-05"))
        self.ok(self.send("2026-03-02", "--request", NAME))
        self.ok(self.send("2026-03-02", agency=self.other))
        rows = self.rows()
        expected = sorted([("2026-03-05", self.agency, "main"), ("2026-03-02", self.agency, NAME),
                           ("2026-03-02", self.other, "main")])
        self.assertEqual([(r["sent_on"], r["agency_id"], r["request"]) for r in rows], expected)

    def test_named_draft_missing_symlinked_or_unfilled_refused_and_nothing_written(self):
        self.named_draft.unlink()
        self.refused(self.send("2026-03-02", "--request", NAME), f"kit/requests/{self.agency}--{NAME}.md",
                     "regular file")
        self.assertFalse(self.sent.exists())
        target = self.kit / "elsewhere.md"
        target.write_text("Narrowed request.\n", encoding="utf-8")
        self.named_draft.symlink_to(target)
        self.refused(self.send("2026-03-02", "--request", NAME), "regular file")
        self.assertFalse(self.sent.exists())
        self.named_draft.unlink()
        self.named_draft.write_text("Records for [date range] please.\n", encoding="utf-8")
        self.refused(self.send("2026-03-02", "--request", NAME), "unfilled placeholder")
        self.assertFalse(self.sent.exists())
        # The main draft passing kit-check does not let a named request with its own unfilled draft through.
        self.ok(self.send("2026-03-02"))
        before = self.sent.read_bytes()
        self.refused(self.send("2026-03-03", "--request", NAME), "unfilled placeholder")
        self.assertEqual(self.sent.read_bytes(), before)

    def test_request_name_whose_draft_is_another_agencys_draft_refused(self):
        path = self.kit / "agencies.json"
        agencies = json.loads(path.read_text())
        twin = {**next(a for a in agencies if a["agency_id"] == self.agency), "agency_id": f"{self.agency}--{NAME}"}
        path.write_text(json.dumps([*agencies, twin], indent=2))
        self.refused(self.send("2026-03-02", "--request", NAME), "another agency's draft")
        self.assertFalse(self.sent.exists())

    def test_bad_names_refused_and_hash_main_key_is_malformed(self):
        for bad in ("Main", "-x", "a" * 41, "a/b", "main#x", "", "a_b", "ünder"):
            with self.subTest(name=bad):
                flag = f"--request={bad}"  # the = form, so argparse passes "-x" through as a value
                self.refused(self.send("2026-03-02", flag), "--request must be")
                self.assertFalse(self.sent.exists())
                self.refused(self.respond("2026-03-03", "acknowledged", flag), "--request must be")
                self.refused(run("kit-due", "--directory", self.root, flag), "--request must be")
                self.refused(run("kit-status", "--directory", self.root, flag), "--request must be")
        longest = "a" * 40
        (self.kit / "requests" / f"{self.agency}--{longest}.md").write_text("Ok.\n", encoding="utf-8")
        self.ok(self.send("2026-03-02", "--request", longest))
        document = self.old_file()
        entry = {**document["requests"][self.agency], "draft": f"requests/{self.agency}--main.md"}
        for key in (f"{self.agency}#main", f"{self.agency}#Bad", f"{self.agency}#a#b", f"{self.agency}#"):
            with self.subTest(key=key):
                self.sent.write_text(json.dumps({"schema_version": 1, "requests": {key: entry}}))
                before = self.sent.read_bytes()
                self.refused(run("kit-status", "--directory", self.root), "malformed entry", key)
                self.refused(self.send("2026-03-02"), "malformed entry")
                self.assertEqual(self.sent.read_bytes(), before)
        wrong = {**entry, "draft": f"requests/{self.agency}.md"}  # named key bound to the main draft
        self.sent.write_text(json.dumps({"schema_version": 1, "requests": {f"{self.agency}#{NAME}": wrong}}))
        self.refused(run("kit-status", "--directory", self.root), "malformed entry")

    def test_response_unknown_request_refused_and_steps_land_on_the_named_request_only(self):
        self.ok(self.send("2026-03-02"))
        self.ok(self.send("2026-03-03", "--request", NAME))
        before = self.sent.read_bytes()
        self.refused(self.respond("2026-03-04", "acknowledged", "--request", "nope"),
                     f"no sent request {self.agency}/nope")
        self.assertEqual(self.sent.read_bytes(), before)
        self.refused(run("kit-response", "--directory", self.root, "--agency", f"{self.agency}#{NAME}", "--date",
                         "2026-03-04", "--kind", "acknowledged"), "no hand-sent request recorded")
        self.assertEqual(self.sent.read_bytes(), before)
        self.ok(self.respond("2026-03-04", "acknowledged"))
        printed = json.loads(self.ok(self.respond("2026-03-05", "denied", "--request", NAME)).split("Recorded")[0])
        self.assertEqual((printed["request"], [s["kind"] for s in printed["steps"]]), (NAME, ["denied"]))
        requests = json.loads(self.sent.read_text())["requests"]
        self.assertEqual([s["kind"] for s in requests[self.agency]["steps"]], ["acknowledged"])
        self.assertEqual([s["kind"] for s in requests[f"{self.agency}#{NAME}"]["steps"]], ["denied"])
        self.ok(self.respond("2026-03-06", "closed", "--request", NAME))
        self.refused(self.respond("2026-03-07", "produced", "--request", NAME), f"{self.agency}/{NAME} is closed")
        self.ok(self.respond("2026-03-07", "produced"))  # main is still open

    def test_replace_touches_only_the_named_request(self):
        self.ok(self.send("2026-03-02"))
        self.ok(self.send("2026-03-03", "--request", NAME))
        before = self.sent.read_bytes()
        self.refused(self.send("2026-03-04", "--request", NAME), f"{self.agency}/{NAME} already recorded",
                     "2026-03-03")
        self.assertEqual(self.sent.read_bytes(), before)
        main_before = json.loads(before)["requests"][self.agency]
        self.ok(self.send("2026-03-04", "--request", NAME, "--replace"))
        requests = json.loads(self.sent.read_text())["requests"]
        self.assertEqual(requests[self.agency], main_before)
        self.assertEqual(requests[f"{self.agency}#{NAME}"]["sent_on"], "2026-03-04")
        self.ok(self.respond("2026-03-05", "acknowledged", "--request", NAME))
        before = self.sent.read_bytes()
        self.refused(self.send("2026-03-06", "--request", NAME, "--replace"), f"{self.agency}/{NAME} has recorded steps")
        self.assertEqual(self.sent.read_bytes(), before)
        self.ok(self.send("2026-03-06", "--replace"))  # main has no steps: still replaceable
        self.assertEqual(json.loads(self.sent.read_text())["requests"][self.agency]["sent_on"], "2026-03-06")

    def test_due_lists_both_requests_with_their_own_dates_and_filters_by_name(self):
        self.ok(self.send("2026-02-20"))
        self.ok(self.send("2026-03-03", "--request", NAME))
        report = self.due()
        self.assertEqual([(r["agency_id"], r["request"], r["sent_on"], r["due"]) for r in report["open"]],
                         [(self.agency, "main", "2026-02-20", law.deadline(self.package, "2026-02-20").isoformat()),
                          (self.agency, NAME, "2026-03-03", law.deadline(self.package, "2026-03-03").isoformat())])
        self.assertEqual([r["request"] for r in self.due("--request", NAME)["open"]], [NAME])
        self.assertEqual([r["request"] for r in self.due("--request", "main")["open"]], ["main"])
        self.refused(run("kit-due", "--directory", self.root, "--request", "nope"), "no sent request */nope")
        self.ok(self.respond("2026-03-04", "determination", "--request", NAME))
        report = self.due()
        self.assertEqual([r["request"] for r in report["open"]], ["main"])
        self.assertEqual([(r["request"], r["last_step"]["kind"]) for r in report["answered"]], [(NAME, "determination")])


if __name__ == "__main__":
    unittest.main()
