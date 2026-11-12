"""DFGL-07h: import host outbox files as pending finding packets (container side; public_packets.propose only)."""
import argparse
import hashlib
import json
import os
import re
import stat
import sys
import tarfile

from . import analysis_export as ae
from . import public_packets as pp

SCHEMA, MEMBER, LIMIT, RECEIPT = "records-analysis-packet-v2", re.compile(r"[a-z0-9-]+-[0-9a-f]{12}\.json"), 1 << 20, "analysis-packets-imported.jsonl"


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
            docs, prov = entry["packet"]["documents"], entry["provenance"]["documents"]
            good = good and isinstance(entry["packet"]["source"], str) and len(docs) == len(prov) and all(
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


def _check(project, packet, provenance, agency, cache):
    """-> an IMPORT_* code, or None when every document is one the live project received on that day for that agency."""
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
    return None


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
                result = "ALREADY" if (body["key"], source) in done else _check(args.project, packet, entry["provenance"], body["agency_id"], cache)
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
