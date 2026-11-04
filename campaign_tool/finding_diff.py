"""`finding diff ID --directory D --from DRAFT` (published vs draft) or `finding diff --directory D OLD NEW`:
field-level diff of a finding's public FIELDS; publication-only fields are ignored. Exit 3 on any difference,
0 when identical. Read-only: nothing is written."""
import difflib
import json
from collections import Counter

from .findings import _real_dir, load_draft

FIELDS = ("title", "summary", "body_md", "sources", "classification", "confidence", "limitations", "counterevidence",
          "agency_id", "also_agency_ids")
LISTS = ("limitations", "counterevidence", "also_agency_ids")
SOURCE_FIELDS = ("locator", "title", "rule_id")


def load(path):
    """load_draft plus the shape the diff relies on; errors name the file."""
    document = load_draft(path)
    for field in ("title", "summary", "body_md", "classification", "confidence", "agency_id"):
        if document.get(field) is not None and not isinstance(document[field], str):
            raise ValueError(f"{path}: key {field!r} must be a string")
    for field in LISTS:
        value = document.get(field, [])
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ValueError(f"{path}: key {field!r} must be a list of strings")
    sources = document.get("sources", [])
    if not isinstance(sources, list) or not all(isinstance(s, dict) and isinstance(s.get("sha256"), str)
                                                for s in sources):
        raise ValueError(f"{path}: every entry of 'sources' must be an object with a 'sha256' string")
    return document


def published_by_id(root, finding_id):
    folder = root / "content" / "findings"
    matches = [(path, document) for path in (sorted(folder.glob("*.json")) if _real_dir(folder) else [])
               for document in [load(path)] if document.get("id") == finding_id]
    if not matches:
        raise ValueError(f"no published finding with id {finding_id!r} in content/findings/")
    if len(matches) > 1:
        raise ValueError(f"id {finding_id!r} is published more than once: "
                         + " and ".join(f"content/findings/{path.name}" for path, _ in matches))
    return matches[0][1]


def _minus(items, other):
    """Items of ``items`` not matched one-for-one in ``other`` (multiset difference), in original order."""
    left, out = Counter(other), []
    for item in items:
        if left[item]:
            left[item] -= 1
        else:
            out.append(item)
    return out


def _by_sha(sources):
    grouped = {}
    for source in sources or []:
        grouped.setdefault(source["sha256"], []).append(source)
    return grouped


def _sources(old, new):
    before, after, changed = _by_sha(old), _by_sha(new), []
    for sha in (sha for sha in before if sha in after):
        a, b = before[sha], after[sha]
        keys = (["count"] if len(a) != len(b) else []) + [
            k for k in SOURCE_FIELDS if [s.get(k) for s in a] != [s.get(k) for s in b]]
        if keys:
            changed.append({"sha256": sha[:12], "keys": keys})
    return {"field": "sources", "removed": [sha[:12] for sha in before if sha not in after],
            "added": [sha[:12] for sha in after if sha not in before], "changed": changed}


def diff(old, new):
    """One entry per public field that differs, in FIELDS order; a missing field counts as None / []."""
    changes = []
    for field in FIELDS:
        a, b = old.get(field), new.get(field)
        if field == "body_md":
            if (a or "") != (b or ""):
                lines = list(difflib.unified_diff((a or "").splitlines(), (b or "").splitlines(), n=1, lineterm=""))
                # Drop only the two generated '--- '/'+++ ' file headers; hunk lines such as '--- old detail'
                # (a removed '-- old detail') are body changes and stay.
                changes.append({"field": field, "lines": lines[2:]})
        elif field == "sources":
            entry = _sources(a, b)
            if entry["removed"] or entry["added"] or entry["changed"]:
                changes.append(entry)
        elif field in LISTS:
            if (a or []) != (b or []):
                removed, added = _minus(a or [], b or []), _minus(b or [], a or [])
                if field == "also_agency_ids" and not removed and not added:  # order only: show both ordered lists
                    changes.append({"field": field, "old": a or [], "new": b or []})
                else:
                    changes.append({"field": field, "removed": removed, "added": added})
        elif a != b:
            changes.append({"field": field, "old": a, "new": b})
    return changes


def render(changes):
    out, quote = [], (lambda value: json.dumps(value, ensure_ascii=False))
    for change in changes:
        field = change["field"]
        if "old" in change:
            out.append(f"{field}: {quote(change['old'])} -> {quote(change['new'])}")
            continue
        item_text = quote if field in LISTS else str  # list items quoted; source hashes bare
        out += [f"{field}:", *change.get("lines", []), *(f"- {item_text(item)}" for item in change.get("removed", [])),
                *(f"+ {item_text(item)}" for item in change.get("added", [])),
                *(f"~ {item['sha256']} {', '.join(item['keys'])}" for item in change.get("changed", []))]
    return out


def run_cli(root, inputs, from_draft, as_json):
    """Print the diff (text or ``--json``) and a last summary line; return the exit code (3 differ, 0 same)."""
    if from_draft is not None:
        old, new = published_by_id(root, inputs[0]), load(from_draft)
    else:
        old, new = load(inputs[0]), load(inputs[1])
    changes = diff(old, new)
    for line in [json.dumps(changes, indent=2, ensure_ascii=False)] if as_json else render(changes):
        print(line)
    print(f"{len(changes)} field(s) differ" if changes else "no differences")
    return 3 if changes else 0
