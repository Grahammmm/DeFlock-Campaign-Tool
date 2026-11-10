"""DF1109B-07: ``content elsewhere add|remove|list`` maintain ``content/elsewhere.json`` without hand-editing.

The file is the DF1107-04 one the site build reads: a JSON array of 1-100 organizer-verified decisions by other
governments. Every change validates the whole prospective array with the build's own ``content_elsewhere.load_elsewhere``
(its literal place + body + decided_on + kind duplicate rule included) and the shared privacy lint on every text field,
then writes once in ``content/`` (a temporary file, fsync, one rename); a refusal leaves the old bytes and mode.

* list prints each entry's 1-based STORED position, the N ``meetings-stage --elsewhere N`` takes (never the page order).
* add appends one entry; the first add creates the file (0644). Legal-claim wording is an advisory note, never a refusal.
* remove drops the entry at ``--index N``. It names every staged ``content/meetings/*.json`` that cites an entry with the
  same place, body and date (any kind) and says later positions shift; staged kits are never rewritten. Removing the last
  entry deletes the file (a present ``[]`` is refused by the loader), returning to the no-file state.

Nothing is fetched or built: a URL is the organizer's record of where they read the decision. Refusals name the flag or
stored place and a category, never a supplied value. One organizer writes at a time; there is no concurrent-writer promise.
"""
import json
import os
import re
import stat
from datetime import date
from pathlib import Path

from .content import MAX_JSON_BYTES, ContentError, load_meeting
from .content_elsewhere import load_elsewhere
from .privacy_patterns import refusal

FILE = "elsewhere.json"
LABEL = "content/" + FILE
UNTOUCHED = "nothing was written"
INDEX_SHAPE = re.compile(r"[1-9][0-9]{0,8}", re.ASCII)
INDEX_RULE = "--index: must be a whole number of 1 or more (stored order)"
DONE = "Content only; nothing was fetched, built, sent or deployed."
TRIPLE = ("place", "body", "decided_on")  # removal warnings: broader than the four-field citation identity on purpose


class ElsewhereError(ContentError):
    """The decisions file, or a requested change to it, is not acceptable; the message never repeats a supplied value."""


class _Duplicate(ValueError):
    pass


# --- reading (anchored at content/, links never followed) ------------------------------------------------------

def _content_fd(root):
    try:
        return os.open(Path(root) / "content", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        raise ElsewhereError(f"content/ must be a real folder of this campaign (links are never followed); {UNTOUCHED}") from None


def _read(folder_fd, name, label, single_link=False):
    """-> (bytes, identity, mode) of one regular file, or None when absent; a link, FIFO, folder, extra hard link (with
    ``single_link``) or oversized file is refused unread."""
    try:
        info = os.stat(name, dir_fd=folder_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError:
        raise ElsewhereError(f"{label}: cannot be inspected; {UNTOUCHED}") from None
    if stat.S_ISLNK(info.st_mode):
        raise ElsewhereError(f"{label}: is a symbolic link; links are never followed; {UNTOUCHED}")
    if not stat.S_ISREG(info.st_mode) or (single_link and info.st_nlink != 1):
        raise ElsewhereError(f"{label}: must be a regular file" + (" with a single link" if single_link else "")
                             + f"; {UNTOUCHED}")
    if info.st_size > MAX_JSON_BYTES:
        raise ElsewhereError(f"{label}: file too large; {UNTOUCHED}")
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=folder_fd)
        with os.fdopen(fd, "rb") as handle:
            opened = os.fstat(handle.fileno())
            data = handle.read(MAX_JSON_BYTES + 1)
    except OSError:
        raise ElsewhereError(f"{label}: cannot be read; {UNTOUCHED}") from None
    if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
        raise ElsewhereError(f"{label}: changed while it was being read; {UNTOUCHED}")
    if len(data) > MAX_JSON_BYTES:
        raise ElsewhereError(f"{label}: file too large; {UNTOUCHED}")
    identity = (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns, opened.st_nlink)
    return data, identity, stat.S_IMODE(opened.st_mode)


def _hook(pairs):
    seen = {}
    for key, value in pairs:
        if key in seen:
            raise _Duplicate(key)
        seen[key] = value
    return seen


def _parse(data, label):
    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=_hook)
    except _Duplicate:
        raise ElsewhereError(f"{label}: a JSON object repeats a key; {UNTOUCHED}") from None
    except (ValueError, RecursionError):
        raise ElsewhereError(f"{label}: not valid UTF-8 JSON; {UNTOUCHED}") from None


def _validate(document, today):
    """The whole array through the build's loader, then the shared privacy lint on every text field of every entry."""
    try:
        rows = load_elsewhere(document, today=today)
    except ContentError as exc:  # the loader's unknown-key list would repeat stored text: name the category only
        raise ElsewhereError(re.sub(r"unknown field\(s\) .*", "unknown field(s)", str(exc)) + f"; {UNTOUCHED}") from None
    for n, row in enumerate(rows):
        for key, value in row.items():
            if found := refusal(f"{FILE}[{n}].{key}", value):
                raise ElsewhereError(f"{found}; {UNTOUCHED}")
    return rows


def _state(root, today):
    """-> (validated rows or None when the file is absent, loaded (bytes, identity, mode) or None)."""
    folder = _content_fd(root)
    try:
        loaded = _read(folder, FILE, LABEL, single_link=True)
    finally:
        os.close(folder)
    if loaded is None:
        return None, None
    return _validate(_parse(loaded[0], LABEL), today), loaded


def _citing(root, removed, today):
    """Names of staged ``content/meetings/*.json`` (lexical) whose snapshots share the removed place, body and date.
    Each file is validated as a meeting (snapshots included) without resolving references; a broken one is refused."""
    folder = _content_fd(root)
    try:
        try:
            meetings_fd = os.open("meetings", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=folder)
        except FileNotFoundError:
            return []
        except OSError:
            raise ElsewhereError(f"content/meetings: must be a real folder (links are never followed); {UNTOUCHED}") from None
    finally:
        os.close(folder)
    try:
        names, wanted = [], tuple(removed[key] for key in TRIPLE)
        for name in sorted(os.listdir(meetings_fd)):
            if name.startswith(".") or not name.endswith(".json"):
                continue
            label = f"content/meetings/{name}"
            loaded = _read(meetings_fd, name, label)
            if loaded is None:
                raise ElsewhereError(f"{label}: changed while it was being read; {UNTOUCHED}")
            try:
                meeting = load_meeting(_parse(loaded[0], label), "meetings/" + name, today=today)
            except ContentError as exc:
                raise ElsewhereError(f"{exc}; {UNTOUCHED}") from None
            if any(tuple(snap[key] for key in TRIPLE) == wanted for snap in meeting.get("cited_elsewhere", ())):
                names.append("meetings/" + name)
        return names
    except OSError:
        raise ElsewhereError(f"content/meetings: cannot be read; {UNTOUCHED}") from None
    finally:
        os.close(meetings_fd)


# --- writing ---------------------------------------------------------------------------------------------------

def _dump(rows):
    """The exact bytes to store, refused over the loader's byte cap before any temporary file exists."""
    try:
        data = (json.dumps(rows, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    except (UnicodeError, ValueError):
        raise ElsewhereError(f"{LABEL}: an entry cannot be stored as UTF-8 text; {UNTOUCHED}") from None
    if len(data) > MAX_JSON_BYTES:
        raise ElsewhereError(f"{LABEL}: would be larger than {MAX_JSON_BYTES // 1024} KiB; {UNTOUCHED}")
    return data


def _unchanged(folder, loaded):
    current = _read(folder, FILE, LABEL, single_link=True)
    if (current is None) != (loaded is None) or (current is not None and current[:2] != loaded[:2]):
        raise FileExistsError


def _install(root, data, loaded):
    """Stage ``data`` beside the file, fsync, then publish it with one rename (a no-replace link for the first file).
    The target must still be the very file read (or still absent); every ordinary failure keeps the old bytes."""
    folder = _content_fd(root)
    temp = f".elsewhere-{os.getpid()}-{os.urandom(4).hex()}.tmp"
    staged = committed = False
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=folder)
        staged = True
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), 0o644 if loaded is None else loaded[2])
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        _unchanged(folder, loaded)
        if loaded is None:
            os.link(temp, FILE, src_dir_fd=folder, dst_dir_fd=folder, follow_symlinks=False)  # never replaces
        else:
            os.replace(temp, FILE, src_dir_fd=folder, dst_dir_fd=folder)
            staged = False
        committed = True
        os.fsync(folder)
    except FileExistsError:
        raise ElsewhereError(f"{LABEL}: changed while it was being edited; {UNTOUCHED}") from None
    except OSError:
        if committed:
            raise ElsewhereError(f"{LABEL}: written, but the folder could not be flushed to disk; check it with "
                                 "content elsewhere list") from None
        raise ElsewhereError(f"{LABEL}: write failed; the previous file was kept") from None
    finally:
        if staged:
            try:
                os.unlink(temp, dir_fd=folder)
            except OSError:
                pass
        os.close(folder)


def _remove_file(root, loaded):
    """Unlink the very file read: the commit point of removing the last entry (an empty array is never written)."""
    folder = _content_fd(root)
    try:
        _unchanged(folder, loaded)
        os.unlink(FILE, dir_fd=folder)
    except FileExistsError:
        raise ElsewhereError(f"{LABEL}: changed while it was being edited; {UNTOUCHED}") from None
    except OSError:
        raise ElsewhereError(f"{LABEL}: could not be removed; the previous file was kept") from None
    finally:
        os.close(folder)


# --- commands --------------------------------------------------------------------------------------------------

def parse_index(text):
    """The 1-based stored position typed on the command line (ASCII digits only; no sign, space, zero or huge token)."""
    if not isinstance(text, str) or not INDEX_SHAPE.fullmatch(text):
        raise ElsewhereError(INDEX_RULE)
    return int(text)


def add_entry(root, *, place, state, body, decided_on, kind, as_stated, source_url, checked_on, news_url=None,
              today=None):
    """Append one organizer-verified decision. -> {"index" (1-based stored position), "notes" (advisory, stderr)}."""
    from .legal_claims import elsewhere_notes
    today = today or date.today()
    rows, loaded = _state(root, today)
    rows = rows or []
    new = {"place": place, "state": state, "body": body, "decided_on": decided_on, "kind": kind, "as_stated": as_stated,
           "source_url": source_url, "checked_on": checked_on, **({"news_url": news_url} if news_url is not None else {})}
    prospective = _validate(rows + [new], today)
    _install(root, _dump(prospective), loaded)
    at = f"{FILE}[{len(rows)}]."
    notes = [note for note in elsewhere_notes(prospective) if at in note or "guard-error" in note]
    return {"index": len(prospective), "notes": notes}


def remove_entry(root, index, *, today=None):
    """Drop the entry at the 1-based stored ``index``. -> {"index", "citing" (staged meeting files), "last" (file gone)}."""
    if type(index) is not int or index < 1:
        raise ElsewhereError(INDEX_RULE)
    today = today or date.today()
    rows, loaded = _state(root, today)
    if loaded is None:
        raise ElsewhereError(f"{LABEL}: does not exist; there is nothing to remove")
    if index > len(rows):
        raise ElsewhereError(f"--index: choose 1 to {len(rows)} (stored order)")
    citing = _citing(root, rows[index - 1], today)
    rest = rows[:index - 1] + rows[index:]
    if rest:
        _install(root, _dump(_validate(rest, today)), loaded)
    else:
        _remove_file(root, loaded)
    return {"index": index, "citing": citing, "last": not rest}


def list_entries(root, *, today=None):
    """[{"index", "place", "body", "decided_on", "kind", "checked_on"}] in stored order; None without a file."""
    rows, _ = _state(root, today or date.today())
    if rows is None:
        return None
    return [{"index": n, **{key: row[key] for key in ("place", "body", "decided_on", "kind", "checked_on")}}
            for n, row in enumerate(rows, 1)]


def run_cli(root, args):
    """-> (stdout text, advisory stderr notes) for one ``content elsewhere`` verb (date.today(); no override)."""
    verb = args.elsewhere_command
    if verb == "list":
        rows = list_entries(root)
        if not rows:
            return "No decisions elsewhere recorded.", []
        lines = ["index | place | body | decided_on | kind | checked_on"]
        lines += [" | ".join(str(row[key]) for key in ("index", "place", "body", "decided_on", "kind", "checked_on"))
                  for row in rows]
        return "\n".join(lines), []
    if verb == "add":
        done = add_entry(root, place=args.place, state=args.state, body=args.body, decided_on=args.decided_on,
                         kind=args.kind, as_stated=args.as_stated, source_url=args.source_url, checked_on=args.checked_on,
                         news_url=args.news_url)
        return f"Elsewhere entry added: index {done['index']}.\n{DONE}", done["notes"]
    done = remove_entry(root, parse_index(args.index))
    lines = [f"Elsewhere entry removed: index {done['index']}."]
    lines += [f"Warning: {name} cites the same place, body and date; review and restage with meetings-stage --replace."
              for name in done["citing"]] or ["No staged meetings cite the same place, body and date."]
    if done["last"]:
        lines.append(f"{LABEL} removed; no decisions elsewhere remain.")
    lines += ["Later stored positions shift down by one; run content elsewhere list before choosing --elsewhere again.",
              DONE]
    return "\n".join(lines), []
