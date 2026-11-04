"""Synthetic offline meeting-pack acceptance and refusal paths."""
import copy
import csv
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from campaign_tool.labels import DEFAULTS
from campaign_tool.meeting_pack import build

REPO = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 1, tzinfo=timezone.utc)
ES_ENGLISH_KEYS = ('handout.title', 'handout.what_we_found', 'handout.agenda', 'handout.nothing_published',
                   'handout.not_recorded', 'handout.record_explainer', 'classification.documented_fact',
                   'confidence.verified')


class MeetingPackTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='synthetic-meeting-pack-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / 'campaign'
        shutil.copytree(REPO / 'examples/fictional-campaign', self.root)
        self.content = self.root / 'content'
        for folder in ('findings', 'meetings'):
            shutil.rmtree(self.content / folder)
            (self.content / folder).mkdir()
        self.site = json.loads((self.content / 'site.json').read_text())
        self.site['base_url'] = 'https://synthetic.invalid'
        self.write('site.json', self.site)
        self.template = json.loads((REPO / 'examples/fictional-campaign/content/findings/cedar-policy-posted.json').read_text())
        self.meeting = {'id': 'm', 'body': 'Synthetic Council', 'starts_at': '2099-11-02T18:00:00-08:00',
                        'agenda_item': 'Item 7', 'location': 'Synthetic Hall', 'agency_id': 'ca-cedar-police',
                        'agenda_url': 'https://synthetic.invalid/agenda', 'comment_kit_md': 'Hello.\nThank you.\n'}
        self.library = []
        self.finding(1)
        self.finding(2)
        self.stage()
        self.folder = self.root / 'kit/meeting-packs/m'

    def write(self, relative, value):
        (self.content / relative).write_text(json.dumps(value), encoding='utf-8')

    def stage(self):
        self.write('meetings/m.json', self.meeting)

    def finding(self, number, **extra):
        digest = hashlib.sha256(f'synthetic {number}'.encode()).hexdigest()
        finding = {**self.template, 'id': f'f{number}', 'slug': f'finding-{number}', 'title': f'Finding {number}',
                   'event_date': f'2026-01-{number:02d}', 'corrections': [],
                   'sources': [{'sha256': digest, 'title': f'Source {number}', 'locator': f'page:{number}'}], **extra}
        self.write(f'findings/f{number}.json', finding)
        self.library = [entry for entry in self.library if entry['sha256'] != digest]
        self.library.append({'sha256': digest, 'title': f'Source {number}', 'agency_id': 'ca-cedar-police',
                             'received_at': f'2026-02-{number:02d}T12:00:00Z', 'public_url': None, 'fragment': f'source-{number}'})
        self.write('sources.json', self.library)
        return finding

    def pack(self, **kwargs):
        return build(self.root, 'm', today=NOW, **kwargs)

    def html(self):
        return (self.folder / 'handout.html').read_text()

    def snapshot(self):
        return {str(p.relative_to(self.root)): hashlib.sha256(p.read_bytes()).hexdigest()
                for base in ('content', 'public') for p in (self.root / base).rglob('*') if p.is_file()}

    def test_three_files_sources_comment_permissions_and_no_public_mutation(self):
        before = self.snapshot()
        report = self.pack()
        self.assertEqual(report, {'written': [f'kit/meeting-packs/m/{name}' for name in
                                             ('handout.html', 'comment.md', 'sources.csv')], 'findings': 2, 'truncated': 0,
                                  'comment_sent': None})
        page = self.html()
        for number in (1, 2):
            self.assertIn(f'Finding {number}', page)
            self.assertIn(f'received 2026-02-{number:02d}', page)
            self.assertIn(self.library[number-1]['sha256'][:12], page)
        self.assertIn('@page { size: letter; margin: 0.6in }', page)
        self.assertIn('does not by itself prove who wrote it', page)
        self.assertIn('Synthetic Council', page)
        self.assertIn('Item 7', page)
        self.assertIn('Synthetic Hall', page)
        self.assertIn('not legal advice', page.lower())
        self.assertEqual((self.folder / 'comment.md').read_bytes(), self.meeting['comment_kit_md'].encode())
        for path in self.folder.iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertNotRegex(path.read_text(), r'[0-9a-fA-F]{40,}')
        for path in (self.root / 'kit', self.folder.parent, self.folder):
            self.assertEqual(path.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.snapshot(), before)

    def test_newest_first_truncation_and_optional_base(self):
        for number in range(3, 8):
            self.finding(number)
        for base in ('https://synthetic.invalid', None):
            self.site['base_url'] = base
            self.write('site.json', self.site)
            report = self.pack(replace=self.folder.exists())
            self.assertEqual((report['findings'], report['truncated']), (5, 2))
            page = self.html()
            self.assertIn('2 more on the site' + (': https://synthetic.invalid' if base else '</p>'), page)
            self.assertNotIn('Finding 1', page)
            self.assertLess(page.index('Finding 7'), page.index('Finding 3'))
            self.assertEqual('/findings/' in page, bool(base))

    def test_no_agency_includes_all_but_drafts_and_unpublished(self):
        self.meeting.pop('agency_id')
        self.stage()
        self.finding(3, agency_id='ca-other-police')
        self.finding(4, state='draft')
        self.finding(5, state='withdrawn')
        self.assertEqual(self.pack()['findings'], 3)
        self.assertNotIn('Finding 4', self.html())
        self.assertNotIn('Finding 5', self.html())

    def test_agency_filter_and_empty_state(self):
        self.meeting['agency_id'] = 'ca-other-police'
        self.stage()
        self.assertEqual(self.pack()['findings'], 0)
        self.assertIn('Records are requested; nothing is published yet.', self.html())
        self.assertNotIn('<ul', self.html())

    def test_missing_content_or_stage(self):
        (self.content / 'meetings/m.json').unlink()
        with self.assertRaisesRegex(ValueError, 'meetings-stage --id m'):
            self.pack()
        shutil.rmtree(self.content)
        with self.assertRaisesRegex(ValueError, 'run init/content first'):
            self.pack()
        self.assertFalse(self.folder.exists())

    def test_cancelled_cannot_be_bypassed_by_allow_past(self):
        self.meeting['status'] = 'cancelled'
        self.stage()
        with self.assertRaisesRegex(ValueError, 'cancelled'):
            self.pack(allow_past=True)
        self.assertFalse(self.folder.exists())

    def test_past_and_timezone_comparison(self):
        self.meeting['starts_at'] = '2026-10-31T20:00:00-07:00'
        self.stage()
        self.pack()  # 03:00 UTC on November 1, after NOW
        self.meeting['starts_at'] = '2026-10-31T16:00:00-07:00'
        self.stage()
        with self.assertRaisesRegex(ValueError, '--allow-past'):
            self.pack(replace=True)
        self.pack(allow_past=True, replace=True)

    def test_stale_correction_requires_restage_and_fresh_marks_correction(self):
        self.finding(1, corrections=[{'date': '2026-02-03', 'note': 'Synthetic correction'}])
        self.meeting['comment_kit_md'] = '/findings/finding-1.html'
        self.stage()
        with self.assertRaisesRegex(ValueError, 'meetings-stage --id m --replace'):
            self.pack(allow_past=True, replace=True)
        self.meeting['comment_kit_md'] += ' (corrected 2026-02-03;'
        self.stage()
        self.pack()
        self.assertIn('corrected 2026-02-03', self.html())

    def test_existing_pack_is_untouched_without_replace(self):
        self.pack()
        old = (self.folder / 'handout.html').read_bytes()
        self.finding(2, title='Replacement title')
        with self.assertRaisesRegex(ValueError, '--replace'):
            self.pack()
        self.assertEqual((self.folder / 'handout.html').read_bytes(), old)
        self.pack(replace=True)
        self.assertIn('Replacement title', self.html())

    def test_symlinks_at_every_output_level_refused(self):
        outside = self.root.parent / 'outside'
        outside.mkdir()
        for relative in ('kit', 'kit/meeting-packs', 'kit/meeting-packs/m', 'kit/meeting-packs/m/comment.md'):
            with self.subTest(relative=relative):
                target = self.root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.is_dir():
                    shutil.rmtree(target)
                target.symlink_to(outside, target_is_directory=True)
                with self.assertRaisesRegex(ValueError, 'symlink'):
                    self.pack(replace=True)
                target.unlink()
                self.assertEqual(list(outside.iterdir()), [])

    def test_invalid_ids_refused_before_reads_and_cli_max_bounds(self):
        with patch('campaign_tool.meeting_pack._read_json', side_effect=AssertionError('read')):
            for bad in ('../x', '', 'x' * 81, 'a/b', '.', '..'):
                with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, 'invalid meeting id'):
                    build(self.root, bad)
        for maximum in ('0', '11'):
            result = self.cli('--max-findings', maximum)
            self.assertEqual(result.returncode, 2)
        self.assertFalse(self.folder.exists())

    def test_html_escaping_and_only_intended_links(self):
        self.finding(2, title='<script>alert("x")</script> & title')
        self.pack()
        page = self.html()
        self.assertIn('&lt;script&gt;', page)
        self.assertIn('&amp; title', page)
        self.assertNotIn('<script', page)
        urls = set(re.findall(r'https?://[^<"\s]+', page))
        self.assertEqual(urls, {'https://synthetic.invalid/agenda', 'https://synthetic.invalid/findings/finding-1.html',
                                'https://synthetic.invalid/findings/finding-2.html'})

    def test_csv_quoting_dedup_and_multiple_locators(self):
        one = self.finding(1)
        title = 'Source, "quoted"\nsecond line'
        source = {**one['sources'][0], 'title': title}
        self.finding(1, sources=[source])
        self.finding(2, sources=[{**source, 'locator': 'page:9'}])
        # Repeated library registration is not allowed; keep the canonical first entries.
        self.write('sources.json', self.library[:2])
        self.pack()
        rows = list(csv.reader(io.StringIO((self.folder / 'sources.csv').read_text())))
        self.assertEqual(rows[0], ['title', 'record_prefix', 'received', 'locator', 'public_copy', 'cited_by'])
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1][0], title)
        self.assertEqual(rows[1][4], 'held privately')
        self.assertIn('page:9', rows[1][3])
        self.assertIn('page:1', rows[1][3])
        self.assertIn('finding-1', rows[1][5])
        self.assertIn('finding-2', rows[1][5])

    def test_full_hash_in_verbatim_comment_refuses_without_writes(self):
        self.meeting['comment_kit_md'] = 'Do not expose ' + 'a' * 64
        self.stage()
        with self.assertRaisesRegex(ValueError, 'full hash'):
            self.pack()
        self.assertFalse(self.folder.exists())

    def modes(self, folder=None):
        """Name -> (bytes, mode) for the pack members; nothing is read from a non-regular member."""
        folder = folder or self.folder
        return {p.name: (p.read_bytes() if p.is_file() and not p.is_symlink() else None, p.lstat().st_mode & 0o777)
                for p in folder.iterdir()}

    def siblings(self):
        return sorted(p.name for p in self.folder.parent.iterdir())

    def fail_replace(self, when):
        """Patch os.replace so calls for which when(source, target) is true raise; every other rename really happens."""
        real, calls = os.replace, []

        def replace(source, target):
            calls.append((Path(source).name, Path(target).name))
            if when(Path(source), Path(target)):
                raise OSError('synthetic promotion failure')
            return real(source, target)
        return patch('campaign_tool.meeting_pack.os.replace', side_effect=replace), calls

    def change_every_output(self):
        self.finding(2, title='Replacement title', sources=[{**self.template['sources'][0], 'title': 'Changed source',
                                                             'sha256': self.library[1]['sha256'], 'locator': 'page:2'}])
        self.meeting['comment_kit_md'] = 'Changed comment.\n'
        self.stage()

    def test_prepare_failure_never_exposes_partial_new_pack(self):
        real_open, real_fdopen = os.open, os.fdopen

        class Faulty:
            def __init__(self, stream, fail):
                self.stream, self.fail = stream, fail

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self.close()

            def write(self, data):
                if self.fail == 'write':
                    raise OSError('synthetic write failure')
                return self.stream.write(data)

            def close(self):
                self.stream.close()
                if self.fail == 'close':
                    raise OSError('synthetic close failure')
        for existing in (False, True):
            if existing:
                self.pack()
            before, content = (self.modes() if existing else None), self.snapshot()
            for stage in ('open', 'write', 'close'):
                for member in range(3):
                    with self.subTest(existing=existing, stage=stage, member=member):
                        seen = []

                        def opening(path, *args, **kwargs):
                            if '.m.new-' in str(path):
                                seen.append(path)
                                if stage == 'open' and len(seen) == member + 1:
                                    raise OSError('synthetic open failure')
                            return real_open(path, *args, **kwargs)

                        def fdopening(fd, *args, **kwargs):
                            stream = real_fdopen(fd, *args, **kwargs)
                            return Faulty(stream, stage if len(seen) == member + 1 else None)
                        with patch('campaign_tool.meeting_pack.os.open', side_effect=opening), \
                                patch('campaign_tool.meeting_pack.os.fdopen', side_effect=fdopening):
                            with self.assertRaisesRegex(OSError, 'synthetic .* failure'):
                                self.pack(replace=existing)
                        self.assertEqual(self.modes() if existing else self.folder.exists(), before if existing else False)
                        self.assertEqual(self.siblings(), ['m'] if existing else [])
                        self.assertEqual(self.snapshot(), content)

    def test_replace_promotion_failure_restores_complete_generation(self):
        self.pack()
        before = self.modes()
        self.change_every_output()
        failing, calls = self.fail_replace(lambda source, target: source.name.startswith('.m.new-') and target == self.folder)
        with failing, self.assertRaisesRegex(OSError, 'synthetic promotion failure'):
            self.pack(replace=True)
        self.assertEqual(self.modes(), before)
        self.assertEqual(self.siblings(), ['m'])
        self.assertEqual(self.folder.stat().st_mode & 0o777, 0o700)
        self.assertEqual(len(calls), 3)  # old directory moved aside, promotion failed, old directory put back
        self.pack(replace=True)  # nothing blocks the next run
        self.assertIn('Replacement title', self.html())
        self.assertEqual((self.folder / 'comment.md').read_text(), 'Changed comment.\n')
        self.assertIn('Changed source', (self.folder / 'sources.csv').read_text())

    def test_old_directory_move_failure_keeps_pack(self):
        self.pack()
        before = self.modes()
        self.change_every_output()
        failing, calls = self.fail_replace(lambda source, target: source == self.folder)
        with failing, self.assertRaisesRegex(OSError, 'synthetic promotion failure'):
            self.pack(replace=True)
        self.assertEqual(self.modes(), before)
        self.assertEqual(self.siblings(), ['m'])
        self.assertEqual(len(calls), 1)

    def test_new_pack_promotion_failure_leaves_nothing_but_existing_parents(self):
        failing, _ = self.fail_replace(lambda source, target: target == self.folder)
        with failing, self.assertRaisesRegex(OSError, 'synthetic promotion failure'):
            self.pack()
        self.assertFalse(self.folder.exists())
        self.assertEqual(self.siblings(), [])

    def test_restore_failure_retains_recoverable_old_pack(self):
        self.pack()
        before = self.modes()
        self.change_every_output()
        failing, _ = self.fail_replace(lambda source, target: target == self.folder)
        with failing, self.assertRaisesRegex(OSError, r'meeting pack restore failed; previous pack retained at (\S+)') as caught:
            self.pack(replace=True)
        backup = Path(re.search(r'retained at (\S+)', str(caught.exception))[1])
        self.assertEqual(backup.parent, self.folder.parent)
        self.assertEqual(self.modes(backup), before)
        self.assertFalse(self.folder.exists())
        self.assertEqual(self.siblings(), [backup.name])  # the incomplete new directory is gone
        for replace in (True, False):
            with self.subTest(replace=replace), self.assertRaisesRegex(ValueError, f'meeting pack recovery required; inspect {re.escape(str(backup))} before rebuilding'):
                self.pack(replace=replace)
            self.assertEqual(self.modes(backup), before)
            self.assertEqual(self.siblings(), [backup.name])

    def test_cleanup_failure_warns_after_success(self):
        self.pack()
        before = self.modes()
        self.change_every_output()
        with patch('campaign_tool.meeting_pack.shutil.rmtree', side_effect=OSError('synthetic cleanup failure')):
            report = self.pack(replace=True)
        backups = [name for name in self.siblings() if name != 'm']
        self.assertEqual(len(backups), 1)
        backup = self.folder.parent / backups[0]
        self.assertEqual(list(report), ['written', 'findings', 'truncated', 'comment_sent', 'warnings'])
        self.assertEqual(report['warnings'], [f'previous pack retained at {backup}; remove it after checking the new pack'])
        self.assertEqual(self.modes(backup), before)
        self.assertEqual(sorted(self.modes()), ['comment.md', 'handout.html', 'sources.csv'])
        self.assertEqual((self.folder / 'comment.md').read_text(), 'Changed comment.\n')
        with self.assertRaisesRegex(ValueError, 'meeting pack recovery required; inspect .* before rebuilding'):
            self.pack(replace=True)
        self.assertEqual(self.modes(backup), before)

    def test_unexpected_pack_contents_refuse_without_mutation(self):
        refusal = 'meeting pack has unexpected contents; move it aside before regenerating'

        def extra_note():
            (self.folder / 'notes.txt').write_text('organizer note')

        def subdirectory():
            (self.folder / 'attachments').mkdir()

        def missing_member():
            (self.folder / 'sources.csv').unlink()

        def fifo_member():
            (self.folder / 'comment.md').unlink()
            os.mkfifo(self.folder / 'comment.md')

        def symlink_member():
            (self.folder / 'comment.md').unlink()
            (self.folder / 'comment.md').symlink_to(self.folder / 'handout.html')

        def dangling_member():
            (self.folder / 'comment.md').unlink()
            (self.folder / 'comment.md').symlink_to(self.folder / 'nowhere')

        def regular_file_in_place_of_pack():
            shutil.rmtree(self.folder)
            self.folder.write_text('not a directory')
        for mutate in (extra_note, subdirectory, missing_member, fifo_member, symlink_member, dangling_member,
                       regular_file_in_place_of_pack):
            with self.subTest(case=mutate.__name__):
                if self.folder.is_dir() or self.folder.is_symlink() or self.folder.exists():
                    if self.folder.is_dir():
                        shutil.rmtree(self.folder)
                    else:
                        self.folder.unlink()
                self.pack()
                mutate()
                content = self.snapshot()
                state = {p.name: (p.lstat().st_mode, None if p.is_symlink() or not p.is_file() else p.read_bytes())
                         for p in self.folder.iterdir()} if self.folder.is_dir() else self.folder.read_bytes()
                with self.assertRaisesRegex(ValueError, f'{refusal}|symlink'):
                    self.pack(replace=True)
                if mutate in (extra_note, subdirectory, missing_member, fifo_member, regular_file_in_place_of_pack):
                    with self.assertRaisesRegex(ValueError, refusal):
                        self.pack(replace=True)
                self.assertEqual(self.siblings(), ['m'])
                self.assertEqual({p.name: (p.lstat().st_mode, None if p.is_symlink() or not p.is_file() else p.read_bytes())
                                  for p in self.folder.iterdir()} if self.folder.is_dir() else self.folder.read_bytes(), state)
                self.assertEqual(self.snapshot(), content)

    def test_successful_replacement_is_byte_compatible(self):
        first = self.pack()
        before = self.modes()
        again = self.pack(replace=True)
        self.assertEqual(again, first)
        self.assertNotIn('warnings', again)
        self.assertEqual(self.modes(), before)
        self.assertEqual(sorted(before), ['comment.md', 'handout.html', 'sources.csv'])
        self.assertEqual({mode for _, mode in before.values()}, {0o600})
        self.assertEqual(self.folder.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.siblings(), ['m'])

    def test_public_source_url_and_maximum_override(self):
        self.library[0]['public_url'] = 'https://synthetic.invalid/source'
        self.write('sources.json', self.library)
        report = self.pack(max_findings=1)
        self.assertEqual((report['findings'], report['truncated']), (1, 1))
        self.pack(max_findings=10, replace=True)
        self.assertIn('https://synthetic.invalid/source', (self.folder / 'sources.csv').read_text())

    def cli(self, *extra):
        return subprocess.run([sys.executable, '-B', '-m', 'campaign_tool', 'meeting-pack', '--directory',
                               str(self.root), '--id', 'm', *extra], cwd=REPO, capture_output=True, text=True, timeout=30)

    def test_cli_json_and_refusal(self):
        result = self.cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['findings'], 2)
        result = self.cli()
        self.assertEqual(result.returncode, 1)
        self.assertIn('--replace', result.stderr)

    # DF0137-17: the handout follows site.json.language and content/labels.json.
    def rich(self):
        """Findings 1..7 (two cut off), a correction, a source missing from the library, a base url."""
        for number in range(3, 8):
            self.finding(number)
        self.finding(7, corrections=[{'date': '2026-02-03', 'note': 'Synthetic correction'}])
        self.meeting['comment_kit_md'] = '/findings/finding-7.html (corrected 2026-02-03;'
        self.stage()
        self.write('sources.json', self.library[:-1])  # finding 7's source has no received date

    def handouts(self):
        """Handout bytes: plain, rich (corrected, cut off, base url, no received date), rich without base, empty."""
        memory = copy.deepcopy((self.meeting, self.site, self.library))
        backup = self.root.parent / 'content-backup'
        shutil.copytree(self.content, backup)
        shutil.rmtree(self.root / 'kit', ignore_errors=True)
        out = {}
        self.pack()
        out['plain'] = (self.folder / 'handout.html').read_bytes()
        self.rich()
        self.pack(replace=True)
        out['rich'] = (self.folder / 'handout.html').read_bytes()
        self.site['base_url'] = None
        self.write('site.json', self.site)
        self.pack(replace=True)
        out['rich-no-base'] = (self.folder / 'handout.html').read_bytes()
        self.meeting['agency_id'] = 'ca-other-police'
        self.stage()
        self.pack(replace=True)
        out['empty'] = (self.folder / 'handout.html').read_bytes()
        shutil.rmtree(self.content)
        shutil.move(backup, self.content)
        shutil.rmtree(self.root / 'kit')
        self.meeting, self.site, self.library = memory
        return out

    ES = {'handout.title': 'Hoja informativa', 'handout.what_we_found': 'Lo que encontramos', 'handout.agenda': 'Orden del día',
          'handout.nothing_published': 'Se han solicitado registros; aún no se ha publicado nada.',
          'handout.source_line': 'Fuente: {title}, recibida {date}, registro {record}', 'handout.not_recorded': 'sin registrar',
          'handout.corrected': 'corregido {date}', 'handout.more_on_site': '{count} más en el sitio',
          'handout.record_explainer': 'El número de registro son los primeros 12 caracteres de la huella SHA-256 del archivo.',
          'classification.documented_fact': 'Hecho documentado', 'confidence.verified': 'Verificado con el registro'}

    def labels(self, labels):
        self.write('labels.json', {'schema_version': 1, 'labels': labels})

    def test_no_labels_english_handout_is_byte_identical_to_main(self):
        # sha256 of handout.html as built by origin/main f38ae765 (before DF0137-17) for the same four fixtures.
        expected = {  # Public handout fixture digests, not credentials.
            'plain': '30de2c2520239b2f2e02c6b7a94ca46fe09f59d9e74f339b974a6ef47e444515',  # pragma: allowlist secret
            'rich': '82dc65aaeda7a782d66308cab2af059fc575b93f6f1ba0ff6bcccfb0acf1f1ef',  # pragma: allowlist secret
            'rich-no-base': '0fb688d3bd7ea920843e0e3157a57105625a2cab4ada289959341e1653089c9f',  # pragma: allowlist secret
            'empty': 'edf0c2a136c29211f08a65f88bd95dcb1441da6edf92bc7dd7b811054af72ced',  # pragma: allowlist secret
        }
        for case, data in self.handouts().items():
            with self.subTest(case=case):
                self.assertEqual(hashlib.sha256(data).hexdigest(), expected[case])
                self.assertIn(b'<html lang="en">', data)

    def test_default_labels_file_and_explicit_en_change_nothing(self):
        plain = self.handouts()
        self.labels({})
        self.assertEqual(self.handouts(), plain)
        self.site.pop('language', None)
        self.write('site.json', self.site)
        self.assertEqual(self.handouts(), plain)

    def test_spanish_campaign_gets_spanish_handout_with_no_english_fixed_text(self):
        self.rich()
        self.site['language'] = 'es'
        self.write('site.json', self.site)
        self.labels(self.ES)
        self.pack()
        page = self.html()
        self.assertIn('<html lang="es"><meta charset="utf-8"><title>Hoja informativa</title>', page)
        for spanish in ('Lo que encontramos', 'Orden del día', 'Fuente: Source 6, recibida 2026-02-06T12:00:00Z, registro ',
                        'Fuente: Source 7, recibida sin registrar, registro ', '<p>corregido 2026-02-03</p>', '<p>2 más en el sitio: https://synthetic.invalid</p>',
                        'Hecho documentado; Verificado con el registro', 'El número de registro son'):
            self.assertIn(spanish, page)
        for key in ES_ENGLISH_KEYS:  # every default without a placeholder, as written in DEFAULTS
            self.assertNotIn(DEFAULTS[key], page, key)
        for english in ('Source:', ', received ', ', record ', 'corrected 2026', 'more on the site', '>Agenda<'):
            self.assertNotIn(english, page)
        self.meeting['agency_id'] = 'ca-other-police'
        self.stage()
        self.pack(replace=True)
        self.assertIn('<p>Se han solicitado registros; aún no se ha publicado nada.</p>', self.html())
        self.assertNotIn('Records are requested', self.html())

    def test_missing_received_date_label_is_escaped_once(self):
        self.rich()
        self.labels({**self.ES, 'handout.not_recorded': 'sin fecha "A & B"'})
        self.pack()
        page = self.html()
        self.assertIn('Fuente: Source 7, recibida sin fecha &quot;A &amp; B&quot;, registro ', page)
        self.assertNotIn('&amp;amp;', page)
        self.assertNotIn('&amp;quot;', page)

    def test_spanish_language_without_labels_keeps_english_text(self):
        english = self.handouts()
        self.site['language'] = 'es'
        self.write('site.json', self.site)
        spanish = self.handouts()
        self.assertEqual(spanish, {case: data.replace(b'<html lang="en">', b'<html lang="es">') for case, data in english.items()})

    def test_labels_with_unknown_key_or_foreign_placeholder_refuse_and_write_nothing(self):
        for labels, text in (({'handout.nope': 'x'}, 'handout.nope'), ({'handout.title': 'Hoja {title}'}, r'handout\.title.*\{title\}'),
                             ({'handout.corrected': 'corregido {record}'}, r'handout\.corrected.*\{record\}'),
                             ({'handout.agenda': 'Orden {count}'}, r'handout\.agenda.*\{count\}'),
                             ({'handout.source_line': 'Fuente {name}'}, r'handout\.source_line.*\{name\}'),
                             ({'handout.agenda': 'Orden <b>'}, r'handout\.agenda'), ({'handout.agenda': 'Orden {title'}, r'handout\.agenda')):
            with self.subTest(labels=labels):
                self.labels(labels)
                with self.assertRaisesRegex(ValueError, text):
                    self.pack()
                self.assertFalse((self.root / 'kit').exists())

    def test_symlinked_labels_refused_and_braces_in_titles_print_literally(self):
        outside = self.root.parent / 'labels-elsewhere.json'
        outside.write_text(json.dumps({'schema_version': 1, 'labels': self.ES}))
        (self.content / 'labels.json').symlink_to(outside)
        with self.assertRaisesRegex(ValueError, 'labels.json: symlinks are not read'):
            self.pack()
        (self.content / 'labels.json').unlink()
        (self.content / 'labels.json').symlink_to(self.root.parent / 'missing-labels.json')  # dangling
        with self.assertRaisesRegex(ValueError, 'symlink'):
            self.pack()
        self.assertFalse((self.root / 'kit').exists())
        (self.content / 'labels.json').unlink()
        self.labels(self.ES)
        one = self.finding(1)
        self.finding(1, title='Costs {count} {date}', sources=[{**one['sources'][0], 'title': 'Source {record} {title}'}])
        self.pack()
        page = self.html()
        self.assertIn('<strong>Costs {count} {date}</strong>', page)
        self.assertIn('Fuente: Source {record} {title}, recibida 2026-02-01T12:00:00Z, registro ', page)

    def test_labels_never_change_comment_or_sources_and_cannot_smuggle_a_full_hash(self):
        self.rich()
        self.pack()
        before = {name: (self.folder / name).read_bytes() for name in ('comment.md', 'sources.csv')}
        self.labels(self.ES)
        self.pack(replace=True)
        self.assertEqual({name: (self.folder / name).read_bytes() for name in before}, before)
        self.assertTrue(before['sources.csv'].startswith(b'title,record_prefix,received,locator,public_copy,cited_by'))
        self.labels({**self.ES, 'handout.agenda': 'Orden ' + 'b' * 64})
        with self.assertRaisesRegex(ValueError, 'full hash'):
            self.pack(replace=True)
