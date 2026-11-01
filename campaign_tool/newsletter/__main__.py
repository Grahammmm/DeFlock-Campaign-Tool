"""``python3 -m campaign_tool.newsletter --directory D [--manifest FILE | --from-content [--since YYYY-MM-DD] | --record-sent YYYY-MM-DD]``.

Builds kit/newsletter-draft.{html,txt,json}. Without either flag the draft
uses the campaign name and an empty findings list, which is useful to check
the template. ``--record-sent`` records that the current draft was sent by
hand on that day (kit/newsletter-sent.json); ``--from-content`` without
``--since`` then starts the day after the last recorded send. Nothing is sent.
"""
import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

from ..cli import read_config
from ..content import ContentError
from .draft import DraftError, build_draft, load_manifest, write_draft
from .from_content import manifest_from_content
from .sent import last_sent, record_sent


def _day(value):  # YYYY-MM-DD only; argparse turns the ValueError into exit 2
    if date.fromisoformat(value).isoformat() != value:
        raise ValueError(value)
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--manifest", help="Newsletter manifest JSON (from the workspace newsletter_draft job)")
    source.add_argument("--from-content", action="store_true", help="Build the manifest from content/")
    source.add_argument("--record-sent", metavar="DATE",
                        help="Record that kit/newsletter-draft was sent by hand on this YYYY-MM-DD day; sends nothing")
    parser.add_argument("--since", type=_day, help="With --from-content: findings published on or after this UTC day")
    args = parser.parse_args(argv)
    if args.since and not args.from_content:
        parser.error("--since needs --from-content")
    origin = "manifest" if args.manifest else "content" if args.from_content else "template"
    root = Path(args.directory).expanduser().resolve()
    since_from = None
    try:
        cfg = read_config(root)
        if args.record_sent is not None:
            print(json.dumps({"recorded": record_sent(root, args.record_sent), "sent": False}, indent=2))
            return 0
        if args.manifest:
            manifest = load_manifest(args.manifest)
        elif args.from_content:
            since, since_from = args.since, "flag" if args.since else None
            previous = None if since else last_sent(root)
            if previous:
                since, since_from = (date.fromisoformat(previous) + timedelta(days=1)).isoformat(), "record"
            manifest = manifest_from_content(root, cfg, since and since + "T00:00:00+00:00")
        else:
            manifest = {"schema_version": 1, "campaign": {"name": cfg["name"], "base_url": None,
                                                          "county_name": cfg["county"]},
                        "since": None, "findings": [], "meetings": []}
        draft = build_draft(manifest)
        paths = write_draft(root, draft)
    except (OSError, ValueError, DraftError, ContentError, KeyError) as exc:
        print("Stopped: " + str(exc), file=sys.stderr)
        return 1
    for note in draft["review_flags"]:  # DF-4 alert-only: stderr, so the stdout contract is unchanged; exit stays 0
        print(note, file=sys.stderr)
    print(json.dumps({"subject": draft["subject"], "counts": draft["counts"], "excluded": draft["excluded"],
                      "written": [str(p) for p in paths], "sent": False, "source": origin,
                      "since_from": since_from}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
