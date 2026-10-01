"""Provider-neutral extraction for notices using /documents/<item> links.

The caller supplies the request identity and a hash-bound preserved message.
Parsing a notice grants no download permission.
"""
import re
from html.parser import HTMLParser
from .policy import PortalError, url_parts
from .store import check_hash, identifier


class Links(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.urls=[]

    def handle_starttag(self,tag,attrs):
        if tag.lower()=="a":
            value=dict(attrs).get("href")
            if value:
                self.urls.append(value)

    def handle_data(self,data):
        self.urls.extend(re.findall(r'https://[^\s<>"\']+',data))


def inventory_notice(queue, *, body, request_id, source_sha256, known_hosts):
    if not isinstance(body,str) or len(body.encode("utf-8"))>2*1024*1024:
        raise PortalError("notice_oversize")
    identifier(request_id)
    check_hash(source_sha256)
    parser=Links()
    parser.feed(body)
    # Both decoded href attributes and text nodes are examined, never raw
    # HTML attributes (which would duplicate entity-escaped signed URLs).
    urls=dict.fromkeys(parser.urls)
    seen=set()
    for url in urls:
        try:
            p=url_parts(url)
            match=re.fullmatch(r"/documents/([A-Za-z0-9_-]+)(?:/download)?",p.path)
            if p.hostname not in known_hosts or not match:
                continue
        except PortalError:
            continue  # Malformed/unrecognized URL data, not an operational failure.
        key=queue.inventory(p.hostname,request_id,match.group(1),url,source_sha256)
        seen.add(key)
    return {"items":len(seen)}
