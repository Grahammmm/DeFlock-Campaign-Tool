"""``python3 -m campaign_tool.newsletter --directory D [--manifest FILE | --from-content [--since YYYY-MM-DD] | --record-sent YYYY-MM-DD]``,
``... --directory D sent --date YYYY-MM-DD`` (same as ``--record-sent``) or ``... --directory D archive --date YYYY-MM-DD [--publish]``.

Builds kit/newsletter-draft.{html,txt,json}. Without either flag the draft
uses the campaign name and an empty findings list, which is useful to check
the template. ``--record-sent`` records that the current draft was sent by
hand on that day (kit/newsletter-sent.json); ``--from-content`` without
``--since`` then starts the day after the last recorded send. Recording also keeps the sent draft's exact bytes as
kit/newsletter-issues/DATE.json (private, never overwritten). ``archive --date`` checks that issue's kept copy and
public projection; ``--publish`` then writes content/newsletters/DATE.json (a reviewable local file, not hosting).
Nothing is sent.
"""
import argparse
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .. import registry
from ..cli import _campaign_option, read_config
from ..content import ContentError
from ..site_check import resolve
from .archive import publish_issue
from .check import check_draft, report_json, report_text
from .draft import DraftError, build_draft, load_manifest, write_draft
from .from_content import manifest_from_content
from .sent import last_sent, record_sent
from .site_links import external_links, site_links


def _day(value):  # YYYY-MM-DD only; argparse turns the ValueError into exit 2
    if date.fromisoformat(value).isoformat() != value:
        raise ValueError(value)
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", nargs="?", choices=("sent", "archive"),
                        help="sent: keep the draft as a sent issue; archive: check (or --publish) its public copy")
    parser.add_argument("--directory", required=True)
    parser.add_argument("--date", type=_day, help="With sent or archive: the YYYY-MM-DD day the issue was sent")
    parser.add_argument("--publish", action="store_true",
                        help="With archive: write content/newsletters/DATE.json locally (never replaces; hosts nothing)")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--manifest", help="Newsletter manifest JSON (from the workspace newsletter_draft job)")
    source.add_argument("--from-content", action="store_true", help="Build the manifest from content/")
    source.add_argument("--record-sent", metavar="DATE",
                        help="Record that kit/newsletter-draft was sent by hand on this YYYY-MM-DD day; sends nothing")
    parser.add_argument("--since", type=_day, help="With --from-content: findings published on or after this UTC day")
    parser.add_argument("--site", metavar="PUBLIC", help="Built public/ folder; every campaign-site link must reach it")
    parser.add_argument("--check", action="store_true",
                        help="Read-only: does kit/newsletter-draft.json still match current content? Takes no other flag")
    parser.add_argument("--json", action="store_true", help="With --check: print the report as JSON")
    _campaign_option(parser)  # DF1109-15: --campaign NAME, exactly as every main command that takes --directory
    args = parser.parse_args(argv)
    if getattr(args, "campaign", None):  # confined to that campaign, as cli.main does
        registry.confine(args.directory, args.registered)
    if args.json and not args.check:
        parser.error("--json belongs to --check")
    if args.check:
        used = [flag for flag, value in (("action", args.action), ("--manifest", args.manifest),
                                         ("--from-content", args.from_content), ("--record-sent", args.record_sent),
                                         ("--since", args.since), ("--site", args.site), ("--date", args.date),
                                         ("--publish", args.publish)) if value]
        if used:
            parser.error("--check takes no other flag or action (found " + ", ".join(used) + ")")
        root = Path(args.directory).expanduser().resolve()
        try:
            report = check_draft(root)
        except (OSError, ValueError, KeyError, ContentError) as exc:  # CheckError is a ValueError
            print("Stopped: " + str(exc), file=sys.stderr)
            return 1
        print(report_json(report) if args.json else report_text(report))
        return 0 if report["status"] == "match" else 3
    if args.action is None and (args.date is not None or args.publish):
        parser.error("--date and --publish need the sent or archive action")
    if args.action is not None:
        if args.manifest or args.from_content or args.record_sent is not None or args.since or args.site is not None:
            parser.error(f"{args.action} does not take drafting flags (--manifest, --from-content, --record-sent, --since, --site)")
        if args.date is None:
            parser.error(f"{args.action} needs --date YYYY-MM-DD")
        if args.publish and args.action != "archive":
            parser.error("--publish belongs to archive")
    if args.since and not args.from_content:
        parser.error("--since needs --from-content")
    if args.site is not None and args.record_sent is not None:
        parser.error("--site checks a draft; not with --record-sent")
    origin = "manifest" if args.manifest else "content" if args.from_content else "template"
    root = Path(args.directory).expanduser().resolve()
    since_from = None
    now = datetime.now(timezone.utc)  # DF1107-09: one instant for the manifest and the draft
    try:
        if args.site is not None and (Path(args.site).is_symlink() or not Path(args.site).is_dir()):
            raise ValueError(f"--site {args.site}: must be an existing folder, not a file or symlink")
        cfg = read_config(root)
        if args.record_sent is not None or args.action == "sent":
            print(json.dumps({"recorded": record_sent(root, args.date or args.record_sent), "sent": False}, indent=2))
            return 0
        if args.action == "archive":
            print(json.dumps({**publish_issue(root, args.date, publish=args.publish), "sent": False}, indent=2))
            return 0
        if args.manifest:
            manifest = load_manifest(args.manifest)
        elif args.from_content:
            since, since_from = args.since, "flag" if args.since else None
            previous = None if since else last_sent(root)
            if previous:
                since, since_from = (date.fromisoformat(previous) + timedelta(days=1)).isoformat(), "record"
            manifest = manifest_from_content(root, cfg, since and since + "T00:00:00+00:00", now=now)
        else:
            manifest = {"schema_version": 1, "campaign": {"name": cfg["name"], "base_url": None,
                                                          "county_name": cfg["county"]},
                        "since": None, "findings": [], "meetings": []}
        draft = build_draft(manifest, now, archive_public=args.from_content, provenance=args.from_content)
        paths = write_draft(root, draft)
    except (OSError, ValueError, DraftError, ContentError, KeyError) as exc:
        print("Stopped: " + str(exc), file=sys.stderr)
        return 1
    for note in draft.get("review_flags", []):  # DF-4 alert-only: stderr, so the stdout contract is unchanged; exit stays 0
        print(note, file=sys.stderr)
    out = {"subject": draft["subject"], "counts": draft["counts"], "excluded": draft["excluded"],
           "written": [str(p) for p in paths], "sent": False, "source": origin, "since_from": since_from}
    if args.site is not None:
        base_url = (manifest.get("campaign") or {}).get("base_url")  # same normalisation as build_draft: "campaign": null is allowed
        links = site_links(draft, base_url)
        missing = [{"link": link, "reason": reason} for link in links if (reason := resolve(Path(args.site), link))]
        for item in missing:
            print(f"newsletter: link {item['link']} not in {args.site} ({item['reason']})", file=sys.stderr)
        out["site_links"] = {"public": args.site, "checked": len(links),
                             "external_skipped": len(external_links(draft, base_url)), "missing": missing}
    print(json.dumps(out, indent=2))
    return 3 if out.get("site_links", {}).get("missing") else 0


if __name__ == "__main__":
    sys.exit(main())
