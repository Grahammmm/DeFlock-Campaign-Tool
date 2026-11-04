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
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from . import map_controller
from .agency_cards import text as plain_text
from .review import CLASSIFICATIONS, review_blockers

CONFIDENCE = ("verified", "likely", "needs_attorney_review")
SIGNUP_MODES = ("none", "brevo_hosted")
SIGNUP_HOST_SUFFIXES = (".sibforms.com",)
AGENCY_KINDS = ("sheriff", "police", "chp", "campus_police", "district_attorney", "county_board", "city_council",
                "other")
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


def utc_moment(value):
    """An ISO timestamp as an aware datetime; a zone-less value is read as UTC."""
    moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


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
             "font_family", "not_legal_advice", "brand", "privacy_contact", "share_image"}
SHARE_IMAGE_KEYS = {"path", "alt"}
SHARE_RATIO, SHARE_RATIO_TOLERANCE = 1.905, 0.02  # 1200x630
BRAND_KEYS = {"logo", "logo_alt", "accent", "ink", "favicon"}
HEX_COLOUR = re.compile(r"#[0-9a-fA-F]{6}")
ASSET_PART = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,120}")  # site._asset_plan's plain-name rule
TEXT_BACKGROUNDS = {"cream": "#fff8ec", "paper": "#f3eee4"}  # styles.css.tmpl --cream / --paper
BRAND_CONTRAST = (("ink", ("cream", "paper")), ("accent", ("cream",)))  # the text pairs the stylesheet uses
MIN_CONTRAST, LOGO_MAX_SIDE = 4.5, 2000
PROFILE_KEYS = {"vendor", "status", "summary", "cameraCount", "facts", "open", "flags"}
SIGNUP_KEYS = {"mode", "form_html_allowlisted_url", "label", "note", "privacy_url"}


def _luminance(colour):
    channels = [int(colour[at:at + 2], 16) / 255 for at in (1, 3, 5)]
    red, green, blue = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def contrast(first, second):
    """WCAG 2.x contrast ratio of two ``#rrggbb`` colours (1.0 to 21.0)."""
    light, dark = sorted((_luminance(first), _luminance(second)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


def _brand_asset(value, assets_dir, label, suffixes):
    """(width, height) of a PNG/WebP/SVG public asset (None for .ico or without ``assets_dir``); the value is never shown."""
    from . import doc_assets, image_meta  # local: both import this module
    parts = value.split("/") if isinstance(value, str) else []
    if not parts or len(parts) > 8 or any(not ASSET_PART.fullmatch(part) for part in parts):
        raise ContentError(f"{label}: use a plain file name inside content/public-assets/")
    suffix = Path(value).suffix.lower()
    if suffix not in suffixes:
        raise ContentError(f"{label}: use a {', '.join(suffixes[:-1])} or {suffixes[-1]} file")
    if assets_dir is None:  # callers that render no page (meeting packs) check everything but the file
        return None
    chain = [Path(assets_dir).joinpath(*parts[:depth]) for depth in range(len(parts) + 1)]
    if any(step.is_symlink() for step in chain):
        raise ContentError(f"{label}: is or sits under a symlink; use a regular file in content/public-assets/")
    path = chain[-1]
    if not path.is_file() or path.stat().st_size > image_meta.MAX_BYTES:
        raise ContentError(f"{label}: file not found in content/public-assets/ or over 5 MiB")
    if suffix == ".svg":
        data = path.read_bytes()
        kinds = doc_assets.svg_kinds(data)
        if kinds:
            raise ContentError(f"{label}: SVG ({', '.join(kinds)})")
        size = doc_assets.svg_size(data)
        if size is None:
            raise ContentError(f"{label}: unreadable SVG (no readable width and height)")
        return size
    if suffix not in image_meta.FORMATS:
        return None
    data = path.read_bytes()
    kinds, size = image_meta.metadata_kinds(data, suffix), image_meta.image_size(data, suffix)
    if kinds and kinds != [image_meta.UNREADABLE]:
        raise ContentError(f"{label}: image metadata ({', '.join(kinds)}); run content strip-metadata")
    if kinds or size is None:
        raise ContentError(f"{label}: {image_meta.UNREADABLE} (no readable width and height)")
    return size


def load_brand(value, assets_dir=None):
    """site.json.brand: optional logo (+ required logo_alt), accent/ink colours and favicon; refusals name the field."""
    label = "site.json.brand"
    _require_keys(value, BRAND_KEYS, set(), label)
    brand = {}
    for key in ("accent", "ink"):
        if key in value:
            if not isinstance(value[key], str) or not HEX_COLOUR.fullmatch(value[key]):
                raise ContentError(f"{label}.{key}: use a #rrggbb hex colour")
            brand[key] = value[key].lower()
    for key, backgrounds in BRAND_CONTRAST:
        for name in backgrounds if key in brand else ():
            ratio = contrast(brand[key], TEXT_BACKGROUNDS[name])
            if ratio < MIN_CONTRAST:  # shown rounded down, so a refused ratio never prints as 4.50
                raise ContentError(f"{label}.{key}: contrast {int(ratio * 100) / 100:.2f}:1 against {name} "
                                   f"({TEXT_BACKGROUNDS[name]}); needs {MIN_CONTRAST}:1")
    if "logo_alt" in value and "logo" not in value:
        raise ContentError(f"{label}.logo_alt: set only together with logo")
    if "logo" in value:
        alt = value.get("logo_alt")
        if not plain_text(alt) or not alt.strip() or len(alt) > 120:
            raise ContentError(f"{label}.logo_alt: required with logo, 1-120 characters of plain text")
        size = _brand_asset(value["logo"], assets_dir, f"{label}.logo", (".png", ".webp", ".svg"))
        if size and not all(1 <= side <= LOGO_MAX_SIDE for side in size):
            raise ContentError(f"{label}.logo: {size[0]}x{size[1]} px; each side must be 1-{LOGO_MAX_SIDE} px")
        brand.update(logo=value["logo"], logo_alt=alt, logo_size=size)
    if "favicon" in value:
        _brand_asset(value["favicon"], assets_dir, f"{label}.favicon", (".ico", ".png", ".svg"))
        brand["favicon"] = value["favicon"]
    return brand


def load_share_image(value, assets_dir=None):
    """site.json.share_image: a PNG/JPEG public asset 1200x630 to 2400x1260 (ratio 1.905) plus required alt; refusals name the field."""
    label = "site.json.share_image"
    _require_keys(value, SHARE_IMAGE_KEYS, set(), label)
    alt = value.get("alt")
    if (not isinstance(alt, str) or not alt.strip() or len(alt) > 300
            or any(ord(char) < 32 or ord(char) == 127 for char in alt)):  # "<" and quotes are fine: the page escapes them
        raise ContentError(f"{label}.alt: required, 1-300 characters on one line, no control characters")
    size = _brand_asset(value.get("path"), assets_dir, f"{label}.path", (".png", ".jpg", ".jpeg"))
    if size:
        width, height = size
        if not (1200 <= width <= 2400 and 630 <= height <= 1260) or abs(width / height - SHARE_RATIO) > SHARE_RATIO_TOLERANCE:
            raise ContentError(f"{label}.path: {width}x{height} px; use 1200x630 to 2400x1260 at a 1.905 ratio")
    return {"path": value["path"], "alt": alt, "size": size}


def load_site(document, assets_dir=None):
    """``assets_dir`` (content/public-assets) lets ``brand`` check its logo and favicon files."""
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
    urls = {}
    for field, required in (("form_html_allowlisted_url", signup["mode"] == "brevo_hosted"), ("privacy_url", False)):
        if required or signup.get(field) is not None:
            urls[field] = _https(signup.get(field), f"site.json.signup.{field}")
            host = urlsplit(urls[field]).hostname.lower()
            if not any(host.endswith(suffix) and host != suffix.lstrip(".") for suffix in SIGNUP_HOST_SUFFIXES):
                raise ContentError(f"site.json.signup.{field}: host is not an allowlisted "
                                   "hosted-form provider (" + ", ".join("*" + s for s in SIGNUP_HOST_SUFFIXES) + ")")
    form_url, privacy_url = urls.get("form_html_allowlisted_url"), urls.get("privacy_url")
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
        "privacy_contact": _email(document.get("privacy_contact"), "site.json.privacy_contact"),
        "signup": {"mode": signup["mode"], "url": form_url, "privacy_url": privacy_url,
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
        **({"brand": load_brand(document["brand"], assets_dir)} if "brand" in document else {}),
        **({"share_image": load_share_image(document["share_image"], assets_dir)} if "share_image" in document else {}),
    }


# --- findings --------------------------------------------------------------

FINDING_KEYS = {"id", "title", "slug", "summary", "body_md", "classification", "confidence",
                "event_date", "sources", "published_at", "updated_at", "corrections", "state",
                "author", "limitations", "counterevidence", "agency_id", "rule_version",
                "duty", "exceptions", "rule_ids"}
FINDING_REQUIRED = {"id", "title", "slug", "summary", "classification", "confidence",
                    "event_date", "sources", "published_at", "corrections", "state",
                    "author", "limitations", "counterevidence"}
SOURCE_KEYS = {"sha256", "locator", "title", "rule_id", "page"}
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
        page = source.get("page")
        if "page" in source and not (type(page) is int and 1 <= page <= 100000):
            raise ContentError(f"{label}.sources[{index}].page: a whole number of 1 or more")
        sources.append({
            "sha256": source["sha256"],
            "locator": _text(source, "locator", f"{label}.sources[{index}]", allow_empty=False),
            "title": _text(source, "title", f"{label}.sources[{index}]", default="Source record"),
            "rule_id": _text(source, "rule_id", f"{label}.sources[{index}]", required=False, default=None),
            "page": page,
        })
    published_at = _timestamp(document["published_at"], f"{label}.published_at")
    published_day = datetime.fromisoformat(published_at.replace("Z", "+00:00")).date()
    corrections = []
    for index, correction in enumerate(document["corrections"]):
        _require_keys(correction, CORRECTION_KEYS, CORRECTION_KEYS, f"{label}.corrections[{index}]")
        corrections.append({"date": _timestamp(correction["date"], f"{label}.corrections[{index}].date", date_only=True),
                            "note": _text(correction, "note", f"{label}.corrections[{index}]", allow_empty=False)})
        if datetime.strptime(corrections[-1]["date"], "%Y-%m-%d").date() < published_day:
            raise ContentError(f"{label}.corrections[{index}].date: is before the publication date {published_day}")
    updated_at = _timestamp(document["updated_at"], f"{label}.updated_at") if document.get("updated_at") else None
    if updated_at and utc_moment(updated_at) < utc_moment(published_at):
        raise ContentError(f"{label}.updated_at: {updated_at} is before published_at {published_at}")
    agency_id = document.get("agency_id")
    if agency_id is not None and (not isinstance(agency_id, str) or not AGENCY_ID.fullmatch(agency_id)):
        raise ContentError(f"{label}.agency_id: invalid agency identifier")
    rule_ids = _text_list(document, "rule_ids", label)
    if any(not item.strip() or len(item) > 120 for item in rule_ids):
        raise ContentError(f"{label}.rule_ids: each rule id must be a non-empty string of at most 120 characters")
    if len(set(rule_ids)) != len(rule_ids):
        raise ContentError(f"{label}.rule_ids: duplicate rule id")
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
        "published_at": published_at,
        "updated_at": updated_at,
        "corrections": corrections,
        "author": _text(document, "author", label, allow_empty=False),
        "limitations": _text_list(document, "limitations", label),
        "counterevidence": _text_list(document, "counterevidence", label),
        "agency_id": agency_id,
        "rule_ids": rule_ids,
        "rule_version": _text(document, "rule_version", label, required=False, default=None),
        "duty": _text(document, "duty", label, required=False, default=None),
        "exceptions": (_text_list(document, "exceptions", label) if isinstance(document.get("exceptions"), list)
                       else _text(document, "exceptions", label, required=False, default=None)),
    }


def load_finding_preview(document, label, now=None):
    """``load_finding`` for a draft preview (DF1822-02). A draft not yet at ``state: published`` is read as
    ``finding publish`` would write it (publication fields replaced: published, ``published_at`` = the preview
    time, no corrections), for validation and rendering only; every refusal is ``load_finding``'s own."""
    if not isinstance(document, dict):
        raise ContentError(f"{label}: expected an object")
    if document.get("state") != "published":
        when = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        document = {key: value for key, value in document.items()
                    if key not in ("state", "published_at", "updated_at", "corrections")}
        document.update(state="published", published_at=when, corrections=[])
    return load_finding(document, label)


# --- meetings --------------------------------------------------------------

MEETING_KEYS = {"id", "body", "starts_at", "agenda_url", "agenda_item", "comment_kit_md", "location", "agency_id",
                "status", "participation", "outcome"}
MEETING_STATUSES = ("scheduled", "cancelled")
OUTCOME_RESULTS = ("approved", "denied", "continued", "amended", "no_action", "informational")
OUTCOME_KEYS = {"result", "on", "source_url", "verified_by", "verified_at", "vote", "votes"}
VOTE_VALUES = ("yes", "no", "abstain", "absent", "recused")
PARTICIPATION_KEYS = {"in_person", "remote", "written_comment", "speaker_minutes", "source_url", "verified_by",
                      "verified_at"}


def load_participation(value, starts_at, label, today=None):
    """Organizer-verified participation facts; ``None`` fields mean "not stated by the source"."""
    _require_keys(value, PARTICIPATION_KEYS, {"source_url", "verified_by", "verified_at"}, label)
    starts = datetime.fromisoformat(starts_at.replace("Z", "+00:00"))
    in_person, remote, written = value.get("in_person"), value.get("remote"), value.get("written_comment")
    if in_person is not None and not isinstance(in_person, bool):
        raise ContentError(f"{label}.in_person: must be true, false or null")
    if remote is not None:
        _require_keys(remote, {"available", "how"}, {"available"}, f"{label}.remote")
        if not isinstance(remote["available"], bool):
            raise ContentError(f"{label}.remote.available: must be true or false")
        remote = {"available": remote["available"], "how": _text(remote, "how", f"{label}.remote")}
    if written is not None:
        where = f"{label}.written_comment"
        _require_keys(written, {"how", "deadline"}, {"how", "deadline"}, where)
        if written["deadline"] is not None:
            due = datetime.fromisoformat(_timestamp(written["deadline"], f"{where}.deadline").replace("Z", "+00:00"))
            if due.tzinfo is None or due >= starts:
                raise ContentError(f"{where}.deadline: needs a UTC offset and must be before starts_at")
        written = {"how": _text(written, "how", where), "deadline": written["deadline"]}
    for key, part in (("remote", remote), ("written_comment", written)):
        if part and len(part["how"]) > 200:
            raise ContentError(f"{label}.{key}.how: at most 200 characters")
    minutes = value.get("speaker_minutes")  # per-speaker limit from the same official source; never assumed
    if minutes is not None and (type(minutes) is not int or not 1 <= minutes <= 10):
        raise ContentError(f"{label}.speaker_minutes: must be a whole number of minutes from 1 to 10, or null")
    verified_at = _timestamp(value["verified_at"], f"{label}.verified_at", date_only=True)
    checked = datetime.strptime(verified_at, "%Y-%m-%d").date()
    if checked > (today or date.today()) or (starts.date() - checked).days > 30:
        raise ContentError(f"{label}.verified_at: must not be in the future or more than 30 days before the meeting")
    return {"in_person": in_person, "remote": remote, "written_comment": written,
            "source_url": _https(value["source_url"], f"{label}.source_url"),
            "verified_by": _text(value, "verified_by", label, allow_empty=False), "verified_at": verified_at,
            **({"speaker_minutes": minutes} if minutes is not None else {})}


def load_outcome(value, starts_at, status, label, today=None):
    """Organizer-verified result of a past meeting (DF2237-11), read from an official https record."""
    if status == "cancelled":
        raise ContentError(f"{label}: a cancelled meeting has no outcome")
    _require_keys(value, OUTCOME_KEYS, OUTCOME_KEYS - {"vote", "votes"}, label)
    if not isinstance(value["result"], str) or value["result"] not in OUTCOME_RESULTS:
        raise ContentError(f"{label}.result: must be one of {', '.join(OUTCOME_RESULTS)}")
    held, now = starts_at[:10], (today or date.today()).isoformat()
    for key, earliest in (("on", held), ("verified_at", value["on"])):
        day = _timestamp(value[key], f"{label}.{key}", date_only=True)
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) or not earliest <= day <= now:
            raise ContentError(f"{label}.{key}: must be a YYYY-MM-DD date from {earliest} to {now}")
    for key in ("verified_by", "vote"):
        text = value.get(key, "-")
        if (not isinstance(text, str) or not plain_text(text) or not text.strip() or len(text) > 120
                or "\n" in text or "\t" in text):
            raise ContentError(f"{label}.{key}: must be 1 to 120 characters of plain text "
                               "(no angle brackets or control characters)")
    votes = value.get("votes", [])  # DF0137-07: per-member votes; names are cross-checked against officials later
    if "votes" in value and (not isinstance(votes, list) or not 1 <= len(votes) <= 30):
        raise ContentError(f"{label}.votes: must list 1 to 30 votes")
    for index, item in enumerate(votes):
        where = f"{label}.votes[{index}]"
        _require_keys(item, {"name", "vote"}, {"name", "vote"}, where)
        name = _text(item, "name", where, allow_empty=False)
        if len(name) > 120 or "\n" in name or "\t" in name:
            raise ContentError(f"{where}.name: at most 120 characters on one line")
        if any(other["name"] == name for other in votes[:index]):
            raise ContentError(f"{where}.name: listed twice")
        if not isinstance(item["vote"], str) or item["vote"] not in VOTE_VALUES:
            raise ContentError(f"{where}.vote: must be one of {', '.join(VOTE_VALUES)}")
    return {"result": value["result"], "on": value["on"], "source_url": _https(value["source_url"], f"{label}.source_url"),
            "verified_by": value["verified_by"], "verified_at": value["verified_at"],
            **({"vote": value["vote"]} if "vote" in value else {}),
            **({"votes": [{"name": v["name"], "vote": v["vote"]} for v in votes]} if "votes" in value else {})}


def load_meeting(document, label, today=None):
    _require_keys(document, MEETING_KEYS, {"body", "starts_at"}, label)
    agency_id = document.get("agency_id")
    if agency_id is not None and (not isinstance(agency_id, str) or not AGENCY_ID.fullmatch(agency_id)):
        raise ContentError(f"{label}.agency_id: invalid agency identifier")
    status = document.get("status", "scheduled")
    if not isinstance(status, str) or status not in MEETING_STATUSES:
        raise ContentError(f"{label}.status: must be scheduled or cancelled")
    starts_at = _timestamp(document["starts_at"], f"{label}.starts_at")
    if datetime.fromisoformat(starts_at.replace("Z", "+00:00")).tzinfo is None:
        raise ContentError(f"{label}.starts_at: needs a UTC offset (e.g. -07:00)")
    return {
        "id": _slug(document["id"], f"{label}.id") if "id" in document else None,
        "body": _text(document, "body", label, allow_empty=False),
        "starts_at": starts_at,
        "agenda_url": _https(document.get("agenda_url"), f"{label}.agenda_url", allow_none=True),
        "agenda_item": _text(document, "agenda_item", label, default=""),
        "comment_kit_md": _text(document, "comment_kit_md", label, default=""),
        "location": _text(document, "location", label, default=""),
        "agency_id": agency_id,
        "status": status,
        "participation": (load_participation(document["participation"], starts_at, f"{label}.participation", today)
                          if document.get("participation") is not None else None),
        "outcome": (load_outcome(document["outcome"], starts_at, status, f"{label}.outcome", today)
                    if "outcome" in document else None),
    }


# --- sources.json ----------------------------------------------------------

LIBRARY_KEYS = {"sha256", "title", "agency_id", "received_at", "public_url", "fragment", "pages", "note", "public_copy"}
COPY_REASONS = ("redacted", "converted", "unknown")


def _public_copy(entry, label):
    """Optional organizer check of the linked public copy (DF2037-05): {matches, checked_on, reason iff differing}."""
    value, label = entry["public_copy"], f"{label}.public_copy"
    if entry["public_url"] is None:
        raise ContentError(f"{label}: needs a public_url")
    _require_keys(value, {"matches", "checked_on", "reason"}, {"matches", "checked_on"}, label)
    if type(value["matches"]) is not bool:
        raise ContentError(f"{label}.matches: must be true or false")
    _timestamp(value["checked_on"], f"{label}.checked_on", date_only=True)
    if value["matches"] == ("reason" in value) or value.get("reason", "redacted") not in COPY_REASONS:
        raise ContentError(f"{label}.reason: one of {', '.join(COPY_REASONS)}, present only when matches is false")
    return {key: value[key] for key in ("matches", "checked_on", "reason") if key in value}


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
            **({"public_copy": _public_copy(entry, label)} if "public_copy" in entry else {}),
        })
    return entries


def check_source_pages(findings, sources):
    """DF2037-04: problem lines for finding ``sources[].page`` against the library (build and content-check)."""
    problems = []
    for finding in findings:
        for index, source in enumerate(finding["sources"]):
            if source.get("page") is None:
                continue
            where = f"findings/{finding['slug']}.sources[{index}].page"
            entry = next((s for s in sources if s["sha256"] == source["sha256"]), None)  # as site.finding_html
            if entry is None:
                problems.append(f"{where}: source is not in the library")
            elif not entry["public_url"]:
                problems.append(f"{where}: the source has no public copy")
            elif entry["pages"] and source["page"] > entry["pages"]:
                problems.append(f"{where}: above the copy's {entry['pages']} pages")
    return problems


# --- agencies --------------------------------------------------------------

AGENCY_KEYS = {"agency_id", "name", "kind", "jurisdiction_name", "records_url", "records_email",
               "portal", "selected", "verified", "sources", "slug", "profile", "place_fips",
               "flock_transparency_slug", "muckrock_agency_id", "notes", "verification", "asked"}
ASKED_STATES = ("awaiting_response", "acknowledged", "partly_answered", "answered", "declined_in_writing", "withdrawn")
ASKED_KEYS, ASKED_TEXT = ("topic", "asked_on", "state", "updated_on", "note"), {"topic": 120, "note": 200}
CONTRACT_KEYS = {"vendor", "start", "end", "renewal_note", "source_url", "checked_on"}
FLOCK_SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+){0,7}")
VERIFIED_FIELDS = ("records_email", "records_url", "portal")


def _verification(entry, item):
    """Where and when an organizer checked the contact; required when ``verified`` is true, never fetched."""
    value = entry.get("verification")
    label = f"{item}.verification"
    if value is None:
        if entry.get("verified") is True:
            raise ContentError(f"{label}: verified:true needs source_url and checked_on")
        return None
    _require_keys(value, {"source_url", "checked_on", "fields"}, {"source_url", "checked_on"}, label)
    source_url = _https(value["source_url"], f"{label}.source_url")
    checked_on = _timestamp(value["checked_on"], f"{label}.checked_on", date_only=True)
    checked = datetime.strptime(checked_on, "%Y-%m-%d").date()
    if checked.isoformat() != checked_on or checked > date.today():
        raise ContentError(f"{label}.checked_on: must be a YYYY-MM-DD date not after the build date")
    fields = value.get("fields", [])
    if (not isinstance(fields, list) or any(name not in VERIFIED_FIELDS for name in fields)
            or len(set(fields)) != len(fields)):
        raise ContentError(f"{label}.fields: list names from {list(VERIFIED_FIELDS)} only, each once")
    return {"source_url": source_url, "checked_on": checked_on, "fields": list(fields)}


def _calendar_day(value, label):
    day = datetime.strptime(_timestamp(value, label, date_only=True), "%Y-%m-%d").date()
    if day.isoformat() != value:
        raise ContentError(f"{label}: must be a YYYY-MM-DD date")
    return day


def _contract(value, label):
    """An organizer-checked contract term (``profile.contract``) from its official source; never fetched."""
    if value is None:
        return None
    if isinstance(value, dict) and not {"source_url", "checked_on"} <= set(value):
        raise ContentError(f"{label}: needs source_url and checked_on")
    _require_keys(value, CONTRACT_KEYS, CONTRACT_KEYS - {"renewal_note"}, label)
    vendor, note = _text(value, "vendor", label, allow_empty=False), _text(value, "renewal_note", label)
    for key, text, limit in (("vendor", vendor, 80), ("renewal_note", note, 200)):
        if len(text) > limit:
            raise ContentError(f"{label}.{key}: at most {limit} characters")
    start, end = _calendar_day(value["start"], f"{label}.start"), _calendar_day(value["end"], f"{label}.end")
    if end < start:
        raise ContentError(f"{label}.end: must not be before start")
    if _calendar_day(value["checked_on"], f"{label}.checked_on") > date.today():
        raise ContentError(f"{label}.checked_on: must not be after the build date")
    return {"vendor": vendor, "start": value["start"], "end": value["end"], "renewal_note": note,
            "source_url": _https(value["source_url"], f"{label}.source_url"), "checked_on": value["checked_on"]}


ALPR_POLICY_KEYS = {"url", "source_url", "checked_on"}


def _alpr_policy(value, label):
    """A posted ALPR usage policy an organizer found (``profile.alpr_policy``); linked only, never fetched or judged."""
    if value is None:
        return None
    if isinstance(value, dict) and not ALPR_POLICY_KEYS <= set(value):
        raise ContentError(f"{label}: needs url, source_url and checked_on")
    _require_keys(value, ALPR_POLICY_KEYS, ALPR_POLICY_KEYS, label)
    try:
        checked = _calendar_day(value["checked_on"], f"{label}.checked_on")
    except ContentError:
        checked = None
    if checked is None or checked > date.today():
        raise ContentError(f"{label}.checked_on: must be a YYYY-MM-DD date not after the build date")
    return {"url": _https(value["url"], f"{label}.url"),
            "source_url": _https(value["source_url"], f"{label}.source_url"), "checked_on": value["checked_on"]}


TRANSPARENCY_FIELDS = ("cameras", "retention_days", "searches_last_30_days", "hotlist_alerts", "shared_with_agencies")


def _transparency(value, label, slug, today=None):
    """Organizer-transcribed Flock transparency-portal snapshots (``profile.transparency``), newest first; never fetched."""
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 24:
        raise ContentError(f"{label}: must be a list of at most 24 snapshots")
    if value and slug is None:
        raise ContentError(f"{label}: needs a verified agency with a flock_transparency_slug")
    result = []
    for index, snapshot in enumerate(value):
        item = f"{label}[{index}]"
        _require_keys(snapshot, {"checked_on", "source_url", *TRANSPARENCY_FIELDS}, {"checked_on", "source_url"}, item)
        if _calendar_day(snapshot["checked_on"], f"{item}.checked_on") > (today or date.today()):
            raise ContentError(f"{item}.checked_on: must not be after the build date")
        if result and snapshot["checked_on"] >= result[-1]["checked_on"]:
            raise ContentError(f"{item}.checked_on: " + ("duplicate date" if snapshot["checked_on"] == result[-1]["checked_on"]
                                                          else "snapshots must be listed newest first"))
        portal = f"https://transparency.flocksafety.com/{slug}"
        if _https(snapshot["source_url"], f"{item}.source_url") not in (portal, portal + "/"):
            raise ContentError(f"{item}.source_url: must be {portal}")
        clean = {"checked_on": snapshot["checked_on"], "source_url": snapshot["source_url"]}
        for key in TRANSPARENCY_FIELDS:
            number = snapshot.get(key)
            if number is not None and (type(number) is not int or number < 0):
                raise ContentError(f"{item}.{key}: must be null or a non-negative integer")
            if key == "retention_days" and number is not None and number > 3650:
                raise ContentError(f"{item}.retention_days: at most 3650")
            clean[key] = number
        result.append(clean)
    return result


def _flock_url(entry, item):
    """Transparency-portal link for an organizer-verified slug; built from the slug only, never fetched."""
    slug = entry.get("flock_transparency_slug")
    if slug is None:
        return None
    if not isinstance(slug, str) or len(slug) > 80 or not FLOCK_SLUG.fullmatch(slug):
        raise ContentError(f"{item}.flock_transparency_slug: lowercase letters, digits and single hyphens only")
    return f"https://transparency.flocksafety.com/{slug}" if entry.get("verified") is True else None


def _agency_slug(agency):
    if "slug" in agency:
        return _slug(agency["slug"], f"agencies[{agency.get('agency_id')}].slug")
    prefix, _, rest = agency["agency_id"].partition("-")
    return _slug(rest, f"agencies[{agency['agency_id']}] derived slug")


def _asked(entry, label, today=None, problems=None):
    """Organizer-entered requests; every problem goes to ``problems`` (content-check), else the first is raised."""
    issues, result, values = [], [], entry.get("asked", [])
    if not isinstance(values, list) or len(values) > 20:
        issues, values = [f"{label}: must be a list of at most 20 items"], []
    for index, value in enumerate(values):
        item, clean = f"{label}[{index}]", {}
        if not isinstance(value, dict):
            issues.append(f"{item}: must be an object")
            continue
        issues += [f"{item}: unknown field {key!r}" for key in sorted(set(value) - set(ASKED_KEYS))]
        for key in [key for key in ASKED_KEYS if key in value or key in ("topic", "asked_on", "state")]:
            try:
                if key in ASKED_TEXT and len(_text(value, key, item, allow_empty=key == "note")) > ASKED_TEXT[key]:
                    raise ContentError(f"{item}.{key}: at most {ASKED_TEXT[key]} characters")
                if key == "state" and value.get(key) not in ASKED_STATES:
                    raise ContentError(f"{item}.state: choose one of {list(ASKED_STATES)}")
                if key.endswith("_on") and _calendar_day(value.get(key), f"{item}.{key}") > (today or date.today()):
                    raise ContentError(f"{item}.{key}: must not be after the build date")
                clean[key] = value[key]
            except ContentError as exc:
                issues.append(str(exc))
        if clean.get("updated_on", "9") < clean.get("asked_on", ""):
            issues.append(f"{item}.updated_on: must not be before asked_on")
        result.append(clean)
    if problems is not None:
        problems.extend(issues)
    elif issues:
        raise ContentError(issues[0])
    return result


def load_agencies(document, label, today=None):
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
        _require_keys(profile, PROFILE_KEYS | {"contract", "alpr_policy", "transparency"}, set(), f"{item}.profile")  # not county_profile
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
            "verification": _verification(entry, item),
            "asked": _asked(entry, f"{item}.asked", today) if label == "content/agencies.json" else [],
            "flock_transparency_url": _flock_url(entry, item),
            "contract": _contract(profile.get("contract"), f"{item}.profile.contract"),  # kept out of card JS
            "alpr_policy": _alpr_policy(profile.get("alpr_policy"), f"{item}.profile.alpr_policy"),  # likewise
            "transparency": _transparency(profile.get("transparency"), f"{item}.profile.transparency",  # likewise
                                          entry.get("flock_transparency_slug") if _flock_url(entry, item) else None, today),
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


SNAPSHOT_KEYS = {"schema_version", "osm_base", "source"}
OSM_BASE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")


def load_snapshot(path):
    """map/snapshot.json from tools/fetch_osm_alprs.py -> the OSM data day "YYYY-MM-DD", or None for a null osm_base."""
    document = _read_json(path, 4096)
    _require_keys(document, SNAPSHOT_KEYS, SNAPSHOT_KEYS, "map/snapshot.json")
    version, base = document["schema_version"], document["osm_base"]
    if type(version) is not int or version != 1:
        raise ContentError("map/snapshot.json.schema_version: must be 1")
    if base is not None and not (isinstance(base, str) and OSM_BASE.fullmatch(base)):
        raise ContentError("map/snapshot.json.osm_base: must be a UTC timestamp YYYY-MM-DDTHH:MM:SSZ or null")
    if document["source"] not in ("overpass", "saved_file"):
        raise ContentError("map/snapshot.json.source: must be 'overpass' or 'saved_file'")
    return base[:10] if base else None


HISTORY_KEYS = {"schema_version", "records"}
HISTORY_RECORD_KEYS = {"recorded_on", "osm_base", "source", "total", "by_city", "added", "removed"}
MAX_HISTORY_RECORDS, MAX_HISTORY_BYTES, MAX_HISTORY_CITIES = 120, 256 * 1024, 500
DAY = re.compile(r"\d{4}-\d{2}-\d{2}")


def _count(value):
    return type(value) is int and value >= 0


def history_present(path):
    """map/history.json is optional; any entry under that name, a dangling link too, is loaded and so judged."""
    return path.is_symlink() or path.exists()


def load_history(path):
    """map/history.json (DF0137-13) -> its validated records, oldest first; a refusal names the field, never a value."""
    path, label = Path(path), "map/history.json"
    if path.is_symlink():
        raise ContentError(f"{label}: symlinks are not read")
    if not path.is_file():  # a FIFO or directory is never opened
        raise ContentError(f"{label}: not a regular file")
    if path.stat().st_size > MAX_HISTORY_BYTES:
        raise ContentError(f"{label}: file too large (over {MAX_HISTORY_BYTES // 1024} KiB)")
    document = _read_json(path, MAX_HISTORY_BYTES)
    _require_keys(document, HISTORY_KEYS, HISTORY_KEYS, label)
    if type(document["schema_version"]) is not int or document["schema_version"] != 1:
        raise ContentError(f"{label}.schema_version: must be 1")
    records = document["records"]
    if not isinstance(records, list) or len(records) > MAX_HISTORY_RECORDS:
        raise ContentError(f"{label}.records: expected a list of at most {MAX_HISTORY_RECORDS} records")
    previous = None
    for index, record in enumerate(records):
        where = f"{label}.records[{index}]"
        _require_keys(record, HISTORY_RECORD_KEYS, HISTORY_RECORD_KEYS, where)
        day, base = record["recorded_on"], record["osm_base"]
        try:
            valid_day = isinstance(day, str) and DAY.fullmatch(day) and date.fromisoformat(day)
        except ValueError:
            valid_day = False
        if not valid_day:
            raise ContentError(f"{where}.recorded_on: must be an ISO date YYYY-MM-DD")
        if base is not None and not (isinstance(base, str) and OSM_BASE.fullmatch(base) and base[:10] == day):
            raise ContentError(f"{where}.osm_base: must be null or a UTC timestamp on the recorded_on day")
        if record["source"] not in ("overpass", "saved_file"):
            raise ContentError(f"{where}.source: must be 'overpass' or 'saved_file'")
        for key in ("total", "added", "removed"):
            if not _count(record[key]):
                raise ContentError(f"{where}.{key}: must be a non-negative integer")
        by_city = record["by_city"]
        if by_city is not None and (not isinstance(by_city, dict) or len(by_city) > MAX_HISTORY_CITIES or any(
                not (plain_text(name) and name.strip() and len(name) <= 120) or not _count(count)
                for name, count in by_city.items())):
            raise ContentError(f"{where}.by_city: must be null or {{city name: non-negative integer}} "
                               f"(at most {MAX_HISTORY_CITIES} names of plain text)")
        if previous is not None and day <= previous:
            raise ContentError(f"{where}.recorded_on: dates must be strictly increasing")
        previous = day
    return records


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
    snapshot_path = map_dir / "snapshot.json"  # optional: a map fetched before DF1507-09 has none
    history_path = map_dir / "history.json"  # optional (DF0137-13): one record per writing fetch
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
        "snapshot_date": load_snapshot(snapshot_path) if snapshot_path.is_file() else None,
        "history": load_history(history_path) if history_present(history_path) else [],
    }


# --- explainer pages (DF2237-07): content/pages/<slug>.json -> /<slug>.html ----
PAGE_KEYS, MAX_PAGES, MAX_NAV_PAGES = {"title", "description", "body_md", "nav"}, 50, 3
RESERVED_PAGE_SLUGS = STARTER_SLUGS | {"404", "corrections", "feed", "sitemap", "robots", "style", "site-data",
                                       "agency-cards", "app", "data", "vendor", "meetings", "methods", "privacy"}  # generated top-level names


def _page_string(document, key, label, limit, required):
    value = document.get(key, "")
    if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
        kind = "non-empty string" if required else "string"
        raise ContentError(f"{label}.{key}: must be a {kind} of at most {limit} characters")
    return value


def load_pages(content_dir, finding_slugs):
    """Pages sorted by slug, ``[]`` without the folder; non-``.json`` names ignored. Text is escaped at render."""
    folder = Path(content_dir) / "pages"
    if folder.is_symlink():
        raise ContentError("pages: the folder must not be a symlink")
    paths = sorted(path for path in folder.iterdir() if path.name.endswith(".json")) if folder.is_dir() else []
    if len(paths) > MAX_PAGES:
        raise ContentError(f"pages: at most {MAX_PAGES} page files")
    pages = []
    for path in paths:
        label, slug = "pages/" + path.name, path.stem
        if path.is_symlink() or not path.is_file():
            raise ContentError(f"{label}: must be a regular file, not a symlink or folder")
        if slug in RESERVED_PAGE_SLUGS or slug in finding_slugs:
            raise ContentError(f"{label}: slug {slug} is reserved")
        _slug(slug, f"{label} filename")
        document = _read_json(path)
        _require_keys(document, PAGE_KEYS, {"title", "body_md"}, label)
        if not isinstance(document.get("nav", False), bool):
            raise ContentError(f"{label}.nav: must be true or false")
        pages.append({"slug": slug, "title": _page_string(document, "title", label, 120, True),
                      "description": _page_string(document, "description", label, 300, False),
                      "body_md": _page_string(document, "body_md", label, 200 * 1024, True),
                      "nav": document.get("nav", False)})
    if sum(page["nav"] for page in pages) > MAX_NAV_PAGES:
        raise ContentError(f"pages: at most {MAX_NAV_PAGES} pages may set nav: true")
    return pages


OFFICIALS_KEYS = {"schema_version", "bodies"}
OFFICIAL_KEYS = {"name", "role", "profile_url", "source_url", "checked_on"}
CONTACT_LIKE = re.compile(r"@|\d(?:[\s().+-]*\d){6}")  # an address or a phone-like run of 7+ digits


def load_officials(document, agencies, today):
    """``content/officials.json`` (DF2237-13): organizer-checked decision makers per loaded agency, ``{agency_id: [..]}``.

    No contact fields exist; each ``checked_on`` must fall within the 365 days up to ``today``. Never fetched."""
    _require_keys(document, OFFICIALS_KEYS, OFFICIALS_KEYS, "officials.json")
    if document["schema_version"] != 1:
        raise ContentError("officials.json.schema_version: must be 1")
    bodies = document["bodies"]
    if not isinstance(bodies, list) or len(bodies) > 100:
        raise ContentError("officials.json.bodies: must be a list of at most 100 bodies")
    known, result = {agency["agency_id"] for agency in agencies}, {}
    for index, body in enumerate(bodies):
        label = f"officials.json.bodies[{index}]"
        _require_keys(body, {"agency_id", "officials"}, {"agency_id", "officials"}, label)
        if not isinstance(body["agency_id"], str) or body["agency_id"] not in known:
            raise ContentError(f"{label}.agency_id: not a loaded agency")
        if body["agency_id"] in result:
            raise ContentError(f"{label}.agency_id: listed more than once")
        people = body["officials"]
        if not isinstance(people, list) or not 1 <= len(people) <= 30:
            raise ContentError(f"{label}.officials: must list 1 to 30 officials")
        result[body["agency_id"]] = []
        for number, person in enumerate(people):
            item = f"{label}.officials[{number}]"
            _require_keys(person, OFFICIAL_KEYS, OFFICIAL_KEYS - {"profile_url"}, item)
            for key in ("name", "role"):
                text = _text(person, key, item, allow_empty=False)
                if len(text) > 120:
                    raise ContentError(f"{item}.{key}: at most 120 characters")
                if CONTACT_LIKE.search(text):
                    raise ContentError(f"{item}.{key}: no contact details in officials.json")
            checked = _calendar_day(person["checked_on"], f"{item}.checked_on")
            if checked > today:
                raise ContentError(f"{item}.checked_on: must not be after the build date")
            if (today - checked).days > 365:
                raise ContentError(f"{item}.checked_on: older than 365 days; re-check on the official site")
            result[body["agency_id"]].append({
                "name": person["name"], "role": person["role"],
                "profile_url": _https(person.get("profile_url"), f"{item}.profile_url", allow_none=True),
                "source_url": _https(person["source_url"], f"{item}.source_url"), "checked_on": person["checked_on"]})
    return result


# --- assembly ---------------------------------------------------------------

def load_content(root, today=None):
    """Return the validated content model for ``root`` or None when no content/ exists."""
    root = Path(root)
    content_dir = root / "content"
    if not content_dir.is_dir():
        return None
    site_path = content_dir / "site.json"
    if not site_path.is_file():
        raise ContentError("content/site.json is required when content/ exists")
    site = load_site(_read_json(site_path), content_dir / "public-assets")

    findings, slugs, ids = [], set(), set()
    for path in sorted((content_dir / "findings").glob("*.json")) if (content_dir / "findings").is_dir() else []:
        finding = load_finding(_read_json(path), "findings/" + path.name)
        if finding["slug"] in slugs or finding["id"] in ids:
            raise ContentError(f"findings/{path.name}: duplicate finding slug or id")
        slugs.add(finding["slug"])
        ids.add(finding["id"])
        findings.append(finding)
    from .finding_law import check_findings  # local: finding_law imports this module
    law_problems = check_findings(root, findings)
    if law_problems:
        raise ContentError(law_problems[0])
    findings.sort(key=lambda f: (f["published_at"], f["slug"]), reverse=True)

    meetings, meeting_ids = [], {}
    for path in sorted((content_dir / "meetings").glob("*.json")) if (content_dir / "meetings").is_dir() else []:
        meeting = load_meeting(_read_json(path), "meetings/" + path.name)
        meeting["id"] = meeting["id"] or _slug(path.stem, "meetings/" + path.name + " filename")
        if meeting["id"] in meeting_ids:  # each id names one meetings/<id>.html page
            raise ContentError(f"meetings/{path.name}: duplicate meeting id {meeting['id']!r} "
                               f"(also meetings/{meeting_ids[meeting['id']]})")
        meeting_ids[meeting["id"]] = path.name
        meetings.append(meeting)
    meetings.sort(key=lambda m: m["starts_at"])

    sources_path = content_dir / "sources.json"
    sources = load_sources(_read_json(sources_path)) if sources_path.is_file() else []
    page_problems = check_source_pages(findings, sources)
    if page_problems:
        raise ContentError(page_problems[0])

    agencies_path = content_dir / "agencies.json"
    if agencies_path.is_file():
        agencies = load_agencies(_read_json(agencies_path), "content/agencies.json", today)
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

    from .labels import DEFAULTS, MAX_LABELS_BYTES, load_labels  # local: labels imports this module
    labels_path = content_dir / "labels.json"  # optional; a symlink is refused by _read_json
    labels = (load_labels(_read_json(labels_path, MAX_LABELS_BYTES))
              if labels_path.is_file() or labels_path.is_symlink() else dict(DEFAULTS))
    redirects_path = content_dir / "redirects.json"
    officials_path = content_dir / "officials.json"  # optional; a symlink is refused by _read_json
    officials = (load_officials(_read_json(officials_path), agencies, today or date.today())
                 if officials_path.is_file() or officials_path.is_symlink() else {})
    for meeting in meetings:  # DF0137-07: a stored vote must still name one of the body's listed officials
        listed = {person["name"] for person in officials.get(meeting["agency_id"], [])}
        for index, vote in enumerate((meeting["outcome"] or {}).get("votes", [])):
            if vote["name"] not in listed:
                raise ContentError(f"meetings/{meeting_ids[meeting['id']]}.outcome.votes[{index}].name: "
                                   f"not in officials.json for {meeting['agency_id']}")

    model = {
        "site": site,
        "findings": findings,
        "pages": load_pages(content_dir, slugs),
        "meetings": meetings,
        "sources": sources,
        "agencies": agencies,
        "map": map_config,
        "vendor": vendor,
        "public_assets": public_assets,
        "assets_dir": assets_dir,
        "labels": labels,
        "officials": officials,
    }
    # content/redirects.json (DF2037-16): the key is present exactly when the file is, so a parsed JSON null is still
    # checked (and refused) by site against the pages it writes; without the file there is no key.
    if redirects_path.is_file() or redirects_path.is_symlink():
        model["redirects"] = _read_json(redirects_path)
    return model
