"""tools/skills_bundle.py: portable records skills, links rewritten, refusals leave nothing."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('skills_bundle', ROOT / 'tools' / 'skills_bundle.py')
sb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sb)


def run(output, root=None):
    argv = ['--output-dir', str(output)] + (['--repo-root', str(root)] if root else [])
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = sb.main(argv)
    lines = out.getvalue().splitlines()
    assert len(lines) == 1, out.getvalue()
    return code, json.loads(lines[0])


def tree(base):
    return {p.relative_to(base).as_posix(): (p.read_bytes() if p.is_file() else None, p.stat().st_mode & 0o777)
            for p in sorted(Path(base).rglob('*'))}


def broken_links(base):
    """Relative links in bundled .md files that do not resolve inside the bundle."""
    bad = []
    for path in Path(base).rglob('*.md'):
        for match in sb.REF.finditer(path.read_text(encoding='utf-8')):
            target = match.group(0)
            if sb.EXTERNAL.match(target) or target.startswith('#'):
                continue
            if 'docs/' not in target and 'tools/' in target:
                continue  # listed as needs_checkout, kept as text
            file_part = target.partition('#')[0]
            if not (path.parent / file_part).resolve().is_file():
                bad.append(f'{path.name}: {target}')
    return bad


class Synthetic(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / 'repo'
        self.out_parent = self.tmp / 'out'
        self.out_parent.mkdir()
        self.out = self.out_parent / 'bundle'
        for name in sb.SKILLS:
            self.skill(name)
        for name in ('LICENSE', 'NOTICE'):
            self.put(name, name + ' text\n')
        self.put('docs/A.md', '# A\n')

    def put(self, rel, text):
        path = self.repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')

    def skill(self, name, body='Body.\n', front=None):
        front = front if front is not None else f'name: {name}\ndescription: does {name}'
        self.put(f'skills/{name}/SKILL.md', f'---\n{front}\n---\n\n{body}')

    def refused(self, code):
        before = sorted(p.name for p in self.out_parent.iterdir())
        status, result = run(self.out, self.repo)
        self.assertEqual((status, result['ok'], result['error']), (2, False, code))
        self.assertEqual(sorted(p.name for p in self.out_parent.iterdir()), before)  # nothing written

    def built(self):
        status, result = run(self.out, self.repo)
        self.assertEqual((status, result['ok']), (0, True), result)
        return result

    def test_missing_doc_refused(self):
        self.skill('records-capture', 'See [x](../../docs/NOPE.md).\n')
        self.refused('missing_doc')

    def test_bare_path_to_missing_doc_refused(self):
        self.skill('records-capture', 'Read ../../docs/NOPE.md now.\n')
        self.refused('missing_doc')

    def test_symlinked_file_refused(self):
        (self.repo / 'skills/records-capture/extra.txt').symlink_to(self.repo / 'LICENSE')
        self.refused('symlink')

    def test_symlinked_dir_and_doc_refused(self):
        (self.repo / 'skills/records-capture/refs').symlink_to(self.repo / 'docs')
        self.refused('symlink')
        os.unlink(self.repo / 'skills/records-capture/refs')
        shutil.copy(self.repo / 'docs/A.md', self.repo / 'real.md')
        (self.repo / 'docs/B.md').symlink_to(self.repo / 'real.md')
        self.skill('records-capture', 'See [b](../../docs/B.md).\n')
        self.refused('symlink')

    def test_bad_frontmatter_refused(self):
        for front in ('name: other\ndescription: d', 'name: records-capture\ndescription:', 'description: only'):
            self.skill('records-capture', front=front)
            self.refused('bad_frontmatter')
        self.put('skills/records-capture/SKILL.md', 'no frontmatter at all\n')
        self.refused('bad_frontmatter')

    def test_missing_skill_and_licence_refused(self):
        shutil.rmtree(self.repo / 'skills/independent-review')
        self.refused('missing_skill')
        self.skill('independent-review')
        os.unlink(self.repo / 'NOTICE')
        self.refused('missing_license')

    def test_link_outside_repo_refused(self):
        self.skill('records-capture', 'See [x](../../../elsewhere.md).\n')
        self.refused('link_outside_repo')

    def test_tools_link_kept_as_text_and_listed(self):
        self.put('tools/x.py', 'print(1)\n')
        self.skill('records-capture', 'Run [x](../../tools/x.py), see [ext](https://example.org/a) and [top](#top).\n')
        self.built()
        text = (self.out / 'records-capture/SKILL.md').read_text()
        self.assertIn('(../../tools/x.py)', text)
        self.assertIn('(https://example.org/a)', text)
        self.assertIn('(#top)', text)
        manifest = json.loads((self.out / 'MANIFEST.json').read_text())
        self.assertEqual(manifest['skills']['records-capture']['needs_checkout'], ['tools/x.py'])
        self.assertEqual(manifest['skills']['records-analysis']['needs_checkout'], [])
        self.assertFalse((self.out / 'tools').exists())

    def test_doc_linked_three_times_copied_once(self):
        for name in ('records-capture', 'records-analysis', 'records-publication'):
            self.skill(name, 'Link [a](../../docs/A.md#sec) and bare ../../docs/A.md here.\n')
        result = self.built()
        self.assertEqual(result['docs'], 1)
        manifest = json.loads((self.out / 'MANIFEST.json').read_text())
        self.assertEqual([f['path'] for f in manifest['files'] if f['path'].startswith('docs/')], ['docs/A.md'])
        text = (self.out / 'records-capture/SKILL.md').read_text()
        self.assertIn('[a](../docs/A.md#sec) and bare ../docs/A.md here', text)
        self.assertEqual(broken_links(self.out), [])

    def test_nested_reference_and_subdir_doc(self):
        self.put('docs/sub/D.md', 'd\n')
        self.put('skills/records-capture/references/R.md', 'See [d](../../../docs/sub/D.md).\n')
        self.built()
        self.assertIn('(../../docs/sub/D.md)', (self.out / 'records-capture/references/R.md').read_text())
        self.assertEqual(broken_links(self.out), [])

    def test_sibling_skill_link_kept(self):
        self.skill('records-capture', 'Next: [p](../records-publication/SKILL.md).\n')
        self.built()
        self.assertEqual(broken_links(self.out), [])

    def test_no_flock_private_kit_workers(self):
        self.skill('flock-x')
        self.put('private/secret.md', 's\n')
        self.put('kit/k.md', 'k\n')
        self.put('workers/w.md', 'w\n')
        self.skill('records-capture', 'See [w](../../workers/w.md) and [k](../../kit/k.md).\n')
        self.built()
        paths = list(tree(self.out))
        self.assertFalse([p for p in paths if p.startswith(('flock', 'private', 'kit', 'workers'))], paths)
        needs = json.loads((self.out / 'MANIFEST.json').read_text())['skills']['records-capture']['needs_checkout']
        self.assertEqual(needs, ['kit/k.md', 'workers/w.md'])

    def test_output_exists_untouched(self):
        self.out.mkdir()
        (self.out / 'keep.txt').write_text('mine')
        self.refused('output_exists')
        self.assertEqual((self.out / 'keep.txt').read_text(), 'mine')
        self.assertEqual(os.listdir(self.out), ['keep.txt'])

    def test_output_exists_as_file_or_dangling_link(self):
        (self.out_parent / 'bundle').symlink_to(self.tmp / 'nowhere')
        self.refused('output_exists')

    def test_parent_missing(self):
        self.out = self.tmp / 'no-such-dir' / 'bundle'
        status, result = run(self.out, self.repo)
        self.assertEqual((status, result['error']), (2, 'parent_missing'))
        self.assertFalse((self.tmp / 'no-such-dir').exists())

    def test_failure_mid_build_leaves_nothing(self):
        real = sb.write_file
        calls = []

        def flaky(path, data):
            calls.append(path)
            if len(calls) == 5:
                raise OSError('disk full')
            real(path, data)
        with mock.patch.object(sb, 'write_file', flaky):
            self.refused('build_failed')
        self.assertGreaterEqual(len(calls), 5)
        self.assertEqual(os.listdir(self.out_parent), [])

    def test_interrupt_mid_build_leaves_nothing(self):
        with mock.patch.object(sb, 'write_file', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                run(self.out, self.repo)
        self.assertEqual(os.listdir(self.out_parent), [])

    def test_bad_arguments_json_exit_2(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = sb.main([])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out.getvalue())['error'], 'bad_arguments')

    def test_twice_byte_identical_and_modes(self):
        self.put('tools/x.py', 'x\n')
        self.skill('records-capture', 'See [a](../../docs/A.md) and [x](../../tools/x.py).\n')
        other = self.out_parent / 'second'
        self.built()
        self.assertEqual(run(other, self.repo)[0], 0)
        first, second = tree(self.out), tree(other)
        self.assertEqual(first, second)
        for rel, (data, mode) in first.items():
            self.assertEqual(mode, 0o644 if data is not None else 0o755, rel)
        self.assertEqual(oct(self.out.stat().st_mode & 0o777), '0o755')


class RealRepo(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def verify_hashes(self, bundle):
        manifest = json.loads((bundle / 'MANIFEST.json').read_text())
        self.assertEqual(manifest['schema_version'], 1)
        self.assertEqual(list(manifest['skills']), sorted(sb.SKILLS))
        for entry in manifest['files']:
            self.assertEqual(sb.sha256((bundle / entry['path']).read_bytes()), entry['sha256'], entry['path'])
            if entry['source']:
                self.assertTrue((ROOT / entry['source']).is_file(), entry['source'])
        listed = {e['path'] for e in manifest['files']} | {'MANIFEST.json'}
        self.assertEqual(listed, {p for p, (d, _m) in tree(bundle).items() if d is not None})
        return manifest

    def test_real_repo_cli_bundle_resolves_and_moves(self):
        out = self.tmp / 'bundle'
        done = subprocess.run([sys.executable, '-B', str(ROOT / 'tools/skills_bundle.py'), '--output-dir', str(out)],
                              capture_output=True, text=True, cwd=self.tmp)
        self.assertEqual(done.returncode, 0, done.stderr)
        result = json.loads(done.stdout)
        self.assertEqual((result['ok'], result['skills']), (True, 9))
        self.assertGreaterEqual(result['docs'], 2)
        skills = sorted(p.name for p in out.iterdir() if p.is_dir() and p.name != 'docs')
        self.assertEqual(skills, sorted(sb.SKILLS))
        self.assertEqual(len(list((out / 'docs').iterdir())), result['docs'])
        for md in out.glob('*/**/*.md'):
            self.assertNotIn('../../', md.read_text(encoding='utf-8'), md)
        self.assertEqual(broken_links(out), [])
        self.assertIn('../docs/DATA-HANDLING.md', (out / 'records-analysis/SKILL.md').read_text())
        self.assertIn('../docs/RECORDS-STAGE-HANDOFF.md', (out / 'records-publication/SKILL.md').read_text())
        manifest = self.verify_hashes(out)
        self.assertTrue(all(isinstance(v['needs_checkout'], list) for v in manifest['skills'].values()))
        self.assertIn('flock-* skills', (out / 'README.md').read_text())
        self.assertTrue(all(not p.startswith(('flock', 'private', 'kit', 'workers')) for p in tree(out)))
        # Moved elsewhere: no repo above it, every link still resolves.
        moved_parent = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, moved_parent, True)
        moved = moved_parent / 'elsewhere'
        shutil.move(str(out), str(moved))
        self.assertEqual(broken_links(moved), [])
        self.verify_hashes(moved)

    def test_real_repo_twice_identical(self):
        a, b = self.tmp / 'a', self.tmp / 'b'
        self.assertEqual(run(a)[0], 0)
        self.assertEqual(run(b)[0], 0)
        self.assertEqual(tree(a), tree(b))

    def test_rewrite_changes_only_link_targets(self):
        out = self.tmp / 'bundle'
        run(out)
        for skill in sb.SKILLS:
            src = (ROOT / 'skills' / skill / 'SKILL.md').read_text(encoding='utf-8')
            got = (out / skill / 'SKILL.md').read_text(encoding='utf-8')
            self.assertEqual(src.replace('../../docs/', '../docs/'), got, skill)


if __name__ == '__main__':
    unittest.main()
