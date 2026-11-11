"""Bounded, read-only per-campaign configuration (names, law package, public URL, mailbox label, clock)."""
from dataclasses import dataclass
import json
import re
import urllib.parse
import os
import stat
from zoneinfo import ZoneInfo

from .outcomes import received_items

DEFAULT_TIMEZONE = "America/Los_Angeles"
_MAX_BYTES = 64 * 1024
_ERROR = "campaign_timezone_invalid"
CONFIG_ERROR = "campaign_config_invalid"
DEFAULT_DISPLAY_NAME = "Records project"
DEFAULT_LAW_PACKAGE = "us-ca"
_ID = re.compile(r"[a-z0-9-]{2,40}")
_LAW = re.compile(r"[a-z]{2}-[a-z]{2}")
_KEYS = {"schema_version", "campaign_id", "display_name", "law_package", "public_base_url",
         "records_mailbox_label", "timezone"}


@dataclass(frozen=True)
class CampaignConfig:
    campaign_id: "str | None" = None
    display_name: str = DEFAULT_DISPLAY_NAME
    law_package: str = DEFAULT_LAW_PACKAGE
    public_base_url: "str | None" = None
    records_mailbox_label: "str | None" = None
    timezone: str = DEFAULT_TIMEZONE


def _read(project, error):
    """Return the parsed campaign.json object, or None when the file is absent."""
    root = received_items._safe_dir(project)

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(error)
            result[key] = value
        return result
    try:
        fd = os.open(root / "campaign.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError(error)
        raw = stream.read(_MAX_BYTES + 1)
    if len(raw) > _MAX_BYTES:
        raise ValueError(error)
    config = json.loads(raw, object_pairs_hook=unique)
    if (not isinstance(config, dict) or type(config.get("schema_version")) is not int
            or config["schema_version"] != 1):
        raise ValueError(error)
    return config


def _timezone(config, error):
    if "timezone" not in config:
        return DEFAULT_TIMEZONE
    zone = config["timezone"]
    if not isinstance(zone, str) or not 1 <= len(zone) <= 128 or zone != zone.strip():
        raise ValueError(error)
    ZoneInfo(zone)
    return zone


def _text(config, key, error):
    value = config.get(key)
    if not isinstance(value, str) or not 1 <= len(value) <= 80 or value != value.strip() or not value.isprintable():
        raise ValueError(error)
    return value


def _base_url(value, error):
    if not isinstance(value, str) or not value.isascii() or not value.isprintable() or len(value) > 200:
        raise ValueError(error)
    parts = urllib.parse.urlsplit(value)
    if (parts.scheme != "https" or not parts.hostname or parts.path not in ("", "/") or parts.query
            or parts.fragment or parts.username is not None or parts.password is not None):
        raise ValueError(error)
    return "https://" + parts.netloc


def load(project) -> CampaignConfig:
    """Validated campaign.json v1; a missing file gives neutral defaults, invalid data never defaults."""
    try:
        config = _read(project, CONFIG_ERROR)
        if config is None:
            return CampaignConfig()
        if set(config) - _KEYS:
            raise ValueError(CONFIG_ERROR)
        values = {"timezone": _timezone(config, CONFIG_ERROR)}
        if "campaign_id" in config:
            if not isinstance(config["campaign_id"], str) or not _ID.fullmatch(config["campaign_id"]):
                raise ValueError(CONFIG_ERROR)
            values["campaign_id"] = config["campaign_id"]
        for key in ("display_name", "records_mailbox_label"):
            if key in config:
                values[key] = _text(config, key, CONFIG_ERROR)
        if "law_package" in config:
            if not isinstance(config["law_package"], str) or not _LAW.fullmatch(config["law_package"]):
                raise ValueError(CONFIG_ERROR)
            values["law_package"] = config["law_package"]
        if "public_base_url" in config:
            values["public_base_url"] = _base_url(config["public_base_url"], CONFIG_ERROR)
        return CampaignConfig(**values)
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        raise ValueError(CONFIG_ERROR) from None


def load_timezone(project) -> str:
    """Read only project/campaign.json; invalid supplied data never defaults."""
    try:
        config = _read(project, _ERROR)
        return DEFAULT_TIMEZONE if config is None else _timezone(config, _ERROR)
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        raise ValueError(_ERROR) from None
