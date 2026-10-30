"""Fail-closed offline readiness. READY is evidence shape, never permission."""
import argparse
from collections import Counter
import hashlib
import json
import os
import re
import stat
from pathlib import Path

ROWS = ('S2', 'S3', 'S3e', 'S4', 'S4b', 'S5', 'S6', 'S7', 'S7r', 'S8', 'S9a', 'S9b', 'S10', 'F1')
CELLS = ('gate_evidence', 'backup_receipt', 'rollback_rehearsal', 'authority')
SCHEMA = 'mail-readiness-matrix-v1'


class MatrixError(ValueError):
    """Only fixed diagnostic codes cross the input boundary."""


def require(condition, code='matrix_shape'):
    if not condition:
        raise MatrixError(code)


def text(value):
    return isinstance(value, str) and bool(value.strip())


def read_bounded(path):
    try:
        path = Path(path).absolute()
        require(not any(p.is_symlink() for p in (path, *path.parents)), 'input_file')
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            require(stat.S_ISREG(info.st_mode) and info.st_size <= 256 * 1024, 'input_file')
            data = stream.read(256 * 1024 + 1)
        require(len(data) <= 256 * 1024, 'input_file')
        return data.decode('utf-8')
    except (OSError, ValueError) as exc:
        if isinstance(exc, MatrixError):
            raise
        raise MatrixError('input_file') from None


def unique(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, 'duplicate_key')
        result[key] = value
    return result


def load(path):
    try:
        matrix = json.loads(read_bounded(path), object_pairs_hook=unique)
    except (ValueError, RecursionError) as exc:
        if isinstance(exc, MatrixError):
            raise
        raise MatrixError('invalid_json') from None
    require(isinstance(matrix, dict) and set(matrix) == {'schema', 'rows'})
    require(matrix['schema'] == SCHEMA and isinstance(matrix['rows'], dict))
    require(tuple(matrix['rows']) == ROWS)
    for name, row in matrix['rows'].items():
        require(isinstance(row, dict) and set(row) in (set(('purpose', 'tools', *CELLS)),
                                                      set(('purpose', 'tools', 'label', *CELLS))))
        require(text(row['purpose']) and ('label' not in row or text(row['label'])))
        require(isinstance(row['tools'], list) and (row['tools'] or name == 'S10'))
        for tool in row['tools']:
            require(isinstance(tool, dict) and {'name', 'status', 'owner', 'prerequisite'} <= set(tool)
                    <= {'name', 'status', 'owner', 'prerequisite', 'path', 'sha256', 'argv'})
            require(all(text(tool[k]) for k in ('name', 'owner', 'prerequisite')))
            require(tool['status'] in ('EXECUTABLE', 'NON_EXECUTABLE', 'UNSUPPLIED'))
            if 'path' in tool:
                require(text(tool['path']) and not Path(tool['path']).is_absolute()
                        and '..' not in Path(tool['path']).parts)
            if 'sha256' in tool:
                require(isinstance(tool['sha256'], str) and
                        re.fullmatch(r'[0-9a-f]{64}', tool['sha256']) is not None)
            if tool['status'] == 'EXECUTABLE':
                require(text(tool.get('path')) and isinstance(tool.get('argv'), list) and tool['argv'])
                require(all(text(arg) and all(p.startswith('/') for p in re.findall(r'<([^<>]*)>', arg))
                            and '<' not in re.sub(r'<[^<>]+>', '', arg)
                            and '>' not in re.sub(r'<[^<>]+>', '', arg) for arg in tool['argv']))
            else:
                require('argv' not in tool)
        for key in CELLS:
            c = row[key]
            require(isinstance(c, dict) and set(c) == {'required', 'status', 'owner', 'prerequisite', 'what'})
            require(type(c['required']) is bool and c['status'] in ('MET', 'UNSUPPLIED', 'NOT_APPLICABLE'))
            require(all(text(c[k]) for k in ('owner', 'prerequisite', 'what')))
            require(not (c['required'] and c['status'] == 'NOT_APPLICABLE'))
    return matrix


def tool_identity(tool, repo_root):
    try:
        root, rel = Path(repo_root).resolve(), Path(tool['path'])
        if rel.is_absolute() or '..' in rel.parts:
            return False
        path = root / rel
        if any(p.is_symlink() for p in (path, *path.parents)) or not path.is_file():
            return False
        if not path.resolve().is_relative_to(root):
            return False
        if 'sha256' in tool:
            with path.open('rb') as stream:
                return hashlib.file_digest(stream, 'sha256').hexdigest() == tool['sha256']
        return True
    except (OSError, ValueError):
        return False


def cell_met(cell):
    return cell['status'] == 'MET'


def evaluate(matrix, repo_root):
    result = {}
    for name, row in matrix['rows'].items():
        reasons = ['cell_' + key + '_not_met' for key in CELLS
                   if row[key]['required'] and not cell_met(row[key])]
        if any(tool['status'] != 'EXECUTABLE' for tool in row['tools']):
            reasons.append('tool_not_executable')
        if any(t['status'] == 'EXECUTABLE' and not tool_identity(t, repo_root) for t in row['tools']):
            reasons.append('tool_identity')
        authority = row['authority']
        if name == 'S9b' and (not cell_met(authority) or not re.match(
                r'^separate G exact-action approval:\s*\S', authority['what'])):
            reasons.append('s9b_separate_approval_required')
        if name == 'F1' and not cell_met(authority):
            reasons.append('fallback_approval_required')
        entry = dict(verdict='HELD' if reasons else 'READY', reasons=reasons)
        if name == 'S4' and not reasons and any(
                c['status'] == 'UNSUPPLIED' and 'M-31' in c['prerequisite'] + c['what']
                for c in (row[key] for key in CELLS)):
            entry['label'] = 'as of one capture; nonmutation UNSUPPLIED (M-31)'
        result[name] = entry
    return result


def check_runbook(text):
    codes, fence = set(), False
    for line in text.splitlines():
        if line == '```' and fence:
            fence = False
            continue
        if fence:
            if any(t in line for t in (';', '&&', '||', '|', '$(', '`', '<<')):
                codes.add('shell_chaining')
            if re.search(r'\bsudo\b|\bdocker\s+exec\b|\brm\s|password=|token=|PYTHONPATH=', line):
                codes.add('forbidden_token')
            if re.search(r'mail_config|\.netrc|credentials', line, re.I) and (
                    re.search(r'[<>]|\b(?:vi|vim|nvim|nano|emacs|ed|sed|tee)\b', line)):
                codes.add('credential_edit')
            continue
        if line == '```sh':
            fence = True
            continue
        # Reject code outside the sole supported container, including nested containers.
        stripped = re.sub(r'^(?:\s*> ?)+', '', line)
        listed = re.sub(r'^\s*(?:[-+*]|\d+[.)]) ', '', stripped)
        if re.search(r'`{3,}|~{3,}', line) or re.match(r'^(?: {4}|\t)\S?', listed):
            codes.add('unsupported_code_container')
    if fence:
        codes.add('unsupported_code_container')
    return sorted(codes)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('matrix')
    parser.add_argument('--runbook')
    args = parser.parse_args()
    summary = dict(schema=SCHEMA, rows=0, ready=0, held=0, codes={}, runbook_codes=[])
    try:
        results = evaluate(load(args.matrix), Path(__file__).resolve().parents[1])
        summary.update(rows=len(results), ready=sum(r['verdict'] == 'READY' for r in results.values()),
                       held=sum(r['verdict'] == 'HELD' for r in results.values()),
                       codes=dict(Counter(c for r in results.values() for c in r['reasons'])))
        if args.runbook:
            summary['runbook_codes'] = check_runbook(read_bounded(args.runbook))
    except MatrixError as exc:
        summary['codes'] = {str(exc): 1}
        print(json.dumps(summary, sort_keys=True))
        return 2
    print(json.dumps(summary, sort_keys=True))
    return 2 if summary['runbook_codes'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
