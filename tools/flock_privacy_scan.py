#!/usr/bin/env python3
"""Flock privacy scan: run the shared email/phone/plate patterns (tools/flock_privacy.py) over private drafts before handoff.
  python3 tools/flock_privacy_scan.py PATH... [--allow FILE] [--max-bytes N]
PATH is a file or a folder (recursed in sorted, depth-first order; hidden folders such as .git/.venv are skipped silently). It prints
one `path:line: category` line per (file, line, category), then `files=N scanned=N skipped=N findings=N allowed=N`. Paths are the
PATH argument joined with the path inside it. It never prints the matched text, never echoes the allow file and writes nothing.
  skipped (link)    a symlink (file or folder) is never followed or read
  skipped (size)    over --max-bytes (default 2000000)
  skipped (binary)  not UTF-8 text, or a NUL byte in the first 8 KiB
  skipped (special) not a regular file;  skipped (allow): the --allow file itself
--allow FILE: one exact value per line (`#` comment lines); emails match case-insensitively. Allowed matches are only counted.
Exit 0 no findings, 3 findings, 1 any missing PATH or unreadable file (findings still printed), 2 usage."""
import argparse, importlib.util, os, stat, sys
from pathlib import Path
sys.dont_write_bytecode = True  # read-only: loading the sibling must not leave a __pycache__ behind
_spec = importlib.util.spec_from_file_location("_scan_flock_privacy", Path(__file__).resolve().parent / "flock_privacy.py")
_spec.loader.exec_module(privacy := importlib.util.module_from_spec(_spec))
SNIFF, NOFOLLOW = 8192, getattr(os, "O_NOFOLLOW", 0)

def show(path):
    return os.fsencode(path).decode("utf-8", "replace")

class Scan:
    def __init__(self, allow, max_bytes, allow_id):
        self.allow, self.allow_lower = set(allow), {v.lower() for v in allow}
        self.max_bytes, self.allow_id, self.failed = max_bytes, allow_id, False
        self.files = self.scanned = self.skipped = self.findings = self.allowed = 0

    def skip(self, name, why):
        self.skipped += 1
        print(f"{show(name)}: skipped ({why})")

    def fail(self, name, why):
        self.failed = True
        print(f"{show(name)}: {why}")

    def entry(self, path, name, st):  # path opens it, name is what is printed
        self.files += 1
        why = ("link" if stat.S_ISLNK(st.st_mode) else "special" if not stat.S_ISREG(st.st_mode)
               else "allow" if (st.st_dev, st.st_ino) == self.allow_id else "size" if st.st_size > self.max_bytes else None)
        if why:
            return self.skip(name, why)
        try:
            with open(path, "rb", opener=lambda p, f: os.open(p, f | NOFOLLOW)) as fh:
                data = fh.read(self.max_bytes + 1)
        except OSError:
            return self.fail(name, "unreadable")
        if len(data) > self.max_bytes:
            return self.skip(name, "size")
        try:
            text = None if b"\0" in data[:SNIFF] else data.decode("utf-8")
        except UnicodeError:
            text = None
        if text is None:
            return self.skip(name, "binary")
        self.scanned += 1
        for number, line in enumerate(text.split("\n"), 1):
            for category, rx in privacy.PATTERNS:
                hits = [m[0] for m in rx.finditer(line)]
                kept = [h for h in hits if not (h in self.allow or (category == "email" and h.lower() in self.allow_lower))]
                self.allowed += len(hits) - len(kept)
                if kept:
                    self.findings += 1
                    print(f"{show(name)}:{number}: {category}")

    def folder(self, path, name):
        try:
            entries = sorted(os.scandir(path), key=lambda e: e.name)
        except OSError:
            return self.fail(name, "unreadable")
        for e in entries:
            child, st = os.path.join(name, e.name), e.stat(follow_symlinks=False)
            if not stat.S_ISDIR(st.st_mode):
                self.entry(e.path, child, st)
            elif not e.name.startswith("."):
                self.folder(e.path, child)

    def path(self, arg):
        try:
            st = os.lstat(arg)
        except OSError as exc:
            return self.fail(arg, "missing" if isinstance(exc, FileNotFoundError) else "unreadable")
        return self.folder(arg, arg) if stat.S_ISDIR(st.st_mode) else self.entry(arg, arg, st)

def main(argv=None):
    ap = argparse.ArgumentParser(prog="flock_privacy_scan.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="+", metavar="PATH")
    ap.add_argument("--allow", metavar="FILE")
    ap.add_argument("--max-bytes", type=int, default=2_000_000, metavar="N")
    args = ap.parse_args(argv)
    if args.max_bytes < 0:
        ap.error("--max-bytes must be 0 or more")
    allow, allow_id = [], None
    if args.allow:
        try:
            st = os.stat(args.allow)
            with open(args.allow, "rb") as fh:
                allow = [v.strip() for v in fh.read().decode("utf-8").splitlines() if v.strip() and not v.strip().startswith("#")]
        except (OSError, UnicodeError):
            print(f"{show(args.allow)}: unreadable (allow)")
            return 1
        allow_id = (st.st_dev, st.st_ino)
    scan = Scan(allow, args.max_bytes, allow_id)
    for arg in args.paths:
        scan.path(arg)
    print(f"files={scan.files} scanned={scan.scanned} skipped={scan.skipped} findings={scan.findings} allowed={scan.allowed}")
    return 1 if scan.failed else 3 if scan.findings else 0

if __name__ == "__main__":
    sys.exit(main())
