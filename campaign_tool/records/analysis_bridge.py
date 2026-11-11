"""Receive DFGL-06A exports into a retained run store and replay them into agency review inputs (DFGL-06, host side).

`records analysis-sync --root R` keeps every received export under `R/.analysis/runs/<run_id>/` (append-only, hash
verified, received in id order) and, on every call, places each run x agency pair that has no final ledger outcome yet
into `R/agencies/<slug>/review/inputs/` (texts, MANIFEST.csv, SOURCES.csv) and rebuilds the review config. It never
writes a fact and never calls a model. See docs/RECORDS-ANALYSIS-LANE.md.
"""
import argparse
import csv
import datetime
import json
import os
import re
import shutil
import sys
import tarfile
from pathlib import Path

from . import analysis_config as ac
from . import analysis_export as ae

MEMBER = ae.RUN.pattern + r"/(analysis-ready\.json|EXPORT\.json|TEXTS\.csv|texts/[0-9a-f]{64}\.txt)"
MAX = 64 << 20
FINAL = frozenset({"SYNCED", "UNCHANGED", "NO_AGENCIES", "ANALYSIS_READY_MISSING"})
SOURCES = ("source_id", "request_id", "original_sha256", "pages", "text_sha256", "run_id", "received_at", "received_at_source")


def _load(folder):
    """{relative name: bytes} of a folder; None when anything in it is not a bounded regular file."""
    if folder.is_symlink():
        return None
    files = {}
    for path in sorted(folder.rglob("*")):
        if path.is_dir() and not path.is_symlink():
            continue
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX:
            return None
        files[path.relative_to(folder).as_posix()] = path.read_bytes()
    return files


def _read_tar(stream):
    found = {}
    with tarfile.open(fileobj=stream, mode="r|") as tar:
        for member in tar:
            run = member.name.split("/")[0]
            if not ae.RUN.fullmatch(run):
                raise ValueError("member")
            name = member.name[len(run) + 1:]
            if found.get(run, {}) is None:
                continue
            body = tar.extractfile(member) if member.isreg() and member.size <= MAX else None
            if body is None or not re.fullmatch(MEMBER, member.name) or name in found.get(run, {}):
                found[run] = None
            else:
                found.setdefault(run, {})[name] = body.read()
    return found


def _rows(data):
    """TEXTS.csv rows (validated); ValueError on a bad header, agency or hash."""
    if data is None:
        return []
    reader = csv.DictReader(data.decode("utf-8").splitlines())
    rows = list(reader)
    if reader.fieldnames != list(ae.COLUMNS) or any(not re.fullmatch("[0-9a-f]{64}", r["original_sha256"] or "") or not r["agency_id"] for r in rows):
        raise ValueError("TEXTS.csv")
    return rows


def _verify(run, files):
    try:
        meta = json.loads(files["EXPORT.json"])
        listed, state = meta["files"], meta["analysis_ready"]
        rows = _rows(files.get("TEXTS.csv"))
        names = {"analysis-ready.json", "TEXTS.csv"} if state == "present" else set()
        return (meta["schema"] == ae.SCHEMA and meta["run_id"] == run and state in ("present", "missing")
                and set(listed) == set(files) - {"EXPORT.json"} and names <= set(listed)
                and all(ae._sha(files[n]) == d for n, d in listed.items())
                and all(r["state"] != "exported" or ae._sha(files.get("texts/%s.txt" % r["original_sha256"], b"")) == r["text_sha256"]
                        for r in rows))
    except (KeyError, ValueError, TypeError, AttributeError, csv.Error):
        return False


def _runs(root):
    folder = root / ".analysis" / "runs"
    return sorted(p.name for p in folder.iterdir() if ae.RUN.fullmatch(p.name)) if folder.is_dir() else []


def _ledger(root):
    path = root / ".analysis" / "analysis-ledger.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []


def _log(root, **entry):
    path = root / ".analysis" / "analysis-ledger.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    entry["at"] = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600), "a") as handle:
        handle.write(json.dumps(entry, sort_keys=True) + "\n")


def receive(root, found):
    """Retain verified exports in id order; stop at the first invalid one. -> True when nothing was refused."""
    store, cursor, clean = root / ".analysis" / "runs", (_runs(root) or ["-"])[-1], True
    for run, files in sorted(found.items()):
        dest = store / run
        if files is None or not _verify(run, files):
            outcome, clean = "EXPORT_INVALID", False
        elif run <= cursor:
            outcome = "RECEIVED_UNCHANGED" if dest.is_dir() and _load(dest) == files else "EXPORT_CONFLICT"
        else:
            tmp = store / ("." + run + ".tmp")
            if tmp.exists():
                shutil.rmtree(tmp)
            for name, data in files.items():
                (tmp / name).parent.mkdir(parents=True, exist_ok=True)
                (tmp / name).write_bytes(data)
            os.rename(tmp, dest)
            outcome = "RECEIVED"
        clean = clean and outcome != "EXPORT_CONFLICT"
        _log(root, kind="receive", run_id=run, outcome=outcome)
        print("RECEIVE %s %s" % (run, outcome))
        if outcome == "EXPORT_INVALID":
            break
    return clean


def _slug(root, agency):
    """The slug of the profile whose records_agency_id is agency; None when no usable profile file exists."""
    for path in sorted((root / "profiles").glob("*.json")) if (root / "profiles").is_dir() else []:
        try:
            profile = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(profile, dict) and profile.get("records_agency_id") == agency:
            slug = profile.get("slug")
            return slug if isinstance(slug, str) and ac.SLUG.fullmatch(slug) and path.name == slug + ".json" else None
    return None


def _merge_csv(path, header, rows, key):
    """Append rows whose key is not in the file yet; -> number appended."""
    have, fresh = set(), []
    if path.exists():
        with open(path, newline="") as handle:
            have = {tuple(r[c] for c in key) for r in csv.DictReader(handle)}
    for row in rows:
        if tuple(row[c] for c in key) not in have:
            have.add(tuple(row[c] for c in key))
            fresh.append(row)
    if fresh or not path.exists():
        with open(path, "a", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=header, lineterminator="\n")
            if handle.tell() == 0:
                writer.writeheader()
            writer.writerows(fresh)
    return len(fresh)


def _place(root, run, agency, files, rows, dry):
    """-> (outcome, slug, new_texts, config) for one run x agency pair; writes nothing when dry."""
    if not agency:
        missing = json.loads(files["EXPORT.json"])["analysis_ready"] == "missing"
        return ("ANALYSIS_READY_MISSING" if missing else "NO_AGENCIES"), "", 0, ""
    slug = _slug(root, agency)
    if slug is None:
        return "PROFILE_MISSING", "", 0, ""
    mine = [r for r in rows if r["agency_id"] == agency and r["state"] == "exported"]
    inputs = root / "agencies" / slug / "review" / "inputs"
    texts = {r["original_sha256"]: files["texts/%s.txt" % r["original_sha256"]] for r in mine}
    fresh = [sha for sha in sorted(texts) if not (inputs / "records" / (sha + ".txt")).exists()]
    if any((inputs / "records" / (sha + ".txt")).read_bytes() != texts[sha] for sha in set(texts) - set(fresh)):
        return "TEXT_CONFLICT", slug, 0, ""
    if dry:
        return "WOULD_SYNC", slug, len(fresh), ""
    (inputs / "records").mkdir(parents=True, exist_ok=True)
    for sha in fresh:
        (inputs / "records" / (".%s.tmp" % sha)).write_bytes(texts[sha])
        os.replace(inputs / "records" / (".%s.tmp" % sha), inputs / "records" / (sha + ".txt"))
    changed = _merge_csv(inputs / "records" / "MANIFEST.csv", ("file", "orig_sha256"),
                         [{"file": sha + ".txt", "orig_sha256": sha} for sha in sorted(texts)], ("orig_sha256",))
    changed += _merge_csv(inputs / "SOURCES.csv", SOURCES, [
        {"source_id": "REC-" + r["original_sha256"][:12], "request_id": r["request_id"], "original_sha256": r["original_sha256"],
         "pages": r["pages"], "text_sha256": r["text_sha256"], "run_id": run, "received_at": r["received_at"],
         "received_at_source": r["received_at_source"]} for r in mine], ("original_sha256", "run_id"))
    config = ac.write_config(root, slug)
    if config not in ("CONFIG_WRITTEN", "CONFIG_UNCHANGED"):
        return "CONFIG_INVALID", slug, len(fresh), config
    return ("SYNCED" if changed or config == "CONFIG_WRITTEN" else "UNCHANGED"), slug, len(fresh), config


def _pairs(root):
    """[(run, agency, last ledger outcome or None)] for every retained run x agency pair, in order."""
    last = {(e["run_id"], e["agency_id"]): e["outcome"] for e in _ledger(root) if e.get("kind") == "sync"}
    store, pairs = root / ".analysis" / "runs", []
    for run in _runs(root):
        texts = store / run / "TEXTS.csv"
        for agency in sorted({r["agency_id"] for r in _rows(texts.read_bytes() if texts.exists() else None)}) or [""]:
            pairs.append((run, agency, last.get((run, agency))))
    return pairs


def sync(root, dry):
    """Process every pending pair in run-id order. -> number of pairs still pending."""
    loaded, still = {}, 0
    for run, agency, last in _pairs(root):
        if last in FINAL:
            continue
        files = loaded.setdefault(run, _load(root / ".analysis" / "runs" / run))
        outcome, slug, fresh, config = _place(root, run, agency, files, _rows(files.get("TEXTS.csv")), dry)
        if not dry and (outcome in FINAL or outcome != last):
            _log(root, kind="sync", run_id=run, agency_id=agency, slug=slug, outcome=outcome, new_texts=fresh, config=config)
        still += outcome not in FINAL
        print("SYNC %s %s %s new_texts=%d" % (run, agency or "-", outcome, fresh))
    print("SYNC_DONE pending=%d" % still)
    return still


def sync_main(argv=None):
    parser = argparse.ArgumentParser(prog="records analysis-sync", description=__doc__)
    parser.add_argument("--root", required=True)
    parser.add_argument("--receive", action="store_true", help="receive exports from DIR or --tar FILE|- (receipt only)")
    parser.add_argument("dir", nargs="?")
    parser.add_argument("--tar")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--last-received", action="store_true")
    parser.add_argument("--pending", action="store_true")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    modes, source = [args.receive, args.last_received, args.pending], (args.dir is not None) + (args.tar is not None)
    if sum(modes) > 1 or source != int(args.receive) or (args.dry_run and any(modes)):
        parser.error("choose one of --receive (DIR | --tar FILE|-), --last-received, --pending, or none (replay)")
    root = Path(args.root).resolve()
    if (root.exists() and not root.is_dir()) or ac._flock_review().under(root, ac.REPO) or ac._store_like(root):
        print("ROOT_REFUSED")
        return 1
    if args.last_received:
        print((_runs(root) or ["-"])[-1])
    elif args.pending:
        for run, agency, last in _pairs(root):
            if last not in FINAL:
                print(run, agency or "-", last or "NOT_SYNCED")
    elif args.receive:
        os.umask(0o077)
        try:
            if args.dir:
                found = {p.name: _load(p) for p in sorted(Path(args.dir).iterdir()) if ae.RUN.fullmatch(p.name)}
            elif args.tar == "-":
                found = _read_tar(sys.stdin.buffer)
            else:
                with open(args.tar, "rb") as handle:
                    found = _read_tar(handle)
        except (tarfile.TarError, ValueError, OSError):
            print("EXPORT_INVALID")
            return 1
        (root / ".analysis" / "runs").mkdir(parents=True, exist_ok=True)
        return 0 if receive(root, found) else 1
    else:
        os.umask(0o077)
        return 1 if sync(root, args.dry_run) else 0
    return 0


if __name__ == "__main__":
    sys.exit(sync_main())
