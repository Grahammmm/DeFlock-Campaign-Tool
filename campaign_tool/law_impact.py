"""`law impact OLD.json NEW.json --directory D`: which findings an amendment of the law package touches.

OLD and NEW are two versions of one jurisdiction's package. Rules are matched by ``rule_id`` and compared on
``citation``, ``duty``, ``exceptions`` (a list, compared in order), ``effective_from`` and ``effective_to``; every other
field is ignored. A rule's *changed window* is the days whose answer may differ: the union of the old and new
effective intervals when the citation, duty or exceptions changed, the symmetric difference of the two intervals when
only the dates changed, the new interval for an added rule and the old one for a removed rule. A published finding or
open draft is *affected* when it cites a changed, added or removed rule and its ``event_date`` falls in that window.

Read-only: nothing is written, no package is marked reviewed and no finding is corrected. It lists the next command
for each finding; whether a correction is needed stays a human legal decision. Exit 0: no finding affected;
3: at least one affected; 2: an input error.
"""
import json
import shlex
import sys
from datetime import date
from pathlib import Path

from . import finding_law, law

COMPARED = ("citation", "duty", "exceptions", "effective_from", "effective_to")
DATE_FIELDS = ("effective_from", "effective_to")
EDIT_MARKER = "[EDIT ME"
DRAFT_STATES = ("draft", "in_review", "blocked", "ready", "pending correction")
MAX_PACKAGE_BYTES = 8 * 1024 * 1024
OPEN = 1 << 40  # ordinal sentinel for "no end"; windows are half-open [start, end) in date ordinals
# The last event day a date can name (9999-12-31). An inclusive end on that day gives an exclusive end one past it,
# which bounds a window but is never itself an event day, so no window may start there.
LAST_DAY = date.max.toordinal()
NO_DAYS = "none (no representable event dates)"
CORRECT = "campaign_tool finding correct {id} --directory {d} --date YYYY-MM-DD --note TEXT --approved-by NAME [--from DRAFT]"
NOTE = "Read-only: no package marked reviewed and no finding corrected. Not a legal conclusion."


class ImpactError(ValueError):
    """An input error (exit 2)."""


def load_file(path):
    """A package file read with the loader's schema and semantic checks; any failure names the file."""
    path = Path(path)
    try:
        if not path.is_file():
            raise ValueError("not a file")
        if path.stat().st_size > MAX_PACKAGE_BYTES:
            raise ValueError(f"larger than {MAX_PACKAGE_BYTES} bytes")
        try:
            package = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON: {exc}") from None
        law.validate(package, law.load_schema())
        if not law._valid_jurisdiction(package["jurisdiction"]):
            raise ValueError(f"$.jurisdiction: {package['jurisdiction']!r} is not like 'us-ca'")
        law._semantic_checks(package, package["jurisdiction"])
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise ImpactError(f"{path}: {exc}") from None
    return package


def _interval(rule):
    end = rule["effective_to"]
    return (date.fromisoformat(rule["effective_from"]).toordinal(),
            OPEN if end is None else date.fromisoformat(end).toordinal() + 1)


def _merge(spans):
    merged = []
    for lo, hi in sorted(spans):
        if merged and lo <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
        else:
            merged.append((lo, hi))
    return merged


def _symmetric(first, second):
    points = sorted({*first, *second})
    inside = lambda span, day: span[0] <= day < span[1]  # noqa: E731
    return _merge([(lo, hi) for lo, hi in zip(points, points[1:] + [OPEN])
                   if lo <= LAST_DAY and inside(first, lo) != inside(second, lo)])


def window_text(window):
    if not window:
        return NO_DAYS
    day = lambda ordinal: date.fromordinal(ordinal).isoformat()  # noqa: E731
    return ", ".join(f"{day(lo)}..{'open' if hi >= OPEN else day(hi - 1)}" for lo, hi in window)


def diff_rules(old, new):
    """Rows ``{rule_id, kind: changed|added|removed, fields, window}`` sorted by kind then rule id."""
    before, after = ({r["rule_id"]: r for r in package["rules"]} for package in (old, new))
    rows = []
    for rule_id in sorted(before.keys() | after.keys()):
        if rule_id not in before:
            rows.append(dict(rule_id=rule_id, kind="added", fields=[], window=[_interval(after[rule_id])]))
        elif rule_id not in after:
            rows.append(dict(rule_id=rule_id, kind="removed", fields=[], window=[_interval(before[rule_id])]))
        else:
            fields = [f for f in COMPARED if before[rule_id].get(f) != after[rule_id].get(f)]
            if fields:
                spans = _interval(before[rule_id]), _interval(after[rule_id])
                window = _symmetric(*spans) if set(fields) <= set(DATE_FIELDS) else _merge(spans)
                rows.append(dict(rule_id=rule_id, kind="changed", fields=fields, window=window))
    return sorted(rows, key=lambda r: ("changed", "added", "removed").index(r["kind"]))


def _cited(document):
    """``finding_law.cited`` over a document that may be a half-written draft: malformed entries are ignored."""
    ids, sources = document.get("rule_ids"), document.get("sources")
    return finding_law.cited({
        "rule_ids": [i for i in ids if isinstance(i, str)] if isinstance(ids, list) else [],
        "sources": [s for s in sources if isinstance(s, dict) and isinstance(s.get("rule_id"), str)]
        if isinstance(sources, list) else []})


def collect(root):
    """(findings, unreadable paths): published and draft documents to check; withdrawn ones are left out."""
    from . import findings
    rows = findings.status_rows(root, draft_paths=True)
    unreadable = [str(row["path"]) for row in rows if "path" in row]
    live = {row["id"] for row in rows if "path" not in row and "draft_path" not in row
            and row["state"] in ("published", "corrected")}
    found = []
    folder = root / "content" / "findings"
    for path in sorted(folder.glob("*.json")) if findings._real_dir(folder) else []:
        try:
            document = findings._read_json(path, findings.MAX_DRAFT_BYTES, "published finding")
        except ValueError:
            continue  # status_rows already listed it as unreadable
        if isinstance(document, dict) and document.get("id") in live:
            found.append(("published", str(path), document))
    for row in rows:
        if "draft_path" in row and row["state"] in DRAFT_STATES:
            try:
                state = "pending correction" if row["state"] == "pending correction" else "draft"
                found.append((state, row["draft_path"], findings.load_draft(row["draft_path"])))
            except (OSError, ValueError):
                unreadable.append(str(row["draft_path"]))
    return found, sorted(set(unreadable))


def _event_day(document):
    value = document.get("event_date")
    if isinstance(value, str) and not value.startswith(EDIT_MARKER):
        try:
            return law.parse_date(value).toordinal()
        except ValueError:
            pass
    return None


def affected(rule_rows, found):
    """(affected findings, findings with no usable event_date that cite a changed rule)."""
    by_rule = {row["rule_id"]: row for row in rule_rows}
    hits, undated = [], []
    for state, path, document in found:
        cites = [rule_id for rule_id in _cited(document) if rule_id in by_rule]
        if not cites:
            continue
        day = _event_day(document)
        entry = dict(id=document["id"], state=state, event_date=document.get("event_date") if isinstance(
            document.get("event_date"), str) else None, path=path)
        if day is None:
            undated.append(dict(entry, rules=cites))
            continue
        hit = [r for r in cites if any(lo <= day < hi for lo, hi in by_rule[r]["window"])]
        if hit:
            hits.append(dict(entry, rules=hit, kinds={r: by_rule[r]["kind"] for r in hit}))
    order = lambda e: (e["state"] != "published", e["id"], e["path"])  # noqa: E731
    return sorted(hits, key=order), sorted(undated, key=order)


def next_commands(entry, directory):
    d, ident = shlex.quote(directory), shlex.quote(entry["id"])
    if entry["state"] == "draft":
        return [f"campaign_tool finding review-status {shlex.quote(entry['path'])} --directory {d}",
                "edit the unpublished draft to reassess the cited rules, then obtain fresh reviews before publication"]
    if entry["state"] == "pending correction":
        return [f"campaign_tool finding diff {ident} --directory {d} --from {shlex.quote(entry['path'])}",
                "if a correction is needed: revise this draft and obtain fresh reviews before correcting the published finding"]
    return [f"campaign_tool finding diff {ident} --directory {d} --from DRAFT",
            "if a correction is needed: " + CORRECT.format(id=ident, d=d)]


def report(old_path, new_path, old, new, directory):
    rule_rows = diff_rules(old, new)
    found, unreadable = collect(Path(directory).expanduser().resolve())
    hits, undated = affected(rule_rows, found)
    return dict(
        schema_version=1, jurisdiction=new["jurisdiction"], old=str(old_path), new=str(new_path),
        old_status=old["status"], new_status=new["status"],
        rules=[dict(r, window=[{"from": date.fromordinal(lo).isoformat(),
                                "to": None if hi >= OPEN else date.fromordinal(hi - 1).isoformat()}
                               for lo, hi in r["window"]]) for r in rule_rows],
        affected=[dict(h, next=next_commands(h, directory)) for h in hits],
        undated=[dict(u, next=next_commands(u, directory)) for u in undated],
        unreadable=unreadable,
        counts=dict(changed=sum(r["kind"] == "changed" for r in rule_rows),
                    added=sum(r["kind"] == "added" for r in rule_rows),
                    removed=sum(r["kind"] == "removed" for r in rule_rows),
                    affected=len(hits), undated=len(undated), unreadable=len(unreadable)),
        note=NOTE), rule_rows


def render(data, rule_rows):
    counts = data["counts"]
    lines = [f"law impact {data['jurisdiction']}: old {data['old']} ({data['old_status']}), "
             f"new {data['new']} ({data['new_status']})"]
    if not rule_rows:
        lines.append("no rule changed")
    for row in rule_rows:
        detail = f"{row['kind']} {row['rule_id']}" + (f": {', '.join(row['fields'])}" if row["fields"] else "")
        lines.append(f"{detail}; window {window_text(row['window'])}")
    lines.append(f"findings affected: {counts['affected']}")
    for hit in data["affected"]:
        cites = ", ".join(f"{r} ({hit['kinds'][r]})" for r in hit["rules"])
        lines.append(f"{hit['id']} {hit['state']} {hit['event_date']} cites {cites}")
        lines += ["  next: " + command for command in hit["next"][:1]] + ["  then " + c for c in hit["next"][1:]]
    lines.append(f"drafts with no event date: {counts['undated']}")
    for item in data["undated"]:
        lines.append(f"{item['id']} {item['state']} {item['event_date'] or 'no event_date'} cites "
                     + ", ".join(item["rules"]) + " (event date unknown; may be affected)")
        lines += ["  next: " + command for command in item["next"][:1]]
    if counts["unreadable"]:
        lines.append(f"unreadable files not checked: {counts['unreadable']}")
        lines += [f"unreadable {path}" for path in data["unreadable"]]
    lines.append(NOTE)
    return "\n".join(lines)


def run_cli(old_path, new_path, directory, as_json=False):
    try:
        old, new = load_file(old_path), load_file(new_path)
        if old["jurisdiction"] != new["jurisdiction"]:
            raise ImpactError(f"{old_path} is {old['jurisdiction']} but {new_path} is {new['jurisdiction']}: "
                              "compare two versions of one jurisdiction's package")
        root = Path(directory).expanduser().resolve()
        if not root.is_dir():
            raise ImpactError(f"{directory}: not a directory")
        data, rule_rows = report(old_path, new_path, old, new, str(directory))
    except (OSError, ValueError) as exc:
        print("Stopped: " + str(exc), file=sys.stderr)
        return 2
    print(json.dumps(data, indent=2, ensure_ascii=False) if as_json else render(data, rule_rows))
    return 3 if data["counts"]["affected"] else 0
