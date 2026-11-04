"""Named campaign register: names and directories only, never campaign data (docs/MULTI-CAMPAIGN.md).

`remove` only unregisters. The register is one 0600 file outside the repository; an advisory lock on that file itself
serializes its read-modify-write, held only for that transaction: never in a campaign, nor while a campaign is read.
"""
import argparse
import contextlib
import fcntl
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ENV = "DEFLOCK_CAMPAIGN_REGISTER"
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,39}")
KEYS = {"name", "directory", "added_at"}


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


def _load(path):
    if not os.path.lexists(path):
        return []
    if path.is_symlink() or not path.is_file():
        raise ValueError("the register is not a regular file")
    entries = json.loads(path.read_text(encoding="utf-8") or "[]")
    if not isinstance(entries, list) or not all(
            isinstance(e, dict) and set(e) == KEYS and all(isinstance(v, str) for v in e.values()) for e in entries):
        raise ValueError("the register must hold only a list of {name, directory, added_at}")
    return entries


def _store(path, entries):  # same-directory temp (reused under the lock), then an atomic replace
    temporary = path.with_name(path.name + ".tmp")
    with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600), "w",
                   encoding="utf-8") as out:
        os.fchmod(out.fileno(), 0o600)
        out.write(json.dumps(entries, indent=2) + "\n")
        out.flush()
        os.fsync(out.fileno())
    os.replace(temporary, path)


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
    opened = sum(not (r["closed"] or kind == "denied" or (kind == "produced" and not r["producing"]))
                 for r, kind in zip(rows, last))
    found = findings.status_rows(root)
    if any("error" in f for f in found):
        raise ValueError("an unreadable finding file")
    return len(rows), opened, waiting_send, sum(f["state"] == "ready" for f in found), sent["law_package_status"]


def status_lines():
    entries = _load(register_path())  # read once; no lock is held while any campaign is read
    lines, current = [], True
    for entry in sorted(entries, key=lambda e: e["name"]):
        try:
            root = _root(entry["directory"])
            confine(root, [e["directory"] for e in entries])
            requests, opened, send, publish, law = _state(root)
            text = f"requests={requests} open={opened} waiting-send={send} waiting-publish={publish} law={law}"
        except Exception as exc:  # one broken campaign must not hide the others; the type name only, never its text
            text, current = f"NOT CURRENT: {type(exc).__name__}", False
        finally:
            release()
        lines.append(f"campaign | {entry['name']} | {text} | private; review-required")
    return (lines or ["campaign | (none registered) | private; review-required"]), current


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
        else:
            lines, current = status_lines()
            print("\n".join(lines))
            return 0 if current else 3
    except (OSError, ValueError) as exc:
        print(f"Stopped: {exc}", file=sys.stderr)
        return 1
    return 0
