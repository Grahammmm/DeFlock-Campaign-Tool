"""DF2007-15: the advisory wording warnings (`intent`, `undated-count`) show on every reporting lint and change nothing else.

Each test drives a real command or helper twice: once with advisory-only wording (the warning appears; exit code, artifacts and
state equal the control) and once clean (no `warning:` text anywhere), plus mixed legacy input and the legacy refusal order.
"""
import csv
import datetime as dt
import importlib.util
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.flock import test_custody_report as ct
from tests.flock import test_deadlines as td
from tests.flock import test_legal_claim_guard as guard  # modules only: their TestCases are not re-collected here
from tests.flock import test_publish as tp
from tests.flock import test_rehearse_agency as rt
from tests.flock import test_report as tr
from tests.records import test_collection_triage as tt

ROOT = Path(__file__).resolve().parents[2]
FIX_INTENT = "Fix: Describe what the record says; do not infer intent."
FIX_COUNT = "Fix: Add a date or describe the count as documented or mapped."
ADVICE = "The sample department secretly shared data from 40 cameras."
INTENT_ONLY = "The sample department secretly shared data."
COUNT_ONLY = "There are 40 cameras."
LEGAL = "Agency X violated SB 34."


def tool(name):
    spec = importlib.util.spec_from_file_location("_df2007_" + name, ROOT / "tools" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def warnings_in(text):
    return [line for line in text.splitlines() if line.startswith("warning: ")]


def both(where):
    return ["warning: %s: wording advisory (intent). %s" % (where, FIX_INTENT),
            "warning: %s: wording advisory (undated-count). %s" % (where, FIX_COUNT)]


class ReportAndPublishSurfaceTest(unittest.TestCase):
    setUp, stage = tr.FlockReportTest.setUp, tr.FlockReportTest.stage

    def test_report_build_warns_exit_zero_and_the_draft_is_written(self):
        config, p = self.stage(edit=lambda text, _: text.replace(tr.BASIS, tr.BASIS + " " + ADVICE))
        code, out = tr.build(config)
        self.assertEqual(code, 0, out)
        found = warnings_in(out)
        self.assertEqual(len(found), 2, out)
        self.assertRegex(found[0], r"^warning: line \d+: wording advisory \(intent\)\. " + FIX_INTENT + "$")
        self.assertRegex(found[1], r"^warning: line \d+: wording advisory \(undated-count\)\. " + FIX_COUNT + "$")
        for leaked in ("secretly", "sample department", "40 cameras"):
            self.assertNotIn(leaked, out.lower())
        self.assertTrue((p["CO"] / "report" / "REPORT-DRAFT.md").is_file())
        # the control: no advisory wording, no warning text, the exact old output
        config, _ = self.stage()
        self.assertEqual(tr.build(config), (0, tr.HAPPY))

    def test_report_mixed_legacy_flag_keeps_its_notes_first(self):
        config, _ = self.stage(edit=lambda text, _: text.replace(tr.BASIS, tr.BASIS + " " + LEGAL + " " + COUNT_ONLY))
        code, out = tr.build(config)
        self.assertEqual(code, 0, out)
        lines = out.splitlines()
        legal = [i for i, line in enumerate(lines) if line.startswith("review-required: line") and "legal-claim wording" in line]
        warned = [i for i, line in enumerate(lines) if line.startswith("warning: ")]
        self.assertEqual((len(legal), len(warned)), (1, 1), out)
        self.assertLess(legal[0], warned[0])          # review_notes order: legal notes, then the warnings, then quote-gated notes
        self.assertTrue(lines[warned[0] + 1].endswith("quote-gated wording"), out)


class PublishSurfaceTest(unittest.TestCase):
    setUp, export = tp.FlockPublishTest.setUp, tp.FlockPublishTest.export
    fixture_text = tp.FlockPublishTest.fixture_text

    def edit_claim(self, claim):
        with open(self.run_dir / "FINDINGS.csv", newline="") as handle:
            table = list(csv.reader(handle))
        row = next(r for r in table if r[0] == "O1")
        row[table[0].index("claim")] = claim
        with open(self.run_dir / "FINDINGS.csv", "w", newline="") as handle:
            csv.writer(handle, lineterminator="\n").writerows(table)

    def test_publish_export_and_check_warn_and_stay_pending(self):
        self.edit_claim(ADVICE)
        code, out = self.export()
        self.assertEqual(code, 0, out)
        self.assertEqual(warnings_in(out), both("finding O1"), out)
        self.assertNotIn("refused=1", out)
        entry = next(f for f in json.loads(self.out.read_text())["findings"] if f["finding_id"] == "O1")
        self.assertEqual(entry["review"]["status"], "pending")  # the human gate still decides; no warning approves anything
        code, out = tp.run("flock_publish.py", "--check", str(self.out))
        self.assertEqual(code, 0, out)
        self.assertEqual(warnings_in(out), both("finding O1"), out)

    def test_publish_clean_export_has_no_warning_and_mixed_keeps_legacy_first(self):
        code, out = self.export()
        self.assertEqual((code, warnings_in(out)), (0, []), out)
        self.out.unlink()
        self.edit_claim(LEGAL + " " + COUNT_ONLY)
        code, out = self.export()
        self.assertEqual(code, 0, out)
        lines = out.splitlines()
        legacy = [i for i, line in enumerate(lines) if line.startswith("review-required: finding O1: legal-claim wording")]
        warned = [i for i, line in enumerate(lines) if line.startswith("warning: finding O1")]
        self.assertEqual((len(legacy), len(warned)), (1, 1), out)
        self.assertLess(legacy[0], warned[0])


class PageNewsletterAndSiteSurfaceTest(unittest.TestCase):
    def test_page_lint_warns_and_still_passes(self):
        done = guard.PageSurfaceTest.lint_with(ADVICE)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(warnings_in(done.stdout), both("dek 0"), done.stdout)
        clean = guard.PageSurfaceTest.lint_with("The department answered on time.")
        self.assertEqual((clean.returncode, warnings_in(clean.stdout)), (0, []), clean.stdout + clean.stderr)

    def test_publish_stage_lint_helper_carries_the_warnings(self):
        stage = tool("flock_publish_stage")
        gated = tool("flock_compare").GATED
        notes = stage.publish.privacy.review_notes([("stage F1 text", ADVICE)], gated)
        self.assertEqual(notes, both("stage F1 text"))
        self.assertEqual(stage.publish.privacy.review_notes([("stage F1 text", "The department answered on time.")], gated), [])

    def test_newsletter_draft_carries_warnings_and_clean_drafts_stay_identical(self):
        from campaign_tool.newsletter.draft import build_draft
        manifest = json.loads(json.dumps(guard.NewsletterAndSiteSurfaceTest.MANIFEST))
        manifest["findings"][0]["summary"] = "The agency declined to answer. " + COUNT_ONLY
        draft = build_draft(manifest)
        self.assertEqual(draft["counts"]["findings"], 1)
        self.assertTrue(draft["review_flags"] and all(f.startswith("warning: ") and "(undated-count). " + FIX_COUNT in f
                                                       for f in draft["review_flags"]))
        manifest["findings"][0]["summary"] = "The agency declined to answer. SB 34 requires a published policy."
        self.assertNotIn("review_flags", build_draft(manifest))

    def test_site_scan_content_check_and_social_carry_the_warnings(self):
        from campaign_tool import cli, social
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "campaign"
            shutil.copytree(ROOT / "examples" / "fictional-campaign", root, ignore=shutil.ignore_patterns("public"))
            self.assertEqual(cli._site_claim_notes(root), [])
            path = next((root / "content" / "findings").glob("*.json"))
            doc = json.loads(path.read_text(encoding="utf-8"))
            doc["summary"] = "The sheriff secretly shared data from 40 cameras."
            path.write_text(json.dumps(doc), encoding="utf-8")
            self.assertEqual(cli._site_claim_notes(root), both("findings/" + doc["slug"]))
            self.assertEqual(social.build_social(root)["written"], [doc["slug"]])  # still drafted
            doc["summary"] = "The department is out of compliance with SB 34. " + COUNT_ONLY
            path.write_text(json.dumps(doc), encoding="utf-8")
            notes = cli._site_claim_notes(root)
            self.assertEqual(notes[0], "review-required: findings/%s: legal-claim wording (non-compliant)" % doc["slug"])
            self.assertEqual(notes[1:], [both("findings/" + doc["slug"])[1]])

    def test_content_check_prints_warnings_on_stderr_outside_its_count_and_exit_code(self):
        def check(answer):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp) / "campaign"
                shutil.copytree(ROOT / "examples" / "fictional-campaign", root)
                (root / "content" / "faq.json").write_text(json.dumps({"schema_version": 1, "entries": [
                    {"question": "How many?", "answer_md": answer}]}), encoding="utf-8")
                done = subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "content-check", "--directory", str(root)],
                                      cwd=ROOT, capture_output=True, text=True, timeout=120)
                return done.returncode, done.stdout, done.stderr
        code, out, err = check("There are 40 cameras. The sheriff hid the logs.")
        clean = check("How long a record is kept.")
        self.assertEqual((code, clean[0]), (0, 0), err)
        self.assertEqual(out, clean[1])           # stdout, including the problem count, is unchanged
        self.assertEqual(clean[2], "")
        self.assertEqual(err.splitlines(), both("faq.json.entries[0].answer_md"))


class DeadlinesSurfaceTest(unittest.TestCase):
    setUp, tearDown, folder, go = td.Base.setUp, td.Base.tearDown, td.Base.folder, td.Base.go
    receipt, generated = td.Base.receipt, td.Base.generated

    def test_notes_adapter_appends_warnings_with_the_existing_labels(self):
        notes = td.MOD.notes(["Clean text.", ADVICE, "Plates 7ABC123 and " + LEGAL])
        self.assertTrue(notes[0].startswith("review-required: generated text 3: "), notes)
        self.assertEqual(notes[1:], both("generated text 2"))
        self.assertEqual(td.MOD.notes(["Clean text.", "Another."]), [])
        self.assertEqual(td.MOD.notes(iter([ADVICE])), both("generated text 1"))

    def test_run_with_advisory_text_keeps_exit_code_artifacts_and_state(self):
        self.go("2026-10-20")
        control = (self.err, self.generated(), self.receipt())
        self.assertEqual(control[0], "")
        shutil.rmtree(self.store / "deadlines")
        real = td.MOD.plan

        def advised(*a, **k):
            rows, writes, queue, errs = real(*a, **k)
            return rows, [(path, text + "\n" + ADVICE) for path, text in writes], queue, errs
        with mock.patch.object(td.MOD, "plan", side_effect=advised):
            lines, code = self.go("2026-10-20")
        self.assertEqual(code, 0)
        self.assertEqual(warnings_in(self.err), both("generated text 1"), self.err)
        self.assertEqual(self.receipt(), control[2])                  # state is the same
        self.assertEqual(sorted(p.name for p in self.generated()), sorted(p.name for p in control[1]))


class TriageSurfaceTest(unittest.TestCase):
    setUp, run_tool, run_triage, rows = tt.Base.setUp, tt.Base.run_tool, tt.Base.run_triage, tt.Base.rows

    def notes(self, out):
        return json.loads((out / "triage-notes.json").read_text())["notes"]

    def test_warnings_follow_the_existing_notes_with_member_labels(self):
        control = self.run_triage()
        self.assertEqual((control[0], self.notes(control[2])), (1, []))
        legal = "The agency violated the Public Records Act.\n"
        collection = tt.make_collection(self.tmp / "c2", {"m04-memo": tt.MEMO + legal + ADVICE + "\n"})
        code, _, out = self.run_triage(name="out2", collection=collection)
        self.assertEqual(code, control[0])                              # same exit code as the clean control
        self.assertEqual(self.rows(out)[1]["m04-memo"]["kind"], "responsive")
        self.assertEqual(self.notes(out), ["review-required: member:m04-memo: legal-claim wording flagged (1 rule)"] + both("member:m04-memo"))

    def test_advisory_only_member_exits_like_the_control(self):
        collection = tt.make_collection(self.tmp / "c3", {"m04-memo": tt.MEMO + INTENT_ONLY + "\n"})
        code, text, out = self.run_triage(name="out3", collection=collection)
        self.assertEqual(code, 1)
        self.assertEqual(self.notes(out), [both("member:m04-memo")[0]])
        self.assertEqual(self.run_triage(name="again", collection=collection)[1], text)


class RehearseSurfaceTest(unittest.TestCase):
    setUp, contact, email, requests, facts, findings = (rt.RehearseTests.setUp, rt.RehearseTests.contact, rt.RehearseTests.email,
                                                       rt.RehearseTests.requests, rt.RehearseTests.facts, rt.RehearseTests.findings)
    full_chain, run_tool, receipt = rt.RehearseTests.full_chain, rt.RehearseTests.run_tool, rt.RehearseTests.receipt

    def test_warnings_are_in_the_report_and_change_no_stage_verdict_or_code(self):
        self.full_chain()
        code, doc, rows, out = self.receipt(publish="pending")
        self.assertNotIn("wording_warnings", doc)
        self.assertNotIn("warning:", (out / "REHEARSAL.md").read_text(encoding="utf-8"))
        control = (code, [(r["stage"], r["state"]) for r in doc["stages"]], doc["verdict"], rows["detected"]["source"])
        self.findings(claim=ADVICE)
        code, doc, rows, out = self.receipt(publish="pending", out=self.tmp / "out2")
        self.assertEqual((code, [(r["stage"], r["state"]) for r in doc["stages"]], doc["verdict"], rows["detected"]["source"]), control)
        self.assertEqual(doc["wording_warnings"], both("finding 1"))
        page = (out / "REHEARSAL.md").read_text(encoding="utf-8")
        self.assertEqual(warnings_in(page), both("finding 1"))
        for leaked in ("secretly", "sample department", "40 cameras"):
            self.assertNotIn(leaked, page + json.dumps(doc))
        self.assertIn("legal_claim_flags=0", rows["detected"]["source"])  # an advisory is not a legal-claim flag

    def test_legal_claim_still_withholds_and_counts_beside_a_warning(self):
        self.full_chain()
        self.findings(claim="Sample City violated SB 34 by sharing data. " + COUNT_ONLY)
        code, doc, rows, out = self.receipt(publish="pending")
        self.assertIn("legal_claim_flags=1", rows["detected"]["source"])
        self.assertEqual(doc["wording_warnings"], [both("finding 1")[1]])
        self.assertEqual(rt.MOD.guard("The agency violated the act."), "(withheld by guard)")
        self.assertEqual(rt.MOD.guard("The agency secretly shared data from 40 cameras."), "The agency secretly shared data from 40 cameras.")


class CustodySurfaceTest(unittest.TestCase):
    setUp, tearDown = ct.CustodyReportTests.setUp, ct.CustodyReportTests.tearDown

    def run_frozen(self, *argv):
        """Run the tool with its generated timestamp frozen, so custody.json compares byte for byte across runs."""
        class Frozen(dt.datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 11, 15, 12, 0, 0, tzinfo=tz)
        with mock.patch.object(ct.MOD, "datetime", Frozen):
            return ct.run(*argv)

    def test_warning_is_printed_after_the_old_line_and_files_return_code_are_unchanged(self):
        self.store.add(ct.finding("fnd_a"))
        control_code, control_out = self.run_frozen("build", "--store", str(self.store.root), "--finding", "fnd_a", "--out", str(self.outs / "c"))
        self.assertEqual(control_out.splitlines(), ["custody | fnd_a | chain complete"])
        control_files = {p.name: p.read_bytes() for p in (self.outs / "c").iterdir()}
        real = ct.MOD.render
        with mock.patch.object(ct.MOD, "render", side_effect=lambda doc: real(doc) + "\n" + ADVICE + "\n"):
            code, out = self.run_frozen("build", "--store", str(self.store.root), "--finding", "fnd_a", "--out", str(self.outs / "o"))
        self.assertEqual(code, control_code)
        self.assertEqual(out.splitlines(), ["custody | fnd_a | chain complete"] + both("custody page"))
        files = {p.name: p.read_bytes() for p in (self.outs / "o").iterdir()}
        self.assertEqual(files["custody.json"], control_files["custody.json"])          # the JSON bytes are untouched
        self.assertEqual(files["CUSTODY.md"], control_files["CUSTODY.md"] + ("\n" + ADVICE + "\n").encode())  # the page is only what render made

    def test_a_legacy_refusal_still_comes_first_and_prints_no_warning(self):
        self.store.add(ct.finding("fnd_a"))
        real = ct.MOD.render
        with mock.patch.object(ct.MOD, "render", side_effect=lambda doc: real(doc) + "\n" + LEGAL + " " + ADVICE + "\n"):
            code, out = ct.run("build", "--store", str(self.store.root), "--finding", "fnd_a", "--out", str(self.outs / "r"))
        self.assertEqual((code, out), (2, "custody refused: generated_text_failed_privacy_check\n"))
        self.assertFalse((self.outs / "r").exists())


# Each dated row reaches actual inherited boundaries; the old intent/count tests above remain unchanged.
NOT_LAW = (
    ("SB 274 requires retention.", "SB 274 was vetoed; say what it would have done and that it is not law"),
    ("SB 1013 bans sharing.", "SB 1013 was not chaptered as of 2026-11-09; describe it as a bill, not law, and re-check its official status"),
    ("Carpenter requires a warrant for ALPR.", "Carpenter concerns cell-site records, not an ALPR holding; do not say it requires an ALPR warrant or held ALPR is a search"),
    ("Yang held ALPR is a search.", "Yang is a standing holding on its facts; do not say it requires an ALPR warrant or held ALPR is a search"),
)


def not_law(where, fix):
    return ["warning: %s: wording advisory (not-law). Fix: %s" % (where, fix)]


class NotLawReportTest(unittest.TestCase):
    setUp, stage = tr.FlockReportTest.setUp, tr.FlockReportTest.stage

    def test_not_law_report_matrix(self):
        for claim, fix in NOT_LAW:
            with self.subTest(claim=claim):
                config, p = self.stage(edit=lambda text, _: text.replace(tr.BASIS, tr.BASIS + " " + claim))
                code, out = tr.build(config)
                self.assertEqual(code, 0, out)
                notes = warnings_in(out)
                self.assertEqual(len(notes), 1, out)
                self.assertRegex(notes[0], r"^warning: line \d+: wording advisory \(not-law\)\. Fix: " + re.escape(fix) + "$")
                self.assertTrue((p["CO"] / "report" / "REPORT-DRAFT.md").is_file())


class NotLawPublishTest(unittest.TestCase):
    setUp, export = tp.FlockPublishTest.setUp, tp.FlockPublishTest.export
    fixture_text, edit_claim = tp.FlockPublishTest.fixture_text, PublishSurfaceTest.edit_claim

    def test_not_law_publish_check_state_and_duplicate_run(self):
        for claim, fix in NOT_LAW:
            with self.subTest(claim=claim):
                self.out.unlink(missing_ok=True)
                self.edit_claim(claim)
                code, out = self.export()
                self.assertEqual(code, 0, out)
                self.assertEqual(warnings_in(out), not_law("finding O1", fix))
                data = self.out.read_bytes()
                entry = next(f for f in json.loads(data)["findings"] if f["finding_id"] == "O1")
                self.assertEqual(entry["review"]["status"], "pending")
                for _ in range(2):
                    code, out = tp.run("flock_publish.py", "--check", str(self.out))
                    self.assertEqual(code, 0, out)
                    self.assertEqual(warnings_in(out), not_law("finding O1", fix))
                    self.assertEqual(self.out.read_bytes(), data)


class NotLawSiteTest(unittest.TestCase):
    def test_not_law_inherited_reporting_surface_matrix(self):
        from campaign_tool import cli, social
        from campaign_tool.newsletter.draft import build_draft
        stage, gated = tool("flock_publish_stage"), tool("flock_compare").GATED
        for claim, fix in NOT_LAW:
            with self.subTest(claim=claim):
                page = guard.PageSurfaceTest.lint_with(claim)
                self.assertEqual(page.returncode, 0, page.stdout + page.stderr)
                self.assertEqual(warnings_in(page.stdout), not_law("dek 0", fix))
                self.assertEqual(stage.publish.privacy.review_notes([("stage F1 text", claim)], gated), not_law("stage F1 text", fix))
                manifest = json.loads(json.dumps(guard.NewsletterAndSiteSurfaceTest.MANIFEST))
                manifest["findings"][0]["summary"] = claim
                draft = build_draft(manifest)
                self.assertEqual(draft["counts"]["findings"], 1)
                self.assertTrue(draft["review_flags"])
                self.assertTrue(all(n.endswith("wording advisory (not-law). Fix: " + fix) for n in draft["review_flags"]))
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp) / "campaign"
                    shutil.copytree(ROOT / "examples" / "fictional-campaign", root)
                    path = next((root / "content" / "findings").glob("*.json"))
                    doc = json.loads(path.read_text())
                    doc["summary"] = claim
                    path.write_text(json.dumps(doc))
                    before = path.read_bytes()
                    self.assertEqual(cli._site_claim_notes(root), not_law("findings/" + doc["slug"], fix))
                    self.assertEqual(social.build_social(root)["written"], [doc["slug"]])
                    # content-check's inherited wording boundary scans FAQ answers, not finding summaries.
                    (root / "content" / "faq.json").write_text(json.dumps({"schema_version": 1, "entries": [
                        {"question": "What does the source say?", "answer_md": claim}]}))
                    done = subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "content-check", "--directory", str(root)],
                                          cwd=ROOT, capture_output=True, text=True, timeout=120)
                    self.assertEqual((done.returncode, done.stdout), (0, "content/: 0 problem(s)\nlaw package us-ca: draft\n"))
                    self.assertEqual(done.stderr.splitlines(), not_law("faq.json.entries[0].answer_md", fix))
                    self.assertEqual(path.read_bytes(), before)


class NotLawDeadlinesTest(unittest.TestCase):
    setUp, tearDown, folder, go = td.Base.setUp, td.Base.tearDown, td.Base.folder, td.Base.go
    receipt, generated = td.Base.receipt, td.Base.generated

    def test_not_law_deadlines_state_and_code(self):
        self.go("2026-10-20")
        control = self.receipt()
        real = td.MOD.plan
        for claim, fix in NOT_LAW:
            shutil.rmtree(self.store / "deadlines")
            def advised(*a, **k):
                rows, writes, queue, errs = real(*a, **k)
                return rows, [(path, text + "\n" + claim) for path, text in writes], queue, errs
            with mock.patch.object(td.MOD, "plan", side_effect=advised):
                _, code = self.go("2026-10-20")
            self.assertEqual(code, 0)
            self.assertEqual(warnings_in(self.err), not_law("generated text 1", fix))
            self.assertEqual(self.receipt(), control)


class NotLawTriageTest(unittest.TestCase):
    setUp, run_tool, run_triage, rows = tt.Base.setUp, tt.Base.run_tool, tt.Base.run_triage, tt.Base.rows
    notes = TriageSurfaceTest.notes

    def test_not_law_triage_retains_nonzero_control_code(self):
        control = self.run_triage()
        for i, (claim, fix) in enumerate(NOT_LAW):
            collection = tt.make_collection(self.tmp / ("c%d" % i), {"m04-memo": tt.MEMO + claim + "\n"})
            code, _, out = self.run_triage(name="out%d" % i, collection=collection)
            self.assertEqual(code, control[0])
            self.assertEqual(self.notes(out), not_law("member:m04-memo", fix))
            self.assertEqual(self.rows(out)[1]["m04-memo"]["kind"], "responsive")


class NotLawRehearseTest(unittest.TestCase):
    setUp, contact, email, requests, facts, findings = (rt.RehearseTests.setUp, rt.RehearseTests.contact, rt.RehearseTests.email,
                                                       rt.RehearseTests.requests, rt.RehearseTests.facts, rt.RehearseTests.findings)
    full_chain, run_tool, receipt = rt.RehearseTests.full_chain, rt.RehearseTests.run_tool, rt.RehearseTests.receipt

    def test_not_law_rehearsal_stage_and_verdict(self):
        self.full_chain()
        code, doc, _, _ = self.receipt(publish="pending")
        control = (code, doc["stages"], doc["verdict"])
        for i, (claim, fix) in enumerate(NOT_LAW):
            self.findings(claim=claim)
            code, doc, rows, out = self.receipt(publish="pending", out=self.tmp / ("nl%d" % i))
            self.assertEqual((code, doc["stages"], doc["verdict"]), control)
            self.assertEqual(doc["wording_warnings"], not_law("finding 1", fix))
            self.assertEqual(warnings_in((out / "REHEARSAL.md").read_text()), not_law("finding 1", fix))
            self.assertIn("legal_claim_flags=0", rows["detected"]["source"])


class NotLawCustodyTest(unittest.TestCase):
    setUp, tearDown, run_frozen = ct.CustodyReportTests.setUp, ct.CustodyReportTests.tearDown, CustodySurfaceTest.run_frozen

    def test_not_law_refusal_and_clean_byte_invariance(self):
        self.store.add(ct.finding("fnd_a"))
        args = ("build", "--store", str(self.store.root), "--finding", "fnd_a", "--out")
        code, _ = self.run_frozen(*args, str(self.outs / "control"))
        control = {p.name: p.read_bytes() for p in (self.outs / "control").iterdir()}
        real = ct.MOD.render
        for i, (claim, fix) in enumerate(NOT_LAW):
            destination = self.outs / str(i)
            with mock.patch.object(ct.MOD, "render", side_effect=lambda doc: real(doc) + "\n" + claim + "\n"):
                found_code, out = self.run_frozen(*args, str(destination))
            self.assertEqual(found_code, code)
            self.assertEqual(warnings_in(out), not_law("custody page", fix))
            self.assertEqual((destination / "custody.json").read_bytes(), control["custody.json"])
            self.assertEqual((destination / "CUSTODY.md").read_bytes(), control["CUSTODY.md"] + ("\n" + claim + "\n").encode())
            with mock.patch.object(ct.MOD, "render", side_effect=lambda doc: real(doc) + "\n" + LEGAL + " " + claim):
                result = self.run_frozen(*args, str(self.outs / ("refused%d" % i)))
            self.assertEqual(result, (2, "custody refused: generated_text_failed_privacy_check\n"))
            self.assertFalse((self.outs / ("refused%d" % i)).exists())


if __name__ == "__main__":
    unittest.main()
