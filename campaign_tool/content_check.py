"""Name every content/ problem in one pass, with exactly the rules ``build`` enforces (read-only).

``load_content`` stops at the first ``ContentError``. This module calls the same
public loaders file by file, keeps each refusal and carries on, so an organizer
fixes a broken site update in one pass. It adds no rule of its own: the
parity invariant is ``content_problems(root) == []`` iff ``load_content(root)``
returns without raising. Cross-file rules run only over documents that loaded.
"""
from pathlib import Path

from .content import (CITY_KINDS, ContentError, _read_json, _slug, default_agencies, load_agencies,
                      load_finding, load_map, load_meeting, load_site, load_sources)

LIMIT = 200


def _attempt(problems, label, call):
    """Return ``call()`` or record its ContentError (labelled with ``label``) and return None."""
    try:
        return call()
    except ContentError as exc:
        text = str(exc)
        name = label.rsplit("/", 1)[-1]
        if label != name and text.startswith(name + ":"):  # _read_json names the bare file
            text = label[: -len(name)] + text
        problems.append(text)
        return None


def _files(content_dir, sub):
    folder = content_dir / sub
    return sorted(folder.glob("*.json")) if folder.is_dir() else []


def _meeting(path, label):
    meeting = load_meeting(_read_json(path), label)
    meeting["id"] = meeting["id"] or _slug(path.stem, label + " filename")
    return meeting


def _cap(problems, limit):
    if limit is None or len(problems) <= limit:
        return problems
    return problems[:limit] + [f"\u2026 and {len(problems) - limit} more"]


def content_problems(root, limit=LIMIT):
    """Sorted, de-duplicated problem lines for ``root/content``; None when there is no content/."""
    root = Path(root)
    content_dir = root / "content"
    if not content_dir.is_dir():
        return None
    problems = []
    site_path = content_dir / "site.json"
    if not site_path.is_file():
        problems.append("content/site.json is required when content/ exists")
    else:
        _attempt(problems, "site.json", lambda: load_site(_read_json(site_path)))

    slugs, ids = set(), set()
    for path in _files(content_dir, "findings"):
        label = "findings/" + path.name
        finding = _attempt(problems, label, lambda: load_finding(_read_json(path), label))
        if finding is None:
            continue
        if finding["slug"] in slugs or finding["id"] in ids:
            problems.append(f"findings/{path.name}: duplicate finding slug or id")
        slugs.add(finding["slug"])
        ids.add(finding["id"])

    for path in _files(content_dir, "meetings"):
        label = "meetings/" + path.name
        _attempt(problems, label, lambda: _meeting(path, label))

    sources_path = content_dir / "sources.json"
    if sources_path.is_file():
        _attempt(problems, "sources.json", lambda: load_sources(_read_json(sources_path)))

    agencies_path = content_dir / "agencies.json"
    if agencies_path.is_file():
        agencies = _attempt(problems, "content/agencies.json",
                            lambda: load_agencies(_read_json(agencies_path), "content/agencies.json"))
    else:
        agencies = _attempt(problems, "kit/agencies.json", lambda: default_agencies(root))

    map_config = _attempt(problems, "map/map-config.json", lambda: load_map(content_dir))
    if map_config and agencies is not None:  # a broken agency list would only add false lines
        known = {agency["slug"] for agency in agencies if agency["kind"] in CITY_KINDS}
        for slug in map_config["controller"]["bounding_box_fallbacks"]:
            if slug not in known:
                problems.append(f"map/map-config.json.bounding_box_fallbacks: {slug!r} is not a "
                                "police/city_council agency slug")
    return _cap(sorted(set(problems)), limit)


def run_cli(root):
    """Print ``content/: N problem(s)`` and one line each; exit 3 when any problem is listed."""
    problems = content_problems(root, limit=None)
    if problems is None:
        print("no content/ directory")
        return
    print(f"content/: {len(problems)} problem(s)")
    for line in _cap(problems, LIMIT):
        print(line)
    if problems:
        raise SystemExit(3)
