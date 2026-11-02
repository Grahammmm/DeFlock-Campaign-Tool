"""One fixed `Fix:` sentence for each known `Stopped:` cause of the starter CLI.

Pure and deterministic. Rows are matched in order, first match wins, by exception
class plus a literal message prefix. A fix text is a fixed string: it may only
interpolate the JSON error line number, the count `n` from a limit/leak message,
or a campaign field name that matched [a-z_]{1,32}. It never repeats a path,
str(exc) or any user text, and an unknown failure gets no fix (nothing invented).
"""
import errno
import json
import re
import sqlite3
import sys

_BACKUP = "restore a verified backup (`verify`, then `restore`)"


def _file(suffix):
    return lambda exc: str(getattr(exc, "filename", None) or "").endswith(suffix)


def _msg(pattern):
    compiled = re.compile(pattern, re.S)
    return lambda exc: compiled.match(str(exc))


ROWS = (
    (FileNotFoundError, _file("campaign.json"),
     "Create the campaign with `init`, or check the --directory path."),
    (FileExistsError, _file("campaign.json"),
     "init never overwrites an existing campaign; pick a new --directory, or run `doctor` on the existing one."),
    (json.JSONDecodeError, lambda exc: True, lambda m, exc: (
        f"The JSON file is not valid near line {int(exc.lineno)}; fix that line. For campaign.json you can "
        f"instead {_BACKUP}.")),
    (ValueError, _msg(r"Unsupported campaign schema version\Z"),
     f"campaign.json was written by another version of this tool; use that version or {_BACKUP}; do not edit it by hand."),
    (ValueError, _msg(r"Missing campaign field: ([a-z_]{1,32})\Z"), lambda m, exc: (
        f'Give a non-empty "{m.group(1)}" (in campaign.json, or as the `init` option).')),
    (ValueError, _msg(r"(State must be a two-letter code|Use a two-letter state code; jurisdiction is not auto-verified)\Z"),
     "Use two capital letters for the state, such as CA; the tool does not verify jurisdiction."),
    (ValueError, _msg(r"Alpha limit: 50 MiB per file; original was not imported"),
     "That file was not imported; keep the original untouched in your own storage, because this alpha cannot hold it."),
    (ValueError, _msg(r"(Input must be a regular, non-symlink file|Input changed or is not a regular, non-symlink file"
                      r"|--dir must be a real directory, not a symlink or a file"
                      r"|Folder changed during the run \(not a real directory\))"),
     "Point --file or --dir at the real file or folder (copy it first if it is a link); `status` shows what was already stored."),
    (ValueError, _msg(r".*: more than (\d{1,6}) files; split the folder\Z"), lambda m, exc: (
        f"Split the folder into batches of at most {m.group(1)} files and ingest each with its own --source-id.")),
    (ValueError, _msg(r"Stored object integrity mismatch; intake stopped"),
     f"Stop here: run `status --check` and {_BACKUP}; do not ingest over it."),
    (ValueError, _msg(r"ledger (is not readable|has a journal or WAL file beside it); nothing was written\Z"),
     "The dry run wrote nothing. Let any other run on this campaign finish, then run `status --check`; "
     f"keep private/ as it is, and {_BACKUP} if it reports problems."),
    (ValueError, _msg(r"public tree check found (\d{1,6}) potential leak\(s\); fix content and rebuild\Z"), lambda m, exc: (
        f"Fix the {m.group(1)} item(s) listed above in the content, rebuild with `build --check`, "
        "and do not publish until it passes.")),
    (ValueError, _msg(r"meetings needs --online to query the Legistar Web API, or --from-json with a recorded response\Z"),
     "Add --online, or pass --from-json FILE with a recorded response."),
    (ValueError, _msg(r".* exists; the drill never overwrites a receipt\Z"),
     "Choose a receipt path that does not exist yet."),
    (sqlite3.Error, lambda exc: True,
     "The ledger could not be opened; run `status --check`, keep private/ as it is, "
     f"and {_BACKUP} if it reports problems."),
    (PermissionError, lambda exc: True,
     "Check who owns the folder and that you may write to it (`ls -ld`), then run the command again."),
    (OSError, lambda exc: exc.errno == errno.ENOSPC,
     "Free some disk space, then run `status --check` to see what was stored."),
)


def fix_for(exc):
    """The fix sentence for a known failure, else None."""
    for kind, test, text in ROWS:
        if isinstance(exc, kind):
            found = test(exc)
            if found:
                return text(found, exc) if callable(text) else text
    return None


def emit(exc):
    """Print `Stopped: <message>` then, for a known cause, one `Fix:` line, both on stderr."""
    print("Stopped: " + str(exc), file=sys.stderr)
    fix = fix_for(exc)
    if fix:
        print("Fix: " + fix, file=sys.stderr)
