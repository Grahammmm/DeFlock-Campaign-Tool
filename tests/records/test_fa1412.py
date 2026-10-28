"""Offline post-TLS policy regressions, using full modules and real policies."""
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests.fixtures.fa1412.support import offline_boundary

REPO = Path(__file__).resolve().parents[2]
# Scoped also for ordinary unittest import/discovery, not just the launcher.
with tempfile.TemporaryDirectory(prefix='fa1412-import-') as _import_root:
    with offline_boundary(_import_root, REPO):
        from campaign_tool.records.portal.https_transport import StdlibHTTPS, EgressApproval
        from campaign_tool.records.portal.policy import Approval, PortalError
        from tests.test_records_portal_https import SocketFixture
        from tests.test_records_portal import NOW, HOST, URL, PDF, approval


class PresendPolicyTests(unittest.TestCase):
    def setUp(self):
        self.fixture = tempfile.TemporaryDirectory(prefix='fa1412-case-')
        self.addCleanup(self.fixture.cleanup)
        self.boundary = offline_boundary(self.fixture.name, REPO)
        self.boundary.__enter__()
        self.addCleanup(self.boundary.__exit__, None, None, None)
        self.sock = SocketFixture(b'HTTP/1.1 200 OK\r\nContent-Length: ' +
                                  str(len(PDF)).encode() + b'\r\n\r\n' + PDF)
        self.now = NOW
        self.mono = 0
        self.connected = False
        self.events = []
        self.resolutions = []
        self.connections = []
        self.auth = approval()
        self.egress = EgressApproval([HOST], NOW + timedelta(days=1))
        self.on_resolve = lambda: None
        self.on_connect = lambda: None
        self.on_approval = lambda: None
        self.on_egress = lambda: None

    def transport(self, origin=HOST):
        def wall():
            self.events.append(('wall', self.connected, self.now))
            return self.now
        def admin():
            self.events.append(('admin', self.connected))
            self.on_approval()
            return self.auth
        def egress():
            self.events.append(('egress', self.connected))
            self.on_egress()
            return self.egress
        def resolve(host, timeout):
            self.resolutions.append((host, timeout))
            self.on_resolve()
            return ['93.184.216.34']
        def connect(ip, host, timeout):
            self.connections.append((ip, host, timeout))
            self.connected = True
            self.on_connect()
            return self.sock
        return StdlibHTTPS(apply=True, approval_loader=admin, egress_loader=egress,
                           origin_host=origin, resolver=resolve, connector=connect,
                           clock=lambda: self.mono, wall=wall)

    def consume(self, url=URL, origin=HOST):
        result = self.transport(origin).open(url, timeout=10)
        try:
            self.assertEqual(b''.join(result.chunks), PDF)
        finally:
            result.close()

    def refuse(self, reason, *, connects=1, resolutions=1, url=URL, origin=HOST):
        error = None
        try:
            self.consume(url, origin)
        except PortalError as exc:
            error = exc
        # This is deliberately first: the mutant must fail zero-send itself.
        self.assertEqual(self.sock.sent, [], 'policy refusal must send zero bytes')
        self.assertIsNotNone(error, 'missing policy refusal')
        self.assertEqual(str(error), reason)
        self.assertEqual(len(self.connections), connects)
        self.assertEqual(len(self.resolutions), resolutions)
        self.assertEqual(self.sock.closed, bool(connects))

    def test_valid_permissions_exact_request(self):
        self.consume()
        self.assertEqual(self.sock.sent, [b'GET /documents/42/download?token=synthetic-only HTTP/1.1\r\n'
                         b'Host: portal.example.test\r\nUser-Agent: records-portal/1\r\n'
                         b'Accept-Encoding: identity\r\nConnection: close\r\n\r\n'])
        self.assertEqual([row[0] for row in self.resolutions], [HOST])
        self.assertEqual([row[:2] for row in self.connections], [('93.184.216.34', HOST)])
        self.assertTrue(self.sock.closed)

    def test_admin_expiry_after_connect_zero_send(self):
        for delta in (timedelta(0), timedelta(microseconds=1)):
            with self.subTest(delta=delta):
                self.reset_subcase()
                self.on_connect = lambda: setattr(self, 'now', self.auth.expires + delta)
                self.refuse('approval_expired')

    def reset_subcase(self):
        self.sock = SocketFixture(b'HTTP/1.1 200 OK\r\nContent-Length: ' +
                                  str(len(PDF)).encode() + b'\r\n\r\n' + PDF)
        self.now = NOW
        self.connected = False
        self.events = []
        self.resolutions = []
        self.connections = []

    def test_egress_expiry_after_connect_zero_send(self):
        for delta in (timedelta(0), timedelta(microseconds=1)):
            with self.subTest(delta=delta):
                self.reset_subcase()
                # Administrator stays valid so only independent egress decides.
                self.auth = replace(self.auth, expires=NOW + timedelta(days=2))
                self.on_connect = lambda: setattr(self, 'now', self.egress.expires + delta)
                self.refuse('egress_denied')

    def test_permission_instant_just_before_expiry_is_positive(self):
        self.on_connect = lambda: setattr(self, 'now', self.auth.expires - timedelta(microseconds=1))
        self.consume()
        self.assertEqual(len(self.sock.sent), 1)

    def test_admin_scope_revoked_in_connector(self):
        self.on_connect = lambda: setattr(self, 'auth', replace(self.auth, scopes={HOST: ('/other/',)}))
        self.refuse('path_not_approved')

    def test_original_origin_rechecked_in_connector(self):
        self.on_connect = lambda: setattr(self, 'auth', replace(self.auth, portals=frozenset()))
        self.refuse('portal_not_approved')

    def test_redirect_permission_rechecked_against_original_origin(self):
        other = 'objects.example.test'
        self.egress = EgressApproval([other], self.egress.expires)
        self.on_connect = lambda: setattr(self, 'auth', replace(self.auth, redirects=frozenset()))
        self.refuse('redirect_not_approved', url='https://' + other + '/production/file?part=1')
        self.assertEqual(self.connections[0][1], other)

    def test_egress_host_revoked_in_connector(self):
        self.on_connect = lambda: setattr(self, 'egress', EgressApproval([], self.egress.expires))
        self.refuse('egress_denied')

    def test_admin_loader_typed_error_no_retry(self):
        def load():
            if self.connected:
                raise PortalError('approval_missing_or_unsafe')
        self.on_approval = load
        self.refuse('approval_missing_or_unsafe')

    def test_egress_loader_typed_error_no_retry(self):
        def load():
            if self.connected:
                raise PortalError('egress_policy_missing_or_invalid')
        self.on_egress = load
        self.refuse('egress_policy_missing_or_invalid')

    def test_already_expired_admin_precedes_dns(self):
        self.auth = replace(self.auth, expires=NOW)
        self.refuse('approval_expired', connects=0, resolutions=0)
        self.assertFalse(any(event[0] == 'egress' for event in self.events))

    def test_already_expired_egress_precedes_dns(self):
        self.egress = EgressApproval([HOST], NOW)
        self.refuse('egress_denied', connects=0, resolutions=0)

    def test_expired_after_resolution_still_precedes_connect(self):
        self.on_resolve = lambda: setattr(self, 'now', self.auth.expires)
        self.refuse('approval_expired', connects=0)

    def test_post_connect_deadline_precedes_policy_work(self):
        self.on_connect = lambda: setattr(self, 'mono', 10)
        with self.assertRaises(TimeoutError):
            self.consume()
        self.assertEqual(self.sock.sent, [])
        self.assertTrue(self.sock.closed)
        self.assertEqual(len(self.connections), 1)
        self.assertFalse(any(e[1] for e in self.events if e[0] in ('admin', 'egress', 'wall')))

    def deadline_during_loader(self, which):
        def load():
            if self.connected:
                self.mono = 10
        setattr(self, 'on_' + which, load)
        with self.assertRaises(TimeoutError):
            self.consume()
        self.assertEqual(self.sock.sent, [])
        self.assertTrue(self.sock.closed)
        self.assertEqual(len(self.connections), 1)

    def test_admin_load_consumes_remaining_budget(self):
        self.deadline_during_loader('approval')

    def test_egress_load_consumes_remaining_budget(self):
        self.deadline_during_loader('egress')

    def test_load_elapsed_time_reduces_socket_timeout(self):
        def load():
            if self.connected:
                self.mono = 4
        self.on_egress = load
        self.consume()
        self.assertEqual(self.sock.timeouts[0], 6)
        self.assertEqual(len(self.connections), 1)

    def test_one_fresh_shared_wall_instant_after_connect(self):
        seen = []
        authorize, allows = Approval.authorize, EgressApproval.allows
        def admin(obj, url, origin, now):
            if self.connected:
                seen.append(('admin', now))
            return authorize(obj, url, origin, now)
        def egress(obj, host, now):
            if self.connected:
                seen.append(('egress', now))
            return allows(obj, host, now)
        self.on_connect = lambda: setattr(self, 'now', NOW + timedelta(hours=1))
        with patch.object(Approval, 'authorize', admin), patch.object(EgressApproval, 'allows', egress):
            self.consume()
        expected = NOW + timedelta(hours=1)
        self.assertEqual(seen, [('admin', expected), ('egress', expected)])
        self.assertEqual([e for e in self.events if e[0] == 'wall' and e[1]], [('wall', True, expected)])
        self.assertEqual([e[0] for e in self.events if e[0] in ('admin', 'egress')],
                         ['admin', 'egress'] * 3)

    def test_signed_query_and_literal_target_preserved(self):
        target = '/documents/42/download?part=two%2Fthree&part=1+2&empty=&sig=synthetic'
        self.consume('https://' + HOST + target)
        self.assertEqual(self.sock.sent[0].split(b'\r\n')[0], b'GET ' + target.encode('ascii') + b' HTTP/1.1')


if __name__ == '__main__':
    unittest.main()
