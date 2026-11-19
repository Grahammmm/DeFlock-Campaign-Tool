"""Read-only comparison of rendered versus installed user systemd units (``records units-diff``).

Compares every ``records-*`` and ``deflock-console-*`` unit file, plus each file of the
``deflockslo-daily-mail.service.d`` drop-in directory, of a rendered tree with the installed user unit directory
and prints one line per unit: MATCH, DRIFT (changed directive names only), MISSING (rendered, not installed) or
EXTRA (installed ``records-*`` unit without a rendered counterpart), plus what ``systemctl --user is-enabled``
reports. Nothing is written, copied, enabled, reloaded or started, a symlink is reported and never followed,
and no directive value (Environment=, EnvironmentFile=, ExecStart= ...), absolute path or exception text is
ever printed. Exit 0 when everything matches, 1 on any difference, 2 for unusable input.
"""
import argparse
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

UNIT = re.compile(r"(records|deflock-console)-[A-Za-z0-9_.@-]{1,80}\.(service|timer|path|socket|target)")
DROPIN_DIR = "deflockslo-daily-mail.service.d"
DROPIN_PARENT = "deflockslo-daily-mail.service"
DROPIN_FILE = re.compile(r"[A-Za-z0-9_.@-]{1,80}\.conf")
SECTION = re.compile(r"\[([A-Za-z0-9]{1,40})\]")
DIRECTIVE = re.compile(r"([A-Za-z][A-Za-z0-9-]{0,60})\s*=(.*)", re.S)
ENABLED_STATES = frozenset({"enabled", "enabled-runtime", "linked", "linked-runtime", "masked", "masked-runtime",
                            "static", "indirect", "disabled", "generated", "transient", "alias"})
MAX_UNIT_BYTES, MAX_ENTRIES = 1 << 20, 500
DEFAULT_INSTALLED = "~/.config/systemd/user"
UNREADABLE = object()


def is_enabled(unit):
    """Fixed word for ``systemctl --user is-enabled UNIT`` (the only call made), or ``unknown``."""
    try:
        done = subprocess.run(["systemctl", "--user", "is-enabled", unit], capture_output=True, text=True,
                              timeout=5)
    except (OSError, subprocess.SubprocessError, ValueError):
        return "unknown"
    word = done.stdout.strip() if isinstance(done.stdout, str) else ""
    return word if word in ENABLED_STATES else "unknown"  # `disabled` exits non-zero, so the code is not used


def scan(root):
    """{relative name: Path} of the compared names under ``root`` (a missing root is empty). Never follows links."""
    found = {}
    try:
        with os.scandir(root) as entries:
            for entry in entries:
                if UNIT.fullmatch(entry.name):
                    found[entry.name] = Path(entry.path)
                elif entry.name == DROPIN_DIR:
                    found[DROPIN_DIR] = Path(entry.path)  # judged below: a symlinked directory is never entered
                if len(found) > MAX_ENTRIES:
                    break
    except FileNotFoundError:
        return {}
    for name, path in list(found.items()):
        if name != DROPIN_DIR:
            continue
        del found[name]
        if path.is_symlink():
            found[name] = path
            continue
        try:
            with os.scandir(path) as entries:
                for entry in entries:
                    if DROPIN_FILE.fullmatch(entry.name):
                        found[f"{DROPIN_DIR}/{entry.name}"] = Path(entry.path)
        except OSError:
            found[name] = path  # reported as unreadable
    return found


def read(path):
    """File bytes, the string ``"symlink"`` for a link (never followed), or UNREADABLE."""
    try:
        if path.is_symlink():
            return "symlink"
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return UNREADABLE
            data = os.read(fd, MAX_UNIT_BYTES + 1)
        finally:
            os.close(fd)
    except OSError:
        return UNREADABLE
    return UNREADABLE if len(data) > MAX_UNIT_BYTES else data


def directives(data):
    """{(section, name): [values]} with continuation lines joined; raw non-directive text is only counted."""
    found, section, pending = {}, "", ""
    for line in data.decode("utf-8", "replace").splitlines():
        text = pending + line.strip()
        if text.endswith("\\"):
            pending = text[:-1] + " "
            continue
        pending = ""
        if not text or text[0] in "#;":
            continue
        head = SECTION.fullmatch(text)
        match = DIRECTIVE.fullmatch(text)
        if head:
            section = head.group(1)
        elif match:
            found.setdefault((section, match.group(1)), []).append(match.group(2).strip())
        else:
            found.setdefault((section, ""), []).append(text)
    return found


def changed(old, new):
    """Sorted directive names whose value lists differ; ``non-directive`` when only comments or layout do."""
    one, two = directives(old), directives(new)
    names = {key[1] for key in set(one) | set(two) if one.get(key) != two.get(key) and key[1]}
    if any(one.get(key) != two.get(key) for key in set(one) | set(two) if not key[1]):
        names.add("non-directive")
    return sorted(names) or ["non-directive"]


def compare(name, rendered, installed):
    """(status, note) for one relative name."""
    if rendered is None:
        return "EXTRA", ""
    if installed is None:
        return "MISSING", ""
    one, two = read(rendered), read(installed)
    for side, data in (("rendered", one), ("installed", two)):
        if data == "symlink":
            return "DRIFT", f"symlink ({side}, not followed)"
        if data is UNREADABLE:
            return "DRIFT", f"unreadable ({side})"
    if one == two:
        return "MATCH", ""
    return "DRIFT", "changed: " + ", ".join(changed(two, one))


def main(argv=None, enabled=is_enabled):
    parser = argparse.ArgumentParser(prog="records units-diff", description=__doc__)
    parser.add_argument("--rendered", required=True, help="rendered unit tree (deploy/records-runtime/render.py)")
    parser.add_argument("--installed", default=DEFAULT_INSTALLED, help="installed user unit directory "
                        "(default: %(default)s)")
    args = parser.parse_args(argv)
    rendered_root, installed_root = Path(args.rendered).expanduser(), Path(args.installed).expanduser()
    if rendered_root.is_symlink() or not rendered_root.is_dir():
        print("units-diff: --rendered is not a real directory", file=sys.stderr)
        return 2
    if installed_root.exists() and not installed_root.is_dir():
        print("units-diff: --installed is not a directory", file=sys.stderr)
        return 2
    try:
        rendered, installed = scan(rendered_root), scan(installed_root)
    except OSError:
        print("units-diff: a unit directory is unreadable", file=sys.stderr)
        return 2
    if not rendered:
        print("units-diff: nothing to compare: the rendered tree has no records-*, deflock-console-* or "
              "mail drop-in file", file=sys.stderr)
        return 2
    names = sorted(set(rendered) | {n for n in installed if n.startswith("records-")})
    states, bad = {}, False
    for name in names:
        # a symlinked (or unreadable) drop-in directory stands for each file inside it and is never entered
        link = DROPIN_DIR if name.startswith(DROPIN_DIR + "/") else None
        status, note = compare(name, rendered.get(name) or rendered.get(link),
                               installed.get(name) or installed.get(link))
        parent = DROPIN_PARENT if name.startswith(DROPIN_DIR) else name
        if parent not in states:
            states[parent] = enabled(parent)  # one is-enabled per parent unit; names are regex-checked by scan()
        bad = bad or status != "MATCH"
        print(f"{status} {name} enabled={states[parent]}" + (f" {note}" if note else ""))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
