"""Private request owners (DF1103-05): a teammate's label per recorded request in kit/assignments.json (0600).

An owner is an alias the volunteer team chose, not a verified identity. Assigning never changes a due date, a response
step or any other store, and nothing is sent or published from this module.
"""
import contextlib
import errno
import json
import os
import secrets
import stat
import unicodedata
from pathlib import Path

from . import kit, kit_track

LABEL_MAX = 40
MAX_BYTES = 1 << 20
DIGITS_MAX = 6  # seven or more decimal digits make a label phone-like
FILE = "kit/assignments.json"
LEAF = "assignments.json"
TEMP_TRIES = 8
_DIR_FD = os.open in os.supports_dir_fd


def owner_label(value, flag="--to"):
    """The normalized owner label (whitespace collapsed, case kept, 1-40 characters) or ValueError naming ``flag``.

    The refused text is never echoed. No email address, phone number, markup or control character is accepted."""
    if not isinstance(value, str) or any(c in "<>" or unicodedata.category(c).startswith("C") for c in value):
        raise ValueError(f"{flag} must be a plain-text label without <, > or control characters")
    text = " ".join(value.split())
    if not text or len(text) > LABEL_MAX:
        raise ValueError(f"{flag} must be 1-{LABEL_MAX} characters after collapsing whitespace")
    if "@" in unicodedata.normalize("NFKC", text):
        raise ValueError(f"{flag} is a teammate's label, not an email address")
    if sum(c.isdecimal() for c in text) > DIGITS_MAX:
        raise ValueError(f"{flag} is a teammate's label, not a phone number")
    return text


def _pairs(pairs):
    keys = [k for k, _ in pairs]
    if len(set(keys)) != len(keys):
        raise ValueError("duplicate key")
    return dict(pairs)


def _kit_dir(root):
    kit_dir = Path(root) / "kit"
    if kit_dir.is_symlink():
        raise ValueError("kit/ must not be a symlink")
    return kit_dir


def _entry_ok(key, label):
    parts = kit_track._split(key) if isinstance(key, str) else None
    return bool(parts and kit.AGENCY_ID.match(parts[0]) and key == kit_track._key(*parts)
                and isinstance(label, str) and _label_ok(label))


def _label_ok(label):
    try:
        return owner_label(label) == label
    except ValueError:
        return False


def _bad(reason):
    return ValueError(f"{FILE} unreadable: {reason}")


@contextlib.contextmanager
def _open_kit(root):
    """Yield a descriptor for the campaign's kit/ directory, or None when root or kit/ does not exist.

    kit/ is opened relative to the root descriptor without following a symlink, so every later assignments read,
    temporary, replacement and cleanup stays in this one directory even if kit/ is renamed or replaced afterwards.
    The caller's root and its ancestors keep their existing trust. No unsafe fallback without the needed flags."""
    needed = ("O_DIRECTORY", "O_NOFOLLOW", "O_NONBLOCK")
    if not all(hasattr(os, n) for n in needed) or not _DIR_FD:
        raise _bad("file changed or could not be read")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        root_fd = os.open(root, flags & ~os.O_NOFOLLOW)  # the root and its ancestors keep link semantics
    except FileNotFoundError:
        yield None
        return
    except OSError:
        raise _bad("file changed or could not be read") from None
    kit_fd = None
    try:
        try:
            kit_fd = os.open("kit", flags, dir_fd=root_fd)
        except FileNotFoundError:
            pass
        except OSError:
            try:
                linked = stat.S_ISLNK(os.stat("kit", dir_fd=root_fd, follow_symlinks=False).st_mode)
            except OSError:
                linked = False
            if linked:
                raise ValueError("kit/ must not be a symlink") from None
            raise _bad("file changed or could not be read") from None
        if kit_fd is not None and not stat.S_ISDIR(os.fstat(kit_fd).st_mode):
            raise _bad("file changed or could not be read")
    except BaseException:
        if kit_fd is not None:
            os.close(kit_fd)
        raise
    finally:
        os.close(root_fd)
    try:
        yield kit_fd
    finally:
        if kit_fd is not None:
            os.close(kit_fd)


def _read_leaf(kit_fd, bad):
    """Up to MAX_BYTES + 1 bytes of the regular file opened once, nonblocking and without following a symlink."""
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_NONBLOCK"):  # no unsafe fallback
        raise bad("file changed or could not be read")
    try:
        fd = os.open(LEAF, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=kit_fd)
    except OSError as exc:
        if exc.errno == errno.ELOOP:  # a symlink, dangling or not, swapped in after the early checks
            raise bad("not a regular file") from None
        raise bad("file changed or could not be read") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise bad("not a regular file")
        if info.st_size > MAX_BYTES:
            raise bad("larger than 1 MiB")
        handle = os.fdopen(fd, "rb")  # on success the stream owns the descriptor
    except OSError:
        os.close(fd)
        raise bad("file changed or could not be read") from None
    except BaseException:  # includes the ValueError refusals above, and MemoryError from the constructor
        os.close(fd)
        raise
    try:
        with handle:
            return handle.read(MAX_BYTES + 1)
    except OSError:
        raise bad("file changed or could not be read") from None


def _load(kit_fd):
    """The validated assignments from the leaf in the admitted kit directory; {} when kit/ or the leaf is absent."""
    if kit_fd is None:
        return {}
    bad = _bad
    try:
        early = os.stat(LEAF, dir_fd=kit_fd, follow_symlinks=False)
    except FileNotFoundError:
        return {}
    except OSError:
        raise bad("file changed or could not be read") from None
    if not stat.S_ISREG(early.st_mode):
        raise bad("not a regular file")
    if early.st_size > MAX_BYTES:
        raise bad("larger than 1 MiB")
    # The early checks only refuse sooner. The bytes come from the object opened here relative to the admitted
    # kit descriptor, without following a leaf symlink, and re-checked on its own descriptor.
    raw = _read_leaf(kit_fd, bad)
    if len(raw) > MAX_BYTES:
        raise bad("larger than 1 MiB")
    try:
        document = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs)
    except (UnicodeDecodeError, ValueError):  # json.JSONDecodeError and the duplicate-key error are ValueErrors
        raise bad("not JSON, or a key is repeated") from None
    except RecursionError:  # the decoder's own refusal of excessive nesting: fixed and value-free
        raise bad("JSON nesting is too deep") from None
    if not isinstance(document, dict) or set(document) != {"schema_version", "assignments"} \
            or type(document["schema_version"]) is not int or document["schema_version"] != 1 \
            or not isinstance(document["assignments"], dict):
        raise bad("expected schema_version 1 and an assignments object only")
    for key, label in document["assignments"].items():
        if not _entry_ok(key, label):
            raise bad("an assignment is malformed")
    return document["assignments"]


def read_assignments(root):
    """``{sent.json key: owner label}`` from kit/assignments.json; {} when absent. Never resets a bad file.

    kit/ is opened once without following a symlink and the leaf is read relative to it. Reads at most MAX_BYTES + 1
    bytes before decoding. A document the JSON decoder refuses for excessive nesting (RecursionError) is reported as
    the fixed "JSON nesting is too deep" unreadable store. Orphans (a key with no recorded request) are kept as
    stored; callers ignore them."""
    with _open_kit(root) as kit_fd:
        return _load(kit_fd)


def assign(root, agency_id, *, request="main", to=None, clear=False):
    """Set (``to``) or remove (``clear``) the owner of one recorded request; return what is stored afterwards.

    Everything is checked before anything is written; a repeat of the stored state writes nothing."""
    if (to is None) == (not clear):
        raise ValueError("give exactly one of --to and --clear")
    request = kit_track._request(request)
    label = None if clear else owner_label(to, "--to")
    root = Path(root)
    kit_dir = _kit_dir(root)
    agencies = kit_dir / "agencies.json"
    if not agencies.is_file() or agencies.is_symlink():
        raise ValueError("run kit first: kit/agencies.json is missing")
    if not isinstance(agency_id, str) or not any(a["agency_id"] == agency_id for a in kit._load_existing(agencies)):
        raise ValueError("--agency is not in kit/agencies.json")
    key = kit_track._key(agency_id, request)
    if key not in kit_track.read_sent(root):
        raise ValueError(f"no recorded request {kit_track._label(agency_id, request)}: "
                         "owners are assigned to requests recorded with kit-sent or kit-sent-import")
    with _open_kit(root) as kit_fd:
        if kit_fd is None:
            raise _bad("file changed or could not be read")
        stored = _load(kit_fd)
        result = {"agency_id": agency_id, "request": request, "owner": label}
        if stored.get(key) == label:  # same owner, or clearing a request that has none: nothing to write
            return {**result, "changed": False}
        updated = {k: v for k, v in stored.items() if k != key}
        if label is not None:
            updated[key] = label
        document = {"schema_version": 1, "assignments": dict(sorted(updated.items()))}
        data = (json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        if len(data) > MAX_BYTES:
            raise ValueError(f"{FILE} would be larger than 1 MiB; owner not recorded")
        # A private temporary and the atomic replace, both relative to the admitted kit descriptor.
        for _ in range(TEMP_TRIES):
            temp = f".{LEAF}.{secrets.token_hex(8)}.tmp"
            try:
                fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=kit_fd)
                break
            except FileExistsError:
                continue
            except OSError:
                raise _bad("file changed or could not be read") from None
        else:
            raise _bad("file changed or could not be read")
        try:
            try:
                handle = os.fdopen(fd, "wb")
            except BaseException:
                os.close(fd)
                raise
            with handle:
                handle.write(data)
            os.replace(temp, LEAF, src_dir_fd=kit_fd, dst_dir_fd=kit_fd)
        except BaseException:
            try:
                os.unlink(temp, dir_fd=kit_fd)  # only our own temporary, in the admitted directory
            except OSError:
                pass  # keep the original failure
            raise
    return {**result, "changed": True}
