"""Deterministic reconciliation of explicit evidence inventories; no promotion."""
import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import re

HASH = re.compile(r'^[0-9a-f]{64}$')
LIMIT = 100000

def _hash(value):
    return isinstance(value, str) and HASH.fullmatch(value) is not None

def _identity(row, kind):
    value = row.get('identity')
    if not isinstance(value, dict):
        return None
    fields = ('portal_host', 'request_id', 'item_id') if kind == 'portal' else ('account', 'folder', 'uidvalidity', 'uid')
    if any(value.get(k) in (None, '') for k in fields):
        return None
    if kind != 'portal':
        if any(not isinstance(value[k], str) for k in ('account', 'folder')):
            return None
        if any(type(value[k]) is not int or value[k] < 1 for k in ('uidvalidity', 'uid')):
            return None
        if row.get('kind') == 'attachment':
            part = value.get('mime_part_path')
            if not isinstance(part, str) or not re.fullmatch(r'[1-9][0-9]*(?:\.[1-9][0-9]*)*', part):
                return None
            fields += ('mime_part_path',)
    elif any(not isinstance(value[k], str) for k in fields):
        return None
    return json.dumps({k: value[k] for k in fields}, sort_keys=True, separators=(',', ':'))

def reconcile(kind, sources, indexed, objects):
    """Caller supplies byte-verified object hashes; identities never use filenames."""
    if kind not in ('mail', 'portal', 'digests'):
        raise ValueError('unknown reconciliation kind')
    if not isinstance(sources, list) or not isinstance(indexed, list) or len(sources)+len(indexed)>LIMIT:
        raise ValueError('row bound or type')
    if not isinstance(objects, list) or len(objects)>LIMIT or any(not _hash(x) for x in objects):
        raise ValueError('explicit object hashes required')
    if any(not isinstance(r, dict) for r in sources+indexed):
        raise ValueError('rows must be objects')
    known = set(objects)
    rows = []
    match_groups = {}
    if kind == 'digests':
        coverage = defaultdict(set)
        for row in sources:
            if _hash(row.get('sha256')):
                coverage[row['sha256']].add(json.dumps(row.get('coverage_declared'),sort_keys=True))
        for i,row in enumerate(sources):
            sha = row.get('sha256')
            if row.get('error'):
                classification = 'parser_error'
            elif not _hash(sha):
                classification = 'incomplete_receipt'
            elif sha not in known:
                classification = 'outside_intake'
            elif len(coverage[sha]) > 1:
                classification = 'declaration_difference_needs_reconciliation'
            else:
                classification = 'declared_only'
            rows.append({'source_row':i,'classification':classification,'subject_sha256':sha if _hash(sha) else None})
        if indexed:
            raise ValueError('digest reconciliation uses sources only')
    else:
        by_key = defaultdict(lambda: defaultdict(list))
        for i,row in enumerate(indexed):
            key = _identity(row,kind)
            if key is not None:
                sha = row.get("sha256")
                by_key[key][sha if _hash(sha) else None].append(i)
        used = set()
        seen_groups = set()
        for i,row in enumerate(sources):
            key = _identity(row,kind)
            sha = row.get('sha256')
            matches = by_key.get(key,{}) if key is not None else {}
            exact = matches.get(sha,[]) if _hash(sha) else []
            group_id = hashlib.sha256((key+'\0'+sha).encode()).hexdigest() if exact else None
            # A (identity, sha256) pair may bind to its index rows only once; later
            # source receipts for the same pair are duplicates, never extra matches.
            repeat = group_id is not None and group_id in seen_groups
            if group_id is not None and not repeat:
                seen_groups.add(group_id)
                used.update(exact)
                match_groups[group_id] = exact
            if kind == 'portal':
                binding = row.get('binding_evidence')
                joined = (key is not None and isinstance(binding,dict)
                          and _identity(binding,'portal') == key
                          and binding.get('sha256') == sha
                          and _hash(binding.get('receipt_sha256'))
                          and binding.get('receipt_sha256') == row.get('receipt_sha256'))
                classification = ('parser_error' if row.get('error') else
                                  'receipt-without-bytes' if not _hash(sha) or sha not in known else
                                  'bytes-present-and-joined' if joined else 'bytes-present-unjoined')
            elif row.get('error'):
                classification = 'parser_error'
            elif row.get('scope') == 'excluded' and row.get('exclusion_reason'):
                classification = 'excluded_content'
            elif key is None or not _hash(sha) or not _hash(row.get('receipt_sha256')):
                classification = 'incomplete_receipt'
            elif sha not in known:
                classification = 'unavailable_original'
            elif matches and not exact:
                classification = 'identity_hash_conflict'
            elif not exact:
                classification = 'stale_index'
            elif repeat:
                classification = 'duplicate_source_receipt'
            elif len(exact)>1:
                classification = 'duplicate_index_rows'
            else:
                classification = 'matched'
            rows.append({'source_row':i,'index_match_group':group_id,'classification':classification,
                         'subject_sha256':sha if _hash(sha) else None})
        for i,row in enumerate(indexed):
            if i not in used:
                rows.append({'index_row':i,'classification':'index_without_bound_receipt',
                             'subject_sha256':row.get('sha256') if _hash(row.get('sha256')) else None})
    result = {'schema':'reconciliation-v2','kind':kind,'source_rows':len(sources),
              'index_rows':len(indexed),'classified_rows':len(rows),'unexplained_rows':0,
              'classifications':dict(sorted(Counter(r['classification'] for r in rows).items())),
              'rows':rows,'index_match_groups':match_groups,'stage_promotions':0,'acceptance':'supplied_evidence_only'}
    canonical=json.dumps(result,sort_keys=True,separators=(',',':')).encode()
    result['report_sha256']=hashlib.sha256(canonical).hexdigest()
    return result

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('kind',choices=('mail','portal','digests'))
    parser.add_argument('--input',required=True)
    parser.add_argument('--report',required=True)
    args=parser.parse_args()
    with Path(args.input).open('rb') as stream:
        raw=stream.read(32*1024*1024+1)
    if len(raw)>32*1024*1024:
        parser.error('input exceeds bound')
    value=json.loads(raw)
    result=reconcile(args.kind,value['sources'],value.get('index',[]),value['objects'])
    output=(json.dumps(result,sort_keys=True,indent=2)+'\n').encode()
    import os
    fd=os.open(args.report,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'wb') as stream:
        stream.write(output)
    print(json.dumps({'report_sha256':result['report_sha256'],'classifications':result['classifications'],'stage_promotions':0}))

if __name__=='__main__':
    main()
