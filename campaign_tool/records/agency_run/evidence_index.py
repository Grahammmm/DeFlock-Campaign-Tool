"""One evidence index per agency (REC-EVID-IDX-1). Read-only; never fetches a link."""
import argparse
import csv
import hashlib
import logging
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

HEADER = "path location kind date_pacific request_ref sha256 pdf_pages text_layer role".split()
MAX_HASH = 200 * 1024 * 1024
MAX_PDF = 50 * 1024 * 1024
MAX_INDEX = 64 * 1024 * 1024
MAX_ENTRIES = 200000
REF = re.compile(r"[0-9]{2}-[0-9]{2,5}")
DATE_FORMATS = ("%d-%b-%Y %H:%M:%S %z", "%Y-%m-%d %H:%M:%S %z")


class Refuse(Exception):
    pass


def key(text):
    return re.sub(r"[^a-z0-9]", "", str(text).lower())


def load_aliases(path):
    p = Path(path)
    if p.is_symlink() or not p.is_file():
        raise Refuse("alias_file_missing")
    aliases = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if len(aliases) >= 64 or not 3 <= len(line) <= 80 or not key(line):
            raise Refuse("alias_invalid")
        aliases.append(line)
    if not aliases:
        raise Refuse("alias_file_empty")
    return aliases


def pacific_date(cell):
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(str(cell).strip(), fmt).astimezone(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""


def inside(root, rel):
    """Return the real path of rel if it is a non-symlink inside root, else None."""
    if not rel or os.path.isabs(rel):
        return None
    joined = os.path.join(root, rel)
    real = os.path.realpath(joined)
    if os.path.commonpath([root, real]) != root or os.path.islink(joined):
        return None
    return real


def sha256(path, size):
    if size > MAX_HASH:
        return "unknown - too large"
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def pdf_facts(path, size):
    if not path.lower().endswith(".pdf"):
        return "none", "none"
    if size > MAX_PDF:
        return "unknown", "unknown"
    try:
        from pypdf import PdfReader
        logging.getLogger("pypdf").setLevel(logging.CRITICAL)
    except ImportError:
        raise Refuse("pypdf_missing")
    try:
        with open(path, "rb") as handle:
            reader = PdfReader(handle)
            pages = len(reader.pages)
            native = any(len("".join((reader.pages[i].extract_text() or "").split())) >= 20
                         for i in range(min(5, pages)))
    except Exception:
        return "unknown", "unknown"
    return str(pages), "native" if native else "none"


def walk(root, counts):
    stack, seen = [root], 0
    while stack:
        directory = stack.pop()
        with os.scandir(directory) as it:
            for entry in it:
                seen += 1
                if seen > MAX_ENTRIES:
                    raise Refuse("too_many_files")
                rel = os.path.relpath(entry.path, root).replace(os.sep, "/")
                if entry.is_symlink():
                    counts["symlinks"] += 1
                elif entry.is_dir(follow_symlinks=False):
                    if rel != "mail-history":
                        stack.append(entry.path)
                elif entry.is_file(follow_symlinks=False):
                    yield rel


def read_index(root, rel):
    real = inside(root, rel)
    if not real or not os.path.isfile(real) or os.path.getsize(real) > MAX_INDEX:
        return []
    with open(real, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def build(aliases, root):
    root = os.path.realpath(root)
    counts = {"symlinks": 0, "refused": 0}
    keys = [(a, key(a)) for a in aliases]
    refs = [a for a in aliases if REF.fullmatch(a)]
    rows = {}

    def hits(text):
        k = key(text)
        return [a for a, ak in keys if ak in k]

    def add(rel, kind, role, text, matched, date="", location="records-ro", digest=None):
        real = inside(root, rel) if location != "index-only" else None
        pages = text_layer = "none"
        if real:
            size = os.path.getsize(real)
            digest = sha256(real, size)
            pages, text_layer = pdf_facts(real, size)
        ref = next((a for a in refs if key(a) in key(text)), "")
        entry = rows.setdefault((rel, kind), {"matched": set()})
        entry["matched"].update(matched)
        entry["row"] = [rel, location, kind, date, ref, digest or "", pages, text_layer, role]

    for rel in walk(root, counts):
        found = hits(rel)
        if found:
            first = rel.split("/", 1)[0]
            kind, role = (("portal-file", "original") if first == "received" else
                          ("third-party", "third-party") if first == "third-party" else
                          ("prior-analysis", "lead"))
            add(rel, kind, role, rel, found)

    messages = read_index(root, "mail-history/MESSAGE-INDEX.csv")
    dates = {(m.get("folder"), m.get("uid")): pacific_date(m.get("internaldate", "")) for m in messages}
    matched_msgs = {}
    for m in messages:
        text = " ".join(m.get(h) or "" for h in ("From", "To", "Cc", "Subject"))
        found = hits(text)
        if not found:
            continue
        matched_msgs[(m.get("folder"), m.get("uid"))] = found
        rel = (m.get("eml_path") or "").strip()
        if inside(root, rel):
            if os.path.isfile(inside(root, rel)):
                add(rel, "mail", "original", rel + " " + text, found, dates[(m.get("folder"), m.get("uid"))])
                continue
        elif rel:
            counts["refused"] += 1
            continue
        add(rel, "mail", "original", rel + " " + text, found, dates[(m.get("folder"), m.get("uid"))],
            "index-only", m.get("sha256", ""))
    for a in read_index(root, "mail-history/ATTACHMENT-INDEX.csv"):
        ident = (a.get("folder"), a.get("uid"))
        found = list(matched_msgs.get(ident, [])) + hits(a.get("original_filename") or "")
        if not found:
            continue
        rel = (a.get("path") or "").strip()
        text = rel + " " + (a.get("original_filename") or "")
        real = inside(root, rel)
        if real and os.path.isfile(real):
            add(rel, "mail", "original", text, found, dates.get(ident, ""))
        elif rel and not real:
            counts["refused"] += 1
        else:
            add(rel, "mail", "original", text, found, dates.get(ident, ""), "index-only", a.get("sha256", ""))
    return rows, counts


def clean(cell):
    return re.sub(r"[\t\r\n]", " ", str(cell).replace("<", "_").replace(">", "_"))


def write_private(path, text):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def render(aliases, root, rows, counts):
    ordered = sorted(rows.values(), key=lambda e: (e["row"][0], e["row"][2]))
    tsv = "\n".join("\t".join(clean(c) for c in r) for r in [HEADER] + [e["row"] for e in ordered]) + "\n"
    tally = {}
    for e in ordered:
        for label in (e["row"][2], "role " + e["row"][8]):
            tally[label] = tally.get(label, 0) + 1
    md = ["# Evidence index", "", "Scope: " + clean("; ".join(aliases)), "Root: " + clean(os.path.basename(root)), ""]
    md += ["- %s: %d" % (clean(k), v) for k, v in sorted(tally.items())]
    md += ["", "symlinks skipped: %d" % counts["symlinks"], "refused paths: %d" % counts["refused"], ""]
    for alias in aliases:
        n = sum(1 for e in ordered if alias in e["matched"])
        md.append("alias: %s - %s" % (clean(alias), "%d files" % n if n else "no files"))
    return tsv, "\n".join(md) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(prog="records agency-evidence", description=__doc__)
    ap.add_argument("alias_file")
    ap.add_argument("--root", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    try:
        aliases = load_aliases(args.alias_file)
        if not os.path.isdir(args.root):
            raise Refuse("root_missing")
        root, out = os.path.realpath(args.root), os.path.realpath(args.out)
        if out == root or os.path.commonpath([root, out]) == root:
            raise Refuse("out_inside_root")
        if os.path.exists(out) and (not os.path.isdir(out) or os.listdir(out)):
            raise Refuse("out_not_empty")
        rows, counts = build(aliases, root)
        tsv, md = render(aliases, root, rows, counts)
        os.makedirs(out, mode=0o700, exist_ok=True)
        write_private(os.path.join(out, "evidence-index.tsv"), tsv)
        write_private(os.path.join(out, "EVIDENCE-INDEX.md"), md)
    except Refuse as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print("%d rows written" % len(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
