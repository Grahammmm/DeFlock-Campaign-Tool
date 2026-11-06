"""Read-only offline launch checklist; no deployment or approval inference."""
import json
from pathlib import Path
import re
import shutil
import tempfile

from .content import load_content
from .evidence import evidence_report
from .kit import stale_contacts
from .site import build_site
from .site_check import check_links

INIT = "Run content init (DF2237-06) or create content/"
NOTE = ("Offline readiness only. This is not a launch approval; deployment "
        "(docs/CLOUDFLARE.md) and human review remain.")


def launch_checks(root, contact_max_age_days=365, today=None):
    from .cli import check_public_tree, law_package_report, read_config
    root = Path(root)
    rows = []

    def add(name, state, detail, action=None):
        # Validation messages may contain source identifiers; never expose full hashes.
        detail = re.sub(r"[0-9a-fA-F]{64}", lambda m: m[0][:12], str(detail))
        rows.append(dict(check=name, state=state, detail=detail, next_action=action))

    absent = not (root / "content").is_dir()
    model, error = None, None
    try:
        model = load_content(root, today=today)
    except (ValueError, OSError) as exc:
        error = str(exc)
    site = model["site"] if model else {}
    for name, action in (("base_url", "Set site.json base_url to the public https address"),
                         ("contact_email", "Set site.json contact_email to a public contact address")):
        value = site.get(name)
        add(name, "todo" if absent else "fail" if error else "ok" if value else "todo",
            "Content absent" if absent else error or ("Set" if value else "Not set"),
            INIT if absent else "Fix content validation errors" if error else None if value else action)
    signup = site.get("signup", {}).get("mode", "none")
    add("signup", "todo" if absent else "fail" if error else "ok" if signup == "brevo_hosted" else "todo",
        "Content absent" if absent else error or signup,
        INIT if absent else "Fix content validation errors" if error else None if signup == "brevo_hosted"
        else "Decide on signup (docs/NEWSLETTER.md); `none` is allowed")
    count = len(model["findings"]) if model else 0
    add("published_findings", "todo" if absent else "fail" if error else "ok" if count else "todo",
        "Content absent" if absent else error or str(count),
        INIT if absent else "Fix content validation errors" if error else None if count
        else "Publish a finding through the existing review process")
    try:
        problems = evidence_report(root)["problems"]
        add("evidence", "fail" if problems else "ok", len(problems),
            "Register and ingest the cited evidence" if problems else None)
    except (ValueError, OSError) as exc:
        add("evidence", "fail", exc, "Fix evidence inputs and run evidence check")
    if absent:
        for name in ("build_check", "links"):
            add(name, "todo", "Content absent", INIT)
    else:
        scratch = None
        try:
            scratch = Path(tempfile.mkdtemp(prefix="campaign-launch-"))
            shutil.copy2(root / "campaign.json", scratch / "campaign.json")
            shutil.copytree(root / "content", scratch / "content", symlinks=True)
            for name in ("agencies.json", "governing-bodies.json"):
                source = root / "kit" / name
                if source.exists():
                    (scratch / "kit").mkdir(exist_ok=True)
                    shutil.copy2(source, scratch / "kit" / name, follow_symlinks=False)
            build_site(scratch)
            hits = check_public_tree(scratch / "public")
            add("build_check", "fail" if hits else "ok", len(hits),
                "Fix public-tree scan findings and run build --check" if hits else None)
            broken = check_links(scratch / "public")
            add("links", "fail" if broken else "ok", len(broken),
                "Fix broken internal links and run build --check" if broken else None)
        except (ValueError, OSError) as exc:
            if not any(row["check"] == "build_check" for row in rows):
                add("build_check", "fail", exc, "Fix content errors and run build --check")
            add("links", "fail", "Temporary build could not be checked", "Fix the temporary build and recheck links")
        finally:
            if scratch is not None:
                shutil.rmtree(scratch)
    try:
        stale = stale_contacts(root, contact_max_age_days, today)
        path = root / "kit" / "agencies.json"
        agencies = json.loads(path.read_text()) if path.exists() else []
        verified = isinstance(agencies, list) and any(
            isinstance(a, dict) and a.get("selected") is True and a.get("verified") is True for a in agencies)
        if stale is None:
            raise ValueError("kit/agencies.json is unreadable")
        add("verified_contacts", "todo" if stale or not verified else "ok",
            f"{len(stale)} stale; verified selected contact {'present' if verified else 'absent'}",
            "Re-check selected agency contacts on official sites (kit-verify-agency)" if stale or not verified else None)
    except (ValueError, OSError) as exc:
        add("verified_contacts", "fail", exc, "Fix kit/agencies.json and verify selected contacts")
    law = law_package_report(read_config(root))
    status = law["law_package_status"]
    add("law_package", "ok" if status == "reviewed" else "todo" if status == "draft" else "fail", status,
        None if status == "reviewed" else "Law package is a draft; dates stay provisional" if status == "draft"
        else "Obtain an independently reviewed jurisdiction law package")
    return dict(checks=rows, passed=sum(row["state"] == "ok" for row in rows), total=len(rows), note=NOTE)
