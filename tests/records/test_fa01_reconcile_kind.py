"""FA01: unsupported mail occurrence kinds never consume reconciliation coverage.

Synthetic inventory rows shaped like tests/records/test_ledger_reconcile.py;
never campaign records, mailbox data or provider responses.
"""
import hashlib
import unittest

from campaign_tool.records.ledger.reconcile import reconcile

SHA = hashlib.sha256(b'example').hexdigest()
OTHER_SHA = hashlib.sha256(b'other').hexdigest()
RECEIPT = hashlib.sha256(b'receipt').hexdigest()
IDENTITY = {'account': 'fixture', 'folder': 'inbox', 'uidvalidity': 1, 'uid': 2}
MESSAGE = {'identity': IDENTITY, 'sha256': SHA, 'receipt_sha256': RECEIPT}
# Every explicit discriminator outside the frozen {'mail', 'attachment'} set.
UNSUPPORTED_KINDS = (None, True, False, 0, 1, 2.5, [], ['mail'], ['attachment'], {}, {'kind': 'mail'},
                     'unsupported-kind', '', 'MAIL', 'Mail', 'mail ', 'Attachment', 'message', 'portal',
                     'digests')
GATE = 'FA01-KIND-GATE'
# Raw outcomes of the synthetic control.
CONTROL_OUTCOMES = []


def message(**changes):
    return dict(MESSAGE, **changes)


def attachment(part, **changes):
    row = dict(MESSAGE, kind='attachment', identity=dict(IDENTITY, mime_part_path=part))
    row.update(changes)
    return row


def kind_bypass(kind, sources, indexed, objects):
    """Test-only defective reconciler: treats unsupported kinds as an absent kind.

    It reproduces the pre-fix behaviour without patching the production module:
    the real reconcile is called on copies whose unsupported kind is dropped.
    """
    def strip(row):
        if 'kind' in row and not (isinstance(row['kind'], str) and row['kind'] in ('mail', 'attachment')):
            row = {k: v for k, v in row.items() if k != 'kind'}
        return row
    return reconcile(kind, [strip(r) for r in sources], [strip(r) for r in indexed], objects)


def check_v2_shape(test, result, sources, index):
    test.assertEqual(result['schema'], 'reconciliation-v2', GATE)
    test.assertEqual(result['kind'], 'mail', GATE)
    test.assertEqual(result['stage_promotions'], 0, GATE)
    test.assertEqual(result['acceptance'], 'supplied_evidence_only', GATE)
    test.assertEqual(result['unexplained_rows'], 0, GATE)
    test.assertEqual((result['source_rows'], result['index_rows']), (sources, index), GATE)
    test.assertEqual(result['classified_rows'], len(result['rows']), GATE)
    test.assertRegex(result['report_sha256'], r'^[0-9a-f]{64}$')


def check_valid_match(test, fn):
    """Positive: a legitimate message pair binds exactly one index row."""
    result = fn('mail', [message()], [message()], [SHA])
    check_v2_shape(test, result, 1, 1)
    test.assertEqual(result['classifications'], {'matched': 1}, GATE + ' positive')
    test.assertEqual(sum(map(len, result['index_match_groups'].values())), 1, GATE)
    test.assertIsNotNone(result['rows'][0]['index_match_group'], GATE)
    return result


def check_invalid_kind_rejected(test, fn, bad):
    """Negative: an unsupported source kind is incomplete and binds nothing."""
    result = fn('mail', [message(kind=bad)], [message()], [SHA])
    check_v2_shape(test, result, 1, 1)
    test.assertEqual(result['classifications'], {'incomplete_receipt': 1, 'index_without_bound_receipt': 1},
                     GATE + ' source kind=' + repr(bad))
    test.assertEqual(result['index_match_groups'], {}, GATE + ' source kind=' + repr(bad))
    test.assertIsNone(result['rows'][0]['index_match_group'], GATE)
    return result


class Regression(unittest.TestCase):
    """A1: real reconcile API, positive, negative, boundary and preservation."""

    def run_mail(self, sources, index, objects=(SHA,)):
        return reconcile('mail', sources, index, list(objects))

    def test_absent_kind_and_explicit_mail_are_the_same_message(self):
        check_valid_match(self, reconcile)
        for source, index in ((message(kind='mail'), message()), (message(), message(kind='mail')),
                              (message(kind='mail'), message(kind='mail'))):
            with self.subTest(source=source.get('kind'), index=index.get('kind')):
                result = self.run_mail([source], [index])
                check_v2_shape(self, result, 1, 1)
                self.assertEqual(result['classifications'], {'matched': 1})
                self.assertEqual(sum(map(len, result['index_match_groups'].values())), 1)
        # The established legacy shape is preserved byte for byte in the report.
        self.assertEqual(self.run_mail([message()], [message()]),
                         self.run_mail([message(kind='mail')], [message(kind='mail')]))

    def test_attachment_parts_with_identical_bytes_match_separately(self):
        parts = [attachment('1.2'), attachment('1.3')]
        result = self.run_mail(parts, list(parts))
        check_v2_shape(self, result, 2, 2)
        self.assertEqual(result['classifications'], {'matched': 2})
        groups = [row['index_match_group'] for row in result['rows']]
        self.assertEqual(len(set(groups)), 2)
        self.assertEqual(sorted(sum(result['index_match_groups'].values(), [])), [0, 1])
        # Identical source and index identities: message and attachment stay distinct.
        mixed = self.run_mail([message(), attachment('2')], [attachment('2'), message()])
        self.assertEqual(mixed['classifications'], {'matched': 2})
        self.assertEqual(sorted(sum(mixed['index_match_groups'].values(), [])), [0, 1])

    def test_every_unsupported_source_kind_is_incomplete(self):
        for bad in UNSUPPORTED_KINDS:
            with self.subTest(kind=bad):
                check_invalid_kind_rejected(self, reconcile, bad)
                # Attachment-shaped identity does not rescue an unsupported discriminator.
                shaped = attachment('1.2', kind=bad)
                result = self.run_mail([shaped], [attachment('1.2')])
                self.assertEqual(result['classifications'],
                                 {'incomplete_receipt': 1, 'index_without_bound_receipt': 1})
                self.assertEqual(result['index_match_groups'], {})

    def test_every_unsupported_index_kind_stays_unbound(self):
        for bad in UNSUPPORTED_KINDS:
            with self.subTest(kind=bad):
                result = self.run_mail([message()], [message(kind=bad)])
                check_v2_shape(self, result, 1, 1)
                self.assertEqual(result['classifications'], {'stale_index': 1, 'index_without_bound_receipt': 1})
                self.assertEqual(result['index_match_groups'], {})
                self.assertEqual(result['rows'][1], {'index_row': 0, 'classification': 'index_without_bound_receipt',
                                                     'subject_sha256': SHA})
                both = self.run_mail([message(kind=bad)], [message(kind=bad)])
                self.assertEqual(both['classifications'], {'incomplete_receipt': 1, 'index_without_bound_receipt': 1})
                self.assertEqual(both['index_match_groups'], {})

    def test_rejected_kind_does_not_reserve_later_valid_pair(self):
        bad = message(kind='unsupported-kind')
        for sources, expected in (([bad, message(), message()], ['incomplete_receipt', 'matched', 'duplicate_source_receipt']),
                                  ([message(), bad, message()], ['matched', 'incomplete_receipt', 'duplicate_source_receipt'])):
            with self.subTest(expected=expected):
                result = self.run_mail(sources, [message()])
                self.assertEqual([r['classification'] for r in result['rows']], expected)
                self.assertEqual(sum(map(len, result['index_match_groups'].values())), 1)
                rejected = next(r for r in result['rows'] if r['classification'] == 'incomplete_receipt')
                self.assertIsNone(rejected['index_match_group'])
                self.assertEqual(result['stage_promotions'], 0)

    def test_changing_only_kind_decides_the_result(self):
        for legitimate, objects in ((message(), [SHA]), (attachment('1.2'), [SHA])):
            for bad in ('unsupported-kind', None, 1, []):
                with self.subTest(base=legitimate.get('kind'), kind=bad):
                    good = self.run_mail([legitimate], [legitimate], objects)
                    changed = self.run_mail([dict(legitimate, kind=bad)], [legitimate], objects)
                    self.assertEqual(good['classifications'], {'matched': 1})
                    self.assertEqual(changed['classifications'],
                                     {'incomplete_receipt': 1, 'index_without_bound_receipt': 1})

    def test_attachment_kind_never_inferred(self):
        # kind=attachment still needs the hierarchical locator; filename/hash do not substitute.
        for identity in (dict(IDENTITY), dict(IDENTITY, mime_part_path='0.1'), dict(IDENTITY, part=2),
                         dict(IDENTITY, mime_part_path=2)):
            with self.subTest(identity=identity):
                row = dict(MESSAGE, kind='attachment', identity=identity, filename='synthetic.pdf')
                self.assertEqual(self.run_mail([row], [])['classifications'], {'incomplete_receipt': 1})
        # A locator without a kind remains the legacy message, not an inferred attachment.
        located = dict(MESSAGE, identity=dict(IDENTITY, mime_part_path='1.2'), filename='synthetic.pdf')
        result = self.run_mail([located], [message()])
        self.assertEqual(result['classifications'], {'matched': 1})
        self.assertEqual(self.run_mail([located], [attachment('1.2')])['classifications'],
                         {'stale_index': 1, 'index_without_bound_receipt': 1})

    def test_existing_mail_precedence_preserved(self):
        bad = 'unsupported-kind'
        cases = ((message(kind=bad, error='synthetic parser failure'), [SHA], 'parser_error'),
                 (message(kind=bad, scope='excluded', exclusion_reason='synthetic exclusion'), [SHA], 'excluded_content'),
                 (message(kind=bad), [], 'incomplete_receipt'),
                 (message(kind=bad, sha256=OTHER_SHA), [SHA, OTHER_SHA], 'incomplete_receipt'))
        for source, objects, reason in cases:
            with self.subTest(reason=reason, objects=len(objects)):
                result = self.run_mail([source], [message()], objects)
                self.assertEqual(result['classifications'], {reason: 1, 'index_without_bound_receipt': 1})
                self.assertEqual(result['index_match_groups'], {})

    def test_portal_and_digest_branches_unchanged(self):
        portal = {'identity': {'portal_host': 'example.invalid', 'request_id': 'R1', 'item_id': '1'},
                  'sha256': SHA, 'receipt_sha256': RECEIPT, 'kind': 'unsupported-kind'}
        joined = dict(portal, binding_evidence=dict(portal))
        self.assertEqual(reconcile('portal', [joined], [portal], [SHA])['classifications'],
                         {'bytes-present-and-joined': 1})
        self.assertEqual(reconcile('portal', [portal], [], [SHA])['classifications'], {'bytes-present-unjoined': 1})
        digest = reconcile('digests', [{'sha256': SHA, 'kind': None}], [], [SHA])
        self.assertEqual(digest['classifications'], {'declared_only': 1})
        self.assertEqual(digest['stage_promotions'], 0)

    def test_replay_and_reversed_order_keep_coverage_totals(self):
        bad_source = message(kind='unsupported-kind')
        bad_index = message(kind=None)
        forward = self.run_mail([bad_source, message(), attachment('1.2')],
                                [bad_index, message(), attachment('1.2')])
        reverse = self.run_mail([attachment('1.2'), message(), bad_source],
                                [attachment('1.2'), message(), bad_index])
        self.assertEqual(forward, self.run_mail([bad_source, message(), attachment('1.2')],
                                                [bad_index, message(), attachment('1.2')]))
        expected = {'incomplete_receipt': 1, 'index_without_bound_receipt': 1, 'matched': 2}
        self.assertEqual(forward['classifications'], expected)
        self.assertEqual(reverse['classifications'], expected)
        self.assertEqual(sorted(forward['index_match_groups']), sorted(reverse['index_match_groups']))
        for result in (forward, reverse):
            self.assertEqual(sum(map(len, result['index_match_groups'].values())), 2)


class GateControl(unittest.TestCase):
    """A3: a synthetic kind-check bypass must be caught; the real check restores the positive."""

    def test_bypass_is_detected_by_the_invalid_kind_assertion(self):
        for bad in ('unsupported-kind', None, 1, []):
            with self.subTest(kind=bad):
                with self.assertRaises(AssertionError) as caught:
                    check_invalid_kind_rejected(self, kind_bypass, bad)
                message_text = str(caught.exception)
                self.assertIn(GATE + ' source kind=' + repr(bad), message_text)
                CONTROL_OUTCOMES.append({'case': 'bypass', 'kind': repr(bad), 'outcome': 'assertion_failed',
                                         'detail': message_text.splitlines()[0][:300]})
        # The bypass itself is not broken for legitimate input: only the gate differs.
        check_valid_match(self, kind_bypass)

    def test_restored_real_check_passes(self):
        check_valid_match(self, reconcile)
        for bad in ('unsupported-kind', None, 1, []):
            with self.subTest(kind=bad):
                check_invalid_kind_rejected(self, reconcile, bad)
                CONTROL_OUTCOMES.append({'case': 'restored', 'kind': repr(bad), 'outcome': 'passed'})
        CONTROL_OUTCOMES.append({'case': 'restored', 'kind': 'valid-match', 'outcome': 'passed'})


if __name__ == '__main__':
    unittest.main()
