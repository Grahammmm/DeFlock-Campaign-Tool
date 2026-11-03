"""Campaign-site links of a newsletter draft (``newsletter --site PUBLIC``).

Every ``a[href]`` in the draft HTML that points into ``base_url`` becomes a
root-relative link (``/findings/x.html``, ``/#id``, ``/`` for the home link).
Without a ``base_url`` the plain-text ``/findings/<slug>.html`` paths of the
text draft are used instead. Links to other hosts or schemes are only counted;
nothing is ever fetched. Stdlib only.
"""
from html.parser import HTMLParser

from .draft import _PATH


class _Anchors(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hrefs = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.hrefs.extend(value for name, value in attrs if name == "href" and value is not None)


def _split(draft, base_url):
    parser = _Anchors()
    parser.feed(draft.get("html") or "")
    parser.close()
    site, external = set(), set()
    base = base_url.rstrip("/") if base_url else None
    for href in parser.hrefs:
        if base and (href == base or href.startswith((base + "/", base + "#"))):
            site.add("/" + href[len(base):].lstrip("/"))
        else:
            external.add(href)
    if not base:
        for token in (draft.get("text") or "").split():
            token = token.strip("()")
            if _PATH.fullmatch(token):
                site.add(token)
    return sorted(site), sorted(external)


def site_links(draft, base_url):
    """Sorted, de-duplicated root-relative campaign-site links of ``draft``."""
    return _split(draft, base_url)[0]


def external_links(draft, base_url):
    """Sorted, de-duplicated ``a[href]`` values on other hosts or schemes; never fetched."""
    return _split(draft, base_url)[1]
