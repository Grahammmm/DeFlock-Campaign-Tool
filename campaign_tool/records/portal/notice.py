"""Provider-neutral extraction for notices using /documents/<item> links.

The caller supplies the request identity and a hash-bound preserved message.
Parsing a notice grants no download permission.
"""
import re
from html.parser import HTMLParser
from .policy import PortalError, url_parts


class Links(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.urls=[]

    def handle_starttag(self,tag,attrs):
        if tag.lower()=="a":
            value=dict(attrs).get("href")
            if value:
                self.urls.append(value)


def inventory_notice(queue, *, body, request_id, source_sha256, known_hosts):
    if not isinstance(body,str) or len(body.encode("utf-8"))>2*1024*1024:
        raise PortalError("notice_oversize")
    parser=Links()
    parser.feed(body)
    # HTML attributes are decoded by HTMLParser; do not also ingest their raw
    # entity-escaped form as a different refreshed signed URL.
    urls=parser.urls if parser.urls else re.findall(r'https://[^\s<>"\']+',body)
    seen=set()
    for url in urls:
        try:
            p=url_parts(url)
            match=re.fullmatch(r"/documents/([A-Za-z0-9_-]+)(?:/download)?",p.path)
            if p.hostname not in known_hosts or not match:
                continue
            key=queue.inventory(p.hostname,request_id,match.group(1),url,source_sha256)
            seen.add(key)
        except PortalError:
            continue
    return {"items":len(seen)}
