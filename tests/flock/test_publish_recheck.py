"""DF1537-10: `recheck RUNDIR` moves approved PUBLISH.json entries whose cited facts became disputed, missing or expired to
needs-review (exit 3). Its gate is the unchanged schema check plus drift with one allowance: a FACTS.csv edit."""
import contextlib, csv, hashlib, importlib.util, io, json, shutil, unittest
from unittest import mock
from tests.flock import test_publish as base

APPROVED = {"status": "approved", "approved_by": "G", "approved_on": "2026-10-30", "approval_ref": "G-1"}
FINAL = "recheck | approved=%d moved=%d | review-required; G decides any site use\n"
REFUSED = "recheck refused | nothing written\n"
N1_FACT, O1_FACT = "F-090c3d39b556", "F-76dbe8a9b4ac"  # cited by N1 and O1 (tests/fixtures/flock/findings-sample.csv)


class PublishRecheckTest(base.FlockPublishTest):  # setUp: verified run (F4 finished); here exported, N1 and O1 approved
    def setUp(self):
        super().setUp()
        self.assertEqual(self.export(), (0, "exported=4 refused=0\n"))
        self.doc = json.loads(self.out.read_text())
        self.approve("N1", "O1")
        self.facts = self.store / "agencies" / base.AGENCY / "FACTS.csv"

    def approve(self, *ids):  # the listed entries approved, every other entry pending
        pending = {"status": "pending", "approved_by": "", "approved_on": "", "approval_ref": ""}
        for f in self.doc["findings"]:
            f["review"] = dict(APPROVED if f["finding_id"] in ids else pending)
        self.out.write_text(json.dumps(self.doc, indent=2, ensure_ascii=False) + "\n")

    def recheck(self, *extra, today="2026-10-31"):  # every run is checked for output hygiene (edge path 9)
        code, out = base.run("flock_publish.py", "recheck", self.rd, *extra, *(("--today", today) if today else ()))
        secrets = [f["text"] for f in self.doc["findings"]] + [c["label"] for f in self.doc["findings"] for c in f["cites"]]
        with open(self.facts, newline="") as handle:
            secrets += [r[k] for r in csv.DictReader(handle) for k in ("value", "verbatim_quote", "fact_id")]
        for secret in secrets + ["G-1", "http", "@"]:
            self.assertNotIn(secret, out)
        return code, out

    def edit_facts(self, fact_id, remove=False, **change):  # a deliberate FACTS.csv edit: change fields or remove the row
        with open(self.facts, newline="") as handle:
            table = list(csv.DictReader(handle))
        fields = list(table[0])
        table = [dict(r, **change) if r["fact_id"] == fact_id else r for r in table if not (remove and r["fact_id"] == fact_id)]
        with open(self.facts, "w", newline="") as handle:
            writer = csv.DictWriter(handle, fields, lineterminator="\n")
            writer.writeheader()
            writer.writerows(table)

    def dispute(self, fact_id):  # through DF1537-01's own review verb, as an organizer would
        self.assertEqual(base.run("flock_facts.py", "review", "--store", str(self.store), "--agency", base.AGENCY, "--ids", fact_id,
                                  "--set", "disputed", "--reviewer", "second-reader", "--date", "2026-10-30")[0], 0)

    def statuses(self):
        return {f["finding_id"]: f["review"] for f in json.loads(self.out.read_text())["findings"]}

    def rerecord_facts_hash(self):  # as if the run had been scaffolded on this FACTS.csv: the full check passes again
        meta = json.loads((self.run_dir / "RUN.json").read_text())
        meta["inputs"]["FACTS.csv"] = hashlib.sha256(self.facts.read_bytes()).hexdigest()
        (self.run_dir / "RUN.json").write_text(json.dumps(meta, indent=2) + "\n")

    def test_path1_disputed_fact_moves_only_its_entry(self):
        self.dispute(N1_FACT)
        code, out = base.run("flock_publish.py", "--check", str(self.out), "--run", self.rd)  # the old gate cannot pass here
        self.assertEqual(code, 1)
        self.assertIn("inputs: FACTS.csv changed since scaffold", out)
        self.assertEqual(self.recheck(), (3, "recheck | N1 | needs-review | disputed=1\n" + FINAL % (2, 1)))
        want = {f["finding_id"]: f["review"] for f in self.doc["findings"]}
        want["N1"] = dict(APPROVED, status="needs-review")  # approved_by/on/ref kept as the audit trail
        self.assertEqual(self.statuses(), want)
        fresh = json.loads(self.out.read_text())
        for before, after in zip(self.doc["findings"], fresh["findings"]):
            self.assertEqual({**before, "review": None}, {**after, "review": None})
        self.assertEqual({k: v for k, v in fresh.items() if k != "findings"}, {k: v for k, v in self.doc.items() if k != "findings"})
        self.assertEqual(self.out.stat().st_mode & 0o777, 0o600)

    def test_path2_removed_fact_is_missing(self):
        self.edit_facts(N1_FACT, remove=True)
        self.assertEqual(self.recheck(), (3, "recheck | N1 | needs-review | missing=1\n" + FINAL % (2, 1)))
        self.assertEqual(self.statuses()["N1"]["status"], "needs-review")
        self.assertEqual(self.statuses()["O1"], APPROVED)

    def test_path2_removed_quote_fact_is_missing(self):  # O2 (rejected, not exported) quotes it: that check_row error is the removal's
        self.edit_facts(O1_FACT, remove=True)
        self.assertEqual(self.recheck(), (3, "recheck | O1 | needs-review | missing=1\n" + FINAL % (2, 1)))

    def test_path1_disputed_quote_fact_moves_its_entry(self):  # DFMAINRED-5: compare's quote_fact_id error is the dispute's too
        self.dispute(O1_FACT)
        self.assertEqual(self.recheck(), (3, "recheck | O1 | needs-review | disputed=1\n" + FINAL % (2, 1)))
        self.assertEqual(self.statuses()["N1"], APPROVED)

    def test_path3_expired_the_day_before_moves_on_the_day_kept(self):
        self.edit_facts(O1_FACT, effective_to="2026-10-30")
        before = self.out.read_bytes()
        self.assertEqual(self.recheck(today="2026-10-30"), (0, FINAL % (2, 0)))
        self.assertEqual(self.out.read_bytes(), before)
        self.assertEqual(self.recheck(today="2026-10-31"), (3, "recheck | O1 | needs-review | expired=1\n" + FINAL % (2, 1)))

    def test_path4_dry_run_same_lines_and_exit_file_untouched(self):
        self.dispute(N1_FACT)
        self.edit_facts(O1_FACT, effective_to="2026-10-01")
        before = self.out.read_bytes()
        lines = "recheck | N1 | needs-review | disputed=1\nrecheck | O1 | needs-review | expired=1\n" + FINAL % (2, 2)
        self.assertEqual(self.recheck("--dry-run"), (3, lines))
        self.assertEqual(self.out.read_bytes(), before)
        self.assertEqual(self.recheck(), (3, lines))
        self.assertNotEqual(self.out.read_bytes(), before)

    def test_path5_second_recheck_moves_nothing_and_writes_nothing(self):
        self.edit_facts(N1_FACT, remove=True)
        self.assertEqual(self.recheck()[0], 3)
        after, mtime = self.out.read_bytes(), self.out.stat().st_mtime_ns
        self.assertEqual(self.recheck(), (0, FINAL % (1, 0)))
        self.assertEqual((self.out.read_bytes(), self.out.stat().st_mtime_ns), (after, mtime))

    def test_pending_entries_citing_disputed_facts_not_moved_or_listed(self):
        self.approve("O1")
        self.dispute(N1_FACT)
        before = self.out.read_bytes()
        self.assertEqual(self.recheck(), (0, FINAL % (1, 0)))
        self.assertEqual(self.out.read_bytes(), before)

    def test_legacy_run_without_inputs_record_reaches_disputed(self):
        meta = json.loads((self.run_dir / "RUN.json").read_text())
        del meta["inputs"]
        (self.run_dir / "RUN.json").write_text(json.dumps(meta))
        self.dispute(N1_FACT)
        self.assertEqual(self.recheck(), (3, "recheck | N1 | needs-review | disputed=1\n" + FINAL % (2, 1)))

    def test_drift_and_schema_failures_refuse_and_write_nothing(self):  # the FACTS.csv allowance hides no other change
        def text(d): d["findings"][0]["text"] = "Changed."  # noqa: E704
        def extra_cite(d): d["findings"][0]["cites"].append({"kind": "record", "label": "Request REQ-9, page 9"})  # noqa: E704
        def law(): (self.store / "LAW.csv").write_bytes((self.store / "LAW.csv").read_bytes() + b"\n")  # noqa: E704
        def policy():  # noqa: E301
            path = self.store / "agencies" / base.AGENCY / "POLICY.csv"
            path.write_bytes(path.read_bytes() + b"\n")
        def template(): (self.run_dir / "FINDINGS.md").write_text("Final findings\n")  # noqa: E704
        cases = [("edited text", text, None, "finding N1: text differs from the run"),
                 ("extra record cite beside a removed fact", extra_cite, lambda: self.edit_facts(N1_FACT, remove=True),
                  "finding N1: cites differs from the run"),
                 ("edited page of a cited fact (fact_id no longer matches)", None, lambda: self.edit_facts(N1_FACT, page="7"),
                  "findings: store: "),
                 ("quote no longer in a kept fact (check_row still runs on every row)", None,
                  lambda: self.edit_facts(O1_FACT, verbatim_quote="Some outside agencies have access."),
                  "quote is not in the verbatim_quote of quote_fact_id"),
                 ("LAW.csv input changed", None, law, "inputs: LAW.csv changed since scaffold"),
                 ("POLICY.csv input changed", None, policy, "inputs: POLICY.csv changed since scaffold"),
                 ("FINDINGS.md template changed", None, template, "FINDINGS.md: line 1 must be exactly"),
                 ("PUBLISH.json header changed", lambda d: d.update(header="Final"), None, "file: schema or header is invalid"),
                 ("review status unknown", lambda d: d["findings"][0]["review"].update(status="withdrawn"), None,
                  "finding N1: review status is not pending, approved or needs-review")]
        cases += [("truncated run: no %s" % n, None, lambda n=n: (self.run_dir / n).unlink(), "run: cannot re-derive (exit 1)")
                  for n in ("FINDINGS.csv", "VERIFY.csv", "PUBLIC-OK.csv")]
        pristine = self.tmp / "pristine"
        shutil.copytree(self.store, pristine)
        for name, change, setup, line in cases:
            with self.subTest(name):
                shutil.rmtree(self.store)
                shutil.copytree(pristine, self.store)
                self.dispute(N1_FACT)  # a facts edit is present in every case: it must not mask the real one
                if change:
                    doc = json.loads(self.out.read_text())
                    change(doc)
                    self.out.write_text(json.dumps(doc, indent=2) + "\n")
                if setup:
                    setup()
                before = self.out.read_bytes()
                code, out = self.recheck()
                self.assertEqual(code, 1, out)
                self.assertIn(line, out)
                self.assertTrue(out.endswith(REFUSED), out)
                self.assertEqual(self.out.read_bytes(), before)
        for name, raw in (("not json", "{not json"), ("no PUBLISH.json", None)):
            with self.subTest(name):
                self.out.unlink(missing_ok=True)
                if raw:
                    self.out.write_text(raw)
                code, out = self.recheck()
                self.assertEqual((code, out.endswith(REFUSED)), (1, True), out)
                self.assertEqual(self.out.read_text() if raw else self.out.exists(), raw or False)
        self.out.write_text(json.dumps(self.doc))
        (self.run_dir / "VERIFY.json").write_text(json.dumps({"schema": 1, "verifier_provider": "anthropic"}))
        self.assertEqual(self.recheck()[0], 1)  # same-family run: refused, nothing written

    def test_needs_review_counts_as_not_approved_in_check_status_and_stage(self):
        self.edit_facts(O1_FACT, effective_to="2026-10-01")
        self.rerecord_facts_hash()  # the run is clean, so check, status and stage see the needs-review entry itself
        self.assertEqual(self.recheck(), (3, "recheck | O1 | needs-review | expired=1\n" + FINAL % (2, 1)))
        self.assertEqual(base.run("flock_publish.py", "--check", str(self.out), "--run", self.rd),
                         (0, "findings=4 approved=1 errors=0 drift=0\n"))
        line = "publish | %s | 2026-10-30 | %s | findings=4 pending=%d approved=%d errors=0\n"
        self.assertEqual(base.run("flock_publish.py", "status", str(self.store))[1].splitlines(True)[0],
                         line % (base.AGENCY, "partly-approved", 3, 1))
        copy = self.tmp / "copy.json"
        copy.write_text(json.dumps({"N1": {"title": "Search purpose field", "slug": "search-purpose", "event_date": "2026-10-01",
                                           "limitations": ["One records response only."], "counterevidence": [],
                                           "author": "organizer"}}))
        code, out = base.run("flock_publish.py", "stage", self.rd, "--copy", str(copy), "--out", str(self.tmp / "staging"))
        self.assertEqual(code, 0, out)
        self.assertTrue(out.endswith("stage | approved=1 staged=1 skipped_pending=3 | review-required; not built, not deployed;"
                                     " G decides any site use\n"), out)
        self.approve("O1")  # N1 pending: once O1 moves, nothing in the run is approved
        self.assertEqual(self.recheck()[0], 3)
        self.assertEqual(base.run("flock_publish.py", "status", str(self.store))[1].splitlines(True)[0],
                         line % (base.AGENCY, "pending", 4, 0))

    def test_injected_replace_failure_leaves_file_and_no_temp(self):
        self.dispute(N1_FACT)
        before = self.out.read_bytes()
        spec = importlib.util.spec_from_file_location("_publish_recheck", base.TOOLS / "flock_publish.py")
        publish, buf = importlib.util.module_from_spec(spec), io.StringIO()
        spec.loader.exec_module(publish)
        with mock.patch("os.replace", side_effect=OSError("private path")), contextlib.redirect_stdout(buf):
            self.assertEqual(publish.main(["recheck", self.rd, "--today", "2026-10-31"]), 1)
        self.assertEqual(buf.getvalue(), "io: OSError\n")
        self.assertEqual(self.out.read_bytes(), before)
        self.assertEqual(sorted(p.name for p in self.run_dir.iterdir() if p.name.startswith(".")), [])

    def test_usage_and_repository_refusal(self):
        for argv in (("recheck",), ("recheck", self.rd, "--today", "2026-02-30"), ("recheck", self.rd, "--today", ""),
                     ("recheck", self.rd, "--site-slug", "sample"), ("recheck", self.rd, "--check", str(self.out)),
                     ("export", self.rd, "--site-slug", "sample", "--dry-run"), ("status", str(self.store), "--today", "2026-10-31"),
                     ("recheck", self.rd, "--out", str(self.tmp))):
            with self.subTest(argv=argv):
                self.assertEqual(base.run("flock_publish.py", *argv), (2, ""))
        self.assertEqual(self.call(self.store, "recheck", self.rd), (1, "flock-publish: RUNDIR is inside the repository\n"))
        self.assertEqual(base.run("flock_publish.py", "recheck", self.rd), (0, FINAL % (2, 0)))  # --today: the real date


for _name in [n for n in dir(base.FlockPublishTest) if n.startswith("test_")]:
    setattr(PublishRecheckTest, _name, None)  # base tests run once, in tests/flock/test_publish.py

if __name__ == "__main__":
    unittest.main()
