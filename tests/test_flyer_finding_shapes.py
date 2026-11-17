"""Campaign flyer refuses non-object finding JSON with an attributed ContentError (synthetic, offline)."""
import json
import os
import unittest

from campaign_tool.content import ContentError
from campaign_tool.flyer import build
from tests import test_flyer as base
from tests.test_flyer_output_admission import tree

CANARY = 'CANARY-SECRET-PAYLOAD'
BAD = ('[]', json.dumps([CANARY]), 'null', 'true', 'false', '4', '4.5', '""', json.dumps(CANARY))
NAME = 'zz-malformed.json'
STOPPED = f'Stopped: {NAME}: expected an object'


class Shapes(unittest.TestCase):
    # borrow fixture helpers by name without subclassing (and so re-running) the historical TestCase
    for _helper in ('setUp', 'site', 'finding', 'html', 'text', 'cli', 'branded'):
        locals()[_helper] = base.FlyerTests.__dict__[_helper]
    del _helper

    def malformed(self, raw='[]', name=NAME):
        (self.content / 'findings' / name).write_text(raw, encoding='utf-8')

    def test_nonobject_finding_documents_raise_attributed_content_error(self):
        self.finding(1)
        for raw in BAD:
            with self.subTest(raw=raw):
                self.malformed(raw)
                with self.assertRaises(ContentError) as caught:
                    build(self.root, replace=True)
                self.assertEqual(str(caught.exception), f'{NAME}: expected an object')
                self.assertNotIn(CANARY, str(caught.exception))
                self.assertNotIn(str(self.root), str(caught.exception))

    def test_shape_admission_precedes_state_filter_and_limit(self):
        for position, name in (('after', 'zz-malformed.json'), ('before', '0-malformed.json')):
            for limit in (1, 6):
                with self.subTest(position=position, limit=limit):
                    for n in (1, 2):
                        self.finding(n)
                    self.malformed(name=name)
                    with self.assertRaises(ContentError) as caught:
                        build(self.root, limit, replace=True)
                    self.assertEqual(str(caught.exception), f'{name}: expected an object')
                    self.assertFalse(self.folder.exists())
                    (self.content / 'findings' / name).unlink()

    def test_first_build_shape_refusal_creates_no_outputs(self):
        self.finding(1)
        self.malformed()
        before = tree(self.root)
        with self.assertRaises(ContentError):
            build(self.root)
        self.assertEqual(tree(self.root), before)
        self.assertFalse((self.root / 'kit/flyer').exists())

    def test_replace_shape_refusal_preserves_private_flyer(self):
        self.branded()
        build(self.root)
        sentinel = self.root / 'kit' / 'unrelated.txt'
        sentinel.write_text('unrelated')
        os.chmod(sentinel, 0o644)
        self.malformed()
        before = tree(self.root)
        with self.assertRaises(ContentError) as caught:
            build(self.root, replace=True)
        self.assertEqual(str(caught.exception), f'{NAME}: expected an object')
        self.assertEqual(tree(self.root), before)
        (self.content / 'findings' / NAME).unlink()
        report = build(self.root, replace=True)
        self.assertEqual(report['findings'], 1)
        self.assertEqual((self.folder / 'logo.png').read_bytes(), (self.content / 'public-assets/mark.png').read_bytes())

    def test_real_cli_shape_refusal_and_recovery(self):
        self.finding(1)
        self.malformed(json.dumps([CANARY]))
        before = tree(self.root)
        for extra in ((), ('--replace',)):
            with self.subTest(extra=extra):
                result = self.cli(*extra)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, '')
                self.assertEqual(result.stderr.strip(), STOPPED)
                self.assertNotIn('Traceback', result.stderr)
                self.assertNotIn(CANARY, result.stderr)
                self.assertEqual(tree(self.root), before)
        (self.content / 'findings' / NAME).unlink()
        result = self.cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.folder / 'flyer.html').is_file())

    def test_object_state_filter_and_healthy_output_compatibility(self):
        self.finding(1)
        self.finding(2, state='draft')
        self.finding(3, state='challenged')
        self.finding(4, state='withdrawn')
        self.malformed('{}', name='zz-absent-state.json')
        self.malformed('{"state": "draft"}', name='zz-minimal-draft.json')
        report = build(self.root)
        self.assertEqual(report, {'written': ['kit/flyer/flyer.html', 'kit/flyer/flyer.txt'], 'findings': 1, 'truncated': 0})
        self.assertIn('Finding 1', self.text())
        self.assertNotIn('Finding 2', self.text())
        # a malformed published object still gets the existing loader error
        self.malformed('{"state": "published", "title": "x"}', name='zz-bad-published.json')
        with self.assertRaises(ContentError) as caught:
            build(self.root, replace=True)
        self.assertTrue(str(caught.exception).startswith('zz-bad-published.json'), str(caught.exception))


if __name__ == '__main__':
    unittest.main()
