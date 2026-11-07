"""Shared, asset-free layout for the private records console."""
from .outcomes.reading import BANNER, esc

LINKS = (("Home", "/"), ("Approvals", "/#decide"), ("Agencies", "/board"),
         ("Calendar", "/calendar"), ("Timeline", "/#timeline"), ("Library", "/documents"))
MORE = (("Dashboard", "/dashboard"), ("Oversight", "/oversight"),
        ("Public updates", "/public-updates"), ("Packets", "/packets"),
        ("Drafts and next steps", "/next-steps"))
STYLE = """
*{box-sizing:border-box}body{margin:0;padding:1rem;background:white;color:#222;font:16px/1.5 system-ui,sans-serif;
display:grid;grid-template-columns:11rem minmax(0,1fr);gap:1rem}
body>.notice{grid-column:1/-1;margin:0}.side{display:flex;flex-direction:column;align-self:start;gap:.4rem}
.side>a{display:block}.side .more{font-size:.85rem}.side .more a{display:inline}
main{min-width:0}a{color:#174b76}a[aria-current=page]{font-weight:700}
table{width:100%;border-collapse:collapse}th,td{padding:.25rem .5rem;text-align:left;vertical-align:top;
border-bottom:1px solid #ccc;overflow-wrap:anywhere}pre,code,q{white-space:pre-wrap;overflow-wrap:anywhere}
.overdue{color:#b00020;font-weight:700}.muted{color:#555}.diag{border-left:4px solid #804000;padding:.5rem}
textarea,input[type=text]{width:100%;font:inherit}button{min-height:44px;padding:.5rem 1rem;font:inherit}
.draft{border:1px solid #aaa;padding:.75rem;margin:.75rem 0}:focus-visible{outline:3px solid #804000}
"""
END = "</main></body></html>"


def nav(active=None):
    def link(label, href):
        current = ' aria-current="page"' if label == active else ''
        return '<a href="%s"%s>%s</a>' % (href, current, label)
    return ('<nav aria-label="Sections" class="side">' +
            ''.join(link(*pair) for pair in LINKS) + '<p class="more">More: ' +
            ' · '.join(link(*pair) for pair in MORE) + '</p></nav>')


def start(title, active=None):
    """Opening shell for renderers that accumulate a list of body fragments."""
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<meta name="robots" content="noindex,nofollow"><title>' + esc(title) +
            '</title><style>' + STYLE + '</style><style media="(max-width:699px)">'
            'body{grid-template-columns:minmax(0,1fr)}.side{grid-column:1}'
            '</style></head><body><p class="notice">' +
            esc(BANNER) + '</p>' + nav(active) + '<main id="content" tabindex="-1">')


def shell(title, body_html, active=None):
    """Wrap trusted renderer HTML; record text must already be escaped."""
    return start(title, active) + body_html + END + '\n'
