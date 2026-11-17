"""Run the ask, citation and quote lints and the banned-phrase check on one draft.

A mechanical check only: nothing is written, hit text is never printed, G still reads the draft.
"""
import argparse
import contextlib
import importlib
import io
import re
import sys
from pathlib import Path

from .. import playbook

MAX_DRAFT = 2 * 1024 * 1024
# "12:4 Q1 ..." (quotes) or "draft line 12 ask 3: code" / "draft line 12: code" (asks, cites).
_HIT = re.compile(r"^(?:draft line (\d+)|(\d+(?::\d+)?)\s)")


def _asks(draft, agency, package):
    if not (agency / "inventory.tsv").is_file():
        return "no inventory", None
    return None, ["--draft", draft, "--agency-dir", str(agency)]


def _cites(draft, agency, package):
    if not package or not Path(package).exists():
        return "no package", None
    return None, ["--draft", draft, "--package", str(package)]


def _quotes(draft, agency, package):
    pages = agency / "pages"
    if not pages.is_dir() or not any(pages.glob("*_p[0-9][0-9][0-9]*.txt")):
        return "no pages", None
    return None, ["--draft", draft, "--pages", str(pages)]


# Fixed order. Each entry: name, module, input check returning (skip reason, argv).
_LINTS = [
    ["asks", "lint_asks", _asks],
    ["cites", "lint_cites", _cites],
    ["quotes", "lint_quotes", _quotes],
]


def _run(module, argv):
    out = io.StringIO()
    try:
        mod = importlib.import_module("." + module, __package__)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = mod.main(argv)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 2
    except Exception:
        return "error"
    if code == 0:
        return "pass"
    if code != 1:
        return "error"
    places = [m.group(1) or m.group(2) for m in map(_HIT.match, out.getvalue().splitlines()) if m]
    if not places:
        return "FAIL, 0 hits"
    return "FAIL, %d hits, first %s" % (len(places), places[0])


def _banned(text):
    hits = playbook.lint(playbook.load(), text)["hits"]
    if not hits:
        return "pass"
    start = hits[0]["start"]
    line = text.count("\n", 0, start) + 1
    col = start - (text.rfind("\n", 0, start) + 1) + 1
    return "FAIL, %d hits, first %d:%d" % (len(hits), line, col)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="records agency-lint", description=__doc__)
    parser.add_argument("draft")
    parser.add_argument("--agency-dir", required=True)
    parser.add_argument("--package")
    args = parser.parse_args(argv)
    draft, agency = Path(args.draft), Path(args.agency_dir)
    try:
        if not agency.is_dir() or draft.stat().st_size > MAX_DRAFT:
            raise OSError
        text = draft.read_bytes().decode("utf-8-sig")
    except (OSError, ValueError):
        print("refused agency_lint_input")
        return 2
    results = []
    for name, module, check in _LINTS:
        reason, lint_argv = check(str(draft), agency, args.package)
        results.append((name, "skipped: " + reason if reason else _run(module, lint_argv)))
    try:
        results.append(("banned phrases", _banned(text)))
    except Exception:
        results.append(("banned phrases", "error"))
    for name, status in results:
        print("%s: %s" % (name, status))
    statuses = [s for _, s in results]
    if any(s.startswith(("FAIL", "error")) for s in statuses):
        print("agency-lint: FAIL")
        return 1
    skipped = sum(s.startswith("skipped") for s in statuses)
    if skipped:
        print("agency-lint: INCOMPLETE (%d skipped)" % skipped)
        return 3
    print("agency-lint: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
