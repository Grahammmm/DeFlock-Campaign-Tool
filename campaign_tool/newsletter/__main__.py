"""``python3 -m campaign_tool.newsletter --directory D [--manifest FILE]``.

Builds kit/newsletter-draft.{html,txt,json}. Without ``--manifest`` the draft
uses the campaign name and an empty findings list, which is useful to check
the template. Nothing is sent.
"""
import argparse
import json
import sys
from pathlib import Path

from ..cli import read_config
from .draft import DraftError, build_draft, load_manifest, write_draft


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True)
    parser.add_argument("--manifest", help="Newsletter manifest JSON (from the workspace newsletter_draft job)")
    args = parser.parse_args(argv)
    root = Path(args.directory).expanduser().resolve()
    try:
        cfg = read_config(root)
        if args.manifest:
            manifest = load_manifest(args.manifest)
        else:
            manifest = {"schema_version": 1, "campaign": {"name": cfg["name"], "base_url": None,
                                                          "county_name": cfg["county"]},
                        "since": None, "findings": [], "meetings": []}
        draft = build_draft(manifest)
        paths = write_draft(root, draft)
    except (OSError, ValueError, DraftError, KeyError) as exc:
        print("Stopped: " + str(exc), file=sys.stderr)
        return 1
    print(json.dumps({"subject": draft["subject"], "counts": draft["counts"],
                      "written": [str(p) for p in paths], "sent": False}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
