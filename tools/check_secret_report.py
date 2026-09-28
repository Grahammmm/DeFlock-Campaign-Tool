"""Fail CI on any scanner hit, printing only paths, line numbers and categories."""
import json
from pathlib import Path
import sys


def main():
    data = json.loads(Path(sys.argv[1]).read_text())
    results = data["results"]
    if not isinstance(results, dict):
        raise ValueError("Invalid secret-scan report")
    count = 0
    for path, hits in sorted(results.items()):
        if not isinstance(hits, list):
            raise ValueError("Invalid scan entries")
        for hit in hits:
            print(f"{path}:{hit['line_number']}: {hit['type']}")
            count += 1
    print(f"Potential secrets: {count}; manual privacy review is still required.")
    return int(count > 0)


if __name__ == "__main__":
    raise SystemExit(main())
