"""Read-only observation of the approval-anchor state recorded in a pinned console export (FLK-CON-1).

The export manifest (EXPORT.json) carries ``approval_anchor.state`` as observed when it was built. This
module only reads that one field from the already pinned export directory. It never touches the trust file,
keys, signatures or verification: verification still happens when decisions are applied. Only the exact
strings ``ok`` and ``unavailable`` are recognised; anything else, including an old export without the field,
is ``unknown`` and never looks ok. Supplied reasons and exception text are never returned.
"""
import os

from . import approval_queue as aq, console_export as ce, console_snapshot as snap

OK, UNAVAILABLE, UNKNOWN = "ok", "unavailable", "unknown"
EXPLANATION = "Approval anchor state from the pinned export; verification still occurs when decisions are applied."
NO_PIN = "Approval anchor state: unknown (no pinned export)."
_MESSAGES = {
    OK: "approvals verify",
    UNAVAILABLE: "approvals are recorded but DEFERRED until the trust anchor is available",
    UNKNOWN: "unknown — approvals are recorded but DEFERRED until the trust anchor is available",
}


def message(state):
    return _MESSAGES.get(state, _MESSAGES[UNKNOWN])


def _normalize(manifest):
    anchor = manifest.get("approval_anchor") if isinstance(manifest, dict) else None
    state = anchor.get("state") if isinstance(anchor, dict) else None
    return state if isinstance(state, str) and state in (OK, UNAVAILABLE) else UNKNOWN


def _from_pin_path(path):
    fd = None
    try:
        fd = aq._open_chain(path)
        raw = snap._read_file(fd, ce.MANIFEST, snap.MAX_MANIFEST)
        if raw is None:
            return UNKNOWN
        return _normalize(aq._strict_load(raw, "export_manifest_invalid"))
    except Exception:  # every failure is "unknown": never show detail
        return UNKNOWN
    finally:
        if fd is not None:
            os.close(fd)


def from_source(source):
    """State from the manifest of the request's own pinned export; unknown for live or unpinned sources."""
    pin = getattr(source, "pin", None)
    if not getattr(source, "snapshot", False) or not isinstance(getattr(pin, "path", None), str):
        return UNKNOWN
    return _from_pin_path(pin.path)


def from_export_root(root):
    """Pin the export root once with a fresh Pinner and read its manifest; unknown on any failure."""
    try:
        pin = snap.Pinner(root).pin()
    except Exception:
        return UNKNOWN
    return _from_pin_path(pin.path)


def pinned(source):
    return bool(getattr(source, "snapshot", False)) and getattr(source, "pin", None) is not None


def paragraph(state, is_pinned=True):
    """The fixed status sentence shown on Home and Settings (plain text; callers escape)."""
    if not is_pinned:
        return NO_PIN
    return "%s. %s" % (message(state), EXPLANATION)
