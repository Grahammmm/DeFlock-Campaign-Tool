"""Distinct approved follow-ups retain request-wide caps and uncertainty holds."""
import tempfile
import unittest
from pathlib import Path

from campaign_tool.outbox import (AmbiguousFailure, Blocked, Outbox, ProviderReceipt,
                                  RequestDraft, idempotency_key)


class FollowupIntentTests(unittest.TestCase):
    def test_later_followup_same_scope_is_distinct_but_replay_is_not(self):
        with tempfile.TemporaryDirectory() as folder:
            stamp = ['2026-10-03T10:00:00Z']
            box = Outbox(Path(folder)/'outbox.sqlite', clock=lambda: stamp[0])
            calls = []
            def send(draft):
                calls.append(draft.intent_id)
                return ProviderReceipt('email', 'synthetic-'+draft.intent_id)
            try:
                for intent, day in [('followup-one', '03'), ('followup-two', '04')]:
                    stamp[0] = f'2026-10-{day}T10:00:00Z'
                    draft = RequestDraft('request', 'agency', 1, 'email', intent,
                        'Synthetic follow-up '+intent, 'records@example.invalid',
                        kind='send_followup', intent_id=intent)
                    row = box.propose(draft); box.approve(row['idempotency_key'], 'owner@example.invalid')
                    box.send(row['idempotency_key'], send)
                    box.send(row['idempotency_key'], send)
                self.assertEqual(calls, ['followup-one', 'followup-two'])
                self.assertEqual(len(box.rows()), 2)
            finally:
                box.close()

    def test_new_intent_does_not_evade_ambiguous_request_hold(self):
        with tempfile.TemporaryDirectory() as folder:
            box = Outbox(Path(folder)/'outbox.sqlite')
            try:
                for intent in ['one', 'two']:
                    draft = RequestDraft('request', 'agency', 1, 'email', intent, intent,
                        'records@example.invalid', kind='send_followup', intent_id=intent)
                    row = box.propose(draft); box.approve(row['idempotency_key'], 'owner@example.invalid')
                    if intent == 'one':
                        def uncertain(_):
                            raise AmbiguousFailure('synthetic uncertain provider')
                        with self.assertRaises(AmbiguousFailure):
                            box.send(row['idempotency_key'], uncertain)
                    else:
                        with self.assertRaises(Blocked) as refused:
                            box.send(row['idempotency_key'], lambda _: self.fail('no second provider call'))
                        self.assertEqual(refused.exception.reason, 'ambiguous_send_unresolved')
            finally:
                box.close()

    def test_legacy_key_and_serialization_remain_stable(self):
        draft = RequestDraft('request', 'agency', 1, 'email', 's', 'b', 'records@example.invalid')
        self.assertNotIn('intent_id', draft.to_json())
        self.assertEqual(RequestDraft.from_json(draft.to_json()).intent_id, '')
        self.assertEqual(idempotency_key('send_request', 'request', 1),
                         idempotency_key('send_request', 'request', 1, ''))
        with self.assertRaises(ValueError):
            idempotency_key('send_request', 'request', 1, 'forged-intent')
        with self.assertRaises(ValueError):
            idempotency_key('send_followup', 'request', 1, 'bad\0intent')

    def test_distinct_intent_keeps_daily_cap_and_legacy_hold(self):
        for legacy in [False, True]:
            with self.subTest(legacy=legacy), tempfile.TemporaryDirectory() as folder:
                box = Outbox(Path(folder)/'outbox.sqlite')
                try:
                    first = RequestDraft('request', 'agency', 1, 'email', 'first', 'first',
                        'records@example.invalid', kind='send_followup', intent_id='' if legacy else 'one')
                    row = box.propose(first); box.approve(row['idempotency_key'], 'owner@example.invalid')
                    box.send(row['idempotency_key'], lambda _: ProviderReceipt('email', 'synthetic-first'))
                    second = RequestDraft('request', 'agency', 1, 'email', 'second', 'second',
                        'records@example.invalid', kind='send_followup', intent_id='two')
                    row = box.propose(second); box.approve(row['idempotency_key'], 'owner@example.invalid')
                    with self.assertRaises(Blocked) as held:
                        box.send(row['idempotency_key'], lambda _: self.fail('no second call'))
                    self.assertEqual(held.exception.reason, 'legacy_followup_intent_unresolved' if legacy else 'daily_agency_cap')
                finally:
                    box.close()
