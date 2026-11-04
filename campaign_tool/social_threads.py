"""Private numbered thread drafts, one per published finding (DF2207-08). Nothing is posted.

``build_thread_social(root, finding_id)`` writes ``kit/social/threads/<slug>.md`` (or
``kit/social/threads/<platform>/<slug>.md`` per ``platforms``): post 1 is the published title, summary and
finding link, then one post per public source (a library entry with a public copy), one for the finding's
limitations and, when the finding has corrections, one for the latest correction. Every post starts with an
``(n/N) `` counter and is counted by the platform's own rule (``social.post_length``; ``len()`` within 280
without a platform). Text is never shortened, summarized or dropped: a thread that cannot fit, or needs more
than ten posts, is refused before anything is written. Kept edits go to the ``.suggested`` folders through
``social._write_drafts``; drafts for other published findings are never reported as stale.
"""
import os
import stat

from .content import load_content
from .legal_claims import legal_notes
from .social import PLATFORMS, SOCIAL_MAX_CHARS, _write_drafts, post_length
from .kit import _kit_folder

MAX_POSTS = 10
SEPARATOR = "\n\n---\n\n"


def _load(root, finding_id):
    """Return ``(finding, model, base_url, record_pages)`` for the published ``finding_id`` (exact id only)."""
    model = load_content(root)
    if model is None:
        raise ValueError("social threads need a content/ directory")
    base_url = model["site"].get("base_url")
    if not base_url:
        raise ValueError("social threads need content/site.json base_url")
    finding = next((f for f in model["findings"] if f["id"] == finding_id), None)
    if finding is None:
        raise ValueError("no published finding has that id; only published findings get a thread")
    from .cli import read_config  # local: cli imports this module lazily
    from .site import Site
    record_pages = Site(root, read_config(root), model).record_pages()  # refuses an ambiguous record prefix
    return finding, model, base_url, record_pages


def _bodies(finding, sources, base_url, record_pages):
    """The unnumbered post bodies in their fixed order; at most ``MAX_POSTS``."""
    base = base_url.rstrip("/")
    bodies = [f"{finding['title']}\n{finding['summary']}\n{base}/findings/{finding['slug']}.html"]
    library = {entry["sha256"]: entry for entry in sources}
    for citation in finding["sources"]:
        entry = library.get(citation["sha256"])
        if entry is None or not entry["public_url"]:
            continue  # uncatalogued or private copy: no post, and nothing of it is shown
        where = (f"Page: {citation['page']}" if citation.get("page") is not None
                 else f"Locator: {citation['locator']}")
        bodies.append(f"Source: {citation['title']}\n{where}\n{base}{record_pages[citation['sha256']]}")
    limits = finding["limitations"]
    bodies.append("Limitations:\n" + "\n".join("- " + item for item in limits) if limits else "Limitations: None stated.")
    if finding["corrections"]:
        latest = finding["corrections"][0]
        for correction in finding["corrections"]:
            if correction["date"] >= latest["date"]:  # equal dates: the last stored one wins
                latest = correction
        bodies.append(f"Correction {latest['date']}: {latest['note']}")
    if len(bodies) > MAX_POSTS:
        raise ValueError(f"finding {finding['id']}: thread needs {len(bodies)} posts; at most {MAX_POSTS} "
                         "(nothing is dropped or split)")
    return bodies


def _number(bodies):
    return [f"({n}/{len(bodies)}) {body}" for n, body in enumerate(bodies, 1)]


def thread_posts(finding, sources, base_url, record_pages, platform=None):
    """Return the numbered posts for ``finding``; refuse one that does not fit (``platform=None``: ``len()`` of 280)."""
    posts = _number(_bodies(finding, sources, base_url, record_pages))
    limit = PLATFORMS[platform][0] if platform is not None else SOCIAL_MAX_CHARS
    for n, post in enumerate(posts, 1):
        size = post_length(post, platform) if platform is not None else len(post)
        if size > limit:
            raise ValueError(f"finding {finding['id']}: post {n}/{len(posts)} is {size} long; the limit is {limit} "
                             f"on {platform or 'the legacy len() rule'}; nothing was shortened or written")
    return posts


def thread_review_notes(posts, finding_id):
    """DF-4 alert-only ``review-required`` notes for the exact numbered posts. Never raises, never approves."""
    try:
        return legal_notes((f"social/threads/{finding_id}/{n}", post) for n, post in enumerate(posts, 1))
    except Exception:  # noqa: BLE001 - alert-only
        return ["review-required: social: legal-claim wording (thread posts could not be scanned; read them before use)"]


def thread_notes(root, finding_id):
    """Review notes for ``finding_id``'s thread (no length check: the build already did it). Never raises."""
    try:
        finding, model, base_url, record_pages = _load(root, finding_id)
        return thread_review_notes(_number(_bodies(finding, model["sources"], base_url, record_pages)), finding_id)
    except Exception:  # noqa: BLE001 - alert-only; build_thread_social reports its own refusals
        return ["review-required: social: legal-claim wording (thread posts could not be scanned; read them before use)"]


def _draft(finding_id, posts):
    return f"<!-- DRAFT: not posted. Review before any use. thread finding {finding_id} -->\n\n{SEPARATOR.join(posts)}\n"


def _preflight(folders, targets, slug):
    """Refuse links, nonfolders and nonregular targets before any folder or file is created."""
    for folder in folders:
        if os.path.lexists(folder):
            _kit_folder(folder)
    for out, suggested in targets:
        record = out / ".written.json"
        if os.path.lexists(record) and not stat.S_ISREG(os.lstat(record).st_mode):
            raise ValueError(f"{out.name}/.written.json is not a regular file")
        target = suggested / (slug + ".md")
        if os.path.lexists(target) and not (stat.S_ISREG(os.lstat(target).st_mode) or os.path.islink(target)):
            raise ValueError(f"{suggested.name}/{slug}.md is not a regular file")


def build_thread_social(root, finding_id, platforms=None):
    """Write the thread draft for one published finding; return ``{finding, posts, threads, posted}``.

    ``threads`` is ``_write_drafts``' report (per platform under ``platforms`` with ``platforms``)."""
    finding, model, base_url, record_pages = _load(root, finding_id)
    if platforms is not None:
        platforms = list(dict.fromkeys(platforms))
        unknown = [p for p in platforms if p not in PLATFORMS]
        if unknown or not platforms:
            raise ValueError(f"unknown platform: {', '.join(unknown) or 'none given'} (choose from {', '.join(PLATFORMS)})")
    plan = {p: thread_posts(finding, model["sources"], base_url, record_pages, p) for p in (platforms or [None])}
    slug, kit_dir = finding["slug"], root / "kit"
    social_dir = kit_dir / "social"
    thread_dir = social_dir / "threads"
    if platforms is None:
        places = {None: (thread_dir, social_dir / "threads.suggested")}
    else:
        places = {p: (thread_dir / p, thread_dir / (p + ".suggested")) for p in platforms}
    _preflight([kit_dir, social_dir, thread_dir] + [d for pair in places.values() for d in pair], list(places.values()), slug)
    published = {f["slug"] for f in model["findings"]}
    reports = {}
    for p, (out, suggested) in places.items():
        for folder in (kit_dir, social_dir, thread_dir, out):
            _kit_folder(folder, mode=0o700)
        had_suggestions = os.path.lexists(suggested)
        report = _write_drafts(out, suggested, {slug: _draft(finding["id"], plan[p])})
        if not had_suggestions and os.path.isdir(suggested):
            os.chmod(suggested, 0o700)  # _write_drafts made it with the default mode
        report["stale"] = [s for s in report["stale"] if s not in published]  # another published finding's draft is not stale
        reports[p] = report
    threads = reports[None] if platforms is None else {"platforms": reports}
    return {"finding": finding["id"], "posts": len(next(iter(plan.values()))), "threads": threads, "posted": False}
