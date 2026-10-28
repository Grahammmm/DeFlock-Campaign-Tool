"""Administrator authorization is separate from independently permitted egress."""
import json
import os
import re
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit


class PortalError(Exception):
    """Only fixed reason codes, never URL-bearing upstream exception text."""


FETCH_MIN_TOTAL_BYTES = 25 * 1024 * 1024


@dataclass(frozen=True)
class FetchSettings:
    apply: bool = False
    max_items: int = 20
    max_bytes_total: int = 524288000


def fetch_settings(plan):
    """Read only plan.portal.fetch; absent means the unchanged dry-run path."""
    try:
        if not isinstance(plan,dict):
            raise ValueError()
        portal=plan.get('portal',{})
        if not isinstance(portal,dict) or set(portal)-{'fetch'}:
            raise ValueError()
        value=portal.get('fetch',{})
        if not isinstance(value,dict) or set(value)-{'apply','max_items','max_bytes_total'}:
            raise ValueError()
        result=FetchSettings(**value)
        if type(result.apply) is not bool or type(result.max_items) is not int or not 1<=result.max_items<=1000:
            raise ValueError()
        if type(result.max_bytes_total) is not int or not FETCH_MIN_TOTAL_BYTES<=result.max_bytes_total<=1024*1024*1024:
            raise ValueError()
        return result
    except (TypeError,ValueError):
        raise PortalError('fetch_plan_invalid') from None


def strict_json(raw):
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError('duplicate_key')
            result[key] = value
        return result
    def constant(value):
        raise ValueError('nonfinite_json')
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)


def hostname(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?", value):
        raise PortalError("invalid_host")
    if ".." in value or "." not in value:
        raise PortalError("invalid_host")
    return value


def url_parts(url):
    if not isinstance(url, str) or len(url) > 16384 or any(ord(c) < 33 or ord(c) == 127 for c in url):
        raise PortalError("invalid_url")
    try:
        p = urlsplit(url)
        if p.scheme != "https" or p.username or p.password or p.port not in (None, 443) or p.fragment:
            raise ValueError()
        hostname(p.hostname)
        path = unquote(p.path)
        if "\\" in path or any(x in (".", "..") for x in path.split("/")) or "%" in path:
            raise ValueError()
        return p
    except (TypeError, ValueError):
        raise PortalError("invalid_url") from None


def safe_ancestors(path):
    path = Path(os.path.abspath(path))
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise PortalError("symlink_path")
    return path


@dataclass(frozen=True)
class Approval:
    expires: datetime
    portals: frozenset
    redirects: frozenset
    scopes: dict

    def authorize(self, url, origin, now):
        if now.tzinfo is None or now >= self.expires:
            raise PortalError("approval_expired")
        p = url_parts(url)
        host = p.hostname
        if origin not in self.portals:
            raise PortalError("portal_not_approved")
        if host != origin and host not in self.redirects:
            raise PortalError("redirect_not_approved")
        if not any(unquote(p.path).startswith(prefix) for prefix in self.scopes.get(host, ())):
            raise PortalError("path_not_approved")
        return host


LEGACY_PURPOSE = "flock-nextrequest-portal-intake"
LEGACY_KEYS = frozenset({"enabled", "purpose", "expires_utc", "expires_at", "portal_hosts",
                         "approved_portal_hosts", "redirect_hosts", "approved_redirect_hosts"})


def _expiry(value, now):
    expiry = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if expiry.tzinfo is None or now.tzinfo is None or expiry <= now:
        raise PortalError("approval_expired")
    return expiry


def _legacy_approval(data, now):
    """Read-only mapping of the deployed coordinator allowlist format.

    The root-owned allowlist already deployed for the existing NextRequest
    intake has no ``version`` or ``path_prefixes``. It is read as is, never
    rewritten, and grants nothing beyond what it names: the same portal and
    redirect hosts, with the coordinator's own path scopes made explicit:
    ``/documents/`` on each portal host, and on each redirect host only object
    keys under ``/<portal slug>/`` (the slug is the portal host's first label).
    Any key outside the known format, a mirror list that disagrees, or a host
    in both lists is refused.
    """
    if set(data) - LEGACY_KEYS or data.get("enabled") is not True:
        raise ValueError()
    expiry = _expiry(data["expires_utc"], now)
    if "expires_at" in data and data["expires_at"] != data["expires_utc"]:
        raise ValueError()
    lists = {}
    for key in ("portal_hosts", "redirect_hosts"):
        values = data[key]
        if not isinstance(values, list) or len(set(values)) != len(values):
            raise ValueError()
        mirror = data.get("approved_" + key, values)
        if not isinstance(mirror, list) or sorted(mirror) != sorted(values):
            raise ValueError()
        lists[key] = frozenset(hostname(x) for x in values)
    portals, redirects = lists["portal_hosts"], lists["redirect_hosts"]
    if not portals or portals & redirects:
        raise ValueError()
    slugs = sorted({host.split(".", 1)[0] for host in portals})
    scopes = {host: ("/documents/",) for host in portals}
    scopes.update({host: tuple("/" + slug + "/" for slug in slugs) for host in redirects})
    for host, prefixes in scopes.items():
        for prefix in prefixes:
            if url_parts("https://" + host + prefix).path != prefix:
                raise ValueError()
    return Approval(expiry, portals, redirects, scopes)


def parse_approval(raw, *, uid, mode, now):
    if uid != 0 or mode & 0o022 or not stat.S_ISREG(mode):
        raise PortalError("approval_not_admin_owned")
    try:
        data = strict_json(raw)
        if isinstance(data, dict) and data.get("purpose") == LEGACY_PURPOSE:
            return _legacy_approval(data, now)
        if set(data) != {'version','enabled','purpose','expires_utc','portal_hosts','redirect_hosts','path_prefixes'} or type(data.get('version')) is not int or data.get("version") != 1 or data.get("enabled") is not True or data.get("purpose") != "portal-document-retrieval":
            raise ValueError()
        expiry = datetime.fromisoformat(data["expires_utc"].replace("Z", "+00:00"))
        if expiry.tzinfo is None or now.tzinfo is None or expiry <= now:
            raise PortalError("approval_expired")
        for name in ('portal_hosts','redirect_hosts'):
            if not isinstance(data[name],list) or len(data[name]) != len(set(data[name])):
                raise ValueError()
        portals = frozenset(hostname(x) for x in data["portal_hosts"])
        redirects = frozenset(hostname(x) for x in data["redirect_hosts"])
        if portals & redirects:
            raise ValueError()
        scopes = data["path_prefixes"]
        if not portals or not isinstance(scopes, dict) or set(scopes) != portals | redirects:
            raise ValueError()
        for host, prefixes in scopes.items():
            if not isinstance(prefixes, list) or not prefixes:
                raise ValueError()
            for prefix in prefixes:
                if not isinstance(prefix, str) or not prefix.startswith("/") or not prefix.endswith("/"):
                    raise ValueError()
                if url_parts("https://" + host + prefix).path != prefix:
                    raise ValueError()
        return Approval(expiry, portals, redirects, {h: tuple(v) for h, v in scopes.items()})
    except (KeyError, TypeError, ValueError, AttributeError):
        raise PortalError("approval_invalid") from None


def load_approval(path, now=None):
    now = now or datetime.now(timezone.utc)
    try:
        path = safe_ancestors(path)
        parent = path.parent.stat()
        if parent.st_uid != 0 or parent.st_mode & 0o022:
            raise PortalError("approval_parent_not_admin_owned")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            before = os.fstat(fd)
            raw = os.read(fd, 65537)
            after = os.fstat(fd)
            if len(raw) > 65536 or (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
                raise PortalError("approval_changed_or_oversize")
            return parse_approval(raw, uid=before.st_uid, mode=before.st_mode, now=now)
        finally:
            os.close(fd)
    except OSError:
        raise PortalError("approval_missing_or_unsafe") from None
