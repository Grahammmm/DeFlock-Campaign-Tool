"""DFGL-07h: import host outbox files as pending finding packets (container side; public_packets.propose only)."""
import argparse
import hashlib
import json
import os
import re
import sqlite3
import stat
import sys
import tarfile
from pathlib import Path

from . import analysis_export as ae
from . import public_packets as pp
from .outcomes import received_items as ri
from .run_stages import LedgerBusy, snapshot_ro

SCHEMA, MEMBER, LIMIT, RECEIPT = "records-analysis-packet-v2", re.compile(r"[a-z0-9-]+-[0-9a-f]{12}\.json"), 1 << 20, "analysis-packets-imported.jsonl"
CITE = re.compile(r"record ([0-9a-f]{12}) page ([1-9][0-9]{0,8}) \(([^()\s]+)\)", re.ASCII)  # the one shape analysis_bridge writes


def _members(args):
    """Yield (name, bytes or None); None marks a member that is not a bounded regular file of the right name."""
    if args.from_tar:
        with tarfile.open(fileobj=sys.stdin.buffer, mode="r|") as tar:
            for m in tar:
                body = tar.extractfile(m) if m.isreg() and m.size <= LIMIT and MEMBER.fullmatch(m.name) else None
                yield m.name, body.read() if body else None
        return
    for name in sorted(os.listdir(args.source)):
        path, info = os.path.join(args.source, name), os.lstat(os.path.join(args.source, name))
        if not stat.S_ISDIR(info.st_mode):
            yield name, pp._read(path, LIMIT) if stat.S_ISREG(info.st_mode) and MEMBER.fullmatch(name) else None


def _body(name, data):
    """-> the verified outbox body, or None (IMPORT_INVALID): schema, recomputed key, file name and shape."""
    try:
        body = json.loads(data.decode("utf-8"))
        key = body.pop("key")
        good = (body["schema"] == SCHEMA and isinstance(key, str) and name == "%s-%s.json" % (body["slug"], key[:12])
                and key == hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest() and isinstance(body["agency_id"], str))
        for entry in body["packets"]:
            docs, prov, cites = entry["packet"]["documents"], entry["provenance"]["documents"], entry["packet"].get("citations") or []
            good = good and isinstance(entry["packet"]["source"], str) and isinstance(cites, list) and isinstance(docs, list) and isinstance(prov, list) and len(docs) == len(prov) and all(
                isinstance(d, dict) and isinstance(d.get("sha256"), str) for d in docs + prov) and all(isinstance(d.get("run_id"), str) for d in prov)
    except (ValueError, UnicodeError, KeyError, TypeError, AttributeError):
        return None
    return dict(body, key=key) if good else None


def _items(project, run, cache):
    """The live project's received items of one run (read-only; written once per run); [] when absent or unreadable."""
    if run not in cache:
        try:
            raw = pp._read(os.path.join(project, "run-outputs", run, "outcomes", "received-items.json"), 64 << 20) if ae.RUN.fullmatch(run) else None
            cache[run] = [i for i in json.loads(raw.decode("utf-8"))["items"] if isinstance(i, dict)] if raw else []
        except (pp.PacketError, ValueError, UnicodeError, KeyError, TypeError):
            cache[run] = []
    return cache[run]


def _count(project, sha, cache):
    """-> the page count of one received record from its read-only current extraction, or None when it cannot be established.

    Pages are counted as analysis_export does (distinct usable page numbers) and bound only when they run 1..N with no gap.
    Nothing a packet or an item claims is trusted; LedgerBusy is the caller's retry, never a count.
    """
    key = ("pages", sha)
    if key not in cache:
        processing = Path(os.path.abspath(project)) / "processing-v1"  # snapshot_ro needs an absolute path (Path.as_uri)
        try:
            with snapshot_ro(processing / "ledger.sqlite") as db:
                db.row_factory = sqlite3.Row
                units, diagnostics, _ = ri._current_units(db, processing, sha)
            pages = {u["page"] for u in units}
            cache[key] = len(pages) if units and not diagnostics and pages == set(range(1, len(pages) + 1)) else None
        except LedgerBusy:
            raise
        except (sqlite3.Error, ValueError, OSError, KeyError, TypeError, RecursionError):
            cache[key] = None
    return cache[key]


def _pages(project, packet, cache):
    """-> PACKET_PAGE_OUT_OF_RANGE unless every generated `record <12 hex> page <n> (<fact>)` citation names a page its document has, else None."""
    docs, cites = {d["sha256"] for d in packet["documents"]}, []
    for cite in packet.get("citations") or []:
        ref = cite.get("ref") if isinstance(cite, dict) else None
        if not isinstance(ref, str) or not ref.startswith("record "):
            continue  # a legal citation: opaque, no grammar of its own
        match = CITE.fullmatch(ref)
        full = {s for s in docs if match and s.startswith(match[1])}
        if len(full) != 1:
            return "PACKET_PAGE_OUT_OF_RANGE"
        cites.append((full.pop(), int(match[2])))
    for sha, page in cites:
        count = _count(project, sha, cache)
        if count is None or page > count:
            return "PACKET_PAGE_OUT_OF_RANGE"
    return None


def _check(project, packet, provenance, agency, cache):
    """-> an IMPORT_* or PACKET_* code, or None when every document is one the live project received on that day for that agency and every cited page exists."""
    if packet.get("agency_key") != agency or packet.get("kind") != "finding":
        return "IMPORT_IDENTITY_MISMATCH"
    for doc, prov in zip(packet["documents"], provenance["documents"]):
        found = [i for i in _items(project, prov["run_id"], cache) if i.get("original_sha256") == doc["sha256"] and i.get("agency_id") == agency]
        if doc["sha256"] != prov["sha256"]:
            return "IMPORT_IDENTITY_MISMATCH"
        if not found:
            return "IMPORT_DOCUMENT_UNKNOWN"
        if not any(isinstance(i.get("received_at"), str) and i["received_at"][:10] == doc.get("date_received") for i in found):
            return "IMPORT_DATE_MISMATCH"
    return _pages(project, packet, cache)


def _seen(path):
    try:
        rows = [json.loads(x) for x in (pp._read(path, pp.MAX_JOURNAL) or b"").decode("utf-8").splitlines()]
        return {(r["key"], r["source"]) for r in rows if isinstance(r, dict) and "key" in r and "source" in r}
    except (pp.PacketError, ValueError, UnicodeError):
        return set()


def main(argv=None):
    parser = argparse.ArgumentParser(prog="records analysis-packets-import", description="Import host outbox packets as pending finding packets (DFGL-07h).")
    parser.add_argument("--project", required=True)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--from", dest="source")
    group.add_argument("--from-tar", choices=["-"])
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    os.umask(0o077)
    if not os.path.isdir(args.project) or os.path.islink(args.project):
        print("IMPORT_INVALID project")
        return 1
    receipt, cache, counts, invalid = os.path.join(args.project, RECEIPT), {}, {}, 0
    done = _seen(receipt)
    try:
        for name, data in _members(args):
            body = _body(name, data) if data is not None else None
            invalid += body is None
            for entry in (body or {}).get("packets", ()):
                packet, source = entry["packet"], entry["packet"]["source"]
                try:
                    result = "ALREADY" if (body["key"], source) in done else _check(args.project, packet, entry["provenance"], body["agency_id"], cache)
                except LedgerBusy:
                    print("ledger_busy")
                    return 75
                if result is None:
                    try:
                        proposed = pp.propose(args.project, packet)
                        result = "duplicate" if proposed["result"] == "duplicate" else "proposed r%d" % proposed["revision"]
                    except pp.PacketError as exc:
                        if exc.code == "journal_busy":
                            print("journal_busy")
                            return 75
                        result = "REFUSED " + exc.code
                if result != "ALREADY":
                    fd = os.open(receipt, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
                    with os.fdopen(fd, "w") as out:
                        out.write(json.dumps({"key": body["key"], "source": source, "result": result}, sort_keys=True) + "\n")
                    done.add((body["key"], source))
                counts[result.split(" r")[0]] = counts.get(result.split(" r")[0], 0) + 1
                print(body["key"][:12], result)
            if body is None:
                print("IMPORT_INVALID", name[:40] if MEMBER.fullmatch(name) else "member")
    except (tarfile.TarError, OSError, pp.PacketError):
        print("IMPORT_INVALID input")
        return 1
    print("analysis-packets-import: %s invalid=%d" % (" ".join("%s=%d" % kv for kv in sorted(counts.items())) or "nothing", invalid))
    return 1 if invalid else 0


if __name__ == "__main__":
    sys.exit(main())
