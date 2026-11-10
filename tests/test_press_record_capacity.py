"""Private press hash records are measured before any write so a successful run never installs a record its own
reader refuses (RF1025-5). Fictional content only; real generators, real writers and the real CLI."""
import contextlib
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from campaign_tool import press
from campaign_tool.press import _letter_record, build_letter, build_press

REPO = Path(__file__).resolve().parents[1]
FIRST = 'fnd_0000000000000001'
SLUG = 'cedar-policy-posted'
DAY = '2026-03-01'
NAME = f'{DAY}-{SLUG}.md'
NOW = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
LIMIT = 1 << 20
# (generator, selects the suggestion store): the four independent provenance records.
STORES = [('draft', False), ('draft', True), ('letter', False), ('letter', True)]
DIRS = {'draft': ('kit/press', 'kit/press.suggested'), 'letter': ('kit/press/letters', 'kit/press/letters.suggested')}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def pretty(mapping):
    """The exact bytes kit._write_json installs, computed here from the documented JSON layout."""
    return (json.dumps(dict(sorted(mapping.items())), indent=2, ensure_ascii=False) + '\n').encode('utf-8')


def fillers(total):
    """Valid old record entries so that fillers + the target name serialize to exactly ``total`` bytes.

    Layout oracle: '{\\n' + entries joined by ',\\n' + '\\n}\\n'; an entry is name + 74 bytes counting the separator.
    """
    remaining = total - 3 - (len(NAME) + 74)
    out, index = {}, 0
    while remaining:
        cost = remaining if remaining <= 275 else (275 if remaining - 275 >= 86 else remaining - 86)
        assert 86 <= cost <= 275, cost
        stem = f'f{index:05d}-'
        out[stem + 'a' * (cost - 74 - len(stem) - 3) + '.md'] = sha(str(index).encode())
        remaining -= cost
        index += 1
    assert len(pretty({**out, NAME: '0' * 64})) == total
    return out


def snapshot(root):
    """{path: (mode, mtime_ns, bytes | link target | None)} for everything under root, directories included."""
    out = {}
    for item in sorted(Path(root).rglob('*')):
        info = os.lstat(item)
        body = os.readlink(item) if stat.S_ISLNK(info.st_mode) else item.read_bytes() if stat.S_ISREG(info.st_mode) else None
        out[item.relative_to(root).as_posix()] = (stat.S_IMODE(info.st_mode), info.st_mtime_ns, body)
    return out


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='fictional-press-capacity-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'campaign'
        shutil.copytree(REPO / 'examples/fictional-campaign', self.root, ignore=shutil.ignore_patterns('public'))
        for target in ('socket.socket.connect', 'socket.create_connection'):
            patcher = mock.patch(target, side_effect=AssertionError('network is blocked'))
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_generator(self, kind):
        if kind == 'draft':
            return build_press(self.root, [FIRST], DAY)
        return build_letter(self.root, [FIRST], DAY, now=NOW)

    def change_summary(self, text):
        path = self.root / 'content/findings' / (SLUG + '.json')
        data = json.loads(path.read_text())
        data['summary'] = text
        path.write_text(json.dumps(data))

    def folder(self, kind, suggested):
        return self.root / DIRS[kind][suggested]

    def seed(self, kind, suggested):
        """Real generations leaving a known-generated target in the selected store (and an edited primary for the
        suggestion stores). Returns the selected record path."""
        self.run_generator(kind)
        if suggested:
            (self.root / DIRS[kind][0] / NAME).write_text('Organizer edit')
            self.assertEqual(self.run_generator(kind)['path'], f'{DIRS[kind][1]}/{NAME}')
        return self.folder(kind, suggested) / '.written.json'

    def put_record(self, kind, suggested, mapping, compact=False):
        folder = self.folder(kind, suggested)
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = folder / '.written.json'
        path.write_bytes(json.dumps(mapping, separators=(',', ':')).encode() if compact else pretty(mapping))
        os.chmod(path, 0o600)
        return path

    def prepare_primary_edit(self, kind, suggested):
        """Make the generator select the suggestion store without touching its record."""
        if suggested:
            self.run_generator(kind)
            (self.root / DIRS[kind][0] / NAME).write_text('Organizer edit')

    def reset_kit(self):
        shutil.rmtree(self.root / 'kit', ignore_errors=True)
        self.assertFalse((self.root / 'kit').exists())

    def blocked_writers(self):
        """Patch the module's folder/text/json writers to fail the test if reached."""
        stack = contextlib.ExitStack()
        for name in ('_kit_folder', '_write_text', '_write_json'):
            stack.enter_context(mock.patch.object(press, name, side_effect=AssertionError(name + ' reached too early')))
        return stack


class CapacityTests(Base):
    def test_exact_limit_succeeds_and_installed_record_rereads(self):
        for kind, suggested in STORES:
            with self.subTest(kind=kind, suggested=suggested):
                self.reset_kit()
                old = fillers(LIMIT)
                path = self.put_record(kind, suggested, old)  # prospective: old + target == exactly LIMIT
                self.prepare_primary_edit(kind, suggested)
                result = self.run_generator(kind)
                self.assertFalse(result['sent'])
                self.assertEqual(result['path'], f'{DIRS[kind][suggested]}/{NAME}')
                installed = path.read_bytes()
                self.assertEqual(len(installed), LIMIT)
                record = _letter_record(path)  # the reader accepts what the writer installed
                self.assertEqual(installed, pretty(record))
                self.assertEqual(record[NAME], sha((self.root / result['path']).read_bytes()))
                self.assertTrue(set(old) <= set(record) and len(record) == len(old) + 1)
                self.assertEqual(stat.S_IMODE(os.lstat(path).st_mode), 0o600)

    def test_canonical_near_cap_append_refuses_all_four_records(self):
        for kind, suggested in STORES:
            with self.subTest(kind=kind, suggested=suggested):
                self.reset_kit()
                path = self.put_record(kind, suggested, fillers(LIMIT + 1))  # one byte over once the target is added
                self.assertLess(len(path.read_bytes()), LIMIT)
                self.assertEqual(sorted(_letter_record(path)), sorted(fillers(LIMIT + 1)))
                self.prepare_primary_edit(kind, suggested)
                before = snapshot(self.root)
                rel = f'{DIRS[kind][suggested]}/.written.json'
                with self.blocked_writers(), \
                        self.assertRaisesRegex(ValueError, rf'{rel} would exceed the 1 MiB.*Nothing was written') as caught:
                    self.run_generator(kind)
                self.assertNotIn(str(self.root), str(caught.exception))
                self.assertEqual(snapshot(self.root), before)
                _letter_record(path)  # original metadata is still readable

    def test_compact_input_expansion_is_measured_before_any_write(self):
        for kind, suggested in STORES:
            for present in (False, True):
                with self.subTest(kind=kind, suggested=suggested, target_already_recorded=present):
                    self.reset_kit()
                    old = fillers(LIMIT + 500)
                    if present:
                        old[NAME] = '0' * 64  # replacing a fixed-width hash, yet the pretty form is over the limit
                    path = self.put_record(kind, suggested, old, compact=True)
                    self.assertLess(path.stat().st_size, LIMIT)
                    _letter_record(path)
                    self.prepare_primary_edit(kind, suggested)
                    before = snapshot(self.root)
                    with self.assertRaisesRegex(ValueError, r'\.written\.json would exceed the 1 MiB'):
                        self.run_generator(kind)
                    self.assertEqual(snapshot(self.root), before)

    def test_at_capacity_hash_replacement_succeeds_and_keeps_sorted_siblings(self):
        for kind, suggested in STORES:
            with self.subTest(kind=kind, suggested=suggested):
                self.reset_kit()
                path = self.seed(kind, suggested)
                old = fillers(LIMIT)
                old[NAME] = _letter_record(path)[NAME]
                path.write_bytes(pretty(old))
                self.assertEqual(path.stat().st_size, LIMIT)
                self.change_summary(f'A replacement summary {kind} {suggested}.')
                result = self.run_generator(kind)
                self.assertEqual(result['kept_edited'], suggested)
                draft = (self.root / result['path']).read_bytes()
                self.assertIn(f'A replacement summary {kind} {suggested}.'.encode(), draft)
                new = json.loads(path.read_bytes())
                self.assertEqual(list(new), sorted(new))
                self.assertEqual(new[NAME], sha(draft))
                self.assertEqual({k: v for k, v in new.items() if k != NAME}, {k: v for k, v in old.items() if k != NAME})
                self.assertEqual(len(path.read_bytes()), LIMIT)
                self.assertEqual(stat.S_IMODE(os.lstat(path).st_mode), 0o600)

    def test_small_and_missing_records_generate_normally(self):
        for kind, suggested in STORES:
            with self.subTest(kind=kind, suggested=suggested):
                self.reset_kit()
                path = self.seed(kind, suggested)
                self.assertEqual(path.read_bytes(), pretty({NAME: sha((self.root / DIRS[kind][suggested] / NAME).read_bytes())}))
                self.assertEqual(stat.S_IMODE(os.lstat(path.parent).st_mode), 0o700)

    def test_refusal_preserves_existing_generated_output_states(self):
        for kind, suggested in STORES:
            with self.subTest(kind=kind, suggested=suggested):
                self.reset_kit()
                path = self.seed(kind, suggested)
                old = fillers(LIMIT + 500)
                old[NAME] = _letter_record(path)[NAME]  # the target is a known-generated, unedited draft
                path.write_bytes(json.dumps(old, separators=(',', ':')).encode())
                self.change_summary(f'A refused summary {kind} {suggested}.')
                before = snapshot(self.root)
                with self.assertRaisesRegex(ValueError, r'would exceed the 1 MiB'):
                    self.run_generator(kind)
                self.assertEqual(snapshot(self.root), before)
                self.assertNotIn(f'A refused summary {kind} {suggested}.'.encode(), (self.folder(kind, suggested) / NAME).read_bytes())
                if not suggested:  # the unused suggestion folder stays absent
                    self.assertFalse(self.root.joinpath(DIRS[kind][1]).exists())

    def test_only_selected_record_is_prospectively_measured(self):
        for kind in ('draft', 'letter'):
            with self.subTest(kind=kind):
                self.reset_kit()
                # An unsafe unused suggestion store does not block a clean primary refresh.
                self.run_generator(kind)
                self.put_record(kind, True, {}).write_text('[]')
                self.change_summary(f'Primary refresh {kind}.')
                self.assertFalse(self.run_generator(kind)['kept_edited'])
                # An accepted but nearly full primary is neither rewritten nor measured when the suggestion is selected.
                self.reset_kit()
                primary = self.seed(kind, False)
                big = fillers(LIMIT + 500)
                big[NAME] = _letter_record(primary)[NAME]
                primary.write_bytes(json.dumps(big, separators=(',', ':')).encode())
                (self.root / DIRS[kind][0] / NAME).write_text('Organizer edit')
                now = snapshot(self.root)
                kept = {p: now[p] for p in (f'{DIRS[kind][0]}/.written.json', f'{DIRS[kind][0]}/{NAME}')}
                self.assertTrue(self.run_generator(kind)['kept_edited'])
                after = snapshot(self.root)
                self.assertEqual({p: after[p] for p in kept}, kept)
                self.assertEqual(list(json.loads((self.folder(kind, True) / '.written.json').read_bytes())), [NAME])
                # A selected suggestion store over capacity refuses and keeps both originals and suggestions.
                self.reset_kit()
                self.put_record(kind, True, fillers(LIMIT + 1))
                self.prepare_primary_edit(kind, True)
                before = snapshot(self.root)
                with self.assertRaisesRegex(ValueError, r'press.*suggested/\.written\.json would exceed the 1 MiB'):
                    self.run_generator(kind)
                self.assertEqual(snapshot(self.root), before)

    def test_malformed_selected_record_is_still_refused_by_existing_validation(self):
        for kind, suggested in STORES:
            with self.subTest(kind=kind, suggested=suggested):
                self.reset_kit()
                self.put_record(kind, suggested, {}).write_text('[]')
                self.prepare_primary_edit(kind, suggested)
                with self.assertRaisesRegex(ValueError, r'must map simple \.md file names'):
                    self.run_generator(kind)

    def test_write_failures_keep_existing_conservative_behaviour(self):
        for kind in ('draft', 'letter'):
            with self.subTest(kind=kind):
                self.reset_kit()
                path = self.seed(kind, False)
                before = path.read_bytes()
                self.change_summary(f'Never written {kind}.')
                with mock.patch.object(press, '_write_text', side_effect=OSError('disk')):
                    with self.assertRaises(OSError):
                        self.run_generator(kind)
                self.assertEqual(path.read_bytes(), before)
                with mock.patch.object(press, '_write_json', side_effect=OSError('disk')):
                    with self.assertRaises(OSError):
                        self.run_generator(kind)
                # draft replaced but record not advanced: the next run treats it as edited and keeps it.
                self.assertTrue(self.run_generator(kind)['kept_edited'])


class CliTests(Base):
    def cli(self, *args):
        return subprocess.run([sys.executable, '-B', '-m', 'campaign_tool', 'press', *args, '--directory', str(self.root),
                               '--finding', FIRST, '--date', DAY], cwd=REPO, capture_output=True, text=True, timeout=120)

    def test_cli_capacity_refusal_is_controlled_private_and_no_success(self):
        for command, kind in (('draft', 'draft'), ('letter', 'letter')):
            with self.subTest(command=command):
                self.reset_kit()
                canary = fillers(LIMIT + 1)
                path = self.put_record(kind, False, canary)
                before = snapshot(self.root)
                done = self.cli(command)
                out = done.stdout + done.stderr
                self.assertEqual(done.returncode, 1, out)
                rel = f'{DIRS[kind][0]}/.written.json'
                for needle in (rel, '1 MiB', 'Nothing was written'):
                    self.assertIn(needle, out)
                for forbidden in ('Traceback', 'Nothing was sent', 'written to kit', 'Private letter draft', str(self.root),
                                  next(iter(canary.values())), 'Cedar'):
                    self.assertNotIn(forbidden, out)
                self.assertEqual(snapshot(self.root), before)
                self.assertTrue(path.exists())


if __name__ == '__main__':
    unittest.main()
