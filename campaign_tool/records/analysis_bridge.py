"""Receive DFGL-06A exports into a retained run store and replay them into agency review inputs (DFGL-06, host side).

`records analysis-sync --root R` keeps every received export under `R/.analysis/runs/<run_id>/` (append-only, hash
verified, received in id order) and, on every call, places each run x agency pair that has no final ledger outcome yet
into `R/agencies/<slug>/review/inputs/` (texts, MANIFEST.csv, SOURCES.csv) and rebuilds the review config. It never
writes a fact and never calls a model. See docs/RECORDS-ANALYSIS-LANE.md.
"""
import argparse
import csv
import datetime
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

from .. import law
from . import analysis_config as ac
from . import analysis_export as ae
from . import public_packets as pp

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
                and all(r["state"] != "exported" or ae._sha(files["texts/%s.txt" % r["original_sha256"]]) == r["text_sha256"]
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


def _review(tool, verb, config):
    """-> (exit code, stdout) of one flock_review verb run as a subprocess (the tests count the `run` calls here)."""
    done = subprocess.run([sys.executable, "-B", str(tool), verb, str(config)] + (["--json"] if verb == "status" else []),
                          stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False)
    return done.returncode, done.stdout


def _status(tool, config):
    """-> (flock-review-status-v1 document or None when refused, sha256 of its bytes)."""
    text = _review(tool, "status", config)[1]
    try:
        doc = json.loads(text)
    except ValueError:
        return None, ""
    return (doc if isinstance(doc, dict) and not doc.get("errors") and doc.get("stages") else None), ae._sha(text.encode())


def _key(config, status_sha, config_sha):
    """The UNCHANGED key: status document, config and review state (FACTS.csv then FACT-REVIEWS.csv bytes, `missing` when absent)."""
    facts = Path(config["compare"]["facts"])
    review = b"".join(p.read_bytes() if p.is_file() else b"missing" for p in (facts, facts.with_name("FACT-REVIEWS.csv")))
    return {"status_sha256": status_sha, "config_sha256": config_sha, "review_sha256": ae._sha(review)}


def _tick_one(tool, path, last, dry):
    """-> (outcome, status document or None, key); runs the DAG to its next wait only when the key moved."""
    if not tool.is_file():
        return "ERROR REVIEW_TOOL_MISSING", None, {}
    try:
        text = path.read_bytes()
        config, config_sha = json.loads(text), ae._sha(text)
        doc, sha = _status(tool, path)
        key = _key(config, sha, config_sha)
    except (OSError, ValueError, KeyError, TypeError):
        return "ERROR CONFIG", None, {}
    if doc is None:
        return "ERROR CONFIG", None, {}
    if doc["locked"] == "yes":
        return "ERROR LOCKED", doc, key
    if last and last["outcome"][:5] != "ERROR" and all(last.get(k) == v for k, v in key.items()):
        return "UNCHANGED", doc, key
    if dry:
        return "WOULD_RUN", doc, key
    before = doc["next"]
    code = _review(tool, "run", path)[0]
    doc, sha = _status(tool, path)
    if doc is None:
        return "ERROR CONFIG", None, {}
    stage, key = doc["next"], _key(config, sha, config_sha)
    row = next((r for r in doc["stages"] if r["id"] == stage), {})
    failed = next((r["id"] for r in doc["stages"] if r["state"] == "failed"), stage)
    if code == 1:
        return ("ERROR LOCKED" if doc["locked"] == "yes" else "ERROR FAILED %s" % failed), doc, key
    return ("DONE" if stage is None else "WAITING " + stage if row.get("kind") == "wait" else "ADVANCED %s→%s" % (before, stage)), doc, key


def _entry(root, tool, slug, path, doc, key):
    """One needs-model.json entry for an agency whose next stage is a wait (ids, families and paths only)."""
    c, stage, rel = tool.context(path), doc["next"], lambda p: str(Path(p).relative_to(root)) if tool.under(Path(p), root) else str(p)
    marks = {r["id"]: (r["mark"] or {}).get("provider") for r in doc["stages"]}
    files = [rel(p) for p in tool.wait_files(c, stage)] or [rel(c["FO"] / "drafts" / "*.yaml")] * (stage == "fact-drafts")
    try:
        agency = json.loads((root / "profiles" / (slug + ".json")).read_text(encoding="utf-8")).get("records_agency_id")
    except (OSError, ValueError, AttributeError):
        agency = None
    row = next(r for r in doc["stages"] if r["id"] == stage)
    return {"agency": slug, "records_agency_id": agency, "stage": stage, "role": "verify" if stage in ("verify", "f4") else "author",
            "author_family": marks.get("f3-findings") or marks.get("assess"), "verify_family": marks.get("verify") if stage == "f4" else None,
            "input_fp": ae._sha((json.dumps(row, sort_keys=True) + key["config_sha256"]).encode()), "wait_files": files, "config": rel(path),
            "sources": rel(c["out"] / "inputs" / "SOURCES.csv") if stage == "fact-drafts" else None}


PACKET_SCHEMA, DAYTIME = "records-analysis-packet-v2", re.compile(r"\d{4}-\d{2}-\d{2}T\S*Z")


def _tool(name):
    spec = importlib.util.spec_from_file_location("_analysis_" + name, ac.REPO / "tools" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _table(path):
    try:
        with open(path, newline="", encoding="utf-8") as handle:
            return list(csv.DictReader(handle))
    except (OSError, ValueError, csv.Error):
        return []


def _file_sha(path):
    return ae._sha(path.read_bytes()) if path.is_file() else "missing"


def _law_package(root, agency):
    """-> (retained law_package ref, loaded package or None, code or None) from the newest analysis-ready.json listing the agency."""
    for run in reversed(_runs(root)):
        try:
            ready = json.loads((root / ".analysis" / "runs" / run / "analysis-ready.json").read_text(encoding="utf-8"))
            ref = next(a["law_package"] for a in ready["agencies"] if a["agency_id"] == agency)
            package = law.load_package(ref["id"])
        except (StopIteration, FileNotFoundError):  # a run with no analysis-ready.json (EXPORT analysis_ready=missing) is skipped
            continue
        except (OSError, ValueError, KeyError, TypeError):
            return None, None, "EVIDENCE_LAW_UNAVAILABLE"
        return ref, package, None if package["status"] == ref["status"] else "EVIDENCE_LAW_MISMATCH"
    return None, None, "EVIDENCE_LAW_UNAVAILABLE"


def _first(rows):
    """The earliest-received SOURCES row; every provenance field of a document comes from this one row."""
    return min(rows, key=lambda r: r["received_at"])


def _hold(row, code, ids):
    return {"finding_id": row["finding_id"], "code": code, "ids": sorted(ids)}


def _packet_one(row, found, facts, sources, ref, package, agency, slug, draft):
    """-> (packet entry, None) or (None, (code, ids)); every join is frozen in the spec and nothing is invented."""
    ids = [i for i in found["fact_ids"].split(";") if i]
    for code, bad in (("EVIDENCE_FACT_MISSING", lambda f: f is None), ("EVIDENCE_FACT_UNREVIEWED", lambda f: f["status"] == "unreviewed"),
                      ("EVIDENCE_FACT_DISPUTED", lambda f: f["status"] == "disputed"),
                      ("EVIDENCE_SOURCE_UNKNOWN", lambda f: f["status"] != "checked" or f["kind"] != "record" or f["source_sha256"] not in sources)):
        if not ids or any(bad(facts.get(i)) for i in ids):
            return None, (code, ids)
    docs, cites = {}, []
    for i in ids:
        sha = facts[i]["source_sha256"]
        first = _first(sources[sha])
        if not DAYTIME.fullmatch(first["received_at"]):
            return None, ("EVIDENCE_DATE_UNAVAILABLE", ids)
        docs[sha] = {"sha256": sha, "date_received": first["received_at"][:10], "source_request": first["request_id"], "redaction_ids": [],
                     "use_restriction": False}
        cites.append({"ref": "record %s page %s (%s)" % (sha[:12], facts[i]["page"], i), "verified": True})
    rules = {r["rule_id"]: r for r in package["rules"]}
    rule_ids = [i for i in found["rule_ids"].split(";") if i]
    cited = [i for i in rule_ids if package["status"] == "reviewed" and rules.get(i, {}).get("review") == "verified"]
    cites += [{"ref": rules[i]["citation"], "verified": True} for i in cited]
    requests = {d["source_request"] for d in docs.values()}
    packet = {"kind": "finding", "agency_key": agency, "request_id": requests.pop() if len(requests) == 1 else "",
              "source": "fr-" + ae._sha((slug + "\x1f" + row["finding_id"]).encode())[:24], "title": found["topic"],
              "text": found["claim"] + ("\n\n" + draft + ". The %s law package is draft and unreviewed." % ref["id"] if package["status"] == "draft" else ""),
              "documents": sorted(docs.values(), key=lambda d: d["sha256"]), "citations": cites, "finding_state": pp.PUBLIC_STATE}
    if "\n" in packet["title"] or len(packet["title"]) > pp.MAX_TITLE:
        return None, ("EVIDENCE_TITLE_INVALID", ids)
    if not isinstance(agency, str) or pp.KEY.fullmatch(agency) is None:
        return None, ("EVIDENCE_AGENCY_INVALID", ids)
    try:
        pp.clean(packet)
    except pp.PacketError as exc:
        return None, ("EVIDENCE_REFUSED " + exc.code, ids)
    return {"packet": packet, "provenance": {
        "finding_id": row["finding_id"], "fact_ids": ids, "rule_ids": cited, "withheld_rule_ids": [i for i in rule_ids if i not in cited],
        "withheld_policy_refs": [i for i in found["policy_refs"].split(";") if i],
        "documents": [{"sha256": d["sha256"], "run_id": f["run_id"], "received_at": f["received_at"], "received_at_source": f["received_at_source"]}
                      for d in packet["documents"] for f in [_first(sources[d["sha256"]])]]}}, None


def _packets(root, slug, config):
    """-> (outcome line, ledger fields) for one DONE agency: the PUBLIC-OK findings as finding packets in the outbox (DFGL-07g)."""
    review, facts_tool = ac._flock_review(), _tool("flock_facts")
    out, store = Path(config["review"]["out"]), Path(config["facts_intake"]["fact_store"])
    run = Path(config["compare"]["f3"]["store"]) / "agencies" / slug / "runs" / config["review"]["run_date"]
    folder = store / "agencies" / slug
    try:
        agency = json.loads((root / "profiles" / (slug + ".json")).read_text(encoding="utf-8")).get("records_agency_id")
    except (OSError, ValueError, AttributeError):
        agency = None
    ok, findings = _table(run / "PUBLIC-OK.csv"), {r["finding_id"]: r for r in _table(run / "FINDINGS.csv")}
    with facts_tool.store_lock(store, exclusive=False):  # one shared snapshot: the validated view and the hashes recorded for it
        rows, _, codes = facts_tool.read_reviewed(store, slug)
        fact_hashes = (_file_sha(folder / "FACTS.csv"), _file_sha(folder / "FACT-REVIEWS.csv"))
    facts, sources = {r["fact_id"]: r for r in rows}, {}
    for r in _table(out / "inputs" / "SOURCES.csv"):
        sources.setdefault(r["original_sha256"], []).append(r)
    ref, package, law_code = _law_package(root, agency)
    packets, held = [], []
    for row in ok:
        found = findings.get(row["finding_id"])
        if found is None:
            held.append(_hold(row, "EVIDENCE_FINDING_MISSING", []))
        elif codes or law_code:
            held.append(_hold(row, "EVIDENCE_" + codes[0] if codes else law_code, found["fact_ids"].split(";")))
        else:
            entry, bad = _packet_one(row, found, facts, sources, ref, package, agency, slug, _tool("flock_compare").DRAFT)
            packets.append(entry) if entry else held.append(_hold(row, *bad))
    inputs = {"public_ok": _file_sha(run / "PUBLIC-OK.csv"), "findings": _file_sha(run / "FINDINGS.csv"), "facts": fact_hashes[0],
              "fact_reviews": fact_hashes[1], "sources": _file_sha(out / "inputs" / "SOURCES.csv")}
    inputs["law_package"] = _file_sha(law.package_path(ref["id"])) if ref else "missing"
    body = {"schema": PACKET_SCHEMA, "agency_id": agency, "slug": slug, "inputs": inputs, "packets": packets, "held": held}
    key = ae._sha(json.dumps(body, sort_keys=True).encode())
    counts = {}
    for h in held:
        counts[h["code"]] = counts.get(h["code"], 0) + 1
    summary = "n=%d held=%d%s" % (len(packets), len(held), "".join(" %s=%d" % kv for kv in sorted(counts.items())))
    name = "%s-%s.json" % (slug, key[:12])
    last = next((e.get("key") for e in reversed(_ledger(root)) if e.get("kind") == "packet" and e.get("agency") == slug and e.get("outcome") == "PACKET_STAGED"), None)
    outcome = "PACKET_NOTHING_OK" if not ok else "PACKET_ALL_HELD" if not packets else "PACKET_UNCHANGED" if key == last else "PACKET_STAGED"
    if outcome == "PACKET_STAGED":
        return outcome, key, name, summary, (review, root / ".analysis" / "packet-outbox" / name, (json.dumps(dict(body, key=key), indent=1, sort_keys=True) + "\n").encode())
    return outcome, key, name, summary, None


def tick_main(argv=None):
    parser = argparse.ArgumentParser(prog="records analysis-tick", description="Advance every agency review DAG to its next model wait (DFGL-07f).")
    parser.add_argument("--root", required=True)
    parser.add_argument("--max-agencies", type=int, default=20)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    root = Path(args.root).resolve()
    if not (root / ".analysis" / "LANE-ON").exists():
        print("ANALYSIS_LANE_OFF")
        return 0
    if not root.is_dir() or root == ac.REPO or ac.REPO in root.parents or ac._store_like(root):
        print("ROOT_REFUSED")
        return 1
    os.umask(0o077)
    tool, last, entries = ac.REPO / "tools" / "flock_review.py", {}, []
    review = ac._flock_review() if tool.is_file() else None
    for e in _ledger(root):
        if e.get("kind") == "tick":
            last[e["agency"]] = e
    counts, hard = dict.fromkeys("AWDE", 0), False
    for path in sorted(root.glob("agencies/*/review-config.json"))[:max(args.max_agencies, 0)]:
        slug = path.parent.name
        outcome, doc, key = _tick_one(tool, path, last.get(slug), args.dry_run)
        row = next((r for r in doc["stages"] if r["id"] == doc["next"]), {}) if doc else {}
        waiting = row.get("kind") == "wait"
        if doc and waiting:
            entries.append(_entry(root, review, slug, path, doc, key))
        counts["E" if outcome[:5] == "ERROR" else "A" if outcome[:8] == "ADVANCED" else "W" if waiting else "D" if doc and not doc["next"] else "A"] += 1
        hard = hard or (outcome[:5] == "ERROR" and outcome != "ERROR LOCKED")
        if not args.dry_run:
            _log(root, kind="tick", agency=slug, outcome=outcome, **key)
        state = "" if outcome != "UNCHANGED" else " DONE" if not doc["next"] else " %s%s" % ("WAITING " if waiting else "AT ", doc["next"])
        print("%s %s%s" % (slug, outcome, state))
        if doc and not doc["next"] and outcome[:5] != "ERROR":
            packet, pkey, name, summary, write = _packets(root, slug, json.loads(path.read_text()))
            if write and not args.dry_run:
                write[0].put(write[1], write[2])
            if not args.dry_run:
                _log(root, kind="packet", agency=slug, outcome=packet, key=pkey, summary=summary)
            print("%s %s%s %s" % (slug, packet, " " + name if packet == "PACKET_STAGED" else "", summary))
    if review and not args.dry_run:
        body = {"schema": "records-needs-model-v1", "generated_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "entries": sorted(entries, key=lambda e: e["agency"])}
        review.put(root / ".analysis" / "needs-model.json", (json.dumps(body, indent=1, sort_keys=True) + "\n").encode())
    print("analysis: %(A)d advanced, %(W)d waiting for a model, %(D)d done, %(E)d errors" % counts)
    return 1 if hard else 0


if __name__ == "__main__":
    sys.exit(sync_main())
