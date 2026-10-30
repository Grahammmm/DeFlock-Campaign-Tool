"""Manual private outcome reader; no decisions, network or publication."""
import argparse
from contextlib import contextmanager
import hashlib
import html
import json
import os
from pathlib import Path
import re
import stat
import sys


BANNER = "PRIVATE — REVIEW REQUIRED — NOT FOR PUBLICATION"
INPUTS = (
    ("factsheets", "Agency fact sheets", "agency-factsheets.json", "records-agency-factsheets-v1", "agencies"),
    ("comparison", "Policy comparison", "comparison.json", "records-agency-comparison-v1", "rows"),
    ("register", "Request register", "CURRENT-REQUEST-REGISTER.json", "records-request-register-v1", "requests"),
)
STYLE = """
*{box-sizing:border-box}body{margin:0;background:#fffdf6;color:#173e3a;font:18px/1.5 system-ui,sans-serif}
header,main,footer{max-width:1100px;margin:auto;padding:20px}h1{line-height:1.2}h3{margin-top:32px}
a{display:inline-block;min-height:44px;padding:10px;color:#173e3a}nav{display:flex;flex-wrap:wrap;gap:8px}
:focus-visible{outline:3px solid #804000;outline-offset:3px}.notice{padding:16px;border:2px solid #173e3a}
dl{margin:0}dt{font-weight:bold}dd{margin:0 0 12px 16px;white-space:pre-wrap;overflow-wrap:anywhere}
code,q,th{overflow-wrap:anywhere}table{width:100%;border-collapse:collapse}th,td{padding:12px;text-align:left;vertical-align:top;border:1px solid #729186}
table,tbody,tr,th,td{display:block}tr{margin-bottom:16px}ul{padding-left:20px}
@media(min-width:800px){table{display:table}tbody{display:table-row-group}tr{display:table-row}th,td{display:table-cell}th{width:22%}}
"""


def esc(value):
    return html.escape(str(value), quote=True)


def _value(value):
    if isinstance(value, dict):
        if "document_sha256" in value and "page" in value:
            return "<code>" + esc(str(value["document_sha256"])[:12]) + "</code>, page " + esc(value["page"])
        if "source_id" in value and "locator" in value:
            return "<code>" + esc(value["source_id"]) + "</code>: " + _value(value["locator"])
        return "<dl>" + "".join("<dt>" + esc(k) + "</dt><dd>" +
            ("<q>" + esc(v) + "</q>" if k == "quote" else _value(v)) + "</dd>"
            for k, v in value.items()) + "</dl>"
    if isinstance(value, list):
        return "<ul>" + "".join("<li>" + _value(v) + "</li>" for v in value) + "</ul>"
    return esc(value)


def _section(report, key):
    rows = report[key]
    output = _value({k: v for k, v in report.items() if k != key})
    groups = {}
    for row in rows:
        agency = row.get("agency", row.get("agency_id", "Unknown agency"))
        groups.setdefault(str(agency), []).append(row)
    for agency, items in groups.items():
        output += "<h3>" + esc(agency) + "</h3>"
        if key == "agencies":
            output += "".join(_value(row) for row in items)
        else:
            output += "<table><tbody>" + "".join(
                '<tr><th scope="row">' + esc(row.get("field", row.get("request_id", "Record"))) +
                "</th><td>" + _value(row) + "</td></tr>" for row in items) + "</tbody></table>"
    return output


MAX_BYTES = 8 * 1024 * 1024


@contextmanager
def _directory(value):
    # Traverse descriptors, not resolved paths: no symlink can become an ancestor.
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise OSError("reading_unsafe_path")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except OSError:
                raise OSError("reading_unsafe_path") from None
            os.close(fd)
            fd = child
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", path.name):
            raise OSError("reading_run_id_invalid")
        if path.parent.name != "run-outputs":
            raise OSError("reading_not_run_outputs")
        yield fd
    finally:
        os.close(fd)


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("reading_duplicate_json_key")
        result[key] = value
    return result


def _constant(_):
    raise ValueError("reading_json_invalid")


def _load(fd, name, schema, key):
    try:
        source = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    except FileNotFoundError:
        return None, "Not produced in this run."
    except OSError:
        raise ValueError("reading_unsafe_path") from None
    with os.fdopen(source, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("reading_unsafe_path")
        if info.st_size > MAX_BYTES:
            raise ValueError("reading_input_bound")
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("reading_input_bound")
    try:
        report = json.loads(raw, object_pairs_hook=_unique, parse_constant=_constant)
        # Reject non-UTF-8 text (including escaped lone surrogates) per section.
        json.dumps(report, ensure_ascii=False).encode("utf-8")
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError("reading_json_invalid") from None
    if (type(report) is not dict or report.get("schema") != schema
            or type(report.get(key)) is not list or any(type(row) is not dict for row in report[key])):
        raise ValueError("reading_schema_invalid")
    try:
        body = _section(report, key)
    except (ValueError, TypeError, RecursionError):
        raise ValueError("reading_schema_invalid") from None
    return body, "Input: " + name + " sha256 " + hashlib.sha256(raw).hexdigest()[:12]


def render(run_dir):
    """Read only; same bytes in yield the same HTML bytes out."""
    with _directory(run_dir) as fd:
        return _render(fd, Path(run_dir).name)


def _render(fd, run_id):
    output = ['<!doctype html><html lang="en"><head><meta charset="utf-8">',
              '<meta name="viewport" content="width=device-width, initial-scale=1">',
              '<meta name="robots" content="noindex,nofollow"><title>Private run packet</title>',
              '<style>' + STYLE + '</style></head><body><header>',
              '<p class="notice">' + esc(BANNER) + '</p><a href="#content">Skip to content</a>',
              '<h1>Private run packet</h1><p>Run: ' + esc(run_id) + '</p><nav aria-label="Packet sections">']
    output.extend('<a href="#' + esc(i) + '">' + esc(title) + '</a>' for i, title, *_ in INPUTS)
    output.append('</nav></header><main id="content" tabindex="-1">')
    for identity, title, name, schema, key in INPUTS:
        try:
            body, status = _load(fd, name, schema, key)
        except ValueError as error:
            body, status = None, "Unreadable (" + str(error) + ")"
        except OSError:
            body, status = None, "Unreadable (reading_input_unreadable)"
        output += ['<section id="' + esc(identity) + '"><h2>' + esc(title) + '</h2>',
                   '<p>' + esc(status) + '</p>', body or '', '</section>']
    output.append('</main><footer><p class="notice">' + esc(BANNER) + '</p></footer></body></html>')
    return "\n".join(output) + "\n"


def _target_safe(fd):
    try:
        info = os.stat("reading.html", dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISREG(info.st_mode):
        raise OSError("reading_unsafe_path")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True)
    args = parser.parse_args(argv)
    try:
        with _directory(args.run_dir) as fd:
            text = _render(fd, Path(args.run_dir).name)
            _target_safe(fd)
            temporary = ".reading.html.tmp"
            source = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
            try:
                with os.fdopen(source, "w", encoding="utf-8") as stream:
                    os.fchmod(stream.fileno(), 0o600)
                    stream.write(text)
                    stream.flush()
                    os.fsync(stream.fileno())
                _target_safe(fd)
                os.replace(temporary, "reading.html", src_dir_fd=fd, dst_dir_fd=fd)
            finally:
                try:
                    os.unlink(temporary, dir_fd=fd)
                except FileNotFoundError:
                    pass
    except OSError as error:
        code = str(error)
        print(code if code in {"reading_unsafe_path", "reading_run_id_invalid", "reading_not_run_outputs"}
              else "reading_write_failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
