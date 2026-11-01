"""Render agency/finding/source-link UI from reviewed, campaign-owned settings."""
import json
import re
import sys
import unicodedata
from pathlib import Path

ASSET = Path(__file__).parent / "assets" / "agency-cards.js.tmpl"
MAX_BYTES = 2 * 1024 * 1024
KEYS = {"schema_version", "county_profile", "source_page", "default_fragment",
        "source_aliases", "source_link_label"}
PROFILE_FIELDS = {"name", "vendor", "status", "summary", "cameraCount", "facts", "open", "flags"}
SLUG = re.compile(r"[a-z][a-z0-9-]{0,63}")
NAME = re.compile(r"[A-Za-z0-9_]{1,64}")


def visible(char):  # refuse controls (except newline/tab), invisible format/bidi chars and line/paragraph separators
    return char in "\n\t" or (unicodedata.category(char) not in ("Cc", "Cf") and char not in "\u2028\u2029")


def text(value):
    return (isinstance(value, str) and len(value) <= 20000 and "<" not in value and ">" not in value
            and all(map(visible, value)))


def keyset(value, keys, prefix=""):  # an odd key name is shown by its length only
    named = lambda key: prefix + key if isinstance(key, str) and NAME.fullmatch(key) else f"{prefix}{len(str(key))} chars"
    return ["missing field: " + prefix + key for key in keys - set(value)] + [
        "unknown field: " + named(key) for key in value if key not in keys]


def problems(config):
    """Every agency-card input problem, sorted; each names its field (dotted), never its value."""
    if not isinstance(config, dict):
        return ["config: must be an object"]
    found, slug = keyset(config, KEYS), lambda v: isinstance(v, str) and SLUG.fullmatch(v)
    if "schema_version" in config and (type(config["schema_version"]) is not int or config["schema_version"] != 1):
        found.append("schema_version: unsupported schema version")
    profile = config.get("county_profile", {})
    if not isinstance(profile, dict):
        found.append("county_profile: must be an object")
        profile = {}
    elif "county_profile" in config:
        found += keyset(profile, PROFILE_FIELDS, "county_profile.")
    for key in sorted(PROFILE_FIELDS & set(profile)):
        value = profile[key]
        if key not in ("facts", "flags"):
            found += [] if text(value) else [f"county_profile.{key}: not plain text"]
        elif not isinstance(value, list) or len(value) > 100:
            found.append(f"county_profile.{key}: must be a list of at most 100 items")
        else:
            found += [f"county_profile.{key}: item {i} not plain text" for i, item in enumerate(value) if not text(item)]
    if "source_page" in config and not (isinstance(config["source_page"], str) and
                                        re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]*\.html", config["source_page"])):
        found.append("source_page: not a local .html file name")
    if "default_fragment" in config and not slug(config["default_fragment"]):
        found.append("default_fragment: invalid slug")
    aliases = config.get("source_aliases", {})
    if not isinstance(aliases, dict) or len(aliases) > 100:
        found.append("source_aliases: must be an object of at most 100 pairs")
    elif bad := sum(1 for key, value in aliases.items() if not slug(key) or not slug(value)):
        found.append(f"source_aliases: invalid slug pair (count {bad})")
    if "source_link_label" in config and (not text(config["source_link_label"]) or not config["source_link_label"].strip()):
        found.append("source_link_label: blank or not plain text")
    return sorted(found)


def render(config):
    found = problems(config)
    if found:
        raise ValueError("; ".join(found))
    profile = config["county_profile"]
    page = config["source_page"]
    fragment = config["default_fragment"]
    aliases = config["source_aliases"]
    label = config["source_link_label"]
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
        print("Agency-card check stopped: config file missing, unreadable, over 2 MB, not UTF-8 JSON or not an object",
              file=sys.stderr)
        return 2
    found = problems(config)
    print("\n".join(found + [f"problems={len(found)}"]))
    return 1 if found else 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv:
        if len(argv) == 2 and argv[0] == "--check":
            return check(argv[1])
        print("usage: agency_cards [--check CONFIG.json]  (no arguments: build from stdin)", file=sys.stderr)
        return 2
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
