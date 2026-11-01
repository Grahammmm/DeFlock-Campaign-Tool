"""``python3 -m campaign_tool.newsletter --directory D [--manifest FILE | --from-content [--since YYYY-MM-DD]]``.

Builds kit/newsletter-draft.{html,txt,json}. ``--from-content`` builds the
manifest from the campaign's own content/ (published findings, upcoming
meetings). Without either the draft uses the campaign name and an empty
findings list, which is useful to check the template. Nothing is sent.
"""
import argparse
import json
import sys
from datetime import date
from pathlib import Path

from ..cli import read_config
from ..content import ContentError
from .draft import DraftError, build_draft, load_manifest, write_draft
from .from_content import manifest_from_content


def _day(value):
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        parsed = None
    if parsed is None or parsed.isoformat() != value:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got {value!r}")
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--manifest", help="Newsletter manifest JSON (from the workspace newsletter_draft job)")
    source.add_argument("--from-content", action="store_true", help="Build the manifest from content/")
    parser.add_argument("--since", type=_day, help="With --from-content: findings published on or after this UTC day")
    args = parser.parse_args(argv)
    if args.since and not args.from_content:
        parser.error("--since needs --from-content")
    root = Path(args.directory).expanduser().resolve()
    try:
        cfg = read_config(root)
        if args.manifest:
            manifest, origin = load_manifest(args.manifest), "manifest"
        elif args.from_content:
            since = args.since + "T00:00:00+00:00" if args.since else None
            manifest, origin = manifest_from_content(root, cfg, since), "content"
        else:
            manifest = {"schema_version": 1, "campaign": {"name": cfg["name"], "base_url": None,
                                                          "county_name": cfg["county"]},
                        "since": None, "findings": [], "meetings": []}
            origin = "template"
        draft = build_draft(manifest)
        paths = write_draft(root, draft)
    except (OSError, ValueError, DraftError, ContentError, KeyError) as exc:
        print("Stopped: " + str(exc), file=sys.stderr)
        return 1
    print(json.dumps({"subject": draft["subject"], "counts": draft["counts"], "excluded": draft["excluded"],
                      "written": [str(p) for p in paths], "sent": False, "source": origin}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
