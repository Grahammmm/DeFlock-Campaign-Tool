"""Read-only summary of records-extract-outcomes-v1 files (RF0805-4). Usage: FILE... | --self-test
One JSON line of counts per FILE; never a document hash, form, accept status, OCR page list, written_at or a path
other than the basename. Writes nothing; wired into no stage. The extract stage (records.native_extract) owns the file.
"""
import json
import os
import stat
import sys

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
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        raise Refused("wrong-schema")
    documents = data.get("documents")
    if not isinstance(documents, list) or not all(isinstance(d, dict) for d in documents):
        raise Refused("bad-documents")
    outcomes = dict.fromkeys(OUTCOMES, 0)
    known = [d["outcome"] for d in documents if isinstance(d.get("outcome"), str) and d["outcome"] in outcomes]
    outcomes.update((code, known.count(code)) for code in set(known))
    top = sorted(refusal_code_histogram(documents).items(), key=lambda item: (-item[1], item[0]))[:3]
    run_id = data.get("run_id")
    return {"schema": "records-extract-summary-v1", "file": name, "run_id": run_id if isinstance(run_id, str) else None,
            "documents": len(documents), "outcomes": outcomes, "unknown_outcomes": len(documents) - len(known),
            "extracted": outcomes[EXTRACTED], "failed": sum(outcomes[c] for c in FAILED), "top_refusal_codes": [list(t) for t in top]}


def checked(read, name):
    try:
        return summarize(read(), name)
    except Refused as error:
        return {"file": name, "error": str(error)}


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
    print("extract summary: self-test OK")
    return 0


def main(argv):
    if argv == ["--self-test"]:
        return self_test()
    if not argv or any(arg.startswith("-") for arg in argv):
        print("usage: records_extract_summary.py FILE... | --self-test", file=sys.stderr)
        return 2
    lines = [checked(lambda path=path: load(path), os.path.basename(path)) for path in argv]
    print("\n".join(json.dumps(line, sort_keys=True) for line in lines))
    return 1 if any("error" in line for line in lines) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
