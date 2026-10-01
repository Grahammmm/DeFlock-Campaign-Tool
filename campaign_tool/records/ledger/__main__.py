"""Private candidate ledger initialization, import and evidence-backed status."""
import argparse
import json
from .store import import_legacy, initialize
from .stages import query_counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    init.add_argument("--database", required=True)
    status = sub.add_parser("status")
    status.add_argument("--database", required=True)
    count = sub.add_parser("counts")
    count.add_argument("--database", required=True)
    imp = sub.add_parser("import-legacy")
    imp.add_argument("--snapshot", required=True)
    imp.add_argument("--database", required=True)
    imp.add_argument("--batch-size", type=int, default=1000)
    imp.add_argument("--max-batches", type=int)
    args = parser.parse_args()
    try:
        if args.command == "init":
            result = initialize(args.database)
        elif args.command in ("counts", "status"):
            result = query_counts(args.database)
        else:
            result = import_legacy(args.snapshot, args.database, batch_size=args.batch_size, max_batches=args.max_batches)
    except (ValueError, OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        parser.exit(1, "Ledger operation failed (" + type(exc).__name__ + "); private diagnostic required.\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
