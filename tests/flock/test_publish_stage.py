"""NB-DF-17: `stage RUNDIR --copy COPY.json --out DIR` turns approved PUBLISH.json entries into private site-finding files."""
import contextlib, csv, hashlib, importlib.util, io, json, os, re, subprocess, sys, unittest
from pathlib import Path
from unittest import mock
from campaign_tool import content
from tests.flock import test_publish as base

APPROVED = {"status": "approved", "approved_by": "G", "approved_on": "2026-10-30", "approval_ref": "G-1"}
FINAL = "stage | approved=%d staged=%d skipped_pending=%d | review-required; not built, not deployed; G decides any site use\n"
SHA = "b" * 64


def block(slug, **extra):
    return {"title": "Search purpose field (" + slug + ")", "slug": slug, "event_date": "2026-10-01",
            "limitations": ["One records response only."], "counterevidence": [], "author": "organizer", **extra}


def load_stage():
    spec = importlib.util.spec_from_file_location("_test_stage", base.TOOLS / "flock_publish_stage.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PublishStageTest(base.FlockPublishTest):  # setUp: verified run; here N1 becomes a duty-concern citing pages 2 and 3
    def setUp(self):
        super().setUp()
        with open(self.run_dir / "FINDINGS.csv", newline="") as handle:
            table = list(csv.reader(handle))
        row = next(r for r in table if r[0] == "N1")
        row[2], row[5] = "duty-concern", "F-090c3d39b556;F-76dbe8a9b4ac"
        with open(self.run_dir / "FINDINGS.csv", "w", newline="") as handle:
            csv.writer(handle, lineterminator="\n").writerows(table)
        self.assertEqual(self.export(), (0, "exported=4 refused=0\n"))
        self.doc = json.loads(self.out.read_text())
        self.staging, self.copy = self.tmp / "staging", self.tmp / "copy.json"
        self.approve("N1")
        self.write_copy({"N1": block("search-purpose", classification="documented_fact")})

    def approve(self, *ids):
        for f in self.doc["findings"]:
            if f["finding_id"] in ids:
                f["review"] = dict(APPROVED)
        self.out.write_text(json.dumps(self.doc))

    def write_copy(self, blocks):
        self.copy.write_text(json.dumps(blocks))

    def stage(self, *extra, out=None):  # every run is checked for output hygiene (edge path 9)
        argv = ["stage", self.rd, "--copy", str(self.copy), "--out", str(out or self.staging), *extra]
        done = subprocess.run([sys.executable, "-B", str(base.TOOLS / "flock_publish.py"), *argv],
                              capture_output=True, text=True, check=False)
        self.assertNotRegex(done.stdout + done.stderr, r"[0-9a-fA-F]{64}")
        for f in self.doc["findings"]:
            for secret in [f["text"]] + [c["label"] for c in f["cites"]]:
                self.assertNotIn(secret, done.stdout + done.stderr)
        return done.returncode, done.stdout

    def assert_refused(self, result, *lines, code=1):
        self.assertEqual(result[0], code, result[1])
        for line in lines:
            self.assertIn(line + "\n", result[1])
        self.assertRegex(result[1], r"stage refused \| errors=\d+ \| nothing written\n$")
        self.assertFalse(self.staging.exists())

    def test_one_approved_entry_staged_pending_skipped(self):
        self.assertEqual(self.stage(), (0, "staged N1 -> content/findings/search-purpose.json\n" + FINAL % (1, 1, 3)))
        path = self.staging / "content" / "findings" / "search-purpose.json"
        self.assertEqual(sorted(str(p.relative_to(self.staging)) for p in self.staging.rglob("*")),
                         ["STAGE-MANIFEST.json", "content", "content/findings", "content/findings/search-purpose.json"])
        for p in (self.staging, path.parent, path.parent.parent):
            self.assertEqual(p.stat().st_mode & 0o777, 0o700)
        for p in (path, self.staging / "STAGE-MANIFEST.json"):
            self.assertEqual(p.stat().st_mode & 0o777, 0o600)
        doc, n1 = json.loads(path.read_text()), self.doc["findings"][0]
        self.assertEqual(doc["summary"], n1["text"])
        self.assertEqual(doc["id"], "fnd_" + hashlib.sha256(b"sample\x1fN1").hexdigest()[:16])
        self.assertEqual((doc["state"], doc["published_at"], doc["confidence"], doc["classification"]),
                         ("staged", None, "verified", "documented_fact"))
        self.assertEqual(doc["sources"], [{"sha256": SHA, "locator": "page:%d" % p, "title": "Source record", "rule_id": None}
                                          for p in (2, 3)])
        loaded = content.load_finding({**doc, "state": "published", "published_at": "2026-10-30T12:00:00+00:00"}, "staged")
        self.assertEqual(loaded["summary"], n1["text"])
        with self.assertRaises(content.ContentError):  # as staged, the site refuses to render it
            content.load_finding(doc, "staged")
        manifest = json.loads((self.staging / "STAGE-MANIFEST.json").read_text())
        self.assertEqual(manifest, {"schema_version": 1, "site_slug": "sample", "run_date": "2026-10-30", "agency": base.AGENCY,
                                    "staged": [{"finding_id": "N1", "slug": "search-purpose",
                                                "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}],
                                    "skipped_pending": 3, "approval_refs": ["G-1"]})

    def test_record_gap_defaults_to_information_gap_and_empty_out_folder_is_used(self):
        self.approve("O1")
        self.write_copy({"N1": block("search-purpose", classification="documented_fact"), "O1": block("outside-access")})
        self.staging.mkdir()
        self.assertEqual(self.stage()[1].splitlines()[-1] + "\n", FINAL % (2, 2, 2))
        doc = json.loads((self.staging / "content" / "findings" / "outside-access.json").read_text())
        self.assertEqual((doc["classification"], doc["sources"][0]["locator"]), ("information_gap", "page:3"))

    def test_sources_one_per_sha_and_page_sorted(self):
        stage = load_stage()
        facts = {"F-a": {"source_sha256": SHA, "page": "10"}, "F-b": {"source_sha256": SHA, "page": " 10"},
                 "F-c": {"source_sha256": SHA, "page": "9"}, "F-d": {"source_sha256": "a" * 64, "page": "12"}}
        self.assertEqual([(s["sha256"][0], s["locator"]) for s in stage.sources("F-a; F-b;F-c;F-d", facts)],
                         [("a", "page:12"), ("b", "page:9"), ("b", "page:10")])
        self.assertEqual(len(stage.sources("F-a;F-b", facts)), 1)

    def test_check_failures_write_nothing(self):
        text = self.doc["findings"][0]["text"]
        self.doc["findings"][0]["text"] = "Changed."
        self.out.write_text(json.dumps(self.doc))
        self.assert_refused(self.stage(), "finding N1: text differs from the run", "PUBLISH.json: check failed")
        self.doc["findings"][0].update(text=text, review=dict(APPROVED, approval_ref=""))
        self.out.write_text(json.dumps(self.doc))
        self.assert_refused(self.stage(), "PUBLISH.json: check failed")
        self.out.unlink()
        self.assert_refused(self.stage(), "PUBLISH.json: missing")
        self.out.symlink_to(self.tmp / "copy.json")
        self.assert_refused(self.stage(), "PUBLISH.json: missing")

    def test_nothing_approved(self):
        self.doc["findings"][0]["review"] = {"status": "pending", "approved_by": "", "approved_on": "", "approval_ref": ""}
        self.out.write_text(json.dumps(self.doc))
        self.assert_refused(self.stage(), "PUBLISH.json: nothing approved")

    def test_copy_refusals_are_named(self):
        self.approve("O1")
        good = {"N1": block("search-purpose", classification="documented_fact"), "O1": block("outside-access")}
        n1 = good["N1"]
        cases = [({"N1": n1}, "copy O1: approved entry has no copy block"),
                 ({**good, "A1": block("audits")}, "copy A1: entry is not approved"),
                 ({**good, "Z9": block("other")}, "copy Z9: not a finding in PUBLISH.json"),
                 ({**good, "bad id @x": block("other")}, "copy #3: not a finding in PUBLISH.json"),
                 ({**good, "N1": {**n1, "body_md": "extra"}}, "copy N1: keys differ from the copy schema"),
                 ({**good, "N1": {k: v for k, v in n1.items() if k != "author"}}, "copy N1: keys differ from the copy schema"),
                 ({**good, "N1": {**n1, "classification": "confirmed_conflict"}},
                  "copy N1: conflict classification confirmed_conflict refused"),
                 ({**good, "O1": block("outside-access", classification="apparent_conflict")},
                  "copy O1: conflict classification apparent_conflict refused"),
                 ({**good, "N1": block("search-purpose")}, "copy N1: duty-concern needs a classification"),
                 ({**good, "N1": {**n1, "classification": None}}, "copy N1: classification is not documented_fact or information_gap"),
                 ({**good, "N1": {**n1, "limitations": []}}, "copy N1: limitations needs at least one non-empty text"),
                 ({**good, "N1": {**n1, "limitations": [" "]}}, "copy N1: limitations needs at least one non-empty text"),
                 ({**good, "N1": {**n1, "counterevidence": "none"}}, "copy N1: counterevidence must be a list of non-empty texts"),
                 ({**good, "N1": {**n1, "title": ""}}, "copy N1: title, slug, event_date and author must be non-empty text"),
                 ({**good, "N1": {**n1, "title": "Ask records@sample.org"}}, "copy N1: title refused email"),
                 ({**good, "N1": {**n1, "agency_id": 7}}, "copy N1: agency_id must be text or null"),
                 ({**good, "N1": {**n1, "event_date": "2026-13-01"}}, "schema N1.event_date: invalid ISO-8601 value '2026-13-01'"),
                 ({**good, "N1": {**n1, "slug": "findings"}}, "schema N1.slug: 'findings' is reserved"),
                 ({**good, "N1": {**n1, "slug": "Bad Slug"}}, "schema N1.slug: must be a lowercase slug (a-z, 0-9, hyphen)"),
                 ({**good, "O1": block("search-purpose")}, "copy O1: slug duplicates the block of N1"),
                 (["N1"], "copy: not a JSON object")]
        for blocks, line in cases:
            with self.subTest(line):
                self.write_copy(blocks)
                self.assert_refused(self.stage(), line)
        self.copy.write_text("{not json")
        self.assert_refused(self.stage(), "copy: not a JSON object")

    def test_out_and_copy_paths_refused(self):
        (self.tmp / "full").mkdir()
        (self.tmp / "full" / "x").write_text("x")
        (self.tmp / "empty").mkdir()
        (self.tmp / "link").symlink_to(self.tmp / "empty")
        for out, line in ((self.tmp / "full", "--out exists and is not an empty folder"), (self.copy, "--out exists and is not an empty folder"),
                          (self.tmp / "link", "--out is a symlink"), (self.run_dir / "staged", "--out is inside RUNDIR"),
                          (self.run_dir, "--out is inside RUNDIR")):
            with self.subTest(line=line, out=out.name):
                self.assert_refused(self.stage(out=out), line)
        self.assertEqual(list((self.tmp / "empty").iterdir()), [])
        fake = self.tmp / "fake-repo"  # verify_tool.REPO patched to a root that holds --out or --copy but not RUNDIR
        fake.mkdir()
        inner = fake / "copy.json"
        inner.write_text(self.copy.read_text())
        for out, copy, line in ((fake / "staged", self.copy, "--out is inside the repository"),
                                (self.staging, inner, "--copy is inside the repository")):
            with self.subTest(line=line):
                code, text = self.call(fake, "stage", self.rd, "--copy", str(copy), "--out", str(out))
                self.assertEqual((code, line + "\n" in text, out.exists()), (1, True, False), text)
        code, text = self.call(self.tmp / "repo", "stage", self.rd, "--copy", str(self.copy), "--out", str(self.staging))
        self.assertEqual(code, 0, text)  # a lookalike root holding neither is not the repository

    def test_copy_inside_real_repository_refused(self):
        self.assert_refused(self.stage("--copy", str(base.ROOT / "README.md")), "--copy is inside the repository")
        self.assert_refused(self.stage(out=base.ROOT / "staged-nb-df-17"), "--out is inside the repository")
        self.assertFalse((base.ROOT / "staged-nb-df-17").exists())

    def test_injected_write_failure_leaves_nothing(self):
        self.approve("O1")
        self.write_copy({"N1": block("search-purpose", classification="documented_fact"), "O1": block("outside-access")})
        real, calls = os.link, []

        def flaky(src, dst):
            calls.append(dst)
            if len(calls) == 2:
                raise OSError("disk full")
            return real(src, dst)
        with mock.patch("os.link", side_effect=flaky):
            code, out = self.call(self.tmp / "repo", "stage", self.rd, "--copy", str(self.copy), "--out", str(self.staging))
        self.assertEqual((code, out), (1, "io: OSError\n"))
        self.assertEqual(len(calls), 2)
        self.assertFalse(self.staging.exists())
        self.staging.mkdir()
        calls.clear()
        with mock.patch("os.link", side_effect=flaky):
            self.assertEqual(self.call(self.tmp / "repo", "stage", self.rd, "--copy", str(self.copy), "--out", str(self.staging))[0], 1)
        self.assertEqual(list(self.staging.iterdir()), [])  # a pre-existing empty folder is kept, emptied again

    def test_existing_file_is_never_replaced(self):
        stage = load_stage()
        target = self.tmp / "taken.json"
        target.write_text("old")
        with self.assertRaises(FileExistsError):
            stage.link(target, b"new")
        self.assertEqual((target.read_text(), sorted(p.name for p in self.tmp.glob(".taken*"))), ("old", []))

    def test_same_family_run_exit_3(self):
        (self.run_dir / "VERIFY.json").write_text(json.dumps({"schema": 1, "verifier_provider": "anthropic"}))
        self.assert_refused(self.stage(), "refused: verifier family equals author family", code=3)

    def test_usage(self):
        for argv in (("stage", self.rd, "--copy", str(self.copy)), ("stage", self.rd, "--out", str(self.staging)),
                     ("stage", "--copy", str(self.copy), "--out", str(self.staging)),
                     ("stage", self.rd, "--copy", str(self.copy), "--out", str(self.staging), "--site-slug", "sample"),
                     ("export", self.rd, "--site-slug", "sample", "--out", str(self.staging)),
                     ("status", str(self.store), "--copy", str(self.copy)), ("--check", str(self.out), "--copy", str(self.copy))):
            with self.subTest(argv=argv):
                self.assertEqual(base.run("flock_publish.py", *argv), (2, ""))
        self.assertFalse(self.staging.exists())


for _name in [n for n in dir(base.FlockPublishTest) if n.startswith("test_")]:
    setattr(PublishStageTest, _name, None)  # base tests run once, in tests/flock/test_publish.py

if __name__ == "__main__":
    unittest.main()
