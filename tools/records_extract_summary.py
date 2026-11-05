"""Read-only summary of records-extract-outcomes-v1 files (RF0805-4). Usage: [--total | --unique] FILE... | --self-test
One JSON line of counts per FILE; never a document hash, form, accept status, OCR page list, written_at or a path
other than the basename. Writes nothing; wired into no stage. The extract stage (records.native_extract) owns the file.
--total (RF0140-9) adds one last records-extract-summary-total-v1 line of counts and codes over all FILEs.
--unique (RF1110-7) is exclusive: FILEs are listed oldest first, and each original_sha256 counts once, by its last
observation in that caller order (not verified current state). One records-extract-summary-unique-v1 line, counts only,
over the provided files only; the first refused input gives one error line with its 1-based ordinal and exit 1.
"""
import json
import os
import re
import stat
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from campaign_tool.records.native_extract import EXTRACTED, FAILED, OUTCOMES, refusal_code_histogram  # noqa: E402

SCHEMA = "records-extract-outcomes-v1"
MAX_BYTES = 64 * 1024 * 1024


class Refused(Exception):
    """A fixed error code; never carries file content or a path."""


def load(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        raise Refused("not-a-regular-file") from None
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise Refused("not-a-regular-file")
    with os.fdopen(fd, "rb") as handle:
        raw = handle.read(MAX_BYTES + 1)  # bounded read: a file over the limit is refused, never parsed
    if len(raw) > MAX_BYTES:
        raise Refused("too-large")
    try:
        return json.loads(raw)
    except (ValueError, RecursionError):
        raise Refused("not-json") from None


def summarize(data, name):
    """(per-file line, full refusal-code histogram); the line keeps only the top 3 codes."""
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        raise Refused("wrong-schema")
    documents = data.get("documents")
    if not isinstance(documents, list) or not all(isinstance(d, dict) for d in documents):
        raise Refused("bad-documents")
    outcomes = dict.fromkeys(OUTCOMES, 0)
    known = [d["outcome"] for d in documents if isinstance(d.get("outcome"), str) and d["outcome"] in outcomes]
    outcomes.update((code, known.count(code)) for code in set(known))
    histogram = refusal_code_histogram(documents)
    top = sorted(histogram.items(), key=lambda item: (-item[1], item[0]))[:3]
    run_id = data.get("run_id")
    return {"schema": "records-extract-summary-v1", "file": name, "run_id": run_id if isinstance(run_id, str) else None,
            "documents": len(documents), "outcomes": outcomes, "unknown_outcomes": len(documents) - len(known),
            "extracted": outcomes[EXTRACTED], "failed": sum(outcomes[c] for c in FAILED), "top_refusal_codes": [list(t) for t in top]}, histogram


def checked(read, name):
    return checked_full(read, name)[0]


def checked_full(read, name):
    try:
        return summarize(read(), name)
    except Refused as error:
        return {"file": name, "error": str(error)}, {}


def total(results):
    """One line over (line, histogram) pairs: counts and codes only; repeated_run_ids is a count, never the ids."""
    good = [(line, hist) for line, hist in results if "error" not in line]
    outcomes, histogram = Counter(dict.fromkeys(OUTCOMES, 0)), Counter()
    for line, hist in good:
        outcomes.update(line["outcomes"])
        histogram.update(hist)
    runs = Counter(line["run_id"] for line, _ in good if isinstance(line["run_id"], str))
    return {"schema": "records-extract-summary-total-v1", "files": len(results), "summarized_files": len(good),
            "refused_files": dict(Counter(line["error"] for line, _ in results if "error" in line)),
            **{key: sum(line[key] for line, _ in good) for key in ("documents", "unknown_outcomes", "extracted", "failed")},
            "outcomes": dict(outcomes), "top_refusal_codes": [list(t) for t in sorted(histogram.items(), key=lambda item: (-item[1], item[0]))[:3]],
            "repeated_run_ids": sum(n - 1 for n in runs.values())}


UNIQUE_SCHEMA = "records-extract-summary-unique-v1"
SHA256 = re.compile(r"[0-9a-f]{64}")
USAGE = "usage: records_extract_summary.py [--total | --unique] FILE... | --self-test"


class UniqueRefused(Exception):
    """A fixed error code and the 1-based ordinal of the refused input file; never content or a path."""

    def __init__(self, code, ordinal):
        super().__init__(code)
        self.code, self.ordinal = code, ordinal


def unique_winners(winners, data, ordinal):
    """Fold one loaded file into winners (hash -> outcome and refusal_code only); nothing else is kept."""
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        raise UniqueRefused("wrong-schema", ordinal)
    documents = data.get("documents")
    if not isinstance(documents, list) or not all(isinstance(d, dict) for d in documents):
        raise UniqueRefused("bad-documents", ordinal)
    seen = set()
    for document in documents:
        identity = document.get("original_sha256")
        if not isinstance(identity, str) or not SHA256.fullmatch(identity):
            raise UniqueRefused("bad-original-sha256", ordinal)
        if identity in seen:
            raise UniqueRefused("duplicate-original", ordinal)
        seen.add(identity)
    for document in documents:
        outcome = document.get("outcome")
        kept = {"outcome": outcome if isinstance(outcome, str) else None}
        if "refusal_code" in document:
            kept["refusal_code"] = document["refusal_code"]
        winners[document["original_sha256"]] = kept
    return len(documents)


def unique_summary(winners, files, observations):
    """One line over the winning rows only: counts and codes, no identity, name, run label or time."""
    outcomes = dict.fromkeys(OUTCOMES, 0)
    for row in winners.values():
        if row["outcome"] in outcomes:
            outcomes[row["outcome"]] += 1
    histogram = refusal_code_histogram(list(winners.values()))
    top = sorted(histogram.items(), key=lambda item: (-item[1], item[0]))[:3]
    return {"schema": UNIQUE_SCHEMA, "selection": "last-input-wins", "scope": "provided-outcome-files-only",
            "files": files, "observations": observations, "documents": len(winners),
            "replaced_observations": observations - len(winners), "outcomes": outcomes,
            "unknown_outcomes": len(winners) - sum(outcomes.values()), "extracted": outcomes[EXTRACTED],
            "failed": sum(outcomes[c] for c in FAILED), "top_refusal_codes": [list(t) for t in top]}


def run_unique(paths):
    """(exit status, one line): files are read once each, in the order given; the first refusal ends the run."""
    winners, observations = {}, 0
    try:
        for ordinal, path in enumerate(paths, 1):
            try:
                data = load(path)
            except Refused as error:
                raise UniqueRefused(str(error), ordinal) from None
            observations += unique_winners(winners, data, ordinal)
    except UniqueRefused as error:
        return 1, {"schema": UNIQUE_SCHEMA, "error": error.code, "input": error.ordinal}
    return 0, unique_summary(winners, len(paths), observations)


def self_test():
    pairs = [(EXTRACTED, "b"), (sorted(FAILED)[0], "a"), ("NEW_CODE", "c"), (EXTRACTED, "d"), (EXTRACTED, "Bad!")]
    docs = [{"original_sha256": "ab" * 32, "form": "pdf", "outcome": o, "refusal_code": r} for o, r in pairs]
    line = checked(lambda: {"schema": SCHEMA, "run_id": "r1", "documents": docs}, "x.json")
    assert (line["unknown_outcomes"], line["extracted"], line["failed"], line["documents"]) == (1, 3, 1, 5)
    assert "NEW_CODE" not in line["outcomes"] and line["top_refusal_codes"] == [["a", 1], ["b", 1], ["c", 1]]
    assert "ab" * 32 not in json.dumps(line) and "pdf" not in json.dumps(line)
    bad = [([], "wrong-schema"), ({"schema": "x"}, "wrong-schema"), ({"schema": SCHEMA, "documents": {}}, "bad-documents"),
           ({"schema": SCHEMA, "documents": [1]}, "bad-documents")]
    assert all(checked(lambda data=data: data, "x.json").get("error") == code for data, code in bad)
    empty = checked(lambda: {"schema": SCHEMA, "run_id": "r", "documents": []}, "e.json")
    assert empty["documents"] == empty["failed"] == 0 and not any(empty["outcomes"].values())
    runs = [checked_full(lambda r=r, p=p: {"schema": SCHEMA, "run_id": r, "documents": [
        {"outcome": sorted(FAILED)[0], "refusal_code": c} for c in [p + "a", p + "b", p + "c"] * 2 + ["x"]]}, "t.json")
        for r, p in [("r", "p"), ("r", "q"), ("s", "s")]] + [checked_full(lambda: [], "w.json")]
    summary = total(runs)
    assert summary["top_refusal_codes"] == [["x", 3], ["pa", 2], ["pb", 2]] and summary["failed"] == 21
    assert summary["refused_files"] == {"wrong-schema": 1} and (summary["files"], summary["summarized_files"]) == (4, 3)
    assert summary["repeated_run_ids"] == 1
    print("extract summary: self-test OK")
    return 0


def main(argv):
    if argv == ["--self-test"]:
        return self_test()
    if "--unique" in argv:
        files = [arg for arg in argv if arg != "--unique"]
        if not files or argv.count("--unique") > 1 or any(arg.startswith("-") for arg in files):
            print(USAGE, file=sys.stderr)
            return 2
        status, line = run_unique(files)
        print(json.dumps(line, sort_keys=True))
        return status
    files = [arg for arg in argv if arg != "--total"]
    if not files or argv.count("--total") > 1 or any(arg.startswith("-") for arg in files):
        print(USAGE, file=sys.stderr)
        return 2
    results = [checked_full(lambda path=path: load(path), os.path.basename(path)) for path in files]
    lines = [line for line, _ in results] + ([total(results)] if "--total" in argv else [])
    print("\n".join(json.dumps(line, sort_keys=True) for line in lines))
    return 1 if any("error" in line for line, _ in results) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
