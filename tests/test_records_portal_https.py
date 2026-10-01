"""HTTPS protocol fixtures only: socket and DNS operations are forbidden."""
import io
import ssl
import unittest
from datetime import timedelta
from unittest.mock import patch
from campaign_tool.records.portal.https_transport import (
    StdlibHTTPS,EgressApproval,public_addresses,DeadlineReader,connect_pinned,load_egress)
from campaign_tool.records.portal.policy import PortalError
from tests.test_records_portal import NOW,HOST,URL,PDF,approval


class SocketFixture:
    def __init__(self,raw):
        self.raw=io.BytesIO(raw);self.timeouts=[];self.sent=[];self.closed=False
    def settimeout(self,value):self.timeouts.append(value)
    def recv_into(self,buffer):
        data=self.raw.read(len(buffer));buffer[:len(data)]=data;return len(data)
    def sendall(self,raw):self.sent.append(raw)
    def close(self):self.closed=True


class HTTPSTests(unittest.TestCase):
    def setUp(self):
        self.no_socket=patch('socket.socket',side_effect=AssertionError('live socket forbidden'))
        self.no_dns=patch('socket.getaddrinfo',side_effect=AssertionError('live DNS forbidden'))
        self.no_socket.start();self.no_dns.start()
        self.addCleanup(self.no_socket.stop);self.addCleanup(self.no_dns.stop)
        self.sock=SocketFixture(b'HTTP/1.1 200 OK\r\nContent-Type: application/pdf\r\nContent-Length: '+str(len(PDF)).encode()+b'\r\n\r\n'+PDF)
        self.connections=[];self.resolutions=[]

    def transport(self,**kwargs):
        def resolve(host,timeout):self.resolutions.append(host);return ['93.184.216.34']
        def connect(ip,host,timeout):self.connections.append((ip,host,timeout));return self.sock
        params=dict(apply=True,approval_loader=approval,egress_loader=lambda:EgressApproval([HOST],NOW+timedelta(days=1)),origin_host=HOST,resolver=resolve,connector=connect,wall=lambda:NOW)
        params.update(kwargs)
        return StdlibHTTPS(**params)

    def test_success_pins_numeric_ip_and_original_host(self):
        result=self.transport().open(URL,timeout=10)
        self.assertEqual(b''.join(result.chunks),PDF)
        self.assertEqual(self.resolutions,[HOST])
        self.assertEqual(self.connections[0][:2],('93.184.216.34',HOST))
        self.assertIn(('Host: '+HOST).encode(),self.sock.sent[0])
        self.assertTrue(self.sock.closed)

    def test_apply_is_mandatory(self):
        with self.assertRaisesRegex(PortalError,'transport_apply_required'):
            self.transport(apply=False).open(URL,timeout=10)
        self.assertEqual(self.resolutions,[])

    def test_egress_is_separate_and_precedes_dns(self):
        with self.assertRaisesRegex(PortalError,'egress_denied'):
            self.transport(egress_loader=lambda:EgressApproval([],NOW+timedelta(days=1))).open(URL,timeout=10)
        self.assertEqual(self.resolutions,[])

    def test_missing_admin_egress_file(self):
        with self.assertRaises(PortalError):load_egress('/nonexistent-synthetic-egress-policy',NOW)

    def test_private_and_special_addresses_rejected(self):
        for value in ['127.0.0.1','10.1.1.1','192.168.1.1','169.254.169.254','0.0.0.0','224.0.0.1','::1','fc00::1','fe80::1','::ffff:93.184.216.34','2001:db8::1','64:ff9b::a00:1','2002:0a00:0001::1']:
            with self.subTest(ip=value),self.assertRaises(PortalError):public_addresses([value])

    def test_mixed_dns_answer_rejected_entirely(self):
        with self.assertRaisesRegex(PortalError,'ssrf_address_denied'):
            self.transport(resolver=lambda h,t:['93.184.216.34','127.0.0.1']).open(URL,timeout=10)
        self.assertEqual(self.connections,[])

    def test_dns_rebinding_cannot_trigger_second_lookup(self):
        calls=[]
        def resolver(host,timeout):
            calls.append(host)
            return ['93.184.216.34'] if len(calls)==1 else ['127.0.0.1']
        result=self.transport(resolver=resolver).open(URL,timeout=10)
        list(result.chunks)
        self.assertEqual(len(calls),1)
        self.assertEqual(self.connections[0][0],'93.184.216.34')

    def test_expired_after_resolution_blocks_connect(self):
        count=[0]
        def permission():
            count[0]+=1
            return EgressApproval([HOST],NOW+(timedelta(days=1) if count[0]==1 else timedelta(days=-1)))
        with self.assertRaisesRegex(PortalError,'egress_denied'):
            self.transport(egress_loader=permission).open(URL,timeout=10)
        self.assertEqual(self.connections,[])

    def test_transport_returns_redirect_without_following(self):
        self.sock=SocketFixture(b'HTTP/1.1 302 Found\r\nLocation: https://other.example.test/a\r\nContent-Length: 0\r\n\r\n')
        result=self.transport().open(URL,timeout=10)
        self.assertEqual(result.status,302)
        self.assertEqual(len(self.connections),1)
        result.close()

    def test_declared_body_limit(self):
        with self.assertRaisesRegex(PortalError,'size_limit'):
            self.transport(max_bytes=5).open(URL,timeout=10)
        self.assertTrue(self.sock.closed)

    def test_streaming_body_limit(self):
        self.sock=SocketFixture(b'HTTP/1.1 200 OK\r\n\r\n123456')
        result=self.transport(max_bytes=5).open(URL,timeout=10)
        with self.assertRaisesRegex(PortalError,'size_limit'):list(result.chunks)
        self.assertTrue(self.sock.closed)

    def test_ambiguous_length_and_chunking_rejected(self):
        for headers in (b'Content-Length: 1\r\nContent-Length: 2\r\n',b'Content-Length: 1\r\nTransfer-Encoding: chunked\r\n'):
            self.sock=SocketFixture(b'HTTP/1.1 200 OK\r\n'+headers+b'\r\nx')
            with self.assertRaisesRegex(PortalError,'ambiguous_http_headers'):
                self.transport().open(URL,timeout=10)

    def test_read_deadline_applies_to_each_recv(self):
        times=iter([0,2])
        reader=DeadlineReader(self.sock,1,lambda:next(times))
        with self.assertRaises(TimeoutError):reader.readinto(bytearray(3))
        self.assertEqual(self.sock.timeouts,[1])

    def test_dns_timeout_budget(self):
        times=iter([0,0,11])
        with self.assertRaises(TimeoutError):self.transport(clock=lambda:next(times)).open(URL,timeout=10)
        self.assertEqual(self.connections,[])

    def test_tls_hostname_passed_to_verified_context(self):
        class Raw(SocketFixture):
            def connect(self,target):self.target=target
            def getpeername(self):return ('93.184.216.34',443)
        raw=Raw(b'')
        class Context:
            def wrap_socket(self,sock,*,server_hostname):
                self.hostname=server_hostname;return sock
        context=Context()
        with patch('socket.socket',return_value=raw),patch('ssl.create_default_context',return_value=context):
            result=connect_pinned('93.184.216.34',HOST,5)
        self.assertIs(result,raw)
        self.assertEqual(raw.target,('93.184.216.34',443))
        self.assertEqual(context.hostname,HOST)

    def test_non_https_and_credentials_rejected(self):
        for url in ['http://'+HOST+'/documents/42','https://user:pass@'+HOST+'/documents/42']:
            with self.assertRaises(PortalError):self.transport().open(url,timeout=10)
        self.assertEqual(self.resolutions,[])

    def test_origin_clone_preserves_limits(self):
        cloned=self.transport(max_bytes=20).for_origin(HOST)
        self.assertEqual(cloned.max_bytes,20)
        self.assertEqual(cloned.origin,HOST)


if __name__=='__main__':unittest.main()
