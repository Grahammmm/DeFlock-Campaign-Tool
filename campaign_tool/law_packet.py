"""law review-packet: one private, offline packet a human reviewer of a law package can work from.

Built from review-sheet's worksheet plus the package data. A packet is a question, not an answer: it never
changes the package, never sets status, reviewed_by, reviewed_at or a review label, and states no conclusion.
"""
import io
import json
import os
import stat
import sys
from datetime import date
from pathlib import Path

from . import law

ORDER = ("needs_attorney_review", "likely", "verified")
NAMES = ("PACKET.md", "RULES.md", "WORKSHEET.csv", "RETURN.md")


def ordered_rules(package):
    """Grouped by each rule's own ``review`` field, never its text; package order kept inside a group."""
    return sorted(package["rules"], key=lambda rule: ORDER.index(rule["review"]))


def _sources(sources):
    return [f"  - {s['title']} — {s['url']} (accessed {s['accessed']})" for s in sources]


def _rule(number, rule):
    end = f"to {rule['effective_to']}" if rule["effective_to"] else "with no end date"
    span = f"from {rule['effective_from']} {end}"
    return [f"## {number}. {rule['rule_id']}", "",
            f"- Current label (the author's, not a review): `{rule['review']}`",
            f"- Citation: {rule['citation']}", f"- In force: {span}",
            f"- Actor: {rule['actor']}", f"- Activity: {rule['activity']}",
            f"- The tool's reading: {rule['duty']}",
            "- Exceptions as encoded:" + ("" if rule["exceptions"] else " none listed"),
            *[f"  - {item}" for item in rule["exceptions"]],
            f"- Remedy as encoded: {rule['remedy']}", "- Cited primary sources:", *_sources(rule["sources"]), "",
            f"**Question.** Read the cited primary text as in force {span}. Does it support the reading above "
            "as written? Answer `verified`, `likely` or `needs_attorney_review` in this rule's worksheet row. "
            "If you cannot confirm it, answer `needs_attorney_review`: a blank cell keeps the author's label.", ""]


def render(package, counts):
    law_ = package["records_law"]
    j = package["jurisdiction"]
    head = f"Package `{j}`, status `{package['status']}`, reviewers on record: " + (
        ", ".join(package["reviewed_by"]) or "none") + "."
    tally = ", ".join(f"{counts[label]} `{label}`" for label in ORDER)
    packet = "\n".join([
        f"# Law package review packet: {j}", "", head, "",
        "This packet is a question, not an answer. It is not legal advice, it contains no conclusion about the law "
        "or about any agency, and building it changed nothing in the package.", "",
        "## What the package is used for", "",
        f"The campaign tool computes every records-request deadline, follow-up and automated reminder from this "
        f"package: the {law_['name']} ({law_['citation']}) and {len(package['rules'])} rules. A reading that is "
        "wrong here is repeated in every one of those outputs.", "",
        f"Rules by current label: {tally}. RULES.md lists them in that order. A `verified` label today is the "
        "author's own restatement, not an independent check.", "",
        "## What independence means here", "",
        "You did not write or edit this package. You check each reading against the primary text it cites "
        "(the source links in RULES.md), not against the package's wording or this packet.", "",
        "## What signing commits you to", "",
        "Your stable identifier (a name or handle, never an email address) is recorded in `reviewed_by`, with a "
        "review date, in a pull request. It says you checked every rule against its cited primary text as in force "
        "on its dates and that each label is the one you gave. After that pull request merges, the tool treats the "
        f"package as reviewed for every campaign in `{j}`. Whether to sign at all is your decision.", "",
        "## A rule you cannot confirm", "",
        "Leave it unconfirmed: answer `needs_attorney_review` for it, never `verified`.", "",
        "## Files", "",
        "- RULES.md: every rule with its reading, cited sources, dates and one question.",
        "- WORKSHEET.csv: the review-sheet worksheet, unchanged, where your answers go.",
        "- RETURN.md: how to return the worksheet.", ""])
    rules = [f"# Rules to check: {j}", "",
             "Order: `needs_attorney_review` first, then `likely`, then `verified`, read from each rule's own "
             "`review` field. Text after a label is quoted from the package as written.", ""]
    for number, rule in enumerate(ordered_rules(package), 1):
        rules += _rule(number, rule)
    rules += ["## records_law (worksheet row 1; carries no label)", "",
              f"- Name: {law_['name']}", f"- Citation: {law_['citation']}",
              f"- Determination period as encoded: {law_['determination_days']} {law_['day_type']} days, one "
              f"extension of up to {law_['determination_extension_days']} days",
              f"- Fees as encoded: {law_['fee_basis']}", f"- Enforcement as encoded: {law_['appeal']}",
              "- Cited primary sources:", *_sources(law_["sources"]), "",
              "**Question.** Does the cited primary text support the period and the other text above? This row "
              "carries no label, so leave its `reviewer_conclusion` blank and put what you find in "
              "`reviewer_notes`.", ""]
    back = "\n".join([
        "# Returning the worksheet", "",
        "1. Fill only the four reviewer columns of WORKSHEET.csv: `reviewer_conclusion` (`verified`, `likely` or "
        "`needs_attorney_review`), `reviewer_notes`, `reviewer_name` and `reviewed_on` (YYYY-MM-DD, not after the "
        "day you return it). Leave `reviewer_conclusion` blank on the `records_law` row.",
        "2. Keep the header, the columns (including `package_sha256`, which ties the worksheet to the exact "
        "package it was made from; never edit or copy it) and the row order as they are. A correction to `duty`, "
        "`exceptions` or `remedy` may be made in place or described in `reviewer_notes`; it is listed for the "
        "maintainer, never applied automatically.",
        "3. Give the filled worksheet to G, the campaign maintainer, who opens the pull request. This packet names "
        "no address: return it the way you received it. If the package changes before then, the worksheet is "
        "refused: the maintainer builds a new packet and you reassess each rule against it.",
        f"4. The maintainer reads it back with `python3 -m campaign_tool.law apply-sheet {j} --sheet FILLED.csv "
        "--out PROPOSED.json`, checks the result with `tools/check_law_review.py`, and records `reviewed_by` and "
        "`reviewed_at` through docs/LAW-PACKAGES.md.", "",
        "Nothing in this packet has been sent anywhere.", ""])
    return packet, "\n".join(rules), back


def _checked_out(out):
    path = Path(out)
    if not path.is_absolute() or Path(os.path.normpath(path)) != path:
        raise ValueError("--out must be an absolute, normalised path")
    if os.path.lexists(path):
        raise ValueError("--out already exists, not overwriting")
    if any(p.is_symlink() for p in (path.parent, *path.parent.parents)) or not path.parent.is_dir():
        raise ValueError("--out parent must be an existing directory with no symlink in its path")
    if path == law.REPO_ROOT or law.REPO_ROOT in path.parents:
        raise ValueError("--out must be outside the repository")
    return path


_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def _open_parent(path):
    """Open ``path`` one component at a time from the root, refusing a symlink at any level; returns its fd."""
    fd = os.open("/", _DIR_FLAGS)
    for part in path.parts[1:]:
        try:
            nxt = os.open(part, _DIR_FLAGS, dir_fd=fd)
        finally:
            os.close(fd)
        fd = nxt
    return fd


def _owned(out_fd, member, fd):
    """True only when ``member`` in the folder still names the regular file ``fd`` opened; else leave it."""
    mine = os.fstat(fd)
    there = os.stat(member, dir_fd=out_fd, follow_symlinks=False)
    return (stat.S_ISREG(mine.st_mode) and stat.S_ISREG(there.st_mode)
            and (mine.st_dev, mine.st_ino) == (there.st_dev, there.st_ino))


def _roll_back(parent_fd, name, out_fd, created):
    """Remove what this invocation created, through the retained descriptors.

    ``created`` holds (basename, still-open descriptor) for each member this invocation opened exclusively.
    A member is unlinked only if its entry still is that same file; a replacement (file, link, directory,
    hard link to another file), a missing entry or one that cannot be inspected is left untouched, which can
    leave the output folder behind. Best effort: never masks the original error.
    """
    if out_fd is None:
        return  # the new folder could not be verified as ours: leave it
    for member, fd in created:
        try:
            if _owned(out_fd, member, fd):
                os.unlink(member, dir_fd=out_fd)
        except OSError:
            pass
    try:
        here = os.fstat(out_fd)
        there = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (here.st_dev, here.st_ino) == (there.st_dev, there.st_ino):
            os.rmdir(name, dir_fd=parent_fd)
    except OSError:
        pass


def build(jurisdiction, out, as_of=None, base=None):
    """Write the four files into a new 0700 folder; everything is built in memory first."""
    path = _checked_out(out)
    package = law.load_package(jurisdiction, base)
    as_of = law.parse_date(as_of, "--as-of") if as_of else date.today()
    sheet = io.StringIO(newline="")
    law.write_sheet(sheet, law.review_sheet(package, as_of)[0])
    counts = law.summary(package)["review_counts"]
    texts = (*render(package, counts), sheet.getvalue())
    files = dict(zip(("PACKET.md", "RULES.md", "RETURN.md", "WORKSHEET.csv"), texts))
    parent_fd = _open_parent(path.parent)
    out_fd = None
    created = []
    try:
        os.mkdir(path.name, 0o700, dir_fd=parent_fd)
        out_fd = os.open(path.name, _DIR_FLAGS, dir_fd=parent_fd)
        for name in NAMES:
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=out_fd)
            created.append((name, fd))  # owned from here: the descriptor is closed in the final cleanup
            with os.fdopen(fd, "w", encoding="utf-8", newline="", closefd=False) as stream:
                stream.write(files[name])
    except BaseException:
        _roll_back(parent_fd, path.name, out_fd, created)
        raise
    finally:
        for _, fd in created:  # kept open until now so an inode in use for ownership checks is never recycled
            os.close(fd)
        if out_fd is not None:
            os.close(out_fd)
        os.close(parent_fd)
    return {"status": "review_required", "jurisdiction": jurisdiction, "package_status": package["status"],
            "as_of": as_of.isoformat(), "out": str(path), "files": list(NAMES), "review_counts": counts,
            "note": "A packet for a human reviewer, not a review; the package is unchanged."}


def main(args):
    try:
        print(json.dumps(build(args.jurisdiction, args.out, args.as_of, args.base)))
    except (OSError, ValueError) as exc:
        print("Stopped: " + str(exc).splitlines()[0], file=sys.stderr)
        return 2
    return 0
