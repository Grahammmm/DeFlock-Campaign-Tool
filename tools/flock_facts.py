"""Per-agency fact store (schema v2): import v1 FACTS.csv, check, and coverage per posted INVENTORY document. Prints counts only."""
import argparse, contextlib, csv, datetime, hashlib, os, re, sys, tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
V1 = ["agency", "request", "fact_type", "value", "source_sha256", "page", "verbatim_quote"]
V2 = V1 + ["fact_id", "kind", "effective_from", "effective_to", "added_by", "added_at", "status"]
KINDS = {"record", "policy", "law", "public"}
STATUSES = {"unreviewed", "checked", "disputed"}


def slugify(agency):
    return re.sub(r"[^a-z0-9]+", "-", agency.lower()).strip("-")


def dedupe_key(row):
    return (slugify(row["agency"]),) + tuple(row[k].strip() for k in ("source_sha256", "page", "fact_type", "value"))


def fact_id(row):
    return "F-" + hashlib.sha256("\x1f".join(dedupe_key(row)).encode()).hexdigest()[:12]


def valid_date(text):
    try:
        return len(text) == 10 and bool(datetime.datetime.strptime(text, "%Y-%m-%d"))
    except ValueError:
        return False


def validate(row, v2, folder=None, kind="record"):
    errs = [(k, "is empty") for k in ("agency", "fact_type", "value") if not (slugify(row[k]) if k == "agency" else row[k].strip())]
    if not re.fullmatch(r"[0-9a-f]{64}", row["source_sha256"].strip()):
        errs.append(("source_sha256", "must be 64 lowercase hex"))
    if not re.fullmatch(r"[1-9][0-9]*", row["page"].strip()):
        errs.append(("page", "must be an integer >= 1"))
    if not row["verbatim_quote"].strip() and row["value"].strip() != "UNREADABLE":
        errs.append(("verbatim_quote", "is empty"))
    kind = row["kind"] if v2 else kind
    if kind == "record" and not row["request"].strip():
        errs.append(("request", "is empty for kind record"))
    if not v2:
        return errs
    dates = [row["effective_from"], row["effective_to"]]
    for field, ok in (("kind", kind in KINDS), ("status", row["status"] in STATUSES),
                      ("effective_from", not dates[0] or valid_date(dates[0])),
                      ("effective_to", not dates[1] or valid_date(dates[1])),
                      ("added_by", row["added_by"].strip()), ("added_at", valid_date(row["added_at"])),
                      ("fact_id", row["fact_id"] == fact_id(row)),
                      ("agency", folder is None or slugify(row["agency"]) == folder)):
        if not ok:
            errs.append((field, "is invalid"))
    if all(dates) and all(map(valid_date, dates)) and dates[0] > dates[1]:
        errs.append(("effective_to", "is before effective_from"))
    return errs


def load(path, folder=None, kind="record"):
    with open(path, encoding="utf-8-sig", newline="") as handle:
        records = list(csv.reader(handle))
    if not records or records[0] not in (V1, V2) or (folder is not None and records[0] != V2):
        return False, [], ["row 1: header is not an accepted header"]
    v2, rows, errors = records[0] == V2, [], []
    for number, rec in enumerate(records[1:], 2):
        if not rec:
            continue
        if len(rec) != len(records[0]):
            errors.append("row %d: columns have the wrong count" % number)
            continue
        rows.append((number, dict(zip(records[0], rec))))
        errors += ["row %d: %s %s" % (number, f, why) for f, why in validate(rows[-1][1], v2, folder, kind)]
    return v2, rows, errors


def check_file(path, folder=None):
    _, rows, errors = load(path, folder)
    first = {}
    for number, row in rows:
        if dedupe_key(row) in first:
            errors.append("row %d: duplicate of row %d" % (number, first[dedupe_key(row)]))
        first.setdefault(dedupe_key(row), number)
    return errors


def check_path(target):
    if not Path(target).is_dir():
        return check_file(target)
    files = sorted(Path(target).glob("agencies/*/FACTS.csv"))
    return ["file %d: %s" % (n, e) for n, f in enumerate(files, 1) for e in check_file(f, f.parent.name)] \
        or ([] if files else ["no FACTS.csv files in store"])


def write_atomic(path, rows):
    for sub in reversed(path.parents[:3]):
        sub.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as out:
            csv.writer(out, lineterminator="\n", quoting=csv.QUOTE_MINIMAL).writerows([V2] + [[r[k] for k in V2] for r in rows])
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError): os.unlink(tmp)
        raise


def do_import(args):
    store = Path(args.store).resolve()
    if store == REPO or REPO in store.parents:
        print("store inside repository")
        return 1
    v2, rows, errors = load(args.src, kind=args.kind)
    by_slug, old = {}, {}
    for _, row in rows:
        if not v2:
            row = dict(row, kind=args.kind, effective_from="", effective_to="", added_by=args.added_by,
                       added_at=args.date, status=args.status, fact_id=fact_id(row))
        by_slug.setdefault(slugify(row["agency"]), []).append(row)
    for rel in ([] if errors else ["agencies"] + ["agencies/" + slug for slug in by_slug]):
        if os.path.islink(store / rel):
            errors.append("%s: is a symlink; refusing" % rel)
        elif os.path.lexists(store / rel) and not (store / rel).is_dir():
            errors.append("%s: exists and is not a folder" % rel)
    for n, slug in enumerate(by_slug if not errors else (), 1):
        path = store / "agencies" / slug / "FACTS.csv"
        _, found, errs = load(path, slug) if path.exists() else (True, [], [])
        errors += ["store file %d: %s" % (n, e) for e in errs]
        old[slug] = [r for _, r in found]
    if errors:
        print("\n".join(errors))
        print("imported=0 duplicates=0 errors=%d" % len(errors))
        return 1
    imported = duplicates = 0
    for slug, new_rows in by_slug.items():
        seen, fresh = {dedupe_key(r) for r in old[slug]}, []
        for row in new_rows:
            duplicates += dedupe_key(row) in seen
            if dedupe_key(row) not in seen:
                fresh.append(row)
                seen.add(dedupe_key(row))
        if fresh:
            write_atomic(store / "agencies" / slug / "FACTS.csv", old[slug] + fresh)
        imported += len(fresh)
    print("imported=%d duplicates=%d errors=0 agencies=%d" % (imported, duplicates, len(by_slug)))
    return 0


def coverage(args):
    slug = slugify(args.agency)
    path = Path(args.store) / "agencies" / slug / "FACTS.csv"
    if not path.is_file():
        return print("facts: %s has no FACTS.csv" % slug) or 1
    errors = check_file(path, slug)
    if errors:
        return print("facts: %d errors (run --check)" % len(errors)) or 1
    with open(args.inventory, encoding="utf-8-sig", newline="") as handle:
        records = list(csv.reader(handle))
    head = records[0] if records else []
    lacks = [c for c in ("request", "doc_id", "sha256", "analysis_only") if c not in head]
    if lacks:
        return print("inventory: header lacks %s" % ", ".join(lacks)) or 1
    docs, facts = {}, {}
    for number, rec in ((n, r) for n, r in enumerate(records[1:], 2) if r):
        if len(rec) != len(head):
            return print("inventory: row %d: columns have the wrong count" % number) or 1
        docs.setdefault((row := dict(zip(head, rec)))["sha256"].strip(), []).append(row)
    for _, row in load(path, slug)[1]:
        facts.setdefault(row["source_sha256"].strip(), []).append(row)
    withheld = {sha for sha, rows in docs.items() if any(r["analysis_only"] != "no" for r in rows)}
    posted = sorted((rows[0]["request"].strip(), rows[0]["doc_id"].strip(), sha) for sha, rows in docs.items() if sha not in withheld)
    covered = sum(sha in facts for _, _, sha in posted)
    for request, doc_id, sha in posted:
        got = facts.get(sha, [])
        print("coverage | %s | document %s | facts=%d pages=%d unreadable=%d" % (request or "-", doc_id, len(got),
              len({r["page"].strip() for r in got}), sum(r["value"].strip() == "UNREADABLE" for r in got)))
    print("documents=%d posted=%d covered=%d uncovered=%d withheld=%d facts_on_withheld=%d orphan_facts=%d"
          " | fact rows per document, not a review verdict" % (len(docs), len(posted), covered, len(posted) - covered,
          len(withheld), sum(len(facts.get(s, ())) for s in withheld), sum(len(v) for s, v in facts.items() if s not in docs)))
    return 0


def status(args):
    """Review state per agency (RF1220-2): counts by kind and status, and ids to review. Read-only; never a value or quote."""
    store, slug, fail = Path(args.store), slugify(args.agency or ""), lambda lines: print(*lines, sep="\n", file=sys.stderr) or 1
    if not store.is_dir():
        return fail(["store: is not a folder"])
    links = ["%s: is a symlink; refusing" % r for r in ["agencies"] + ["agencies/" + slug] * bool(slug) if os.path.islink(store / r)]
    if links:
        return fail(links)
    if slug and not (store / "agencies" / slug / "FACTS.csv").is_file():
        return fail(["agency %s: no FACTS.csv" % slug])
    agencies = store / "agencies"
    files = [agencies / slug / "FACTS.csv"] if slug else sorted(agencies.glob("*/FACTS.csv"))
    links = ["%s: is a symlink; refusing" % p.relative_to(store) for p in sorted(set(
        ([] if slug else list(agencies.glob("*"))) + files)) if p.is_symlink()]
    errors = links or ["file %d: %s" % (n, e) for n, f in enumerate(files, 1) for e in check_file(f, f.parent.name)]
    if errors or not files:
        return fail(errors or ["no FACTS.csv files in store"])
    listed, total = [], {"rows": 0, "unreviewed": 0, "disputed": 0}
    for f in files:
        rows, count = load(f, f.parent.name)[1], {}
        for _, row in rows:
            for key in ("rows", row["kind"], row["status"]) + ("unreadable",) * (row["value"].strip() == "UNREADABLE"):
                count[key] = count.get(key, 0) + 1
        print(" | ".join([f.parent.name, "rows=%d" % len(rows)] + [" ".join("%s=%d" % (k, count.get(k, 0)) for k in group) for group in (
            ("record", "policy", "law", "public"), ("unreviewed", "checked", "disputed"), ("unreadable",))]))
        listed += ["%s | %s | %s | %s | page %s | row %d" % (f.parent.name, r["fact_id"], r["kind"], r["fact_type"], r["page"].strip(), n)
                   for n, r in rows if r["status"] == args.list]
        total = {k: v + count.get(k, 0) for k, v in total.items()}
    print(*listed, sep="\n") if listed else None
    print("agencies=%d rows=%d unreviewed=%d disputed=%d" % (len(files), total["rows"], total["unreviewed"], total["disputed"]))
    return 0


def main(argv=None):
    try:
        return _main(argv)
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        print("io: %s" % type(exc).__name__)
        return 1


def _main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="*", help="import SRC | coverage --store DIR --agency NAME --inventory CSV"
                        " | status --store DIR [--agency SLUG] [--list unreviewed|disputed]")
    for name in ("--check", "--store", "--added-by", "--agency", "--inventory", "--list"):
        parser.add_argument(name)
    parser.add_argument("--kind", default="record", choices=sorted(KINDS))
    parser.add_argument("--status", default="checked", choices=sorted(STATUSES))
    parser.add_argument("--date", default=datetime.date.today().isoformat())
    args = parser.parse_args(argv)
    if args.command[:1] == ["status"]:
        if len(args.command) != 1 or args.check or not args.store or args.inventory or args.added_by is not None \
                or args.list not in (None, "unreviewed", "disputed") or (args.agency is not None and not slugify(args.agency)):
            parser.error("usage: status --store DIR [--agency SLUG] [--list unreviewed|disputed]")
        return status(args)
    if args.check and not args.command and args.list is None:
        errors = check_path(args.check)
        return print("\n".join(errors)) or int(bool(errors))
    if args.command == ["coverage"] and not args.check and args.store and args.inventory and slugify(args.agency or "") \
            and args.list is None:
        return coverage(args)
    if args.check or args.list is not None or args.command[:1] != ["import"] or len(args.command) != 2 or not args.store \
            or not (args.added_by or "").strip() or not valid_date(args.date):
        parser.error("usage: import SRC --store DIR --added-by NAME [--date YYYY-MM-DD] | --check PATH"
                     " | coverage --store DIR --agency NAME --inventory CSV")
    args.src = args.command[1]
    return do_import(args)


if __name__ == "__main__":
    sys.exit(main())
