"""kit-agency atlas: cross-check a locally saved Atlas of Surveillance CSV against the kit. Private and read-only.

The organizer saves an Atlas export by hand; this module reads that one local file, the shipped agency seed for the
campaign's county and ``kit/agencies.json``, and reports three lists. It never fetches, writes, selects or publishes:
no network, no temporary file, no campaign file is touched. An Atlas listing is a lead, not evidence: it says nothing
about whether an agency operates plate readers, and a missing listing says nothing either.
"""
import csv
import io
import os
import stat
import unicodedata
from pathlib import Path

from . import discovery, kit, kit_agencies

ATLAS_MAX_BYTES = 10 * 1024 * 1024
REQUIRED_HEADERS = ("Agency", "City", "County", "State", "Technology")
VENDOR_HEADERS = ("Company", "Vendor")
NAME_SUFFIX_WORDS = ("department", "dept", "office")
TECHNOLOGY = "automated license plate readers"
NOTICE = "An Atlas listing is a lead, not evidence"
SECTIONS = (("listed_seed", "Seed agencies listed in Atlas"),
            ("unmatched_atlas", "Atlas rows without a unique seed match"),
            ("selected_not_listed", "Selected agencies not listed in Atlas"))
TRAILER = "Private, read-only: nothing was fetched, written, selected or published."
_CONTROL_CATEGORIES = ("Cc", "Cf", "Zl", "Zp")


class AtlasError(ValueError):
    """A refusal. Messages name the line and the rule, never a cell value."""


def _collapse(text):
    return " ".join(text.split()).casefold()


def normalize_agency_name(text):
    """Match key: casefold, apostrophes deleted, other punctuation to spaces, trailing suffix words removed."""
    text = str(text).casefold().replace("'", "").replace("\u2019", "")
    text = "".join(" " if unicodedata.category(char).startswith("P") else char for char in text)
    words = text.split()
    while words and words[-1] in NAME_SUFFIX_WORDS:
        words.pop()
    return " ".join(words)


def _county_key(text):
    text = _collapse(text)
    return text[:-len(" county")] if text.endswith(" county") else text


def _has_control(text):
    return any(unicodedata.category(char) in _CONTROL_CATEGORIES for char in text)


def _read_bytes(csv_path):
    try:
        fd = os.open(csv_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:  # O_NOFOLLOW: a symlink, dangling or not, fails here with ELOOP
        raise AtlasError(f"Atlas CSV: cannot be opened as a regular file (symlinks are refused): {exc.strerror}") from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise AtlasError("Atlas CSV: must be a regular file")
        with os.fdopen(fd, "rb") as handle:
            fd = None
            data = handle.read(ATLAS_MAX_BYTES + 1)
    except OSError as exc:
        raise AtlasError(f"Atlas CSV: cannot be read: {exc.strerror}") from None
    finally:
        if fd is not None:
            os.close(fd)
    if len(data) > ATLAS_MAX_BYTES:
        raise AtlasError("Atlas CSV: file is larger than 10 MiB")
    return data


def _records(text):
    """[(starting physical line, cells)] for every non-blank record; malformed CSV refuses the whole file."""
    reader, rows, start = csv.reader(io.StringIO(text, newline=""), strict=True), [], 1
    previous = csv.field_size_limit(ATLAS_MAX_BYTES)
    try:
        for cells in reader:
            if any(cell.strip() for cell in cells):
                rows.append((start, cells))
            start = reader.line_num + 1
    except csv.Error:
        raise AtlasError(f"Atlas CSV: line {start}: malformed quoting or field too large") from None
    finally:
        csv.field_size_limit(previous)
    return rows


def read_atlas_csv(path):
    """Return the data rows ``[{line, agency, city, county, state, technology, vendor}]`` of a local Atlas CSV.

    Any structural, encoding or field problem refuses the whole file, whatever the row's county.
    """
    data = _read_bytes(path)
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise AtlasError("Atlas CSV: file is not UTF-8") from None
    if "\x00" in text:
        raise AtlasError("Atlas CSV: file contains a NUL character")
    rows = _records(text)
    if not rows:
        raise AtlasError("Atlas CSV: file is empty or has no header row")
    header = rows[0][1]
    known = REQUIRED_HEADERS + VENDOR_HEADERS
    position, repeated = {}, []
    for place, name in enumerate(header):  # one pass: first position per exact name, in first-occurrence order
        if name in position:
            repeated.append(name)
        else:
            position[name] = place
    if repeated:
        repeats = set(repeated)
        name = next(name for name in position if name in repeats)  # earliest first occurrence among duplicates
        raise AtlasError("Atlas CSV: duplicate column: " + (name if name in known else "(an extra column)"))
    if missing := [name for name in REQUIRED_HEADERS if name not in position]:
        raise AtlasError("Atlas CSV: missing required column: " + ", ".join(missing))
    vendor_columns = [name for name in VENDOR_HEADERS if name in position]
    if len(vendor_columns) > 1:
        raise AtlasError("Atlas CSV: use one vendor column: Company or Vendor")
    index = {name: position[name] for name in REQUIRED_HEADERS + tuple(vendor_columns)}
    result = []
    for line, cells in rows[1:]:
        if len(cells) != len(header):
            raise AtlasError(f"Atlas CSV: line {line}: expected {len(header)} cells, found {len(cells)}")
        row = {"line": line}
        for name in REQUIRED_HEADERS + tuple(vendor_columns):
            value = cells[index[name]]
            if name != "City" and name not in VENDOR_HEADERS and not value.strip():
                raise AtlasError(f"Atlas CSV: line {line}: {name} is required")
            if name in ("Agency", "City") + VENDOR_HEADERS and _has_control(value):
                raise AtlasError(f"Atlas CSV: line {line}: {name} contains a control or format character")
            row["vendor" if name in VENDOR_HEADERS else name.lower()] = value
        row.setdefault("vendor", "")
        result.append(row)
    return result


def _seed_agencies(seed, county_fips):
    return [agency for county in seed["counties"] if county["county_fips"] == county_fips
            for agency in county["agencies"]]


def compare_atlas(root, csv_path, *, seed=None):
    """Compare the Atlas CSV with the county seed and kit/agencies.json. Reads only; returns the report dict."""
    from .cli import read_config  # local import: cli imports this module lazily
    root = Path(root)
    try:
        cfg = read_config(root)
        seed = seed if seed is not None else discovery.load_seed(kit._jurisdiction(cfg))
        location = kit._resolve(cfg, seed, False)  # offline only
        kit_entries = kit_agencies.load_agencies(root)
    except AtlasError:
        raise
    except ValueError as exc:
        raise AtlasError(str(exc)) from None
    atlas_rows = read_atlas_csv(csv_path)
    state, county = cfg["state"], location.county_name
    wanted = [row for row in atlas_rows
              if _collapse(row["technology"]) == TECHNOLOGY
              and row["state"].strip().casefold() == state.casefold()
              and _county_key(row["county"]) == _county_key(county)]
    seed_agencies = {}
    candidates = {}
    for agency in _seed_agencies(seed, location.county_fips):
        seed_agencies[agency["agency_id"]] = agency["name"]
        key = normalize_agency_name(agency["name"])
        if key:
            candidates.setdefault(key, set()).add(agency["agency_id"])
    selected = {entry["agency_id"] for entry in kit_entries if entry.get("selected", True) is True}
    listed, unmatched = {}, []
    for row in wanted:
        found = sorted(candidates.get(normalize_agency_name(row["agency"]), ()))
        atlas = {"line": row["line"], "agency": row["agency"], "city": row["city"], "vendor": row["vendor"]}
        if len(found) == 1:
            listed.setdefault(found[0], []).append(atlas)
        else:
            unmatched.append({**atlas, "reason": "ambiguous_seed_name" if found else "no_seed_match",
                              "candidate_agency_ids": found})
    return {
        "schema_version": 1, "notice": NOTICE, "county": county, "state": state,
        "listed_seed": {"notice": NOTICE, "rows": [
            {"agency_id": agency_id, "name": seed_agencies[agency_id], "selected": agency_id in selected,
             "atlas": listed[agency_id]} for agency_id in sorted(listed)]},
        "unmatched_atlas": {"notice": NOTICE, "rows": unmatched},
        "selected_not_listed": {"notice": NOTICE, "rows": [
            {"agency_id": entry["agency_id"], "name": entry["name"]}
            for entry in sorted(kit_entries, key=lambda e: e["agency_id"])
            if entry["agency_id"] in selected and entry["agency_id"] not in listed]},
    }


def _vendor(row):
    return row["vendor"] or "vendor not supplied"


def _city(row):
    return row["city"] if row["city"].strip() else "city not supplied"


def _check_text_agency_names(report):
    """Admit only printed seed and selected-not-listed kit names, using the CSV character policy."""
    for section in ("listed_seed", "selected_not_listed"):
        for row in report[section]["rows"]:
            if _has_control(row["name"]):
                raise AtlasError("Atlas report: an agency name contains a control or format character")


def render_atlas(report):
    """Plain text: ordinary admitted values are literal; preflight both printed agency-name sources."""
    _check_text_agency_names(report)
    lines = [f"Atlas cross-check for {report['county']} County, {report['state']}"]
    for key, heading in SECTIONS:
        section = report[key]
        lines += ["", heading, section["notice"]]
        if not section["rows"]:
            lines.append("(none)")
        for row in section["rows"]:
            if key == "listed_seed":
                lines.append(f"- {row['name']} [{row['agency_id']}]: {'selected' if row['selected'] else 'not selected'}")
                lines += [f"    CSV line {a['line']}: {a['agency']}; vendor: {_vendor(a)}; city: {_city(a)}" for a in row["atlas"]]
            elif key == "unmatched_atlas":
                lines.append(f"- CSV line {row['line']}: {row['agency']}; vendor: {_vendor(row)}; reason: {row['reason']}; city: {_city(row)}")
                if row["candidate_agency_ids"]:
                    lines.append("    candidates: " + ", ".join(row["candidate_agency_ids"]))
            else:
                lines.append(f"- {row['name']} [{row['agency_id']}]")
    return "\n".join(lines + ["", TRAILER])
