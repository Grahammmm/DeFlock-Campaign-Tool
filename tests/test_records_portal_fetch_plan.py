"""Synthetic producer-shaped queue/ledger/CAS tests; no record content in output."""
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import http.server
import io
import json
import os
from pathlib import Path
import socket
import socketserver
import stat
import threading
import time
import tempfile
import subprocess
import sys
import unittest
from unittest import mock
from urllib.parse import urlsplit
import uuid

from campaign_tool.records.portal import engine, policy
from campaign_tool.records.portal.download import run_download, processing_lock, validate_queue
from campaign_tool.records.portal.https_transport import StdlibHTTPS, EgressApproval, load_egress, public_addresses
from campaign_tool.records.portal.ledger_adapter import WP1LedgerAdapter, verify_cas
from campaign_tool.records.portal.store import Queue, digest_bytes
from campaign_tool.records.ledger import store
from campaign_tool.records import run_stages

FIXTURES = Path(__file__).resolve().parent / 'fixtures' / 'portal_fetch_plan'
OUT = None
NEGATIVE_CONTROLS = []
PROGRESS_EVIDENCE = []
HOST = 'synthetic.example.net'
REDIRECT = 'objects.example.net'
BODY = b'%PDF-1.4\n% synthetic offline fixture\n%%EOF\n'
NOW = datetime.now(timezone.utc)
EXPIRES = (NOW+timedelta(days=2)).isoformat()


def approval_raw(**updates):
    value = {'enabled':True,'purpose':'flock-nextrequest-portal-intake',
             'expires_utc':EXPIRES,'portal_hosts':[HOST],'redirect_hosts':[REDIRECT]}
    value.update(updates)
    return json.dumps(value)


def approval():
    # Parser ownership metadata is an explicit synthetic seam. No chown/root file.
    return policy.parse_approval(approval_raw(),uid=0,mode=stat.S_IFREG|0o600,now=datetime.now(timezone.utc))


def egress():
    return EgressApproval([HOST,REDIRECT],NOW+timedelta(days=2))


def orchestrator():
    # Unlike the records-only candidate copy, repo CI has the actual law/schema
    # dependency. Never leave a missing-dependency stub in sys.modules.
    from campaign_tool.records import run_orchestrator
    return run_orchestrator


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    def log_message(self,*args):
        pass
    def do_GET(self):
        fixture = self.server.fixture
        path = urlsplit(self.path).path
        with fixture.lock:
            fixture.active += 1
            fixture.max_active = max(fixture.max_active,fixture.active)
            fixture.calls.append((self.headers.get('Host'),path))
            route = fixture.routes.get(path,{})
            sequence = route.get('sequence')
            if sequence:
                route = sequence.pop(0) if len(sequence)>1 else sequence[0]
        try:
            time.sleep(route.get('header_delay',0))
            status = route.get('status',200)
            data = route.get('body',BODY)
            self.send_response(status)
            if not route.get('omit_type'):
                self.send_header('Content-Type',route.get('type','application/pdf'))
            if 'location' in route:
                self.send_header('Location',route['location'])
            if 'retry_after' in route:
                self.send_header('Retry-After',route['retry_after'])
            if not route.get('omit_length'):
                self.send_header('Content-Length',str(route.get('declared',len(data))))
            if route.get('duplicate_length'):
                self.send_header('Content-Length',str(len(data)))
            self.send_header('Connection','close')
            self.end_headers()
            time.sleep(route.get('body_delay',0))
            if route.get('piece_delay'):
                for byte in data:
                    self.wfile.write(bytes([byte])); self.wfile.flush()
                    time.sleep(route['piece_delay'])
            else:
                self.wfile.write(data); self.wfile.flush()
        except (BrokenPipeError,ConnectionResetError,OSError):
            pass
        finally:
            with fixture.lock:
                fixture.active -= 1
            self.close_connection = True


class Server(socketserver.ThreadingMixIn,socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = False
    def handle_error(self,*args):
        # Broken clients/timeouts are deliberate fixture inputs, never log requests.
        pass


class Fixture:
    def __init__(self):
        self.routes = {}
        self.calls = []
        self.lock = threading.Lock()
        self.active = self.max_active = 0
        self.server = Server(('127.0.0.1',0),Handler)
        self.server.fixture = self
        self.thread = threading.Thread(target=lambda:self.server.serve_forever(poll_interval=0.01),daemon=True)
        self.thread.start()
    def close(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(2)
    def connector(self,ip,host,timeout):
        if host not in {HOST,REDIRECT,'denied.example.net'}:
            raise AssertionError('unknown_fixture_host')
        sock = socket.socket(socket.AF_INET,socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(('127.0.0.1',self.server.server_address[1]))
        return sock
    def transport(self,**kwargs):
        # Actual production HTTP parser/stream/deadline machinery. The only seam
        # maps a validated public numeric fixture address to this loopback server.
        return StdlibHTTPS(apply=True,approval_loader=kwargs.get('approval_loader',approval),
                           egress_loader=kwargs.get('egress_loader',egress),max_bytes=kwargs.get('max_bytes',25*1024*1024),
                           resolver=lambda host,timeout:['93.184.216.34'],connector=self.connector)


class World:
    def __init__(self):
        self.root = OUT / ('fixture-'+uuid.uuid4().hex)
        self.root.mkdir(mode=0o700)
        self.processing = self.root/'processing-v1'; self.processing.mkdir(mode=0o700)
        (self.processing/'intake').mkdir(mode=0o700)
        (self.processing/'intake'/'blobs').mkdir(mode=0o700)
        store.initialize(self.processing/'ledger.sqlite')
        self.queue = Queue(self.root/'portal-queue-v1')
        self.fixture = Fixture()
    def close(self):
        self.queue.close(); self.fixture.close()
    def item(self,path='/documents/ok',*,item_id=None,host=HOST,source=None):
        source = source or digest_bytes(b'synthetic preserved mail message')
        return self.queue.inventory(host,'mail:'+source[:32],item_id or path.rsplit('/',1)[-1],
                                    'https://'+host+path+'?signed=private-fixture-token',source)
    def fetch(self,**kwargs):
        return run_download(self.queue,self.processing,apply=True,approval_loader=kwargs.pop('approval_loader',approval),
                            egress_loader=kwargs.pop('egress_loader',egress),
                            transport=kwargs.pop('transport',self.fixture.transport(max_bytes=kwargs.get('limits',engine.Limits()).max_bytes)),**kwargs)
    def item_state(self,key):
        return dict(self.queue.db.execute('SELECT * FROM portal_items WHERE id=?',(key,)).fetchone())
    def canonical_counts(self):
        with store.ledger(self.processing/'ledger.sqlite',readonly=True) as con:
            return tuple(con.execute('SELECT count(*) FROM '+table).fetchone()[0]
                         for table in ('originals','occurrences','portal_items','runs','stage_state'))


@contextmanager
def world():
    value = World()
    try:
        yield value
    finally:
        value.close()


class PortalTests(unittest.TestCase):
    def setUp(self):
        global OUT
        self.temp = tempfile.TemporaryDirectory(prefix='portal-fetch-plan-')
        self.addCleanup(self.temp.cleanup)
        previous = OUT
        OUT = Path(self.temp.name)
        self.addCleanup(setattr, sys.modules[__name__], 'OUT', previous)
        # The production connector is replaced by the fixture; also fail closed
        # on accidental DNS or non-loopback sockets in any composed path.
        connect, bind = socket.socket.connect, socket.socket.bind
        def local(operation):
            def checked(sock, address):
                if not isinstance(address, tuple) or address[0] != '127.0.0.1':
                    raise AssertionError('offline_nonloopback_forbidden')
                return operation(sock, address)
            return checked
        for name, value in (('connect', local(connect)), ('bind', local(bind))):
            self.enterContext(mock.patch.object(socket.socket, name, value))
        for name in ('getaddrinfo', 'gethostbyname', 'gethostbyaddr'):
            self.enterContext(mock.patch.object(socket, name, side_effect=AssertionError('offline_dns_forbidden')))

    def test_01_import_guards_and_no_default_fetch(self):
        # Audit hooks cannot be removed. Exercise the pre-import guards in an
        # isolated process, not in the repository's shared unittest interpreter.
        result = subprocess.run([sys.executable, '-B', str(FIXTURES / 'guard_smoke.py'), str(OUT)],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('pre-import guards and dry default: OK', result.stdout)

    def test_02_legacy_allowlist_and_precise_scopes(self):
        a = approval()
        self.assertEqual(a.authorize('https://'+HOST+'/documents/a',HOST,NOW),HOST)
        self.assertEqual(a.authorize('https://'+REDIRECT+'/synthetic/a',HOST,NOW),REDIRECT)
        for url,reason in [('https://denied.example.net/documents/a','redirect_not_approved'),
                           ('https://'+HOST+'/account/a','path_not_approved'),
                           ('https://'+REDIRECT+'/other/a','path_not_approved')]:
            with self.assertRaisesRegex(policy.PortalError,reason):
                a.authorize(url,HOST,NOW)
        for url in ('http://'+HOST+'/documents/a','https://u:p@'+HOST+'/documents/a',
                    'https://'+HOST+':444/documents/a','https://'+HOST+'/documents/../a',
                    'https://'+HOST+'/documents/%252e%252e/a','https://'+HOST+'/documents/a#fragment'):
            with self.subTest(url_kind=url.split(':',1)[0]):
                with self.assertRaises(policy.PortalError):
                    a.authorize(url,HOST,NOW)

    def test_03_fail_closed_parse_ownership_expiry(self):
        variants = ['null','[]','{',approval_raw(enabled=False),approval_raw(extra='unknown'),
                    approval_raw(portal_hosts=None),approval_raw(portal_hosts=[HOST,HOST]),
                    approval_raw(redirect_hosts=[HOST]),approval_raw(expires_utc='2000-01-01T00:00:00+00:00'),
                    approval_raw(expires_utc='2030-01-01T00:00:00'),
                    approval_raw(approved_portal_hosts=['denied.example.net']),
                    approval_raw().replace('"enabled": true','"enabled": false, "enabled": true'),
                    approval_raw().replace('true','NaN',1)]
        for raw in variants:
            with self.subTest(case=variants.index(raw)):
                with self.assertRaises(policy.PortalError):
                    policy.parse_approval(raw,uid=0,mode=stat.S_IFREG|0o600,now=NOW)
        for uid,mode in ((1000,stat.S_IFREG|0o600),(0,stat.S_IFREG|0o622),(0,stat.S_IFIFO|0o600)):
            with self.assertRaisesRegex(policy.PortalError,'approval_not_admin_owned'):
                policy.parse_approval(approval_raw(),uid=uid,mode=mode,now=NOW)
        with world() as w:
            w.item()
            def broken():
                return policy.parse_approval('{',uid=0,mode=stat.S_IFREG|0o600,now=NOW)
            with self.assertRaises(policy.PortalError):
                w.fetch(approval_loader=broken)
            self.assertEqual(w.fixture.calls,[])
            self.assertEqual(w.queue.status()['attempts'],0)

    def test_04_initial_allowlist_denial_no_socket(self):
        with world() as w:
            key=w.item(host='denied.example.net')
            w.fetch()
            self.assertEqual(w.fixture.calls,[])
            self.assertEqual(w.item_state(key)['reason'],'portal_not_approved')
            self.assertEqual(w.item_state(key)['state'],'blocked')

    def test_05_redirect_denial_before_target_request(self):
        with world() as w:
            w.fixture.routes['/documents/redirect']={'status':302,'location':'https://denied.example.net/documents/other'}
            key=w.item('/documents/redirect'); w.fetch()
            self.assertEqual(len(w.fixture.calls),1)
            self.assertEqual(w.item_state(key)['reason'],'redirect_not_approved')
            self.assertEqual(w.queue.status()['versions'],0)

    def test_06_allowed_redirect_and_loop_limit(self):
        with world() as w:
            w.fixture.routes['/documents/redirect']={'status':302,'location':'https://'+REDIRECT+'/synthetic/file'}
            key=w.item('/documents/redirect'); result=w.fetch()
            self.assertEqual(result['ledger_delivered'],1)
            self.assertEqual(w.item_state(key)['state'],'received')
            self.assertEqual([c[0] for c in w.fixture.calls],[HOST,REDIRECT])
        with world() as w:
            w.fixture.routes['/documents/loop']={'status':302,'location':'/documents/loop'}
            key=w.item('/documents/loop'); w.fetch(limits=engine.Limits(max_redirects=1))
            self.assertEqual(len(w.fixture.calls),2)
            self.assertEqual(w.item_state(key)['reason'],'redirect_limit')

    def test_07_store_metadata_and_idempotent_rerun(self):
        with world() as w:
            key=w.item(); report=w.fetch()
            self.assertEqual(report['ledger_delivered'],1)
            version=dict(w.queue.db.execute('SELECT * FROM portal_versions').fetchone())
            provenance=json.loads(version['provenance_json'])
            self.assertEqual(provenance['queue_item_id'],key)
            self.assertEqual(provenance['source_path_sha256'],digest_bytes(b'/documents/ok'))
            self.assertEqual(provenance['mail_message_sha256s'],[digest_bytes(b'synthetic preserved mail message')])
            self.assertIsNotNone(datetime.fromisoformat(provenance['fetched_at']).tzinfo)
            self.assertEqual(provenance['http_evidence'][-1]['content_type'],'application/pdf')
            self.assertEqual(provenance['http_evidence'][-1]['http_status'],200)
            self.assertEqual(version['sha256'],digest_bytes(BODY))
            self.assertEqual(version['byte_count'],len(BODY))
            blob=w.processing/'intake'/'blobs'/digest_bytes(BODY)
            self.assertEqual(blob.read_bytes(),BODY)
            self.assertEqual(stat.S_IMODE(blob.stat().st_mode),0o400)
            before=blob.stat(); counts=w.canonical_counts(); calls=len(w.fixture.calls)
            w.queue.close(); w.queue=Queue(w.root/'portal-queue-v1')
            again=w.fetch()
            self.assertEqual(again['attempted'],0)
            self.assertEqual(again['body_bytes_received'],0)
            self.assertEqual(len(w.fixture.calls),calls)
            self.assertEqual(w.canonical_counts(),counts)
            self.assertEqual((blob.stat().st_ino,blob.stat().st_mtime_ns),(before.st_ino,before.st_mtime_ns))
            self.assertEqual(w.queue.status()['versions'],1)
            with store.ledger(w.processing/'ledger.sqlite',readonly=True) as con:
                self.assertEqual(set(r[0] for r in con.execute('SELECT status FROM stage_state')),{'pending'})
                original=dict(con.execute('SELECT * FROM originals').fetchone())
                self.assertEqual(original['storage_path'],str(blob))
                self.assertEqual(original['preservation_status'],'receipt_validation_pending')
            # No private query, body or exception narrative enters metadata/status.
            self.assertNotIn('private-fixture-token',json.dumps(provenance))
            self.assertNotIn('signed=',json.dumps(w.queue.status()))

    def test_08_duplicate_content_retains_distinct_provenance(self):
        with world() as w:
            one=w.item('/documents/a')
            two=w.item('/documents/b',source=digest_bytes(b'second synthetic message'))
            report=w.fetch()
            self.assertEqual(report['ledger_delivered'],2)
            self.assertEqual(w.canonical_counts(),(1,2,2,2,7))
            self.assertEqual(len(list((w.processing/'intake'/'blobs').iterdir())),1)
            self.assertEqual(w.item_state(one)['current_sha256'],w.item_state(two)['current_sha256'])
            self.assertEqual(w.fixture.max_active,1)

    def test_09_per_file_cap_and_streaming_cap(self):
        for omit in (False,True):
            with self.subTest(omit_length=omit), world() as w:
                w.fixture.routes['/documents/big']={'body':BODY*5,'omit_length':omit}
                key=w.item('/documents/big')
                report=w.fetch(limits=engine.Limits(max_bytes=len(BODY)))
                self.assertEqual(w.item_state(key)['state'],'blocked')
                self.assertEqual(w.item_state(key)['reason'],'size_limit')
                self.assertLessEqual(report['body_bytes_received'],len(BODY))
                self.assertEqual(w.queue.status()['versions'],0)
                self.assertEqual(w.canonical_counts()[0],0)

    def test_10_shared_total_cap_and_item_cap(self):
        with world() as w:
            for label in ('a','b','c'): w.item('/documents/'+label)
            report=w.fetch(limits=engine.Limits(max_bytes=len(BODY),max_total_bytes=len(BODY)*2))
            self.assertEqual(report['body_bytes_received'],len(BODY)*2)
            self.assertEqual(report['attempted'],2)
            self.assertEqual(report['deferred_by_run_cap'],1)
            self.assertEqual(w.queue.status()['versions'],2)
        with world() as w:
            for label in ('a','b','c'): w.item('/documents/'+label)
            report=w.fetch(limits=engine.Limits(max_items=1))
            self.assertEqual(report['attempted'],1)
            self.assertEqual(len(w.fixture.calls),1)
            self.assertEqual(w.queue.status()['versions'],1)
        with world() as w:
            w.item(); w.item('/documents/second')
            report=w.fetch(limits=engine.Limits(max_bytes=len(BODY),max_total_bytes=len(BODY)+3))
            self.assertLessEqual(report['body_bytes_received'],len(BODY)+3)
            self.assertEqual(w.queue.status()['versions'],1)

    def test_11_404_accounting_persists(self):
        with world() as w:
            w.fixture.routes['/documents/gone']={'status':404,'body':b''}
            key=w.item('/documents/gone'); w.fetch()
            self.assertEqual(w.item_state(key)['state'],'blocked')
            self.assertEqual(run_stages.fetch_blocked(w.queue,w.queue.status()['items']),(1,1))
            before=len(w.fixture.calls); w.fetch()
            self.assertEqual(len(w.fixture.calls),before)
            self.assertEqual(run_stages.fetch_blocked(w.queue,w.queue.status()['items']),(1,1))
            with mock.patch.object(run_stages,'iter_notices',return_value=iter(())), mock.patch.object(policy,'load_approval',side_effect=lambda path:approval()):
                args=run_stages.parser().parse_args(['portal','--project',str(w.root),'--approval-file','synthetic'])
                result,exit_code=run_stages.stage_portal(args)
            self.assertEqual(exit_code,0)
            self.assertEqual(result['error_code'],'PORTAL_HTTP_404')
            self.assertIn({'code':'PORTAL_HTTP_404','severity':'action','count':1},result['needs_g'])

    def test_12_429_and_all_5xx_backoff_exhaustion(self):
        for status in (429,500,501,502,503,504,599):
            with self.subTest(status=status), world() as w:
                w.fixture.routes['/documents/retry']={'status':status,'body':b'','retry_after':'60'}
                key=w.item('/documents/retry')
                limits=engine.Limits(max_attempts=2,backoff=2)
                before=time.time(); w.fetch(limits=limits)
                item=w.item_state(key)
                self.assertEqual(item['state'],'retry')
                self.assertGreaterEqual(item['next_attempt'],before+60)
                calls=len(w.fixture.calls); w.fetch(limits=limits)
                self.assertEqual(len(w.fixture.calls),calls)
                with w.queue.db:
                    w.queue.db.execute('UPDATE portal_items SET next_attempt=0 WHERE id=?',(key,))
                w.fetch(limits=limits)
                self.assertEqual(w.item_state(key)['state'],'blocked')
                self.assertEqual(w.item_state(key)['tries'],2)

    def test_13_header_body_and_whole_run_timeout(self):
        for delay in ('header_delay','body_delay','piece_delay'):
            with self.subTest(phase=delay), world() as w:
                w.fixture.routes['/documents/slow']={delay:0.3 if delay!='piece_delay' else 0.02}
                key=w.item('/documents/slow')
                start=time.monotonic(); w.fetch(limits=engine.Limits(timeout=0.1))
                elapsed=time.monotonic()-start
                self.assertLess(elapsed,0.8)
                self.assertEqual(w.item_state(key)['state'],'retry')
                self.assertEqual(w.item_state(key)['reason'],'timeout')
        with world() as w:
            for label in ('a','b','c'):
                w.item('/documents/'+label)
                w.fixture.routes['/documents/'+label]={'body_delay':0.3}
            start=time.monotonic()
            result=w.fetch(limits=engine.Limits(run_timeout=0.1,timeout=1))
            self.assertLess(time.monotonic()-start,0.8)
            self.assertEqual(result['attempted'],1)
            self.assertEqual(result['deferred_by_run_cap'],2)

    def test_14_login_malformed_lengths_and_integrity(self):
        cases = [({'type':'text/html','body':b'<html>login</html>'},'login_page'),
                 ({'type':'application/pdf','body':b'not a pdf'},'signature_mismatch'),
                 ({'duplicate_length':True},'ambiguous_http_headers'),
                 ({'declared':'invalid'},'size_limit'),
                 ({'body':b''},'empty_document')]
        for route,reason in cases:
            with self.subTest(reason=reason), world() as w:
                w.fixture.routes['/documents/bad']=route
                key=w.item('/documents/bad'); w.fetch()
                self.assertEqual(w.item_state(key)['reason'],reason)
                self.assertEqual(w.queue.status()['versions'],0)

    def test_15_interrupted_after_object_recovery_and_exhaustion(self):
        class PowerLoss(BaseException): pass
        with world() as w:
            key=w.item()
            def crash(phase):
                if phase=='after_object': raise PowerLoss()
            with self.assertRaises(PowerLoss): w.fetch(fault=crash)
            self.assertEqual(w.item_state(key)['state'],'fetching')
            self.assertEqual(w.queue.status()['versions'],0)
            self.assertEqual(len(list(w.queue.objects.iterdir())),1)
            w.queue.close(); w.queue=Queue(w.root/'portal-queue-v1')
            w.fetch()
            self.assertEqual(w.item_state(key)['state'],'received')
            self.assertEqual(w.canonical_counts(),(1,1,1,1,7))
            self.assertEqual(w.queue.status()['versions'],1)
            self.assertEqual(w.queue.db.execute("SELECT count(*) FROM portal_attempts WHERE result='interrupted'").fetchone()[0],1)
        with world() as w:
            key=w.item()
            with self.assertRaises(PowerLoss): w.fetch(fault=crash,limits=engine.Limits(max_attempts=1))
            before=len(w.fixture.calls); w.fetch(limits=engine.Limits(max_attempts=1))
            self.assertEqual(len(w.fixture.calls),before)
            self.assertEqual(w.item_state(key)['reason'],'retry_exhausted')

    def test_16_commit_before_ack_replays_without_network(self):
        class PowerLoss(BaseException): pass
        original=WP1LedgerAdapter.record_original
        def crash(adapter,*args):
            original(adapter,*args)
            raise PowerLoss()
        with world() as w:
            key=w.item()
            with mock.patch.object(WP1LedgerAdapter,'record_original',crash):
                with self.assertRaises(PowerLoss): w.fetch()
            before=w.canonical_counts(); calls=len(w.fixture.calls)
            self.assertEqual(w.queue.status()['ledger_pending'],1)
            again=w.fetch()
            self.assertEqual(again['attempted'],0)
            self.assertEqual(again['ledger_delivered'],1)
            self.assertEqual(w.canonical_counts(),before)
            self.assertEqual(len(w.fixture.calls),calls)
            self.assertEqual(w.queue.status()['ledger_pending'],0)

    def test_17_delivery_failure_keeps_outbox_and_peer_progress(self):
        original=WP1LedgerAdapter.record_original
        with world() as w:
            first=w.item('/documents/a'); w.item('/documents/b')
            def fail_one(adapter,receipt,path):
                if receipt['item']==first: raise policy.PortalError('synthetic_delivery_failed')
                return original(adapter,receipt,path)
            with mock.patch.object(WP1LedgerAdapter,'record_original',fail_one):
                report=w.fetch()
            self.assertEqual(report['ledger_delivered'],1)
            self.assertEqual(report['ledger_pending'],1)
            self.assertEqual(w.queue.status()['ledger_failures'][0]['reason'],'synthetic_delivery_failed')
            calls=len(w.fixture.calls); report=w.fetch()
            self.assertEqual(report['ledger_delivered'],1)
            self.assertEqual(len(w.fixture.calls),calls)
            self.assertEqual(w.canonical_counts(),(1,2,2,2,7))

    def test_18_locks_serial_bound_and_unchanged_original(self):
        with world() as w:
            w.item()
            with processing_lock(w.processing):
                with self.assertRaisesRegex(policy.PortalError,'processing_busy'): w.fetch()
            self.assertEqual(w.fixture.calls,[])
            other=Queue(w.root/'portal-queue-v1')
            try:
                with w.queue.lock():
                    with self.assertRaisesRegex(policy.PortalError,'queue_busy'):
                        run_download(other,w.processing,apply=True,approval_loader=approval,
                                     egress_loader=egress,transport=w.fixture.transport())
            finally: other.close()
            self.assertEqual(w.fixture.calls,[])
            w.fetch()
            self.assertEqual(w.fixture.max_active,1)
            blob=w.processing/'intake'/'blobs'/digest_bytes(BODY)
            before=blob.read_bytes(); os.chmod(blob,0o600)
            with self.assertRaisesRegex(policy.PortalError,'canonical_cas_invalid'):
                verify_cas(blob,digest_bytes(BODY),len(BODY))
            self.assertEqual(blob.read_bytes(),before)

    def test_19_malformed_queue_preflight_and_missing_provenance(self):
        for variant in ('unknown_state','missing_source','null_url','bad_current','bad_generation'):
            with self.subTest(variant=variant), world() as w:
                key=w.item()
                with w.queue.db:
                    if variant=='unknown_state':
                        w.queue.db.execute("UPDATE portal_items SET state='mystery'")
                    elif variant=='missing_source':
                        w.queue.db.execute('DELETE FROM portal_notices')
                    elif variant=='null_url':
                        w.queue.db.execute("UPDATE portal_items SET last_url_private='null'")
                    elif variant=='bad_current':
                        w.queue.db.execute("UPDATE portal_items SET current_sha256='not-a-hash'")
                    else:
                        w.queue.db.execute('UPDATE portal_items SET generation=0')
                with self.assertRaises(policy.PortalError): w.fetch()
                self.assertEqual(w.fixture.calls,[])
                self.assertEqual(w.queue.status()['attempts'],0)

    def test_20_egress_independent_and_symlinks(self):
        with world() as w:
            w.item()
            def denied(): return EgressApproval([REDIRECT],NOW+timedelta(days=2))
            result=w.fetch(egress_loader=denied)
            self.assertEqual(result['blocked'],1)
            self.assertEqual(w.fixture.calls,[])
            self.assertEqual(w.queue.status()['items'][0]['reason'],'egress_denied')
            link=w.root/'alias'; link.symlink_to(w.processing,target_is_directory=True)
            with self.assertRaisesRegex(policy.PortalError,'symlink_path'):
                run_download(w.queue,link,apply=True,approval_loader=approval,egress_loader=egress,
                             transport=w.fixture.transport())
            with self.assertRaises(policy.PortalError):
                policy.load_approval(link/'nonexistent')
            with self.assertRaises(policy.PortalError):
                load_egress(link/'nonexistent')
        for values in (['127.0.0.1'],['93.184.216.34','10.0.0.1'],['::ffff:127.0.0.1'],['169.254.1.1']):
            with self.assertRaisesRegex(policy.PortalError,'ssrf_address_denied'): public_addresses(values)

    def test_21_stage_opt_in_and_invalid_limits(self):
        for switches in (['--portal-download'],['--portal-download','--portal-egress-file','x','--portal-max-items','0'],
                         ['--portal-download','--portal-egress-file','x','--portal-request-timeout','nan']):
            args=run_stages.parser().parse_args(['portal','--project','synthetic','--approval-file','synthetic']+switches)
            self.assertTrue(run_stages.config_invalid(args))
        args=run_stages.parser().parse_args(['portal','--project','synthetic','--approval-file','synthetic'])
        self.assertFalse(run_stages.config_invalid(args)); self.assertFalse(args.portal_download)
        with world() as w:
            w.item(); state=w.queue.status()
            self.assertEqual(run_download(w.queue,w.processing),{'mode':'inventory_only',**state})
            self.assertEqual(w.fixture.calls,[])
            self.assertEqual(w.queue.status(),state)
        for kwargs in ({'max_total_bytes':0},{'max_total_bytes':True},{'run_timeout':float('inf')},
                       {'timeout':float('nan')},{'max_items':1001},{'backoff':-1}):
            with self.assertRaises(ValueError): engine.Limits(**kwargs)

    def test_22_negative_controls_initial_and_redirect_gates(self):
        def disabled(self,url,origin,now): return urlsplit(url).hostname
        for initial in (True,False):
            with world() as w:
                if initial:
                    key=w.item(host='denied.example.net')
                else:
                    w.fixture.routes['/documents/redir']={'status':302,'location':'https://denied.example.net/documents/other'}
                    key=w.item('/documents/redir')
                # All unrelated inputs/gates remain valid; independent egress is
                # deliberately inclusive to isolate allowlist authorization.
                broad=lambda:EgressApproval([HOST,REDIRECT,'denied.example.net'],NOW+timedelta(days=2))
                transport=w.fixture.transport(egress_loader=broad)
                with mock.patch.object(policy.Approval,'authorize',disabled):
                    w.fetch(transport=transport,egress_loader=broad)
                with self.assertRaises(AssertionError):
                    self.assertEqual(w.item_state(key)['state'],'blocked')
                self.assertGreater(len(w.fixture.calls),0)
                NEGATIVE_CONTROLS.append('allowlist_initial' if initial else 'allowlist_redirect')

    def test_23_negative_control_duplicate_key_parser(self):
        raw=approval_raw().replace('"enabled": true','"enabled": false, "enabled": true')
        with mock.patch.object(policy,'strict_json',json.loads):
            # Disabling the exact parser gate admits this otherwise-valid input.
            parsed=policy.parse_approval(raw,uid=0,mode=stat.S_IFREG|0o600,now=NOW)
            self.assertIsInstance(parsed,policy.Approval)
            with self.assertRaises(AssertionError):
                with self.assertRaises(policy.PortalError):
                    policy.parse_approval(raw,uid=0,mode=stat.S_IFREG|0o600,now=NOW)
        NEGATIVE_CONTROLS.append('duplicate_json_key')

    def test_24_negative_control_shared_total_cap(self):
        original=engine._download
        def unlimited(*args,**kwargs):
            kwargs['budget']['remaining']=10000
            return original(*args,**kwargs)
        with world() as w:
            w.item('/documents/a'); w.item('/documents/b')
            with mock.patch.object(engine,'_download',unlimited):
                report=w.fetch(limits=engine.Limits(max_bytes=len(BODY),max_total_bytes=len(BODY)))
            with self.assertRaises(AssertionError):
                self.assertLessEqual(report['body_bytes_received'],len(BODY))
        NEGATIVE_CONTROLS.append('run_total_bytes')

    def test_25_full_queue_synthetic_126_and_rerun(self):
        with world() as w:
            for number in range(126): w.item('/documents/f'+str(number))
            report=w.fetch(limits=engine.Limits(max_items=126))
            self.assertEqual(report['attempted'],126)
            self.assertEqual(report['ledger_delivered'],126)
            self.assertEqual(w.canonical_counts(),(1,126,126,126,7))
            self.assertEqual(w.fixture.max_active,1)
            before=len(w.fixture.calls); again=w.fetch(limits=engine.Limits(max_items=126))
            self.assertEqual(again['attempted'],0)
            self.assertEqual(len(w.fixture.calls),before)

    def test_26_actual_stage_notice_to_canonical_composition(self):
        from campaign_tool.records.portal import download
        from campaign_tool.records import run_coverage
        with world() as w:
            mail=w.root/'mail-history'; mail.mkdir(mode=0o700)
            (mail/'receipts').mkdir(mode=0o700)
            raw=('From: fixture@example.invalid\r\nTo: records@example.invalid\r\n'
                 'Content-Type: text/plain; charset=utf-8\r\n\r\n'
                 'https://'+HOST+'/documents/composed\r\n').encode()
            (mail/'notice.eml').write_bytes(raw)
            (mail/'receipts'/'notice.json').write_text(json.dumps({'original_eml':{
                'path':'notice.eml','sha256':digest_bytes(raw)}}))
            args=run_stages.parser().parse_args(['portal','--project',str(w.root),
                '--approval-file','synthetic','--portal-download','--portal-egress-file','synthetic',
                '--portal-max-items','126'])
            def make_transport(**kwargs): return w.fixture.transport()
            with mock.patch.object(policy,'load_approval',side_effect=lambda path:approval()), \
                 mock.patch.object(download,'load_approval',side_effect=lambda path:approval()), \
                 mock.patch.object(download,'load_egress',side_effect=lambda path:egress()), \
                 mock.patch.object(download,'StdlibHTTPS',side_effect=make_transport):
                line,exit_code=run_stages.stage_portal(args)
                self.assertEqual(exit_code,0)
                self.assertEqual(line['counts']['output'],1)
                calls=len(w.fixture.calls)
                again,exit_code=run_stages.stage_portal(args)
                self.assertEqual(again['counts']['output'],0)
                self.assertEqual(len(w.fixture.calls),calls)
                (mail/'receipts'/'bad.json').write_text('null')
                line,exit_code=run_stages.stage_portal(args)
                self.assertEqual(exit_code,1)
                self.assertEqual(line['error_code'],'PORTAL_NOTICE_UNREADABLE')
                self.assertEqual(len(w.fixture.calls),calls)
            for code in ('PORTAL_DOWNLOAD_PENDING','PORTAL_LEDGER_PENDING',
                         'PORTAL_FETCH_DRY_RUN_ONLY','PORTAL_HTTP_404','PORTAL_FETCH_BLOCKED'):
                report={'mode':'test','stages':[{'name':'portal','status':'warn'}],
                        'needs_g':[{'stage':'portal','code':code,'count':1}]}
                self.assertEqual(run_coverage.coverage_counts(report)['portal_held'],1)
            self.assertEqual(w.canonical_counts(),(1,1,1,1,7))

    def test_27_revocation_on_redirect_no_target_request(self):
        with world() as w:
            w.fixture.routes['/documents/redir']={'status':302,'location':'https://'+REDIRECT+'/synthetic/file'}
            key=w.item('/documents/redir')
            def current():
                if w.fixture.calls:
                    return policy.parse_approval(approval_raw(redirect_hosts=[]),uid=0,mode=stat.S_IFREG|0o600,now=NOW)
                return approval()
            report=w.fetch(approval_loader=current,transport=w.fixture.transport(approval_loader=current))
            self.assertEqual(len(w.fixture.calls),1)
            self.assertEqual(report['blocked'],1)
            self.assertEqual(w.item_state(key)['reason'],'redirect_not_approved')

    def test_28_negative_controls_immutability_and_deadline(self):
        from campaign_tool.records.portal import ledger_adapter
        with world() as w:
            w.item(); w.fetch()
            blob=w.processing/'intake'/'blobs'/digest_bytes(BODY)
            os.chmod(blob,0o600)
            with mock.patch.object(ledger_adapter,'verify_cas',return_value=None):
                with self.assertRaises(AssertionError):
                    with self.assertRaises(policy.PortalError):
                        ledger_adapter.verify_cas(blob,digest_bytes(BODY),len(BODY))
            with self.assertRaisesRegex(policy.PortalError,'canonical_cas_invalid'):
                ledger_adapter.verify_cas(blob,digest_bytes(BODY),len(BODY))
        NEGATIVE_CONTROLS.append('immutable_mode')
        original=engine._download
        def unlimited_time(*args,**kwargs):
            kwargs['budget']['deadline']=time.monotonic()+100
            return original(*args,**kwargs)
        with world() as w:
            for name in ('a','b'):
                w.item('/documents/'+name)
                w.fixture.routes['/documents/'+name]={'body_delay':0.05}
            with mock.patch.object(engine,'_download',unlimited_time):
                report=w.fetch(limits=engine.Limits(run_timeout=0.02,timeout=1))
            with self.assertRaises(AssertionError): self.assertEqual(report['attempted'],1)
        NEGATIVE_CONTROLS.append('run_deadline')

    def test_29_empty_queue_and_egress_missing_policy(self):
        with world() as w:
            result=w.fetch()
            self.assertEqual(result['attempted'],0)
            self.assertEqual(w.fixture.calls,[])
            with self.assertRaisesRegex(policy.PortalError,'download_policies_required'):
                run_download(w.queue,w.processing,apply=True,approval_loader=approval)
            self.assertEqual(w.canonical_counts(),(0,0,0,0,0))

    def test_30_baseline_compatible_positive_dry_run_and_404(self):
        # Frozen baseline oracle, measured against 080f891 in original
        # acceptance. Do not vendor an old production engine into repository CI.
        summaries=[]
        for implementation in (engine,):
            with world() as w:
                key=w.item()
                before=w.queue.status()
                self.assertEqual(implementation.fetch_queue(w.queue),{'mode':'inventory_only',**before})
                implementation.fetch_queue(w.queue,apply=True,approval_loader=approval,
                    egress=lambda url:True,transport=w.fixture.transport(),limits=implementation.Limits())
                gone=w.item('/documents/baseline-gone')
                w.fixture.routes['/documents/baseline-gone']={'status':404,'body':b''}
                implementation.fetch_queue(w.queue,apply=True,approval_loader=approval,
                    egress=lambda url:True,transport=w.fixture.transport(),limits=implementation.Limits())
                self.assertEqual(w.item_state(gone)['state'],'blocked')
                self.assertEqual(run_stages.fetch_blocked(w.queue,w.queue.status()['items']),(1,1))
                summaries.append((w.item_state(key)['state'],w.queue.status()['versions'],
                                  w.queue.status()['ledger_pending'],len(w.fixture.calls)))
        self.assertEqual(summaries, [('received',1,1,2)])
        # Preserve original dataclass positional ordering as well as defaults.
        self.assertEqual(engine.Limits(123,7).max_items,7)

    def test_31_plan_delta_preserves_other_stages(self):
        from tests.portal_fetch_plan_helpers import portal_plan, APPROVAL, EGRESS
        base=json.loads((FIXTURES/'plan.json').read_text())
        before=json.dumps(base,sort_keys=True)
        disabled=portal_plan(base)
        self.assertEqual(disabled['stages'],base['stages'])
        self.assertEqual(disabled['portal']['fetch'],{'apply':False,'max_items':20,'max_bytes_total':524288000})
        changed=portal_plan(base,release_path='/synthetic/portal-fixture/engine-portal-'+'a'*64,apply=True,max_items=126)
        self.assertEqual(json.dumps(base,sort_keys=True),before)
        for old,new in zip(base['stages'],changed['stages']):
            if old['name']!='portal': self.assertEqual(old,new)
            else:
                self.assertEqual(new['timeout_seconds'],1200)
                self.assertEqual(new['on_fail'],'stop')
                for flag,value in (('--approval-file',APPROVAL),('--portal-egress-file',EGRESS),
                                   ('--timeout','1190')):
                    self.assertEqual(new['argv'][new['argv'].index(flag)+1],value)
                self.assertNotIn('--portal-download',new['argv'])
                self.assertEqual(changed['portal']['fetch']['max_items'],126)
        with self.assertRaisesRegex(ValueError,'reviewed_release_required'):
            portal_plan(base,release_path='/not-reviewed',apply=True)
        base['stages'].append(base['stages'][3])
        with self.assertRaisesRegex(ValueError,'portal_stage_identity'):
            portal_plan(base,release_path='/synthetic/portal-fixture/engine-portal-'+'a'*64)

    def test_32_run_budget_defers_then_recovers_declared_and_streamed(self):
        for omit in (False,True):
            with self.subTest(no_length=omit), world() as w:
                keys=[w.item('/documents/'+name) for name in ('a','b','c')]
                for name in ('a','b','c'):
                    w.fixture.routes['/documents/'+name]={'omit_length':omit}
                report=w.fetch(limits=engine.Limits(max_bytes=len(BODY),max_total_bytes=len(BODY)+3,max_attempts=1))
                self.assertEqual(report['received'],1)
                self.assertEqual(report['blocked'],0)
                self.assertEqual(report['deferred_by_run_cap'],2)
                self.assertLessEqual(report['body_bytes_received'],len(BODY)+3)
                deferred=[w.item_state(key) for key in keys if w.item_state(key)['reason']=='total_size_limit']
                self.assertEqual(len(deferred),1)
                self.assertEqual(deferred[0]['state'],'retry')
                self.assertEqual(deferred[0]['tries'],0)
                self.assertEqual(deferred[0]['next_attempt'],0)
                self.assertEqual(report['receipts'][-1]['status'],'deferred_by_run_cap')
                self.assertEqual(report['receipts'][-1]['reason'],'total_size_limit')
                w.queue.close(); w.queue=Queue(w.root/'portal-queue-v1')
                again=w.fetch(limits=engine.Limits(max_bytes=len(BODY),max_total_bytes=len(BODY)*3,max_attempts=1))
                self.assertEqual(again['received'],2)
                self.assertTrue(all(w.item_state(k)['state']=='received' for k in keys))
                self.assertEqual(w.queue.status()['versions'],3)
                calls=len(w.fixture.calls); final=w.fetch()
                self.assertEqual(final['attempted'],0)
                self.assertEqual(len(w.fixture.calls),calls)

    def test_33_exact_cap_and_overflow_boundary_matrix(self):
        for omit in (False,True):
            for extra in (0,1):
                with self.subTest(no_length=omit,extra=extra), world() as w:
                    w.fixture.routes['/documents/boundary']={'body':BODY+b'x'*extra,'omit_length':omit}
                    key=w.item('/documents/boundary')
                    report=w.fetch(limits=engine.Limits(max_bytes=len(BODY),max_total_bytes=len(BODY)*2))
                    item=w.item_state(key)
                    self.assertEqual(item['state'],'received' if extra==0 else 'blocked')
                    self.assertEqual(item['reason'],None if extra==0 else 'size_limit')
                    self.assertLessEqual(report['body_bytes_received'],len(BODY))
                    self.assertEqual(w.queue.status()['versions'],1 if extra==0 else 0)
        with world() as w:
            w.fixture.routes['/documents/exactrun']={'omit_length':True}
            key=w.item('/documents/exactrun')
            report=w.fetch(limits=engine.Limits(max_bytes=len(BODY),max_total_bytes=len(BODY)))
            self.assertEqual(w.item_state(key)['state'],'received')
            self.assertEqual(report['body_bytes_received'],len(BODY))

    def test_34_negative_control_budget_terminalization(self):
        original=engine._download
        def after_progress(*args,**kwargs):
            # Synthetic fixture: a valid run has already spent one BODY. This
            # isolates the original terminalization gate, not invalid config.
            kwargs['budget']['remaining']=3
            kwargs['budget']['received']=len(BODY)
            return original(*args,**kwargs)
        def terminal(*args,**kwargs):
            try: return after_progress(*args,**kwargs)
            except policy.PortalError as exc:
                if str(exc)=='total_size_limit': raise policy.PortalError('size_limit') from None
                raise
        for omit in (False,True):
            with world() as w:
                w.fixture.routes['/documents/a']={'omit_length':omit}
                w.item('/documents/a')
                with mock.patch.object(engine,'_download',terminal):
                    report=w.fetch(limits=engine.Limits(max_bytes=len(BODY),max_total_bytes=len(BODY)+3))
                with self.assertRaises(AssertionError): self.assertEqual(report['blocked'],0)
                self.assertEqual(w.queue.status()['items'][0]['reason'],'size_limit')
            with world() as w:
                w.fixture.routes['/documents/a']={'omit_length':omit}
                key=w.item('/documents/a')
                with mock.patch.object(engine,'_download',after_progress):
                    report=w.fetch(limits=engine.Limits(max_bytes=len(BODY),max_total_bytes=len(BODY)+3))
                self.assertEqual(report['blocked'],0)
                self.assertEqual(w.item_state(key)['reason'],'total_size_limit')
                self.assertEqual(w.item_state(key)['tries'],0)
                self.assertEqual(len(w.fixture.calls),1)
        NEGATIVE_CONTROLS.append('budget_mapped_to_terminal_block')

    def test_35_negative_control_exact_cap_eof(self):
        original=StdlibHTTPS.open
        def no_probe(transport,*args,**kwargs):
            response=original(transport,*args,**kwargs)
            def chunks():
                count=0
                for chunk in response.chunks:
                    count+=len(chunk)
                    yield chunk
                    if count==transport.max_bytes:
                        raise policy.PortalError('size_limit')
            return engine.Response(response.status,response.headers,chunks(),response.close)
        with world() as w:
            w.fixture.routes['/documents/exact']={'omit_length':True}
            key=w.item('/documents/exact')
            with mock.patch.object(StdlibHTTPS,'open',no_probe):
                w.fetch(limits=engine.Limits(max_bytes=len(BODY)))
            with self.assertRaises(AssertionError): self.assertEqual(w.item_state(key)['state'],'received')
            self.assertEqual(w.item_state(key)['reason'],'size_limit')
        NEGATIVE_CONTROLS.append('exact_cap_eof')

    def test_36_plan_defaults_strict_types_missing_null_and_bounds(self):
        self.assertEqual(policy.fetch_settings({}),policy.FetchSettings(False,20,524288000))
        self.assertEqual(policy.fetch_settings({'portal':{'fetch':{}}}),policy.FetchSettings())
        for value in (None,[],True,{'apply':None},{'apply':1},{'apply':'false'},
                      {'max_items':None},{'max_items':True},{'max_items':0},{'max_items':1001},
                      {'max_bytes_total':True},{'max_bytes_total':0},{'max_bytes_total':1073741825},
                      {'max_bytes_total':1.5},{'extra':1}):
            with self.subTest(value=value):
                with self.assertRaisesRegex(policy.PortalError,'fetch_plan_invalid'):
                    policy.fetch_settings({'portal':{'fetch':value}})
        for portal in (None,[],{'unknown':1}):
            with self.assertRaises(policy.PortalError): policy.fetch_settings({'portal':portal})
        for n in (1,1000):
            self.assertEqual(policy.fetch_settings({'portal':{'fetch':{'max_items':n}}}).max_items,n)

    def test_37_actual_plan_loader_bridge_and_fail_closed_parsing(self):
        from copy import deepcopy
        from tests.portal_fetch_plan_helpers import portal_plan
        module=orchestrator()
        base=json.loads((FIXTURES/'plan.json').read_text())
        for plan in (base,portal_plan(base)):
            module.validate_target_config(plan)
            for entry,old in zip(plan['stages'],base['stages']):
                self.assertEqual(module.stage_argv(plan,entry),old['argv'])
        enabled=portal_plan(base,apply=True,max_items=126,release_path='/synthetic/portal-fixture/engine-portal-'+'a'*64)
        module.validate_target_config(enabled)
        entry=enabled['stages'][3]
        argv=module.stage_argv(enabled,entry)
        self.assertEqual(argv[-5:],['--portal-download','--portal-max-items','126','--portal-total-bytes','524288000'])
        with world() as w:
            path=w.root/'plan.json'
            path.write_text(json.dumps(enabled))
            self.assertEqual(module.load_target_config(path),enabled)
            raw=json.dumps(enabled)
            for bad in ('null','{',raw.replace('"apply": true','"apply": false, "apply": true'),
                        raw.replace('524288000','NaN'),raw.replace('"apply": true','"apply": null')):
                path.write_text(bad)
                with self.assertRaises(module.TargetConfigError): module.load_target_config(path)
            self.assertEqual(w.fixture.calls,[])
        for flag in ('--portal-download','--portal-download=true','--portal-max-items','--portal-total-bytes=1',
                     '--portal-down','--portal-max-i=100','--portal-total=100'):
            bad=deepcopy(base); bad['stages'][3]['argv'].append(flag)
            with self.assertRaisesRegex(module.TargetConfigError,'must come from plan'):
                module.validate_target_config(bad)
        bad=deepcopy(enabled); bad['stages'][3]['argv'].remove('--portal-egress-file')
        with self.assertRaises(module.TargetConfigError): module.validate_target_config(bad)

    def test_38_baseline_and_false_plan_stdout_byte_identical(self):
        import importlib
        from contextlib import redirect_stdout
        module=orchestrator()
        base=json.loads((FIXTURES/'plan.json').read_text())
        results=[]
        for impl,pol,plan in ((run_stages,policy,base),
                              (run_stages,policy,{**base,'portal':{'fetch':{'apply':False,'max_items':20,'max_bytes_total':524288000}}})):
            with world() as w:
                w.item()
                oldstate=w.queue.status()
                argv=module.stage_argv(plan,plan['stages'][3])
                self.assertNotIn('--portal-download',argv)
                stream=io.StringIO()
                with mock.patch.object(pol,'load_approval',side_effect=lambda path:approval()),redirect_stdout(stream):
                    code=impl.main(['portal','--project',str(w.root),'--approval-file','synthetic'])
                self.assertEqual(code,0)
                self.assertEqual(w.queue.status(),oldstate)
                self.assertEqual(w.fixture.calls,[])
                results.append(stream.getvalue().encode())
        self.assertEqual(results[0],results[1])
        self.assertEqual(results[0], (FIXTURES/'baseline-dry-stdout.txt').read_bytes())

    def test_39_plan_execute_to_existing_stage_engine_and_receipts(self):
        from tests.portal_fetch_plan_helpers import portal_plan
        from campaign_tool.records.portal import download
        module=orchestrator()
        base=json.loads((FIXTURES/'plan.json').read_text())
        plan=portal_plan(base,apply=True,max_items=1,max_bytes_total=policy.FETCH_MIN_TOTAL_BYTES,
                         release_path='/synthetic/portal-fixture/engine-portal-'+'a'*64)
        for entry in plan['stages']:
            if entry['name']!='portal': entry['enabled']=False
        with world() as w:
            w.item('/documents/a'); w.item('/documents/b')
            stage=plan['stages'][3]
            stage['argv'][stage['argv'].index('--project')+1]=str(w.root)
            module.validate_target_config(plan)
            seen=[]
            original=download.run_download
            def capture(*args,**kwargs):
                outcome=original(*args,**kwargs); seen.append(outcome); return outcome
            def child(argv,*args):
                index=argv.index('campaign_tool.records.run_stages')
                parsed=run_stages.parser().parse_args(argv[index+1:])
                self.assertFalse(run_stages.config_invalid(parsed))
                line,code=run_stages.stage_portal(parsed)
                self.assertEqual(code,0)
                # Validate actual producer output with the real stage schema;
                # only the subprocess boundary is replaced by this callback.
                self.assertEqual(module.parse_stage_result(json.dumps(line).encode(), args[1]), line)
                return line['status'],line['counts'],line.get('error_code'),line,0.0
            with mock.patch.object(module,'run_stage',side_effect=child), \
                 mock.patch.object(policy,'load_approval',side_effect=lambda path:approval()), \
                 mock.patch.object(download,'load_approval',side_effect=lambda path:approval()), \
                 mock.patch.object(download,'load_egress',side_effect=lambda path:egress()), \
                 mock.patch.object(download,'StdlibHTTPS',side_effect=lambda **kw:w.fixture.transport()), \
                 mock.patch.object(download,'run_download',side_effect=capture):
                code=module.execute(plan,'run_20261024T040000Z_12345678','manual',module.utc_now(),
                    time.time()+100,w.root/'report.json',str(w.root))
            self.assertEqual(code,0)
            self.assertEqual(seen[0]['attempted'],1)
            module.run_report.validate_run_report(json.loads((w.root/'report.json').read_text()))
            receipt=seen[0]['receipts'][0]
            self.assertEqual(receipt['sha256'],digest_bytes(BODY))
            self.assertEqual(receipt['host'],HOST); self.assertEqual(receipt['bytes'],len(BODY))
            self.assertEqual(receipt['http_status'],200); self.assertEqual(receipt['status'],'received')
            self.assertEqual(w.queue.status()['versions'],1)
            persisted=dict(w.queue.db.execute('SELECT * FROM portal_versions').fetchone())
            self.assertEqual(persisted['sha256'],receipt['sha256'])
            self.assertEqual(persisted['byte_count'],receipt['bytes'])
            self.assertEqual(w.canonical_counts(),(1,1,1,1,7))

    def test_40_negative_control_false_plan_gate(self):
        module=orchestrator()
        base=json.loads((FIXTURES/'plan.json').read_text())
        original=policy.fetch_settings
        def wrong(plan): return replace(original(plan),apply=True)
        entry=dict(base['stages'][3]); entry['argv']=entry['argv']+['--portal-egress-file','synthetic']
        with mock.patch.object(policy,'fetch_settings',wrong):
            argv=module.stage_argv(base,entry)
        with self.assertRaises(AssertionError): self.assertNotIn('--portal-download',argv)
        NEGATIVE_CONTROLS.append('false_plan_apply_gate')

    def test_41_reject_incompatible_caps_at_all_admitted_entrypoints(self):
        from contextlib import redirect_stdout
        from copy import deepcopy
        from campaign_tool.records.portal import download
        from tests.portal_fetch_plan_helpers import portal_plan
        module=orchestrator()
        base=json.loads((FIXTURES/'plan.json').read_text())
        minimum=25*1024*1024
        self.assertEqual(policy.FETCH_MIN_TOTAL_BYTES,minimum)
        with world() as w:
            w.item()
            for cap in (1,len(BODY),minimum,100*1024*1024):
                if cap>1:
                    with self.assertRaisesRegex(ValueError,'^invalid_limit$'):
                        engine.Limits(max_bytes=cap,max_total_bytes=cap-1)
                self.assertEqual(engine.Limits(max_bytes=cap,max_total_bytes=cap).max_bytes,cap)
            for apply in (False,True):
                bad={'portal':{'fetch':{'apply':apply,'max_bytes_total':minimum-1}}}
                with self.assertRaisesRegex(policy.PortalError,'^fetch_plan_invalid$'):
                    policy.fetch_settings(bad)
                self.assertEqual(policy.fetch_settings({'portal':{'fetch':{
                    'apply':apply,'max_bytes_total':minimum}}}).max_bytes_total,minimum)
                with self.assertRaisesRegex(policy.PortalError,'^fetch_plan_invalid$'):
                    portal_plan(base,apply=apply,max_bytes_total=minimum-1,
                                release_path='/synthetic/portal-fixture/engine-portal-'+'a'*64)
                plan=deepcopy(base); plan.update(bad)
                with self.assertRaises(module.TargetConfigError): module.validate_target_config(plan)
                with self.assertRaisesRegex(policy.PortalError,'^fetch_plan_invalid$'):
                    module.stage_argv(plan,plan['stages'][3])
                path=w.root/'bad-plan.json'; path.write_text(json.dumps(plan))
                with self.assertRaises(module.TargetConfigError): module.load_target_config(path)
            switches=['portal','--project',str(w.root),'--approval-file','synthetic',
                      '--portal-download','--portal-egress-file','synthetic',
                      '--portal-max-bytes',str(len(BODY)),'--portal-total-bytes',str(len(BODY)-1)]
            stdout=io.StringIO()
            with mock.patch.object(download,'run_download',wraps=download.run_download) as dispatch, \
                 mock.patch.object(download,'StdlibHTTPS',wraps=StdlibHTTPS) as transport, \
                 redirect_stdout(stdout):
                code=run_stages.main(switches)
                self.assertEqual(code,1)
                self.assertEqual(json.loads(stdout.getvalue())['error_code'],'STAGE_CONFIG_INVALID')
                # download.py is a stage adapter, not a second CLI. Exercise its
                # real from_stage entry with the exact same parsed CLI args.
                args=run_stages.parser().parse_args(switches)
                with self.assertRaisesRegex(ValueError,'^invalid_limit$'):
                    download.from_stage(w.queue,args)
                dispatch.assert_not_called(); transport.assert_not_called()
            self.assertEqual(w.fixture.calls,[])
            self.assertEqual(w.queue.status()['attempts'],0)
            self.assertEqual(w.canonical_counts(),(0,0,0,0,0))
            PROGRESS_EVIDENCE.append({'test':41,'requests':0,'attempts':0,
                                      'stage_exit':code,'adapter':'invalid_limit'})

    def _ordered_payloads(self,w,sizes,omit):
        keys=sorted(w.item('/documents/progress'+str(i)) for i in range(len(sizes)))
        for key,size in zip(keys,sizes):
            self.assertGreaterEqual(size,len(BODY))
            path=urlsplit(w.item_state(key)['last_url_private']).path
            w.fixture.routes[path]={'body':BODY+b'x'*(size-len(BODY)),'omit_length':omit}
        return keys

    def _assert_head_progress(self,w,before,report):
        self.assertGreater(report['received']+report['blocked'],0)
        self.assertIn(w.item_state(before[0])['state'],{'received','blocked'})

    def test_42_exact_budget_head_and_later_peer_length_and_no_length(self):
        cap=len(BODY)+17
        for omit in (False,True):
            with self.subTest(no_length=omit), world() as w:
                keys=self._ordered_payloads(w,[cap,len(BODY)],omit)
                limits=engine.Limits(max_bytes=cap,max_total_bytes=cap,max_attempts=1)
                first=w.fetch(limits=limits)
                self._assert_head_progress(w,keys,first)
                self.assertEqual(first['received'],1)
                self.assertEqual(first['body_bytes_received'],cap)
                self.assertEqual(w.item_state(keys[0])['state'],'received')
                self.assertEqual(w.item_state(keys[1])['state'],'pending')
                self.assertEqual(w.item_state(keys[1])['tries'],0)
                self.assertEqual(len(w.fixture.calls),1)
                second=w.fetch(limits=limits)
                self._assert_head_progress(w,keys[1:],second)
                self.assertEqual(second['received'],1)
                self.assertTrue(all(w.item_state(k)['state']=='received' for k in keys))
                self.assertEqual(len(w.fixture.calls),2)
                self.assertEqual(len(set(w.fixture.calls)),2)
                self.assertEqual(w.fixture.max_active,1)
                self.assertEqual(w.fetch(limits=limits)['attempted'],0)
                self.assertEqual(len(w.fixture.calls),2)
                PROGRESS_EVIDENCE.append({'test':42,'no_length':omit,'runs':2,
                    'requests':2,'resolved':2,'rerun_requests':0,'max_active':w.fixture.max_active})
            with self.subTest(no_length=omit,overflow=True), world() as w:
                key=self._ordered_payloads(w,[cap+1],omit)[0]
                result=w.fetch(limits=engine.Limits(max_bytes=cap,max_total_bytes=cap))
                self.assertEqual(result['blocked'],1)
                self.assertEqual(w.item_state(key)['state'],'blocked')
                self.assertEqual(w.item_state(key)['reason'],'size_limit')
                self.assertEqual(w.item_state(key)['tries'],1)
                self.assertLessEqual(result['body_bytes_received'],cap)
                self.assertEqual(len(w.fixture.calls),1)
                self.assertEqual(w.fetch()['attempted'],0)
                self.assertEqual(len(w.fixture.calls),1)

    def test_43_mixed_sizes_progress_invariant_and_bounded_host_requests(self):
        cap=len(BODY)*2
        sizes=[cap,cap-1,len(BODY),len(BODY)+3]
        for omit in (False,True):
            for total in (cap,2*cap-1):
                with self.subTest(no_length=omit,total=total), world() as w:
                    keys=self._ordered_payloads(w,sizes,omit)
                    limits=engine.Limits(max_bytes=cap,max_total_bytes=total,max_attempts=1)
                    runs=0; requests=0
                    while any(w.item_state(k)['state']!='received' for k in keys) and runs<len(keys):
                        before=[k for k in keys if w.item_state(k)['state']!='received']
                        prior=len(w.fixture.calls)
                        report=w.fetch(limits=limits); runs+=1
                        requests+=report['attempted']
                        self._assert_head_progress(w,before,report)
                        self.assertGreater(report['received'],0)
                        self.assertEqual(report['blocked'],0)
                        self.assertEqual(len(w.fixture.calls)-prior,report['attempted'])
                        self.assertLessEqual(report['body_bytes_received'],total)
                        for key in before:
                            if w.item_state(key)['state']=='retry':
                                self.assertEqual(w.item_state(key)['reason'],'total_size_limit')
                                self.assertEqual(w.item_state(key)['tries'],0)
                        # Reopen on every step: progress is durable, not a cache.
                        w.queue.close(); w.queue=Queue(w.root/'portal-queue-v1')
                    self.assertTrue(all(w.item_state(k)['state']=='received' for k in keys))
                    self.assertLessEqual(runs,len(keys))
                    self.assertEqual(len(w.fixture.calls),requests)
                    self.assertLessEqual(requests,len(keys)+runs)
                    self.assertTrue(all(host==HOST for host,path in w.fixture.calls))
                    self.assertEqual(w.fixture.max_active,1)
                    # Fixed mixed-size fixture: independent exact count oracle.
                    self.assertEqual((runs,requests),(4,6) if total==cap else (2,4))
                    self.assertEqual(w.fetch(limits=limits)['attempted'],0)
                    self.assertEqual(len(w.fixture.calls),requests)
                    PROGRESS_EVIDENCE.append({'test':43,'no_length':omit,'max_bytes':cap,
                        'total_bytes':total,'runs':runs,'requests':requests,'resolved':len(keys),
                        'max_active':w.fixture.max_active,'rerun_requests':0})

    def test_44_negative_control_bypassed_cross_field_check_catches_livelock(self):
        for omit in (False,True):
            with self.subTest(no_length=omit), world() as w:
                cap=len(BODY)*4
                keys=self._ordered_payloads(w,[len(BODY)*3,len(BODY)],omit)
                limits=engine.Limits(max_bytes=cap,max_total_bytes=cap)
                # Only the new relational check is bypassed, in memory only.
                object.__setattr__(limits,'max_total_bytes',len(BODY)*2)
                for number in range(3):
                    report=w.fetch(limits=limits)
                    self.assertEqual(report['received'],0)
                    self.assertEqual(report['attempted'],1)
                    with self.assertRaises(AssertionError): self._assert_head_progress(w,keys,report)
                    self.assertEqual(w.item_state(keys[0])['reason'],'total_size_limit')
                    self.assertEqual(w.item_state(keys[0])['state'],'retry')
                    self.assertEqual(w.item_state(keys[0])['tries'],0)
                    self.assertEqual(w.item_state(keys[1])['state'],'pending')
                    self.assertEqual(w.item_state(keys[1])['tries'],0)
                    self.assertEqual(len(w.fixture.calls),number+1)
                self.assertEqual(w.queue.status()['versions'],0)
                self.assertEqual(len(w.fixture.calls),3)
                self.assertEqual(w.fixture.max_active,1)
                PROGRESS_EVIDENCE.append({'test':44,'negative_control':True,'no_length':omit,
                    'runs':3,'requests':3,'received':0,'small_state':'pending','head_tries':0})
        # Isolate policy bound independently of Limits, no queue or host needed.
        with mock.patch.object(policy,'FETCH_MIN_TOTAL_BYTES',1):
            accepted=policy.fetch_settings({'portal':{'fetch':{'max_bytes_total':1}}})
            self.assertEqual(accepted.max_bytes_total,1)
            with self.assertRaises(AssertionError):
                with self.assertRaisesRegex(policy.PortalError,'^fetch_plan_invalid$'):
                    policy.fetch_settings({'portal':{'fetch':{'max_bytes_total':1}}})
        NEGATIVE_CONTROLS.append('cross_field_cap_livelock')
