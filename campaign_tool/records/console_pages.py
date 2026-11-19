"""Registry for trusted, in-package read-only console pages.

A console_page_<name>.py exports PATHS (a nonempty tuple of exact GET paths),
NAV (None or (label, integer order), linking PATHS[0]), and serve(handler) -> bool.
The callback can read the pinned request state and use _send/_fail. It receives
handler.path unchanged, including any query. There is no write or POST hook.
"""
import importlib
import pkgutil
import re
from pathlib import Path
from types import MappingProxyType

# Reserve the built-in GET and POST handlers, plus the edge smoke's sentinel
# paths. Keep this in sync when the built-in chain or edge inventory changes.
EXISTING_PATHS = frozenset({
    "/", "/agency", "/automation", "/board", "/calendar", "/calendar.ics",
    "/dashboard", "/documents", "/findings/review", "/jobs", "/manual-event",
    "/next-steps", "/ocr-review", "/oversight", "/packets", "/public-updates",
    "/redactions", "/review", "/send", "/settings", "/site-prs",
    "/approvals/approve", "/approvals/edit", "/approvals/phone-approve", "/approvals/reject", "/phone-approve.js",
    "/findings/review/approve", "/findings/review/reject", "/message-links/relink",
    "/ocr-review/confirm", "/ocr-review/reject", "/packets/approve", "/packets/reject",
    "/public-updates/approve-line", "/public-updates/kill", "/public-updates/off",
    "/public-updates/retract", "/redactions/signoff",
    "/healthz", "/does-not-exist", "/%2e%2e/etc/passwd",
})
_NAME = re.compile(r"console_page_[a-z][a-z0-9_]*")
_PATH = re.compile(r"/[A-Za-z0-9._~-]+(?:/[A-Za-z0-9._~-]+)*/?")
_OTHER_METHOD = re.compile(r"(?:do_)?(?:POST|PUT|PATCH|DELETE|HEAD|OPTIONS|TRACE|CONNECT)(?:_PATHS)?", re.I)


def _discover():
    directory = Path(__file__).resolve().parent
    for _, name, is_package in sorted(pkgutil.iter_modules([str(directory)]), key=lambda info: info.name):
        if not is_package and _NAME.fullmatch(name):
            source = directory / (name + ".py")
            if source.is_file() and not source.is_symlink():
                yield importlib.import_module(f"{__package__}.{name}")


def _load():
    routes, nav = {}, []
    for page in _discover():
        def invalid(reason):
            return ValueError(f"Invalid console page {page.__name__}: {reason}")

        paths = getattr(page, "PATHS", None)
        callback = getattr(page, "serve", None)
        if not callable(callback):
            raise invalid("serve must be callable")
        if "METHODS" in vars(page) or any(_OTHER_METHOD.fullmatch(name) for name in vars(page)):
            raise invalid("only GET exports are supported")
        if not isinstance(paths, tuple) or not paths:
            raise invalid("PATHS must be a nonempty tuple of GET paths")
        for path in paths:
            if not isinstance(path, str):
                raise invalid("PATHS must contain exact GET paths")
            if path in EXISTING_PATHS:
                raise invalid(f"reserved path {path}")
            if not _PATH.fullmatch(path) or {".", ".."}.intersection(path.split("/")):
                raise invalid("PATHS must contain exact GET paths")
            if path in routes:
                raise invalid(f"duplicate path {path}")
            routes[path] = callback
        if not hasattr(page, "NAV"):
            raise invalid("NAV must be exported")
        item = page.NAV
        if item is not None:
            if (not isinstance(item, tuple) or len(item) != 2 or not isinstance(item[0], str)
                    or not item[0].strip() or not item[0].isprintable() or type(item[1]) is not int):
                raise invalid("NAV must be None or a printable label and integer order")
            label, order = item
            nav.append((order, label, paths[0]))
    return MappingProxyType(routes), tuple((label, path) for _, label, path in sorted(nav))


# Validate all modules before the panel can bind; do not discover per request.
_ROUTES, _NAV = _load()


def nav_links():
    """Registered (label, path) rows, ordered by (order, label, path)."""
    return _NAV


def serve(handler) -> bool:
    """Dispatch an exact GET path without decoding or passing it to other pages."""
    callback = _ROUTES.get(handler.path.partition("?")[0])
    return False if callback is None else callback(handler)
