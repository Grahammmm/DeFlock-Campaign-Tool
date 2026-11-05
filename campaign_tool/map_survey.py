"""DF0237-13: a private camera field-survey sheet (``map survey-sheet``) and its sourced re-import (``map survey-import``).

The sheet lists the community-mapped points inside one exactly named city boundary, with four blank columns for a
new observation. The import turns filled rows into ``content/map/point-checks.json`` entries and validates the
merged document with ``content.point_check_problems`` unchanged. The tool never fills in, defaults, copies or
infers a status, date, source or note. Diagnostics name a row or feature index and a field, never a value.
Nothing is published: the next ``build`` is the only thing that reads the checks."""
import csv
import hashlib
import html
import io
import json
import os
import re
import stat
import tempfile
import unicodedata
from datetime import date
from pathlib import Path

from . import camera_counts, content, kit_export

HEADER = ("node_url", "latitude", "longitude", "manufacturer", "operator", "direction", "latest_status",
          "latest_checked_on", "latest_source", "latest_note", "status", "checked_on", "source", "note")
FILLED = HEADER[-4:]
MAX_CSV_BYTES = 2 * 1024 * 1024
ROW_PROBLEM = re.compile(r"map/point-checks\.json\.checks\[(\d+)\]\.(\w+): (.*)")
NOTE = "Private worksheet; nothing was built, imported or published."


class SurveyError(content.ContentError):
    """A survey refusal; the message names an index, row and field, never a supplied value."""


def _plain_dir(path, label):
    if path.is_symlink():
        raise SurveyError(f"{label}: symlinks are not followed")
    if path.exists() and not path.is_dir():
        raise SurveyError(f"{label}: not a folder")


def _load(root, day):
    root = Path(root)
    for rel in ("content", "content/map"):
        _plain_dir(root / rel, rel)
    loaded = content.load_map(root / "content", today=day)
    if loaded is None:
        raise SurveyError("map: no configured camera map")
    cameras = content._read_json(loaded["cameras_path"], content.MAX_GEOJSON_BYTES)
    return loaded, cameras


def _in_city(cameras, boundaries, city):
    """Indexes of camera features inside every boundary feature named exactly ``city`` (each once)."""
    counts, problems = camera_counts.count_by_name(cameras, boundaries)
    if problems:
        raise SurveyError("city boundaries: " + problems[0])
    if city not in counts:
        raise SurveyError("--city: unknown city boundary name")
    found = set()
    for feature in boundaries["features"]:
        if camera_counts._name(feature) != city:
            continue
        for rings in camera_counts._polygons(feature["geometry"]):
            xs, ys = [p[0] for p in rings[0]], [p[1] for p in rings[0]]
            west, east, south, north = min(xs), max(xs), min(ys), max(ys)
            outer, holes = camera_counts._Ring(rings[0]), [camera_counts._Ring(h) for h in rings[1:]]
            for index, feat in enumerate(cameras.get("features") or []):
                geometry = feat.get("geometry") if isinstance(feat, dict) else None
                pos = geometry.get("coordinates") if isinstance(geometry, dict) else None
                if index in found or not (isinstance(pos, list) and len(pos) >= 2
                                          and camera_counts._number(pos[0]) and camera_counts._number(pos[1])):
                    continue
                x, y = pos[0], pos[1]
                if west <= x <= east and south <= y <= north and outer.contains(x, y) \
                        and not any(hole.contains(x, y) for hole in holes):
                    found.add(index)
    return sorted(found)


def _text(value):
    return "" if value is None else str(value)


def survey_rows(root, city, *, today=None):
    """``(rows, loaded_map, day)``; rows are dicts in HEADER order, sorted by node URL, final four fields blank."""
    day = today or date.today()
    loaded, cameras = _load(root, day)
    boundaries = content._read_json(loaded["boundary_path"], content.MAX_GEOJSON_BYTES)
    rows, seen = [], set()
    for index in _in_city(cameras, boundaries, city):
        feature = cameras["features"][index]
        where = f"map/cameras.geojson features[{index}]"
        lon, lat = feature["geometry"]["coordinates"][:2]
        if not (-180 <= lon <= 180 and -90 <= lat <= 90):
            raise SurveyError(f"{where}.coordinates: out of range")
        props = feature.get("properties") if isinstance(feature.get("properties"), dict) else {}
        url = props.get("url")
        match = content.OSM_NODE_URL.fullmatch(url) if isinstance(url, str) else None
        if not match or int(match.group(1)) < 1 or str(int(match.group(1))) != match.group(1):
            raise SurveyError(f"{where}.url: expected a canonical OpenStreetMap node URL")
        if url in seen:
            raise SurveyError(f"{where}.url: duplicate node URL")
        seen.add(url)
        newest = (loaded["point_checks"].get(int(match.group(1))) or [{}])[0]
        direction = props.get("direction")
        row = {"node_url": url, "latitude": _text(lat), "longitude": _text(lon),
               "manufacturer": _text(props.get("manufacturer")), "operator": _text(props.get("operator")),
               "direction": direction if isinstance(direction, str) else "",
               "latest_status": newest.get("status", ""), "latest_checked_on": newest.get("checked_on", ""),
               "latest_source": newest.get("source", ""), "latest_note": newest.get("note", "")}
        row.update({key: "" for key in FILLED})
        rows.append(row)
    return sorted(rows, key=lambda row: row["node_url"]), loaded, day


def render_csv(rows):
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(HEADER)
    for row in rows:
        writer.writerow([kit_export.formula_safe(_text(row[key])) for key in HEADER])
    return buffer.getvalue().encode("utf-8")


CSS = ("@page{size:landscape;margin:12mm}body{font:12px/1.35 sans-serif;margin:1em}"
       "table{border-collapse:collapse;width:100%}thead{display:table-header-group}"
       "th,td{border:1px solid #444;padding:4px;vertical-align:top;overflow-wrap:anywhere;word-break:break-word}"
       "tr{break-inside:avoid;page-break-inside:avoid}td.blank{min-width:9em;height:3.2em}")
HUMAN = ("Node URL", "Latitude", "Longitude", "Manufacturer", "Operator", "Direction", "Latest status",
         "Latest checked on", "Latest source", "Latest note", "New status", "New checked on", "New source", "New note")


def render_sheet(city, day, rows, attribution):
    esc = html.escape
    head = "".join(f"<th scope=\"col\">{esc(name)}</th>" for name in HUMAN)
    body = "".join("<tr>" + "".join(f"<td>{esc(_text(row[key]))}</td>" for key in HEADER[:10])
                   + "<td class=\"blank\"></td>" * 4 + "</tr>" for row in rows)
    table = (f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>" if rows
             else "<p>No mapped points inside this city boundary.</p>")
    statuses = ", ".join(content.POINT_CHECK_STATUSES)
    page = (f"<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><title>Camera field survey: {esc(city)}"
            f"</title><style>{CSS}</style></head><body><h1>Camera field survey: {esc(city)}</h1>"
            f"<p>{esc(day.isoformat())}. Private worksheet; nothing is published.</p>"
            "<p>Community-mapped points; ownership unconfirmed.</p>"
            "<p>Supply your own source for every new check. A previous check's source is not evidence for a new check.</p>"
            f"<p>Status is one of: {esc(statuses)}. Source is an https:// URL (no userinfo, at most "
            f"{content.MAX_POINT_SOURCE} characters) or a 12-hex library prefix that matches exactly one library entry.</p>"
            "<p>Do not record people, plates, addresses or phone numbers. Stay on public property.</p>"
            "<p>Fill in the CSV copy of this sheet, not this page; an organizer brings it back with "
            "map survey-import. Nothing is published until the next build.</p>"
            f"{table}<p>{esc(attribution)}</p><p>ODbL 1.0; share-alike applies to reused map data.</p></body></html>\n")
    return page.encode("utf-8")


def stem(city):
    ascii_name = unicodedata.normalize("NFKD", city).encode("ascii", "ignore").decode("ascii").lower()
    name = re.sub(r"[^a-z0-9]+", "-", ascii_name).strip("-")[:72].strip("-")
    return name or "city-" + hashlib.sha256(city.encode("utf-8")).hexdigest()[:12]


def _private_dir(path):
    if path.is_symlink():
        raise SurveyError(f"{path.name}: symlinks are not followed")
    if not path.exists():
        path.mkdir(mode=0o700)
        os.chmod(path, 0o700)
        return
    if not path.is_dir():
        raise SurveyError(f"{path.name}: not a folder")


def _install_new(directory, name, data):
    """Stage ``data`` in a fresh exclusive temp file, then link it to ``name`` without overwriting anything."""
    fd, temp = tempfile.mkstemp(dir=directory, prefix=f".{name}.", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            fd = None
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temp, directory / name)
        except FileExistsError:
            raise SurveyError(f"kit/survey/{name}: exists; nothing was overwritten") from None
    finally:
        if fd is not None:
            os.close(fd)
        os.unlink(temp)


def survey_sheet(root, city, *, today=None):
    rows, loaded, day = survey_rows(root, city, today=today)
    html_bytes, csv_bytes = render_sheet(city, day, rows, loaded["attribution"]), render_csv(rows)
    root = Path(root)
    base = f"{stem(city)}-{day.isoformat()}"
    names = (base + ".html", base + ".csv")
    folder = root / "kit" / "survey"
    for part in (root / "kit", folder):
        _plain_dir(part, f"kit/{part.name}" if part.name == "survey" else part.name)
    for name in names:
        if os.path.lexists(folder / name):
            raise SurveyError(f"kit/survey/{name}: exists; nothing was overwritten")
    _private_dir(root / "kit")
    _private_dir(folder)
    done = []
    try:
        for name, data in zip(names, (html_bytes, csv_bytes)):
            _install_new(folder, name, data)
            done.append(folder / name)
    except BaseException:
        for path in done:
            os.unlink(path)
        raise
    return {"written": [f"kit/survey/{name}" for name in names], "city": city, "date": day.isoformat(),
            "points": len(rows), "note": NOTE}


def _read_sheet(csv_path):
    path = Path(csv_path)
    if path.is_symlink() or not path.is_file():
        raise SurveyError("--csv: not a regular file")
    data = path.read_bytes()
    if len(data) > MAX_CSV_BYTES:
        raise SurveyError("--csv: file too large")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise SurveyError("--csv: not UTF-8") from None
    try:
        table = list(csv.reader(io.StringIO(text, newline="")))
    except csv.Error:
        raise SurveyError("--csv: not valid CSV") from None
    if not table or tuple(table[0]) != HEADER:
        raise SurveyError("--csv: the header row does not match the survey sheet")
    return table[1:]


def _existing(root):
    path = Path(root) / "content" / "map" / "point-checks.json"
    if path.is_symlink():
        raise SurveyError("map/point-checks.json: symlinks are not read")
    if not os.path.lexists(path):
        return path, {"schema_version": 1, "checks": []}
    return path, content._read_json(path, content.MAX_POINT_CHECKS_BYTES)


def survey_import(root, csv_path, *, apply=False, today=None):
    day = today or date.today()
    root = Path(root)
    loaded, cameras = _load(root, day)
    path, document = _existing(root)
    table = _read_sheet(csv_path)
    nodes = content.point_nodes(cameras)
    problems, additions, seen = [], [], set()
    for number, cells in enumerate(table, start=2):  # the header is row 1, as in a spreadsheet
        if len(cells) != len(HEADER):
            problems.append(f"row {number}: wrong number of columns")
            continue
        row = dict(zip(HEADER, cells))
        if not any(row[key].strip() for key in FILLED):
            continue
        match = content.OSM_NODE_URL.fullmatch(row["node_url"])
        node = int(match.group(1)) if match else None
        if node is None or node < 1 or row["node_url"] != f"https://www.openstreetmap.org/node/{node}":
            problems.append(f"row {number} node_url: edited; it must be a node URL exactly as the sheet wrote it")
            continue
        if node not in nodes:
            problems.append(f"row {number} node_url: not a mapped point")
            continue
        for key in FILLED[:3]:
            if not row[key].strip():
                problems.append(f"row {number} {key}: required")
        entry = {"node": node, "status": row["status"], "checked_on": row["checked_on"], "source": row["source"]}
        if row["note"].strip():
            entry["note"] = row["note"]
        known = {(c["status"], c["checked_on"], c["source"], c.get("note", "")) for c in loaded["point_checks"].get(node, [])}
        key = (entry["status"], entry["checked_on"], entry["source"], entry.get("note", ""))
        if key in known or (node, key) in seen:
            problems.append(f"row {number}: duplicate of an existing check")
        seen.add((node, key))
        additions.append((number, entry))
    merged = {"schema_version": 1, "checks": list(document["checks"]) + [entry for _, entry in additions]}
    if additions and not problems:
        library = root / "content" / "sources.json"
        sources = content.load_sources(content._read_json(library)) if library.is_file() else []
        with tempfile.TemporaryDirectory(prefix="survey-import-") as scratch:
            probe = Path(scratch) / "point-checks.json"
            probe.write_text(json.dumps(merged), encoding="utf-8")
            found, _ = content.point_check_problems(probe, nodes, sources, day)
        offset = len(document["checks"])
        for text in found:
            hit = ROW_PROBLEM.fullmatch(text)
            if hit and int(hit.group(1)) >= offset:
                problems.append(f"row {additions[int(hit.group(1)) - offset][0]} {hit.group(2)}: {hit.group(3)}")
            else:
                problems.append(text)
    if problems:
        raise SurveyError("; ".join(problems[:40]) + ("; more not shown" if len(problems) > 40 else ""))
    report = {"applied": False, "added": len(additions), "checks": [entry for _, entry in additions],
              "note": "Dry run: nothing was written. Rerun with --apply; the checks reach the site at the next build."}
    if apply and additions:
        _write_checks(path, merged)
        report.update(applied=True, note="Checks written to content/map/point-checks.json; "
                                         "nothing is published until the next build.")
    elif apply:
        report.update(applied=True, note="No filled rows; nothing was written.")
    return report


def _write_checks(path, document):
    """Fresh temp file beside the target, fsync, then os.replace; a symlink target is refused."""
    if path.is_symlink():
        raise SurveyError("map/point-checks.json: symlinks are not written")
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644
    data = (json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".point-checks.", suffix=".tmp")
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            fd = None
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except BaseException:
        if fd is not None:
            os.close(fd)
        if os.path.lexists(temp):
            os.unlink(temp)
        raise
