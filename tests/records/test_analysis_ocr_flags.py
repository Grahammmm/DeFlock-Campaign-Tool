"""DF2007-07: OCR pages at or above the confidence bar reach analysis flagged; a finding citing one waits for its confirmation.

Synthetic ledgers and analysis roots only. Building blocks come from the export, bridge and packet-outbox test fixtures.
"""
import contextlib
import csv
import io
import itertools
import json
import unittest
from unittest import mock

from campaign_tool.records import analysis_bridge as bridge, analysis_export as ae, ocr_review
from campaign_tool.records.ledger import store
from campaign_tool.records.outcomes import analysis_ready as ar, textstate as ts
from tests.records import test_analysis_bridge as tb, test_analysis_export as tae, test_analysis_packet_outbox as to
from tests.records.test_outcome_textstate import finish_extract, write_extract_list

LEGACY_HEADER = "original_sha256,agency_id,request_id,pages,text_sha256,state,received_at,received_at_source"
FLAGGED_WORDS = "Text came from scanning (OCR); usable for analysis with a flag, not yet visually confirmed."


def set_pages(database, digest, **pages):
    """pages: pN=(confidence or None, needs_visual_review) for the document's page N."""
    with store.ledger(database) as con:
        for key, (confidence, needs) in pages.items():
            con.execute("UPDATE page_state SET confidence=?, needs_visual_review=? WHERE original_sha256=? AND page_no=?",
                        (confidence, needs, digest, int(key[1:])))
        con.commit()


def mark(project, digest, page, decision):
    with contextlib.redirect_stdout(io.StringIO()):
        return ocr_review.main(["mark", "--project", str(project), "--original-sha256", digest, "--page", str(page),
                                "--decision", decision, "--reviewer-label", "organizer"])


class PagePolicyTests(unittest.TestCase):
    """The admitted page policy decides text state and export; a flag never stands for a review."""

    def setUp(self):
        self.e = tae.AnalysisExportTests("test_ready_record_exports_text_dates_and_hashes")
        self.e.setUp()
        self.addCleanup(self.e.doCleanups)
        self.counter = itertools.count()

    def doc(self, p1, p2=(None, 0)):
        digest, _ = self.e.document(tae.PAGES, "ag1")
        finish_extract(self.e.database, digest)
        set_pages(self.e.database, digest, p1=p1, p2=p2)
        return digest

    def settle(self, run, digest):
        """The outputs stage for one run (text state, then the manifest); -> (text state, the run's exported folder)."""
        self.e.settled_run(run, [digest], digest)
        state = {r["sha256"]: r["state"] for r in json.loads(
            (self.e.project / "run-outputs" / run / "text-state.json").read_text())["records"]}[digest]
        return state, self.e.export_dir("--run", run) / run

    def test_92_confidence_exports_flagged_and_80_79_60_none_and_rejected_follow_the_admitted_policy(self):
        for confidence, decision, state, exported in ((92.0, None, "ocr_flagged", True), (80.0, None, "ocr_flagged", True),
                                                      (79.99, None, "ocr_unreviewed", False), (60.0, None, "ocr_unreviewed", False),
                                                      (None, None, "ocr_unreviewed", False), (92.0, "rejected", "could_not_be_read", False),
                                                      (60.0, "confirmed", "text_reviewed", True)):
            with self.subTest(confidence=confidence, decision=decision):
                digest = self.doc((confidence, 1))
                if decision:
                    self.assertEqual(mark(self.e.project, digest, 1, decision), 0)
                run = "run_202611%02dT000000Z_%s" % (10 + next(self.counter), "d" * 8)
                got, folder = self.settle(run, digest)
                self.assertEqual(got, state)
                row = {r["original_sha256"]: r for r in self.e.rows(folder)}[digest]
                self.assertEqual(row["state"], "exported" if exported else "TEXT_NOT_READY")
                self.assertEqual((folder / "texts" / (digest + ".txt")).exists(), exported)
                if state == "ocr_flagged":
                    self.assertEqual(row["page_flags"], '{"1":"read_with_flag"}')
                    self.assertEqual((folder / "texts" / (digest + ".txt")).read_text(),
                                     "[[PAGE 1 FLAGGED]]\nFirst page text\n[[PAGE 2]]\nSecond page text\n")
                else:
                    self.assertNotIn("page_flags", row)  # a run with nothing flagged keeps the legacy eight columns

    def test_textstate_words_and_counts_for_a_flagged_record(self):
        digest = self.doc((92.0, 1))
        self.e.settled_run(tae.R1, [digest], digest)
        report = json.loads((self.e.project / "run-outputs" / tae.R1 / "text-state.json").read_text())
        self.assertEqual([(r["state"], r["words"]) for r in report["records"]], [("ocr_flagged", FLAGGED_WORDS)])
        self.assertEqual(report["counts"]["ocr_flagged"], 1)
        self.assertEqual(sum(report["counts"].values()), 1)
        self.assertIn("| ocr_flagged | 1 |", (self.e.project / "run-outputs" / tae.R1 / "text-state.md").read_text())

    def test_policy_is_called_for_every_review_page_and_a_mixed_record_exports_no_partial_text(self):
        digest = self.doc((92.0, 1), (60.0, 1))  # page 1 flaggable, page 2 held
        with mock.patch.object(ae.ocr_review, "page_use_allowed", wraps=ocr_review.page_use_allowed) as spy:
            state, folder = self.settle(tae.R1, digest)
        self.assertEqual(state, "ocr_unreviewed")
        self.assertTrue(spy.called)
        self.assertFalse((folder / "texts").exists())
        self.assertEqual([r["state"] for r in self.e.rows(folder)], ["TEXT_NOT_READY"])
        self.assertEqual(mark(self.e.project, digest, 2, "confirmed"), 0)  # page 2 confirmed: page 1 still flagged
        state, folder = self.settle(tae.R2, digest)
        self.assertEqual(state, "ocr_flagged")
        self.assertEqual(self.e.rows(folder)[0]["page_flags"], '{"1":"read_with_flag"}')

    def test_a_stale_text_reviewed_manifest_cannot_bypass_a_rejection_or_a_changed_binding(self):
        digest = self.doc((60.0, 1))
        self.assertEqual(mark(self.e.project, digest, 1, "confirmed"), 0)
        self.e.make_run(tae.R1, [(digest, "text_reviewed", "rq1")])
        self.assertEqual(self.e.rows(self.e.export_dir("--run", tae.R1) / tae.R1)[0]["state"], "exported")
        with store.ledger(self.e.database) as con:  # the page is read again: the old confirmation no longer binds
            con.execute("UPDATE page_state SET derivative_sha256=? WHERE original_sha256=? AND page_no=1", ("e" * 64, digest))
            con.commit()
        self.e.make_run(tae.R2, [(digest, "text_reviewed", "rq1")])
        folder = self.e.export_dir("--run", tae.R2) / tae.R2
        self.assertEqual([r["state"] for r in self.e.rows(folder)], ["TEXT_NOT_READY"])
        self.assertFalse((folder / "texts").exists())
        rejected = self.doc((92.0, 1))
        self.assertEqual(mark(self.e.project, rejected, 1, "rejected"), 0)
        self.e.make_run(tae.R3, [(rejected, "text_reviewed", "rq1")])  # even a manifest that says reviewed
        self.assertEqual([r["state"] for r in self.e.rows(self.e.export_dir("--run", tae.R3) / tae.R3)], ["TEXT_NOT_READY"])

    def test_a_broken_review_chain_is_never_read_as_confirmation(self):
        digest = self.doc((92.0, 1))
        self.assertEqual(mark(self.e.project, digest, 1, "confirmed"), 0)
        reviews = self.e.project / "ocr-review-v1" / "reviews.jsonl"
        reviews.write_text(reviews.read_text().replace('"prev_line_sha256":null', '"prev_line_sha256":"%s"' % ("f" * 64)))  # the chain no longer holds
        write_extract_list(self.e.project, tae.R1, [digest])
        with self.assertRaises(OSError):
            ts.write_run_textstate(self.e.project, tae.R1)
        self.e.make_run(tae.R2, [(digest, "ocr_flagged", "rq1")])
        self.assertEqual([r["state"] for r in self.e.rows(self.e.export_dir("--run", tae.R2) / tae.R2)], ["TEXT_NOT_READY"])

    def test_legacy_runs_keep_their_exact_bytes_and_a_mixed_run_adds_the_column_only_for_itself(self):
        confirmed = self.doc((60.0, 1))
        self.assertEqual(mark(self.e.project, confirmed, 1, "confirmed"), 0)
        plain, _ = self.e.document(tae.PAGES, "ag1")
        self.e.make_run(tae.R1, [(confirmed, "text_reviewed", "rq1"), (plain, "text_reviewed", "rq1")])
        folder = self.e.export_dir("--run", tae.R1) / tae.R1
        text = (folder / "TEXTS.csv").read_text()
        self.assertEqual(text.splitlines()[0], LEGACY_HEADER)
        self.assertEqual({ln.count(",") for ln in text.splitlines()}, {7})
        self.assertTrue(all("FLAGGED" not in p.read_text() for p in (folder / "texts").iterdir()))
        flagged = self.doc((92.0, 1))
        self.e.make_run(tae.R2, [(confirmed, "text_reviewed", "rq1"), (flagged, "ocr_flagged", "rq1")])
        folder = self.e.export_dir("--run", tae.R2) / tae.R2
        rows = {r["original_sha256"]: r for r in self.e.rows(folder)}
        self.assertEqual((rows[confirmed]["page_flags"], rows[flagged]["page_flags"]), ("{}", '{"1":"read_with_flag"}'))
        self.assertEqual((folder / "TEXTS.csv").read_text().splitlines()[0], LEGACY_HEADER + ",page_flags")
        export = json.loads((folder / "EXPORT.json").read_text())  # the envelope is unchanged and covers the new bytes
        self.assertEqual(sorted(export), ["analysis_ready", "engine", "files", "run_id", "schema"])
        self.assertEqual(export["files"]["TEXTS.csv"], ae._sha((folder / "TEXTS.csv").read_bytes()))


class FlagMetadataTests(unittest.TestCase):
    """Receipt accepts exactly the legacy header or the legacy header plus a closed, hash-bound page_flags."""
    SHA = "a" * 64
    TEXT = b"[[PAGE 1]]\nOne\n[[PAGE 2 FLAGGED]]\nTwo\n"

    def files(self, flags='{"2":"read_with_flag"}', header=None, text=None, pages="2", extra=""):
        text = self.TEXT if text is None else text
        run = tb.R1
        header = header or ",".join(ae.FLAG_COLUMNS)
        row = [self.SHA, "ag", "rq", pages, ae._sha(text), "exported", "", "", flags]
        sink = io.StringIO()
        writer = csv.writer(sink, lineterminator="\n")
        writer.writerow(header.split(","))
        writer.writerow(row[:len(header.split(","))] + ([extra] if extra else []))
        texts = sink.getvalue().encode()
        files = {"TEXTS.csv": texts, "analysis-ready.json": b"{}", "texts/%s.txt" % self.SHA: text}
        listed = {n: ae._sha(d) for n, d in files.items()}
        files["EXPORT.json"] = json.dumps({"schema": ae.SCHEMA, "run_id": run, "analysis_ready": "present", "files": listed}).encode()
        return run, files

    def test_a_consistent_extended_export_is_accepted_and_every_inconsistency_is_refused(self):
        self.assertTrue(bridge._verify(*self.files()))
        bad = {"unknown label": dict(flags='{"2":"confirmed"}'), "page beyond the count": dict(flags='{"3":"read_with_flag"}'),
               "marker disagrees": dict(flags='{"1":"read_with_flag"}'), "not canonical": dict(flags='{"2": "read_with_flag"}'),
               "duplicate key": dict(flags='{"2":"read_with_flag","2":"read_with_flag"}'), "not an object": dict(flags='["2"]'),
               "leading zero": dict(flags='{"02":"read_with_flag"}'), "not json": dict(flags="{"),
               "extra column": dict(extra="x"), "marker without metadata": dict(flags="{}"),
               "metadata without marker": dict(text=b"[[PAGE 1]]\nOne\n[[PAGE 2]]\nTwo\n"),
               "legacy header, marker present": dict(header=",".join(ae.COLUMNS))}
        for name, change in bad.items():
            with self.subTest(name):
                self.assertFalse(bridge._verify(*self.files(**change)))

    def test_a_legacy_export_has_no_flags(self):
        run, files = self.files(flags="", header=",".join(ae.COLUMNS), text=b"[[PAGE 1]]\nOne\n[[PAGE 2]]\nTwo\n")
        self.assertTrue(bridge._verify(run, files))
        self.assertEqual(bridge._rows(files["TEXTS.csv"])[0]["flags"], {})
        self.assertNotIn(b"page_flags", files["TEXTS.csv"])

    def test_snapshot_difference_is_only_the_flagged_token_of_whole_header_lines(self):
        old = b"[[PAGE 1]]\nOne\n[[PAGE 2 FLAGGED]]\nTwo\n"
        new = b"[[PAGE 1]]\nOne\n[[PAGE 2]]\nTwo\n"
        self.assertTrue(ae.annotation_only(old, new) and ae.annotation_only(new, old))
        for name, other in {"prose": b"[[PAGE 1]]\nOne\n[[PAGE 2]]\nTwo changed\n", "page count": b"[[PAGE 1]]\nOne\n",
                            "reordered": b"[[PAGE 2]]\nTwo\n[[PAGE 1]]\nOne\n", "other page number": b"[[PAGE 1]]\nOne\n[[PAGE 3]]\nTwo\n",
                            "prose that looks like a header": b"[[PAGE 1]]\nOne [[PAGE 2]]\n[[PAGE 2]]\nTwo\n"}.items():
            with self.subTest(name):
                self.assertFalse(ae.annotation_only(old, other))
        prose = b"[[PAGE 1]]\nsee the [[PAGE 2 FLAGGED]] note\n"
        self.assertFalse(ae.annotation_only(prose, b"[[PAGE 1]]\nsee the [[PAGE 2]] note\n"))


class FlaggedLifecycleTests(unittest.TestCase):
    """Export, receive, sync and tick on a synthetic root: the finding waits, and confirmation stages it on the next tick."""

    def setUp(self):
        original = tb.AnalysisBridgeTests.document

        def flagged(case, pages, agency, **kw):
            result = original(case, pages, agency, **kw)
            set_pages(case.database, result[0], p1=(None, 0), p2=(92.0, 1))  # the cited page 2 is the 92-confidence one
            return result

        self.o = to.PacketOutboxTests("test_empty_received_at_holds_the_finding_as_date_unavailable")
        self.addCleanup(self.o.doCleanups)
        with mock.patch.object(tb.AnalysisBridgeTests, "document", flagged):
            self.o.start()
        self.b, self.root = self.o.t.b, self.o.root
        self.input = self.root / "agencies" / to.tt.SLUG / "review" / "inputs" / "records" / (self.o.digest + ".txt")

    def new_run(self, run):
        self.b.record(run, tb.AGY, "rq1", "2026-11-02T18:00:00Z", digest=self.o.digest)
        done = self.b.cli("--project", str(self.b.project), "--after", tb.R1, "--max-runs", "10", "--stdout", command="analysis-export")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.b.sync("--receive", "--tar", "-", stdin=done.stdout)[0], 0)
        code, out, err = self.b.sync()
        self.assertEqual(code, 0, out + err)
        return out

    def runs(self):
        return self.o.t.verbs.count("run")

    def test_92_confidence_exports_flagged_and_its_finding_is_held_then_confirming_stages_it_on_the_next_tick(self):
        retained = (self.root / ".analysis" / "runs" / tb.R1 / "TEXTS.csv").read_text()
        self.assertEqual(retained.splitlines()[0], LEGACY_HEADER + ",page_flags")
        flagged_text = self.input.read_bytes()
        self.assertEqual(flagged_text, b"[[PAGE 1]]\nFirst page text\n[[PAGE 2 FLAGGED]]\nSecond page text\n")
        self.o.review("N1")  # checked fact plus a valid signed finding approval (DF2007-05): the page is the only hold
        self.o.tick("PACKET_ALL_HELD n=0 held=4 EVIDENCE_FACT_UNREVIEWED=3 EVIDENCE_PAGE_UNCONFIRMED=1")
        self.assertEqual(self.o.outbox(), [])
        self.assertEqual(self.o.ledger()[-1]["outcome"], "PACKET_ALL_HELD")
        ran = self.runs()
        self.assertEqual(mark(self.b.project, self.o.digest, 2, "confirmed"), 0)
        out = self.new_run(tb.R2)
        self.assertIn("SYNC %s %s SYNCED new_texts=0" % (tb.R2, tb.AGY), out)
        retained2 = (self.root / ".analysis" / "runs" / tb.R2 / "TEXTS.csv").read_text()
        self.assertEqual(retained2.splitlines()[0], LEGACY_HEADER)
        self.assertEqual(self.input.read_bytes(), flagged_text)  # the analysis snapshot keeps its export-time marker
        self.assertIn(b"FLAGGED", (self.root / ".analysis" / "runs" / tb.R1 / "texts" / (self.o.digest + ".txt")).read_bytes())
        line = self.o.tick("PACKET_STAGED")
        self.assertTrue(line.endswith(" n=1 held=3 EVIDENCE_FACT_UNREVIEWED=3"), line)
        self.assertEqual(self.runs(), ran)  # the finished review DAG was not rerun and no model was asked
        (path,) = self.o.outbox()
        self.assertEqual(len(json.loads(path.read_text())["packets"]), 1)
        self.o.tick("PACKET_UNCHANGED")
        self.assertEqual(self.o.outbox(), [path])

    def test_a_flagged_page_holds_before_the_finding_review_and_a_page_elsewhere_does_not(self):
        self.o.review("N1", approve=False)  # checked but never approved: FINDING_UNREVIEWED would hold it...
        self.o.tick("PACKET_ALL_HELD n=0 held=4 EVIDENCE_FACT_UNREVIEWED=3 EVIDENCE_PAGE_UNCONFIRMED=1")  # ...but the page comes first
        sources = self.o.out / "inputs" / "SOURCES.csv"
        self.assertTrue(sources.is_file())
        with store.ledger(self.b.database) as con:  # the page flagged in the next run is page 1, not the cited page 2
            con.execute("UPDATE page_state SET confidence=92.0, needs_visual_review=1 WHERE page_no=1")
            con.execute("UPDATE page_state SET confidence=NULL, needs_visual_review=0 WHERE page_no=2")
            con.commit()
        self.new_run(tb.R2)
        self.o.approve_finding("N1")
        line = self.o.tick("PACKET_STAGED")  # page 2 is unflagged now; page 1 is flagged but uncited
        self.assertTrue(line.endswith(" n=1 held=3 EVIDENCE_FACT_UNREVIEWED=3"), line)

    def test_malformed_or_conflicting_latest_evidence_holds_and_never_clears_an_older_flag(self):
        self.o.review("N1")
        root, slug, agency, sha = self.root, to.tt.SLUG, tb.AGY, self.o.digest
        self.assertEqual(bridge._current_flags(root, slug, agency, {sha}), {sha: frozenset({2})})
        self.assertEqual(bridge._current_flags(root, slug, "someone-else", {sha}), {})  # another agency cannot clear or set it
        self.assertEqual(mark(self.b.project, sha, 2, "confirmed"), 0)
        self.new_run(tb.R2)
        self.assertEqual(bridge._current_flags(root, slug, agency, {sha}), {sha: frozenset()})
        newest = root / ".analysis" / "runs" / tb.R2 / "texts" / (sha + ".txt")
        newest.write_bytes(newest.read_bytes() + b"extra\n")  # evidence that no longer matches the analysis input
        self.assertEqual(bridge._current_flags(root, slug, agency, {sha}), {sha: None})
        self.o.tick("PACKET_ALL_HELD n=0 held=4 EVIDENCE_FACT_UNREVIEWED=3 EVIDENCE_PAGE_UNCONFIRMED=1")
        self.assertEqual(self.o.outbox(), [])


if __name__ == "__main__":
    unittest.main()
