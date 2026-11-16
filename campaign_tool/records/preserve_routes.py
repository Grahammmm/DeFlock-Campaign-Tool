"""Clearing routes for PRESERVE_PENDING originals (DF0652-07): read-only, metadata only.

``records holds --routes --mail-history DIR`` labels every unpreserved original with one fixed route and the
fixed next-step guidance for it. Nothing here writes, creates, moves, approves or clears anything, and no path,
file name, address, full hash or exception text leaves this module: callers get a route name from ``ROUTES``.

R1 a stable content-addressed mail copy (same sha256 and size) exists in ``MAIL/objects``.
R2 a legacy local occurrence in a received / received-user-files / portal-intake / productions class.
R2s a legacy local occurrence in an archive or research class (supplement-root preparation).
R3 a retained-import workbook marked ``pending trusted validation`` in the ledger.
R4 a json or csv original that has occurrence evidence (metadata class; G scope decision).
R5 no occurrence, no retained storage locator and no mail copy in the supplied evidence (G scope decision).
unavailable the evidence cannot be read or is unclassifiable; never read as R5.
"""
import hashlib
import os
import re
import stat

ROUTES = ("R1", "R2", "R2s", "R3", "R4", "R5", "unavailable")
UNAVAILABLE = "unavailable"
_MANIFEST = (
    "python3 -B -m campaign_tool.records.legacy_adoption manifest --root ROOT --config CFG --out OUT; "
    "python3 -B -m campaign_tool.records.legacy_adoption --root ROOT --config CFG --manifest MANIFEST "
    "--policy POLICY --report-sources")
COMMANDS = {
    "R1": ("python3 -B -m campaign_tool.records adopt-mail --root ROOT --mail-history MAIL --config CFG "
           "--write-resolution-policy-proposal OUT --from-inventory INV --approval-record APPROVAL"),
    "R2": _MANIFEST,
    "R2s": "prepare approved supplement source; " + _MANIFEST,
    "R3": "custodian trusted validation required; no clearing command",
    "R4": "G scope decision",
    "R5": "G scope decision",
    UNAVAILABLE: "route evidence unavailable; retry read-only listing with accessible mail history",
}
R2_CLASSES = frozenset({"received", "received-user-files", "portal-intake", "productions"})
R2S_CLASSES = frozenset({"archive", "research"})
MARKER = "pending trusted validation"
MAX_LEGACY_ROWS = 20000
_MALFORMED = object()


def clearing_command(route):
    return COMMANDS[route if route in COMMANDS else UNAVAILABLE]


def route_counts(routes):
    """Fixed-order totals with every key present, zeroes included."""
    counts = dict.fromkeys(ROUTES, 0)
    for route in routes:
        counts[route if route in counts else UNAVAILABLE] += 1
    return counts


# --- mail content-addressed objects (read only, descriptor relative, no symlink followed) --------------------

def _open_dir(path):
    """A directory descriptor for ``path`` reached through no symlink component, or None."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        names = [n for n in os.path.abspath(os.fspath(path)).split("/") if n]
        fd = os.open("/", flags)
        for name in names:
            try:
                nxt = os.open(name, flags, dir_fd=fd)
            finally:
                os.close(fd)
            fd = nxt
        return fd
    except (OSError, TypeError, ValueError):
        return None


class MailObjects:
    """``lookup(sha, size)`` is ``found``, ``absent`` (a definite no-copy) or ``unknown``."""

    def __init__(self, mail_history):
        self.fd = None
        root = _open_dir(mail_history) if mail_history is not None else None
        if root is not None:
            try:
                self.fd = os.open("objects", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                                  dir_fd=root)
            except OSError:
                self.fd = None
            finally:
                os.close(root)

    @property
    def inspectable(self):
        return self.fd is not None

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def lookup(self, sha, size):
        if self.fd is None or not re.fullmatch(r"[0-9a-f]{64}", sha) or type(size) is not int or size < 0:
            return "unknown"
        try:
            fd = os.open(sha, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=self.fd)
        except FileNotFoundError:
            return "absent"
        except OSError:
            return "unknown"
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or before.st_size != size:
                return "unknown"
            digest, seen = hashlib.sha256(), 0
            while seen <= size:
                chunk = os.read(fd, min(1 << 20, size + 1 - seen))
                if not chunk:
                    break
                digest.update(chunk)
                seen += len(chunk)
            after = os.fstat(fd)
            same = all(getattr(before, k) == getattr(after, k) for k in ("st_ino", "st_dev", "st_size", "st_mtime_ns"))
            return "found" if same and seen == size and digest.hexdigest() == sha else "unknown"
        except OSError:
            return "unknown"
        finally:
            os.close(fd)


# --- ledger metadata ------------------------------------------------------------------------------------------

def _components(value):
    """Lexical components of a stored root or path, or None when it is malformed."""
    if type(value) is not str or not value.strip() or any(ord(c) < 32 or ord(c) == 127 for c in value):
        return None
    parts = [p for p in value.split("/") if p not in ("", ".")]
    return None if ".." in parts else tuple(parts)


def _local(root, path, key):
    r, p = _components(root), _components(path)
    return _MALFORMED if r is None or p is None or not p else (str(root), str(path), key, r, p)


def _canonical_occurrences(snapshot, subject):
    from .ledger.occurrence_projection import decode
    found = []
    for row in snapshot._query("SELECT id,source_ref FROM occurrences WHERE original_sha256=? "
                               "AND acquisition_method='captured_legacy_local'", (subject,)):
        try:
            ref = decode(row["source_ref"])
        except (TypeError, ValueError):
            ref = None
        if (type(ref) is not dict or ref.get("scheme") != "legacy_filesystem_observation_v1"
                or ref.get("original_sha256") not in (None, subject)):
            found.append(_MALFORMED)
        else:
            found.append(_local(ref.get("root"), ref.get("path"), str(row["id"])))
    return found


def _legacy_index(snapshot):
    """``{sha256: [local occurrence | _MALFORMED]}`` from the retained legacy occurrence rows; the key
    ``None`` collects rows whose subject cannot be told."""
    from .ledger.occurrence_projection import decode, decode_source, valid_sha
    index = {}
    try:
        total = snapshot._query("SELECT COUNT(*) AS n FROM legacy_rows WHERE source_table='occurrences'")[0]["n"]
    except Exception:
        return {None: [_MALFORMED]}
    if total > MAX_LEGACY_ROWS:
        return {None: [_MALFORMED]}
    for source in snapshot._query("SELECT import_id,source_rowid,identity_json,payload_json,payload_sha256 "
                                  "FROM legacy_rows WHERE source_table='occurrences' ORDER BY import_id,source_rowid"):
        key = f"legacy:{source['import_id']}:{source['source_rowid']}"
        try:
            row = decode_source(source)
        except (TypeError, ValueError):
            try:
                sha = decode(source["identity_json"]).get("sha")
            except (TypeError, ValueError, AttributeError):
                sha = None
            index.setdefault(sha if valid_sha(sha) else None, []).append(_MALFORMED)
            continue
        sha = row.get("sha")
        if not valid_sha(sha):
            index.setdefault(None, []).append(_MALFORMED)
            continue
        try:
            receipt = decode(row["receipt"]) if row.get("receipt") is not None else {}
        except (TypeError, ValueError):
            receipt = None
        if type(receipt) is not dict:
            index.setdefault(sha, []).append(_MALFORMED)
        elif "mail_identity" in receipt or "portal_identity" in receipt or row.get("parent") not in (None, ""):
            continue  # not a local filesystem occurrence
        else:
            index.setdefault(sha, []).append(_local(row.get("root"), row.get("path"), key))
    return index


def _class_of(occurrence):
    """The route class of a local occurrence from whole path components, or None."""
    _, _, _, root, path = occurrence
    for parts in (root, path[:-1]):
        hits = {p for p in parts if p in R2_CLASSES or p in R2S_CLASSES}
        if hits:
            if hits <= R2_CLASSES:
                return "R2"
            return "R2s" if hits <= R2S_CLASSES else None
    return None


class Classifier:
    """Context manager: ``route(snapshot, subject)`` for one pending original inside the caller's snapshot."""

    def __init__(self, mail_history):
        self.mail = MailObjects(mail_history)
        self._legacy = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.mail.close()
        return False

    def route(self, snapshot, subject):
        try:
            return self._route(snapshot, subject)
        except Exception:  # fixed outcome only: never a message
            return UNAVAILABLE

    def _route(self, snapshot, subject):
        if not self.mail.inspectable:
            return UNAVAILABLE
        rows = snapshot._query("SELECT bytes,storage_path,preservation_status,legacy_format FROM originals "
                               "WHERE sha256=?", (subject,))
        if len(rows) != 1:
            return UNAVAILABLE
        original = rows[0]
        state = self.mail.lookup(subject, original["bytes"])
        if state == "found":
            return "R1"
        if state != "absent":
            return UNAVAILABLE
        stage = snapshot._query("SELECT reason FROM stage_state WHERE original_sha256=? AND stage='preserve'",
                                (subject,))
        text = " ".join(" ".join(str(v or "") for v in (original["preservation_status"],
                                                        stage[0]["reason"] if stage else "")).lower().split())
        stored = original["storage_path"]
        if stored is not None:
            return "R3" if original["legacy_format"] == "xlsx" and MARKER in text else UNAVAILABLE
        if self._legacy is None:
            self._legacy = _legacy_index(snapshot)
        found = _canonical_occurrences(snapshot, subject) + self._legacy.get(subject, []) + self._legacy.get(None, [])
        if any(o is _MALFORMED for o in found):
            return UNAVAILABLE
        found = sorted({(o[3], o[4]): o for o in found}.values(), key=lambda o: o[:3])
        if not found:
            return "R5"
        if original["legacy_format"] in ("json", "csv"):
            return "R4"
        return _class_of(found[0]) or UNAVAILABLE
