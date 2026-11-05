"""`content init`: write the minimal valid content/ tree so `build` switches to the content site.

Every organizer-owned sentence is a "[EDIT ME: ...]" placeholder; nothing is built, sent or published.
A fresh init also seeds content/faq.json from the packaged draft FAQ starter (DF0022-02)
and content/pages/help-map.json and content/pages/background.json from the packaged page starters (DF0237-15, DF1407-12).
"""
import ctypes
import errno
import json
import os
import re
import shutil
import stat
import sys
import tempfile
from pathlib import Path

from .cli import read_config
from .content import load_faq, load_pages, load_site
from .markdown_lite import lint

PLACEHOLDER = "[EDIT ME: "
_AT_FDCWD = -100
_RENAME_NOREPLACE = 1
FAQ_STARTER = Path(__file__).resolve().parent / "_resources" / "examples" / "faq.json"  # packaged mirror of examples/faq.json
HELP_MAP_STARTER = Path(__file__).resolve().parent / "_resources" / "examples" / "pages" / "help-map.json"  # mirror of examples/pages/help-map.json
BACKGROUND_STARTER = Path(__file__).resolve().parent / "_resources" / "examples" / "pages" / "background.json"  # mirror of examples/pages/background.json
GENERIC_DEADLINE = (PLACEHOLDER + "verify for this campaign] Check the applicable law package and official guidance for response timing. "
                    "A reminder is not a legal determination or a promise that records will be produced by that date.")


def _plain(value):
    """Campaign text as inert markdown_lite text: markup characters dropped, whitespace collapsed."""
    return re.sub(r"\s+", " ", re.sub(r"[<>\[\]*_`]", "", value)).strip() or "this campaign"


def site_document(cfg):
    name, county, state = _plain(cfg["name"]), _plain(cfg["county"]), _plain(cfg["state"])
    return {
        "schema_version": 1,
        "tagline": PLACEHOLDER + "one-line campaign tagline]",
        "mission_md": PLACEHOLDER + "two or three sentences on what the campaign asks for and why]",
        "about_md": ("## Who we are\n\n" + PLACEHOLDER + f"who runs {name} in {county}, {state}]\n\n"
                     "## How findings are reviewed\n\n" + PLACEHOLDER
                     + "who checks each finding against its cited records before it is published]"),
        "contact_email": None,
        "signup": {"mode": "none"},
        "language": "en",
    }


def faq_document(cfg):
    """The draft FAQ starter for this campaign, validated by the real FAQ loader.

    Read from the packaged resource (never a checkout path). For California the starter is kept as shipped, including the
    one law-package ``rule_id`` and its draft caveat; for any other state the one answer that cites a California rule is
    replaced by generic wording without a ``rule_id``, so no California rule is applied elsewhere.
    """
    document = json.loads(FAQ_STARTER.read_text(encoding="utf-8"))
    if str(cfg.get("country", "US")).upper() + "-" + cfg["state"] != "US-CA":
        for entry in document["entries"]:
            if entry.pop("rule_id", None) is not None:
                entry["answer_md"] = GENERIC_DEADLINE
    with tempfile.TemporaryDirectory(prefix=".faq-check-") as folder:  # the loader reads the jurisdiction from campaign.json
        (Path(folder) / "campaign.json").write_text(json.dumps(cfg), encoding="utf-8")
        if load_faq(document, Path(folder), [], []) is None:
            raise RuntimeError("content init produced a FAQ the build would refuse")
    if any(lint(entry["answer_md"]) for entry in document["entries"]):
        raise RuntimeError("content init produced Markdown the build would degrade")
    return document


def help_map_document():
    """The draft help-map page starter: a fresh dict read from the packaged resource (never a checkout path).

    Checked here by the Markdown lint; the page loader runs on the temporary content tree before publication."""
    document = json.loads(HELP_MAP_STARTER.read_text(encoding="utf-8"))
    if lint(document["body_md"]):
        raise RuntimeError("content init produced Markdown the build would degrade")
    return document


def background_document():
    """The draft "Background reading" page starter: a fresh dict read from the packaged resource (never a checkout path).

    Checked here by the Markdown lint; the page loader runs on the temporary content tree before publication."""
    document = json.loads(BACKGROUND_STARTER.read_text(encoding="utf-8"))
    if lint(document["body_md"]):
        raise RuntimeError("content init produced Markdown the build would degrade")
    return document


def _write(path, text):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        out.write(text)


def _renameat2():
    """Linux renameat2(2), or None where it is unavailable."""
    if not sys.platform.startswith("linux"):
        return None
    rename = getattr(ctypes.CDLL(None, use_errno=True), "renameat2", None)
    if rename is not None:
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
    return rename


def _rename_noreplace(source, target):
    """Move directory source to target; FileExistsError, never a replacement, when target exists."""
    rename = _renameat2()
    if rename is not None:
        if rename(_AT_FDCWD, os.fsencode(source), _AT_FDCWD, os.fsencode(target), _RENAME_NOREPLACE) == 0:
            return
        code = ctypes.get_errno()
        if code not in (errno.ENOSYS, errno.EINVAL):  # EINVAL: filesystem without RENAME_NOREPLACE
            raise OSError(code, os.strerror(code), str(target))
    # No atomic no-replace rename: claim target with an exclusive mkdir, then move the entries in.
    os.mkdir(target, 0o700)
    try:
        for name in os.listdir(source):
            os.rename(os.path.join(source, name), os.path.join(target, name))
        os.rmdir(source)
    except BaseException:
        shutil.rmtree(target, ignore_errors=True)
        raise


def init_content(root, source_root=None):
    """Create root/content atomically; refuse when anything named content already exists."""
    if source_root is not None: return init_from(root, source_root)
    root = Path(root)
    cfg = read_config(root)
    target = root / "content"
    if os.path.lexists(target):
        raise ValueError("content/ already exists; nothing was written")
    document = site_document(cfg)
    text = json.dumps(document, indent=2, ensure_ascii=False) + "\n"
    faq_text = json.dumps(faq_document(cfg), indent=2, ensure_ascii=False) + "\n"
    page_text = json.dumps(help_map_document(), indent=2, ensure_ascii=False) + "\n"
    background_text = json.dumps(background_document(), indent=2, ensure_ascii=False) + "\n"
    temporary = Path(tempfile.mkdtemp(prefix=".content.new-", dir=root))
    try:
        _write(temporary / "site.json", text)
        _write(temporary / "faq.json", faq_text)
        os.mkdir(temporary / "pages", 0o700)
        _write(temporary / "pages" / "help-map.json", page_text)
        _write(temporary / "pages" / "background.json", background_text)
        if len(load_pages(temporary, set())) != 2:
            raise RuntimeError("content init produced a page the build would refuse")
        # Self-check: a written file the build would reject is a bug here, not an organizer error.
        written = json.loads((temporary / "site.json").read_text(encoding="utf-8"))
        load_site(written)
        if lint(written["about_md"]) or lint(written["mission_md"]):
            raise RuntimeError("content init produced Markdown the build would degrade")
        os.mkdir(temporary / "findings", 0o700)
        os.mkdir(temporary / "meetings", 0o700)
        _write(temporary / "sources.json", "[]\n")
        try:
            _rename_noreplace(temporary, target)
        except FileExistsError:
            raise ValueError("content/ already exists; nothing was written") from None
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return {"written": ["content/faq.json", "content/findings/", "content/meetings/", "content/pages/",
                        "content/pages/background.json", "content/pages/help-map.json", "content/site.json", "content/sources.json"],
            "placeholders": (text.count(PLACEHOLDER) + faq_text.count(PLACEHOLDER) + page_text.count(PLACEHOLDER)
                             + background_text.count(PLACEHOLDER))}


def init_from(root, source_root):
    """Copy only explicitly allowed public branding, validating before publication."""
    from .content import SLUG, load_pages
    from .labels import load_labels, MAX_LABELS_BYTES
    root, source_root = Path(root), Path(source_root)
    target = root / "content"
    if root.resolve() == source_root.resolve() or source_root.resolve() in root.resolve().parents:
        raise ValueError("destination must be outside the source campaign")
    if os.path.lexists(target):
        raise ValueError("content/ already exists; nothing was written")
    new = read_config(root)
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY
    def read(fd, name, limit=256 * 1024):
        file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        with os.fdopen(file_fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
                raise ValueError(f"{name}: must be a regular file at most {limit} bytes")
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise ValueError(f"{name}: file too large")
        return json.loads(data.decode("utf-8"))
    # Reuse the actual config validator on the securely opened bytes, without reopening the source path.
    class ConfigBytes:
        def __init__(self, document): self.document = document
        def __truediv__(self, name): return self
        def read_text(self, encoding): return json.dumps(self.document)
    source_fd = os.open(source_root, flags)
    try:
        old_document = read(source_fd, "campaign.json")
        if not isinstance(old_document, dict):
            raise ValueError("campaign.json: expected an object")
        old = read_config(ConfigBytes(old_document))
        skipped = [name for name in os.listdir(source_fd) if name != "content"]
        content_fd = os.open("content", flags, dir_fd=source_fd)
        try:
            names = os.listdir(content_fd)
            site = read(content_fd, "site.json")
            load_site(site)
            allowed = {"schema_version", "tagline", "mission_md", "about_md", "social", "language",
                       "county_profile", "source_link_label", "font_family", "not_legal_advice"}
            site = {key: value for key, value in site.items() if key in allowed}
            site.update(contact_email=None, signup={"mode": "none"})
            documents = {"site.json": site}
            if "labels.json" in names:
                documents["labels.json"] = read(content_fd, "labels.json", MAX_LABELS_BYTES)
            skipped += [name for name in names if name not in ("site.json", "labels.json", "pages")]
            if "pages" in names:
                pages_fd = os.open("pages", flags, dir_fd=content_fd)
                try:
                    for name in sorted(os.listdir(pages_fd)):
                        if name.endswith(".json") and SLUG.fullmatch(name[:-5]):
                            documents["pages/" + name] = read(pages_fd, name)
                        else:
                            skipped.append("pages/" + name)
                finally:
                    os.close(pages_fd)
        finally:
            os.close(content_fd)
    finally:
        os.close(source_fd)
    counts, review = {key: 0 for key in ("name", "county", "state")}, []
    pairs = sorted(((old[key], new[key], key) for key in ("name", "county")),
                   key=lambda item: -len(item[0]))
    pattern = re.compile("|".join(re.escape(item[0]) for item in pairs))
    by_old = {item[0]: item[1:] for item in reversed(pairs)}
    def replace(value, path):
        if isinstance(value, dict):
            return {key: replace(item, path + "." + key) for key, item in value.items()}
        if isinstance(value, list):
            return [replace(item, f"{path}[{index}]") for index, item in enumerate(value)]
        if not isinstance(value, str):
            return value
        def substitute(match):
            replacement, key = by_old[match.group()]
            counts[key] += 1
            return replacement
        value = pattern.sub(substitute, value)
        value, number = re.subn(r"\b" + re.escape(old["state"]) + r"\b", lambda _: new["state"], value)
        counts["state"] += number
        if any(part.casefold() in value.casefold() for part in (old["name"], old["county"])):
            review.append(path)
        return value

    documents = {name: replace(value, name) for name, value in documents.items()}
    temporary = Path(tempfile.mkdtemp(prefix=".content.new-", dir=root))
    try:
        if any(name.startswith("pages/") for name in documents):
            os.mkdir(temporary / "pages", 0o700)
        for name, document in documents.items():
            _write(temporary / name, json.dumps(document, indent=2, ensure_ascii=False) + "\n")
        load_site(json.loads((temporary / "site.json").read_text(encoding="utf-8")))
        if "labels.json" in documents:
            load_labels(json.loads((temporary / "labels.json").read_text(encoding="utf-8")))
        load_pages(temporary, set())
        try:
            _rename_noreplace(temporary, target)
        except FileExistsError:
            raise ValueError("content/ already exists; nothing was written") from None
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return {"copied": ["content/" + name for name in documents], "replaced": counts,
            "reset": ["contact_email", "signup", "base_url"], "skipped": sorted(set(skipped)), "review": review}
