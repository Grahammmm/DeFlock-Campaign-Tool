"""records project-init: create an empty private records project for an existing campaign.

Writes only the project tree (0700), campaign.json (0600), the .campaign-root pointer (0600), an empty ledger and an
empty portal queue. No mailbox, credential, target, send or service is configured.
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path

from . import campaign_config
from .ledger import store as ledger_store
from .portal.store import Queue
from .run_stages import CAMPAIGN_ROOT_POINTER

_ID = re.compile(r"[a-z0-9-]{2,40}")


class Refused(Exception):
    pass


def _campaign(root):
    from ..cli import read_config
    try:
        return read_config(Path(root))
    except (OSError, ValueError):
        raise Refused("not_a_campaign") from None


def _slug(config):
    text = re.sub(r"[^a-z0-9]+", "-", ("%s-%s" % (config["county"], config["state"])).lower()).strip("-")
    return text[:40] if len(text) >= 2 else "campaign"


def _write(path, text):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        out.write(text)


def init_project(project, campaign_root, campaign_id=None):
    root = os.path.abspath(campaign_root)
    config = _campaign(root)
    campaign_id = campaign_id or _slug(config)
    if not _ID.fullmatch(campaign_id):
        raise Refused("campaign_id_invalid")
    project = Path(os.path.abspath(project))
    if os.path.lexists(project) and (project.is_symlink() or not project.is_dir() or any(project.iterdir())):
        raise Refused("project_exists")
    if project.is_symlink() or any(p.is_symlink() for p in project.parents):
        raise Refused("project_path_symlink")
    document = {"schema_version": 1, "campaign_id": campaign_id,
                "display_name": config["name"].strip()[:80], "law_package": "us-%s" % config["state"].lower()}
    project.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(project, 0o700)
    _write(project / "campaign.json", json.dumps(document, indent=2) + "\n")
    try:
        campaign_config.load(project)
    except ValueError:
        (project / "campaign.json").unlink()
        raise Refused("campaign_config_invalid") from None
    _write(project / CAMPAIGN_ROOT_POINTER, root + "\n")
    ledger_store.initialize(project / "processing-v1" / "ledger.sqlite")
    Queue(str(project / "portal-queue-v1")).close()
    return document


def main(argv=None):
    parser = argparse.ArgumentParser(prog="records project-init", description=__doc__.split("\n")[0])
    parser.add_argument("--project", required=True, help="new private project directory (must not exist or be empty)")
    parser.add_argument("--campaign-root", required=True, help="existing campaign created by campaign_tool init")
    parser.add_argument("--campaign-id", help="id for campaign.json (default: county and state)")
    args = parser.parse_args(argv)
    try:
        init_project(args.project, args.campaign_root, args.campaign_id)
    except Refused as error:
        print("records project-init: refused (%s)" % error, file=sys.stderr)
        return 2
    print("Project ready: %s\nNext: render a TEST target for this campaign, then run records e2e-proof with "
          "--campaign-root %s. No mailbox or credentials were configured." % (args.project, args.campaign_root))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
