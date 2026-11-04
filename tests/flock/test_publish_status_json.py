"""RF0910-11: `status STORE --json` prints one flock-publish-status-v1 document: the text rows and totals, same exit code."""
import json, re, shutil, subprocess, sys, unittest
from tests.flock import test_publish as base
from tests.flock import test_publish_status as text

STAGES = ("no-export", "invalid", "drifted", "pending", "partly-approved", "approved", "link-refused")
NOTE = "review-required; G decides any site use"


def parse(out):  # the text form -> (rows as JSON-shaped dicts, totals); "-" counts become None
    lines = out.splitlines()
    rows = []
    for line in lines[:-1]:
        _, agency, run, stage, counts = line.split(" | ")
        n = {k: None if v == "-" else int(v) for k, v in re.findall(r"(\w+)=(\S+)", counts)}
        rows.append({"agency": agency, "run": run, "stage": stage, **n})
    totals = {k: int(v) for k, v in re.findall(r"(\w+)=(\d+)", lines[-1])}
    return rows, totals


class PublishStatusJsonTest(text.PublishStatusTest):
    def both(self, store=None):
        code, out = self.status(store)
        jcode, jout = base.run("flock_publish.py", "status", str(store or self.store), "--json")
        self.assertEqual(jout.count("\n"), 1)  # one document
        return code, out, jcode, json.loads(jout), jout

    def assert_matches_text(self, out, doc, link_refused=0):
        rows, totals = parse(out)
        self.assertEqual(doc["schema"], "flock-publish-status-v1")
        self.assertEqual(doc["note"], NOTE)
        self.assertEqual(doc["runs"], rows)
        self.assertEqual(doc["totals"], {**totals, "link_refused": link_refused})
        self.assertEqual(set(doc["totals"]), {"runs"} | {s.replace("-", "_") for s in STAGES})

    def second_run(self, date):  # a second verified run of the agency, built and exported exactly as setUp builds the first
        rd = self.run_dir.with_name(date)
        self.assertEqual(base.run("flock_compare.py", "scaffold", "--store", str(self.store), "--agency", base.AGENCY, "--date", date,
                                  "--provider", "anthropic", "--model", "claude-opus-5-5", "--applicability", "Sample city police")[0], 0)
        (rd / "FINDINGS.csv").write_text(self.fixture_text("findings-sample.csv"))
        self.assertEqual(base.run("flock_verify.py", "start", str(rd), "--provider", "openai", "--model", "gpt-5-codex")[0], 0)
        (rd / "VERIFY.csv").write_text(self.fixture_text("verify-sample.csv"))
        self.assertEqual(base.run("flock_verify.py", "finish", str(rd))[0], 0)
        self.assertEqual(base.run("flock_publish.py", "export", str(rd), "--site-slug", "sample")[0], 0)
        return rd

    def test_four_stages_match_text_field_for_field(self):
        doc = self.exported()
        for f in doc["findings"]:
            f["review"] = text.APPROVED
        self.rewrite(doc)  # 2026-10-30 approved
        self.second_run("2026-11-01")  # pending
        shutil.copytree(self.run_dir, self.run_dir.with_name("2026-10-31"))  # stale copy: drifted
        shutil.copytree(self.run_dir, self.run_dir.with_name("2026-10-29"))
        (self.run_dir.with_name("2026-10-29") / "PUBLISH.json").unlink()  # no-export
        code, out, jcode, got, _ = self.both()
        self.assertEqual((code, jcode), (1, 1))
        self.assertEqual([r["stage"] for r in got["runs"]], ["no-export", "approved", "drifted", "pending"])
        self.assert_matches_text(out, got)
        self.assertEqual(got["runs"][0]["findings"], None)
        self.assertEqual(got["totals"]["runs"], 4)

    def test_links_refused_with_null_counts(self):
        (self.store / "agencies" / "b-agency").symlink_to(self.store / "agencies" / base.AGENCY)
        self.exported()
        moved = self.tmp / "PUBLISH.json"
        self.out.rename(moved)
        self.out.symlink_to(moved)
        code, out, jcode, got, _ = self.both()
        self.assertEqual((code, jcode), (1, 1))
        nulls = dict.fromkeys(("findings", "pending", "approved", "errors"))
        self.assertEqual(got["runs"], [{"agency": "b-agency", "run": "-", "stage": "link-refused", **nulls},
                                       {"agency": base.AGENCY, "run": "2026-10-30", "stage": "link-refused", **nulls}])
        self.assert_matches_text(out, got, link_refused=2)
        self.assertEqual(out.splitlines()[-1], text.totals(2).strip())  # text totals row unchanged: no link_refused

    def test_empty_agencies_folder(self):
        empty = self.tmp / "bare"
        (empty / "agencies").mkdir(parents=True)
        code, out, jcode, got, raw = self.both(empty)
        self.assertEqual((code, out, jcode), (0, text.totals(0), 0))
        self.assertEqual(got, {"schema": "flock-publish-status-v1", "runs": [], "note": NOTE,
                               "totals": {"runs": 0, **{s.replace("-", "_"): 0 for s in STAGES}}})
        self.assertEqual(raw, json.dumps(got, sort_keys=True, ensure_ascii=False) + "\n")  # sort_keys, one line

    def test_no_agencies_folder_is_a_json_error(self):
        (self.tmp / "empty").mkdir()
        (self.tmp / "filed").mkdir()
        (self.tmp / "filed" / "agencies").write_text("x")
        (self.tmp / "linked").mkdir()
        (self.tmp / "linked" / "agencies").symlink_to(self.store / "agencies")
        for store in ("empty", "missing", "filed", "linked"):
            with self.subTest(store=store):
                done = subprocess.run([sys.executable, "-B", str(base.TOOLS / "flock_publish.py"), "status", str(self.tmp / store), "--json"],
                                      capture_output=True, text=True, check=False)
                self.assertEqual((done.returncode, json.loads(done.stdout), done.stderr),
                                 (2, {"schema": "flock-publish-status-v1", "error": "no agencies folder"}, ""))

    def test_unsafe_names_and_no_paths(self):
        self.exported()
        bad = self.store / "agencies" / "bad name!"
        shutil.copytree(self.store / "agencies" / base.AGENCY, bad)
        (bad / "runs" / "2026-10-30" / "PUBLISH.json").unlink()
        odd = self.run_dir.with_name("note@sample.org")
        shutil.copytree(self.run_dir, odd)
        (odd / "PUBLISH.json").unlink()
        code, out, jcode, got, raw = self.both()
        self.assertEqual((code, jcode), (0, 0))
        self.assertEqual([(r["agency"], r["run"], r["stage"]) for r in got["runs"]],
                         [("?", "2026-10-30", "no-export"), (base.AGENCY, "2026-10-30", "pending"), (base.AGENCY, "?", "no-export")])
        self.assert_matches_text(out, got)
        for leak in (str(self.store), str(self.tmp), "bad name", "note@", "@", "/"):
            self.assertNotIn(leak, raw)

    def test_json_only_with_status_and_text_unchanged(self):
        self.exported()
        argv = [("export", str(self.run_dir), "--site-slug", "sample"), ("stage", str(self.run_dir), "--copy", str(self.out), "--dry-run"),
                ("brief", str(self.store), "--agency", base.AGENCY, "--out", str(self.tmp / "b.md")), ("recheck", str(self.run_dir)),
                ("withdraw", str(self.run_dir), "--finding", "N1", "--on", "2026-10-30", "--by", "G", "--approval-ref", "G-1"),
                ("--check", str(self.out)), ("status",)]
        for args in argv:
            with self.subTest(argv=args[0]):
                self.assertEqual(base.run("flock_publish.py", *args, "--json"), (2, ""))
        self.assertFalse((self.tmp / "b.md").exists())
        pending = text.row("pending", "findings=4 pending=4 approved=0 errors=0") + text.totals(1, pending=1)
        self.assertEqual(self.status(), (0, pending))  # the text form is byte-identical without --json
        self.assertEqual(base.run("flock_publish.py", "--json", "status", str(self.store))[0], 0)  # flag position is free


for _name in [n for n in dir(text.PublishStatusTest) if n.startswith("test_")]:
    setattr(PublishStatusJsonTest, _name, None)  # the text tests run once, in tests/flock/test_publish_status.py


if __name__ == "__main__":
    unittest.main()
