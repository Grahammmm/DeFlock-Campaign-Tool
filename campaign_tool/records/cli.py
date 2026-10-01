"""Records engine command line; services remain explicit and separately enabled."""
import argparse
import importlib
import os
import sys

COMMANDS = {
    "validate-findings": "gates.validate_findings",
    "reconcile-coverage": "gates.reconcile_coverage",
    "collect-reviews": "gates.collect_reviews",
    "batch-report": "gates.batch_report",
    "version": "release_manifest",
    "scan-public": "public_scan",
}


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=COMMANDS)
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    module = importlib.import_module("." + COMMANDS[args.command], __package__)
    sys.argv = [sys.argv[0] + " " + args.command, *args.arguments]
    return module.main()
