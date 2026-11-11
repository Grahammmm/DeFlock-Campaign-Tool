"""Campaign flyer --replace refuses directory/special-file outputs before changing anything (synthetic, offline)."""
import os
import stat
import subprocess
import sys
import unittest
from pathlib import Path

from campaign_tool.flyer import build
from tests import test_flyer as base

REPO = base.REPO


def tree(folder):
    """Entry type, mode, mtime (and bytes of regular files only) for every member."""
    out = {}
    for path in sorted(Path(folder).rglob('*')) + [Path(folder)]:
        info = os.lstat(path)
        data = path.read_bytes() if stat.S_ISREG(info.st_mode) else None
        out[str(path)] = (stat.S_IFMT(info.st_mode), stat.S_IMODE(info.st_mode), info.st_mtime_ns, data)
    return out


class Admission(unittest.TestCase):
    # borrow fixture helpers by name without subclassing (and so re-running) the historical TestCase
    for _helper in ('setUp', 'site', 'finding', 'html', 'text', 'cli', 'branded'):
        locals()[_helper] = base.FlyerTests.__dict__[_helper]
    del _helper

    def make(self, kind, path):
        if kind == 'dir':
            path.mkdir()
        else:
            os.mkfifo(path)

    def expect_refusal(self, name, **kw):
        before = tree(self.root / 'kit')
        with self.assertRaises(ValueError) as caught:
            build(self.root, replace=True, **kw)
        self.assertEqual(str(caught.exception), f'kit/flyer/{name} is not a regular file; move it aside first')
        self.assertEqual(tree(self.root / 'kit'), before)

    def test_late_text_obstruction_refuses_before_html_replacement(self):
        for kind in ('dir', 'fifo'):
            with self.subTest(kind=kind):
                self.finding(1)
                build(self.root, replace=True) if (self.folder / 'flyer.html').exists() else build(self.root)
                (self.folder / 'flyer.txt').unlink()
                self.make(kind, self.folder / 'flyer.txt')
                self.finding(2, published='2026-03-01T18:00:00+00:00')
                os.chmod(self.folder, 0o755)
                self.expect_refusal('flyer.txt')
                self.assertEqual(stat.S_IMODE(os.lstat(self.folder).st_mode), 0o755)
                self.assertFalse([p for p in self.folder.iterdir() if p.name.startswith('.')])
                (self.folder / 'flyer.txt').rmdir() if kind == 'dir' else (self.folder / 'flyer.txt').unlink()

    def test_html_obstruction_refused(self):
        for kind in ('dir', 'fifo'):
            with self.subTest(kind=kind):
                self.finding(1)
                self.folder.mkdir(parents=True, exist_ok=True)
                (self.folder / 'flyer.txt').write_text('old')
                self.make(kind, self.folder / 'flyer.html')
                self.expect_refusal('flyer.html')
                (self.folder / 'flyer.html').rmdir() if kind == 'dir' else (self.folder / 'flyer.html').unlink()
                (self.folder / 'flyer.txt').unlink()

    def test_new_logo_directory_preserves_previous_logo_and_twins(self):
        self.branded()
        build(self.root)
        self.assertTrue((self.folder / 'logo.png').exists())
        assets = self.content / 'public-assets'
        (assets / 'mark.svg').write_text('<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"/>')
        self.site({'brand': {'logo': 'mark.svg', 'logo_alt': 'Mark'}})
        (self.folder / 'logo.svg').mkdir()
        self.expect_refusal('logo.svg')
        self.assertTrue((self.folder / 'logo.png').is_file())

    def test_logo_fifo_refused_and_retry_succeeds(self):
        self.branded()
        os.mkfifo(self.folder / 'logo.png')
        self.expect_refusal('logo.png')
        (self.folder / 'logo.png').unlink()
        report = build(self.root, replace=True)
        self.assertEqual(report['written'], ['kit/flyer/flyer.html', 'kit/flyer/flyer.txt', 'kit/flyer/logo.png'])

    def test_no_replace_message_and_symlink_precedence_unchanged(self):
        self.finding(1)
        self.folder.mkdir(parents=True)
        os.mkfifo(self.folder / 'flyer.txt')
        with self.assertRaises(ValueError) as caught:
            build(self.root)
        self.assertEqual(str(caught.exception), 'flyer exists (kit/flyer/flyer.txt); use --replace to overwrite')
        (self.folder / 'flyer.txt').unlink()
        (self.folder / 'flyer.txt').symlink_to(self.root / 'nowhere')
        with self.assertRaises(ValueError) as caught:
            build(self.root, replace=True)
        self.assertIn('symlink', str(caught.exception))

    def test_regular_and_hardlinked_targets_still_replaced(self):
        self.finding(1)
        build(self.root)
        peer = self.root.parent / 'peer.txt'
        os.link(self.folder / 'flyer.txt', peer)
        before = (peer.read_bytes(), os.lstat(peer).st_mtime_ns, stat.S_IMODE(os.lstat(peer).st_mode))
        self.finding(2, published='2026-03-01T18:00:00+00:00')
        build(self.root, replace=True)
        self.assertEqual((peer.read_bytes(), os.lstat(peer).st_mtime_ns, stat.S_IMODE(os.lstat(peer).st_mode)), before)
        self.assertIn('Finding 2', self.text())
        self.assertEqual(stat.S_IMODE(os.lstat(self.folder / 'flyer.txt').st_mode), 0o600)

    def test_real_cli_refuses_cleanly_without_changing_outputs(self):
        self.finding(1)
        build(self.root)
        (self.folder / 'flyer.txt').unlink()
        (self.folder / 'flyer.txt').mkdir()
        self.finding(2, published='2026-03-01T18:00:00+00:00')
        before = tree(self.root / 'kit')
        result = self.cli('--replace')
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, '')
        self.assertIn('kit/flyer/flyer.txt is not a regular file; move it aside first', result.stderr)
        self.assertNotIn('Traceback', result.stderr)
        self.assertEqual(tree(self.root / 'kit'), before)


if __name__ == '__main__':
    unittest.main()
