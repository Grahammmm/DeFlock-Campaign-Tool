"""Optional, memory-only agenda PDF keyword leads; never publication approval."""
import contextlib
import hashlib
import http.client
import os
import re
import selectors
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter

MAX_BYTES = 10 * 1024 * 1024
MAX_TEXT_BYTES = 8 * 1024 * 1024
CONVERT_SECONDS = 30
_PDFTOTEXT = ("pdftotext", "-layout", "-", "-")
_CHUNK = 64 * 1024


class _TextTooLarge(subprocess.SubprocessError):
    """The converter wrote more than MAX_TEXT_BYTES; nothing it wrote is used."""


def _convert(data, *, seconds=CONVERT_SECONDS, limit=MAX_TEXT_BYTES):
    """Run pdftotext on in-memory bytes; one deadline, at most limit output bytes.

    Bounds what Python captures, not the converter's own memory. Any refusal kills
    and reaps the child; only complete output of a zero exit is returned."""
    if os.name != "posix" or not hasattr(os, "set_blocking"):
        raise OSError("bounded converter pipes need POSIX")
    deadline = time.monotonic() + seconds
    proc = subprocess.Popen(list(_PDFTOTEXT), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL)
    done = False
    try:
        out = bytearray()
        pending = memoryview(data)
        with selectors.DefaultSelector() as selector:
            for pipe in (proc.stdin, proc.stdout):
                os.set_blocking(pipe.fileno(), False)
            if pending:
                selector.register(proc.stdin, selectors.EVENT_WRITE)
            else:
                proc.stdin.close()
            selector.register(proc.stdout, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(_PDFTOTEXT[0], seconds)
                for key, _ in selector.select(remaining):
                    try:
                        if key.fileobj is proc.stdin:
                            pending = pending[os.write(proc.stdin.fileno(), pending[:_CHUNK]):]
                            if not pending:
                                selector.unregister(proc.stdin)
                                proc.stdin.close()
                            continue
                        chunk = os.read(proc.stdout.fileno(), min(_CHUNK, limit + 1 - len(out)))
                    except BlockingIOError:
                        continue
                    if not chunk:
                        selector.unregister(proc.stdout)
                        continue
                    out += chunk
                    if len(out) > limit:
                        raise _TextTooLarge(len(out))
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(_PDFTOTEXT[0], seconds)
        code = proc.wait(remaining)
        done = True
        if code:
            raise subprocess.CalledProcessError(code, _PDFTOTEXT[0])
        return bytes(out)
    finally:
        if not done:
            with contextlib.suppress(OSError):
                proc.kill()
        for pipe in (proc.stdin, proc.stdout):
            with contextlib.suppress(OSError):
                pipe.close()
        if not done:
            proc.wait()


def _declared_length(response):
    """None when absent; else the declared byte count (capped at MAX_BYTES + 1).

    Raises ValueError for anything but one plain ASCII decimal value; never echoes it."""
    raw = response.getheader("Content-Length")
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ValueError("Content-Length")
    digits = raw.strip(" \t")
    if not re.fullmatch(r"[0-9]+", digits, re.ASCII):
        raise ValueError("Content-Length")
    digits = digits.lstrip("0")
    if len(digits) > len(str(MAX_BYTES)):
        return MAX_BYTES + 1
    return min(int(digits or "0"), MAX_BYTES + 1)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def scan(url, opener=None, *, platform_host_rule):
    """One bounded GET through an injectable urllib-style opener; retain no text."""
    result = {"pdf_scan": "refused", "pdf_sha256": None, "pages": 0,
              "pages_matched": [], "matched_terms": []}
    if not isinstance(url, str):  # malformed feed value: never parsed, coerced or echoed
        return result
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
            declared = _declared_length(response)
            if declared is not None and declared > MAX_BYTES:
                return dict(result, pdf_scan="too_large")
            data = response.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            return dict(result, pdf_scan="too_large")
        if declared is not None and declared != len(data):
            return dict(result, pdf_scan="failed")
        if not data.startswith(b"%PDF-"):
            return dict(result, pdf_scan="not_pdf")
        result["pdf_sha256"] = hashlib.sha256(data).hexdigest()
        try:
            converted = _convert(data)
        except FileNotFoundError:
            return dict(result, pdf_scan="unavailable_pdftotext")
        from .meetings import KEYWORDS
        pages = converted.decode("utf-8", errors="replace").split("\f")
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
    except (OSError, http.client.HTTPException, subprocess.SubprocessError, ValueError):
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
        if not isinstance(url, str):
            # A non-string feed value is refused before it is hashed into the cache key. It is never
            # cached (no equality semantics for arbitrary JSON), so each occurrence spends one of the
            # body's 20 attempts; past the cap nothing more is counted, as for string URLs.
            if self.bodies[key] < 20:
                self.bodies[key] += 1
                self.counts["refused"] += 1
            return {}
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
