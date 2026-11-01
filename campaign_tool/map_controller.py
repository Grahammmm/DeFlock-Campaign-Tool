"""Build the shared map controller from trusted campaign settings and a UI hook."""
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

ASSET = Path(__file__).parent / "assets" / "map-controller.js.tmpl"
KEYS = {"schema_version", "group_slug", "candidate_property",
        "bounding_box_fallbacks", "source_map_url", "city_boundaries_url",
        "initial_point_count", "candidate_count_suffix"}
SLUG = re.compile(r"[a-z][a-z0-9-]{0,63}")
MAX_BYTES = 2 * 1024 * 1024
NAME = re.compile(r"[A-Za-z0-9_]{1,64}")


def _https(value):
    return isinstance(value, str) and re.fullmatch(r"https://[A-Za-z0-9./:?&=%_+#~-]+", value)


def _origin(value):  # host present, no user or password
    return not _https(value) or (lambda p: p.hostname and not p.username and not p.password)(urlsplit(value))


CHECKS = (  # (field, accepts value, reason); reasons never include the value
    ("schema_version", lambda v: type(v) is int and v == 1, "unsupported schema version"),
    ("group_slug", lambda v: isinstance(v, str) and SLUG.fullmatch(v), "invalid slug"),
    ("candidate_property", lambda v: isinstance(v, str) and re.fullmatch(r"[a-z][a-zA-Z0-9]{0,63}", v), "invalid identifier"),
    ("bounding_box_fallbacks", lambda v: isinstance(v, list) and len(v) <= 128 and all(
        isinstance(s, str) and SLUG.fullmatch(s) for s in v) and len(set(v)) == len(v), "invalid or duplicate slugs"),
    ("initial_point_count", lambda v: type(v) is int and 0 <= v <= 10000000, "out of range"),
    ("source_map_url", _https, "not a public https URL"),
    ("source_map_url", _origin, "credentials or no host in URL"),
    ("city_boundaries_url", lambda v: isinstance(v, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,160}", v) and ".." not in v.split("/"), "not a local public asset path"),
    ("candidate_count_suffix", lambda v: isinstance(v, str) and 1 <= len(v) <= 200, "must be 1-200 characters"),
)


def problems(config, profile_renderer="", point_context_renderer=""):
    """Every build-input problem, sorted; each names its field, never its value."""
    if not isinstance(config, dict):
        return ["config: must be an object"]
    found = ["missing field: " + key for key in KEYS - set(config)]
    found += ["unknown field: " + (key if isinstance(key, str) and NAME.fullmatch(key) else f"{len(str(key))} chars")
              for key in config if key not in KEYS]
    found += [key + ": " + reason for key, ok, reason in CHECKS if key in config and not ok(config[key])]
    for name, value in (("profile_renderer", profile_renderer), ("point_context_renderer", point_context_renderer)):
        if not isinstance(value, str) or len(value.encode("utf-8")) > MAX_BYTES:
            found.append(name + ": invalid trusted renderer")
    return sorted(found)


def render(config, profile_renderer, point_context_renderer=""):
    found = problems(config, profile_renderer, point_context_renderer)
    if found:
        raise ValueError("; ".join(found))
    prop = config["candidate_property"]
    fallbacks = config["bounding_box_fallbacks"]
    count = config["initial_point_count"]
    source = config["source_map_url"]
    boundary = config["city_boundaries_url"]
    suffix = config["candidate_count_suffix"]
    tokens = {
        "GROUP_SLUG": json.dumps(config["group_slug"]),
        "CANDIDATE_PROPERTY": prop,
        "BOUNDING_BOX_FALLBACKS": "[" + ",".join("'" + slug + "'" for slug in fallbacks) + "]",
        "SOURCE_MAP_URL": source,
        "CITY_BOUNDARIES_URL": json.dumps(boundary),
        "INITIAL_POINT_COUNT": str(count),
        "CANDIDATE_COUNT_SUFFIX": json.dumps(suffix, ensure_ascii=False),
        "PROFILE_RENDERER": profile_renderer,
        "POINT_CONTEXT_RENDERER": "+(" + point_context_renderer + ")(p)" if point_context_renderer else "",
    }
    template = ASSET.read_text(encoding="utf-8")
    token = re.compile(r"\{\{([A-Z_]+)\}\}")
    if set(token.findall(template)) != set(tokens):
        raise ValueError("Map template contract mismatch")
    return token.sub(lambda match: tokens[match.group(1)], template)


def _refuse(name):
    raise ValueError("non-finite number")


def check(path):
    """Print each config problem (never a value) then problems=N; exit 0 none, 1 problems, 2 unreadable."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_BYTES + 1)
        config = json.loads(raw.decode("utf-8"), parse_constant=_refuse) if len(raw) <= MAX_BYTES else None
        if not isinstance(config, dict):
            raise ValueError("not an object")
    except (OSError, ValueError, RecursionError):
        print("Map check stopped: config file missing, unreadable, over 2 MB, not UTF-8 JSON or not an object",
              file=sys.stderr)
        return 2
    found = problems(config)
    print("\n".join(found + ["renderers: not checked", f"problems={len(found)}"]))
    return 1 if found else 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv:
        if len(argv) == 2 and argv[0] == "--check":
            return check(argv[1])
        print("usage: map_controller [--check CONFIG.json]  (no arguments: build from stdin)", file=sys.stderr)
        return 2
    try:
        raw = sys.stdin.buffer.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError("Map input too large")
        value = json.loads(raw)
        if not isinstance(value, dict) or set(value) not in ({"config", "profile_renderer"}, {"config", "profile_renderer", "point_context_renderer"}):
            raise ValueError("Invalid map build payload")
        sys.stdout.write(json.dumps({"javascript": render(value["config"], value["profile_renderer"], value.get("point_context_renderer", ""))}))
    except (ValueError, TypeError, OSError) as exc:
        print("Map build stopped: " + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
