"""Render agency/finding/source-link UI from reviewed, campaign-owned settings."""
import json
import re
import sys
from pathlib import Path

ASSET = Path(__file__).parent / "assets" / "agency-cards.js.tmpl"
MAX_BYTES = 2 * 1024 * 1024
KEYS = {"schema_version", "county_profile", "source_page", "default_fragment",
        "source_aliases", "source_link_label"}
PROFILE_FIELDS = {"name", "vendor", "status", "summary", "cameraCount", "facts", "open", "flags"}
SLUG = re.compile(r"[a-z][a-z0-9-]{0,63}")


def text(value):
    return isinstance(value, str) and len(value) <= 20000 and "<" not in value and ">" not in value


def render(config):
    if not isinstance(config, dict) or set(config) != KEYS:
        raise ValueError("Invalid agency-card fields")
    if type(config["schema_version"]) is not int or config["schema_version"] != 1:
        raise ValueError("Unsupported agency-card schema")
    profile = config["county_profile"]
    if not isinstance(profile, dict) or set(profile) != PROFILE_FIELDS:
        raise ValueError("Invalid county profile")
    for key, value in profile.items():
        if key in ("facts", "flags"):
            if not isinstance(value, list) or len(value) > 100 or any(not text(item) for item in value):
                raise ValueError("Invalid plain-text profile list")
        elif not text(value):
            raise ValueError("Invalid plain-text profile field")
    page = config["source_page"]
    if not isinstance(page, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]*\.html", page):
        raise ValueError("Source page must be a local HTML filename")
    fragment = config["default_fragment"]
    if not isinstance(fragment, str) or not SLUG.fullmatch(fragment):
        raise ValueError("Invalid default source fragment")
    aliases = config["source_aliases"]
    if not isinstance(aliases, dict) or len(aliases) > 100 or any(
        not isinstance(key, str) or not SLUG.fullmatch(key) or
        not isinstance(value, str) or not SLUG.fullmatch(value)
        for key, value in aliases.items()
    ):
        raise ValueError("Invalid source aliases")
    label = config["source_link_label"]
    if not text(label) or not label.strip():
        raise ValueError("Invalid source-link label")
    quote = lambda value: json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    route = "".join("slug===" + quote(key) + "?" + quote(value) + ":" for key, value in aliases.items())
    route += "slug||" + quote(fragment)
    tokens = {
        "COUNTY_PROFILE": quote(profile),
        "SOURCE_PREFIX": quote(page + "#"),
        "SOURCE_ROUTE": route,
        "SOURCE_LINK_LABEL": quote(label)
    }
    template = ASSET.read_text(encoding="utf-8")
    token = re.compile(r"\{\{([A-Z_]+)\}\}")
    if set(token.findall(template)) != set(tokens):
        raise ValueError("Agency-card template contract mismatch")
    return token.sub(lambda match: tokens[match.group(1)], template)


def main():
    try:
        raw = sys.stdin.buffer.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError("Agency-card input too large")
        sys.stdout.write(json.dumps({"javascript": render(json.loads(raw))}))
    except (ValueError, TypeError, OSError) as exc:
        print("Agency-card build stopped: " + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
