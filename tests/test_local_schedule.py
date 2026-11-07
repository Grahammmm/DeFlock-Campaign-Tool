"""tools/local_schedule_check.py and the shipped deploy/local-schedule/ user units (BF-df-local-schedule)."""

import ast
import contextlib
import hashlib
import io
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import local_schedule_check as lsc  # noqa: E402

SHIPPED = ROOT / 'deploy' / 'local-schedule'
SLOTS = ('backup', 'deadlines', 'reconcile', 'run-report-review', 'site-build', 'stale-check')
COMMANDS = ('campaign_tool/__main__.py', 'tools/stage_stale_check.py', 'tools/run_report_review.py',
            'tools/flock_deadlines.py')
CLEAN = 'slots=6 available=6 missing=0 collisions=0 gate-unsafe=0 | nothing installed'


def run(*argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        try:
            code = lsc.main(list(argv))
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue()


def tree_digest(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else 'dir'
            for p in sorted(Path(root).rglob('*'))}


class RenderedSet(unittest.TestCase):
    """A rendered copy of the shipped units against a synthetic checkout holding every command."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.work = self.tmp / 'checkout'
        for rel in COMMANDS:
            (self.work / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.work / rel).write_text('', encoding='utf-8')
        self.reports = self.tmp / 'records-runtime' / 'slo' / 'reports'
        self.reports.mkdir(parents=True)
        self.units = self.tmp / 'units'
        self.units.mkdir()
        for path in SHIPPED.glob('deflock-*'):
            text = path.read_text(encoding='utf-8').replace('@WORKDIR@', str(self.work))
            text = text.replace('@PYTHON@', sys.executable).replace('@STORE@', '/srv/store')
            text = text.replace('@REPORTS@', str(self.reports))
            (self.units / path.name).write_text(text, encoding='utf-8')

    def edit(self, name, old, new):
        path = self.units / name
        text = path.read_text(encoding='utf-8')
        self.assertIn(old, text)
        path.write_text(text.replace(old, new), encoding='utf-8')

    def check(self):
        return run('--store', '/srv/store', '--reports', str(self.reports), '--installed', str(self.units))

    def test_all_present_is_clean(self):
        code, out = self.check()
        self.assertEqual(code, 0, out)
        self.assertTrue(out.endswith(f'local-schedule | {CLEAN}\n'), out)
        for slot in SLOTS:
            self.assertIn(f'slot deflock-{slot} | at ', out)
        self.assertIn(f'{self.work}/tools/stage_stale_check.py available', out)
        for slot in ('stale-check', 'run-report-review'):
            self.assertRegex(out, rf'slot deflock-{slot} \| .* available \| .*\| reports {re.escape(str(self.reports))} present\n')

    def test_absent_reports_dir_leaves_both_reading_slots_unavailable(self):
        self.reports.rmdir()
        code, out = self.check()
        self.assertEqual(code, 1, out)
        for slot, rel in (('stale-check', 'stage_stale_check.py'), ('run-report-review', 'run_report_review.py')):
            self.assertIn(f'slot deflock-{slot} | at ', out)
            self.assertRegex(out, rf'{re.escape(str(self.work))}/tools/{rel} unavailable \| gate-safe \| '
                                  rf'lock \S+ \| reports {re.escape(str(self.reports))} missing\n')
        self.assertEqual(out.count(' available | gate-safe'), 4)
        self.assertIn('available=4 missing=2 collisions=0 gate-unsafe=0', out)

    def test_reports_dir_symlink_or_relative_is_not_available(self):
        real = self.tmp / 'real-reports'
        real.mkdir()
        self.reports.rmdir()
        self.reports.symlink_to(real)
        code, out = self.check()
        self.assertEqual(code, 1)
        self.assertIn('available=4 missing=2', out)  # stage_stale_check refuses a symlinked directory
        self.setUp()
        self.edit('deflock-run-report-review.service', f'--dir {self.reports}', '--dir run-reports')
        code, out = self.check()
        self.assertEqual(code, 1)
        self.assertIn('FAIL deflock-run-report-review.service: reports directory run-reports is not an absolute path', out)

    def test_reading_slot_without_its_reports_flag_is_refused(self):
        self.edit('deflock-run-report-review.service', f'--dir {self.reports}', f'{self.reports}/latest.json')
        self.assert_refused('deflock-run-report-review.service', 'missing --dir')

    def test_one_command_absent_is_missing_and_others_reported(self):
        (self.work / 'tools' / 'run_report_review.py').unlink()
        code, out = self.check()
        self.assertEqual(code, 1)
        self.assertIn(f'slot deflock-run-report-review | at 16:00 | {self.work}/tools/run_report_review.py missing', out)
        self.assertEqual(out.count(' available | gate-safe'), 5)
        self.assertIn('available=5 missing=1 collisions=0 gate-unsafe=0', out)

    def assert_refused(self, name, needle):
        code, out = self.check()
        self.assertEqual(code, 1, out)
        self.assertIn(f'REFUSED gate-unsafe {name}: ', out)
        self.assertIn(needle, out)
        self.assertNotIn('slot deflock-', out)  # the whole check is refused, not a warning among slots
        self.assertIn('gate-unsafe=1', out)

    def test_publish_command_refuses_the_check(self):
        self.edit('deflock-site-build.service', '-m campaign_tool build --directory',
                  'tools/flock_publish.py export --directory')
        self.assert_refused('deflock-site-build.service', 'not on the gate-safe allowlist: tools/flock_publish.py export')

    def test_publish_bypass_by_wording_is_refused(self):
        variants = [
            ('-m campaign_tool build', 'tools/../tools/flock_publish.py export', 'tools/flock_publish.py'),
            ('-m campaign_tool build', '-m campaign_tool.publish build', 'campaign_tool.publish'),
            ('-m campaign_tool build', 'tools/flock_email.py send', 'tools/flock_email.py send'),
            ('-m campaign_tool build --directory /srv/store', '-m campaign_tool build --publish --directory /srv/store',
             'sends, publishes or changes state'),
            ('-m campaign_tool build --directory /srv/store', '-m campaign_tool status --directory /srv/store',
             'missing --check'),
            ('-m campaign_tool build --directory /srv/store',
             '-m campaign_tool status --check --fix-modes --directory /srv/store', 'changes state'),
        ]
        for old, new, needle in variants:
            with self.subTest(new=new):
                self.setUp()
                self.edit('deflock-site-build.service', old, new)
                self.assert_refused('deflock-site-build.service', needle)

    def test_shell_body_and_extra_exec_lines_are_refused(self):
        self.edit('deflock-stale-check.service', '--now "$(date', '--now x; python3 tools/flock_publish.py "$(date')
        self.assert_refused('deflock-stale-check.service', 'shell body with operators')
        self.setUp()
        self.edit('deflock-backup.service', 'TimeoutStartSec', 'ExecStartPost=/usr/bin/python3 tools/flock_publish.py\n'
                  'TimeoutStartSec')
        self.assert_refused('deflock-backup.service', 'extra ExecStartPost')
        self.setUp()
        self.edit('deflock-backup.service', 'ExecStart=/usr/bin/flock', 'ExecStart=+/usr/bin/flock')
        self.assert_refused('deflock-backup.service', 'ExecStart prefix')

    def test_deadlines_slot_without_a_run_time_la_date_is_refused(self):
        date_arg = ' --date "$(TZ=America/Los_Angeles date +%%Y-%%m-%%d)"'
        self.edit('deflock-deadlines.service', date_arg, '')
        self.assert_refused('deflock-deadlines.service', 'missing --date: tools/flock_deadlines.py run')
        self.setUp()
        self.edit('deflock-deadlines.service', date_arg, ' --date 2026-11-03')
        self.assert_refused('deflock-deadlines.service', '--date must be the America/Los_Angeles day at run time')
        self.setUp()
        self.edit('deflock-deadlines.service', 'TZ=America/Los_Angeles date', 'date -u')
        self.assert_refused('deflock-deadlines.service', 'shell body with operators')

    def test_same_time_and_records_slot_collisions_are_named(self):
        self.edit('deflock-deadlines.timer', '09:00:00', '08:00:00')
        self.edit('deflock-backup.timer', '03:00:00', '14:40:00')
        code, out = self.check()
        self.assertEqual(code, 1)
        self.assertIn('COLLISION deflock-deadlines 08:00 collides with deflock-stale-check 08:00', out)
        self.assertIn('COLLISION deflock-backup 14:40 collides with records slot 15:00', out)
        self.assertIn('collisions=2 gate-unsafe=0', out)

    def test_collision_window_wraps_midnight(self):
        self.assertEqual(lsc.apart('23:50', '00:10'), 20)
        self.edit('deflock-backup.timer', '03:00:00', '00:10:00')
        self.edit('deflock-reconcile.timer', '10:00:00', '23:50:00')
        code, out = self.check()
        self.assertEqual(code, 1)
        self.assertIn('COLLISION deflock-backup 00:10 collides with deflock-reconcile 23:50', out)

    def test_unsubstituted_placeholder_names_the_file(self):
        self.edit('deflock-backup.service', '--directory /srv/store', '--directory @STORE@')
        code, out = self.check()
        self.assertEqual(code, 1)
        self.assertIn('FAIL deflock-backup.service: unsubstituted placeholder @STORE@', out)

    def test_unknown_placeholder_in_shipped_templates_fails(self):
        for path in self.units.glob('*'):
            path.unlink()
        for path in SHIPPED.glob('deflock-*'):
            (self.units / path.name).write_bytes(path.read_bytes())
        code, out = self.check()  # rendered mode: every template placeholder is left over
        self.assertEqual(code, 1)
        for name in ('WORKDIR', 'PYTHON', 'STORE'):
            self.assertIn(f'FAIL deflock-reconcile.service: unsubstituted placeholder @{name}@', out)
        for name in ('stale-check', 'run-report-review'):
            self.assertIn(f'FAIL deflock-{name}.service: unsubstituted placeholder @REPORTS@', out)

    def test_shared_or_absent_lock_fails(self):
        self.edit('deflock-deadlines.service', '%t/deflock-deadlines.lock', '%t/deflock-stale-check.lock')
        code, out = self.check()
        self.assertEqual(code, 1)
        self.assertIn('FAIL deflock-stale-check.service: lock %t/deflock-stale-check.lock shared with deflock-deadlines', out)
        self.setUp()
        self.edit('deflock-backup.service', '/usr/bin/flock -n -E 73 %t/deflock-backup.lock ', '')
        code, out = self.check()
        self.assertEqual(code, 1)
        self.assertIn('FAIL deflock-backup.service: no per-slot flock overlap lock', out)

    def test_system_unit_markers_fail(self):
        for name, old, new, needle in [
                ('deflock-backup.service', 'UMask', 'User=root\nUMask', 'not a plain user unit'),
                ('deflock-backup.timer', 'timers.target', 'multi-user.target', 'WantedBy must be timers.target'),
                ('deflock-backup.timer', 'Persistent=true', 'Persistent=false', 'needs Persistent=true'),
                ('deflock-backup.service', 'Type=oneshot', 'Type=simple', 'needs Type=oneshot')]:
            with self.subTest(new=new):
                self.setUp()
                self.edit(name, old, new)
                code, out = self.check()
                self.assertEqual(code, 1)
                self.assertIn(f'FAIL {name}: {needle}', out)

    def test_process_runs_no_scheduler_and_writes_nothing(self):
        home, spy = self.tmp / 'home', self.tmp / 'bin'
        home.mkdir()
        spy.mkdir()
        for tool in ('systemctl', 'crontab', 'at', 'hermes'):
            (spy / tool).write_text(f'#!/bin/sh\necho {tool} >> {self.tmp}/CALLED\n', encoding='utf-8')
            (spy / tool).chmod(0o755)
        env = dict(os.environ, HOME=str(home), PATH=f'{spy}:/usr/bin:/bin')
        before = tree_digest(self.tmp)
        for _ in range(2):
            proc = subprocess.run([sys.executable, '-B', str(ROOT / 'tools' / 'local_schedule_check.py'),
                                   '--store', '/srv/store', '--reports', str(self.reports),
                                   '--installed', str(self.units)],
                                  env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn(CLEAN, proc.stdout)
        self.assertFalse((self.tmp / 'CALLED').exists())
        self.assertFalse((home / '.config' / 'systemd').exists())
        self.assertEqual(tree_digest(self.tmp), before)


class ShippedUnits(unittest.TestCase):
    def test_six_user_unit_pairs(self):
        names = sorted(p.name for p in SHIPPED.iterdir() if p.suffix in ('.service', '.timer'))
        self.assertEqual(names, sorted(f'deflock-{s}.{k}' for s in SLOTS for k in ('service', 'timer')))
        for name in names:
            text = (SHIPPED / name).read_text(encoding='utf-8')
            keys = lsc.parse_unit(text)
            with self.subTest(name=name):
                self.assertLessEqual(len(text.splitlines()), 20)
                self.assertNotRegex(text, r'multi-user\.target|^User=|^Group=|\bsudo\b|/etc/')
                self.assertEqual(set(lsc.PLACEHOLDER.findall(text)) - {'@STORE@', '@REPORTS@', '@PYTHON@', '@WORKDIR@'},
                                 set())
                if name.endswith('.service'):
                    self.assertEqual(keys['Type'], ['oneshot'])
                    self.assertEqual(keys['WorkingDirectory'], ['@WORKDIR@'])
                    self.assertTrue(keys['ExecStart'][0].startswith('/usr/bin/flock -n -E 73 %t/'))
                    self.assertIn('@PYTHON@ -B ', keys['ExecStart'][0])
                    self.assertNotIn('WantedBy', keys)
                else:
                    self.assertEqual((keys['Persistent'], keys['WantedBy']), (['true'], ['timers.target']))
                    self.assertTrue(keys['RandomizedDelaySec'])
                    hhmm = re.search(r'OnCalendar=(?:\S+ )?\*-\*-\* (\d\d:\d\d):00', text)[1]
                    self.assertIn(f'# {hhmm} America/Los_Angeles', text)

    def test_shipped_cadence_is_weekday_after_records_runs_and_never_collides(self):
        want = {'backup': (None, '03:00'), 'stale-check': ('Mon..Fri', '08:00'), 'deadlines': ('Mon..Fri', '09:00'),
                'reconcile': ('Mon..Fri', '10:00'), 'run-report-review': ('Mon..Fri', '16:00'),
                'site-build': ('Mon..Fri', '17:00')}
        for slot, (days, hhmm) in want.items():
            keys = lsc.parse_unit((SHIPPED / f'deflock-{slot}.timer').read_text(encoding='utf-8'))
            with self.subTest(slot=slot):
                match = lsc.CALENDAR.fullmatch(keys['OnCalendar'][0])
                self.assertEqual(match[1] + ':' + match[2], hhmm)
                self.assertEqual(keys['OnCalendar'][0].startswith(f'{days} ') if days else
                                 keys['OnCalendar'][0].startswith('*-*-*'), True)
                self.assertEqual(keys['Persistent'], ['true'])
        self.assertEqual(lsc.RECORDS_SLOTS, ('07:00', '15:00'))
        # Only the backup runs on weekends; every other job follows a weekday records run.
        weekend = [s for s in want if not lsc.parse_unit((SHIPPED / f'deflock-{s}.timer').read_text(
            encoding='utf-8'))['OnCalendar'][0].startswith('Mon..Fri ')]
        self.assertEqual(weekend, ['backup'])

    def test_day_prefix_is_accepted_but_other_calendars_are_not(self):
        for ok in ('*-*-* 09:00:00 America/Los_Angeles', 'Mon..Fri *-*-* 09:00:00 America/Los_Angeles',
                   'Mon,Wed *-*-* 09:00:00 America/Los_Angeles'):
            self.assertTrue(lsc.CALENDAR.fullmatch(ok), ok)
        for bad in ('Mon..Fri *-*-* 09:00:00 UTC', 'Funday *-*-* 09:00:00 America/Los_Angeles',
                    'Mon..Fri; reboot *-*-* 09:00:00 America/Los_Angeles', '*-*-* 09:00:00'):
            self.assertFalse(lsc.CALENDAR.fullmatch(bad), bad)

    def test_reading_slots_read_the_records_reports_not_the_store(self):
        for name, flag in (('deflock-stale-check.service', '--reports'), ('deflock-run-report-review.service', '--dir')):
            execstart = lsc.parse_unit((SHIPPED / name).read_text(encoding='utf-8'))['ExecStart'][0]
            with self.subTest(name=name):
                self.assertIn(f' {flag} @REPORTS@', execstart)
                self.assertNotIn('@STORE@', execstart)
                self.assertNotIn('run-reports', execstart)

    def test_shipped_deadlines_command_passes_the_tool_usage_check(self):
        """The shipped ExecStart, run as systemd would (%% -> %, sh -c), gets past flock_deadlines usage."""
        tool = ROOT / 'tools' / 'flock_deadlines.py'
        if not tool.is_file():
            self.skipTest('tools/flock_deadlines.py not in this checkout')
        execstart = lsc.parse_unit((SHIPPED / 'deflock-deadlines.service').read_text(encoding='utf-8'))['ExecStart'][0]
        argv = lsc.shlex.split(execstart.replace('%%', '%'))
        self.assertEqual(argv[5:7], ['/bin/sh', '-c'])
        with tempfile.TemporaryDirectory() as tmp:
            body = argv[7].replace('@PYTHON@', sys.executable).replace('@STORE@', f'{tmp}/absent-store')
            utc = dict(os.environ, TZ='UTC')  # the LA day must not come from the host zone
            proc = subprocess.run(['/bin/sh', '-c', body], cwd=ROOT, env=utc, capture_output=True, text=True,
                                  timeout=60)
            self.assertNotEqual(proc.returncode, 2, proc.stderr)  # 2 is usage: the bug this guards
            self.assertNotIn('usage', proc.stderr.lower())
            echo = re.sub(r'^exec .* --date ', 'printf %s ', body)
            day = subprocess.run(['/bin/sh', '-c', echo], env=utc, capture_output=True, text=True).stdout
            la_day = subprocess.run(['date', '+%Y-%m-%d'], env=dict(os.environ, TZ='America/Los_Angeles'),
                                    capture_output=True, text=True).stdout.strip()
            self.assertRegex(day, r'^\d{4}-\d\d-\d\d$')
            self.assertEqual(day, la_day)

    def test_shipped_templates_report_every_slot(self):
        with tempfile.TemporaryDirectory() as reports:
            code, out = run('--store', '/srv/store', '--reports', reports)
        absent = [rel for rel in COMMANDS if not (ROOT / rel).is_file()]
        self.assertIn(f'missing={len(absent)} collisions=0 gate-unsafe=0 | nothing installed', out)
        self.assertEqual(code, 1 if absent else 0)
        for slot in SLOTS:
            self.assertRegex(out, rf'slot deflock-{slot} \| at \d\d:\d\d \| {re.escape(str(ROOT))}/\S+ (available|missing)')
        for rel in absent:
            self.assertIn(f'{ROOT}/{rel} missing', out)
        self.assertIn(f'| reports {reports} present', out)

    def test_shipped_templates_with_absent_reports_dir_are_not_available(self):
        absent_reports = '/nonexistent/records-runtime/slo/reports'
        code, out = run('--store', '/srv/store', '--reports', absent_reports)
        self.assertEqual(code, 1)
        self.assertEqual(out.count(f'| reports {absent_reports} missing'), 2)
        self.assertNotRegex(out, r'(stage_stale_check|run_report_review)\.py available')

    def test_usage_errors_exit_2(self):
        self.assertEqual(run('--store', 'relative/store', '--reports', '/r')[0], 2)
        self.assertEqual(run('--store', '/s', '--reports', 'relative/reports')[0], 2)
        self.assertEqual(run('--store', '/s')[0], 2)
        self.assertEqual(run()[0], 2)
        self.assertEqual(run('--store', '/s', '--reports', '/r', '--installed', '/nonexistent/units')[0], 2)

    def test_checker_never_calls_schedulers_or_network(self):
        source = (ROOT / 'tools' / 'local_schedule_check.py').read_text(encoding='utf-8')
        imports = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                imports |= {alias.name.split('.')[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                imports.add((node.module or '').split('.')[0])
        self.assertEqual(imports, {'argparse', 'posixpath', 're', 'shlex', 'sys', 'pathlib'})
        for word in ('systemctl', 'crontab', 'subprocess', 'socket', 'urllib', 'requests', 'os.system', 'exec('):
            self.assertNotIn(word, source)
        self.assertNotRegex(source, r"['\"]at['\"]|write_text|write_bytes|open\(|mkdir|unlink")


if __name__ == '__main__':
    unittest.main()
