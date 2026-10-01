"""Load and validate a campaign's reviewed public content directory.

``load_content(root)`` reads ``<root>/content/`` and returns a plain dict the
site builder renders. Every text field is checked with the same
no-angle-bracket rule as ``agency_cards.text``; Markdown fields are rendered
later by ``markdown_lite``. Findings must carry the structural fields that
``campaign_tool.review.review_blockers`` requires, must be ``state:
published`` and must not carry ``confidence: needs_attorney_review``.

Nothing here is a publication approval, a legal review or an HTML sanitizer.
It refuses malformed or unreviewed input so the build stops instead of
publishing it. Records originals, mail, subscribers and private analysis never
belong in ``content/``.
"""
import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from . import map_controller
from .agency_cards import text as plain_text
from .review import CLASSIFICATIONS, review_blockers

CONFIDENCE = ("verified", "likely", "needs_attorney_review")
SIGNUP_MODES = ("none", "brevo_hosted")
SIGNUP_HOST_SUFFIXES = (".sibforms.com",)
AGENCY_KINDS = ("sheriff", "police", "chp", "district_attorney", "county_board", "city_council", "other")
CITY_KINDS = ("police", "city_council")
PORTAL_VENDORS = ("nextrequest", "govqa", "justfoia", "none", "unknown")
SLUG = re.compile(r"[a-z][a-z0-9-]{0,63}")
AGENCY_ID = re.compile(r"[a-z]{2}-[a-z0-9-]{1,80}")
SHA256 = re.compile(r"[0-9a-f]{64}")
HOST = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,62}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,62}[A-Za-z0-9])?)+")
LOCAL_ASSET = re.compile(r"data/[A-Za-z0-9][A-Za-z0-9._-]{0,120}\.(?:geojson|json)")
MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_GEOJSON_BYTES = 8 * 1024 * 1024
MAX_FEATURES = 20000
# Review-receipt blockers cannot be evaluated offline from content alone; the
# structural ones must all be clear.
RECEIPT_BLOCKERS = {"missing_factual_review", "missing_legal_review",
                    "missing_privacy_review", "need_two_independent_reviewers"}
STARTER_SLUGS = {"index", "agencies", "findings", "sources", "meetings", "about"}


class ContentError(ValueError):
    """A content file is missing, malformed or not publishable."""


def _read_json(path, limit=MAX_JSON_BYTES):
    path = Path(path)
    if path.is_symlink():
        raise ContentError(f"{path.name}: symlinks are not read")
    data = path.read_bytes()
    if len(data) > limit:
        raise ContentError(f"{path.name}: file too large")
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContentError(f"{path.name}: not valid UTF-8 JSON ({exc})") from None


def _require_keys(document, allowed, required, label):
    if not isinstance(document, dict):
        raise ContentError(f"{label}: expected an object")
    unknown = sorted(set(document) - set(allowed))
    if unknown:
        raise ContentError(f"{label}: unknown field(s) {unknown}")
    missing = sorted(set(required) - set(document))
    if missing:
        raise ContentError(f"{label}: missing field(s) {missing}")


def _text(document, key, label, required=True, default="", allow_empty=True):
    value = document.get(key, default)
    if value is None and not required:
        return None
    if not plain_text(value):
        raise ContentError(f"{label}.{key}: must be plain text without angle brackets")
    if required and not allow_empty and not value.strip():
        raise ContentError(f"{label}.{key}: must not be empty")
    return value


def _text_list(document, key, label, limit=100):
    value = document.get(key, [])
    if not isinstance(value, list) or len(value) > limit or any(not plain_text(item) for item in value):
        raise ContentError(f"{label}.{key}: must be a list of plain-text strings")
    return list(value)


def _https(value, label, allow_none=False):
    if value is None and allow_none:
        return None
    if not isinstance(value, str) or not re.fullmatch(r"https://[A-Za-z0-9./:?&=%_+#~-]+", value):
        raise ContentError(f"{label}: must be an https:// URL")
    parsed = urlsplit(value)
    if not parsed.hostname or parsed.username or parsed.password or not HOST.fullmatch(parsed.hostname):
        raise ContentError(f"{label}: invalid URL host")
    return value


def _timestamp(value, label, date_only=False):
    if not isinstance(value, str):
        raise ContentError(f"{label}: must be an ISO-8601 string")
    try:
        if date_only:
            datetime.strptime(value, "%Y-%m-%d")
        else:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ContentError(f"{label}: invalid ISO-8601 value {value!r}") from None
    return value


def _slug(value, label):
    if not isinstance(value, str) or not SLUG.fullmatch(value):
        raise ContentError(f"{label}: must be a lowercase slug (a-z, 0-9, hyphen)")
    return value


def _email(value, label):
    if value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,190}\.[A-Za-z]{2,24}", value):
        raise ContentError(f"{label}: invalid email address")
    return value


# --- site.json -------------------------------------------------------------

SITE_KEYS = {"schema_version", "tagline", "mission_md", "about_md", "contact_email", "signup",
             "social", "language", "base_url", "county_profile", "source_link_label",
             "font_family", "not_legal_advice"}
PROFILE_KEYS = {"vendor", "status", "summary", "cameraCount", "facts", "open", "flags"}
SIGNUP_KEYS = {"mode", "form_html_allowlisted_url", "label", "note"}


def load_site(document):
    _require_keys(document, SITE_KEYS, {"schema_version", "tagline", "about_md"}, "site.json")
    if type(document["schema_version"]) is not int or document["schema_version"] != 1:
        raise ContentError("site.json: unsupported schema_version")
    language = document.get("language", "en")
    if not isinstance(language, str) or not re.fullmatch(r"[a-zA-Z]{2,8}(?:-[a-zA-Z0-9]{1,8})*", language):
        raise ContentError("site.json.language: invalid language tag")
    signup = document.get("signup", {"mode": "none"})
    _require_keys(signup, SIGNUP_KEYS, {"mode"}, "site.json.signup")
    if signup["mode"] not in SIGNUP_MODES:
        raise ContentError(f"site.json.signup.mode: choose one of {list(SIGNUP_MODES)}")
    form_url = None
    if signup["mode"] == "brevo_hosted":
        form_url = _https(signup.get("form_html_allowlisted_url"), "site.json.signup.form_html_allowlisted_url")
        host = urlsplit(form_url).hostname.lower()
        if not any(host.endswith(suffix) and host != suffix.lstrip(".") for suffix in SIGNUP_HOST_SUFFIXES):
            raise ContentError("site.json.signup.form_html_allowlisted_url: host is not an allowlisted "
                               "hosted-form provider (" + ", ".join("*" + s for s in SIGNUP_HOST_SUFFIXES) + ")")
    social = document.get("social", [])
    if not isinstance(social, list) or len(social) > 12:
        raise ContentError("site.json.social: must be a list of {label, url}")
    links = []
    for index, entry in enumerate(social):
        _require_keys(entry, {"label", "url"}, {"label", "url"}, f"site.json.social[{index}]")
        links.append({"label": _text(entry, "label", "site.json.social", allow_empty=False),
                      "url": _https(entry["url"], f"site.json.social[{index}].url")})
    profile = document.get("county_profile", {})
    _require_keys(profile, PROFILE_KEYS, set(), "site.json.county_profile")
    county_profile = {
        "vendor": _text(profile, "vendor", "site.json.county_profile", default="Not yet documented"),
        "status": _text(profile, "status", "site.json.county_profile", default="Records requested"),
        "summary": _text(profile, "summary", "site.json.county_profile",
                         default="Published findings are listed with their sources; nothing else is asserted."),
        "cameraCount": _text(profile, "cameraCount", "site.json.county_profile", default=""),
        "facts": _text_list(profile, "facts", "site.json.county_profile"),
        "open": _text(profile, "open", "site.json.county_profile",
                      default="Open questions are tracked as findings with the information_gap classification."),
        "flags": _text_list(profile, "flags", "site.json.county_profile"),
    }
    font = document.get("font_family", "Georgia,serif")
    if not isinstance(font, str) or not re.fullmatch(r"[A-Za-z0-9 ,'\"-]{1,120}", font):
        raise ContentError("site.json.font_family: use a plain CSS font-family list")
    return {
        "tagline": _text(document, "tagline", "site.json", allow_empty=False),
        "mission_md": _text(document, "mission_md", "site.json", default=""),
        "about_md": _text(document, "about_md", "site.json", allow_empty=False),
        "contact_email": _email(document.get("contact_email"), "site.json.contact_email"),
        "signup": {"mode": signup["mode"], "url": form_url,
                   "label": _text(signup, "label", "site.json.signup", default="Get campaign updates"),
                   "note": _text(signup, "note", "site.json.signup",
                                 default="Hosted by the newsletter provider; unsubscribe any time.")},
        "social": links,
        "language": language,
        "base_url": _https(document.get("base_url"), "site.json.base_url", allow_none=True),
        "county_profile": county_profile,
        "source_link_label": _text(document, "source_link_label", "site.json",
                                   default="Review the evidence notes", allow_empty=False),
        "font_family": font,
        "not_legal_advice": _text(document, "not_legal_advice", "site.json",
                                  default="This is not legal advice. Findings describe records and "
                                          "published rules as of the event date; consult counsel "
                                          "before relying on them."),
    }


# --- findings --------------------------------------------------------------

FINDING_KEYS = {"id", "title", "slug", "summary", "body_md", "classification", "confidence",
                "event_date", "sources", "published_at", "updated_at", "corrections", "state",
                "author", "limitations", "counterevidence", "agency_id", "rule_version",
                "duty", "exceptions", "rule_ids"}
FINDING_REQUIRED = {"id", "title", "slug", "summary", "classification", "confidence",
                    "event_date", "sources", "published_at", "corrections", "state",
                    "author", "limitations", "counterevidence"}
SOURCE_KEYS = {"sha256", "locator", "title", "rule_id"}
CORRECTION_KEYS = {"date", "note"}


def load_finding(document, label):
    _require_keys(document, FINDING_KEYS, FINDING_REQUIRED, label)
    if document["state"] != "published":
        raise ContentError(f"{label}: state is {document['state']!r}; only published findings are rendered")
    if document["confidence"] not in CONFIDENCE:
        raise ContentError(f"{label}.confidence: choose one of {list(CONFIDENCE)}")
    if document["confidence"] == "needs_attorney_review":
        raise ContentError(f"{label}: confidence needs_attorney_review blocks publication; "
                           "the site refuses to render it")
    if document["classification"] not in CLASSIFICATIONS:
        raise ContentError(f"{label}.classification: choose one of {sorted(CLASSIFICATIONS)}")
    blockers = sorted(set(review_blockers(document, [])) - RECEIPT_BLOCKERS)
    if blockers:
        raise ContentError(f"{label}: review blockers {blockers}")
    finding_id = document["id"]
    if not isinstance(finding_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", finding_id):
        raise ContentError(f"{label}.id: use letters, digits, underscore or hyphen")
    slug = _slug(document["slug"], f"{label}.slug")
    if slug in STARTER_SLUGS:
        raise ContentError(f"{label}.slug: {slug!r} is reserved")
    sources = []
    for index, source in enumerate(document["sources"]):
        _require_keys(source, SOURCE_KEYS, {"sha256", "locator"}, f"{label}.sources[{index}]")
        if not SHA256.fullmatch(str(source["sha256"])):
            raise ContentError(f"{label}.sources[{index}].sha256: must be 64 lowercase hex characters")
        sources.append({
            "sha256": source["sha256"],
            "locator": _text(source, "locator", f"{label}.sources[{index}]", allow_empty=False),
            "title": _text(source, "title", f"{label}.sources[{index}]", default="Source record"),
            "rule_id": _text(source, "rule_id", f"{label}.sources[{index}]", required=False, default=None),
        })
    corrections = []
    for index, correction in enumerate(document["corrections"]):
        _require_keys(correction, CORRECTION_KEYS, CORRECTION_KEYS, f"{label}.corrections[{index}]")
        corrections.append({"date": _timestamp(correction["date"], f"{label}.corrections[{index}].date", date_only=True),
                            "note": _text(correction, "note", f"{label}.corrections[{index}]", allow_empty=False)})
    agency_id = document.get("agency_id")
    if agency_id is not None and (not isinstance(agency_id, str) or not AGENCY_ID.fullmatch(agency_id)):
        raise ContentError(f"{label}.agency_id: invalid agency identifier")
    return {
        "id": finding_id,
        "title": _text(document, "title", label, allow_empty=False),
        "slug": slug,
        "summary": _text(document, "summary", label, allow_empty=False),
        "body_md": _text(document, "body_md", label, default=""),
        "classification": document["classification"],
        "confidence": document["confidence"],
        "event_date": _timestamp(document["event_date"], f"{label}.event_date", date_only=True),
        "sources": sources,
        "published_at": _timestamp(document["published_at"], f"{label}.published_at"),
        "updated_at": _timestamp(document["updated_at"], f"{label}.updated_at") if document.get("updated_at") else None,
        "corrections": corrections,
        "author": _text(document, "author", label, allow_empty=False),
        "limitations": _text_list(document, "limitations", label),
        "counterevidence": _text_list(document, "counterevidence", label),
        "agency_id": agency_id,
        "rule_version": _text(document, "rule_version", label, required=False, default=None),
        "duty": _text(document, "duty", label, required=False, default=None),
        "exceptions": (_text_list(document, "exceptions", label) if isinstance(document.get("exceptions"), list)
                       else _text(document, "exceptions", label, required=False, default=None)),
    }


# --- meetings --------------------------------------------------------------

MEETING_KEYS = {"id", "body", "starts_at", "agenda_url", "agenda_item", "comment_kit_md", "location", "agency_id"}


def load_meeting(document, label):
    _require_keys(document, MEETING_KEYS, {"body", "starts_at"}, label)
    agency_id = document.get("agency_id")
    if agency_id is not None and (not isinstance(agency_id, str) or not AGENCY_ID.fullmatch(agency_id)):
        raise ContentError(f"{label}.agency_id: invalid agency identifier")
    return {
        "id": _slug(document["id"], f"{label}.id") if "id" in document else None,
        "body": _text(document, "body", label, allow_empty=False),
        "starts_at": _timestamp(document["starts_at"], f"{label}.starts_at"),
        "agenda_url": _https(document.get("agenda_url"), f"{label}.agenda_url", allow_none=True),
        "agenda_item": _text(document, "agenda_item", label, default=""),
        "comment_kit_md": _text(document, "comment_kit_md", label, default=""),
        "location": _text(document, "location", label, default=""),
        "agency_id": agency_id,
    }


# --- sources.json ----------------------------------------------------------

LIBRARY_KEYS = {"sha256", "title", "agency_id", "received_at", "public_url", "fragment", "pages", "note"}


def load_sources(document):
    if not isinstance(document, list) or len(document) > 5000:
        raise ContentError("sources.json: must be a list of library entries")
    entries, seen = [], set()
    for index, entry in enumerate(document):
        label = f"sources.json[{index}]"
        _require_keys(entry, LIBRARY_KEYS, {"sha256", "title", "agency_id", "received_at", "public_url", "fragment"}, label)
        if not SHA256.fullmatch(str(entry["sha256"])):
            raise ContentError(f"{label}.sha256: must be 64 lowercase hex characters")
        if not isinstance(entry["agency_id"], str) or not AGENCY_ID.fullmatch(entry["agency_id"]):
            raise ContentError(f"{label}.agency_id: invalid agency identifier")
        fragment = _slug(entry["fragment"], f"{label}.fragment")
        if fragment == "library":
            raise ContentError(f"{label}.fragment: 'library' is the page anchor; choose another slug")
        key = (entry["sha256"], fragment)
        if key in seen:
            raise ContentError(f"{label}: duplicate sha256 within fragment {fragment!r}")
        seen.add(key)
        entries.append({
            "sha256": entry["sha256"],
            "title": _text(entry, "title", label, allow_empty=False),
            "agency_id": entry["agency_id"],
            "received_at": _timestamp(entry["received_at"], f"{label}.received_at"),
            "public_url": _https(entry["public_url"], f"{label}.public_url", allow_none=True),
            "fragment": fragment,
            "pages": entry.get("pages") if type(entry.get("pages")) is int and entry.get("pages") >= 0 else None,
            "note": _text(entry, "note", label, default=""),
        })
    return entries


# --- agencies --------------------------------------------------------------

AGENCY_KEYS = {"agency_id", "name", "kind", "jurisdiction_name", "records_url", "records_email",
               "portal", "selected", "verified", "sources", "slug", "profile", "place_fips",
               "flock_transparency_slug", "muckrock_agency_id", "notes"}


def _agency_slug(agency):
    if "slug" in agency:
        return _slug(agency["slug"], f"agencies[{agency.get('agency_id')}].slug")
    prefix, _, rest = agency["agency_id"].partition("-")
    return _slug(rest, f"agencies[{agency['agency_id']}] derived slug")


def load_agencies(document, label):
    if not isinstance(document, list) or len(document) > 500:
        raise ContentError(f"{label}: must be a list of agencies")
    agencies, ids, slugs = [], set(), set()
    for index, entry in enumerate(document):
        item = f"{label}[{index}]"
        _require_keys(entry, AGENCY_KEYS, {"agency_id", "name"}, item)
        if not isinstance(entry["agency_id"], str) or not AGENCY_ID.fullmatch(entry["agency_id"]):
            raise ContentError(f"{item}.agency_id: invalid agency identifier")
        if entry.get("selected", True) is not True:
            continue
        kind = entry.get("kind", "other")
        if kind not in AGENCY_KINDS:
            raise ContentError(f"{item}.kind: choose one of {list(AGENCY_KINDS)}")
        portal = entry.get("portal") or {"vendor": "unknown", "url": None}
        _require_keys(portal, {"vendor", "url"}, {"vendor"}, f"{item}.portal")
        if portal["vendor"] not in PORTAL_VENDORS:
            raise ContentError(f"{item}.portal.vendor: choose one of {list(PORTAL_VENDORS)}")
        profile = entry.get("profile", {})
        _require_keys(profile, PROFILE_KEYS, set(), f"{item}.profile")
        slug = _agency_slug(entry)
        if entry["agency_id"] in ids or slug in slugs:
            raise ContentError(f"{item}: duplicate agency_id or slug")
        ids.add(entry["agency_id"])
        slugs.add(slug)
        agencies.append({
            "agency_id": entry["agency_id"],
            "slug": slug,
            "name": _text(entry, "name", item, allow_empty=False),
            "kind": kind,
            "jurisdiction_name": _text(entry, "jurisdiction_name", item, default=""),
            "records_url": _https(entry.get("records_url"), f"{item}.records_url", allow_none=True),
            "records_email": _email(entry.get("records_email"), f"{item}.records_email"),
            "portal": {"vendor": portal["vendor"],
                       "url": _https(portal.get("url"), f"{item}.portal.url", allow_none=True)},
            "verified": entry.get("verified") is True,
            "profile": {
                "vendor": _text(profile, "vendor", f"{item}.profile", default="Not yet documented"),
                "status": _text(profile, "status", f"{item}.profile", default="Records requested"),
                "summary": _text(profile, "summary", f"{item}.profile", default=""),
                "cameraCount": _text(profile, "cameraCount", f"{item}.profile", default=""),
                "facts": _text_list(profile, "facts", f"{item}.profile"),
                "open": _text(profile, "open", f"{item}.profile", default=""),
                "flags": _text_list(profile, "flags", f"{item}.profile"),
            },
        })
    return agencies


def default_agencies(root):
    """Selected kit agencies plus governing bodies, or [] when no kit exists."""
    kit_dir = Path(root) / "kit"
    agencies = []
    for name in ("agencies.json", "governing-bodies.json"):
        path = kit_dir / name
        if path.is_file():
            agencies.extend(load_agencies(_read_json(path), "kit/" + name))
    seen, unique = set(), []
    for agency in agencies:
        if agency["agency_id"] not in seen:
            seen.add(agency["agency_id"])
            unique.append(agency)
    return unique


# --- map -------------------------------------------------------------------

MAP_EXTRA_KEYS = {"county_bounds", "style_url", "tile_hosts", "group_label", "attribution", "county_boundary"}


def _bounds(value, label):
    ok = (isinstance(value, list) and len(value) == 2 and all(
        isinstance(corner, list) and len(corner) == 2 and all(
            isinstance(n, (int, float)) and not isinstance(n, bool) for n in corner) for corner in value))
    if not ok:
        raise ContentError(f"{label}: expected [[west, south], [east, north]]")
    (west, south), (east, north) = value
    if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
        raise ContentError(f"{label}: bounds out of range or inverted")
    return [[float(west), float(south)], [float(east), float(north)]]


def _validate_cameras(document, label):
    if not isinstance(document, dict) or document.get("type") != "FeatureCollection" \
            or not isinstance(document.get("features"), list):
        raise ContentError(f"{label}: expected a GeoJSON FeatureCollection")
    if len(document["features"]) > MAX_FEATURES:
        raise ContentError(f"{label}: too many features")
    for index, feature in enumerate(document["features"]):
        geometry = feature.get("geometry") if isinstance(feature, dict) else None
        coords = geometry.get("coordinates") if isinstance(geometry, dict) else None
        if not isinstance(geometry, dict) or geometry.get("type") != "Point" or not isinstance(coords, list) \
                or len(coords) != 2 or any(isinstance(n, bool) or not isinstance(n, (int, float)) for n in coords):
            raise ContentError(f"{label}.features[{index}]: cameras must be Point features")
        properties = feature.get("properties") or {}
        if not isinstance(properties, dict):
            raise ContentError(f"{label}.features[{index}].properties: expected an object")
        for key, value in properties.items():
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_:]{0,63}", key):
                raise ContentError(f"{label}.features[{index}]: invalid property name")
            if key == "url":
                if value is not None and (not isinstance(value, str) or
                                          not re.fullmatch(r"https://www\.openstreetmap\.org/(?:node|way)/\d+", value)):
                    raise ContentError(f"{label}.features[{index}].url: must be an openstreetmap.org object URL")
            elif isinstance(value, str) and not plain_text(value):
                raise ContentError(f"{label}.features[{index}].{key}: plain text only")
            elif not isinstance(value, (str, int, float, bool)) and value is not None:
                raise ContentError(f"{label}.features[{index}].{key}: scalar values only")
    return len(document["features"])


def load_map(content_dir):
    map_dir = Path(content_dir) / "map"
    config_path = map_dir / "map-config.json"
    if not config_path.is_file():
        return None
    document = _read_json(config_path)
    _require_keys(document, map_controller.KEYS | MAP_EXTRA_KEYS,
                  map_controller.KEYS | {"county_bounds", "style_url"}, "map/map-config.json")
    controller = {key: document[key] for key in map_controller.KEYS}
    map_controller.render(controller, "")  # validates the controller keys now, with a clear error
    style_url = document["style_url"]
    if isinstance(style_url, str) and style_url.startswith("/"):
        if not re.fullmatch(r"/[A-Za-z0-9][A-Za-z0-9._/-]{0,160}\.json", style_url) or ".." in style_url.split("/"):
            raise ContentError("map/map-config.json.style_url: local style must be a /path/to/style.json")
        style_host = None
    else:
        _https(style_url, "map/map-config.json.style_url")
        style_host = urlsplit(style_url).hostname.lower()
    tile_hosts = document.get("tile_hosts", [])
    if not isinstance(tile_hosts, list) or len(tile_hosts) > 8 or any(
            not isinstance(host, str) or not HOST.fullmatch(host) for host in tile_hosts):
        raise ContentError("map/map-config.json.tile_hosts: list of host names")
    boundary_file = map_dir / Path(controller["city_boundaries_url"]).name
    if not LOCAL_ASSET.fullmatch(controller["city_boundaries_url"]):
        raise ContentError("map/map-config.json.city_boundaries_url: must be data/<file>.geojson, "
                           "copied from content/map/<file>.geojson")
    if not boundary_file.is_file():
        raise ContentError(f"map/{boundary_file.name}: city boundary file is missing")
    cameras_path = map_dir / "cameras.geojson"
    if not cameras_path.is_file():
        raise ContentError("map/cameras.geojson is missing; run tools/fetch_osm_alprs.py or supply reviewed data")
    camera_count = _validate_cameras(_read_json(cameras_path, MAX_GEOJSON_BYTES), "map/cameras.geojson")
    boundaries = _read_json(boundary_file, MAX_GEOJSON_BYTES)
    if not isinstance(boundaries, dict) or boundaries.get("type") != "FeatureCollection":
        raise ContentError(f"map/{boundary_file.name}: expected a GeoJSON FeatureCollection")
    county_boundary = map_dir / "county.geojson"
    cities_path = map_dir / "cities.json"
    cities = {}
    if cities_path.is_file():
        raw = _read_json(cities_path)
        if not isinstance(raw, dict) or len(raw) > 200:
            raise ContentError("map/cities.json: expected {slug: {bounds: [[w,s],[e,n]]}}")
        for slug, entry in raw.items():
            _slug(slug, "map/cities.json key")
            _require_keys(entry, {"bounds", "name"}, {"bounds"}, f"map/cities.json[{slug}]")
            cities[slug] = {"bounds": _bounds(entry["bounds"], f"map/cities.json[{slug}].bounds"),
                            "name": _text(entry, "name", f"map/cities.json[{slug}]", required=False, default=None)}
    attribution_path = map_dir / "attribution.txt"
    attribution = document.get("attribution")
    if attribution is None and attribution_path.is_file():
        attribution = attribution_path.read_text(encoding="utf-8").strip()
    if attribution is None:
        attribution = "Map data (c) OpenStreetMap contributors, ODbL. Community ALPR points via DeFlock."
    if not plain_text(attribution) or len(attribution) > 500:
        raise ContentError("map attribution must be plain text under 500 characters")
    for slug in controller["bounding_box_fallbacks"]:
        if slug not in cities:
            raise ContentError(f"map/map-config.json.bounding_box_fallbacks: {slug!r} has no bounds in map/cities.json")
    return {
        "controller": controller,
        "county_bounds": _bounds(document["county_bounds"], "map/map-config.json.county_bounds"),
        "style_url": style_url,
        "style_host": style_host,
        "tile_hosts": sorted(set(tile_hosts)),
        "group_label": _text(document, "group_label", "map/map-config.json",
                             default="Unincorporated area (ownership unconfirmed)"),
        "attribution": attribution,
        "cameras_path": cameras_path,
        "camera_count": camera_count,
        "boundary_path": boundary_file,
        "county_boundary_path": county_boundary if county_boundary.is_file() else None,
        "cities": cities,
    }


# --- assembly ---------------------------------------------------------------

def load_content(root):
    """Return the validated content model for ``root`` or None when no content/ exists."""
    root = Path(root)
    content_dir = root / "content"
    if not content_dir.is_dir():
        return None
    site_path = content_dir / "site.json"
    if not site_path.is_file():
        raise ContentError("content/site.json is required when content/ exists")
    site = load_site(_read_json(site_path))

    findings, slugs, ids = [], set(), set()
    for path in sorted((content_dir / "findings").glob("*.json")) if (content_dir / "findings").is_dir() else []:
        finding = load_finding(_read_json(path), "findings/" + path.name)
        if finding["slug"] in slugs or finding["id"] in ids:
            raise ContentError(f"findings/{path.name}: duplicate finding slug or id")
        slugs.add(finding["slug"])
        ids.add(finding["id"])
        findings.append(finding)
    findings.sort(key=lambda f: (f["published_at"], f["slug"]), reverse=True)

    meetings = []
    for path in sorted((content_dir / "meetings").glob("*.json")) if (content_dir / "meetings").is_dir() else []:
        meeting = load_meeting(_read_json(path), "meetings/" + path.name)
        meeting["id"] = meeting["id"] or _slug(path.stem, "meetings/" + path.name + " filename")
        meetings.append(meeting)
    meetings.sort(key=lambda m: m["starts_at"])

    sources_path = content_dir / "sources.json"
    sources = load_sources(_read_json(sources_path)) if sources_path.is_file() else []

    agencies_path = content_dir / "agencies.json"
    if agencies_path.is_file():
        agencies = load_agencies(_read_json(agencies_path), "content/agencies.json")
    else:
        agencies = default_agencies(root)

    map_config = load_map(content_dir)
    if map_config:
        known = {agency["slug"] for agency in agencies if agency["kind"] in CITY_KINDS}
        for slug in map_config["controller"]["bounding_box_fallbacks"]:
            if slug not in known:
                raise ContentError(f"map/map-config.json.bounding_box_fallbacks: {slug!r} is not a "
                                   "police/city_council agency slug")

    vendor_dir = content_dir / "vendor"
    vendor = {}
    for name in ("maplibre-gl.js", "maplibre-gl.css"):
        path = vendor_dir / name
        if path.is_file() and not path.is_symlink():
            vendor[name] = path

    public_assets = []
    assets_dir = content_dir / "public-assets"
    if assets_dir.is_dir():
        for path in sorted(assets_dir.rglob("*")):
            if path.is_file() and not path.is_symlink():
                public_assets.append(path)

    return {
        "site": site,
        "findings": findings,
        "meetings": meetings,
        "sources": sources,
        "agencies": agencies,
        "map": map_config,
        "vendor": vendor,
        "public_assets": public_assets,
        "assets_dir": assets_dir,
    }
