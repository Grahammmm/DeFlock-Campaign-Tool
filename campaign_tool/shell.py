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


def exact(value, keys, label):
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError("Invalid " + label + " fields")


def substitute(template, fields):
    if set(TOKEN.findall(template)) != set(fields):
        raise ValueError("Template contract mismatch")
    # One pass: text inside a supplied fragment is never evaluated as a template.
    return TOKEN.sub(lambda match: fields[match.group(1)], template)


def render(payload):
    exact(payload, {"schema_version", "language", "html", "styles"}, "shell")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
        raise ValueError("Unsupported shell schema version")
    language = payload["language"]
    if not isinstance(language, str) or not re.fullmatch(r"[a-zA-Z]{2,8}(?:-[a-zA-Z0-9]{1,8})*", language):
        raise ValueError("Invalid language tag")
    exact(payload["html"], HTML_KEYS, "HTML")
    exact(payload["styles"], STYLE_KEYS, "style")
    values = list(payload["html"].values()) + list(payload["styles"].values())
    if any(not isinstance(value, str) for value in values):
        raise ValueError("Shell slots must be strings")
    if sum(len(value.encode("utf-8")) for value in values) > MAX_INPUT:
        raise ValueError("Shell input too large")
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
