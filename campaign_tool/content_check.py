"""Name every content/ problem in one pass, with exactly the rules ``build`` enforces (read-only).

``load_content`` stops at the first refusal; this calls the same loaders file by file and keeps going. Parity
invariant: ``content_problems(root) == []`` iff ``load_content(root)`` returns and no public-assets image carries
metadata (build's ``image_meta.check_asset``). Cross-file rules use loaded files.
"""
from datetime import date
import json
import os
from pathlib import Path

from .content import (ASKED_REQUEST, CITY_KINDS, LOCAL_ASSET, MAX_GEOJSON_BYTES, SOURCES_UNKNOWN, ContentError, _alpr_policy, _asked, _bounds, _contract, _contract_check, _policy_check, _read_json,
                      _require_keys, _slug, _text, _validate_cameras, check_finding_agencies, check_source_pages, continuation_problems,
                      default_agencies,
                      history_present, load_agencies, load_finding, load_history, load_map, load_meeting, load_site,
                      load_snapshot, load_sources, load_topic, plain_text, point_check_problems, point_nodes, topic_paths, read_glossary,
                      topics_namespace_problem)
from .finding_law import check as check_law, jurisdiction, package_line
from .doc_assets import check_asset as check_doc_asset
from .image_meta import check_asset
from .labels import MAX_LABELS_BYTES, load_labels
from .newsletter.archive import FOLDER as NEWSLETTERS, issue_paths, load_public_issue
from .redirects import check_file as check_redirects
from .supporters import supporter_problems

LIMIT = 200
SCHEMA = "content-check-v1"
CAMERAS_MISSING = "map/cameras.geojson is missing; run tools/fetch_osm_alprs.py or supply reviewed data"


def _attempt(problems, label, call):
    """Return ``call()`` or record its refusal (named after ``label``'s file) and return None."""
    try:
        return call()
    except ContentError as exc:
        text, head, folder = str(exc), str(exc).split(":", 1)[0], label.rpartition("/")[0]
        if folder and "/" not in head and head.endswith((".json", ".geojson")):  # _read_json names the bare file
            text = folder + "/" + text
    except ValueError as exc:  # map_controller.render refuses with a plain ValueError naming the field
        text = f"{label}: {exc}"
    problems.append(text)
    return None


def _cities(path, _name):  # load_map's map/cities.json expressions
    raw = _read_json(path)
    if not isinstance(raw, dict) or len(raw) > 200:
        raise ContentError("map/cities.json: expected {slug: {bounds: [[w,s],[e,n]]}}")
    for slug, entry in raw.items():
        _slug(slug, "map/cities.json key")
        _require_keys(entry, {"bounds", "name"}, {"bounds"}, f"map/cities.json[{slug}]")
        _bounds(entry["bounds"], f"map/cities.json[{slug}].bounds")
        _text(entry, "name", f"map/cities.json[{slug}]", required=False, default=None)
    return set(raw)


def _boundaries(path, name):  # load_map's city-boundary-file expressions
    document = _read_json(path, MAX_GEOJSON_BYTES)
    if not isinstance(document, dict) or document.get("type") != "FeatureCollection":
        raise ContentError(f"map/{name}: expected a GeoJSON FeatureCollection")


def _attribution(document, map_dir):  # load_map's attribution expressions
    attribution, path = document.get("attribution"), map_dir / "attribution.txt"
    if attribution is None and path.is_file():
        attribution = path.read_text(encoding="utf-8").strip()
    if attribution is not None and (not plain_text(attribution) or len(attribution) > 500):
        raise ContentError("map attribution must be plain text under 500 characters")


def _config_tail(document, cities):  # load_map's map-config.json checks that run after its sibling files
    for slug in document["bounding_box_fallbacks"] if cities is not None else []:  # cities None: its file is named
        if slug not in cities:
            raise ContentError(f"map/map-config.json.bounding_box_fallbacks: {slug!r} has no bounds in "
                               "map/cities.json")
    _bounds(document["county_bounds"], "map/map-config.json.county_bounds")
    _text(document, "group_label", "map/map-config.json", default="Unincorporated area (ownership unconfirmed)")


def _point_checks(problems, map_dir, library):
    """DF1422-11: every refused entry of map/point-checks.json (load_map names only the first)."""
    path = map_dir / "point-checks.json"
    if not history_present(path):
        return
    nodes = set()
    try:
        nodes = point_nodes(_read_json(map_dir / "cameras.geojson", MAX_GEOJSON_BYTES))
    except (ContentError, AttributeError, TypeError, OSError):
        return  # an unreadable cameras.geojson is already named; every node would only add false lines
    known = SOURCES_UNKNOWN if library is None else library
    problems.extend(point_check_problems(path, nodes, known, date.today())[0])


def _map(problems, content_dir, library=None):
    """load_map stops at its first bad file; once it has refused (so parity holds), name the other files' too.

    ``library``: the validated sources.json entries, or None when that file is itself refused."""
    before, map_dir = len(problems), content_dir / "map"
    config = _attempt(problems, "map/map-config.json",
                      lambda: load_map(content_dir, SOURCES_UNKNOWN if library is None else library))
    if len(problems) == before:
        return config
    config_named = problems[-1].startswith("map/map-config.json")  # else load_map passed the config's head
    checks = {"cameras.geojson": lambda p, n: _validate_cameras(_read_json(p, MAX_GEOJSON_BYTES), "map/" + n),
              "cities.json": _cities, "snapshot.json": lambda p, _n: load_snapshot(p),  # snapshot: optional
              "history.json": lambda p, _n: load_history(p)}  # optional; any entry, a dangling link too, is judged
    missing = {"cameras.geojson": CAMERAS_MISSING}
    document = None
    try:
        document = _read_json(map_dir / "map-config.json")
        url = document["city_boundaries_url"]
        if LOCAL_ASSET.fullmatch(url):
            checks.setdefault(Path(url).name, _boundaries)
            missing.setdefault(Path(url).name, f"map/{Path(url).name}: city boundary file is missing")
    except (ValueError, KeyError, TypeError):
        pass  # map-config.json's own refusal is already listed
    cities = set()  # an absent cities.json means no city bounds, as in load_map
    for name, check in checks.items():
        if (map_dir / name).is_file() or (name == "history.json" and history_present(map_dir / name)):
            result = _attempt(problems, "map/" + name, lambda: check(map_dir / name, name))
            if name == "cities.json":
                cities = result
        elif name in missing:
            problems.append(missing[name])
    _point_checks(problems, map_dir, library)
    if not config_named:
        _attempt(problems, "map/attribution.txt", lambda: _attribution(document, map_dir))
        _attempt(problems, "map/map-config.json", lambda: _config_tail(document, cities))
    return config


def _files(content_dir, sub):
    folder = content_dir / sub
    return sorted(folder.glob("*.json")) if folder.is_dir() else []


def _meeting(path, label):
    meeting = load_meeting(_read_json(path), label)
    meeting["id"] = meeting["id"] or _slug(path.stem, label + " filename")
    return meeting


def _topics(problems, content_dir, findings, finding_slugs):
    """DF1952-06: every refused content/topics/<slug>.json by file (load_topics stops at the first). ``findings`` None:
    a finding file is itself refused, so ids are not matched. Page slugs are the page file stems, as load_pages names them."""
    paths = _attempt(problems, "topics/", lambda: topic_paths(content_dir))
    folder = content_dir / "pages"
    pages = [{"slug": path.stem} for path in (sorted(folder.iterdir()) if folder.is_dir() else [])
             if path.name.endswith(".json")]
    loaded = [topic for path in paths or []
              for topic in [_attempt(problems, "topics/" + path.name, lambda: load_topic(path, findings, pages))]
              if topic is not None]
    clash = loaded and topics_namespace_problem({p["slug"] for p in pages}, finding_slugs)
    if clash:
        problems.append(clash)


def _cap(problems, limit):
    if limit is None or len(problems) <= limit:
        return problems
    return problems[:limit] + [f"\u2026 and {len(problems) - limit} more"]


def _asked_links(problems, root, document):
    """DF1207-03: an asked item linked to a kit/sent.json request is named when the request moved on after it
    (its latest step is dated after the item's updated_on, else asked_on) or the request is not there. Read-only."""
    from . import kit_asked, kit_track  # local import: only content-check reads the private tracker
    rows = [(index, number, agency["agency_id"], item) for index, agency in enumerate(document)
            if isinstance(agency, dict) and agency.get("selected", True) is True and isinstance(agency.get("agency_id"), str)
            for number, item in enumerate(agency["asked"] if isinstance(agency.get("asked"), list) else [])
            if isinstance(item, dict) and isinstance(item.get("request"), str) and ASKED_REQUEST.fullmatch(item["request"])]
    if not rows or not os.path.lexists(root / "kit" / "sent.json"):
        return
    try:
        sent = kit_track.read_sent(root)
    except ValueError as exc:
        problems.append(str(exc))
        return
    for index, number, agency_id, item in rows:
        label, request = f"content/agencies.json[{index}].asked[{number}]", item["request"]
        entry = sent.get(kit_track._key(agency_id, request))
        if entry is None:
            problems.append(f"{label}: links request {request} not in kit/sent.json")
            continue
        latest, shown = kit_asked.latest_step(entry["steps"]), item.get("updated_on") or item.get("asked_on")
        if latest and kit_track._day(shown) and latest[1] > shown:
            problems.append(f"{label}: older than its request's latest step ({latest[0]} on {latest[1]}); run kit-asked")


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
        _attempt(problems, "site.json", lambda: load_site(_read_json(site_path), content_dir / "public-assets"))

    slugs, ids, findings, labelled = set(), set(), [], []
    for path in _files(content_dir, "findings"):
        label = "findings/" + path.name
        finding = _attempt(problems, label, lambda: load_finding(_read_json(path), label))
        if finding is None:
            continue
        if finding["slug"] in slugs or finding["id"] in ids:
            problems.append(f"findings/{path.name}: duplicate finding slug or id")
        slugs.add(finding["slug"])
        ids.add(finding["id"])
        labelled.append((label, finding))
        findings.append(finding)
    findings_intact = len(findings) == len(_files(content_dir, "findings"))

    meeting_ids, loaded_meetings = set(), []
    for path in _files(content_dir, "meetings"):
        label = "meetings/" + path.name
        meeting = _attempt(problems, label, lambda: _meeting(path, label))
        if meeting is None:
            continue
        if meeting["id"] in meeting_ids:
            problems.append(f"meetings/{path.name}: duplicate meeting id")
        meeting_ids.add(meeting["id"])
        loaded_meetings.append(meeting)
    problems.extend(continuation_problems(loaded_meetings))  # DF0022-07: the same cross-record rule build enforces

    sources_path = content_dir / "sources.json"
    library = (_attempt(problems, "sources.json", lambda: load_sources(_read_json(sources_path)))
               if sources_path.is_file() else [])
    if library is not None:  # an unreadable library is already named; build refuses it before the page check
        problems.extend(check_source_pages(findings, library))

    _topics(problems, content_dir, findings if findings_intact else None, slugs)

    labels_path = content_dir / "labels.json"  # optional, read exactly as load_content: a symlink is named, not read
    if labels_path.is_file() or labels_path.is_symlink():
        _attempt(problems, "labels.json", lambda: load_labels(_read_json(labels_path, MAX_LABELS_BYTES)))

    read_glossary(root, problems)  # DF1952-04: the build's own reader; it names every independent problem itself

    agencies_path = content_dir / "agencies.json"
    if agencies_path.is_file():
        document = _attempt(problems, "content/agencies.json", lambda: _read_json(agencies_path))
        if isinstance(document, list):
            for index, entry in enumerate(document):
                if isinstance(entry, dict) and entry.get("selected", True) is True:
                    _asked(entry, f"content/agencies.json[{index}].asked", problems=problems)
                    profile = entry.get("profile", {})
                    if isinstance(profile, dict) and profile.get("contract_check") is not None:  # DF2207-03
                        label = f"content/agencies.json[{index}].profile"
                        contract = _attempt(problems, label + ".contract",
                                            lambda: _contract(profile.get("contract"), label + ".contract"))
                        _contract_check(profile["contract_check"], contract, label + ".contract_check", problems=problems)
                    if isinstance(profile, dict) and profile.get("policy_check") is not None:
                        label = f"content/agencies.json[{index}].profile"
                        alpr = _attempt(problems, label + ".alpr_policy",
                                        lambda: _alpr_policy(profile.get("alpr_policy"), label + ".alpr_policy"))
                        _attempt(problems, label + ".policy_check",
                                 lambda: _policy_check(profile["policy_check"], label + ".policy_check", alpr))
            _asked_links(problems, root, document)
        agencies = _attempt(problems, "content/agencies.json",
                            lambda: load_agencies(_read_json(agencies_path), "content/agencies.json"))
    else:
        agencies = _attempt(problems, "kit/agencies.json", lambda: default_agencies(root))
    if agencies is not None:  # DF1952-07: a failed agency pass is already named; do not add an unknown-id line per finding
        known = {agency["agency_id"] for agency in agencies}
        for label, finding in labelled:
            _attempt(problems, label, lambda: check_finding_agencies(finding, known, label))

    # DF1952-01: the law check follows agency loading so a local overlay's agency_ids are judged against loaded agencies
    # (None, when agencies are themselves refused and named, skips only that scope check).
    _attempt(problems, "campaign.json", lambda: problems.extend(check_law(root, findings, agencies)[0]))

    map_config = _map(problems, content_dir, library)
    if map_config and agencies is not None:  # a broken agency list would only add false lines
        known = {agency["slug"] for agency in agencies if agency["kind"] in CITY_KINDS}
        for slug in map_config["controller"]["bounding_box_fallbacks"]:
            if slug not in known:
                problems.append(f"map/map-config.json.bounding_box_fallbacks: {slug!r} is not a "
                                "police/city_council agency slug")
    assets_dir = content_dir / "public-assets"  # collected exactly as load_content; build refuses image metadata
    for path in sorted(assets_dir.rglob("*")) if assets_dir.is_dir() else []:
        if path.is_file() and not path.is_symlink():
            relative = path.relative_to(assets_dir)
            _attempt(problems, f"public-assets/{relative}", lambda: check_asset(path, relative))
            _attempt(problems, f"public-assets/{relative}", lambda: check_doc_asset(path, relative))
    paths = _attempt(problems, NEWSLETTERS, lambda: issue_paths(content_dir))  # DF1952-09: the loader's own validator
    for path in paths or []:
        _attempt(problems, f"{NEWSLETTERS}/{path.name}", lambda: load_public_issue(path))
    check_redirects(root, problems)  # DF2037-16: content/redirects.json with build's own refusal text
    problems.extend(supporter_problems(root))  # DF2207-13: every invalid or unconsented supporter id, no values
    return _cap(sorted(set(problems)), limit)


# Leading paths the problem lines start with (``_attempt`` labels, the loaders' own names, build's file names).
FILE_PREFIXES = ("content/", "public-assets/", "map/", "findings/", "meetings/", "topics/", "kit/", "law/", "newsletters/",
                 "site.json", "sources.json", "labels.json", "agencies.json", "campaign.json", "redirects.json", "glossary.json", "supporters.json")


def problem_record(line):
    """``{"file", "message"}`` for one problem line; ``file + ": " + message == line`` whenever file is set.

    ``file`` is the text before the first ``": "`` when it is a single token starting with a known content path;
    otherwise None and the whole line is the message. The message text is never parsed further.
    """
    head, sep, rest = line.partition(": ")
    if sep and head.startswith(FILE_PREFIXES) and not any(c.isspace() for c in head):
        return {"file": head, "message": rest}
    return {"file": None, "message": line}


def _law_package(root):
    try:
        name = jurisdiction(root)
    except ContentError:  # an unreadable campaign.json is already a listed problem when a rule is cited
        return None
    return None if name is None else package_line(name)


def run_cli(root, as_json=False):
    """Print ``content/: N problem(s)`` and one line each (or one ``content-check-v1`` JSON line); exit 3 when any."""
    problems = content_problems(root, limit=None)
    if problems is None:
        if as_json:
            print(json.dumps({"schema": SCHEMA, "count": 0, "problems": [], "truncated": False,
                              "law_package": None, "no_content": True}, sort_keys=True, ensure_ascii=False))
        else:
            print("no content/ directory")
        return
    package = _law_package(root)
    if as_json:
        print(json.dumps({"schema": SCHEMA, "count": len(problems),
                          "problems": [problem_record(line) for line in problems[:LIMIT]],
                          "truncated": len(problems) > LIMIT, "law_package": package},
                         sort_keys=True, ensure_ascii=False))
    else:
        print(f"content/: {len(problems)} problem(s)")
        if package is not None:
            print(package)
        for line in _cap(problems, LIMIT):
            print(line)
    if problems:
        raise SystemExit(3)
