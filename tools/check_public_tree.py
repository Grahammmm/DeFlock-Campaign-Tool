"""Compatibility entry point for the packaged offline public-tree scanner."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from campaign_tool.records.public_scan import DENIED_PARTS, DENIED_SUFFIXES, PATTERNS, violations
from campaign_tool.records.public_scan import main as scan_main


def main():
    return scan_main(["--root", str(Path(__file__).resolve().parents[1]), *sys.argv[1:]])


if __name__ == "__main__":
    raise SystemExit(main())
