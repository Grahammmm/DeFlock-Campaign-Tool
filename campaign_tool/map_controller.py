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


def render(config, profile_renderer):
    if not isinstance(config, dict) or set(config) != KEYS:
        raise ValueError("Invalid map configuration fields")
    if type(config["schema_version"]) is not int or config["schema_version"] != 1:
        raise ValueError("Unsupported map schema version")
    if not isinstance(config["group_slug"], str) or not SLUG.fullmatch(config["group_slug"]):
        raise ValueError("Invalid group slug")
    prop = config["candidate_property"]
    if not isinstance(prop, str) or not re.fullmatch(r"[a-z][a-zA-Z0-9]{0,63}", prop):
        raise ValueError("Invalid candidate property")
    fallbacks = config["bounding_box_fallbacks"]
    if not isinstance(fallbacks, list) or len(fallbacks) > 128 or any(
        not isinstance(slug, str) or not SLUG.fullmatch(slug) for slug in fallbacks
    ) or len(set(fallbacks)) != len(fallbacks):
        raise ValueError("Invalid bounding-box fallbacks")
    count = config["initial_point_count"]
    if type(count) is not int or not 0 <= count <= 10000000:
        raise ValueError("Invalid initial point count")
    source = config["source_map_url"]
    if not isinstance(source, str) or not re.fullmatch(r"https://[A-Za-z0-9./:?&=%_+#~-]+", source):
        raise ValueError("Invalid public source-map URL")
    parsed = urlsplit(source)
    if not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Invalid public source-map origin")
    boundary = config["city_boundaries_url"]
    if not isinstance(boundary, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,160}", boundary) or ".." in boundary.split("/"):
        raise ValueError("Boundary must be a local public asset path")
    suffix = config["candidate_count_suffix"]
    if not isinstance(suffix, str) or not 1 <= len(suffix) <= 200:
        raise ValueError("Invalid candidate count label")
    if not isinstance(profile_renderer, str) or len(profile_renderer.encode("utf-8")) > MAX_BYTES:
        raise ValueError("Invalid trusted profile renderer")
    tokens = {
        "GROUP_SLUG": json.dumps(config["group_slug"]),
        "CANDIDATE_PROPERTY": prop,
        "BOUNDING_BOX_FALLBACKS": "[" + ",".join("'" + slug + "'" for slug in fallbacks) + "]",
        "SOURCE_MAP_URL": source,
        "CITY_BOUNDARIES_URL": json.dumps(boundary),
        "INITIAL_POINT_COUNT": str(count),
        "CANDIDATE_COUNT_SUFFIX": json.dumps(suffix, ensure_ascii=False),
        "PROFILE_RENDERER": profile_renderer,
    }
    template = ASSET.read_text(encoding="utf-8")
    token = re.compile(r"\{\{([A-Z_]+)\}\}")
    if set(token.findall(template)) != set(tokens):
        raise ValueError("Map template contract mismatch")
    return token.sub(lambda match: tokens[match.group(1)], template)


def main():
    try:
        raw = sys.stdin.buffer.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError("Map input too large")
        value = json.loads(raw)
        if not isinstance(value, dict) or set(value) != {"config", "profile_renderer"}:
            raise ValueError("Invalid map build payload")
        sys.stdout.write(json.dumps({"javascript": render(value["config"], value["profile_renderer"])}))
    except (ValueError, TypeError, OSError) as exc:
        print("Map build stopped: " + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
