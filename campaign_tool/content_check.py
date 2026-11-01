"""Name every content/ problem in one read-only pass, with exactly the rules `build` enforces.

``load_content`` stops at the first ``ContentError``. This module calls the same public loaders per file,
keeps going, and runs the cross-file rules only over the documents that loaded, so
``content_problems(root) == []`` exactly when ``load_content(root)`` returns without raising.
"""

import sys
from pathlib import Path

from . import content as c

LIMIT = 200


def _attempt(problems, label, function, *args):
    """Run one loader; on refusal record its message, with the file's directory, and return None."""
    try:
        return function(*args)
    except c.ContentError as exc:
        message, name = str(exc), Path(label).name
        if not message.startswith(label) and message.startswith(name + ":"):
            message = label + message[len(name):]  # _read_json names only the bare file
        problems.append(message)
    except ValueError as exc:  # map_controller.render raises plain ValueError
        problems.append(f"{label}: {exc}")
    return None


def _finding(path):
    return c.load_finding(c._read_json(path), "findings/" + path.name)


def _meeting(path):
    meeting = c.load_meeting(c._read_json(path), "meetings/" + path.name)
    meeting["id"] = meeting["id"] or c._slug(path.stem, "meetings/" + path.name + " filename")
    return meeting


def _agencies(root, path):
    if path.is_file():
        return c.load_agencies(c._read_json(path), "content/agencies.json")
    return c.default_agencies(root)


def _documents(directory):
    return sorted(directory.glob("*.json")) if directory.is_dir() else []


def all_problems(root):
    """Every problem in content/, sorted by file label and de-duplicated; None without content/."""
    root = Path(root)
    content_dir = root / "content"
    if not content_dir.is_dir():
        return None
    problems = []
    site_path = content_dir / "site.json"
    if site_path.is_file():
        _attempt(problems, "site.json", lambda: c.load_site(c._read_json(site_path)))
    else:
        problems.append("content/site.json is required when content/ exists")

    slugs, ids = set(), set()
    for path in _documents(content_dir / "findings"):
        finding = _attempt(problems, "findings/" + path.name, _finding, path)
        if finding is None:
            continue
        if finding["slug"] in slugs or finding["id"] in ids:
            problems.append(f"findings/{path.name}: duplicate finding slug or id")
            continue
        slugs.add(finding["slug"])
        ids.add(finding["id"])

    for path in _documents(content_dir / "meetings"):
        _attempt(problems, "meetings/" + path.name, _meeting, path)

    sources_path = content_dir / "sources.json"
    if sources_path.is_file():
        _attempt(problems, "sources.json", lambda: c.load_sources(c._read_json(sources_path)))

    agencies_path = content_dir / "agencies.json"
    agencies = _attempt(problems, "content/agencies.json" if agencies_path.is_file() else "kit",
                        _agencies, root, agencies_path)
    map_config = _attempt(problems, "map/map-config.json", c.load_map, content_dir)
    if map_config and agencies is not None:  # a broken agency list would only cascade false lines
        known = {agency["slug"] for agency in agencies if agency["kind"] in c.CITY_KINDS}
        for slug in map_config["controller"]["bounding_box_fallbacks"]:
            if slug not in known:
                problems.append(f"map/map-config.json.bounding_box_fallbacks: {slug!r} is not a "
                                "police/city_council agency slug")
    return sorted(set(problems))


def capped(problems, limit=LIMIT):
    if len(problems) <= limit:
        return list(problems)
    return problems[:limit] + [f"… and {len(problems) - limit} more"]


def content_problems(root):
    """Problems as printed (at most LIMIT lines plus a remainder line); None without content/."""
    problems = all_problems(root)
    return None if problems is None else capped(problems)


def content_check_cmd(root):
    problems = all_problems(root)
    if problems is None:
        print("no content/ directory")
        return
    print(f"content/: {len(problems)} problem(s)")
    for line in capped(problems):
        print(line)
    if problems:
        sys.exit(3)
