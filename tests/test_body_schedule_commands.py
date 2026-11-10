"""Offline schedule maintenance: synthetic campaigns only, no fetching or publishing."""
import contextlib
import io
import json
import os
import shlex
import shutil
import stat
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from campaign_tool import body_schedule_cmd as cmd, body_schedules, cli, today
from tests.test_body_schedules import Base, BODY, URL, document, row

DAY = date(2026, 11, 9)
# Captured from integrated main before editing today.py, with only the temporary root normalized.
BASELINES = {'absent': ["1. [outcome_missing] Cedar City Council (fictional) was scheduled for 2026-10-14 (cedar-council-2026-10) with no outcome recorded: verify what happened, then record it — meetings-outcome --directory CAMPAIGN --id cedar-council-2026-10 --result RESULT --on YYYY-MM-DD --source OFFICIAL_URL --verified-by NAME\n2. [official_recheck_due] Synthetic Official, Cedar City Council (fictional): checked 2025-11-09; build refuses this row from 2026-11-10 — officials recheck --directory CAMPAIGN --agency ca-cedar-city-council --name 'Synthetic Official' --checked-on YYYY-MM-DD\n3. [site_rebuild_due] the built site still lists 1 meetings and 0 events as upcoming that are now past, cancelled or no longer staged; rebuild and publish — build --directory CAMPAIGN", '[\n  {\n    "rank": 1,\n    "kind": "outcome_missing",\n    "what": "Cedar City Council (fictional) was scheduled for 2026-10-14 (cedar-council-2026-10) with no outcome recorded: verify what happened, then record it",\n    "due": "2026-10-14",\n    "command": "meetings-outcome --directory CAMPAIGN --id cedar-council-2026-10 --result RESULT --on YYYY-MM-DD --source OFFICIAL_URL --verified-by NAME"\n  },\n  {\n    "rank": 2,\n    "kind": "official_recheck_due",\n    "what": "Synthetic Official, Cedar City Council (fictional): checked 2025-11-09; build refuses this row from 2026-11-10",\n    "due": "2026-11-10",\n    "command": "officials recheck --directory CAMPAIGN --agency ca-cedar-city-council --name \'Synthetic Official\' --checked-on YYYY-MM-DD"\n  },\n  {\n    "rank": 3,\n    "kind": "site_rebuild_due",\n    "what": "the built site still lists 1 meetings and 0 events as upcoming that are now past, cancelled or no longer staged; rebuild and publish",\n    "due": null,\n    "command": "build --directory CAMPAIGN"\n  }\n]'], 'fresh': ["1. [outcome_missing] Cedar City Council (fictional) was scheduled for 2026-10-14 (cedar-council-2026-10) with no outcome recorded: verify what happened, then record it — meetings-outcome --directory CAMPAIGN --id cedar-council-2026-10 --result RESULT --on YYYY-MM-DD --source OFFICIAL_URL --verified-by NAME\n2. [official_recheck_due] Synthetic Official, Cedar City Council (fictional): checked 2025-11-09; build refuses this row from 2026-11-10 — officials recheck --directory CAMPAIGN --agency ca-cedar-city-council --name 'Synthetic Official' --checked-on YYYY-MM-DD\n3. [site_rebuild_due] the built site still lists 1 meetings and 0 events as upcoming that are now past, cancelled or no longer staged; rebuild and publish — build --directory CAMPAIGN\n4. [expected_regular_meeting] Expected Synthetic Council meeting 2026-11-17: watch for the agenda — Check https://example.invalid/calendar for the agenda; stage only a verified meeting\n5. [expected_regular_meeting] Expected Synthetic Council meeting 2026-12-01: watch for the agenda — Check https://example.invalid/calendar for the agenda; stage only a verified meeting", '[\n  {\n    "rank": 1,\n    "kind": "outcome_missing",\n    "what": "Cedar City Council (fictional) was scheduled for 2026-10-14 (cedar-council-2026-10) with no outcome recorded: verify what happened, then record it",\n    "due": "2026-10-14",\n    "command": "meetings-outcome --directory CAMPAIGN --id cedar-council-2026-10 --result RESULT --on YYYY-MM-DD --source OFFICIAL_URL --verified-by NAME"\n  },\n  {\n    "rank": 2,\n    "kind": "official_recheck_due",\n    "what": "Synthetic Official, Cedar City Council (fictional): checked 2025-11-09; build refuses this row from 2026-11-10",\n    "due": "2026-11-10",\n    "command": "officials recheck --directory CAMPAIGN --agency ca-cedar-city-council --name \'Synthetic Official\' --checked-on YYYY-MM-DD"\n  },\n  {\n    "rank": 3,\n    "kind": "site_rebuild_due",\n    "what": "the built site still lists 1 meetings and 0 events as upcoming that are now past, cancelled or no longer staged; rebuild and publish",\n    "due": null,\n    "command": "build --directory CAMPAIGN"\n  },\n  {\n    "rank": 4,\n    "kind": "expected_regular_meeting",\n    "what": "Expected Synthetic Council meeting 2026-11-17: watch for the agenda",\n    "due": "2026-11-17",\n    "command": "Check https://example.invalid/calendar for the agenda; stage only a verified meeting"\n  },\n  {\n    "rank": 5,\n    "kind": "expected_regular_meeting",\n    "what": "Expected Synthetic Council meeting 2026-12-01: watch for the agenda",\n    "due": "2026-12-01",\n    "command": "Check https://example.invalid/calendar for the agenda; stage only a verified meeting"\n  }\n]'], 'stale': [{'rank': 1, 'kind': 'outcome_missing', 'what': 'Cedar City Council (fictional) was scheduled for 2026-10-14 (cedar-council-2026-10) with no outcome recorded: verify what happened, then record it', 'due': '2026-10-14', 'command': 'meetings-outcome --directory CAMPAIGN --id cedar-council-2026-10 --result RESULT --on YYYY-MM-DD --source OFFICIAL_URL --verified-by NAME'}, {'rank': 2, 'kind': 'official_recheck_due', 'what': 'Synthetic Official, Cedar City Council (fictional): checked 2025-11-09; build refuses this row from 2026-11-10', 'due': '2026-11-10', 'command': "officials recheck --directory CAMPAIGN --agency ca-cedar-city-council --name 'Synthetic Official' --checked-on YYYY-MM-DD"}, {'rank': 3, 'kind': 'site_rebuild_due', 'what': 'the built site still lists 1 meetings and 0 events as upcoming that are now past, cancelled or no longer staged; rebuild and publish', 'due': None, 'command': 'build --directory CAMPAIGN'}, {'rank': 4, 'kind': 'stale_body_schedule', 'what': 'Regular meeting schedule for Synthetic Council is stale (checked 2020-01-01); re-check https://example.invalid/calendar', 'due': '2020-01-01', 'command': 'Re-check https://example.invalid/calendar and update content/body-schedules.json'}, {'rank': 5, 'kind': 'expected_regular_meeting', 'what': 'Expected Synthetic Council meeting 2026-11-17: watch for the agenda', 'due': '2026-11-17', 'command': 'Check https://example.invalid/calendar for the agenda; stage only a verified meeting'}, {'rank': 6, 'kind': 'expected_regular_meeting', 'what': 'Expected Synthetic Council meeting 2026-12-01: watch for the agenda', 'due': '2026-12-01', 'command': 'Check https://example.invalid/calendar for the agenda; stage only a verified meeting'}]}


def snapshot(root):
    result = {}
    for path in [root, *sorted(root.rglob('*'))]:
        info = path.lstat()
        value = os.readlink(path) if path.is_symlink() else path.read_bytes() if stat.S_ISREG(info.st_mode) else None
        result[str(path.relative_to(root))] = (info.st_mode, info.st_mtime_ns if not path.is_dir() else None, value)
    return result


class CommandTests(Base):
    def setUp(self):
        super().setUp()
        previous_umask = os.umask(0o077)
        self.addCleanup(os.umask, previous_umask)
        self.clock = mock.patch.object(cmd, 'date', wraps=date)
        clock = self.clock.start()
        clock.today.return_value = DAY
        self.addCleanup(self.clock.stop)

    def invoke(self, *args, code=0, directory=True):
        argv = ['campaign_tool', 'body-schedule', *args]
        if directory:
            argv += ['--directory', str(self.root)]
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, 'argv', argv), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                cli.main()
                actual = 0
            except SystemExit as exc:
                actual = exc.code
        self.assertEqual(actual, code, err.getvalue())
        if code:
            self.assertEqual(out.getvalue(), '')
        else:
            self.assertEqual(err.getvalue(), '')
        return out.getvalue(), err.getvalue()

    def add(self, **changes):
        return cmd.add_schedule(self.root, **row(**changes), today=DAY)

    def add_args(self, **changes):
        value = row(**changes)
        return ['add', '--body', value['body'], '--weekday', value['weekday'], '--ordinals',
                *map(str, value['ordinals']), '--local-start-time', value['local_start_time'],
                '--time-zone', value['time_zone'], '--source-url', value['source_url'], '--checked-on', value['checked_on']]

    def loaded(self):
        return body_schedules.load_body_schedules(json.loads(self.path.read_text()), today=DAY)

    def refuse(self, function, *args, **kwargs):
        before = snapshot(self.root)
        with self.assertRaises(cmd.BodyScheduleError):
            function(*args, **kwargs)
        self.assertEqual(snapshot(self.root), before)

    def test_cli_lifecycle_uses_real_loader_after_each_write(self):
        self.assertEqual(self.invoke(*self.add_args(body='Zed', ordinals=['last', 3, 1]))[0], 'Added body schedule: Zed.\n')
        self.assertEqual(self.loaded()[0]['ordinals'], [1, 3, 'last'])
        self.invoke(*self.add_args(body='Alpha'))
        self.assertEqual([r['body'] for r in self.loaded()], ['Alpha', 'Zed'])
        self.assertTrue(self.invoke('list')[0].startswith('Alpha | weekday=tuesday;'))
        before = self.loaded()
        self.assertEqual(self.invoke('recheck', '--body', 'Zed', '--checked-on', '2026-11-09')[0],
                         'Rechecked body schedule: Zed (checked 2026-11-09).\n')
        before[1]['checked_on'] = '2026-11-09'
        self.assertEqual(self.loaded(), before)
        for body in ('Alpha', 'Zed'):
            self.assertEqual(self.invoke('remove', '--body', body)[0], f'Removed body schedule: {body}.\n')
            self.loaded()
        self.assertEqual(json.loads(self.path.read_text()), document())
        self.assertEqual(self.invoke('list')[0], 'No body schedules recorded.\n')

    def test_absent_store_read_and_refusals_never_create_it(self):
        before = snapshot(self.root)
        self.assertEqual(self.invoke('list')[0], 'No body schedules recorded.\n')
        for verb in ('recheck', 'remove'):
            args = ['--checked-on', DAY.isoformat()] if verb == 'recheck' else []
            self.assertIn('body not found: Unknown', self.invoke(verb, '--body', 'Unknown', *args, code=2)[1])
        self.invoke(*self.add_args(time_zone='Not/AZone'), code=2)
        self.assertEqual(snapshot(self.root), before)
        self.add()
        self.assertTrue(self.path.is_file())
        for missing in (self.root/'content/site.json',):
            missing.unlink()
            self.refuse(cmd.list_schedules, self.root)
        shutil.rmtree(self.root/'content')
        self.refuse(cmd.add_schedule, self.root, **row())

    def test_add_fields_are_shared_loader_contract(self):
        # Synthetic userinfo assembled at runtime; still exercise URL rejection.
        userinfo = ':'.join(('user', 'pass'))
        for field, values in {
            'time_zone': ['Not/AZone', '../UTC'], 'checked_on': ['2026-02-30', '2026-11-10', '2026-1-01'],
            'source_url': ['http://example.invalid', f'https://{userinfo}@example.invalid'],
            'local_start_time': ['24:00', '9:00'], 'ordinals': [[5], [], [1, 1], ['fifth']],
            'body': ['', 'x'*201, ' Council', 'Council\n']}.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    self.refuse(cmd.add_schedule, self.root, **row(**{field: value}), today=DAY)
        self.add()
        self.refuse(cmd.add_schedule, self.root, **row(), today=DAY)
        self.add(body=BODY.lower())
        self.assertEqual(len(self.loaded()), 2)
        self.write(document(*[row(body=f'Body {n}') for n in range(99)]))
        self.add()
        self.assertEqual(len(self.loaded()), 100)
        self.refuse(cmd.add_schedule, self.root, **row(body='101'), today=DAY)
        self.write(document(row(extra='unknown')))
        self.refuse(cmd.remove_schedule, self.root, body=BODY, today=DAY)

    def test_recheck_monotonic_date_and_no_other_field_changes(self):
        self.write(document(row(checked_on='2020-01-01'), row(body='Other')))
        for value in ('2019-12-31', '2026-11-10', '2026-2-02', 'invalid'):
            self.refuse(cmd.recheck_schedule, self.root, body=BODY, checked_on=value, today=DAY)
        before = snapshot(self.root)
        cmd.recheck_schedule(self.root, body=BODY, checked_on='2020-01-01', today=DAY)
        self.assertEqual(snapshot(self.root), before)
        expected = self.loaded()
        cmd.recheck_schedule(self.root, body=BODY, checked_on='2021-01-01', today=DAY)
        expected[1]['checked_on'] = '2021-01-01'
        self.assertEqual(self.loaded(), expected)
        self.assertIn('stale |', cmd.list_schedules(self.root, today=DAY))
        cmd.recheck_schedule(self.root, body=BODY, checked_on=DAY.isoformat(), today=DAY)
        self.assertNotIn('stale |', cmd.list_schedules(self.root, today=DAY))
        self.refuse(cmd.remove_schedule, self.root, body=BODY.lower(), today=DAY)

    def test_list_reports_365_fresh_366_stale_without_writes(self):
        self.write(document(*[row(body=body, checked_on=(DAY-timedelta(age)).isoformat())
                              for body, age in [('C', 366), ('B', 365), ('A', 0)]]))
        before = snapshot(self.root)
        lines = cmd.list_schedules(self.root, today=DAY).splitlines()
        for line, body, age, state in zip(lines, 'ABC', (0, 365, 366), ('fresh', 'fresh', 'stale')):
            self.assertEqual(line, f'{body} | weekday=tuesday; ordinals=1,3; local_start_time=18:30; '
                             f'time_zone=America/Los_Angeles | source_url={URL} | '
                             f'checked_on={(DAY-timedelta(age)).isoformat()}; age={age} days; limit=365 days; {state} | '
                             'agenda-watch=2026-11-17,2026-12-01,2026-12-15')
        self.assertEqual(len(lines), 4)
        self.assertEqual(snapshot(self.root), before)

    def test_list_three_actual_agenda_watch_dates(self):
        cases = [
            (date(2026, 11, 3), [1, 3], 'tuesday', [], '2026-11-03,2026-11-17,2026-12-01'),
            (date(2026, 12, 31), [1, 3], 'tuesday', [], '2027-01-05,2027-01-19,2027-02-02'),
            (date(2028, 2, 22), [4, 'last'], 'tuesday', [], '2028-02-22,2028-02-29,2028-03-28'),
            (date(2026, 2, 1), [4, 'last'], 'tuesday', [], '2026-02-24,2026-03-24,2026-03-31'),
            (date(2026, 3, 1), [1, 2, 3], 'sunday',
             [('2026-03-09T02:00:00Z', BODY, 'cancelled'), ('2026-03-01T18:00:00Z', 'Other', 'scheduled')],
             '2026-03-01,2026-03-15,2026-04-05'),
            (date(2026, 11, 1), [1, 2, 3], 'sunday', [('2026-11-02T02:00:00Z', BODY, 'cancelled')],
             '2026-11-08,2026-11-15,2026-12-06'),
            (date(9999, 12, 30), [1], 'tuesday', [], 'none in supported calendar range'),
            (date(9999, 12, 1), [1], 'tuesday', [], '9999-12-07')]
        folder = self.root/'content/meetings'
        shutil.rmtree(folder)
        folder.mkdir()
        for day, ordinals, weekday, meetings, expected in cases:
            with self.subTest(day=day, expected=expected):
                for p in folder.iterdir():
                    p.unlink()
                self.write(document(row(ordinals=ordinals, weekday=weekday, checked_on='2000-01-01')))
                for n, (instant, body, status) in enumerate(meetings):
                    self.stage(f'meeting-{n}', instant, body, status=status)
                self.assertIn('agenda-watch='+expected+'\n', cmd.list_schedules(self.root, today=day))
        self.write(document(row()))
        self.assertEqual(len([i for i in today._body_schedules(self.root, DAY) if i['kind']=='expected_regular_meeting']), 2)

    def test_list_refuses_broken_staged_inputs_before_output(self):
        self.add()
        folder = self.root/'content/meetings'
        target = folder/'bad.json'
        for value in ('{', '{}', '{"body":"Council","starts_at":"bad"}'):
            target.write_text(value)
            before = snapshot(self.root)
            self.assertIn('bad.json', self.invoke('list', code=2)[1])
            self.assertEqual(snapshot(self.root), before)
        target.unlink()
        target.mkdir()
        self.invoke('list', code=2)
        target.rmdir()
        target.symlink_to(self.root/'absent')
        self.invoke('list', code=2)
        target.unlink()
        shutil.rmtree(folder)
        self.invoke('list')
        folder.symlink_to(self.root/'content', target_is_directory=True)
        self.invoke('list', code=2)
        self.write(document())
        self.assertEqual(self.invoke('list')[0], 'No body schedules recorded.\n')

    def test_broken_store_and_links_refuse_without_side_effects(self):
        def all_refused():
            before = snapshot(self.root)
            for args in (self.add_args(), ['recheck', '--body', BODY, '--checked-on', DAY.isoformat()],
                         ['remove', '--body', BODY], ['list']):
                self.invoke(*args, code=2)
                self.assertEqual(snapshot(self.root), before)
        for value in (b'{', b'', b'\xff', b' '*(cmd.MAX_JSON_BYTES+1),
                      json.dumps({'schema_version': 2, 'schedules': []}).encode(),
                      json.dumps(document(row(checked_on='9999-01-01'))).encode()):
            self.path.write_bytes(value)
            all_refused()
        self.path.unlink()
        for kind in ('directory', 'fifo', 'dangling', 'link'):
            if kind == 'directory':
                self.path.mkdir()
            elif kind == 'fifo':
                os.mkfifo(self.path)
            else:
                self.path.symlink_to(self.root/('absent' if kind=='dangling' else 'content/site.json'))
            all_refused()
            self.path.rmdir() if kind=='directory' else self.path.unlink()
        content = self.root/'content'
        content.rename(self.root/'other-content')
        content.symlink_to(self.root/'other-content', target_is_directory=True)
        all_refused()

    def test_serialized_size_and_atomic_faults(self):
        value = row(body='É Council')
        payload = (json.dumps(document(value), indent=2, ensure_ascii=False)+'\n').encode()
        with mock.patch.object(cmd, 'MAX_JSON_BYTES', len(payload)-1), mock.patch.object(cmd, '_temp_name') as temp:
            self.refuse(cmd.add_schedule, self.root, **value, today=DAY)
            temp.assert_not_called()
        with mock.patch.object(cmd, 'MAX_JSON_BYTES', len(payload)):
            self.add(body='É Council')
        self.assertEqual(self.path.read_bytes(), payload)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o644)
        self.path.chmod(0o600)
        for exists in (True, False):
            if not exists:
                self.path.unlink()
            for function in ('fsync', 'replace', 'fchmod'):
                with mock.patch.object(cmd.os, function, side_effect=OSError('injected')):
                    before = snapshot(self.root)
                    with self.assertRaisesRegex(cmd.BodyScheduleError, 'previous file was kept'):
                        self.add(body='New')
                    self.assertEqual(snapshot(self.root), before)
            for operation in ('write', 'flush'):
                real_fdopen = os.fdopen
                def broken(*args, **kwargs):
                    stream = real_fdopen(*args, **kwargs)
                    proxy = mock.MagicMock(wraps=stream)
                    proxy.__enter__.return_value = proxy
                    proxy.__exit__.side_effect = lambda *a: stream.close()
                    getattr(proxy, operation).side_effect = OSError('injected')
                    return proxy
                with mock.patch.object(cmd.os, 'fdopen', side_effect=broken):
                    self.refuse(cmd.add_schedule, self.root, **row(body='New'), today=DAY)
        self.add()
        self.path.chmod(0o600)
        self.add(body='Other')
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        with mock.patch.object(cmd, '_temp_name', side_effect=OSError('injected')):
            self.refuse(cmd.remove_schedule, self.root, body=BODY, today=DAY)

    def test_stale_hint_quotes_selector_body_and_records_only_after_check(self):
        renamed = self.root.with_name("a campaign's directory")
        self.root.rename(renamed)
        self.root = renamed
        self.path = self.root/'content/body-schedules.json'
        body = "Council's Committee"
        self.add(body=body, checked_on='2020-01-01')
        items = today.todo(self.root, DAY)
        stale = next(i for i in items if i['kind']=='stale_body_schedule')
        self.assertIn(URL, stale['what'])
        args = shlex.split(stale['command'])
        self.assertEqual(args, ['body-schedule','recheck','--directory',str(self.root),'--body',body,'--checked-on',DAY.isoformat()])
        named = next(i for i in today.todo(self.root, DAY, campaign_name='synthetic') if i['kind']=='stale_body_schedule')
        self.assertEqual(shlex.split(named['command'])[2:4], ['--campaign','synthetic'])
        before = self.loaded()
        self.invoke(*args[1:], directory=False)
        before[0]['checked_on'] = DAY.isoformat()
        self.assertEqual(self.loaded(), before)
        self.assertFalse(any(i['kind']=='stale_body_schedule' for i in today.todo(self.root, DAY)))

    def test_argparse_refuses_mixed_modes_and_preserves_campaign_selection(self):
        before = snapshot(self.root)
        for args in ([], ['add'], ['recheck','--body',BODY], ['remove'], ['list','--body',BODY],
                     ['list','--json'], ['list','--as-of','2026-11-09'], ['list','--replace'],
                     ['list','--campaign','synthetic'], ['remove','--body',BODY,'--weekday','tuesday'],
                     ['recheck','--body',BODY,'--checked-on','2026-11-09','--source-url',URL],
                     self.add_args(ordinals=[5]), self.add_args(ordinals=['first'])):
            self.invoke(*args, code=2)
        self.assertEqual(snapshot(self.root), before)
        from campaign_tool import registry
        with mock.patch.dict(os.environ, {registry.ENV: str(self.tmp/'register.json')}):
            registry.add('synthetic', self.root)
            try:
                self.assertEqual(self.invoke('list', '--campaign', 'synthetic', directory=False)[0],
                                 'No body schedules recorded.\n')
                self.invoke(*self.add_args(), '--campaign', 'synthetic', directory=False)
                self.assertEqual(len(self.loaded()), 1)
            finally:
                registry.release()

    def test_no_fetch_no_build_no_private_or_public_writes(self):
        with mock.patch('socket.socket', side_effect=AssertionError('socket used')), \
             mock.patch('campaign_tool.site.build_site', side_effect=AssertionError('build used')), \
             mock.patch('campaign_tool.cli.database', side_effect=AssertionError('ledger used')):
            before = snapshot(self.root)
            self.invoke(*self.add_args())
            self.invoke('list')
            self.invoke('recheck','--body',BODY,'--checked-on',DAY.isoformat())
            self.invoke('remove','--body',BODY)
            after = snapshot(self.root)
            after.pop('content/body-schedules.json')
            self.assertEqual(after, before)

    def test_default_today_preserves_integrated_main_bytes(self):
        public = self.root/'public'
        public.mkdir()
        (public/'meetings.ics').write_text('BEGIN:VCALENDAR\nVERSION:2.0\nBEGIN:VEVENT\nUID:meeting-cedar-council-2026-10@deflock.local\nDTSTART:20261015T010000Z\nEND:VEVENT\nEND:VCALENDAR\n')
        (self.root/'content/officials.json').write_text(json.dumps({'schema_version': 1, 'bodies': [
            {'agency_id': 'ca-cedar-city-council', 'officials': [{'name': 'Synthetic Official', 'role': 'Council member',
             'source_url': 'https://example.invalid/roster', 'checked_on': '2025-11-09'}]}]}))
        for name in ('absent', 'fresh'):
            if name == 'fresh':
                self.write(document(row()))
            before = snapshot(self.root)
            for index, as_json in enumerate((False, True)):
                actual = today.run_cli(self.root, DAY.isoformat(), as_json=as_json).replace(str(self.root), 'CAMPAIGN')
                self.assertEqual(actual, BASELINES[name][index])
            self.assertEqual(snapshot(self.root), before)

        self.write(document(row(checked_on='2020-01-01')))
        actual = today.todo(self.root, DAY)
        expected = BASELINES['stale']
        for item in actual:
            item['command'] = item['command'].replace(str(self.root), 'CAMPAIGN')
            if item['kind'] == 'stale_body_schedule':
                self.assertTrue(item['command'].startswith('body-schedule recheck '))
                item['command'] = next(i['command'] for i in expected if i['kind']=='stale_body_schedule')
        self.assertEqual(actual, expected)

    def test_pre_replace_type_recheck_and_unencodable_text(self):
        self.refuse(cmd.add_schedule, self.root, **row(body='Bad\ud800'), today=DAY)
        real_fsync = os.fsync
        outside = self.tmp/'outside.json'
        outside.write_text('synthetic target')
        def swapped(fd):
            real_fsync(fd)
            self.path.symlink_to(outside)
        with mock.patch.object(cmd.os, 'fsync', side_effect=swapped):
            with self.assertRaises(cmd.BodyScheduleError):
                self.add()
        self.assertTrue(self.path.is_symlink())
        self.assertEqual(outside.read_text(), 'synthetic target')
        self.assertFalse(list((self.root/'content').glob('.body-schedules-*.tmp')))

    def trace_sync(self, fail_dir=False, dir_errors=None):
        """Wrap real fsync/replace/open/close; record order and the real directory descriptor."""
        events, dirs = [], []
        real_fsync, real_replace, real_open, real_close = os.fsync, os.replace, os.open, os.close

        def is_dir(fd):
            return stat.S_ISDIR(os.fstat(fd).st_mode)

        def fsync(fd):
            if is_dir(fd):
                events.append('fsync:dir')
                real_fsync(fd)
                if fail_dir:
                    raise OSError('injected dir sync SECRET')
            else:
                events.append('fsync:file')
                real_fsync(fd)

        def replace(src, dst, **kwargs):
            events.append('replace')
            real_replace(src, dst, **kwargs)

        def opener(path, flags, *args, **kwargs):
            fd = real_open(path, flags, *args, **kwargs)
            if is_dir(fd):
                dirs.append(fd)
            return fd

        patches = [mock.patch.object(cmd.os, 'fsync', fsync), mock.patch.object(cmd.os, 'replace', replace),
                   mock.patch.object(cmd.os, 'open', opener)]
        return events, dirs, patches

    def run_traced(self, function, *args, fail_dir=False, **kwargs):
        events, dirs, patches = self.trace_sync(fail_dir)
        for p in patches:
            p.start()
        try:
            return function(*args, **kwargs), events, dirs
        finally:
            for p in patches:
                p.stop()

    def closed(self, fd):
        try:
            os.fstat(fd)
        except OSError:
            return True
        return False

    def test_mutations_sync_directory_after_replace_before_success(self):
        steps = [(cmd.add_schedule, dict(**row(body='É Council')), 0o644),
                 (cmd.add_schedule, dict(**row(body='Other')), 0o600),
                 (cmd.recheck_schedule, dict(body='Other', checked_on='2026-11-09'), 0o600),
                 (cmd.remove_schedule, dict(body='Other'), 0o600),
                 (cmd.remove_schedule, dict(body='É Council'), 0o600)]
        for function, kwargs, mode in steps:
            if mode == 0o600 and self.path.exists():
                self.path.chmod(0o600)
            expected = {'add': 'Added', 'recheck': 'Rechecked', 'remove': 'Removed'}[function.__name__.split('_')[0]]
            message, events, dirs = self.run_traced(function, self.root, today=DAY, **kwargs)
            self.assertTrue(message.startswith(expected), message)
            self.assertEqual(events, ['fsync:file', 'replace', 'fsync:dir'])
            self.assertEqual(len(dirs), 1)
            self.assertTrue(self.closed(dirs[0]))
            self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), mode)
            self.loaded()
            self.assertFalse(list((self.root/'content').glob('.body-schedules-*.tmp')))
        self.assertEqual(self.loaded(), [])

    def test_directory_open_failure_preserves_store(self):
        for exists in (False, True):
            if exists:
                self.add()
            real_open = os.open

            def opener(path, flags, *a, **k):
                if Path(path) == self.root/'content':
                    raise OSError('injected')
                return real_open(path, flags, *a, **k)
            with mock.patch.object(cmd.os, 'open', opener), mock.patch.object(cmd, '_temp_name') as temp:
                before = snapshot(self.root)
                with self.assertRaisesRegex(cmd.BodyScheduleError, 'previous file was kept'):
                    self.add(body='New')
                self.assertEqual(snapshot(self.root), before)
                temp.assert_not_called()
            self.path.unlink(missing_ok=True)

    def test_preinstall_failures_never_sync_directory_after(self):
        for exists in (False, True):
            if exists:
                self.add()
            for target in ('fchmod', 'replace', 'fsync'):
                events, dirs, patches = self.trace_sync()
                before = snapshot(self.root)
                real = getattr(os, target)
                def boom(*a, **k):
                    raise OSError('injected')
                for p in patches[2:]:
                    p.start()
                try:
                    with mock.patch.object(cmd.os, target, boom):
                        with self.assertRaisesRegex(cmd.BodyScheduleError, 'previous file was kept'):
                            self.add(body='New')
                finally:
                    for p in patches[2:]:
                        p.stop()
                self.assertNotIn('fsync:dir', events)
                self.assertTrue(all(self.closed(fd) for fd in dirs))
                self.assertEqual(snapshot(self.root), before)
            self.path.unlink(missing_ok=True)

    def test_directory_sync_failure_reports_installed_state_without_rollback(self):
        self.add()
        self.add(body='Other')
        later = (DAY + timedelta(days=0)).isoformat()
        cases = [(cmd.add_schedule, row(body='Third')),
                 (cmd.recheck_schedule, dict(body=BODY, checked_on=later)),
                 (cmd.remove_schedule, dict(body='Other'))]
        for function, kwargs in cases:
            before = self.path.read_bytes()
            try:
                function(self.root, today=DAY, **kwargs)  # untraced pass only to compute the expected bytes
                expected = self.path.read_bytes()
            finally:
                self.path.write_bytes(before)
            if expected == before:  # equal-date recheck is a no-op and never reaches the writer
                continue
            with self.assertRaises(cmd.BodyScheduleError) as caught:
                self.run_traced(function, self.root, today=DAY, fail_dir=True, **kwargs)
            self.assertEqual(str(caught.exception), cmd._UNCONFIRMED)
            self.assertNotIn('SECRET', str(caught.exception))
            self.assertEqual(self.path.read_bytes(), expected)
            self.assertFalse(list((self.root/'content').glob('.body-schedules-*.tmp')))
            self.path.write_bytes(before)

    def test_directory_sync_failure_on_new_file_and_final_removal(self):
        with self.assertRaises(cmd.BodyScheduleError) as caught:
            self.run_traced(cmd.add_schedule, self.root, today=DAY, fail_dir=True, **row())
        self.assertEqual(str(caught.exception), cmd._UNCONFIRMED)
        self.assertEqual(len(self.loaded()), 1)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o644)
        with self.assertRaises(cmd.BodyScheduleError):
            self.run_traced(cmd.remove_schedule, self.root, today=DAY, fail_dir=True, body=BODY)
        self.assertEqual(self.loaded(), [])

    def test_cli_postinstall_uncertainty_has_no_success_or_false_rollback(self):
        events, dirs, patches = self.trace_sync(fail_dir=True)
        for p in patches:
            p.start()
        try:
            out, err = io.StringIO(), io.StringIO()
            argv = ['campaign_tool', 'body-schedule', *self.add_args(), '--directory', str(self.root)]
            with mock.patch.object(sys, 'argv', argv), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                with self.assertRaises(SystemExit) as caught:
                    cli.main()
        finally:
            for p in patches:
                p.stop()
        self.assertEqual(caught.exception.code, 2)
        self.assertEqual(out.getvalue(), '')
        self.assertIn('directory sync failed', err.getvalue())
        self.assertNotIn('previous file was kept', err.getvalue())
        self.assertNotIn('Added', err.getvalue())
        self.assertNotIn('SECRET', err.getvalue())
        self.assertNotIn('Traceback', err.getvalue())
        self.assertEqual(len(self.loaded()), 1)

    def test_close_failure_keeps_primary_diagnostic(self):
        real_close = os.close
        events, dirs, patches = self.trace_sync(fail_dir=True)

        def close(fd):
            real_close(fd)
            if fd in dirs:
                raise OSError('close injected')
        for p in patches:
            p.start()
        try:
            with mock.patch.object(cmd.os, 'close', close):
                with self.assertRaises(cmd.BodyScheduleError) as caught:
                    self.add()
        finally:
            for p in patches:
                p.stop()
        self.assertEqual(str(caught.exception), cmd._UNCONFIRMED)
        self.assertTrue(self.closed(dirs[0]))
        self.assertEqual(len(self.loaded()), 1)

    def test_list_equal_recheck_and_refusals_do_not_reach_writer(self):
        self.add()
        before = snapshot(self.root)
        real_open = os.open
        write_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | getattr(os, 'O_DIRECTORY', 0)

        def open_guard(path, flags, *args, **kwargs):
            # RF0325-2: the bounded schedule reader opens the file read-only; only a writer-side open is a failure.
            if flags & write_flags:
                raise AssertionError('writer reached')
            return real_open(path, flags, *args, **kwargs)
        with mock.patch.object(cmd.os, 'open', side_effect=open_guard), \
                mock.patch.object(cmd.os, 'fsync', side_effect=AssertionError('writer reached')):
            cmd.recheck_schedule(self.root, body=BODY, checked_on=row()['checked_on'], today=DAY)
            cmd.list_schedules(self.root, today=DAY) if hasattr(cmd, 'list_schedules') else None
            for function, kwargs in ((cmd.remove_schedule, dict(body='Missing')),
                                     (cmd.add_schedule, row())):
                with self.assertRaises(cmd.BodyScheduleError):
                    function(self.root, today=DAY, **kwargs)
        self.assertEqual(snapshot(self.root), before)
