"""Run the ask, citation, quote and outgoing-letter lints and the banned-phrase check on one draft.

A mechanical check only: nothing is written, hit text is never printed, G still reads the draft.
"""
import contextlib
import importlib
import io
import re
import sys
from pathlib import Path

from .. import playbook
from . import lint_receipt
from .lint_out import QuietParser

MAX_DRAFT = 2 * 1024 * 1024
# "12:4 Q1 ..." (quotes) or "draft line 12 ask 3: code" / "draft line 12: code" (asks, cites).
_HIT = re.compile(r"^(?:draft line (\d+)|(\d+(?::\d+)?)\s)")


def _asks(draft, agency, opts):
    missing = [name for name in ("inventory", "asks", "events")
               if not (agency / (name + ".tsv")).exists()
               and not (agency / (name + ".tsv")).is_symlink()]
    if missing:
        return "no " + ", ".join(missing), None
    return None, ["--draft", draft, "--agency-dir", str(agency)]


def _cites(draft, agency, opts):
    package = opts.package
    if not package or (not Path(package).exists() and not Path(package).is_symlink()):
        return "no package", None
    path = Path(package)
    if path.is_dir() and not path.is_symlink():
        manifest = path / "package.json"
        if not manifest.exists() and not manifest.is_symlink():
            return "no package", None
    return None, ["--draft", draft, "--package", str(package)]


def _quotes(draft, agency, opts):
    pages = agency / "pages"
    if not pages.is_dir() or not any(pages.glob("*_p[0-9][0-9][0-9]*.txt")):
        return "no pages", None
    argv = ["--draft", draft, "--pages", str(pages)]
    if opts.quote_ledger:
        argv += ["--ledger", opts.quote_ledger, "--inventory", opts.inventory, "--original-root", opts.original_root]
    return None, argv


def _out(draft, agency, opts):
    if not opts.team_list:
        return "no team list", None
    if not opts.allow:
        return "no allow file", None
    return None, ["--draft", draft, "--team-list", opts.team_list, "--allow", opts.allow]


def _precedent(draft, agency, opts):
    if not opts.agencies:
        return "no agencies file", None
    if not opts.agency:
        return "no agency key", None
    return None, ["--draft", draft, "--agencies", opts.agencies, "--agency", opts.agency]


# Fixed order. Each entry: name, module, input check returning (skip reason, argv).
_LINTS = [
    ["asks", "lint_asks", _asks],
    ["cites", "lint_cites", _cites],
    ["quotes", "lint_quotes", _quotes],
    ["out", "lint_out", _out],
    ["precedent", "lint_precedent", _precedent],
]
_PRECEDENT_ROW = re.compile(r"^(\d+) (refusal|note) L9_[A-Z_]+$")


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
    if code not in (0, 1):
        return "error"
    output = out.getvalue()
    if module == "lint_precedent":  # notes never fail; only refusals count as hits
        rows = [m.groups() for m in map(_PRECEDENT_ROW.match, output.splitlines()) if m]
        refusals = [n for n, kind in rows if kind == "refusal"]
        notes = [n for n, kind in rows if kind == "note"]
        found = "%d notes, first %s" % (len(notes), notes[0]) if notes else ""
        if code == 0:
            return "note, " + found if notes else "pass"
        return ("FAIL, %d hits, first %s" % (len(refusals), refusals[0]) if refusals else "FAIL, 0 hits") + ("; " + found if notes else "")
    coverage = ""
    summary = None
    if module == "lint_quotes":
        summary = re.search(r"^agency-lint-quotes: \d+ hits, \d+ quotes checked, (\d+) message quotes not checked against pages$", output, re.MULTILINE)
        if summary:
            coverage = "; text check only; no visual check of page images"
            coverage += ("; Q3 ledger consistency only, no visual check" if "--ledger" in argv
                         else "; Q3 original-page ledger not run")
            if int(summary.group(1)):
                coverage += "; %s message quotes not checked against pages" % summary.group(1)
    if code == 0:
        if module == "lint_quotes" and summary and int(summary.group(1)):
            return "incomplete" + coverage
        return "pass" + coverage
    places = [m.group(1) or m.group(2) for m in map(_HIT.match, output.splitlines()) if m]
    if not places:
        return "FAIL, 0 hits" + coverage
    return "FAIL, %d hits, first %s%s" % (len(places), places[0], coverage)


def _banned(text):
    hits = playbook.lint(playbook.load(), text)["hits"]
    if not hits:
        return "pass"
    start = hits[0]["start"]
    line = text.count("\n", 0, start) + 1
    col = start - (text.rfind("\n", 0, start) + 1) + 1
    return "FAIL, %d hits, first %d:%d" % (len(hits), line, col)


def main(argv=None):
    parser = QuietParser(prog="records agency-lint", description=__doc__, refusal="refused agency_lint_input")
    parser.add_argument("draft")
    lint_receipt.add_options(parser, True)
    parser.add_argument("--receipt", help="write records-agency-lint-receipt-v1 here (new file, hashes and fixed codes only)")
    args = parser.parse_args(argv)
    draft, agency = Path(args.draft), Path(args.agency_dir)
    group = (args.quote_ledger, args.inventory, args.original_root)
    try:
        if any(group) and not all(group):
            raise OSError
        if not agency.is_dir() or draft.stat().st_size > MAX_DRAFT:
            raise OSError
        raw = draft.read_bytes()
        text = raw.decode("utf-8-sig")
        before = lint_receipt.snapshot(args, raw) if args.receipt else None
    except (OSError, ValueError, lint_receipt.Unverifiable):
        print("refused agency_lint_input")
        return 2
    results = []
    for name, module, check in _LINTS:
        reason, lint_argv = check(str(draft), agency, args)
        results.append((name, "skipped: " + reason if reason else _run(module, lint_argv)))
    try:
        results.append(("banned phrases", _banned(text)))
    except Exception:
        results.append(("banned phrases", "error"))
    for name, status in results:
        print("%s: %s" % (name, status))
    statuses = [s for _, s in results]
    coded_skips = 0
    if args.receipt:
        try:  # an operational error, or any draft/rule/input change during the run, writes no receipt
            if lint_receipt.snapshot(args, draft.read_bytes()) != before:
                raise lint_receipt.Unverifiable
            coded = {n: lint_receipt.structured(s) for n, s in results}
            lint_receipt.write(args.receipt, before, coded)
            print("agency-lint receipt: written")
            coded_skips = sum(r["status"] == "skipped" for r in coded.values())  # receipt mode: coverage gaps are skips
        except (OSError, lint_receipt.Unverifiable):
            print("agency-lint receipt: refused")
            return 2
    if any(s.startswith(("FAIL", "error")) for s in statuses):
        print("agency-lint: FAIL")
        return 1
    skipped = sum(s.startswith("skipped") for s in statuses)
    incomplete = sum(s.startswith("incomplete") for s in statuses)
    if coded_skips and not skipped + incomplete:
        print("agency-lint: INCOMPLETE (%d skipped)" % coded_skips)
        return 3
    if skipped or incomplete:
        parts = []
        if skipped:
            parts.append("%d skipped" % skipped)
        if incomplete:
            parts.append("%d incomplete" % incomplete)
        print("agency-lint: INCOMPLETE (%s)" % ", ".join(parts))
        return 3
    print("agency-lint: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
