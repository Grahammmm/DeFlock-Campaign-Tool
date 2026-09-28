"""Offline records gates. Intake and scheduled pipeline commands follow in M0b-M5."""
import argparse
import importlib
import os
import sys

COMMANDS = {
    "validate-findings": "validate_findings",
    "reconcile-coverage": "reconcile_coverage",
    "collect-reviews": "collect_reviews",
    "batch-report": "batch_report",
}


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=COMMANDS)
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    module = importlib.import_module(".gates." + COMMANDS[args.command], __package__)
    sys.argv = [sys.argv[0] + " " + args.command, *args.arguments]
    return module.main()


if __name__ == "__main__":
    raise SystemExit(main())
