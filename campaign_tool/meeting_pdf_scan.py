"""Optional, memory-only agenda PDF keyword leads; never publication approval."""
import hashlib
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter

MAX_BYTES = 10 * 1024 * 1024


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def scan(url, opener=None, *, platform_host_rule):
    """One bounded GET through an injectable urllib-style opener; retain no text."""
    result = {"pdf_scan": "refused", "pdf_sha256": None, "pages": 0,
              "pages_matched": [], "matched_terms": []}
    try:
        parts = urllib.parse.urlsplit(url)
        if (parts.scheme != "https" or not parts.hostname or parts.port is not None
                or parts.username is not None or parts.password is not None
                or any(c.isspace() or ord(c) < 32 for c in url)
                or not platform_host_rule(url)):
            return result
    except (ValueError, TypeError):
        return result
    try:
        request = urllib.request.Request(url, headers={"Accept": "application/pdf",
                                                       "User-Agent": "deflock-campaign-tool/0.1 (+meetings)"})
        with (opener or urllib.request.build_opener(NoRedirect())).open(request, timeout=20) as response:
            if response.getcode() != 200 or response.geturl() != url:
                return result
            data = response.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            return dict(result, pdf_scan="too_large")
        if not data.startswith(b"%PDF-"):
            return dict(result, pdf_scan="not_pdf")
        result["pdf_sha256"] = hashlib.sha256(data).hexdigest()
        try:
            converted = subprocess.run(["pdftotext", "-layout", "-", "-"], input=data,
                                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                       timeout=30, check=True)
        except FileNotFoundError:
            return dict(result, pdf_scan="unavailable_pdftotext")
        from .meetings import KEYWORDS
        pages = converted.stdout.decode("utf-8", errors="replace").split("\f")
        if pages and not pages[-1].strip():
            pages.pop()
        terms = set()
        for number, page in enumerate(pages, 1):
            hits = {m.group(0).lower() for m in KEYWORDS.finditer(page)}
            if hits:
                result["pages_matched"].append(number)
                terms.update(hits)
        return dict(result, pdf_scan="ok", pages=len(pages), matched_terms=sorted(terms))
    except urllib.error.HTTPError as exc:
        exc.close()
        return dict(result, pdf_scan="refused" if 300 <= exc.code < 400 else "failed")
    except (OSError, subprocess.SubprocessError, ValueError):
        return dict(result, pdf_scan="failed")


def host_rule(client):
    """Use the existing platform's agenda-link boundary, never an arbitrary feed host."""
    from .meetings import ATTACHMENT_HOST

    def allowed(url):
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname or ""
        if client.source == "legistar":
            return bool(ATTACHMENT_HOST.fullmatch(host))
        if client.source == "civicclerk":
            return host == "civicclerk.com" or host.endswith(".civicclerk.com")
        if client.source in ("granicus", "agendacenter"):
            return host == client.host and (client.source != "agendacenter" or parts.path.startswith("/AgendaCenter/"))
        return False
    return allowed


class AgendaScans:
    """Per-run cache and per-body cap; only eligible unmatched agenda URLs use it."""
    def __init__(self, opener=None):
        self.opener = opener
        self.counts = Counter()
        self.bodies = Counter()
        self.cache = {}

    def meeting(self, client, body, url):
        key = (client.source, client.client, body)
        cache_key = (key, url)
        if cache_key not in self.cache:
            if self.bodies[key] >= 20:
                return {}
            self.bodies[key] += 1
            result = scan(url, self.opener, platform_host_rule=host_rule(client))
            self.counts[result["pdf_scan"]] += 1
            self.cache[cache_key] = result
        result = self.cache[cache_key]
        if not result["pages_matched"]:
            return {}
        return {"relevance": "alpr_item", "matched_in": "agenda_pdf",
                "pdf_pages_matched": result["pages_matched"], "pdf_sha256": result["pdf_sha256"],
                "matched_terms": result["matched_terms"]}
