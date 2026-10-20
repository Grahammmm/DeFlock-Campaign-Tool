"""OCR stage for documents queued as ``awaiting_ocr`` (T6a, Phase 2 prep).

Phase 1's native-text extraction (T11k) counts scanned or no-text documents as
``awaiting_ocr`` and never drops them. This module is the stage that drains
that queue once the rootless flock-ocr image is live:

1. **OCRmyPDF first.** The pinned image's ``/usr/bin/ocrmypdf`` (Tesseract
   inside) runs once per document with ``--skip-text`` and a text sidecar.
   Pages it reads get ``engine=tesseract``, ``method=ocrmypdf``.
2. **Vision fallback.** A page whose OCRmyPDF text is empty or low quality
   (handwriting, faint scans) is rendered with ``/usr/bin/pdftoppm`` and sent
   to the local Qwen vision model behind an OpenAI-compatible llama.cpp server
   (default ``http://127.0.0.1:8003/v1/chat/completions``). Only loopback
   endpoints are accepted, and no proxy is used. Text it returns gets
   ``engine=qwen``.

Backends are pluggable: anything with the ``OcrmypdfBackend.pages`` or
``QwenVisionBackend.page_text`` shape works. The tests drive the stage with
fake backends and synthetic scanned PDFs, never real OCR or a model.

Queue interface (``awaiting-ocr-v1``): JSON Lines, one document per line::

    {"document_id": "<safe id>", "sha256": "<64 hex>", "path": "<absolute path>"}

``path`` must be a normalized absolute path under ``--source-root``. It is
opened component by component beneath the root with ``O_NOFOLLOW``, so no
symlinked file or ancestor directory can lead outside it. The source is read
once through that descriptor, hashed and copied into a private work directory;
the tools only ever see that copy, and the original is never modified.

Outputs live only under the stage's own ``--out`` root, one directory per
source hash and configuration signature (``<out>/<sha256>/<sig16>/``):
``page-NNNN.txt`` for every page with OCR text and ``result.json`` carrying
per-page status, engine, method, character count and text hash, never raw
text. Publication is a no-replace rename, so a finished document is never
rewritten and a rerun with the same configuration makes no backend call.
A cache hit is verified first (no symlinks, private regular files, manifest
consistency, every page text's length and hash); a result that fails to verify
is reported as ``cache_damaged`` and left in place for the operator, never
used. Cache hits do not count against ``--max-documents``, and partial
documents keep reporting their unresolved pages on every run.
Transient failures (tool timeout, vision server down) are not cached and are
retried next run. This stage never writes the canonical ledger; binding OCR
text into the ledger is the separate T6 wiring step.

The CLI prints exactly one ``records-stage-result-v1`` JSON line (counts, enum
status, fixed codes; no paths or text). OCR text is machine output: every
document with OCR text is counted as ``review_required``, never accepted.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

SCHEMA = "ocr-stage-v1"
QUEUE_SCHEMA = "awaiting-ocr-v1"

# Paths the pinned flock-ocr image provides (container/flock-ocr/Dockerfile:
# Debian 12 packages ocrmypdf 14.0.1 and poppler-utils 22.12.0).
DEFAULT_OCRMYPDF = "/usr/bin/ocrmypdf"
DEFAULT_PDFTOPPM = "/usr/bin/pdftoppm"
DEFAULT_VISION_ENDPOINT = "http://127.0.0.1:8003/v1/chat/completions"
DEFAULT_VISION_MODEL = "qwen3.8:27b-thinkingcap-q6"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

MAX_SOURCE_BYTES = 512 * 1024 ** 2
MAX_SIDECAR_BYTES = 64 * 1024 ** 2
MAX_MANIFEST_BYTES = 16 * 1024 ** 2
MAX_IMAGE_BYTES = 32 * 1024 ** 2
MAX_RESPONSE_BYTES = 4 * 1024 ** 2
MAX_QUEUE_LINE = 64 * 1024
CHILD_MEMORY_BYTES = 3 * 1024 ** 3
CHILD_FILE_BYTES = 1024 ** 3

SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
# Anything shaped like an OCRmyPDF skip marker, and the one form it may take:
# OCRmyPDF's merge_sidecars writes "[OCR skipped on page(s) N]" for one skipped
# (native-text) page and "[OCR skipped on page(s) A-B]" for a run of them.
SKIPPED_PAGE = re.compile(r"^\[OCR skipped on page\(s\) [0-9, -]+\]$")
SKIPPED_RANGE = re.compile(r"^\[OCR skipped on page\(s\) ([1-9][0-9]{0,8})(?:-([1-9][0-9]{0,8}))?\]$")
MAX_PAGES = 100_000
# Bumped whenever sidecar parsing changes, so results parsed the old way are
# never served from cache (2: skip ranges expanded, no trailing form-feed strip).
SIDECAR_PARSER = 2
THINK_BLOCK = re.compile(r"^\s*<think>.*?</think>\s*", re.S)
# Page statuses a published result.json may carry.
PAGE_STATUSES = frozenset({"native_text", "ocr_text", "ocr_empty", "ocr_low_quality"})

# OCRmyPDF ExitCode values (ocrmypdf.exceptions.ExitCode).
OCRMYPDF_EXIT = {1: "ocrmypdf_bad_args", 2: "ocrmypdf_input_file", 3: "ocrmypdf_missing_dependency",
                 4: "ocrmypdf_invalid_output", 5: "ocrmypdf_file_access", 6: "ocrmypdf_prior_ocr",
                 7: "ocrmypdf_child_process", 8: "ocrmypdf_encrypted", 9: "ocrmypdf_invalid_config",
                 10: "ocrmypdf_pdfa_conversion", 15: "ocrmypdf_other", 130: "ocrmypdf_interrupted"}
# Codes that may clear on their own; such outcomes are never cached.
TRANSIENT = frozenset({"ocrmypdf_timeout", "ocrmypdf_child_process", "ocrmypdf_interrupted",
                       "ocrmypdf_unavailable", "ocrmypdf_missing_dependency",
                       "vision_timeout", "vision_unavailable", "vision_http_error",
                       "render_timeout", "render_unavailable"})

VISION_PROMPT = ("You transcribe scanned public-records pages. Output only the text visible on the "
                 "page, in reading order, preserving line breaks. Do not summarize, translate, "
                 "correct or add anything. If the page has no legible text, output nothing.")


class BackendError(Exception):
    """A backend failure with a fixed lower-snake code (never tool output)."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _limit_child() -> None:
    """Bound OCR children without relaxing stricter inherited limits."""
    import resource
    for kind, ceiling in ((resource.RLIMIT_AS, CHILD_MEMORY_BYTES),
                          (resource.RLIMIT_FSIZE, CHILD_FILE_BYTES)):
        inherited = resource.getrlimit(kind)
        finite = [value for value in inherited if value != resource.RLIM_INFINITY]
        effective = min(ceiling, *finite) if finite else ceiling
        resource.setrlimit(kind, (effective, effective))
    os.umask(0o077)


def _child_env(executable: str, workdir: Path) -> dict:
    tool_dir = os.path.dirname(executable)
    return {"PATH": os.pathsep.join(dict.fromkeys((tool_dir, "/usr/bin", "/bin"))),
            "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "HOME": str(workdir), "TMPDIR": str(workdir),
            "OMP_THREAD_LIMIT": "1"}


def run_group(argv, *, timeout, **kwargs):
    """``subprocess.run`` shape, but the child's whole process group dies with it.

    OCRmyPDF forks Tesseract and Ghostscript; on timeout, on the stage deadline
    or on any other exit the session is killed so no OCR process outlives it.
    """
    kwargs.pop("check", None)
    kwargs["start_new_session"] = True
    proc = subprocess.Popen(argv, **kwargs)
    try:
        return subprocess.CompletedProcess(argv, proc.wait(timeout=timeout))
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        proc.wait()


def _run_tool(runner, argv: list[str], workdir: Path, timeout: int, prefix: str) -> int:
    try:
        completed = runner(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=timeout, check=False, cwd=str(workdir),
                           env=_child_env(argv[0], workdir), preexec_fn=_limit_child,
                           start_new_session=True)
    except subprocess.TimeoutExpired:
        raise BackendError(prefix + "_timeout") from None
    except OSError:
        raise BackendError(prefix + "_unavailable") from None
    return completed.returncode


def skipped_marker(page: int) -> str:
    """The per-page marker ``OcrmypdfBackend.pages`` returns for native-text page ``page``."""
    return f"[OCR skipped on page(s) {page}]"


def split_sidecar(text: str) -> list[str]:
    """Split an OCRmyPDF sidecar into exactly one string per PDF page.

    OCRmyPDF 14 (``_pipeline.merge_sidecars``) writes a form feed only
    *between* segments, never after the last one, so a sidecar that ends in
    ``\\f`` ends with an empty (blank) page. Each run of consecutive skipped
    native-text pages is collapsed into ONE segment,
    ``[OCR skipped on page(s) A-B]`` (``... N]`` for a single page). Every such
    range is expanded here into one ``skipped_marker(n)`` per page, so index
    ``i`` of the result is always page ``i + 1``. The page numbers a marker
    names must continue the count exactly; a marker-shaped segment that does
    not, or more than ``MAX_PAGES`` pages, raises
    ``BackendError('ocrmypdf_sidecar_inconsistent')``.
    """
    pages: list[str] = []
    if text.count("\f") >= MAX_PAGES:
        raise BackendError("ocrmypdf_sidecar_inconsistent")
    for segment in text.split("\f"):
        marker = segment.strip()
        if not SKIPPED_PAGE.match(marker):
            pages.append(segment)
        else:
            match = SKIPPED_RANGE.match(marker)
            first = int(match.group(1)) if match else 0
            last = int(match.group(2) or first) if match else 0
            if first != len(pages) + 1 or last < first or last > MAX_PAGES:
                raise BackendError("ocrmypdf_sidecar_inconsistent")
            pages.extend(skipped_marker(number) for number in range(first, last + 1))
        if len(pages) > MAX_PAGES:
            raise BackendError("ocrmypdf_sidecar_inconsistent")
    return pages


def _absolute_executable(path: str, name: str) -> str:
    if not isinstance(path, str) or not os.path.isabs(path):
        raise ValueError(name + " must be an absolute path")
    return path


# --- backends ----------------------------------------------------------------

class OcrmypdfBackend:
    """Tier 1: one OCRmyPDF run per document; per-page text from its sidecar."""

    engine = "tesseract"
    method = "ocrmypdf"

    def __init__(self, executable: str = DEFAULT_OCRMYPDF, language: str = "eng",
                 timeout: int = 900, runner=run_group):
        self.executable = _absolute_executable(executable, "ocrmypdf")
        if not re.fullmatch(r"[a-z]{3}(\+[a-z]{3}){0,3}", language or ""):
            raise ValueError("language must be tesseract codes like eng or eng+spa")
        self.language = language
        self.timeout = int(timeout)
        self.runner = runner

    def identity(self) -> dict:
        return {"engine": self.engine, "method": self.method, "executable": self.executable,
                "language": self.language}

    def available(self) -> bool:
        return os.path.isfile(self.executable) and os.access(self.executable, os.X_OK)

    def argv(self, source: Path, sidecar: Path, output: Path) -> list[str]:
        return [self.executable, "--quiet", "--skip-text", "--jobs", "1", "--output-type", "pdf",
                "-l", self.language, "--sidecar", str(sidecar), str(source), str(output)]

    def pages(self, source: Path, workdir: Path) -> list[str]:
        """Return one text string per PDF page (``skipped_marker(n)`` for native-text page n)."""
        sidecar, output = workdir / "sidecar.txt", workdir / "ocr.pdf"
        code = _run_tool(self.runner, self.argv(source, sidecar, output), workdir, self.timeout,
                         "ocrmypdf")
        if code:
            raise BackendError(OCRMYPDF_EXIT.get(code, "ocrmypdf_failed"))
        try:
            with open(sidecar, "rb") as stream:
                raw = stream.read(MAX_SIDECAR_BYTES + 1)
        except OSError:
            raise BackendError("ocrmypdf_sidecar_missing") from None
        if len(raw) > MAX_SIDECAR_BYTES:
            raise BackendError("ocrmypdf_sidecar_oversize")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise BackendError("ocrmypdf_sidecar_unreadable") from None
        # The form feed separates pages and never terminates the last one: a
        # trailing "\f" is a blank final page, so nothing is stripped here.
        return split_sidecar(text)


def loopback_endpoint(endpoint: str) -> str:
    """Accept only ``http://<loopback>:<port>/path`` (no credentials, no query)."""
    parts = urllib.parse.urlsplit(endpoint or "")
    if parts.scheme != "http" or parts.hostname not in LOOPBACK_HOSTS:
        raise ValueError("vision endpoint must be http on a loopback host")
    if parts.username or parts.password or parts.query or parts.fragment or parts.port is None:
        raise ValueError("vision endpoint must name a port and carry no credentials or query")
    return endpoint


def urllib_transport(endpoint: str, body: bytes, timeout: int) -> bytes:
    """POST JSON to a loopback endpoint with proxies disabled; bounded response."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(endpoint, data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError:
        raise BackendError("vision_http_error") from None
    except TimeoutError:
        raise BackendError("vision_timeout") from None
    except (urllib.error.URLError, OSError) as error:
        reason = getattr(error, "reason", None)
        if isinstance(error, TimeoutError) or isinstance(reason, TimeoutError):
            raise BackendError("vision_timeout") from None
        raise BackendError("vision_unavailable") from None
    if len(raw) > MAX_RESPONSE_BYTES:
        raise BackendError("vision_response_oversize")
    return raw


class QwenVisionBackend:
    """Tier 2: render one page and transcribe it with local Qwen vision (llama.cpp)."""

    engine = "qwen"
    method = "llama.cpp-vision"

    def __init__(self, endpoint: str = DEFAULT_VISION_ENDPOINT, model: str = DEFAULT_VISION_MODEL,
                 pdftoppm: str = DEFAULT_PDFTOPPM, dpi: int = 200, timeout: int = 300,
                 max_tokens: int = 4096, transport=urllib_transport, runner=run_group):
        self.endpoint = loopback_endpoint(endpoint)
        if not isinstance(model, str) or not 0 < len(model) <= 128:
            raise ValueError("vision model must be a 1-128 character name")
        if not 72 <= int(dpi) <= 400:
            raise ValueError("vision dpi must be 72-400")
        self.model = model
        self.pdftoppm = _absolute_executable(pdftoppm, "pdftoppm")
        self.dpi = int(dpi)
        self.timeout = int(timeout)
        self.max_tokens = int(max_tokens)
        self.transport = transport
        self.runner = runner

    def identity(self) -> dict:
        return {"engine": self.engine, "method": self.method, "endpoint": self.endpoint,
                "model": self.model, "pdftoppm": self.pdftoppm, "dpi": self.dpi}

    def render(self, source: Path, page: int, workdir: Path) -> bytes:
        base = workdir / f"vision-page-{page}"
        argv = [self.pdftoppm, "-r", str(self.dpi), "-f", str(page), "-l", str(page), "-png",
                "-singlefile", str(source), str(base)]
        if _run_tool(self.runner, argv, workdir, 120, "render"):
            raise BackendError("render_failed")
        try:
            with open(str(base) + ".png", "rb") as stream:
                image = stream.read(MAX_IMAGE_BYTES + 1)
        except OSError:
            raise BackendError("render_failed") from None
        if len(image) > MAX_IMAGE_BYTES:
            raise BackendError("render_oversize")
        if not image.startswith(b"\x89PNG\r\n\x1a\n"):
            raise BackendError("render_failed")
        return image

    def request_body(self, image: bytes) -> bytes:
        url = "data:image/png;base64," + base64.b64encode(image).decode("ascii")
        return json.dumps({
            "model": self.model, "temperature": 0, "max_tokens": self.max_tokens, "stream": False,
            "messages": [
                {"role": "system", "content": VISION_PROMPT},
                {"role": "user", "content": [
                    {"type": "text", "text": "Transcribe this scanned page."},
                    {"type": "image_url", "image_url": {"url": url}}]}]}).encode("utf-8")

    @staticmethod
    def parse(raw: bytes) -> str:
        try:
            reply = json.loads(raw.decode("utf-8"))
            content = reply["choices"][0]["message"]["content"]
        except (UnicodeDecodeError, ValueError, KeyError, IndexError, TypeError):
            raise BackendError("vision_response_invalid") from None
        if not isinstance(content, str):
            raise BackendError("vision_response_invalid")
        # Thinking models may inline their reasoning; only the transcription is kept.
        return THINK_BLOCK.sub("", content, count=1)

    def page_text(self, source: Path, page: int, workdir: Path) -> str:
        image = self.render(source, page, workdir)
        return self.parse(self.transport(self.endpoint, self.request_body(image), self.timeout))


# --- quality and queue -------------------------------------------------------

@dataclass(frozen=True)
class Quality:
    """Triage thresholds for OCRmyPDF page text (not a calibrated confidence)."""

    min_chars: int = 20
    min_alnum_ratio: float = 0.6

    def check(self, text: str) -> str | None:
        """``None`` when the page text is usable, else ``ocr_empty``/``ocr_low_quality``."""
        visible = "".join(text.split())
        if not visible:
            return "ocr_empty"
        alnum = sum(1 for char in visible if char.isalnum())
        if alnum < self.min_chars or alnum / len(visible) < self.min_alnum_ratio:
            return "ocr_low_quality"
        return None


@dataclass(frozen=True)
class QueueItem:
    document_id: str
    sha256: str
    path: str
    root: str = "/"
    parts: tuple = ()


def parse_queue_line(line: str, source_root: Path) -> QueueItem:
    """Validate one ``awaiting-ocr-v1`` line; raises ``BackendError('queue_item_invalid')``."""
    try:
        value = json.loads(line)
    except ValueError:
        raise BackendError("queue_item_invalid") from None
    if not isinstance(value, dict) or set(value) - {"document_id", "sha256", "path", "queue_schema"}:
        raise BackendError("queue_item_invalid")
    if value.get("queue_schema", QUEUE_SCHEMA) != QUEUE_SCHEMA:
        raise BackendError("queue_item_invalid")
    document_id, digest, path = value.get("document_id"), value.get("sha256"), value.get("path")
    if not (isinstance(document_id, str) and SAFE_ID.match(document_id)
            and isinstance(digest, str) and SHA256.match(digest)
            and isinstance(path, str) and os.path.isabs(path) and "\x00" not in path):
        raise BackendError("queue_item_invalid")
    # The path must already be normal: no ".", "..", "//" or trailing slash, so
    # the lexical check below and the physical traversal open the same names.
    if os.path.normpath(path) != path or path.startswith("//"):
        raise BackendError("queue_item_invalid")
    root = os.path.normpath(str(source_root))
    if os.path.commonpath((root, path)) != root or path == root:
        raise BackendError("source_outside_root")
    parts = tuple(Path(os.path.relpath(path, root)).parts)
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise BackendError("source_outside_root")
    return QueueItem(document_id, digest, path, root, parts)


def read_queue(queue_path: Path, source_root: Path):
    """Yield ``(item, error_code)`` per non-empty line; bad lines are counted, not fatal."""
    with open(queue_path, "rb") as stream:
        for raw in stream:
            if len(raw) > MAX_QUEUE_LINE:
                yield None, "queue_item_invalid"
                continue
            try:
                line = raw.decode("utf-8").strip()
            except UnicodeDecodeError:
                yield None, "queue_item_invalid"
                continue
            if not line:
                continue
            try:
                yield parse_queue_line(line, source_root), None
            except BackendError as error:
                yield None, error.code


# --- stage -------------------------------------------------------------------

@dataclass
class Outcome:
    """Per-document outcome; ``status`` and ``code`` are fixed enums."""

    document_id: str
    # ocr_complete | ocr_partial | ocr_failed | source_invalid | cache_invalid | cached
    status: str
    code: str | None = None
    engines: dict = field(default_factory=dict)
    vision_calls: int = 0
    unresolved_pages: int = 0
    cached: bool = False
    attempted: bool = False  # uncached work that counts against --max-documents

    def summary(self) -> dict:
        return {"document_id": self.document_id, "status": self.status, "code": self.code,
                "engines": dict(sorted(self.engines.items())), "vision_calls": self.vision_calls,
                "unresolved_pages": self.unresolved_pages, "cached": self.cached}


def _canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _owner_dir(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = os.lstat(path)
    if not os.path.isdir(path) or os.path.islink(path) or info.st_uid != os.getuid() \
            or info.st_mode & 0o077:
        raise BackendError("out_root_unsafe")
    return path


_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | _NOFOLLOW | _CLOEXEC
# O_NONBLOCK keeps a FIFO planted in place of a file from hanging the open.
_FILE_FLAGS = os.O_RDONLY | _NOFOLLOW | _CLOEXEC | getattr(os, "O_NONBLOCK", 0)


def _is_symlink_at(dir_fd: int, name: str) -> bool:
    try:
        return stat.S_ISLNK(os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_mode)
    except OSError:
        return False


def open_source(item: QueueItem) -> int:
    """Open ``item`` beneath its source root without following any symlink.

    The root is the operator's ``--source-root`` and is opened as given. Every
    component below it is opened relative to its parent's descriptor with
    ``O_NOFOLLOW``, so a symlinked file or ancestor directory (or one swapped
    in mid-run) cannot lead outside the root. Returns a read-only descriptor
    of a regular file; raises ``BackendError`` with a ``source_*`` code.
    """
    try:
        fd = os.open(item.root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | _CLOEXEC)
    except FileNotFoundError:
        raise BackendError("source_missing") from None
    except OSError:
        raise BackendError("source_unreadable") from None
    try:
        for index, name in enumerate(item.parts):
            last = index == len(item.parts) - 1
            try:
                child = os.open(name, _FILE_FLAGS if last else _DIR_FLAGS, dir_fd=fd)
            except FileNotFoundError:
                raise BackendError("source_missing") from None
            except OSError as error:
                if error.errno == errno.ELOOP or _is_symlink_at(fd, name):
                    raise BackendError("source_symlink") from None
                if error.errno == errno.ENOTDIR:
                    raise BackendError("source_not_regular") from None
                raise BackendError("source_unreadable") from None
            os.close(fd)
            fd = child
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise BackendError("source_not_regular")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _copy_verified(fd: int, expected: str, destination: Path) -> int:
    """Copy an opened original into the work dir while hashing; returns its size."""
    digest, size, head = hashlib.sha256(), 0, b""
    info = os.fstat(fd)
    if info.st_size > MAX_SOURCE_BYTES:
        raise BackendError("source_oversize")
    os.lseek(fd, 0, os.SEEK_SET)
    try:
        with os.fdopen(os.dup(fd), "rb") as stream, open(destination, "xb") as out:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_SOURCE_BYTES:
                    raise BackendError("source_oversize")
                if not head:
                    head = chunk[:5]
                digest.update(chunk)
                out.write(chunk)
    except OSError:
        raise BackendError("source_unreadable") from None
    if digest.hexdigest() != expected:
        raise BackendError("source_hash_mismatch")
    if head != b"%PDF-":
        raise BackendError("source_not_pdf")
    return size


class CacheDamaged(Exception):
    """A published result directory exists but does not verify."""


def _read_at(dir_fd: int, name: str, limit: int) -> bytes:
    """Read an owner-only regular file under ``dir_fd``; any doubt is damage."""
    try:
        fd = os.open(name, _FILE_FLAGS, dir_fd=dir_fd)
    except OSError:
        raise CacheDamaged() from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 \
                or info.st_size > limit:
            raise CacheDamaged()
        with os.fdopen(os.dup(fd), "rb") as stream:
            data = stream.read(limit + 1)
    except OSError:
        raise CacheDamaged() from None
    finally:
        os.close(fd)
    if len(data) > limit:
        raise CacheDamaged()
    return data


def _open_dir_at(dir_fd: int | None, name, missing_ok: bool) -> int | None:
    """Open an owner-only, non-symlink directory; ``None`` when absent and allowed."""
    try:
        fd = os.open(name, _DIR_FLAGS, dir_fd=dir_fd) if dir_fd is not None else os.open(name, _DIR_FLAGS)
    except FileNotFoundError:
        if missing_ok:
            return None
        raise CacheDamaged() from None
    except OSError:
        raise CacheDamaged() from None
    info = os.fstat(fd)
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        os.close(fd)
        raise CacheDamaged()
    return fd


def _write_private(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _publish_noreplace(staging: Path, final: Path) -> bool:
    """Rename the staging directory to ``final`` unless ``final`` exists.

    A published result directory is never empty, and POSIX ``rename`` onto a
    non-empty directory fails with EEXIST/ENOTEMPTY, so a result published by
    a concurrent run is never replaced. True when this call published it.
    """
    if os.path.lexists(final):
        return False
    try:
        os.rename(staging, final)
    except OSError as error:
        if error.errno in (errno.EEXIST, errno.ENOTEMPTY):
            return False
        raise
    return True


class OcrStage:
    """Drain ``awaiting_ocr`` items through a primary backend and an optional fallback."""

    def __init__(self, out_root: str | Path, primary, fallback=None, quality: Quality = Quality()):
        self.out_root = Path(out_root)
        self.primary = primary
        self.fallback = fallback
        self.quality = quality
        self.signature = _sha(_canonical({
            "schema": SCHEMA, "sidecar_parser": SIDECAR_PARSER, "primary": primary.identity(),
            "fallback": fallback.identity() if fallback is not None else None,
            "quality": {"min_chars": quality.min_chars, "min_alnum_ratio": quality.min_alnum_ratio}}))

    def result_dir(self, sha256: str) -> Path:
        return self.out_root / sha256 / self.signature[:16]

    @contextmanager
    def locked(self):
        _owner_dir(self.out_root)
        fd = os.open(self.out_root / ".lock", os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise BackendError("stage_busy") from None
            yield
        finally:
            os.close(fd)

    def cached(self, item: QueueItem) -> tuple[Outcome, int] | None:
        """The verified published result for ``item`` as ``(outcome, source_bytes)``.

        ``None`` when nothing is published for this source hash and
        configuration. Raises ``CacheDamaged`` when a result directory exists
        but anything in it fails to verify: a symlinked or foreign directory,
        a missing, symlinked, oversize or unparsable ``result.json``, a
        manifest inconsistent with itself, or a page text that is missing,
        not a private regular file, or does not match its recorded hash and
        length. A damaged result is never a cache hit and is never deleted
        or overwritten here; the operator inspects it.
        """
        with ExitStack() as stack:
            root = _open_dir_at(None, self.out_root, missing_ok=True)
            if root is None:
                return None
            stack.callback(os.close, root)
            by_hash = _open_dir_at(root, item.sha256, missing_ok=True)
            if by_hash is None:
                return None
            stack.callback(os.close, by_hash)
            result = _open_dir_at(by_hash, self.signature[:16], missing_ok=True)
            if result is None:
                return None
            stack.callback(os.close, result)
            try:
                return self._verify(item, result)
            except CacheDamaged:
                raise
            except Exception:  # noqa: BLE001 - any malformed manifest is damage, never a crash
                raise CacheDamaged() from None

    def _verify(self, item: QueueItem, result_fd: int) -> tuple[Outcome, int]:
        record = json.loads(_read_at(result_fd, "result.json", MAX_MANIFEST_BYTES).decode("utf-8"))
        if not isinstance(record, dict) or record.get("schema") != SCHEMA \
                or record.get("config_signature") != self.signature \
                or record.get("source_sha256") != item.sha256:
            raise CacheDamaged()
        pages, source_bytes = record["pages"], record["source_bytes"]
        if not isinstance(pages, list) or record["page_count"] != len(pages) or not pages \
                or type(source_bytes) is not int or source_bytes <= 0:
            raise CacheDamaged()
        known_engines = {self.primary.engine}
        if self.fallback is not None:
            known_engines.add(self.fallback.engine)
        engines, unresolved = {}, 0
        for number, row in enumerate(pages, start=1):
            if not isinstance(row, dict) or row.get("page") != number \
                    or row.get("status") not in PAGE_STATUSES:
                raise CacheDamaged()
            engine = row.get("engine")
            if row["status"] == "native_text":
                if engine is not None or "text_file" in row:
                    raise CacheDamaged()
                continue
            if engine not in known_engines:
                raise CacheDamaged()
            engines[engine] = engines.get(engine, 0) + 1
            if row["status"] != "ocr_text":
                unresolved += 1
            if "text_file" not in row:
                if row["status"] == "ocr_text":
                    raise CacheDamaged()
                continue
            if row["text_file"] != f"page-{number:04d}.txt":
                raise CacheDamaged()
            data = _read_at(result_fd, row["text_file"], MAX_SIDECAR_BYTES)
            if _sha(data) != row["text_sha256"] or len(data.decode("utf-8")) != row["chars"]:
                raise CacheDamaged()
        status = "ocr_partial" if unresolved else "ocr_complete"
        if record["unresolved_pages"] != unresolved or record["status"] != status \
                or record["engines"] != dict(sorted(engines.items())) \
                or record["code"] != ("ocr_pages_unresolved" if unresolved else None):
            raise CacheDamaged()
        outcome = Outcome(item.document_id, "cached", record["code"], engines,
                          unresolved_pages=unresolved, cached=True)
        return outcome, source_bytes

    def process(self, item: QueueItem, allow_work: bool = True) -> Outcome | None:
        """One queue item. ``None`` means uncached work was needed but not allowed (backlog).

        The original is opened beneath the source root first, so a vanished,
        symlinked or swapped original is reported even when its content hash
        already has a published result. Cache hits cost no backend call and
        never count as work.
        """
        try:
            fd = open_source(item)
        except BackendError as error:
            return Outcome(item.document_id, "source_invalid", error.code)
        try:
            try:
                hit = self.cached(item)
            except CacheDamaged:
                return Outcome(item.document_id, "cache_invalid", "cache_damaged")
            if hit is not None:
                outcome, source_bytes = hit
                if os.fstat(fd).st_size != source_bytes:
                    return Outcome(item.document_id, "source_invalid", "source_hash_mismatch")
                return outcome
            if not allow_work:
                return None
            work_root = _owner_dir(self.out_root / ".work")
            workdir = Path(tempfile.mkdtemp(prefix="doc-", dir=work_root))
            try:
                outcome = self._process(item, fd, workdir)
            finally:
                shutil.rmtree(workdir, ignore_errors=True)
            outcome.attempted = True
            return outcome
        finally:
            os.close(fd)

    def _process(self, item: QueueItem, fd: int, workdir: Path) -> Outcome:
        source = workdir / "source.pdf"
        try:
            source_bytes = _copy_verified(fd, item.sha256, source)
        except BackendError as error:
            return Outcome(item.document_id, "source_invalid", error.code)
        try:
            texts = self.primary.pages(source, workdir)
        except BackendError as error:
            return Outcome(item.document_id, "ocr_failed", error.code)
        # Index i must be page i + 1: a skip marker naming any other page (an
        # unexpanded range, say) would shift every later page and send the
        # wrong page to vision, so the document fails closed before any call.
        if not texts or len(texts) > MAX_PAGES or any(
                SKIPPED_PAGE.match(text.strip()) and text.strip() != skipped_marker(number)
                for number, text in enumerate(texts, start=1)):
            return Outcome(item.document_id, "ocr_failed", "ocrmypdf_sidecar_inconsistent")

        pages, outcome, transient = [], Outcome(item.document_id, "ocr_complete"), False
        for number, text in enumerate(texts, start=1):
            row: dict = {"page": number}
            if SKIPPED_PAGE.match(text.strip()):
                row.update(status="native_text", engine=None, method=None)
                pages.append((row, None))
                continue
            reason = self.quality.check(text)
            effective, engine, method = text, self.primary.engine, self.primary.method
            if reason is not None and self.fallback is not None:
                row["tesseract_reason"] = reason
                outcome.vision_calls += 1
                try:
                    vision = self.fallback.page_text(source, number, workdir)
                except BackendError as error:
                    row["vision_error"] = error.code
                    transient = transient or error.code in TRANSIENT
                else:
                    if vision.strip():
                        effective, engine, method, reason = vision, self.fallback.engine, self.fallback.method, None
                    else:
                        row["vision_error"] = "vision_empty"
            if reason is None:
                row["status"] = "ocr_text"
            else:
                row["status"] = reason
                outcome.unresolved_pages += 1
            row.update(engine=engine, method=method)
            outcome.engines[engine] = outcome.engines.get(engine, 0) + 1
            pages.append((row, effective if effective.strip() else None))

        if outcome.unresolved_pages:
            outcome.status, outcome.code = "ocr_partial", "ocr_pages_unresolved"
        if transient:
            # Not published: the vision server or a tool may be back next run.
            outcome.code = "vision_transient"
            return outcome
        self._publish(item, pages, outcome, source_bytes)
        return outcome

    def _publish(self, item: QueueItem, pages, outcome: Outcome, source_bytes: int) -> None:
        staging = Path(tempfile.mkdtemp(prefix="publish-", dir=_owner_dir(self.out_root / ".work")))
        rows = []
        for row, text in pages:
            if text is not None:
                data = text.encode("utf-8")
                name = f"page-{row['page']:04d}.txt"
                _write_private(staging / name, data)
                row.update(text_file=name, chars=len(text), text_sha256=_sha(data))
            rows.append(row)
        record = {"schema": SCHEMA, "document_id": item.document_id, "source_sha256": item.sha256,
                  "source_bytes": source_bytes, "config_signature": self.signature,
                  "status": outcome.status, "code": outcome.code, "page_count": len(rows),
                  "unresolved_pages": outcome.unresolved_pages,
                  "engines": dict(sorted(outcome.engines.items())), "review_state": "not_reviewed",
                  "pages": rows}
        _write_private(staging / "result.json", _canonical(record) + b"\n")
        final = self.result_dir(item.sha256)
        _owner_dir(final.parent)
        if not _publish_noreplace(staging, final):
            shutil.rmtree(staging, ignore_errors=True)  # a concurrent run published first


def stage_result(outcomes: list[Outcome], queue_errors: dict, backlog: int = 0) -> tuple[dict, int]:
    """Fold outcomes into one ``records-stage-result-v1`` dict and an exit code."""
    def count(*statuses):
        return sum(1 for o in outcomes if o.status in statuses)

    processed = count("ocr_complete", "ocr_partial")
    cached = count("cached")
    damaged = count("cache_invalid")
    failed = count("ocr_failed", "source_invalid", "cache_invalid") + sum(queue_errors.values())
    vision_pages = sum(o.engines.get("qwen", 0) for o in outcomes if not o.cached)
    # Outstanding coverage gaps: pages left unresolved by this run AND by
    # earlier published results that are still in the queue. A cache hit does
    # not make a partial document whole, so it keeps warning every run.
    unresolved = sum(o.unresolved_pages for o in outcomes if o.status in ("ocr_partial", "cached"))
    transient = sum(1 for o in outcomes if o.code == "vision_transient"
                    or (o.status == "ocr_failed" and o.code in TRANSIENT))
    pages = sum(sum(o.engines.values()) for o in outcomes if not o.cached and o.status != "ocr_failed")
    counts = {"input": len(outcomes) + sum(queue_errors.values()) + backlog, "output": processed,
              "failed": failed, "skipped": cached + backlog}
    items = [{"code": code, "severity": severity, "count": int(n)} for code, severity, n in (
        ("OCR_VISION_PAGES", "info", vision_pages),
        ("OCR_PAGES_UNRESOLVED", "action", unresolved),
        ("OCR_BACKEND_TRANSIENT", "action", transient),
        ("OCR_DOCUMENT_FAILED", "action", count("ocr_failed")),
        ("OCR_SOURCE_INVALID", "action", count("source_invalid")),
        ("OCR_CACHE_DAMAGED", "action", damaged),
        ("OCR_QUEUE_ITEM_INVALID", "action", sum(queue_errors.values())),
        ("OCR_BACKLOG", "info", backlog)) if n > 0]
    out = {"stage_result_version": 1, "status": "ok", "counts": counts, "error_code": None}
    if processed:
        out["new_data"] = {"documents": processed, "pages": pages}
        out["review_required"] = processed  # machine OCR text is never accepted here
    if items:
        out["needs_g"] = items
    if failed or unresolved or transient:
        out["status"] = "warn"
        out["error_code"] = ("OCR_DOCUMENT_FAILED" if failed else
                             "OCR_BACKEND_TRANSIENT" if transient else "OCR_PAGES_UNRESOLVED")
    return out, 0


def fail(code: str) -> tuple[dict, int]:
    return {"stage_result_version": 1, "status": "fail",
            "counts": {"input": 0, "output": 0, "failed": 1, "skipped": 0}, "error_code": code}, 1


def load_config(path: str | None) -> dict:
    """Owner-only ``ocr-stage-config-v1`` JSON; all keys optional (image defaults)."""
    if not path:
        return {}
    info = os.lstat(path)
    if os.path.islink(path) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("config must be an owner-only regular file")
    with open(path, encoding="utf-8") as stream:
        config = json.load(stream)
    allowed = {"config_schema", "ocrmypdf", "pdftoppm", "language", "ocrmypdf_timeout",
               "min_chars", "min_alnum_ratio", "vision"}
    if not isinstance(config, dict) or set(config) - allowed \
            or config.get("config_schema", "ocr-stage-config-v1") != "ocr-stage-config-v1":
        raise ValueError("config has unknown keys or schema")
    vision = config.get("vision")
    if vision is not None and (not isinstance(vision, dict)
                               or set(vision) - {"endpoint", "model", "dpi", "timeout", "max_tokens"}):
        raise ValueError("vision config has unknown keys")
    return config


def vision_backend(config: dict, runner=run_group, transport=urllib_transport):
    """The configured Qwen vision backend, or None when no ``vision`` block is set."""
    vision = config.get("vision")
    if vision is None:
        return None
    return QwenVisionBackend(vision.get("endpoint", DEFAULT_VISION_ENDPOINT),
                             vision.get("model", DEFAULT_VISION_MODEL),
                             config.get("pdftoppm", DEFAULT_PDFTOPPM), vision.get("dpi", 200),
                             vision.get("timeout", 300), vision.get("max_tokens", 4096),
                             transport=transport, runner=runner)


def build_stage(out_root: str, config: dict, runner=run_group, transport=urllib_transport) -> OcrStage:
    primary = OcrmypdfBackend(config.get("ocrmypdf", DEFAULT_OCRMYPDF), config.get("language", "eng"),
                              config.get("ocrmypdf_timeout", 900), runner=runner)
    fallback = vision_backend(config, runner=runner, transport=transport)
    quality = Quality(int(config.get("min_chars", 20)), float(config.get("min_alnum_ratio", 0.6)))
    return OcrStage(out_root, primary, fallback, quality)


def transcribe_png_main(argv, transport=urllib_transport) -> int:
    """``transcribe-png --config C PAGE.png``: the T6 ``extract.vision`` command adapter.

    WP4's Tesseract-first path (``extract.vision.run_fallback``) hands a verified
    page raster to an operator-configured command as ``argv + [page.png]`` and
    reads UTF-8 text from stdout. This entry point makes that command the same
    loopback-only Qwen client the queue stage uses, so there is one vision
    client. Exit 0 prints only the transcription; any failure exits non-zero
    with nothing on stdout (the caller records a blocked receipt).
    """
    p = argparse.ArgumentParser(prog="ocr_stage transcribe-png",
                                description="Print the Qwen vision transcription of one PNG page.")
    p.add_argument("--config", required=True, help="owner-only ocr-stage-config-v1 JSON with a vision block")
    p.add_argument("png")
    args = p.parse_args(argv)
    try:
        backend = vision_backend(load_config(args.config), transport=transport)
        if backend is None:
            return 2
        with open(args.png, "rb") as stream:
            image = stream.read(MAX_IMAGE_BYTES + 1)
        if len(image) > MAX_IMAGE_BYTES or not image.startswith(b"\x89PNG\r\n\x1a\n"):
            return 1
        text = backend.parse(backend.transport(backend.endpoint, backend.request_body(image), backend.timeout))
    except (BackendError, OSError, ValueError, TypeError):
        return 1
    sys.stdout.buffer.write(text.encode("utf-8"))
    sys.stdout.flush()
    return 0


def run(args, runner=run_group, transport=urllib_transport) -> tuple[dict, int, list]:
    try:
        stage = build_stage(args.out, load_config(args.config), runner=runner, transport=transport)
    except (OSError, ValueError, TypeError):
        return (*fail("OCR_CONFIG_INVALID"), [])
    if not stage.primary.available():
        return (*fail("OCR_BACKEND_UNAVAILABLE"), [])
    outcomes, errors, backlog, attempted = [], {}, 0, 0
    try:
        with stage.locked():
            for item, error in read_queue(Path(args.queue), Path(args.source_root)):
                if error:
                    errors[error] = errors.get(error, 0) + 1
                    continue
                # Only uncached work counts against the cap, so cached documents
                # never starve the rest of a stable queue across capped runs.
                outcome = stage.process(item, allow_work=attempted < args.max_documents)
                if outcome is None:
                    backlog += 1
                    continue
                attempted += outcome.attempted
                outcomes.append(outcome)
    except BackendError as error:
        if error.code == "stage_busy":
            line = {"stage_result_version": 1, "status": "warn",
                    "counts": {"input": 0, "output": 0, "failed": 0, "skipped": 1},
                    "error_code": "OCR_STAGE_BUSY"}
            return line, 0, []
        return (*fail("OCR_OUT_ROOT_UNSAFE"), [])
    except OSError:
        return (*fail("OCR_QUEUE_UNREADABLE"), [])
    line, code = stage_result(outcomes, errors, backlog)
    return line, code, outcomes


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ocr_stage", description="Drain awaiting-ocr-v1 items (one JSON result line).")
    p.add_argument("--queue", required=True, help="awaiting-ocr-v1 JSON Lines file")
    p.add_argument("--source-root", required=True, help="every queued path must lie under this directory")
    p.add_argument("--out", required=True, help="private OCR output root (owner-only)")
    p.add_argument("--config", help="owner-only ocr-stage-config-v1 JSON (defaults: image tool paths, no vision)")
    p.add_argument("--max-documents", type=int, default=50, help="bound per run; the rest is backlog")
    p.add_argument("--timeout", type=int, default=2700, help="stage deadline in seconds")
    p.add_argument("--report", help="optional private per-document outcome JSON (ids and codes only)")
    return p


def main(argv=None) -> int:
    os.umask(0o077)
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["transcribe-png"]:
        return transcribe_png_main(argv[1:])
    args = parser().parse_args(argv)
    if not 1 <= args.timeout <= 7200 or not 1 <= args.max_documents <= 10000:
        print(json.dumps(fail("STAGE_CONFIG_INVALID")[0], sort_keys=True))
        return 1

    class StageDeadline(BaseException):
        """Not an OSError/Exception, so no backend handler can swallow it."""

    def on_alarm(signum, frame):
        raise StageDeadline()

    signal.signal(signal.SIGALRM, on_alarm)
    signal.alarm(args.timeout)
    outcomes = []
    try:
        line, code, outcomes = run(args)
    except StageDeadline:
        line, code = fail("STAGE_CONTAINER_TIMEOUT")
    except Exception:  # noqa: BLE001 - never print exception text (may hold private values)
        line, code = fail("STAGE_INTERNAL_ERROR")
    finally:
        signal.alarm(0)
    if args.report and outcomes:
        try:
            temporary = Path(args.report + ".tmp")
            temporary.unlink(missing_ok=True)
            _write_private(temporary, _canonical([o.summary() for o in outcomes]) + b"\n")
            os.replace(temporary, args.report)
        except OSError:
            pass
    print(json.dumps(line, sort_keys=True))
    return code


if __name__ == "__main__":
    sys.exit(main())
