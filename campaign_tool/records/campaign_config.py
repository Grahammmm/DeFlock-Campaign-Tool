"""Bounded, read-only configuration for the records project's local clock."""
import json
import os
import stat
from zoneinfo import ZoneInfo

from .outcomes import received_items

DEFAULT_TIMEZONE = "America/Los_Angeles"
_MAX_BYTES = 64 * 1024
_ERROR = "campaign_timezone_invalid"


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(_ERROR)
        result[key] = value
    return result


def load_timezone(project) -> str:
    """Read only project/campaign.json; invalid supplied data never defaults."""
    try:
        root = received_items._safe_dir(project)
        try:
            fd = os.open(root / "campaign.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return DEFAULT_TIMEZONE
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError(_ERROR)
            raw = stream.read(_MAX_BYTES + 1)
        if len(raw) > _MAX_BYTES:
            raise ValueError(_ERROR)
        config = json.loads(raw, object_pairs_hook=_unique_object)
        if (not isinstance(config, dict) or type(config.get("schema_version")) is not int
                or config["schema_version"] != 1):
            raise ValueError(_ERROR)
        if "timezone" not in config:
            return DEFAULT_TIMEZONE
        zone = config["timezone"]
        if not isinstance(zone, str) or not 1 <= len(zone) <= 128 or zone != zone.strip():
            raise ValueError(_ERROR)
        ZoneInfo(zone)
        return zone
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        raise ValueError(_ERROR) from None
