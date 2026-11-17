"""Read-only: which agency-run stage outputs exist in a folder, and what is next."""
import fnmatch
import os
import stat
import sys
import time

# (label, required files, directory holding >= 1 qualifying file, glob)
STAGES = (
    ("evidence index", ("EVIDENCE-INDEX.md", "evidence-index.tsv"), None, None),
    ("events", ("events.tsv",), None, None),
    ("inventory and pages", ("inventory.tsv",), "pages", "*_p[0-9][0-9][0-9]*.txt"),
    ("findings", ("FINDINGS.md",), None, None),
    ("drafts", (), "drafts", "*"),
    ("next response", ("NEXT-RESPONSE.md",), None, None),
)


def scan(path):
    """Map relative path -> mtime for regular, non-symlink files (lstat only)."""
    found = {}
    def regular(p):
        st = os.lstat(p)
        return st.st_mtime if stat.S_ISREG(st.st_mode) else None
    with os.scandir(path) as top:
        for entry in top:
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode):
                found[entry.name] = st.st_mtime
            elif stat.S_ISDIR(st.st_mode) and entry.name in ("pages", "drafts"):
                with os.scandir(entry.path) as sub:
                    for child in sub:
                        mtime = regular(child.path)
                        if mtime is not None:
                            found[entry.name + "/" + child.name] = mtime
    return found


def stages(entries):
    """Return [(label, state, newest_mtime or None)] for the six stages."""
    out = []
    for label, files, folder, pattern in STAGES:
        times = [entries[f] for f in files if f in entries]
        extra = [m for k, m in entries.items() if folder and k.startswith(folder + "/")
                 and fnmatch.fnmatchcase(k.split("/", 1)[1], pattern)]
        times += extra
        complete = len(times) - len(extra) == len(files) and (not folder or extra)
        if complete and times:
            state = "present"
        elif times and label in ("evidence index", "inventory and pages"):
            state = "partial"
        else:
            state = "missing"
        out.append((label, state, max(times) if times and state != "missing" else None))
    return out


def age_text(seconds):
    if seconds < 60:
        return "now"
    if seconds < 3600:
        return "%dm" % (seconds // 60)
    if seconds < 48 * 3600:
        return "%dh" % (seconds // 3600)
    return "%dd" % (seconds // 86400)


def report(name, entries, now):
    if len(name) > 60:
        name = name[:57] + "..."
    lines = ["agency run: " + name]
    first = None
    for label, state, mtime in stages(entries):
        age = "-" if mtime is None else age_text(now - mtime)
        lines.append("%-20s %-8s %s" % (label, state, age))
        if state != "present" and first is None:
            first = label
    lines.append("next: " + (first or "none, all stages present"))
    return lines


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    now = time.time()
    if "--now" in args:
        i = args.index("--now")
        try:
            now = float(args[i + 1])
        except (IndexError, ValueError):
            print("refused agency_status_args")
            return 2
        del args[i:i + 2]
    if len(args) != 1:
        print("refused agency_status_args")
        return 2
    path = args[0]
    try:
        if not stat.S_ISDIR(os.lstat(path).st_mode):
            raise OSError
        entries = scan(path)
    except OSError:
        print("refused agency_status_dir")
        return 2
    print("\n".join(report(os.path.basename(os.path.abspath(path)), entries, now)))
    return 0
