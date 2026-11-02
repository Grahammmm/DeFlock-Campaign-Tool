#!/usr/bin/env python3
"""Flock privacy scan: run the shared email/phone/plate patterns (tools/flock_privacy.py) over private drafts before handoff.
  python3 tools/flock_privacy_scan.py PATH... [--allow FILE] [--max-bytes N] [--json-out NEWFILE]
PATH is a file or a folder (recursed in sorted, depth-first order; hidden folders such as .git/.venv are skipped silently). It prints
one `path:line: category` line per (file, line, category), then `files=N scanned=N skipped=N findings=N allowed=N`. Paths are the
PATH argument joined with the path inside it. It never prints the matched text, never echoes the allow file and writes nothing
unless --json-out is given.
--json-out NEWFILE: after the scan, write a flock-privacy-scan-v1 handoff receipt (0600, created new, never overwritten; absolute,
normalized, no symlink on its folder, outside the repository and every scanned PATH): each file's outcome, the sha256 of the scanned
bytes and finding {line, category} pairs, plus totals; never matched text or the allow file. Use it for the handoff receipt and re-scan
if any sha256 differs. A write error removes the file, prints `NEWFILE: not written` and exits 1.
  skipped (link)    a symlink (file or folder) is never followed or read
  skipped (size)    over --max-bytes (default 2000000)
  skipped (binary)  not UTF-8 text, or a NUL byte in the first 8 KiB
  skipped (special) not a regular file;  skipped (allow): the --allow file itself
--allow FILE: one exact value per line (`#` comment lines); emails match case-insensitively. Allowed matches are only counted.
Exit 0 no findings, 3 findings, 1 any missing PATH or unreadable file (findings still printed), 2 usage."""
import argparse, contextlib, hashlib, importlib.util, json, os, stat, sys
from pathlib import Path
sys.dont_write_bytecode = True  # read-only: loading the sibling must not leave a __pycache__ behind
_spec = importlib.util.spec_from_file_location("_scan_flock_privacy", Path(__file__).resolve().parent / "flock_privacy.py")
_spec.loader.exec_module(privacy := importlib.util.module_from_spec(_spec))
SNIFF, NOFOLLOW = 8192, getattr(os, "O_NOFOLLOW", 0)
REPO = os.path.realpath(Path(__file__).resolve().parents[1])

def show(path):
    return os.fsencode(path).decode("utf-8", "replace")

class Scan:
    def __init__(self, allow, max_bytes, allow_id):
        self.allow, self.allow_lower = set(allow), {v.lower() for v in allow}
        self.max_bytes, self.allow_id, self.failed = max_bytes, allow_id, False
        self.files = self.scanned = self.skipped = self.findings = self.allowed = 0
        self.records = []  # one per file in visit order, for --json-out; never the matched text

    def note(self, name, outcome, reason=None, sha=None):
        self.records.append({"path": show(name), "outcome": outcome, "reason": reason, "sha256": sha, "findings": []})

    def skip(self, name, why):
        self.skipped += 1
        self.note(name, "skipped", why)
        print(f"{show(name)}: skipped ({why})")

    def fail(self, name, why):
        self.failed = True
        self.note(name, why)
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
        self.note(name, "scanned", sha=hashlib.sha256(data).hexdigest())
        for number, line in enumerate(text.split("\n"), 1):
            for category, rx in privacy.PATTERNS:
                hits = [m[0] for m in rx.finditer(line)]
                kept = [h for h in hits if not (h in self.allow or (category == "email" and h.lower() in self.allow_lower))]
                self.allowed += len(hits) - len(kept)
                if kept:
                    self.findings += 1
                    self.records[-1]["findings"].append({"line": number, "category": category})
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

def out_problem(target, paths):  # --json-out rules, checked before any scan; the message names the rule, never the path
    parent, folders = os.path.dirname(target), [r if os.path.isdir(r) else os.path.dirname(r) for r in map(os.path.realpath, paths)]
    inside = lambda ds: any(target == d or target.startswith(d.rstrip(os.sep) + os.sep) for d in ds)
    rules = ((not os.path.isabs(target), "be an absolute path"), (os.path.normpath(target) != target, "be a normalized path"),
             (os.path.lexists(target), "not exist (never overwritten)"), (os.path.realpath(parent) != parent, "have no symlink on its folder"),
             (not os.path.isdir(parent), "be in an existing folder"), (inside([REPO]), "be outside the repository"),
             (inside(folders), "be outside every scanned PATH"))
    return next((f"--json-out must {rule}" for bad, rule in rules if bad), None)

def write_receipt(target, doc):  # created new and 0600; any write error removes the file
    data = memoryview((json.dumps(doc, sort_keys=True, indent=2, ensure_ascii=True) + "\n").encode("ascii"))
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW, 0o600)
    except OSError:
        return print(f"{show(target)}: not written")
    try:
        while data:
            data = data[os.write(fd, data):]
        os.close(fd)
        return True
    except OSError:
        with contextlib.suppress(OSError):
            os.close(fd)
        os.unlink(target)
        print(f"{show(target)}: not written")

def main(argv=None):
    ap = argparse.ArgumentParser(prog="flock_privacy_scan.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="+", metavar="PATH")
    ap.add_argument("--allow", metavar="FILE")
    ap.add_argument("--max-bytes", type=int, default=2_000_000, metavar="N")
    ap.add_argument("--json-out", metavar="NEWFILE")
    args = ap.parse_args(argv)
    if args.max_bytes < 0:
        ap.error("--max-bytes must be 0 or more")
    if args.json_out is not None and (problem := out_problem(args.json_out, args.paths)):
        ap.error(problem)
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
    if args.json_out is not None and not write_receipt(args.json_out, {
            "schema": "flock-privacy-scan-v1", "review_required": True, "note": "patterns only; a clean scan is not a privacy review",
            "max_bytes": args.max_bytes, "allow_file_used": bool(args.allow), "files": scan.records,
            "totals": {"files": scan.files, "scanned": scan.scanned, "skipped": scan.skipped, "findings": scan.findings,
                       "allowed": scan.allowed, "failed": scan.failed}}):
        return 1
    return 1 if scan.failed else 3 if scan.findings else 0

if __name__ == "__main__":
    sys.exit(main())
