#!/usr/bin/env python3
"""Create a new, empty private flock store an organizer can fill: STORE/LAW.csv and, per agency, agencies/<slug>/FACTS.csv
(schema v2) and POLICY.csv, header rows only. Never writes a fact, policy, law or request row.
  python3 tools/flock_store_init.py STORE --agency SLUG [--agency SLUG ...]
  python3 tools/flock_store_init.py add STORE --agency SLUG [...]   (and init STORE ..., the same as the bare form)
add: only new agencies/<slug> folders in an existing store; existing files keep their bytes and mode (sha256 checked).
All or nothing: every refusal is checked before any write; the tree is built in a sibling STORE.tmp-<pid> (0700) and
renamed to STORE. Folders 0700, files 0600. Prints slugs and counts, never the path.
Exit 0 created, 1 refused (nothing written), 2 usage."""
import argparse, contextlib, csv, hashlib, importlib.util, io, json, os, re, stat, sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SLUG = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")  # tools/flock_publish.py, tools/flock_region.py
MAX_SLUG = 64

def _tool(name):  # loaded by file path, never copied (tools/flock_store_check.py pattern)
    spec = importlib.util.spec_from_file_location("_store_init_" + name, REPO / "tools" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

facts_tool, law_policy = _tool("flock_facts"), _tool("flock_law_policy")

def slug_refusals(slugs):
    errs = []
    if not slugs:
        errs.append("no --agency given")
    seen = set()
    for s in slugs:
        if not (SLUG.fullmatch(s) and len(s) <= MAX_SLUG):
            errs.append("agency slug is not [a-z0-9]+(-[a-z0-9]+)* of at most 64 characters")
        elif s in seen:
            errs.append(f"agency {s} given twice")
        seen.add(s)
    return errs

def refusals(store_arg, slugs):
    errs = slug_refusals(slugs)
    raw = Path(os.path.abspath(store_arg))
    if raw.is_symlink() or raw.parent.is_symlink():
        errs.append("STORE or its parent is a symlink")
    real = raw.resolve()
    if real == REPO or REPO in real.parents:
        errs.append("STORE is inside the repository")
    if not raw.parent.is_dir():
        errs.append("STORE parent folder is missing")
    elif os.path.lexists(raw) and not (raw.is_dir() and not raw.is_symlink() and not any(raw.iterdir())):
        errs.append("STORE exists and is not an empty folder")
    return errs, raw

def write_csv(path, header, made):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    made.append(path)
    try:
        fh = open(fd, "w", encoding="utf-8", newline="")
    except BaseException:
        os.close(fd)
        raise
    with fh:
        csv.writer(fh, lineterminator="\n").writerow(header)

def build(tmp, slugs, made):
    os.mkdir(tmp, 0o700)
    made.append(tmp)
    write_csv(tmp / "LAW.csv", law_policy.LAW_HEADER, made)
    os.mkdir(tmp / "agencies", 0o700)
    made.append(tmp / "agencies")
    for s in slugs:
        os.mkdir(tmp / "agencies" / s, 0o700)
        made.append(tmp / "agencies" / s)
        write_csv(tmp / "agencies" / s / "FACTS.csv", facts_tool.V2, made)
        write_csv(tmp / "agencies" / s / "POLICY.csv", law_policy.POLICY_HEADER, made)

def remove(made):  # only what this run created, by exact name, deepest first; never a recursive delete
    for path in reversed(made):
        try:
            os.rmdir(path) if path.is_dir() and not path.is_symlink() else os.unlink(path)
        except OSError:
            pass

def add_refusals(store_arg, slugs):
    errs = slug_refusals(slugs)
    raw = Path(os.path.abspath(store_arg))
    if raw.is_symlink():
        errs.append("STORE is a symlink")
    elif (real := raw.resolve()) == REPO or REPO in real.parents:
        errs.append("STORE is inside the repository")
    elif not raw.is_dir():
        errs.append("STORE is not an existing folder")
    elif (raw / "agencies").is_symlink() or not (raw / "agencies").is_dir():
        errs.append("STORE/agencies is missing or a symlink")
    else:
        errs += [f"agency {s} already there" for s in dict.fromkeys(slugs)
                 if SLUG.fullmatch(s) and os.path.lexists(raw / "agencies" / s)]
    return errs, raw

def store_errors(store):  # flock_store_check BEFORE the change: exit 2 refuses, exit 1 errors are only counted
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        code = _tool("flock_store_check").main([str(store), "--json"])  # fixed, valid arguments: no usage exit
    return None if code not in (0, 1) else json.loads(out.getvalue())["totals"]["errors"]

def sha256(path):
    with open(path, "rb") as fh:
        return hashlib.file_digest(fh, "sha256").hexdigest()

def snapshot(root):  # every existing entry by path, walked without following links: mode and sha256 or link target
    seen, todo = {}, [root]
    while todo:
        for entry in os.scandir(todo.pop()):
            mode = entry.stat(follow_symlinks=False).st_mode
            seen[entry.path] = (mode, os.readlink(entry.path) if stat.S_ISLNK(mode) else
                                sha256(entry.path) if stat.S_ISREG(mode) else None)
            todo += [entry.path] if stat.S_ISDIR(mode) else []
    return seen

def add(store, slugs, made):
    for s in slugs:
        tmp, final = store / "agencies" / f".{s}.tmp-{os.getpid()}", store / "agencies" / s
        if os.path.lexists(tmp) or os.path.lexists(final):
            raise FileExistsError
        os.mkdir(tmp, 0o700)
        made.append(tmp)
        write_csv(tmp / "FACTS.csv", facts_tool.V2, made)
        write_csv(tmp / "POLICY.csv", law_policy.POLICY_HEADER, made)
        os.rename(tmp, final)
        made[-3:] = [final / p.relative_to(tmp) for p in made[-3:]]  # the folder and its 2 files, now renamed

def add_main(args):
    def refuse(lines):
        return print("".join(f"add refused | {e}\n" for e in [*lines, "nothing written"]), end="", file=sys.stderr) or 1
    try:
        errs, store = add_refusals(args.store, args.agency)
        if not errs and (errors := store_errors(store)) is None:
            errs.append("store check exits 2 (refusal or usage)")
        if errs:
            return refuse(errs)
        existing = sum(1 for _ in os.scandir(store / "agencies"))
        before = snapshot(store)
    except OSError as exc:  # the class only, never a path
        return refuse([f"io: {type(exc).__name__}"])
    made = []
    try:
        add(store, args.agency, made)
        after = snapshot(store)
    except Exception as exc:  # the class only, never a path
        remove(made)
        return refuse([f"io: {type(exc).__name__}"])
    if any(after.get(k) != v for k, v in before.items()):
        remove(made)
        return print("add: existing file changed", file=sys.stderr) or 1
    for s in args.agency:
        print(f"add | {s} | FACTS.csv POLICY.csv")
    print(f"add | agencies={len(args.agency)} added, {existing} already there | store check before: errors={errors}")
    return 0

def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    verb = argv.pop(0) if argv and argv[0] in ("init", "add") else "init"  # a bare STORE --agency ... is init
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("store")
    parser.add_argument("--agency", action="append", default=[], metavar="SLUG")
    args = parser.parse_args(argv)
    if verb == "add":
        return add_main(args)
    try:  # preflight stat/list errors (e.g. STORE statable but not listable) get the same path-free refusal
        errs, store = refusals(args.store, args.agency)
    except OSError as exc:  # the class only, never a path
        print(f"init refused | io: {type(exc).__name__}\ninit refused | nothing written", file=sys.stderr)
        return 1
    if errs:
        print("".join(f"init refused | {e}\n" for e in errs) + "init refused | nothing written", file=sys.stderr)
        return 1
    tmp, made = store.parent / f"{store.name}.tmp-{os.getpid()}", []
    try:
        if os.path.lexists(tmp):
            raise FileExistsError
        build(tmp, args.agency, made)
        os.rename(tmp, store)  # onto a missing or an empty folder (POSIX rename(2))
    except Exception as exc:  # the class only, never a path
        remove(made)
        print(f"init refused | io: {type(exc).__name__}\ninit refused | nothing written", file=sys.stderr)
        return 1
    for s in args.agency:
        print(f"init | {s} | FACTS.csv POLICY.csv")
    print(f"init | agencies={len(args.agency)} | LAW.csv | private store; keep it out of the repository and back it up "
          "(flock_backup.py)")
    return 0

if __name__ == "__main__":
    sys.exit(main())
