"""Descriptor ownership of the suspected-location reader when stream construction fails (synthetic data only)."""
import errno
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool import content_suspected as cs
from campaign_tool.content import ContentError

REAL_OPEN, REAL_FDOPEN, REAL_CLOSE = os.open, os.fdopen, os.close
FIXED = ('content/suspected-locations.json: must be readable regular UTF-8 JSON without duplicate keys, '
         'at most 512 KiB (no symlinks)')
GOOD = {'schema_version': 1, 'entries': []}
ROW = {'id': 'a-one', 'place_label': 'Fictional Corner', 'latitude': 36.123456, 'longitude': -121.987654,
       'source_kind': 'records', 'source': 'https://example.org/doc', 'as_of': '2020-01-01'}


class Fatal(BaseException):
    pass


class OwnershipTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'content').mkdir()
        self.file = self.root / cs.FILE
        self.leaked = []
        self.addCleanup(self._close_leaks)

    def _close_leaks(self):
        for fd in self.leaked:
            try:
                REAL_CLOSE(fd)
            except OSError:
                pass

    def write(self, doc):
        self.file.write_text(json.dumps(doc), encoding='utf-8')

    def tracked_open(self):
        opened = []

        def opener(*args, **kwargs):
            fd = REAL_OPEN(*args, **kwargs)
            opened.append(fd)
            self.leaked.append(fd)
            return fd
        return opened, opener

    def is_open(self, fd):
        try:
            os.fstat(fd)
            return True
        except OSError as error:
            self.assertEqual(error.errno, errno.EBADF)
            return False

    def test_fdopen_failure_before_transfer_closes_real_descriptor(self):
        self.write(GOOD)
        opened, opener = self.tracked_open()
        with mock.patch.object(os, 'open', opener), mock.patch.object(os, 'fdopen', side_effect=OSError('boom')):
            with self.assertRaises(OSError):
                cs._document(self.file)
            for _ in range(5):
                problems = []
                self.assertIsNone(cs.read_suspected(self.root, None, [], problems=problems))
                self.assertEqual(problems, [FIXED])
                with self.assertRaises(ContentError) as caught:
                    cs.read_suspected(self.root, None, [])
                self.assertEqual(str(caught.exception), FIXED)
        self.assertEqual(len(opened), 11)
        self.assertFalse([fd for fd in opened if self.is_open(fd)])

    def test_fdopen_failure_after_consuming_descriptor_preserves_original(self):
        self.write(GOOD)
        sentinel = OSError('constructor failed after close')

        def consuming(fd, *args, **kwargs):
            REAL_CLOSE(fd)
            raise sentinel
        with mock.patch.object(os, 'fdopen', consuming):
            with self.assertRaises(OSError) as caught:
                cs._document(self.file)
            self.assertIs(caught.exception, sentinel)
            problems = []
            self.assertIsNone(cs.read_suspected(self.root, None, [], problems=problems))
            self.assertEqual(problems, [FIXED])

    def test_failed_handoff_cleanup_error_does_not_mask_exception(self):
        self.write(GOOD)
        opened, opener = self.tracked_open()
        for sentinel in (OSError('first'), KeyboardInterrupt(), Fatal()):
            with mock.patch.object(os, 'open', opener), mock.patch.object(os, 'fdopen', side_effect=sentinel), \
                    mock.patch.object(os, 'close', side_effect=OSError(errno.EIO, 'close failed')):
                with self.assertRaises(type(sentinel)) as caught:
                    cs._document(self.file)
                self.assertIs(caught.exception, sentinel)
        fatal = Fatal()
        with mock.patch.object(os, 'fdopen', side_effect=fatal):
            with self.assertRaises(Fatal) as caught:
                cs.read_suspected(self.root, None, [], problems=[])
            self.assertIs(caught.exception, fatal)

    def test_successful_handoff_has_one_owner(self):
        self.write(GOOD)
        closes = []
        opened, opener = self.tracked_open()
        with mock.patch.object(os, 'open', opener), mock.patch.object(os, 'close', lambda fd: closes.append(fd) or REAL_CLOSE(fd)):
            self.assertEqual(cs._document(self.file), GOOD)
        self.assertEqual(closes, [])  # stream closes via its own C-level close, not os.close
        self.assertFalse(self.is_open(opened[0]))

    def test_descriptor_zero_handoff_in_child_process(self):
        self.write(GOOD)
        script = self.root / 'child.py'
        script.write_text(
            'import os, sys\n'
            'from pathlib import Path\n'
            'from campaign_tool import content_suspected as cs\n'
            'saved = os.dup(0)\n'
            'os.close(0)\n'
            'try:\n'
            '    assert os.open(os.devnull, os.O_RDONLY) == 0\n'
            '    os.close(0)\n'
            '    assert cs._document(Path(sys.argv[1])) == {"schema_version": 1, "entries": []}\n'
            '    os.close(0) if False else None\n'
            '    zero = os.open(sys.argv[1], os.O_RDONLY)\n'
            '    assert zero == 0, zero\n'
            '    os.close(0)\n'
            '    real = os.fdopen\n'
            '    def bad(fd, *a, **k):\n'
            '        assert fd == 0\n'
            '        raise OSError("x")\n'
            '    os.fdopen = bad\n'
            '    try:\n'
            '        cs._document(Path(sys.argv[1]))\n'
            '    except OSError:\n'
            '        pass\n'
            '    else:\n'
            '        raise SystemExit("no error")\n'
            '    os.fdopen = real\n'
            '    try:\n'
            '        os.fstat(0)\n'
            '    except OSError:\n'
            '        pass\n'
            '    else:\n'
            '        raise SystemExit("fd 0 leaked")\n'
            'finally:\n'
            '    os.dup2(saved, 0)\n'
            '    os.close(saved)\n', encoding='utf-8')
        done = subprocess.run([sys.executable, '-B', str(script), str(self.file)], cwd=Path(cs.__file__).parent.parent,
                              capture_output=True, text=True, timeout=60,
                              env={**os.environ, 'PYTHONPATH': str(Path(cs.__file__).parent.parent)})
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)

    def test_stream_owned_failures_close_after_transfer(self):
        cases = {
            'duplicate': b'{"a": 1, "a": 2}',
            'utf8': b'\xff\xfe',
            'json': b'{',
            'large': b' ' * (cs.MAX_BYTES + 1),
        }
        for name, data in cases.items():
            self.file.write_bytes(data)
            opened, opener = self.tracked_open()
            with mock.patch.object(os, 'open', opener):
                problems = []
                self.assertIsNone(cs.read_suspected(self.root, None, [], problems=problems), name)
                self.assertEqual(problems, [FIXED], name)
            self.assertFalse(self.is_open(opened[0]), name)
        # read error after a successful handoff
        self.write(GOOD)
        opened, opener = self.tracked_open()
        real_fstat = os.fstat
        with mock.patch.object(os, 'open', opener), mock.patch.object(os, 'fstat', side_effect=OSError('fstat')):
            with self.assertRaises(OSError):
                cs._document(self.file)
        self.assertFalse(self.is_open(opened[0]))
        self.assertIs(os.fstat, real_fstat)

    def test_public_failed_handoff_leaves_input_bytes_untouched(self):
        self.write({'schema_version': 1, 'entries': [ROW]})
        before, mode = self.file.read_bytes(), self.file.stat().st_mode
        with mock.patch.object(os, 'fdopen', side_effect=OSError('/secret/canary')):
            with self.assertRaises(ContentError) as caught:
                cs.read_suspected(self.root, None, [])
        self.assertEqual(str(caught.exception), FIXED)
        self.assertNotIn('canary', str(caught.exception))
        self.assertEqual((self.file.read_bytes(), self.file.stat().st_mode), (before, mode))

    def test_healthy_absence_empty_and_reviewed_entry_unchanged(self):
        self.assertIsNone(cs.read_suspected(self.root, None, []))
        self.write(GOOD)
        opened, opener = self.tracked_open()
        with mock.patch.object(os, 'open', opener):
            self.assertEqual(cs.read_suspected(self.root, None, []), [])
        self.assertFalse(self.is_open(opened[0]))
        self.write({'schema_version': 1, 'entries': [ROW]})
        rows = cs.read_suspected(self.root, None, [], today=__import__('datetime').date(2021, 1, 1))
        self.assertEqual(rows[0]['latitude'], 36.1235)
        self.assertEqual(rows[0]['longitude'], -121.9877)
        self.assertEqual(cs.export(rows), cs.export(rows))


if __name__ == '__main__':
    unittest.main()
