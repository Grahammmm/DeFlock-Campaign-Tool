"""Read-only check that a campaign's private files stay out of git (docs/FORK-AND-RUN.md steps 2 and 6).

Nothing here stages, commits, rewrites or edits a .gitignore: git is only asked read-only questions.
"""
import os
import shutil
import subprocess
import tomllib
from pathlib import Path

PROTECTED = ("kit", "private", "public")
PROBE = ".df-git-probe"
TIMEOUT = 5
MAX_ENTRIES = 5000
NO_GIT = "git is not installed"
ENGINE_MESSAGE = ("Campaign directory must be outside the engine checkout; choose a sibling directory such as "
                  "../my-campaign. Nothing was written.")
NOT_CHECKED_NEXT = ("Rerun doctor --checklist once git can read this work tree; "
                    "do not commit campaign files until checked.")
NO_GIT_NEXT = "Install git and rerun doctor --checklist; do not commit campaign files until checked."
TRACKED_NOTE = (".gitignore does not untrack existing files; review tracked files manually before any push.")


class GitExposureError(ValueError):
    """The campaign directory is inside the engine checkout."""


class _Unchecked(Exception):
    """Something prevented a trustworthy answer; the row must say 'not checked'."""


def engine_checkout_root():
    """Source root of the running engine (campaign_tool/cli.py plus the project's pyproject.toml), else None."""
    try:
        root = Path(__file__).resolve().parent.parent
        if not (root / "campaign_tool" / "cli.py").is_file():
            return None
        with (root / "pyproject.toml").open("rb") as handle:
            if tomllib.load(handle).get("project", {}).get("name") != "deflock-campaign-tool":
                return None
        return root
    except (OSError, ValueError):  # tomllib.TOMLDecodeError is a ValueError
        return None


def _inside(path, root):
    return path == root or root in path.parents


def refuse_engine_directory(directory):
    """Raise GitExposureError before anything is written when directory is the engine checkout or inside it."""
    engine = engine_checkout_root()
    if engine is None:
        return
    try:
        resolved = Path(directory).resolve(strict=False)
        engine = engine.resolve(strict=False)
    except (OSError, RuntimeError):
        raise GitExposureError(ENGINE_MESSAGE) from None
    if _inside(resolved, engine):
        raise GitExposureError(ENGINE_MESSAGE)


def _find_marker(resolved):
    """True when a .git directory or worktree .git file exists at or above resolved, else False."""
    for folder in (resolved, *resolved.parents):
        marker = folder / ".git"
        try:
            mode = os.lstat(marker).st_mode
        except FileNotFoundError:
            continue
        except OSError:
            raise _Unchecked() from None
        if os.path.isdir(marker) and not os.path.islink(marker):
            return True
        if mode & 0o170000 != 0o100000:
            raise _Unchecked()
        try:
            with marker.open("rb") as handle:
                if not handle.read(512).startswith(b"gitdir:"):
                    raise _Unchecked()
        except OSError:
            raise _Unchecked() from None
        return True
    return False


def _env():
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0")
    return env


def _git(git, cwd, args, data=None):
    try:
        done = subprocess.run([git, "-C", str(cwd), *args], input=data, capture_output=True, timeout=TIMEOUT,
                              shell=False, env=_env(), check=False)
    except (OSError, subprocess.SubprocessError, ValueError):
        raise _Unchecked() from None
    return done.returncode, done.stdout


def _nearest_existing(path):
    for folder in (path, *path.parents):
        if folder.is_dir():
            return folder
    raise _Unchecked()


def _descendants(base, rel):
    """Existing paths below base as work-tree-relative strings; a symlink or an oversized tree is unchecked."""
    found, stack = [], [(base, rel)]
    while stack:
        folder, folder_rel = stack.pop()
        try:
            with os.scandir(folder) as entries:
                for entry in entries:
                    child = f"{folder_rel}/{entry.name}"
                    if entry.is_symlink():
                        raise _Unchecked()
                    if entry.is_dir(follow_symlinks=False):
                        found.append(child + "/")
                        stack.append((Path(entry.path), child))
                    else:
                        found.append(child)
                    if len(found) > MAX_ENTRIES:
                        raise _Unchecked()
        except OSError:
            raise _Unchecked() from None
    return found


def _exposure(resolved):
    """(names, tracked) from PROTECTED: names unignored or tracked, tracked = names with tracked files."""
    git = shutil.which("git")
    if git is None:
        raise _Unchecked(NO_GIT)
    code, out = _git(git, _nearest_existing(resolved), ["rev-parse", "--is-inside-work-tree", "--show-toplevel"])
    try:
        lines = out.decode("utf-8").splitlines() if code == 0 else []
    except UnicodeDecodeError:
        lines = []
    if len(lines) != 2 or lines[0] != "true":
        raise _Unchecked()
    top = Path(lines[1]).resolve()
    if not _inside(resolved, top):
        raise _Unchecked()
    prefix = resolved.relative_to(top).as_posix()
    prefix = "" if prefix == "." else prefix + "/"
    inputs = {}
    for name in PROTECTED:
        rel = prefix + name
        paths = [rel + "/", rel + "/" + PROBE]
        target = resolved / name
        if target.is_symlink():
            raise _Unchecked()
        if target.is_dir():
            paths += _descendants(target, rel)
        elif os.path.lexists(target):
            paths.append(rel)
        inputs[name] = paths
    every = [p for paths in inputs.values() for p in paths]
    code, out = _git(git, top, ["check-ignore", "--no-index", "-z", "--stdin"],
                     ("\0".join(every) + "\0").encode("utf-8"))
    if code not in (0, 1):
        raise _Unchecked()
    ignored = set(out.decode("utf-8").split("\0")) - {""}
    exposed = {name for name, paths in inputs.items() if any(p not in ignored for p in paths)}
    code, out = _git(git, top, ["ls-files", "--cached", "-z", "--", *[":(literal)" + prefix + n for n in PROTECTED]])
    if code != 0:
        raise _Unchecked()
    tracked = {name for name in PROTECTED for path in out.decode("utf-8").split("\0")
               if path.startswith(prefix + name + "/")}
    return [n for n in PROTECTED if n in exposed | tracked], tracked


def _fix_text(names):
    listed = [f"/{n}/" for n in names]
    if len(listed) == 1:
        return f"Add {listed[0]} as a line in the campaign directory's .gitignore, then rerun doctor --checklist."
    return (f"Add {', '.join(listed[:-1])} and {listed[-1]} as separate lines in the campaign directory's "
            f".gitignore, then rerun doctor --checklist.")


def _row(state, label, next_=None):
    return {"key": "git_exposure", "state": state, "label": label, "next": next_,
            "owner": "organizer" if state == "todo" else None}


def git_exposure_row(directory):
    """One checklist row, or None when the campaign is outside the engine checkout and outside any git work tree."""
    engine = engine_checkout_root()
    in_engine, note, missing, names, tracked = False, None, False, [], set()
    try:
        resolved = Path(directory).resolve(strict=False)
        if engine is not None:
            in_engine = _inside(resolved, engine.resolve(strict=False))
        if not _find_marker(resolved) and not in_engine:
            return None
        names, tracked = _exposure(resolved)
    except _Unchecked as exc:
        missing = str(exc) == NO_GIT
        note = NO_GIT_NEXT if missing else NOT_CHECKED_NEXT
    except (OSError, RuntimeError, ValueError):
        note = NOT_CHECKED_NEXT
    if in_engine:
        text = "Move the campaign outside the engine checkout (docs/FORK-AND-RUN.md step 2)."
        if names:
            text += " Also exposed: " + ", ".join(f"{n}/" for n in names) + ". " + _fix_text(names)
            text += " " + TRACKED_NOTE if tracked else ""
        elif note:
            text += " " + note
        return _row("todo", "Campaign is inside the engine checkout", text)
    if note:
        return _row("todo", "Git exposure: not checked" + (" (git is not installed)" if missing else ""), note)
    if names:
        text = f"Affected: {', '.join(f'{n}/' for n in names)}. {_fix_text(names)}"
        return _row("todo", "Campaign files may be included in git", text + (" " + TRACKED_NOTE if tracked else ""))
    return _row("ok", "Campaign kit/, private/ and public/ are ignored by git")
