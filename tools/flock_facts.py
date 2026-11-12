"""Per-agency fact store (schema v2): import v1 FACTS.csv, check, and coverage per posted INVENTORY document. Prints counts only."""
import argparse, contextlib, csv, datetime, fcntl, hashlib, importlib.util, io, json, time, os, re, sys, tempfile, unicodedata
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
V1 = ["agency", "request", "fact_type", "value", "source_sha256", "page", "verbatim_quote"]
V2 = V1 + ["fact_id", "kind", "effective_from", "effective_to", "added_by", "added_at", "status"]
V3 = V2 + ["source_path"]  # original's absolute location recorded with the citation; legacy rows have none
KINDS = {"record", "policy", "law", "public"}
STATUSES = {"unreviewed", "checked", "disputed"}
REVIEW_HEAD = ["fact_id", "from_status", "to_status", "reviewer", "reviewed_at", "note"]  # FACT-REVIEWS.csv v1
REVIEW_HEAD_V2 = REVIEW_HEAD + ["evidence_fp"]  # v2: each row bound to the evidence it reviewed
EVIDENCE_FIELDS = ["agency", "request", "fact_type", "value", "source_sha256", "page", "verbatim_quote", "fact_id", "kind",
                   "effective_from", "effective_to", "added_by", "added_at", "source_path"]  # every reviewed column but status
LOCK_WAIT = 30.0


JOURNAL, STAGED = ".FACTS-TXN.json", (".FACTS.csv.txn", ".FACT-REVIEWS.csv.txn", ".FACTS-TXN.json.tmp")


class Locked(Exception):
    pass


class TxnCorrupt(Exception):
    pass


@contextlib.contextmanager
def store_lock(store, exclusive=True):
    """DFGL-07a: flock on the store folder itself (read-only descriptor), so no lock file exists to list, back up or record."""
    fd, mode, end = os.open(store, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH, time.monotonic() + LOCK_WAIT
    try:
        while True:
            try:
                fcntl.flock(fd, mode | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= end:
                    raise Locked() from None
                time.sleep(0.1)
        yield
    finally:
        os.close(fd)


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
    if not records or records[0] not in (V1, V2, V3) or (folder is not None and records[0] not in (V2, V3)):
        return False, [], ["row 1: header is not an accepted header"]
    v2, rows, errors = records[0] in (V2, V3), [], []
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
    files, said = sorted(Path(target).glob("agencies/*/FACTS.csv")), set()
    return ["file %d: %s" % (n, e) for n, f in enumerate(files, 1)
            for e in check_file(f, f.parent.name) + check_reviews(f.parent / "FACT-REVIEWS.csv", f)
            + link_errors(Path(target), f.parent.name, load(f, f.parent.name)[1], said)] \
        or ([] if files else ["no FACTS.csv files in store"])


def link_errors(store, folder, rows, said=None):
    """Store mode only (DF1537-14): a policy fact cites a doc_sha256 of its agency's POLICY.csv, a law fact a text_sha256 of
    STORE/LAW.csv. Hashes are compared, never printed; a missing, linked or invalid POLICY/LAW file is named, not cascaded."""
    said = set() if said is None else said  # "LAW.csv has errors" is reported once per run
    errors, cited = [], {}
    for number, row in rows:
        if row["kind"] in ("policy", "law") and re.fullmatch(r"[0-9a-f]{64}", row["source_sha256"].strip()):
            cited.setdefault(row["kind"], []).append((number, row["source_sha256"].strip()))
    for kind, path, field in (("policy", store / "agencies" / folder / "POLICY.csv", "doc_sha256"),
                              ("law", store / "LAW.csv", "text_sha256")):
        name, owner = path.name, "agency" if kind == "policy" else "store"
        if not cited.get(kind):
            continue
        if not os.path.lexists(path):
            errors += ["row %d: %s fact but %s has no %s" % (n, kind, owner, name) for n, _ in cited[kind]]
            continue
        bad = "%s is a link or not a file; refusing" % name if os.path.islink(path) or not path.is_file() else None
        if bad is None:
            spec = importlib.util.spec_from_file_location("_facts_law_policy", REPO / "tools" / "flock_law_policy.py")
            spec.loader.exec_module(tool := importlib.util.module_from_spec(spec))  # loaded by path, never a copy
            found, errs = tool.load_policy(path, folder) if kind == "policy" else tool.load_law(path)
            bad = errs and "%s has errors (run flock_law_policy.py --check)" % name
        if bad:
            errors += [] if kind == "law" and "law" in said else [bad]
            said.add(kind)
            continue
        known = {r[field] for _, r in found}
        errors += ["row %d: %s fact source is not a %s" % (n, kind, "POLICY version of this agency" if kind == "policy"
                                                            else "LAW row") for n, h in cited[kind] if h not in known]
    return errors


def read_log(log):
    """(header, records) of a regular FACT-REVIEWS.csv, or None when it is absent or unreadable as CSV."""
    if os.path.islink(log) or not os.path.isfile(log):
        return None
    with open(log, encoding="utf-8-sig", newline="") as handle:
        records = list(csv.reader(handle))
    return (records[0], records[1:]) if records else None


def check_reviews(log, facts):
    """FACT-REVIEWS.csv v1 (DF1537-01) or v2 if present. v1: rows valid, ids in FACTS.csv, last logged to_status == FACTS status.
    v2 (DFGL-07b): a row is bound to the FACTS row with the same (fact_id, evidence_fp) and keeps every v1 rule; an unbound row
    (changed or removed evidence) is history, format-checked only, and never confers or contradicts a status."""
    if os.path.islink(log) or (os.path.lexists(log) and not log.is_file()):
        return ["FACT-REVIEWS.csv: is a symlink or not a file; refusing"]
    if not log.exists():
        return []
    with open(log, encoding="utf-8-sig", newline="") as handle:
        records = list(csv.reader(handle))
    if not records or records[0] not in (REVIEW_HEAD, REVIEW_HEAD_V2):
        return ["reviews row 1: header is not FACT-REVIEWS.csv v1"]
    head = records[0]
    v2, last, errors = head == REVIEW_HEAD_V2, {}, []
    cur = {(r["fact_id"], evidence_fp(r) if v2 else None): r for _, r in load(facts, facts.parent.name)[1]}
    for n, rec in ((n, r) for n, r in enumerate(records[1:], 2) if r):
        if len(rec) != len(head):
            errors.append("reviews row %d: columns have the wrong count" % n)
            continue
        r = dict(zip(head, rec))
        key = (r["fact_id"], r.get("evidence_fp"))
        fact, who = cur.get(key), r["reviewer"].strip().casefold()
        checks = (("fact_id", fact if not v2 else re.fullmatch(r"F-[0-9a-f]{12}", r["fact_id"])),
                  ("evidence_fp", not v2 or re.fullmatch(r"E-[0-9a-f]{16}", key[1])),
                  ("from_status", r["from_status"] in STATUSES and r["from_status"] == last.get(key, r["from_status"])),
                  ("to_status", r["to_status"] in {"checked", "disputed"} - {r["from_status"]}),
                  ("reviewer", who and not (fact and fact["added_by"].strip().casefold() == who)),
                  ("reviewed_at", valid_date(r["reviewed_at"])),
                  ("note", not re.search(r"[\r\n]|.{121}", r["note"])))
        for field, ok in checks:
            if not ok:
                errors.append("reviews row %d: %s %s" % (n, field, "is not in FACTS.csv" if field == "fact_id" and not v2 else "is invalid"))
        last[key] = r["to_status"]
    return errors + ["reviews: %s: FACTS status %s does not match the review log (%s)" % (k[0], cur[k]["status"], s)
                     for k, s in last.items() if k in cur and cur[k]["status"] != s]


def overlay_errors(facts_path, log_path):
    """check_reviews plus every FACTS status other than unreviewed that no bound review chain explains (a raw edit, not a
    review). For the DAG and packet paths; `--check` keeps check_reviews so a manually imported store stays valid."""
    log, got = Path(log_path), read_log(log_path)
    v2 = bool(got) and got[0] == REVIEW_HEAD_V2
    bound = {(rec[0], rec[-1] if v2 else None) for rec in (got[1] if got and got[0] in (REVIEW_HEAD, REVIEW_HEAD_V2) else []) if len(rec) == len(got[0])}
    return check_reviews(log, Path(facts_path)) + [
        "%s: status %s without a bound review" % (r["fact_id"], r["status"]) for _, r in load(facts_path, Path(facts_path).parent.name)[1]
        if r["status"] != "unreviewed" and (r["fact_id"], evidence_fp(r) if v2 else None) not in bound]


def upgrade_v1(log_rows, facts_rows):
    """v2 log rows (dicts) from consistent v1 log rows: each is stamped with the evidence_fp of the current FACTS row of its
    fact_id. None when a fact_id has no current row; the caller has already required check_reviews to pass."""
    fps = {r["fact_id"]: evidence_fp(r) for r in facts_rows}
    return None if any(r["fact_id"] not in fps for r in log_rows) else [dict(r, evidence_fp=fps[r["fact_id"]]) for r in log_rows]


def evidence_fp(row):
    return "E-" + hashlib.sha256("\x1f".join(row.get(k, "") for k in EVIDENCE_FIELDS).encode()).hexdigest()[:16]


def serialize(rows, header=None, status=None):
    """The exact bytes write_atomic writes; `status` forces every row's status (projection)."""
    header, out = header or (V3 if any("source_path" in r for r in rows) else V2), io.StringIO()
    csv.writer(out, lineterminator="\n", quoting=csv.QUOTE_MINIMAL).writerows(
        [header] + [[status if status and k == "status" else r.get(k, "") for k in header] for r in rows])
    return out.getvalue().encode()


def projection_sha256(path):
    """sha256 of the file's rows serialized with every status unreviewed (status-blind); "missing" or "invalid", never raises."""
    try:
        _, rows, errors = load(path)
        with open(path, encoding="utf-8-sig", newline="") as handle:
            header = next(csv.reader(handle), [])
    except FileNotFoundError:
        return "missing"
    except (OSError, UnicodeDecodeError, csv.Error):
        return "invalid"
    return "invalid" if errors or header not in (V2, V3) else hashlib.sha256(serialize([r for _, r in rows], header, "unreviewed")).hexdigest()


def write_atomic(path, rows):
    for sub in reversed(path.parents[:3]):
        sub.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(serialize(rows))
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError): os.unlink(tmp)
        raise


def _digest(path):
    data = plain_bytes(path)
    return "missing" if data is None else hashlib.sha256(data).hexdigest()


def _put(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as out:
        out.write(data)
        out.flush()
        os.fsync(out.fileno())


def _sync_dir(folder):
    fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def txn_write(store, slug, facts_rows, log_bytes):
    """DFGL-07c: FACTS.csv and FACT-REVIEWS.csv change as one journaled transaction. Call inside store_lock(store). The journal
    replace is the commit point; recover() rolls a committed transaction forward, and no journal means the old pair is intact."""
    folder, new = store / "agencies" / slug, {"facts": serialize(facts_rows), "log": log_bytes}
    names, doc = {"facts": "FACTS.csv", "log": "FACT-REVIEWS.csv"}, {"schema": "flock-facts-txn-v1", "agency": slug}
    try:
        for key, name in names.items():
            _put(folder / STAGED[key == "log"], new[key])
            doc[key] = {"old": _digest(folder / name), "new": hashlib.sha256(new[key]).hexdigest()}
        _put(folder / STAGED[2], json.dumps(doc).encode())
        os.replace(folder / STAGED[2], folder / JOURNAL)  # commit point
    except BaseException:  # not committed: the old pair stands, drop this writer's temps
        for name in STAGED:
            with contextlib.suppress(OSError): os.unlink(folder / name)
        raise
    _sync_dir(folder)
    os.replace(folder / STAGED[1], folder / names["log"])
    os.replace(folder / STAGED[0], folder / names["facts"])
    _sync_dir(folder)
    os.unlink(folder / JOURNAL)
    _sync_dir(folder)


def recover(store, slug):
    """Inside store_lock(store): finish a committed transaction (per file: current == new is done; else the staged file with
    hash new is replaced in) or drop the writer's own uncommitted temps. TxnCorrupt, nothing touched, when a hash fits neither."""
    folder = store / "agencies" / slug
    if os.path.islink(folder.parent) or os.path.islink(folder) or not folder.is_dir():  # a linked agencies folder is refused untouched
        return
    if os.path.lexists(folder / JOURNAL):
        try:
            doc = json.loads(plain_bytes(folder / JOURNAL))
            todo = []
            assert doc["schema"] == "flock-facts-txn-v1" and doc["agency"] == slug
            for key, name, stage in (("log", "FACT-REVIEWS.csv", STAGED[1]), ("facts", "FACTS.csv", STAGED[0])):
                now, old, new = _digest(folder / name), doc[key]["old"], doc[key]["new"]
                if now != new:
                    assert now == old and _digest(folder / stage) == new
                    todo.append((folder / stage, folder / name))
        except (AssertionError, KeyError, TypeError, ValueError) as exc:
            raise TxnCorrupt() from exc
        for stage, target in todo:
            os.replace(stage, target)
        _sync_dir(folder)
    for name in STAGED:
        if os.path.lexists(folder / name):
            os.unlink(folder / name)
    if os.path.lexists(folder / JOURNAL):
        os.unlink(folder / JOURNAL)
    _sync_dir(folder)


def read_reviewed(store, slug):
    """(rows, log rows, codes) under the shared lock: REVIEW_TXN_PENDING for a journal, REVIEW_STATE_INVALID for a failed check
    or a raw status edit, else the validated view. Never writes and never recovers."""
    folder = Path(store) / "agencies" / slug
    with store_lock(Path(store), exclusive=False):
        if os.path.lexists(folder / JOURNAL):
            return [], [], ["REVIEW_TXN_PENDING"]
        path, log = folder / "FACTS.csv", folder / "FACT-REVIEWS.csv"
        try:
            if os.path.islink(folder) or os.path.islink(path) or not path.is_file() or check_file(path, slug) or overlay_errors(path, log):
                return [], [], ["REVIEW_STATE_INVALID"]
            got = read_log(log)
            return [r for _, r in load(path, slug)[1]], [dict(zip(got[0], rec)) for rec in got[1] if rec] if got else [], []
        except (OSError, UnicodeDecodeError, csv.Error):
            return [], [], ["REVIEW_STATE_INVALID"]


def plan_import(args):
    """Every import check, no writes (RF0740-9): (errors, source rows by slug, store rows by slug)."""
    store = Path(args.store).resolve()
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
    return errors, by_slug, old


def do_import(args, store_lock_held=False):
    store = Path(args.store).resolve()
    if store == REPO or REPO in store.parents:
        print("store inside repository")
        return 1
    errors, by_slug, old = plan_import(args)
    if not errors and not args.dry_run and not store_lock_held:  # plan found nothing wrong: create the store, then plan again under its lock
        store.mkdir(mode=0o700, parents=True, exist_ok=True)
        with store_lock(store):
            for folder in sorted(store.glob("agencies/*")):  # DFGL-07c: finish a crashed review before the locked re-plan reads the store
                if any(os.path.lexists(folder / n) for n in (JOURNAL, *STAGED)):
                    recover(store, folder.name)  # recover() itself leaves a linked agencies folder alone
            return do_import(args, store_lock_held=True)
    return import_planned(args, errors, by_slug, old)


def import_planned(args, errors, by_slug, old):
    if errors:
        print("\n".join(errors))
        print("imported=0 duplicates=0 errors=%d" % len(errors))
        return 1
    store = Path(args.store).resolve()
    fresh = {}  # per slug: source rows not in the store and not repeated earlier in the source
    for slug, new_rows in by_slug.items():
        seen, fresh[slug] = {dedupe_key(r) for r in old[slug]}, []
        for row in new_rows:
            if dedupe_key(row) not in seen:
                fresh[slug].append(row)
                seen.add(dedupe_key(row))
    imported = sum(len(rows) for rows in fresh.values())
    duplicates = sum(len(rows) for rows in by_slug.values()) - imported
    if args.dry_run:  # slug and counts only; returns before any write_atomic, so no folder or file is made
        for slug in sorted(fresh):
            print("would-import | %s | new=%d duplicates=%d" % (slug, len(fresh[slug]), len(by_slug[slug]) - len(fresh[slug])))
        print("imported=0 would_import=%d duplicates=%d errors=0 agencies=%d dry_run=1" % (imported, duplicates, len(by_slug)))
        return 0
    for slug, rows in fresh.items():
        if rows:
            write_atomic(store / "agencies" / slug / "FACTS.csv", old[slug] + rows)
    print("imported=%d duplicates=%d errors=0 agencies=%d" % (imported, duplicates, len(by_slug)))
    return 0


def coverage_data(args):
    """(refusal or None, data) for coverage (RF0740-11): flock-facts-coverage-v1; the sha256 joins only, never shown."""
    slug = slugify(args.agency)
    path = Path(args.store) / "agencies" / slug / "FACTS.csv"
    if not path.is_file():
        return "facts: %s has no FACTS.csv" % slug, {}
    errors = check_file(path, slug)
    if errors:
        return "facts: %d errors (run --check)" % len(errors), {}
    with open(args.inventory, encoding="utf-8-sig", newline="") as handle:
        records = list(csv.reader(handle))
    head = records[0] if records else []
    lacks = [c for c in ("request", "doc_id", "sha256", "analysis_only") if c not in head]
    if lacks:
        return "inventory: header lacks %s" % ", ".join(lacks), {}
    docs, facts = {}, {}
    for number, rec in ((n, r) for n, r in enumerate(records[1:], 2) if r):
        if len(rec) != len(head):
            return "inventory: row %d: columns have the wrong count" % number, {}
        docs.setdefault((row := dict(zip(head, rec)))["sha256"].strip(), []).append(row)
    for _, row in load(path, slug)[1]:
        facts.setdefault(row["source_sha256"].strip(), []).append(row)
    withheld = {sha for sha, rows in docs.items() if any(r["analysis_only"] != "no" for r in rows)}
    posted = sorted((rows[0]["request"].strip(), rows[0]["doc_id"].strip(), sha) for sha, rows in docs.items() if sha not in withheld)
    covered = sum(sha in facts for _, _, sha in posted)
    documents = [{"request": request or "-", "doc_id": doc_id, "facts": len(got), "pages": len({r["page"].strip() for r in got}),
                  "unreadable": sum(r["value"].strip() == "UNREADABLE" for r in got)}
                 for request, doc_id, got in ((q, d, facts.get(s, [])) for q, d, s in posted)]
    totals = {"documents": len(docs), "posted": len(posted), "covered": covered, "uncovered": len(posted) - covered,
              "withheld": len(withheld), "facts_on_withheld": sum(len(facts.get(s, ())) for s in withheld),
              "orphan_facts": sum(len(v) for s, v in facts.items() if s not in docs)}
    return None, {"schema": "flock-facts-coverage-v1", "agency": slug, "documents": documents, "totals": totals,
                  "note": "fact rows per document, not a review verdict"}


def coverage(args):
    refusal, data = coverage_data(args)
    if refusal:
        return print(refusal) or 1
    if args.json:
        return print(json.dumps(data, indent=2, sort_keys=False, ensure_ascii=True)) or 0
    for d in data["documents"]:
        print("coverage | %s | document %s | facts=%d pages=%d unreadable=%d" % tuple(d.values()))
    print(" ".join("%s=%d" % kv for kv in data["totals"].items()) + " | " + data["note"])
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


def status_data(args):
    """(errors, data) for status (RF0740-10): flock-facts-status-v1 counts, listed ids and totals; never a value, quote or hash."""
    files, errors = store_files(Path(args.store), slugify(args.agency or ""))
    errors = errors or ["file %d: %s" % (n, e) for n, f in enumerate(files, 1) for e in check_file(f, f.parent.name)]
    if errors or not files:
        return errors or ["no FACTS.csv files in store"], {}
    agencies, listed = [], []
    for f in files:
        rows, count = load(f, f.parent.name)[1], {}
        for _, row in rows:
            for key in (row["kind"], row["status"]) + ("unreadable",) * (row["value"].strip() == "UNREADABLE"):
                count[key] = count.get(key, 0) + 1
        agencies.append({"agency": f.parent.name, "rows": len(rows), "kinds": {k: count.get(k, 0) for k in (
            "record", "policy", "law", "public")}, "statuses": {k: count.get(k, 0) for k in ("unreviewed", "checked", "disputed")},
            "unreadable": count.get("unreadable", 0)})
        listed += [{"agency": f.parent.name, "fact_id": r["fact_id"], "kind": r["kind"], "fact_type": r["fact_type"],
                    "page": int(r["page"].strip()), "row": n} for n, r in rows if r["status"] == args.list]
    totals = {"agencies": len(files), "rows": sum(a["rows"] for a in agencies)}
    totals.update((k, sum(a["statuses"][k] for a in agencies)) for k in ("unreviewed", "disputed"))
    return [], {"schema": "flock-facts-status-v1", "agencies": agencies, "listed": listed, "totals": totals, "list": args.list}


def status(args):
    """Review state per agency (RF1220-2): counts by kind and status, and ids to review. Read-only; never a value or quote."""
    errors, data = status_data(args)
    if errors:
        return fail(errors)
    if args.json:
        return print(json.dumps(data, indent=2, sort_keys=False, ensure_ascii=True)) or 0
    for a in data["agencies"]:
        print(" | ".join([a["agency"], "rows=%d" % a["rows"]] + [" ".join("%s=%d" % kv for kv in group.items()) for group in (
            a["kinds"], a["statuses"], {"unreadable": a["unreadable"]})]))
    for r in data["listed"]:
        print("%s | %s | %s | %s | page %d | row %d" % (r["agency"], r["fact_id"], r["kind"], r["fact_type"], r["page"], r["row"]))
    print("agencies=%d rows=%d unreviewed=%d disputed=%d" % tuple(data["totals"].values()))
    return 0


def unreadable(args):
    """DF1537-15: private CSV of distinct (agency, source_sha256, page) with UNREADABLE fact rows, for the extraction owner.
    New file only (0600, temp + link), outside the repository and STORE/agencies; stdout has counts, never a hash or page."""
    out, store = Path(args.out), Path(args.store)
    where, agencies = out.parent.resolve() / out.name, store.resolve() / "agencies"
    refuse = ["--out: is inside the repository"] * (where == REPO or REPO in where.parents) + [
        "--out: is inside the store's agencies folder"] * (where == agencies or agencies in where.parents)
    refuse = refuse or ["--out: exists or is a link; not overwritten"] * os.path.lexists(out) or [
        "--out: parent folder is missing"] * (not out.parent.is_dir())
    files, errors = ([], refuse) if refuse else store_files(store, slugify(args.agency or ""))
    bad = 0 if errors else sum(len(check_file(f, f.parent.name)) for f in files)
    if errors or bad or not files:
        return fail(errors or ["facts: %d errors (run --check)" % bad] * bool(bad) or ["no FACTS.csv files in store"])
    pages = {}
    for f in files:
        for _, r in load(f, f.parent.name)[1]:
            if r["value"].strip() == "UNREADABLE":
                key = (f.parent.name, r["source_sha256"].strip(), int(r["page"].strip()))
                pages[key] = pages.get(key, 0) + 1
    text = io.StringIO()
    csv.writer(text, lineterminator="\n").writerows([["agency", "source_sha256", "page", "fact_rows"]]
                                                    + [[*key, n] for key, n in sorted(pages.items())])
    fd, tmp = tempfile.mkstemp(prefix=".unreadable.", suffix=".tmp", dir=out.parent)  # mode 0600
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text.getvalue())
        os.link(tmp, out)  # fails rather than overwrite a file that appeared meanwhile
    finally:
        with contextlib.suppress(OSError): os.unlink(tmp)
    print("unreadable | agencies=%d pages=%d documents=%d rows=%d written=%s | hand-off for the extraction owner;"
          " not a review verdict" % (len(files), len(pages), len({k[:2] for k in pages}), sum(pages.values()), out.name))
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
    store = Path(args.store).resolve()
    if store.is_dir() and store != REPO and REPO not in store.parents:  # a missing store keeps the unlocked error path
        with store_lock(store):
            recover(store, slugify(args.agency))
            return review_locked(args)
    return review_locked(args)


def review_locked(args):
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
    changed = [[i, by_id[i]["status"], args.set, who, date, args.note or "", evidence_fp(by_id[i])] for i in ids if by_id[i]["status"] != args.set]
    if changed:
        for i, *_ in changed:
            by_id[i]["status"] = args.set
        old, add = plain_bytes(log), io.StringIO()
        got = read_log(log) if old else None
        v1 = bool(got) and got[0] == REVIEW_HEAD
        if v1:  # a consistent v1 log (checked above) is upgraded in this same write
            up = upgrade_v1([dict(zip(REVIEW_HEAD, rec)) for rec in got[1] if rec], rows)
            csv.writer(add, lineterminator="\n").writerows([REVIEW_HEAD_V2] + [[u[k] for k in REVIEW_HEAD_V2] for u in up])
        csv.writer(add, lineterminator="\n").writerows(([] if old else [REVIEW_HEAD_V2]) + changed)
        txn_write(store, slug, rows, (b"" if v1 else old + b"\n" if old and not old.endswith(b"\n") else old or b"") + add.getvalue().encode())
    print(*lines, "reviewed=%d changed=%d unchanged=%d" % (len(ids), len(changed), len(ids) - len(changed)), sep="\n")
    return 0


def reviews(args):
    """RF0420-9: one agency's FACT-REVIEWS.csv history after the full check `review` runs. Read-only; ids, statuses, dates
    and counts only, never a reviewer, note, value, quote or page. A reversal is a fact changed into both checked and disputed."""
    store, slug = Path(args.store).resolve(), slugify(args.agency)
    if store == REPO or REPO in store.parents:
        return fail(["store inside repository"])
    files, errors = store_files(store, slug)
    log = store / "agencies" / slug / "FACT-REVIEWS.csv"
    errors = errors or ["file 1: %s" % e for e in check_file(files[0], slug) + check_reviews(log, files[0])]
    if errors:
        return fail(errors)
    if args.fact is not None and args.fact not in {r["fact_id"] for _, r in load(files[0], slug)[1]}:
        return fail(["--fact: unknown fact id"])
    rows = []
    if log.exists():
        with open(log, encoding="utf-8-sig", newline="") as handle:
            recs = list(csv.reader(handle))
            rows = [dict(zip(recs[0], rec)) for rec in recs[1:] if rec]
    rows = [r for r in rows if args.fact in (None, r["fact_id"]) and r["reviewed_at"] >= (args.since or "")]
    into = {s: {r["fact_id"] for r in rows if r["to_status"] == s} for s in ("checked", "disputed")}
    for r in rows:
        print("reviews | %s | %s | %s -> %s | %s" % (slug, r["fact_id"], r["from_status"], r["to_status"], r["reviewed_at"]))
    print("reviews=%d facts=%d to_checked=%d to_disputed=%d reversals=%d" % (len(rows), len({r["fact_id"] for r in rows}), sum(
        r["to_status"] == "checked" for r in rows), sum(r["to_status"] == "disputed" for r in rows), len(into["checked"] & into["disputed"])))
    return 0


def main(argv=None):
    try:
        return _main(argv)
    except Locked:
        print("locked: fact store busy")
        return 1
    except TxnCorrupt:
        print("REVIEW_TXN_CORRUPT")
        return 1
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        print("io: %s" % type(exc).__name__)
        return 1


def _main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="*", help="import SRC | coverage --store DIR --agency NAME --inventory CSV [--json]"
                        " | status --store DIR [--agency SLUG] [--list unreviewed|disputed] [--json]"
                        " | quote-check --store DIR --text EXPORT [--agency SLUG]"
                        " | stale --store DIR --date YYYY-MM-DD [--agency SLUG] [--findings FINDINGS.csv]"
                        " | review --store DIR --agency SLUG --ids ID[,ID...] --set checked|disputed --reviewer NAME"
                        " [--date YYYY-MM-DD] [--note TEXT] | unreadable --store DIR --out FILE.csv [--agency SLUG]"
                        " | reviews --store DIR --agency SLUG [--fact ID] [--since YYYY-MM-DD]")
    for name in ("--check", "--store", "--added-by", "--agency", "--inventory", "--list", "--text", "--findings", "--date",
                 "--ids", "--reviewer", "--note", "--out", "--fact", "--since"):
        parser.add_argument(name)
    parser.add_argument("--kind", default="record", choices=sorted(KINDS))
    parser.add_argument("--status", default="checked", choices=sorted(STATUSES))
    parser.add_argument("--set", choices=["checked", "disputed"])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.json and args.command[:1] not in (["status"], ["coverage"]):
        parser.error("--json belongs to status or coverage")
    if args.dry_run and (args.command[:1] != ["import"] or args.check is not None):
        parser.error("--dry-run belongs to import only")
    if args.out is not None and args.command[:1] != ["unreadable"]:
        parser.error("--out belongs to unreadable only")
    if (args.fact is not None or args.since is not None) and args.command[:1] != ["reviews"]:
        parser.error("--fact and --since belong to reviews only")
    if args.command[:1] == ["reviews"]:
        if len(args.command) != 1 or args.check or not args.store or not slugify(args.agency or "") or any(v is not None for v in (
                args.inventory, args.list, args.added_by, args.date, args.findings, args.text, args.ids, args.set, args.reviewer,
                args.note)) or (args.fact is not None and not re.fullmatch(r"F-[0-9a-f]{12}", args.fact)) \
                or (args.since is not None and not valid_date(args.since)):
            parser.error("usage: reviews --store DIR --agency SLUG [--fact ID] [--since YYYY-MM-DD]")
        return reviews(args)
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
    if args.command[:1] == ["unreadable"]:
        if len(args.command) != 1 or args.check or not args.store or not args.out or args.inventory or args.list is not None \
                or args.added_by is not None or args.date is not None or (args.agency is not None and not slugify(args.agency)):
            parser.error("usage: unreadable --store DIR --out FILE.csv [--agency SLUG]")
        return unreadable(args)
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
        parser.error("usage: import SRC --store DIR --added-by NAME [--date YYYY-MM-DD] [--dry-run] | --check PATH"
                     " | coverage --store DIR --agency NAME --inventory CSV")
    args.src = args.command[1]
    return do_import(args)


if __name__ == "__main__":
    sys.exit(main())
