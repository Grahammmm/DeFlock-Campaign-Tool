"""Opt-in stdlib HTTPS adapter with pinned numeric-IP connections.

Tests inject resolver/connector fixtures; importing this module makes no calls.
"""
import http.client
import io
import ipaddress
import json
import multiprocessing
import os
import socket
import ssl
import stat
import time
from datetime import datetime,timezone
from .engine import Response
from .policy import PortalError,hostname,safe_ancestors,url_parts


def _dns_worker(host,pipe):
    try:
        result=socket.getaddrinfo(host,443,type=socket.SOCK_STREAM)
        pipe.send(sorted({row[4][0] for row in result}))
    except Exception:
        pipe.send(None)
    finally:
        pipe.close()


def resolve_bounded(host,timeout):
    """Killable resolver, not an unbounded thread left behind after timeout."""
    context=multiprocessing.get_context("spawn")
    parent,child=context.Pipe(duplex=False)
    process=context.Process(target=_dns_worker,args=(host,child),daemon=True)
    deadline=time.monotonic()+timeout
    try:
        process.start();child.close()
        if not parent.poll(max(0,deadline-time.monotonic())):
            raise PortalError("dns_timeout")
        addresses=parent.recv()
        if not addresses:
            raise PortalError("dns_failed")
        return addresses
    except (OSError,EOFError):
        raise PortalError("dns_failed") from None
    finally:
        parent.close();child.close()
        if process.pid:
            if process.is_alive():
                process.kill()
            process.join(timeout=1)


def public_addresses(values):
    if not isinstance(values,(list,tuple)) or not 1<=len(values)<=32:
        raise PortalError("dns_invalid")
    result=[]
    for value in values:
        try:
            if "%" in value:
                raise ValueError()
            ip=ipaddress.ip_address(value)
            if not ip.is_global or ip.is_multicast or ip.is_reserved or ip.is_loopback or ip.is_link_local or ip.is_unspecified:
                raise ValueError()
            if isinstance(ip,ipaddress.IPv6Address):
                if ip.ipv4_mapped is not None or ip.sixtofour is not None or ip.teredo is not None:
                    raise ValueError()
                if ip in ipaddress.ip_network("64:ff9b::/96") or ip in ipaddress.ip_network("64:ff9b:1::/48"):
                    raise ValueError()
            result.append(str(ip))
        except (ValueError,TypeError):
            raise PortalError("ssrf_address_denied") from None
    return sorted(set(result))


class EgressApproval:
    def __init__(self,hosts,expires):
        self.hosts=frozenset(hostname(x) for x in hosts)
        self.expires=expires

    def allows(self,host,now):
        return host in self.hosts and now.tzinfo is not None and now<self.expires


def load_egress(path,now=None):
    now=now or datetime.now(timezone.utc)
    try:
        path=safe_ancestors(path)
        parent=path.parent.stat()
        if parent.st_uid!=0 or parent.st_mode&0o022:
            raise PortalError("egress_parent_not_admin_owned")
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        try:
            before=os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or before.st_uid!=0 or before.st_mode&0o022 or before.st_size>65536:
                raise PortalError("egress_not_admin_owned")
            raw=os.read(fd,65537);after=os.fstat(fd)
            if len(raw)>65536 or (before.st_ino,before.st_size,before.st_mtime_ns)!=(after.st_ino,after.st_size,after.st_mtime_ns):
                raise PortalError("egress_policy_changed")
        finally:
            os.close(fd)
        data=json.loads(raw)
        expiry=datetime.fromisoformat(data["expires_utc"].replace("Z","+00:00"))
        if data.get("version")!=1 or data.get("enabled") is not True or data.get("purpose")!="independent-portal-egress" or expiry.tzinfo is None or expiry<=now or not isinstance(data["hosts"],list) or not data["hosts"]:
            raise PortalError("egress_policy_invalid")
        return EgressApproval(data["hosts"],expiry)
    except (OSError,ValueError,TypeError,KeyError,AttributeError):
        raise PortalError("egress_policy_missing_or_invalid") from None


def connect_pinned(ip,host,timeout):
    """Never pass the hostname back to a resolver at connect time."""
    public_addresses([ip])
    address=ipaddress.ip_address(ip)
    family=socket.AF_INET6 if address.version==6 else socket.AF_INET
    sock=socket.socket(family,socket.SOCK_STREAM)
    start=time.monotonic()
    try:
        sock.settimeout(timeout)
        sock.connect((str(address),443))
        if ipaddress.ip_address(sock.getpeername()[0])!=address:
            raise PortalError("peer_address_changed")
        remaining=timeout-(time.monotonic()-start)
        if remaining<=0:
            raise TimeoutError()
        sock.settimeout(remaining)
        context=ssl.create_default_context()
        return context.wrap_socket(sock,server_hostname=host)
    except BaseException:
        sock.close()
        raise


class DeadlineReader(io.RawIOBase):
    def __init__(self,sock,deadline,clock):
        self.sock=sock;self.deadline=deadline;self.clock=clock

    def readable(self):
        return True

    def readinto(self,buffer):
        remaining=self.deadline-self.clock()
        if remaining<=0:
            raise TimeoutError()
        self.sock.settimeout(remaining)
        count=self.sock.recv_into(buffer)
        if self.clock()>self.deadline:
            raise TimeoutError()
        return count


class DeadlineSocket:
    def __init__(self,sock,deadline,clock):
        self.sock=sock;self.deadline=deadline;self.clock=clock

    def makefile(self,mode):
        if mode!="rb":
            raise PortalError("transport_mode_invalid")
        return io.BufferedReader(DeadlineReader(self.sock,self.deadline,self.clock),buffer_size=65536)


class StdlibHTTPS:
    def __init__(self,*,apply=False,approval_loader=None,egress_loader=None,origin_host=None,
                 max_bytes=25*1024*1024,resolver=None,connector=None,clock=time.monotonic,
                 wall=lambda:datetime.now(timezone.utc)):
        if type(max_bytes) is not int or not 1<=max_bytes<=100*1024*1024:
            raise ValueError("invalid_limit")
        self.apply=apply;self.approval_loader=approval_loader;self.egress_loader=egress_loader
        self.origin=origin_host;self.max_bytes=max_bytes
        self.resolver=resolver or resolve_bounded;self.connector=connector or connect_pinned
        self.clock=clock;self.wall=wall

    def for_origin(self,origin):
        return StdlibHTTPS(apply=self.apply,approval_loader=self.approval_loader,
            egress_loader=self.egress_loader,origin_host=origin,max_bytes=self.max_bytes,
            resolver=self.resolver,connector=self.connector,clock=self.clock,wall=self.wall)

    def open(self,url,*,timeout):
        if self.apply is not True:
            raise PortalError("transport_apply_required")
        if self.approval_loader is None or self.egress_loader is None or self.origin is None:
            raise PortalError("transport_authorization_unconfigured")
        if not 0<timeout<=120:
            raise PortalError("timeout_invalid")
        parts=url_parts(url)
        host=self.approval_loader().authorize(url,self.origin,self.wall())
        if not self.egress_loader().allows(host,self.wall()):
            raise PortalError("egress_denied")
        deadline=self.clock()+timeout
        addresses=public_addresses(self.resolver(host,max(0.001,deadline-self.clock())))
        # Validate the whole answer. Mixed public/private answers never fall back.
        if self.clock()>=deadline:
            raise TimeoutError()
        sock=None;response=None
        try:
            for ip in addresses:
                remaining=deadline-self.clock()
                if remaining<=0:
                    raise TimeoutError()
                # Policy could expire or be revoked during resolution.
                self.approval_loader().authorize(url,self.origin,self.wall())
                if not self.egress_loader().allows(host,self.wall()):
                    raise PortalError("egress_denied")
                try:
                    sock=self.connector(ip,host,remaining)
                    break
                except (OSError,ssl.SSLError):
                    continue
            if sock is None:
                raise PortalError("https_connect_failed")
            remaining=deadline-self.clock()
            if remaining<=0:
                raise TimeoutError()
            sock.settimeout(remaining)
            target=(parts.path or "/")+("?"+parts.query if parts.query else "")
            request=("GET "+target+" HTTP/1.1\r\nHost: "+host+"\r\nUser-Agent: records-portal/1\r\nAccept-Encoding: identity\r\nConnection: close\r\n\r\n").encode("ascii")
            sock.sendall(request)
            response=http.client.HTTPResponse(DeadlineSocket(sock,deadline,self.clock))
            response.begin()
            pairs=response.getheaders()
            headers={}
            for key,value in pairs:
                lower=key.lower()
                if lower in headers and lower in ("content-length","transfer-encoding","location"):
                    raise PortalError("ambiguous_http_headers")
                headers[lower]=value
            if "content-length" in headers and "transfer-encoding" in headers:
                raise PortalError("ambiguous_http_headers")
            if headers.get("transfer-encoding","").lower() not in ("","chunked"):
                raise PortalError("unsupported_transfer_encoding")
            length=headers.get("content-length")
            if length is not None and (not length.isdigit() or len(length)>12 or int(length)>self.max_bytes):
                raise PortalError("size_limit")
            owned_response=response;owned_sock=sock
            def close():
                owned_response.close();owned_sock.close()
            def chunks():
                count=0
                try:
                    while True:
                        if self.clock()>=deadline:
                            raise TimeoutError()
                        chunk=owned_response.read1(min(65536,self.max_bytes-count+1))
                        if not chunk:
                            break
                        count+=len(chunk)
                        if count>self.max_bytes:
                            raise PortalError("size_limit")
                        yield chunk
                finally:
                    close()
            return Response(response.status,headers,chunks(),close)
        except BaseException:
            if response:
                response.close()
            if sock:
                sock.close()
            raise
