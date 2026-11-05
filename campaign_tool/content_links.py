"""DF1107-11: `content link-check` - the external https links the public content cites, and an opt-in availability check.

Default: an offline inventory (no network, DNS, file or cache). With ``online=True`` each distinct URL is requested once, one
at a time (GET, first 64 KiB, no cookies or credentials, fixed User-Agent, 15 s timeout, at most 5 https redirects).
HTTP availability is neither authenticity, source equivalence, factual verification nor legal review. Nothing is written
and no content or date is changed; an archived_url is only reported beside its original, never tried instead of it.
"""
import json
import re
import ssl
import socket
import urllib.error
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urldefrag, urljoin, urlsplit

from . import markdown_lite
from .content import ContentError, load_content

USER_AGENT = "DeFlock-Campaign-Tool/link-check"
TIMEOUT = 15
MAX_BYTES = 65536
MAX_REDIRECTS = 5
REDIRECT_CODES = (301, 302, 303, 307, 308)
GONE_CODES = (404, 410)
CONTROL = re.compile(r"[\x00-\x20\x7f-\x9f\u2028\u2029]")
OFFLINE_LINE = "Offline inventory; no URLs fetched."
DISCLAIMER = ("A reachable URL is not proof the source is authentic, equivalent to what was cited, factually correct or "
              "legally reviewed. Nothing was changed.")


class LinkCheckError(ContentError):
    """The content cannot be inventoried (no content/ or invalid content); nothing was fetched or written."""


def _safe(text, limit=300):
    """Terminal-safe one-line text: controls and escapes shown as \\xNN."""
    text = str(text)
    out = "".join(c if c.isprintable() and c not in "\x7f" else "\\x%02x" % ord(c) if ord(c) < 256 else "\\u%04x" % ord(c)
                  for c in text)
    return out if len(out) <= limit else out[:limit] + "..."


def _external(url):
    """True for an absolute https URL with a host and no userinfo or control characters."""
    if not isinstance(url, str) or CONTROL.search(url):
        return False
    try:
        parts = urlsplit(url)
        return parts.scheme == "https" and bool(parts.hostname) and parts.username is None and parts.password is None
    except ValueError:
        return False


class _Anchors(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hrefs = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.hrefs.append(href)


def _anchors(markdown):
    parser = _Anchors()
    parser.feed(markdown_lite.render(markdown))
    parser.close()
    return parser.hrefs


def _file_names(content_dir, folder, key):
    """{document key: file name} for content/<folder>/*.json (read-only; loaders already refused symlinks)."""
    names = {}
    directory = content_dir / folder
    for path in sorted(directory.glob("*.json")) if directory.is_dir() else []:
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        names[(document.get(key) if isinstance(document, dict) else None) or path.stem] = path.name
    return names


def collect_links(root, *, today=None):
    """Ordered external-link groups ``[{"url", "citations": [locator..], "archives": [{locator, archived_url, archived_on}]}]``.

    Reads the validated public content model only. ``(groups, note)``; ``note`` explains the same-site classification."""
    root = Path(root)
    model = load_content(root, today=today)
    if model is None:
        raise LinkCheckError("content/ does not exist; nothing was scaffolded or fetched")
    content_dir = root / "content"
    cites = []  # (url, locator, archive dict or None)

    def cite(url, locator, source=None):
        if _external(url):
            archive = None
            if isinstance(source, dict) and source.get("archived_url"):
                archive = {"locator": locator, "archived_url": source["archived_url"], "archived_on": source.get("archived_on")}
            cites.append((url, locator, archive))
            if archive:
                cites.append((archive["archived_url"], locator.rsplit(".", 1)[0] + ".archived_url", None))

    def prose(markdown, locator):
        if isinstance(markdown, str):
            for number, href in enumerate(_anchors(markdown), 1):
                cite(href, f"{locator}[anchor {number}]")

    site = model["site"]
    base_host = None
    if isinstance(site.get("base_url"), str) and site["base_url"]:
        base_host = (urlsplit(site["base_url"]).hostname or "").lower() or None
    prose(site.get("mission_md"), "content/site.json:mission_md")
    prose(site.get("about_md"), "content/site.json:about_md")

    agencies_file = "content/agencies.json" if (content_dir / "agencies.json").is_file() else "kit/agencies.json"
    for agency in model["agencies"]:
        base = f"{agencies_file}:{agency['agency_id']}"
        cite(agency.get("records_url"), base + ".records_url")
        cite((agency.get("portal") or {}).get("url"), base + ".portal.url")
        cite((agency.get("verification") or {}).get("source_url"), base + ".verification.source_url")
        cite(agency.get("flock_transparency_url"), base + ".flock_transparency_url")
        policy = agency.get("alpr_policy")
        if policy:
            cite(policy.get("url"), base + ".profile.alpr_policy.url", policy)
            cite(policy.get("source_url"), base + ".profile.alpr_policy.source_url")  # the archive annotates the policy url
        cite((agency.get("policy_check") or {}).get("policy_url"), base + ".profile.policy_check.policy_url")
        contract = agency.get("contract")
        if contract:
            cite(contract.get("source_url"), base + ".profile.contract.source_url", contract)
        cite((agency.get("contract_check") or {}).get("contract_url"), base + ".profile.contract_check.contract_url")
        cite((agency.get("pre_implementation_meeting") or {}).get("source_url"),
             base + ".profile.pre_implementation_meeting.source_url")
        for index, snapshot in enumerate(agency.get("transparency") or []):
            cite(snapshot.get("source_url"), f"{base}.profile.transparency[{index}].source_url", snapshot)
        for index, entry in enumerate(agency.get("funding") or []):
            cite(entry.get("source_url"), f"{base}.profile.funding[{index}].source_url", entry)
        for index, entry in enumerate(agency.get("reports") or []):
            cite(entry.get("source_url"), f"{base}.profile.reports[{index}].source_url", entry)
        cite((agency.get("sharing") or {}).get("source_url"), base + ".profile.sharing.source_url")
        for index, action in enumerate(agency.get("actions") or []):
            cite(action.get("source_url"), f"content/agency-actions.json:{agency['agency_id']}[{index}].source_url")

    for index, entry in enumerate(model["sources"]):
        cite(entry.get("public_url"), f"content/sources.json[{index}].public_url")

    meeting_files = _file_names(content_dir, "meetings", "id")
    for meeting in model["meetings"]:
        base = f"content/meetings/{meeting_files.get(meeting['id'], meeting['id'] + '.json')}"
        cite(meeting.get("agenda_url"), base + ":agenda_url")
        prose(meeting.get("comment_kit_md"), base + ":comment_kit_md")
        cite((meeting.get("participation") or {}).get("source_url"), base + ":participation.source_url")
        outcome = meeting.get("outcome") or {}
        for key in ("source_url", "minutes_url", "recording_url", "direction_source"):
            cite(outcome.get(key), f"{base}:outcome.{key}")

    for body_index, (agency_id, people) in enumerate(sorted(model["officials"].items())):
        for number, person in enumerate(people):
            where = f"content/officials.json:{agency_id}.officials[{number}]"
            cite(person.get("profile_url"), where + ".profile_url")
            cite(person.get("source_url"), where + ".source_url")

    finding_files = _file_names(content_dir, "findings", "id")
    for finding in model["findings"]:
        prose(finding.get("body_md"), f"content/findings/{finding_files.get(finding['id'], finding['id'] + '.json')}:body_md")
    for page in model["pages"]:
        prose(page.get("body_md"), f"content/pages/{page['slug']}.json:body_md")
    for topic in model.get("topics") or []:
        prose(topic.get("intro_md"), f"content/topics/{topic['slug']}.json:intro_md")
    for index, entry in enumerate(model.get("faq") or []):
        prose(entry.get("answer_md") if isinstance(entry, dict) else None, f"content/faq.json:entries[{index}].answer_md")
    glossary = model.get("glossary")
    if isinstance(glossary, dict):
        prose(glossary.get("notice_md"), "content/glossary.json:notice_md")
        for index, term in enumerate(glossary.get("terms") or []):
            prose(term.get("definition_md"), f"content/glossary.json:terms[{index}].definition_md")
            cite(term.get("source_url"), f"content/glossary.json:terms[{index}].source_url")
    for index, entry in enumerate(model.get("elsewhere") or []):
        cite(entry.get("source_url"), f"content/elsewhere.json[{index}].source_url")
        cite(entry.get("news_url"), f"content/elsewhere.json[{index}].news_url")

    note = ("Same-site links (host of site.base_url) are not listed." if base_host else
            "site.base_url is not set, so every absolute https reference is treated as external.")
    groups = {}
    for url, locator, archive in cites:
        if base_host and (urlsplit(url).hostname or "").lower() == base_host:
            continue
        group = groups.setdefault(url, {"url": url, "citations": [], "archives": []})
        group["citations"].append(locator)
        if archive:
            group["archives"].append(archive)
    ordered = []
    for url in sorted(groups):
        group = groups[url]
        group["citations"].sort()
        group["archives"].sort(key=lambda a: (a["locator"], a["archived_url"], str(a["archived_on"])))
        ordered.append(group)
    return ordered, note


# --- online transport -------------------------------------------------------

class _Record:
    """The single GET made for one fragmentless URL: outcome 'redirect' | 'final' | 'timeout' | 'error'."""
    def __init__(self, kind, status=None, location=None, detail=None):
        self.kind, self.status, self.location, self.detail = kind, status, location, detail


def real_opener():
    """A clean https opener: no proxy, cookie, auth or redirect handlers; certificates verified."""
    opener = urllib.request.OpenerDirector()
    for handler in (urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=ssl.create_default_context())):
        opener.add_handler(handler)
    return opener


def _category(exc):
    reason = getattr(exc, "reason", exc)
    if isinstance(exc, (TimeoutError, socket.timeout)) or isinstance(reason, (TimeoutError, socket.timeout)):
        return "timeout"
    if isinstance(reason, ssl.SSLError) or isinstance(exc, ssl.SSLError) or isinstance(reason, ssl.CertificateError):
        return "TLS error"
    if isinstance(reason, socket.gaierror):
        return "DNS error"
    if isinstance(reason, ConnectionRefusedError):
        return "connection refused"
    return "network error"


def _fetch(opener, url, timeout):
    request = urllib.request.Request(url, method="GET", headers={"User-Agent": USER_AGENT})
    response = None
    try:
        try:
            response = opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as exc:  # carries status and headers; must be closed
            response = exc
        except (urllib.error.URLError, OSError) as exc:
            kind = _category(exc)
            return _Record("timeout" if kind == "timeout" else "error", detail=kind)
        status = getattr(response, "status", None) or getattr(response, "code", None)
        if status in REDIRECT_CODES:
            return _Record("redirect", status, response.headers.get("Location") if response.headers else None)
        if isinstance(status, int) and 200 <= status < 300:
            try:
                response.read(MAX_BYTES)  # once, bounded; the body is neither kept nor shown
            except (urllib.error.URLError, OSError) as exc:
                kind = _category(exc)
                return _Record("timeout" if kind == "timeout" else "error", status, detail="read failed: " + kind)
        return _Record("final", status)
    finally:
        if response is not None:
            response.close()


def _target(current, location):
    """(target url, problem). The target is shown even when refused."""
    if not isinstance(location, str) or not location.strip():
        return None, "redirect without a usable Location"
    target = urljoin(current, location.strip())
    if CONTROL.search(target):
        return target, "redirect target has control characters; not followed"
    if not _external(target):
        return target, "redirect target is not an https URL with a host and no credentials; not followed"
    return target, None


def check_links(entries, *, opener, timeout=TIMEOUT):
    """Result per group, in order: ``{"status", "chain": [url..], "detail"}``; one GET per distinct fragmentless URL."""
    cache, results = {}, []

    def get(url):
        if url not in cache:
            cache[url] = _fetch(opener, url, timeout)
        return cache[url]

    for entry in entries:
        current, chain, seen = urldefrag(entry["url"])[0], [], set()
        result = None
        while result is None:
            seen.add(current)
            record = get(current)
            if record.kind == "timeout":
                result = {"status": "timeout", "detail": record.detail}
            elif record.kind == "error":
                result = {"status": "error", "detail": record.detail}
            elif record.kind == "final":
                status = record.status
                if status in GONE_CODES:
                    result = {"status": "gone", "detail": f"HTTP {status}"}
                elif isinstance(status, int) and 200 <= status < 300:
                    result = {"status": "moved" if chain else "ok", "detail": f"HTTP {status}"}
                else:
                    result = {"status": "error", "detail": f"HTTP {status}"}
            else:
                target, problem = _target(current, record.location)
                if target is not None:
                    chain.append(target)
                if problem is None and urldefrag(target)[0] in seen:
                    problem = "redirect loop"
                if problem is None and len(chain) > MAX_REDIRECTS:
                    problem = f"more than {MAX_REDIRECTS} redirects; not followed"
                if problem:
                    result = {"status": "error", "detail": problem}
                else:
                    current = urldefrag(target)[0]
        results.append({**result, "chain": chain})
    return results


# --- rendering and command --------------------------------------------------

def render(groups, note, results=None):
    lines = [OFFLINE_LINE if results is None else "Online check: each URL requested once, GET, first 64 KiB, "
             "no cookies, 15 s timeout, at most 5 https redirects.", note,
             f"{len(groups)} external https URL(s) cited."]
    for number, group in enumerate(groups):
        lines.append("")
        lines.append(f"{group['url']}")
        for locator in group["citations"]:
            lines.append(f"  cited by {locator}")
        for archive in group["archives"]:
            lines.append(f"  archive recorded at {archive['locator']}: {archive['archived_url']} "
                         f"(archived_on {archive['archived_on']})")
        if results is not None:
            result = results[number]
            for target in result["chain"]:
                lines.append(f"  redirect -> {_safe(target)}")
            lines.append(f"  result: {result['status']} ({_safe(result['detail'])})")
    if results is not None:
        counts = {}
        for result in results:
            counts[result["status"]] = counts.get(result["status"], 0) + 1
        lines += ["", "Summary: " + (", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "nothing to check")]
    lines += ["", DISCLAIMER]
    return "\n".join(lines) + "\n"


def run_cli(root, online=False, *, opener=None, today=None):
    """``(text, exit code)``: 0 for an offline inventory or only ok/moved, 3 when any URL is gone, error or timeout."""
    groups, note = collect_links(root, today=today)
    if not online:
        return render(groups, note), 0
    results = check_links(groups, opener=opener or real_opener(), timeout=TIMEOUT)
    bad = any(result["status"] in ("gone", "error", "timeout") for result in results)
    return render(groups, note, results), 3 if bad else 0
