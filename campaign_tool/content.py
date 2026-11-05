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
import os
import re
import unicodedata
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

from . import law, map_controller
from .agency_cards import text as plain_text, visible
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
STARTER_SLUGS = {"index", "agencies", "findings", "sources", "meetings", "about", "glossary", "supporters", "newsletters", "faq"}  # DF1952-04 / DF2207-13 / DF1952-09 / DF0022-01: reserved even without content


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


def _quote(source, label):
    """DF1952-08: the optional transcribed excerpt of a finding source, returned exactly as written.

    1-400 code points, at least one non-whitespace character, no Unicode control or format characters (Cc/Cf: tabs,
    newlines, DEL, C1 and bidi controls included). Angle brackets are ordinary text (the site escapes it). Nothing is
    coerced, stripped or truncated, and the refusal never repeats the excerpt."""
    value = source["quote"]
    if not isinstance(value, str):
        raise ContentError(f"{label}.quote: must be a string")
    if not 1 <= len(value) <= QUOTE_MAX or not value.strip():
        raise ContentError(f"{label}.quote: must be 1 to {QUOTE_MAX} characters with at least one that is not a space")
    if any(unicodedata.category(char) in ("Cc", "Cf") for char in value):
        raise ContentError(f"{label}.quote: must be plain text without control characters or line breaks")
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
             "font_family", "not_legal_advice", "brand", "privacy_contact", "share_image", "license", "comment_asks", "press"}
# DF2207-10: the optional reporter page. contact_role is required; the address defaults to site.json.contact_email.
PRESS_KEYS = {"contact_role", "contact_email", "about_md"}
MAX_PRESS_ROLE, MAX_PRESS_ABOUT = 80, 1500
# DF1422-13: the open-data licences an organizer may pick: id -> (name, deed URL). Nothing else is accepted.
LICENSES = {
    "CC-BY-4.0": ("Creative Commons Attribution 4.0 International (CC BY 4.0)", "https://creativecommons.org/licenses/by/4.0/"),
    "CC-BY-SA-4.0": ("Creative Commons Attribution-ShareAlike 4.0 International (CC BY-SA 4.0)",
                     "https://creativecommons.org/licenses/by-sa/4.0/"),
    "CC0-1.0": ("CC0 1.0 Universal (Public Domain Dedication)", "https://creativecommons.org/publicdomain/zero/1.0/"),
}
LICENSE_KEYS = {"id", "attribution"}
MAX_LICENSE_ATTRIBUTION = 200
# Camera points come from OpenStreetMap, so they carry ODbL whatever licence the campaign picks (a fixed fact, not a choice).
ODBL = {"id": "ODbL-1.0", "name": "Open Database License (ODbL) 1.0", "url": "https://opendatacommons.org/licenses/odbl/1-0/",
        "attribution": "\u00a9 OpenStreetMap contributors", "attribution_url": "https://www.openstreetmap.org/copyright"}
MAX_COMMENT_ASKS, MAX_ASK_CHARS = 3, 200
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


def load_license(value):
    """site.json.license: ``{"id": <LICENSES key>, "attribution": <optional plain text, one line, 1-200 characters>}``."""
    label = "site.json.license"
    _require_keys(value, LICENSE_KEYS, {"id"}, label)
    if not isinstance(value["id"], str) or value["id"] not in LICENSES:
        raise ContentError(f"{label}.id: choose one of {list(LICENSES)}")
    name, url = LICENSES[value["id"]]
    found = {"id": value["id"], "name": name, "url": url}
    if "attribution" in value:
        text = value["attribution"]
        if (not isinstance(text, str) or not plain_text(text) or not text.strip() or len(text) > MAX_LICENSE_ATTRIBUTION
                or any(ord(char) < 32 or ord(char) == 127 for char in text) or CONTACT_LIKE.search(text)):
            raise ContentError(f"{label}.attribution: must be plain text on one line of 1-{MAX_LICENSE_ATTRIBUTION} characters, "
                               "without angle brackets, an address or a phone-like number")
        found["attribution"] = text
    return found


def load_press(value, contact_email):
    """DF2207-10 site.json.press: ``{"contact_role", optional "contact_email", optional "about_md"}``.

    ``contact_email`` is the validated site.json.contact_email (or None); it is the address when press names none.
    No field is trimmed or coerced; the page escapes the text. A refusal names the field, never the text."""
    label = "site.json.press"
    _require_keys(value, PRESS_KEYS, {"contact_role"}, label)
    role = value["contact_role"]
    if (not isinstance(role, str) or not role.strip() or len(role) > MAX_PRESS_ROLE
            or any(unicodedata.category(char) == "Cc" or char in "\u2028\u2029" for char in role)):
        raise ContentError(f"{label}.contact_role: required, 1-{MAX_PRESS_ROLE} characters on one line, no control characters")
    if "contact_email" in value:
        address = value["contact_email"]
        if not isinstance(address, str) or not address:
            raise ContentError(f"{label}.contact_email: must be a non-empty email address")
        address = _email(address, f"{label}.contact_email")
    elif contact_email:
        address = contact_email
    else:
        raise ContentError(f"{label}.contact_email: required when site.json.contact_email is not set")
    about = value.get("about_md", "")
    if not plain_text(about) or len(about) > MAX_PRESS_ABOUT:
        raise ContentError(f"{label}.about_md: must be plain text without angle brackets, at most {MAX_PRESS_ABOUT} characters")
    return {"contact_role": role, "contact_email": address, "about_md": about}


def load_comment_asks(value, label):
    """DF2207-07: the campaign's own public-comment asks, 1-3 plain-text strings of 1-200 characters.

    Checked on the raw text; nothing is coerced, trimmed or dropped. A refusal names the one-based position, never the text.
    Control and format characters (newline, tab, DEL, bidi, zero-width) and U+2028/U+2029 are refused as well as angle brackets."""
    if not isinstance(value, list) or not value:
        raise ContentError(f"{label}: must be a list of 1 to {MAX_COMMENT_ASKS} asks")
    if len(value) > MAX_COMMENT_ASKS:
        raise ContentError(f"{label}[{MAX_COMMENT_ASKS + 1}]: at most {MAX_COMMENT_ASKS} asks")
    for index, ask in enumerate(value, 1):
        where = f"{label}[{index}]"
        if not isinstance(ask, str) or not ask.strip():
            raise ContentError(f"{where}: must be a non-empty string")
        if len(ask) > MAX_ASK_CHARS:
            raise ContentError(f"{where}: at most {MAX_ASK_CHARS} characters")
        if (not plain_text(ask) or "\u2028" in ask or "\u2029" in ask
                or any(unicodedata.category(char) in ("Cc", "Cf") for char in ask)):
            raise ContentError(f"{where}: must be plain text on one line without angle brackets or control characters")
    return list(value)


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
        **({"comment_asks": load_comment_asks(document["comment_asks"], "site.json.comment_asks")}
           if "comment_asks" in document else {}),
        **({"brand": load_brand(document["brand"], assets_dir)} if "brand" in document else {}),
        **({"share_image": load_share_image(document["share_image"], assets_dir)} if "share_image" in document else {}),
        **({"license": load_license(document["license"])} if "license" in document else {}),
        **({"press": load_press(document["press"], _email(document.get("contact_email"), "site.json.contact_email"))}
           if "press" in document else {}),
    }


# --- findings --------------------------------------------------------------

FINDING_KEYS = {"id", "title", "slug", "summary", "body_md", "classification", "confidence",
                "event_date", "sources", "published_at", "updated_at", "corrections", "state",
                "author", "limitations", "counterevidence", "agency_id", "rule_version",
                "duty", "exceptions", "rule_ids", "also_agency_ids", "agency_response"}
FINDING_REQUIRED = {"id", "title", "slug", "summary", "classification", "confidence",
                    "event_date", "sources", "published_at", "corrections", "state",
                    "author", "limitations", "counterevidence"}
SOURCE_KEYS = {"sha256", "locator", "title", "rule_id", "page", "quote"}
QUOTE_MAX = 400  # DF1952-08: code points of an optional transcribed excerpt
CORRECTION_KEYS = {"date", "note"}

# DF1103-08: the agency's right of reply. "pending" means a request for comment was PREPARED, not that anyone was asked.
REPLY_STATUSES = ("received", "declined", "no_response")
REPLY_BASE_KEYS = frozenset(("status", "prepared_on", "reply_by"))
REPLY_FINAL_KEYS = REPLY_BASE_KEYS | {"asked_on", "on"}
REPLY_OPTIONAL = {"received": {"text", "record_sha256"}, "declined": {"text"}, "no_response": set()}
MAX_REPLY_TEXT = 2000
_REPLY_DAY, _FULL_HEX = re.compile(r"\d{4}-\d{2}-\d{2}"), re.compile(r"[0-9a-fA-F]{40,}")


def reply_day(value):
    """A real padded YYYY-MM-DD as a date, else None."""
    try:
        return date.fromisoformat(value) if isinstance(value, str) and _REPLY_DAY.fullmatch(value) else None
    except ValueError:
        return None


def _agency_response(value, where, cited=None, today=None):
    """Validate one agency_response and return a normalized copy. Every refusal names ``where.<field>`` with a fixed
    reason and never quotes the supplied text or an unknown key. ``cited``: the finding's source sha256 values."""
    if not isinstance(value, dict):
        raise ContentError(f"{where}: must be an object")
    status = value.get("status")
    if status != "pending" and status not in REPLY_STATUSES:
        raise ContentError(f"{where}.status: must be pending, received, declined or no_response")
    final = status != "pending"
    base = REPLY_FINAL_KEYS if final else REPLY_BASE_KEYS
    allowed = base | (REPLY_OPTIONAL[status] if final else set())
    for key in sorted(value, key=str):
        if key not in REPLY_FINAL_KEYS | REPLY_OPTIONAL["received"]:
            raise ContentError(f"{where}: unknown field")
        if key not in allowed:
            raise ContentError(f"{where}.{key}: not allowed for status {status}")
    for key in sorted(base | ({"text"} if status == "received" else set())):
        if key not in value:
            raise ContentError(f"{where}.{key}: required for status {status}")
    days = {key: reply_day(value[key]) for key in base - {"status"}}
    for key, day in days.items():
        if day is None:
            raise ContentError(f"{where}.{key}: must be a real YYYY-MM-DD date")
    today = today or date.today()
    if days["prepared_on"] > today:
        raise ContentError(f"{where}.prepared_on: must not be in the future")
    if days["prepared_on"] > days["reply_by"]:
        raise ContentError(f"{where}.reply_by: must not be before prepared_on")
    if final:
        asked = days["asked_on"]
        if not days["prepared_on"] <= asked <= days["reply_by"]:
            raise ContentError(f"{where}.asked_on: must be between prepared_on and reply_by")
        if not asked <= days["on"] <= today:
            raise ContentError(f"{where}.on: must be on or after asked_on and not in the future")
        if status == "no_response" and days["on"] < days["reply_by"]:
            raise ContentError(f"{where}.on: no_response needs a date on or after reply_by")
    if "text" in value:
        text = value["text"]
        if (not isinstance(text, str) or not 1 <= len(text) <= MAX_REPLY_TEXT or not text.strip()
                or any(ch not in "\n\t" and unicodedata.category(ch) in ("Cc", "Cf", "Cs") for ch in text)):
            raise ContentError(f"{where}.text: must be 1-{MAX_REPLY_TEXT} characters of non-blank text "
                               "without control or format characters")
        if _FULL_HEX.search(text):
            raise ContentError(f"{where}.text: must not hold a full hash (40 or more hex characters)")
    if "record_sha256" in value:
        sha = value["record_sha256"]
        if not isinstance(sha, str) or not SHA256.fullmatch(sha):
            raise ContentError(f"{where}.record_sha256: must be 64 lowercase hex characters")
        if cited is not None and sha not in cited:
            raise ContentError(f"{where}.record_sha256: must be one of the finding's cited sources")
    return {key: value[key] for key in sorted(value)}


MAX_ALSO_AGENCIES = 10


def _also_agency_ids(document, agency_id, label):
    """DF1952-07: the optional ``also_agency_ids``: 1-10 distinct agency ids besides the primary ``agency_id``, in the
    order entered. None when the key is absent; every refusal names the field and the index."""
    if "also_agency_ids" not in document:
        return None
    field, value = f"{label}.also_agency_ids", document["also_agency_ids"]
    if not agency_id:
        raise ContentError(f"{field}: needs a non-empty primary agency_id")
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_ALSO_AGENCIES:
        raise ContentError(f"{field}: must be a list of 1 to {MAX_ALSO_AGENCIES} agency identifiers")
    seen = set()
    for index, item in enumerate(value):
        if not isinstance(item, str) or not AGENCY_ID.fullmatch(item):
            raise ContentError(f"{field}[{index}]: invalid agency identifier")
        if item == agency_id:
            raise ContentError(f"{field}[{index}]: {item!r} is already the primary agency_id")
        if item in seen:
            raise ContentError(f"{field}[{index}]: duplicate agency identifier {item!r}")
        seen.add(item)
    return list(value)


def finding_agency_ids(finding):
    """Every agency a loaded finding is about, primary first then the extras as entered; a legacy finding with no
    primary agency has none. One helper for every public surface (pages, feeds, counts, filter, related)."""
    ids = [finding["agency_id"]] if finding.get("agency_id") else []
    return ids + [item for item in finding.get("also_agency_ids") or () if item not in ids]


def check_finding_agencies(finding, known_agency_ids, label):
    """A finding that uses ``also_agency_ids`` may only name known agencies, primary included; a legacy primary-only
    finding is never rejected here. Raises ContentError naming the field and index."""
    if finding.get("also_agency_ids") is None:
        return finding
    known = set(known_agency_ids)
    if finding["agency_id"] not in known:
        raise ContentError(f"{label}.agency_id: unknown agency {finding['agency_id']!r}")
    for index, item in enumerate(finding["also_agency_ids"]):
        if item not in known:
            raise ContentError(f"{label}.also_agency_ids[{index}]: unknown agency {item!r}")
    return finding


def load_finding(document, label, *, known_agency_ids=None, today=None):
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
    if not isinstance(finding_id, str) or not FINDING_ID.fullmatch(finding_id):
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
        if "quote" in source:  # absent stays absent: no default or null key, so a quote-free finding keeps its exact shape
            sources[-1]["quote"] = _quote(source, f"{label}.sources[{index}]")
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
    also_agency_ids = _also_agency_ids(document, agency_id, label)
    rule_ids = _text_list(document, "rule_ids", label)
    if any(not item.strip() or len(item) > 120 for item in rule_ids):
        raise ContentError(f"{label}.rule_ids: each rule id must be a non-empty string of at most 120 characters")
    if len(set(rule_ids)) != len(rule_ids):
        raise ContentError(f"{label}.rule_ids: duplicate rule id")
    response = ({"agency_response": _agency_response(document["agency_response"], f"{label}.agency_response",
                                                      {s["sha256"] for s in sources}, today)}
                if "agency_response" in document else {})
    loaded = {
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
    if also_agency_ids is not None:  # absent stays absent: a primary-only finding keeps its exact shape
        loaded["also_agency_ids"] = also_agency_ids
    loaded.update(response)  # absent stays absent, like also_agency_ids: a finding with no reply keeps its exact shape
    if known_agency_ids is not None:
        check_finding_agencies(loaded, known_agency_ids, label)
    return loaded


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

FINDING_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")  # public finding ids; distinct from a slug
MEETING_KEYS = {"id", "body", "starts_at", "agenda_url", "agenda_item", "comment_kit_md", "location", "agency_id",
                "status", "participation", "outcome", "cited_findings", "cosigners"}
MEETING_STATUSES = ("scheduled", "cancelled")
OUTCOME_RESULTS = ("approved", "denied", "continued", "amended", "no_action", "informational")
CONTINUATION_KEYS = ("continued_to", "continued_to_date")  # DF0022-07: where the official source says an item went
DIRECTION_KEYS = ("direction", "return_by", "direction_source")  # DF0022-08: a staff direction the source states, as a trio
COMMENT_KEYS = ("spoke", "written", "comment_source_url")  # DF0022-09: public-comment totals the official record states
OUTCOME_KEYS = {"result", "on", "source_url", "verified_by", "verified_at", "vote", "votes", "recording_url",
                "recording_start", "minutes_url", "minutes_approved_on", *CONTINUATION_KEYS, *DIRECTION_KEYS,
                *COMMENT_KEYS}
RECORDING_START = re.compile(r"([0-9]{1,2}):([0-5][0-9]):([0-5][0-9])")
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
    _require_keys(value, OUTCOME_KEYS, OUTCOME_KEYS - {"vote", "votes", "recording_url", "recording_start",
                                                       "minutes_url", "minutes_approved_on", *CONTINUATION_KEYS,
                                                       *DIRECTION_KEYS, *COMMENT_KEYS}, label)
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
    recording, start = value.get("recording_url"), value.get("recording_start")  # DF1422-10: the body's own recording
    if "recording_url" in value:
        if isinstance(recording, str) and len(recording) > 500:
            raise ContentError(f"{label}.recording_url: at most 500 characters")
        _https(recording, f"{label}.recording_url")
    if "recording_start" in value:
        match = RECORDING_START.fullmatch(start) if isinstance(start, str) else None
        if "recording_url" not in value:
            raise ContentError(f"{label}.recording_start: needs recording_url")
        if not match or (int(match[1]), int(match[2]), int(match[3])) > (12, 0, 0):
            raise ContentError(f"{label}.recording_start: use H:MM:SS, at most 12:00:00")
    minutes = _load_minutes(value, held, now, label)  # DF2207-05: the body's approved minutes, always as a pair
    continued = _load_continuation(value, held, label)  # DF0022-07: where a continued item went, as the source states
    direction = _load_direction(value, held, label)  # DF0022-08: what the body told its staff, as the source states
    comments = load_comment_counts(value, label)  # DF0022-09: how many people spoke or wrote, as the record states
    return {"result": value["result"], "on": value["on"], "source_url": _https(value["source_url"], f"{label}.source_url"),
            "verified_by": value["verified_by"], "verified_at": value["verified_at"],
            **({"vote": value["vote"]} if "vote" in value else {}),
            **({"votes": [{"name": v["name"], "vote": v["vote"]} for v in votes]} if "votes" in value else {}),
            **({"recording_url": recording} if "recording_url" in value else {}),
            **({"recording_start": start} if "recording_start" in value else {}), **minutes, **continued, **direction,
            **comments}


def load_comment_counts(value, label):
    """DF0022-09: ``{spoke?, written?, comment_source_url}`` or ``{}``. Each count is a whole number (``type is int``,
    never a bool, float, string or null) from 0 to 999 exactly as the official record states it, and a count never
    appears without the https record that states it. Zero is a stated count; an absent count is simply not stated."""
    present = [key for key in ("spoke", "written") if key in value]
    if not present:
        if "comment_source_url" in value:
            raise ContentError(f"{label}.comment_source_url: needs spoke or written")
        return {}
    for key in present:
        if type(value[key]) is not int or not 0 <= value[key] <= 999:
            raise ContentError(f"{label}.{key}: must be a whole number from 0 to 999")
    if "comment_source_url" not in value:
        raise ContentError(f"{label}.comment_source_url: required with spoke or written")
    source = value["comment_source_url"]
    if isinstance(source, str) and len(source) > 500:
        raise ContentError(f"{label}.comment_source_url: at most 500 characters")
    _https(source, f"{label}.comment_source_url")
    return {**{key: value[key] for key in present}, "comment_source_url": source}


def direction_text_ok(value):
    """DF0022-08: a literal one-line plain-text string of 1 to 200 characters with no edge whitespace."""
    return (isinstance(value, str) and 1 <= len(value) <= 200 and plain_text(value) and value == value.strip()
            and not any(char in value for char in "\r\n\t"))


def _load_direction(value, held, label):
    """``{direction, return_by, direction_source}`` or ``{}``. All three or none; a null, an empty or a partial group is
    refused by field name. ``return_by`` is the date the official source states (a real ASCII calendar day on or after the
    meeting's own date; it may be long past or far ahead), never a legal deadline."""
    present = [key for key in DIRECTION_KEYS if key in value]
    if not present:
        return {}
    for key in DIRECTION_KEYS:
        if key not in value:
            raise ContentError(f"{label}.{key}: needed together with {present[0]}")
    text, day, source = (value[key] for key in DIRECTION_KEYS)
    if not direction_text_ok(text):
        raise ContentError(f"{label}.direction: must be 1 to 200 characters of plain text on one line")
    try:
        real = isinstance(day, str) and day.isascii() and _calendar_day(day, label) is not None
    except ContentError:
        real = False
    if not real or day < held:
        raise ContentError(f"{label}.return_by: must be a YYYY-MM-DD date on or after the meeting")
    if isinstance(source, str) and len(source) > 500:
        raise ContentError(f"{label}.direction_source: at most 500 characters")
    _https(source, f"{label}.direction_source")
    return {"direction": text, "return_by": day, "direction_source": source}


def _load_continuation(value, held, label):
    """``{continued_to: ID}``, ``{continued_to_date: D}`` or ``{}``. Only a continued result carries either, never both;
    the date is a real ASCII calendar day strictly after the meeting's own date (never compared with ``on``, no ceiling)."""
    present = [key for key in CONTINUATION_KEYS if key in value]
    if len(present) == 2:
        raise ContentError(f"{label}.continued_to: continued_to and continued_to_date are mutually exclusive")
    if not present:
        return {}
    key = present[0]
    if value["result"] != "continued":
        raise ContentError(f"{label}.{key}: requires result continued")
    if key == "continued_to":
        return {key: _slug(value[key], f"{label}.continued_to")}
    day = value[key]
    try:
        real = isinstance(day, str) and day.isascii() and _calendar_day(day, label) is not None
    except ContentError:
        real = False
    if not real or day <= held:
        raise ContentError(f"{label}.continued_to_date: must be a YYYY-MM-DD date after the meeting")
    return {key: day}


def _meeting_day(meeting):
    return meeting["starts_at"][:10]  # the meeting's own local calendar date; no time zone is converted


def _target_problem(source, target):
    """Why ``target`` cannot be where ``source``'s item was continued to, or None."""
    if target is None:
        return "unknown meeting ID"
    if target["status"] == "cancelled":
        return "target is cancelled"
    if target["body"] != source["body"]:
        return "target must have the same body"
    if _meeting_day(target) <= _meeting_day(source):
        return "target must be dated after the meeting"
    return None


def continuation_targets(meeting, meetings):
    """DF0022-07: the staged meetings ``meeting``'s continued outcome points at, sorted by start then id. ``meetings`` are
    loaded meetings whose ``id`` is set (the explicit id or the filename stem). An explicit id gives its one valid
    target; a stated date gives every non-cancelled meeting of the exact same body on that exact local date (none is
    valid: not staged yet). A match is a follow-up cue, never evidence the item was heard."""
    outcome = meeting.get("outcome") or {}
    if "continued_to" in outcome:
        found = [m for m in meetings if m["id"] == outcome["continued_to"]]
        return found if len(found) == 1 and _target_problem(meeting, found[0]) is None else []
    if "continued_to_date" not in outcome:
        return []
    return sorted((m for m in meetings if m["body"] == meeting["body"] and m["status"] != "cancelled"
                   and _meeting_day(m) == outcome["continued_to_date"]), key=lambda m: (m["starts_at"], m["id"]))


def continuation_back_links(meetings):
    """DF0022-07: ``{target id: [source meetings]}``, each list sorted by the source's start then id. One pass over the
    sources that carry a continuation; a chain shows each link once and nothing is followed recursively."""
    back = {}
    for source in sorted(meetings, key=lambda m: (m["starts_at"], m["id"])):
        for target in continuation_targets(source, meetings):
            if target["id"] != source["id"]:
                back.setdefault(target["id"], []).append(source)
    return back


def continuation_issues(meetings):
    """``[(source meeting, message)]`` for each explicit ``continued_to`` that does not name one valid later meeting."""
    issues = []
    for meeting in meetings:
        outcome = meeting.get("outcome") or {}
        if "continued_to" not in outcome:
            continue
        found = [m for m in meetings if m["id"] == outcome["continued_to"]]
        problem = ("duplicate meeting ID" if len(found) > 1 else _target_problem(meeting, found[0] if found else None))
        if problem:
            issues.append((meeting, problem))
    return issues


def continuation_problems(meetings):
    """DF0022-07: one ``meetings/<source-id>.outcome.continued_to: <why>`` line per broken explicit reference."""
    return [f"meetings/{meeting['id']}.outcome.continued_to: {problem}" for meeting, problem in continuation_issues(meetings)]


def _load_minutes(value, held, now, label):
    """``{minutes_url, minutes_approved_on}`` or ``{}``; one without the other, or a null, is refused by field name."""
    present = [key for key in ("minutes_url", "minutes_approved_on") if key in value]
    if not present:
        return {}
    for key in ("minutes_url", "minutes_approved_on"):
        if key not in value:
            raise ContentError(f"{label}.{key}: needed together with {present[0]}")
    url, day = value["minutes_url"], value["minutes_approved_on"]
    if isinstance(url, str) and len(url) > 500:
        raise ContentError(f"{label}.minutes_url: at most 500 characters")
    _https(url, f"{label}.minutes_url")
    if (not isinstance(day, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", day)
            or not held <= _timestamp(day, f"{label}.minutes_approved_on", date_only=True) <= now):
        raise ContentError(f"{label}.minutes_approved_on: must be a YYYY-MM-DD date from {held} to {now}")
    return {"minutes_url": url, "minutes_approved_on": day}


def load_cited_findings(value, label):
    """DF2207-06: the finding ids a staged comment kit cites: a list of 1 to 3 unique ids, order kept as written."""
    if not isinstance(value, list) or not 1 <= len(value) <= 3:
        raise ContentError(f"{label}: must be a list of 1 to 3 finding ids")
    for index, finding_id in enumerate(value):
        if not isinstance(finding_id, str) or not FINDING_ID.fullmatch(finding_id):
            raise ContentError(f"{label}[{index}]: use letters, digits, underscore or hyphen (1 to 64 characters)")
        if finding_id in value[:index]:
            raise ContentError(f"{label}[{index}]: {finding_id!r} is listed more than once")
    return list(value)


def load_cosigners(value, label):
    """DF2207-14: supporter ids named under a staged written comment: 1 to 30 unique canonical ids, order kept."""
    if not isinstance(value, list) or not 1 <= len(value) <= 30:
        raise ContentError(f"{label}: must be a list of 1 to 30 supporter ids")
    for index, supporter_id in enumerate(value):
        if not isinstance(supporter_id, str) or not SLUG.fullmatch(supporter_id):
            raise ContentError(f"{label}[{index}]: not a canonical supporter id")
        if supporter_id in value[:index]:
            raise ContentError(f"{label}[{index}]: {supporter_id!r} is listed more than once")
    return list(value)


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
        **({"cited_findings": load_cited_findings(document["cited_findings"], f"{label}.cited_findings")}
           if "cited_findings" in document else {}),
        **({"cosigners": load_cosigners(document["cosigners"], f"{label}.cosigners")}
           if "cosigners" in document else {}),
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
ASKED_KEYS, ASKED_TEXT = ("topic", "asked_on", "state", "updated_on", "note", "request"), {"topic": 120, "note": 200}
ASKED_REQUEST = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")  # DF1207-03: kit_track.REQUEST; private link to kit/sent.json, never rendered
CONTRACT_KEYS = {"vendor", "start", "end", "renewal_note", "source_url", "checked_on", "cost"}
COST_KEYS, COST_PERIODS = ("amount_usd", "period", "as_stated_in"), ("annual", "total", "one_time")
COST_AMOUNT, COST_URL = re.compile(r"(0|[1-9][0-9]*)([.][0-9]{1,2})?"), re.compile(r"(?i)[a-z][a-z0-9+.-]*://|www[.]")
FLOCK_SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+){0,7}")
VERIFIED_FIELDS = ("records_email", "records_url", "portal")
KIT_POSTAL_KEYS = {"mailing_address", "mailing_verification"}  # private kit/agencies.json only; never published


def _private_postal(entry, item, today=None):
    """Validate the kit-only postal proof (both keys or neither); the public model never carries it."""
    from . import kit
    if ("mailing_address" in entry) != ("mailing_verification" in entry):
        raise ContentError(f"{item}: mailing_address and mailing_verification go together")
    if "mailing_address" not in entry:
        return
    try:
        kit.postal_proof({**entry, "verified": True}, today)
    except ValueError as exc:
        raise ContentError(f"{item}: {exc}") from None


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


def _contract_cost(value, label):
    """``profile.contract.cost``: an organizer-transcribed amount exactly as a document states it; never computed."""
    if not isinstance(value, dict) or set(value) != set(COST_KEYS):
        raise ContentError(f"{label}: needs only amount_usd, period and as_stated_in")
    amount, period, title = value["amount_usd"], value["period"], value["as_stated_in"]
    if not (isinstance(amount, str) and COST_AMOUNT.fullmatch(amount) and Decimal(amount) <= 100000000):
        raise ContentError(f"{label}.amount_usd: decimal string from 0 to 100000000 with at most 2 decimals")
    if not (isinstance(period, str) and period in COST_PERIODS):
        raise ContentError(f"{label}.period: choose annual, total or one_time")
    if not (isinstance(title, str) and 1 <= len(title) <= 120 and title == title.strip() and plain_text(title)
            and not COST_URL.match(title) and not any(unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp") for c in title)):
        raise ContentError(f"{label}.as_stated_in: plain document title of 1 to 120 characters")
    return {"amount_usd": amount, "period": period, "as_stated_in": title}


def _contract(value, label):
    """An organizer-checked contract term (``profile.contract``) from its official source; never fetched."""
    if value is None:
        return None
    if isinstance(value, dict) and not {"source_url", "checked_on"} <= set(value):
        raise ContentError(f"{label}: needs source_url and checked_on")
    _require_keys(value, CONTRACT_KEYS, CONTRACT_KEYS - {"renewal_note", "cost"}, label)
    vendor, note = _text(value, "vendor", label, allow_empty=False), _text(value, "renewal_note", label)
    for key, text, limit in (("vendor", vendor, 80), ("renewal_note", note, 200)):
        if len(text) > limit:
            raise ContentError(f"{label}.{key}: at most {limit} characters")
    start, end = _calendar_day(value["start"], f"{label}.start"), _calendar_day(value["end"], f"{label}.end")
    if end < start:
        raise ContentError(f"{label}.end: must not be before start")
    if _calendar_day(value["checked_on"], f"{label}.checked_on") > date.today():
        raise ContentError(f"{label}.checked_on: must not be after the build date")
    contract = {"vendor": vendor, "start": value["start"], "end": value["end"], "renewal_note": note,
                "source_url": _https(value["source_url"], f"{label}.source_url"), "checked_on": value["checked_on"]}
    if "cost" in value:
        contract["cost"] = _contract_cost(value["cost"], f"{label}.cost")
    return contract


FUNDING_KINDS = ("grant", "general_fund", "asset_forfeiture", "donation", "other")
FUNDING_REQUIRED = {"kind", "name", "as_stated_in", "source_url", "checked_on"}
FUNDING_KEYS = FUNDING_REQUIRED | {"amount_usd", "period", "fiscal_year"}
FUNDING_YEAR = re.compile(r"[1-9][0-9]{3}")
FUNDING_DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def _funding(value, label, today=None):
    """``profile.funding`` (DF2207-02): how an agency says it is paid for, as an organizer transcribed it from a document.

    Entries keep their input order and are never summed, sorted or merged. An error names the entry and the rule it
    broke, never the value submitted (a donor name, an unknown key). Nothing is fetched.
    """
    if not isinstance(value, list) or not 1 <= len(value) <= 10:
        raise ContentError(f"{label}: must be a list of 1 to 10 funding entries")
    build_day, result = today or date.today(), []
    for index, entry in enumerate(value):
        item = f"{label}[{index}]"
        if not isinstance(entry, dict):
            raise ContentError(f"{item}: expected an object")
        if not FUNDING_REQUIRED <= set(entry) or set(entry) - FUNDING_KEYS:
            raise ContentError(f"{item}: needs kind, name, as_stated_in, source_url and checked_on, and may add only "
                               "amount_usd with period, and fiscal_year")
        if entry["kind"] not in FUNDING_KINDS:
            raise ContentError(f"{item}.kind: choose one of {list(FUNDING_KINDS)}")
        name = entry["name"]
        if not (isinstance(name, str) and 1 <= len(name) <= 120 and name == name.strip() and plain_text(name)
                and not any(unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp") for c in name)):
            raise ContentError(f"{item}.name: plain program or donor organization name of 1 to 120 characters")
        if ("amount_usd" in entry) != ("period" in entry):
            raise ContentError(f"{item}: amount_usd and period go together")
        # DF1103-07's own validator checks the amount, period and document title (a placeholder pair when none is given)
        cost = _contract_cost({"amount_usd": entry.get("amount_usd", "0"), "period": entry.get("period", "annual"),
                               "as_stated_in": entry["as_stated_in"]}, item)
        year = entry.get("fiscal_year")
        if "fiscal_year" in entry and not (isinstance(year, str) and FUNDING_YEAR.fullmatch(year)):
            raise ContentError(f"{item}.fiscal_year: four digits as the document states it, e.g. 2026")
        checked = entry["checked_on"]
        try:
            if not (isinstance(checked, str) and FUNDING_DAY.fullmatch(checked)):
                raise ValueError
            day = date.fromisoformat(checked)
        except ValueError:
            raise ContentError(f"{item}.checked_on: must be a YYYY-MM-DD date not after the build date") from None
        if day > build_day:
            raise ContentError(f"{item}.checked_on: must be a YYYY-MM-DD date not after the build date")
        clean = {"kind": entry["kind"], "name": name, "as_stated_in": cost["as_stated_in"],
                 "source_url": _https(entry["source_url"], f"{item}.source_url"), "checked_on": checked}
        if "amount_usd" in entry:
            clean.update(amount_usd=cost["amount_usd"], period=cost["period"])
        if "fiscal_year" in entry:
            clean["fiscal_year"] = year
        result.append(clean)
    return result


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


POLICY_CHECK_KEYS = {"checked_on", "policy_url", "elements"}
POLICY_STATUSES = ("addressed", "not_found", "unclear")
CHECKLIST_JURISDICTION = "us-ca"  # the only checklist resource; see jurisdictions/us-ca/policy-checklist.json


def _policy_check(value, label, alpr_policy, today=None):
    """DF1207-06: an organizer's reading of a posted policy (``profile.policy_check``) against the draft checklist.

    Every problem is named by field in ONE refusal (``; `` separated), so build and content-check list them all.
    Never fetched; no score or judgement.
    Returns ``{"checked_on", "policy_url", "elements": [{"id", "label", "status", "page"?}]}`` in checklist order."""
    if value is None:
        return None
    issues, elements = [], []
    alpr, shaped = alpr_policy if isinstance(alpr_policy, dict) else None, isinstance(value, dict)
    if not shaped:
        issues.append(f"{label}: must be an object")
        value = {}
    issues += [f"{label}: unknown field {str(key)[:40]!r}" for key in sorted(set(value) - POLICY_CHECK_KEYS, key=str)]
    issues += [f"{label}.{key}: required" for key in ("checked_on", "policy_url", "elements") if shaped and key not in value]
    if "policy_url" in value:
        if alpr is None:
            issues.append(f"{label}.policy_url: needs profile.alpr_policy for the same agency")
        elif value["policy_url"] != alpr.get("url"):
            issues.append(f"{label}.policy_url: must equal profile.alpr_policy.url")
    if "checked_on" in value:
        try:
            checked, posted = _calendar_day(value["checked_on"], f"{label}.checked_on"), None
            try:
                posted = _calendar_day(alpr.get("checked_on"), "alpr_policy.checked_on") if alpr else None
            except ContentError:
                pass  # a bad alpr_policy.checked_on is reported on its own field
            if checked > (today or date.today()):
                issues.append(f"{label}.checked_on: must not be after the build date")
            elif posted and checked < posted:
                issues.append(f"{label}.checked_on: must not be before profile.alpr_policy.checked_on")
        except ContentError as exc:
            issues.append(str(exc))
    if "elements" in value:
        try:
            checklist = law.load_checklist(CHECKLIST_JURISDICTION)["elements"]
        except ValueError as exc:
            checklist = []
            issues.append(f"{label}.elements: the policy checklist could not be loaded ({exc})")
        given = value["elements"]
        if not isinstance(given, dict):
            issues.append(f"{label}.elements: must be an object keyed by element id")
            given = {}
        known = {element["id"] for element in checklist}
        issues += [f"{label}.elements.{str(key)[:40]}: unknown element" for key in sorted(set(given) - known, key=str)]
        for element in checklist:
            where, entry = f"{label}.elements.{element['id']}", given.get(element["id"])
            if element["id"] not in given:
                issues.append(f"{where}: missing")
                continue
            if not isinstance(entry, dict):
                issues.append(f"{where}: must be an object with status and optional page")
                continue
            issues += [f"{where}: unknown field {str(key)[:40]!r}" for key in sorted(set(entry) - {"status", "page"}, key=str)]
            status, bad = entry.get("status"), len(issues)
            if status not in POLICY_STATUSES:
                issues.append(f"{where}.status: choose one of {list(POLICY_STATUSES)}")
            page = entry.get("page")
            if "page" in entry:
                if isinstance(page, bool) or not isinstance(page, int) or not 1 <= page <= 9999:
                    issues.append(f"{where}.page: must be a whole number from 1 to 9999")
                elif status != "addressed":
                    issues.append(f"{where}.page: only allowed when status is addressed")
            if len(issues) == bad:
                elements.append({"id": element["id"], "label": element["label"], "status": status,
                                 **({"page": page} if "page" in entry else {})})
    if issues:
        raise ContentError("; ".join(issues))
    return {"checked_on": value["checked_on"], "policy_url": value["policy_url"], "elements": elements}


PRE_MEETING_KEYS = ("status", "checked_on", "meeting_on", "body", "source_url", "note")
PRE_MEETING_STATUSES = ("found", "not_found_yet")
PRE_MEETING_RULE_ID = "ca-civ-1798.90.55-public-comment-before-implementation"  # the law-package rule the section cites by id


def _pre_meeting(value, label, today=None, problems=None):
    """DF1207-07: the public meeting an organizer found before an ALPR program started (``profile.pre_implementation_meeting``).

    ``found`` needs ``meeting_on``, ``body`` and an https ``source_url``; ``not_found_yet`` allows none of them.
    Organizer-verified and never fetched. Every problem is named by field: with ``problems`` a list they are all
    appended (content-check), else the first is raised (build). Returns the clean dict, or None when absent."""
    if value is None:
        return None
    issues, today = [], today or date.today()
    if not isinstance(value, dict):
        issues, value = [f"{label}: must be an object"], {}
    issues += [f"{label}: unknown field {str(key)[:40]!r}" for key in sorted(set(value) - set(PRE_MEETING_KEYS), key=str)]
    status = value.get("status")
    if status not in PRE_MEETING_STATUSES:
        issues.append(f"{label}.status: choose one of {list(PRE_MEETING_STATUSES)}")
    found = status == "found"
    for key in ("meeting_on", "body", "source_url"):
        if key in value and status == "not_found_yet":
            issues.append(f"{label}.{key}: not allowed when status is not_found_yet")
        elif key not in value and found:
            issues.append(f"{label}.{key}: required when status is found")
    days = {}
    for key in ("meeting_on", "checked_on"):
        if key not in value:
            if key == "checked_on":
                issues.append(f"{label}.checked_on: required")
        elif key == "checked_on" or found:
            try:
                days[key] = _calendar_day(value[key], f"{label}.{key}")
                if days[key] > today:
                    issues.append(f"{label}.{key}: must not be after the build date")
            except ContentError as exc:
                issues.append(str(exc))
    if "meeting_on" in days and "checked_on" in days and days["checked_on"] < days["meeting_on"]:
        issues.append(f"{label}.checked_on: must not be before meeting_on")
    if found and "body" in value:
        body = value["body"]  # the page escapes it, so angle brackets are fine here
        if not (isinstance(body, str) and body.strip() and len(body) <= 120 and all(map(visible, body))):
            issues.append(f"{label}.body: 1-120 characters of visible text")
    if found and "source_url" in value:
        try:
            _https(value["source_url"], f"{label}.source_url")
        except ContentError as exc:
            issues.append(str(exc))
    if "note" in value:
        try:
            if len(str(_text(value, "note", label))) > 200:
                raise ContentError(f"{label}.note: at most 200 characters")
        except ContentError as exc:
            issues.append(str(exc))
    if problems is not None:
        problems.extend(issues)
    elif issues:
        raise ContentError(issues[0])
    if issues:
        return None
    clean = {key: value[key] for key in PRE_MEETING_KEYS if key in value}
    if not clean.get("note"):
        clean.pop("note", None)
    return clean


SHARING_KEYS = ("checked_on", "source_url", "recipients")
SHARING_KINDS = ("local", "state", "federal", "private", "unknown")
SHARING_STATES = frozenset("AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR "
                           "PA RI SC SD TN TX UT VT VA WA WV WI WY DC AS GU MP PR VI".split()) | {"US"}  # US = federal
SHARING_HEADER = ["name", "state", "kind"]  # the import CSV's exact header
SHARING_MAX_ROWS = 2000
SHARING_HOST = "transparency.flocksafety.com"
SHARING_RULE_ID = "ca-civ-1798.90.55-sharing-limits"  # the law-package rules the agency page cites by id (DF1207-08)
SHARING_BULLETIN_ID = "ca-oag-2023-dle-06-out-of-state-sharing-guidance"


def sharing_row_problems(name, state, kind):
    """DF1207-08: ``[(field, message)]`` for one recipient the organizer transcribed from the vendor portal; [] when fine."""
    found = []
    if not (isinstance(name, str) and name.strip() and name == name.strip()):
        found.append(("name", "required, without leading or trailing spaces"))
    elif len(name) > 160 or any(unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp") for c in name):
        found.append(("name", "at most 160 characters, no control characters"))
    if not (isinstance(state, str) and state in SHARING_STATES):
        found.append(("state", "a two-letter USPS state or territory code, or US for a federal agency"))
    if not (isinstance(kind, str) and kind in SHARING_KINDS):
        found.append(("kind", f"choose one of {list(SHARING_KINDS)}"))
    elif state == "US" and kind != "federal":
        found.append(("state", "US is only for kind federal"))
    return found


def sharing_source_problem(value):
    """DF1207-08: why ``value`` is not an https URL on the vendor transparency portal host, else None."""
    try:
        _https(value, "source_url")
        parsed = urlsplit(value)
        if parsed.hostname != SHARING_HOST or parsed.port is not None:
            raise ValueError
    except ValueError:  # ContentError is one
        return f"must be an https:// URL on {SHARING_HOST}"
    return None


def _sharing(value, label, today=None):
    """DF1207-08: the organizer-transcribed list of agencies the vendor transparency portal names as receiving data
    (``profile.sharing``), as the portal shows it; never fetched and never read as a finding. Recipients are returned
    sorted by state then name; a duplicate (name casefolded, state) is refused."""
    if value is None:
        return None
    _require_keys(value, SHARING_KEYS, SHARING_KEYS, label)
    if _calendar_day(value["checked_on"], f"{label}.checked_on") > (today or date.today()):
        raise ContentError(f"{label}.checked_on: must not be after the build date")
    if (problem := sharing_source_problem(value["source_url"])) is not None:
        raise ContentError(f"{label}.source_url: {problem}")
    rows = value["recipients"]
    if not isinstance(rows, list) or not 1 <= len(rows) <= SHARING_MAX_ROWS:
        raise ContentError(f"{label}.recipients: must list 1 to {SHARING_MAX_ROWS} agencies")
    clean, seen = [], {}
    for index, row in enumerate(rows):
        item = f"{label}.recipients[{index}]"
        _require_keys(row, ("name", "state", "kind"), ("name", "state", "kind"), item)
        for field, message in sharing_row_problems(row["name"], row["state"], row["kind"]):
            raise ContentError(f"{item}.{field}: {message}")
        key = (row["name"].casefold(), row["state"])
        if key in seen:
            raise ContentError(f"{item}: duplicate of recipients[{seen[key]}] (same name and state)")
        seen[key] = index
        clean.append({"name": row["name"], "state": row["state"], "kind": row["kind"]})
    clean.sort(key=lambda r: (r["state"], r["name"].casefold(), r["name"]))
    return {"checked_on": value["checked_on"], "source_url": value["source_url"], "recipients": clean}


CONTRACT_CHECKLIST_PATH = Path(__file__).resolve().parent / "_resources" / "checklists" / "contract-checklist.json"
CONTRACT_CHECKLIST_NAME, MAX_CONTRACT_CHECKLIST_BYTES = "checklists/contract-checklist.json", 64 * 1024
CONTRACT_CHECK_IDS = ("term_renewal", "price_changes", "data_ownership", "retention", "sharing_network",
                      "vendor_data_use", "audit_search_logs", "termination_data_exit")
CONTRACT_CHECK_KEYS = {"checked_on", "contract_url", "elements"}
CONTRACT_STATUSES = ("addressed", "not_found", "unclear")


def load_contract_checklist():
    """DF2207-03: the draft list of terms an ALPR vendor agreement may set, read from the packaged resource only.

    Tool-wide, not part of any law package. Every failure is a ContentError naming the resource, never a path."""
    path, name = CONTRACT_CHECKLIST_PATH, CONTRACT_CHECKLIST_NAME

    def refuse(reason):
        return ContentError(f"contract checklist {name}: {reason}")
    try:
        for part in (path, path.parent, path.parents[1]):  # the file, its folder and the _resources folder
            if part.is_symlink():
                raise refuse("a symbolic link is not read")
        if not path.is_file():
            raise refuse("missing or not a file")
        if path.stat().st_size > MAX_CONTRACT_CHECKLIST_BYTES:
            raise refuse("larger than 64 KiB")
        data = path.read_bytes()
    except OSError as exc:
        raise refuse(f"unreadable ({exc.strerror or type(exc).__name__})") from None
    if len(data) > MAX_CONTRACT_CHECKLIST_BYTES:
        raise refuse("larger than 64 KiB")
    try:
        document = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise refuse("not valid UTF-8 JSON") from None
    if not isinstance(document, dict) or set(document) != {"schema_version", "status", "elements"}:
        raise refuse("must be an object with exactly schema_version, status and elements")
    if type(document["schema_version"]) is not int or document["schema_version"] != 1:
        raise refuse("schema_version must be the integer 1")
    if document["status"] != "draft":
        raise refuse("status must be draft")
    elements = document["elements"]
    if not isinstance(elements, list) or not all(isinstance(item, dict) and set(item) == {"id", "label"} for item in elements):
        raise refuse("elements must be a list of {id, label} objects")
    ids = [item["id"] for item in elements]
    if len(set(map(str, ids))) != len(ids):
        raise refuse("an element id is listed twice")
    if ids != list(CONTRACT_CHECK_IDS):
        raise refuse("element ids must be exactly " + ", ".join(CONTRACT_CHECK_IDS) + " in that order")
    for item in elements:
        label = item["label"]
        if (not isinstance(label, str) or not label.strip() or len(label) > 120 or not plain_text(label)
                or any(unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp") for c in label)):
            raise refuse(f"label of {item['id']} must be plain text of 1 to 120 characters")
    return {"status": "draft", "elements": [{"id": item["id"], "label": item["label"]} for item in elements]}


def _contract_check(value, contract, label, today=None, problems=None):
    """DF2207-03: an organizer's reading of the linked contract (``profile.contract_check``); never fetched or judged.

    ``contract`` is the agency's already validated ``profile.contract`` (or None). Every problem names its field; they
    go to ``problems`` (content-check), else the first is raised. Returns ``{"checked_on", "contract_url", "elements":
    [{"id", "label", "status", "page"?, "note"?}]}`` in checklist order, or None when absent or refused."""
    if value is None:
        return None
    issues, elements, shaped = [], [], isinstance(value, dict)
    if not shaped:
        issues.append(f"{label}: must be an object")
        value = {}
    issues += [f"{label}: unknown field {str(key)[:40]!r}" for key in sorted(set(value) - CONTRACT_CHECK_KEYS, key=str)]
    issues += [f"{label}.{key}: required" for key in ("checked_on", "contract_url", "elements") if shaped and key not in value]
    if "contract_url" in value:
        try:
            _https(value["contract_url"], f"{label}.contract_url")
            if contract is None:
                issues.append(f"{label}.contract_url: needs a valid profile.contract for the same agency")
            elif value["contract_url"] != contract["source_url"]:
                issues.append(f"{label}.contract_url: must equal profile.contract.source_url")
        except ContentError as exc:
            issues.append(str(exc))
    if "checked_on" in value:
        try:
            checked, read = _calendar_day(value["checked_on"], f"{label}.checked_on"), None
            if contract is not None:
                read = _calendar_day(contract["checked_on"], "contract.checked_on")
            if checked > (today or date.today()):
                issues.append(f"{label}.checked_on: must not be after the build date")
            elif read and checked < read:
                issues.append(f"{label}.checked_on: must not be before profile.contract.checked_on")
        except ContentError as exc:
            issues.append(str(exc))
    if "elements" in value:
        try:
            checklist = load_contract_checklist()["elements"]
        except ContentError as exc:
            checklist = []
            issues.append(f"{label}.elements: the contract checklist could not be loaded ({exc})")
        given = value["elements"]
        if not isinstance(given, dict) or not given:
            issues.append(f"{label}.elements: must be a non-empty object keyed by element id")
            given = {}
        known = {element["id"] for element in checklist}
        issues += [f"{label}.elements.{str(key)[:40]}: unknown element" for key in sorted(set(given) - known, key=str)]
        for element in checklist if given else []:
            where, entry = f"{label}.elements.{element['id']}", given.get(element["id"])
            if element["id"] not in given:
                issues.append(f"{where}: missing")
                continue
            if not isinstance(entry, dict):
                issues.append(f"{where}: must be an object with status, optional page and optional note")
                continue
            issues += [f"{where}: unknown field {str(key)[:40]!r}" for key in sorted(set(entry) - {"status", "page", "note"}, key=str)]
            status, bad = entry.get("status"), len(issues)
            if not isinstance(status, str) or status not in CONTRACT_STATUSES:
                issues.append(f"{where}.status: choose one of {list(CONTRACT_STATUSES)}")
            page, note = entry.get("page"), entry.get("note")
            if "page" in entry and (type(page) is not int or not 1 <= page <= 9999):
                issues.append(f"{where}.page: must be a whole number from 1 to 9999")
            if "note" in entry and (not isinstance(note, str) or not note.strip() or len(note) > 200 or not plain_text(note)
                                    or any(unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp") for c in note)):
                issues.append(f"{where}.note: plain text of 1 to 200 characters (no angle brackets or control characters)")
            if len(issues) == bad:
                elements.append({"id": element["id"], "label": element["label"], "status": status,
                                 **({"page": page} if "page" in entry else {}), **({"note": note} if "note" in entry else {})})
    if problems is not None:
        problems.extend(issues)
    elif issues:
        raise ContentError(issues[0])
    return None if issues else {"checked_on": value["checked_on"], "contract_url": value["contract_url"], "elements": elements}


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
    issues, result, values, linked = [], [], entry.get("asked", []), {}
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
                if key == "request" and (not isinstance(value[key], str) or not ASKED_REQUEST.fullmatch(value[key])):
                    raise ContentError(f"{item}.request: 1-40 characters a-z, 0-9 or -, starting with a letter or digit")
                if key == "request" and value[key] in linked:
                    raise ContentError(f"{item}.request: already linked by asked[{linked[value[key]]}]")
                if key.endswith("_on") and _calendar_day(value.get(key), f"{item}.{key}") > (today or date.today()):
                    raise ContentError(f"{item}.{key}: must not be after the build date")
                clean[key] = value[key]
            except ContentError as exc:
                issues.append(str(exc))
        if clean.get("updated_on", "9") < clean.get("asked_on", ""):
            issues.append(f"{item}.updated_on: must not be before asked_on")
        if "request" in clean:
            linked[clean["request"]] = index
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
        private = label == "kit/agencies.json"
        _require_keys(entry, AGENCY_KEYS | KIT_POSTAL_KEYS if private else AGENCY_KEYS, {"agency_id", "name"}, item)
        if private:
            _private_postal(entry, item, today)
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
        _require_keys(profile, PROFILE_KEYS | {"contract", "contract_check", "alpr_policy", "policy_check", "transparency", "funding", "pre_implementation_meeting", "sharing"}, set(), f"{item}.profile")  # not county_profile
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
            "contract": (contract := _contract(profile.get("contract"), f"{item}.profile.contract")),  # kept out of card JS
            "contract_check": _contract_check(profile.get("contract_check"), contract, f"{item}.profile.contract_check", today),  # likewise
            "alpr_policy": (alpr_policy := _alpr_policy(profile.get("alpr_policy"), f"{item}.profile.alpr_policy")),  # likewise
            "policy_check": _policy_check(profile.get("policy_check"), f"{item}.profile.policy_check", alpr_policy, today),  # likewise
            "pre_implementation_meeting": _pre_meeting(profile.get("pre_implementation_meeting"),  # likewise
                                                       f"{item}.profile.pre_implementation_meeting", today),
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
        if "funding" in profile:  # DF2207-02: only when the organizer recorded it, so other profiles keep their shape
            agencies[-1]["funding"] = _funding(profile["funding"], f"{item}.profile.funding", today)
        if profile.get("sharing") is not None:  # DF1207-08: likewise only when recorded, so other agencies keep their shape
            agencies[-1]["sharing"] = _sharing(profile["sharing"], f"{item}.profile.sharing", today)
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


def load_campaign_agencies(root, today=None):
    """The campaign's loaded agencies: content/agencies.json when present, else the kit's selected agencies."""
    path = Path(root) / "content" / "agencies.json"
    if path.is_file():
        return load_agencies(_read_json(path), "content/agencies.json", today)
    return default_agencies(root)


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


POINT_CHECKS_KEYS = {"schema_version", "checks"}
POINT_CHECK_KEYS = {"node", "status", "checked_on", "source", "note"}
POINT_CHECK_STATUSES = ("seen_in_person", "in_agency_record", "not_found_on_site", "duplicate")
MAX_POINT_CHECKS, MAX_POINT_CHECKS_PER_NODE, MAX_POINT_CHECKS_BYTES = 2000, 5, 512 * 1024
MAX_POINT_NOTE, MAX_POINT_SOURCE = 200, 500
LIBRARY_PREFIX = re.compile(r"[0-9a-f]{12}")
OSM_NODE_URL = re.compile(r"https://www\.openstreetmap\.org/node/(\d+)")
SOURCES_UNKNOWN = object()  # content-check passes this when sources.json is itself unreadable: skip prefix matching


def point_nodes(cameras):
    """The OSM node ids of a parsed cameras.geojson (its ``url`` property ``.../node/N``); ways have no node id."""
    nodes = set()
    for feature in cameras.get("features") or []:
        url = (feature.get("properties") or {}).get("url")
        match = OSM_NODE_URL.fullmatch(url) if isinstance(url, str) else None
        if match:
            nodes.add(int(match.group(1)))
    return nodes


def point_check_problems(path, nodes, sources, today):
    """map/point-checks.json (DF1422-11) -> ``(problems, {node: [checks, newest first]})``.

    Every entry is judged; a problem names its field, never a value. ``sources`` is the validated library list
    (``SOURCES_UNKNOWN`` skips the prefix match); ``today`` is the build day, which a ``checked_on`` may not pass.
    Nothing is fetched or written: a check never changes a point or the counts."""
    path, label = Path(path), "map/point-checks.json"
    if path.is_symlink():
        return [f"{label}: symlinks are not read"], {}
    if not path.is_file():  # a FIFO or directory is never opened
        return [f"{label}: not a regular file"], {}
    if path.stat().st_size > MAX_POINT_CHECKS_BYTES:
        return [f"{label}: file too large (over {MAX_POINT_CHECKS_BYTES // 1024} KiB)"], {}
    try:
        document = _read_json(path, MAX_POINT_CHECKS_BYTES)
        _require_keys(document, POINT_CHECKS_KEYS, POINT_CHECKS_KEYS, label)
    except ContentError as exc:
        text = str(exc)
        return [text if text.startswith(label) else f"map/{text}"], {}
    if type(document["schema_version"]) is not int or document["schema_version"] != 1:
        return [f"{label}.schema_version: must be 1"], {}
    entries = document["checks"]
    if not isinstance(entries, list) or len(entries) > MAX_POINT_CHECKS:
        return [f"{label}.checks: expected a list of at most {MAX_POINT_CHECKS} checks"], {}
    problems, found, seen = [], [], {}
    for index, entry in enumerate(entries):
        where = f"{label}.checks[{index}]"
        try:
            _require_keys(entry, POINT_CHECK_KEYS, POINT_CHECK_KEYS - {"note"}, where)
        except ContentError as exc:
            problems.append(str(exc))
            continue
        before = len(problems)
        node = entry["node"]
        if type(node) is not int or node < 1:
            problems.append(f"{where}.node: must be a positive integer")
        elif node not in nodes:
            problems.append(f"{where}.node: not a mapped point")
        else:
            seen[node] = seen.get(node, 0) + 1
            if seen[node] > MAX_POINT_CHECKS_PER_NODE:
                problems.append(f"{where}.node: more than {MAX_POINT_CHECKS_PER_NODE} checks for one point")
        if entry["status"] not in POINT_CHECK_STATUSES:
            problems.append(f"{where}.status: must be one of {', '.join(POINT_CHECK_STATUSES)}")
        day = entry["checked_on"]
        try:
            valid_day = isinstance(day, str) and DAY.fullmatch(day) and date.fromisoformat(day)
        except ValueError:
            valid_day = False
        if not valid_day:
            problems.append(f"{where}.checked_on: must be an ISO date YYYY-MM-DD")
        elif valid_day > today:
            problems.append(f"{where}.checked_on: must not be after the build day")
        source, fragment, title = entry["source"], None, None
        if isinstance(source, str) and LIBRARY_PREFIX.fullmatch(source):
            if sources is not SOURCES_UNKNOWN:
                matches = [item for item in sources if item["sha256"][:12] == source]
                if len(matches) != 1:
                    problems.append(f"{where}.source: library prefix must match exactly one sources.json entry")
                else:
                    fragment, title = matches[0]["fragment"], matches[0]["title"]
        elif isinstance(source, str) and source.startswith("https://") and len(source) <= MAX_POINT_SOURCE:
            try:
                _https(source, f"{where}.source")
            except ContentError as exc:
                problems.append(str(exc))
        else:
            problems.append(f"{where}.source: must be a 12-hex library prefix or an https:// URL of at most "
                            f"{MAX_POINT_SOURCE} characters")
        note = entry.get("note", "")
        if not isinstance(note, str) or not plain_text(note) or len(note) > MAX_POINT_NOTE or CONTACT_LIKE.search(note):
            problems.append(f"{where}.note: must be plain text of at most {MAX_POINT_NOTE} characters, "
                            "without an address or phone-like number")
        if len(problems) == before:
            found.append((day, index, node, {"status": entry["status"], "checked_on": day, "source": source,
                                             "fragment": fragment, "title": title, "note": note}))
    checks = {}
    for day, index, node, check in sorted(found, key=lambda item: item[:2], reverse=True):  # newest first
        checks.setdefault(node, []).append(check)
    return problems, checks


def load_point_checks(path, nodes, sources, today):
    problems, checks = point_check_problems(path, nodes, sources, today)
    if problems:
        raise ContentError(problems[0])
    return checks


def load_map(content_dir, sources=None, today=None):
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
    cameras_document = _read_json(cameras_path, MAX_GEOJSON_BYTES)
    camera_count = _validate_cameras(cameras_document, "map/cameras.geojson")
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
    checks_path = map_dir / "point-checks.json"  # optional (DF1422-11): organizer checks of single points
    point_checks = {}
    if history_present(checks_path):
        if sources is None:  # a direct call: the library comes from this content directory
            library = Path(content_dir) / "sources.json"
            sources = load_sources(_read_json(library)) if library.is_file() else []
        point_checks = load_point_checks(checks_path, point_nodes(cameras_document), sources,
                                         today or date.today())
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
        "point_checks": point_checks,
    }


# --- explainer pages (DF2237-07): content/pages/<slug>.json -> /<slug>.html ----
PAGE_KEYS, MAX_PAGES, MAX_NAV_PAGES = {"title", "description", "body_md", "nav"}, 50, 3
RESERVED_PAGE_SLUGS = STARTER_SLUGS | {"404", "corrections", "feed", "sitemap", "robots", "style", "site-data",
                                       "agency-cards", "app", "data", "vendor", "meetings", "methods", "privacy", "search",
                                       "press", "news"}  # generated top-level names (press: DF2207-10, news: DF0022-03; reserved even when disabled)


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


def load_newsletters(content_dir):
    """Public newsletter issues of ``content/newsletters/<day>.json`` (DF1952-09), oldest first; ``[]`` without any.

    Read through the one validator ``content-check`` also uses; never reads ``kit/``."""
    from .newsletter.archive import load_public_issues  # local: the newsletter package imports this module
    try:
        return load_public_issues(content_dir)
    except ValueError as exc:
        raise ContentError(str(exc)) from None


# --- topic pages (DF1952-06): content/topics/<slug>.json -> /topics/<slug>.html -------------------
TOPIC_KEYS, MAX_TOPICS, MAX_TOPIC_FINDINGS = {"title", "intro_md", "finding_ids"}, 30, 100
TOPIC_RESERVED = {"index", "topics", "timeline", "cameras"}  # generated names the page list does not reserve


def topic_paths(content_dir):
    """The ``content/topics/*.json`` paths in filename order; ``[]`` without the folder or without ``.json`` names."""
    folder = Path(content_dir) / "topics"
    if folder.is_symlink():
        raise ContentError("topics/: the folder must not be a symlink")
    if not folder.exists():
        return []
    if not folder.is_dir():
        raise ContentError("topics/: must be a folder, not a file")
    paths = sorted(path for path in folder.iterdir() if path.name.endswith(".json"))
    if len(paths) > MAX_TOPICS:
        raise ContentError(f"topics/: at most {MAX_TOPICS} topic files")
    return paths


def topics_namespace_problem(page_slugs, finding_slugs):
    """A loaded page or finding slug ``topics`` would share a name with the generated folder; only checked with topics."""
    for kind, slugs in (("explainer page", page_slugs), ("published finding", finding_slugs)):
        if "topics" in slugs:
            return f"topics/: the {kind} slug 'topics' clashes with the generated topics folder"
    return None


def load_topic(path, findings, pages):
    """One validated ``topics/<slug>.json``. ``findings`` None skips the finding-id check (the checker passes it when
    a finding file is itself refused, so every id would only add false lines)."""
    label, slug = "topics/" + path.name, path.stem
    if path.is_symlink() or not path.is_file():
        raise ContentError(f"{label}: must be a regular file, not a symlink or folder")
    _slug(slug, f"{label} filename")
    page_slugs = {page["slug"] for page in pages}
    finding_slugs = {finding["slug"]: finding["id"] for finding in findings or ()}
    if slug in RESERVED_PAGE_SLUGS or slug in TOPIC_RESERVED:
        raise ContentError(f"{label}: slug {slug} is a reserved page name")
    if slug in page_slugs:
        raise ContentError(f"{label}: slug {slug} clashes with the explainer page pages/{slug}.json")
    if slug in finding_slugs:
        raise ContentError(f"{label}: slug {slug} clashes with the published finding {finding_slugs[slug]}")
    try:
        document = _read_json(path)
    except ContentError as exc:
        raise ContentError("topics/" + str(exc)) from None
    _require_keys(document, TOPIC_KEYS, TOPIC_KEYS, label)
    title = _page_string(document, "title", label, 120, True)
    intro = _page_string(document, "intro_md", label, 200 * 1024, False)
    ids = document["finding_ids"]
    if not isinstance(ids, list) or not 1 <= len(ids) <= MAX_TOPIC_FINDINGS:
        raise ContentError(f"{label}.finding_ids: must list 1 to {MAX_TOPIC_FINDINGS} finding ids")
    known = {finding["id"] for finding in findings} if findings is not None else None
    by_slug = {finding["slug"] for finding in findings or ()}
    for number, finding_id in enumerate(ids):
        item = f"{label}.finding_ids[{number}]"
        if not isinstance(finding_id, str):
            raise ContentError(f"{item}: must be a finding id string")
        if finding_id in ids[:number]:
            raise ContentError(f"{item}: duplicate id {finding_id[:80]!r}")
        if known is not None and finding_id not in known:
            hint = " (use the finding id, not its slug)" if finding_id in by_slug else ""
            raise ContentError(f"{item}: {finding_id[:80]!r} is not a published finding id{hint}")
    return {"slug": slug, "title": title, "intro_md": intro, "finding_ids": list(ids)}


def load_topics(content_dir, findings, pages):
    """Topics sorted by slug (``[]`` without the folder or any file). Only the validated published ``findings`` can be
    listed: an unknown, draft or withdrawn id refuses by file, field and id. Pure read of ``content/topics``."""
    topics = sorted((load_topic(path, findings, pages) for path in topic_paths(content_dir)), key=lambda t: t["slug"])
    if topics:
        problem = topics_namespace_problem({p["slug"] for p in pages}, {f["slug"] for f in findings})
        if problem:
            raise ContentError(problem)
    return topics


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


# --- glossary (DF1952-04): content/glossary.json -> /glossary.html and per-finding term lists -----------------
GLOSSARY_KEYS, GLOSSARY_TERM_KEYS = {"schema_version", "terms", "notice_md"}, {"term", "aliases", "definition_md", "rule_id", "source_url"}
MAX_GLOSSARY_TERMS, MAX_GLOSSARY_NAME, MAX_GLOSSARY_DEFINITION, MAX_GLOSSARY_NOTICE = 80, 100, 8000, 1000


def glossary_key(name):
    """Name used for uniqueness and matching: Unicode NFC, casefolded, whitespace collapsed."""
    folded = unicodedata.normalize("NFC", name).casefold()
    return unicodedata.normalize("NFC", " ".join(folded.split()))


def _glossary_name(value):
    """The trimmed name, or None unless it is 1-100 characters of plain single-line text."""
    if not isinstance(value, str):
        return None
    name = value.strip()
    if (not 1 <= len(name) <= MAX_GLOSSARY_NAME or not plain_text(name)
            or any(unicodedata.category(char) == "Cc" for char in name)):
        return None
    return name


def _glossary_markdown(value, limit):
    return isinstance(value, str) and len(value) <= limit and all(visible(char) for char in value)


def _law_status(package):
    return "reviewed on " + package["reviewed_at"] if package["status"] == "reviewed" else "draft, not legally reviewed"


def load_glossary(document, root, problems=None):
    """Validate a parsed ``content/glossary.json``: ``{"terms": [...sorted...], "notice_md": str}``.

    Each term keeps ``term``, ``aliases``, ``definition_md`` and the optional ``rule_id``/``source_url`` it was given,
    plus a derived ``anchor``; a term with a ``rule_id`` also gets ``citation`` and ``law_status`` from the campaign's
    shipped law package (never input keys). Terms are ordered by normalized term, then the organizer's spelling.
    With ``problems`` a list, every independent refusal is appended and None returned; without it the first
    refusal raises ``ContentError`` (build). An empty ``terms`` list is valid and means the feature is off."""
    found = []

    def refuse(where, message):
        found.append(f"glossary.json{where}: {message}")

    package_state = []  # [(jurisdiction, package or None)] once a rule_id needs it

    def law_package():
        if not package_state:
            from . import law  # local: law is only needed when a rule is cited
            from .finding_law import jurisdiction  # local: finding_law imports this module
            try:
                name = jurisdiction(root)
            except ContentError as exc:
                found.append(str(exc))
                name = False
            package = None
            if name:
                try:
                    package = law.load_package(name)
                except (OSError, ValueError):
                    package = None
            package_state.append((name, package))
        return package_state[0]

    if not isinstance(document, dict):
        refuse("", "expected an object")
        document = {}
    else:
        unknown = sorted(str(key) for key in set(document) - GLOSSARY_KEYS)
        if unknown:
            refuse("", f"unknown field(s) {unknown}")
        missing = sorted({"schema_version", "terms"} - set(document))
        if missing:
            refuse("", f"missing field(s) {missing}")
    if "schema_version" in document and (type(document["schema_version"]) is not int or document["schema_version"] != 1):
        refuse(".schema_version", "must be 1")
    notice = document.get("notice_md", "")
    if not _glossary_markdown(notice, MAX_GLOSSARY_NOTICE):
        refuse(".notice_md", f"must be text of at most {MAX_GLOSSARY_NOTICE} characters without control characters")
        notice = ""
    entries = document.get("terms", [])
    if not isinstance(entries, list):
        refuse(".terms", "must be a list")
        entries = []
    elif len(entries) > MAX_GLOSSARY_TERMS:
        refuse(".terms", f"at most {MAX_GLOSSARY_TERMS} terms")
        entries = entries[:MAX_GLOSSARY_TERMS]

    names, anchors, terms = {}, {}, []  # normalized name -> (where, name); anchor -> term; accepted terms

    def claim(name, where):
        key = glossary_key(name)
        if key in names:
            first_where, first_name = names[key]
            refuse(where, f"name {name!r} repeats {first_name!r} (glossary.json{first_where})")
            return False
        names[key] = (where, name)
        return True

    for index, entry in enumerate(entries):
        base = f".terms[{index}]"
        if not isinstance(entry, dict):
            refuse(base, "expected an object")
            continue
        unknown = sorted(str(key) for key in set(entry) - GLOSSARY_TERM_KEYS)
        if unknown:
            refuse(base, f"unknown field(s) {unknown}")
        missing = sorted({"term", "aliases", "definition_md"} - set(entry))
        if missing:
            refuse(base, f"missing field(s) {missing}")
        ok, term, anchor = not missing, _glossary_name(entry.get("term")), None
        if "term" in entry:
            if term is None:
                refuse(base + ".term", f"must be plain single-line text of 1-{MAX_GLOSSARY_NAME} characters")
                ok = False
            elif claim(term, base + ".term"):
                anchor = "term-" + re.sub(r"[^a-z0-9]+", "-", glossary_key(term)).strip("-")
                if anchor == "term-":
                    refuse(base + ".term", f"{term!r} has no letter or digit a-z/0-9 to build an anchor from")
                    ok = False
                elif anchor in anchors:
                    refuse(base + ".term", f"{term!r} gives anchor {anchor}, which {anchors[anchor]!r} already has")
                    ok = False
                else:
                    anchors[anchor] = term
            else:
                ok = False
        aliases = entry.get("aliases")
        if "aliases" in entry:
            if not isinstance(aliases, list) or not 1 <= len(aliases) <= 3:
                refuse(base + ".aliases", "must be a list of 1 to 3 names")
                ok = False
            else:
                accepted = []
                for number, alias in enumerate(aliases):
                    where, name = f"{base}.aliases[{number}]", _glossary_name(alias)
                    if name is None:
                        refuse(where, f"must be plain single-line text of 1-{MAX_GLOSSARY_NAME} characters")
                        ok = False
                    elif claim(name, where):
                        accepted.append(name)
                    else:
                        ok = False
                aliases = accepted
        definition = entry.get("definition_md")
        if "definition_md" in entry and (not _glossary_markdown(definition, MAX_GLOSSARY_DEFINITION)
                                         or not definition.strip()):
            refuse(base + ".definition_md", f"must be non-blank text of at most {MAX_GLOSSARY_DEFINITION} characters "
                                            "without control characters")
            ok = False
        accepted_term = {"term": term, "aliases": aliases, "definition_md": definition}
        if "source_url" in entry:
            try:
                accepted_term["source_url"] = _https(entry["source_url"], "glossary.json" + base + ".source_url")
            except ContentError as exc:
                found.append(str(exc))
                ok = False
        if "rule_id" in entry:
            rule_id = entry["rule_id"]
            if not isinstance(rule_id, str) or not rule_id:
                refuse(base + ".rule_id", "must be a non-empty rule id from the law package")
                ok = False
            elif "source_url" not in entry:
                refuse(base + ".source_url", "is required with rule_id")
                ok = False
            else:
                jur, package = law_package()
                rules = {rule["rule_id"]: rule for rule in package["rules"]} if package else {}
                if jur is False:
                    ok = False  # the unreadable campaign.json is already named once
                elif jur is None:
                    refuse(base + ".rule_id", "there is no campaign.json, so no law package to check the rule against")
                    ok = False
                elif package is None:
                    refuse(base + ".rule_id", f"there is no usable {jur} law package")
                    ok = False
                elif rule_id not in rules:
                    refuse(base + ".rule_id", f"{rule_id!r} is not in the {jur} law package")
                    ok = False
                elif "source_url" in accepted_term and accepted_term["source_url"] not in {
                        source["url"] for source in rules[rule_id]["sources"]}:
                    refuse(base + ".source_url", f"is not one of the sources the {jur} law package lists for {rule_id}")
                    ok = False
                else:
                    accepted_term.update(rule_id=rule_id, citation=rules[rule_id]["citation"],
                                         law_status=_law_status(package))
        if ok:
            accepted_term["anchor"] = anchor
            terms.append(accepted_term)
    if found:
        if problems is None:
            raise ContentError(found[0])
        problems.extend(found)
        return None
    terms.sort(key=lambda item: (glossary_key(item["term"]), item["term"]))
    return {"terms": terms, "notice_md": notice}


def read_glossary(root, problems=None):
    """``load_glossary`` of ``<root>/content/glossary.json``; None when the file is absent.

    A symlink (even dangling), folder or other non-regular path is refused, never skipped. ``problems``: see
    ``load_glossary`` (content-check collects, build raises)."""
    path = Path(root) / "content" / "glossary.json"
    try:
        if not os.path.lexists(path):
            return None
        if path.is_symlink() or not path.is_file():
            raise ContentError("glossary.json: must be a regular file, not a symlink or folder")
        try:
            document = _read_json(path)
        except OSError:
            raise ContentError("glossary.json: could not be read") from None
    except ContentError as exc:
        if problems is None:
            raise
        problems.append(str(exc))
        return None
    return load_glossary(document, root, problems)


def _word_char(char):
    return char == "_" or unicodedata.category(char)[0] in "LN"


def _mentions(text, name):
    """True when ``name`` occurs in ``text`` with a non-word character (or the end) on both sides."""
    start = text.find(name)
    while start != -1:
        end = start + len(name)
        if (start == 0 or not _word_char(text[start - 1])) and (end == len(text) or not _word_char(text[end])):
            return True
        start = text.find(name, start + 1)
    return False


def glossary_terms_used(finding, glossary):
    """The glossary terms a finding's title, summary or body_md use, in the glossary's alphabetical order.

    Each field is searched on its own (never joined), normalized like the names (``glossary_key``); a term counts
    when its name or any alias is a whole-word literal match. Markdown is searched as written, so link destinations
    and code spans count. Definitions, sources and other findings never count."""
    fields = [glossary_key(finding.get(key) or "") for key in ("title", "summary", "body_md")]
    return [term for term in glossary["terms"]
            if any(_mentions(field, glossary_key(name)) for field in fields for name in [term["term"], *term["aliases"]])]


# --- faq (DF0022-01): content/faq.json -> /faq.html ----------------------------------------------------------
FAQ_KEYS, FAQ_ENTRY_KEYS = {"schema_version", "entries"}, {"question", "answer_md", "finding_ids", "rule_id"}
MAX_FAQ_ENTRIES, MAX_FAQ_QUESTION, MAX_FAQ_ANSWER, MAX_FAQ_FINDINGS, MAX_FAQ_RULE = 40, 160, 1500, 3, 120


def _faq_question(value):
    """True for a trimmed, non-blank, single-line plain-text question of 1-160 characters."""
    return (isinstance(value, str) and value == value.strip() and 1 <= len(value) <= MAX_FAQ_QUESTION
            and plain_text(value) and not any(unicodedata.category(char) in ("Cc", "Cf") for char in value))


def load_faq(document, root, findings, problems=None):
    """Validate a parsed ``content/faq.json``: ``[{question, answer_md, finding_ids, [rule_id, citation, law_status]}]``.

    ``findings`` are the already validated, published content findings (a finding id must be one of their ids; None
    skips only that membership check, for content-check when a finding file is itself refused). ``rule_id`` is looked
    up in the campaign's shipped law package, loaded only when an entry cites one; ``citation`` and ``law_status``
    come from the package, never from the file. Entries keep file order. With ``problems`` a list every independent
    refusal is appended (``faq.json.entries[N]...``, zero-based) and None returned; without it the first raises
    ``ContentError``. Answers are never echoed in a refusal."""
    found = []

    def refuse(where, message):
        found.append(f"faq.json{where}: {message}")

    package_state = []  # [(package or None)] once a rule_id needs it

    def law_package():
        if not package_state:
            from . import law  # local: law is only needed when a rule is cited
            from .finding_law import jurisdiction  # local: finding_law imports this module
            package = None
            try:
                name = jurisdiction(root)
                package = law.load_package(name) if name else None
            except (ContentError, OSError, ValueError):
                package = None
            package_state.append(package)
        return package_state[0]

    if not isinstance(document, dict):
        refuse("", "expected an object")
        document = {}
    else:
        unknown = sorted(str(key) for key in set(document) - FAQ_KEYS)
        if unknown:
            refuse("", f"unknown field(s) {unknown}")
        missing = sorted(FAQ_KEYS - set(document))
        if missing:
            refuse("", f"missing field(s) {missing}")
    if "schema_version" in document and (type(document["schema_version"]) is not int or document["schema_version"] != 1):
        refuse(".schema_version", "must be 1")
    entries = document.get("entries", [])
    if "entries" in document and (not isinstance(entries, list) or not 1 <= len(entries) <= MAX_FAQ_ENTRIES):
        refuse(".entries", f"must be a list of 1 to {MAX_FAQ_ENTRIES} entries")
        entries = []
    elif "entries" not in document:
        entries = []

    published = None if findings is None else {finding["id"] for finding in findings}
    first_at, accepted = {}, []  # normalized question -> first position; validated entries
    for index, entry in enumerate(entries):
        base = f".entries[{index}]"
        before = len(found)
        if not isinstance(entry, dict):
            refuse(base, "expected an object")
            continue
        unknown = sorted(str(key) for key in set(entry) - FAQ_ENTRY_KEYS)
        if unknown:
            refuse(base, f"unknown field(s) {unknown}")
        missing = sorted({"question", "answer_md"} - set(entry))
        if missing:
            refuse(base, f"missing field(s) {missing}")
        question = entry.get("question")
        if "question" in entry:
            if not _faq_question(question):
                refuse(base + ".question", f"must be trimmed plain single-line text of 1-{MAX_FAQ_QUESTION} characters "
                                           "without <, > or control characters")
            else:
                key = glossary_key(question)
                if key in first_at:
                    refuse(base + ".question", f"duplicates entries[{first_at[key]}].question")
                else:
                    first_at[key] = index
        answer = entry.get("answer_md")
        if "answer_md" in entry and (not isinstance(answer, str) or not answer.strip() or len(answer) > MAX_FAQ_ANSWER
                                     or not plain_text(answer)):
            refuse(base + ".answer_md", f"must be non-blank plain text of at most {MAX_FAQ_ANSWER} characters "
                                        "without <, > or control characters")
        ids = entry.get("finding_ids", [])
        if "finding_ids" in entry and (not isinstance(ids, list) or len(ids) > MAX_FAQ_FINDINGS):
            refuse(base + ".finding_ids", f"must be a list of at most {MAX_FAQ_FINDINGS} finding ids")
            ids = []
        seen_ids = set()
        for number, finding_id in enumerate(ids):
            where = f"{base}.finding_ids[{number}]"
            if not isinstance(finding_id, str) or not FINDING_ID.fullmatch(finding_id):
                refuse(where, "must be a finding id (letters, digits, underscore or hyphen, 1-64 characters)")
            elif finding_id in seen_ids:
                refuse(where, "duplicate finding id")
            else:
                seen_ids.add(finding_id)
                if published is not None and finding_id not in published:
                    refuse(where, "not a published finding id")
        rule = {}
        if "rule_id" in entry:
            rule_id = entry["rule_id"]
            if not isinstance(rule_id, str) or not rule_id or len(rule_id) > MAX_FAQ_RULE:
                refuse(base + ".rule_id", f"must be a non-empty rule id of at most {MAX_FAQ_RULE} characters")
            else:
                package = law_package()
                if package is None:
                    refuse(base + ".rule_id", "no usable campaign law package")
                else:
                    rules = {item["rule_id"]: item for item in package["rules"]}
                    if rule_id not in rules:
                        refuse(base + ".rule_id", "unknown law-package rule id")
                    else:
                        rule = {"rule_id": rule_id, "citation": rules[rule_id]["citation"], "law_status": _law_status(package)}
        if len(found) == before:
            accepted.append({"question": question, "answer_md": answer, "finding_ids": list(ids), **rule})
    if found:
        if problems is None:
            raise ContentError(found[0])
        problems.extend(found)
        return None
    return accepted


def read_faq(root, findings, problems=None):
    """``load_faq`` of ``<root>/content/faq.json``; None when the file is absent.

    A symlink (even dangling), folder or other non-regular path is refused, never skipped, and a read failure is a
    named ``ContentError``, not a traceback. ``problems``: see ``load_faq`` (content-check collects, build raises)."""
    path = Path(root) / "content" / "faq.json"
    try:
        if path.is_symlink():
            raise ContentError("faq.json: symlinks are not read")
        if not os.path.lexists(path):
            return None
        if not path.is_file():
            raise ContentError("faq.json: must be a regular file, not a symlink or folder")
        try:
            document = _read_json(path)
        except OSError:
            raise ContentError("faq.json: could not be read") from None
        except RecursionError:
            raise ContentError("faq.json: not valid UTF-8 JSON (nested too deeply)") from None
    except ContentError as exc:
        if problems is None:
            raise
        problems.append(str(exc))
        return None
    return load_faq(document, root, findings, problems)


# --- press coverage (DF0022-03): content/coverage.json -> /news.html and "Reported in" lines ------------------
COVERAGE_KEYS = {"schema_version", "entries"}
COVERAGE_REQUIRED, COVERAGE_OPTIONAL = {"outlet", "headline", "url", "published_on"}, {"findings", "kind"}
COVERAGE_KINDS = ("news", "opinion", "broadcast", "other")
MAX_COVERAGE_ENTRIES, MAX_COVERAGE_OUTLET, MAX_COVERAGE_HEADLINE, MAX_COVERAGE_FINDINGS = 1000, 80, 200, 3
COVERAGE_DATE = re.compile(r"\d{4}-\d{2}-\d{2}", re.ASCII)


def _coverage_text(value, limit):
    """True for a non-blank string of 1..limit characters with no angle bracket, control character or line break.

    The text is kept exactly as written (no trimming): it is the outlet's name or the headline as published."""
    return (isinstance(value, str) and 1 <= len(value) <= limit and value.strip() != "" and "<" not in value
            and ">" not in value and not any(unicodedata.category(char) in ("Cc", "Cs", "Zl", "Zp") for char in value))


def coverage_entries(document, *, today=None):
    """Validate the shape of a parsed ``content/coverage.json`` and return its normalized entries in stored order.

    ``{"schema_version": 1, "entries": [{outlet, headline, url, published_on, [findings, kind]}]}``. The first refusal
    raises ``ContentError`` naming ``coverage.json.entries[N].field`` (zero-based) and never repeating a value. Finding ids
    are checked for form and uniqueness only; ``load_coverage`` also checks that each one is published. A URL is
    identified by its exact text, so a second entry with the same string is refused (no canonicalization)."""
    today = today or date.today()
    if not isinstance(document, dict):
        raise ContentError("coverage.json: expected an object")
    if set(document) != COVERAGE_KEYS:
        raise ContentError("coverage.json: must have exactly the fields schema_version and entries")
    if type(document["schema_version"]) is not int or document["schema_version"] != 1:
        raise ContentError("coverage.json.schema_version: must be 1")
    entries = document["entries"]
    if not isinstance(entries, list) or len(entries) > MAX_COVERAGE_ENTRIES:
        raise ContentError(f"coverage.json.entries: must be a list of at most {MAX_COVERAGE_ENTRIES} entries")
    accepted, first_at = [], {}
    for index, entry in enumerate(entries):
        base = f"coverage.json.entries[{index}]"
        if not isinstance(entry, dict):
            raise ContentError(f"{base}: expected an object")
        unknown = sorted(str(key) for key in set(entry) - COVERAGE_REQUIRED - COVERAGE_OPTIONAL)
        if unknown:
            raise ContentError(f"{base}: unknown field(s) {unknown}")
        missing = sorted(COVERAGE_REQUIRED - set(entry))
        if missing:
            raise ContentError(f"{base}: missing field(s) {missing}")
        if not _coverage_text(entry["outlet"], MAX_COVERAGE_OUTLET):
            raise ContentError(f"{base}.outlet: must be non-blank plain single-line text of 1-{MAX_COVERAGE_OUTLET} characters "
                               "without <, > or control characters")
        if not _coverage_text(entry["headline"], MAX_COVERAGE_HEADLINE):
            raise ContentError(f"{base}.headline: must be non-blank plain single-line text of 1-{MAX_COVERAGE_HEADLINE} characters "
                               "without <, > or control characters")
        url = _https(entry["url"], f"{base}.url")
        if url in first_at:
            raise ContentError(f"{base}.url: duplicate URL")
        first_at[url] = index
        published_on = entry["published_on"]
        try:
            real = (date.fromisoformat(published_on)
                    if isinstance(published_on, str) and COVERAGE_DATE.fullmatch(published_on) else None)
        except ValueError:
            real = None
        if real is None or real.isoformat() != published_on:
            raise ContentError(f"{base}.published_on: must be a real date written YYYY-MM-DD")
        if real > today:
            raise ContentError(f"{base}.published_on: must not be after today")
        ids = entry.get("findings", [])
        if (not isinstance(ids, list) or len(ids) > MAX_COVERAGE_FINDINGS or len(set(map(str, ids))) != len(ids)
                or any(not isinstance(item, str) or not FINDING_ID.fullmatch(item) for item in ids)):
            raise ContentError(f"{base}.findings: must name at most {MAX_COVERAGE_FINDINGS} unique published finding IDs")
        kind = entry.get("kind", "news")
        if not isinstance(kind, str) or kind not in COVERAGE_KINDS:
            raise ContentError(f"{base}.kind: must be one of {', '.join(COVERAGE_KINDS)}")
        accepted.append({"outlet": entry["outlet"], "headline": entry["headline"], "url": url, "published_on": published_on,
                         "findings": list(ids), "kind": kind})
    return accepted


def load_coverage(document, findings, *, today=None):
    """``coverage_entries`` plus the reference check: every cited finding id must be one of the already validated,
    published content ``findings`` (a draft, a withdrawn or an unknown id is refused). ``findings`` None skips only that
    membership check (content-check, when a finding file is itself refused and already named). Entries keep file order."""
    entries = coverage_entries(document, today=today)
    if findings is not None:
        published = {finding["id"] for finding in findings}
        for index, entry in enumerate(entries):
            if any(finding_id not in published for finding_id in entry["findings"]):
                raise ContentError(f"coverage.json.entries[{index}].findings: "
                                   f"must name at most {MAX_COVERAGE_FINDINGS} unique published finding IDs")
    return entries


COVERAGE_ABSENT = object()  # read_coverage_document's answer for a missing file (a JSON null is a present, refused file)


def read_coverage_document(root):
    """The parsed ``<root>/content/coverage.json``, or ``COVERAGE_ABSENT`` when the file does not exist.

    A symlink (even dangling), folder or other non-regular path is refused, never skipped; a read failure is a named
    ``ContentError``, not a traceback. The file is read through the size-limited ``_read_json``."""
    path = Path(root) / "content" / "coverage.json"
    if path.is_symlink():
        raise ContentError("coverage.json: symlinks are not read")
    if not os.path.lexists(path):
        return COVERAGE_ABSENT
    if not path.is_file():
        raise ContentError("coverage.json: must be a regular file, not a symlink or folder")
    try:
        return _read_json(path)
    except OSError:
        raise ContentError("coverage.json: could not be read") from None
    except RecursionError:
        raise ContentError("coverage.json: not valid UTF-8 JSON (nested too deeply)") from None


def read_coverage(root, findings, *, today=None):
    """``load_coverage`` of ``<root>/content/coverage.json``; None when the file is absent (present but empty is ``[]``)."""
    document = read_coverage_document(root)
    return None if document is COVERAGE_ABSENT else load_coverage(document, findings, today=today)


# --- assembly ---------------------------------------------------------------

def load_content(root, today=None, *, _replacing_cosign_id=None):
    """Return the validated content model for ``root`` or None when no content/ exists.

    ``_replacing_cosign_id`` is private to ``meetings-stage --replace``: it skips only that one staged meeting's
    co-signer cross-check so a stale grant can be repaired; its structure is still validated."""
    root = Path(root)
    content_dir = root / "content"
    if not content_dir.is_dir():
        return None
    site_path = content_dir / "site.json"
    if not site_path.is_file():
        raise ContentError("content/site.json is required when content/ exists")
    site = load_site(_read_json(site_path), content_dir / "public-assets")

    findings, slugs, ids, finding_files = [], set(), set(), {}
    for path in sorted((content_dir / "findings").glob("*.json")) if (content_dir / "findings").is_dir() else []:
        finding = load_finding(_read_json(path), "findings/" + path.name)
        if finding["slug"] in slugs or finding["id"] in ids:
            raise ContentError(f"findings/{path.name}: duplicate finding slug or id")
        slugs.add(finding["slug"])
        ids.add(finding["id"])
        finding_files[finding["id"]] = "findings/" + path.name
        findings.append(finding)
    from .finding_law import check as check_law  # local: finding_law imports this module
    law_problems, local_rules = check_law(root, findings)
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
    continued = continuation_problems(meetings)  # DF0022-07: a continued item names a real, later, same-body meeting
    if continued:
        raise ContentError(continued[0])

    from .supporters import cosign_problems  # local: supporters imports this module
    cosigned = cosign_problems(root, meetings, today=today, skip_meeting_id=_replacing_cosign_id)  # DF2207-14
    if cosigned:
        raise ContentError(cosigned[0])

    sources_path = content_dir / "sources.json"
    sources = load_sources(_read_json(sources_path)) if sources_path.is_file() else []
    page_problems = check_source_pages(findings, sources)
    if page_problems:
        raise ContentError(page_problems[0])

    agencies = load_campaign_agencies(root, today)
    known_agencies = {agency["agency_id"] for agency in agencies}
    for finding in findings:  # DF1952-07: extra agencies of a multi-agency finding must be known agencies
        check_finding_agencies(finding, known_agencies, finding_files[finding["id"]])

    map_config = load_map(content_dir, sources, today)
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

    pages = load_pages(content_dir, slugs)
    topics = load_topics(content_dir, findings, pages)
    from .supporters import load_supporters  # local: supporters imports this module
    supporters = load_supporters(root, today=today)  # DF2207-13: None without content/supporters.json
    model = {
        "site": site,
        "findings": findings,
        "newsletters": load_newsletters(content_dir),
        "pages": pages,
        "topics": topics,
        "meetings": meetings,
        "sources": sources,
        "agencies": agencies,
        "map": map_config,
        "vendor": vendor,
        "public_assets": public_assets,
        "assets_dir": assets_dir,
        "labels": labels,
        "officials": officials,
        "local_rules": local_rules,  # DF1952-01: every validated version of law/local-rules.json, [] without one
    }
    glossary = read_glossary(root)  # DF1952-04: optional; None without the file
    if glossary is not None:
        model["glossary"] = glossary
    faq = read_faq(root, findings)  # DF0022-01: optional; None without the file
    if faq is not None:
        model["faq"] = faq
    coverage = read_coverage(root, findings, today=today)  # DF0022-03: optional; None without the file, [] when present and empty
    if coverage is not None:
        model["coverage"] = coverage
    # content/redirects.json (DF2037-16): the key is present exactly when the file is, so a parsed JSON null is still
    # checked (and refused) by site against the pages it writes; without the file there is no key.
    if redirects_path.is_file() or redirects_path.is_symlink():
        model["redirects"] = _read_json(redirects_path)
    if supporters is not None:  # DF2207-13: public rows only (never the private consent records)
        model["supporters"] = supporters
    from .body_schedules import read_body_schedules  # local: body_schedules imports this module
    schedules = read_body_schedules(root, today=today)  # DF0022-10: optional; the key exists only with rows
    if schedules:
        model["body_schedules"] = schedules
    return model
