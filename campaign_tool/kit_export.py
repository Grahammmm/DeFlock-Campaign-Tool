"""kit-status --csv: the hand-sent request tracker as a private spreadsheet file (0600, never overwritten).

Reads ``kit_track.status`` only; nothing is sent or published from this module."""
import csv
import os
from pathlib import Path

from . import kit_track

HEADER = ("agency_id", "name", "sent_on", "channel", "provisional_determination_due", "provisional_with_extension",
          "extended", "closed", "steps", "last_step_on", "last_step_kind", "linked_records",
          "draft_changed_since_sent")
FORMULA_START = ("=", "+", "-", "@", "\t", "\r")  # a spreadsheet would run these as a formula


def _cell(value):
    text = "" if value is None else ("yes" if value else "no") if isinstance(value, bool) else str(value)
    return "'" + text if text.startswith(FORMULA_START) else text


def _cells(row):
    step = row["last_step"] or {}
    return [_cell(value) for value in (
        row["agency_id"], row["name"], row["sent_on"], row["channel"], row["determination_due"],
        row["with_extension"], row["extended"], row["closed"], row["steps"], step.get("on"), step.get("kind"),
        row["records"], row["draft_changed_since_sent"])]


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


def status_csv(root, out, today=None, law_base=None, *, request=None):
    """Write one CSV row per recorded request (kit-status order) to a new private file; return a summary."""
    target = _target(root, out)
    report = kit_track.status(root, today, law_base, request=request)
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        raise ValueError(f"--csv {out}: exists; choose a new file name") from None
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            fd = None
            writer = csv.writer(handle, lineterminator="\r\n")
            writer.writerow(HEADER)
            for row in report["requests"]:
                writer.writerow(_cells(row))
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        if fd is not None:
            os.close(fd)
        os.unlink(target)  # the file this call created with O_EXCL
        raise
    return {"written": str(target), "rows": len(report["requests"]),
            "law_package_status": report["law_package_status"], "note": report["note"]}
