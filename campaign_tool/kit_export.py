"""kit-status --csv: the hand-sent request tracker as a private spreadsheet file (0600, never overwritten).

Reads ``kit_track.status`` only; nothing is sent or published from this module."""
import csv
import os
import stat
from pathlib import Path

from . import kit_track

HEADER = ("agency_id", "name", "sent_on", "channel", "provisional_determination_due", "provisional_with_extension",
          "extended", "closed", "steps", "last_step_on", "last_step_kind", "linked_records",
          "draft_changed_since_sent", "fee_estimate", "fee_state", "fee_paid", "request_url", "owner")
CHAIN_HEADER = ("follows", "covers_through")  # added only when a selected request has chain or coverage data
FORMULA_START = ("=", "+", "-", "@", "\t", "\r")  # a spreadsheet would run these as a formula


def formula_safe(text):
    """``text`` with a leading ``'`` when a spreadsheet would run it as a formula (shared with the site's open-data CSVs)."""
    return "'" + text if text.startswith(FORMULA_START) else text


def _cell(value):
    text = "" if value is None else (("yes" if value else "no") if isinstance(value, bool) else str(value))
    return formula_safe(text)


def _cells(row, chain=False):
    step = row["last_step"] or {}
    return [_cell(value) for value in (
        row["agency_id"], row["name"], row["sent_on"], row["channel"], row["determination_due"],
        row["with_extension"], row["extended"], row["closed"], row["steps"], step.get("on"), step.get("kind"),
        row["records"], row["draft_changed_since_sent"], row["fee_estimate"], row["fee_state"], row["fee_paid"],
        row.get("request_url"), row.get("owner"), *((row.get(key) for key in CHAIN_HEADER) if chain else ()))]


def _target(root, out):
    """The resolved output path; every rule is checked before anything is written."""
    out = Path(out)
    if out.suffix != ".csv":
        raise ValueError(f"--csv {out}: the file name must end in .csv")
    parent = out.parent.resolve()
    if not parent.is_dir():
        raise ValueError(f"--csv {out}: folder {out.parent} does not exist")
    target = parent / out.name
    root = Path(root)
    for folder in ("public", "content"):
        for base in {(root / folder).resolve(), root.resolve() / folder}:
            if target == base or target.is_relative_to(base):
                raise ValueError(f"--csv {out}: must not be inside the campaign's {folder}/ folder; "
                                 "this file is private")
    if os.path.lexists(target):
        raise ValueError(f"--csv {out}: exists; choose a new file name")
    return target


_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK
_PARENT_ERROR = "csv output parent changed or cannot be opened; export not confirmed"


def _open_parent(path):
    """Descriptor of ``path``'s parent directory, walked component by component without following any link.

    Read-only: nothing is created or changed. Every intermediate descriptor is closed (all of them, even after a
    close error); the returned one belongs to the caller. A missing, linked or non-directory component raises
    ValueError before anything is written."""
    names = [part for part in os.path.abspath(path).split(os.sep) if part][:-1]
    held = []
    try:
        held.append(os.open(os.sep, _DIR_FLAGS))
        for name in names:
            held.append(os.open(name, _DIR_FLAGS, dir_fd=held[-1]))
    except OSError:
        _close_all(held)
        raise ValueError(_PARENT_ERROR) from None
    except BaseException:
        _close_all(held)
        raise
    last = held.pop()
    try:
        _close_all(held)
    except OSError:
        _close_all([last])
        raise ValueError(_PARENT_ERROR) from None
    except BaseException:
        _close_all([last])
        raise
    return last


def _close_all(fds):
    """Close every descriptor; the first OSError is raised after all have been tried."""
    first = None
    for fd in fds:
        try:
            os.close(fd)
        except OSError as error:
            first = first or error
    if first is not None:
        raise first


def _same_dir(fd, identity):
    info = os.fstat(fd)
    return (info.st_dev, info.st_ino) == identity


def write_rows(target, out, header, rows):
    """Write header + rows (already formula-safe cells) to the new private file ``target``: 0600, O_EXCL.

    The parent directory is acquired once without following links; creation, verification and cleanup are
    anchored to that descriptor. A parent that no longer is the admitted directory at the end is refused. Bounded:
    not a confinement sandbox (an already-open directory moved elsewhere, hardlinks, mounts are out of scope)."""
    parent_fd = _open_parent(target)
    name = os.path.basename(os.fspath(target))
    fd = None
    identity = None  # (st_dev, st_ino) of the file this call created, taken from its descriptor
    try:
        info = os.fstat(parent_fd)
        parent_identity = (info.st_dev, info.st_ino)
        try:
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent_fd)
        except FileExistsError:
            raise ValueError(f"--csv {out}: exists; choose a new file name") from None
        created = os.fstat(fd)
        identity = (created.st_dev, created.st_ino)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            fd = None
            writer = csv.writer(handle, lineterminator="\r\n")
            writer.writerow(header)
            writer.writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())
        # The stream is closed. Re-walk the canonical parent (metadata only) and require the admitted directory,
        # then confirm the name still is the regular file this call created (lstat, no follow, no read).
        # One observation, not an atomic check-and-return, and no byte proof.
        try:
            check_fd = _open_parent(target)
            try:
                confirmed = _same_dir(check_fd, parent_identity)
            finally:
                os.close(check_fd)
            if confirmed:
                final = os.lstat(name, dir_fd=parent_fd)
                confirmed = stat.S_ISREG(final.st_mode) and (final.st_dev, final.st_ino) == identity
        except (OSError, ValueError):
            confirmed = False
        if not confirmed:
            raise ValueError("csv output changed or cannot be verified; export not confirmed") from None
    except BaseException:
        # Best-effort cleanup, subordinate to the original failure, relative to the admitted parent only. Remove the
        # name only while it still is the regular file this call created (lstat, no follow); a replacement,
        # symlink, directory or absent name is left alone. Not an atomic compare-and-unlink.
        try:
            if fd is not None:
                os.close(fd)
        except OSError:
            pass
        try:
            if identity is not None:
                current = os.lstat(name, dir_fd=parent_fd)
                if stat.S_ISREG(current.st_mode) and (current.st_dev, current.st_ino) == identity:
                    os.unlink(name, dir_fd=parent_fd)
        except OSError:
            pass
        try:
            os.close(parent_fd)
        except OSError:
            pass
        raise
    try:
        os.close(parent_fd)
    except OSError:
        raise ValueError("csv output changed or cannot be verified; export not confirmed") from None


def status_csv(root, out, today=None, law_base=None, *, request=None, owner=None):
    """Write one CSV row per recorded request (kit-status order) to a new private file; return a summary.

    The store and every filter are read before the file is created, so a refusal leaves no CSV behind."""
    target = _target(root, out)
    report = kit_track.status(root, today, law_base, request=request, owner=owner)
    chain = any(key in row for row in report["requests"] for key in CHAIN_HEADER)
    write_rows(target, out, HEADER + (CHAIN_HEADER if chain else ()), [_cells(row, chain) for row in report["requests"]])
    return {"written": str(target), "rows": len(report["requests"]),
            "law_package_status": report["law_package_status"], "note": report["note"]}
