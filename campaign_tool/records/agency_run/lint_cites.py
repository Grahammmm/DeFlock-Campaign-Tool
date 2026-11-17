"""REC-LINT-CITE-1: lint a draft for loose citations, unsourced facts and bare overdue wording.

Read-only: reads the draft and the law package, prints "draft line N: RULE" and never the
quoted text. A clean result is NOT legal or semantic acceptance, and VERIFIED-IN-PACKAGE only
means a section number appears in the package file (which may still be status draft with no
human reviewer). Limit: sections compare as the dotted number only (1798.90.51(a) -> 1798.90.51),
ignoring the code name; the package's section numbers do not collide across codes today.
"""
import argparse
import json
import os
import re
import stat
import sys

DRAFT_MAX, PACKAGE_MAX = 512 * 1024, 2 * 1024 * 1024
NUM = r"[0-9]+(?:\.[0-9]+)*"
CODE = (r"(?:Gov\.?|Government|Civ\.?|Civil|Penal|Veh\.?|Vehicle|Sts\.? & Hy\.?|"
        r"Streets and Highways) Code")
CITATION = re.compile(rf"{CODE}\s+(?:§§?\s*|[Ss]ections?\s+)?({NUM})(?:\([a-z0-9]+\))*|§§?\s*({NUM})(?:\([a-z0-9]+\))*")
LABEL = re.compile(r"(?<![A-Z])VERIFIED-IN-PACKAGE|UNVERIFIED")
ET_SEQ = re.compile(r"(?i:\bet\s+seq\b)|\bff\.")
SENTENCE_SPLIT = re.compile(r"(?<=[.!?])(?<!\bpp\.)(?<!\bp\.)(?<!\bNo\.)(?<!\be\.g\.)(?<!\bi\.e\.)\s+")
MONTH = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?"
NUMWORD = r"(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|twenty|thirty)"
FACT = re.compile(
    r"\b\d{4}-\d{2}-\d{2}\b|\b" + MONTH + r"\s+\d{1,2}\b|\b\d{1,2}/\d{1,2}/\d{2,4}\b"
    r"|\b\d{2,}\b|\b" + NUMWORD + r"\s+(?:documents|pages|requests|records|cameras|days|files|messages)\b"
    r"|\b\d{2}-\d{2,5}\b|\bD[0-9]+\b", re.I)
SOURCE_TOKEN = re.compile(
    r"\bD[0-9]+\b|\bM[0-9]+\b|\b(?:INBOX|Sent):[0-9]+|\bpp?\.\s*[0-9]+|Message-ID"
    r"|\S\.(?:tsv|md|csv|json|pdf|txt)\b|\b[A-Z]{2,6}-[0-9]{3}\b|\bUNVERIFIED\b|\bINFERRED\b")
OVERDUE = re.compile(r"\boverdue\b|\blate\b|\bpast\s+due\b", re.I)


class Refusal(Exception):
    pass


def _read(path, limit, code):
    try:
        info = os.lstat(path)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise Refusal(code)
        with open(path, "rb") as handle:
            return handle.read().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        raise Refusal(code)


def package_sections(path):
    if os.path.isdir(path) and not os.path.islink(path):
        path = os.path.join(path, "package.json")
    try:
        data = json.loads(_read(path, PACKAGE_MAX, "package_invalid"))
    except ValueError:
        raise Refusal("package_invalid")
    if not isinstance(data, dict) or "records_law" not in data:
        raise Refusal("package_invalid")
    sections, stack = set(), [data]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
        elif isinstance(item, str):
            sections.update(re.findall(rf"§+\s*({NUM})", item))
            for pair in re.findall(rf"§§\s*({NUM})\s*[-–]\s*({NUM})", item):
                sections.update(pair)
    return sections


def lint(text, sections):
    hits = []
    lines, fenced, paragraphs, current = text.splitlines(), False, [], []
    for number, raw in enumerate(lines, 1):
        if raw.lstrip().startswith("```"):
            fenced = not fenced
            current = _flush(paragraphs, current)
            continue
        if fenced:
            continue
        if not raw.strip():
            current = _flush(paragraphs, current)
            continue
        current.append((number, raw))
        hits += [(number, "C3_et_seq") for _ in ET_SEQ.finditer(raw)]
        found = list(CITATION.finditer(raw))
        for index, match in enumerate(found):
            end = found[index + 1].start() if index + 1 < len(found) else len(raw)
            label = LABEL.search(raw[match.end():min(match.end() + 80, end)])
            section = match.group(1) or match.group(2)
            if not label:
                hits.append((number, "C1_label_missing"))
            elif (label.group() == "UNVERIFIED") != (section not in sections):
                hits.append((number, "C2_label_mismatch"))
        blanked = CITATION.sub(lambda m: " " * len(m.group()), raw)
        blanked = ET_SEQ.sub(" ", blanked)
        for sentence in SENTENCE_SPLIT.split(blanked):
            if FACT.search(sentence) and not SOURCE_TOKEN.search(sentence):
                hits.append((number, "C4_unsourced_fact"))
    _flush(paragraphs, current)
    for paragraph in paragraphs:
        flat = " ".join(" ".join(row for _, row in paragraph).lower().split())
        first = next((n for n, row in paragraph if OVERDUE.search(row)), None)
        if first and not ("on our records" in flat and "not a legal finding" in flat):
            hits.append((first, "C5_overdue_wording"))
    return sorted(hits)


def _flush(paragraphs, current):
    if current:
        paragraphs.append(current)
    return []


def main(argv=None):
    parser = argparse.ArgumentParser(prog="agency-lint-cites", description=__doc__)
    parser.add_argument("--draft", required=True)
    parser.add_argument("--package", required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        sections = package_sections(args.package)
        hits = lint(_read(args.draft, DRAFT_MAX, "draft_invalid"), sections)
    except Refusal as refusal:
        print(f"agency-lint-cites: {refusal}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps({"ok": not hits, "hits": [{"line": n, "code": c} for n, c in hits]}))
    else:
        for number, code in hits:
            print(f"draft line {number}: {code}")
        if not hits:
            print("agency-lint-cites: no hits (not legal or semantic acceptance)")
    return 1 if hits else 0
