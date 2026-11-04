"""DF-4: the legal-claim wording guard is alert-only. It flags text that ASSERTS a violation (corpus: tests/fixtures/flock/
legal_claim_corpus.json, boundary explained there), and every generated surface reports a flag as a review-required note
while build, export and exit codes carry on unchanged."""
import csv, importlib.util, json, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path

from tests.flock import test_publish as _tp, test_report as _tr  # modules only: their TestCases are not re-collected here

FIXTURES, run, BASIS, build_report = _tp.FIXTURES, _tp.run, _tr.BASIS, _tr.build

ROOT = Path(__file__).resolve().parents[2]
CORPUS = json.loads((ROOT / "tests" / "fixtures" / "flock" / "legal_claim_corpus.json").read_text(encoding="utf-8"))


def load(name):
    spec = importlib.util.spec_from_file_location("_df4_" + name, ROOT / "tools" / (name + ".py"))
    spec.loader.exec_module(module := importlib.util.module_from_spec(spec))
    return module


privacy = load("flock_privacy")


class CorpusTest(unittest.TestCase):
    def test_every_assertion_is_flagged(self):
        missed = [t for t in CORPUS["flag"] if not privacy.legal_claims(t)]
        self.assertEqual(missed, [])

    def test_every_neutral_sentence_passes(self):
        wrongly = [t for t in CORPUS["pass"] if privacy.legal_claims(t)]
        self.assertEqual(wrongly, [])

    def test_notes_name_place_and_rules_never_the_text(self):
        notes = privacy.legal_notes([("line 3", "Agency Zed violated SB 34."), ("line 4", "SB 34 requires a policy.")])
        self.assertEqual(notes, ["review-required: line 3: legal-claim wording (violation-verb)"])
        self.assertNotIn("Zed", notes[0])

    def test_guard_fault_is_a_flag_not_a_crash(self):
        self.assertEqual(privacy.legal_claims(None), [])
        original = privacy._legal.SENTENCE_SPLIT
        privacy._legal.SENTENCE_SPLIT = None  # a broken guard
        try:
            self.assertEqual(privacy.legal_claims("x"), ["guard-error"])
        finally:
            privacy._legal.SENTENCE_SPLIT = original


# --- every generated surface: a flag is a note, the run carries on (alert-only, never fail-closed) -------------------------
WORDING = "The sample department is non-compliant with the access-record rule."


class ReportSurfaceTest(unittest.TestCase):
    setUp, stage = _tr.FlockReportTest.setUp, _tr.FlockReportTest.stage

    def test_report_flag_is_a_note_and_the_draft_is_still_written(self):
        config, p = self.stage(edit=lambda text, _: text.replace(BASIS, BASIS + " " + WORDING))
        code, out = build_report(config)
        self.assertEqual(code, 0, out)
        self.assertRegex(out, r"(?m)^review-required: line \d+: legal-claim wording \(non-compliant\)$")
        self.assertNotIn("sample department", out.lower())
        self.assertTrue((p["CO"] / "report" / "REPORT-DRAFT.md").is_file())


class PublishSurfaceTest(unittest.TestCase):
    setUp, export = _tp.FlockPublishTest.setUp, _tp.FlockPublishTest.export
    fixture_text = _tp.FlockPublishTest.fixture_text

    def test_publish_flag_is_exported_pending_for_the_human_gate(self):
        with open(self.run_dir / "FINDINGS.csv", newline="") as handle:
            table = list(csv.reader(handle))
        row = next(r for r in table if r[0] == "O1")
        row[table[0].index("claim")] = WORDING
        with open(self.run_dir / "FINDINGS.csv", "w", newline="") as handle:
            csv.writer(handle, lineterminator="\n").writerows(table)
        code, out = self.export()
        self.assertEqual(code, 0, out)
        self.assertIn("review-required: finding O1: legal-claim wording (non-compliant)\n", out)
        doc = json.loads(self.out.read_text())
        entry = next(f for f in doc["findings"] if f["finding_id"] == "O1")  # flagged, not dropped: the run did not halt
        self.assertEqual(entry["review"]["status"], "pending")
        code, out = run("flock_publish.py", "--check", str(self.out))
        self.assertEqual(code, 0, out)
        self.assertIn("review-required: finding O1: legal-claim wording (non-compliant)\n", out)


class PageSurfaceTest(unittest.TestCase):
    @staticmethod
    def lint_with(wording):
        page_fix = ROOT / "tests" / "fixtures" / "flock" / "page"
        with tempfile.TemporaryDirectory() as tmp:
            story = Path(tmp) / "s-story.json"
            args = ["--cards", page_fix / "cards.json", "--sources", page_fix / "sources.json", "--location", page_fix / "location.json",
                    "--thread", page_fix / "thread.json", "--base-sources", 8]
            subprocess.run([sys.executable, "-B", str(ROOT / "tools" / "flock_page.py"), "build", *map(str, args), "--out", story],
                           capture_output=True, text=True, check=True)
            doc = json.loads(story.read_text(encoding="utf-8"))
            doc["dek"] += " " + wording
            story.write_text(json.dumps(doc), encoding="utf-8")
            return subprocess.run([sys.executable, "-B", str(ROOT / "tools" / "flock_page.py"), "lint", "--story", str(story),
                                   "--location", str(page_fix / "location.json"), "--thread", str(page_fix / "thread.json"),
                                   "--base-sources", "8"], capture_output=True, text=True, check=False)

    def test_page_flag_is_a_note_and_lint_still_passes(self):
        page_fix = ROOT / "tests" / "fixtures" / "flock" / "page"
        with tempfile.TemporaryDirectory() as tmp:
            story = Path(tmp) / "s-story.json"
            args = ["--cards", page_fix / "cards.json", "--sources", page_fix / "sources.json", "--location", page_fix / "location.json",
                    "--thread", page_fix / "thread.json", "--base-sources", 8]
            done = subprocess.run([sys.executable, "-B", str(ROOT / "tools" / "flock_page.py"), "build", *map(str, args), "--out", story],
                                  capture_output=True, text=True, check=False)
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            doc = json.loads(story.read_text(encoding="utf-8"))
            doc["dek"] += " " + WORDING
            story.write_text(json.dumps(doc), encoding="utf-8")
            done = subprocess.run([sys.executable, "-B", str(ROOT / "tools" / "flock_page.py"), "lint", "--story", str(story),
                                   "--location", str(page_fix / "location.json"), "--thread", str(page_fix / "thread.json"),
                                   "--base-sources", "8"], capture_output=True, text=True, check=False)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("review-required: dek 0: legal-claim wording (non-compliant)\n", done.stdout)


class NewsletterAndSiteSurfaceTest(unittest.TestCase):
    MANIFEST = {"schema_version": 1, "campaign": {"name": "Synthetic", "base_url": "https://synthetic.example.invalid", "county_name": "S"},
                "since": None, "meetings": [],
                "findings": [{"title": "A record", "summary": "Agency Zed violated SB 34 by sharing plate data.",
                              "classification": "documented_fact", "confidence": "verified", "path": "/findings/a.html",
                              "published_at": "2026-02-01T18:00:00+00:00", "corrected": False}]}

    def test_newsletter_flag_travels_with_the_draft_and_does_not_block_it(self):
        from campaign_tool.newsletter.draft import build_draft
        draft = build_draft(self.MANIFEST)
        self.assertEqual(draft["counts"]["findings"], 1)  # built, not refused
        self.assertIn("{{ unsubscribe }}", draft["html"])
        self.assertTrue(draft["review_flags"] and all("legal-claim wording (violation-verb)" in f for f in draft["review_flags"]))
        self.assertFalse(any("Zed" in f for f in draft["review_flags"]))
        manifest = json.loads(json.dumps(self.MANIFEST))
        manifest["findings"][0]["summary"] = "The agency declined to answer. SB 34 requires a published policy."
        self.assertNotIn("review_flags", build_draft(manifest))  # absent when clean: unflagged drafts stay byte-identical

    def test_social_post_flag_is_a_note_and_the_draft_is_still_written(self):
        from campaign_tool import social
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "campaign"
            shutil.copytree(ROOT / "examples" / "fictional-campaign", root, ignore=shutil.ignore_patterns("public"))
            path = next((root / "content" / "findings").glob("*.json"))
            doc = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(social.review_notes(root), [])  # the shipped example reads as fact
            # Wording outside DF2037-02's separately owned review blocker (campaign_tool/review.py LEGAL_TERMS), so this exercises
            # DF-4's own advisory path; DF2037Retained below pins what that older blocker still does.
            doc["title"] = "Cedar Police is out of compliance with SB 34 (fictional)"
            path.write_text(json.dumps(doc), encoding="utf-8")
            self.assertEqual(social.review_notes(root), ["review-required: social/%s: legal-claim wording (non-compliant)" % doc["slug"]])
            self.assertEqual(social.build_social(root)["written"], [doc["slug"]])  # still drafted

    def test_site_build_scan_flags_findings_and_never_raises(self):
        from campaign_tool import cli
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "content" / "findings").mkdir(parents=True)
            (root / "content" / "site.json").write_text("{}")
            self.assertEqual(len(cli._site_claim_notes(root)), 1)  # an unreadable model: one note asking for a manual read, no raise
            self.assertIn("review-required", cli._site_claim_notes(root)[0])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "campaign"
            shutil.copytree(ROOT / "examples" / "fictional-campaign", root, ignore=shutil.ignore_patterns("public"))
            self.assertEqual(cli._site_claim_notes(root), [])
            path = next((root / "content" / "findings").glob("*.json"))
            doc = json.loads(path.read_text(encoding="utf-8"))
            doc["summary"] = "The department is out of compliance with SB 34 (fictional)."
            path.write_text(json.dumps(doc), encoding="utf-8")
            self.assertEqual(cli._site_claim_notes(root), ["review-required: findings/%s: legal-claim wording (non-compliant)" % doc["slug"]])

    def test_site_build_scan_flags_configured_comment_asks_by_position(self):  # DF2207-07
        from campaign_tool import cli
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "campaign"
            shutil.copytree(ROOT / "examples" / "fictional-campaign", root, ignore=shutil.ignore_patterns("public"))
            path = root / "content" / "site.json"
            site = json.loads(path.read_text(encoding="utf-8"))
            site["comment_asks"] = ["Publish the policy before any renewal.", "The department is out of compliance with SB 34."]
            path.write_text(json.dumps(site), encoding="utf-8")
            self.assertEqual(cli._site_claim_notes(root), ["review-required: site.json.comment_asks[2]: legal-claim wording (non-compliant)"])


def _fictional_with(field, text):
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name) / "campaign"
    shutil.copytree(ROOT / "examples" / "fictional-campaign", root, ignore=shutil.ignore_patterns("public"))
    path = next((root / "content" / "findings").glob("*.json"))
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc[field] = text
    path.write_text(json.dumps(doc), encoding="utf-8")
    return tmp, root


class ExplicitExampleTest(unittest.TestCase):
    """'Agency X violated SB 34.' driven through the real report, publish and page commands, the newsletter draft, and the site and
    social scans. DF-4 ruling 1 (operator, 2026-11-02): the older compare.GATED word ban no longer refuses; it and this guard
    both add review-required notes and the run carries on."""
    EXAMPLE = "Agency X violated SB 34."

    def test_guard_flags_it(self):
        self.assertEqual(privacy.legal_claims(self.EXAMPLE), ["violation-verb"])

    def test_report_build_notes_it_and_writes_the_draft(self):
        t = ReportSurfaceTest()
        t.setUp()
        self.addCleanup(t.doCleanups)
        config, p = t.stage(edit=lambda text, _: text.replace(BASIS, BASIS + " " + self.EXAMPLE))
        code, out = build_report(config)
        self.assertEqual(code, 0, out)
        self.assertRegex(out, r"(?m)^review-required: line \d+: legal-claim wording \(violation-verb\)$")
        self.assertRegex(out, r"(?m)^review-required: line \d+: quote-gated wording$")
        self.assertNotIn("violated", out.lower())
        self.assertTrue((p["CO"] / "report" / "REPORT-DRAFT.md").is_file())

    def test_publish_export_notes_it_and_exports_it_pending(self):
        t = PublishSurfaceTest()
        t.setUp()
        self.addCleanup(t.doCleanups)
        with open(t.run_dir / "FINDINGS.csv", newline="") as handle:
            table = list(csv.reader(handle))
        row = next(r for r in table if r[0] == "O1")
        row[table[0].index("claim")] = self.EXAMPLE
        with open(t.run_dir / "FINDINGS.csv", "w", newline="") as handle:
            csv.writer(handle, lineterminator="\n").writerows(table)
        code, out = t.export()
        self.assertEqual(code, 0, out)
        self.assertIn("review-required: finding O1: quote-gated wording\n", out)
        self.assertIn("review-required: finding O1: legal-claim wording (violation-verb)\n", out)
        self.assertNotIn("refused=1", out)
        entry = next(f for f in json.loads(t.out.read_text())["findings"] if f["finding_id"] == "O1")
        self.assertEqual(entry["review"]["status"], "pending")  # the human gate still decides
        code, out = run("flock_publish.py", "--check", str(t.out))
        self.assertEqual(code, 0, out)

    def test_page_lint_notes_it_and_passes(self):
        done = PageSurfaceTest.lint_with(self.EXAMPLE)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("review-required: dek 0: legal-claim wording (violation-verb)\n", done.stdout)
        self.assertIn("quote-gated wording", done.stdout)
        self.assertNotIn("violated", done.stdout.lower())

    def test_newsletter_draft_notes_it(self):
        from campaign_tool.newsletter.draft import legal_claim_notes
        self.assertEqual(legal_claim_notes([("subject", self.EXAMPLE)]), ["review-required: subject: legal-claim wording (violation-verb)"])


class DF2037RetainedTest(unittest.TestCase):
    """NOT changed by DF-4: DF2037-02 (campaign_tool/review.py LEGAL_TERMS) is a separately owned, merged review blocker that
    refuses legal-conclusion words in a site finding's title/summary/body_md. Ruling 1 covered compare.GATED only, so this pins
    the retained behaviour: the site and social scans still never raise and still give the human a review-required note."""
    EXAMPLE = ExplicitExampleTest.EXAMPLE

    def test_site_scan_notes_unscannable_content_without_raising(self):
        from campaign_tool import cli
        tmp, root = _fictional_with("summary", self.EXAMPLE)
        with tmp:
            notes = cli._site_claim_notes(root)
        self.assertEqual(len(notes), 1)
        self.assertTrue(notes[0].startswith("review-required: "), notes)

    def test_social_scan_notes_unscannable_content_without_raising(self):
        from campaign_tool import social
        tmp, root = _fictional_with("title", self.EXAMPLE)
        with tmp:
            self.assertEqual(social.review_notes(root),
                             ["review-required: social: legal-claim wording (posts could not be scanned; read them before use)"])


if __name__ == "__main__":
    unittest.main()
