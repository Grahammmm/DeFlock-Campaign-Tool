#!/usr/bin/env python3
"""Read-only check of the campaign-side local schedule (deploy/local-schedule/).

For each shipped .service/.timer pair: the command's resolved path and whether it
exists, whether it is on the gate-safe allowlist, its OnCalendar time and any
collision with another slot or a records slot (07:00/13:00/20:30), and whether its
overlap lock is per-slot. It only reads unit files and checks that paths exist:
it installs, enables and starts nothing and writes nothing.
Exit 0 all available and safe; 1 missing (a command, or the records reports directory a
reading slot needs), colliding, malformed or gate-unsafe; 2 usage.
"""
import argparse
import posixpath
import re
import shlex
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHIPPED = ROOT / 'deploy' / 'local-schedule'
RECORDS_SLOTS = ('07:00', '13:00', '20:30')
WINDOW = 50  # minutes; a records run stops by 47 min, a slot fires up to 5 min late
PLACEHOLDER = re.compile(r'@[A-Za-z_]+@')
CALENDAR = re.compile(r'\*-\*-\* (\d\d):(\d\d):00 America/Los_Angeles')
NOW_ARG = '"$(date -u +%%Y-%%m-%%dT%%H:%%M:%%SZ)"'
FLOCK = '/usr/bin/flock'
# Gate-safe allowlist: (target, subcommand) -> flags that must be present.
ALLOW = {
    ('-m campaign_tool', 'status'): ('--check',),
    ('-m campaign_tool', 'build'): (),
    ('-m campaign_tool', 'backup'): ('--into',),
    ('tools/stage_stale_check.py', 'check'): ('--reports',),
    ('tools/run_report_review.py', 'summary'): ('--dir',),
    ('tools/flock_deadlines.py', 'run'): (),
}
# Slots that read the records run reports (RUNTIME_DIR/<target>/reports/): the flag naming that
# directory. The slot is only available when the directory exists (a missing one fails every run).
READS_REPORTS = {('tools/stage_stale_check.py', 'check'): '--reports',
                 ('tools/run_report_review.py', 'summary'): '--dir'}
UNSAFE_WORDS = re.compile(r'publish|send|deploy|mail|approve|upload|fix-modes|restore', re.I)
FORBIDDEN_LINES = re.compile(r'^\s*(User|Group|PermissionsStartOnly)\s*=', re.M)


def parse_unit(text):
    keys = {}
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith(('#', ';', '[')) and '=' in line:
            key, value = line.split('=', 1)
            keys.setdefault(key.strip(), []).append(value.strip())
    return keys


def split_command(execstart):
    """(lock, interpreter, target, subcommand, args) or a string naming why it is unsafe."""
    argv = shlex.split(execstart)
    if not argv or argv[0][:1] in '-@+!:':
        return 'ExecStart prefix or empty command'
    lock = None
    if argv[0] == FLOCK:
        i = 1
        while i < len(argv) and argv[i].startswith('-'):
            i += 2 if argv[i] == '-E' else 1
        if i + 1 >= len(argv):
            return 'flock without a command'
        lock, argv = argv[i], argv[i + 1:]
    if argv[:2] == ['/bin/sh', '-c'] and len(argv) == 3:
        body = argv[2].replace(NOW_ARG, 'NOW')
        if re.search(r'[;|&`$<>()\n]', body):
            return 'shell body with operators'
        argv = shlex.split(body)
        argv = argv[1:] if argv[:1] == ['exec'] else argv
    i = 1
    while i < len(argv) and argv[i] in ('-B', '-I', '-s', '-E'):
        i += 1
    if i < len(argv) and argv[i] == '-m' and i + 1 < len(argv):
        target, i = '-m ' + argv[i + 1], i + 2
    elif i < len(argv):
        target, i = posixpath.normpath(argv[i]), i + 1
    else:
        return 'no command'
    return lock, argv[0], target, (argv[i] if i < len(argv) else ''), argv[i + 1:]


def gate_problem(target, sub, args):
    required = ALLOW.get((target, sub))
    if required is None:
        return 'not on the gate-safe allowlist'
    if any(UNSAFE_WORDS.search(word) for word in [target, sub, *args] if not word.startswith(('/', '%'))):
        return 'sends, publishes or changes state'
    missing = [flag for flag in required if flag not in args]
    return f'missing {" ".join(missing)}' if missing else None


def minutes(hhmm):
    hours, mins = hhmm.split(':')
    return int(hours) * 60 + int(mins)


def apart(a, b):
    gap = abs(minutes(a) - minutes(b)) % 1440
    return min(gap, 1440 - gap)


def reports_dir(target, sub, args):
    """The records reports directory a slot reads, or None when the slot reads none."""
    flag = READS_REPORTS.get((target, sub))
    if flag is None:
        return None
    i = args.index(flag) if flag in args else -1
    return args[i + 1] if 0 <= i < len(args) - 1 else ''


def check(units_dir, store, rendered, reports=''):
    fails, unsafe, slots = [], [], []
    subst = {} if rendered else {'@STORE@': store, '@REPORTS@': reports, '@PYTHON@': sys.executable,
                                 '@WORKDIR@': str(ROOT)}
    services = {p.stem for p in units_dir.glob('*.service')}
    timers = {p.stem for p in units_dir.glob('*.timer')}
    for stem in sorted(services ^ timers):
        fails.append(f'{stem}: .service and .timer must come in pairs')
    for stem in sorted(services & timers):
        texts = {}
        for kind in ('service', 'timer'):
            name = f'{stem}.{kind}'
            text = (units_dir / name).read_text(encoding='utf-8')
            for key, value in subst.items():
                text = text.replace(key, value)
            for left in sorted(set(PLACEHOLDER.findall(text))):
                fails.append(f'{name}: unsubstituted placeholder {left}')
            if FORBIDDEN_LINES.search(text) or re.search(r'\bsudo\b|/etc/systemd|/lib/systemd', text):
                fails.append(f'{name}: not a plain user unit (User=/Group=, sudo or system path)')
            texts[kind] = parse_unit(text)
            for key, values in texts[kind].items():
                if key.startswith('Exec') and (key != 'ExecStart' or kind == 'timer'):
                    unsafe.extend(f'{name}: extra {key} is not scheduled-safe: {value}' for value in values)
        svc, tmr = texts['service'], texts['timer']
        if svc.get('Type') != ['oneshot'] or 'WantedBy' in svc:
            fails.append(f'{stem}.service: needs Type=oneshot and no [Install]')
        if tmr.get('WantedBy', ['timers.target']) != ['timers.target']:
            fails.append(f'{stem}.timer: WantedBy must be timers.target only')
        if tmr.get('Persistent') != ['true'] or not tmr.get('RandomizedDelaySec'):
            fails.append(f'{stem}.timer: needs Persistent=true and RandomizedDelaySec')
        if tmr.get('Unit', [f'{stem}.service']) != [f'{stem}.service']:
            fails.append(f'{stem}.timer: Unit= must name {stem}.service')
        workdir = (svc.get('WorkingDirectory') or [''])[0]
        if len(svc.get('WorkingDirectory', [])) != 1 or not workdir.startswith('/'):
            fails.append(f'{stem}.service: needs one absolute WorkingDirectory')
        calendar = tmr.get('OnCalendar', [])
        match = CALENDAR.fullmatch(calendar[0]) if len(calendar) == 1 else None
        if not match:
            fails.append(f'{stem}.timer: needs one OnCalendar=*-*-* HH:MM:00 America/Los_Angeles')
        execs = svc.get('ExecStart', [])
        parsed = split_command(execs[0]) if len(execs) == 1 else 'needs exactly one ExecStart'
        if isinstance(parsed, str):
            unsafe.append(f'{stem}.service: {parsed}: {" ".join(execs)}')
            continue
        lock, python, target, sub, args = parsed
        problem = gate_problem(target, sub, args)
        if problem:
            unsafe.append(f'{stem}.service: {problem}: {target} {sub}'.rstrip())
        if not python.startswith('/'):
            fails.append(f'{stem}.service: interpreter {python} is not an absolute path')
        rel = 'campaign_tool/__main__.py' if target == '-m campaign_tool' else target.replace('-m ', '')
        path = Path(workdir or '/') / rel
        reads = reports_dir(target, sub, args)
        if reads is not None and not reads.startswith('/'):
            fails.append(f'{stem}.service: reports directory {reads or "(none)"} is not an absolute path')
        reads_ok = reads is None or (reads.startswith('/') and Path(reads).is_dir()
                                     and not Path(reads).is_symlink())
        slots.append({'name': stem, 'time': match and f'{match[1]}:{match[2]}', 'lock': lock,
                      'path': path, 'reports': reads, 'reports_ok': reads_ok,
                      'command_ok': path.is_file() and Path(python).is_file()})
    collisions = []
    for i, slot in enumerate(slots):
        if not slot['time']:
            continue
        for other in slots[i + 1:]:
            if other['time'] and apart(slot['time'], other['time']) < WINDOW:
                collisions.append(f'{slot["name"]} {slot["time"]} collides with {other["name"]} {other["time"]}')
        for records in RECORDS_SLOTS:
            if apart(slot['time'], records) < WINDOW:
                collisions.append(f'{slot["name"]} {slot["time"]} collides with records slot {records}')
    locks = {}
    for slot in slots:
        if not slot['lock']:
            fails.append(f'{slot["name"]}.service: no per-slot flock overlap lock')
        elif slot['lock'] in locks:
            fails.append(f'{slot["name"]}.service: lock {slot["lock"]} shared with {locks[slot["lock"]]}')
        else:
            locks[slot['lock']] = slot['name']
    return slots, collisions, fails, unsafe


def main(argv=None):
    parser = argparse.ArgumentParser(description='Read-only check of the campaign-side local schedule.', epilog=(
        'Templates are read from deploy/local-schedule/ with @STORE@/@REPORTS@/@PYTHON@/@WORKDIR@ filled in '
        'memory; --installed DIR checks rendered copies, where any leftover placeholder fails. A slot that '
        'reads the records run reports is missing while its reports directory is absent. Nothing is installed.'))
    parser.add_argument('--store', required=True, help='absolute campaign store the units run against')
    parser.add_argument('--reports', required=True, metavar='DIR',
                        help='absolute records run-report directory, RUNTIME_DIR/<target>/reports')
    parser.add_argument('--installed', metavar='DIR', help='folder of rendered unit copies to check instead')
    args = parser.parse_args(argv)
    units_dir = Path(args.installed) if args.installed else SHIPPED
    if not args.store.startswith('/') or not args.reports.startswith('/') or not units_dir.is_dir():
        parser.error('--store and --reports must be absolute and the units folder must exist')
    slots, collisions, fails, unsafe = check(units_dir, args.store, bool(args.installed), args.reports)
    missing = [slot for slot in slots if not (slot['command_ok'] and slot['reports_ok'])]
    if unsafe:
        for line in unsafe:
            print(f'REFUSED gate-unsafe {line}')
    else:
        for slot in slots:
            # A present command whose reports directory is absent is not available: every run would fail.
            state = 'missing' if not slot['command_ok'] else 'available' if slot['reports_ok'] else 'unavailable'
            reads = '' if slot['reports'] is None else \
                f' | reports {slot["reports"]} {"present" if slot["reports_ok"] else "missing"}'
            print(f'slot {slot["name"]} | at {slot["time"]} | {slot["path"]} {state} | gate-safe | '
                  f'lock {slot["lock"]}{reads}')
        for line in collisions:
            print(f'COLLISION {line}')
        for line in fails:
            print(f'FAIL {line}')
    print(f'local-schedule | slots={len(slots)} available={len(slots) - len(missing)} missing={len(missing)} '
          f'collisions={len(collisions)} gate-unsafe={len(unsafe)} | nothing installed')
    return 1 if unsafe or missing or collisions or fails else 0


if __name__ == '__main__':
    sys.exit(main())
