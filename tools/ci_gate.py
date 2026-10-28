"""Fail-closed aggregate for unchanged legacy required-check names."""
from __future__ import annotations

import argparse
import json
import os


def accepted(needs: dict, test_job: str) -> bool:
    if not isinstance(needs, dict) or test_job not in needs or "scope" not in needs:
        return False
    scope = needs["scope"]
    if not isinstance(scope, dict) or scope.get("result") != "success":
        return False
    decision = scope.get("outputs", {}).get("run-tests")
    if decision not in ("true", "false"):
        return False
    for name, job in needs.items():
        if not isinstance(job, dict):
            return False
        result = job.get("result")
        if name == test_job and decision == "false":
            if result != "skipped":
                return False
        elif result != "success":
            return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--test-job", required=True)
    args = parser.parse_args()
    needs = json.loads(os.environ["CI_NEEDS"])
    passed = accepted(needs, args.test_job)
    print(json.dumps({"passed": passed, "needs": needs}, sort_keys=True))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
