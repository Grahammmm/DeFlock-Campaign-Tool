"""Open-data catalogue and Frictionless Data Package for the generated site (DF2207-12).

``resource_records`` freezes, for the open-data files a build writes, the public path, a stable name, a fixed description, the
media type, the byte length, the SHA-256 of the final bytes, the schema and the licence that really applies to that file.
``datapackage_json`` writes ``data/datapackage.json`` from those records and ``Site.data_html`` writes ``data.html`` from the
same records, so both agree with the written bytes. ``verify_data_package`` re-reads a built ``public/`` tree offline.

The registry is explicit: the four JSON and four CSV documents, the optional officials JSON/CSV pair (DF1109-10),
``cameras.geojson``, ``cameras.csv``, ``county.geojson``, ``sources.zip`` and the one city boundary copy. Nothing is
discovered by walking ``public/``, and the catalogue never lists itself (no hash cycle).
For the county and city boundaries the check also rederives the licence and credit from the listed bytes (RF1415-1).
"""
import csv
import hashlib
import io
import json
import os
import posixpath
import re
import stat
from html.parser import HTMLParser
from pathlib import Path

from .content import LICENSES, ODBL, ContentError
from .source_bundle import MAX_BYTES as SOURCE_ZIP_MAX

PACKAGE_NAME = "campaign-open-data"
DESCRIPTOR = "data/datapackage.json"
PAGE = "data.html"
SCHEMA_BASE = "https://raw.githubusercontent.com/Grahammmm/DeFlock-Campaign-Tool/main/schemas/"
GEOJSON_SCHEMA = "https://geojson.org/schema/FeatureCollection.json"
JSON, CSV, GEO = "application/json", "text/csv", "application/geo+json"
ZIP = "application/zip"  # DF0022-11: data/sources.zip, the one binary resource (schema null, no licence claimed)
CITY_NAME = "city-boundary-geojson"
MAX_BYTES = 64 * 1024 * 1024
NAME = re.compile(r"[a-z0-9][a-z0-9-]*")
SHA = re.compile(r"[0-9a-f]{64}")
LOCAL_FILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,120}\.(?:geojson|json)")

# path -> (name, title, description, media type, schema kind); the schema kind of a CSV is its source-record JSON schema.
DATASETS = {
    "data/findings.json": ("findings-json", "Findings (JSON)", "Published findings with classification, confidence, dates and 12-character source prefixes.", JSON, "findings"),
    "data/findings.csv": ("findings-csv", "Findings (CSV)", "The findings as one flat row per finding; every cell is text.", CSV, "findings"),
    "data/sources.json": ("sources-json", "Sources (JSON)", "The source library: hashed records the findings cite, with their agency and received date.", JSON, "sources"),
    "data/sources.csv": ("sources-csv", "Sources (CSV)", "The source library as one flat row per record; every cell is text.", CSV, "sources"),
    "data/agencies.json": ("agencies-json", "Agencies (JSON)", "Agencies with a public page: requests asked, policy, contract and transparency facts.", JSON, "agencies"),
    "data/agencies.csv": ("agencies-csv", "Agencies (CSV)", "The agencies as one flat row per agency; every cell is text.", CSV, "agencies"),
    "data/meetings.json": ("meetings-json", "Meetings (JSON)", "Staged public meetings with recorded outcomes and votes.", JSON, "meetings"),
    "data/meetings.csv": ("meetings-csv", "Meetings (CSV)", "The meetings as one flat row per meeting; every cell is text.", CSV, "meetings"),
    # DF1109-10: the DF1622-11 roster exports, listed only when the build writes them (someone is on content/officials.json)
    "data/officials.json": ("officials-json", "Officials (JSON)", "The organizer-checked public roster of decision makers, with roles and official source links.", JSON, "officials"),
    "data/officials.csv": ("officials-csv", "Officials (CSV)", "The public roster as one flat row per official; every cell is text.", CSV, "officials"),
    "data/cameras.geojson": ("cameras-geojson", "Mapped camera points (GeoJSON)", "Community-mapped camera points from OpenStreetMap, as reviewed for the map.", GEO, None),
    "data/cameras.csv": ("cameras-csv", "Mapped camera points (CSV)", "Community-mapped camera points; ownership unconfirmed.", CSV, None),  # DF0237-12
    "data/county.geojson": ("county-geojson", "County boundary (GeoJSON)", "County boundary used by the map.", GEO, None),
    "data/sources.zip": ("sources-zip", "Public source copies (ZIP)", "The public source copies this site hosts, with manifest.csv; external links and private originals are not included.", ZIP, None),
}
CITY_META = (CITY_NAME, "City boundaries (GeoJSON)", "City boundaries used to count mapped points.", GEO, None)
CAMERA_CSV_ATTRIBUTION = ODBL["attribution"] + ". ODbL 1.0; attribution and share-alike apply."  # DF0237-12
KNOWN_LICENSES = {ODBL["id"]: ODBL["url"], **{key: value[1] for key, value in LICENSES.items()}}
PACKAGE_KEYS = {"profile", "name", "title", "resources"}
RESOURCE_KEYS = {"profile", "name", "title", "description", "path", "mediatype", "bytes", "hash", "schema", "licenses"}


def _meta(path, city=None):
    if path in DATASETS:
        return DATASETS[path]
    if path == city:
        return CITY_META
    raise ContentError(f"{path}: not a registered open-data file")


def csv_columns(data):
    """The header row of CSV bytes (UTF-8), as the list of column names."""
    rows = csv.reader(io.StringIO(data.decode("utf-8")))
    return next(rows, [])


def _expected_schema(path, media, kind, data):
    """``(descriptor schema, link target for the HTML page)``."""
    if media == ZIP:
        return None, None
    if media == CSV:
        link = f"{SCHEMA_BASE}{kind}-open-data.schema.json" if kind else GEOJSON_SCHEMA  # DF0237-12: cameras.csv is a copy of the GeoJSON
        return {"fields": [{"name": name, "type": "string"} for name in csv_columns(data)]}, link
    link = GEOJSON_SCHEMA if media == GEO else f"{SCHEMA_BASE}{kind}-open-data.schema.json"
    return link, link


def _stated_license(data):
    """The id a GeoJSON file states about itself in its own top-level ``license`` member, if it is a known one."""
    try:
        document = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    value = document.get("license") if isinstance(document, dict) else None
    return value if isinstance(value, str) and value in KNOWN_LICENSES else None


def _licenses(path, media, data, campaign):
    """``(licenses, attribution or None)`` for one file; a licence never crosses from one resource to another."""
    if media == ZIP:  # mixed source rights: no blanket campaign licence
        return [], None
    if path == "data/cameras.csv":  # DF0237-12: the same OpenStreetMap licence, with the share-alike note beside the credit
        return [{"name": ODBL["id"], "path": ODBL["url"]}], CAMERA_CSV_ATTRIBUTION
    if path == "data/cameras.geojson":  # OpenStreetMap-derived: ODbL always, whatever the campaign chose
        return [{"name": ODBL["id"], "path": ODBL["url"]}], ODBL["attribution"]
    if media != GEO:  # the campaign's own data
        if not campaign:
            return [], None
        return [{"name": campaign["id"], "path": campaign["url"]}], campaign.get("attribution")
    stated = _stated_license(data)  # a boundary keeps only a licence its own file states; never a guess
    if stated is None:
        return [], None
    return [{"name": stated, "path": KNOWN_LICENSES[stated]}], ODBL["attribution"] if stated == ODBL["id"] else None


def resource_records(payloads, license_info):
    """Records (sorted by path) for ``{public path: final bytes}``.

    ``license_info`` is ``{"campaign": site.json.license or None, "city_boundary": the city boundary path or None}``."""
    city, campaign = license_info.get("city_boundary"), license_info.get("campaign")
    if city in DATASETS or city == DESCRIPTOR or city == PAGE:
        raise ContentError(f"{city}: the city boundary copy collides with another generated open-data file")
    records, names = [], set()
    for path in sorted(payloads):
        name, title, description, media, kind = _meta(path, city)
        data = payloads[path]
        if name in names:
            raise ContentError(f"{path}: duplicate data package resource name {name}")
        names.add(name)
        schema, link = _expected_schema(path, media, kind, data)
        licenses, attribution = _licenses(path, media, data, campaign)
        records.append({"path": path, "name": name, "title": title, "description": description, "mediatype": media,
                        "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "schema": schema,
                        "schema_url": link, "licenses": licenses, "attribution": attribution})
    return records


def datapackage_json(records, title):
    """``data/datapackage.json`` text: a generic Frictionless Data Package (paths are relative to ``data/``)."""
    resources = []
    for record in records:
        resource = {"profile": "data-resource", "name": record["name"], "title": record["title"],
                    "description": record["description"], "path": posixpath.relpath(record["path"], "data"),
                    "mediatype": record["mediatype"], "bytes": record["bytes"], "hash": "sha256:" + record["sha256"],
                    "schema": record["schema"], "licenses": record["licenses"]}
        if record["attribution"]:
            resource["attribution"] = record["attribution"]
        resources.append(resource)
    document = {"profile": "data-package", "name": PACKAGE_NAME, "title": title, "resources": resources}
    return json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


# --- offline verification of a built tree -------------------------------------------------------------------------

class _Rows(HTMLParser):
    """The ``data-path`` table rows of data.html (fixed data attributes and visible cells) and its links."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows, self.links, self._row, self._cell = [], [], None, None

    def handle_starttag(self, tag, attrs):
        attrs = {name: value or "" for name, value in attrs}
        if tag == "a" and "href" in attrs:
            self.links.append(attrs["href"])
        elif tag == "tr" and "data-path" in attrs:
            self._row = {"path": attrs["data-path"], "bytes": attrs.get("data-bytes"), "sha256": attrs.get("data-sha256"),
                         "shown_bytes": "", "shown_sha256": ""}
            self.rows.append(self._row)
        elif tag == "td" and self._row is not None and attrs.get("class") in ("bytes", "sha256"):
            self._cell = "shown_" + attrs["class"]

    def handle_endtag(self, tag):
        if tag == "td":
            self._cell = None
        elif tag == "tr":
            self._row = None

    def handle_data(self, data):
        if self._cell and self._row is not None:
            self._row[self._cell] += data


def _duplicate_keys(pairs):
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate key")
    return dict(pairs)


def _read_regular(public, relative, limit=MAX_BYTES):
    """Bytes of ``public/<relative>``; ``None`` when absent, a reason string when it is a symlink, escapes or is not a file or exceeds ``limit`` bytes (enforced on the bytes actually read, at most limit + 1 requested).

    The leaf is opened without following links or blocking and the opened descriptor must be the regular
    file (same device and inode) that the path walk inspected, so a late replacement is never read."""
    current = public
    for index, part in enumerate(relative.split("/")):
        current = current / part
        try:
            inspected = os.lstat(current)
        except FileNotFoundError:
            return None
        except OSError:
            return "cannot be read"
        mode = inspected.st_mode
        if stat.S_ISLNK(mode):
            return "is or sits under a symlink"
        if index < relative.count("/") and not stat.S_ISDIR(mode):
            return "has a non-directory ancestor"
    if not stat.S_ISREG(mode):
        return "is not a regular file"
    if inspected.st_size > limit:
        return "is too large to check"
    flags = getattr(os, "O_NOFOLLOW", None), getattr(os, "O_NONBLOCK", None)
    if None in flags:
        return "cannot be read"
    try:
        fd = os.open(current, os.O_RDONLY | flags[0] | flags[1])
    except OSError:
        return "cannot be read"
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            return "is not a regular file"
        if (opened.st_dev, opened.st_ino) != (inspected.st_dev, inspected.st_ino):
            return "cannot be read"
        if opened.st_size > limit:
            return "is too large to check"
        # Bound the read itself: the file may have grown since the size check above.
        handle = os.fdopen(fd, "rb", closefd=False)
        try:
            data = handle.read(limit + 1)
        finally:
            handle.close()
    except OSError:
        return "cannot be read"
    finally:
        try:
            os.close(fd)
        except OSError:
            data = None
    if data is None:
        return "cannot be read"
    if len(data) > limit:
        return "is too large to check"
    return data


def _safe_path(path):
    return (isinstance(path, str) and bool(path) and path == posixpath.normpath(path) and not path.startswith("/")
            and "\\" not in path and ":" not in path and ".." not in path.split("/") and path != "." and "/" not in path)


def _check_resource(public, resource, seen, problems, city_names):
    where = "data/datapackage.json"
    if not isinstance(resource, dict):
        problems.append((where, "a resource is not an object"))
        return None
    path = resource.get("path")
    shown = "data/" + path if _safe_path(path) else where
    if set(resource) - RESOURCE_KEYS - {"attribution"} or RESOURCE_KEYS - set(resource):
        problems.append((shown, "resource keys are not the fixed set"))
    if not _safe_path(path):
        problems.append((where, f"resource path {path!r} is not a plain file name relative to data/"))
        return None
    full = "data/" + path
    if full in seen:
        problems.append((full, "listed twice"))
    seen.add(full)
    name = resource.get("name")
    if full in DATASETS:
        meta = DATASETS[full]
    elif LOCAL_FILE.fullmatch(path) and full not in (DESCRIPTOR,) and name == CITY_NAME:
        meta = CITY_META
        city_names.append(full)
    else:
        problems.append((full, "is not a generated open-data file"))
        return full
    if not isinstance(name, str) or not NAME.fullmatch(name) or name != meta[0]:
        problems.append((full, f"resource name must be {meta[0]}"))
    if resource.get("profile") != "data-resource":
        problems.append((full, "resource profile must be data-resource"))
    if resource.get("mediatype") != meta[3]:
        problems.append((full, f"mediatype must be {meta[3]}"))
    size, digest = resource.get("bytes"), resource.get("hash")
    if type(size) is not int or size < 0:
        problems.append((full, "bytes must be a non-negative integer"))
    if not (isinstance(digest, str) and digest.startswith("sha256:") and SHA.fullmatch(digest[7:])):
        problems.append((full, "hash must be sha256:<64 lowercase hex>"))
    data = _read_regular(public, full, SOURCE_ZIP_MAX if meta[3] == ZIP else MAX_BYTES)
    if data is None:
        problems.append((full, "listed file is missing"))
        return full
    if isinstance(data, str):
        problems.append((full, data))
        return full
    if type(size) is int and size != len(data):
        problems.append((full, f"size changed: listed {size}, found {len(data)}"))
    if isinstance(digest, str) and digest[7:] != hashlib.sha256(data).hexdigest():
        problems.append((full, "sha256 changed"))
    try:
        expected, _ = _expected_schema(full, meta[3], meta[4], data)
    except UnicodeDecodeError:
        problems.append((full, "CSV is not UTF-8"))
        expected = None
    except csv.Error:  # RF0211-4: only the header parse; a fixed reason, never the parser's text or the cell
        problems.append((full, "CSV header cannot be parsed"))
        expected = None
    if expected is not None and resource.get("schema") != expected:
        problems.append((full, "schema does not match the file"))
    if meta[3] == ZIP and (resource.get("schema") is not None or resource.get("licenses") != []):
        problems.append((full, "sources.zip must have a null schema and no licences"))
    if full in ("data/cameras.geojson", "data/cameras.csv") and resource.get("licenses") != [{"name": ODBL["id"], "path": ODBL["url"]}]:
        problems.append((full, f"{full[5:]} must carry ODbL-1.0"))
    for item in resource.get("licenses") if isinstance(resource.get("licenses"), list) else [None]:
        if not (isinstance(item, dict) and set(item) == {"name", "path"}
                and isinstance(item["name"], str) and isinstance(item["path"], str)
                and item["name"] in KNOWN_LICENSES and KNOWN_LICENSES[item["name"]] == item["path"]):
            problems.append((full, "licenses must be known {name, path} entries"))
            break
    if "attribution" in resource and not isinstance(resource["attribution"], str):
        problems.append((full, "attribution must be text"))
    if full == "data/county.geojson" or meta is CITY_META:
        _check_boundary_rights(full, resource, data, problems)
    return full


def _check_boundary_rights(full, resource, data, problems):
    """RF1415-1: a boundary's listed licences and credit must be exactly what ``_licenses`` derives from its own bytes.

    Agreement with the file's own ``license`` member only, never a legal rights review; the reasons are fixed text."""
    try:
        licenses, attribution = _licenses(full, GEO, data, None)
    except RecursionError:
        problems.append((full, "boundary file is too deeply nested to check"))
        return
    if resource.get("licenses") != licenses:
        problems.append((full, "licenses differ from the licence the boundary file states"))
    if resource.get("attribution") != attribution:
        problems.append((full, "attribution differs from the credit the boundary file's licence requires"))


def verify_data_package(public):
    """Sorted ``(path, reason)`` problems of the catalogue in ``public``; ``[]`` when both files agree with every listed file.

    Reads only: no network, nothing is created or repaired."""
    public, problems = Path(public), []
    if public.is_symlink():
        return [("public", "is a symlink")]
    raw = _read_regular(public, DESCRIPTOR)
    page = _read_regular(public, PAGE)
    for path, data in ((DESCRIPTOR, raw), (PAGE, page)):
        if data is None:
            problems.append((path, "missing"))
        elif isinstance(data, str):
            problems.append((path, data))
    unread = object()
    document = unread
    if isinstance(raw, bytes):
        try:
            document = json.loads(raw.decode("utf-8"), object_pairs_hook=_duplicate_keys)
        except (UnicodeDecodeError, ValueError, RecursionError):
            problems.append((DESCRIPTOR, "is not valid JSON without duplicate keys"))
    listed, city_names = set(), []
    if document is not unread:
        resources = document.get("resources") if isinstance(document, dict) else None
        if (not isinstance(document, dict) or set(document) != PACKAGE_KEYS or document["profile"] != "data-package"
                or document["name"] != PACKAGE_NAME or not isinstance(document["title"], str)
                or not isinstance(resources, list) or not resources):
            problems.append((DESCRIPTOR, "package keys are not the fixed set"))
        else:
            names, expected_rows = set(), {}
            for resource in resources:
                full = _check_resource(public, resource, listed, problems, city_names)
                if isinstance(resource, dict) and isinstance(resource.get("name"), str):
                    if resource["name"] in names:
                        problems.append((full or DESCRIPTOR, "duplicate resource name"))
                    names.add(resource["name"])
                if full and isinstance(resource.get("bytes"), int) and isinstance(resource.get("hash"), str):
                    expected_rows[full] = (str(resource["bytes"]), resource["hash"][7:])
            if len(city_names) > 1:
                problems.append((DESCRIPTOR, "more than one city boundary is listed"))
            # The map controller independently names the actual city copy (which may be renamed or JSON).
            # Read its literal fetch argument, never execute JavaScript or discover unrelated data files.
            expected_paths = set(DATASETS)
            controller = _read_regular(public, "app.js")
            if isinstance(controller, bytes):
                for match in re.finditer(rb'fetch\(("data/[A-Za-z0-9._-]+")\)', controller):
                    path = json.loads(match[1])
                    if LOCAL_FILE.fullmatch(path[5:]) and path != DESCRIPTOR:
                        expected_paths.add(path)
            for path in sorted(expected_paths):  # a generated file the package leaves out
                if path not in listed and os.path.lexists(public / path):
                    problems.append((path, "present but not listed in the data package"))
            if isinstance(page, bytes):
                _check_page(page, expected_rows, problems)
    return sorted(set(problems))


def _check_page(page, expected, problems):
    parser = _Rows()
    parser.feed(page.decode("utf-8", errors="replace"))
    parser.close()
    if "/" + DESCRIPTOR not in parser.links:
        problems.append((PAGE, "does not link /data/datapackage.json"))
    seen = set()
    for row in parser.rows:
        path = row["path"]
        if path in seen:
            problems.append((path, "listed twice in data.html"))
        seen.add(path)
        want = expected.get(path)
        if want is None:
            problems.append((path, "listed in data.html but not in the data package"))
        elif (row["bytes"], row["sha256"]) != want or (row["shown_bytes"].strip(), row["shown_sha256"].strip()) != want:
            problems.append((path, "data.html row size or sha256 differs from the data package"))
    for path in sorted(set(expected) - seen):
        problems.append((path, "missing from data.html"))


def check_data_catalogue(public):
    """Raise ``ContentError`` naming every path and reason when the catalogue disagrees with ``public``."""
    problems = verify_data_package(public)
    if problems:
        raise ContentError("data catalogue check failed: " + "; ".join(f"{path}: {reason}" for path, reason in problems))
