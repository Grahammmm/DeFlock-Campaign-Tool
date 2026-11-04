"""Hosted-path status remains honest without hiding the live records engine."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HEADING = '## Hosted path: deploy scheduled, nothing provisioned (G 2026-11-01)'
PURPOSES = ('Setup wizard', 'Organizer workspace', 'Public-site Worker', 'Runner')
NEEDS = ('df-bind-sender', 'df-social-posting', 'df-subscribe',
         'df-legistar-poll', 'df-runner-handlers')
SCOPE = ("G's 2026-11-01 decision approves planning and then spending, with the "
         'cost shown to G before anything is provisioned; it is not standing '
         'approval to provision.')


def section(text, heading):
    parts = text.split(heading + '\n')
    if len(parts) != 2:
        raise AssertionError(f'expected exactly one section: {heading}')
    return parts[1].split('\n## ', 1)[0]


def check_hosted(text):
    hosted = section(text, HEADING)
    # Explicit carve-out: never widen this assertion to the live records engine.
    try:
        live_records = section(text, '## Live')
    except AssertionError as exc:
        raise AssertionError('missing live records-engine carve-out') from exc
    if 'Records engine run' not in live_records or 'Records stages' not in live_records:
        raise AssertionError('missing live records-engine carve-out')
    if '## Live' in hosted or 'Records engine run' in hosted:
        raise AssertionError('hosted assertion scope includes live records-engine carve-out')
    for old in ('Offline-only, frozen', 'No new features until a deploy is scheduled'):
        if old in text:
            raise AssertionError(f'retired text remains: {old}')
    if SCOPE not in hosted:
        raise AssertionError('missing decision scope')
    for phrase in ('not deployed', 'no deploy receipt exists', 'Not run', 'Path-scoped CI'):
        if phrase not in hosted:
            raise AssertionError(f'missing status: {phrase}')
    for name in PURPOSES:
        if not re.search(r'^- ' + re.escape(name) + r':\s+\S.+', hosted, re.M):
            raise AssertionError(f'missing purpose: {name}')
    needs = hosted.split('Before serving anyone, the hosted path still needs:\n', 1)
    if len(needs) != 2:
        raise AssertionError('missing needs list')
    lines = []
    for line in needs[1].splitlines():
        if not line.startswith('- '):
            break
        lines.append(line)
    if len(lines) != 6:
        raise AssertionError('expected six needs entries')
    for line in lines:
        if not re.search(r'\bdf-[a-z-]+\b|\bGAP\b', line):
            raise AssertionError(f'need without sheet item or GAP: {line}')
    for item in NEEDS:
        if not any(item in line for line in lines):
            raise AssertionError(f'missing need: {item}')
    if not any('job budget' in line and 'spend cap' in line and 'GAP' in line
               for line in lines):
        raise AssertionError('missing budget GAP')
    # Inspect sentences independently so a prior negation cannot mask a claim.
    # The mandated scope condition and the explicit negative status are not claims.
    for line in hosted.replace(SCOPE, '').splitlines():
        for sentence in re.split(r'[.;,]|\b(?:and|but|yet)\b', line.lower()):
            if re.search(r'\b(?:not|no|nothing|never|undeployed)\b', sentence):
                continue
            if re.search(r'\b(?:deployed|provisioned|live|in production)\b', sentence):
                raise AssertionError(f'affirmative hosted deployment claim: {sentence}')
    if re.search(r'(?<!not )standing approval to provision', hosted.lower()):
        raise AssertionError('standing provisioning approval claimed')


class StatusFreezeDocTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = (ROOT / 'docs/STATUS.md').read_text(encoding='utf-8')

    def seeded(self, line):
        return self.text.replace(HEADING + '\n', HEADING + '\n' + line + '\n')

    def test_hosted_section(self):
        check_hosted(self.text)

    def test_live_records_section_is_named_carve_out(self):
        self.assertIn('Records engine run', section(self.text, '## Live'))
        with self.assertRaisesRegex(AssertionError, 'carve-out'):
            check_hosted(self.text.replace('## Live\n', '## Former records\n'))
        with self.assertRaisesRegex(AssertionError, 'scope'):
            check_hosted(self.seeded('Records engine run'))

    def test_seeded_affirmative_claims_fail(self):
        for claim in ('the Workers are deployed', 'the Workers are live', 'live in production', 'provisioned',
                      'the runner is in production', 'not deployed. Workers are deployed',
                      'nothing is provisioned, but the Workers are deployed',
                      'nothing is provisioned and the Workers are deployed'):
            with self.subTest(claim=claim), self.assertRaisesRegex(AssertionError, 'deployment claim'):
                check_hosted(self.seeded(claim))

    def test_standing_approval_fails(self):
        with self.assertRaisesRegex(AssertionError, 'standing provisioning'):
            check_hosted(self.seeded('There is standing approval to provision'))

    def test_missing_purpose_fails_by_name(self):
        for name in PURPOSES:
            with self.subTest(name=name), self.assertRaisesRegex(AssertionError, name):
                check_hosted(self.text.replace('- ' + name + ':', '- Removed:'))

    def test_missing_heading_scope_and_need_fail(self):
        for changed in (self.text.replace(HEADING, '## Removed'),
                        self.text.replace(SCOPE, ''),
                        self.text.replace('df-bind-sender', 'missing-item'),
                        self.text.replace('**GAP**', 'uncovered')):
            with self.subTest(), self.assertRaises(AssertionError):
                check_hosted(changed)

    def test_code_and_ci_paths_remain(self):
        for directory in ('workers', 'runner'):
            self.assertTrue((ROOT / directory).is_dir(), directory)
            workflow = (ROOT / '.github/workflows' / f'{directory}.yml').read_text()
            self.assertIn(f'{directory}/**', workflow)

    def test_planned_rows_and_evidence(self):
        planned = section(self.text, '## Planned / not started')
        self.assertNotIn('frozen rows above', planned)
        for label in ('Monitored scheduling and cost controls', 'Any production deployment'):
            row = next(line for line in planned.splitlines() if label in line)
            self.assertIn('#hosted-path-deploy-scheduled-nothing-provisioned-g-2026-11-01', row)
        sender = next(line for line in planned.splitlines() if 'Registered sender' in line)
        self.assertIn('Outbox code is bound', sender)
        self.assertIn('registered sender', sender)
        schedule = next(line for line in planned.splitlines() if 'Monitored scheduling' in line)
        self.assertIn('authored in code but undeployed', schedule)

    def test_component_doc_pointers(self):
        for name in ('WORKERS', 'RUNNER'):
            content = (ROOT / f'docs/{name}.md').read_text()
            self.assertIn('STATUS.md#hosted-path-deploy-scheduled-nothing-provisioned-g-2026-11-01', content)


if __name__ == '__main__':
    unittest.main()
