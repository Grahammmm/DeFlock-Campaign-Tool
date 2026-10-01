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


def parse_approval(raw, *, uid, mode, now):
    if uid != 0 or mode & 0o022 or not stat.S_ISREG(mode):
        raise PortalError("approval_not_admin_owned")
    try:
        data = json.loads(raw)
        if data.get("version") != 1 or data.get("enabled") is not True or data.get("purpose") != "portal-document-retrieval":
            raise ValueError()
        expiry = datetime.fromisoformat(data["expires_utc"].replace("Z", "+00:00"))
        if expiry.tzinfo is None or now.tzinfo is None or expiry <= now:
            raise PortalError("approval_expired")
        portals = frozenset(hostname(x) for x in data["portal_hosts"])
        redirects = frozenset(hostname(x) for x in data["redirect_hosts"])
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
