"""Per-agency fact store (schema v2): import v1 FACTS.csv, check, and coverage per posted INVENTORY document. Prints counts only."""
import argparse, contextlib, csv, datetime, hashlib, importlib.util, io, json, os, re, sys, tempfile, unicodedata
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
V1 = ["agency", "request", "fact_type", "value", "source_sha256", "page", "verbatim_quote"]
V2 = V1 + ["fact_id", "kind", "effective_from", "effective_to", "added_by", "added_at", "status"]
KINDS = {"record", "policy", "law", "public"}
STATUSES = {"unreviewed", "checked", "disputed"}
REVIEW_HEAD = ["fact_id", "from_status", "to_status", "reviewer", "reviewed_at", "note"]  # FACT-REVIEWS.csv v1


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
    return ["file %d: %s" % (n, e) for n, f in enumerate(files, 1)
            for e in check_file(f, f.parent.name) + check_reviews(f.parent / "FACT-REVIEWS.csv", f)] \
        or ([] if files else ["no FACTS.csv files in store"])


def check_reviews(log, facts):
    """FACT-REVIEWS.csv v1 if present (DF1537-01): rows valid, ids in FACTS.csv, last logged to_status == FACTS status."""
    if os.path.islink(log) or (os.path.lexists(log) and not log.is_file()):
        return ["FACT-REVIEWS.csv: is a symlink or not a file; refusing"]
    if not log.exists():
        return []
    with open(log, encoding="utf-8-sig", newline="") as handle:
        records = list(csv.reader(handle))
    if not records or records[0] != REVIEW_HEAD:
        return ["reviews row 1: header is not FACT-REVIEWS.csv v1"]
    by_id, last, errors = {r["fact_id"]: r for _, r in load(facts, facts.parent.name)[1]}, {}, []
    for n, rec in ((n, r) for n, r in enumerate(records[1:], 2) if r):
        if len(rec) != len(REVIEW_HEAD):
            errors.append("reviews row %d: columns have the wrong count" % n)
            continue
        r, fact = dict(zip(REVIEW_HEAD, rec)), by_id.get(rec[0])
        who = r["reviewer"].strip().casefold()
        checks = (("fact_id", fact),
                  ("from_status", r["from_status"] in STATUSES and r["from_status"] == last.get(rec[0], r["from_status"])),
                  ("to_status", r["to_status"] in {"checked", "disputed"} - {r["from_status"]}),
                  ("reviewer", who and not (fact and fact["added_by"].strip().casefold() == who)),
                  ("reviewed_at", valid_date(r["reviewed_at"])),
                  ("note", not re.search(r"[\r\n]|.{121}", r["note"])))
        for field, ok in checks:
            if not ok:
                errors.append("reviews row %d: %s %s" % (n, field, "is not in FACTS.csv" if field == "fact_id" else "is invalid"))
        last[rec[0]] = r["to_status"]
    return errors + ["reviews: %s: FACTS status %s does not match the review log (%s)" % (i, by_id[i]["status"], s)
                     for i, s in last.items() if i in by_id and by_id[i]["status"] != s]


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


def fail(lines):
    return print(*lines, sep="\n", file=sys.stderr) or 1


def store_files(store, slug):
    """(FACTS.csv files of every agency or one slug, refusal lines); an agency symlink is refused, never followed."""
    if not store.is_dir():
        return [], ["store: is not a folder"]
    links = ["%s: is a symlink; refusing" % r for r in ["agencies"] + ["agencies/" + slug] * bool(slug) if os.path.islink(store / r)]
    if links:
        return [], links
    if slug and not (store / "agencies" / slug / "FACTS.csv").is_file():
        return [], ["agency %s: no FACTS.csv" % slug]
    agencies = store / "agencies"
    files = [agencies / slug / "FACTS.csv"] if slug else sorted(agencies.glob("*/FACTS.csv"))
    return files, ["%s: is a symlink; refusing" % p.relative_to(store) for p in sorted(set(
        ([] if slug else list(agencies.glob("*"))) + files)) if p.is_symlink()]


def status(args):
    """Review state per agency (RF1220-2): counts by kind and status, and ids to review. Read-only; never a value or quote."""
    files, errors = store_files(Path(args.store), slugify(args.agency or ""))
    errors = errors or ["file %d: %s" % (n, e) for n, f in enumerate(files, 1) for e in check_file(f, f.parent.name)]
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


OUTCOMES = ("found", "not-found", "no-document", "ambiguous-export", "no-page", "page-hash-mismatch", "unreadable-fact",
            "skipped-kind", "bad-row")
FOLD = str.maketrans("\u2018\u2019\u201a\u201b\u2032\u201c\u201d\u201e\u201f\u2033\u2010\u2011\u2012\u2013\u2014\u2015\u2212",
                     "'''''\"\"\"\"\"-------")


def fold(text):
    """Match form (NB-DF-02): NFKC, casefold, curly quotes and dashes folded, every whitespace run one space, stripped."""
    return " ".join(unicodedata.normalize("NFKC", text).casefold().translate(FOLD).split())


def plain_bytes(path):
    """Bytes of a regular file; None when it is absent, a symlink or not a regular file (a link is never followed)."""
    return None if path.is_symlink() or not path.is_file() else path.read_bytes()


def export_doc(export, sha, docs):
    """(signature folder, {page: text sha256}) of one exported document, or (outcome, None); cached per sha256."""
    if sha not in docs:
        base = export / sha
        subs = [] if base.is_symlink() or not base.is_dir() else sorted(
            p for p in base.iterdir() if not p.name.startswith(".") and (p.is_dir() or p.is_symlink()))
        if len(subs) != 1 or subs[0].is_symlink():
            docs[sha] = ("ambiguous-export" if len(subs) > 1 else "no-document", None)
            return docs[sha]
        try:
            rec = json.loads(plain_bytes(subs[0] / "result.json") or b"null")
        except ValueError:
            rec = None
        ok = isinstance(rec, dict) and rec.get("schema") == "ocr-stage-v1" and rec.get("source_sha256") == sha \
            and isinstance(rec.get("pages"), list)
        docs[sha] = (subs[0], {r["page"]: r.get("text_sha256") for r in rec["pages"] if isinstance(r, dict)
                               and type(r.get("page")) is int} if ok else {})
    return docs[sha]


def quote_outcome(export, row, docs):
    if row["kind"] != "record" or row["value"].strip() == "UNREADABLE":
        return "skipped-kind" if row["kind"] != "record" else "unreadable-fact"
    where, hashes = export_doc(export, row["source_sha256"].strip(), docs)
    if hashes is None:
        return where
    data = plain_bytes(where / ("page-%04d.txt" % int(row["page"])))
    if data is None:
        return "no-page"
    if hashlib.sha256(data).hexdigest() != hashes.get(int(row["page"])):
        return "page-hash-mismatch"
    return "found" if fold(row["verbatim_quote"]) in fold(data.decode("utf-8")) else "not-found"


def quote_check(args, parser):
    """NB-DF-02: record facts' verbatim_quote on the cited page of a page-text export. Ids/counts only, never text or paths."""
    text = Path(args.text).resolve()
    if any(text == p or p in text.parents for p in (REPO, Path(args.store).resolve())):
        parser.error("quote-check: --text must be outside the repository and the store")
    if not text.is_dir():
        return fail(["text: is not a folder"])
    files, errors = store_files(Path(args.store), slugify(args.agency or ""))
    if errors or not files:
        return fail(errors or ["no FACTS.csv files in store"])
    docs, table, listed, seen = {}, [], [], set()
    for n, f in enumerate(files, 1):
        v2, rows, errs = load(f, f.parent.name)
        if not v2:
            return fail(["file %d: %s" % (n, errs[0])])
        got = [(num, "bad-row" if validate(r, True, f.parent.name) else quote_outcome(text, r, docs), r) for num, r in rows]
        got += [(int(m[1]), "bad-row", None) for m in (re.match(r"row (\d+): columns", e) for e in errs) if m]
        count = {k: 0 for k in OUTCOMES}
        for num, outcome, r in sorted(got, key=lambda g: g[0]):
            count[outcome] += 1
            if outcome != "found":
                ident = r["fact_id"] if r and re.fullmatch(r"F-[0-9a-f]{12}", r["fact_id"]) else "row %d" % num
                page = r["page"].strip() if r and re.fullmatch(r"[1-9][0-9]*", r["page"].strip()) else "-"
                listed.append("%s | %s | %s | page %s" % (f.parent.name, ident, outcome, page))
        seen |= {k for k in OUTCOMES if count[k]}
        table.append("quote-check | %s | facts=%d " % (f.parent.name, len(got)) + " ".join("%s=%d" % (k, count[k]) for k in OUTCOMES))
    print(*table + listed, sep="\n")
    return 3 if seen & {"not-found", "page-hash-mismatch"} else 1 if "bad-row" in seen else 0


def stale(args):
    """NB-DF-08: facts expired on --date, superseded, disputed or not yet in force, and the findings citing them. Read-only;
    ids, dates and counts only, never a value or quote. Nothing is printed unless the whole report can be built."""
    files, errors = store_files(Path(args.store), slugify(args.agency or ""))
    errors = errors or ["file %d: %s" % (n, e) for n, f in enumerate(files, 1) for e in check_file(f, f.parent.name)]
    if errors or not files:
        return fail(errors or ["no FACTS.csv files in store"])
    date, why, lines, total, count = args.date, {}, [], 0, dict.fromkeys((
        "expired", "superseded", "disputed", "not_yet"), 0)
    for f in files:
        rows = [r for _, r in load(f, f.parent.name)[1]]
        total += len(rows)
        for r in rows:
            start, end = r["effective_from"], r["effective_to"]
            later = [(g["effective_from"], g["fact_id"]) for g in rows if start and not end and (g["kind"], g["fact_type"])
                     == (r["kind"], r["fact_type"]) and start < g["effective_from"] <= date]
            by = min((i for d, i in later if d == max(later)[0]), default=None)  # latest in force; equal dates: smallest id
            got = [("expired", "expired " + end)] * bool(end and end < date) + [("superseded", "superseded-by %s" % by)] * bool(
                by) + [("disputed", "disputed")] * (r["status"] == "disputed") + [("not_yet", "not-yet " + start)] * bool(
                start and start > date)
            for key, text in got:
                count[key] += 1
                lines.append("stale | %s | %s | %s" % (f.parent.name, r["fact_id"], text))
                if key != "not_yet":
                    why.setdefault(r["fact_id"], []).append(text)
    affected = set()
    if args.findings is not None:
        spec = importlib.util.spec_from_file_location("_facts_compare", REPO / "tools" / "flock_compare.py")
        spec.loader.exec_module(compare := importlib.util.module_from_spec(spec))  # ids() split, loaded by path
        with open(args.findings, encoding="utf-8-sig", newline="") as handle:
            records = list(csv.reader(handle))
        head = records[0] if records else []
        lacks = [c for c in ("finding_id", "fact_ids") if c not in head]
        if lacks:
            return fail(["findings: header lacks %s" % ", ".join(lacks)])
        for number, rec in ((n, r) for n, r in enumerate(records[1:], 2) if r):
            if len(rec) != len(head):
                return fail(["findings: row %d: columns have the wrong count" % number])
            row = dict(zip(head, rec))
            for cited in (i for i in dict.fromkeys(compare.ids(row["fact_ids"])) if i in why):
                lines.append("stale | finding %s | cites %s (%s)" % (row["finding_id"].strip(), cited, ", ".join(why[cited])))
                affected.add(number)
    print(*lines, sep="\n") if lines else None
    print("facts=%d expired=%d superseded=%d disputed=%d not_yet=%d findings_affected=%d" % (
        total, count["expired"], count["superseded"], count["disputed"], count["not_yet"], len(affected)))
    return 3 if (affected if args.findings is not None else count["expired"] + count["superseded"]) else 0


def review(args):
    """DF1537-01: set checked or disputed on listed fact ids by a reviewer other than added_by, each change logged to
    FACT-REVIEWS.csv. All or nothing; ids and counts only, never a value, quote, page or note."""
    store, slug, date, who = Path(args.store).resolve(), slugify(args.agency), args.date, args.reviewer.strip()
    if store == REPO or REPO in store.parents:
        return fail(["store inside repository"])
    files, errors = store_files(store, slug)
    log = store / "agencies" / slug / "FACT-REVIEWS.csv"
    errors = errors or ["file 1: %s" % e for e in check_file(files[0], slug) + check_reviews(log, files[0])]
    if errors:
        return fail(errors)
    rows = [r for _, r in load(files[0], slug)[1]]
    by_id, ids, ok_date = {r["fact_id"]: r for r in rows}, [i.strip() for i in args.ids.split(",")], valid_date(date)
    shown = lambda n, i: i if re.fullmatch(r"F-[0-9a-f]{12}", i) else "at position %d" % n  # noqa: E731
    errors = ["--ids: more than 200 ids"] * (len(ids) > 200)
    errors += ["--ids: duplicate id %s" % shown(n, i) for n, i in enumerate(ids, 1) if i in ids[:n - 1]]
    errors += ["--ids: unknown fact id %s" % shown(n, i) for n, i in enumerate(ids, 1) if i not in by_id]
    errors += ["--reviewer: is empty"] * (not who) + ["--date: is not YYYY-MM-DD"] * (not ok_date)
    errors += ["--date: is after today"] * (ok_date and date > datetime.date.today().isoformat())
    errors += ["--note: longer than 120 characters or has a line break"] * bool(re.search(r"[\r\n]|.{121}", args.note or ""))
    for i in (i for i in dict.fromkeys(ids) if i in by_id):
        errors += ["%s: reviewer is the person who added it" % i] * (who.casefold() == by_id[i]["added_by"].strip().casefold())
        errors += ["%s: --date is before added_at" % i] * (ok_date and date < by_id[i]["added_at"])
    if errors:
        return fail(errors)
    lines = ["review | %s | %s | %s" % (slug, i, "unchanged" if by_id[i]["status"] == args.set else "%s -> %s" % (
        by_id[i]["status"], args.set)) for i in ids]
    changed = [[i, by_id[i]["status"], args.set, who, date, args.note or ""] for i in ids if by_id[i]["status"] != args.set]
    if changed:
        for i, *_ in changed:
            by_id[i]["status"] = args.set
        old, add, swapped = plain_bytes(log), io.StringIO(), False
        csv.writer(add, lineterminator="\n").writerows(([] if old else [REVIEW_HEAD]) + changed)
        fd, tmp = tempfile.mkstemp(prefix=".FACT-REVIEWS.csv.", suffix=".tmp", dir=log.parent)
        try:  # the log is replaced first; if FACTS.csv then cannot be, the old log is put back
            with os.fdopen(fd, "wb") as out:
                out.write((old + b"\n" if old and not old.endswith(b"\n") else old or b"") + add.getvalue().encode())
            os.replace(tmp, log)
            swapped = True
            write_atomic(files[0], rows)
        except BaseException:
            with contextlib.suppress(OSError): os.unlink(tmp)
            if swapped:
                with contextlib.suppress(OSError): log.write_bytes(old) if old is not None else log.unlink()
            raise
    print(*lines, "reviewed=%d changed=%d unchanged=%d" % (len(ids), len(changed), len(ids) - len(changed)), sep="\n")
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
                        " | status --store DIR [--agency SLUG] [--list unreviewed|disputed]"
                        " | quote-check --store DIR --text EXPORT [--agency SLUG]"
                        " | stale --store DIR --date YYYY-MM-DD [--agency SLUG] [--findings FINDINGS.csv]"
                        " | review --store DIR --agency SLUG --ids ID[,ID...] --set checked|disputed --reviewer NAME"
                        " [--date YYYY-MM-DD] [--note TEXT]")
    for name in ("--check", "--store", "--added-by", "--agency", "--inventory", "--list", "--text", "--findings", "--date",
                 "--ids", "--reviewer", "--note"):
        parser.add_argument(name)
    parser.add_argument("--kind", default="record", choices=sorted(KINDS))
    parser.add_argument("--status", default="checked", choices=sorted(STATUSES))
    parser.add_argument("--set", choices=["checked", "disputed"])
    args = parser.parse_args(argv)
    if args.command[:1] == ["review"]:
        if len(args.command) != 1 or args.check or not args.store or not slugify(args.agency or "") or not args.ids \
                or not args.set or args.reviewer is None or args.inventory or args.list is not None \
                or args.added_by is not None or args.text is not None or args.findings is not None:
            parser.error("usage: review --store DIR --agency SLUG --ids ID[,ID...] --set checked|disputed --reviewer NAME"
                         " [--date YYYY-MM-DD] [--note TEXT]")
        args.date = datetime.date.today().isoformat() if args.date is None else args.date
        return review(args)
    if any(v is not None for v in (args.ids, args.set, args.reviewer, args.note)):
        parser.error("--ids, --set, --reviewer and --note belong to review only")
    if args.command[:1] == ["quote-check"]:
        if len(args.command) != 1 or args.check or not args.store or not args.text or args.inventory or args.list is not None \
                or args.added_by is not None or (args.agency is not None and not slugify(args.agency)):
            parser.error("usage: quote-check --store DIR --text EXPORT [--agency SLUG]")
        return quote_check(args, parser)
    if args.text is not None:
        parser.error("--text belongs to quote-check only")
    if args.command[:1] == ["stale"]:
        if len(args.command) != 1 or args.check or not args.store or not valid_date(args.date or "") or args.inventory \
                or args.list is not None or args.added_by is not None or (args.agency is not None and not slugify(args.agency)):
            parser.error("usage: stale --store DIR --date YYYY-MM-DD [--agency SLUG] [--findings FINDINGS.csv]")
        return stale(args)
    if args.findings is not None:
        parser.error("--findings belongs to stale only")
    args.date = datetime.date.today().isoformat() if args.date is None else args.date
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
