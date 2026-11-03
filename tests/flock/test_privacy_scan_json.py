"""RF0250-8: flock_privacy_scan.py --json-out NEWFILE writes one flock-privacy-scan-v1 handoff receipt (per-file outcome, sha256
of the scanned bytes, finding line/category, totals), 0600 and created new, outside the repository and every scanned PATH; never
the matched text. Stdout and exit codes are unchanged with or without the flag. Synthetic fixtures only."""
import contextlib, errno, hashlib, importlib.util, io, json, os, stat, subprocess, sys, tempfile, unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools" / "flock_privacy_scan.py"
EMAIL, PHONE, PLATE = "clerk@city.example.gov", "(805) 555-1234", "7ABC123"
SECRETS = (EMAIL, PHONE, PLATE, "555", "7ABC", "clerk")


class PrivacyScanJsonTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(os.path.realpath(tmp.name))
        for sub in ("cwd", "out"):
            (self.tmp / sub).mkdir()
        self.cwd, self.out = self.tmp / "cwd", self.tmp / "out" / "receipt.json"

    def put(self, rel, content):
        path = self.tmp / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
        return path

    def run_scan(self, *args):
        done = subprocess.run([sys.executable, "-B", str(TOOL), *map(str, args)], cwd=self.cwd, capture_output=True,
                              text=True, check=False, timeout=60)
        return done.returncode, done.stdout, done.stderr

    def receipt(self):
        raw = self.out.read_bytes()
        return raw, json.loads(raw)

    def edge1(self):
        self.put("d/clean.md", "Dear records officer,\nPlease send the retention policy.\n")
        return self.put("d/story.md", f"write {EMAIL} or call {PHONE}\nplate {PLATE}\n").parent

    def test_scanned_files_sha256_findings_never_matched_text(self):
        d = self.edge1()
        code, out, err = self.run_scan(d, "--json-out", self.out)
        self.assertEqual((code, err), (3, ""))
        raw, doc = self.receipt()
        sha = {n: hashlib.sha256((d / n).read_bytes()).hexdigest() for n in ("clean.md", "story.md")}
        self.assertEqual(doc, {
            "schema": "flock-privacy-scan-v1", "review_required": True, "note": "patterns only; a clean scan is not a privacy review",
            "max_bytes": 2000000, "allow_file_used": False,
            "files": [{"path": f"{d}/clean.md", "outcome": "scanned", "reason": None, "sha256": sha["clean.md"], "findings": []},
                      {"path": f"{d}/story.md", "outcome": "scanned", "reason": None, "sha256": sha["story.md"], "findings": [
                          {"line": 1, "category": "email"}, {"line": 1, "category": "phone"}, {"line": 2, "category": "plate"}]}],
            "totals": {"files": 2, "scanned": 2, "skipped": 0, "findings": 3, "allowed": 0, "failed": False}})
        for secret in SECRETS:
            self.assertNotIn(secret.encode(), raw)
        self.assertEqual(raw, (json.dumps(doc, sort_keys=True, indent=2, ensure_ascii=True) + "\n").encode())

    def test_stdout_unchanged_with_and_without_flag(self):
        d = self.edge1()
        expected = (f"{d}/story.md:1: email\n{d}/story.md:1: phone\n{d}/story.md:2: plate\n"
                    "files=2 scanned=2 skipped=0 findings=3 allowed=0\n")
        self.assertEqual(self.run_scan(d), (3, expected, ""))
        self.assertEqual(self.run_scan(d, "--json-out", self.out), (3, expected, ""))

    def test_every_skip_reason_has_null_sha(self):
        d = self.tmp / "d"
        allow = self.put("d/ALLOW.txt", "nobody@example.org\n")
        self.put("d/big.md", "x" * 100)
        self.put("d/bin.dat", b"ok\0")
        self.put("d/ok.md", "fine\n")
        os.mkfifo(d / "pipe")
        os.symlink(d / "ok.md", d / "zlink.md")
        code, out, err = self.run_scan(d, "--allow", allow, "--max-bytes", 50, "--json-out", self.out)
        self.assertEqual((code, err), (0, ""))
        doc = self.receipt()[1]
        got = [(f["path"], f["outcome"], f["reason"], f["sha256"], f["findings"]) for f in doc["files"]]
        self.assertEqual(got, [
            (f"{d}/ALLOW.txt", "skipped", "allow", None, []), (f"{d}/big.md", "skipped", "size", None, []),
            (f"{d}/bin.dat", "skipped", "binary", None, []),
            (f"{d}/ok.md", "scanned", None, hashlib.sha256(b"fine\n").hexdigest(), []),
            (f"{d}/pipe", "skipped", "special", None, []), (f"{d}/zlink.md", "skipped", "link", None, [])])
        self.assertEqual(doc["totals"], {"files": 6, "scanned": 1, "skipped": 5, "findings": 0, "allowed": 0, "failed": False})
        self.assertEqual((doc["max_bytes"], doc["allow_file_used"]), (50, True))

    def test_missing_path_recorded_failed_exit_1_still_written(self):
        a = self.put("d/a.md", f"{EMAIL}\n")
        gone = self.tmp / "d" / "gone.md"
        code, out, err = self.run_scan(a, gone, "--json-out", self.out)
        self.assertEqual((code, err), (1, ""))
        self.assertEqual(out.splitlines(), [f"{a}:1: email", f"{gone}: missing", "files=1 scanned=1 skipped=0 findings=1 allowed=0"])
        doc = self.receipt()[1]
        self.assertEqual(doc["files"][1], {"path": str(gone), "outcome": "missing", "reason": None, "sha256": None, "findings": []})
        self.assertEqual(doc["totals"], {"files": 1, "scanned": 1, "skipped": 0, "findings": 1, "allowed": 0, "failed": True})

    def test_refusals_before_any_scan_output_nothing_created(self):
        d = self.edge1()
        (d / "sub").mkdir()
        (self.tmp / "real").mkdir()
        os.symlink(self.tmp / "real", self.tmp / "via")
        os.symlink(d, self.tmp / "dlink")
        os.symlink(self.tmp / "nowhere.json", self.tmp / "out" / "dangling.json")
        self.put("out/exists.json", "{}\n")
        repo = Path(os.path.realpath(ROOT))
        cases = [  # (rule named in the message, PATH, --json-out, path that must not appear)
            ("absolute", d, "out/r.json", self.cwd / "out"), ("normalized", d, f"{self.tmp}/out/../out/r.json", self.out.parent / "r.json"),
            ("normalized", d, f"{self.tmp}/out//r.json", self.out.parent / "r.json"),
            ("not exist", d, self.tmp / "out" / "exists.json", None), ("not exist", d, self.tmp / "out" / "dangling.json", self.tmp / "nowhere.json"),
            ("existing folder", d, self.tmp / "nofolder" / "r.json", self.tmp / "nofolder"),
            ("symlink", d, self.tmp / "via" / "r.json", self.tmp / "real" / "r.json"),
            ("repository", d, repo / "rf0250-8-receipt.json", repo / "rf0250-8-receipt.json"),
            ("scanned PATH", d, d / "r.json", d / "r.json"), ("scanned PATH", d, d / "sub" / "r.json", d / "sub" / "r.json"),
            ("scanned PATH", d / "story.md", d / "r.json", d / "r.json"),
            ("scanned PATH", self.tmp / "dlink", d / "r.json", d / "r.json"),
            ("scanned PATH", ".", self.cwd / "r.json", self.cwd / "r.json"),
        ]
        for rule, path, target, absent in cases:
            with self.subTest(rule=rule, target=str(target)):
                code, out, err = self.run_scan(path, "--json-out", target)
                self.assertEqual((code, out), (2, ""))
                self.assertIn("--json-out must", err)
                self.assertIn(rule, err)
                self.assertNotIn(str(target), err)
                if absent is not None:
                    self.assertFalse(os.path.lexists(absent))
        self.assertEqual((self.tmp / "out" / "exists.json").read_text(), "{}\n")
        self.assertTrue(os.path.islink(self.tmp / "out" / "dangling.json"))
        self.assertEqual(sorted(p.name for p in d.iterdir()), ["clean.md", "story.md", "sub"])
        self.assertEqual(list(self.cwd.iterdir()), [])

    def test_write_fault_removes_file_scan_lines_kept(self):
        spec = importlib.util.spec_from_file_location("_rf0250_8_scan", TOOL)
        spec.loader.exec_module(tool := importlib.util.module_from_spec(spec))
        d, buf = self.edge1(), io.StringIO()
        with mock.patch.object(tool.os, "write", side_effect=OSError(errno.ENOSPC, "No space left on device")), \
                contextlib.redirect_stdout(buf):
            code = tool.main([str(d), "--json-out", str(self.out)])
        self.assertEqual(code, 1)
        self.assertFalse(os.path.lexists(self.out))
        self.assertEqual(buf.getvalue().splitlines(), [
            f"{d}/story.md:1: email", f"{d}/story.md:1: phone", f"{d}/story.md:2: plate",
            "files=2 scanned=2 skipped=0 findings=3 allowed=0", f"{self.out}: not written"])

    def test_mode_0600_and_never_overwritten(self):
        d = self.edge1()
        old = os.umask(0o022)
        try:
            self.assertEqual(self.run_scan(d, "--json-out", self.out)[0], 3)
        finally:
            os.umask(old)
        self.assertEqual(stat.S_IMODE(os.lstat(self.out).st_mode), 0o600)
        first = self.out.read_bytes()
        self.put("d/story.md", "now clean\n")
        code, out, err = self.run_scan(d, "--json-out", self.out)
        self.assertEqual((code, out), (2, ""))
        self.assertIn("never overwritten", err)
        self.assertEqual(self.out.read_bytes(), first)

    def test_allowed_counted_only_allow_values_never_written(self):
        d = self.tmp / "d"
        self.put("d/req.md", f"To: Records@City.Example.GOV\nFax {PHONE}\n")
        allow = self.put("private/office-allow.txt", "# office numbers\nrecords@city.example.gov\n")
        code, out, err = self.run_scan(d, "--allow", allow, "--json-out", self.out)
        self.assertEqual((code, err), (3, ""))
        raw, doc = self.receipt()
        self.assertEqual(doc["files"][0]["findings"], [{"line": 2, "category": "phone"}])
        self.assertEqual(doc["totals"], {"files": 1, "scanned": 1, "skipped": 0, "findings": 1, "allowed": 1, "failed": False})
        self.assertTrue(doc["allow_file_used"])
        for value in ("records@", "Records@", "office", str(allow), "555"):
            self.assertNotIn(value.encode(), raw)


if __name__ == "__main__":
    unittest.main()
