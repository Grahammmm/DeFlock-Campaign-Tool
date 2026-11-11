"""Read-only export of hash-bound record text per settled run (DFGL-06A, container side).

`records analysis-export` copies, per settled `run_*` run, the DFGL-05 analysis-ready manifest,
a [[PAGE n]]-marked text copy of each ready record named by its original sha256, TEXTS.csv (with
the received item's received_at and source) and a hashed EXPORT.json, as folders or a
deterministic tar on stdout. It never copies originals, mail or approvals and never writes in the
project. Text derivatives are review-required; the export is not a review. See
docs/RECORDS-ANALYSIS-LANE.md.
"""
from __future__ import annotations

import argparse
import contextlib
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import sys
import tarfile

from .. import __version__
from .outcomes import analysis_ready as ar
from .outcomes import received_items as ri
from .run_stages import LedgerBusy, snapshot_ro

SCHEMA = "records-analysis-export-v1"
RUN = re.compile(r"run_[0-9]{8}T[0-9]{6}Z_[0-9a-f]{8}")
READY_STATE = "text_reviewed"  # OCR text not yet visually checked is TEXT_NOT_READY
COLUMNS = ("original_sha256", "agency_id", "request_id", "pages", "text_sha256", "state", "received_at",
           "received_at_source")


class Refusal(Exception):
    def __init__(self, code, status=2):
        super().__init__(code)
        self.code, self.status = code, status


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _raw(directory, name):
    """Bounded no-follow regular-file bytes; None when absent."""
    if directory is None:
        return None
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=directory)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise Refusal("EXPORT_UNSAFE_PATH")
        data = handle.read(ar.MAX_BYTES + 1)
    if len(data) > ar.MAX_BYTES:
        raise Refusal("EXPORT_INPUT_TOO_LARGE")
    return data


def _json(data):
    try:
        value = json.loads(data) if data is not None else None
    except (ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def _valid(manifest, run):
    if not manifest or manifest.get("schema") != ar.SCHEMA or manifest.get("run_id") != run:
        return False
    agencies = manifest.get("agencies")
    return isinstance(agencies, list) and all(
        isinstance(a, dict) and isinstance(a.get("agency_id"), str) and isinstance(a.get("records"), list)
        and all(isinstance(r, dict) and isinstance(r.get("sha256"), str) and ar.HASH.fullmatch(r["sha256"])
                and isinstance(r.get("text_state"), str) for r in a["records"]) for a in agencies)


def _text(db, processing, digest, state):
    """-> (bytes, pages, None) or (None, None, not-ready code). Never partial text."""
    if state != READY_STATE:
        return None, None, "TEXT_NOT_READY"
    try:
        units, diagnostics, _ = ri._current_units(db, processing, digest)
    except (ValueError, OSError):
        return None, None, "EXTRACTION_UNAVAILABLE"
    if diagnostics or not units:
        return None, None, "EXTRACTION_UNAVAILABLE"
    pages = {}
    for unit in sorted(units, key=lambda u: (u["page"], u["ordinal"])):
        pages.setdefault(unit["page"], []).append(unit["text"])
    body = "".join("[[PAGE %d]]\n%s\n" % (n, "\n".join(texts)) for n, texts in sorted(pages.items()))
    return body.encode("utf-8"), len(pages), None


def _run_files(run, outcomes, db, processing):
    """-> (files {relative name: bytes}, analysis_ready, records, exported)."""
    raw = _raw(outcomes, "analysis-ready.json")
    manifest = _json(raw)
    if not _valid(manifest, run):
        raw, manifest = None, None
    files = {}
    rows, exported = [], 0
    if manifest is not None:
        files["analysis-ready.json"] = raw
        received = {}
        for item in (_json(_raw(outcomes, "received-items.json")) or {}).get("items", []):
            if isinstance(item, dict):
                received[(item.get("original_sha256"), item.get("agency_id"), item.get("request_id"))] = item
        texts, seen = {}, set()
        for agency in manifest["agencies"]:
            for record in agency["records"]:
                digest, request = record["sha256"], agency.get("request_id")
                if (digest, agency["agency_id"], request) in seen:
                    continue
                seen.add((digest, agency["agency_id"], request))
                if digest not in texts:
                    texts[digest] = _text(db, processing, digest, record["text_state"])
                data, pages, code = texts[digest]
                item = received.get((digest, agency["agency_id"], request)) or {}
                if data is not None:
                    files["texts/%s.txt" % digest] = data
                    exported += 1
                rows.append((digest, agency["agency_id"], request or "", "" if pages is None else pages,
                             "" if data is None else _sha(data), code or "exported",
                             item.get("received_at") or "", item.get("received_at_source") or ""))
        sink = io.StringIO()
        writer = csv.writer(sink, lineterminator="\n")
        writer.writerow(COLUMNS)
        writer.writerows(sorted(rows))
        files["TEXTS.csv"] = sink.getvalue().encode("utf-8")
    summary = {"schema": SCHEMA, "run_id": run, "analysis_ready": "present" if manifest is not None else "missing",
               "engine": __version__, "files": {name: _sha(data) for name, data in sorted(files.items())}}
    files["EXPORT.json"] = (json.dumps(summary, sort_keys=True, indent=2) + "\n").encode()
    return files, summary["analysis_ready"], len(rows), exported


def _build(args):
    project = Path(os.path.abspath(args.project))
    with contextlib.ExitStack() as stack:
        def opened(parent, name):
            try:
                fd = ar._open_dir(parent, name)
            except OSError:
                raise Refusal("EXPORT_UNSAFE_PATH") from None
            if fd is not None:
                stack.callback(os.close, fd)
            return fd

        root = os.open(project, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        stack.callback(os.close, root)
        processing = opened(root, "processing-v1")
        try:
            ledger = os.stat("ledger.sqlite", dir_fd=processing, follow_symlinks=False) if processing is not None else None
        except FileNotFoundError:
            ledger = None
        if ledger is None or not stat.S_ISREG(ledger.st_mode):
            raise Refusal("EXPORT_NO_LEDGER")
        outputs = opened(root, "run-outputs")
        ids = sorted(n for n in os.listdir(outputs) if RUN.fullmatch(n)) if outputs is not None else []
        outcomes = {}

        def folder(run):
            if run not in outcomes:
                outcomes[run] = opened(opened(outputs, run), "outcomes") if outputs is not None else None
            return outcomes[run]

        def settled(run):
            # A run is finished when a later run exists; the newest only once its manifest is written.
            return run != ids[-1] or _raw(folder(run), "analysis-ready.json") is not None

        if args.run:
            if args.run not in ids:
                raise Refusal("EXPORT_RUN_MISSING")
            if not settled(args.run):
                raise Refusal("EXPORT_RUN_UNSETTLED", 3)
            chosen = [args.run]
        else:
            chosen = []
            for run in ids:
                if args.after != "-" and run <= args.after:
                    continue
                if not settled(run) or len(chosen) >= args.max_runs:
                    break
                chosen.append(run)
        built = []
        if chosen:
            with snapshot_ro(project / "processing-v1" / "ledger.sqlite") as db:
                db.row_factory = sqlite3.Row
                for run in chosen:
                    built.append((run, *_run_files(run, folder(run), db, project / "processing-v1")))
        return built


def _write(base, run, files):
    target = base / run
    if os.path.lexists(target):
        try:
            same = not target.is_symlink() and (target / "EXPORT.json").read_bytes() == files["EXPORT.json"]
        except OSError:
            same = False
        if not same:
            raise Refusal("EXPORT_OUT_EXISTS")
        return  # EXPORT.json hashes every other file, so an identical one is an identical export
    temporary = base / (".%s.tmp" % run)
    if os.path.lexists(temporary):
        shutil.rmtree(temporary)
    for name in sorted(files, key=lambda n: n == "EXPORT.json"):  # EXPORT.json last
        path = temporary / name
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(files[name])
            stream.flush()
            os.fsync(stream.fileno())
    os.chmod(temporary, 0o700)
    os.rename(temporary, target)


def _tar(stream, built):
    with tarfile.open(fileobj=stream, mode="w|", format=tarfile.USTAR_FORMAT) as tar:
        for run, files, *_ in built:
            for name in sorted(files):
                info = tarfile.TarInfo("%s/%s" % (run, name))
                info.size, info.mode, info.mtime, info.uid, info.gid = len(files[name]), 0o600, 0, 0, 0
                tar.addfile(info, io.BytesIO(files[name]))


def main(argv=None):
    parser = argparse.ArgumentParser(prog="records analysis-export", description="Export hash-bound record text for settled runs (read-only).")
    parser.add_argument("--project", required=True)
    pick = parser.add_mutually_exclusive_group(required=True)
    pick.add_argument("--run", help="export this one settled run")
    pick.add_argument("--after", help="export settled runs after this run id ('-' = from the first)")
    parser.add_argument("--max-runs", type=int, default=5)
    sink = parser.add_mutually_exclusive_group(required=True)
    sink.add_argument("--out", help="directory outside the project")
    sink.add_argument("--stdout", action="store_true", help="uncompressed deterministic tar on stdout")
    args = parser.parse_args(argv)
    if not 1 <= args.max_runs <= 100 or not (args.run is None or RUN.fullmatch(args.run)) or not (
            args.after is None or args.after == "-" or RUN.fullmatch(args.after)):
        parser.error("EXPORT_BAD_ARGUMENT")
    base = None
    try:
        if args.out:
            base = Path(os.path.realpath(args.out))
            project = Path(os.path.realpath(args.project))
            if base == project or project in base.parents or base in project.parents:
                raise Refusal("EXPORT_OUT_IN_PROJECT")
            if Path(args.out).is_symlink():
                raise Refusal("EXPORT_UNSAFE_PATH")
        built = _build(args)
        if args.out:
            base.mkdir(mode=0o700, parents=True, exist_ok=True)
            for run, files, *_ in built:
                _write(base, run, files)
        else:
            _tar(sys.stdout.buffer, built)
            sys.stdout.buffer.flush()
    except Refusal as refusal:
        print(refusal.code, file=sys.stderr)
        return refusal.status
    except LedgerBusy:
        print("EXPORT_LEDGER_BUSY", file=sys.stderr)
        return 75
    except (OSError, sqlite3.Error):
        print("EXPORT_UNAVAILABLE", file=sys.stderr)
        return 2
    for run, _, state, records, exported in built:
        print("EXPORT_RUN %s analysis_ready=%s records=%d exported=%d" % (run, state, records, exported),
              file=sys.stderr)
    print("EXPORT_OK runs=%d" % len(built), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
