"""Deterministic page shell for trusted, reviewed campaign fragments.

This is a build-time renderer, not an HTML sanitizer. Never pass record originals,
mail, LLM output or visitor input into its raw HTML/CSS slots.
"""
import json
import re
import sys
from pathlib import Path

ASSETS = Path(__file__).parent / "assets"
MAX_INPUT = 2 * 1024 * 1024
HTML_KEYS = {"head", "header", "main", "footer"}
STYLE_KEYS = {"font_family", "font_faces"}
TOKEN = re.compile(r"\{\{([A-Z_]+)\}\}")
NAME = re.compile(r"[A-Za-z0-9_]{1,64}")


def keyset(value, keys, prefix=""):  # an odd key name is shown by its length only
    named = lambda key: prefix + key if isinstance(key, str) and NAME.fullmatch(key) else f"{prefix}{len(str(key))} chars"
    return ["missing field: " + prefix + key for key in keys - set(value)] + [
        "unknown field: " + named(key) for key in value if key not in keys]


def exact(value, keys, label):
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError("Invalid " + label + " fields")


def problems(payload):
    """Every shell-input problem, sorted; each names its field (dotted), never its value."""
    if not isinstance(payload, dict):
        return ["payload: must be an object"]
    found = keyset(payload, {"schema_version", "language", "html", "styles"})
    if "schema_version" in payload and (type(payload["schema_version"]) is not int or payload["schema_version"] != 1):
        found.append("schema_version: unsupported")
    language = payload.get("language", "en")
    if not isinstance(language, str) or not re.fullmatch(r"[a-zA-Z]{2,8}(?:-[a-zA-Z0-9]{1,8})*", language):
        found.append("language: invalid tag")
    size = 0
    for name, keys in (("html", HTML_KEYS), ("styles", STYLE_KEYS)):
        part = payload.get(name, {})
        if not isinstance(part, dict):
            found.append(name + ": must be an object")
            continue
        found += keyset(part, keys, name + ".") if name in payload else []
        for key in sorted(keys & set(part)):
            if isinstance(part[key], str):
                size += len(part[key].encode("utf-8"))
            else:
                found.append(f"{name}.{key}: must be a string")
    if size > MAX_INPUT:
        found.append("input: too large")
    return sorted(found)


def substitute(template, fields):
    if set(TOKEN.findall(template)) != set(fields):
        raise ValueError("Template contract mismatch")
    # One pass: text inside a supplied fragment is never evaluated as a template.
    return TOKEN.sub(lambda match: fields[match.group(1)], template)


def render(payload):
    found = problems(payload)
    if found:
        raise ValueError("; ".join(found))
    language = payload["language"]
    html_fields = {key.upper(): value for key, value in payload["html"].items()}
    html_fields["LANGUAGE"] = language
    return {
        "html": substitute((ASSETS / "shell.html.tmpl").read_text(encoding="utf-8"), html_fields),
        "css": substitute((ASSETS / "styles.css.tmpl").read_text(encoding="utf-8"),
                          {key.upper(): value for key, value in payload["styles"].items()})
    }


def main():
    try:
        raw = sys.stdin.buffer.read(MAX_INPUT + 1)
        if len(raw) > MAX_INPUT:
            raise ValueError("Shell input too large")
        result = render(json.loads(raw))
        sys.stdout.write(json.dumps(result, ensure_ascii=True))
    except (ValueError, TypeError, OSError) as exc:
        print("Shell build stopped: " + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
