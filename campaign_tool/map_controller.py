"""Build the shared map controller from trusted campaign settings and a UI hook."""
import json
import os
import re
import stat
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


def _port(value):
    """True unless the authority of an otherwise admitted https URL carries an impossible port.

    Pure and value-free: omitted or empty ports and numeric 0-65535 pass; urllib's own port
    parse refuses nonnumeric, negative, fractional, multiple-colon and out-of-range ports. The
    old type/scheme/character and host diagnostics are left to _https and _origin."""
    if not _https(value):
        return True
    parts = urlsplit(value)
    if not parts.hostname:
        return True
    try:
        parts.port
    except ValueError:
        return False
    return True


_REFUSED = -1  # sentinel from _utf8_size: not a str, or not strictly encodable (e.g. a lone surrogate)


def _utf8_size(value):
    """Strict UTF-8 byte length of a str, or _REFUSED. Never raises, never repairs the text."""
    if not isinstance(value, str):
        return _REFUSED
    try:
        return len(value.encode("utf-8"))
    except UnicodeEncodeError:
        return _REFUSED


CHECKS = (  # (field, accepts value, reason); reasons never include the value
    ("schema_version", lambda v: type(v) is int and v == 1, "unsupported schema version"),
    ("group_slug", lambda v: isinstance(v, str) and SLUG.fullmatch(v), "invalid slug"),
    ("candidate_property", lambda v: isinstance(v, str) and re.fullmatch(r"[a-z][a-zA-Z0-9]{0,63}", v), "invalid identifier"),
    ("bounding_box_fallbacks", lambda v: isinstance(v, list) and len(v) <= 128 and all(
        isinstance(s, str) and SLUG.fullmatch(s) for s in v) and len(set(v)) == len(v), "invalid or duplicate slugs"),
    ("initial_point_count", lambda v: type(v) is int and 0 <= v <= 10000000, "out of range"),
    ("source_map_url", _https, "not a public https URL"),
    ("source_map_url", _origin, "credentials or no host in URL"),
    ("source_map_url", _port, "invalid port in URL"),
    ("city_boundaries_url", lambda v: isinstance(v, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,160}", v) and ".." not in v.split("/"), "not a local public asset path"),
    ("candidate_count_suffix", lambda v: isinstance(v, str) and 1 <= len(v) <= 200, "must be 1-200 characters"),
    # the suffix is emitted into UTF-8 JavaScript; judged only when the type/length check above passed
    ("candidate_count_suffix", lambda v: not (isinstance(v, str) and 1 <= len(v) <= 200) or _utf8_size(v) != _REFUSED,
     "not valid UTF-8 text"),
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
        size = _utf8_size(value)
        if size == _REFUSED or size > MAX_BYTES:
            found.append(name + ": invalid trusted renderer")
    return sorted(found)


# DF0137-14: with relabel=True each fixed English fragment becomes a lookup in LABELS (site-data.js). Label text reaches
# the page through textContent (fmt, fill) or the existing mapText escaper; it is never inserted as raw HTML.
RELABEL = (
    ('document.getElementById("map-selection-count").textContent=count+(slug==={{GROUP_SLUG}}?{{CANDIDATE_COUNT_SUFFIX}}'
     ':(count===1?" mapped point":" mapped points"));',
     'document.getElementById("map-selection-count").textContent=slug==={{GROUP_SLUG}}?count+{{CANDIDATE_COUNT_SUFFIX}}'
     ':fmt(LABELS[count===1?"js.mapped_point":"js.mapped_points"],{count:count});'),
    ('notice.innerHTML="The interactive map could not load in this browser. You can still select a city below, '
     '<a href=\\"sources.html#library\\">review the evidence notes</a>, or <a href=\\"{{SOURCE_MAP_URL}}\\" '
     'target=\\"_blank\\" rel=\\"noopener noreferrer\\">open the DeFlock source map</a>.";',
     'fill(notice,LABELS["js.map_failed"],{evidence:["sources.html#library",LABELS["js.map_evidence"]],'
     'source:["{{SOURCE_MAP_URL}}",LABELS["js.map_source"],1]});'),
    ('fallback.innerHTML="Camera data could not load. <a href=\\"{{SOURCE_MAP_URL}}\\" target=\\"_blank\\" '
     'rel=\\"noopener noreferrer\\">Open the DeFlock source map</a>";',
     'fill(fallback,LABELS["js.data_failed"],{source:["{{SOURCE_MAP_URL}}",LABELS["js.data_source"],1]});'),
    ('"<strong>Community-mapped ALPR point</strong><br>"', '"<strong>"+mapText(LABELS["js.popup_title"])+"</strong><br>"'),
    ('"Manufacturer not specified"', 'LABELS["js.popup_no_manufacturer"]'),
    ('"Operator not specified"', 'LABELS["js.popup_no_operator"]'),
    ('>View source record &nearr;</a>"', '>"+mapText(LABELS["js.popup_source"])+" &nearr;</a>"'),
)
RELABEL_HELPERS = (
    r'  function fmt(s,o){return String(s).replace(/\{([a-z_]+)\}/g,function(m,k){return Object.prototype.hasOwnProperty.call(o,k)?String(o[k]):m;});}'
    "\n"
    r'  function fill(el,s,links){el.textContent="";String(s).split(/(\{[a-z_]+\})/).forEach(function(part){var k=/^\{([a-z_]+)\}$/.exec(part),l=k&&Object.prototype.hasOwnProperty.call(links,k[1])?links[k[1]]:null,a;'
    r'if(l){a=document.createElement("a");a.href=l[0];a.textContent=l[1];if(l[2]){a.target="_blank";a.rel="noopener noreferrer";}el.appendChild(a);}else if(part)el.appendChild(document.createTextNode(part));});}'
    "\n")


def render(config, profile_renderer, point_context_renderer="", relabel=False):
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
    if relabel:
        for old, new in RELABEL:
            if template.count(old) != 1:
                raise ValueError("Map template contract mismatch")
            template = template.replace(old, new)
        template += RELABEL_HELPERS
    return token.sub(lambda match: tokens[match.group(1)], template)


def _refuse(name):
    raise ValueError("non-finite number")


def _open_regular(path):
    """Open one nonblocking descriptor, require it be a regular file, and hand it to a stream.

    A FIFO without a writer must not block; the admitted descriptor is the only one ever read."""
    flag = getattr(os, "O_NONBLOCK", None)
    if flag is None:
        raise ValueError("nonblocking open unavailable")
    fd = os.open(path, os.O_RDONLY | flag)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("not a regular file")
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise


def check(path):
    """Print each config problem (never a value) then problems=N; exit 0 none, 1 problems, 2 unreadable."""
    try:
        with _open_regular(path) as handle:
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
        try:
            value = json.loads(raw)
        except RecursionError:  # decoder-only: the stdlib's own refusal of overdeep nesting
            raise ValueError("JSON nesting is too deep") from None
        if not isinstance(value, dict) or set(value) not in ({"config", "profile_renderer"}, {"config", "profile_renderer", "point_context_renderer"}):
            raise ValueError("Invalid map build payload")
        sys.stdout.write(json.dumps({"javascript": render(value["config"], value["profile_renderer"], value.get("point_context_renderer", ""))}))
    except (ValueError, TypeError, OSError) as exc:
        print("Map build stopped: " + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
