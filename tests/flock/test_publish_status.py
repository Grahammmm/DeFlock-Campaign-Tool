"""RF1130-3: `status STORE` prints one publication-stage line per verified run, then totals; read-only, counts only."""
import json, os, shutil, subprocess, sys, unittest
from tests.flock import test_publish as base

APPROVED = {"status": "approved", "approved_by": "G", "approved_on": "2026-10-30", "approval_ref": "G-1"}
KEYS = ("no_export", "invalid", "drifted", "pending", "partly_approved", "approved")
AGENCY = base.AGENCY


def row(stage, counts="findings=- pending=- approved=- errors=-", slug=AGENCY, date="2026-10-30"):
    return "publish | %s | %s | %s | %s\n" % (slug, date, stage, counts)


def totals(runs, **n):
    counts = " ".join("%s=%d" % (k, n.get(k, 0)) for k in KEYS)
    return "publish | runs=%d %s | review-required; G decides any site use\n" % (runs, counts)


class PublishStatusTest(base.FlockPublishTest):  # setUp builds store/agencies/<AGENCY>/runs/2026-10-30 (F4 finished)
    def status(self, store=None):
        return base.run("flock_publish.py", "status", str(store or self.store))
    def exported(self):
        self.assertEqual(self.export(), (0, "exported=4 refused=0\n"))
        return json.loads(self.out.read_text())
    def rewrite(self, doc, path=None):
        (path or self.out).write_text(json.dumps(doc))
    def test_verified_run_without_export(self):
        self.assertEqual(self.status(), (0, row("no-export") + totals(1, no_export=1)))
    def test_pending_partly_and_fully_approved(self):
        doc = self.exported()
        self.assertEqual(self.status(), (0, row("pending", "findings=4 pending=4 approved=0 errors=0") + totals(1, pending=1)))
        doc["findings"][0]["review"] = APPROVED
        self.rewrite(doc)
        self.assertEqual(self.status(), (0, row("partly-approved", "findings=4 pending=3 approved=1 errors=0")
                                         + totals(1, partly_approved=1)))
        for f in doc["findings"]:
            f["review"] = APPROVED
        self.rewrite(doc)
        self.assertEqual(self.status(), (0, row("approved", "findings=4 pending=0 approved=4 errors=0") + totals(1, approved=1)))
    def test_invalid_and_drifted_print_counts_only(self):
        doc = self.exported()
        cases = [("bad header", lambda d: d.update(header="Final"), "invalid", "findings=0 pending=0 approved=0 errors=1"),
                 ("not json", None, "invalid", "findings=0 pending=0 approved=0 errors=1"),
                 ("email wording", lambda d: d["findings"][0].update(text=d["findings"][0]["text"] + " ask records@sample.org"),
                  "invalid", "findings=4 pending=4 approved=0 errors=1"),
                 ("edited text", lambda d: d["findings"][0].update(text="Changed."), "drifted",
                  "findings=4 pending=4 approved=0 errors=1"),
                 ("edited and approved", lambda d: d["findings"][0].update(text="Changed.", review=APPROVED), "drifted",
                  "findings=4 pending=3 approved=1 errors=1")]
        for name, change, stage, counts in cases:
            with self.subTest(name):
                bad = json.loads(json.dumps(doc))
                if change:
                    change(bad)
                self.out.write_text(json.dumps(bad) if change else "{not json")
                code, out = self.status()
                self.assertEqual((code, out), (1, row(stage, counts) + totals(1, **{stage: 1})))
                for word in ("N1", "differs", "refused", "Changed", "records@", "schema"):
                    self.assertNotIn(word, out)
    def test_run_that_cannot_be_rederived_or_copied_from_another_run_is_drifted(self):
        self.exported()
        other = self.run_dir.with_name("2026-10-31")
        shutil.copytree(self.run_dir, other)  # PUBLISH.json carries run_date 2026-10-30: stale for this run
        signed = (self.run_dir / "VERIFY.json").read_text()
        (self.run_dir / "VERIFY.json").write_text(json.dumps({"schema": 1, "verifier_provider": "anthropic"}))
        drifted = "findings=4 pending=4 approved=0 errors=1"
        self.assertEqual(self.status(), (1, row("drifted", drifted) + row("drifted", drifted, date="2026-10-31")
                                         + totals(2, drifted=2)))
        (self.run_dir / "VERIFY.json").write_text(signed)
        (self.run_dir / "RUN.json").write_text("{}")  # family unknown: derive refused
        self.assertEqual(self.status()[1].splitlines()[0], row("drifted", drifted).strip())
        import contextlib, importlib.util, io
        from unittest import mock
        spec = importlib.util.spec_from_file_location("_publish_status", base.TOOLS / "flock_publish.py")
        publish, buf = importlib.util.module_from_spec(spec), io.StringIO()
        spec.loader.exec_module(publish)
        with mock.patch.object(publish, "derive", side_effect=RuntimeError("private source text")), \
                contextlib.redirect_stdout(buf):  # derive raising: still drifted, counts unknown, never a traceback
            self.assertEqual(publish.main(["status", str(self.store)]), 1)
        self.assertEqual(buf.getvalue(), row("drifted") + row("drifted", date="2026-10-31") + totals(2, drifted=2))
    def test_two_agencies_two_dates_sorted_with_totals(self):
        self.exported()
        later = self.run_dir.with_name("2026-10-31")
        shutil.copytree(self.run_dir, later)
        (later / "PUBLISH.json").unlink()
        first = self.store / "agencies" / "a-agency"
        for date in ("2026-10-31", "2026-10-29"):  # created out of order
            shutil.copytree(later, first / "runs" / date)
        self.assertEqual(self.status(), (0, row("no-export", slug="a-agency", date="2026-10-29")
                                         + row("no-export", slug="a-agency", date="2026-10-31")
                                         + row("pending", "findings=4 pending=4 approved=0 errors=0")
                                         + row("no-export", date="2026-10-31") + totals(4, no_export=3, pending=1)))
    def test_unverified_runs_and_stray_files_ignored(self):
        early = self.run_dir.with_name("2026-10-31")
        shutil.copytree(self.run_dir, early)
        (early / "VERIFY.json").unlink()
        (self.run_dir.parent / "notes.txt").write_text("x")
        (self.store / "agencies" / "stray.txt").write_text("x")
        (self.store / "agencies" / "no-runs-yet").mkdir()
        self.assertEqual(self.status(), (0, row("no-export") + totals(1, no_export=1)))
    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root reads mode-000 folders")
    def test_symlinks_refused_and_never_followed(self):
        hidden = self.tmp / "hidden-run"
        shutil.copytree(self.run_dir, hidden)
        hidden.chmod(0)
        self.addCleanup(hidden.chmod, 0o700)
        runs = self.run_dir.parent
        (runs / "2026-10-31").symlink_to(hidden)
        (runs / "2026-11-01").symlink_to(self.tmp / "missing")
        (self.store / "agencies" / "b-agency").symlink_to(self.store / "agencies" / AGENCY)
        linked = self.store / "agencies" / "c-agency"
        linked.mkdir()
        (linked / "runs").symlink_to(runs)
        self.assertEqual(self.status(), (1, row("link-refused", slug="b-agency", date="-")
                                         + row("link-refused", slug="c-agency", date="-") + row("no-export")
                                         + row("link-refused", date="2026-10-31") + row("link-refused", date="2026-11-01")
                                         + totals(5, no_export=1)))
        hidden.chmod(0o700)
        self.exported()
        moved = self.tmp / "PUBLISH.json"
        self.out.rename(moved)
        self.out.symlink_to(moved)
        self.assertEqual(self.status()[1].splitlines()[2], row("link-refused").strip())
    def test_no_agencies_folder(self):
        empty = self.tmp / "empty"
        empty.mkdir()
        (self.tmp / "filed").mkdir()
        (self.tmp / "filed" / "agencies").write_text("x")
        (self.tmp / "linked").mkdir()
        (self.tmp / "linked" / "agencies").symlink_to(self.store / "agencies")
        for store in (empty, self.tmp / "missing", self.tmp / "filed", self.tmp / "linked"):
            with self.subTest(store=store.name):
                done = subprocess.run([sys.executable, "-B", str(base.TOOLS / "flock_publish.py"), "status", str(store)],
                                      capture_output=True, text=True, check=False)
                self.assertEqual((done.returncode, done.stdout, done.stderr), (2, "", "status: no agencies folder\n"))
    def test_store_inside_repository_refused_and_usage(self):
        refused = (1, "flock-publish: STORE is inside the repository\n")
        for repo, store in ((self.store, self.store), (self.tmp, self.store), (self.store, self.store / "nope")):
            with self.subTest(repo=repo.name, store=store.name):
                self.assertEqual(self.call(repo, "status", str(store)), refused)
        self.assertEqual(base.run("flock_publish.py", "status", str(base.ROOT)), refused)
        for argv in (("status",), ("status", str(self.store), "--site-slug", "sample"), ("status", str(self.store), "--run", ""),
                     ("status", str(self.store), "--check", str(self.out)), ("status", str(self.store), "extra")):
            with self.subTest(argv=argv):
                self.assertEqual(base.run("flock_publish.py", *argv), (2, ""))
    def test_status_never_writes(self):
        doc = self.exported()
        later = self.run_dir.with_name("2026-10-31")
        shutil.copytree(self.run_dir, later)
        doc["findings"][0]["text"] = "Changed."
        self.rewrite(doc, later / "PUBLISH.json")

        def snap():
            return {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in sorted(self.store.rglob("*")) if p.is_file()}
        before = snap()
        self.assertEqual(self.status()[0], 1)
        self.assertEqual(snap(), before)
    def test_output_carries_no_record_text(self):
        doc = self.exported()
        doc["findings"][0]["review"] = APPROVED
        self.rewrite(doc)
        odd = self.run_dir.with_name("note@sample.org")
        shutil.copytree(self.run_dir, odd)
        (odd / "PUBLISH.json").unlink()
        code, out = self.status()
        self.assertEqual((code, out), (0, row("partly-approved", "findings=4 pending=3 approved=1 errors=0")
                                       + row("no-export", date="?") + totals(2, no_export=1, partly_approved=1)))
        secrets = [f["text"] for f in doc["findings"]] + [c["label"] for f in doc["findings"] for c in f["cites"]]
        secrets += [c["url"] for f in doc["findings"] for c in f["cites"] if "url" in c] + ["@", "http", "G-1"]
        for secret in secrets:
            self.assertNotIn(secret, out)


for _name in [n for n in dir(base.FlockPublishTest) if n.startswith("test_")]:
    setattr(PublishStatusTest, _name, None)  # base tests run once, in tests/flock/test_publish.py

if __name__ == "__main__":
    unittest.main()
