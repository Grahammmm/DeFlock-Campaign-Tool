"""Small, retained-evidence launcher for the offline FA1705 qualification."""
import sys
sys.dont_write_bytecode = True

import argparse
from contextlib import redirect_stdout, redirect_stderr
import hashlib
import io
import json
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
import tempfile
import traceback
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
NEW_TEST = "tests/records/test_fa1705_ocr_cache_axes.py"
HELPER = "tools/accept_fa1705.py"
MODULE = "tests.records.test_fa1705_ocr_cache_axes.CacheAxesTests"
BEHAVIOR = [MODULE + ".test_axes_replay_and_history"]
CONTROL = [MODULE + ".test_language_projection_oracle"]
COMPATIBILITY = ["tests.records.test_ocr.OCRTests." + method for method in (
    "test_low_confidence_receipt_index_and_idempotent_rerun",
    "test_high_confidence_remains_unreviewed")]
SOURCES = ["campaign_tool/records/extract/ocr.py", "campaign_tool/records/pdf_eligibility.py",
           "tests/records/test_ocr.py", "tools/check_secret_report.py",
           "tools/check_public_tree.py", "campaign_tool/records/public_scan.py"]


def require(condition, reason):
    if not condition:
        raise RuntimeError(reason)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def safe_directory(path):
    path = Path(os.path.abspath(path))
    for component in reversed((path, *path.parents)):
        require(not component.is_symlink(), "symlink ancestry rejected: " + str(component))
        require(not component.exists() or component.is_dir(), "nondirectory ancestry rejected")
    return path


class Launcher:
    def __init__(self, case, output):
        parent = safe_directory(output)
        require(parent.is_relative_to(REPO.parent / "out"), "output must be under candidate out")
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.run = Path(tempfile.mkdtemp(prefix="run-", dir=parent))
        self.tmp = self.run / "tmp"
        self.tmp.mkdir(mode=0o700)
        self.receipt = dict(case=case, argv=sys.argv, cwd=str(Path.cwd()),
                            interpreter=sys.executable, commands=[], suites=[],
                            status="RUNNING_REVIEW_REQUIRED", exit=None)
        self.sequence = 0
        self.seal()

    def seal(self):
        files = {}
        for directory, children, names in os.walk(self.run):
            children[:] = [name for name in children if name != "scan-checkout"]
            for name in names:
                path = Path(directory) / name
                if path != self.run / "receipt.json":
                    files[str(path.relative_to(self.run))] = sha(path.read_bytes())
        self.receipt["output_sha256"] = files
        (self.run / "receipt.json").write_text(json.dumps(self.receipt, indent=2, sort_keys=True) + "\n")

    def command(self, argv, cwd=REPO):
        self.sequence += 1
        stem = f"command-{self.sequence:02d}"
        result = subprocess.run([str(a) for a in argv], cwd=cwd, capture_output=True,
                                timeout=180, check=False)
        (self.run / (stem + ".stdout")).write_bytes(result.stdout)
        (self.run / (stem + ".stderr")).write_bytes(result.stderr)
        self.receipt["commands"].append(dict(argv=[str(a) for a in argv], cwd=str(cwd),
            exit=result.returncode, stdout=stem + ".stdout", stderr=stem + ".stderr"))
        self.seal()
        require(result.returncode == 0, f"child failed: {argv}; exit={result.returncode}")
        return result.stdout

    def git(self, *args, cwd=REPO):
        return self.command(["git", *args], cwd=cwd).decode().strip()

    def identities(self):
        manifest_path = REPO.parent / "SOURCE-MANIFEST.json"
        require(not manifest_path.is_symlink(), "manifest symlink rejected")
        self.manifest = json.loads(manifest_path.read_bytes())
        self.base = self.manifest["head"]
        require(bool(re.fullmatch(r"[a-f0-9]{40}", self.base)), "invalid base identity")
        self.head = self.git("rev-parse", "HEAD")
        self.tree = self.git("rev-parse", "HEAD^{tree}")
        hashes = {}
        for path in SOURCES:
            baseline = self.command(["git", "show", self.base + ":" + path])
            require((REPO / path).read_bytes() == baseline, "protected source drift: " + path)
            hashes[path] = dict(sha256=sha(baseline), git_blob=self.git("rev-parse", self.base + ":" + path))
        for path in (NEW_TEST, HELPER):
            hashes[path] = dict(sha256=sha((REPO / path).read_bytes()))
        self.receipt.update(head=self.head, tree=self.tree, base=self.base,
                            source_manifest_sha256=sha(manifest_path.read_bytes()), files=hashes)
        self.seal()

    def suite(self, label, selectors):
        # Set inherited temp storage before importing ANY application/test module.
        temporary = self.tmp / label
        temporary.mkdir(mode=0o700)
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, {"TMPDIR": str(temporary)}), patch.object(tempfile, "tempdir", str(temporary)):
            before = (dict(os.environ), list(sys.path), tempfile.tempdir,
                      subprocess.Popen, socket.socket.connect, socket.create_connection)
            from tests.records.test_fa1705_ocr_cache_axes import offline_runtime
            with redirect_stdout(stdout), redirect_stderr(stderr), offline_runtime():
                suite = unittest.defaultTestLoader.loadTestsFromNames(selectors)
                count = suite.countTestCases()
                result = unittest.TextTestRunner(stream=stderr, verbosity=2).run(suite)
            after = (dict(os.environ), list(sys.path), tempfile.tempdir,
                     subprocess.Popen, socket.socket.connect, socket.create_connection)
            require(before == after, "suite poisoned ambient state")
            from campaign_tool.records.extract import ocr
            require(ocr.extract_image_only_pages.__kwdefaults__["runner"] is subprocess.run,
                    "import poisoned bound runner default")
        (self.run / (label + ".stdout")).write_text(stdout.getvalue())
        (self.run / (label + ".stderr")).write_text(stderr.getvalue())
        row = dict(label=label, selected_test_ids=selectors, loaded=count,
                   executed=result.testsRun, skips=len(result.skipped),
                   errors=len(result.errors), failures=len(result.failures),
                   expected_failures=len(result.expectedFailures),
                   unexpected_successes=len(result.unexpectedSuccesses),
                   stdout=label + ".stdout", stderr=label + ".stderr")
        self.receipt["suites"].append(row)
        self.seal()
        require(count == len(selectors) == result.testsRun and count > 0, "missing/zero test selection")
        require(result.wasSuccessful() and not result.skipped and not result.expectedFailures
                and not result.unexpectedSuccesses, "unexpected suite outcome")
        if selectors == CONTROL:
            require("EXPECTED test-local omitted-language assertion failure:" in stdout.getvalue()
                    and "cache identity collision" in stdout.getvalue(), "control evidence missing")
        return temporary

    def portable(self, label):
        temporary = self.tmp / label
        temporary.mkdir(mode=0o700)
        # Explicitly required ordinary unittest loading; no interpreter source.
        with patch.dict(os.environ, {"TMPDIR": str(temporary), "PYTHONDONTWRITEBYTECODE": "1"}):
            self.command([sys.executable, "-B", REPO / HELPER, "--portable-unittest", *BEHAVIOR, *COMPATIBILITY])
        last = self.receipt["commands"][-1]
        raw = (self.run / last["stderr"]).read_text()
        require(re.search(r"Ran 3 tests in", raw) is not None
                and raw.rstrip().endswith("OK") and "skipped" not in raw, "portable test counts not verified")
        self.receipt["suites"].append(dict(label=label, selected_test_ids=BEHAVIOR + COMPATIBILITY,
            executed=3, skips=0, errors=0, failures=0, command_index=len(self.receipt["commands"]) - 1))
        self.seal()

    def scope(self, clean=False):
        changed = self.git("diff", "--name-only", self.base, "HEAD").splitlines()
        status = self.git("status", "--porcelain=v1", "--untracked-files=all")
        self.receipt["scope"] = dict(changed=changed, status=status)
        require(all(p in (NEW_TEST, HELPER) or p.startswith("tests/fixtures/fa1705/") for p in changed),
                "changed path outside admitted scope")
        if clean:
            require(not status, "scan requires clean committed candidate")
            require(NEW_TEST in changed and HELPER in changed, "new files are not committed")
        for path in (NEW_TEST, HELPER):
            raw = (REPO / path).read_bytes()
            require(re.search(rb"(?<![0-9a-fA-F])(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})(?![0-9a-fA-F])", raw) is None,
                    "committed code contains a hash literal")
            require(b"/home" + b"/" not in raw and b"/Users" + b"/" not in raw, "box-only path in committed code")
        self.seal()

    def clone(self):
        self.scope(clean=True)
        checkout = self.run / "scan-checkout"
        self.command(["git", "clone", "--no-hardlinks", "--no-checkout", "--", REPO, checkout])
        self.git("checkout", "--detach", self.head, cwd=checkout)
        require(self.git("rev-parse", "HEAD", cwd=checkout) == self.head, "clone head mismatch")
        require(self.git("rev-parse", "HEAD^{tree}", cwd=checkout) == self.tree, "clone tree mismatch")
        source_paths = self.command(["git", "ls-files", "-z"])
        clone_paths = self.command(["git", "ls-files", "-z"], cwd=checkout)
        require(clone_paths == source_paths, "incomplete clone tracked path set")
        entries = self.command(["git", "ls-tree", "-rz", "--full-tree", self.head], cwd=checkout)
        count = 0
        for entry in entries.split(b"\0"):
            if not entry:
                continue
            meta, relative = entry.split(b"\t", 1)
            mode, kind, identity = meta.split()
            require(kind == b"blob" and mode in (b"100644", b"100755"), "nonregular tracked artifact")
            name = relative.decode()
            require(not Path(name).is_absolute() and ".." not in Path(name).parts, "unsafe tracked name")
            for root in (REPO, checkout):
                safe_directory((root / name).parent)
                target = root / name
                require(not target.is_symlink() and stat.S_ISREG(target.stat().st_mode), "tracked symlink/nonfile")
                content = target.read_bytes()
                blob = hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest().encode()
                require(blob == identity, "tracked byte identity mismatch: " + name)
            count += 1
        require(count == len([p for p in clone_paths.split(b"\0") if p]), "tree/path count mismatch")
        require(not self.git("status", "--porcelain", "--untracked-files=all", cwd=checkout), "dirty scan clone")
        # No added runtime link or symlink artifacts, including in Git metadata.
        require(not any(p.is_symlink() for p in checkout.rglob("*")), "symlink in complete scan checkout")
        self.receipt["scan_checkout"] = dict(path=str(checkout), head=self.head, tree=self.tree,
            tracked_files=count, tracked_paths_sha256=sha(clone_paths), tree_entries_sha256=sha(entries),
            complete_bytes_verified=True)
        self.seal()
        return checkout

    def execute(self, case):
        self.identities()
        self.scope()
        if case == "behavior":
            self.suite("behavior", BEHAVIOR)
        elif case == "compatibility":
            self.suite("compatibility", COMPATIBILITY)
        elif case == "negative-control":
            self.suite("negative-control", CONTROL)
            self.suite("positive-restored", BEHAVIOR)
        elif case == "isolation":
            original = {p: (REPO / p).read_bytes() for p in SOURCES}
            self.portable("portable-before")
            first = self.suite("fixture-prefix-one", BEHAVIOR)
            second = self.suite("fixture-prefix-two", BEHAVIOR)
            first_roots = list(first.glob("fa1705-fixture-*"))
            second_roots = list(second.glob("fa1705-fixture-*"))
            require(len(first_roots) == len(second_roots) == 1 and first_roots[0] != second_roots[0],
                    "isolation prefixes not distinct")
            self.portable("portable-after")
            require(all((REPO / p).read_bytes() == raw for p, raw in original.items()), "source changed during isolation")
            self.receipt["isolation"] = dict(first=str(first_roots[0]), second=str(second_roots[0]),
                protected_source_unchanged=True, ambient_state_restored=True)
        elif case == "credential-scan":
            runtime = self.manifest["runtime"]
            interpreter = Path(runtime["credential_scan_python3"])
            scanner = Path(runtime["detect_secrets"])
            require(interpreter.is_file() and scanner.is_file(), "standard scanner runtime missing")
            checkout = self.clone()
            report = self.run / "detect-secrets.json"
            raw = self.command([interpreter, scanner, "scan", "--all-files", "--exclude-files", "^.git/", "--no-verify"],
                               cwd=checkout)
            report.write_bytes(raw)
            data = json.loads(raw)
            require(isinstance(data.get("results"), dict), "invalid actual scanner JSON")
            checked = self.command([interpreter, checkout / "tools/check_secret_report.py", report], cwd=checkout)
            require(b"potential secrets: 0;" in checked, "checker did not report zero unapproved potential secrets")
            self.receipt["credential_scan"] = dict(report_sha256=sha(raw), checker_stdout_sha256=sha(checked),
                potential_secrets=0, actual_report=True)
        elif case == "public-tree":
            checkout = self.clone()
            raw = self.command([REPO / ".venv/bin/python3", checkout / "tools/check_public_tree.py"], cwd=checkout)
            data = json.loads(raw)
            require(data.get("findings") == [] and data.get("scanned_files") == self.receipt["scan_checkout"]["tracked_files"],
                    "public scanner incomplete or violations present")
            self.receipt["public_tree"] = data
        self.scope(clean=case in ("credential-scan", "public-tree"))
        require(self.git("rev-parse", "HEAD") == self.head, "head changed during acceptance")
        for path, identity in self.receipt["files"].items():
            require(sha((REPO / path).read_bytes()) == identity["sha256"], "source/test changed during acceptance")
        self.receipt.update(status="PASS_REVIEW_REQUIRED", exit=0)
        self.seal()


def main():
    if sys.argv[1:2] == ["--portable-unittest"]:
        # Named child launcher, then the standard -m unittest module path under
        # scoped pre-import guards. No operator manifest needed for test loading.
        require(Path.cwd() == REPO, "portable cwd must be candidate repo")
        selectors = sys.argv[2:]
        require(selectors == BEHAVIOR + COMPATIBILITY, "unexpected portable selectors")
        sys.path.insert(0, str(REPO))
        from tests.records.test_fa1705_ocr_cache_axes import offline_runtime
        import runpy
        sys.argv = ["unittest", "-v", *selectors]
        with offline_runtime():
            runpy.run_module("unittest", run_name="__main__")
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", required=True, choices=("behavior", "compatibility", "negative-control", "isolation",
                                                         "credential-scan", "public-tree"))
    parser.add_argument("--output-root", required=True, type=Path)
    args = parser.parse_args()
    require(Path.cwd() == REPO, "acceptance cwd must be candidate repo")
    launcher = Launcher(args.case, args.output_root)
    sys.path.insert(0, str(REPO))
    try:
        with patch.dict(os.environ, {"TMPDIR": str(launcher.tmp), "PYTHONDONTWRITEBYTECODE": "1"}), \
                patch.object(tempfile, "tempdir", str(launcher.tmp)):
            launcher.execute(args.case)
    except Exception:
        (launcher.run / "launcher-error.txt").write_text(traceback.format_exc())
        launcher.receipt.update(status="FAIL_HELD", exit=1)
        launcher.seal()
        print(traceback.format_exc(), file=sys.stderr)
        print(str(launcher.run / "receipt.json"))
        return 1
    print(str(launcher.run / "receipt.json"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
