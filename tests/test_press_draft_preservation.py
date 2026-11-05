"""Press-draft preservation: organizer edits to originals and suggestions survive reruns (fictional content only)."""
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool.press import build_press

REPO = Path(__file__).resolve().parents[1]
FIRST = 'fnd_0000000000000001'
SLUG = 'cedar-policy-posted'
DAY = '2026-03-01'
NAME = f'{DAY}-{SLUG}.md'
PRIMARY = f'kit/press/{NAME}'
SUGGESTED = f'kit/press.suggested/{NAME}'


def sha(data):
    return hashlib.sha256(data).hexdigest()


def snapshot(path):
    """{relative path: (mode, bytes | link target | None)} of everything under path."""
    out = {}
    for item in sorted(Path(path).rglob('*')):
        info = os.lstat(item)
        key = item.relative_to(path).as_posix()
        if stat.S_ISLNK(info.st_mode):
            body = os.readlink(item)
        elif stat.S_ISREG(info.st_mode):
            body = item.read_bytes()
        else:
            body = None
        out[key] = (stat.S_IMODE(info.st_mode), body)
    return out


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='fictional-press-keep-')
        self.addCleanup(self.tmp.cleanup)
        self.top = Path(self.tmp.name)
        self.root = self.top / 'campaign'
        shutil.copytree(REPO / 'examples/fictional-campaign', self.root, ignore=shutil.ignore_patterns('public'))
        self.finding = self.root / 'content/findings' / (SLUG + '.json')

    def run_draft(self):
        return build_press(self.root, [FIRST], DAY)

    def change_summary(self, text):
        data = json.loads(self.finding.read_text())
        data['summary'] = text
        self.finding.write_text(json.dumps(data))

    def two_generations(self):
        """Edited original -> generated suggestion; returns the suggestion path."""
        self.run_draft()
        (self.root / PRIMARY).write_text('Original edit')
        self.assertEqual(self.run_draft()['path'], SUGGESTED)
        return self.root / SUGGESTED

    def record(self, rel):
        return json.loads((self.root / rel).read_text())

    def cli(self, *args):
        return subprocess.run([sys.executable, '-B', '-m', 'campaign_tool', 'press', 'draft', '--directory',
                               str(self.root), *args], cwd=REPO, capture_output=True, text=True, timeout=60)


class PreservationTests(Base):
    def test_edited_suggestion_is_preserved_without_any_write(self):
        suggestion = self.two_generations()
        suggestion.write_text('Suggestion edit')
        before = snapshot(self.root / 'kit')
        self.change_summary('A newer summary.')
        with self.assertRaisesRegex(ValueError, 'kit/press.suggested/.*move it aside'):
            self.run_draft()
        self.assertEqual(snapshot(self.root / 'kit'), before)
        self.assertEqual(suggestion.read_text(), 'Suggestion edit')
        self.assertEqual((self.root / PRIMARY).read_text(), 'Original edit')

    def test_generated_suggestion_refreshes_only_its_own_record(self):
        suggestion = self.two_generations()
        primary_record = (self.root / 'kit/press/.written.json').read_bytes()
        primary = (self.root / PRIMARY).read_bytes()
        other = 'a-other.md'
        record = self.record('kit/press.suggested/.written.json')
        self.assertEqual(record, {NAME: sha(suggestion.read_bytes())})
        record[other] = 'f' * 64
        (self.root / 'kit/press.suggested/.written.json').write_text(json.dumps(record))
        self.assertFalse(self.run_draft() is None)
        self.change_summary('A newer summary.')
        result = self.run_draft()
        self.assertTrue(result['kept_edited'])
        self.assertIn('A newer summary.', suggestion.read_text())
        record = self.record('kit/press.suggested/.written.json')
        self.assertEqual(list(record), sorted([other, NAME]))
        self.assertEqual(record[NAME], sha(suggestion.read_bytes()))
        self.assertEqual((self.root / PRIMARY).read_bytes(), primary)
        self.assertEqual((self.root / 'kit/press/.written.json').read_bytes(), primary_record)

    def test_legacy_untracked_suggestion_requires_manual_move(self):
        suggestion = self.two_generations()
        (self.root / 'kit/press.suggested/.written.json').unlink()
        before = snapshot(self.root / 'kit')
        with self.assertRaisesRegex(ValueError, 'move it aside'):
            self.run_draft()
        self.assertEqual(snapshot(self.root / 'kit'), before)
        suggestion.rename(self.top / 'moved.md')
        self.assertEqual(self.run_draft()['path'], SUGGESTED)
        self.assertEqual(self.record('kit/press.suggested/.written.json'), {NAME: sha(suggestion.read_bytes())})

    def test_nonregular_original_is_kept_and_nonregular_suggestion_refuses(self):
        outside = self.top / 'outside'
        outside.write_text('untouched')
        (self.root / 'kit/press').mkdir(parents=True)
        original = self.root / PRIMARY
        for make in (lambda: os.symlink(outside, original), lambda: os.symlink(self.top / 'gone', original),
                     original.mkdir, lambda: os.mkfifo(original)):
            with self.subTest(make=make):
                if os.path.lexists(original):
                    original.rmdir() if original.is_dir() and not original.is_symlink() else original.unlink()
                make()
                self.assertEqual(self.run_draft()['path'], SUGGESTED)
                self.assertTrue(os.path.lexists(original))
                suggestion = self.root / SUGGESTED
                suggestion.unlink()
                (self.root / 'kit/press.suggested/.written.json').unlink()
        for make in (lambda: os.symlink(outside, suggestion), lambda: os.symlink(self.top / 'gone', suggestion),
                     suggestion.mkdir, lambda: os.mkfifo(suggestion)):
            with self.subTest(suggestion=make):
                if os.path.lexists(suggestion):
                    suggestion.rmdir() if suggestion.is_dir() and not suggestion.is_symlink() else suggestion.unlink()
                make()
                before = snapshot(self.root / 'kit')
                with self.assertRaisesRegex(ValueError, 'move it aside'):
                    self.run_draft()
                self.assertEqual(snapshot(self.root / 'kit'), before)
                self.assertEqual(outside.read_text(), 'untouched')


class RefusalTests(Base):
    BAD = {
        'malformed': b'{', 'duplicate': b'{"a.md": "%s", "a.md": "%s"}' % (b'0' * 64, b'0' * 64),
        'nonobject': b'[]', 'hash type': b'{"a.md": 5}', 'hash format': b'{"a.md": "ABC"}',
        'path key': b'{"../a.md": "%s"}' % (b'0' * 64), 'bad utf8': b'\xff\xfe', 'oversized': b' ' * ((1 << 20) + 1),
    }

    def break_record(self, path, how):
        path.parent.mkdir(parents=True, exist_ok=True)
        if how in self.BAD:
            path.write_bytes(self.BAD[how])
        elif how == 'symlink':
            os.symlink(self.top / 'target', path)
            (self.top / 'target').write_text('{}')
        elif how == 'dangling':
            os.symlink(self.top / 'nowhere', path)
        elif how == 'directory':
            path.mkdir()
        else:
            os.mkfifo(path)

    def reset_kit(self):
        kit = self.root / 'kit'
        if kit.is_symlink():
            kit.unlink()
        else:
            shutil.rmtree(kit, ignore_errors=True)

    def test_malformed_records_refuse_before_mkdir_or_chmod(self):
        for how in list(self.BAD) + ['symlink', 'dangling', 'directory', 'fifo']:
            with self.subTest(primary=how):
                shutil.rmtree(self.root / 'kit', ignore_errors=True)
                self.break_record(self.root / 'kit/press/.written.json', how)
                (self.root / 'kit/press').chmod(0o755)
                before = snapshot(self.root / 'kit')
                with self.assertRaisesRegex(ValueError, 'fix or remove it'):
                    self.run_draft()
                self.assertEqual(snapshot(self.root / 'kit'), before)
                self.assertFalse((self.root / 'kit/press.suggested').exists())
        for how in list(self.BAD) + ['symlink', 'dangling', 'directory', 'fifo']:
            with self.subTest(suggestion=how):
                shutil.rmtree(self.root / 'kit', ignore_errors=True)
                self.run_draft()
                (self.root / PRIMARY).write_text('edit')
                self.break_record(self.root / 'kit/press.suggested/.written.json', how)
                before = snapshot(self.root / 'kit')
                with self.assertRaisesRegex(ValueError, 'fix or remove it'):
                    self.run_draft()
                self.assertEqual(snapshot(self.root / 'kit'), before)

    def test_output_ancestor_refusal_and_unused_suggestion_is_ignored(self):
        real = self.top / 'real'
        shutil.move(self.root, real)
        os.symlink(real, self.root)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            self.run_draft()
        self.assertFalse((real / 'kit').exists())
        os.unlink(self.root)
        shutil.move(real, self.root)
        outside = self.top / 'outside'
        outside.mkdir()
        for rel in ('kit', 'kit/press'):
            with self.subTest(link=rel):
                self.reset_kit()
                if rel == 'kit/press':
                    (self.root / 'kit').mkdir()
                os.symlink(outside, self.root / rel)
                with self.assertRaisesRegex(ValueError, 'symlink'):
                    self.run_draft()
                self.assertEqual(list(outside.iterdir()), [])
        self.reset_kit()
        self.run_draft()
        (self.root / PRIMARY).write_text('edit')
        os.symlink(outside, self.root / 'kit/press.suggested')
        with self.assertRaisesRegex(ValueError, 'symlink'):
            self.run_draft()
        self.assertEqual(list(outside.iterdir()), [])
        self.reset_kit()
        self.run_draft()
        os.symlink(outside, self.root / 'kit/press.suggested')
        self.assertFalse(self.run_draft()['kept_edited'])
        self.assertEqual(list(outside.iterdir()), [])

    def test_write_failure_does_not_advance_selected_hash(self):
        suggestion = self.two_generations()
        record_path = self.root / 'kit/press.suggested/.written.json'
        before = record_path.read_bytes()
        self.change_summary('A newer summary.')
        with mock.patch('campaign_tool.press._write_text', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.run_draft()
        self.assertEqual(record_path.read_bytes(), before)
        with mock.patch('campaign_tool.press._write_json', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.run_draft()
        self.assertIn('A newer summary.', suggestion.read_text())
        self.assertEqual(record_path.read_bytes(), before)
        with self.assertRaisesRegex(ValueError, 'move it aside'):
            self.run_draft()

    def test_cli_preservation_refusal_is_actionable(self):
        result = self.cli('--finding', FIRST, '--date', DAY)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout[:result.stdout.rindex('}') + 1]),
                         {'path': PRIMARY, 'findings': [FIRST], 'kept_edited': False, 'sent': False})
        (self.root / PRIMARY).write_text('edit')
        self.assertEqual(self.cli('--finding', FIRST, '--date', DAY).returncode, 0)
        (self.root / SUGGESTED).write_text('second edit')
        result = self.cli('--finding', FIRST, '--date', DAY)
        self.assertEqual(result.returncode, 1)
        self.assertIn(SUGGESTED, result.stderr)
        self.assertIn('move it aside', result.stderr)
        self.assertNotIn('Traceback', result.stderr)
        self.assertNotIn('Draft written', result.stdout)
        self.assertEqual((self.root / SUGGESTED).read_text(), 'second edit')


if __name__ == '__main__':
    unittest.main()
