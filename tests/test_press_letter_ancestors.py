"""The private letter writer refuses linked campaign roots and ancestors before output (RF1045-2); fictional data only."""
import json
import os
import shutil
import stat
import subprocess
import sys
import unittest
from pathlib import Path

from campaign_tool.press import build_letter, build_press
from tests.test_press_letter import DAY, FIRST, NOW, REPO, SECOND, SLUG, LetterBase

NAME = f'{DAY}-{SLUG}.md'
LETTER_PATH = f'kit/press/letters/{NAME}'


def snapshot(top):
    """{path: (kind, mode, mtime_ns, bytes or link target)} for everything under top, never following links."""
    out = {}
    for item in [Path(top), *sorted(Path(top).rglob('*'))]:
        status = os.lstat(item)
        if stat.S_ISLNK(status.st_mode):
            payload = os.readlink(item)
        elif stat.S_ISREG(status.st_mode):
            payload = item.read_bytes()
        else:
            payload = None
        out[item.as_posix()] = (stat.S_IFMT(status.st_mode), stat.S_IMODE(status.st_mode), status.st_mtime_ns, payload)
    return out


class AncestorBase(LetterBase):
    def setUp(self):
        super().setUp()
        self.real = self.top / 'real'
        self.real.mkdir()
        self.real_root = self.real / 'campaign'
        self.root.rename(self.real_root)
        self.cwd = os.getcwd()
        self.addCleanup(os.chdir, self.cwd)
        self.root = self.real_root  # a plain, real campaign
        self.findings = self.root / 'content/findings'

    def root_link(self):
        link = self.top / 'alias'
        if not os.path.lexists(link):
            os.symlink(self.real_root, link)
        return link

    def parent_link(self):
        link = self.top / 'parent-alias'
        if not os.path.lexists(link):
            os.symlink(self.real, link)
        return link / 'campaign'

    def deep_parent_link(self):
        deep = self.top / 'deep'
        (deep / 'mid').mkdir(parents=True)
        shutil.move(self.real_root, deep / 'mid' / 'campaign')
        link = self.top / 'high-alias'
        os.symlink(deep, link)
        return link / 'mid' / 'campaign', deep / 'mid' / 'campaign'

    def refuses_via(self, path, pattern, **kw):
        before = snapshot(self.top)
        with self.assertRaisesRegex(ValueError, pattern):
            build_letter(path, [FIRST], DAY, now=NOW, **kw)
        self.assertEqual(snapshot(self.top), before, 'a refused letter must create, chmod and write nothing')


class LinkedRootTests(AncestorBase):
    def test_linked_root_refuses_before_creating_private_output(self):
        alias = self.root_link()
        self.assertFalse((self.real_root / 'kit').exists())
        self.refuses_via(alias, r'^alias/ must not be a symlink$')
        self.assertFalse((self.real_root / 'kit').exists())
        with self.assertRaisesRegex(ValueError, r'^alias/ must not be a symlink$'):
            build_press(alias, [FIRST], DAY)  # the release writer's existing refusal is the control

    def test_linked_parent_with_real_campaign_leaf_refuses(self):
        path = self.parent_link()
        self.assertFalse(os.path.islink(path))
        self.refuses_via(path, r'^parent-alias/ must not be a symlink$')
        self.assertFalse((self.real_root / 'kit').exists())
        high, real = self.deep_parent_link()
        self.assertFalse(os.path.islink(high))
        self.refuses_via(high, r'^high-alias/ must not be a symlink$')
        self.assertFalse((real / 'kit').exists())

    def test_existing_output_folders_do_not_bypass_the_root_guard(self):
        # Every immediate output folder is an ordinary folder, so only the root guard can catch the alias.
        for part in ('kit', 'kit/press', 'kit/press/letters'):
            (self.real_root / part).mkdir(exist_ok=True)
            os.chmod(self.real_root / part, 0o755)
        self.refuses_via(self.root_link(), r'alias/ must not be a symlink')
        for part in ('kit', 'kit/press', 'kit/press/letters'):
            self.assertEqual(stat.S_IMODE(os.stat(self.real_root / part).st_mode), 0o755)
        self.assertEqual(list((self.real_root / 'kit/press/letters').iterdir()), [])

    def test_existing_primary_and_record_survive_alias_refusal(self):
        first = build_letter(self.real_root, [FIRST], DAY, now=NOW)
        for part in ('kit', 'kit/press', 'kit/press/letters'):
            os.chmod(self.real_root / part, 0o755)
        self.save(dict(self.first, summary='A changed valid public summary.'))
        for path in (self.root_link(), self.parent_link()):
            with self.subTest(path=path.as_posix()):
                self.refuses_via(path, r'alias/ must not be a symlink')
        self.assertEqual(first['path'], LETTER_PATH)
        self.assertEqual(stat.S_IMODE(os.stat(self.real_root / 'kit/press/letters').st_mode), 0o755)

    def test_suggestion_branch_cannot_bypass_campaign_guard(self):
        first = build_letter(self.real_root, [FIRST], DAY, now=NOW)
        (self.real_root / first['path']).write_text('Organizer edit')
        suggested = build_letter(self.real_root, [FIRST], DAY, now=NOW)
        self.assertTrue(suggested['kept_edited'])
        self.save(dict(self.first, summary='A changed valid public summary.'))
        for path in (self.root_link(), self.parent_link()):
            with self.subTest(path=path.as_posix()):  # edited original and an unedited suggestion both exist
                self.refuses_via(path, r'alias/ must not be a symlink')
        # The suggestion folder absent: the edited original would route a new suggestion there; still nothing is made.
        shutil.rmtree(self.real_root / 'kit/press/letters.suggested')
        for path in (self.root_link(), self.parent_link()):
            with self.subTest(path=path.as_posix(), suggestion='absent'):
                self.refuses_via(path, r'alias/ must not be a symlink')
        self.assertFalse((self.real_root / 'kit/press/letters.suggested').exists())

    def test_relative_unresolved_alias_refuses_but_relative_real_root_succeeds(self):
        self.root_link()
        self.parent_link()
        os.chdir(self.top)
        before = snapshot(self.top)
        for relative in ('alias', 'parent-alias/campaign'):
            with self.subTest(relative=relative):
                with self.assertRaisesRegex(ValueError, r'alias/ must not be a symlink'):
                    build_letter(Path(relative), [FIRST], DAY, now=NOW)
                self.assertEqual(snapshot(self.top), before)
        expected = build_letter(self.real_root, [FIRST], DAY, now=NOW)
        written = (self.real_root / LETTER_PATH).read_bytes()
        shutil.rmtree(self.real_root / 'kit')
        relative = build_letter(Path('real/campaign'), [FIRST], DAY, now=NOW)
        self.assertEqual(relative, expected)
        self.assertEqual((self.real_root / LETTER_PATH).read_bytes(), written)


class RetainedBehaviourTests(AncestorBase):
    def test_real_root_keeps_healthy_letter_bytes_and_private_modes(self):
        first = build_letter(self.real_root, [FIRST], DAY, now=NOW)
        self.assertEqual(first['path'], LETTER_PATH)
        self.assertEqual((first['findings'], first['dropped_findings'], first['kept_edited'], first['sent']),
                         ([FIRST], [], False, False))
        text = (self.real_root / LETTER_PATH).read_bytes()
        again = build_letter(self.real_root, [FIRST], DAY, now=NOW)
        self.assertEqual(again, first)
        self.assertEqual((self.real_root / LETTER_PATH).read_bytes(), text)
        for part in ('kit', 'kit/press', 'kit/press/letters'):
            self.assertEqual(stat.S_IMODE(os.stat(self.real_root / part).st_mode), 0o700, part)
        for part in (LETTER_PATH, 'kit/press/letters/.written.json'):
            self.assertEqual(stat.S_IMODE(os.stat(self.real_root / part).st_mode), 0o600, part)
        self.assertEqual(list(json.loads((self.real_root / 'kit/press/letters/.written.json').read_text())), [NAME])
        (self.real_root / LETTER_PATH).write_text('Organizer edit')
        suggestion = build_letter(self.real_root, [FIRST], DAY, now=NOW)
        self.assertEqual((suggestion['path'], suggestion['kept_edited']),
                         (f'kit/press/letters.suggested/{NAME}', True))
        self.assertEqual((self.real_root / f'kit/press/letters.suggested/{NAME}').read_bytes(), text)

    def test_descendant_guard_and_unused_suggestion_precedence_are_retained(self):
        outside = self.top / 'outside'
        outside.mkdir()
        for part in ('kit', 'kit/press', 'kit/press/letters'):
            with self.subTest(part=part):
                shutil.rmtree(self.real_root / 'kit', ignore_errors=True)
                (self.real_root / part).parent.mkdir(parents=True, exist_ok=True)
                os.symlink(outside, self.real_root / part)
                self.refuses_via(self.real_root, 'symlink')
                (self.real_root / part).unlink()
        shutil.rmtree(self.real_root / 'kit', ignore_errors=True)
        first = build_letter(self.real_root, [FIRST], DAY, now=NOW)
        # An unsafe but unused suggestion folder does not block refreshing the unedited primary.
        os.symlink(outside, self.real_root / 'kit/press/letters.suggested')
        self.assertFalse(build_letter(self.real_root, [FIRST], DAY, now=NOW)['kept_edited'])
        # Once the primary is edited the suggestion folder is selected, and its link refuses.
        (self.real_root / first['path']).write_text('Organizer edit')
        self.refuses_via(self.real_root, 'symlink')
        # Earlier argument refusals keep their precedence over the root guard.
        alias = self.root_link()
        with self.assertRaisesRegex(ValueError, '--words'):
            build_letter(alias, [FIRST], DAY, words=10, now=NOW)
        with self.assertRaisesRegex(ValueError, 'unknown|not published|no finding|fnd_'):
            build_letter(alias, ['fnd_9999999999999999'], DAY, now=NOW)

    def test_real_cli_and_resolved_callable_remain_compatible(self):
        base = [sys.executable, '-B', '-m', 'campaign_tool', 'press']
        letter = subprocess.run([*base, 'letter', '--directory', str(self.real_root), '--finding', FIRST, '--date', DAY],
                                cwd=REPO, capture_output=True, text=True, timeout=60)
        self.assertEqual(letter.returncode, 0, letter.stderr)
        self.assertTrue((self.real_root / LETTER_PATH).is_file())
        draft = subprocess.run([*base, 'draft', '--directory', str(self.real_root), '--finding', FIRST, '--date', DAY],
                               cwd=REPO, capture_output=True, text=True, timeout=60)
        self.assertEqual(draft.returncode, 0, draft.stderr)
        self.assertTrue((self.real_root / f'kit/press/{NAME}').is_file())
        # An explicitly resolved alias path is a real path and keeps working through the API.
        shutil.rmtree(self.real_root / 'kit')
        result = build_letter(Path(self.root_link()).resolve(), [FIRST], DAY, now=NOW)
        self.assertEqual(result['path'], LETTER_PATH)
        self.assertEqual(build_press(self.real_root, [SECOND], DAY)['path'], f'kit/press/{DAY}-cedar-second.md')


if __name__ == '__main__':
    unittest.main()
