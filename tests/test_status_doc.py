"""docs/STATUS.md stays true: named paths exist, links resolve, Flock tools/skills listed."""

import os
from pathlib import Path
import re
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
TOKEN = re.compile(r'`([^`\n]+)`')
LINK = re.compile(r'\]\(([^)\s]+)\)')


def path_tokens(text):
    """Backticked tokens that are paths: they contain '/' and no space."""
    return [t for t in TOKEN.findall(text) if '/' in t and ' ' not in t]


def path_failures(text, root):
    failures = []
    for token in path_tokens(text):
        path = Path(root) / token
        if os.path.lexists(path) and not path.exists():
            failures.append(f'STATUS.md names broken symlink {token}')
        elif not path.exists():
            failures.append(f'STATUS.md names missing path {token}')
        elif token.endswith('/') and not path.is_dir():
            failures.append(f'STATUS.md names {token} as a directory but it is not one')
        elif not token.endswith('/') and not path.is_file():
            failures.append(f'STATUS.md names {token} as a file but it is not a regular file')
    return failures


def link_failures(text, root):
    failures = []
    for target in LINK.findall(text):
        if target.startswith(('http:', 'https:', '#')):
            continue
        if not (Path(root) / 'docs' / target.split('#', 1)[0]).is_file():
            failures.append(f'STATUS.md links missing document {target}')
    return failures


def coverage_failures(text, root):
    named = set(path_tokens(text))
    root = Path(root)
    wanted = [f'tools/{p.name}' for p in root.glob('tools/flock_*.py') if p.is_file()]
    wanted += [f'skills/{p.name}/' for p in root.glob('skills/flock-*') if p.is_dir()]
    return [f'STATUS.md does not name {rel}' for rel in sorted(wanted) if rel not in named]


class RealPageTest(unittest.TestCase):
    def test_real_page_passes(self):
        text = (ROOT / 'docs' / 'STATUS.md').read_text(encoding='utf-8')
        self.assertEqual(path_failures(text, ROOT) + link_failures(text, ROOT)
                         + coverage_failures(text, ROOT), [])
        for name in ('verify', 'golden', 'batch'):
            self.assertIn(f'`tools/flock_review_{name}.py`', text)


class SyntheticTreeTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        for directory in ('docs', 'tools', 'skills'):
            (self.root / directory).mkdir()
        self.addCleanup(self._tmp.cleanup)

    def touch(self, rel):
        (self.root / rel).write_text('x', encoding='utf-8')

    def test_missing_file_is_named(self):
        self.assertEqual(path_failures('Evidence: `tools/gone.py`.', self.root),
                         ['STATUS.md names missing path tools/gone.py'])

    def test_directory_and_file_mismatch(self):
        self.touch('skills/flock-x')
        (self.root / 'tools' / 'a.py').mkdir()
        failures = path_failures('`skills/flock-x/` and `tools/a.py`', self.root)
        self.assertEqual(len(failures), 2)
        self.assertIn('skills/flock-x/ as a directory', failures[0])
        self.assertIn('tools/a.py as a file', failures[1])

    def test_existing_file_and_directory_pass(self):
        self.touch('tools/a.py')
        (self.root / 'skills' / 'flock-x').mkdir()
        self.assertEqual(path_failures('`tools/a.py`, `skills/flock-x/`', self.root), [])

    def test_non_path_tokens_ignored(self):
        text = '`send_request` `status: draft` `--fake` `PUBLISH.json` `records run`'
        self.assertEqual(path_failures(text, self.root), [])

    def test_links(self):
        self.touch('LAUNCH.md')
        self.touch('docs/RECORDS-OCR.md')
        text = ('[L](../LAUNCH.md) [A](RECORDS-OCR.md#stage) [W](https://example.org/x.md) '
                '[H](http://example.org/y.md) [S](#section) [X](X.md) [B](GONE.md#stage)')
        self.assertEqual(link_failures(text, self.root),
                         ['STATUS.md links missing document X.md',
                          'STATUS.md links missing document GONE.md#stage'])

    def test_coverage_requires_flock_tools_and_skills(self):
        self.touch('tools/flock_new.py')
        self.touch('tools/flock_new_helper.txt')
        self.touch('tools/other.py')
        (self.root / 'skills' / 'flock-new').mkdir()
        (self.root / 'skills' / 'other').mkdir()
        self.assertEqual(coverage_failures('prose mentions flock_new.py and flock-new', self.root),
                         ['STATUS.md does not name skills/flock-new/',
                          'STATUS.md does not name tools/flock_new.py'])
        self.assertEqual(coverage_failures('`tools/flock_new.py` `skills/flock-new/`', self.root), [])

    def test_broken_symlink_fails(self):
        (self.root / 'tools' / 'link.py').symlink_to(self.root / 'tools' / 'absent.py')
        self.assertEqual(path_failures('`tools/link.py`', self.root),
                         ['STATUS.md names broken symlink tools/link.py'])


if __name__ == '__main__':
    unittest.main()
