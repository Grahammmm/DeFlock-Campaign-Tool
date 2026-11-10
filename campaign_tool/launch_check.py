"""Read-only offline launch checklist; no deployment or approval inference.

The temporary rehearsal build is staged from a campaign's validated inputs. Local rules are projected from the model
that ``load_content`` already admitted (see ``_local_rules_bytes``), so a finding citing an organizer-transcribed local
rule rehearses like the real build, without re-reading or copying the campaign's ``law/`` folder.
"""
import json
from pathlib import Path
import re
import shutil
import tempfile

from . import law
from .content import load_content
from .evidence import evidence_report
from .finding_law import jurisdiction
from .kit import stale_contacts
from .law import LOCAL_RULES_FILE
from .site import build_site
from .site_check import check_links

INIT = "Run content init (DF2237-06) or create content/"
# A caught OSError (including shutil.Error from staging) carries filenames, absolute source/destination paths and OS
# text. Its arguments are never inspected or rendered: each stage reports one fixed message instead, so local paths
# cannot reach the checklist JSON. ValueError/ContentError messages stay as validators wrote them.
LOAD_FS = "Content files could not be read (filesystem error)"
EVIDENCE_FS = "Evidence inputs could not be read (filesystem error)"
BUILD_FS = "Temporary build could not be checked (filesystem error)"
CONTACTS_FS = "kit/agencies.json could not be read (filesystem error)"
NOTE = ("Offline readiness only. This is not a launch approval; deployment "
        "(docs/CLOUDFLARE.md) and human review remain.")


def _local_rules_bytes(rules, jurisdiction, limit=None):
    """Compact UTF-8 ``law/local-rules.json`` text projecting the validated ``rules``; refuses past the reader's cap.

    ``rules`` is the accepted ``model["local_rules"]`` snapshot (every validated version and field), re-serialized as a
    semantic projection and never re-read or copied from the campaign's ``law/`` folder, so a sibling file or link there
    cannot reach the rehearsal tree. Original file bytes and whitespace are deliberately not preserved.
    """
    limit = law.LOCAL_RULES_MAX_BYTES if limit is None else limit
    document = dict(schema_version=1, jurisdiction=jurisdiction, rules=rules)
    data = json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(data) > limit:
        raise ValueError(f"{law.LOCAL_RULES_FILE}: rehearsal copy larger than {limit} bytes")
    return data


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
    except OSError:
        error = LOAD_FS
    except ValueError as exc:
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
    except OSError:
        add("evidence", "fail", EVIDENCE_FS, "Fix evidence inputs and run evidence check")
    except ValueError as exc:
        add("evidence", "fail", exc, "Fix evidence inputs and run evidence check")
    if absent:
        for name in ("build_check", "links"):
            add(name, "todo", "Content absent", INIT)
    else:
        scratch = None
        try:
            if error is not None:
                # The original admission step refused the campaign (including a malformed, linked or oversized
                # law/local-rules.json); never let a scratch tree without that input report a successful build.
                raise ValueError(error)
            scratch = Path(tempfile.mkdtemp(prefix="campaign-launch-"))
            shutil.copy2(root / "campaign.json", scratch / "campaign.json")
            shutil.copytree(root / "content", scratch / "content", symlinks=True)
            rules = model["local_rules"] if model else []
            if rules:  # none (absent or empty overlay): law/ stays absent, the same empty model
                data = _local_rules_bytes(rules, jurisdiction(scratch))
                (scratch / "law").mkdir()
                (scratch / LOCAL_RULES_FILE).write_bytes(data)
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
                add("build_check", "fail", BUILD_FS if isinstance(exc, OSError) else exc,
                    "Fix content errors and run build --check")
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
    except OSError:
        add("verified_contacts", "fail", CONTACTS_FS, "Fix kit/agencies.json and verify selected contacts")
    except ValueError as exc:
        add("verified_contacts", "fail", exc, "Fix kit/agencies.json and verify selected contacts")
    law = law_package_report(read_config(root))
    status = law["law_package_status"]
    add("law_package", "ok" if status == "reviewed" else "todo" if status == "draft" else "fail", status,
        None if status == "reviewed" else "Law package is a draft; dates stay provisional" if status == "draft"
        else "Obtain an independently reviewed jurisdiction law package")
    return dict(checks=rows, passed=sum(row["state"] == "ok" for row in rows), total=len(rows), note=NOTE)
