"""DF2037-13: `finding packet DRAFT --directory D --out DIR`: a private folder for an outside reviewer.

DIR must be new and outside D's public/, content/ and private/. Every check (records re-hashed) runs before anything
is written into a temporary sibling folder, renamed to DIR at the end. Nothing is zipped, uploaded or sent."""
import hashlib
import json
import os
import shutil
import stat
import tempfile
from pathlib import Path

from . import finding_law, findings
from .content import load_content, load_finding_preview
from .evidence import PREFIX
from .ledger_list import _snapshot

MAX_RECORD_BYTES = 500 * 1024 * 1024
CHUNK = 1024 * 1024
README = """Finding review packet - CONFIDENTIAL WORKING MATERIAL
This folder was made by `finding packet` for one outside reviewer (an attorney or a partner). It is a private copy
of an unpublished draft and the records it cites. Do not forward, upload or publish it.

draft.json          the draft file, byte for byte
preview.html        the finding page as the site would render it, with a "Draft - not published" banner
records/<sha256>    each cited record; the file name is the SHA-256 of its bytes
receipts.json       for each cited record, the intake receipts (receipt id, source id, received at, original name)
reviews.json        the review receipts stored for this draft
review-status.json  the current review status, including anything that still blocks publication
MANIFEST.sha256     the SHA-256 of every file above; to check that nothing changed, run inside the folder:
    sha256sum -c MANIFEST.sha256
"""


def _target(root, out):
    """DIR as an absolute path: new, parent a real folder, outside public/, content/ and private/."""
    out = Path(out).expanduser().absolute()
    if os.path.lexists(out):
        raise ValueError(f"{out} exists; choose a new folder")
    if out.parent.is_symlink() or not out.parent.is_dir():
        raise ValueError(f"{out.parent}: the parent of --out must be a real folder, not a symlink")
    target = out.parent.resolve() / out.name
    for name in ("public", "content", "private"):
        protected = (root / name).resolve()
        if target == protected or protected in target.parents:
            raise ValueError(f"{out}: --out is inside {name}/; choose a new folder outside public/, content/, private/")
    return target


def _preview_html(root, draft, label):
    """The DF1822-02 preview page (banner + noindex), rendered in memory as `finding preview` renders it."""
    from .cli import read_config
    from .site import Site
    finding = load_finding_preview(draft, label)
    model = load_content(root)
    if model is None:
        raise ValueError("finding packet needs content/ (run content init first)")
    others = [f for f in model["findings"] if f["id"] != finding["id"]]
    clash = next((f["id"] for f in others if f["slug"] == finding["slug"]), None)
    if clash:
        raise ValueError(f"{label}: slug {finding['slug']} is already used by finding {clash}; build would refuse it")
    problems = finding_law.check_findings(root, [finding])
    if problems:
        raise ValueError(f"draft {problems[0]}; build would refuse it")
    others.append(finding)
    model = dict(model, findings=sorted(others, key=lambda f: (f["published_at"], f["slug"]), reverse=True))
    return finding, Site(root, read_config(root), model).finding_preview_html(finding)["html"]


def _open_object(objects, sha):
    try:
        fd = os.open(objects / sha, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        raise ValueError(f"{sha[:PREFIX]} object_missing") from None
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise ValueError(f"{sha[:PREFIX]} object_missing")
    return os.fdopen(fd, "rb")


def _copy_hash(source, sink=None):
    digest = hashlib.sha256()
    while chunk := source.read(CHUNK):
        digest.update(chunk)
        if sink is not None:
            sink.write(chunk)
    return digest.hexdigest()


def _create(path, data=None):
    """A new 0600 file (O_EXCL, no link followed); bytes ``data`` or an open binary handle to fill."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    os.fchmod(fd, 0o600)
    handle = os.fdopen(fd, "wb")
    if data is None:
        return handle
    with handle:
        handle.write(data)


def _json(document):
    return (json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def build_packet(root, draft_path, out):
    """Write the packet; returns {"written": DIR, "files": n, "records": n}. Refusals raise before any write."""
    root = Path(root)
    target = _target(root, out)
    draft = findings.load_draft(draft_path)
    raw = findings._read_json(draft_path, findings.MAX_DRAFT_BYTES, "draft", raw=True).encode("utf-8")
    if json.loads(raw) != draft:
        raise ValueError(f"{draft_path}: draft changed while it was read; nothing was written")
    finding, html = _preview_html(root, draft, str(draft_path))
    ledger = root / "private" / "ledger.sqlite"
    db = _snapshot(ledger) if os.path.lexists(ledger) else None  # a journal or WAL sidecar refuses first
    try:
        stored = {row[0] for row in db.execute("SELECT sha256 FROM objects")} if db else set()
        problems = findings.cited_problems(root, finding, stored)  # the snapshot, never the live ledger
        if problems:
            raise ValueError(f"cited record {problems[0]}; nothing was written")
        shas = sorted({source["sha256"] for source in finding["sources"]})
        rows = {sha: [dict(zip(("receipt_id", "source_id", "received_at", "original_name"), row)) for row in db.execute(
            "SELECT receipt_id, source_id, received_at, original_name FROM receipts WHERE object_sha256 = ? "
            "ORDER BY received_at, receipt_id", (sha,))] for sha in shas}
    finally:
        db and db.close()
    objects, total = root / "private" / "objects", 0
    for sha in shas:  # size guard and every object re-hashed before anything is written
        with _open_object(objects, sha) as handle:
            total += os.fstat(handle.fileno()).st_size
            if total > MAX_RECORD_BYTES:
                raise ValueError(f"packet too large; cited records exceed {MAX_RECORD_BYTES >> 20} MiB")
            if _copy_hash(handle) != sha:
                raise ValueError(f"cited record {sha[:PREFIX]} hash_mismatch; nothing was written")
    reviews = findings._load_receipts(findings._receipts_path(root, draft))
    status = findings.review_status(root, draft)
    staging = Path(tempfile.mkdtemp(prefix=f".{target.name}.", dir=target.parent))
    try:
        os.mkdir(staging / "records", 0o700)
        manifest = {}
        for name, data in (("draft.json", raw), ("preview.html", html.encode("utf-8")), ("receipts.json", _json(rows)),
                           ("reviews.json", _json(reviews)), ("review-status.json", _json(status)),
                           ("README.txt", README.encode("utf-8"))):
            _create(staging / name, data)
            manifest[name] = hashlib.sha256(data).hexdigest()
        for sha in shas:
            with _open_object(objects, sha) as handle, _create(staging / "records" / sha) as sink:
                if _copy_hash(handle, sink) != sha:
                    raise ValueError(f"cited record {sha[:PREFIX]} changed while copying; nothing was written")
            manifest[f"records/{sha}"] = sha
        _create(staging / "MANIFEST.sha256", "".join(f"{manifest[p]}  {p}\n" for p in sorted(manifest)).encode())
        for folder in (staging / "records", staging):
            os.chmod(folder, 0o700)
        if os.path.lexists(target):
            raise ValueError(f"{target} exists; choose a new folder")
        os.rename(staging, target)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return {"written": str(target), "files": len(manifest) + 1, "records": len(shas)}
