"""DF1107-11: `content link-check` - the external https links the public content cites, and an opt-in availability check.

Default: an offline inventory (no network, DNS, file or cache). With ``online=True`` each distinct URL is requested once, one
at a time (GET, first 64 KiB, no cookies or credentials, fixed User-Agent, 15 s timeout, at most 5 https redirects).
HTTP availability is neither authenticity, source equivalence, factual verification nor legal review. Nothing is written
and no content or date is changed; an archived_url is only reported beside its original, never tried instead of it.
"""
import http.client
import json
import os
import re
import ssl
import socket
import stat
import urllib.error
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urldefrag, urljoin, urlsplit

from . import agency_actions, body_schedules, markdown_lite
from .content import MAX_JSON_BYTES, ContentError, _read_json, load_content, load_glossary

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


def _safe(text, limit: int | None = 300):
    """Terminal-safe one-line text: controls and escapes shown as \\xNN. ``limit=None`` escapes completely, never truncating."""
    text = str(text)
    out = "".join(c if c.isprintable() and c not in "\x7f" else "\\x%02x" % ord(c) if ord(c) < 256 else "\\u%04x" % ord(c)
                  for c in text)
    return out if limit is None or len(out) <= limit else out[:limit] + "..."


def _external(url):
    """True for an absolute https URL with a host and no userinfo or control characters."""
    if not isinstance(url, str) or CONTROL.search(url):
        return False
    try:
        parts = urlsplit(url)
        parts.port  # raises ValueError for a malformed or out-of-range port
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


def _read_metadata(directory, name):
    """The parsed JSON object of one immediate content file, read at most MAX_JSON_BYTES + 1 bytes.

    Refuses (ValueError/OSError) a symlink, non-regular file, oversize or non-UTF-8/JSON/object content. A leaf that
    is replaced after the loader ran is refused here; this is not complete ancestor-race confinement."""
    path = directory / name
    if not stat.S_ISREG(os.lstat(path).st_mode):
        raise ValueError("not a regular file")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("not a regular file")
        handle = os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise
    with handle:
        data = handle.read(MAX_JSON_BYTES + 1)
    if len(data) > MAX_JSON_BYTES:
        raise ValueError("too large")
    document = json.loads(data.decode("utf-8"))
    if not isinstance(document, dict):
        raise ValueError("not an object")
    return document


def _file_names(content_dir, folder, expected, *, stem_ids=False):
    """{document id: file name} for content/<folder>/*.json, fail-closed against the loaded ``expected`` ids.

    ``stem_ids`` (meetings) lets a file without an ``id`` key use its filename stem, as the loader does. An empty
    ``expected`` reads nothing. Any unsafe, unreadable, malformed, duplicate or mismatching metadata is refused with
    one content-free message; no file name is ever guessed from the id."""
    if not expected:
        return {}
    refusal = LinkCheckError(f"content/{folder}: cannot resolve stored citation locations")
    try:
        directory = content_dir / folder
        if content_dir.is_symlink() or directory.is_symlink() or not directory.is_dir():
            raise ValueError("unsafe folder")
        names = {}
        for name in sorted(entry for entry in os.listdir(directory) if entry.endswith(".json")):
            document = _read_metadata(directory, name)
            if "id" in document:
                identity = document["id"]
            elif stem_ids:
                identity = name[:-len(".json")]
            else:
                raise ValueError("missing id")
            if not isinstance(identity, str) or not identity or identity in names:
                raise ValueError("unusable or duplicate id")
            names[identity] = name
        if set(names) != set(expected):
            raise ValueError("identities differ from the loaded content")
        return names
    except (OSError, ValueError, RecursionError, TypeError):
        raise refusal from None


def _stored_schedule_positions(content_dir, schedules, today):
    """``{body: stored index}`` of body-schedules.json (the loader returns rows sorted by body).

    Nothing is read for an empty ``schedules``. Otherwise the file is re-read through the bounded ``_read_metadata``
    and must validate to exactly the loaded rows; anything else is refused with one content-free message."""
    if not schedules:
        return {}
    try:
        if content_dir.is_symlink() or not content_dir.is_dir():
            raise ValueError("unsafe folder")
        document = _read_metadata(content_dir, "body-schedules.json")
        if body_schedules.load_body_schedules(document, today=today) != schedules:
            raise ValueError("differs from the loaded schedules")
        return {row["body"]: number for number, row in enumerate(document["schedules"])}
    except (OSError, ValueError, RecursionError, TypeError, KeyError):
        raise LinkCheckError("content/body-schedules.json: cannot resolve stored citation locations") from None


def _read_json_quiet(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _stored_report_positions(path, agency_id, loaded):
    """Stored profile.reports indexes for ``loaded`` (the loader returns them newest period first)."""
    document = _read_json_quiet(path)
    stored = []
    for entry in document if isinstance(document, list) else []:
        if isinstance(entry, dict) and entry.get("agency_id") == agency_id:
            reports = (entry.get("profile") or {}).get("reports") if isinstance(entry.get("profile"), dict) else None
            stored = reports if isinstance(reports, list) else []
            break
    keys = ("title", "kind", "period_start", "period_end", "source_url", "checked_on")
    queues = {}
    for number, row in enumerate(stored):
        if isinstance(row, dict):
            queues.setdefault(tuple(str(row.get(k)) for k in keys), []).append(number)
    positions = []
    for row in loaded:
        queue = queues.get(tuple(str(row.get(k)) for k in keys))
        positions.append(queue.pop(0) if queue else None)
    return positions


def _stored_local_rule_positions(root):
    """``{(rule_id, effective_from): stored index}`` of law/local-rules.json (the loader sorts the versions)."""
    document = _read_json_quiet(Path(root) / "law" / "local-rules.json")
    rules = document.get("rules") if isinstance(document, dict) else None
    return {(row.get("rule_id"), row.get("effective_from")): number
            for number, row in enumerate(rules if isinstance(rules, list) else []) if isinstance(row, dict)}


def _stored_actions(root, agencies, today):
    loaded = {agency["agency_id"]: agency["actions"] for agency in agencies if agency.get("actions")}
    if not loaded:
        return []
    try:
        rows = agency_actions.read_actions(root, agencies, today=today)
        if rows is None or agency_actions.by_agency(rows) != loaded:
            raise ValueError
        return rows
    except (OSError, ValueError):
        raise LinkCheckError("content/agency-actions.json: cannot resolve stored citation locations") from None


def _stored_glossary_positions(root, loaded):
    if not loaded or not loaded.get("terms"):
        return {}
    try:
        path = root / "content" / "glossary.json"
        if path.parent.is_symlink() or path.is_symlink() or not path.is_file():
            raise ValueError
        document = _read_json(path)
        if load_glossary(document, root) != loaded:
            raise ValueError
        # The validator trims names but preserves their exact spelling (not glossary_key).
        return {row["term"].strip(): index for index, row in enumerate(document["terms"])}
    except (OSError, ValueError):
        raise LinkCheckError("content/glossary.json: cannot resolve stored citation locations") from None


def collect_links(root, *, today=None):
    """Ordered external-link groups ``[{"url", "citations": [locator..], "archives": [{locator, archived_url, archived_on}]}]``.

    Reads the validated public content model only. ``(groups, note)``; ``note`` explains the same-site classification."""
    root = Path(root)
    model = load_content(root, today=today)
    if model is None:
        raise LinkCheckError("content/ does not exist; nothing was scaffolded or fetched")
    content_dir = root / "content"
    stored_actions = _stored_actions(root, model["agencies"], today)
    glossary_positions = _stored_glossary_positions(root, model.get("glossary"))
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
    prose((site.get("press") or {}).get("about_md"), "content/site.json:press.about_md")

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
        reports = agency.get("reports") or []  # the loader sorts newest period first; the locator is the stored index
        stored_reports = _stored_report_positions(root / agencies_file, agency["agency_id"], reports)
        for index, entry in enumerate(reports):
            where = f"[{stored_reports[index]}]" if stored_reports[index] is not None else f"[{entry.get('title')!r}]"
            cite(entry.get("source_url"), f"{base}.profile.reports{where}.source_url", entry)
        cite((agency.get("sharing") or {}).get("source_url"), base + ".profile.sharing.source_url")

    for index, action in enumerate(stored_actions):
        cite(action.get("source_url"), f"content/agency-actions.json:actions[{index}].source_url")

    local_positions = _stored_local_rule_positions(root)
    for rule in model.get("local_rules") or []:  # sorted by (rule_id, effective_from); the locator is the stored index
        position = local_positions.get((rule.get("rule_id"), rule.get("effective_from")))
        where = f"rules[{position}]" if position is not None else f"rules[{rule.get('rule_id')}]"
        cite(rule.get("source_url"), f"law/local-rules.json:{where}.source_url")

    for index, entry in enumerate(model["sources"]):
        cite(entry.get("public_url"), f"content/sources.json[{index}].public_url")

    meeting_files = _file_names(content_dir, "meetings", {m["id"] for m in model["meetings"]}, stem_ids=True)
    for meeting in model["meetings"]:
        base = f"content/meetings/{meeting_files[meeting['id']]}"
        cite(meeting.get("agenda_url"), base + ":agenda_url")
        prose(meeting.get("comment_kit_md"), base + ":comment_kit_md")
        cite((meeting.get("participation") or {}).get("source_url"), base + ":participation.source_url")
        outcome = meeting.get("outcome") or {}
        for key in ("source_url", "minutes_url", "recording_url", "direction_source", "comment_source_url"):
            cite(outcome.get(key), f"{base}:outcome.{key}")

    for body_index, (agency_id, people) in enumerate(sorted(model["officials"].items())):
        for number, person in enumerate(people):
            where = f"content/officials.json:{agency_id}.officials[{number}]"
            cite(person.get("profile_url"), where + ".profile_url")
            cite(person.get("source_url"), where + ".source_url")

    for index, entry in enumerate(model.get("coverage") or []):
        cite(entry.get("url"), f"content/coverage.json:entries[{index}].url")
    for issue in model.get("newsletters") or []:
        prose(issue.get("body_md"), f"content/newsletters/{issue.get('sent_on')}.json:body_md")

    for index, row in enumerate(model.get("statements") or []):  # stored order; the reader refuses rather than skips
        cite(row.get("source_url"), f"content/statements.json[{index}].source_url")
    stored = _stored_schedule_positions(content_dir, model.get("body_schedules") or [], today)
    for schedule in model.get("body_schedules") or []:  # the loader sorts by body; the locator is the stored index
        cite(schedule.get("source_url"), f"content/body-schedules.json:schedules[{stored[schedule['body']]}].source_url")
    for index, event in enumerate(model.get("events") or []):  # file order; the loader refuses rather than skips
        prose(event.get("description_md"), f"content/events.json[{index}].description_md")
        cite(event.get("url"), f"content/events.json[{index}].url")

    finding_files = _file_names(content_dir, "findings", {f["id"] for f in model["findings"]})
    for finding in model["findings"]:
        prose(finding.get("body_md"), f"content/findings/{finding_files[finding['id']]}:body_md")
    for page in model["pages"]:
        prose(page.get("body_md"), f"content/pages/{page['slug']}.json:body_md")
    for topic in model.get("topics") or []:
        prose(topic.get("intro_md"), f"content/topics/{topic['slug']}.json:intro_md")
    for index, entry in enumerate(model.get("faq") or []):
        prose(entry.get("answer_md") if isinstance(entry, dict) else None, f"content/faq.json:entries[{index}].answer_md")
    glossary = model.get("glossary")
    if isinstance(glossary, dict):
        prose(glossary.get("notice_md"), "content/glossary.json:notice_md")
        for term in glossary.get("terms") or []:
            index = glossary_positions[term["term"]]
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
    response = None
    try:
        try:
            request = urllib.request.Request(url, method="GET", headers={"User-Agent": USER_AGENT})
            response = opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as exc:  # carries status and headers; must be closed
            response = exc
        except (urllib.error.URLError, OSError) as exc:
            kind = _category(exc)
            return _Record("timeout" if kind == "timeout" else "error", detail=kind)
        except (ValueError, http.client.HTTPException):  # malformed URL/port or response
            return _Record("error", detail="malformed request or response")
        status = getattr(response, "status", None) or getattr(response, "code", None)
        if status in REDIRECT_CODES:
            return _Record("redirect", status, response.headers.get("Location") if response.headers else None)
        if isinstance(status, int) and 200 <= status < 300:
            try:
                response.read(MAX_BYTES)  # once, bounded; the body is neither kept nor shown
            except (urllib.error.URLError, OSError) as exc:
                kind = _category(exc)
                return _Record("timeout" if kind == "timeout" else "error", status, detail="read failed: " + kind)
            except (ValueError, http.client.HTTPException):  # e.g. IncompleteRead from a truncated chunked body
                return _Record("error", status, detail="read failed: truncated or malformed response")
        return _Record("final", status)
    finally:
        if response is not None:
            response.close()


def _target(current, location):
    """(target url, problem). The target is shown even when refused."""
    if not isinstance(location, str) or not location.strip():
        return None, "redirect without a usable Location"
    if CONTROL.search(location):  # raw header, before strip/urljoin can erase a control character
        return location, "redirect target has control characters; not followed"
    try:
        target = urljoin(current, location)
    except ValueError:
        return location, "redirect target is malformed; not followed"
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
            lines.append(f"  cited by {_safe(locator, limit=None)}")  # stored filenames may hold terminal controls
        for archive in group["archives"]:
            lines.append(f"  archive recorded at {_safe(archive['locator'], limit=None)}: {archive['archived_url']} "
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
