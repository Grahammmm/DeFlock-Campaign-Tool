"""`content init`: write the minimal valid content/ tree so `build` switches to the content site.

Every organizer-owned sentence is a "[EDIT ME: ...]" placeholder; nothing is built, sent or published.
"""
import json
import os
import re
import shutil
import tempfile
from pathlib import Path

from .cli import read_config
from .content import load_site
from .markdown_lite import lint

PLACEHOLDER = "[EDIT ME: "


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


def _write(path, text):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        out.write(text)


def init_content(root):
    """Create root/content atomically; refuse when anything named content already exists."""
    root = Path(root)
    cfg = read_config(root)
    target = root / "content"
    if os.path.lexists(target):
        raise ValueError("content/ already exists; nothing was written")
    document = site_document(cfg)
    text = json.dumps(document, indent=2, ensure_ascii=False) + "\n"
    temporary = Path(tempfile.mkdtemp(prefix=".content.new-", dir=root))
    try:
        _write(temporary / "site.json", text)
        # Self-check: a written file the build would reject is a bug here, not an organizer error.
        written = json.loads((temporary / "site.json").read_text(encoding="utf-8"))
        load_site(written)
        if lint(written["about_md"]) or lint(written["mission_md"]):
            raise RuntimeError("content init produced Markdown the build would degrade")
        os.mkdir(temporary / "findings", 0o700)
        os.mkdir(temporary / "meetings", 0o700)
        _write(temporary / "sources.json", "[]\n")
        if os.path.lexists(target):
            raise ValueError("content/ already exists; nothing was written")
        os.rename(temporary, target)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return {"written": ["content/findings/", "content/meetings/", "content/site.json", "content/sources.json"],
            "placeholders": text.count(PLACEHOLDER)}
