"""Named campaign register: names and directories only, never campaign data (docs/MULTI-CAMPAIGN.md).

`remove` only unregisters. The register is one 0600 file outside the repository; an advisory lock on that file itself
serializes its read-modify-write, held only for that transaction: never in a campaign, nor while a campaign is read.
"""
import argparse
import contextlib
import errno
import fcntl
import json
import os
import re
import stat
import sys
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ENV = "DEFLOCK_CAMPAIGN_REGISTER"
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,39}")
KEYS = {"name", "directory", "added_at"}
MAX_BYTES = 1 << 20  # RF1025-6: raw register bytes read, and exact serialized bytes written; a byte budget, not a sandbox


def _within(path, other):
    return path == other or other in path.parents


def register_path():
    given = os.environ.get(ENV)
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "deflock-campaign" / "campaigns.json"
    path = Path(os.path.realpath(Path(given).expanduser() if given else base))
    if _within(path, REPO):
        raise ValueError("the register must live outside the repository")
    return path


@contextlib.contextmanager
def _transaction(path):
    """Advisory flock on the register file; once held, confirm the path still names the locked inode."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    while True:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            if os.path.samestat(os.fstat(fd), os.stat(path)):  # not replaced while we waited
                yield
                return
        finally:
            os.close(fd)  # closing releases the lock


def _read_bounded(path):
    """The register's raw bytes from one admitted regular-file descriptor, at most MAX_BYTES; None when absent."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as exc:
        if exc.errno == errno.ELOOP:  # a symlink (including a dangling one) is never followed
            raise ValueError("the register is not a regular file") from None
        raise
    try:
        info = os.fstat(fd)  # the descriptor actually read, not an earlier pathname stat
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("the register is not a regular file")
        if info.st_size > MAX_BYTES:
            raise ValueError("the register is larger than 1 MiB")
        chunks, total = [], 0
        while total <= MAX_BYTES:  # MAX_BYTES + 1 bytes at most: the extra byte catches growth after the fstat
            chunk = os.read(fd, MAX_BYTES + 1 - total)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        if total > MAX_BYTES:
            raise ValueError("the register is larger than 1 MiB")
        return b"".join(chunks)
    finally:
        os.close(fd)


def _load(path):
    raw = _read_bounded(path)
    if raw is None:
        return []
    entries = json.loads(raw.decode("utf-8") or "[]")
    if not isinstance(entries, list) or not all(
            isinstance(e, dict) and set(e) == KEYS and all(isinstance(v, str) for v in e.values()) for e in entries):
        raise ValueError("the register must hold only a list of {name, directory, added_at}")
    return entries


def _store(path, entries):
    """Same-directory staging file, then an atomic replace. The stage is a fresh, exclusively created 0600 file (mkstemp):
    a pre-existing entry, such as a hardlink at `<register>.tmp`, is never opened, truncated, chmodded or consumed. Only
    this call's own stage is ever removed, and only when it did not become the register. The exact serialized bytes are
    measured before the stage exists: a register the bounded reader would refuse is never installed."""
    text = json.dumps(entries, indent=2) + "\n"
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise ValueError("the register would be larger than 1 MiB; register not updated")
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        try:
            out = os.fdopen(fd, "w", encoding="utf-8")
        except BaseException:
            os.close(fd)
            raise
        with out:
            os.fchmod(out.fileno(), 0o600)
            out.write(text)
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, path)
    except BaseException:
        with contextlib.suppress(OSError):  # the primary error wins; cleanup is best effort
            os.unlink(name)
        raise


def _root(path):
    """The absolute, normalised root of a campaign directory; refused unless it is a real, separate campaign."""
    path = Path(path).expanduser()
    if path.is_symlink() or not path.is_dir():
        raise ValueError("the campaign directory is a symlink, missing or not a directory")
    root = Path(os.path.realpath(path))
    if _within(root, REPO) or _within(REPO, root):
        raise ValueError("the campaign directory overlaps the repository")
    from .cli import read_config
    try:
        read_config(root)
    except (OSError, ValueError):
        raise ValueError("the directory has no readable campaign.json") from None
    return root


def _unknown(name, entries):
    return f"unknown campaign {name!r}; registered: " + (", ".join(sorted(e["name"] for e in entries)) or "(none)")


def add(name, directory):
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise ValueError("a campaign name is 1-40 letters, digits, . _ or -, starting with a letter or digit")
    root, path = _root(directory), register_path()
    with _transaction(path):
        entries = _load(path)
        if any(e["name"] == name for e in entries):
            raise ValueError(f"a campaign named {name!r} is already registered")
        for e in entries:
            if _within(root, Path(e["directory"])) or _within(Path(e["directory"]), root):
                raise ValueError(f"that directory is the same as, inside or around the one registered as {e['name']!r}")
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        _store(path, entries + [{"name": name, "directory": str(root), "added_at": stamp}])
    return {"name": name, "directory": str(root)}


def remove(name):  # unregisters only: the campaign directory is never read, moved or deleted
    path = register_path()
    with _transaction(path):
        entries = _load(path)
        if not any(e["name"] == name for e in entries):
            raise ValueError(_unknown(name, entries))
        _store(path, [e for e in entries if e["name"] != name])


def resolve(name):
    """Name to (the campaign's absolute root, every registered directory); the register is read once, unlocked."""
    entries = _load(register_path())
    entry = next((e for e in entries if e["name"] == name), None)
    if entry is None:
        raise ValueError(_unknown(name, entries))
    return _root(entry["directory"]), [e["directory"] for e in entries]


# Isolation: with --campaign A (and while `campaigns status` reads A) no path under any other registered campaign may be
# opened, listed, written or renamed, whatever a symlink, a `..` or a path inside campaign.json points at.
_PATH_EVENTS = {"open": 1, "os.listdir": 1, "os.scandir": 1, "os.mkdir": 1, "os.remove": 1, "os.rmdir": 1, "os.chmod": 1,
                "os.truncate": 1, "os.utime": 1, "sqlite3.connect": 1, "shutil.copytree": 2, "os.rename": 2, "os.link": 2,
                "shutil.copyfile": 2, "shutil.move": 2}
_denied, _hooked = (), False


def _guard(event, args):
    for arg in args[:_PATH_EVENTS.get(event, 0)] if _denied else ():
        if isinstance(arg, (str, bytes, os.PathLike)):
            target = Path(os.path.realpath(os.path.abspath(os.fsdecode(arg))))
            if any(_within(target, Path(d)) for d in _denied):
                raise PermissionError("a command for one campaign may not reach another registered campaign")


def confine(root, registered):  # release() is confine("", ())
    global _denied, _hooked
    if not _hooked:
        sys.addaudithook(_guard)
        _hooked = True
    _denied = tuple(os.path.realpath(d) for d in registered if os.path.realpath(d) != str(root))


def release():
    confine("", ())


def _state(root):
    """(requests, open, waiting-send, waiting-publish, law status) from the campaign's own recorded files, read-only."""
    from . import findings, kit_track
    sent = kit_track.status(root)
    rows, drafts = sent["requests"], root / "kit" / "requests"
    stems = {kit_track._stem(r["agency_id"], r["request"]) for r in rows}
    waiting_send = sum(p.stem not in stems and p.is_file() and not p.is_symlink()
                       for p in (sorted(drafts.glob("*.md")) if drafts.is_dir() and not drafts.is_symlink() else []))
    last = [(r["last_step"] or {}).get("kind") for r in rows]
    opened = sum(not (r["closed"] or "withdrawn_on" in r or kind == "denied" or (kind == "produced" and not r["producing"]))
                 for r, kind in zip(rows, last))
    found = findings.status_rows(root)
    if any("error" in f for f in found):
        raise ValueError("an unreadable finding file")
    return len(rows), opened, waiting_send, sum(f["state"] == "ready" for f in found), sent["law_package_status"]


def status_lines():
    entries = _load(register_path())  # read once; no lock is held while any campaign is read
    registered = [e["directory"] for e in entries]
    lines, current = [], True
    for entry in sorted(entries, key=lambda e: e["name"]):
        try:
            confine(entry["directory"], registered)  # before _root reads campaign.json
            root = _root(entry["directory"])
            confine(root, registered)
            requests, opened, send, publish, law = _state(root)
            text = f"requests={requests} open={opened} waiting-send={send} waiting-publish={publish} law={law}"
        except Exception as exc:  # one broken campaign must not hide the others; the type name only, never its text
            text, current = f"NOT CURRENT: {type(exc).__name__}", False
        finally:
            release()
        lines.append(f"campaign | {entry['name']} | {text} | private; review-required")
    return (lines or ["campaign | (none registered) | private; review-required"]), current


def today_rows(as_of):
    """DF1109-15: every registered campaign's `today` items in name order, each keeping its own rank, with
    ``--campaign NAME`` selectors; ``(rows, current)``. Each campaign is read alone, confined exactly as for
    ``campaigns status`` (and already before its campaign.json is read); a campaign that cannot be read, or whose
    `today` names an unreadable source, is one type-only ``not_current`` row, its partial items dropped. Read-only."""
    from . import today
    entries = _load(register_path())  # read once; no lock is held while any campaign is read
    registered = [e["directory"] for e in entries]
    rows, current = [], True
    for entry in sorted(entries, key=lambda e: e["name"]):
        name = entry["name"]
        try:
            confine(entry["directory"], registered)  # before _root reads campaign.json
            root = _root(entry["directory"])
            confine(root, registered)
            items = today.todo(root, as_of, campaign_name=name)
            if any(item["kind"] == "unreadable" for item in items):
                raise ValueError("a source of this campaign could not be read")  # normalised type; text never shown
            found = [{"campaign": name, **item} for item in items] or [{"campaign": name, "_nothing": True}]
        except Exception as exc:  # one broken campaign must not hide the others; the type name only, never its text
            found, current = [{"campaign": name, "rank": None, "kind": "not_current",
                               "what": f"NOT CURRENT: {type(exc).__name__}", "due": None, "command": None}], False
        finally:
            release()
        rows.extend(found)
    return rows, current


def today_text(rows, day):
    if not rows:
        return "campaign | (none registered) | private; review-required"
    return "\n".join(f"campaign | {row['campaign']} | " + (
        f"Nothing due on {day.isoformat()}." if row.get("_nothing") else row["what"] if row["kind"] == "not_current"
        else f"{row['rank']}. [{row['kind']}] {row['what']} — {row['command']}") + " | private; review-required"
        for row in rows)


class CampaignAction(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        try:
            root, registered = resolve(values)
        except (OSError, ValueError) as exc:
            parser.error(str(exc))
        namespace.directory, namespace.campaign, namespace.registered = str(root), values, registered


def run(args):
    """Run a `campaigns` verb; returns the exit code."""
    try:
        if args.campaigns_command == "add":
            done = add(args.name, args.directory)
            print(f"campaign | {done['name']} | registered {done['directory']} | private; nothing was copied")
        elif args.campaigns_command == "remove":
            remove(args.name)
            print(f"campaign | {args.name} | unregistered; the directory and its data were not touched")
        elif args.campaigns_command == "list":
            for e in sorted(_load(register_path()), key=lambda e: e["name"]):
                print(f"campaign | {e['name']} | {e['directory']} | added {e['added_at']}")
        elif args.campaigns_command == "today":  # DF1109-15: the date is checked, and fixed once, before any read
            from .kit_track import _day
            day = date.today() if args.as_of is None else _day(args.as_of)
            if day is None:
                raise ValueError("--as-of must be a real YYYY-MM-DD date")
            rows, current = today_rows(day)
            print(json.dumps([r for r in rows if not r.get("_nothing")], indent=2, ensure_ascii=False)
                  if args.as_json else today_text(rows, day))
            return 0 if current else 3
        else:
            lines, current = status_lines()
            print("\n".join(lines))
            return 0 if current else 3
    except (OSError, ValueError) as exc:
        print(f"Stopped: {exc}", file=sys.stderr)
        return 1
    return 0
