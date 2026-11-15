"""Fixed, escaped correction and copyright guidance; no submission, storage or send path."""
from html import escape
from urllib.parse import quote

from .labels import DEFAULTS, html_text

NO_ADDRESS = DEFAULTS["corrections_policy.no_address"]
OSM_URL = "https://wiki.openstreetmap.org/wiki/Tag:surveillance:type=ALPR"


def policy_html(contact_email, labels=None):
    """Render both fixed sections using the site's already-validated merged labels."""
    labels = DEFAULTS if labels is None else labels

    def t(key, **values):
        return html_text(labels["corrections_policy." + key]).format(**values)

    def contact(subject_key):
        if not contact_email:
            return t("no_address")
        href = (f'mailto:{quote(contact_email, safe="@+")}?subject='
                + quote(labels["corrections_policy." + subject_key]))
        anchor = f'<a href="{escape(href, quote=True)}">{escape(contact_email, quote=True)}</a>'
        return t("email", email=anchor)

    osm = f'<a href="{OSM_URL}">{t("osm_link")}</a>'
    return ('<div class="notice" id="request">'
            + f'<h3>{t("request_heading")}</h3>'
            + f'<p>{t("request_include")}</p><p>{t("request_privacy")}</p>'
            + f'<p>{contact("request_subject")}</p><p>{t("request_review")}</p>'
            + f'<p>{t("osm", osm=osm)}</p></div>'
            + '<div class="notice" id="copyright">'
            + f'<h3>{t("copyright_heading")}</h3><p>{t("copyright_include")}</p>'
            + f'<p>{contact("copyright_subject")}</p><p>{t("copyright_review")}</p></div>')
