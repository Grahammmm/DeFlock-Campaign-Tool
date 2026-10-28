"""Shard the exact unittest discovery population without filtering test cases.

Modules stay together, including class/module fixtures and load_tests results.
Every shard discovers the whole tree, checks partition multiplicity, then runs
its share. Import failures are fatal even if they belong to another shard.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]


def flatten(suite):
    if isinstance(suite, unittest.TestSuite):
        if type(suite) is not unittest.TestSuite:
            raise ValueError("Custom suite semantics must be reviewed before sharding")
        for child in suite:
            yield from flatten(child)
    else:
        yield suite


def partition(cases, count):
    if count < 1:
        raise ValueError("shard count must be positive")
    modules = defaultdict(list)
    for case in cases:
        modules[type(case).__module__].append(case)
    shards = [[] for _ in range(count)]
    # Stable, deterministic largest-module-first balancing. No hardcoded list
    # of modules, exclusions or counts that could lose newly discovered tests.
    for _, members in sorted(modules.items(), key=lambda item: (-len(item[1]), item[0])):
        index = min(range(count), key=lambda i: (len(shards[i]), i))
        shards[index].extend(members)
    original = Counter(case.id() for case in cases)
    assigned = Counter(case.id() for shard in shards for case in shard)
    if original != assigned:
        raise ValueError("shards do not preserve the discovery multiset")
    return shards


def discover(root=ROOT):
    sys.path.insert(0, str(root))
    loader = unittest.TestLoader()
    # Same default discovery contract as `python -m unittest discover -v`.
    suite = loader.discover(str(root), pattern="test*.py", top_level_dir=str(root))
    if loader.errors:
        raise RuntimeError("\n".join(str(error) for error in loader.errors))
    cases = list(flatten(suite))
    if not cases:
        raise ValueError("empty discovery is not acceptance")
    return cases


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shard", type=int, required=True)
    parser.add_argument("--shards", type=int, default=4)
    parser.add_argument("--collect-only", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if not 0 <= args.shard < args.shards:
        parser.error("shard must be in [0, shards)")
    cases = discover()
    shards = partition(cases, args.shards)
    selected = shards[args.shard]
    if not selected:
        raise ValueError("empty shard is not acceptance")
    all_ids = sorted(case.id() for case in cases)
    report = {"discovered": len(cases), "shard": args.shard, "shards": args.shards,
              "counts": [len(shard) for shard in shards],
              "population_sha256": hashlib.sha256(json.dumps(all_ids).encode()).hexdigest(),
              "selected_ids": [case.id() for case in selected]}
    print(json.dumps({key: value for key, value in report.items() if key != "selected_ids"}), flush=True)
    status = 0
    if not args.collect_only:
        start = time.monotonic()
        result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(selected))
        report.update(seconds=round(time.monotonic() - start, 3), ran=result.testsRun,
                      skipped=[(case.id(), reason) for case, reason in result.skipped],
                      failures=len(result.failures), errors=len(result.errors))
        status = 0 if result.wasSuccessful() else 1
        # Existing discovery permits opt-in integration skips. The separate
        # strict zero-skip integration workflows remain unchanged and required.
    if args.report:
        args.report.write_text(json.dumps(report, indent=2) + "\n")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
