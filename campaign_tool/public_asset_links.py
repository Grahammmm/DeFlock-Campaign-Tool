"""DF0237-08: name the public-assets files that are copied to public/ but that no public page, source, brand setting or
redirect points at, and validate the optional ``site.json.unlinked_assets_ok`` acknowledgement list.

Pure functions: no filesystem traversal (beyond the existence check of ``validate_unlinked_assets_ok``), no network, no
writes. Nothing is deleted, moved or left uncopied; an acknowledgement is publication intent, not a privacy review.
"""
import stat
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

from .content import ASSET_PART, ContentError

MAX_ENTRIES = 50
LABEL = "site.json.unlinked_assets_ok"
SUFFIX = "published but not linked from any page"
_LINKS = {"a": ("href",), "area": ("href",), "link": ("href",), "img": ("src",), "script": ("src",), "source": ("src",),
          "video": ("src",), "audio": ("src",), "track": ("src",), "iframe": ("src",), "embed": ("src",)}
_SHARE_META = {"og:image", "twitter:image"}


def _canonical(value):
    """The parts of a plain public-assets relative path, or None."""
    if not isinstance(value, str):
        return None
    parts = value.split("/")
    if not parts or len(parts) > 8 or any(not ASSET_PART.fullmatch(part) for part in parts):
        return None
    if parts[0] == "public-assets":  # the report prefix, never part of the field value, even if such a subdirectory exists
        return None
    return parts


def validate_unlinked_assets_ok(value, assets_dir=None):
    """A fresh list of the acknowledged paths, or a ContentError naming ``site.json.unlinked_assets_ok[N]`` and a fixed
    reason (the supplied value is never shown). Without ``assets_dir`` only the syntax is checked."""
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_ENTRIES:
        raise ContentError(f"{LABEL}: use a list of 1 to {MAX_ENTRIES} paths inside content/public-assets/")
    seen = set()
    for n, entry in enumerate(value):
        label = f"{LABEL}[{n}]"
        parts = _canonical(entry)
        if parts is None:
            raise ContentError(f"{label}: use a plain relative path inside content/public-assets/ (no leading /, "
                               "public-assets/ prefix, dot, query, fragment or backslash)")
        if entry in seen:
            raise ContentError(f"{label}: repeats an earlier entry")
        seen.add(entry)
        if assets_dir is None:
            continue
        _inspect_chain(label, Path(assets_dir), parts)
    return list(value)


def _inspect_chain(label, assets_dir, parts):
    """Walk assets_dir -> leaf with lstat, stopping at the first failed component. Only metadata is read: no file is
    opened or resolved. An inspection failure other than 'absent' becomes a fixed refusal that carries no path,
    errno text or exception context; it is not a readability policy (a regular leaf whose metadata is visible passes)."""
    for depth in range(len(parts) + 1):
        step = assets_dir.joinpath(*parts[:depth])
        try:
            mode = step.lstat().st_mode
        except (FileNotFoundError, NotADirectoryError):
            raise ContentError(f"{label}: file not found in content/public-assets/") from None
        except OSError:
            raise ContentError(f"{label}: cannot inspect file in content/public-assets/") from None
        if stat.S_ISLNK(mode):
            raise ContentError(f"{label}: is or sits under a symlink; use a regular file in content/public-assets/")
    if not stat.S_ISREG(mode):
        raise ContentError(f"{label}: file not found in content/public-assets/")


def local_path(url, page, base_url=None):
    """The public-root-relative path ``url`` (found on output page ``page``) names on this site, or None."""
    if not isinstance(url, str):
        return None
    url = url.strip()
    if not url or "\\" in url or any(ord(c) < 32 for c in url):
        return None
    try:
        split = urlsplit(url)
        port = split.port
    except ValueError:
        return None
    if split.scheme:
        # Authority identity uses presence, not truthiness: empty userinfo is still userinfo,
        # and explicit port 0 is distinct from the default 443.
        if split.scheme.lower() != "https" or not base_url or split.username is not None or split.password is not None or not split.hostname:
            return None
        try:
            base = urlsplit(base_url.rstrip("/"))
            base_port = base.port
        except ValueError:
            return None
        if base.username is not None or base.password is not None or not base.hostname:
            return None
        if base.scheme.lower() != "https" or (split.hostname, 443 if port is None else port) != (base.hostname, 443 if base_port is None else base_port):
            return None
        prefix = base.path.rstrip("/") + "/"
        if not split.path.startswith(prefix):
            return None
        segments = split.path[len(prefix):].split("/")
    elif split.netloc:  # protocol-relative
        return None
    elif split.path.startswith("/"):
        segments = split.path[1:].split("/")
    else:
        segments = [*str(page).split("/")[:-1], *split.path.split("/")]
    if "%" in split.path:
        return None
    out = []
    for segment in segments:
        if segment == "..":
            if not out:
                return None
            out.pop()
        elif segment == ".":
            continue
        elif segment == "":
            return None
        else:
            out.append(segment)
    return "/".join(out) if out else None


class _Collector(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.urls = []

    def handle_starttag(self, tag, attrs):
        values = {name: value for name, value in attrs if value is not None}
        for name in _LINKS.get(tag, ()):
            if name in values:
                self.urls.append(values[name])
        if tag in ("img", "source") and "srcset" in values:
            for candidate in values["srcset"].split(","):
                parts = candidate.split()
                if parts:
                    self.urls.append(parts[0])
        if tag == "meta" and "content" in values:
            key = (values.get("property") or values.get("name") or "").strip().lower()
            if key in _SHARE_META:
                self.urls.append(values["content"])



def _html_text(payload):
    return payload.decode("utf-8", errors="replace") if isinstance(payload, (bytes, bytearray)) else str(payload)


def unlinked_asset_paths(files, asset_paths, *, base_url=None, public_urls=(), declared_assets=(), redirect_targets=()):
    """Sorted unique POSIX paths, relative to public-assets, of ``asset_paths`` that nothing in this build references.

    ``files``: ``{output name: str or bytes}``; only names ending ``.html`` are parsed (never archives or manifests).
    ``public_urls``: library ``public_url`` values; ``declared_assets``: brand logo/favicon/share image paths;
    ``redirect_targets``: validated redirect target URLs. Matching is on the full case-sensitive path."""
    assets = {Path(p).as_posix() for p in asset_paths}
    referenced = set()
    for name, payload in files.items():
        if not str(name).endswith(".html"):
            continue
        collector = _Collector()
        collector.feed(_html_text(payload))
        collector.close()
        referenced.update(filter(None, (local_path(url, name, base_url) for url in collector.urls)))
    referenced.update(filter(None, (local_path(url, "index.html", base_url) for url in public_urls)))
    referenced.update(filter(None, (local_path(url, "index.html", base_url) for url in redirect_targets)))
    referenced.update(Path(p).as_posix() for p in declared_assets)
    return sorted(assets - referenced)


def unlinked_lines(paths, acknowledged=()):
    """One ``public-assets/PATH: published but not linked from any page`` line per path not acknowledged."""
    skip = set(acknowledged)
    return [f"public-assets/{path}: {SUFFIX}" for path in sorted(set(paths)) if path not in skip]
