#!/usr/bin/env python3
"""Synthetic mount data for `rootless_swap.py --rehearse` (stdlib only).

Runs inside a throwaway helper container as root of the rootless daemon's
namespace and fills each DIR (the rehearsal's volume and bind mount) with
synthetic data shaped like the cases earlier swap attempts met:

  - bulk files of pseudo-random bytes, nested directories, a symlink, modes
    0640/0600 and a file owned by namespace uid:gid 1000:1000;
  - a valid WAL-mode SQLite database with -wal/-shm sidecars left in place;
  - an intentionally corrupt SQLite test fixture (the [S2] case), whose
    sha256 is printed so the rehearsal can pin it;
  - a Unix socket left behind by a stopped process (the [S3] case).

Nothing here is campaign data. Prints {"pins": {RELPATH: SHA256}, "bytes": N}.
"""
import argparse
import hashlib
import json
import os
import socket
import sqlite3
import sys
from pathlib import Path

CHUNK = 1 << 20
FIXTURE = "fixtures/db-mutation/ledger.sqlite"
_OPEN = []  # WAL connections kept open until os._exit so -wal/-shm stay on disk


def _bulk(root, budget, seed):
    """Write about budget bytes of deterministic pseudo-random data."""
    written, n = 0, 0
    block = hashlib.sha256(seed.encode()).digest() * (CHUNK // 32)
    while written < budget:
        size = min(budget - written, (8 + n % 24) * CHUNK)
        path = root / "bulk" / f"d{n % 7}" / f"part-{n:04d}.bin"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as handle:
            left = size
            while left > 0:
                piece = block[:min(left, CHUNK)]
                handle.write(piece)
                left -= len(piece)
            # Different bytes per file so archives cannot dedupe or compress away.
            handle.write(n.to_bytes(8, "big"))
        written += size + 8
        n += 1
    return written


def _wal_db(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("pragma journal_mode=wal")
    conn.execute("create table synthetic(n integer, value text)")
    conn.executemany("insert into synthetic values (?, ?)", [(n, f"row {n}") for n in range(500)])
    conn.commit()
    _OPEN.append(conn)


def _malformed(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("create table synthetic(value)")
    conn.executemany("insert into synthetic values (?)", [(n,) for n in range(20)])
    conn.commit()
    conn.close()
    data = bytearray(path.read_bytes())
    data[100] = 0xFF  # page-1 b-tree type byte: "database disk image is malformed"
    path.write_bytes(bytes(data))
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _socket(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(path))
    sock.close()  # the socket file stays, like one left by a stopped app-server


def main(argv=None):
    parser = argparse.ArgumentParser(description="Synthetic mount data for rootless_swap.py --rehearse")
    parser.add_argument("--mib", type=int, required=True, help="total synthetic bytes, in MiB, across all DIRs")
    parser.add_argument("dirs", nargs="+")
    args = parser.parse_args(argv)
    total, pins = 0, {}
    budget = args.mib << 20
    for index, name in enumerate(args.dirs):
        root = Path(name)
        share = budget * 3 // 5 if index == 0 and len(args.dirs) > 1 else budget // max(1, len(args.dirs))
        total += _bulk(root, share, f"{index}")
        notes = root / "state" / "notes.txt"
        notes.parent.mkdir(parents=True, exist_ok=True)
        notes.write_text("synthetic rehearsal data\n")
        os.chmod(notes, 0o640)
        restricted = root / "state" / "mode-0600.bin"
        restricted.write_bytes(b"\0" * 4096)
        os.chmod(restricted, 0o600)
        owned = root / "state" / "owned-by-1000.txt"
        owned.write_text("namespace uid 1000\n")
        os.chown(owned, 1000, 1000)
        os.symlink("notes.txt", root / "state" / "notes.link")
        (root / "empty-dir").mkdir()
        if index == 0:
            _wal_db(root / "state" / "flock.db")
            pins[FIXTURE] = _malformed(root / FIXTURE)
        _socket(root / "tmp" / "app.sock")
    print(json.dumps({"pins": pins, "bytes": total}, sort_keys=True))
    sys.stdout.flush()
    os._exit(0)  # leave the WAL connection unclosed: its -wal/-shm sidecars stay


if __name__ == "__main__":
    main()
