"""Public text saying in advance how to ask for a correction or a takedown.

Fixed, escaped HTML only: no form, script, storage or outbound mail. The one
optional link is a ``mailto:`` anchor built from the campaign's contact address.
"""
from html import escape
from urllib.parse import quote

NO_ADDRESS = ("This campaign has not published a contact address, so correction requests "
              "cannot be made through this site yet.")


def policy_html(contact_email):
    """Return the "How to ask for a correction or takedown" section."""
    if contact_email:
        href = f'mailto:{quote(contact_email, safe="@+")}?subject={quote("Correction request")}'
        where = (f'<p>Send your request by email to <a href="{escape(href, quote=True)}">'
                 f'{escape(contact_email, quote=True)}</a>.</p>')
    else:
        where = f"<p>{NO_ADDRESS}</p>"
    return ('<div class="notice" id="request"><h3>How to ask for a correction or takedown</h3>'
            "<p>Include the link to the page, what is wrong, and a source if you have one. "
            "Do not send personal information about anyone else.</p>"
            "<p>If a page shows personal information about a private person, say so in the first line; "
            "that page is reviewed first.</p>"
            + where
            + "<p>An organizer reviews every request against the cited records. A change is listed below "
            "with its date. A request that is not accepted gets a reply saying why.</p></div>")
