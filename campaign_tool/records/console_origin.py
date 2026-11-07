"""Exact Host/Origin pairs for the always-loopback records panel."""
import re

from campaign_tool.records import console_access

_DNS = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


def check_public_origin(value):
    """Validate a literal HTTPS DNS origin, without normalizing untrusted input."""
    if not isinstance(value, str) or not value.startswith("https://"):
        raise ValueError("public_origin_invalid")
    host = value[len("https://"):]
    labels = host.split(".")
    if (len(host) > 253 or len(labels) < 2 or labels[-1].isdigit()
            or any(_DNS.fullmatch(label) is None for label in labels)):
        raise ValueError("public_origin_invalid")
    return host, value


def request_ok(handler, post):
    """A POST must match one entire pair, never a mixture of accepted pairs."""
    hosts = handler.headers.get_all("Host") or []
    if len(hosts) != 1:
        return False
    port = handler.server.server_address[1]
    pairs = [("127.0.0.1:%d" % port, "http://127.0.0.1:%d" % port)]
    public = getattr(handler.server, "public_pair", None)
    if public is not None:
        pairs.append(public)
    origins = handler.headers.get_all("Origin") or []
    for host, origin in pairs:
        if hosts == [host] and (not post or origins == [origin]):
            return (public is None or host != public[0]
                    or console_access.claims_ok(handler.headers, handler.server.access_cfg, handler.server.access_clock()))
    return False
