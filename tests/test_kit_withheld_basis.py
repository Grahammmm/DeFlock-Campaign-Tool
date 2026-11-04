"""The agency's own stated withheld basis on kit requests (DF1207-01). Offline, synthetic."""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import cli  # noqa: E402
from tests.test_kit_check import ready_drafts  # noqa: E402

FLAG = "--withheld-basis"
FOOTER = "Read-only. These are the agencies' own words; no legal reading is added.\n"
NEW = "withheld_basis"
OLD_DRAFT = """# Denial reply draft: {name}

DRAFT — NOT SENT. Review before sending it yourself.

To: {name}, records custodian

On 2026-03-02 we sent a records request by email to {name}. On 2026-03-20 we recorded the agency's response
as a denial. We asked for: [EDIT ME: what you asked for]

Please:
1. Tell us which exemption or other legal basis the agency relied on for each record or category withheld.
2. Send the parts of the requested records that are not exempt, with only the exempt portions redacted.
3. Explain the denial in writing.

Thank you,

[name]

---
Draft only. Not legal advice.
law package: draft
"""


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
    return {str(p.relative_to(root)): p.read_bytes() for p in Path(root).rglob("*") if p.is_file()}


class WithheldBasisTests(unittest.TestCase):
    def setUp(self):
        for target in ("socket.socket", "socket.create_connection", "urllib.request.urlopen"):
            offline = mock.patch(target, side_effect=AssertionError("network used"))
            offline.start()
            self.addCleanup(offline.stop)
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-kit-withheld-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        for command in (("init", "--name", "Synthetic", "--county", "San Luis Obispo", "--state", "CA"), ("kit",)):
            code, _, err = run(command[0], "--directory", self.root, *command[1:])
            self.assertEqual(code, 0, err)
        self.a, self.b = sorted(p.stem for p in (self.root / "kit" / "requests").glob("*.md"))[:2]
        ready_drafts(self.root)
        self.sent = self.root / "kit" / "sent.json"

    def cli(self, command, agency, *extra):
        return run(command, "--directory", self.root, "--agency", agency, *extra)

    def send(self, agency):
        code, _, err = self.cli("kit-sent", agency, "--date", "2026-03-02", "--channel", "email")
        self.assertEqual(code, 0, err)

    def step(self, agency, kind, *extra, on="2026-03-20"):
        code, out, err = self.cli("kit-response", agency, "--date", on, "--kind", kind, *extra)
        self.assertEqual(code, 0, err)
        return json.loads(out[:out.rindex("}") + 1])

    def stored(self, agency):
        return json.loads(self.sent.read_text(encoding="utf-8"))["requests"][agency]["steps"]

    def refused(self, kind, *extra, words=FLAG):
        before = self.sent.read_bytes()
        code, out, err = self.cli("kit-response", self.a, "--date", "2026-03-20", "--kind", kind, *extra)
        self.assertEqual(code, 1, out)
        self.assertTrue(err.startswith("Stopped: "), err)
        self.assertIn(words, err)
        self.assertEqual(self.sent.read_bytes(), before)
        return err

    def report(self, command, *extra):
        code, out, err = run(command, "--directory", self.root, *extra)
        self.assertEqual(code, 0, err)
        return json.loads(out), err

    def status_rows(self):
        return self.report("kit-status")[0]["requests"]

    def edit(self, change):
        document = json.loads(self.sent.read_text(encoding="utf-8"))
        change(document["requests"][self.a]["steps"])
        self.sent.write_text(json.dumps(document), encoding="utf-8")

    def test_denied_with_two_bases_stored_in_order_and_listed_by_status(self):
        self.send(self.a)
        printed = self.step(self.a, "denied", FLAG, "  Gov. Code § 7923.600  ", FLAG, "investigative records")
        self.assertEqual(printed["steps"][0][NEW], ["Gov. Code § 7923.600", "investigative records"])
        self.assertEqual(self.stored(self.a)[0][NEW], ["Gov. Code § 7923.600", "investigative records"])
        row = next(r for r in self.status_rows() if r["agency_id"] == self.a)
        self.assertEqual(row[NEW], [{"on": "2026-03-20", "kind": "denied", "basis": "Gov. Code § 7923.600"},
                                    {"on": "2026-03-20", "kind": "denied", "basis": "investigative records"}])

    def test_produced_step_takes_a_basis_and_a_step_without_one_stores_no_key(self):
        self.send(self.a)
        self.step(self.a, "acknowledged", on="2026-03-05")
        self.step(self.a, "produced", "--partial", FLAG, "personnel records withheld", on="2026-03-10")
        self.step(self.a, "produced", on="2026-03-15")
        steps = self.stored(self.a)
        self.assertNotIn(NEW, steps[0])
        self.assertEqual(steps[1][NEW], ["personnel records withheld"])
        self.assertNotIn(NEW, steps[2])
        row = next(r for r in self.status_rows() if r["agency_id"] == self.a)
        self.assertEqual(row[NEW], [{"on": "2026-03-10", "kind": "produced", "basis": "personnel records withheld"}])

    def test_other_kinds_refuse_the_flag_and_sent_json_bytes_are_unchanged(self):
        self.send(self.a)
        self.step(self.a, "acknowledged", on="2026-03-05")
        for kind, extra in (("acknowledged", ()), ("extension", ()), ("fee_estimate", ("--amount", "10")),
                            ("closed", ()), ("clarification_requested", ())):
            err = self.refused(kind, FLAG, "x", *extra, words="--withheld-basis is only for denied or produced")
            self.assertNotIn("Traceback", err)

    def test_bad_values_are_each_refused_naming_the_flag_and_nothing_is_written(self):
        self.send(self.a)
        for bad in ("x" * 201, "", "   ", "line\nbreak", "bell\x07", "tab\there", "trailing\n", "\x00", "zero\u200bwidth"):
            self.refused("denied", FLAG, bad)
        self.refused("denied", FLAG, "same", FLAG, " same ")  # a duplicate after stripping
        self.refused("denied", *[arg for n in range(6) for arg in (FLAG, f"reason {n}")], words="at most 5")
        self.refused("denied", FLAG, "ok", FLAG, "")
        self.assertEqual(self.stored(self.a), [])

    def test_limits_are_inclusive(self):
        self.send(self.a)
        edge = "y" * 200
        self.step(self.a, "denied", FLAG, edge, *[arg for n in range(4) for arg in (FLAG, f"reason {n}")])
        self.assertEqual(len(self.stored(self.a)[0][NEW]), 5)
        self.assertEqual(self.stored(self.a)[0][NEW][0], edge)

    def test_old_sent_json_without_the_key_reads_unchanged(self):
        self.send(self.a)
        self.step(self.a, "denied")
        old_bytes = self.sent.read_bytes()
        commands = (("kit-status",), ("kit-due", "--as-of", "2026-04-01"), ("kit-coverage", "--csv"))
        before = {c: run(c[0], "--directory", self.root, *c[1:]) for c in commands}
        self.assertEqual(self.sent.read_bytes(), old_bytes)
        row = json.loads(before[("kit-status",)][1])["requests"][0]
        self.assertEqual(row[NEW], [])
        self.edit(lambda steps: steps[0].update({NEW: ["Gov. Code § 7923.600"]}))
        for c in commands:  # a recorded basis changes nothing but the new status field
            code, out, err = run(c[0], "--directory", self.root, *c[1:])
            self.assertEqual((code, err), before[c][::2])
            if c[0] == "kit-status":
                after = json.loads(out)
                self.assertEqual(after["requests"][0].pop(NEW), [{"on": "2026-03-20", "kind": "denied",
                                                                   "basis": "Gov. Code § 7923.600"}])
                old = json.loads(before[c][1])
                old["requests"][0].pop(NEW)
                self.assertEqual(after, old)
            else:
                self.assertEqual(out, before[c][1])

    def test_hand_edited_malformed_basis_is_refused_as_malformed_steps(self):
        self.send(self.a)
        self.step(self.a, "denied", on="2026-03-10")
        self.step(self.a, "closed", on="2026-03-15")
        original = self.sent.read_text(encoding="utf-8")
        for change in (lambda s: s[0].update({NEW: "Gov. Code 7923.600"}),
                       lambda s: s[0].update({NEW: [f"r{n}" for n in range(6)]}),
                       lambda s: s[0].update({NEW: []}),
                       lambda s: s[0].update({NEW: ["same", "same"]}),
                       lambda s: s[0].update({NEW: ["x" * 201]}),
                       lambda s: s[0].update({NEW: ["bad\nline"]}),
                       lambda s: s[0].update({NEW: [" padded "]}),
                       lambda s: s[0].update({NEW: [7]}),
                       lambda s: s[1].update({NEW: ["on a closed step"]})):
            self.sent.write_text(original, encoding="utf-8")
            self.edit(change)
            before = self.sent.read_bytes()
            for command in (("kit-status",), ("kit-withheld",)):
                code, _, err = run(command[0], "--directory", self.root)
                self.assertEqual(code, 1)
                self.assertIn("kit/sent.json unreadable: malformed steps", err)
            code, _, err = self.cli("kit-denial-reply", self.a)
            self.assertEqual(code, 1)
            self.assertIn("malformed steps", err)
            code, _, err = self.cli("kit-response", self.a, "--date", "2026-03-20", "--kind", "fee_estimate",
                                    "--amount", "5")
            self.assertEqual(code, 1)
            self.assertIn("malformed steps", err)
            self.assertEqual(self.sent.read_bytes(), before)

    def test_denial_reply_quotes_each_stated_basis_and_narrows_the_first_request(self):
        self.send(self.a)
        self.step(self.a, "produced", "--partial", FLAG, "personnel files", on="2026-03-10")
        self.step(self.a, "denied", FLAG, 'Gov. Code § 7923.600 "records of investigation"', FLAG, "too broad")
        code, out, err = self.cli("kit-denial-reply", self.a)
        self.assertEqual(code, 0, err)
        text = (self.root / "kit" / "followups" / f"{self.a}-denial-2026-03-20.md").read_text(encoding="utf-8")
        self.assertIn("The agency stated these reasons, as written:\n"
                      '- "personnel files" (produced on 2026-03-10)\n'
                      '- "Gov. Code § 7923.600 "records of investigation"" (denied on 2026-03-20)\n'
                      '- "too broad" (denied on 2026-03-20)\n\nPlease:\n', text)
        self.assertIn("1. For each record or category withheld that the reasons above do not cover, tell us which "
                      "exemption or other legal basis the agency relied on.\n2. Send the parts", text)
        self.assertNotIn("1. Tell us which exemption", text)

    def test_denial_reply_without_bases_is_byte_identical_to_the_old_draft(self):
        self.send(self.a)
        self.step(self.a, "produced", "--partial", on="2026-03-10")
        self.step(self.a, "denied")
        code, out, err = self.cli("kit-denial-reply", self.a)
        self.assertEqual(code, 0, err)
        name = next(a["name"] for a in json.loads((self.root / "kit" / "agencies.json").read_text("utf-8"))
                    if a["agency_id"] == self.a)
        text = (self.root / "kit" / "followups" / f"{self.a}-denial-2026-03-20.md").read_text(encoding="utf-8")
        self.assertEqual(text, OLD_DRAFT.format(name=name))

    def test_withheld_groups_every_basis_by_section_number(self):
        self.send(self.a)
        self.send(self.b)
        self.step(self.a, "denied", FLAG, "Gov. Code § 7923.600", FLAG, "investigative records")
        self.step(self.b, "produced", "--partial", FLAG, "GC 7922.000", on="2026-03-10")
        self.step(self.b, "denied", FLAG, "Government Code section 7927.705(a) and gov code 7923.600")
        before = snapshot(self.root)
        report, err = self.report("kit-withheld", "--json")
        self.assertEqual(err, FOOTER)
        self.assertEqual(snapshot(self.root), before)
        got = [(g["section"], g["count"], [(r["agency_id"], r["kind"], r["basis"]) for r in g["rows"]])
               for g in report["groups"]]
        self.assertEqual(got, [
            ("7922.000", 1, [(self.b, "produced", "GC 7922.000")]),
            ("7923.600", 2, [(self.a, "denied", "Gov. Code § 7923.600"),
                             (self.b, "denied", "Government Code section 7927.705(a) and gov code 7923.600")]),
            ("7927.705", 1, [(self.b, "denied", "Government Code section 7927.705(a) and gov code 7923.600")]),
            ("no section number stated", 1, [(self.a, "denied", "investigative records")])])
        self.assertEqual(report["total"], 4)
        self.assertEqual(set(report["groups"][0]["rows"][0]), {"agency_id", "request", "on", "kind", "basis"})
        self.assertEqual(self.report("kit-withheld", "--json")[0], report)  # stable order
        code, out, err = run("kit-withheld", "--directory", self.root)
        self.assertEqual((code, err), (0, FOOTER))
        self.assertIn("Gov. Code 7923.600\n", out)
        self.assertIn(f'  {self.a}  denied on 2026-03-20  "investigative records"\n', out)
        self.assertEqual(snapshot(self.root), before)

    def test_withheld_sorts_sections_by_number_not_text(self):
        self.send(self.a)
        self.step(self.a, "denied", FLAG, "Gov. Code 7923.600", FLAG, "Gov. Code 7923.60", FLAG, "Gov. Code 7923",
                  FLAG, "Gov. Code 7922.535")
        self.assertEqual([g["section"] for g in self.report("kit-withheld", "--json")[0]["groups"]],
                         ["7922.535", "7923", "7923.60", "7923.600"])

    def test_withheld_on_a_folder_with_no_sent_json_is_an_empty_report(self):
        self.assertFalse(self.sent.exists())
        before = snapshot(self.root)
        report, err = self.report("kit-withheld", "--json")
        self.assertEqual((report["groups"], report["total"], err), ([], 0, FOOTER))
        code, out, err = run("kit-withheld", "--directory", self.root)
        self.assertEqual((code, err), (0, FOOTER))
        self.assertEqual(snapshot(self.root), before)
        self.assertFalse(self.sent.exists())


if __name__ == "__main__":
    unittest.main()
