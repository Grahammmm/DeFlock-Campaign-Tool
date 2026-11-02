"""Build a self-contained copy of the records-analysis and review skills.

The bundle holds the skills, every repo doc they link (under docs/), links
rewritten to those copies, and MANIFEST.json, so it works with no checkout.
Offline: no network, no git. Output is deterministic (no timestamps).
"""
import argparse
import hashlib
import json
import os
import posixpath
import re
import shutil
import sys
import tempfile
from pathlib import Path

SKILLS = ('records-analysis', 'records-applicability', 'records-capture', 'records-content-review',
          'records-correspondence', 'records-extraction', 'records-preservation',
          'records-publication', 'independent-review')
# One markdown link target, or a bare ../../docs/<file>.md path (links win: they start earlier).
REF = re.compile(r'(?P<md>(?<=\]\()[^)\s]+)|(?P<bare>(?:\.\./)+docs/[\w./-]*\.md)')
EXTERNAL = re.compile(r'^(?:[A-Za-z][A-Za-z0-9+.-]*:|#|//)')
README = """# Portable records skills

A self-contained copy of the DeFlock records-analysis and independent-review skills.
Each skill folder holds a SKILL.md; the repository docs those skills link are copied
under docs/ and the links point at those copies, so nothing here needs a checkout.

Included: records-analysis, records-applicability, records-capture, records-content-review,
records-correspondence, records-extraction, records-preservation, records-publication and
independent-review. The flock-* skills are campaign-operations skills tied to one
deployment and are excluded.

MANIFEST.json lists every file with its sha256 and source path. A skill that refers to
code in the repository (tools/, campaign_tool/, runner/) keeps that reference as plain
text and lists it under needs_checkout in the manifest; that part needs the checkout.

Licence: see LICENSE and NOTICE in this folder.

Install: copy the skill folders you want, together with docs/, into your agent's skills
directory, keeping docs/ next to the skill folders so the ../docs/ links resolve.
"""


class Refuse(Exception):
    def __init__(self, code, detail=''):
        super().__init__(code)
        self.code, self.detail = code, detail


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def check_plain(root, rel):
    """Refuse a symlink anywhere on the path (or a non-regular leaf); return the path."""
    cur = root
    for part in rel.split('/'):
        cur = cur / part
        if cur.is_symlink():
            raise Refuse('symlink', rel)
    return cur


def frontmatter(skill, text):
    lines = text.split('\n')
    if lines[0].strip() != '---' or '---' not in [x.strip() for x in lines[1:]]:
        raise Refuse('bad_frontmatter', f'{skill}: no frontmatter')
    fields = {}
    for line in lines[1:]:
        if line.strip() == '---':
            break
        key, sep, value = line.partition(':')
        if sep:
            fields[key.strip()] = value.strip()
    if fields.get('name') != skill or not fields.get('description'):
        raise Refuse('bad_frontmatter', f'{skill}: name must equal the directory and description must be set')
    return fields


def skill_files(root, skill):
    base = root / 'skills' / skill
    if base.is_symlink():
        raise Refuse('symlink', f'skills/{skill}')
    if not base.is_dir():
        raise Refuse('missing_skill', skill)
    found = []
    for dirpath, dirnames, filenames in os.walk(base):
        for name in dirnames + filenames:
            path = Path(dirpath) / name
            if path.is_symlink():
                raise Refuse('symlink', str(path.relative_to(root)))
        for name in filenames:
            path = Path(dirpath) / name
            if not path.is_file():
                raise Refuse('symlink', f'{path.relative_to(root)} is not a regular file')
            found.append(path.relative_to(base).as_posix())
    if 'SKILL.md' not in found:
        raise Refuse('missing_skill', f'{skill}: no SKILL.md')
    return sorted(found)


def plan_skill(root, skill, docs, needs):
    """Return {bundle path: (bytes, source)} for one skill; fill docs and needs."""
    out = {}
    skill_dir = f'skills/{skill}'
    for rel in skill_files(root, skill):
        data = (root / skill_dir / rel).read_bytes()
        source = f'{skill_dir}/{rel}'
        here = posixpath.dirname(f'{skill}/{rel}')
        if rel.endswith('.md'):
            try:
                text = data.decode('utf-8')
            except UnicodeDecodeError:
                raise Refuse('not_utf8', source)
            if rel == 'SKILL.md':
                frontmatter(skill, text)

            def rewrite(match):
                target = match.group(0)
                if EXTERNAL.match(target):
                    return target
                path, hash_, frag = target.partition('#')
                repo_rel = posixpath.normpath(posixpath.join(posixpath.dirname(source), path))
                if repo_rel == '..' or repo_rel.startswith('../') or posixpath.isabs(repo_rel):
                    raise Refuse('link_outside_repo', f'{source}: {target}')
                if repo_rel.startswith('skills/') and repo_rel.split('/')[1] in SKILLS:
                    # A link inside this skill or to a sibling bundled skill keeps its shape.
                    if not check_plain(root, repo_rel).is_file():
                        raise Refuse('missing_doc', f'{source}: {target}')
                    return target
                if not repo_rel.startswith('docs/'):
                    needs.setdefault(skill, set()).add(repo_rel)
                    return target
                if not check_plain(root, repo_rel).is_file():
                    raise Refuse('missing_doc', f'{source}: {target}')
                docs[repo_rel] = (root / repo_rel).read_bytes()
                return posixpath.relpath(repo_rel, here) + hash_ + frag

            data = REF.sub(rewrite, text).encode('utf-8')
        out[f'{skill}/{rel}'] = (data, source)
    return out


def plan(root):
    docs, needs, files = {}, {}, {}
    for skill in SKILLS:
        files.update(plan_skill(root, skill, docs, needs))
    for rel, data in docs.items():
        files[rel] = (data, rel)
    for name in ('LICENSE', 'NOTICE'):
        if not check_plain(root, name).is_file():
            raise Refuse('missing_license', name)
        files[name] = ((root / name).read_bytes(), name)
    files['README.md'] = (README.encode('utf-8'), None)
    manifest = {'schema_version': 1,
                'files': [{'path': p, 'sha256': sha256(files[p][0]), 'source': files[p][1]}
                          for p in sorted(files)],
                'skills': {s: {'needs_checkout': sorted(needs.get(s, ()))} for s in sorted(SKILLS)}}
    files['MANIFEST.json'] = ((json.dumps(manifest, indent=2, sort_keys=True) + '\n').encode(), None)
    return files, len(docs)


def write_file(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    path.chmod(0o644)


def build(root, output):
    output = Path(os.path.abspath(output))
    if os.path.lexists(output):
        raise Refuse('output_exists', str(output))
    if not output.parent.is_dir():
        raise Refuse('parent_missing', str(output.parent))
    files, doc_count = plan(root)
    tmp = Path(tempfile.mkdtemp(prefix=f'.{output.name}.tmp-', dir=output.parent))
    try:
        for rel, (data, _source) in files.items():
            write_file(tmp / rel, data)
        for directory in [tmp, *(p for p in tmp.rglob('*') if p.is_dir())]:
            directory.chmod(0o755)
        os.rename(tmp, output)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return doc_count


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Refuse('bad_arguments', message)


def main(argv=None):
    try:
        parser = Parser(description='Build a portable bundle of the records and review skills.')
        parser.add_argument('--output-dir', required=True, help='new folder to write; must not exist')
        parser.add_argument('--repo-root', default=str(Path(__file__).resolve().parents[1]),
                            help=argparse.SUPPRESS)
        args = parser.parse_args(argv)
        docs = build(Path(args.repo_root), args.output_dir)
    except Refuse as exc:
        print(json.dumps({'ok': False, 'error': exc.code, 'detail': exc.detail}, sort_keys=True))
        return 2
    except OSError as exc:
        print(json.dumps({'ok': False, 'error': 'build_failed', 'detail': type(exc).__name__}, sort_keys=True))
        return 2
    print(json.dumps({'ok': True, 'skills': len(SKILLS), 'docs': docs}))
    return 0


if __name__ == '__main__':
    sys.exit(main())
