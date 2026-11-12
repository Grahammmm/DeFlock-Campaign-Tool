"""Shared, asset-free layout for the private records console."""
from .outcomes.reading import BANNER, esc

LINKS = (("Home", "/"), ("Approvals", "/#decide"), ("Agencies", "/board"),
         ("Calendar", "/calendar"), ("Timeline", "/#timeline"), ("Library", "/documents"), ("Settings", "/settings"))
MORE = (("Dashboard", "/dashboard"), ("Oversight", "/oversight"),
        ("Public updates", "/public-updates"), ("Packets", "/packets"), ("Jobs", "/jobs"),
        ("Drafts and next steps", "/next-steps"))
STYLE = """
*{box-sizing:border-box}body{margin:0;padding:1rem;background:white;color:#222;font:16px/1.5 system-ui,sans-serif;
display:grid;grid-template-columns:11rem minmax(0,1fr);gap:1rem}
body>header{grid-column:1/-1}body>header .notice{margin:0}
.skip{position:absolute;left:-999px}.skip:focus{position:static;grid-column:1/-1}
.scroll{overflow-x:auto;max-width:100%}.side{display:flex;flex-direction:column;align-self:start;gap:.4rem}
.side>a{display:block}.side .more{font-size:.85rem}.side .more a{display:inline}
main{min-width:0}a{color:#174b76}a[aria-current=page]{font-weight:700}
table{width:100%;border-collapse:collapse}th,td{padding:.25rem .5rem;text-align:left;vertical-align:top;
border-bottom:1px solid #ccc;overflow-wrap:anywhere}pre,code,q{white-space:pre-wrap;overflow-wrap:anywhere}
.overdue{color:#b00020;font-weight:700}.muted{color:#555}.diag{border-left:4px solid #804000;padding:.5rem}
textarea,input[type=text]{width:100%;font:inherit}button{min-height:44px;padding:.5rem 1rem;font:inherit}
.draft{border:1px solid #aaa;padding:.75rem;margin:.75rem 0}:focus-visible{outline:3px solid #804000}
"""
END = "</main></body></html>"
PLAIN = {"timeline": "the request timeline", "queue": "the approval queue", "public": "public updates",
         "packets": "update packets", "sends": "sent-mail results", "calendar": "the calendar",
         "waiting": "waiting on agencies", "received": "received documents", "links": "release links",
         "followup": "the follow-up register", "tiers": "the response reading"}


# One plain sentence per internal source code: what happened and what to do (S2-02).
UNAVAILABLE = {
    "missing": "This file has not been created yet; run the records step that writes it.",
    "schema_invalid": "The file is not in the expected format; ask the operator to regenerate it.",
    "not_regular_file": "The file is not a normal file, so the console will not read it.",
    "too_large": "The file is larger than the console will read.",
    "run_mismatch": "The file belongs to a different run; re-run the step for this run.",
    "shape_invalid": "The file is not in the expected format; ask the operator to regenerate it.",
    "not_loaded": "It was not loaded on this page.",
    "register_missing": "The follow-up register has not been created yet.",
    "register_invalid": "The follow-up register is not in the expected format.",
    "register_unreadable": "The follow-up register could not be read.",
    "settings_invalid": "The follow-up settings file is not valid, so reminders are off.",
    "queue_unavailable": "The approval queue could not be read.",
    "queue_invalid": "The approval queue is not in the expected format.",
    "queue_io": "The approval queue could not be read.",
    "sends_unreadable": "The sent-mail results could not be read.",
    "send_results_invalid": "The sent-mail results are not valid (send_results_invalid); export again after checking the journal.",
    "send_io": "The sent-mail results could not be read (send_io); export again after checking the journal.",
    "packets_unreadable": "The update packets could not be read.",
    "console_model_missing": "The export has no copy of this data (console_model_missing); export again.",
    "console_model_invalid": "The export's copy of this data is not valid (console_model_invalid); export again.",
    "console_model_mismatch": "The export's copy of this data is from another project (console_model_mismatch).",
}


def reason(code):
    """Plain wording for an internal source code; unknown codes get a generic sentence."""
    return UNAVAILABLE.get(code) or "It could not be read; ask the operator to check the file."


def unavailable_note(name, code):
    return "%s: unavailable. %s" % (name, reason(code))


def plain(keys):
    """Plain-English names for source keys, first seen first, without repeats."""
    return list(dict.fromkeys(PLAIN.get(key, key) for key in keys))


def sources_banner(unavailable, notes=()):
    """Top-of-page notice naming each source that could not be loaded; "" when nothing is missing."""
    text = []
    if unavailable:
        text.append("Some information could not be loaded: %s. Counts that depend on it show as unknown."
                    % ", ".join(unavailable))
    text += list(notes)
    return '<div role="status" class="diag">%s</div>' % esc(" ".join(text)) if text else ""


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
            '.side a,.side .more a{display:inline-flex;align-items:center;min-height:44px}'
            '</style></head><body><a class="skip" href="#content">Skip to content</a>'
            '<header><p class="notice">' + esc(BANNER) + '</p></header>' + nav(active) +
            '<main id="content" tabindex="-1">')


def shell(title, body_html, active=None):
    """Wrap trusted renderer HTML; record text must already be escaped."""
    return start(title, active) + body_html + END + '\n'
