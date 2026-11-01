"""RF1720-5: tools/flock_privacy_scan.py runs the shared email/phone/plate patterns over private drafts and prints
path:line: category only (never the matched text), writes nothing, and exits 3 on findings. Synthetic fixtures only."""
import os, subprocess, sys, tempfile, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools" / "flock_privacy_scan.py"
EMAIL, PHONE, PLATE = "clerk@city.example.gov", "(805) 555-1234", "7ABC123"
SENTINEL = "SENTINEL-RF1720-5-q9z"


class PrivacyScanTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.cwd = self.tmp / "cwd"
        self.cwd.mkdir()

    def put(self, rel, content):
        path = self.tmp / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
        return path

    def run_scan(self, *args):
        done = subprocess.run([sys.executable, "-B", str(TOOL), *map(str, args)], cwd=self.cwd, capture_output=True,
                              text=True, check=False, timeout=60)
        return done.returncode, done.stdout, done.stderr

    def lines(self, *args):
        code, out, err = self.run_scan(*args)
        self.assertEqual(err, "")
        return code, out.splitlines()

    def test_clean_folder(self):
        d = self.put("drafts/EMAIL-DRAFT.md", "Dear records officer,\nPlease send the retention policy.\n").parent
        self.assertEqual(self.lines(d), (0, [f"files=1 scanned=1 skipped=0 findings=0 allowed=0"]))

    def test_three_categories_three_lines_never_echoed(self):
        f = self.put("d/story.md", f"intro\nwrite {EMAIL} {SENTINEL}\ncall {PHONE} {SENTINEL}\nplate {PLATE} {SENTINEL}\n")
        code, out, err = self.run_scan(f.parent)
        self.assertEqual(code, 3)
        name = f"{f.parent}/story.md"
        self.assertEqual(out.splitlines(), [f"{name}:2: email", f"{name}:3: phone", f"{name}:4: plate",
                                            "files=1 scanned=1 skipped=0 findings=3 allowed=0"])
        for secret in (EMAIL, PHONE, PLATE, "555", "7ABC", "clerk", SENTINEL):
            self.assertNotIn(secret, out + err)

    def test_two_categories_one_line_in_category_order(self):
        f = self.put("one.txt", f"{PLATE} then {EMAIL} then {PHONE} and other@x.example.org\n")
        self.assertEqual(self.lines(f), (3, [f"{f}:1: email", f"{f}:1: phone", f"{f}:1: plate",
                                             "files=1 scanned=1 skipped=0 findings=3 allowed=0"]))
        f2 = self.put("two.txt", f"x\n{PHONE} {EMAIL}\n")
        self.assertEqual(self.lines(f2)[1][:2], [f"{f2}:2: email", f"{f2}:2: phone"])

    def test_bare_ten_digits_and_lookalikes_not_findings(self):
        f = self.put("ids.md", "Record 8055551234 filed 2026-10-31.\nuser@localhost and @handle; 7abc123 ABC1234\n")
        self.assertEqual(self.lines(f), (0, ["files=1 scanned=1 skipped=0 findings=0 allowed=0"]))

    def test_allow_case_insensitive_email_counted_not_printed(self):
        f = self.put("d/req.md", "To: Records@City.Example.GOV\nCc: someone@else.example.org\nFax (805) 555-1234\n")
        allow = self.put("allow.txt", "# office addresses\n\nrecords@city.example.gov\n(805) 555-1234\n")
        code, out, err = self.run_scan(f.parent, "--allow", allow)
        self.assertEqual((code, err), (3, ""))
        self.assertEqual(out.splitlines(), [f"{f.parent}/req.md:2: email", "files=1 scanned=1 skipped=0 findings=1 allowed=2"])
        self.assertNotIn("office", out)
        self.assertNotIn("records@", out.lower())
        # phones and plates match exactly; case only folds for emails
        self.put("p.md", "7ABC123\n")
        allow2 = self.put("allow2.txt", "7abc123\n")
        self.assertEqual(self.lines(self.tmp / "p.md", "--allow", allow2)[0], 3)
        # the same line with an allowed and a not-allowed email is still one finding
        g = self.put("g.md", "records@city.example.gov, someone@else.example.org\n")
        self.assertEqual(self.lines(g, "--allow", allow), (3, [f"{g}:1: email", "files=1 scanned=1 skipped=0 findings=1 allowed=1"]))

    def test_allow_file_inside_folder_not_scanned_and_unreadable_allow(self):
        d = self.tmp / "d"
        allow = self.put("d/ALLOW.txt", f"{EMAIL}\n")
        self.put("d/a.md", f"{EMAIL}\n")
        self.assertEqual(self.lines(d, "--allow", allow),
                         (0, [f"{d}/ALLOW.txt: skipped (allow)", "files=2 scanned=1 skipped=1 findings=0 allowed=1"]))
        self.assertEqual(self.lines(d, "--allow", self.tmp / "nope.txt"), (1, [f"{self.tmp}/nope.txt: unreadable (allow)"]))

    def test_symlinked_file_and_folder_never_followed(self):
        target = self.put("outside/secret.md", f"{EMAIL}\n")
        d = self.tmp / "d"
        self.put("d/clean.md", "nothing here\n")
        os.symlink(target, d / "link.md")
        os.symlink(target.parent, d / "linkdir")
        before = os.stat(target).st_atime_ns
        self.assertEqual(self.lines(d), (0, [f"{d}/link.md: skipped (link)",
                                             f"{d}/linkdir: skipped (link)", "files=3 scanned=1 skipped=2 findings=0 allowed=0"]))
        self.assertEqual(self.lines(d / "link.md")[1], [f"{d}/link.md: skipped (link)", "files=1 scanned=0 skipped=1 findings=0 allowed=0"])
        self.assertEqual(self.lines(d / "linkdir")[1][0], f"{d}/linkdir: skipped (link)")
        self.assertEqual(os.stat(target).st_atime_ns, before)

    def test_binary_non_utf8_and_size(self):
        d = self.tmp / "d"
        self.put("d/a.png", b"\x89PNG\r\n\x1a\n\0\0\0\rIHDR " + EMAIL.encode())
        self.put("d/b.txt", f"ok {EMAIL}\n".encode() + b"\0")
        self.put("d/c.txt", f"caf\xe9 {EMAIL}\n".encode("latin-1"))
        self.put("d/d.md", f"{EMAIL}\n" + "x" * 100)
        self.put("d/e.md", f"{EMAIL}\n")
        self.assertEqual(self.lines(d, "--max-bytes", 50), (3, [
            f"{d}/a.png: skipped (binary)", f"{d}/b.txt: skipped (binary)", f"{d}/c.txt: skipped (binary)",
            f"{d}/d.md: skipped (size)", f"{d}/e.md:1: email", "files=5 scanned=1 skipped=4 findings=1 allowed=0"]))
        self.assertEqual(self.lines(d / "d.md")[1], [f"{d}/d.md:1: email", "files=1 scanned=1 skipped=0 findings=1 allowed=0"])

    def test_missing_path_among_valid_still_scans_exit_1(self):
        a = self.put("a.md", f"{EMAIL}\n")
        b = self.put("b.md", "clean\n")
        code, out, err = self.run_scan(a, self.tmp / "gone", b)
        self.assertEqual((code, err), (1, ""))
        self.assertEqual(out.splitlines(), [f"{a}:1: email", f"{self.tmp}/gone: missing",
                                            "files=2 scanned=2 skipped=0 findings=1 allowed=0"])
        self.assertNotIn("Traceback", out + err)
        self.assertEqual(self.lines(self.tmp / "gone"), (1, [f"{self.tmp}/gone: missing", "files=0 scanned=0 skipped=0 findings=0 allowed=0"]))

    def test_hidden_folders_skipped_silently(self):
        d = self.tmp / "d"
        self.put("d/.git/config", f"{EMAIL}\n")
        self.put("d/.venv/x.py", f"{PHONE}\n")
        self.put("d/sub/.hidden/y.md", f"{PLATE}\n")
        self.put("d/sub/z.md", "clean\n")
        self.assertEqual(self.lines(d), (0, ["files=1 scanned=1 skipped=0 findings=0 allowed=0"]))

    def test_read_only_no_new_file(self):
        d = self.tmp / "d"
        self.put("d/a.md", f"{EMAIL}\n")
        self.put("d/sub/b.md", f"{PHONE}\r\n")
        self.put("d/c.bin", b"\0\1")
        allow = self.put("allow.txt", f"{EMAIL}\n")

        def snapshot():
            return sorted((str(p), p.read_bytes() if p.is_file() else b"", os.lstat(p).st_mtime_ns) for p in self.tmp.rglob("*"))

        before = snapshot()
        pycache = (ROOT / "tools" / "__pycache__").exists()
        self.assertEqual(self.lines(d, "--allow", allow)[0], 3)
        self.assertEqual(self.lines(d)[0], 3)
        self.assertEqual(snapshot(), before)
        self.assertEqual(list(self.cwd.iterdir()), [])
        self.assertEqual((ROOT / "tools" / "__pycache__").exists(), pycache)

    def test_deterministic_order_and_crlf_line_numbers(self):
        d = self.tmp / "d"
        self.put("d/b.md", f"x\r\ny\r\n{EMAIL}\r\n")
        self.put("d/a/z.md", f"{PLATE}\n")
        self.put("d/a.md", f"\r\n{PHONE}\r\n")
        self.put("d/c.md", f"one\rtwo\x0cthree\u2028four\n{PLATE}\n")
        first, second = self.lines(d), self.lines(d)
        self.assertEqual(first, second)
        self.assertEqual(first, (3, [f"{d}/a/z.md:1: plate", f"{d}/a.md:2: phone", f"{d}/b.md:3: email", f"{d}/c.md:2: plate",
                                     "files=4 scanned=4 skipped=0 findings=4 allowed=0"]))

    def test_usage_and_help(self):
        code, out, err = self.run_scan()
        self.assertEqual((code, out), (2, ""))
        self.assertIn("usage: flock_privacy_scan.py", err)
        self.assertEqual(self.run_scan(self.tmp, "--max-bytes", "-1")[0], 2)
        code, out, err = self.run_scan("--help")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("never prints the matched text", out.replace("\n", " "))


if __name__ == "__main__":
    unittest.main()
