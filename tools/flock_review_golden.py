#!/usr/bin/env python3
"""Flock review golden: recompute one agency's counts from a finished flock_review run and compare them to a counts-only JSON.
  flock_review_golden.py CONFIG --expect FILE | --write FILE
Counts (facts by kind, requirements, findings by kind x verdict, analysis-only rows, F3 findings and check errors) come from the
stage output files, never from logs. Needs every stage through counts done or skip. Never runs a stage or a model; no network.
--write refuses a path inside the repository or an existing file. Prints keys and counts only.
Exit 0 match/written, 1 mismatch/not done/IO, 2 usage."""
import argparse, csv, importlib.util, json, os, re, sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("_golden_flock_review", TOOLS / "flock_review.py")
fr = spec.loader.exec_module(module := importlib.util.module_from_spec(spec)) or module
NAME, MISSING = re.compile(r"[A-Za-z0-9 _.-]{1,40}"), object()


def rows(path):
    with open(path, encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def tally(items):
    found = {}
    for item in items:
        found[item] = found.get(item, 0) + 1
    return dict(sorted(found.items()))


def counts(c):
    state, dig = fr.load_state(c), {}
    for i in range(fr.IDS.index("counts") + 1):  # read-only: the same cache test `run` applies
        if fr.evaluate(c, state, dig, i)[0] not in ("done", "skip"):
            raise fr.Fail("golden: %s not done" % fr.IDS[i])
    facts, found = rows(c["facts"]), rows(c["CO"] / "FINDINGS.csv")
    golden = fr.REPO / c["golden"] if c["golden"] else None
    kinds = {}
    for row in found:
        kinds.setdefault(row.get("kind", ""), []).append(row.get("verdict", ""))
    return {"schema": 1, "slug": c["slug"], "facts": len(facts), "facts_by_kind": tally(r.get("kind", "") for r in facts),
            "requirements": len(rows(c["RO"] / "REQUIREMENTS.csv")), "findings": len(found),
            "findings_by_kind": {k: tally(v) for k, v in sorted(kinds.items())},
            "analysis_only": sum(r.get("analysis_only", "").strip().lower() == "yes" for r in found),
            "f3_findings": len(rows(c["RUN"] / "FINDINGS.csv")), "f3_errors": len(fr.compare_tool.check(c["RUN"], golden)[0])}


def safe(value):  # counts, slugs and key names only: anything else is never echoed
    if value is MISSING:
        return "missing"
    ok = lambda v: (isinstance(v, int) and not isinstance(v, bool)) or (isinstance(v, str) and fr.SLUG.fullmatch(v)) \
        or (isinstance(v, dict) and all(isinstance(k, str) and NAME.fullmatch(k) and ok(x) for k, x in v.items()))  # noqa: E731
    return json.dumps(value, sort_keys=True) if ok(value) else "invalid"


def compare(actual, path):
    try:
        expected = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise fr.Fail("golden: --expect file cannot be read as JSON") from None
    if not isinstance(expected, dict):
        raise fr.Fail("golden: --expect file is not a JSON object")
    bad = [k for k in sorted(set(expected) | set(actual)) if expected.get(k, MISSING) != actual.get(k, MISSING)]
    for key in bad:
        print("golden: %s expected %s got %s" % (key if NAME.fullmatch(key) else "<key>", safe(expected.get(key, MISSING)),
                                                 safe(actual.get(key, MISSING))))
    if not bad:
        print("golden ok keys=%d" % len(expected))
    return 1 if bad else 0


def write(actual, target):
    path = Path(os.path.abspath(os.path.expanduser(target)))
    if fr.under(path.resolve(), fr.REPO):
        raise fr.Fail("golden: --write path is inside the repository")
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        out.write(json.dumps(actual, indent=1, sort_keys=True) + "\n")
    try:
        os.link(tmp, path)  # atomic and never overwrites
    except FileExistsError:
        raise fr.Fail("golden: --write file already exists") from None
    finally:
        os.unlink(tmp)
    print("golden written keys=%d" % len(actual))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("config", metavar="CONFIG")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--expect", metavar="FILE")
    group.add_argument("--write", metavar="FILE")
    args = parser.parse_args(argv)
    try:
        actual = counts(fr.context(args.config))
        return compare(actual, args.expect) if args.expect else write(actual, args.write)
    except fr.Fail as exc:
        return print(exc) or 1
    except OSError as exc:
        return print("io: %s" % type(exc).__name__) or 1


if __name__ == "__main__":
    sys.exit(main())
