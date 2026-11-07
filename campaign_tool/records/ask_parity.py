"""FUP-PARITY-1: pure proposal/checking, no model, network, or letter edits.

Budget: module <=170, CLI registration <=3, tests <=195 lines (<=380 total).
The proposer can miss asks without an initial ask verb: reviewers add/split/merge
rows by hand. Required is a JSON string array inside the editable TSV. Relaxing
it requires an explanatory note. Unit numbers are one-based short-text sentences
or list items. Only role labels, never private names, belong in confirmed_by.
Missing proves absence; candidate proves no survival. Only human confirmation
completes the parity table (send-ready rule 7), not approval to send anything.
"""
import argparse
import csv
import json
from pathlib import Path
import re
import sys

from .outcomes.response_items import content_words, sentences

FIELDS = ('ask_id', 'long_text', 'required', 'short_unit', 'status', 'confirmed_by', 'note')
VERBS = r'provide|produce|include|identify|state|say|describe|explain|clarify|confirm|list|indicate|distinguish|name|assess'
START = re.compile(r'^(?:please\s+)?(?:' + VERBS + r')\b', re.I)
SPLIT = re.compile(r'(?:,\s*and|\band|;\s*(?:and\s+)?)\s+(?=(?:please\s+)?(?:' + VERBS + r')\b)', re.I)
MARK = re.compile(r'^\s*(?:[-*•]|\(?\d+[.)]|\(?[a-z][.)])\s+', re.I)
ROLES = {'reviewer', 'g', 'operator', 'drafter', 'approver'}


class ParityError(ValueError):
    """Text-free validation code suitable for CLI output."""


def units(text):
    return [unit for line in text.splitlines() for unit in sentences(MARK.sub('', line))]


def required(text):
    terms = content_words(text) | set(re.findall(r'\b\d+(?:[./-]\d+)*\b', text))
    terms.update(m.group(1).casefold() for m in re.finditer(r'["“]([^"”]+)["”]', text))
    terms.update(m.group(1).casefold() for m in re.finditer(r"(?<!\w)['‘]([^'’]+)['’](?!\w)", text))
    terms.update(m.group().strip(' .;?!').casefold() for m in re.finditer(
        r'\bor\b.+|\bif\b.*\bnot\s+held\b.*', text, re.I))
    return sorted(terms)


def propose(long_text):
    rows = []
    for unit in units(long_text):
        if START.match(unit):
            for atom in SPLIT.split(unit):
                atom = atom.strip(' ,;')
                rows.append(dict(zip(FIELDS, (f'ask-{len(rows) + 1}', atom,
                    required(atom), '', 'proposed', '', ''))))
    return rows


def _tokens(text):
    # Ignore only a terminal plural s, not word boundaries or other qualifiers.
    return [word[:-1] if word.endswith('s') else word
            for word in re.findall(r'\w+', text.casefold())]


def _contains(text, term):
    hay, needle = _tokens(text), _tokens(term)
    return bool(needle) and any(hay[i:i + len(needle)] == needle
                               for i in range(len(hay) - len(needle) + 1))


def check(table, short_text):
    short = units(short_text)
    result, seen = [], set()
    if not table:
        raise ParityError('empty_table')
    for row in table:
        if any(not isinstance(row.get(k), str) for k in FIELDS if k not in ('required', 'short_unit')):
            raise ParityError('invalid_row')
        if (not isinstance(row.get('ask_id'), str) or
                not re.fullmatch(r'[A-Za-z0-9_-]+', row['ask_id']) or row['ask_id'] in seen):
            raise ParityError('invalid_or_duplicate_ask_id')
        seen.add(row['ask_id'])
        terms = row.get('required')
        if (not isinstance(row.get('long_text'), str) or not row['long_text'].strip() or
                not isinstance(terms, list) or not terms or
                any(not isinstance(t, str) or not _tokens(t) for t in terms)):
            raise ParityError('invalid_required')
        if any(not any(_contains(t, default) for t in terms)
               for default in required(row['long_text'])) and not row.get('note', '').strip():
            raise ParityError('relaxed_without_note')
        chosen = str(row.get('short_unit', '')).strip()
        if chosen and not re.fullmatch(r'[1-9]\d*', chosen):
            raise ParityError('invalid_short_unit')
        role = row.get('confirmed_by', '').strip().casefold()
        if role and role not in ROLES:
            raise ParityError('confirmed_by_requires_role')
        targets = [(int(chosen), short[int(chosen) - 1])] if chosen and int(chosen) <= len(short) else (
            [] if chosen else list(enumerate(short, 1)))
        evaluations = [(n, [t for t in terms if not _contains(text, t)]) for n, text in targets]
        match = next((n for n, missing in evaluations if not missing), None)
        missing = min((m for _, m in evaluations), key=len, default=list(terms))
        status = 'missing' if match is None else ('confirmed' if chosen and role else 'candidate')
        result.append({**row, 'status': status, 'missing': missing, 'short_unit': match or chosen})
    return result


def exit_code(rows):
    return 1 if not rows or any(r['status'] == 'missing' for r in rows) else (
        2 if any(r['status'] != 'confirmed' for r in rows) else 0)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    proposal = commands.add_parser('propose')
    proposal.add_argument('long', type=Path)
    verify = commands.add_parser('check')
    verify.add_argument('table', type=Path)
    verify.add_argument('short', type=Path)
    verify.add_argument('--show-text', action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.command == 'propose':
            rows = propose(args.long.read_text(encoding='utf-8'))
            writer = csv.DictWriter(sys.stdout, FIELDS, delimiter='\t', lineterminator='\n')
            writer.writeheader()
            for row in rows:
                writer.writerow({**row, 'required': json.dumps(row['required'], ensure_ascii=False)})
            return 0
        with args.table.open(encoding='utf-8', newline='') as handle:
            reader = csv.DictReader(handle, delimiter='\t')
            if reader.fieldnames != list(FIELDS):
                raise ParityError('invalid_table_columns')
            table = list(reader)
        for row in table:
            row['required'] = json.loads(row['required'])
        rows = check(table, args.short.read_text(encoding='utf-8'))
        for row in rows:
            output = {key: row[key] for key in ('ask_id', 'status', 'short_unit', 'missing')}
            if args.show_text:
                output['long_text'] = row['long_text']
            print(json.dumps(output, ensure_ascii=False))
        return exit_code(rows)
    except ParityError as exc:
        print('ask-parity: ' + str(exc), file=sys.stderr)
        return 1
    except (OSError, ValueError, TypeError, csv.Error):
        # Never echo parser exceptions: they may contain private letter text.
        print('ask-parity: invalid_input', file=sys.stderr)
        return 1
